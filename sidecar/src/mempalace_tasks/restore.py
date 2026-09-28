"""Offline guards and private-staging epoch activation, never a service manager.

The writer lease excludes cooperating MemPalace writers, not arbitrary raw CLI
or peer writes. SQLite guards exclude database writers while held. Publication
additionally requires an operator-maintained offline/no-new-launch boundary;
registry absence and an acknowledgement flag are not technical proof of it.
"""

from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
from uuid import uuid4

from .codec import canonical_json
from .model import DomainError, instant, state_to_dict, utc
from .protocol import ProtocolError, fold_record, make_epoch
from . import snapshot


PREPARE_MARKER = ".task-restore-incomplete.json"


class RestoreError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.details = {} if details is None else details


def checked_path(path, *, directory=False, optional=False):
    try:
        return snapshot._path(path, directory=directory, optional=optional)
    except (snapshot.SnapshotError, OSError) as exc:
        raise RestoreError("unsafe_path", str(exc)) from exc


def ensure_private_tree(path):
    """Reject links, aliases and special files before mutating private staging."""
    root = checked_path(path, directory=True)
    pending, seen = [root], 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > snapshot.MAX_SNAPSHOT_ROWS:
                    raise RestoreError("unsafe_path", "Staging tree exceeds supported entry count")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RestoreError("unsafe_path", f"Staging requires unaliased regular files: {entry.path}")
    return root


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _key(path, size):
    return hashlib.sha256(_canonical(path).encode("utf-8")).hexdigest()[:size]


def writer_lock_path(data_path):
    return Path.home() / ".mempalace/locks" / f"mine_palace_{_key(data_path, 16)}.lock"


def serverinfo_path(data_path):
    return Path.home() / ".mempalace/server" / _key(data_path, 24) / "serverinfo.json"


def windows_pid_state(pid):
    """Refuse indeterminate Windows liveness; never send os.kill(pid, 0)."""
    raise RestoreError("indeterminate_writer",
                       "Windows registry PID requires independently verified cleanup; no signal sent")


