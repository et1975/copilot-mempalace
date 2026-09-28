"""Bounded external host supervisor over registered managed-process profiles.

step() is deterministic with an injected authority clock. A separate per-process
watchdog enforces the last CONFIRMED authorization during blocked network calls.
elapsed_time supplies seconds on one shared deadline basis; by default it is
suspend-inclusive continuous time. A clock fault latches until a new supervisor
is constructed; stopping owned processes alone does not reconcile their effects.
Native hosts need a real identity/stop/isolation/reconciliation adapter; native
Copilot cancellation or /fleet integration is deliberately not advertised.
"""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import ready_tasks, task_eligibility
from .execution import (
    DeadlineClock, ExecutionError, LocalProfile, OwnedProcess, Workspace,
    read_checkpoint, read_result, require_process_supervision,
)
from .journal import JsonStore
from .maintenance import MutationRequests, error_record, execution_tokens, instant
from .platform_support import continuous_time_ns


def _continuous_seconds():
    return continuous_time_ns() / 1_000_000_000


@dataclass
class Worker:
    task_id: str
    attempt_id: str
    generation: int
    profile: LocalProfile
    confirmed_lease: datetime
    next_renew: datetime
    workspace: Workspace | None = None
    process: OwnedProcess | None = None
    phase: str = "preparing"
    pending: dict | None = None
    purpose: str | None = None
    result: dict | None = None
    failure: str | None = None
    quiescence_unknown: bool = False
    pending_receipt: dict | None = None


