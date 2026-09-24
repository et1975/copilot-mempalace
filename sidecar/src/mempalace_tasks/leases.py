"""Durable effective time, independent of task and protocol state.

The caller must hold the journal's authority lock and serialize access to the
clock store. A pending reboot permits timestamps for recovery, not new lease
grants: the caller must revoke every old active attempt before acknowledging it.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from .journal import JsonStore


_SECOND_NS = 1_000_000_000
_MAX_EFFECTIVE_NS = 253_402_300_799_999_999_999
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MISSING = object()
_STATE_FIELDS = frozenset(
    {
        "version",
        "boot_id",
        "effective_ns",
        "monotonic_ns",
        "pending_reboot",
        "previous_boot_id",
    }
)


class ClockError(Exception):
    """A clock configuration, sample, discontinuity, or state failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _valid_boot_id(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and value.isprintable()
    )


def _read_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ClockError("boot_id_unavailable", "Cannot read the boot identity") from exc
    return value.strip()


def _validate_ns(value: object, field: str, code: str) -> None:
    if type(value) is not int or value < 0:
        raise ClockError(code, f"{field} must be a nonnegative integer")
    if field != "monotonic_ns" and value > _MAX_EFFECTIVE_NS:
        raise ClockError(code, f"{field} is outside the supported UTC range")


def _validate_state(value: object) -> dict:
    code = "invalid_clock_state"
    if type(value) is not dict or set(value) != _STATE_FIELDS:
        raise ClockError(code, "Clock state has invalid or missing fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ClockError(code, "Unsupported clock state version")
    if not _valid_boot_id(value["boot_id"]):
        raise ClockError(code, "Clock state has an invalid boot identity")
    _validate_ns(value["effective_ns"], "effective_ns", code)
    _validate_ns(value["monotonic_ns"], "monotonic_ns", code)
    if type(value["pending_reboot"]) is not bool:
        raise ClockError(code, "Clock state has an invalid reboot flag")
    if value["pending_reboot"]:
        if (
            not _valid_boot_id(value["previous_boot_id"])
            or value["previous_boot_id"] == value["boot_id"]
        ):
            raise ClockError(code, "Pending reboot has an invalid previous identity")
    elif value["previous_boot_id"] is not None:
        raise ClockError(code, "Acknowledged clock state retains a previous identity")
    return value


def _format_utc(effective_ns: int) -> str:
    seconds, remainder = divmod(effective_ns, _SECOND_NS)
    value = _EPOCH + timedelta(seconds=seconds, microseconds=remainder // 1_000)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class EffectiveClock:
    """Persist nanosecond anchors before exposing microsecond UTC timestamps.

    ``initialize=True`` permits a missing store only during construction; it
    never replaces corrupt or unsupported state. Same-boot construction retains
    the persisted anchors. A different boot records recovery as pending before
    construction succeeds; process restarts alone never acknowledge recovery.

    Samples must be nonnegative integer nanoseconds (not booleans), and wall/
    effective time must fit UTC years 1970 through 9999. Output truncates the
    submicrosecond remainder, but the complete nanosecond anchor is retained.
    Journal errors, including ambiguous persistence failures, propagate intact.
    """

    def __init__(
        self,
        store: JsonStore,
        *,
        wall_time_ns: Callable[[], int] | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        boot_id: str | None = None,
        initialize: bool = False,
    ):
        if type(initialize) is not bool:
            raise ClockError("invalid_clock_config", "initialize must be a boolean")
        self._store = store
        self._wall_time_ns = time.time_ns if wall_time_ns is None else wall_time_ns
        self._monotonic_ns = time.monotonic_ns if monotonic_ns is None else monotonic_ns
        if not callable(self._wall_time_ns) or not callable(self._monotonic_ns):
            raise ClockError("invalid_clock_config", "Clock providers must be callable")
        self._boot_id = _read_boot_id() if boot_id is None else boot_id
        if not _valid_boot_id(self._boot_id):
            raise ClockError("invalid_clock_config", "Invalid boot identity")

        stored = self._store.read(default=_MISSING)
        if stored is _MISSING:
            if not initialize:
                raise ClockError("missing_clock_state", "Clock state is missing")
            wall_ns, monotonic_sample = self._sample()
            self._store.write(
                {
                    "version": 1,
                    "boot_id": self._boot_id,
                    "effective_ns": wall_ns,
                    "monotonic_ns": monotonic_sample,
                    "pending_reboot": False,
                    "previous_boot_id": None,
                }
            )
            return

        state = _validate_state(stored)
        if state["boot_id"] != self._boot_id:
            wall_ns, monotonic_sample = self._sample()
            self._store.write(
                {
                    **state,
                    "boot_id": self._boot_id,
                    "effective_ns": max(state["effective_ns"], wall_ns),
                    "monotonic_ns": monotonic_sample,
                    "pending_reboot": True,
                    "previous_boot_id": state["boot_id"],
                }
            )

    def _sample(self) -> tuple[int, int]:
        try:
            wall_ns = self._wall_time_ns()
            monotonic_sample = self._monotonic_ns()
        except Exception as exc:
            raise ClockError(
                "clock_sample_failed", "Cannot sample the wall or monotonic clock"
            ) from exc
        _validate_ns(wall_ns, "wall_time_ns", "invalid_clock_sample")
        _validate_ns(monotonic_sample, "monotonic_ns", "invalid_clock_sample")
        return wall_ns, monotonic_sample

    def _read_existing(self) -> dict:
        stored = self._store.read(default=_MISSING)
        if stored is _MISSING:
            raise ClockError("missing_clock_state", "Existing clock state is missing")
        state = _validate_state(stored)
        if state["boot_id"] != self._boot_id:
            raise ClockError(
                "clock_state_changed", "Clock state belongs to a different boot"
            )
        return state

    @property
    def pending_reboot(self) -> bool:
        """Whether the caller still needs to revoke old active attempts."""
        return self._read_existing()["pending_reboot"]

    def now(self) -> str:
        """Return UTC ISO-8601 with six fractional digits and a ``Z`` suffix."""
        state = self._read_existing()
        wall_ns, monotonic_sample = self._sample()
        delta = monotonic_sample - state["monotonic_ns"]
        if delta < 0:
            raise ClockError(
                "monotonic_discontinuity", "Same-boot monotonic time moved backwards"
            )
        effective_ns = max(state["effective_ns"] + delta, wall_ns)
        _validate_ns(effective_ns, "effective_ns", "invalid_clock_sample")
        formatted = _format_utc(effective_ns)
        self._store.write(
            {
                **state,
                "effective_ns": effective_ns,
                "monotonic_ns": monotonic_sample,
            }
        )
        return formatted

    def acknowledge_reboot(self, *, active_attempts: int) -> None:
        """Clear recovery only after the caller reports every old attempt revoked."""
        if type(active_attempts) is not int or active_attempts < 0:
            raise ClockError(
                "invalid_active_attempts", "active_attempts must be a nonnegative integer"
            )
        if active_attempts:
            raise ClockError(
                "active_attempts_remaining", "Cannot acknowledge with active attempts"
            )
        state = self._read_existing()
        # A previous replace may be visible despite a failed directory fsync.
        self._store.write(
            {**state, "pending_reboot": False, "previous_boot_id": None}
        )
