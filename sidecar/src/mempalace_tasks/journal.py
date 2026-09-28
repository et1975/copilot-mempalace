"""Private POSIX JSON worker artifacts, not authority operational storage.

Used only by the optional Linux executor for accepted results/checkpoints.
Callers serialize their own read/modify/write operations. Writes include file
and directory fsync; errors after replace/unlink report ambiguous publication.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import stat
from typing import Iterator
from uuid import uuid4


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


def _require_posix() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise JournalError("unsupported_platform", "Worker artifacts require POSIX no-follow I/O")


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
        raise JournalError("invalid_state", "Cannot encode worker artifact as strict JSON") from error


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
        "io_error", f"Worker artifact I/O failed: {error.strerror or error}",
        path=path, ambiguous=ambiguous,
    )


class JsonStore:
    """Atomic strict-JSON storage for optional Linux worker artifacts."""

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
