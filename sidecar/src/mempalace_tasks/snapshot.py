"""Read-only task-log verification for an already coherent, staged DATA directory.

The caller must exclude every writer while capturing/using the tree. A SQLite
read transaction is not whole-palace quiescence, nor does it undo external work.
This verifier does not start an authority, mint an epoch/replica, load an SDK, or
consult local pending/head/clock files. A self-consistent older snapshot is valid;
there is no independent rollback anchor here.

Closed/checkpointed SQLite images are opened immutable to avoid creating WAL/SHM
files. A nonempty WAL requires its captured SHM and readonly_shm=1 so SQLite does
not write WAL read-marks. The staged tree must not share a writable SQLite mapping
in this process. Neither opening mode permits validating a changing live tree.
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .model import DomainError, identifier
from .protocol import LogState, MAX_PAYLOAD_BYTES, ProtocolError, fold_record


MAX_SNAPSHOT_ROWS = 1_000_000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
_EVENT_COLUMNS = (
    "id", "type", "stream", "room", "from_agent", "to_agent", "correlation_id",
    "branch", "base_commit", "status", "body", "created_at", "metadata_json",
)
_PROVENANCE_COLUMNS = ("origin_replica", "origin_seq", "hlc")
_ARTIFACT_COLUMNS = (
    "id", "kind", "sha256", "size_bytes", "content", "created_by", "created_at",
    "metadata_json",
)


class SnapshotError(Exception):
    """An explicit invalid/unverifiable snapshot, never an empty-state fallback."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _path(path, *, directory=False, optional=False):
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise SnapshotError("invalid_argument", "Snapshot path must be a filesystem path") from exc
    if type(value) is not str or not value or "\0" in value:
        raise SnapshotError("invalid_argument", "Snapshot path must be nonempty text without NUL")
    try:
        raw = Path(value).expanduser().absolute()
    except RuntimeError as exc:
        raise SnapshotError("invalid_argument", "Cannot expand snapshot path") from exc
    for component in (*reversed(raw.parents), raw):
        if component.is_symlink():
            raise SnapshotError("unsafe_path", f"Snapshot cannot follow symlink: {component}")
    normalized = Path(os.path.abspath(raw))
    try:
        mode = normalized.stat().st_mode
    except FileNotFoundError:
        if optional:
            return None
        raise SnapshotError("unsafe_path", f"Snapshot path is missing: {normalized}") from None
    valid = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
    if not valid:
        raise SnapshotError("unsafe_path", f"Snapshot path has wrong file type: {normalized}")
    return normalized


