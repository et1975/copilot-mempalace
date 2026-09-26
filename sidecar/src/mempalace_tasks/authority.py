"""Epoch-bound authority: the palace journal is the sole durable task store.

The only local ownership primitive is a stable lifetime lock. Pending commands,
verified prefixes, clocks and read snapshots are process-local. Restore requires
a quiesced owner and a fresh instance, never reconciliation of local files.
"""

from collections import Counter, OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import threading
import time
from uuid import uuid4

from .codec import canonical_json, command_hash
from .domain import task_eligibility
from .platform_support import LifetimeLock, PlatformError, ensure_private_directory
from .runtime_clock import ClockError, RuntimeClock
from .model import DomainError, identifier, instant, integer, text, utc
from .palace import PalaceError
from .protocol import (
    LogState, ProtocolError, fold_record, make_epoch, make_proposal, make_settlement, normalize_command,
)


class AuthorityError(Exception):
    def __init__(self, code, message, details=None, *, ambiguous=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(details) if details is not None else {}
        self.ambiguous = ambiguous

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": deepcopy(self.details),
                "ambiguous": self.ambiguous}


class _BoundAuthority:
    """Internal port with a frozen owner epoch; never use for HTTP dispatch."""

    def __init__(self, authority, epoch_id):
        self._authority, self.epoch_id = authority, epoch_id

    @contextmanager
    def serialized(self):
        with self._authority.serialized():
            self._authority._check_epoch(self.epoch_id)
            yield self

    @property
    def state(self):
        with self.serialized():
            return self._authority.state

    def health(self):
        with self.serialized():
            return self._authority.health()

    def reconcile(self):
        with self.serialized():
            return self._authority.reconcile()

    def execute(self, command):
        return self._authority._execute_current(command, self.epoch_id)


