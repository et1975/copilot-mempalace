"""Immutable procedural contracts and pure, order-independent projection.

Repository keys are NFC, outer-trimmed and lowercase. Normative text is NFC
and outer-trimmed, never case-folded. Scores describe observed usefulness, not
truth or probability. No clock, storage, embedding, or KG access occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Any, Iterable, Literal
import unicodedata
from uuid import UUID

from dream_metadata import canonical_json, content_hash

RuleType = Literal["rule", "anti_pattern"]
Category = Literal["debugging", "testing", "architecture", "workflow", "documentation",
                   "integration", "security", "performance"]
EventKind = Literal["proposal", "review", "outcome"]
Verdict = Literal["approve", "hold", "retire", "replace"]
Polarity = Literal["helpful", "harmful", "neutral"]
DispositionKind = Literal["supports", "contradicts", "not_applicable", "invalid"]
Maturity = Literal["candidate", "established", "proven"]


@dataclass(frozen=True)
class RepositoryScope:
    kind: Literal["repository"]
    key: str


@dataclass(frozen=True)
class RuleDefinition:
    rule_type: RuleType
    statement: str
    scope: RepositoryScope
    category: Category
    applies_when: str
    exceptions: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceReference:
    source_kind: Literal["drawer", "session_turn"]
    source_id: str
    session_id: str | None
    quote: str
    source_hash: str
    turn_index: int | None = None
    field: Literal["user_message", "assistant_response"] | None = None


@dataclass(frozen=True)
class ValidationPacket:
    rule_id: str
    repository: str
    validated_at: datetime
    queries: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class EvidenceDisposition:
    evidence_id: str
    disposition: DispositionKind
    reason: str
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ProposalPayload:
    definition: RuleDefinition
    origin_drawer_ids: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ReviewPayload:
    verdict: Verdict
    parent_review_ids: tuple[str, ...]
    validation_packet: ValidationPacket
    validation_digest: str
    dispositions: tuple[EvidenceDisposition, ...]
    acknowledged_evidence_ids: tuple[str, ...]
    reason: str
    replacement_rule_id: str | None


@dataclass(frozen=True)
class OutcomePayload:
    outcome: Polarity
    observation_id: str
    source_session_id: str
    repository: str
    observed_at: datetime
    evidence: tuple[EvidenceReference, ...]
    attribution: str


@dataclass(frozen=True)
class ProceduralEvent:
    schema_version: int
    event_id: str
    rule_id: str
    event_kind: EventKind
    recorded_at: datetime
    actor_kind: Literal["agent", "human"]
    session_id: str
    payload: ProposalPayload | ReviewPayload | OutcomePayload
    digest: str


@dataclass(frozen=True)
class Policy:
    version: str = "procedural-v1"
    half_life_days: float = 90
    harmful_multiplier: float = 4
    stale_days: float = 90
    established_helpful: float = 3
    established_sessions: int = 3
    proven_helpful: float = 10
    proven_sessions: int = 10
    max_events: int = 5000
    max_rules: int = 100

    def __post_init__(self):
        for name in ("half_life_days", "harmful_multiplier", "stale_days",
                     "established_helpful", "proven_helpful"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid policy {name}")
        for name in ("established_sessions", "proven_sessions", "max_events", "max_rules"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"invalid policy {name}")


@dataclass(frozen=True)
class ScoreTerm:
    event_id: str
    source_session_id: str
    outcome: Polarity
    observed_at: datetime
    weight: float


@dataclass(frozen=True)
class ScoreBreakdown:
    helpful: float
    harmful: float
    effective_score: float
    helpful_sessions: int
    harmful_sessions: int
    terms: tuple[ScoreTerm, ...]


@dataclass(frozen=True)
class ProjectionError:
    code: str
    rule_ids: tuple[str, ...]
    event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleProjection:
    rule_id: str
    definition: RuleDefinition | None
    events: tuple[ProceduralEvent, ...]
    review_heads: tuple[str, ...]
    dispositions: tuple[EvidenceDisposition, ...]
    latest_validation: datetime | None
    score: ScoreBreakdown
    maturity: Maturity
    eligible: bool
    suppression_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Projection:
    rules: tuple[RuleProjection, ...]
    errors: tuple[ProjectionError, ...]
    duplicate_count: int = 0


def _object(data: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise ValueError(f"expected exactly fields {sorted(required)} (optional {sorted(optional)})")
    return data


def _text(value: Any, name: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if maximum is not None and len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return normalized


def _enum(value: Any, choices: tuple[str, ...], name: str):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def _list(value: Any, name: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be {'nonempty ' if nonempty else ''}list")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    result = tuple(_text(v, name) for v in _list(value, name))
    if len(result) != len(set(result)):
        raise ValueError(f"duplicate {name}")
    return result


def _hash(value: Any, name: str = "hash") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError(f"invalid {name}")
    return value


def _rule_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("proc:"):
        raise ValueError("invalid rule_id")
    _hash(value[5:], "rule_id")
    return value


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("event ID must be UUID text")
    if str(UUID(value)) != value:
        raise ValueError("event ID must be canonical UUID text")
    return value


def repository_key(value: Any) -> str:
    key = _text(value, "repository").lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", key):
        raise ValueError("repository must be owner/repository")
    return key


def utc_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        if "T" not in value:
            raise ValueError("timestamp must include date, time and UTC offset")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be explicit UTC")
    return value.astimezone(timezone.utc)


def parse_definition(data: Any) -> RuleDefinition:
    if isinstance(data, RuleDefinition):
        data = _data(data)
    d = _object(data, {"rule_type", "statement", "scope", "category", "applies_when", "exceptions"})
    scope = _object(d["scope"], {"kind", "key"})
    _enum(scope["kind"], ("repository",), "scope")
    return RuleDefinition(
        _enum(d["rule_type"], ("rule", "anti_pattern"), "rule_type"),
        _text(d["statement"], "statement", 800),
        RepositoryScope("repository", repository_key(scope["key"])),
        _enum(d["category"], ("debugging", "testing", "architecture", "workflow",
                             "documentation", "integration", "security", "performance"), "category"),
        _text(d["applies_when"], "applies_when", 800),
        tuple(sorted(_strings(d["exceptions"], "exceptions"))))


def canonical_rule_id(definition: RuleDefinition | dict) -> str:
    return "proc:" + content_hash(canonical_json(_data(parse_definition(definition))))


def _evidence(data: Any) -> tuple[EvidenceReference, ...]:
    result = []
    sessions = {}
    for value in _list(data, "evidence", nonempty=True):
        d = _object(value, {"source_kind", "source_id", "session_id", "quote", "source_hash"},
                    {"turn_index", "field"})
        kind = _enum(d["source_kind"], ("drawer", "session_turn"), "source_kind")
        session = None if d["session_id"] is None else _text(d["session_id"], "session_id")
        index, field = d.get("turn_index"), d.get("field")
        if kind == "session_turn":
            if type(index) is not int or index < 0 or session is None:
                raise ValueError("session evidence requires original session and nonnegative turn index")
            field = _enum(field, ("user_message", "assistant_response"), "turn field")
            if d["source_id"] != session:
                raise ValueError("session turn source_id must be the original session_id")
        elif index is not None or field is not None:
            raise ValueError("drawer evidence cannot name a turn")
        quote = d["quote"]
        _text(quote, "quote")
        source_key = (kind, d["source_id"])
        if source_key in sessions and sessions[source_key] != session:
            raise ValueError("conflicting source session attribution")
        sessions[source_key] = session
        result.append(EvidenceReference(kind, _text(d["source_id"], "source_id"), session,
                                        quote, _hash(d["source_hash"]), index, field))
    return tuple(result)


def _packet(data: Any) -> ValidationPacket:
    d = _object(data, {"rule_id", "repository", "validated_at", "queries", "evidence"})
    queries = _strings(d["queries"], "queries")
    if not queries:
        raise ValueError("validation packet needs explicit queries")
    return ValidationPacket(_rule_id(d["rule_id"]), repository_key(d["repository"]),
                            utc_datetime(d["validated_at"]), queries, _evidence(d["evidence"]))


def _data(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, EvidenceReference):
        result = {f.name: _data(getattr(value, f.name)) for f in fields(value)}
        if value.source_kind == "drawer":
            del result["turn_index"]
            del result["field"]
        return result
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _data(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_data(v) for v in value]
    return value


def event_to_data(event: ProceduralEvent) -> dict:
    """Return the canonical JSON-compatible envelope (no storage fields)."""
    return _data(event)


def parse_event(data: dict) -> ProceduralEvent:
    """Strict v1 parser; digest covers the canonical normalized envelope.

    Kind payload field names are the corresponding frozen dataclass fields.
    Evidence uses source_id=session_id for session_turn, with turn_index/field.
    Quotes are kept byte-for-byte; only normative identity text is normalized.
    """
    d = _object(data, {"schema_version", "event_id", "rule_id", "event_kind", "recorded_at",
                       "actor_kind", "session_id", "payload", "digest"})
    if len(canonical_json(d).encode("utf-8")) > 32 * 1024:
        raise ValueError("event exceeds 32 KiB")
    if type(d["schema_version"]) is not int or d["schema_version"] != 1:
        raise ValueError("unknown schema version")
    raw = {k: v for k, v in d.items() if k != "digest"}
    if _hash(d["digest"], "digest") != content_hash(canonical_json(raw)):
        raise ValueError("event digest mismatch")
    kind = _enum(d["event_kind"], ("proposal", "review", "outcome"), "event_kind")
    at = utc_datetime(d["recorded_at"])
    rule_id = _rule_id(d["rule_id"])
    if kind == "proposal":
        p = _object(d["payload"], {"definition", "origin_drawer_ids", "evidence"})
        definition = parse_definition(p["definition"])
        if rule_id != canonical_rule_id(definition):
            raise ValueError("definition rule_id mismatch")
        payload = ProposalPayload(definition, _strings(p["origin_drawer_ids"], "origin_drawer_ids"),
                                  _evidence(p["evidence"]))
    elif kind == "review":
        p = _object(d["payload"], {"verdict", "parent_review_ids", "validation_packet",
                                   "validation_digest", "dispositions", "acknowledged_evidence_ids",
                                   "reason", "replacement_rule_id"})
        packet = _packet(p["validation_packet"])
        if packet.rule_id != rule_id or packet.validated_at > at:
            raise ValueError("invalid validation packet scope/time")
        packet_digest = _hash(p["validation_digest"], "validation_digest")
        if packet_digest != content_hash(canonical_json(p["validation_packet"])):
            raise ValueError("validation packet digest mismatch")
        dispositions = []
        for value in _list(p["dispositions"], "dispositions"):
            v = _object(value, {"evidence_id", "disposition", "reason", "evidence"})
            dispositions.append(EvidenceDisposition(
                _text(v["evidence_id"], "evidence_id"),
                _enum(v["disposition"], ("supports", "contradicts", "not_applicable", "invalid"),
                      "disposition"), _text(v["reason"], "disposition reason"), _evidence(v["evidence"])))
        if len({v.evidence_id for v in dispositions}) != len(dispositions):
            raise ValueError("duplicate evidence disposition")
        verdict = _enum(p["verdict"], ("approve", "hold", "retire", "replace"), "verdict")
        replacement = p["replacement_rule_id"]
        if verdict == "replace":
            replacement = _rule_id(replacement)
        elif replacement is not None:
            raise ValueError("only replace may specify replacement_rule_id")
        parents = tuple(_uuid(v) for v in _strings(p["parent_review_ids"], "parent_review_ids"))
        payload = ReviewPayload(verdict, parents, packet, packet_digest, tuple(dispositions),
                                _strings(p["acknowledged_evidence_ids"], "acknowledged_evidence_ids"),
                                _text(p["reason"], "reason"), replacement)
    else:
        p = _object(d["payload"], {"outcome", "observation_id", "source_session_id", "repository",
                                   "observed_at", "evidence", "attribution"})
        observed = utc_datetime(p["observed_at"])
        if observed > at:
            raise ValueError("future observation relative to recording")
        payload = OutcomePayload(
            _enum(p["outcome"], ("helpful", "harmful", "neutral"), "outcome"),
            _text(p["observation_id"], "observation_id"),
            _text(p["source_session_id"], "source_session_id"), repository_key(p["repository"]),
            observed, _evidence(p["evidence"]), _text(p["attribution"], "attribution"))
        if any(ref.session_id is not None and ref.session_id != payload.source_session_id
               for ref in payload.evidence):
            raise ValueError("outcome source session mismatch")
    event = ProceduralEvent(1, _uuid(d["event_id"]), rule_id, kind, at,
                            _enum(d["actor_kind"], ("agent", "human"), "actor_kind"),
                            _text(d["session_id"], "session_id"), payload, d["digest"])
    # Re-encoding must be stable; otherwise a retry could change its digest.
    canonical = event_to_data(event)
    if canonical != d:
        raise ValueError("event must use canonical normalized values")
    return event


def event_evidence(event: ProceduralEvent) -> tuple[EvidenceReference, ...]:
    payload = event.payload
    if isinstance(payload, ReviewPayload):
        return payload.validation_packet.evidence + tuple(
            ref for disposition in payload.dispositions for ref in disposition.evidence)
    return payload.evidence


def score_outcomes(outcomes: Iterable[ProceduralEvent], *, as_of: datetime,
                   policy: Policy) -> ScoreBreakdown:
    as_of = utc_datetime(as_of)
    sessions: dict[str, list[ProceduralEvent]] = {}
    ids = {}
    for event in outcomes:
        if not isinstance(event.payload, OutcomePayload):
            raise ValueError("score_outcomes accepts only outcome events")
        if event.event_id in ids and ids[event.event_id] != event.digest:
            raise ValueError("conflicting event ID")
        ids[event.event_id] = event.digest
        if event.payload.observed_at > as_of or event.recorded_at > as_of:
            raise ValueError("future outcome")
        sessions.setdefault(event.payload.source_session_id, []).append(event)
    terms = []
    for session, events in sorted(sessions.items()):
        harmful = [e for e in events if e.payload.outcome == "harmful"]
        helpful = [e for e in events if e.payload.outcome == "helpful"]
        winners = harmful or helpful
        if not winners:
            continue
        winner = min(winners, key=lambda e: (e.payload.observed_at, e.event_id))
        age_days = (as_of - winner.payload.observed_at).total_seconds() / 86400
        weight = 2 ** (-age_days / policy.half_life_days)
        terms.append(ScoreTerm(winner.event_id, session, winner.payload.outcome,
                               winner.payload.observed_at, weight))
    helpful = math.fsum(t.weight for t in terms if t.outcome == "helpful")
    harmful = math.fsum(t.weight for t in terms if t.outcome == "harmful")
    return ScoreBreakdown(helpful, harmful, helpful - policy.harmful_multiplier * harmful,
                          sum(t.outcome == "helpful" for t in terms),
                          sum(t.outcome == "harmful" for t in terms), tuple(terms))


def _cyclic_nodes(edges: dict[str, set[str]]) -> set[str]:
    """Bounded reachability; mark cycle participants, not unrelated ancestors."""
    cyclic = set()
    for start in edges:
        pending = list(edges[start])
        seen = set()
        while pending:
            node = pending.pop()
            if node == start:
                cyclic.add(start)
                break
            if node in seen:
                continue
            seen.add(node)
            pending.extend(edges.get(node, set()) - seen)
    return cyclic


def _review_dispositions(reviews: dict[str, ProceduralEvent], head: str
                         ) -> tuple[tuple[EvidenceDisposition, ...], bool]:
    """Propagate reviewed dispositions along the DAG, never by filing order.

    A descendant overrides its ancestors explicitly. Independent dispositions
    require a joining review's explicit resolution when they disagree.
    """
    ancestors = {}
    for eid in reviews:
        seen = set()
        pending = list(reviews[eid].payload.parent_review_ids)
        while pending:
            parent = pending.pop()
            if parent not in seen and parent in reviews:
                seen.add(parent)
                pending.extend(reviews[parent].payload.parent_review_ids)
        ancestors[eid] = seen
    candidates: dict[str, list[tuple[str, EvidenceDisposition]]] = {}
    for eid in ancestors[head] | {head}:
        event = reviews[eid]
        if event.payload.verdict != "approve":
            continue
        for disposition in event.payload.dispositions:
            candidates.setdefault(disposition.evidence_id, []).append((eid, disposition))
    result = []
    conflicted = False
    for _, alternatives in sorted(candidates.items()):
        maximal = [(eid, d) for eid, d in alternatives
                   if not any(eid in ancestors[other] for other, _ in alternatives)]
        if len({d.disposition for _, d in maximal}) > 1:
            conflicted = True
        elif maximal:
            result.append(min(maximal, key=lambda item: item[0])[1])
    return tuple(result), conflicted


def project_rules(events: Iterable[ProceduralEvent], *, as_of: datetime,
                  policy: Policy) -> Projection:
    as_of = utc_datetime(as_of)
    groups: dict[str, list[ProceduralEvent]] = {}
    by_event: dict[str, dict[str, ProceduralEvent]] = {}
    duplicates = 0
    for index, event in enumerate(events):
        if index >= policy.max_events:
            return Projection((), (ProjectionError("event_limit", ()),))
        event = parse_event(event_to_data(event))
        copies = by_event.setdefault(event.event_id, {})
        if event.digest in copies:
            duplicates += 1
        else:
            copies[event.digest] = event
            groups.setdefault(event.rule_id, []).append(event)
    if len(groups) > policy.max_rules:
        return Projection((), (ProjectionError("rule_limit", tuple(sorted(groups))),), duplicates)
    errors = []
    reasons: dict[str, set[str]] = {rid: set() for rid in groups}
    for event_id, copies in sorted(by_event.items()):
        if len(copies) > 1:
            affected = tuple(sorted({e.rule_id for e in copies.values()}))
            errors.append(ProjectionError("event_id_conflict", affected, (event_id,)))
            for rid in affected:
                reasons[rid].add("event_id_conflict")
    replacement_edges = {rid: {e.payload.replacement_rule_id for e in group
                              if isinstance(e.payload, ReviewPayload)
                              and e.payload.replacement_rule_id is not None}
                         for rid, group in groups.items()}
    for rid in _cyclic_nodes(replacement_edges):
        reasons[rid].add("replacement_cycle")
    states = []
    for rid, group in sorted(groups.items()):
        group.sort(key=lambda e: (e.recorded_at, e.event_id, e.digest))
        suppressed = reasons[rid]
        future = [e for e in group if e.recorded_at > as_of]
        if future:
            suppressed.add("future_event")
            errors.append(ProjectionError("future_event", (rid,), tuple(e.event_id for e in future)))
        proposals = [e.payload.definition for e in group if isinstance(e.payload, ProposalPayload)]
        definition = proposals[0] if proposals else None
        if definition is None:
            suppressed.add("missing_definition")
        elif any(d != definition for d in proposals):
            suppressed.add("definition_conflict")
        reviews = {e.event_id: e for e in group if isinstance(e.payload, ReviewPayload)}
        edges = {eid: set(e.payload.parent_review_ids) for eid, e in reviews.items()}
        referenced = set().union(*edges.values()) if edges else set()
        heads = tuple(sorted(set(reviews) - referenced))
        if referenced - reviews.keys():
            suppressed.add("missing_review_parent")
        if _cyclic_nodes(edges):
            suppressed.add("review_cycle")
        for e in reviews.values():
            if any(reviews[p].recorded_at > e.recorded_at for p in edges[e.event_id] if p in reviews):
                suppressed.add("review_time_order")
        if len(heads) > 1:
            suppressed.add("conflicted")
        if any(e.payload.verdict == "retire" for e in reviews.values()):
            suppressed.add("retired")
        if replacement_edges[rid]:
            suppressed.add("replaced")
            if replacement_edges[rid] - groups.keys():
                suppressed.add("missing_replacement")
        head = reviews[heads[0]] if len(heads) == 1 else None
        approved = head is not None and head.payload.verdict == "approve"
        if not approved:
            suppressed.add("unapproved")
        validation = head.payload.validation_packet.validated_at if head else None
        if validation is not None and (as_of - validation).total_seconds() >= policy.stale_days * 86400:
            suppressed.add("stale_validation")
        dispositions, disposition_conflict = _review_dispositions(reviews, head.event_id) if head else ((), False)
        if disposition_conflict:
            suppressed.add("disposition_conflict")
        declared = {e.event_id for e in group} | {
            ref.source_id for e in group for ref in event_evidence(e)}
        for review in reviews.values():
            mentioned = set(review.payload.acknowledged_evidence_ids) | {
                d.evidence_id for d in review.payload.dispositions}
            if mentioned - declared:
                suppressed.add("missing_declared_evidence")
        outcomes = [e for e in group if isinstance(e.payload, OutcomePayload)]
        if definition is not None:
            if any(e.payload.repository != definition.scope.key for e in outcomes) or any(
                    e.payload.validation_packet.repository != definition.scope.key for e in reviews.values()):
                suppressed.add("scope_mismatch")
        ack = set(head.payload.acknowledged_evidence_ids) if approved else set()
        if any(d.disposition == "contradicts" and d.evidence_id not in ack for d in dispositions):
            suppressed.add("unacknowledged_conflict")
        # Only the current explicit approval can invalidate an adverse attribution.
        dismissed = {d.evidence_id for d in dispositions
                     if approved and d.evidence_id in ack and d.disposition in {"invalid", "not_applicable"}}
        harmful = [e for e in outcomes if e.payload.outcome == "harmful"]
        if any(e.event_id not in ack or (head and e.recorded_at > head.recorded_at) for e in harmful):
            suppressed.add("unresolved_harm")
        eligible_outcomes = [e for e in outcomes if e.event_id not in dismissed and e.recorded_at <= as_of]
        score = score_outcomes(eligible_outcomes, as_of=as_of, policy=policy)
        maturity: Maturity = "candidate"
        if score.effective_score > 0:
            if score.helpful_sessions >= policy.proven_sessions and score.helpful >= policy.proven_helpful:
                maturity = "proven"
            elif score.helpful_sessions >= policy.established_sessions and score.helpful >= policy.established_helpful:
                maturity = "established"
        states.append(RuleProjection(rid, definition, tuple(group), heads, dispositions, validation,
                                     score, maturity, not suppressed, tuple(sorted(suppressed))))
    return Projection(tuple(states), tuple(sorted(errors, key=lambda e: (e.code, e.rule_ids, e.event_ids))),
                      duplicates)
