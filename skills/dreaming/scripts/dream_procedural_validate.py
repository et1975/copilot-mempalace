"""Grounding and review admission, not an oracle for causal or logical truth."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
import re
import sqlite3
import sys

import dream_palace
import dream_sessions
from dream_metadata import (canonical_json, content_hash, decode_dream_metadata,
                            is_generated_observation, is_source_record_metadata,
                            reject_generated_transport)
from dream_procedural import (
    EvidenceReference, OutcomePayload, Policy, ProposalPayload, ReviewPayload,
    ValidationPacket, adverse_evidence_ids, canonical_rule_id, event_evidence, project_rules,
    repository_key, to_data, utc_datetime,
)


class EvidenceUnavailable(RuntimeError):
    """Original evidence cannot be resolved; never treat this as an empty search."""

    def __init__(self, message, *, code="original_unavailable"):
        super().__init__(message)
        self.code = code


class SourceInvalid(ValueError):
    """Structured integrity failure, distinct from absent original input."""

    def __init__(self, message, *, code="corrupt_capture"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedEvidence:
    reference: EvidenceReference
    session_id: str
    repository: str
    observed_at: datetime


@dataclass(frozen=True)
class ValidationLimits:
    hits_per_query: int = 10

    def __post_init__(self):
        if type(self.hits_per_query) is not int or not 1 <= self.hits_per_query <= 10:
            raise ValueError("validation hits must be between one and ten")


@dataclass(frozen=True)
class PreflightResult:
    independent_sessions: int


def _session_repository(store: str, session: str) -> str:
    path = Path(store).expanduser().resolve()
    if not path.is_file():
        raise EvidenceUnavailable(f"session store unavailable: {path}", code="session_store_unavailable")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            rows = con.execute("SELECT repository FROM sessions WHERE id=?", (session,)).fetchall()
    except sqlite3.Error as exc:
        raise EvidenceUnavailable(f"session store unavailable: {exc}", code="session_store_unavailable") from exc
    if len(rows) != 1:
        raise EvidenceUnavailable(f"original session unavailable: {session}", code="original_session_missing")
    return repository_key(rows[0][0])


def acquire_original(ref: EvidenceReference, *, palace: str, session_store: str):
    """Check full original text, exact quote/hash, session, scope and source time.

    Drawers require a SESSION_ID stamp and original observed_at metadata or an
    OBSERVED_AT line. filed_at is never an observation clock. The original host
    session supplies repository authority for both diary and raw-turn evidence.
    """
    from dream_procedural_sources import OriginalSource, source_key
    if ref.session_id is None:
        raise SourceInvalid("independent evidence requires an original session_id", code="missing_session")
    source_key(ref)
    repository = _session_repository(session_store, ref.session_id)
    if ref.source_kind == "drawer":
        source = dream_palace.load_source_drawer(palace, ref.source_id)
        if source is None:
            raise EvidenceUnavailable(f"source drawer unavailable: {ref.source_id}",
                                      code="original_drawer_missing")
        if is_generated_observation(source):
            raise ValueError(f"generated independent evidence: {ref.source_id}")
        text = source["text"]
        session, ambiguous = dream_palace._session_id_state(text)
        if ambiguous or session != ref.session_id:
            raise ValueError(f"original session mismatch: {ref.source_id}")
        meta = decode_dream_metadata(source)
        if meta.get("session_id") is not None and meta["session_id"] != ref.session_id:
            raise ValueError("conflicting original drawer session metadata")
        if meta.get("repository") is not None and repository_key(meta["repository"]) != repository:
            raise ValueError("drawer/session repository mismatch")
        stamps = re.findall(r"^OBSERVED_AT:\s*(\S+)\s*$", text, flags=re.MULTILINE)
        if meta.get("observed_at") is not None:
            stamps.append(meta["observed_at"])
        times = {utc_datetime(value) for value in stamps}
        if len(times) != 1:
            raise ValueError("drawer requires one grounded original observation timestamp")
        observed = times.pop()
    else:
        try:
            turns = dream_sessions.load_session_turns(ref.source_id, session_store)
        except sqlite3.Error as exc:
            raise EvidenceUnavailable(f"session turns unavailable: {exc}") from exc
        matches = [t for t in turns if t["turn_index"] == ref.turn_index]
        if len(matches) != 1:
            raise EvidenceUnavailable(f"original turn unavailable: {ref.source_id}/{ref.turn_index}",
                                      code="original_turn_missing")
        text = matches[0].get(ref.field)
        observed = utc_datetime(matches[0]["timestamp"])
    if not isinstance(text, str):
        raise ValueError(f"original source must be a complete text field: {ref.source_id}")
    reject_generated_transport(text)
    if content_hash(text) != ref.source_hash \
            or not ref.quote.strip() or ref.quote not in text:
        raise ValueError(f"source hash/quote drift: {ref.source_id}")
    return OriginalSource(ref, repository, observed,
                          text if ref.source_kind == "session_turn" else None)


def resolve_evidence(ref: EvidenceReference, *, palace: str, session_store: str) -> ResolvedEvidence:
    """Explicit original-input verification; never used for published reads."""
    source = acquire_original(ref, palace=palace, session_store=session_store)
    return ResolvedEvidence(ref, ref.session_id, source.repository, source.observed_at)


class EvidenceReader:
    """Palace-only published evidence. One instance per nonmutating read scope."""

    def __init__(self, palace: str, wing: str):
        from dream_procedural_palace import _wing
        self.palace = palace
        self.wing = _wing(wing)
        self._sources = None

    @property
    def sources(self):
        from dream_procedural_sources import read_source_records
        if self._sources is None:
            try:
                self._sources = read_source_records(self.palace, self.wing)
            except ValueError as exc:
                raise SourceInvalid(str(exc)) from exc
        return self._sources

    def resolve(self, ref: EvidenceReference) -> ResolvedEvidence:
        from dream_procedural_sources import resolve_captured
        if ref.session_id is None:
            raise SourceInvalid("independent evidence requires an original session_id", code="missing_session")
        try:
            return resolve_captured(ref, palace=self.palace, wing=self.wing, sources=self.sources)
        except SourceInvalid:
            raise
        except ValueError as exc:
            raise SourceInvalid(str(exc)) from exc

    def origin(self, source_id: str) -> None:
        if dream_palace.load_source_drawer(self.palace, source_id) is None:
            raise EvidenceUnavailable(f"origin drawer unavailable: {source_id}")

    def source_text(self, ref: dict) -> str:
        from dream_procedural_sources import captured_source_text, SourceAmbiguity
        try:
            return captured_source_text(ref, sources=self.sources)
        except (SourceAmbiguity, SourceInvalid):
            raise
        except ValueError as exc:
            raise SourceInvalid(str(exc)) from exc

    def captured_turn_fields(self, session_id: str, *, max_records=64, max_bytes=8 * 1024 * 1024):
        """Bounded draft lookup; copies collapse to their verified original key.

        Index discovery uses the existing collection API's header scan. The
        bounds here apply to full canonical body verification after discovery.
        """
        from dream_procedural_sources import _verified_key
        index = self.sources
        collection = dream_palace.procedural_collection(self.palace)
        fields, count, size = [], 0, 0
        for key, locators in sorted(index.locators.items()):
            next_size = sum(loc.encoded_bytes for loc in locators)
            if count + len(locators) > max_records or size + next_size > max_bytes:
                return fields, True
            count += len(locators)
            size += next_size
            data = None
            try:
                for candidate, _ in _verified_key(key, index, collection):
                    data = candidate
            except ValueError as exc:
                raise SourceInvalid(str(exc)) from exc
            if data["identity"]["source_kind"] == "session_turn" \
                    and data["identity"]["session_id"] == session_id:
                fields.append(data)
        return fields, False


class AdmissionReader(EvidenceReader):
    """Explicit acquisition scope: prefer admitted captures, acquire missing keys.

    Original full fields are retained only until this admission ends. They are
    never a fallback of EvidenceReader or a source of caller metadata authority.
    """

    def __init__(self, palace: str, wing: str, session_store: str | None = None):
        super().__init__(palace, wing)
        self.session_store = session_store or dream_sessions.default_store_path()
        self.originals = {}
        self.verified_captured_keys = set()

    def resolve(self, ref: EvidenceReference) -> ResolvedEvidence:
        from dream_procedural_sources import source_key
        try:
            resolved = super().resolve(ref)
        except EvidenceUnavailable as exc:
            if exc.code != "uncaptured":
                raise
        else:
            self.verified_captured_keys.add(source_key(ref))
            return resolved
        source = acquire_original(ref, palace=self.palace, session_store=self.session_store)
        self.originals[source_key(ref)] = source
        return ResolvedEvidence(ref, ref.session_id, source.repository, source.observed_at)

    def preflight_captures(self, *, at: datetime, actor: str) -> None:
        from dream_procedural_sources import source_record_data
        for source in self.originals.values():
            source_record_data(source, captured_at=at, captured_by=actor)

    def persist(self, *, writer, at: datetime, actor: str) -> tuple[str, ...]:
        from dream_procedural_sources import capture_source
        warnings = set()
        for source in self.originals.values():
            result = capture_source(source, palace=self.palace, wing=self.wing,
                captured_at=at, captured_by=actor, writer=writer)
            warnings.update(result.warnings)
        # A prewrite index and its resolved cache are invalid across mutation.
        self._sources = None
        return tuple(sorted(warnings))

    def source_text(self, ref: dict) -> str:
        """Preparation never chooses the latest of ambiguous captured versions."""
        try:
            return super().source_text(ref)
        except EvidenceUnavailable as exc:
            if exc.code != "uncaptured":
                raise
        if ref.get("source_kind") == "drawer":
            source = dream_palace.load_source_drawer(self.palace, ref["source_id"])
            if source is not None:
                return source["text"]
        elif ref.get("source_kind") == "session_turn":
            _session_repository(self.session_store, ref["source_id"])
            turns = dream_sessions.load_session_turns(ref["source_id"], self.session_store)
            matches = [t for t in turns if t["turn_index"] == ref.get("turn_index")]
            if len(matches) == 1 and isinstance(matches[0].get(ref.get("field")), str):
                return matches[0][ref["field"]]
        raise EvidenceUnavailable(f"original source unavailable: {ref.get('source_id')}")

    def search(self, wing: str, query: str, limit: int, *, as_of: datetime):
        from dream_procedural_palace import embed_texts
        from dream_procedural_sources import captured_drawer_reference, _verified_key
        from dream_procedural_drafts import is_procedural_echo
        if wing != self.wing:
            raise ValueError("search wing differs from evidence reader")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("search limit must be between one and ten")
        col = dream_palace.procedural_collection(self.palace)
        vector = embed_texts(col, [query])[0]
        def hits(where):
            result = col.query(query_embeddings=[vector], n_results=limit, where=where,
                               include=["documents", "metadatas"])
            ids = result.get("ids")
            if not isinstance(ids, list) or len(ids) != 1 or len(ids[0]) > limit:
                raise RuntimeError("invalid bounded search response")
            metas = result.get("metadatas")
            if not isinstance(metas, list) or len(metas) != 1 or len(metas[0]) != len(ids[0]):
                raise RuntimeError("invalid bounded search metadata")
            return list(zip(ids[0], metas[0]))

        original_hits = hits({"$and": [{"wing": wing}, {"room": {"$ne": "procedural"}},
                                       {"room": {"$ne": "procedural-sources"}}]})
        captured_hits = (hits({"$and": [{"wing": wing}, {"room": "procedural-sources"}]})
                         if self.sources.locators else [])
        # Independent bounded channels prevent generated copies from consuming
        # the ordinary-source budget. Expose at most limit verified originals.
        combined = [hit for pair in zip_longest(original_hits, captured_hits)
                    for hit in pair if hit is not None]
        refs = []
        seen = set()
        for source_id, meta in combined:
            meta = meta or {}
            if meta.get("room") == "procedural" or meta.get("kind") == "procedural_event":
                continue
            if is_source_record_metadata(meta):
                # The hit/header is only a locator. Verify all copies, then
                # expose the original raw identity, never the generated record.
                try:
                    data = None
                    keys = [key for key, locators in self.sources.locators.items()
                            if any(source_id == loc.drawer_id or source_id in loc.member_ids
                                   for loc in locators)]
                    if len(keys) != 1:
                        raise ValueError("search capture locator missing or ambiguous")
                    for candidate, _ in _verified_key(keys[0], self.sources, col):
                        data = candidate
                except ValueError as exc:
                    raise SourceInvalid(str(exc)) from exc
                identity = data["identity"]
                if identity["source_kind"] != "session_turn":
                    continue
                text = data["captured_text"]
                if not text.strip() or is_procedural_echo(text):
                    continue
                ref = EvidenceReference(**identity, quote=text.strip()[:800])
                key = evidence_identity(ref)
                if key in seen:
                    continue
                seen.add(key)
                resolved = self.resolve(ref)
                if resolved.observed_at > as_of:
                    raise ValueError("future search evidence")
                refs.append(ref)
                continue
            source = dream_palace.load_source_drawer(self.palace, source_id)
            if source is None:
                raise EvidenceUnavailable(f"search source vanished: {source_id}")
            if is_generated_observation(source):
                continue
            if is_procedural_echo(source["text"]):
                continue
            source_id = source["id"]
            if source_id in seen:
                continue
            seen.add(source_id)
            session, ambiguous = dream_palace._session_id_state(source["text"])
            # Unattributed legacy text remains searchable normally, but is not
            # historical procedural validation evidence.
            if session is None or ambiguous:
                continue
            ref = EvidenceReference("drawer", source_id, session, source["text"][:800],
                                    content_hash(source["text"]))
            try:
                ref = captured_drawer_reference(source, quote=ref.quote, session_id=session,
                                               sources=self.sources) or ref
            except SourceInvalid:
                raise
            except ValueError as exc:
                raise SourceInvalid(str(exc)) from exc
            resolved = self.resolve(ref)
            if resolved.observed_at > as_of:
                raise ValueError("future search evidence")
            refs.append(ref)
        return refs[:limit]


def _review_packet_bytes(packet: ValidationPacket) -> int:
    data = to_data(packet)
    # Reserve reviewed reasoning, head IDs, and the drawer envelope as well as
    # the exact reference repeated in every disposition. This is a wire-byte
    # budget, not a character/token estimate.
    review = {
        "validation_packet": data,
        "validation_digest": "0" * 64,
        "parent_review_ids": ["0" * 36] * 4,
        "acknowledged_evidence_ids": [ref["source_id"] for ref in data["evidence"]],
        "dispositions": [
            {"evidence_id": ref["source_id"], "disposition": "not_applicable",
             "reason": "x" * 256, "evidence": [ref]} for ref in data["evidence"]
        ],
        "reason": "x" * 1024,
    }
    return len(canonical_json(review).encode("utf-8")) + 2048


def evidence_identity(ref: EvidenceReference) -> tuple:
    """As-written original field/version; quotations and capture copies aren't identities."""
    return (ref.source_kind, ref.source_id, ref.session_id, ref.turn_index, ref.field, ref.source_hash)


