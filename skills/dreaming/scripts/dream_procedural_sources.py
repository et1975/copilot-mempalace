"""Drawer-backed original-source witnesses; no host-session or admission authority.

Callers supply already validated OriginalSource values. Raw turns retain their
full field; drawer witnesses retain provenance only and still require the live
original drawer. Source headers are locators, never sufficient evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
import re
from typing import TYPE_CHECKING, Literal

import dream_palace
from dream_metadata import (
    assemble_exact_chunks, canonical_json, content_hash, decode_dream_metadata,
    exact_chunk_groups, is_generated_observation, strict_json,
)
from dream_procedural import EvidenceReference, _evidence, repository_key, to_data, utc_datetime
from dream_procedural_palace import _wing

if TYPE_CHECKING:
    from dream_procedural_validate import ResolvedEvidence

ROOM = "procedural-sources"
KIND = "procedural_source"
AUTHOR = "dream-procedure-source"
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 1024
MAX_TRAILER_BYTES = 1024
RECORD_WARNING_THRESHOLD = 5000
BYTES_WARNING_THRESHOLD = 64 * 1024 * 1024
_HEADER = "Procedural source record; not independent evidence.\nSource key: {key}\nDigest: {digest}\n\n"
_HEADER_RE = re.compile(
    r"\AProcedural source record; not independent evidence\.\n"
    r"Source key: ([0-9a-f]{64})\nDigest: ([0-9a-f]{64})\n\n")


class SourceAmbiguity(ValueError):
    code = "ambiguous_source"


@dataclass(frozen=True)
class OriginalSource:
    """Validated original provenance supplied by admission, not inferred here."""
    reference: EvidenceReference
    repository: str
    observed_at: datetime
    captured_text: str | None = None


@dataclass(frozen=True)
class SourceLocator:
    drawer_id: str
    member_ids: tuple[str, ...]
    digest: str
    encoded_bytes: int


@dataclass
class SourceIndex:
    """Invocation-local locators/counts; no raw payload or external storage."""
    palace: str
    wing: str
    locators: dict[str, tuple[SourceLocator, ...]]
    record_count: int
    encoded_bytes: int
    warnings: tuple[str, ...]
    _resolved: dict[EvidenceReference, ResolvedEvidence] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CaptureResult:
    status: Literal["appended", "already_exists"]
    source_key: str
    drawer_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def _identity(reference: EvidenceReference) -> dict:
    if not isinstance(reference, EvidenceReference) or reference.session_id is None:
        raise ValueError("source evidence requires an original session_id")
    if reference.source_kind == "drawer" and (
            reference.turn_index is not None or reference.field is not None):
        raise ValueError("drawer evidence cannot name a turn")
    data = to_data(reference)
    _evidence([data])
    # Validation above must not replace as-written identity with normalized IDs.
    return {key: value for key, value in data.items() if key != "quote"}


def source_key(reference: EvidenceReference) -> str:
    return content_hash(canonical_json(_identity(reference)))


def _substantive(source: OriginalSource) -> dict:
    identity = _identity(source.reference)
    repository = repository_key(source.repository)
    if repository != source.repository:
        raise ValueError("source repository must be canonical")
    data = {"schema_version": 1, "identity": identity, "repository": repository,
            "observed_at": to_data(utc_datetime(source.observed_at))}
    if source.reference.source_kind == "session_turn":
        _check_text(source.reference, source.captured_text)
        data["captured_text"] = source.captured_text
    elif source.captured_text is not None:
        raise ValueError("drawer witnesses must not copy original bodies")
    return data


def _check_text(ref: EvidenceReference, text: str) -> None:
    if not isinstance(text, str) or content_hash(text) != ref.source_hash \
            or not ref.quote.strip() or ref.quote not in text:
        raise ValueError(f"source hash/quote drift: {ref.source_id}")


def _metadata(key: str, digest: str) -> dict:
    return {"kind": KIND, "schema_version": 1, "source_key": key, "source_digest": digest}


def _with_trailer(body: str, metadata: dict) -> str:
    return f"{body}\n\n<!--dreaming-meta: {canonical_json(metadata)}-->"


def source_record_data(source: OriginalSource, *, captured_at: datetime,
                       captured_by: str) -> tuple[str, dict]:
    """Canonical body/minimal metadata, bounded including the writer's trailer."""
    data = _substantive(source)
    if not isinstance(captured_by, str) or not captured_by.strip():
        raise ValueError("capture actor must be nonblank text")
    data.update(captured_at=to_data(utc_datetime(captured_at)), captured_by=captured_by)
    payload = canonical_json(data)
    key, digest = source_key(source.reference), content_hash(payload)
    body = _HEADER.format(key=key, digest=digest) + payload
    metadata = _metadata(key, digest)
    if len(_with_trailer(body, metadata).encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("encoded procedural source record exceeds 4 MiB")
    return body, metadata


def _source_groups(collection, wing: str | None):
    reservation = {"$or": [{"room": ROOM}, {"kind": KIND}, {"added_by": AUTHOR}]}
    where = reservation if wing is None else {"$and": [{"wing": wing}, reservation]}
    rows, seen = [], set()
    offset = 0
    while True:
        result = collection.get(where=where, include=["metadatas"], limit=256, offset=offset)
        batch = dream_palace._rows_from_collection_result(result)
        if not batch:
            break
        for row in batch:
            if row["id"] in seen:
                raise ValueError("duplicate physical row during source paging")
            seen.add(row["id"])
            meta = row["metadata"]
            if not isinstance(meta, dict) or meta.get("room") != ROOM \
                    or (wing is not None and meta.get("wing") != wing):
                raise ValueError("source storage returned invalid metadata/scope")
            _wing(meta.get("wing"))
            rows.append({"id": row["id"], "metadata": meta})
        offset += len(batch)
    groups = exact_chunk_groups(rows)
    for parent, members, native in groups:
        _check_chunk_identity(parent, members)
    return groups


def _check_chunk_identity(parent: str, members: list[dict]) -> None:
    for row in members:
        meta = row["metadata"]
        if "parent_drawer_id" in meta:
            index = meta.get("chunk_index")
            if type(index) is not int or row["id"] != f"{parent}_chunk_{index:06d}":
                raise ValueError("invalid source chunk identity")
        elif re.search(r"_chunk_[0-9]+$", row["id"]):
            raise ValueError("source physical chunk is missing its parent identity")


def source_record_ids(collection) -> set[str]:
    """Conservative global retention, even for malformed reserved-room bodies.

    Only metadata is read. Corrupt scope/chunk metadata fails closed; invalid
    record JSON does not make a reserved drawer safe to delete.
    """
    return {value for parent, members, _ in _source_groups(collection, None)
            for value in (parent, *(row["id"] for row in members))}


def _read_row(collection, physical_id: str) -> dict:
    rows = dream_palace._rows_from_collection_result(collection.get(
        ids=[physical_id], include=["documents", "metadatas"]))
    if len(rows) != 1 or rows[0]["id"] != physical_id:
        raise ValueError("missing or conflicting source physical chunk")
    if not isinstance(rows[0]["text"], str):
        raise ValueError("source chunk text must be a string")
    return rows[0]


def _header(text: str) -> tuple[str, str, int]:
    match = _HEADER_RE.match(text)
    if match is None or len(text[:match.end()].encode("utf-8")) > MAX_HEADER_BYTES:
        raise ValueError("invalid or oversized source record header")
    return match[1], match[2], match.end()


def _check_record_metadata(metadata: dict, key: str, digest: str) -> None:
    expected = _metadata(key, digest)
    if any(metadata.get(k) != v or type(metadata.get(k)) is not type(v)
           for k, v in expected.items()):
        raise ValueError("source record metadata/header mismatch")
    if metadata.get("added_by") != AUTHOR:
        raise ValueError("invalid source record author")


def read_source_records(palace: str, wing: str) -> SourceIndex:
    """Reconcile bounded header/trailer claims before routing source locators.

    The collection API has no prefix/byte-length projection. Read one physical
    chunk at a time (at most one 4 MiB logical record), not pages of raw bodies.
    Full canonical/digest validation is deferred until a key is requested.
    """
    path, wing = os.path.realpath(os.path.expanduser(palace)), _wing(wing)
    collection = dream_palace.procedural_collection(path)
    locators, total, count = {}, 0, 0
    for parent, members, native in _source_groups(collection, wing):
        prefix, size = b"", 0
        last_line, line_size = b"", 0
        for member in members:
            row = _read_row(collection, member["id"])
            if row["metadata"] != member["metadata"]:
                raise ValueError("source metadata changed during discovery")
            encoded = row["text"].encode("utf-8")
            size += len(encoded)
            if size > MAX_RECORD_BYTES:
                raise ValueError("encoded procedural source record exceeds 4 MiB")
            prefix += encoded[:max(0, MAX_HEADER_BYTES - len(prefix))]
            newline = encoded.rfind(b"\n")
            if newline >= 0:
                last_line, line_size = b"", 0
            fragment = encoded[newline + 1:]
            line_size += len(fragment)
            last_line += fragment[:max(0, MAX_TRAILER_BYTES - len(last_line))]
        key, digest, _ = _header(prefix.decode("utf-8", errors="ignore"))
        # Keep the start of the actual final line, not an arbitrary body suffix
        # that could itself begin with a quoted metadata marker.
        trailer = ""
        if last_line.startswith(b"<!--dreaming-meta:"):
            if line_size > MAX_TRAILER_BYTES:
                raise ValueError("oversized source metadata trailer")
            trailer = last_line.decode("utf-8")
        elif not last_line.strip():
            raise ValueError("invalid source record terminal line")
        claims = decode_dream_metadata({"text": trailer, "metadata": native})
        _check_record_metadata(claims, key, digest)
        locator = SourceLocator(parent, tuple(m["id"] for m in members), digest, size)
        locators.setdefault(key, []).append(locator)
        total += size
        count += 1
    warnings = []
    if count * 5 >= RECORD_WARNING_THRESHOLD * 4:
        warnings.append(f"procedural sources: {count} records; warning threshold {RECORD_WARNING_THRESHOLD}")
    if total * 5 >= BYTES_WARNING_THRESHOLD * 4:
        warnings.append(f"procedural sources: {total} bytes; warning threshold {BYTES_WARNING_THRESHOLD}")
    return SourceIndex(path, wing, {k: tuple(v) for k, v in locators.items()}, count, total, tuple(warnings))


def _decode_record(drawer: dict, key: str, locator: SourceLocator) -> dict:
    text = drawer["text"]
    if len(text.encode("utf-8")) != locator.encoded_bytes:
        raise ValueError("source record changed size after discovery")
    header_key, digest, end = _header(text)
    if (header_key, digest) != (key, locator.digest):
        raise ValueError("source record changed header after discovery")
    meta = decode_dream_metadata(drawer)
    expected = _metadata(key, digest)
    _check_record_metadata(meta, key, digest)
    payload = text[end:]
    trailer = "\n\n<!--dreaming-meta: " + canonical_json(expected) + "-->"
    if payload.endswith(trailer):
        payload = payload[:-len(trailer)]
    data = strict_json(payload)
    if canonical_json(data) != payload or content_hash(payload) != digest:
        raise ValueError("source record canonical body/digest mismatch")
    if not isinstance(data, dict):
        raise ValueError("source record body must be an object")
    required = {"schema_version", "identity", "repository", "observed_at", "captured_at", "captured_by"}
    if not required <= data.keys() or data.keys() - required - {"captured_text"}:
        raise ValueError("invalid source record fields")
    identity = data["identity"]
    if not isinstance(identity, dict):
        raise ValueError("invalid source identity")
    fields = {"source_kind", "source_id", "session_id", "source_hash"}
    if identity.get("source_kind") == "session_turn":
        fields |= {"turn_index", "field"}
    if identity.keys() != fields:
        raise ValueError("invalid source identity fields")
    # Records deliberately omit quotes: each requested reference is rechecked.
    ref = EvidenceReference(**identity, quote="source witness")
    if source_key(ref) != key:
        raise ValueError("source identity/key mismatch")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("invalid source schema version")
    if repository_key(data["repository"]) != data["repository"]:
        raise ValueError("source repository must be canonical")
    for clock in ("observed_at", "captured_at"):
        if to_data(utc_datetime(data[clock])) != data[clock]:
            raise ValueError("noncanonical source timestamp")
    if not isinstance(data["captured_by"], str) or not data["captured_by"].strip():
        raise ValueError("invalid source capture actor")
    if ref.source_kind == "session_turn":
        text = data.get("captured_text")
        if not isinstance(text, str) or content_hash(text) != ref.source_hash:
            raise ValueError("captured source body/hash mismatch")
    elif "captured_text" in data:
        raise ValueError("drawer witnesses must not copy original bodies")
    return data


def _verified_candidates(ref: EvidenceReference, sources: SourceIndex, collection):
    yield from _verified_key(source_key(ref), sources, collection)


def _verified_key(key: str, sources: SourceIndex, collection):
    locators = sources.locators.get(key)
    if not locators:
        from dream_procedural_validate import EvidenceUnavailable
        raise EvidenceUnavailable(f"captured source unavailable: {key}", code="uncaptured")
    prior = None
    for locator in locators:
        rows, size = [], 0
        for member in locator.member_ids:
            row = _read_row(collection, member)
            size += len(row["text"].encode("utf-8"))
            if size > MAX_RECORD_BYTES:
                raise ValueError("encoded procedural source record exceeds 4 MiB")
            rows.append(row)
        assembled = assemble_exact_chunks(rows)
        if len(assembled) != 1 or assembled[0]["id"] != locator.drawer_id:
            raise ValueError("source chunk identity changed")
        _check_chunk_identity(locator.drawer_id, rows)
        drawer = assembled[0]
        if drawer["wing"] != sources.wing or drawer["room"] != ROOM:
            raise ValueError("source scope changed")
        data = _decode_record(drawer, key, locator)
        substantive = {k: v for k, v in data.items() if k not in {"captured_at", "captured_by"}}
        digest = content_hash(canonical_json(substantive))
        if prior is not None and digest != prior:
            raise ValueError("conflicting captured source provenance/body")
        prior = digest
        yield data, locator


def _drawer_evidence(ref: EvidenceReference, palace: str, repository: str, observed: datetime) -> None:
    drawer = dream_palace.load_source_drawer(palace, ref.source_id)
    if drawer is None:
        from dream_procedural_validate import EvidenceUnavailable
        raise EvidenceUnavailable(f"original drawer unavailable: {ref.source_id}",
                                  code="original_drawer_missing")
    if is_generated_observation(drawer):
        raise ValueError("generated drawer is not original evidence")
    text = drawer["text"]
    _check_text(ref, text)
    session, ambiguous = dream_palace._session_id_state(text)
    if ambiguous or session != ref.session_id:
        raise ValueError("original drawer session mismatch")
    meta = decode_dream_metadata(drawer)
    if meta.get("session_id") is not None and meta["session_id"] != ref.session_id:
        raise ValueError("conflicting original drawer session metadata")
    if meta.get("repository") is not None and repository_key(meta["repository"]) != repository:
        raise ValueError("original drawer repository mismatch")
    stamps = re.findall(r"^OBSERVED_AT:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    if meta.get("observed_at") is not None:
        stamps.append(meta["observed_at"])
    if {utc_datetime(stamp) for stamp in stamps} != {observed}:
        raise ValueError("original drawer observation timestamp mismatch")


def resolve_captured(reference: EvidenceReference, *, palace: str,
                     wing: str, sources: SourceIndex) -> ResolvedEvidence:
    """Resolve all same-key records, then the exact quote/live drawer witness."""
    from dream_procedural_validate import ResolvedEvidence, SourceInvalid

    if sources.palace != os.path.realpath(os.path.expanduser(palace)) or sources.wing != _wing(wing):
        raise ValueError("source index is bound to a different palace/wing")
    source_key(reference)
    if reference in sources._resolved:
        return sources._resolved[reference]
    collection = dream_palace.procedural_collection(palace)
    result = None
    for data, _ in _verified_candidates(reference, sources, collection):
        result = ResolvedEvidence(reference, reference.session_id, data["repository"],
                                  utc_datetime(data["observed_at"]))
        if reference.source_kind == "session_turn":
            _check_text(reference, data["captured_text"])
    if reference.source_kind == "drawer":
        try:
            _drawer_evidence(reference, palace, result.repository, result.observed_at)
        except ValueError as exc:
            raise SourceInvalid(str(exc), code="original_drawer_drift") from exc
    sources._resolved[reference] = result
    return result


def captured_source_text(reference: dict, *, sources: SourceIndex) -> str:
    """Resolve an exact or uniquely captured version for artifact preparation.

    Partial identities cannot use the key index. Verify every candidate body;
    neither an unverified header nor record ordering supplies version authority.
    """
    from dream_procedural_validate import EvidenceUnavailable
    collection = dream_palace.procedural_collection(sources.palace)
    wanted = {k: v for k, v in reference.items() if k != "quote"}
    matched = []
    for key in sources.locators:
        data = None
        for candidate, _ in _verified_key(key, sources, collection):
            data = candidate
        identity = data["identity"]
        if all(identity.get(k) == v for k, v in wanted.items()):
            matched.append(identity)
    if len(matched) > 1:
        raise SourceAmbiguity("ambiguous captured source versions; provide explicit source_hash")
    if not matched:
        raise EvidenceUnavailable("captured source unavailable for preparation", code="uncaptured")
    ref = EvidenceReference(**matched[0], quote=reference["quote"])
    resolve_captured(ref, palace=sources.palace, wing=sources.wing, sources=sources)
    if ref.source_kind == "drawer":
        return dream_palace.load_source_drawer(sources.palace, ref.source_id)["text"]
    # Re-read only this version rather than retaining full fields in the index.
    return next(_verified_candidates(ref, sources, collection))[0]["captured_text"]


def verify_source_records(sources: SourceIndex) -> None:
    """Verify all stored bodies without acquiring originals or retaining text."""
    collection = dream_palace.procedural_collection(sources.palace)
    for key in sources.locators:
        for _ in _verified_key(key, sources, collection):
            pass


def captured_drawer_reference(source: dict, *, quote: str, session_id: str,
                              sources: SourceIndex) -> EvidenceReference | None:
    """Reuse a verified exact physical/logical ID when search finds its drawer.

    Search may return a different chunk of an already admitted original. Reuse
    that witness's identity; never manufacture a logical-key capture from it.
    """
    identities = {source["id"], *source.get("member_ids", [])}
    digest = content_hash(source["text"])
    collection = dream_palace.procedural_collection(sources.palace)
    matches = []
    for key in sources.locators:
        for data, _ in _verified_key(key, sources, collection):
            identity = data["identity"]
            if identity["source_kind"] == "drawer" and identity["source_id"] in identities \
                    and identity["session_id"] == session_id and identity["source_hash"] == digest:
                matches.append(EvidenceReference(**identity, quote=quote))
    if not matches:
        return None
    selected = min(matches, key=lambda ref: ref.source_id)
    resolve_captured(selected, palace=sources.palace, wing=sources.wing, sources=sources)
    return selected


def capture_source(source: OriginalSource, *, palace: str, wing: str,
                   captured_at: datetime, captured_by: str,
                   writer: dream_palace.MempalaceWriter) -> CaptureResult:
    """Storage-only capture of admitted provenance, with locked exact readback.

    This is not public admission and does not derive authority from raw input.
    An orphan record remains protected, but only committed events protect its
    original drawer. There is no source GC or cross-drawer transaction.
    """
    path, wing = os.path.realpath(os.path.expanduser(palace)), _wing(wing)
    if os.path.realpath(writer.palace_path) != path:
        raise ValueError("writer is bound to a different palace")
    body, metadata = source_record_data(source, captured_at=captured_at, captured_by=captured_by)
    key = source_key(source.reference)
    expected = _substantive(source)
    with dream_palace.palace_mutation_lock(path):
        index = read_source_records(path, wing)
        if key in index.locators:
            ids = []
            for data, locator in _verified_candidates(
                    source.reference, index, dream_palace.procedural_collection(path)):
                if {k: v for k, v in data.items() if k not in {"captured_at", "captured_by"}} != expected:
                    raise ValueError("conflicting captured source provenance/body")
                ids.extend(locator.member_ids)
            return CaptureResult("already_exists", key, tuple(ids), index.warnings)
        response = writer.add_drawer(wing, ROOM, body, added_by=AUTHOR, metadata=metadata)
        if not isinstance(response, dict) or response.get("success") is False:
            raise RuntimeError(f"source capture failed: {response}")
        after = read_source_records(path, wing)
        if key not in after.locators or any(
                not set(locators) <= set(after.locators.get(prior_key, ()))
                for prior_key, locators in index.locators.items()):
            raise RuntimeError("source capture readback failed")
        ids, exact = [], False
        for data, locator in _verified_candidates(
                source.reference, after, dream_palace.procedural_collection(path)):
            if {k: v for k, v in data.items() if k not in {"captured_at", "captured_by"}} != expected:
                raise RuntimeError("source capture readback conflict")
            ids.extend(locator.member_ids)
            exact |= locator.digest == metadata["source_digest"]
        if not exact:
            raise RuntimeError("source capture exact record readback failed")
        return CaptureResult("appended", key, tuple(ids), after.warnings)
