"""Bounded receipt drafts. No publication, source capture or causal inference."""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import stat

from dream_metadata import canonical_json, content_hash, strict_json
from dream_procedural import EvidenceReference, repository_key, to_data, utc_datetime

MAX_RECEIPT_BYTES = 24576
MAX_PACKET_BYTES = 65536
MAX_FIELD_BYTES = 256 * 1024


class SessionStoreInvalid(RuntimeError):
    """Present original input is invalid/unreadable, not absent evidence."""
    code = "session_store_invalid"


def read_receipt(path: str, repository: str) -> dict:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt exceeds byte limit")
    return validate_receipt(strict_json(raw.decode("utf-8")), repository)


def validate_receipt(receipt: dict, repository: str) -> dict:
    fields = {"schema_version", "repository", "session_id", "generation", "task",
              "task_digest", "delivered_rule_ids", "guidance_as_of"}
    if not isinstance(receipt, dict) or receipt.keys() != fields:
        raise ValueError("invalid receipt fields")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("invalid receipt schema_version")
    if receipt["repository"] != repository_key(repository):
        raise ValueError("receipt repository mismatch")
    if not isinstance(receipt["session_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", receipt["session_id"]):
        raise ValueError("invalid receipt session_id")
    if type(receipt["generation"]) is not int or receipt["generation"] < 0:
        raise ValueError("invalid receipt generation")
    task = receipt["task"]
    if not isinstance(task, str) or not task.strip() or len(task.encode("utf-8")) > 16384:
        raise ValueError("invalid receipt task")
    if receipt["task_digest"] != content_hash(task):
        raise ValueError("receipt task digest mismatch")
    ids = receipt["delivered_rule_ids"]
    if not isinstance(ids, list) or len(ids) > 5 or any(
            not isinstance(rid, str) or not re.fullmatch(r"proc:[0-9a-f]{64}", rid) for rid in ids
    ) or len(set(ids)) != len(ids):
        raise ValueError("invalid delivered rule IDs")
    if receipt["guidance_as_of"] is not None:
        utc_datetime(receipt["guidance_as_of"])
    return receipt


def is_procedural_echo(text: str) -> bool:
    """Known generated transport markers, not semantic evidence classification."""
    if "[procedural-context]" in text:
        return True
    # Rendered/fenced transport packets often accompany ordinary transcript
    # prose. Abstain on the complete transport signature, even in that wrapper.
    if all(re.search(r'"' + key + r'"\s*:', text)
           for key in ("policy_version", "rules", "anti_patterns", "trials")):
        return True
    if re.search(r'"kind"\s*:\s*"(procedural_draft|lesson|reflect|procedural_event|procedural_source)"', text):
        return True
    event_fields = {"schema_version", "event_id", "rule_id", "event_kind", "recorded_at",
                    "actor_kind", "session_id", "payload", "digest"}
    # Public envelopes use event_kind, not the drawer's kind metadata. Match
    # their complete signature even when prose or a code fence wraps the JSON.
    if re.search(r'"event_kind"\s*:\s*"(proposal|review|outcome)"', text) and all(
            re.search(r'"' + key + r'"\s*:', text) for key in event_fields):
        return True
    try:
        value = strict_json(text)
    except ValueError:
        return False
    return isinstance(value, dict) and (
        value.get("kind") in {"procedural_draft", "lesson", "reflect", "procedural_event", "procedural_source"}
        or {"policy_version", "rules", "anti_patterns", "trials"} <= value.keys()
        or (event_fields <= value.keys() and value.get("event_kind") in {"proposal", "review", "outcome"}))


def _original_turn(receipt, session_store, as_of):
    """Only the unique exact task turn; never use a nearby successful turn."""
    if session_store is None:
        return [], [], ["session_store_not_supplied"]
    path = Path(session_store).expanduser().resolve()
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return [], [], ["session_store_unavailable"]
    if not stat.S_ISREG(mode):
        raise SessionStoreInvalid("original session store must be a regular SQLite file")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            con.execute("BEGIN")
            # Validate required input schema before declaring a missing session
            # or turn; an incomplete database cannot authorize capture fallback.
            con.execute("SELECT id, repository FROM sessions LIMIT 0")
            con.execute("SELECT session_id, turn_index, timestamp, user_message, assistant_response "
                        "FROM turns LIMIT 0")
            sessions = con.execute("SELECT repository FROM sessions WHERE id=? LIMIT 2",
                                   (receipt["session_id"],)).fetchall()
            if not sessions:
                return [], [], ["original_session_missing"]
            if len(sessions) != 1 or repository_key(sessions[0][0]) != receipt["repository"]:
                raise ValueError("original session repository mismatch")
            # SQL projects only bounded fields; oversized originals never cross
            # into Python. Hashes always cover complete fields, never prefixes.
            rows = con.execute("""
                SELECT turn_index, timestamp,
                       CASE WHEN length(CAST(user_message AS BLOB)) <= ? THEN user_message END,
                       CASE WHEN length(CAST(assistant_response AS BLOB)) <= ? THEN assistant_response END,
                       length(CAST(assistant_response AS BLOB))
                FROM turns WHERE session_id=? AND user_message=? LIMIT 2
            """, (MAX_FIELD_BYTES, MAX_FIELD_BYTES, receipt["session_id"], receipt["task"])).fetchall()
            if len(rows) == 1:
                identities = con.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND turn_index=? LIMIT 2",
                    (receipt["session_id"], rows[0][0])).fetchall()
                if len(identities) != 1:
                    return [], [], ["ambiguous_original_task_turn"]
                latest = con.execute("SELECT MAX(turn_index) FROM turns WHERE session_id=?",
                                     (receipt["session_id"],)).fetchone()[0]
                if rows[0][0] != latest:
                    return [], [], ["original_task_turn_not_current"]
    except sqlite3.Error as exc:
        raise SessionStoreInvalid(f"invalid or unreadable original session store: {exc}") from exc
    if not rows:
        return [], [], ["original_task_turn_missing"]
    if len(rows) != 1:
        return [], [], ["ambiguous_original_task_turn"]
    index, timestamp, user, assistant, assistant_bytes = rows[0]
    if type(index) is not int or index < 0 or utc_datetime(timestamp) > as_of:
        raise ValueError("invalid original turn index or timestamp")
    return _turn_references(receipt, index, user, assistant, assistant_bytes)