def build_validation_packet(rule, *, queries, source_reader, limits: ValidationLimits,
                            as_of: datetime) -> ValidationPacket:
    queries = tuple(queries)
    if len(queries) != 2 or queries[0] != rule.statement or \
            any(not isinstance(q, str) or not q.strip() for q in queries) or queries[0] == queries[1]:
        raise ValueError("validation requires the statement and one explicit distinct contrast query")
    results = []
    for query in queries:
        hits = list(source_reader(query, limits.hits_per_query))
        if len(hits) > limits.hits_per_query:
            raise ValueError("source reader exceeded validation search bound")
        results.append(hits)
    refs = {}
    for pair in zip_longest(*results):
        for ref in pair:
            if ref is None:
                continue
            key = evidence_identity(ref)
            if key not in refs:
                refs[key] = ref
    if not refs:
        raise EvidenceUnavailable("no original evidence found within the bounded search")
    packet = ValidationPacket(canonical_rule_id(rule), rule.scope.key, utc_datetime(as_of),
                              queries, ())
    shortened = 0
    for ref in refs.values():
        bounded = replace(ref, quote=ref.quote[:256])
        if not bounded.quote.strip():
            continue
        candidate = replace(packet, evidence=(*packet.evidence, bounded))
        if _review_packet_bytes(candidate) <= 24 * 1024:
            packet = candidate
            shortened += bounded.quote != ref.quote
    omitted = len(refs) - len(packet.evidence)
    if not packet.evidence:
        raise EvidenceUnavailable("validation evidence cannot fit the review encoding budget")
    if shortened or omitted:
        print(f"validation budget: shortened {shortened} quotes; omitted {omitted} "
              f"of {len(refs)} distinct sources", file=sys.stderr)
    return packet