class HostSupervisor:
    def __init__(self, authority, clock, maintenance, *, supervisor_id, worker_id,
                 profiles, pool_size=1, filters=None, stop_margin_seconds=5,
                 elapsed_time: Callable[[], float] | None = None):
        if type(pool_size) is not int or not 1 <= pool_size <= 100:
            raise ValueError("pool_size must be in 1..100")
        if type(stop_margin_seconds) not in {int, float} or not 3 <= stop_margin_seconds <= 60:
            raise ValueError("stop margin must be 3..60 seconds (includes bounded group stop)")
        self.profiles = {p.name: p for p in profiles}
        if not self.profiles or len(self.profiles) != len(profiles) or len(profiles) > 32:
            raise ValueError("Use 1..32 distinct registered profiles")
        self.filters = copy.deepcopy(filters or {})
        allowed = {"project", "goal_id", "priority_ceiling", "execution_profile", "task_ids",
                   "resource_keys"}
        if set(self.filters) - allowed:
            raise ValueError("Unknown supervisor filter")
        for field in ("task_ids", "resource_keys"):
            if field in self.filters and (
                    not isinstance(self.filters[field], list)
                    or any(not isinstance(value, str) or not value for value in self.filters[field])):
                raise ValueError("Task/resource filters must be lists of nonempty strings")
        require_process_supervision()
        source = _continuous_seconds if elapsed_time is None else elapsed_time
        self.elapsed_time = source if isinstance(source, DeadlineClock) else DeadlineClock(source)
        self.elapsed_time()
        with authority.serialized():
            config = authority.state.configuration or {}
            supervisor = config.get("supervisors", {}).get(supervisor_id, {})
            if worker_id not in supervisor.get("workers", []):
                raise ValueError("Worker must be registered to this supervisor")
            for name, profile in self.profiles.items():
                declared = config.get("execution_profiles", {}).get(name)
                if (name not in supervisor.get("profiles", []) or declared is None
                        or not declared["available"]
                        or declared["execution_class"] != profile.execution_class):
                    raise ValueError("Implemented profile must match genesis and supervisor registration")
            self.sweep_seconds = config["policy"]["sweep_seconds"]
            ready_tasks(authority.state, {
                k: v for k, v in self.filters.items() if k not in {"task_ids", "resource_keys"}},
                clock.now())
        self.authority, self.clock, self.maintenance = authority, clock, maintenance
        self.supervisor_id, self.worker_id = supervisor_id, worker_id
        self.pool_size, self.stop_margin = pool_size, stop_margin_seconds
        self.requests = MutationRequests(authority)
        self.workers: dict[str, Worker] = {}
        self._recoveries: dict[tuple[str, str, int], Worker] = {}
        self._claims = {}
        self._claim_receipts = {}
        self._next_sweep = None
        self._last = None
        self._peak = 0
        self._closing = False
        self._lock = threading.RLock()

    def _view(self):
        with self.authority.serialized():
            # Persistence latency must consume, never extend, the execution window.
            elapsed_anchor = self.elapsed_time()
            state, now = self.authority.state, instant(self.clock.now())
            health = self.authority.health()
            return state, now, health["startup_pending"], elapsed_anchor, health

    @staticmethod
    def _current(health):
        return (health.get("fresh") is True and health.get("pending_command") is None
                and health.get("startup_pending") is False and health.get("error") is None)

    def _require_current(self, health, result):
        if self._current(health):
            return True
        result["errors"].append({"operation": "authorization", "code": "authority_not_current",
                                 "message": health.get("reason") or "Authority freshness is unverified"})
        return False

    @staticmethod
    def _matches(worker, task):
        return (task is not None and task["attempt"] is not None
                and task["attempt"]["id"] == worker.attempt_id
                and task["claim_generation"] == worker.generation)

    @staticmethod
    def _worker_key(worker):
        return worker.task_id, worker.attempt_id, worker.generation

    def _tracked(self, worker):
        return (self.workers.get(worker.task_id) is worker
                or self._recoveries.get(self._worker_key(worker)) is worker)

    @staticmethod
    def _receipt_task(receipt, task_id):
        task = next((task for task in receipt.get("tasks", []) if task["id"] == task_id), None)
        if task is None:
            raise ExecutionError("invalid_receipt", "Confirmed receipt is missing the original task")
        return task

    @staticmethod
    def _lease_limit(task, original):
        return min(instant(value) for snapshot in (task, original) for value in (
            snapshot["lease_expires_at"], snapshot["attempt"]["progress_deadline"],
            snapshot["attempt"]["hard_deadline"]))

    def _confirmed(self, worker, task, now, elapsed_anchor, receipt, health):
        if not self._current(health):
            raise ExecutionError("authority_not_current", "Unverified projection cannot authorize execution")
        original = self._receipt_task(receipt, worker.task_id)
        if (not self._matches(worker, task) or not self._matches(worker, original)
                or task["status"] != "in_progress" or original["status"] != "in_progress"
                or task["attempt"]["status"] != "running" or original["attempt"]["status"] != "running"
                or task["attempt"]["supervisor_id"] != self.supervisor_id
                or task["attempt"]["owner"] != self.worker_id
                or original["attempt"]["supervisor_id"] != self.supervisor_id
                or original["attempt"]["owner"] != self.worker_id):
            raise ExecutionError("stale_generation", "No current owned execution authorization")
        until = self._lease_limit(task, original)
        cutoff = elapsed_anchor + (until - now).total_seconds() - self.stop_margin
        if worker.process:
            worker.process.confirm_deadline(cutoff)
        worker.confirmed_lease = until
        worker.next_renew = now + timedelta(seconds=task["policy"]["renewal_seconds"])
        return cutoff

    def _stop(self, worker):
        if worker.process is not None:
            worker.process.stop()

    def _stop_due(self, now, startup_pending, result):
        for worker in list(self.workers.values()):
            if (startup_pending or now >= worker.confirmed_lease - timedelta(seconds=self.stop_margin)
                    or (worker.process is not None
                        and (worker.process.deadline_expired or worker.process.error is not None))):
                try:
                    self._stop(worker)
                    worker.failure = worker.failure or "confirmed_authorization_ended"
                except Exception as error:
                    result["errors"].append(error_record(error, "stop"))

    def _submit(self, worker, command, purpose, result):
        worker.pending, worker.purpose = command, purpose
        return self._resume(worker, result)

    def _resume(self, worker, result):
        if worker.pending_receipt is None:
            outcome, receipt = self.requests.submit(worker.pending)
            if outcome == "committed":
                worker.pending_receipt = copy.deepcopy(receipt)
        else:
            outcome, receipt = "committed", worker.pending_receipt
        if outcome == "unknown":
            result["errors"].append({"operation": worker.purpose, "code": "outcome_unknown",
                                     "message": "Mutation remains unconfirmed"})
            return False
        if outcome == "committed":
            with self.authority.serialized():
                return self._accept_receipt(worker, receipt, result)
        worker.pending, worker.purpose, worker.pending_receipt = None, None, None
        if outcome == "raced":
            result["raced"] += 1
            return False
        if outcome == "abandoned":
            result["abandoned"] += 1
            return False
        if outcome == "error":
            result["errors"].append(receipt)
            worker.failure = receipt.get("code", "mutation_failed")
            self._stop(worker)
            return False
        return False

    def _accept_receipt(self, worker, receipt, result):
        state, now, startup_pending, elapsed_anchor, health = self._view()
        if not self._require_current(health, result):
            return False
        purpose = worker.purpose
        worker.pending, worker.purpose, worker.pending_receipt = None, None, None
        task = state.tasks.get(worker.task_id)
        if purpose in {"started", "renew"}:
            cutoff = None
            if not startup_pending and self._matches(worker, task) and task["status"] == "in_progress":
                cutoff = self._confirmed(worker, task, now, elapsed_anchor, receipt, health)
            if purpose == "started":
                worker.phase = "running"
                if (not self._closing and not startup_pending and not worker.failure
                        and self._matches(worker, task) and task["status"] == "in_progress"
                        and task["attempt"]["status"] == "running"
                        and cutoff is not None
                        and worker.confirmed_lease > now + timedelta(seconds=self.stop_margin)):
                    worker.quiescence_unknown = True
                    worker.process = OwnedProcess(
                        worker.profile, worker.workspace, deadline=cutoff, elapsed_time=self.elapsed_time)
                    worker.quiescence_unknown = False
                    result["started"] += 1
                else:
                    worker.failure = worker.failure or "start_authorization_unavailable"
            else:
                result["renewed"] += 1
        elif purpose == "checkpoint":
            result["checkpoints"] += 1
        elif purpose == "settled":
            worker.phase = "closing"
        elif purpose in {"failed", "release"}:
            worker.phase = "recovering"
            if purpose == "failed":
                result["failed"] += 1
        elif purpose in {"closed", "recover"}:
            if purpose == "closed":
                result["completed"] += 1
            self._retire(worker)
        return True

    def _retire(self, worker):
        self._stop(worker)
        if self.workers.get(worker.task_id) is worker:
            del self.workers[worker.task_id]
        key = self._worker_key(worker)
        if self._recoveries.get(key) is worker:
            del self._recoveries[key]

    def _recover(self, worker, task, state, result):
        self._stop(worker)
        recovery = task["recovery"]
        if recovery is None or recovery["barrier_satisfied"]:
            self._retire(worker)
            return
        if task["execution_class"] == "isolated":
            proof = {"references": [
                f"mptask://{state.authority_id}/tasks/{task['id']}/attempts/"
                f"{task['attempt']['id']}/lease-revisions/{task['lease_revision']}"],
                "publication_revoked": True}
        else:
            if worker.workspace is None:
                raise ExecutionError("reconciliation_required", "No prepared workspace; operator recovery required")
            required = {key: state.resources[key]["counter"] + 1 for key in task["resource_keys"]}
            proof = worker.profile.recovery(
                task, worker.workspace, process_stopped=not worker.quiescence_unknown
                and (worker.process is None or worker.process.stopped),
                required_fences=required)
        recovery_actor = (self.maintenance.recovery_actor if task["execution_class"] == "isolated"
                          else self.supervisor_id)
        self._submit(worker, {"operation": "recover", "actor": recovery_actor,
                             "task_id": task["id"], "expected_version": task["version"],
                             "retry_decision": "preserve", "evidence": proof}, "recover", result)

    def _progress(self, worker, task, checkpoint, result):
        if checkpoint is None:
            return None
        previous = task["attempt"]["checkpoint"]
        sequence, reference = checkpoint["sequence"], checkpoint["reference"]
        if previous is not None:
            if sequence == previous["sequence"] and reference == previous["reference"]:
                return None
            if sequence <= previous["sequence"] or reference == previous["reference"]:
                raise ExecutionError("invalid_checkpoint", "Useful progress requires a new sequence and reference")
        JsonStore(worker.workspace.root / "runtime" / f"accepted-checkpoint-{sequence}.json").write(checkpoint)
        return self._submit(worker, {"operation": "checkpoint", "actor": self.supervisor_id,
                                    **execution_tokens(task), "checkpoint_sequence": sequence,
                                    "reference": reference}, "checkpoint", result)

    def _advance(self, worker, result):
        try:
            for _ in range(5):
                if not self._tracked(worker):
                    return
                if worker.pending and not self._resume(worker, result):
                    return
                if not self._tracked(worker):
                    return
                state, now, startup_pending, _, health = self._view()
                if not self._require_current(health, result):
                    return
                task = state.tasks.get(worker.task_id)
                if not self._matches(worker, task):
                    self._retire(worker)
                    return
                if task["status"] != "in_progress":
                    if task["recovery"] is not None:
                        self._recover(worker, task, state, result)
                    else:
                        self._retire(worker)
                    return
                if startup_pending:
                    self._stop(worker)
                    return
                if worker.failure:
                    self._stop(worker)
                    if now >= min(instant(task["lease_expires_at"]),
                                  instant(task["attempt"]["progress_deadline"]),
                                  instant(task["attempt"]["hard_deadline"])):
                        return  # Only independent maintenance may expire revoked authorization.
                    reference = (worker.workspace.input_path.as_uri() if worker.workspace else
                                 f"mptask://{state.authority_id}/tasks/{task['id']}")
                    if not self._submit(worker, {
                        "operation": "attempt_report", "actor": self.supervisor_id,
                        **execution_tokens(task), "report_kind": "failed",
                        "reason": worker.failure, "evidence": {"references": [reference]},
                    }, "failed", result):
                        return
                    continue
                if worker.phase == "preparing":
                    if worker.workspace is None:
                        worker.workspace = worker.profile.prepare(task)
                    if not self._submit(worker, {
                        "operation": "attempt_report", "actor": self.supervisor_id,
                        **execution_tokens(task), "report_kind": "started",
                        "evidence": worker.workspace.evidence,
                    }, "started", result):
                        return
                    continue
                if worker.phase == "closing":
                    accepted = worker.workspace.root / "runtime" / "accepted-result.json"
                    self._submit(worker, {
                        "operation": "transition", "actor": self.worker_id, **execution_tokens(task),
                        "target": "closed", "summary": worker.result["summary"],
                        "evidence": [accepted.as_uri()] + worker.result["evidence"][:19],
                    }, "closed", result)
                    return
                exit_code = worker.process.observe() if worker.process else None
                if exit_code is not None:
                    self._stop(worker)
                    if exit_code != 0:
                        worker.failure = "worker_exit_nonzero"
                        continue
                    worker.result = worker.result or read_result(worker.workspace, task)
                    if worker.result is None:
                        worker.failure = "missing_result"
                        continue
                    progress = self._progress(worker, task, worker.result.get("checkpoint"), result)
                    if progress is False:
                        return
                    if progress is True:
                        continue
                    JsonStore(worker.workspace.root / "runtime" / "accepted-result.json").write(worker.result)
                    proof = worker.profile.settlement(task, worker.workspace, process_stopped=True)
                    if not self._submit(worker, {
                        "operation": "attempt_report", "actor": self.supervisor_id,
                        **execution_tokens(task), "report_kind": "settled", "evidence": proof,
                    }, "settled", result):
                        return
                    continue
                progress = self._progress(worker, task, read_checkpoint(worker.workspace, task), result)
                if progress is False:
                    return
                if progress is True:
                    state, now, startup_pending, _, health = self._view()
                    if not self._require_current(health, result):
                        return
                    task = state.tasks.get(worker.task_id)
                    if startup_pending or not self._matches(worker, task) or task["status"] != "in_progress":
                        continue
                if now >= worker.next_renew:
                    self._submit(worker, {
                        "operation": "renew", "actor": self.supervisor_id, "task_id": task["id"],
                        "attempt_id": worker.attempt_id, "claim_generation": worker.generation,
                        "expected_lease_revision": task["lease_revision"],
                    }, "renew", result)
                return
        except Exception as error:
            result["errors"].append(error_record(error, "worker"))
            worker.failure = getattr(error, "code", "execution_failed")
            try:
                self._stop(worker)
                task = self.authority.state.tasks.get(worker.task_id)
                if self._matches(worker, task) and task["recovery"] is not None:
                    if self.workers.get(worker.task_id) is worker:
                        del self.workers[worker.task_id]
                    self._recoveries[self._worker_key(worker)] = worker
            except Exception as stop_error:
                result["errors"].append(error_record(stop_error, "stop"))

    def _ready(self, state, now):
        # Apply host-only filters before eligibility/ordering, not after a capped
        # global ready page that could hide all work for this host.
        candidates = []
        for task in state.tasks.values():
            if task["execution_profile"] not in self.profiles:
                continue
            if any(task[field] != self.filters[field]
                   for field in ("project", "goal_id", "execution_profile") if field in self.filters):
                continue
            if task["priority"] > self.filters.get("priority_ceiling", 4):
                continue
            if "task_ids" in self.filters and task["id"] not in self.filters["task_ids"]:
                continue
            if ("resource_keys" in self.filters
                    and not set(self.filters["resource_keys"]) <= set(task["resource_keys"])):
                continue
            if task_eligibility(state, task, now.isoformat())["ready"]:
                candidates.append(task)
        return sorted(candidates, key=lambda task: (task["priority"], task["created_at"], task["id"]))

    def _claim(self, task, command, result):
        pending = self._claims.get(task["id"])
        if pending is not None and pending[1] is not command:
            result["raced"] += 1
            return
        key = self.requests.key(command)
        if key in self._claim_receipts:
            outcome, receipt = "committed", self._claim_receipts[key]
        else:
            outcome, receipt = self.requests.submit(command)
            if outcome == "committed":
                self._claim_receipts[key] = copy.deepcopy(receipt)
                pending = self._claims.setdefault(task["id"], (task, command))
        if outcome == "unknown":
            self._claims[task["id"]] = (task, command)
            result["errors"].append({"operation": "claim", "code": "outcome_unknown",
                                     "message": "Claim remains unconfirmed; no execution started"})
            return
        if outcome == "committed":
            state, now, startup_pending, _, health = self._view()
            if not self._require_current(health, result):
                return
        if pending is not None and self._claims.get(task["id"]) is pending:
            del self._claims[task["id"]]
        self._claim_receipts.pop(key, None)
        if outcome in {"raced", "abandoned"}:
            result[outcome] += 1
            return
        if outcome == "error":
            result["errors"].append(receipt)
            return
        current = state.tasks[task["id"]]
        if (startup_pending or task["id"] in self.workers or current["status"] != "in_progress"
                or current["claim_generation"] != task["claim_generation"] + 1
                or current["attempt"]["supervisor_id"] != self.supervisor_id
                or current["attempt"]["owner"] != self.worker_id):
            result["raced"] += 1
            return
        worker = Worker(current["id"], current["attempt"]["id"], current["claim_generation"],
                        self.profiles[current["execution_profile"]],
                        self._lease_limit(current, self._receipt_task(receipt, current["id"])),
                        now + timedelta(seconds=current["policy"]["renewal_seconds"]))
        self.workers[worker.task_id] = worker
        self._peak = max(self._peak, len(self.workers))
        if not self._closing:
            self._advance(worker, result)

    @staticmethod
    def _result():
        return {"started": 0, "completed": 0, "failed": 0, "renewed": 0, "checkpoints": 0,
                "raced": 0, "abandoned": 0, "errors": [], "active": 0, "queued": 0}

    def step(self):
        with self._lock:
            result = self._result()
            try:
                _, now, startup_pending, _, _ = self._view()
                self._stop_due(now, startup_pending, result)
                if startup_pending or self._next_sweep is None or now >= self._next_sweep:
                    sweep = self.maintenance.tick()
                    self._next_sweep = now + timedelta(seconds=self.sweep_seconds)
                    result["maintenance"] = sweep
                    result["errors"].extend(sweep["errors"])
                    if sweep["errors"] or sweep["unknown"]:
                        return self._finish(result)
                else:
                    self.authority.reconcile()
                state, now, startup_pending, _, health = self._view()
                self._stop_due(now, startup_pending, result)
                if not self._require_current(health, result):
                    return self._finish(result)
                for worker in list(self.workers.values()):
                    self._advance(worker, result)
                for worker in list(self._recoveries.values())[:100]:
                    self._advance(worker, result)
                for task, command in list(self._claims.values()):
                    self._claim(task, command, result)
                attempted = set()
                for _ in range(self.pool_size):
                    if self._closing or len(self.workers) + len(self._claims) >= self.pool_size:
                        break
                    state, now, startup_pending, _, health = self._view()
                    if not self._require_current(health, result):
                        break
                    if startup_pending:
                        break
                    task = next((t for t in self._ready(state, now) if t["id"] not in attempted), None)
                    if task is None:
                        break
                    attempted.add(task["id"])
                    self._claim(task, {"operation": "claim", "actor": self.supervisor_id,
                                       "task_id": task["id"], "expected_version": task["version"],
                                       "supervisor_id": self.supervisor_id,
                                       "worker_id": self.worker_id}, result)
                state, now, _, _, health = self._view()
                if self._require_current(health, result):
                    result["queued"] = len(self._ready(state, now))
            except Exception as error:
                result["errors"].append(error_record(error, "step"))
                if self.elapsed_time.error is not None:
                    for worker in [*self.workers.values(), *self._recoveries.values()]:
                        worker.failure = self.elapsed_time.error.code
                        try:
                            self._stop(worker)
                        except Exception as stop_error:
                            result["errors"].append(error_record(stop_error, "stop"))
            return self._finish(result)

    def _finish(self, result):
        result["active"] = len(self.workers)
        self._last = copy.deepcopy(result)
        return result

    def health(self):
        return {"ok": self.elapsed_time.error is None
                and (self._last is None or not self._last["errors"])
                and not (self._recoveries or self._claims or self._claim_receipts or self.requests.pending)
                and not any(w.pending or w.pending_receipt or (w.process and w.process.error)
                            for w in self.workers.values()),
                "active": len(self.workers), "pending_claims": len(self._claims),
                "unresolved_recoveries": len(self._recoveries), "peak_workers": self._peak,
                "clock_error": (error_record(self.elapsed_time.error, "clock")
                                if self.elapsed_time.error is not None else None),
                "last_step": copy.deepcopy(self._last)}

    def close(self):
        with self._lock:
            self._closing = True
            result = self._result()
            # Stop first: no upstream availability is required to withdraw local execution.
            for worker in list(self.workers.values()):
                try:
                    self._stop(worker)
                except Exception as error:
                    result["errors"].append(error_record(error, "stop"))
            if self._claims:
                try:
                    self.authority.reconcile()
                    for task, command in list(self._claims.values()):
                        self._claim(task, command, result)
                except Exception as error:
                    result["errors"].append(error_record(error, "close_claims"))
            for worker in list(self.workers.values()):
                try:
                    self._stop(worker)
                    if worker.pending:
                        self.authority.reconcile()
                        if not self._resume(worker, result):
                            continue
                    state, now, startup_pending, _, health = self._view()
                    if not self._require_current(health, result):
                        continue
                    task = state.tasks.get(worker.task_id)
                    if self._matches(worker, task) and task["status"] == "in_progress":
                        if now < min(instant(task["lease_expires_at"]),
                                     instant(task["attempt"]["progress_deadline"]),
                                     instant(task["attempt"]["hard_deadline"])) and not startup_pending:
                            self._submit(worker, {
                                "operation": "release", "actor": self.supervisor_id,
                                **execution_tokens(task), "reason": "host_stopped",
                            }, "release", result)
                    state, _, _, _, health = self._view()
                    if not self._require_current(health, result):
                        continue
                    task = state.tasks.get(worker.task_id)
                    if self._matches(worker, task) and task["recovery"] is not None:
                        self._recover(worker, task, state, result)
                    elif worker.pending is None:
                        self._retire(worker)
                except Exception as error:
                    result["errors"].append(error_record(error, "close"))
            return self._finish(result)

    def run(self, *, max_steps, interval_seconds=1, stop_event=None):
        if type(max_steps) is not int or not 1 <= max_steps <= 1000000:
            raise ValueError("max_steps must be in 1..1000000")
        if type(interval_seconds) not in {int, float} or not 0 <= interval_seconds <= 15:
            raise ValueError("runner interval must be in 0..15 seconds")
        count = 0
        try:
            for _ in range(max_steps):
                if stop_event is not None and stop_event.is_set():
                    break
                self.step()
                count += 1
                if interval_seconds:
                    if stop_event is not None:
                        stop_event.wait(interval_seconds)
                    else:
                        time.sleep(interval_seconds)
        finally:
            closed = self.close()
        return {"steps": count, "close": closed, "health": self.health()}
