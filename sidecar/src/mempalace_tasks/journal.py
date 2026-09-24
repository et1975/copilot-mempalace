"""Private, local POSIX recovery files; never a replacement for the task log.

Callers hold AuthorityLock and serialize their own read/modify/write operations.
A successful write includes file and directory fsync. An I/O error after replace
or unlink has an ambiguous durability outcome and must gate upstream dispatch.
Only an authority-confirmed terminal outcome permits pending clear/archive.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import stat
import threading
from typing import Iterator
from uuid import uuid4

if os.name == "posix":
    import fcntl
else:
    fcntl = None


class JournalError(Exception):
    """Explicit state, path, ownership, or persistence failure."""

    def __init__(
        self, code: str, message: str, *, path: Path | None = None, ambiguous: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = str(path) if path is not None else None
        self.ambiguous = ambiguous


_MISSING = object()
_active_locks: set[AuthorityLock] = set()
_fork_guard = threading.Lock()


def _require_posix() -> None:
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise JournalError("unsupported_platform", "The journal requires POSIX no-follow I/O and flock")


def _path(path: str | os.PathLike[str]) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = Path.cwd() / value
    if not value.name or value.name == ".." or "\x00" in str(value):
        raise JournalError("unsafe_path", "State must name a file or directory", path=value)
    # Do not resolve symlinks or collapse '..' before descriptor traversal.
    return value


def _validate_json(value: object) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            key.encode("utf-8")
            _validate_json(item)
        return
    raise ValueError("State must contain only finite JSON-native values")


def _encode(value: object) -> bytes:
    try:
        _validate_json(value)
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise JournalError("invalid_state", "Cannot encode recovery state as strict JSON") from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(data: bytes, path: Path) -> object:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
        _validate_json(value)
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise JournalError("corrupt_state", "Recovery file is not valid strict JSON", path=path) from error


def _unsafe(path: Path, message: str) -> JournalError:
    return JournalError("unsafe_path", message, path=path)


def _owned(info: os.stat_result, path: Path) -> None:
    if info.st_uid != os.geteuid():
        raise _unsafe(path, "Recovery state must be owned by the current user")


def _regular(info: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _unsafe(path, "Recovery files must be regular files with no symlinks or hardlinks")
    _owned(info, path)


def _stat_at(directory: int, path: Path) -> os.stat_result | None:
    try:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _regular(info, path)
    return info


@contextmanager
def _directory(path: Path, *, create: bool) -> Iterator[int | None]:
    _require_posix()
    if Path(os.path.abspath(path)) == Path(path.anchor):
        raise _unsafe(path, "The filesystem root cannot be a private state directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.anchor, flags)
    try:
        for index, part in enumerate(path.parts[1:]):
            component = Path(path.anchor).joinpath(*path.parts[1:index + 2])
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    # Another startup may have created the directory before locking.
                    pass
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise _unsafe(component, "State directory components must not be symlinks")
            if create:
                # Existence alone does not prove an earlier mkdir was durably synced.
                os.fsync(fd)
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _unsafe(component, "State directory changed during validation") from error
                raise
            os.close(fd)
            fd = child
        _owned(os.fstat(fd), path)
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def _open_at(directory: int, path: Path, flags: int) -> int:
    previous = _stat_at(directory, path)
    try:
        fd = os.open(
            path.name,
            flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise _unsafe(path, "Recovery file changed to a symlink") from error
        raise
    valid = False
    try:
        current = os.fstat(fd)
        _regular(current, path)
        if previous is not None and (previous.st_dev, previous.st_ino) != (
            current.st_dev, current.st_ino
        ):
            raise _unsafe(path, "Recovery file changed during validation")
        os.fchmod(fd, 0o600)
        valid = True
        return fd
    finally:
        if not valid:
            os.close(fd)


def _read_at(directory: int, path: Path) -> object:
    if _stat_at(directory, path) is None:
        return _MISSING
    fd = _open_at(directory, path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as source:
        return _decode(source.read(), path)


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "Write made no progress")
        remaining = remaining[written:]


def _io_error(path: Path, error: OSError, *, ambiguous: bool = False) -> JournalError:
    return JournalError(
        "io_error", f"Recovery state I/O failed: {error.strerror or error}",
        path=path, ambiguous=ambiguous,
    )


def _claim_lock(fd: int, path: Path) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise JournalError(
            "authority_locked", "Another local owner is acquiring or holds this authority",
            path=path,
        ) from error


class JsonStore:
    """Atomic strict-JSON storage, also usable for clock and process metadata."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = _path(path)

    def read(self, default: object = None) -> object:
        try:
            with _directory(self.path.parent, create=False) as directory:
                value = _MISSING if directory is None else _read_at(directory, self.path)
                return default if value is _MISSING else value
        except OSError as error:
            raise _io_error(self.path, error) from error

    def write(self, value: object) -> None:
        data = _encode(value)
        replaced = False
        try:
            with _directory(self.path.parent, create=True) as directory:
                assert directory is not None
                _read_at(directory, self.path)
                temporary = f".{self.path.name}.{uuid4().hex}.tmp"
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    try:
                        os.fchmod(fd, 0o600)
                        _write_all(fd, data)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    _stat_at(directory, self.path)
                    os.replace(
                        temporary, self.path.name,
                        src_dir_fd=directory, dst_dir_fd=directory,
                    )
                    replaced = True
                    os.fsync(directory)
                finally:
                    if not replaced:
                        os.unlink(temporary, dir_fd=directory)
        except OSError as error:
            raise _io_error(self.path, error, ambiguous=replaced) from error

    def clear(self) -> None:
        """Remove only this valid file; callers must first resolve uncertainty."""
        removed = False
        try:
            with _directory(self.path.parent, create=False) as directory:
                if directory is None or _read_at(directory, self.path) is _MISSING:
                    return
                os.unlink(self.path.name, dir_fd=directory)
                removed = True
                os.fsync(directory)
        except OSError as error:
            raise _io_error(self.path, error, ambiguous=removed) from error


