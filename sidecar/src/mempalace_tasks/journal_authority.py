"""Palace-only task authority, with one local lifetime lock and no recovery files.

Public mutations require a frozen request epoch. Internal maintenance uses
bound_current(), which pins this owner incarnation rather than upgrading retries.
Restores require a quiesced owner and a new instance; live prefix loss or another
accepted activation permanently fences the current instance.
"""

from contextlib import contextmanager
from copy import deepcopy
import time
from uuid import uuid4

from .authority import AuthorityError, TaskAuthority
from .codec import canonical_json
from .model import identifier, instant
from .palace import PalaceError
from .platform_support import LifetimeLock, PlatformError, ensure_private_directory
from .protocol import ProtocolError, make_epoch
from .runtime_clock import ClockError, RuntimeClock


class _Ownership:
    def __init__(self, directory):
        self.directory = directory
        self.lock = None

    def acquire(self):
        ensure_private_directory(self.directory)
        lock = LifetimeLock(self.directory / "authority.lock")
        lock.acquire()
        self.lock = lock

    def release(self):
        if self.lock is not None:
            self.lock.release()
            self.lock = None


class StartupFenceClock:
    """Explicit legacy-maintenance adapter; pending_reboot means startup fencing.

    The wrapped RuntimeClock has no boot identity or persistence. Acknowledgment
    cannot clear this gate while inherited attempts or uncertain commands remain.
    """

    def __init__(self, authority):
        self._authority = authority

    def now(self):
        return self._authority._now()

    @property
    def pending_reboot(self):
        return self._authority._startup_pending

    def acknowledge_reboot(self, *, active_attempts):
        with self._authority.serialized():
            self._authority._require_live_owner()
            active = any(task["status"] == "in_progress"
                         for task in self._authority._log.state.tasks.values())
            if (type(active_attempts) is not int or active_attempts != 0 or active
                    or self._authority._pending is not None):
                raise AuthorityError("startup_pending", "Inherited attempts are not fully revoked")
            self._authority._startup_pending = False


class _BoundAuthority:
    """Trusted port for maintenance/supervision; never hand this to HTTP dispatch."""

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


