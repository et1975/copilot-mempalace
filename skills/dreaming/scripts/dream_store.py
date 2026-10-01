"""Exact Dreaming documents and immutable records in native MemPalace storage.

Readers never instantiate native Logstream (its constructor migrates). Writers
use native synchronous handlers under the cooperating local directory lock.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dream_transport import call_tool as native_call_tool, mutation_lock


STREAM = "dreaming/v1"
ROOM = "control"
ARTIFACT_LIMIT = 4 * 1024 * 1024
EVENT_LIMIT = 256 * 1024
INDEX_PARTS = 1024
PAGE_SIZE = 500
MAX_INDEX_DEPTH = 16
RECORD_TYPES = frozenset({
    "run_created", "review_saved", "adoption_started", "proposal_started",
    "proposal_settled", "completed", "archive", "ontology_saved", "derive_skip",
    "delete_started", "delete_settled", "restore_settled",
    "archive_delete_started", "archive_delete_settled",
})
_SHA = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = {
    "events": {
        "id", "type", "stream", "room", "topic", "from_agent", "to_agent", "correlation_id",
        "branch", "base_commit", "status", "body", "created_at", "metadata_json",
        "origin_replica", "origin_seq", "hlc",
    },
    "artifacts": {
        "id", "kind", "sha256", "size_bytes", "content", "created_by",
        "created_at", "metadata_json", "origin_replica",
    },
    "event_artifacts": {"event_id", "artifact_id"},
}


class StoreError(ValueError):
    """Missing, malformed, unsupported, or unverifiable native state."""


class StoreConflict(StoreError):
    """A logical operation or predecessor chain conflicts with durable state."""


def _json_value(value):
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise StoreError("JSON object keys must be strings")
        for key, item in value.items():
            key.encode("utf-8", errors="strict")
            _json_value(item)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif value is None or isinstance(value, (bool, int)):
        pass
    elif isinstance(value, float) and math.isfinite(value):
        pass
    else:
        raise StoreError("document contains a non-JSON value")


def canonical_json(value) -> str:
    try:
        _json_value(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StoreError(f"invalid canonical JSON: {exc}") from exc


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StoreError("duplicate JSON object key")
        result[key] = value
    return result


def _decode(text):
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(StoreError("invalid JSON constant")))
        _json_value(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StoreError(f"invalid JSON document: {exc}") from exc


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _semantic(value):
    if isinstance(value, dict):
        if "artifact_id" in value:
            reference = _reference(value)
            if reference["format"] == "dream-document/v1":
                return {"document_sha256": reference["document_sha256"],
                        "document_size_bytes": reference["document_size_bytes"]}
            return {"document_sha256": reference["sha256"],
                    "document_size_bytes": reference["size_bytes"]}
        return {key: _semantic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def semantic_hash(value) -> str:
    """Hash semantics, excluding physical IDs of equivalent document artifacts."""
    try:
        if isinstance(value, dict) and "artifact_id" not in value:
            value = {key: item for key, item in value.items()
                     if key not in {"payload_hash", "event_id", "native_event_ids"}}
        return _digest(canonical_json(_semantic(value)).encode("utf-8"))
    except RecursionError as exc:
        raise StoreError("semantic JSON depth exhausted") from exc


def _reference(value):
    if not isinstance(value, dict):
        raise StoreError("artifact reference must be an object")
    if not isinstance(value.get("artifact_id"), str) or not value["artifact_id"]:
        raise StoreError("artifact reference lacks artifact_id")
    if not isinstance(value.get("sha256"), str) or not _SHA.fullmatch(value["sha256"]):
        raise StoreError("artifact reference lacks valid sha256")
    if type(value.get("size_bytes")) is not int or not 0 < value["size_bytes"] <= ARTIFACT_LIMIT:
        raise StoreError("artifact reference has invalid size_bytes")
    format = value.get("format")
    if not isinstance(format, str) or format not in {
        "dream-json/v1", "dream-document/v1", "dream-index/v1", "dream-fragment/v1",
    }:
        raise StoreError("unsupported artifact reference format")
    fields = {"artifact_id", "sha256", "size_bytes", "format"}
    if format == "dream-document/v1":
        fields |= {"document_sha256", "document_size_bytes"}
        digest, size = value.get("document_sha256"), value.get("document_size_bytes")
        if not isinstance(digest, str) or not _SHA.fullmatch(digest) \
                or type(size) is not int or size <= ARTIFACT_LIMIT:
            raise StoreError("fragmented reference aggregate integrity is invalid")
    if set(value) != fields:
        raise StoreError(f"artifact reference fields do not match {format} schema")
    return value


def _references(value):
    if isinstance(value, dict):
        if "artifact_id" in value:
            yield _reference(value)
        else:
            for item in value.values():
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


class DreamStore:
    def __init__(self, palace: str, *, call_tool=None):
        self.palace = os.path.realpath(os.path.expanduser(palace))
        self.path = Path(self.palace) / "logstream.sqlite3"
        self._caller = call_tool

    @classmethod
    def initialize(cls, palace: str):
        """Explicitly initialize native storage, never repair a damaged store."""
        store = cls(palace)
        if not Path(store.palace).is_dir():
            raise StoreError("initialize requires an existing palace directory")
        with mutation_lock(store.palace):
            if store.path.exists():
                store.events()
                return store
            if any(Path(str(store.path) + suffix).exists() for suffix in ("-wal", "-shm")):
                raise StoreError("orphan native logstream sidecars; restoration required")
            store._validate_palace()
            from dream_transport import initialize_logstream
            initialize_logstream(store.palace)
            store.events()
        return store

    def _validate_palace(self):
        existing = [Path(self.palace) / name for name in ("sqlite_exact.sqlite3", "chroma.sqlite3")
                    if (Path(self.palace) / name).is_file()]
        if not existing:
            raise StoreError("initialize requires an existing valid MemPalace palace")
        for path in existing:
            sidecars = [Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")]
            if any(sidecars) and not all(sidecars):
                raise StoreError("palace backing storage has incomplete WAL sidecars")
            uri = path.as_uri() + "?mode=ro" + ("" if all(sidecars) else "&immutable=1")
            conn = None
            try:
                conn = sqlite3.connect(uri, uri=True)
                if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise StoreError("palace backing storage failed integrity validation")
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "collections" not in tables:
                    raise StoreError("unrecognized palace backing storage schema")
            except sqlite3.Error as exc:
                raise StoreError("invalid palace backing storage") from exc
            finally:
                if conn is not None:
                    conn.close()

    @contextmanager
    def _reader(self):
        if not self.path.is_file():
            raise StoreError("native logstream is missing; explicit initialize is required for a new store")
        sidecars = [Path(str(self.path) + suffix) for suffix in ("-wal", "-shm")]
        present = [path.exists() for path in sidecars]
        if any(present) and not all(present):
            raise StoreError("incomplete native WAL sidecars; coherent restore required")
        uri = self.path.as_uri() + "?mode=ro" + ("" if all(present) else "&immutable=1")
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
                raise StoreError("unsupported native logstream schema version")
            for table, columns in _SCHEMA.items():
                actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if actual != columns:
                    raise StoreError(f"unsupported native logstream schema: {table}")
            yield conn
        except sqlite3.Error as exc:
            raise StoreError(f"native logstream read failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()

    def _call(self, name, arguments):
        result = (self._caller or native_call_tool)(self.palace, name, arguments, vector=False)
        if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
            raise StoreError(f"native {name} failed: {result}")
        return result

    def _artifact(self, artifact_id):
        with self._reader() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            result = dict(row) if row is not None else None
        if not isinstance(result, dict) or result.get("id") != artifact_id:
            raise StoreError(f"native artifact missing or invalid: {artifact_id}")
        return result

    def _read_bytes(self, reference):
        _reference(reference)
        artifact = self._artifact(reference["artifact_id"])
        content = artifact.get("content")
        if not isinstance(content, str):
            raise StoreError("artifact content is missing")
        try:
            raw = content.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise StoreError("artifact contains invalid UTF-8") from exc
        if _digest(raw) != reference["sha256"] or len(raw) != reference["size_bytes"] \
                or artifact.get("sha256") != reference["sha256"] \
                or artifact.get("size_bytes") != reference["size_bytes"]:
            raise StoreError("artifact byte/hash integrity mismatch")
        return raw

    def _put_bytes(self, raw, *, kind, purpose, format):
        result = self._call("mempalace_artifact_put", {
            "kind": kind, "content": raw.decode("utf-8"), "created_by": "dreaming",
            "metadata": {"dream_schema": 1, "purpose": purpose},
        })
        artifact = result.get("artifact", {})
        reference = {"artifact_id": artifact.get("id"), "sha256": _digest(raw),
                     "size_bytes": len(raw), "format": format}
        if artifact.get("sha256") != reference["sha256"] or artifact.get("size_bytes") != len(raw):
            raise StoreError("native artifact put returned mismatched integrity")
        if self._read_bytes(reference) != raw:
            raise StoreError("native artifact readback mismatch")
        return reference

    def put_document(self, value: dict, *, purpose: str) -> dict:
        if not isinstance(value, dict) or not isinstance(purpose, str) or not purpose.strip():
            raise StoreError("document and nonempty purpose required")
        raw = canonical_json(value).encode("utf-8")
        with mutation_lock(self.palace):
            # A write cannot implicitly initialize a missing or unsupported store.
            with self._reader():
                pass
            if len(raw) <= ARTIFACT_LIMIT:
                return self._put_bytes(raw, kind="json", purpose=purpose, format="dream-json/v1")
            parts = []
            offset = 0
            while offset < len(raw):
                end = min(offset + ARTIFACT_LIMIT, len(raw))
                while end < len(raw) and raw[end] & 0xC0 == 0x80:
                    end -= 1
                parts.append(self._put_bytes(raw[offset:end], kind="file", purpose=purpose,
                                             format="dream-fragment/v1"))
                offset = end
            depth = 0
            while len(parts) > INDEX_PARTS:
                depth += 1
                if depth >= MAX_INDEX_DEPTH:
                    raise StoreError("document index depth exhausted")
                parts = [self._put_index(parts[start:start + INDEX_PARTS], purpose)
                         for start in range(0, len(parts), INDEX_PARTS)]
            reference = self._put_index(parts, purpose)
            reference.update(format="dream-document/v1", document_sha256=_digest(raw),
                             document_size_bytes=len(raw))
            return reference

    def _put_index(self, parts, purpose):
        raw = canonical_json({"schema_version": 1, "format": "dream-fragments/v1", "parts": parts}).encode()
        return self._put_bytes(raw, kind="json", purpose=purpose, format="dream-index/v1")

    def get_document(self, reference: dict) -> dict:
        _reference(reference)
        if reference.get("format", "dream-json/v1") == "dream-json/v1":
            raw = self._read_bytes(reference)
        elif reference.get("format") == "dream-document/v1":
            sha, size = reference.get("document_sha256"), reference.get("document_size_bytes")
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or type(size) is not int or size <= ARTIFACT_LIMIT:
                raise StoreError("fragmented document aggregate integrity is missing")
            pieces, visited = [], set()
            total = 0

            def visit(ref, depth):
                nonlocal total
                if depth > MAX_INDEX_DEPTH:
                    raise StoreError("fragment index depth exhausted")
                if ref.get("artifact_id") in visited:
                    raise StoreError("fragment index repeats an artifact")
                visited.add(ref.get("artifact_id"))
                raw_part = self._read_bytes(ref)
                if ref.get("format") == "dream-fragment/v1":
                    total += len(raw_part)
                    if total > size:
                        raise StoreError("fragment bytes exceed declared document size")
                    pieces.append(raw_part)
                    return
                if ref.get("format") not in {"dream-document/v1", "dream-index/v1"}:
                    raise StoreError("unexpected fragment index reference format")
                index = _decode(raw_part)
                if not isinstance(index, dict) or index.get("schema_version") != 1 \
                        or index.get("format") != "dream-fragments/v1" \
                        or not isinstance(index.get("parts"), list) or not 0 < len(index["parts"]) <= INDEX_PARTS:
                    raise StoreError("unsupported fragment index schema")
                for part in index["parts"]:
                    visit(_reference(part), depth + 1)

            visit(reference, 0)
            raw = b"".join(pieces)
            if len(raw) != size or _digest(raw) != sha:
                raise StoreError("fragment aggregate byte/hash integrity mismatch")
        else:
            raise StoreError("reference is not a document")
        value = _decode(raw)
        if not isinstance(value, dict):
            raise StoreError("document must be a JSON object")
        return value

    def _native_events(self):
        with self._reader() as conn:
            cursor = 0
            while True:
                rows = conn.execute(
                    "SELECT rowid AS append_order,* FROM events WHERE stream=? AND room=? "
                    "AND rowid>? ORDER BY rowid LIMIT ?", (STREAM, ROOM, cursor, PAGE_SIZE),
                ).fetchall()
                for row in rows:
                    if row["append_order"] <= cursor:
                        raise StoreError("native append-order cursor failed to progress")
                    event = dict(row)
                    event["artifact_ids"] = [link[0] for link in conn.execute(
                        "SELECT artifact_id FROM event_artifacts WHERE event_id=? ORDER BY artifact_id",
                        (event["id"],))]
                    yield event
                if len(rows) < PAGE_SIZE:
                    break
                cursor = rows[-1]["append_order"]

    def _envelope(self, record):
        if not isinstance(record, dict) or type(record.get("schema_version")) is not int \
                or record["schema_version"] != 1:
            raise StoreError("unsupported Dreaming record schema")
        if record.get("record_type") not in RECORD_TYPES:
            raise StoreError("unsupported Dreaming record type")
        for field in ("operation_id", "scope_id", "run_id"):
            if field in record and (not isinstance(record[field], str) or not record[field]):
                raise StoreError(f"invalid record {field}")
        return record

    def events(self, *, scope_id: str | None = None, run_id: str | None = None) -> list[dict]:
        output, operations, seen = [], {}, set()
        for event in self._native_events():
            if not isinstance(event.get("id"), str) or event["id"] in seen:
                raise StoreError("native event cursor duplicate or lack of progress")
            seen.add(event["id"])
            if event.get("stream") != STREAM or event.get("room") != ROOM:
                raise StoreError("native event page contains unexpected routing")
            body = event.get("body")
            if not isinstance(body, str) or len(body.encode("utf-8")) > EVENT_LIMIT:
                raise StoreError("native Dreaming event body missing or oversized")
            record = self._envelope(_decode(body))
            if event.get("type") != "dream." + record["record_type"]:
                raise StoreError("Dreaming event type does not match record")
            if record.get("scope_id") != event.get("correlation_id"):
                raise StoreError("Dreaming event scope routing mismatch")
            if not record.get("operation_id") or record.get("payload_hash") != semantic_hash(record):
                raise StoreError("Dreaming record payload hash integrity mismatch")
            linked = set(event.get("artifact_ids", []))
            refs = list(_references(record))
            if {ref["artifact_id"] for ref in refs} != linked:
                raise StoreError("native event artifact links do not match record")
            for reference in {ref["artifact_id"]: ref for ref in refs}.values():
                self.get_document(reference)
            operation = record["operation_id"]
            if operation in operations:
                previous = operations[operation]
                if previous["payload_hash"] != record["payload_hash"]:
                    raise StoreConflict("conflicting payloads for one logical operation")
                previous["native_event_ids"].append(event["id"])
                continue
            record = {**record, "event_id": event["id"], "native_event_ids": [event["id"]]}
            operations[operation] = record
            if (scope_id is None or record.get("scope_id") == scope_id) \
                    and (run_id is None or record.get("run_id") == run_id):
                output.append(record)
        return output

    def _prepared(self, record, artifacts):
        if not isinstance(artifacts, list):
            raise StoreError("artifact references must be a list")
        for reference in artifacts:
            _reference(reference)
        if not isinstance(record, dict):
            raise StoreError("Dreaming record must be an object")
        canonical_json(record)
        supplied_hash = record.get("payload_hash")
        record = {key: value for key, value in record.items()
                  if key not in {"event_id", "native_event_ids", "payload_hash"}}
        record.setdefault("schema_version", 1)
        self._envelope(record)
        # References already named in the record need not be duplicated in the body.
        named = {ref["artifact_id"] for ref in _references(record)}
        extra = [reference for reference in artifacts if reference["artifact_id"] not in named]
        if extra:
            record["artifacts"] = extra
        if "operation_id" not in record:
            record["operation_id"] = "dream-" + semantic_hash(record)
        record["payload_hash"] = semantic_hash(record)
        if supplied_hash is not None and supplied_hash != record["payload_hash"]:
            raise StoreError("supplied record payload hash mismatch")
        return record

    def _existing(self, record):
        for existing in self.events():
            if existing["operation_id"] == record["operation_id"]:
                if existing["payload_hash"] != record["payload_hash"]:
                    raise StoreConflict("logical operation semantic payload conflict")
                return existing
        return None

    def publish(self, record: dict, *, artifacts: list[dict]) -> dict:
        prepared = self._prepared(record, artifacts)
        with mutation_lock(self.palace):
            existing = self._existing(prepared)
            if existing is not None:
                return existing
            refs = list(_references(prepared))
            for reference in refs:
                self.get_document(reference)
            if prepared["record_type"] == "completed":
                checkpoint = self.checkpoint(prepared.get("scope_id"))
                expected = checkpoint["operation_id"] if checkpoint else None
                if prepared.get("expected_checkpoint_operation_id") != expected:
                    raise StoreConflict("completion checkpoint predecessor conflict")
            body = canonical_json(prepared)
            if len(body.encode("utf-8")) > EVENT_LIMIT:
                raise StoreError("Dreaming event exceeds native body limit; use document artifacts")
            try:
                self._call("mempalace_event_append", {
                    "type": "dream." + prepared["record_type"], "stream": STREAM,
                    "room": ROOM, "from_agent": "dreaming", "body": body,
                    "correlation_id": prepared.get("scope_id"),
                    "artifact_ids": sorted({ref["artifact_id"] for ref in refs}),
                })
            except Exception:
                reconciled = self._existing(prepared)
                if reconciled is not None:
                    return reconciled
                raise
            result = self._existing(prepared)
            if result is None:
                raise StoreError("native event append did not produce a verified readback")
            return result

    def publish_document(self, record: dict, value: dict, *, field: str, purpose: str) -> dict:
        """Prelookup by semantic content before allocating native random IDs."""
        raw = canonical_json(value).encode("utf-8")
        placeholder = {"artifact_id": "pending", "sha256": _digest(raw),
                       "size_bytes": len(raw), "format": "dream-json/v1"}
        if len(raw) > ARTIFACT_LIMIT:
            placeholder.update(format="dream-document/v1", size_bytes=1,
                               document_sha256=_digest(raw), document_size_bytes=len(raw))
        candidate = self._prepared({**record, field: placeholder}, [])
        with mutation_lock(self.palace):
            existing = self._existing(candidate)
            if existing is not None:
                return existing
            reference = self.put_document(value, purpose=purpose)
            return self.publish({**record, field: reference, "operation_id": candidate["operation_id"]},
                                artifacts=[reference])

    def checkpoint(self, scope_id: str) -> dict | None:
        head = None
        for record in self.events(scope_id=scope_id):
            if record["record_type"] != "completed":
                continue
            predecessor = head["operation_id"] if head else None
            if record.get("expected_checkpoint_operation_id") != predecessor:
                raise StoreConflict("conflicting completed checkpoint branches")
            head = record
        if head is None:
            return None
        versions = head.get("reviewed_versions")
        if not isinstance(versions, dict) or "artifact_id" not in versions:
            raise StoreError("completion has no exact reviewed_versions artifact")
        return {**head, "reviewed_versions": self.get_document(versions)}

    def load_run(self, run_id: str) -> dict:
        manifest, review, review_hash = None, None, None
        for record in self.events(run_id=run_id):
            if record["record_type"] == "run_created":
                current = self.get_document(record.get("manifest"))
                if manifest is not None and current != manifest:
                    raise StoreConflict("conflicting manifests for run")
                manifest = current
            elif record["record_type"] == "review_saved":
                if record.get("previous_review_hash") != review_hash:
                    raise StoreConflict("conflicting review predecessor chain")
                review = self.get_document(record.get("review"))
                review_hash = record.get("review_hash")
        if manifest is None:
            raise StoreError(f"native run not found: {run_id}")
        return review if review is not None else manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", required=True)
    parser.add_argument("--initialize", action="store_true", required=True)
    args = parser.parse_args(argv)
    DreamStore.initialize(args.palace)
    print(json.dumps({"initialized": True, "stream": STREAM}))


if __name__ == "__main__":
    main()