def _object(value: object, code: str, path: Path) -> dict:
    if type(value) is not dict:
        raise JournalError(code, "Recovery payload must be a JSON object", path=path)
    return value


class PendingStore(JsonStore):
    """Exact proposal/settlement envelope, never cleared as error recovery.

    write(proposal) preserves any existing settlement. Idempotent writes still
    fsync: a previous attempt may have failed after rename but before directory
    sync. clear/archive are explicit terminal-outcome operations, not retries.
    """

    def __init__(self, state_dir: str | os.PathLike[str]):
        super().__init__(Path(state_dir) / "pending.json")

    def read(self) -> dict | None:
        value = super().read(_MISSING)
        if value is _MISSING:
            return None
        value = _object(value, "corrupt_state", self.path)
        if set(value) != {"proposal", "settlement"}:
            raise JournalError("corrupt_state", "Invalid pending envelope", path=self.path)
        _object(value["proposal"], "corrupt_state", self.path)
        if value["settlement"] is not None:
            _object(value["settlement"], "corrupt_state", self.path)
        return value

    def write(self, proposal: dict) -> None:
        _object(proposal, "invalid_state", self.path)
        encoded = _encode(proposal)
        pending = self.read()
        if pending is None:
            pending = {"proposal": proposal, "settlement": None}
        elif _encode(pending["proposal"]) != encoded:
            raise JournalError("pending_conflict", "An unresolved proposal already exists", path=self.path)
        super().write(pending)

    def attach_settlement(self, settlement: dict) -> None:
        _object(settlement, "invalid_state", self.path)
        encoded = _encode(settlement)
        pending = self.read()
        if pending is None:
            raise JournalError("missing_pending", "No proposal exists for settlement", path=self.path)
        if pending["settlement"] is not None and _encode(pending["settlement"]) != encoded:
            raise JournalError("settlement_conflict", "The exact settlement is already fixed", path=self.path)
        pending["settlement"] = settlement
        super().write(pending)

    def clear(self) -> None:
        self.read()
        super().clear()

    def archive(self, reason: str) -> Path | None:
        """Durably copy an audit envelope before clearing confirmed pending."""
        if type(reason) is not str or not reason.strip():
            raise JournalError("invalid_state", "Archive requires an explicit reason", path=self.path)
        pending = self.read()
        if pending is None:
            return None
        archive = self.path.with_name(f"pending.archive-{uuid4().hex}.json")
        JsonStore(archive).write({"reason": reason, "pending": pending})
        self.clear()
        return archive