def _json(text, code, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SnapshotError(code, f"Duplicate JSON key in {label}")
            result[key] = value
        return result

    def constant(value):
        raise SnapshotError(code, f"Non-finite JSON number in {label}")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SnapshotError(code, f"Invalid JSON in {label}") from exc
    if type(result) is not dict:
        raise SnapshotError(code, f"Expected JSON object in {label}")
    return result


def _replica(path):
    if path is None:
        raise SnapshotError("invalid_replica", "Logstream requires existing replica.json")
    with path.open("rb") as source:
        body = source.read(MAX_METADATA_BYTES + 1)
    if len(body) > MAX_METADATA_BYTES:
        raise SnapshotError("invalid_replica", "Oversized replica.json")
    value = _json(body, "invalid_replica", "replica.json").get("replica_id")
    if type(value) is not str or re.fullmatch(r"rep_(?:[0-9a-f]{12}|[0-9a-f]{32})", value) is None:
        raise SnapshotError("invalid_replica", "Invalid replica.json identity")
    return value


def _expected(authorities):
    if authorities is None:
        return set()
    if not isinstance(authorities, (list, tuple, set, frozenset)):
        raise SnapshotError("invalid_argument", "expected_authorities must be a UUID collection")
    try:
        return {identifier(value, "expected authority") for value in authorities}
    except DomainError as exc:
        raise SnapshotError("invalid_argument", str(exc)) from exc


def _schema(con):
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('events', 'artifacts', 'event_artifacts')")}
    columns = {}
    for table, required in (("events", _EVENT_COLUMNS), ("artifacts", _ARTIFACT_COLUMNS),
                            ("event_artifacts", ("event_id", "artifact_id"))):
        if table not in tables:
            raise SnapshotError("invalid_database", f"Logstream is missing table {table}")
        columns[table] = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        if not set(required) <= columns[table]:
            raise SnapshotError("invalid_database", f"Logstream has incomplete {table} schema")
    return columns


def _counts(con):
    counts = {}
    for table in ("events", "artifacts", "event_artifacts"):
        count = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if count > MAX_SNAPSHOT_ROWS:
            raise SnapshotError("size_limit", f"{table} exceeds {MAX_SNAPSHOT_ROWS} snapshot rows")
        counts[table] = count
    return counts


def _rows(con, table, columns, count):
    """Stream a finite transaction-local row set; never return a truncated prefix."""
    seen = 0
    for row in con.execute(
            f"SELECT {columns} FROM {table} ORDER BY rowid LIMIT ?", (count + 1,)):
        seen += 1
        if seen > count:
            raise SnapshotError("invalid_database", f"Unexpected extra rows in {table}")
        yield row
    if seen != count:
        raise SnapshotError("invalid_database", f"Incomplete read of {table}")


def _text_id(value):
    return (type(value) is str and 0 < len(value) <= 256 and value == value.strip()
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _artifacts(con, count):
    result = {}
    columns = "rowid AS local_rowid, id, length(CAST(content AS BLOB)) AS content_bytes"
    for header in _rows(con, "artifacts", columns, count):
        if header["content_bytes"] is None or header["content_bytes"] > MAX_ARTIFACT_BYTES:
            raise SnapshotError("size_limit", "Artifact body exceeds the supported 4 MiB limit")
        row = con.execute(
            "SELECT id, kind, sha256, size_bytes, content FROM artifacts WHERE rowid = ?",
            (header["local_rowid"],)).fetchone()
        if (not _text_id(row["id"]) or row["id"] in result or not _text_id(row["kind"])
                or type(row["content"]) is not str or not row["content"] or "\0" in row["content"]):
            raise SnapshotError("invalid_artifact", "Malformed or duplicate native artifact")
        encoded = row["content"].encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if (type(row["size_bytes"]) is not int or row["size_bytes"] != len(encoded)
                or row["sha256"] != digest):
            raise SnapshotError("invalid_artifact", f"Artifact content/hash/size mismatch: {row['id']}")
        result[row["id"]] = {
            "sha256": digest, "size_bytes": len(encoded), "kind": row["kind"],
        }
    return result


def _links(con, artifacts, count):
    result = {}
    for row in _rows(con, "event_artifacts", "event_id, artifact_id", count):
        event_id, artifact_id = row["event_id"], row["artifact_id"]
        if (not _text_id(event_id) or not _text_id(artifact_id) or artifact_id not in artifacts
                or con.execute("SELECT 1 FROM events WHERE id = ? LIMIT 1",
                               (event_id,)).fetchone() is None):
            raise SnapshotError("invalid_artifact_link", "Missing or invalid linked event/artifact")
        ids = result.setdefault(event_id, set())
        if artifact_id in ids:
            raise SnapshotError("invalid_artifact_link", "Duplicate native artifact link")
        ids.add(artifact_id)
    return {event_id: sorted(ids) for event_id, ids in result.items()}


def _authority(stream, event_type):
    reserved_stream = type(stream) is str and stream.startswith("mptask/")
    reserved_type = type(event_type) is str and event_type.startswith("mptask.")
    if not reserved_stream and not reserved_type:
        return None
    if not reserved_stream:
        raise SnapshotError("invalid_protocol", "Sidecar event is outside its reserved stream")
    try:
        return identifier(stream.removeprefix("mptask/"), "stream authority")
    except DomainError as exc:
        raise SnapshotError("invalid_protocol", f"Malformed reserved stream: {stream}") from exc


def _replay(con, columns, links, count):
    logs = {}
    header_columns = (
        "rowid AS local_rowid, stream, type, "
        "length(CAST(body AS BLOB)) AS body_bytes, "
        "length(CAST(metadata_json AS BLOB)) AS metadata_bytes"
    )
    selected = list(_EVENT_COLUMNS) + [
        field if field in columns else f"NULL AS {field}" for field in _PROVENANCE_COLUMNS
    ]
    for header in _rows(con, "events", header_columns, count):
        authority_id = _authority(header["stream"], header["type"])
        if authority_id is None:
            continue
        if (header["body_bytes"] is None or header["body_bytes"] > MAX_PAYLOAD_BYTES
                or header["metadata_bytes"] is None or header["metadata_bytes"] > MAX_METADATA_BYTES):
            raise SnapshotError("size_limit", "Task body or metadata exceeds its complete-record limit")
        row = con.execute(
            f"SELECT rowid AS seq, {', '.join(selected)} FROM events WHERE rowid = ?",
            (header["local_rowid"],)).fetchone()
        raw = dict(row)
        raw["metadata"] = _json(raw.pop("metadata_json"), "invalid_protocol", "task metadata")
        raw["artifact_ids"] = links.get(raw["id"], [])
        if authority_id not in logs:
            logs[authority_id] = LogState(authority_id)
        log = logs[authority_id]
        try:
            fold_record(log, raw)
        except (ProtocolError, DomainError) as exc:
            raise SnapshotError(
                "invalid_protocol", f"Invalid task event {raw['id']} in {authority_id}: {exc}") from exc
    return logs


def _summary(log):
    notes = []
    for row in log.history:
        if row["disposition"] != "accepted":
            continue
        payload = row["payload"]
        command = payload["event"]["command"]
        if command["operation"] == "note":
            notes.append({
                "event_id": row["event_id"], "ordinal": payload["ordinal"],
                "epoch_id": payload.get("epoch_id"), "command_id": command["command_id"],
                "task_id": command["task_id"], "tag": command["tag"], "text": command["text"],
                "text_sha256": hashlib.sha256(command["text"].encode("utf-8")).hexdigest(),
                "payload_hash": payload["payload_hash"],
                "references": deepcopy(command.get("references", [])),
            })
    goals = sorted(tid for tid, task in log.state.tasks.items()
                   if task["goal_id"] == tid and task["goal_policy"] is not None)
    return {
        "initialized": log.state.configuration is not None,
        "epoch_id": log.epoch_id, "activation_id": log.activation_id,
        "activation_at": log.activation_at, "domain_ordinal": log.domain_ordinal,
        "domain_head": log.domain_head, "raw_record_count": len(log.history),
        "stale_record_count": sum(row["disposition"] == "stale" for row in log.history),
        "task_count": len(log.state.tasks), "goal_count": len(goals),
        "task_ids": sorted(log.state.tasks), "goal_ids": goals,
        "tasks": deepcopy(log.state.tasks), "edges": deepcopy(log.state.edges),
        "configuration": deepcopy(log.state.configuration), "notes": notes,
    }


def validate_task_snapshot(data_path, *, expected_authorities=None, require_logstream=False):
    """Return a JSON-native summary, or raise SnapshotError(code, message).

    ``status`` is ``valid`` or optional ``absent``. ``authorities`` maps
    canonical UUIDs to initialization/epoch, accepted domain ordinal/head, raw
    counts, complete task snapshots (including holds), root-goal IDs, edges,
    configuration and full accepted notes with payload/text hashes. Task counts
    include goal nodes. ``artifacts`` maps native IDs to verified kind/hash/size;
    artifact bodies remain in SQLite, not a second recovery store.

    Expected authorities must be a UUID collection and have accepted genesis.
    Up to MAX_SNAPSHOT_ROWS per table, 240 KiB/task body, 64 KiB/metadata and
    4 MiB/artifact are supported; exceeding a limit fails, never truncates.
    Native non-sidecar events are not folded. File/git/cloud references and
    source-plan tag conventions are not interpreted as artifact IDs or chunk
    manifests; notes retain their complete individually hashed protocol text.
    All database reads share one transaction. Quiescence and external effects
    are explicitly unverified; callers must supply a coherent, unchanging tree.
    """
    expected = _expected(expected_authorities)
    if type(require_logstream) is not bool:
        raise SnapshotError("invalid_argument", "require_logstream must be boolean")
    try:
        data = _path(data_path, directory=True)
        db = _path(data / "logstream.sqlite3", optional=True)
        wal = _path(data / "logstream.sqlite3-wal", optional=True)
        shm = _path(data / "logstream.sqlite3-shm", optional=True)
        replica = _path(data / "replica.json", optional=True)
        journal = _path(data / "logstream.sqlite3-journal", optional=True)
        result = {
            "schema_version": 1, "status": "absent", "replica_id": None,
            "event_count": 0, "task_event_count": 0, "artifact_count": 0,
            "artifact_link_count": 0, "artifacts": {}, "authorities": {},
            "quiescence_verified": False, "external_references_verified": False,
        }
        if db is None:
            if require_logstream or expected or wal is not None or shm is not None or journal is not None:
                raise SnapshotError("missing_logstream", "Required logstream database is missing")
            return result
        if journal is not None:
            raise SnapshotError("unsafe_path", "Rollback journal present; capture a clean offline image")
        replica_id = _replica(replica)
        active_wal = wal is not None and wal.stat().st_size > 0
        if active_wal and shm is None:
            raise SnapshotError("unsafe_path", "Nonempty WAL requires captured SHM; do not rebuild it here")
        uri = db.as_uri() + "?mode=ro" + (
            "&readonly_shm=1" if active_wal else "&immutable=1")
        with closing(sqlite3.connect(uri, uri=True)) as con:
            con.row_factory = sqlite3.Row
            con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ARTIFACT_BYTES + 2 * MAX_METADATA_BYTES)
            con.execute("PRAGMA query_only = ON")
            con.execute("PRAGMA trusted_schema = OFF")
            con.execute("BEGIN")
            columns = _schema(con)
            counts = _counts(con)
            check = con.execute("PRAGMA integrity_check(1)").fetchone()
            if check is None or check[0] != "ok":
                raise SnapshotError("invalid_database", "Logstream SQLite integrity check failed")
            artifacts = _artifacts(con, counts["artifacts"])
            links = _links(con, artifacts, counts["event_artifacts"])
            logs = _replay(con, columns["events"], links, counts["events"])
            for authority in sorted(expected):
                if authority not in logs:
                    raise SnapshotError("missing_authority", f"Expected authority stream missing: {authority}")
                if logs[authority].state.configuration is None:
                    raise SnapshotError("uninitialized_authority",
                                        f"Expected authority has no accepted genesis: {authority}")
            result.update(
                status="valid", replica_id=replica_id, event_count=counts["events"],
                task_event_count=sum(len(log.history) for log in logs.values()),
                artifact_count=counts["artifacts"], artifact_link_count=counts["event_artifacts"],
                artifacts=artifacts,
                authorities={key: _summary(logs[key]) for key in sorted(logs)},
            )
            return result
    except sqlite3.Error as exc:
        raise SnapshotError("invalid_database", f"Cannot read logstream SQLite snapshot: {exc}") from exc
    except OSError as exc:
        raise SnapshotError("unsafe_path", f"Cannot read snapshot storage: {exc}") from exc
    except UnicodeError as exc:
        raise SnapshotError("invalid_database", "Invalid UTF-8 in snapshot storage") from exc
