"""Fresh original review to immutable artifacts; publication remains explicit."""
from __future__ import annotations

from copy import deepcopy

from dream_metadata import canonical_json, content_hash
from dream_procedural import (
    _enum, _hash, _object, _text, _uuid, parse_event, to_data, utc_datetime,
)
from dream_procedural_feedback import (
    MAX_PACKET_BYTES, _bounded, _repository, _version, prepare_feedback,
)
from dream_procedural_validate import AdmissionReader, preflight_event

MAX_DECISION_BYTES = 24 * 1024
PACKET_FIELDS = {
    "schema_version", "kind", "status", "repository", "rule_id", "definition",
    "history_digest", "source_session_id", "observed_at", "evidence",
    "delivery_receipt", "draft_origin", "packet_id",
}
DECISION_FIELDS = {
    "schema_version", "packet_id", "decision", "actor_kind", "session_id", "recorded_at",
}
RATIONALE_FIELDS = {"applicability", "exceptions", "behavior", "effect", "alternatives"}


def _canonical_text(value, name):
    if _text(value, name) != value:
        raise ValueError(f"{name} must use canonical normalized values")


def _decision(value, packet, as_of):
    _bounded(value, MAX_DECISION_BYTES, "decision")
    if not isinstance(value, dict):
        raise ValueError("expected decision object")
    choice = _enum(value.get("decision"), ("publish", "abstain"), "decision")
    fields = {"reason"} if choice == "abstain" else {
        "event_id", "observation_id", "outcome", "rationale",
    }
    _object(value, DECISION_FIELDS | fields)
    _version(value["schema_version"])
    if _hash(value["packet_id"], "packet_id") != packet["packet_id"]:
        raise ValueError("decision packet mismatch")
    _enum(value["actor_kind"], ("human", "agent"), "actor_kind")
    _canonical_text(value["session_id"], "session_id")
    recorded = utc_datetime(value["recorded_at"])
    if to_data(recorded) != value["recorded_at"]:
        raise ValueError("recorded_at must use canonical normalized values")
    if not utc_datetime(packet["observed_at"]) <= recorded <= as_of:
        raise ValueError("require observed_at <= recorded_at <= command as_of")
    if choice == "abstain":
        _text(value["reason"], "abstention reason")
    else:
        _uuid(value["event_id"])
        _canonical_text(value["observation_id"], "observation_id")
        _enum(value["outcome"], ("helpful", "harmful", "neutral"), "outcome")
        rationale = _object(value["rationale"], RATIONALE_FIELDS)
        for field, text in rationale.items():
            _text(text, f"rationale {field}")


def reviewed_outcome(packet: dict, decision: dict, *, projection, reader, as_of,
                     draft_origin: dict | None = None) -> dict:
    """Re-resolve the complete T1 packet before accepting either explicit choice.

    Structural review and original provenance do not prove causality or human
    identity. No clock, ID, polarity, rationale or independent source is inferred.
    """
    _bounded(packet, MAX_PACKET_BYTES, "feedback")
    _object(packet, PACKET_FIELDS)
    _version(packet["schema_version"])
    _repository(packet["repository"])
    if packet["kind"] != "procedural_feedback" or packet["status"] != "requires_adjudication":
        raise ValueError("expected procedural_feedback requiring adjudication")
    unsigned = {key: value for key, value in packet.items() if key != "packet_id"}
    if _hash(packet["packet_id"], "packet_id") != content_hash(canonical_json(unsigned)):
        raise ValueError("feedback packet digest mismatch")
    as_of = utc_datetime(as_of)
    _decision(decision, packet, as_of)
    selection = {key: packet[key] for key in ("schema_version", "repository", "rule_id", "evidence")}
    fresh = prepare_feedback(selection, projection=projection, reader=reader, as_of=as_of,
                             receipt=packet["delivery_receipt"], draft_origin=draft_origin)
    # Compare the complete canonical form, not Python equality (True == 1), a
    # subset, or a newly accepted digest of changed history/original draft.
    if canonical_json(fresh) != canonical_json(packet):
        raise ValueError("feedback packet changed; prepare a fresh packet and review")
    if decision["decision"] == "abstain":
        return {"schema_version": 1, "kind": "procedural_feedback_abstention",
                "status": "abstained", "decision": deepcopy(decision)}
    event = {
        "schema_version": 1, "event_id": decision["event_id"], "rule_id": packet["rule_id"],
        "event_kind": "outcome", "recorded_at": decision["recorded_at"],
        "actor_kind": decision["actor_kind"], "session_id": decision["session_id"],
        "payload": {
            "outcome": decision["outcome"], "observation_id": decision["observation_id"],
            "source_session_id": packet["source_session_id"], "repository": packet["repository"],
            "observed_at": packet["observed_at"], "evidence": deepcopy(packet["evidence"]),
            "attribution": canonical_json({
                "packet_id": packet["packet_id"],
                "decision_digest": content_hash(canonical_json(decision)),
                "reviewed_rationale": decision["rationale"],
            }),
        },
    }
    event["digest"] = content_hash(canonical_json(event))
    parsed = parse_event(event)
    preflight_event(parsed, projection=projection, evidence_reader=reader, as_of=as_of)
    if isinstance(reader, AdmissionReader):
        reader.preflight_captures(at=as_of, actor=parsed.session_id)
    return event