def _pid_alive(pid):
    if os.name == "nt":
        return windows_pid_state(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        raise RestoreError("indeterminate_writer", "Cannot safely determine registered PID state") from exc
    return True


def _read_json(path, code):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise RestoreError(code, f"Duplicate JSON key in {path}")
            value[key] = item
        return value
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise RestoreError(code, f"Oversized control record: {path}")
        value = json.loads(raw, object_pairs_hook=pairs)
        if type(value) is not dict:
            raise RestoreError(code, f"Invalid control record: {path}")
        return value
    except (OSError, ValueError) as exc:
        raise RestoreError(code, f"Unreadable or malformed control record: {path}") from exc


def refuse_known_writers(data_path):
    """Positive refusal checks only; missing records never establish exclusion."""
    root = os.environ.get("MEMPALACE_DAEMON_STATE_ROOT")
    daemon = (Path(root).expanduser() if root else Path.home() / ".mempalace/daemon")
    daemon = daemon / _key(data_path, 24)
    for path in (serverinfo_path(data_path), daemon / "endpoint.json", daemon / "pid"):
        try:
            present = checked_path(path, optional=True)
        except RestoreError as exc:
            raise RestoreError("indeterminate_writer", f"Unsafe writer registry: {path}") from exc
        if present is None:
            continue
        if path.name == "pid":
            try:
                with path.open("r", encoding="ascii") as source:
                    raw = source.read(33)
                pid = int(raw) if len(raw) <= 32 else None
            except (OSError, ValueError) as exc:
                raise RestoreError("indeterminate_writer", f"Invalid daemon PID record: {path}") from exc
        else:
            info = _read_json(path, "indeterminate_writer")
            pid = info.get("pid")
            if (type(info.get("palace_path")) is not str
                    or _canonical(info["palace_path"]) != _canonical(data_path)
                    or type(info.get("host")) is not str or not info["host"]
                    or type(info.get("port")) is not int or not 1 <= info["port"] <= 65535
                    or (path.name == "serverinfo.json" and (
                        info.get("scheme") not in ("http", "https")
                        or type(info.get("read_only")) is not bool))):
                raise RestoreError("indeterminate_writer", f"Incomplete or mismatching writer record: {path}")
        if type(pid) is not int or pid <= 0:
            raise RestoreError("indeterminate_writer", f"Invalid registered PID: {path}")
        if _pid_alive(pid):
            raise RestoreError("active_writer", f"Registered writer is still active: {path}")


@contextmanager
def writer_lease(data_path):
    """Hold MemPalace's real lifetime lease without truncating/unlinking its inode."""
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
                raise RestoreError("busy_writer", f"MemPalace writer lease is unavailable: {lock}") from exc
            yield lock
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def sqlite_write_guard(databases):
    """Bounded BEGIN IMMEDIATE leases; callers close them before Windows renames."""
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
                raise RestoreError(code, f"Cannot exclude SQLite writer at {database}: {exc}") from exc
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
    """Administrative progress only, never task truth or an external recovery DB."""
    path = Path(path)
    checked_path(path, optional=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as out:
            os.chmod(temp, 0o600)
            out.write(canonical_json(value) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def read_task_logs(data_path):
    """Replay complete existing native records using the shared snapshot reader."""
    data = checked_path(data_path, directory=True)
    snapshot.validate_task_snapshot(data, require_logstream=True)
    wal = data / "logstream.sqlite3-wal"
    active_wal = wal.exists() and wal.stat().st_size > 0
    uri = (data / "logstream.sqlite3").as_uri() + "?mode=ro" + (
        "&readonly_shm=1" if active_wal else "&immutable=1")
    with closing(sqlite3.connect(uri, uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        con.execute("BEGIN")
        columns = snapshot._schema(con)
        counts = snapshot._counts(con)
        artifacts = snapshot._artifacts(con, counts["artifacts"])
        links = snapshot._links(con, artifacts, counts["event_artifacts"])
        return snapshot._replay(con, columns["events"], links, counts["events"])


def append_epoch_cli(data_path, payload, *, mempalace="mempalace"):
    metadata = {key: payload[key] for key in ("authority_id", "activation_id", "epoch_id")}
    argv = [str(mempalace), "--palace", str(data_path), "logstream", "append",
            "--type", "mptask.epoch", "--stream", "mptask/" + payload["authority_id"],
            "--room", "tasks", "--from-agent", "mempalace-tasks",
            "--correlation-id", payload["activation_id"],
            "--metadata", canonical_json(metadata), "--body-file", "-", "--json"]
    try:
        result = subprocess.run(argv, input=canonical_json(payload).encode("utf-8"),
                                capture_output=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RestoreError("append_uncertain", "Staging append timed out; inspect/retry private staging") from exc
    except OSError as exc:
        raise RestoreError("append_failed", "Preinstalled MemPalace CLI could not be executed") from exc
    if result.returncode:
        raise RestoreError("append_failed", f"Staging append exited {result.returncode}; stage not publishable")
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise RestoreError("append_uncertain", "Staging append returned invalid JSON") from exc
    if type(receipt) is not dict or "error" in receipt:
        raise RestoreError("append_uncertain", "Staging append returned no valid stored-event receipt")
    return receipt


def _domain_fingerprint(log):
    return canonical_json({
        "state": state_to_dict(log.state), "accepted": log.accepted_records,
        "head": log.domain_head, "ordinal": log.domain_ordinal,
    })


def _artifact_fingerprint(data):
    digest = hashlib.sha256()
    with closing(sqlite3.connect((data / "logstream.sqlite3").as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        con.execute("BEGIN")
        counts = snapshot._counts(con)
        for table, columns in (
                ("artifacts", "id, kind, sha256, size_bytes, created_by, created_at, "
                              "metadata_json, origin_replica"),
                ("event_artifacts", "event_id, artifact_id")):
            for row in snapshot._rows(con, table, columns, counts[table]):
                digest.update(canonical_json(dict(row)).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _cli_schema_ready(data):
    with closing(sqlite3.connect((data / "logstream.sqlite3").as_uri() + "?mode=ro", uri=True)) as con:
        columns = snapshot._schema(con)
        if (not set(snapshot._PROVENANCE_COLUMNS) <= columns["events"]
                or "origin_replica" not in columns["artifacts"]
                or con.execute("SELECT 1 FROM events WHERE origin_replica IS NULL LIMIT 1").fetchone()
                or con.execute("SELECT 1 FROM artifacts WHERE origin_replica IS NULL LIMIT 1").fetchone()):
            raise RestoreError("unsupported_schema",
                               "Stage needs explicit upstream migration before restore activation")


@contextmanager
def task_restore_preparation(data_path, *, private_stage=False, expected_authorities=None,
                             append=None, now=None, mempalace="mempalace", live_data_path=None):
    """Fence every initialized authority before publishing a private, stopped stage.

    ``append(data_path, payload)`` may supply a proven native append adapter.
    Default is the preinstalled direct MemPalace CLI, never SQL task INSERTs.
    A failed/ambiguous append leaves an administrative incomplete marker and
    publishable=False error details. Retry replays truth and creates fresh
    successors; pending/head/clock files never participate.
    """
    if private_stage is not True:
        raise RestoreError("private_stage_required", "Explicit private/stopped staging is required")
    data = ensure_private_tree(data_path)
    known = [Path.home() / ".mempalace/palace"]
    known += [Path(value) for name in ("MEMPALACE_PALACE_PATH", "MEMPAL_PALACE_PATH")
              if (value := os.environ.get(name))]
    if live_data_path is not None:
        known.append(Path(live_data_path))
    canonical = Path(_canonical(data))
    if any(canonical == (other := Path(_canonical(path)))
           or canonical in other.parents or other in canonical.parents for path in known):
        raise RestoreError("unsafe_stage", "Refusing administrative activation against a live data location")
    refuse_known_writers(data)
    marker = data / PREPARE_MARKER
    completed = []
    pending_payload = None
    with writer_lease(data):
        refuse_known_writers(data)
        try:
            before = snapshot.validate_task_snapshot(
                data, require_logstream=True, expected_authorities=expected_authorities)
            logs = read_task_logs(data)
            if any(log.state.configuration is None for log in logs.values()):
                raise RestoreError("uninitialized_authority", "Stage contains an uninitialized authority")
            _cli_schema_ready(data)
            with sqlite_write_guard([data / "logstream.sqlite3"]):
                pass
            baseline = {key: _domain_fingerprint(log) for key, log in logs.items()}
            replica = (data / "replica.json").read_bytes()
            artifact_fingerprint = _artifact_fingerprint(data)
            write_private_json(marker, {"schema_version": 1, "phase": "preparing",
                                        "authorities": sorted(logs), "completed": []})
            summaries = {}
            for key in sorted(logs):
                log = logs[key]
                times = [instant(now) if now is not None else datetime.now(timezone.utc)]
                times += [instant(row["event"]["at"]) for row in log.accepted_records]
                times += [instant(row["at"]) for row in log.activation_attempts.values()
                          if row["outcome"] == "accepted"]
                epoch, activation = str(uuid4()), str(uuid4())
                if epoch in log.used_epochs or activation in log.activation_attempts:
                    raise RestoreError("identity_collision", "Fresh activation identity collision")
                payload = make_epoch(log, epoch, activation, utc(max(times)))
                pending_payload = payload
                receipt = (append(data, payload) if append is not None else
                           append_epoch_cli(data, payload, mempalace=mempalace))
                if type(receipt) is not dict or receipt.get("body") != canonical_json(payload):
                    raise RestoreError("append_uncertain", "Stored-event receipt does not match staging epoch")
                expected = deepcopy(log)
                fold_record(expected, receipt)
                logs[key] = expected
                actual = read_task_logs(data)
                if set(actual) != set(logs) or any(
                        actual[item].raw_hash != logs[item].raw_hash
                        or _domain_fingerprint(actual[item]) != baseline[item] for item in logs):
                    raise RestoreError("changed_history", "Staging history changed unexpectedly during activation")
                if (actual[key].epoch_id, actual[key].activation_id) != (epoch, activation):
                    raise RestoreError("activation_rejected", "Staged epoch is not accepted and current")
                completed.append(key)
                summaries[key] = {
                    "epoch_id": epoch, "activation_id": activation, "activation_at": payload["at"],
                    "domain_head": log.domain_head, "domain_ordinal": log.domain_ordinal,
                    "task_count": len(log.state.tasks),
                }
                write_private_json(marker, {"schema_version": 1, "phase": "preparing",
                                            "authorities": sorted(logs), "completed": completed})
            with closing(sqlite3.connect((data / "logstream.sqlite3").as_uri() + "?mode=rw",
                                         uri=True, timeout=0.25)) as con:
                if con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise RestoreError("busy_writer", "Staged WAL could not be checkpointed")
            after = snapshot.validate_task_snapshot(data, require_logstream=True,
                                                     expected_authorities=sorted(logs))
            if (after["artifacts"] != before["artifacts"]
                    or after["artifact_link_count"] != before["artifact_link_count"]
                    or after["event_count"] != before["event_count"] + len(logs)
                    or _artifact_fingerprint(data) != artifact_fingerprint
                    or (data / "replica.json").read_bytes() != replica):
                raise RestoreError("changed_history", "Staged artifact/provenance inventory changed")
            marker.unlink()
            sync_directory(data)
            yield {"status": "prepared", "publishable": True, "replica_id": after["replica_id"],
                   "authorities": summaries, "artifact_count": after["artifact_count"]}
        except (RestoreError, snapshot.SnapshotError, ProtocolError, DomainError,
                sqlite3.Error, OSError) as exc:
            if isinstance(exc, RestoreError):
                code = exc.code
            else:
                code = "invalid_stage"
            details = {"publishable": False, "stage": str(data),
                       "completed_authorities": completed, "activation_state": "not_attempted"}
            if pending_payload is not None:
                try:
                    observed = read_task_logs(data).get(pending_payload["authority_id"])
                    if observed is None:
                        details["activation_state"] = "not_observed"
                    elif (observed.epoch_id, observed.activation_id) == (
                            pending_payload["epoch_id"], pending_payload["activation_id"]):
                        details["activation_state"] = "accepted_current"
                    else:
                        details["activation_state"] = "not_current"
                except (RestoreError, snapshot.SnapshotError, ProtocolError,
                        DomainError, sqlite3.Error, OSError) as replay_error:
                    details["activation_state"] = "indeterminate"
                    details["replay_error"] = str(replay_error)
            raise RestoreError(code, f"Restore stage is not publishable: {exc}", details) from exc


def prepare_task_restore(data_path, *, private_stage=False, expected_authorities=None,
                         append=None, now=None, mempalace="mempalace", live_data_path=None):
    """Prepare a private stage; use task_restore_preparation to hold its lease through publication."""
    with task_restore_preparation(
            data_path, private_stage=private_stage, expected_authorities=expected_authorities,
            append=append, now=now, mempalace=mempalace, live_data_path=live_data_path) as result:
        return result
