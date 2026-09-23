"""Drawer-backed procedural events; no durable state outside sanctioned drawers.

All package mutations share the canonical directory lock. External deletions
remain possible; without an external ledger, loss of an unreferenced leaf
cannot always be detected. There are no cross-drawer transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import escape
import os
import re
import math
from pathlib import Path
from contextlib import contextmanager
import time
from typing import Callable, Iterable, Literal

import dream_palace
from dream_metadata import (canonical_json, decode_procedural_chunks, is_generated_observation,
                            is_procedural_record)
from dream_procedural import (ProceduralEvent, Projection, Policy, ProposalPayload, ReviewPayload,
                              OutcomePayload, event_evidence, event_to_data, parse_event, project_rules,
                              repository_key, to_data, utc_datetime)

RECORD_HEADER = (
    "Procedural memory record; not an active instruction. Resolve current status with guidance/explain."
)


@contextmanager
def nonmutating_read(palace: str):
    """Shared (not write) directory lock and a clean SQLite snapshot boundary.

    SQLite mode=ro may change WAL shared-memory bytes. Refuse those states;
    never checkpoint, copy a partial database or ignore uncommitted WAL data.
    """
    import fcntl
    path = os.path.realpath(os.path.expanduser(palace))
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    deadline = time.monotonic() + 5
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("procedural read lock timeout") from None
                time.sleep(.01)
        def check():
            if any(Path(path, "sqlite_exact.sqlite3" + suffix).exists() for suffix in ("-wal", "-shm")):
                raise RuntimeError("strict read requires a clean SQLite palace without WAL/SHM sidecars; "
                                   "close/checkpoint the owning writer separately, never via guidance")
        check()
        yield
        check()
    finally:
        os.close(fd)


def embed_texts(collection, texts: list[str]) -> list[list[float]]:
    """Existing palace embedding space, offline only; reject unusable vectors."""
    # Set before lazy embedder resolution. No fallback model or installation.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    dimension = None
    try:
        from mempalace.backends.embedding_wrapper import EmbeddingCollection
        if isinstance(collection, EmbeddingCollection):
            identity = collection.get_stored_embedder_identity()
            if identity is None or identity.model_name != "minilm":
                raise RuntimeError("palace embedding identity is unknown or unsupported")
            dimension = identity.dimension or None
            embedder = local_embedder()
            # MiniLM's __call__ includes a download/extract path even with HF
            # offline set. Use its existing forward pass, never that bootstrap.
            vectors = embedder._forward(texts)
        else:
            vectors = dream_palace._resolve_embed_fn(collection)(texts)
    except Exception as exc:
        raise RuntimeError(f"installed palace embedder unavailable: {exc}") from exc
    vectors = checked_vectors(vectors, len(texts))
    if dimension is not None and len(vectors[0]) != dimension:
        raise RuntimeError("palace embedding identity dimension mismatch")
    return vectors


def local_embedder():
    """Require the verified installed local MiniLM cache; no remote fallback."""
    from mempalace.embedding import current_model_name, get_embedding_function
    if current_model_name() != "minilm":
        raise RuntimeError("procedural offline embedding supports installed minilm only")
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    root = Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME
    required = ("config.json", "model.onnx", "special_tokens_map.json", "tokenizer_config.json",
                "tokenizer.json", "vocab.txt")
    if not all((root / name).is_file() for name in required):
        raise RuntimeError("installed minilm cache incomplete; procedural commands never download models")
    return get_embedding_function()


def checked_vectors(vectors, count):
    try:
        vectors = [list(map(float, v)) for v in vectors]
    except (ValueError, TypeError, OverflowError) as exc:
        raise RuntimeError("invalid embedding values") from exc
    if len(vectors) != count or not vectors or not vectors[0] or any(
            len(v) != len(vectors[0]) or not all(math.isfinite(x) for x in v)
            or not math.isfinite(math.hypot(*v)) or math.hypot(*v) == 0 for v in vectors):
        raise RuntimeError("invalid embedding dimensions, values or norm")
    return vectors


@dataclass(frozen=True)
class GuidanceLimits:
    max_items: int = 5
    max_chars: int = 6000
    min_similarity: float = .25

    def __post_init__(self):
        if type(self.max_items) is not int or not 1 <= self.max_items <= 5:
            raise ValueError("guidance max_items must be between one and five")
        if type(self.max_chars) is not int or not 512 <= self.max_chars <= 6000:
            raise ValueError("guidance max_chars must be between 512 and 6000")
        if not math.isfinite(self.min_similarity) or not .25 <= self.min_similarity <= 1:
            raise ValueError("guidance similarity threshold must be between .25 and one")


@dataclass(frozen=True)
class GuidanceResult:
    data: dict
    serialized: str


def revalidate_sources(projection: Projection, *, evidence_reader, as_of: datetime,
                       repository: str | None = None):
    """Re-read immutable lineage without writers, reconciliation or write locks."""
    states, diagnostics = [], {}
    for state in projection.rules:
        if repository is not None and state.definition and state.definition.scope.key != repository:
            states.append(state)
            continue
        failures = []
        for event in state.events:
            try:
                resolved = [evidence_reader.resolve(ref) for ref in event_evidence(event)]
                boundary = (event.payload.validation_packet.validated_at
                            if isinstance(event.payload, ReviewPayload) else event.recorded_at)
                if any(r.observed_at > min(as_of, boundary) for r in resolved):
                    raise ValueError("future source observation")
                if isinstance(event.payload, ProposalPayload):
                    for source_id in event.payload.origin_drawer_ids:
                        evidence_reader.origin(source_id)
                    if len({r.session_id for r in resolved}) < 3:
                        raise ValueError("fewer than three original supporting sessions")
                    if any(r.repository != event.payload.definition.scope.key for r in resolved):
                        raise ValueError("proposal source repository mismatch")
                elif isinstance(event.payload, OutcomePayload):
                    if any(r.observed_at != event.payload.observed_at or
                           r.repository != event.payload.repository or
                           r.session_id != event.payload.source_session_id for r in resolved):
                        raise ValueError("outcome source time/session/repository mismatch")
                elif isinstance(event.payload, ReviewPayload):
                    packet = event.payload.validation_packet
                    dispositions = {d.evidence_id: d for d in event.payload.dispositions}
                    if state.definition is None or len(packet.queries) != 2 \
                            or packet.queries[0] != state.definition.statement \
                            or packet.queries[0] == packet.queries[1] or len(packet.evidence) > 20:
                        raise ValueError("invalid support/contrast packet")
                    support = []
                    for ref in packet.evidence:
                        d = dispositions.get(ref.source_id)
                        if d is None or d.disposition not in {"supports", "contradicts", "not_applicable"} \
                                or ref not in d.evidence:
                            raise ValueError("missing/invalid grounded packet disposition")
                        if d.disposition == "supports":
                            support.append(evidence_reader.resolve(ref))
                    if event.payload.verdict == "approve" and (
                            len({r.session_id for r in support}) < 3 or
                            any(r.repository != state.definition.scope.key for r in support)):
                        raise ValueError("approval lacks three independent in-scope supporting sessions")
            except (ValueError, RuntimeError, OSError) as exc:
                failures.append({"event_id": event.event_id, "error": str(exc)})
        if failures:
            state = replace(state, eligible=False,
                suppression_reasons=tuple(sorted(set(state.suppression_reasons) | {"evidence_unavailable"})))
            diagnostics[state.rule_id] = failures
        states.append(state)
    return replace(projection, rules=tuple(states)), diagnostics


def get_task_guidance(projection: Projection, *, task: str, repository: str, embedder,
                      limits: GuidanceLimits, as_of: datetime,
                      include_candidates: bool = False) -> GuidanceResult:
    """Rank a source-revalidated projection; count and budget the actual JSON."""
    from dream_procedural_validate import EvidenceUnavailable
    repository = repository_key(repository)
    as_of = utc_datetime(as_of)
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be nonblank text")
    invalid = {"missing_definition", "definition_conflict", "missing_review_parent", "review_cycle",
               "review_time_order", "replacement_cycle", "missing_replacement", "missing_declared_evidence",
               "scope_mismatch", "event_id_conflict", "future_event"}
    if projection.errors or any(invalid & set(s.suppression_reasons) for s in projection.rules):
        raise RuntimeError("invalid/incomplete projection; refusing partial guidance")
    scoped = [s for s in projection.rules if s.definition and s.definition.scope.key == repository]
    if any("evidence_unavailable" in s.suppression_reasons for s in scoped):
        raise EvidenceUnavailable("scoped rule source integrity/availability failed; inspect explain")
    eligible = [s for s in scoped if s.eligible and
                (s.maturity in {"established", "proven"} or include_candidates)]
    ranked = []
    if eligible:
        texts = [task, *[s.definition.statement for s in eligible]]
        try:
            vectors = checked_vectors(embedder(texts), len(texts))
        except Exception as exc:
            raise RuntimeError(f"guidance embedding unavailable: {exc}") from exc
        query = vectors[0]
        qnorm = math.hypot(*query)
        for state, vector in zip(eligible, vectors[1:]):
            norm = math.hypot(*vector)
            relevance = math.fsum((a / qnorm) * (b / norm) for a, b in zip(query, vector))
            if relevance >= limits.min_similarity:
                ranked.append((state, relevance))
    ranked.sort(key=lambda item: (-item[1], -item[0].score.effective_score, item[0].rule_id))
    selected = ranked[:limits.max_items]

    def bundle(items):
        data = {"policy_version": Policy().version, "as_of": to_data(as_of), "repository": repository,
                "status": "ok" if items else ("no_rules" if not scoped else "no_eligible_rules"),
                "item_count": len(items), "omitted_count": len(scoped) - len(items),
                "rules": [], "anti_patterns": [], "trials": []}
        for state, relevance in items:
            definition = state.definition
            trial = state.maturity == "candidate"
            section = "trials" if trial else ("rules" if definition.rule_type == "rule" else "anti_patterns")
            refs = sorted({(r.source_kind, r.source_id) for e in state.events for r in event_evidence(e)})
            data[section].append({
                "rule_id": state.rule_id, "rule_type": definition.rule_type,
                "statement": definition.statement, "applies_when": definition.applies_when,
                "exceptions": list(definition.exceptions), "maturity": state.maturity,
                "effective_score": state.score.effective_score, "relevance": relevance,
                "latest_validation": to_data(state.latest_validation),
                "evidence": [{"source_kind": kind, "source_id": source} for kind, source in refs[:3]],
                "delivery": "approved_candidate_trial" if trial else "guidance",
            })
        return data

    while True:
        data = bundle(selected)
        serialized = canonical_json(data) + "\n"
        if len(serialized) <= limits.max_chars:
            return GuidanceResult(data, serialized)
        if not selected:
            raise ValueError("requested budget cannot contain the guidance envelope")
        selected.pop()


def explain_rule(projection: Projection, rule_id: str, *, as_of: datetime, source_diagnostics=None) -> dict:
    state = next((s for s in projection.rules if s.rule_id == rule_id), None)
    if state is None:
        raise ValueError("unknown rule ID")
    return {
        "status": "evidence_unavailable" if "evidence_unavailable" in state.suppression_reasons else "ok",
        "policy": to_data(Policy()), "as_of": to_data(utc_datetime(as_of)),
        "rule": to_data(state), "duplicate_count": projection.duplicate_count,
        "projection_errors": to_data(projection.errors),
        "source_diagnostics": (source_diagnostics or {}).get(rule_id, []),
        "scoring": "H=sum(helpful weights); B=sum(harmful weights); score=H-4*B; weight=2**(-age_days/90)",
        "authority": "Observed usefulness and reviewed causal attribution, not logical proof.",
    }


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


def record_data(event: ProceduralEvent) -> tuple[str, dict]:
    """One size gate for dry-run and append, including worst-case trailer."""
    body = _record_body(event)
    metadata = {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(event)}
    if len(f"{body}\n\n<!--dreaming-meta: {canonical_json(metadata)}-->".encode("utf-8")) > 32 * 1024:
        raise ValueError("encoded procedural drawer exceeds 32 KiB")
    return body, metadata


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
    """Legacy destructive-path safety lookup, not a strict read-only command."""
    col = collection if collection is not None else dream_palace.protection_collection(palace)
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
                 preflight: Callable[[ProceduralEvent, Projection], None] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> AppendResult:
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
        as_of = clock()
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
        if preflight is None:
            verify_event_sources(palace, event)
        prospective = project_rules([*events, event], as_of=as_of, policy=Policy())
        invalid = {"missing_review_parent", "review_cycle", "review_time_order", "replacement_cycle",
                   "missing_replacement", "definition_conflict", "missing_declared_evidence"}
        if prospective.errors or any(invalid.intersection(r.suppression_reasons) for r in prospective.rules):
            raise ValueError("event would create invalid procedural history")
        if preflight is not None:
            preflight(event, projection)
        body, metadata = record_data(event)
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