def _turn_references(receipt, index, user, assistant, assistant_bytes):
    refs, lineage, missing = [], [], []
    for field, text in (("user_message", user), ("assistant_response", assistant)):
        if text is None and field == "assistant_response" and assistant_bytes and assistant_bytes > MAX_FIELD_BYTES:
            missing.append("original_field_byte_limit")
        elif not isinstance(text, str) or not text.strip():
            missing.append("original_response_missing" if field == "assistant_response" else "original_task_missing")
        else:
            ref = to_data(EvidenceReference("session_turn", receipt["session_id"], receipt["session_id"],
                                           text.strip()[:800], content_hash(text), index, field))
            if is_procedural_echo(text):
                lineage.append({**ref, "reason": "generated_transport_echo"})
                missing.append("original_response_is_generated_echo")
            else:
                refs.append(ref)
    return refs, lineage, missing


def _captured_turn(receipt, reader, as_of):
    fields, omitted = reader.captured_turn_fields(receipt["session_id"])
    if omitted:
        return [], [], ["captured_lookup_limit"]
    matches = [data for data in fields if data["identity"]["field"] == "user_message"
               and data["captured_text"] == receipt["task"]]
    if not matches:
        return [], [], ["original_task_turn_missing"]
    if len(matches) != 1:
        return [], [], ["ambiguous_original_task_turn"]
    index = matches[0]["identity"]["turn_index"]
    if any(data["identity"]["turn_index"] > index for data in fields):
        return [], [], ["original_task_turn_not_current"]
    values = {}
    for data in fields:
        identity = data["identity"]
        if identity["turn_index"] != index:
            continue
        if data["repository"] != receipt["repository"] or utc_datetime(data["observed_at"]) > as_of:
            raise ValueError("captured turn repository or timestamp mismatch")
        if identity["field"] in values:
            return [], [], ["ambiguous_original_task_turn"]
        values[identity["field"]] = data["captured_text"]
    assistant = values.get("assistant_response")
    size = len(assistant.encode("utf-8")) if assistant is not None else None
    return _turn_references(receipt, index, values["user_message"],
                            assistant if size is None or size <= MAX_FIELD_BYTES else None, size)


def build_draft(receipt: dict, *, repository: str, session_store: str | None, as_of,
                evidence_reader=None) -> dict:
    receipt = validate_receipt(receipt, repository)
    refs, lineage, missing = _original_turn(receipt, session_store, as_of)
    if not refs and evidence_reader is not None and missing in (
            ["session_store_not_supplied"], ["session_store_unavailable"], ["original_session_missing"]):
        refs, lineage, captured_missing = _captured_turn(receipt, evidence_reader, as_of)
        missing = captured_missing if refs or captured_missing != ["original_task_turn_missing"] else missing
    packet = {
        "schema_version": 1, "kind": "procedural_draft",
        "status": "pending_original_evidence" if missing else "requires_review",
        **{key: receipt[key] for key in ("repository", "session_id", "task_digest", "delivered_rule_ids")},
        "original_references": refs,
        "lineage_references": [
            {"source_kind": "delivery_receipt", "session_id": receipt["session_id"],
             "generation": receipt["generation"], "task_digest": receipt["task_digest"],
             "guidance_as_of": receipt["guidance_as_of"]}, *lineage],
        "missing_requirements": [*missing, "rule_specific_causal_attribution",
                                 "explicit_polarity", "applicability_and_exceptions_review",
                                 "original_evidence_review"],
        "independent_sessions": len({r["session_id"] for r in refs}),
        "omitted_count": len(missing),
    }
    if len((canonical_json(packet) + "\n").encode("utf-8")) > MAX_PACKET_BYTES:
        raise ValueError("draft exceeds complete packet byte limit")
    return packet
