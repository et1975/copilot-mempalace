"""Pure, dependency-free core for wing-scoped logical export/import.

No mempalace import so this module (and its tests) stay unit-testable in
isolation. All palace I/O — ChromaDB reads, KG SQLite reads, MCP handler
writes — lives in ``palace_wing.py``; this module owns only deterministic data
transforms:

* bundle record construction and JSONL (de)serialization,
* manifest build / validate (rejecting unknown ``bundle_version``),
* ``source_drawer_id -> wing`` triple filtering,
* the tunnel-touch predicate and ``--into-wing`` endpoint remap,
* the provenance-trailer encode/decode (mirrors dreaming's ``<!--…-->`` trick).

The bundle is JSONL: line 1 is always a ``manifest`` record; subsequent lines
are ``drawer`` / ``kg_triple`` / ``tunnel`` records. Import is a *replay*:
drawers get new IDs and are re-embedded, so ``orig_*`` fields are informational
provenance only, never authoritative after import.
"""
from __future__ import annotations

import json
import re
from typing import Any

BUNDLE_VERSION = 1
TOOL_VERSION = 1

# Marker for the machine-readable provenance trailer appended to drawer content
# so metadata ``mempalace_add_drawer`` cannot set (topic/hall/type/date) is not
# silently lost. Mirrors dreaming's ``<!--dreaming-meta: …-->`` convention.
TRAILER_MARKER = "wing-meta"
_TRAILER_SEP = f"\n\n<!--{TRAILER_MARKER}: "


# --------------------------------------------------------------------------- #
# Record builders.
# --------------------------------------------------------------------------- #
def build_manifest(
    wing: str,
    mempalace_version: str,
    counts: dict[str, int],
    kg_note: str,
    exported_at: str,
) -> dict[str, Any]:
    """Build the line-1 manifest record for a bundle."""
    return {
        "type": "manifest",
        "bundle_version": BUNDLE_VERSION,
        "wing": wing,
        "mempalace_version": mempalace_version,
        "tool_version": TOOL_VERSION,
        "exported_at": exported_at,
        "counts": {
            "drawers": int(counts.get("drawers", 0)),
            "kg_triples": int(counts.get("kg_triples", 0)),
            "tunnels": int(counts.get("tunnels", 0)),
        },
        "kg_note": kg_note,
    }


