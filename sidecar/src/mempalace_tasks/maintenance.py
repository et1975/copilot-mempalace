"""Independent lease maintenance; all truth changes go through the authority."""
from __future__ import annotations

import copy
from datetime import datetime
from typing import ContextManager, Protocol
from uuid import uuid4

from .codec import canonical_json
from .model import State


class AuthorityPort(Protocol):
    @property
    def state(self) -> State: ...
    def serialized(self) -> ContextManager: ...
    def execute(self, command: dict) -> dict: ...
    def reconcile(self): ...
    def health(self) -> dict: ...


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def execution_tokens(task: dict) -> dict:
    return {"task_id": task["id"], "expected_version": task["version"],
            "attempt_id": task["attempt"]["id"],
            "claim_generation": task["claim_generation"]}


def error_record(error: Exception, operation: str) -> dict:
    return {"operation": operation, "code": getattr(error, "code", type(error).__name__),
            "message": str(error)}


class MutationRequests:
    """Keep exact IDs until terminal within a frozen authority incarnation.

    These local entries are scheduling metadata, not another task journal.
    The authority retains uncertain proposals only in memory; a new owner's
    activation fences absent old requests rather than restoring local metadata.
    """
    def __init__(self, authority: AuthorityPort):
        self.authority = authority
        self.pending: dict[str, dict] = {}

    @staticmethod
    def key(command: dict) -> str:
        return canonical_json({k: v for k, v in command.items() if k != "command_id"})

    def submit(self, command: dict) -> tuple[str, dict]:
        key = self.key(command)
        request = self.pending.setdefault(key, {**copy.deepcopy(command),
                                                "command_id": str(uuid4())})
        try:
            response = self.authority.execute(copy.deepcopy(request))
        except Exception as error:
            code = getattr(error, "code", "")
            if (getattr(error, "ambiguous", False) is True
                    or code in {"outcome_unknown", "upstream_unavailable", "pending_command",
                                "pending_resolution", "io_error"}):
                return "unknown", error_record(error, request["operation"])
            del self.pending[key]
            if code in {"command_abandoned", "abandoned"}:
                return "abandoned", error_record(error, request["operation"])
            if code in {"version_conflict", "stale_generation", "lease_expired", "not_ready"}:
                return "raced", error_record(error, request["operation"])
            return "error", error_record(error, request["operation"])
        outcome = response.get("outcome")
        if outcome in {"unknown", "outcome_unknown"}:
            return "unknown", response
        if outcome == "abandoned":
            del self.pending[key]
            return "abandoned", response
        if response.get("ok") is True and outcome == "committed":
            del self.pending[key]
            return "committed", response
        # A nonterminal or malformed receipt is not permission to issue a new ID.
        return "unknown", response


class LeaseMaintenance:
    def __init__(self, authority: AuthorityPort, clock, *, system_actor: str,
                 recovery_actor: str, batch_limit: int = 100):
        if type(batch_limit) is not int or not 1 <= batch_limit <= 10000:
            raise ValueError("batch_limit must be an integer in 1..10000")
        with authority.serialized():
            configuration = authority.state.configuration or {}
            actors = configuration.get("actors", {})
            if actors.get(system_actor) != "system" or actors.get(recovery_actor) != "operator":
                raise ValueError("registered system and operator recovery identities required")
            if system_actor == recovery_actor:
                raise ValueError("maintenance and recovery identities must differ")
        self.authority, self.clock = authority, clock
        self.system_actor, self.recovery_actor = system_actor, recovery_actor
        self.batch_limit = batch_limit
        self.requests = MutationRequests(authority)
        self._last = None
        self._ticks = 0

    def _snapshot(self):
        with self.authority.serialized():
            now = self.clock.now()
            return self.authority.state, now, self.authority.health()["startup_pending"]

    def _candidates(self, state, now, startup_pending):
        if startup_pending:
            return
        for task in sorted(state.tasks.values(), key=lambda t: t["id"]):
            if task["status"] == "in_progress":
                if instant(now) >= min(instant(task["lease_expires_at"]),
                                         instant(task["attempt"]["progress_deadline"]),
                                         instant(task["attempt"]["hard_deadline"])):
                    yield {"operation": "expire", "actor": self.system_actor,
                           **execution_tokens(task),
                           "expected_lease_revision": task["lease_revision"]}
        for task in sorted(state.tasks.values(), key=lambda t: t["id"]):
            if (task["execution_class"] == "isolated" and task["recovery"] is not None
                    and not task["recovery"]["barrier_satisfied"]
                    and task["attempt"]["status"] == "revoked"):
                yield {"operation": "recover", "actor": self.recovery_actor,
                       "task_id": task["id"], "expected_version": task["version"],
                       "retry_decision": "preserve",
                       "evidence": {"references": [self._reference(state, task)],
                                    "publication_revoked": True}}

    @staticmethod
    def _reference(state, task):
        return (f"mptask://{state.authority_id}/tasks/{task['id']}/attempts/"
                f"{task['attempt']['id']}/lease-revisions/{task['lease_revision']}")

    def tick(self) -> dict:
        result = {"expired": 0, "recovered": 0, "revoked": 0, "raced": 0,
                  "unknown": 0, "abandoned": 0, "unfinished": 0, "errors": []}
        self._ticks += 1
        try:
            self.authority.reconcile()
        except Exception as error:
            result["errors"].append(error_record(error, "reconcile"))
            result["unfinished"] = len(self.requests.pending)
            self._last = result
            return copy.deepcopy(result)
        seen = set()
        try:
            for _ in range(self.batch_limit):
                state, now, startup_pending = self._snapshot()
                if startup_pending:
                    break
                commands = list(self.requests.pending.values())
                commands.extend(self._candidates(state, now, startup_pending))
                command = next((c for c in commands if self.requests.key(c) not in seen), None)
                if command is None:
                    break
                seen.add(self.requests.key(command))
                # No enclosing authority critical section across network-bearing calls.
                outcome, receipt = self.requests.submit(command)
                if outcome == "committed":
                    counter = {"expire": "expired", "recover": "recovered",
                               "attempt_report": "revoked"}[command["operation"]]
                    result[counter] += 1
                elif outcome == "error":
                    result["errors"].append(receipt)
                else:
                    result[outcome] += 1
            state, now, startup_pending = self._snapshot()
            remaining = {self.requests.key(c) for c in self._candidates(state, now, startup_pending)}
            result["unfinished"] = len(remaining | self.requests.pending.keys()) + (
                sum(task["status"] == "in_progress" for task in state.tasks.values())
                if startup_pending else 0)
        except Exception as error:
            result["errors"].append(error_record(error, "maintenance"))
        self._last = copy.deepcopy(result)
        return result

    def health(self) -> dict:
        return {"ok": self._last is None or not (self._last["errors"] or self._last["unknown"]),
                "ticks": self._ticks, "pending_requests": len(self.requests.pending),
                "last_tick": copy.deepcopy(self._last)}
