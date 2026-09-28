"""Native, fail-closed primitives for host configuration and disposable state.

These operations do not implement acknowledged task storage. In particular,
cache publication is atomic visibility, not a power-loss durability promise.
Paths must be absolute and unaliased; directory creation is explicit and
leaf-only. Reads never repair permissions or create directories.
"""

from __future__ import annotations

import os
from pathlib import Path
import threading


class PlatformError(Exception):
    """Safe operational failure; ``code`` is suitable for programmatic handling."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _backend():
    if os.name == "posix":
        from . import platform_posix
        return platform_posix
    if os.name == "nt":
        from . import platform_windows
        return platform_windows
    raise PlatformError("unsupported_platform", "No native platform implementation")


def canonical_path(path: str | os.PathLike[str]) -> Path:
    """Validate a native path and existing ancestors without following links."""
    return _backend().canonical_path(path)


def read_regular(path: str | os.PathLike[str], *, private: bool = False,
                 maximum: int = 1_048_576) -> bytes:
    """Read a bounded, singly linked regular file, validating the opened object."""
    if type(maximum) is not int or not 0 <= maximum <= 2**63 - 2:
        raise PlatformError("invalid_argument", "Read limit must be a nonnegative integer")
    if type(private) is not bool:
        raise PlatformError("invalid_argument", "Private flag must be a boolean")
    return _backend().read_regular(path, private=private, maximum=maximum)


def _bytes(data: bytes) -> None:
    if type(data) is not bytes:
        raise PlatformError("invalid_argument", "File contents must be bytes")


def create_private(path: str | os.PathLike[str], data: bytes) -> None:
    """Exclusively create a private file in an existing parent; never overwrite."""
    _bytes(data)
    _backend().create_private(path, data)


def ensure_private_directory(path: str | os.PathLike[str]) -> Path:
    """Create/secure only a new intended leaf; validate, never repair, existing."""
    return _backend().ensure_private_directory(path)


def atomic_write_cache(path: str | os.PathLike[str], data: bytes) -> None:
    """Publish disposable bytes atomically in an existing private directory."""
    _bytes(data)
    _backend().atomic_write_cache(path, data)


class LifetimeLock:
    """Nonblocking, process-lifetime OS ownership, never PID-based authority.

    The parent must already be private. The stable lock file is never removed
    or replaced, even on failure/release. This is local exclusion, not a
    distributed fence or protection from another process of the same user.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._lock = _backend().LifetimeLock(path)
        self.path = self._lock.path

    def acquire(self) -> LifetimeLock:
        self._lock.acquire()
        return self

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> LifetimeLock:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


_time_guard = threading.Lock()
_last_continuous: int | None = None


def _after_fork() -> None:
    global _time_guard, _last_continuous
    _time_guard = threading.Lock()
    _last_continuous = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def continuous_time_ns() -> int:
    """Suspend-inclusive elapsed time, never substituted with wall/awake time."""
    global _last_continuous
    with _time_guard:
        try:
            value = _backend().continuous_time_ns()
        except PlatformError:
            raise
        except Exception as error:
            raise PlatformError("clock_unavailable", "Cannot sample continuous time") from error
        if type(value) is not int or not 0 <= value <= 2**64 - 1:
            raise PlatformError("invalid_clock_sample", "Invalid continuous time sample")
        if _last_continuous is not None and value < _last_continuous:
            raise PlatformError("continuous_discontinuity", "Continuous time moved backwards")
        _last_continuous = value
        return value