class JournalAuthority(TaskAuthority):
    """Reuse the legacy decision, settlement, receipts and read APIs without files.

    expected_configuration is the normalized genesis configuration, not its
    command. initialize permits explicit authority_create; it never creates one.
    Start/reconcile revoke at most STARTUP_BATCH inherited attempts per call.
    LeaseMaintenance receives bound_current() and clock_source; HTTP callers
    receive neither trusted mutation shortcut.
    """

    recovery_mode = "journal"
    STARTUP_BATCH = 100

    def __init__(self, authority_id, client, runtime_dir, *, clock=None, backoff=time.sleep,
                 expected_configuration=None, initialize=False, system_actor="system",
                 recovery_actor="operator"):
        if clock is not None and (not callable(getattr(clock, "now", None))
                                  or not callable(getattr(clock, "observe", None))):
            raise AuthorityError("clock_error", "Journal clock requires now() and observe(timestamp)")
        if type(initialize) is not bool:
            raise AuthorityError("validation_error", "initialize must be boolean")
        if any(type(actor) is not str or not actor.strip() or len(actor) > 256
               for actor in (system_actor, recovery_actor)):
            raise AuthorityError("validation_error", "Recovery actors must be bounded nonempty names")
        if expected_configuration is not None and type(expected_configuration) is not dict:
            raise AuthorityError("validation_error", "Expected configuration must be an object")
        canonical_json(expected_configuration)
        self._expected_configuration = deepcopy(expected_configuration)
        self._initialize = initialize
        self._system_actor, self._recovery_actor = system_actor, recovery_actor
        self._initial_clock = clock
        self._source = clock
        self._owner_epoch = None
        self._owner_activation = None
        self._startup_pending = True
        self._fenced_error = None
        super().__init__(authority_id, client, runtime_dir, clock=self._sample_clock, backoff=backoff)
        self.clock_source = StartupFenceClock(self)
        self.runtime_dir = self.state_dir

    def _make_recovery_io(self):
        return _Ownership(self.state_dir), None, None

    @contextmanager
    def _boundary(self, *, started=True, mutation=None):
        with super()._boundary(started=started, mutation=mutation):
            try:
                yield
            except (PlatformError, ClockError) as error:
                code = "authority_locked" if error.code == "lock_busy" else error.code
                self._verified = False
                self._last_error = {"code": code}
                raise AuthorityError(code, error.message) from error

    @property
    def epoch_id(self):
        return self._owner_epoch

    def _sample_clock(self):
        if self._source is None:
            raise AuthorityError("clock_error", "Journal clock has not been initialized by replay")
        return self._source.now()

    def _observe_time(self, rebuilt):
        timestamps = [attempt["at"] for attempt in rebuilt.activation_attempts.values()
                      if attempt["outcome"] == "accepted"]
        if rebuilt.state.last_event_at is not None:
            timestamps.append(rebuilt.state.last_event_at)
        high_water = max(timestamps, key=instant) if timestamps else None
        if self._source is None:
            self._source = RuntimeClock(initial_time=high_water)
        elif high_water is not None:
            self._source.observe(high_water)

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
        # Full replay checks the live in-memory prefix even when old local files
        # describe a discarded future. No local persisted checkpoint is consulted.
        self._raise_fenced()
        self._verified = False
        try:
            rebuilt = self._audit_prefix(None)
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
            # A rejected identity is terminal. Re-read before a fresh successor.
            self._refresh_locked(verify_prefix=True)
        raise AuthorityError("activation_rejected", "Startup lost all bounded activation attempts")

    def start(self):
        with self._boundary(started=False):
            if self._started:
                self._require_live_owner()
                return self
            try:
                self._owner.acquire()
                self._owned = True
                self._pending = None
                self._owner_epoch = self._owner_activation = None
                self._startup_pending = True
                self._source = self._initial_clock
                self._started = True
                self.client.discover()
                self._refresh_locked(verify_prefix=True)
                self._activate()
                self._startup_pending = any(task["status"] == "in_progress"
                                            for task in self._log.state.tasks.values())
                self._recover_startup()
                return self
            except BaseException:
                self._started = self._verified = False
                if self._owned:
                    self._owner.release()
                    self._owned = False
                raise

    def _record_pending(self, proposal):
        self._require_live_owner()
        if proposal["event"]["command"]["operation"] == "authority_create":
            self._validate_configuration(proposal["event"]["configuration"])
        self._pending = {"proposal": deepcopy(proposal), "settlement": None}

    def _record_settlement(self, settlement):
        self._require_live_owner()
        self._pending["settlement"] = deepcopy(settlement)

    def _persist_head(self):
        self._require_live_owner()
        if not self._verified:
            raise AuthorityError("not_current", "Journal head has not been verified")

    def _finish_pending(self, outcome):
        self._persist_head()
        self._pending = None
        self._last_error = None
        return outcome

    def _proposal_outcome(self, proposal, log):
        self._require_live_owner()
        if proposal.get("epoch_id") != self._owner_epoch or log.epoch_id != self._owner_epoch:
            raise AuthorityError("stale_epoch", "Pending request belongs to another epoch")
        return super()._proposal_outcome(proposal, log)

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
            return super().execute(command)

    def _execute_current(self, command, expected_epoch):
        with self._boundary():
            self._check_epoch(expected_epoch)
            return super().execute(command)

    def execute_current(self, command):
        """Trusted, process-local mutation only; transport dispatch must use execute."""
        return self._execute_current(command, self._owner_epoch)

    def bound_current(self):
        """Freeze an internal executor for LeaseMaintenance and HostSupervisor."""
        with self._boundary():
            self._require_live_owner()
            return _BoundAuthority(self, self._owner_epoch)

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

    def _reconcile_locked(self):
        self._refresh_locked(verify_prefix=True)
        self._require_live_owner()
        outcome = self._resolve_pending() if self._pending is not None else None
        self._recover_startup()
        return outcome

    def _freshness(self):
        if not self._started:
            return False, "not_started"
        if self._fenced_error is not None:
            return False, self._fenced_error
        if self._startup_pending:
            return False, "startup_pending"
        return super()._freshness()

    def _metadata(self, now):
        return {**super()._metadata(now), "epoch_id": self._owner_epoch,
                "startup_pending": self._startup_pending,
                "request_epoch_required": True, "recovery_mode": self.recovery_mode}

    def _receipt(self, outcome, *, replayed):
        receipt = super()._receipt(outcome, replayed=replayed)
        epoch_id = outcome.get("epoch_id")
        receipt["epoch_id"] = epoch_id
        if epoch_id != self._owner_epoch:
            receipt["authorization"]["fresh"] = False
            for task in receipt["authorization"]["tasks"]:
                task.update(authorized=False, lease_live=False, lease_expires_at=None,
                            reason="stale_epoch")
        return receipt

    def outcome(self, epoch_id, command_id):
        """Historical evidence, not renewed authorization or proof of no external effects."""
        with self._boundary():
            if epoch_id is not None:
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
