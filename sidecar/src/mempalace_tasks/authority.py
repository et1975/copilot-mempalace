"""Legacy file-backed authority and shared serialized decision/read machinery.

Only execute/reconcile/start write recovery metadata or append records. Diagnostic
reads may replay the log, but never settle, expire, recover, or clear pending work.
The upstream append-order log is truth; local files are verified recovery metadata.
JournalAuthority is a lazy export of the palace-only subclass, which replaces
these local persistence hooks and requires epoch-bound requests.
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
from .journal import AuthorityLock, HeadStore, JournalError, PendingStore
from .leases import ClockError
from .model import DomainError, identifier, instant, integer, text, utc
from .palace import PalaceError
from .protocol import (
    LogState, ProtocolError, fold_record, make_proposal, make_settlement, normalize_command,
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


class TaskAuthority:
    recovery_mode = "legacy"

    def __init__(self, authority_id, client, state_dir, *, clock, backoff=time.sleep):
        identifier(authority_id, "authority_id")
        if not callable(clock) or not callable(backoff):
            raise AuthorityError("validation_error", "clock and backoff must be callable")
        self.authority_id = authority_id
        self.client = client
        self.state_dir = Path(state_dir)
        self._clock = clock
        self.clock_source = None
        self._backoff = backoff
        self._mutex = threading.RLock()
        self._process = os.getpid()
        self._owner, self._pending_store, self._head_store = self._make_recovery_io()
        self._owned = False
        self._started = False
        self._log = LogState(authority_id)
        self._pending = None
        self._saved_head = None
        self._head_loaded = False
        self._head_write_attempt = None
        self._verified = False
        self._last_verified_at = None
        self._last_error = None
        self._persistence_error = False
        self._snapshots = OrderedDict()
        self._cursor_key = secrets.token_bytes(32)

    def _make_recovery_io(self):
        return (AuthorityLock(self.state_dir, self.authority_id),
                PendingStore(self.state_dir), HeadStore(self.state_dir))

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
            except (DomainError, ProtocolError, JournalError, PalaceError, ClockError) as error:
                if not isinstance(error, DomainError):
                    self._verified = False
                    self._last_error = {"code": error.code}
                if isinstance(error, JournalError):
                    self._persistence_error = True
                details = error.details if isinstance(error, (DomainError, ProtocolError)) else {}
                ambiguous = bool(getattr(error, "ambiguous", False)
                                 or mutation is not None and mutation["ambiguous"])
                raise AuthorityError(error.code, error.message, details,
                                     ambiguous=ambiguous) from error
            except AuthorityError as error:
                if mutation is not None and mutation["ambiguous"]:
                    error.ambiguous = True
                if error.code in {"log_rollback", "outcome_unknown", "missing_state", "clock_error",
                                  "migration_required"}:
                    self._verified = False
                    self._last_error = {"code": error.code}
                raise

    @contextmanager
    def serialized(self):
        """Public reentrant guard; release between ordinary network-bearing commands."""
        with self._boundary():
            yield self

    def start(self, clock_factory=None):
        with self._boundary(started=False):
            if self._started:
                if clock_factory is not None:
                    raise AuthorityError("already_started", "Clock already selected")
                return self
            try:
                self._owner.acquire()
                self._owned = True
                self._head_loaded = False
                self._head_write_attempt = None
                if clock_factory is not None:
                    source = clock_factory()
                    if (not callable(getattr(source, "now", None))
                            or type(getattr(source, "pending_reboot", None)) is not bool):
                        raise AuthorityError("clock_error", "Clock factory must return a clock")
                    self.clock_source = source
                    self._clock = source.now
                self._started = True
                self.client.discover()
                self._reconcile_locked()
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
            now = utc(instant(self._clock()))
            if (self._log.state.last_event_at is not None
                    and instant(now) < instant(self._log.state.last_event_at)):
                raise AuthorityError("clock_error", "Clock is behind the accepted domain head")
            return now
        except DomainError as error:
            raise AuthorityError("clock_error", "Clock must return canonical UTC") from error

    def _boot_pending(self):
        if self.clock_source is None:
            return False
        pending = self.clock_source.pending_reboot
        if type(pending) is not bool:
            raise AuthorityError("clock_error", "Clock reboot state must be boolean")
        return pending

    def _proposal_outcome(self, proposal, log):
        outcome = log.outcomes.get(proposal["command_id"])
        if outcome is not None and (
                outcome["command_hash"] != proposal["command_hash"]
                or outcome["proposal_hash"] != proposal["payload_hash"]
                or outcome["task_ids"] != proposal["task_ids"]):
            raise ProtocolError("invariant_violation", "Pending proposal conflicts with log")
        return outcome

    def _load_pending(self, *, reacquired_log=None):
        pending = self._pending_store.read()
        prior_resolved = (self._pending is not None and reacquired_log is not None
                          and self._proposal_outcome(self._pending["proposal"], reacquired_log)
                          is not None)
        if pending is None and self._pending is not None:
            known = self._proposal_outcome(self._pending["proposal"], self._log)
            if not prior_resolved and (not self._persistence_error or known is None):
                raise AuthorityError("missing_state", "Unresolved pending proposal disappeared")
        if pending is not None:
            proposal = pending["proposal"]
            if self._pending is not None and not prior_resolved and (
                    canonical_json(proposal) != canonical_json(self._pending["proposal"])
                    or self._pending["settlement"] is not None
                    and canonical_json(pending["settlement"])
                    != canonical_json(self._pending["settlement"])):
                raise ProtocolError("invariant_violation", "Unresolved pending proposal changed")
            settlement = make_settlement(proposal)
            if proposal["authority_id"] != self.authority_id:
                raise ProtocolError("invariant_violation", "Foreign pending proposal")
            if (pending["settlement"] is not None
                    and canonical_json(pending["settlement"]) != canonical_json(settlement)):
                raise ProtocolError("invariant_violation", "Pending settlement mismatch")
        self._pending = pending
        return pending

    def _read_checkpoint(self):
        saved = self._head_store.read()
        if saved is None and self._saved_head is not None:
            raise AuthorityError("missing_state", "Verified checkpoint disappeared")
        if self._head_loaded:
            allowed = [self._saved_head]
            if self._head_write_attempt is not None:
                allowed.append(self._head_write_attempt)
            if saved not in allowed:
                raise AuthorityError("log_rollback", "Verified checkpoint changed unexpectedly")
        return saved

    def _audit_prefix(self, saved):
        checkpoints = [checkpoint for checkpoint in
                       (saved, self._log.checkpoint()) if checkpoint is not None
                       and checkpoint["raw_cursor"] is not None]
        remaining = list(checkpoints)
        rebuilt = LogState(self.authority_id)
        for raw in self.client.replay_events():
            fold_record(rebuilt, raw)
            for checkpoint in list(remaining):
                if checkpoint["raw_cursor"] == rebuilt.raw_cursor:
                    if checkpoint != rebuilt.checkpoint():
                        raise AuthorityError("log_rollback", "Verified raw prefix changed")
                    remaining.remove(checkpoint)
        if remaining:
            raise AuthorityError("log_rollback", "Verified raw prefix is absent")
        self._check_protocol_mode(rebuilt)
        return rebuilt

    def _check_protocol_mode(self, log):
        if self.recovery_mode == "legacy" and log.epoch_id is not None:
            raise AuthorityError(
                "migration_required",
                "This journal requires epoch-bound recovery; use a journal-mode authority",
            )

    def _refresh_locked(self, *, verify_prefix=False):
        saved = self._read_checkpoint()
        if not self._head_loaded:
            self._verified = False
            rebuilt = self._audit_prefix(saved)
            self._load_pending(reacquired_log=rebuilt)
        else:
            had_pending = self._pending is not None
            self._load_pending()
            audit = verify_prefix or not self._verified or not had_pending and self._pending is not None
            self._verified = False
            if audit:
                rebuilt = self._audit_prefix(saved)
            else:
                # Each fold publishes a coherent verified prefix under the mutex.
                # An interrupted tail stays non-current and requires a full audit.
                rebuilt = self._log
                try:
                    for raw in self.client.replay_events(rebuilt.raw_cursor):
                        fold_record(rebuilt, raw)
                except PalaceError as error:
                    if error.code == "unknown_cursor":
                        raise AuthorityError("log_rollback", "Verified raw cursor disappeared") from error
                    raise
        self._check_protocol_mode(rebuilt)
        now = self._now()
        if (rebuilt.state.last_event_at is not None
                and instant(now) < instant(rebuilt.state.last_event_at)):
            raise AuthorityError("clock_error", "Clock is behind the replayed domain head")
        self._log = rebuilt
        if not self._head_loaded:
            self._saved_head = saved
            self._head_loaded = True
        self._verified = True
        self._last_verified_at = now
        if not self._persistence_error:
            self._last_error = None
        return rebuilt

    def _persist_head(self):
        self._read_checkpoint()
        head = self._log.checkpoint()
        self._head_write_attempt = head
        self._head_store.write(head)
        self._saved_head = head
        self._head_loaded = True
        self._head_write_attempt = None

    def _check_pending_outcome(self):
        return self._proposal_outcome(self._pending["proposal"], self._log)

    def _finish_pending(self, outcome):
        self._persist_head()
        self._pending_store.clear()
        self._pending = None
        self._persistence_error = False
        self._last_error = None
        return outcome

    def _record_pending(self, proposal):
        self._pending_store.write(proposal)
        self._pending = {"proposal": proposal, "settlement": None}

    def _record_settlement(self, settlement):
        self._pending_store.attach_settlement(settlement)
        self._pending["settlement"] = settlement

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
        self._refresh_locked(verify_prefix=self._pending is not None)
        if self._pending is not None:
            return self._resolve_pending()
        self._persist_head()
        self._persistence_error = False
        self._last_error = None
        return None

    def reconcile(self):
        with self._boundary():
            outcome = self._reconcile_locked()
            return self._receipt(outcome, replayed=True) if outcome else self._health_locked()

    def refresh(self, *, verify_prefix=False):
        """Read the healthy tail; opt into a full historical prefix audit.

        Startup and recovery after uncertainty/disconnection audit automatically.
        Healthy reads trust historical immutability under the append-only contract.
        """
        with self._boundary():
            if type(verify_prefix) is not bool:
                raise AuthorityError("validation_error", "verify_prefix must be boolean")
            self._refresh_locked(verify_prefix=verify_prefix or self._pending is not None)
            return self._health_locked()

    def _check_boot_command(self, command):
        if not self._boot_pending():
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
            raise AuthorityError("reboot_pending", "Inherited attempts must be revoked first")

    def execute(self, command):
        mutation = {"ambiguous": False}
        with self._boundary(mutation=mutation):
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
                self._persist_head()
                self._persistence_error = False
                self._last_error = None
                return self._receipt(known, replayed=True)
            self._check_boot_command(normalized)
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
        if self._pending is not None:
            return False, "pending_command"
        if self._persistence_error:
            return False, "persistence_unconfirmed"
        if not self._verified:
            return False, self._last_error["code"] if self._last_error else "unverified"
        if self._boot_pending():
            return False, "reboot_pending"
        if self._log.state.configuration is None:
            return False, "uninitialized"
        return True, None

    def _metadata(self, now):
        fresh, reason = self._freshness()
        return {"schema_version": 1, "authority_id": self.authority_id, "as_of": now,
                "last_verified_at": self._last_verified_at, "raw_cursor": self._log.raw_cursor,
                "domain_head": self._log.domain_head, "domain_ordinal": self._log.domain_ordinal,
                "fresh": fresh, "reason": reason}

    def _health_locked(self):
        result = self._metadata(self._now() if self._started else None)
        result.update({"started": self._started,
                       "pending_reboot": self._boot_pending() if self._started else None,
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
        return result

    def _observe(self):
        try:
            self._refresh_locked(verify_prefix=self._pending is not None)
        except (PalaceError, ProtocolError, JournalError, AuthorityError) as error:
            self._verified = False
            self._last_error = {"code": error.code}
            if isinstance(error, JournalError):
                self._persistence_error = True

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


def __getattr__(name):
    if name == "JournalAuthority":
        from .journal_authority import JournalAuthority
        return JournalAuthority
    raise AttributeError(name)