def preflight_event(event, *, projection, evidence_reader: EvidenceReader,
                    as_of: datetime) -> PreflightResult:
    """Shared dry-run/locked-append gate. Invoke only after the exact retry gate."""
    from dream_procedural_palace import record_data
    record_data(event)
    as_of = utc_datetime(as_of)
    if projection.errors:
        raise ValueError("invalid current projection")
    if event.recorded_at > as_of:
        raise ValueError("future recorded_at")
    state = next((s for s in projection.rules if s.rule_id == event.rule_id), None)
    payload = event.payload
    definition = payload.definition if isinstance(payload, ProposalPayload) else (
        state.definition if state else None)
    if definition is None:
        raise ValueError("missing proposal definition")
    repository = definition.scope.key

    def grounded(refs, at):
        resolved = [evidence_reader.resolve(ref) for ref in refs]
        if any(r.observed_at > at for r in resolved):
            raise ValueError("evidence timestamp is later than the claimed observation")
        return resolved

    def support(refs, at):
        resolved = grounded(refs, at)
        if any(r.repository != repository for r in resolved):
            raise ValueError("evidence repository mismatch")
        sessions = {r.session_id for r in resolved}
        if len(sessions) < 3:
            raise ValueError("enrollment/approval requires three independent original source sessions")
        return len(sessions)

    count = 0
    if isinstance(payload, ProposalPayload):
        count = support(payload.evidence, event.recorded_at)
        for source in payload.origin_drawer_ids:
            evidence_reader.origin(source)
    elif isinstance(payload, ReviewPayload):
        if set(payload.parent_review_ids) != set(state.review_heads):
            raise ValueError("review must reference every current head")
        if payload.verdict == "approve" and {"retired", "replaced"} & set(state.suppression_reasons):
            raise ValueError("terminal rule identity cannot be approved")
        packet = payload.validation_packet
        if packet.repository != repository:
            raise ValueError("review repository mismatch")
        if (as_of - packet.validated_at).total_seconds() >= Policy().stale_days * 86400:
            raise ValueError("stale validation packet")
        if len(packet.queries) != 2 or packet.queries[0] != definition.statement \
                or packet.queries[0] == packet.queries[1] or len(packet.evidence) > 20:
            raise ValueError("invalid bounded support/contrast packet")
        for prior in state.events:
            if isinstance(prior.payload, ProposalPayload):
                support(prior.payload.evidence, packet.validated_at)
                for source in prior.payload.origin_drawer_ids:
                    evidence_reader.origin(source)
        grounded(packet.evidence, packet.validated_at)
        by_id = {d.evidence_id: d for d in payload.dispositions}
        source_ids = {r.source_id for r in packet.evidence}
        if not source_ids <= by_id.keys():
            raise ValueError("every packet source requires an explicit evidence disposition")
        for ref in packet.evidence:
            d = by_id[ref.source_id]
            if d.disposition not in {"supports", "contradicts", "not_applicable"} or ref not in d.evidence:
                raise ValueError("packet disposition must retain its exact grounding reference")
        for d in payload.dispositions:
            grounded(d.evidence, packet.validated_at)
        supports = [r for r in packet.evidence if by_id[r.source_id].disposition == "supports"]
        if payload.verdict == "approve":
            count = support(supports, packet.validated_at)
            adverse = adverse_evidence_ids((*state.events, event))
            if not adverse <= set(payload.acknowledged_evidence_ids) or not adverse <= by_id.keys():
                raise ValueError("approval requires explicit adverse evidence acknowledgment/disposition")
            for prior in state.events:
                if prior.event_id in adverse or (isinstance(prior.payload, ReviewPayload) and
                        any(d.evidence_id in adverse for d in prior.payload.dispositions)):
                    grounded(event_evidence(prior), packet.validated_at)
    else:
        if payload.repository != repository:
            raise ValueError("outcome repository mismatch")
        resolved = grounded(payload.evidence, event.recorded_at)
        if any(r.repository != repository or r.session_id != payload.source_session_id for r in resolved):
            raise ValueError("outcome original session/repository mismatch")
        if any(r.observed_at != payload.observed_at for r in resolved):
            raise ValueError("outcome timestamp must equal the original observation timestamp")
        count = 1
    events = [e for s in projection.rules for e in s.events]
    prospective = project_rules([*events, event], as_of=as_of, policy=Policy())
    invalid = {"missing_review_parent", "review_cycle", "review_time_order", "replacement_cycle",
               "missing_replacement", "definition_conflict", "missing_declared_evidence", "scope_mismatch"}
    if prospective.errors or any(invalid & set(s.suppression_reasons) for s in prospective.rules):
        raise ValueError("event would create invalid procedural history")
    return PreflightResult(count)


