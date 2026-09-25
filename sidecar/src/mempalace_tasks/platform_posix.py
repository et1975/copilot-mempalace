"""POSIX no-follow I/O, private local locks, and native suspend-inclusive time."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
import threading
import time
from uuid import uuid4

from .platform_support import PlatformError

if os.name == "posix":
    import fcntl
else:
    fcntl = None


def _require() -> None:
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise PlatformError("unsupported_platform", "Native POSIX no-follow I/O is unavailable")


def _path(value) -> Path:
    _require()
    try:
        text = os.fspath(value)
        path = Path(text)
    except (TypeError, ValueError) as error:
        raise PlatformError("unsafe_path", "Expected an absolute native path") from error
    if (type(text) is not str or not path.is_absolute() or str(path) != text
            or ".." in path.parts or text.startswith("//")
            or any(ord(character) < 32 for character in text)):
        raise PlatformError("unsafe_path", "Expected an absolute, non-aliased native path")
    return path


def _io(error: OSError) -> PlatformError:
    if error.errno in (errno.ELOOP, errno.ENOTDIR):
        return PlatformError("unsafe_path", "Link or non-directory in path")
    if error.errno == errno.ENOENT:
        return PlatformError("not_found", "Required path does not exist")
    if error.errno == errno.EEXIST:
        return PlatformError("already_exists", "Exclusive creation requires a new file")
    return PlatformError("filesystem_error", "Native filesystem operation failed")


@contextmanager
def _directory(path: Path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def canonical_path(value) -> Path:
    path = _path(value)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = None
    try:
        descriptor = os.open(path.anchor, flags)
        for index, component in enumerate(path.parts[1:]):
            try:
                info = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(info.st_mode):
                raise PlatformError("unsafe_path", "Symlink paths are not supported")
            if index < len(path.parts) - 2:
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        return path
    except OSError as error:
        raise _io(error) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private(info: os.stat_result) -> None:
    if info.st_uid != os.geteuid() or info.st_mode & 0o7077:
        raise PlatformError("unsafe_permissions", "Object must be private and owned by this user")


def _regular(info: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PlatformError("unsafe_file", "Expected a singly linked regular file")
    if private:
        _private(info)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _unchanged(directory: int, name: str, opened: os.stat_result) -> None:
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if _identity(current) != _identity(opened):
        raise PlatformError("path_changed", "Path changed while the object was open")


def _open(directory: int, name: str, flags: int, *, private: bool) -> int:
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        _regular(opened, private=private)
        _unchanged(directory, name, opened)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_regular(value, *, private: bool = False, maximum: int = 1_048_576) -> bytes:
    path = _path(value)
    try:
        with _directory(path.parent) as directory:
            descriptor = _open(directory, path.name, os.O_RDONLY, private=private)
            try:
                before = os.fstat(descriptor)
                if before.st_size > maximum:
                    raise PlatformError("file_too_large", "File exceeds the read limit")
                chunks = []
                remaining = maximum + 1
                while remaining:
                    chunk = os.read(descriptor, min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                _regular(after, private=private)
                _unchanged(directory, path.name, after)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise PlatformError("file_changed", "File changed while it was read")
                data = b"".join(chunks)
                if len(data) > maximum:
                    raise PlatformError("file_too_large", "File exceeds the read limit")
                return data
            finally:
                os.close(descriptor)
    except OSError as error:
        raise _io(error) from error


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        count = os.write(descriptor, remaining)
        if count <= 0:
            raise PlatformError("short_write", "File write made no progress")
        remaining = remaining[count:]


def _remove_created(directory: int, name: str, opened: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(current) == _identity(opened):
        os.unlink(name, dir_fd=directory)


def _create(directory: int, name: str, data: bytes) -> os.stat_result:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=directory)
    opened = None
    try:
        opened = os.fstat(descriptor)
        _regular(opened, private=True)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        _regular(os.fstat(descriptor), private=True)
        _unchanged(directory, name, opened)
        return opened
    except BaseException:
        if opened is not None:
            _remove_created(directory, name, opened)
        raise
    finally:
        os.close(descriptor)


def create_private(value, data: bytes) -> None:
    path = _path(value)
    try:
        with _directory(path.parent) as directory:
            _create(directory, path.name, data)
    except OSError as error:
        raise _io(error) from error


def ensure_private_directory(value) -> Path:
    path = _path(value)
    if path == Path(path.anchor):
        raise PlatformError("unsafe_path", "A filesystem root is not a private directory")
    try:
        with _directory(path.parent) as parent:
            created = False
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
            descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                 | os.O_CLOEXEC, dir_fd=parent)
            try:
                _private(os.fstat(descriptor))
                if created:
                    os.fchmod(descriptor, 0o700)
                _unchanged(parent, path.name, os.fstat(descriptor))
            finally:
                os.close(descriptor)
        return path
    except OSError as error:
        raise _io(error) from error


def atomic_write_cache(value, data: bytes) -> None:
    path = _path(value)
    candidate = f".{path.name}.{uuid4().hex}.cache"
    try:
        with _directory(path.parent) as directory:
            _private(os.fstat(directory))
            try:
                existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                _regular(existing, private=True)
            created = _create(directory, candidate, data)
            try:
                if existing is not None:
                    _unchanged(directory, path.name, existing)
                else:
                    try:
                        os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise PlatformError("path_changed", "Cache destination appeared during publication")
                os.replace(candidate, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                _remove_created(directory, candidate, created)
    except OSError as error:
        raise _io(error) from error


_fork_guard = threading.Lock()
_active_locks: set[LifetimeLock] = set()


class LifetimeLock:
    def __init__(self, path):
        self.path = _path(path)
        self._fd: int | None = None

    def acquire(self):
        with _fork_guard:
            if self._fd is not None:
                raise PlatformError("lock_busy", "This lock is already acquired")
            descriptor = None
            try:
                with _directory(self.path.parent) as directory:
                    _private(os.fstat(directory))
                    try:
                        descriptor = _open(directory, self.path.name,
                                           os.O_RDWR | os.O_CREAT | os.O_EXCL, private=True)
                        os.fchmod(descriptor, 0o600)
                    except FileExistsError:
                        descriptor = _open(directory, self.path.name, os.O_RDWR, private=True)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as error:
                        if error.errno in (errno.EACCES, errno.EAGAIN):
                            raise PlatformError("lock_busy", "Another process owns the lock") from error
                        raise
                    _unchanged(directory, self.path.name, os.fstat(descriptor))
                    self._fd = descriptor
                    _active_locks.add(self)
                    descriptor = None
                    return self
            except OSError as error:
                raise _io(error) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _close(self) -> None:
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            _active_locks.discard(self)
            # Never LOCK_UN: a fork shares the parent's open-file description.
            os.close(descriptor)

    def release(self) -> None:
        with _fork_guard:
            try:
                self._close()
            except OSError as error:
                raise _io(error) from error


def _child_close() -> None:
    try:
        for lock in tuple(_active_locks):
            lock._close()
    finally:
        _fork_guard.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_fork_guard.acquire, after_in_parent=_fork_guard.release,
                        after_in_child=_child_close)


_mach = None


def continuous_time_ns() -> int:
    global _mach
    if sys.platform.startswith("linux"):
        if not hasattr(time, "CLOCK_BOOTTIME"):
            raise PlatformError("clock_unavailable", "CLOCK_BOOTTIME is unavailable")
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":
        if _mach is None:
            class Timebase(ctypes.Structure):
                _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            function = library.mach_continuous_time
            function.argtypes = []
            function.restype = ctypes.c_uint64
            timebase = library.mach_timebase_info
            timebase.argtypes = [ctypes.POINTER(Timebase)]
            timebase.restype = ctypes.c_int
            ratio = Timebase()
            if timebase(ctypes.byref(ratio)) != 0 or not ratio.numer or not ratio.denom:
                raise PlatformError("clock_unavailable", "Invalid Mach continuous timebase")
            _mach = function, ratio.numer, ratio.denom
        function, numerator, denominator = _mach
        return int(function()) * numerator // denominator
    raise PlatformError("clock_unavailable", "No suspend-inclusive native clock")
