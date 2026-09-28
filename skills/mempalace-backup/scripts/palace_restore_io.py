"""Stdlib-only offline restore guards for the independently deployed backup skill.

These guards do not interpret task records. Task validation and epoch activation
remain in the optional task package. Lock and registry names match MemPalace's
cooperative writer boundary; raw writers and new launches must remain offline.
"""

from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4


MAX_TREE_ENTRIES = 1_000_000


class RestoreIOError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def checked_path(path, *, directory=False, optional=False):
    raw = Path(path).expanduser().absolute()
    for component in (*reversed(raw.parents), raw):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise RestoreIOError("unsafe_path", f"Restore cannot follow links: {component}")
    normalized = Path(os.path.abspath(raw))
    try:
        info = normalized.stat()
    except FileNotFoundError:
        if optional:
            return None
        raise RestoreIOError("unsafe_path", f"Restore path is missing: {normalized}") from None
    valid = stat.S_ISDIR(info.st_mode) if directory else (
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    if not valid:
        raise RestoreIOError("unsafe_path", f"Restore requires unaliased storage: {normalized}")
    return normalized


def ensure_private_tree(path):
    root = checked_path(path, directory=True)
    pending, seen = [root], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                seen += 1
                if seen > MAX_TREE_ENTRIES:
                    raise RestoreIOError("unsafe_path", "Staging tree exceeds supported entry count")
                info = entry.stat(follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                    raise RestoreIOError("unsafe_path", f"Staging cannot contain links: {entry.path}")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RestoreIOError("unsafe_path", f"Staging requires regular files: {entry.path}")
    return root


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _key(path, size):
    return hashlib.sha256(_canonical(path).encode("utf-8")).hexdigest()[:size]


def writer_lock_path(data_path):
    return Path.home() / ".mempalace/locks" / f"mine_palace_{_key(data_path, 16)}.lock"


def serverinfo_path(data_path):
    return Path.home() / ".mempalace/server" / _key(data_path, 24) / "serverinfo.json"


def _pid_alive(pid):
    if os.name == "nt":
        raise RestoreIOError(
            "indeterminate_writer",
            "Windows registry PID requires independently verified cleanup; no signal sent",
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        raise RestoreIOError("indeterminate_writer", "Cannot determine registered PID state") from exc
    return True


def _read_json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate control key")
            value[key] = item
        return value

    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized control record")
        value = json.loads(raw, object_pairs_hook=pairs)
        if type(value) is not dict:
            raise ValueError("invalid control record")
        return value
    except (OSError, ValueError) as exc:
        raise RestoreIOError("indeterminate_writer", f"Invalid writer record: {path}") from exc


def refuse_known_writers(data_path):
    root = os.environ.get("MEMPALACE_DAEMON_STATE_ROOT")
    daemon = (Path(root).expanduser() if root else Path.home() / ".mempalace/daemon")
    daemon = daemon / _key(data_path, 24)
    for path in (serverinfo_path(data_path), daemon / "endpoint.json", daemon / "pid"):
        try:
            present = checked_path(path, optional=True)
        except RestoreIOError as exc:
            raise RestoreIOError("indeterminate_writer", f"Unsafe writer registry: {path}") from exc
        if present is None:
            continue
        if path.name == "pid":
            try:
                with path.open("r", encoding="ascii") as source:
                    raw = source.read(33)
                pid = int(raw) if len(raw) <= 32 else None
            except (OSError, ValueError) as exc:
                raise RestoreIOError("indeterminate_writer", f"Invalid daemon PID: {path}") from exc
        else:
            info = _read_json(path)
            pid = info.get("pid")
            if (type(info.get("palace_path")) is not str
                    or _canonical(info["palace_path"]) != _canonical(data_path)
                    or type(info.get("host")) is not str or not info["host"]
                    or type(info.get("port")) is not int or not 1 <= info["port"] <= 65535
                    or (path.name == "serverinfo.json" and (
                        info.get("scheme") not in ("http", "https")
                        or type(info.get("read_only")) is not bool))):
                raise RestoreIOError("indeterminate_writer", f"Mismatching writer record: {path}")
        if type(pid) is not int or pid <= 0:
            raise RestoreIOError("indeterminate_writer", f"Invalid registered PID: {path}")
        if _pid_alive(pid):
            raise RestoreIOError("active_writer", f"Registered writer is still active: {path}")


@contextmanager
def writer_lease(data_path):
    lock = writer_lock_path(data_path)
    checked_path(lock, optional=True)
    (Path.home() / ".mempalace").mkdir(mode=0o700, exist_ok=True)
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    checked_path(lock.parent, directory=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise RestoreIOError("busy_writer", f"MemPalace writer lease unavailable: {lock}") from exc
            yield lock
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def sqlite_write_guard(databases):
    with ExitStack() as stack:
        for database in sorted(set(map(Path, databases))):
            database = checked_path(database)
            con = stack.enter_context(closing(sqlite3.connect(
                database.as_uri() + "?mode=rw", uri=True, timeout=0.25)))
            try:
                con.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                native_code = (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
                code = ("busy_writer" if native_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        else "indeterminate_writer")
                raise RestoreIOError(code, f"Cannot exclude SQLite writer at {database}: {exc}") from exc
            stack.callback(con.rollback)
        yield


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_private_json(path, value):
    path = Path(path)
    checked_path(path, optional=True)
    body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n"
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as out:
            os.chmod(temp, 0o600)
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()