def _source_report(events, reader):
    """Validate retained evidence and proposal lineage, including retired history.

    Reference/source counts describe evidence references/capture keys only.
    Origin count is distinct drawer IDs, not additional evidence or captures;
    failed counts unresolved evidence references plus unresolved origin IDs.
    """
    from dream_procedural_sources import source_key
    references = tuple(dict.fromkeys(ref for event in events for ref in event_evidence(event)))
    origins = tuple(dict.fromkeys(source for event in events
                                 if isinstance(event.payload, ProposalPayload)
                                 for source in event.payload.origin_drawer_ids))
    keys, failures = set(), []
    for ref in references:
        try:
            keys.add(source_key(ref))
            reader.resolve(ref)
        except (ValueError, RuntimeError, OSError) as exc:
            failures.append({"source_kind": ref.source_kind, "source_id": ref.source_id,
                "source_hash": ref.source_hash, "session_id": ref.session_id,
                "code": "missing_session" if ref.session_id is None else getattr(exc, "code",
                    "original_drawer_drift" if ref.source_kind == "drawer" else "invalid_original"),
                "error": str(exc)})
    for source in origins:
        try:
            reader.origin(source)
        except (ValueError, RuntimeError, OSError) as exc:
            failures.append({"reference_kind": "origin", "source_kind": "drawer",
                "source_id": source, "code": "origin_drawer_missing"
                    if isinstance(exc, EvidenceUnavailable) else "origin_drawer_invalid",
                "error": str(exc)})
    index = reader.sources
    return {"event_count": len(events), "reference_count": len(references),
        "source_count": len(keys), "origin_count": len(origins), "record_count": index.record_count,
        "encoded_bytes": index.encoded_bytes, "warnings": list(index.warnings),
        "failures": failures, "failed": len(failures)}


