"""Process-local effective UTC time, reinitialized from journal replay.

No boot identity, persistence, or restore high-water file belongs here. Each
owner incarnation starts a new continuous anchor. The authority is responsible
for fencing inherited attempts before using this clock to grant new leases.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import os
import re
import threading
import time

from .platform_support import continuous_time_ns as _continuous_time_ns


_SECOND = 1_000_000_000
_MAX_UTC = 253_402_300_799_999_999_999
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)",
    re.ASCII,
)


class ClockError(Exception):
    """Explicit configuration, sample, timestamp, or discontinuity failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _checked(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ClockError("invalid_clock_sample", "Clock value is outside the supported range")
    return value


def _parse(timestamp: str) -> int:
    match = _TIMESTAMP.fullmatch(timestamp) if type(timestamp) is str else None
    if match is None:
        raise ClockError("invalid_timestamp", "Expected a bounded UTC timestamp")
    try:
        date = datetime.fromisoformat(match[1]).replace(tzinfo=timezone.utc)
        elapsed = date - _EPOCH
        value = (elapsed.days * 86400 + elapsed.seconds) * _SECOND
        value += int((match[2] or "").ljust(9, "0"))
        if not 0 <= value <= _MAX_UTC:
            raise ValueError("out of range")
        return value
    except ValueError as error:
        raise ClockError("invalid_timestamp", "Expected a bounded UTC timestamp") from error


def _format(value: int) -> str:
    seconds, remainder = divmod(value, _SECOND)
    date = _EPOCH + timedelta(seconds=seconds, microseconds=remainder // 1000)
    return date.isoformat(timespec="microseconds").replace("+00:00", "Z")


class RuntimeClock:
    """Effective=max(previous effective+continuous delta, wall, observed bound).

    Sampling the continuous anchor *before* wall sampling or later I/O ensures
    provider/persistence latency is debited on the next observation. Submicro-
    second precision is retained internally; UTC strings have six decimals.
    Failed samples/observations do not commit an anchor. Instances must not be
    reused across fork; construct a new one from replay for a new incarnation.
    """

    def __init__(self, initial_time: str | None = None, *,
                 wall_time_ns: Callable[[], int] | None = None,
                 continuous_time_ns: Callable[[], int] | None = None):
        self._wall = time.time_ns if wall_time_ns is None else wall_time_ns
        self._continuous = _continuous_time_ns if continuous_time_ns is None else continuous_time_ns
        if not callable(self._wall) or not callable(self._continuous):
            raise ClockError("invalid_clock_config", "Clock providers must be callable")
        initial = 0 if initial_time is None else _parse(initial_time)
        self._pid = os.getpid()
        self._guard = threading.Lock()
        wall, continuous = self._sample()
        self._effective = max(initial, wall)
        self._anchor = continuous

    def _sample(self) -> tuple[int, int]:
        try:
            continuous = self._continuous()
            wall = self._wall()
        except Exception as error:
            raise ClockError("clock_sample_failed", "Cannot sample the safety clock") from error
        return _checked(wall, _MAX_UTC), _checked(continuous, 2**64 - 1)

    def _check_process(self) -> None:
        if self._pid != os.getpid():
            raise ClockError("clock_process_changed", "Construct a new clock after fork")

    def _advance(self, lower_bound: int = 0) -> str:
        wall, continuous = self._sample()
        if continuous < self._anchor:
            raise ClockError("continuous_discontinuity", "Continuous time moved backwards")
        effective = _checked(max(self._effective + continuous - self._anchor,
                                 wall, lower_bound), _MAX_UTC)
        formatted = _format(effective)
        self._effective = effective
        self._anchor = continuous
        return formatted

    def now(self) -> str:
        self._check_process()
        with self._guard:
            return self._advance()

    def observe(self, timestamp: str) -> None:
        """Advance/reanchor from a verified replay timestamp without persistence."""
        self._check_process()
        value = _parse(timestamp)
        with self._guard:
            self._advance(value)