class HeadStore(JsonStore):
    """Dual raw/domain checkpoint; protocol validation remains with the caller."""

    def __init__(self, state_dir: str | os.PathLike[str]):
        super().__init__(Path(state_dir) / "verified_head.json")

    def _validate(self, value: object, code: str) -> dict:
        value = _object(value, code, self.path)
        if set(value) != {"raw_cursor", "raw_hash", "domain_head", "domain_ordinal"}:
            raise JournalError(code, "Invalid verified-head fields", path=self.path)
        for field in ("raw_cursor", "raw_hash", "domain_head"):
            if value[field] is not None and (type(value[field]) is not str or not value[field]):
                raise JournalError(code, f"Invalid {field}", path=self.path)
        ordinal = value["domain_ordinal"]
        if type(ordinal) is not int or ordinal < 0:
            raise JournalError(code, "Invalid domain ordinal", path=self.path)
        if (
            (value["raw_cursor"] is None) != (value["raw_hash"] is None)
            or (ordinal == 0) != (value["domain_head"] is None)
            or (ordinal > 0 and value["raw_cursor"] is None)
        ):
            raise JournalError(code, "Inconsistent verified heads", path=self.path)
        return value

    def read(self) -> dict | None:
        value = super().read(_MISSING)
        if value is _MISSING:
            return None
        return self._validate(value, "corrupt_state")

    def write(self, value: dict) -> None:
        self._validate(value, "invalid_state")
        self.read()
        super().write(value)

    def clear(self) -> None:
        self.read()
        super().clear()


class AuthorityLock:
    """Nonblocking single-host owner lock at the canonical state directory.

    Aliases reach the same persistent lock inode. Different state directories do
    not coordinate, and this is neither a distributed fence nor a hostile-writer
    boundary. The file is never unlinked; the OS releases ownership at exit.
    Acquired handles remain owned until explicit release or process exit.
    """

    def __init__(self, state_dir: str | os.PathLike[str], authority_id: str):
        if type(authority_id) is not str or not authority_id.strip():
            raise JournalError("invalid_state", "Authority ID must be a nonempty string")
        self.path = _path(Path(state_dir) / "authority.lock")
        self.authority_id = authority_id
        self._fd: int | None = None

    def acquire(self) -> AuthorityLock:
        _require_posix()
        with _fork_guard:
            return self._acquire_locked()

    def _acquire_locked(self) -> AuthorityLock:
        if self._fd is not None:
            raise JournalError("authority_locked", "This lock is already acquired", path=self.path)
        fd = None
        try:
            with _directory(self.path.parent, create=True) as directory:
                assert directory is not None
                # The stable directory inode guards publication through metadata sync.
                _claim_lock(directory, self.path)
                created = False
                try:
                    fd = _open_at(directory, self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                    created = True
                except FileExistsError:
                    fd = _open_at(directory, self.path, os.O_RDWR)
                _claim_lock(fd, self.path)
                with os.fdopen(os.dup(fd), "rb") as source:
                    data = source.read()
                if not created or data:
                    metadata = _decode(data, self.path)
                    if (
                        type(metadata) is not dict
                        or set(metadata) != {"authority_id"}
                        or type(metadata["authority_id"]) is not str
                        or not metadata["authority_id"].strip()
                    ):
                        raise JournalError("corrupt_state", "Invalid authority lock metadata", path=self.path)
                    if metadata["authority_id"] != self.authority_id:
                        raise JournalError(
                            "authority_mismatch", "State directory belongs to another authority",
                            path=self.path,
                        )
                else:
                    _write_all(fd, _encode({"authority_id": self.authority_id}))
                os.fsync(fd)
                os.fsync(directory)
            self._fd = fd
            _active_locks.add(self)
            fd = None
            return self
        except OSError as error:
            raise _io_error(self.path, error) from error
        finally:
            if fd is not None:
                os.close(fd)

    def release(self) -> None:
        with _fork_guard:
            try:
                self._release_locked()
            except OSError as error:
                raise _io_error(self.path, error) from error

    def _release_locked(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            _active_locks.discard(self)

    def __enter__(self) -> AuthorityLock:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _close_inherited_locks() -> None:
    # Close, never LOCK_UN: fork shares the parent's open-file description.
    try:
        for lock in tuple(_active_locks):
            lock._release_locked()
    finally:
        _fork_guard.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_fork_guard.acquire,
        after_in_parent=_fork_guard.release,
        after_in_child=_close_inherited_locks,
    )
