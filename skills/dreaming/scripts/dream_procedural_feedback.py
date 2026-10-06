"""Read-only original-evidence handoff; transport never supplies causal credit."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from dream_metadata import canonical_json, content_hash, strict_json
from dream_procedural import (
    EvidenceReference, _evidence, _hash, _object, _rule_id, repository_key, to_data, utc_datetime,
)
from dream_procedural_drafts import is_procedural_echo, validate_receipt
from dream_procedural_sources import source_key

MAX_SELECTION_BYTES = 24 * 1024
MAX_RECEIPT_BYTES = 24 * 1024
MAX_DRAFT_BYTES = 64 * 1024
MAX_PACKET_BYTES = 64 * 1024
REVIEW_REQUIREMENTS = frozenset({
    "rule_specific_causal_attribution", "explicit_polarity",
    "applicability_and_exceptions_review", "original_evidence_review",
})


def read_json(path: str, maximum: int) -> dict:
    """Bound actual UTF-8 bytes before parsing, including whitespace and wrappers."""
    with Path(path).expanduser().open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("feedback input exceeds byte limit")
    return strict_json(raw.decode("utf-8"))


def _bounded(value, maximum, name, *, newline=False):
    if len((canonical_json(value) + ("\n" if newline else "")).encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds complete packet byte limit")


def _version(value):
    if type(value) is not int or value != 1:
        raise ValueError("expected schema_version 1")


def _repository(value):
    if repository_key(value) != value:
        raise ValueError("repository must be canonical")
    return value


def feedback_references(values) -> tuple[EvidenceReference, ...]:
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ValueError("feedback requires 1..8 submitted references")
    _evidence(values)
    # The common parser validates shape but normalizes identifiers. A source
    # lookup must retain as-written identities as well as verbatim quotes.
    refs = tuple(EvidenceReference(**value) for value in values)
    for ref in refs:
        if ref.source_kind == "session_turn" and ref.source_id != ref.session_id:
            raise ValueError("session turn source_id must exactly equal the original session_id")
        source_key(ref)
        if len(ref.quote) > 800:
            raise ValueError("quote exceeds 800 Unicode code points")
    encoded = [canonical_json(to_data(ref)) for ref in refs]
    if len(set(encoded)) != len(encoded):
        raise ValueError("duplicate reference")
    return tuple(ref for _, ref in sorted(zip(encoded, refs), key=lambda pair: pair[0]))


def _selection(value):
    _bounded(value, MAX_SELECTION_BYTES, "selection")
    value = _object(value, {"schema_version", "repository", "rule_id", "evidence"})
    _version(value["schema_version"])
    _repository(value["repository"])
    _rule_id(value["rule_id"])
    return feedback_references(value["evidence"])


def _draft(value):
    _bounded(value, MAX_DRAFT_BYTES, "draft")
    _object(value, {"schema_version", "kind", "status", "repository", "session_id",
                    "task_digest", "delivered_rule_ids", "original_references",
                    "lineage_references", "missing_requirements", "independent_sessions",
                    "omitted_count"})
    _version(value["schema_version"])
    if value["kind"] != "procedural_draft":
        raise ValueError("expected legacy schema-1 procedural_draft")
    if value["status"] != "requires_review" or type(value["omitted_count"]) is not int \
            or value["omitted_count"] != 0:
        raise ValueError("pending original evidence: resolve sources before handoff")
    requirements = value["missing_requirements"]
    if not isinstance(requirements, list) or len(requirements) != 4 \
            or any(not isinstance(item, str) for item in requirements) \
            or set(requirements) != REVIEW_REQUIREMENTS:
        raise ValueError("draft must retain all four review obligations")
    _repository(value["repository"])
    session = value["session_id"]
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session):
        raise ValueError("invalid draft session_id")
    _hash(value["task_digest"], "task_digest")
    ids = value["delivered_rule_ids"]
    if not isinstance(ids, list) or len(ids) > 5:
        raise ValueError("invalid draft delivered_rule_ids")
    for rule_id in ids:
        _rule_id(rule_id)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate draft delivered_rule_ids")
    originals = feedback_references(value["original_references"])
    if any(ref.session_id != session for ref in originals):
        raise ValueError("draft original session mismatch")
    if type(value["independent_sessions"]) is not int or value["independent_sessions"] != 1:
        raise ValueError("draft independent_sessions mismatch")
    lineage = value["lineage_references"]
    if not isinstance(lineage, list) or not lineage:
        raise ValueError("draft requires receipt lineage")
    receipt = _object(lineage[0], {"source_kind", "session_id", "generation",
                                  "task_digest", "guidance_as_of"})
    if receipt["source_kind"] != "delivery_receipt" or receipt["session_id"] != session \
            or receipt["task_digest"] != value["task_digest"]:
        raise ValueError("draft receipt lineage mismatch")
    if type(receipt["generation"]) is not int or receipt["generation"] < 0:
        raise ValueError("invalid draft receipt generation")
    if receipt["guidance_as_of"] is not None:
        utc_datetime(receipt["guidance_as_of"])
    for echo in lineage[1:]:
        if not isinstance(echo, dict) or echo.get("reason") != "generated_transport_echo":
            raise ValueError("invalid draft echo lineage")
        refs = feedback_references([{key: item for key, item in echo.items() if key != "reason"}])
        if refs[0].session_id != session:
            raise ValueError("draft echo session mismatch")
    return value


def selection_from_draft(draft: dict, rule_id: str, indexes: list[int]) -> dict:
    """Select explicit original-reference indexes, never lineage or nearby turns."""
    _draft(draft)
    _rule_id(rule_id)
    if rule_id not in draft["delivered_rule_ids"]:
        raise ValueError("explicit delivered rule selection required")
    originals = draft["original_references"]
    if not isinstance(indexes, list) or not 1 <= len(indexes) <= 8 \
            or any(type(i) is not int or not 0 <= i < len(originals) for i in indexes) \
            or len(set(indexes)) != len(indexes):
        raise ValueError("select distinct original-reference indexes")
    return {"schema_version": 1, "repository": draft["repository"], "rule_id": rule_id,
            "evidence": deepcopy([originals[index] for index in indexes])}


def _draft_origin(draft, selection, refs):
    if draft is None:
        return None
    _draft(draft)
    if draft["repository"] != selection["repository"] \
            or selection["rule_id"] not in draft["delivered_rule_ids"]:
        raise ValueError("draft selection scope or delivered rule mismatch")
    allowed = {canonical_json(to_data(ref)) for ref in feedback_references(draft["original_references"])}
    if any(canonical_json(to_data(ref)) not in allowed for ref in refs):
        raise ValueError("selection must map exactly to draft original references, not lineage")
    return {"digest": content_hash(canonical_json(draft)), "status": draft["status"],
            "missing_requirements": deepcopy(draft["missing_requirements"]),
            "lineage_references": deepcopy(draft["lineage_references"])}


def prepare_feedback(selection: dict, *, projection, reader, as_of,
                     receipt: dict | None = None, draft_origin: dict | None = None) -> dict:
    """Resolve one target observation. No publication, capture, or polarity."""
    refs = _selection(selection)
    as_of = utc_datetime(as_of)
    origin = _draft_origin(draft_origin, selection, refs)
    if receipt is not None:
        _bounded(receipt, MAX_RECEIPT_BYTES, "receipt")
        validate_receipt(receipt, selection["repository"])
        if selection["rule_id"] not in receipt["delivered_rule_ids"]:
            raise ValueError("receipt delivered rule mismatch")
    if projection.errors:
        raise ValueError("procedural projection integrity")
    state = next((state for state in projection.rules if state.rule_id == selection["rule_id"]), None)
    if state is None or state.definition is None:
        raise ValueError("unknown rule definition")
    if state.definition.scope.key != selection["repository"]:
        raise ValueError("rule repository mismatch")
    observations = set()
    for ref in refs:
        resolved = reader.resolve(ref)
        text = reader.source_text(to_data(ref))
        if is_procedural_echo(text):
            raise ValueError("generated transport is not original feedback evidence")
        if content_hash(text) != ref.source_hash or ref.quote not in text:
            raise ValueError("source hash/quote drift")
        if resolved.repository != selection["repository"] or resolved.session_id != ref.session_id:
            raise ValueError("original repository/session mismatch")
        observed = utc_datetime(resolved.observed_at)
        if observed > as_of:
            raise ValueError("future original observation")
        observations.add((resolved.repository, resolved.session_id, observed))
    if len(observations) != 1:
        raise ValueError("split_observations: select one repository/session/timestamp")
    _, session, observed = observations.pop()
    if receipt is not None and receipt["session_id"] != session:
        raise ValueError("receipt original session mismatch")
    packet = {
        "schema_version": 1, "kind": "procedural_feedback", "status": "requires_adjudication",
        "repository": selection["repository"], "rule_id": selection["rule_id"],
        "definition": to_data(state.definition),
        "history_digest": content_hash(canonical_json(sorted(
            (event.event_id, event.digest) for event in state.events))),
        "source_session_id": session, "observed_at": to_data(observed),
        "evidence": [to_data(ref) for ref in refs],
        "delivery_receipt": deepcopy(receipt), "draft_origin": origin,
    }
    packet["packet_id"] = content_hash(canonical_json(packet))
    _bounded(packet, MAX_PACKET_BYTES, "feedback", newline=True)
    return packet
