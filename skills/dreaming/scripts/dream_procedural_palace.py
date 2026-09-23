"""Drawer-backed procedural events; no durable state outside sanctioned drawers.

All package mutations share the canonical directory lock. External deletions
remain possible; without an external ledger, loss of an unreferenced leaf
cannot always be detected. There are no cross-drawer transactions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import os
import re
from typing import Callable, Iterable, Literal

import dream_palace
from dream_metadata import (canonical_json, decode_procedural_chunks, is_generated_observation,
                            is_procedural_record)
from dream_procedural import (ProceduralEvent, Projection, Policy, ProposalPayload, ReviewPayload,
                              OutcomePayload, event_evidence, event_to_data, parse_event, project_rules)

RECORD_HEADER = (
    "Procedural memory record; not an active instruction. Resolve current status with guidance/explain."
)


@dataclass(frozen=True)
class AppendResult:
    status: Literal["appended", "already_exists"]
    event_id: str
    drawer_ids: tuple[str, ...]
    projection: Projection


def _record_body(event: ProceduralEvent) -> str:
    payload = event.payload
    if isinstance(payload, ProposalPayload):
        detail = f"Repository: {payload.definition.scope.key}\n{payload.definition.statement}"
    else:
        detail = payload.reason if isinstance(payload, ReviewPayload) else payload.attribution
    # Include immutable event identity even with native metadata: upstream IDs
    # may depend only on content, not on native metadata.
    return (f"{RECORD_HEADER}\nRule: {event.rule_id}\nEvent: {event.event_kind}"
            f"\nEvent ID: {event.event_id}\nDigest: {event.digest}\n{escape(detail, quote=False)}")


def _wing(wing: str) -> str:
    if not isinstance(wing, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", wing):
        raise ValueError("an explicit canonical project wing is required")
    return wing


def _event_drawers(palace: str, wing: str | None, *, max_events: int = 5000,
                   collection=None) -> list[dict]:
    if type(max_events) is not int or not 1 <= max_events <= 5000:
        raise ValueError("max_events must be between 1 and 5000")
    col = collection if collection is not None else dream_palace.procedural_collection(palace)
    where = dream_palace._where(wing, "procedural")
    if wing is None:
        where = {"$or": [{"room": "procedural"}, {"kind": "procedural_event"},
                         {"added_by": "dream-procedure"}]}
    rows = []
    logical_sizes: dict[tuple[str, str], int] = {}
    by_wing: dict[str, set[str]] = {}
    offset = 0
    seen = set()
    while True:
        batch = dream_palace._rows_from_collection_result(col.get(
            where=where, include=["documents", "metadatas"], limit=256, offset=offset))
        if not batch:
            break
        for row in batch:
            if row["id"] in seen:
                raise ValueError("duplicate physical row during procedural paging")
            seen.add(row["id"])
            meta = row.get("metadata") or {}
            if meta.get("room") != "procedural" or (wing is not None and meta.get("wing") != wing):
                raise ValueError("storage returned records outside requested scope")
            parent = meta.get("parent_drawer_id") or row["id"]
            locus = (meta.get("wing"), parent)
            parents = by_wing.setdefault(locus[0], set())
            parents.add(parent)
            if len(parents) > max_events:
                raise ValueError("procedural event limit exceeded; refusing partial history")
            logical_sizes[locus] = logical_sizes.get(locus, 0) + len(row["text"].encode("utf-8"))
            if logical_sizes[locus] > 32 * 1024:
                raise ValueError("encoded procedural drawer exceeds 32 KiB")
        rows.extend(batch)
        offset += len(batch)
    decoded = decode_procedural_chunks(rows)
    ids = {}
    rule_ids: dict[str, set[str]] = {}
    for drawer in decoded:
        event = parse_event(drawer["metadata"]["event"])
        body = _record_body(event)
        metadata = {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(event)}
        trailer_form = f"{body}\n\n<!--dreaming-meta: {canonical_json(metadata)}-->"
        if drawer["text"] not in (body, trailer_form):
            raise ValueError("procedural body/chunk reconstruction mismatch")
        if event.event_id in ids and ids[event.event_id] != event.digest:
            raise ValueError("conflicting procedural event ID")
        ids[event.event_id] = event.digest
        rules = rule_ids.setdefault(drawer["wing"], set())
        rules.add(event.rule_id)
        if len(rules) > 100:
            raise ValueError("procedural rule limit exceeded")
    return decoded


def read_events(palace: str, wing: str, *, max_events: int = 5000) -> list[ProceduralEvent]:
    """Complete, bounded, exact event read. Integrity errors never become []."""
    return [parse_event(d["metadata"]["event"]) for d in
            _event_drawers(palace, _wing(wing), max_events=max_events)]


def protected_drawer_ids(events: Iterable[ProceduralEvent]) -> set[str]:
    """All original/lineage drawer references, regardless of lifecycle status."""
    protected = set()
    for event in events:
        protected.update(ref.source_id for ref in event_evidence(event) if ref.source_kind == "drawer")
        if isinstance(event.payload, ProposalPayload):
            protected.update(event.payload.origin_drawer_ids)
    return protected


def live_protected_drawer_ids(palace: str, *, collection=None) -> set[str]:
    """Palace-wide live protection, including every logical and physical alias."""
    col = collection if collection is not None else dream_palace.procedural_collection(palace)
    drawers = _event_drawers(palace, None, collection=col)
    protected = protected_drawer_ids(parse_event(d["metadata"]["event"]) for d in drawers)
    for drawer in drawers:
        protected.update([drawer["id"], *drawer["member_ids"]])
    # Expand sources through the same read-only collection, including a physical
    # source ID whose siblings were not named in the event.
    for source_id in sorted(protected.copy()):
        exact = dream_palace._rows_from_collection_result(col.get(
            ids=[source_id], include=["metadatas"]))
        parent = ((exact[0]["metadata"] or {}).get("parent_drawer_id") if exact else None) or source_id
        protected.add(parent)
        protected.update(row["id"] for row in dream_palace._rows_from_collection_result(col.get(
            where={"parent_drawer_id": parent}, include=["metadatas"])))
    return protected


def exclude_protected_drawers(palace: str, drawers: list[dict]) -> list[dict]:
    protected = live_protected_drawer_ids(palace)
    return [d for d in drawers if not is_procedural_record(d)
            and not protected.intersection([d["id"], *d.get("member_ids", [])])]


def verify_event_sources(palace: str, event: ProceduralEvent) -> None:
    """Basic locked referential integrity; Task 4 adds enrollment/packet policy.

    Re-read actual source text, exact quotes, hashes and original session stamps.
    Generated lineage is allowed only in proposal.origin_drawer_ids.
    """
    from dream_metadata import content_hash
    import dream_sessions

    for ref in event_evidence(event):
        if ref.source_kind == "drawer":
            source = dream_palace.load_source_drawer(palace, ref.source_id)
            if source is None:
                raise ValueError(f"missing source drawer: {ref.source_id}")
            if is_generated_observation(source):
                raise ValueError(f"generated independent evidence: {ref.source_id}")
            text = source["text"]
            session_id, ambiguous = dream_palace._session_id_state(text)
            if ref.session_id is not None and (ambiguous or session_id != ref.session_id):
                raise ValueError(f"source session mismatch: {ref.source_id}")
        else:
            turns = dream_sessions.load_session_turns(ref.source_id)
            matches = [t for t in turns if t["turn_index"] == ref.turn_index]
            if len(matches) != 1:
                raise ValueError(f"missing source turn: {ref.source_id}/{ref.turn_index}")
            text = matches[0].get(ref.field)
            if not isinstance(text, str):
                raise ValueError("source turn field unavailable")
        if content_hash(text) != ref.source_hash or not ref.quote.strip() or ref.quote not in text:
            raise ValueError(f"source hash/quote drift: {ref.source_id}")
    if isinstance(event.payload, ProposalPayload):
        for source_id in event.payload.origin_drawer_ids:
            if dream_palace.load_source_drawer(palace, source_id) is None:
                raise ValueError(f"missing origin drawer: {source_id}")


def _scope_check(event: ProceduralEvent, existing: list[ProceduralEvent]) -> None:
    definitions = [e.payload.definition for e in existing if e.rule_id == event.rule_id
                   and isinstance(e.payload, ProposalPayload)]
    if isinstance(event.payload, ProposalPayload):
        definitions.append(event.payload.definition)
    if not definitions:
        raise ValueError("missing proposal definition")
    if any(d != definitions[0] for d in definitions):
        raise ValueError("conflicting rule definitions")
    repository = definitions[0].scope.key
    if isinstance(event.payload, OutcomePayload) and event.payload.repository != repository:
        raise ValueError("outcome repository scope mismatch")
    if isinstance(event.payload, ReviewPayload) and event.payload.validation_packet.repository != repository:
        raise ValueError("review repository scope mismatch")


def append_event(palace: str, wing: str, event: ProceduralEvent, *,
                 writer: dream_palace.MempalaceWriter,
                 preflight: Callable[[ProceduralEvent, Projection], None] | None = None) -> AppendResult:
    """Append through sanctioned handlers, then verify exact committed readback.

    The optional Task-4 preflight hook runs *inside* the mutation lock and only
    for new events. Callers must not perform freshness/head checks ahead of this
    retry gate. A failed readback is retryable using the same immutable artifact.
    """
    palace = os.path.realpath(os.path.expanduser(palace))
    wing = _wing(wing)
    event = parse_event(event_to_data(event))
    if os.path.realpath(writer.palace_path) != palace:
        raise ValueError("writer is bound to a different palace")
    with dream_palace.palace_mutation_lock(palace):
        drawers = _event_drawers(palace, wing)
        events = [parse_event(d["metadata"]["event"]) for d in drawers]
        _scope_check(event, events)
        as_of = datetime.now(timezone.utc)
        projection = project_rules(events, as_of=as_of, policy=Policy())
        prior = [d for d in drawers if d["metadata"]["event"]["event_id"] == event.event_id]
        if prior:
            if any(d["metadata"]["event"]["digest"] != event.digest for d in prior):
                raise ValueError("event ID already has a different digest")
            return AppendResult("already_exists", event.event_id,
                                tuple(pid for d in prior for pid in d["member_ids"]), projection)
        if event.recorded_at > as_of:
            raise ValueError("future recorded_at")
        if projection.errors:
            raise ValueError(f"invalid current projection: {projection.errors}")
        state = next((r for r in projection.rules if r.rule_id == event.rule_id), None)
        if isinstance(event.payload, ReviewPayload):
            expected = set(state.review_heads) if state else set()
            if set(event.payload.parent_review_ids) != expected:
                raise ValueError("review must reference every current review head")
            if (as_of - event.payload.validation_packet.validated_at).total_seconds() >= 90 * 86400:
                raise ValueError("stale validation packet")
            if state and event.payload.verdict == "approve" \
                    and {"retired", "replaced"}.intersection(state.suppression_reasons):
                raise ValueError("terminal rule identity cannot be approved again")
        verify_event_sources(palace, event)
        prospective = project_rules([*events, event], as_of=as_of, policy=Policy())
        invalid = {"missing_review_parent", "review_cycle", "review_time_order", "replacement_cycle",
                   "missing_replacement", "definition_conflict", "missing_declared_evidence"}
        if prospective.errors or any(invalid.intersection(r.suppression_reasons) for r in prospective.rules):
            raise ValueError("event would create invalid procedural history")
        if preflight is not None:
            preflight(event, projection)
        body = _record_body(event)
        metadata = {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(event)}
        if len((body + canonical_json(metadata)).encode("utf-8")) + 32 > 32 * 1024:
            raise ValueError("encoded procedural drawer exceeds 32 KiB")
        result = writer.add_drawer(wing, "procedural", body, added_by="dream-procedure", metadata=metadata)
        if not isinstance(result, dict) or result.get("success") is False:
            raise RuntimeError(f"procedural append failed: {result}")
        readback = _event_drawers(palace, wing)
        committed = [d for d in readback if d["metadata"]["event"] == event_to_data(event)]
        before = {(e.event_id, e.digest) for e in events}
        after = {(d["metadata"]["event"]["event_id"], d["metadata"]["event"]["digest"]) for d in readback}
        if not committed or not before <= after:
            raise RuntimeError("procedural append readback failed; retry the same event artifact")
        projection = project_rules([parse_event(d["metadata"]["event"]) for d in readback],
                                   as_of=as_of, policy=Policy())
        return AppendResult("appended", event.event_id,
                            tuple(pid for d in committed for pid in d["member_ids"]), projection)
