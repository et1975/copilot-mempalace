"""Strict dreaming provenance, including the legacy metadata trailer dialect."""
from __future__ import annotations

import hashlib
import json
from typing import Any

MARKER = "<!--dreaming-meta:"
GENERATED_KINDS = frozenset({"lesson", "reflect", "procedural_event"})


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


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)


def decode_dream_metadata(drawer: dict) -> dict:
    """Merge native/trailer metadata; malformed or disagreeing encodings raise."""
    native = drawer.get("metadata")
    if native is None:
        native = {}
    if not isinstance(native, dict):
        raise ValueError("drawer metadata must be an object")
    result = dict(native)
    text = drawer.get("text", "")
    if not isinstance(text, str):
        raise ValueError("drawer text must be a string")
    decoder = json.JSONDecoder(object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
    offset = 0
    while (start := text.find(MARKER, offset)) >= 0:
        body = text[start + len(MARKER):].lstrip()
        metadata, end = decoder.raw_decode(body)
        if not isinstance(metadata, dict) or not body[end:].lstrip().startswith("-->"):
            raise ValueError("invalid dreaming metadata trailer")
        for key, value in metadata.items():
            if key in result and result[key] != value:
                raise ValueError(f"conflicting dreaming metadata: {key}")
            result[key] = value
        offset = len(text) - len(body) + end
    for key in ("kind", "source_kind", "generated_from", "added_by"):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError(f"invalid dreaming metadata field: {key}")
    return result


def is_generated_observation(drawer: dict) -> bool:
    metadata = decode_dream_metadata(drawer)
    return (metadata.get("kind") in GENERATED_KINDS
            or metadata.get("source_kind") in GENERATED_KINDS
            or metadata.get("generated_from") in GENERATED_KINDS
            or metadata.get("added_by") in {"dream-procedure", "dream-reflect"}
            or bool(metadata.get("generated_summary")))


def is_procedural_record(drawer: dict) -> bool:
    metadata = decode_dream_metadata(drawer)
    return metadata.get("kind") == "procedural_event" or metadata.get("room") == "procedural"


def decode_procedural_chunks(rows: list[dict]) -> list[dict]:
    """Reassemble procedural-only rows without altering a single character.

    Unlike legacy mined text, arbitrary offsets can split JSON tokens. Chunk
    indices are zero-based and total_chunks (when present) must match.
    """
    groups: dict[str, list[dict]] = {}
    physical = set()
    for row in rows:
        if row["id"] in physical:
            raise ValueError("duplicate physical chunk")
        physical.add(row["id"])
        meta = row.get("metadata") or {}
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
        text = "".join(m.get("text", "") for m in members)
        native = {}
        for member in members:
            for key, value in (member.get("metadata") or {}).items():
                if key == "chunk_index":
                    continue
                if key in native and native[key] != value:
                    raise ValueError(f"conflicting chunk metadata: {key}")
                native[key] = value
        meta = decode_dream_metadata({"text": text, "metadata": native})
        if meta.get("kind") != "procedural_event" or type(meta.get("schema_version")) is not int \
                or meta["schema_version"] != 1:
            raise ValueError("invalid procedural metadata/schema version")
        event = meta.get("event")
        if not isinstance(event, dict) or event.get("digest") != content_hash(
                canonical_json({k: v for k, v in event.items() if k != "digest"})):
            raise ValueError("procedural event digest mismatch")
        result.append({"id": parent, "member_ids": [m["id"] for m in members],
                       "text": text, "metadata": meta, "wing": meta.get("wing"),
                       "room": meta.get("room"), "content_hash": content_hash(text)})
    return result