class TaskAuthority:
    """Serialized decisions, bounded startup fencing and scoped historical reads."""

    STARTUP_BATCH = 100

    def __init__(self, authority_id, client, runtime_dir, *, clock=None, backoff=time.sleep,
                 expected_configuration=None, initialize=False, system_actor="system",
                 recovery_actor="operator"):
        identifier(authority_id, "authority_id")
        if clock is not None and (not callable(getattr(clock, "now", None))
                                  or not callable(getattr(clock, "observe", None))):
            raise AuthorityError("clock_error", "Clock requires now() and observe(timestamp)")
        if not callable(backoff) or type(initialize) is not bool:
            raise AuthorityError("validation_error", "Invalid backoff or initialization flag")
        if any(type(actor) is not str or not actor.strip() or len(actor) > 256
               for actor in (system_actor, recovery_actor)):
            raise AuthorityError("validation_error", "Recovery actors must be bounded nonempty names")
        if expected_configuration is not None and type(expected_configuration) is not dict:
            raise AuthorityError("validation_error", "Expected configuration must be an object")
        canonical_json(expected_configuration)
        self.authority_id = authority_id
        self.client = client
        self.runtime_dir = Path(runtime_dir)
        self._expected_configuration = deepcopy(expected_configuration)
        self._initialize = initialize
        self._system_actor, self._recovery_actor = system_actor, recovery_actor
        self._initial_clock = clock
        self.clock_source = clock
        self._owner_epoch = self._owner_activation = None
        self._startup_pending = True
        self._fenced_error = None
        self._backoff = backoff
        self._mutex = threading.RLock()
        self._process = os.getpid()
        self._owner = None
        self._owned = False
        self._started = False
        self._log = LogState(authority_id)
        self._pending = None
        self._verified = False
        self._last_verified_at = None
        self._last_error = None
        self._snapshots = OrderedDict()
        self._cursor_key = secrets.token_bytes(32)

    @property
    def epoch_id(self):
        return self._owner_epoch

    @property
    def startup_pending(self):
        return self._startup_pending

    def _same_process(self):
        if os.getpid() != self._process:
            raise AuthorityError("wrong_process", "Authority cannot be used after fork")

    @contextmanager
    def _boundary(self, *, started=True, mutation=None):
        self._same_process()
        with self._mutex:
            if started and not self._started:
                raise AuthorityError("not_started", "Authority has not acquired ownership")
            try:
                yield
            except (DomainError, ProtocolError, PlatformError, PalaceError, ClockError) as error:
                code = "authority_locked" if error.code == "lock_busy" else error.code
                if not isinstance(error, DomainError):
                    self._verified = False
                    self._last_error = {"code": code}
                details = error.details if isinstance(error, (DomainError, ProtocolError)) else {}
                ambiguous = bool(getattr(error, "ambiguous", False)
                                 or mutation is not None and mutation["ambiguous"])
                raise AuthorityError(code, error.message, details,
                                     ambiguous=ambiguous) from error
            except AuthorityError as error:
                if mutation is not None and mutation["ambiguous"]:
                    error.ambiguous = True
                if error.code in {"log_rollback", "outcome_unknown", "clock_error",
                                  "epoch_superseded", "activation_unknown"}:
                    self._verified = False
                    self._last_error = {"code": error.code}
                raise

    @contextmanager
    def serialized(self):
        """Public reentrant guard; release between ordinary network-bearing commands."""
        with self._boundary():
            yield self

    def start(self):
        with self._boundary(started=False):
            if self._started:
                self._require_live_owner()
                return self
            try:
                ensure_private_directory(self.runtime_dir)
                self._owner = LifetimeLock(self.runtime_dir / "authority.lock")
                self._owner.acquire()
                self._owned = True
                self._pending = None
                self._owner_epoch = self._owner_activation = None
                self._startup_pending = True
                self.clock_source = self._initial_clock
                self._started = True
                self.client.discover()
                self._refresh_locked(verify_prefix=True)
                self._activate()
                self._startup_pending = any(task["status"] == "in_progress"
                                            for task in self._log.state.tasks.values())
                self._recover_startup()
                return self
            except BaseException:
                self._started = False
                self._verified = False
                if self._owned:
                    self._owner.release()
                    self._owned = False
                raise

    def close(self):
        self._same_process()
        with self._mutex:
            if self._owned:
                self._owner.release()
                self._owned = False
            self._started = False
            self._verified = False
            self._snapshots.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @property
    def state(self):
        with self._boundary():
            return deepcopy(self._log.state)

    @property
    def log(self):
        """Detached protocol view including raw history and terminal outcomes."""
        with self._boundary():
            return deepcopy(self._log)

    def accepted_records(self, after_ordinal=0, limit=100):
        """Read detached committed events from the already-folded ordinal index.

        This projection feed performs no refresh, clock, or persistence I/O.
        Call refresh/reconcile separately when upstream currency is required.
        """
        with self._boundary():
            integer(after_ordinal, "after_ordinal", 0)
            integer(limit, "limit", 1, 500)
            if after_ordinal >= self._log.domain_ordinal:
                return []
            return deepcopy(self._log.accepted_records[after_ordinal:after_ordinal + limit])

    def _now(self):
        try:
            now = utc(instant(self.clock_source.now()))
            if (self._log.state.last_event_at is not None
                    and instant(now) < instant(self._log.state.last_event_at)):
                raise AuthorityError("clock_error", "Clock is behind the accepted domain head")
            return now
        except DomainError as error:
            raise AuthorityError("clock_error", "Clock must return canonical UTC") from error

    def _proposal_outcome(self, proposal, log):
        self._require_live_owner()
        if proposal["epoch_id"] != self._owner_epoch or log.epoch_id != self._owner_epoch:
            raise AuthorityError("stale_epoch", "Pending request belongs to another epoch")
        outcome = log.outcomes.get(proposal["command_id"])
        if outcome is not None and (
                outcome["command_hash"] != proposal["command_hash"]
                or outcome["proposal_hash"] != proposal["payload_hash"]
                or outcome["task_ids"] != proposal["task_ids"]):
            raise ProtocolError("invariant_violation", "Pending proposal conflicts with log")
        return outcome

    def _audit_prefix(self):
        checkpoint = self._log.checkpoint()
        matched = checkpoint["raw_cursor"] is None
        rebuilt = LogState(self.authority_id)
        for raw in self.client.replay_events():
            fold_record(rebuilt, raw)
            if checkpoint["raw_cursor"] == rebuilt.raw_cursor:
                if checkpoint != rebuilt.checkpoint():
                    raise AuthorityError("log_rollback", "Verified raw prefix changed")
                matched = True
        if not matched:
            raise AuthorityError("log_rollback", "Verified raw prefix is absent")
        return rebuilt

    def _observe_time(self, rebuilt):
        timestamps = [attempt["at"] for attempt in rebuilt.activation_attempts.values()
                      if attempt["outcome"] == "accepted"]
        if rebuilt.state.last_event_at is not None:
            timestamps.append(rebuilt.state.last_event_at)
        high_water = max(timestamps, key=instant) if timestamps else None
        if self.clock_source is None:
            self.clock_source = RuntimeClock(initial_time=high_water)
        elif high_water is not None:
            self.clock_source.observe(high_water)

    def _validate_configuration(self, configuration):
        if configuration is None:
            if not self._initialize:
                raise AuthorityError("not_initialized", "Expected authority journal has no genesis")
            return
        if (self._expected_configuration is not None
                and configuration != self._expected_configuration):
            raise AuthorityError("configuration_conflict",
                                 "Requested genesis differs from accepted configuration")
        actors = configuration["actors"]
        if (actors.get(self._system_actor) != "system"
                or actors.get(self._recovery_actor) != "operator"
                or self._system_actor == self._recovery_actor):
            raise AuthorityError("configuration_conflict",
                                 "Registered system and operator recovery identities are required")

    def _raise_fenced(self):
        if self._fenced_error is not None:
            raise AuthorityError(self._fenced_error, "This authority incarnation is permanently fenced")

    def _require_live_owner(self):
        self._raise_fenced()
        if (self._owner_epoch is None or self._log.epoch_id != self._owner_epoch
                or self._log.activation_id != self._owner_activation):
            raise AuthorityError("activation_pending", "Current owner activation is not verified")

    def _refresh_locked(self, *, verify_prefix=False):
        self._raise_fenced()
        self._verified = False
        try:
            rebuilt = self._audit_prefix()
            self._validate_configuration(rebuilt.state.configuration)
            if self._owner_epoch is not None and (
                    rebuilt.epoch_id != self._owner_epoch
                    or rebuilt.activation_id != self._owner_activation):
                raise AuthorityError("epoch_superseded", "Another startup activation superseded this owner")
        except (ProtocolError, AuthorityError) as error:
            if error.code in {"log_rollback", "invariant_violation", "epoch_superseded"}:
                self._fenced_error = error.code
            self._last_error = {"code": error.code}
            raise
        self._observe_time(rebuilt)
        self._log = rebuilt
        self._last_verified_at = self._now()
        self._verified = True
        self._last_error = None
        return rebuilt

    def _activate(self):
        last_code = None
        for _ in range(3):
            marker = make_epoch(self._log, str(uuid4()), str(uuid4()), self._now())
            for delay in (1, 2, 4):
                try:
                    self.client.append_event(deepcopy(marker))
                except PalaceError as error:
                    last_code = error.code
                try:
                    self._refresh_locked(verify_prefix=True)
                except PalaceError as error:
                    last_code = error.code
                else:
                    attempt = self._log.activation_attempts.get(marker["activation_id"])
                    if attempt is not None:
                        if attempt["outcome"] == "accepted":
                            if self._log.activation_id != marker["activation_id"]:
                                self._fenced_error = "epoch_superseded"
                                raise AuthorityError("epoch_superseded",
                                                     "Startup activation was already superseded")
                            self._owner_epoch = marker["epoch_id"]
                            self._owner_activation = marker["activation_id"]
                            return
                        break
                self._backoff(delay)
            else:
                raise AuthorityError("activation_unknown", "Startup activation remains unconfirmed",
                                     {"activation_id": marker["activation_id"],
                                      "cause": last_code or "outcome_unknown"}, ambiguous=True)
            self._refresh_locked(verify_prefix=True)
        raise AuthorityError("activation_rejected", "Startup lost all bounded activation attempts")

    def _check_pending_outcome(self):
        return self._proposal_outcome(self._pending["proposal"], self._log)

    def _finish_pending(self, outcome):
        self._require_live_owner()
        self._pending = None
        self._last_error = None
        return outcome

    def _record_pending(self, proposal):
        self._require_live_owner()
        if proposal["event"]["command"]["operation"] == "authority_create":
            self._validate_configuration(proposal["event"]["configuration"])
        self._pending = {"proposal": deepcopy(proposal), "settlement": None}

    def _record_settlement(self, settlement):
        self._require_live_owner()
        self._pending["settlement"] = deepcopy(settlement)

    def _resolve_pending(self, *, force_settlement=False):
        outcome = self._check_pending_outcome()
        if outcome is not None and not force_settlement:
            return self._finish_pending(outcome)
        settlement = make_settlement(self._pending["proposal"])
        self._record_settlement(settlement)
        last_code = None
        for delay in (1, 2, 4):
            try:
                self.client.append_event(deepcopy(settlement))
            except PalaceError as error:
                last_code = error.code
            try:
                self._refresh_locked(verify_prefix=True)
            except PalaceError as error:
                last_code = error.code
                self._verified = False
            else:
                outcome = self._check_pending_outcome()
                if outcome is not None:
                    return self._finish_pending(outcome)
            self._backoff(delay)
        self._verified = False
        raise AuthorityError("outcome_unknown", "Settlement remains unconfirmed",
                             {"command_id": self._pending["proposal"]["command_id"],
                              "cause": last_code or "outcome_unknown"}, ambiguous=True)

    def _reconcile_locked(self):
        self._refresh_locked(verify_prefix=True)
        self._require_live_owner()
        outcome = self._resolve_pending() if self._pending is not None else None
        self._recover_startup()
        return outcome

    def _recover_startup(self):
        if not self._startup_pending:
            return
        for _ in range(self.STARTUP_BATCH):
            task = next((task for task in sorted(self._log.state.tasks.values(), key=lambda t: t["id"])
                         if task["status"] == "in_progress"), None)
            if task is None:
                break
            self.execute_current({
                "operation": "attempt_report", "command_id": str(uuid4()),
                "actor": self._system_actor, "task_id": task["id"],
                "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"], "report_kind": "recovery_started",
                "reason": "authority_startup",
                "evidence": {"references": [
                    f"mptask://{self.authority_id}/epochs/{self._owner_epoch}"]},
            })
        self._startup_pending = (self._pending is not None or any(
            task["status"] == "in_progress" for task in self._log.state.tasks.values()))

    def reconcile(self):
        with self._boundary():
            outcome = self._reconcile_locked()
            return self._receipt(outcome, replayed=True) if outcome else self._health_locked()

    def refresh(self, *, verify_prefix=False):
        """Refresh through a verified full prefix; never mutate domain truth."""
        with self._boundary():
            if type(verify_prefix) is not bool:
                raise AuthorityError("validation_error", "verify_prefix must be boolean")
            self._refresh_locked(verify_prefix=verify_prefix or self._pending is not None)
            return self._health_locked()

    def _check_startup_command(self, command):
        if not self._startup_pending:
            return
        config = self._log.state.configuration
        role = config["actors"].get(command.get("actor")) if config is not None else None
        operation = command["operation"]
        maintenance = (
            operation == "expire" and role == "system"
            or operation == "recover" and role in {"operator", "supervisor"}
            or operation == "attempt_report" and command.get("report_kind") == "recovery_started"
            and role == "system"
        )
        if not maintenance:
            raise AuthorityError("startup_pending", "Inherited attempts must be revoked first")

    def _check_epoch(self, expected_epoch):
        if expected_epoch is None:
            raise AuthorityError("epoch_required", "A frozen request epoch is required")
        identifier(expected_epoch, "expected_epoch")
        if expected_epoch != self._owner_epoch:
            raise AuthorityError("stale_epoch", "Request epoch differs from this owner")
        self._require_live_owner()

    def execute(self, command, *, expected_epoch=None):
        with self._boundary():
            self._check_epoch(expected_epoch)
            if self._startup_pending:
                raise AuthorityError("startup_pending", "Inherited attempts must be revoked first")
            return self._execute_current(command, expected_epoch)

    def execute_current(self, command):
        """Trusted initialization only; transport callers must provide a frozen epoch."""
        return self._execute_current(command, self._owner_epoch)

    def bound_current(self):
        with self._boundary():
            self._require_live_owner()
            return _BoundAuthority(self, self._owner_epoch)

    def _execute_current(self, command, expected_epoch):
        mutation = {"ambiguous": False}
        with self._boundary(mutation=mutation):
            self._check_epoch(expected_epoch)
            normalized = normalize_command(command)
            digest = command_hash(normalized)
            previous = self._log.outcomes.get(normalized["command_id"])
            pending = self._pending["proposal"] if self._pending is not None else None
            mutation["ambiguous"] = bool(
                previous is not None and previous["command_hash"] == digest
                or pending is not None and pending["command_id"] == normalized["command_id"]
                and pending["command_hash"] == digest)
            self._refresh_locked(verify_prefix=self._pending is not None)
            # Check collisions before recovering someone else's pending command.
            known = self._log.outcomes.get(normalized["command_id"])
            if known is not None and known["command_hash"] != digest:
                raise AuthorityError("idempotency_conflict", "Command ID has different content")
            if (self._pending is not None
                    and self._pending["proposal"]["command_id"] == normalized["command_id"]
                    and self._pending["proposal"]["command_hash"] != digest):
                raise AuthorityError("idempotency_conflict", "Pending command has different content")
            if self._pending is not None:
                mutation["ambiguous"] = self._pending["proposal"]["command_id"] == normalized["command_id"]
                self._resolve_pending()
                known = self._log.outcomes.get(normalized["command_id"])
            if known is not None:
                mutation["ambiguous"] = True
                self._last_error = None
                return self._receipt(known, replayed=True)
            self._check_startup_command(normalized)
            proposal = make_proposal(self._log, normalized, self._now())
            self._record_pending(proposal)
            mutation["ambiguous"] = True
            uncertain = False
            try:
                self.client.append_event(deepcopy(proposal))
            except PalaceError:
                uncertain = True
            if uncertain:
                outcome = self._resolve_pending(force_settlement=True)
            else:
                try:
                    self._refresh_locked()
                except PalaceError:
                    outcome = self._resolve_pending(force_settlement=True)
                else:
                    outcome = self._resolve_pending()
            return self._receipt(outcome, replayed=False)

    def _freshness(self):
        if not self._started:
            return False, "not_started"
        if self._fenced_error is not None:
            return False, self._fenced_error
        if self._startup_pending:
            return False, "startup_pending"
        if self._pending is not None:
            return False, "pending_command"
        if not self._verified:
            return False, self._last_error["code"] if self._last_error else "unverified"
        if self._log.state.configuration is None:
            return False, "uninitialized"
        return True, None

    def _metadata(self, now):
        fresh, reason = self._freshness()
        return {"schema_version": 1, "authority_id": self.authority_id, "as_of": now,
                "last_verified_at": self._last_verified_at, "raw_cursor": self._log.raw_cursor,
                "domain_head": self._log.domain_head, "domain_ordinal": self._log.domain_ordinal,
                "fresh": fresh, "reason": reason, "epoch_id": self._owner_epoch,
                "startup_pending": self._startup_pending, "request_epoch_required": True}

    def _health_locked(self):
        result = self._metadata(self._now() if self._started else None)
        result.update({"started": self._started,
                       "protocol": {"raw_hash": self._log.raw_hash,
                                    "record_count": len(self._log.history),
                                    "outcomes": dict(Counter(
                                        row["outcome"] for row in self._log.outcomes.values()))},
                       "pending_command": None if self._pending is None else {
                           "command_id": self._pending["proposal"]["command_id"],
                           "settlement_recorded": self._pending["settlement"] is not None},
                       "error": deepcopy(self._last_error)})
        return result

    def health(self):
        with self._boundary(started=False):
            return self._health_locked()

    def _authorization(self, task, original, now, *, freshness=None):
        attempt = task["attempt"] if task is not None else None
        old_attempt = original["attempt"] if original is not None else None
        matching = (attempt is not None and old_attempt is not None
                    and attempt["id"] == old_attempt["id"]
                    and task["claim_generation"] == original["claim_generation"])
        live = (bool(matching) and task["status"] == "in_progress"
                and attempt["status"] in {"preparing", "running", "settled"}
                and instant(now) < min(instant(task["lease_expires_at"]),
                                       instant(attempt["progress_deadline"]),
                                       instant(attempt["hard_deadline"])))
        fresh, reason = self._freshness() if freshness is None else freshness
        return {"task_id": original["id"], "attempt_id": old_attempt["id"] if old_attempt else None,
                "claim_generation": original["claim_generation"], "matches_current": bool(matching),
                "lease_live": bool(live and fresh),
                "authorized": bool(live and fresh and attempt["status"] == "running"),
                "current_lease_revision": task["lease_revision"] if task else None,
                "lease_expires_at": task["lease_expires_at"] if live and fresh else None,
                "reason": reason if not fresh else (
                    "not_current_attempt" if not matching else "lease_expired" if not live
                    else None if attempt["status"] == "running" else "not_running")}

    def _receipt(self, outcome, *, replayed):
        now = self._now()
        response = deepcopy(outcome["response"])
        tasks = response["tasks"] if response is not None else []
        authorization = {"as_of": now, "fresh": self._freshness()[0], "tasks": [
            self._authorization(self._log.state.tasks.get(task["id"]), task, now) for task in tasks]}
        result = deepcopy(response) if response is not None else {}
        result.update({"ok": outcome["outcome"] == "committed", "outcome": outcome["outcome"],
                       "command_id": outcome["command_id"], "event_id": outcome["event_id"],
                       "ordinal": outcome["ordinal"], "replayed": replayed,
                       "response": response, "tasks": deepcopy(tasks), "authorization": authorization})
        result["epoch_id"] = outcome["epoch_id"]
        if outcome["epoch_id"] != self._owner_epoch:
            authorization["fresh"] = False
            for task in authorization["tasks"]:
                task.update(authorized=False, lease_live=False, lease_expires_at=None,
                            reason="stale_epoch")
        return result

    def outcome(self, epoch_id, command_id):
        """Selected historical evidence, never proof of absent external effects."""
        with self._boundary():
            identifier(epoch_id, "epoch_id")
            identifier(command_id, "command_id")
            self._observe()
            outcome = self._log.historical_outcome(epoch_id, command_id)
            receipt = None if outcome is None else self._receipt(outcome, replayed=True)
            return {**self._metadata(self._now()), "request_epoch": epoch_id,
                    "command_id": command_id,
                    "resolution": "not_recorded" if outcome is None else outcome["outcome"],
                    "receipt": receipt, "authorization": {"authorized": False},
                    "external_effects": "not_determined"}

    def _observe(self):
        try:
            self._refresh_locked(verify_prefix=self._pending is not None)
        except (PalaceError, ProtocolError, AuthorityError) as error:
            self._verified = False
            self._last_error = {"code": error.code}

    def _task(self, task_id):
        if type(task_id) is not str or task_id not in self._log.state.tasks:
            raise AuthorityError("not_found", "Task does not exist")
        return self._log.state.tasks[task_id]

    def get(self, task_id):
        """Observe attempt authorization, not permission for the querying actor.

        Callers must still match host-provided owner, attempt, and generation.
        """
        with self._boundary():
            self._observe()
            task = self._task(task_id)
            now = self._now()
            metadata = self._metadata(now)
            authorization = {"as_of": now, "fresh": metadata["fresh"],
                             **self._authorization(
                                 task, task, now,
                                 freshness=(metadata["fresh"], metadata["reason"]))}
            return {**metadata, "task": deepcopy(task), "authorization": authorization,
                    "eligibility": task_eligibility(self._log.state, task, now),
                    "relations": deepcopy([edge for edge in self._log.state.edges
                                           if task_id in (edge["source"], edge["target"])])}

    def _filters(self, filters):
        canonical_json(filters)
        filters = {} if filters is None else deepcopy(filters)
        if type(filters) is not dict or set(filters) - {
                "project", "goal_id", "status", "assignee", "kind", "needs_attention"}:
            raise AuthorityError("validation_error", "Unknown snapshot filters")
        for key, value in filters.items():
            if key == "needs_attention":
                valid = type(value) is bool
            elif key == "status":
                valid = type(value) is str and value in {
                    "open", "in_progress", "recovering", "quarantined", "closed", "cancelled"}
            elif key == "kind":
                valid = type(value) is str and value in {"task", "epic"}
            else:
                valid = type(value) is str and bool(value)
            if not valid:
                raise AuthorityError("validation_error", f"Invalid {key} filter")
        return filters

    def _row(self, task, now):
        eligibility = task_eligibility(self._log.state, task, now)
        attempt = task["attempt"]
        due = (task["status"] == "in_progress" and instant(now) >= min(
            instant(task["lease_expires_at"]), instant(attempt["progress_deadline"]),
            instant(attempt["hard_deadline"])))
        return {**{key: deepcopy(task[key]) for key in (
            "id", "project", "goal_id", "title", "kind", "status", "priority", "version",
            "claim_generation", "lease_revision", "lease_expires_at", "resource_keys", "recovery")},
                "assignee": task.get("assignee"), "attempt_id": attempt["id"] if attempt else None,
                "attempt_status": attempt["status"] if attempt else None,
                "progress_deadline": attempt["progress_deadline"] if attempt else None,
                "hard_deadline": attempt["hard_deadline"] if attempt else None,
                "checkpoint": deepcopy(attempt["checkpoint"]) if attempt else None,
                "ready": eligibility["ready"], "reasons": eligibility["reasons"],
                "needs_attention": bool(due or task["status"] in {"recovering", "quarantined"}
                                        or task["escalation"] is not None),
                "blockers": [edge["source"] for edge in self._log.state.edges
                             if edge["target"] == task["id"] and edge["edge_type"] == "blocks"
                             and self._log.state.tasks[edge["source"]]["status"] != "closed"]}

    def _capture(self, kind, context, view, rows):
        now = time.monotonic()
        expired = [key for key, value in self._snapshots.items() if now >= value["expires"]]
        for key in expired:
            del self._snapshots[key]
        while len(self._snapshots) >= 100:
            self._snapshots.popitem(last=False)
        snapshot_id = str(uuid4())
        self._snapshots[snapshot_id] = {
            "kind": kind, "context": deepcopy(context), "view": deepcopy(view),
            "rows": deepcopy(rows), "expires": now + 60,
        }
        return snapshot_id

    def _cursor(self, snapshot_id, offset):
        content = f"{snapshot_id}:{offset}"
        signature = hmac.new(self._cursor_key, content.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{content}:{signature}".encode()).decode()

    def _parse_cursor(self, cursor, kind, context):
        try:
            if type(cursor) is not str or len(cursor) > 256:
                raise ValueError
            decoded = base64.b64decode(cursor, altchars=b"-_", validate=True).decode("ascii")
            snapshot_id, offset, signature = decoded.split(":")
            expected = hmac.new(self._cursor_key, f"{snapshot_id}:{offset}".encode(),
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature) or not offset.isdigit():
                raise ValueError
            offset = int(offset)
        except (ValueError, UnicodeError) as error:
            raise AuthorityError("invalid_cursor", "Malformed or modified cursor") from error
        saved = self._snapshots.get(snapshot_id)
        if saved is None or time.monotonic() >= saved["expires"]:
            self._snapshots.pop(snapshot_id, None)
            raise AuthorityError("snapshot_expired", "Snapshot expired or was evicted")
        if saved["kind"] != kind or context is not None and context != saved["context"]:
            raise AuthorityError("invalid_cursor", "Cursor belongs to a different query")
        if offset <= 0 or offset >= len(saved["rows"]):
            raise AuthorityError("invalid_cursor", "Cursor offset is outside snapshot")
        return snapshot_id, offset

    def _page(self, snapshot_id, offset, limit):
        saved = self._snapshots[snapshot_id]
        result = deepcopy(saved["view"])
        result.update({"snapshot_id": snapshot_id, "rows": deepcopy(
            saved["rows"][offset:offset + limit]), "next_cursor": None})
        if offset + limit < len(saved["rows"]):
            result["next_cursor"] = self._cursor(snapshot_id, offset + limit)
        if offset:
            result["fresh"], result["reason"] = False, "pinned_snapshot"
        return result

    def snapshot(self, filters=None, limit=100, cursor=None):
        with self._boundary():
            integer(limit, "limit", 1, 500)
            checked = self._filters(filters)
            if cursor is not None:
                snapshot_id, offset = self._parse_cursor(
                    cursor, "snapshot", checked if filters is not None else None)
            else:
                self._observe()
                now = self._now()
                rows = [self._row(task, now) for task in self._log.state.tasks.values()]
                rows = [row for row in rows if all(row[key] == value for key, value in checked.items())]
                rows.sort(key=lambda row: (row["priority"], row["id"]))
                summary = {"total": len(rows), "statuses": dict(Counter(row["status"] for row in rows)),
                           "ready": sum(row["ready"] for row in rows),
                           "needs_attention": sum(row["needs_attention"] for row in rows)}
                snapshot_id = self._capture("snapshot", checked,
                                            {**self._metadata(now), "summary": summary}, rows)
                offset = 0
            return self._page(snapshot_id, offset, limit)

    def ready(self, filters=None):
        with self._boundary():
            canonical_json(filters)
            filters = {} if filters is None else filters
            if type(filters) is not dict:
                raise AuthorityError("validation_error", "Ready filters must be an object")
            common = self._filters({key: value for key, value in filters.items()
                                    if key not in {"execution_profile", "priority_ceiling", "limit"}})
            ceiling = integer(filters.get("priority_ceiling", 4), "priority_ceiling", 0, 4)
            limit = integer(filters.get("limit", 100), "limit", 1, 500)
            if "execution_profile" in filters:
                text(filters["execution_profile"], "execution_profile")
            self._observe()
            now = self._now()
            if not self._freshness()[0]:
                raise AuthorityError("not_current", "Ready requires current authority data",
                                     {"reason": self._freshness()[1]})
            tasks = []
            for task in self._log.state.tasks.values():
                if task["priority"] > ceiling or (
                        "execution_profile" in filters
                        and task["execution_profile"] != filters["execution_profile"]):
                    continue
                row = self._row(task, now)
                if row["ready"] and all(row[key] == value for key, value in common.items()):
                    tasks.append(task)
            tasks.sort(key=lambda task: (task["priority"], task["created_at"], task["id"]))
            return {**self._metadata(now), "tasks": deepcopy(tasks[:limit])}

    def history(self, task_id, limit=100, cursor=None, after_record_seq=0):
        with self._boundary():
            integer(limit, "limit", 1, 500)
            integer(after_record_seq, "after_record_seq", 0)
            if cursor is not None:
                snapshot_id, offset = self._parse_cursor(cursor, "history", None)
                saved = self._snapshots[snapshot_id]
                if (saved["context"]["task_id"] != task_id
                        or after_record_seq not in (0, saved["context"]["after_record_seq"])):
                    raise AuthorityError("invalid_cursor", "History cursor belongs to another query")
            else:
                self._observe()
                self._task(task_id)
                now = self._now()
                upper = len(self._log.history)
                rows = [row for row in self._log.history
                        if after_record_seq < row["record_seq"] <= upper and task_id in row["task_ids"]]
                snapshot_id = self._capture(
                    "history", {"task_id": task_id, "after_record_seq": after_record_seq},
                    {**self._metadata(now), "task_id": task_id, "upper_record_seq": upper,
                     "after_record_seq": after_record_seq}, rows)
                offset = 0
            return self._page(snapshot_id, offset, limit)