def _capacity(report, pending, pending_bytes):
    from dream_procedural_sources import MAX_RECORD_BYTES, RECORD_WARNING_THRESHOLD, BYTES_WARNING_THRESHOLD
    count = report["record_count"] + pending
    size = report["encoded_bytes"] + pending_bytes
    count_warning = (RECORD_WARNING_THRESHOLD * 4 + 4) // 5
    bytes_warning = (BYTES_WARNING_THRESHOLD * 4 + 4) // 5
    if count >= count_warning:
        report["warnings"].append(f"procedural sources: projected {count} records; warning threshold "
                                  f"{RECORD_WARNING_THRESHOLD}")
    if size >= bytes_warning:
        report["warnings"].append(f"procedural sources: projected {size} bytes; warning threshold "
                                  f"{BYTES_WARNING_THRESHOLD}")
    report["capacity"] = {"max_record_bytes": MAX_RECORD_BYTES,
        "record_warning_at": count_warning, "byte_warning_at": bytes_warning,
        "projected_record_count": count, "projected_encoded_bytes": size,
        "records_until_warning": max(0, count_warning - count),
        "bytes_until_warning": max(0, bytes_warning - size)}


def inspect_published_sources(palace: str, wing: str) -> dict:
    """Strict read-only coverage/integrity report for backup staging and health.

    No host path, writer, acquisition or migration. Invalid storage envelopes
    raise; reference-level failures are explicit, never a successful empty scan.
    All retained source bodies (including orphans) are integrity-checked.
    """
    from dream_procedural_palace import nonmutating_read, read_events
    from dream_procedural_sources import verify_source_records
    with nonmutating_read(palace):
        reader = EvidenceReader(palace, wing)
        try:
            verify_source_records(reader.sources)
        except ValueError as exc:
            raise SourceInvalid(str(exc)) from exc
        try:
            events = read_events(palace, wing)
        except ValueError as exc:
            raise RuntimeError(f"procedural storage integrity: {exc}") from exc
        report = _source_report(events, reader)
        _capacity(report, 0, 0)
        return {"status": "blocked" if report["failed"] else "ok", **report}