def drawer_record(
    wing: str,
    room: str,
    content: str,
    source_file: str | None,
    added_by: str | None,
    orig_drawer_id: str | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a ``drawer`` bundle record.

    ``wing`` is carried for clarity and to support ``--into-wing`` remap; the
    importer sets a drawer's wing from the resolved target wing regardless.
    """
    return {
        "type": "drawer",
        "wing": wing,
        "room": room,
        "content": content,
        "source_file": source_file,
        "added_by": added_by,
        "orig_drawer_id": orig_drawer_id,
        "extra": dict(extra) if extra else {},
    }


def kg_triple_record(
    subject: str,
    predicate: str,
    object: str,
    confidence: float | None,
    valid_from: str | None,
    valid_to: str | None,
    orig_source_drawer_id: str | None,
) -> dict[str, Any]:
    """Build a ``kg_triple`` bundle record."""
    return {
        "type": "kg_triple",
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "confidence": confidence,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "orig_source_drawer_id": orig_source_drawer_id,
    }


def tunnel_record(
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str,
) -> dict[str, Any]:
    """Build a ``tunnel`` bundle record."""
    return {
        "type": "tunnel",
        "source": {"wing": source_wing, "room": source_room},
        "target": {"wing": target_wing, "room": target_room},
        "label": label,
    }


# --------------------------------------------------------------------------- #
# JSONL (de)serialization.
# --------------------------------------------------------------------------- #
def dump_jsonl(records: list[dict[str, Any]]) -> str:
    """Serialize records to newline-delimited JSON (one object per line)."""
    lines = [json.dumps(rec, ensure_ascii=False, sort_keys=True) for rec in records]
    return "\n".join(lines) + ("\n" if lines else "")


def _replay_json_decoder() -> json.JSONDecoder:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate record metadata key {key!r}")
            result[key] = value
        return result

    return json.JSONDecoder(object_pairs_hook=unique_object)


def parse_replay_json(text: str) -> Any:
    """Decode without silently overwriting duplicate record/metadata keys."""
    return _replay_json_decoder().decode(text)


def parse_jsonl(text: str, *, for_replay: bool = False) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON; replay refuses ambiguous duplicate keys."""
    records = []
    for line in text.splitlines():
        if line.strip():
            records.append(parse_replay_json(line) if for_replay else json.loads(line))
    return records


# --------------------------------------------------------------------------- #
# Manifest validation.
# --------------------------------------------------------------------------- #
def validate_manifest(obj: Any) -> None:
    """Validate a parsed manifest; raise ``ValueError`` on any problem.

    Rejects a non-manifest first line, an unknown ``bundle_version``, and any
    missing required field so malformed bundles fail fast before writes.
    """
    if not isinstance(obj, dict) or obj.get("type") != "manifest":
        raise ValueError("first bundle line is not a manifest record")
    version = obj.get("bundle_version")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"unsupported bundle_version {version!r}; "
            f"this tool supports version {BUNDLE_VERSION}"
        )
    for field in ("wing", "counts"):
        if field not in obj:
            raise ValueError(f"manifest is missing required field {field!r}")
    wing = obj.get("wing")
    if not isinstance(wing, str) or not wing.strip():
        raise ValueError("manifest 'wing' must be a non-empty string")
    counts = obj.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("manifest 'counts' must be an object")
    for key in ("drawers", "kg_triples", "tunnels"):
        if key in counts:
            value = counts[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(
                    f"manifest 'counts.{key}' must be a non-negative integer"
                )


_PROCEDURAL_METADATA = {
    "room": ("procedural", "procedural-sources", "procedural-receipts"),
    "kind": ("procedural_event", "procedural_source", "procedural_receipt"),
    "added_by": ("dream-procedure", "dream-procedure-source", "dream-procedure-receipt"),
    "agent": ("dream-procedure", "dream-procedure-source", "dream-procedure-receipt"),
}
_METADATA_COMMENT = re.compile(r"<!--(?:dreaming-meta|wing-meta)\b")
_METADATA_VALUE_PREFIX = re.compile(r"\s*:\s*")
_METADATA_VALUE_SUFFIX = re.compile(r"\s*-->")


def _validate_replay_metadata(metadata: Any) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("record metadata must be an object for safe replay")
    for field, protected in _PROCEDURAL_METADATA.items():
        value = metadata.get(field)
        if value in protected:
            raise ValueError(
                "Cannot replay procedural events, sources or receipts: new drawer IDs "
                "cannot preserve immutable references and digests. Use whole-palace "
                "physical backup/restore instead."
            )
        if value is not None and not isinstance(value, str):
            raise ValueError(f"record metadata {field!r} must be a string")


def validate_replay_records(records: list[dict[str, Any]]) -> None:
    """Refuse identity-bound records across the complete bundle before writes.

    Inspect every metadata trailer, not only the last one: wing provenance may
    wrap dreaming metadata. Invalid metadata cannot be treated as absent.
    Ordinary source archives are not procedural sources.
    """
    decoder = _replay_json_decoder()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("bundle record must be an object")
        _validate_replay_metadata(record)
        for field in ("extra", "metadata"):
            if record.get(field) is not None:
                _validate_replay_metadata(record[field])
        content = record.get("content", "")
        if not isinstance(content, str):
            raise ValueError("bundle record content must be a string")
        cursor = 0
        while marker := _METADATA_COMMENT.search(content, cursor):
            prefix = _METADATA_VALUE_PREFIX.match(content, marker.end())
            if prefix is None:
                raise ValueError("malformed record metadata trailer")
            try:
                metadata, end = decoder.raw_decode(content, prefix.end())
            except ValueError as exc:
                raise ValueError("malformed record metadata trailer") from exc
            suffix = _METADATA_VALUE_SUFFIX.match(content, end)
            if suffix is None:
                raise ValueError("unterminated or malformed record metadata trailer delimiter")
            _validate_replay_metadata(metadata)
            cursor = suffix.end()
        if record.get("type") == "manifest" and "drawers" in record:
            entries = record["drawers"]
            if not isinstance(entries, list):
                raise ValueError("manifest drawer records must be an array")
            validate_replay_records(entries)


# --------------------------------------------------------------------------- #
# Triple / tunnel / remap transforms.
# --------------------------------------------------------------------------- #
def filter_wing_triples(
    triples: list[dict[str, Any]], wing_drawer_ids: Any
) -> tuple[list[dict[str, Any]], int]:
    """Keep triples whose ``source_drawer_id`` is a known wing drawer id.

    Triples with a NULL or foreign ``source_drawer_id`` cannot be attributed to
    the wing and are skipped; the second element is that skip count so the
    caller can report best-effort loss in ``manifest.kg_note``.
    """
    ids = set(wing_drawer_ids)
    kept: list[dict[str, Any]] = []
    skipped = 0
    for triple in triples:
        source_id = triple.get("source_drawer_id")
        if source_id is not None and source_id in ids:
            kept.append(triple)
        else:
            skipped += 1
    return kept, skipped


def tunnel_touches_wing(tunnel: dict[str, Any], wing: str) -> bool:
    """True if either endpoint of ``tunnel`` lives in ``wing``."""
    source = tunnel.get("source") or {}
    target = tunnel.get("target") or {}
    return source.get("wing") == wing or target.get("wing") == wing


def remap_into_wing(
    record: dict[str, Any], old_wing: str, new_wing: str
) -> dict[str, Any]:
    """Return a copy of ``record`` with ``old_wing`` endpoints renamed.

    For a drawer record, its ``wing`` is remapped. For a tunnel record, only the
    endpoint(s) whose wing equals ``old_wing`` are renamed; endpoints pointing at
    other wings are left untouched. Records of other types are returned as-is.
    """
    rec = dict(record)
    kind = rec.get("type")
    if kind == "drawer":
        if rec.get("wing") == old_wing:
            rec["wing"] = new_wing
    elif kind == "tunnel":
        for endpoint in ("source", "target"):
            value = rec.get(endpoint)
            if isinstance(value, dict) and value.get("wing") == old_wing:
                remapped = dict(value)
                remapped["wing"] = new_wing
                rec[endpoint] = remapped
    return rec


# --------------------------------------------------------------------------- #
# Provenance trailer.
# --------------------------------------------------------------------------- #
def encode_trailer(content: str, extra: dict[str, Any] | None) -> str:
    """Append a machine-readable metadata trailer to ``content``.

    Returns ``content`` unchanged when ``extra`` is empty so drawers without
    extra metadata are stored verbatim.
    """
    if not extra:
        return content
    meta = json.dumps(extra, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{content}\n\n<!--{TRAILER_MARKER}: {meta}-->"


def decode_trailer(content: str) -> tuple[str, dict[str, Any]]:
    """Split a trailer off ``content``; tolerant of an absent trailer.

    Returns ``(content_without_trailer, extra)``. Parses only the **last**
    trailer marker, so content that itself contains trailer-like text round-trips
    correctly (``encode`` appends exactly one trailer; ``decode`` removes exactly
    one). When no trailer is present, or the trailer is malformed, returns
    ``(content, {})`` and never raises.
    """
    idx = content.rfind(_TRAILER_SEP)
    if idx == -1:
        return content, {}
    tail = content[idx + len(_TRAILER_SEP):].rstrip()
    if not tail.endswith("-->"):
        return content, {}
    try:
        extra = json.loads(tail[:-3])
    except json.JSONDecodeError:
        return content, {}
    if not isinstance(extra, dict):
        return content, {}
    return content[:idx], extra


# --------------------------------------------------------------------------- #
# Markdown-directory serialization (human-readable, lossless round-trip).
#
# An alternative to the single JSONL bundle: a directory holding one markdown
# file PER DRAWER (verbatim content under an HTML metadata header), plus
# ``kg.jsonl`` / ``tunnels.jsonl`` (structured) and a ``manifest.json`` index.
# One-file-per-drawer keeps drawer boundaries unambiguous (the legacy
# one-file-per-room export merged drawers and lost them). These helpers are pure
# text transforms; file I/O lives in ``palace_wing.py``.
# --------------------------------------------------------------------------- #
MD_DRAWER_MARKER = "mempalace-drawer"
_MD_OPEN = f"<!--{MD_DRAWER_MARKER}\n"
_MD_CLOSE = "\n-->\n"
_MD_HEADER_FIELDS = frozenset({"wing", "room", "drawer_id", "added_by", "source_file", "extra"})


def encode_drawer_md(record: dict[str, Any]) -> str:
    """Serialize a ``drawer`` record to markdown: metadata header + verbatim body.

    Metadata (single-line) lives in a leading ``<!--mempalace-drawer ... -->``
    comment; the drawer content follows verbatim (may contain anything, including
    ``-->``). ``encode``/``decode`` are exact inverses.
    """
    extra = record.get("extra") or {}
    meta_lines = [
        f"wing: {record.get('wing') or ''}",
        f"room: {record.get('room') or ''}",
        f"drawer_id: {record.get('orig_drawer_id') or ''}",
        f"added_by: {record.get('added_by') or ''}",
        f"source_file: {_oneline(record.get('source_file'))}",
        "extra: " + json.dumps(extra, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")),
    ]
    return _MD_OPEN + "\n".join(meta_lines) + _MD_CLOSE + (record.get("content") or "")


def decode_drawer_md(text: str, *, for_replay: bool = False) -> dict[str, Any]:
    """Parse a drawer markdown file back into a ``drawer`` bundle record.

    Inverse of ``encode_drawer_md``. Raises ``ValueError`` if ``text`` is not a
    mempalace drawer markdown file. ``for_replay`` also rejects unsafe header
    metadata before tolerant archive decoding could discard it.
    """
    if not text.startswith(_MD_OPEN):
        raise ValueError("not a mempalace drawer markdown file")
    end = text.find(_MD_CLOSE, len(_MD_OPEN) - 1)
    if end == -1:
        raise ValueError("drawer markdown header is not terminated")
    header = text[len(_MD_OPEN):end]
    content = text[end + len(_MD_CLOSE):]
    meta: dict[str, str] = {}
    for line in header.split("\n"):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if for_replay and (not sep or key.strip() not in _MD_HEADER_FIELDS):
            raise ValueError("malformed or unrecognized drawer header metadata")
        if sep:
            if for_replay and key.strip() in meta:
                raise ValueError(f"duplicate drawer header metadata key {key.strip()!r}")
            meta[key.strip()] = value.strip()
    try:
        raw_extra = meta.get("extra") or "{}"
        extra = parse_replay_json(raw_extra) if for_replay else json.loads(raw_extra)
    except ValueError as exc:
        if for_replay:
            raise ValueError("malformed drawer header metadata") from exc
        extra = {}
    if not isinstance(extra, dict):
        if for_replay:
            raise ValueError("drawer header metadata must be an object")
        extra = {}
    if for_replay:
        _validate_replay_metadata(meta)
        _validate_replay_metadata(extra)
    return {
        "type": "drawer",
        "wing": meta.get("wing") or "",
        "room": meta.get("room") or "general",
        "content": content,
        "source_file": meta.get("source_file") or None,
        "added_by": meta.get("added_by") or None,
        "orig_drawer_id": meta.get("drawer_id") or None,
        "extra": extra,
    }


def md_drawer_filename(record: dict[str, Any], index: int) -> str:
    """Deterministic, filesystem-safe markdown filename for a drawer record."""
    did = record.get("orig_drawer_id")
    if did:
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(did))
        return f"{safe}.md"
    room = "".join(c if (c.isalnum() or c in "-_.") else "_"
                   for c in str(record.get("room") or "general"))
    return f"{room}__{index:04d}.md"


def _oneline(value: Any) -> str:
    """Collapse a metadata value to a single line (metadata is never multi-line)."""
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ")
