"""Strict dreaming provenance, including the legacy metadata trailer dialect."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

MARKER = "<!--dreaming-meta:"
GENERATED_TRANSPORT_KINDS = frozenset({
    "procedural_delivery_packet", "procedural_applicability_witness", "procedural_use_check",
})
GENERATED_KINDS = frozenset({"lesson", "reflect", "procedural_event", "procedural_source"}) \
    | GENERATED_TRANSPORT_KINDS
_JSON_STRING = r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"'
_TRANSPORT_PAIRS = re.compile(rf'(?P<key>{_JSON_STRING})\s*:\s*(?P<value>{_JSON_STRING})')
_TRANSPORT_TOKENS = re.compile(_JSON_STRING)


class GeneratedTransportEvidence(ValueError):
    code = "generated_transport"


class TransportInspectionLimit(ValueError):
    code = "transport_inspection_limit"


def generated_transport_kind(full_text: str) -> str | None:
    """Recognize marked copies, including JSON strings in prose/fences.

    This is not a classifier for deliberately stripped provenance or agent prose.
    Four decoding layers bound supported wrappers; uncertainty is not evidence.
    """
    if not isinstance(full_text, str):
        raise ValueError("transport inspection requires a complete text field")
    pending = [full_text]
    for _ in range(4):
        decoded = []
        for text in pending:
            for match in _TRANSPORT_PAIRS.finditer(text):
                key, value = (json.loads(match.group(name)) for name in ("key", "value"))
                if key == "kind" and value in GENERATED_TRANSPORT_KINDS:
                    return value
            for match in _TRANSPORT_TOKENS.finditer(text):
                value = json.loads(match.group())
                if '"' in value or "\\" in value:
                    decoded.append(value)
        if not decoded:
            return None
        pending = decoded
    raise TransportInspectionLimit("bounded full-source transport check is inconclusive")


def reject_generated_transport(full_text: str) -> None:
    kind = generated_transport_kind(full_text)
    if kind is not None:
        raise GeneratedTransportEvidence(f"generated transport is not original evidence: {kind}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        _invalid_constant(value)
    return parsed


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant,
                      parse_float=_finite_float)


def split_dream_metadata(drawer: dict) -> tuple[str, dict]:
    """Peel one recognized terminal trailer and return body/merged metadata."""
    native = drawer.get("metadata")
    if native is None:
        native = {}
    if not isinstance(native, dict):
        raise ValueError("drawer metadata must be an object")
    result = dict(native)
    text = drawer.get("text", "")
    if not isinstance(text, str):
        raise ValueError("drawer text must be a string")
    body_text = text
    lines = text.rstrip().split("\n")
    fence = None
    for line in lines[:-1]:
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if match:
            delimiter, suffix = match.groups()
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not suffix.strip():
                fence = None
    # Writers append one canonical JSON line. Inline examples and quoted code
    # are content, not provenance, even when they contain the marker.
    generated_writer = native.get("added_by") in {
        "dreaming", "dream-reflect", "dream-procedure", "dream-procedure-source"}
    if lines and (fence is None or generated_writer) and lines[-1].startswith(MARKER):
        decoder = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant,
                                   parse_float=_finite_float)
        body = lines[-1][len(MARKER):].lstrip()
        metadata, end = decoder.raw_decode(body)
        if not isinstance(metadata, dict) or body[end:].strip() != "-->":
            raise ValueError("invalid dreaming metadata trailer")
        for key, value in metadata.items():
            if key in result and result[key] != value:
                raise ValueError(f"conflicting dreaming metadata: {key}")
            result[key] = value
        body_text = text[:len(text.rstrip()) - len(lines[-1])]
    for key in ("kind", "source_kind", "generated_from", "added_by"):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError(f"invalid dreaming metadata field: {key}")
    return body_text, result


def decode_dream_metadata(drawer: dict) -> dict:
    """Merge native/trailer metadata; malformed or disagreeing encodings raise."""
    return split_dream_metadata(drawer)[1]


def is_generated_observation(drawer: dict) -> bool:
    metadata = decode_dream_metadata(drawer)
    return (metadata.get("kind") in GENERATED_KINDS
            or metadata.get("source_kind") in GENERATED_KINDS
            or metadata.get("generated_from") in GENERATED_KINDS
            or metadata.get("added_by") in {"dream-procedure", "dream-reflect", "dream-procedure-source"}
            or metadata.get("room") == "procedural-sources"
            or bool(metadata.get("generated_summary"))
            or generated_transport_kind(drawer.get("text", "")) is not None)


def is_procedural_record(drawer: dict) -> bool:
    metadata = decode_dream_metadata(drawer)
    return (metadata.get("kind") in {"procedural_event", "procedural_source"}
            or metadata.get("room") in {"procedural", "procedural-sources"})


def exact_chunk_groups(rows: list[dict]) -> list[tuple[str, list[dict], dict]]:
    """Validate/order arbitrary-offset chunks, also usable with metadata only."""
    groups: dict[str, list[dict]] = {}
    physical = set()
    for row in rows:
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise ValueError("invalid physical chunk ID")
        if row["id"] in physical:
            raise ValueError("duplicate physical chunk")
        physical.add(row["id"])
        meta = row.get("metadata", {})
        if not isinstance(meta, dict):
            raise ValueError("chunk metadata must be an object")
        if "parent_drawer_id" in meta and (
                not isinstance(meta["parent_drawer_id"], str) or not meta["parent_drawer_id"]):
            raise ValueError("invalid parent chunk ID")
        groups.setdefault(meta.get("parent_drawer_id") or row["id"], []).append(row)
    result = []
    for parent, members in sorted(groups.items()):
        chunked = len(members) > 1 or any(
            "chunk_index" in (m.get("metadata") or {}) for m in members)
        if chunked:
            indices = [(m.get("metadata") or {}).get("chunk_index") for m in members]
            if any(type(i) is not int for i in indices) or sorted(indices) != list(range(len(members))):
                raise ValueError(f"noncontiguous procedural chunks: {parent}")
            members = sorted(members, key=lambda m: m["metadata"]["chunk_index"])
        for member in members:
            total = (member.get("metadata") or {}).get("total_chunks", len(members))
            if type(total) is not int or total != len(members):
                raise ValueError(f"incomplete procedural chunks: {parent}")
        native = {}
        for member in members:
            for key, value in (member.get("metadata") or {}).items():
                if key == "chunk_index":
                    continue
                if key in native and native[key] != value:
                    raise ValueError(f"conflicting chunk metadata: {key}")
                native[key] = value
        result.append((parent, members, native))
    return result


def assemble_exact_chunks(rows: list[dict]) -> list[dict]:
    """Reassemble without legacy newline insertion or event-specific parsing."""
    result = []
    for parent, members, native in exact_chunk_groups(rows):
        if any(not isinstance(m.get("text"), str) for m in members):
            raise ValueError("chunk text must be a string")
        text = "".join(m["text"] for m in members)
        result.append({"id": parent, "member_ids": [m["id"] for m in members],
                       "text": text, "metadata": native, "wing": native.get("wing"),
                       "room": native.get("room"), "content_hash": content_hash(text)})
    return result


def decode_procedural_chunks(rows: list[dict]) -> list[dict]:
    """Strict event validation layered over shared arbitrary-offset assembly."""
    result = []
    for drawer in assemble_exact_chunks(rows):
        meta = decode_dream_metadata(drawer)
        if meta.get("kind") != "procedural_event" or type(meta.get("schema_version")) is not int \
                or meta["schema_version"] != 1:
            raise ValueError("invalid procedural metadata/schema version")
        event = meta.get("event")
        if not isinstance(event, dict) or event.get("digest") != content_hash(
                canonical_json({k: v for k, v in event.items() if k != "digest"})):
            raise ValueError("procedural event digest mismatch")
        result.append({**drawer, "metadata": meta})
    return result


def is_source_record_metadata(metadata: dict) -> bool:
    """Native-only reservation check, safe before legacy chunk concatenation."""
    if not isinstance(metadata, dict):
        raise ValueError("drawer metadata must be an object")
    return (metadata.get("room") == "procedural-sources"
            or metadata.get("kind") == "procedural_source"
            or metadata.get("added_by") == "dream-procedure-source")