def capture_retained_sources(palace: str, wing: str, *, session_store: str,
                             as_of: datetime, writer=None) -> dict:
    """Explicit legacy acquisition; writer=None is a complete nonwriting preflight.

    Every missing original must pass before the first write. A storage failure
    can leave protected source records; the next invocation resumes by key.
    Events and references are never rewritten.
    """
    from contextlib import nullcontext
    from dream_procedural_palace import read_events, revalidate_sources
    from dream_procedural_sources import source_record_data, read_source_records
    with dream_palace.palace_mutation_lock(palace) if writer is not None else nullcontext():
        events = read_events(palace, wing)
        reader = AdmissionReader(palace, wing, session_store)
        report = _source_report(events, reader)
        pending_bytes = 0
        for source in reader.originals.values():
            try:
                body, metadata = source_record_data(source, captured_at=as_of, captured_by="capture-sources")
                pending_bytes += len(f"{body}\n\n<!--dreaming-meta: {canonical_json(metadata)}-->".encode("utf-8"))
            except ValueError as exc:
                report["failures"].append({"source_id": source.reference.source_id,
                                           "code": "source_size", "error": str(exc)})
        projection = project_rules(events, as_of=as_of, policy=Policy())
        if projection.errors:
            raise ValueError("invalid current projection")
        if not report["failures"]:
            _, diagnostics = revalidate_sources(projection, evidence_reader=reader, as_of=as_of)
            report["failures"].extend(f for failures in diagnostics.values() for f in failures)
        pending = len(reader.originals)
        _capacity(report, pending, pending_bytes)
        report.update(status="blocked" if report["failures"] else "dry_run",
            already_captured=len(reader.verified_captured_keys), pending=pending, captured=0,
            pending_encoded_bytes=pending_bytes, failed=len(report["failures"]))
        if report["failed"] or writer is None:
            return report
        warnings = reader.persist(writer=writer, at=as_of, actor="capture-sources")
        fresh = EvidenceReader(palace, wing)
        verified = _source_report(events, fresh)
        if verified["failed"]:
            raise RuntimeError(f"capture readback failed: {verified['failures']}")
        after = read_source_records(palace, wing)
        report.update(status="complete", captured=pending, pending=0,
                      record_count=after.record_count, encoded_bytes=after.encoded_bytes,
                      warnings=sorted(set(report["warnings"]) | set(warnings)))
        return report
