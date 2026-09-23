"""Grounding and review admission, not an oracle for causal or logical truth."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import sqlite3

import dream_palace
import dream_sessions
from dream_metadata import content_hash, decode_dream_metadata, is_generated_observation
from dream_procedural import (
    EvidenceReference, OutcomePayload, Policy, ProposalPayload, ReviewPayload,
    ValidationPacket, canonical_rule_id, event_evidence, project_rules, repository_key, utc_datetime,
)


class EvidenceUnavailable(RuntimeError):
    """Original evidence cannot be resolved; never treat this as an empty search."""


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
        raise EvidenceUnavailable(f"session store unavailable: {path}")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
            rows = con.execute("SELECT repository FROM sessions WHERE id=?", (session,)).fetchall()
    except sqlite3.Error as exc:
        raise EvidenceUnavailable(f"session store unavailable: {exc}") from exc
    if len(rows) != 1:
        raise EvidenceUnavailable(f"original session unavailable: {session}")
    return repository_key(rows[0][0])


def resolve_evidence(ref: EvidenceReference, *, palace: str, session_store: str) -> ResolvedEvidence:
    """Check full original text, exact quote/hash, session, scope and source time.

    Drawers require a SESSION_ID stamp and original observed_at metadata or an
    OBSERVED_AT line. filed_at is never an observation clock. The original host
    session supplies repository authority for both diary and raw-turn evidence.
    """
    if ref.session_id is None:
        raise ValueError("independent evidence requires an original session_id")
    repository = _session_repository(session_store, ref.session_id)
    if ref.source_kind == "drawer":
        source = dream_palace.load_source_drawer(palace, ref.source_id)
        if source is None:
            raise EvidenceUnavailable(f"source drawer unavailable: {ref.source_id}")
        if is_generated_observation(source):
            raise ValueError(f"generated independent evidence: {ref.source_id}")
        text = source["text"]
        session, ambiguous = dream_palace._session_id_state(text)
        if ambiguous or session != ref.session_id:
            raise ValueError(f"original session mismatch: {ref.source_id}")
        meta = decode_dream_metadata(source)
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
            raise EvidenceUnavailable(f"original turn unavailable: {ref.source_id}/{ref.turn_index}")
        text = matches[0].get(ref.field)
        observed = utc_datetime(matches[0]["timestamp"])
    if not isinstance(text, str) or content_hash(text) != ref.source_hash \
            or not ref.quote.strip() or ref.quote not in text:
        raise ValueError(f"source hash/quote drift: {ref.source_id}")
    return ResolvedEvidence(ref, ref.session_id, repository, observed)


class EvidenceReader:
    def __init__(self, palace: str, session_store: str | None = None):
        self.palace = palace
        self.session_store = session_store or dream_sessions.default_store_path()

    def resolve(self, ref: EvidenceReference) -> ResolvedEvidence:
        return resolve_evidence(ref, palace=self.palace, session_store=self.session_store)

    def origin(self, source_id: str) -> None:
        if dream_palace.load_source_drawer(self.palace, source_id) is None:
            raise EvidenceUnavailable(f"origin drawer unavailable: {source_id}")

    def source_text(self, ref: dict) -> str:
        """Artifact preparation computes hashes; it never invents a quotation."""
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
        col = dream_palace.procedural_collection(self.palace)
        vector = embed_texts(col, [query])[0]
        result = col.query(query_embeddings=[vector], n_results=limit,
                           where={"$and": [{"wing": wing}, {"room": {"$ne": "procedural"}}]},
                           include=["documents", "metadatas"])
        ids = result.get("ids")
        if not isinstance(ids, list) or len(ids) != 1 or len(ids[0]) > limit:
            raise RuntimeError("invalid bounded search response")
        refs = []
        seen = set()
        for index, source_id in enumerate(ids[0]):
            meta = result["metadatas"][0][index] or {}
            if meta.get("room") == "procedural" or meta.get("kind") == "procedural_event":
                continue
            source = dream_palace.load_source_drawer(self.palace, source_id)
            if source is None:
                raise EvidenceUnavailable(f"search source vanished: {source_id}")
            if is_generated_observation(source):
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
            resolved = self.resolve(ref)
            if resolved.observed_at > as_of:
                raise ValueError("future search evidence")
            refs.append(ref)
        return refs


def build_validation_packet(rule, *, queries, source_reader, limits: ValidationLimits,
                            as_of: datetime) -> ValidationPacket:
    queries = tuple(queries)
    if len(queries) != 2 or queries[0] != rule.statement or \
            any(not isinstance(q, str) or not q.strip() for q in queries) or queries[0] == queries[1]:
        raise ValueError("validation requires the statement and one explicit distinct contrast query")
    refs = {}
    for query in queries:
        hits = list(source_reader(query, limits.hits_per_query))
        if len(hits) > limits.hits_per_query:
            raise ValueError("source reader exceeded validation search bound")
        for ref in hits:
            key = (ref.source_kind, ref.source_id)
            if key not in refs:
                refs[key] = ref
    if not refs:
        raise EvidenceUnavailable("no original evidence found within the bounded search")
    return ValidationPacket(canonical_rule_id(rule), rule.scope.key, utc_datetime(as_of),
                            queries, tuple(refs[key] for key in sorted(refs)))


def preflight_event(event, *, projection, evidence_reader: EvidenceReader,
                    as_of: datetime) -> PreflightResult:
    """Shared dry-run/locked-append gate. Invoke only after the exact retry gate."""
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
            adverse = {e.event_id for e in state.events if isinstance(e.payload, OutcomePayload)
                       and e.payload.outcome == "harmful"}
            adverse |= {d.evidence_id for d in payload.dispositions if d.disposition == "contradicts"}
            if not adverse <= set(payload.acknowledged_evidence_ids) or not adverse <= by_id.keys():
                raise ValueError("approval requires explicit adverse evidence acknowledgment/disposition")
            for prior in state.events:
                if prior.event_id in adverse:
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
