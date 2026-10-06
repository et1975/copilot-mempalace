"""Pure, host-neutral activity evidence; execution status is not task success."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re


_DEFAULT_OUTPUT_BYTES = 4 * 1024 * 1024


def _output_limit(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("output byte limit must be a positive integer")


def _json_chunks(value: object, max_output_bytes: int, *, newline: bool):
    _output_limit(max_output_bytes)
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    remaining = max_output_bytes - int(newline)
    for fragment in encoder.iterencode(value):
        if len(fragment) > remaining:
            raise ValueError("serialized output exceeds byte limit")
        for offset in range(0, len(fragment), 4096):
            encoded = fragment[offset:offset + 4096].encode("utf-8")
            remaining -= len(encoded)
            if remaining < 0:
                raise ValueError("serialized output exceeds byte limit")
            yield encoded
    if newline:
        yield b"\n"


def encode_json(value: object, *, max_output_bytes: int = _DEFAULT_OUTPUT_BYTES) -> bytes:
    """Encode bounded UTF-8 JSON incrementally, including its final newline."""
    return b"".join(_json_chunks(value, max_output_bytes, newline=True))


def _charge_json(value: object, remaining: int, *, comma: bool = False) -> int:
    remaining -= int(comma)
    if remaining <= 0:
        raise ValueError("serialized output exceeds byte limit")
    return remaining - sum(
        len(chunk) for chunk in _json_chunks(value, remaining, newline=False)
    )


def _fields(value: object, fields: str) -> None:
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("Invalid evidence object fields")


def _text(value: object, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty text")


def _list(value: object) -> None:
    if not isinstance(value, list):
        raise ValueError("Expected evidence list")


def _source_ref(value: object) -> None:
    _fields(value, "line event_id timestamp field")
    if type(value["line"]) is not int or value["line"] < 1:
        raise ValueError("Invalid source line")
    _text(value["event_id"], nullable=True)
    _text(value["timestamp"], nullable=True)
    _text(value["field"])


def _validate_packet(packet: dict) -> None:
    _fields(packet, "schema_version source calls warnings")
    if type(packet["schema_version"]) is not int or packet["schema_version"] != 1:
        raise ValueError("Unsupported evidence schema")
    source = packet["source"]
    _fields(source, "kind path session_id sha256 bytes events")
    for key in ("kind", "path", "session_id"):
        _text(source[key])
    if not isinstance(source["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", source["sha256"]):
        raise ValueError("Invalid snapshot digest")
    for key in ("bytes", "events"):
        if type(source[key]) is not int or source[key] < 0:
            raise ValueError("Invalid source count")
    _list(packet["warnings"])
    for warning in packet["warnings"]:
        _text(warning)
    _list(packet["calls"])
    if packet["calls"] and (not source["events"] or not source["bytes"]):
        raise ValueError("Observations require a nonempty source")
    ids, completions = set(), set()
    for call in packet["calls"]:
        _fields(call, "call_id tool_name agent_id parent_call_id source claims artifacts outcome")
        for key in ("call_id", "tool_name"):
            _text(call[key])
        for key in ("agent_id", "parent_call_id"):
            _text(call[key], nullable=True)
        if call["call_id"] in ids:
            raise ValueError("Duplicate call identity")
        ids.add(call["call_id"])
        _source_ref(call["source"])
        _list(call["claims"])
        for claim in call["claims"]:
            _fields(claim, "kind attribution text truncated source")
            if claim["kind"] not in ("description", "delegation_prompt"):
                raise ValueError("Invalid claim kind")
            if claim["attribution"] != "agent":
                raise ValueError("Observed claims must be agent-attributed")
            _text(claim["text"])
            if type(claim["truncated"]) is not bool:
                raise ValueError("Invalid claim truncation")
            _source_ref(claim["source"])
        _list(call["artifacts"])
        for artifact in call["artifacts"]:
            _fields(artifact, "path relation source")
            _text(artifact["path"])
            if artifact["relation"] not in ("created", "modified", "read", "referenced", "invoked"):
                raise ValueError("Invalid artifact relation")
            _source_ref(artifact["source"])
        outcome = call["outcome"]
        _fields(outcome, "completion_observed tool_success exit_code source")
        if type(outcome["completion_observed"]) is not bool:
            raise ValueError("Invalid completion flag")
        if outcome["tool_success"] is not None and type(outcome["tool_success"]) is not bool:
            raise ValueError("Invalid tool success flag")
        if outcome["exit_code"] is not None and type(outcome["exit_code"]) is not int:
            raise ValueError("Invalid process exit code")
        if outcome["completion_observed"]:
            _source_ref(outcome["source"])
            identity = _ref_key(outcome["source"])
            if identity in completions:
                raise ValueError("Duplicate completion evidence")
            completions.add(identity)
        elif any(outcome[key] is not None for key in ("tool_success", "exit_code", "source")):
            raise ValueError("Uncompleted call has completion evidence")
    lines, events = {}, {}
    for call in packet["calls"]:
        for reference in _call_references(call):
            line, event_id, timestamp, _ = _ref_key(reference)
            identity = (event_id, timestamp)
            if line in lines and lines[line] != identity:
                raise ValueError("Conflicting source-line identity")
            lines[line] = identity
            if event_id is not None:
                if event_id in events and events[event_id] != line:
                    raise ValueError("Conflicting source-event identity")
                events[event_id] = line


def _unknown_intent() -> dict:
    return {"status": "unknown", "text": None, "evidence": [], "attribution": None}


def _ref_key(reference: dict) -> tuple:
    return tuple(reference[key] for key in ("line", "event_id", "timestamp", "field"))


def _call_references(call: dict):
    yield call["source"]
    for observation in call["claims"] + call["artifacts"]:
        yield observation["source"]
    if call["outcome"]["source"] is not None:
        yield call["outcome"]["source"]


def _intent(claims: list[dict]) -> dict:
    if not claims:
        return _unknown_intent()
    complete = [claim for claim in claims if not claim["truncated"]]
    eligible = complete or claims
    highest_kind = (
        "delegation_prompt"
        if any(claim["kind"] == "delegation_prompt" for claim in eligible)
        else "description"
    )
    highest = [claim for claim in eligible if claim["kind"] == highest_kind]
    references = []
    for claim in highest:
        if claim["source"] not in references:
            references.append(deepcopy(claim["source"]))
    texts = {claim["text"] for claim in highest}
    if not complete:
        status, text = "unknown", None
    elif len(texts) == 1:
        status, text = "stated", highest[0]["text"]
    else:
        status, text = "ambiguous", None
    return {"status": status, "text": text, "evidence": references, "attribution": "agent"}


def _execution_status(outcome: dict) -> str:
    if not outcome["completion_observed"]:
        return "incomplete"
    if outcome["tool_success"] is False or outcome["exit_code"] not in (None, 0):
        return "failed"
    if outcome["tool_success"] is True or outcome["exit_code"] == 0:
        return "succeeded"
    return "unknown"


def build_activities(packet: dict) -> dict:
    """Build observed-only activities without reading artifacts or inferring intent."""
    _validate_packet(packet)
    evidence = deepcopy(packet)
    by_id = {call["call_id"]: call for call in evidence["calls"]}
    roots = {}
    for call_id in by_id:
        chain, visiting = [], set()
        current = call_id
        while current not in roots:
            if current in visiting:
                raise ValueError("Cyclic parent-call relationship")
            visiting.add(current)
            chain.append(current)
            parent = by_id[current]["parent_call_id"]
            if parent is None or parent not in by_id:
                root = current
                if parent is not None:
                    warning = f"Missing parent call for {current!r}; retained as an unlinked root"
                    if warning not in evidence["warnings"]:
                        evidence["warnings"].append(warning)
                break
            current = parent
        else:
            root = roots[current]
        for member in chain:
            roots[member] = root

    groups = {}
    for call in evidence["calls"]:
        call["intent"] = _intent(call["claims"])
        call["execution_status"] = _execution_status(call["outcome"])
        groups.setdefault(roots[call["call_id"]], []).append(call)
    activities = []
    for root_id, members in groups.items():
        root_call = by_id[root_id]
        members = [root_call] + [call for call in members if call["call_id"] != root_id]
        identity = [
            evidence["source"]["kind"],
            evidence["source"]["session_id"],
            root_id,
        ]
        activities.append({
            "activity_id": "activity-" + hashlib.sha256(
                json.dumps(identity, ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
            "root_call_id": root_id,
            "call_ids": [call["call_id"] for call in members],
            "intent": deepcopy(root_call["intent"]),
            "calls": members,
            "visibility": "observed_calls_only",
        })
    return {
        "schema_version": 1,
        "kind": "activity_intents",
        "source": evidence["source"],
        "warnings": evidence["warnings"],
        "activities": activities,
    }


def _validate_intent(intent: dict) -> None:
    _fields(intent, "status text evidence attribution")
    if intent["status"] not in ("unknown", "stated", "ambiguous", "inferred"):
        raise ValueError("Invalid intent status")
    _text(intent["text"], nullable=True)
    _text(intent["attribution"], nullable=True)
    _list(intent["evidence"])
    for reference in intent["evidence"]:
        _source_ref(reference)


def _activity_references(activity: dict) -> set[tuple]:
    return {_ref_key(reference) for call in activity["calls"]
            for reference in _call_references(call)}


def _review_intent(review: dict, activity: dict, source_sha256: str) -> dict:
    _fields(review, "activity_id source_sha256 status text evidence rationale")
    _text(review["activity_id"])
    _text(review["source_sha256"])
    if review["source_sha256"] != source_sha256:
        raise ValueError("Review does not match the source snapshot")
    if review["activity_id"] != activity["activity_id"] or review["status"] != "inferred":
        raise ValueError("Review must infer intent for the selected activity")
    _text(review["text"])
    _text(review["rationale"])
    _list(review["evidence"])
    if not review["evidence"]:
        raise ValueError("Review requires observed evidence")
    allowed = _activity_references(activity)
    seen = set()
    for reference in review["evidence"]:
        _source_ref(reference)
        key = _ref_key(reference)
        if key not in allowed or key in seen:
            raise ValueError("Review evidence must be distinct observations from this activity")
        seen.add(key)
    return {
        "status": "inferred",
        "text": review["text"],
        "evidence": deepcopy(review["evidence"]),
        "attribution": "reviewer",
    }


def _validate_report(report: dict) -> None:
    """Re-derive execution and observed intent instead of trusting edited reports."""
    _fields(report, "schema_version kind source warnings activities")
    if report["kind"] != "activity_intents":
        raise ValueError("Expected an activity intent report")
    _list(report["activities"])
    calls = []
    observed = []
    for activity in report["activities"]:
        if not isinstance(activity, dict):
            raise ValueError("Invalid activity")
        fields = "activity_id root_call_id call_ids intent calls visibility"
        if "review" in activity:
            fields += " observed_intent review"
        _fields(activity, fields)
        for key in ("activity_id", "root_call_id", "visibility"):
            _text(activity[key])
        _list(activity["call_ids"])
        for call_id in activity["call_ids"]:
            _text(call_id)
        _validate_intent(activity["intent"])
        _list(activity["calls"])
        for call in activity["calls"]:
            if not isinstance(call, dict) or "intent" not in call or "execution_status" not in call:
                raise ValueError("Missing derived call observations")
            _validate_intent(call["intent"])
            _text(call["execution_status"])
            calls.append({key: value for key, value in call.items()
                          if key not in ("intent", "execution_status")})
        original = {key: value for key, value in activity.items()
                    if key not in ("review", "observed_intent")}
        if "review" in activity:
            _validate_intent(activity["observed_intent"])
            original["intent"] = activity["observed_intent"]
        observed.append(original)
    rebuilt = build_activities({
        "schema_version": report["schema_version"],
        "source": report["source"],
        "warnings": report["warnings"],
        "calls": calls,
    })
    if observed != rebuilt["activities"] or report["warnings"] != rebuilt["warnings"]:
        raise ValueError("Report does not match its observed evidence")
    for activity in report["activities"]:
        if "review" not in activity:
            continue
        review = activity["review"]
        _fields(review, "activity_id source_sha256 status text evidence rationale attribution")
        if review["attribution"] != "reviewer":
            raise ValueError("Invalid review attribution")
        raw_review = {key: value for key, value in review.items() if key != "attribution"}
        if activity["intent"] != _review_intent(raw_review, activity, report["source"]["sha256"]):
            raise ValueError("Intent does not match its explicit review")


def apply_reviews(report: dict, reviews: list[dict]) -> dict:
    """Apply snapshot-bound reviewer inference, retaining all observed intent."""
    _validate_report(report)
    _list(reviews)
    result = deepcopy(report)
    activities = {activity["activity_id"]: activity for activity in result["activities"]}
    for review in reviews:
        _fields(review, "activity_id source_sha256 status text evidence rationale")
        _text(review["activity_id"])
        activity = activities.get(review["activity_id"])
        if activity is None:
            raise ValueError("Review references an unknown activity")
        if "review" in activity:
            raise ValueError("Activity has already been reviewed")
        intent = _review_intent(review, activity, report["source"]["sha256"])
        activity["observed_intent"] = deepcopy(activity["intent"])
        activity["intent"] = intent
        activity["review"] = {**deepcopy(review), "attribution": "reviewer"}
    return result


def artifact_review(
    report: dict, *, max_output_bytes: int = _DEFAULT_OUTPUT_BYTES, compact: bool = False,
) -> dict:
    """Project bounded review candidates; compact IDs resolve in the input report."""
    _output_limit(max_output_bytes)
    if type(compact) is not bool:
        raise ValueError("compact must be a boolean")
    _validate_report(report)
    result = {
        "schema_version": 1,
        "kind": "artifact_reuse_index" if compact else "artifact_reuse_review",
        "source": report["source"],
        "candidates": [],
    }
    remaining = max_output_bytes - sum(
        len(chunk) for chunk in _json_chunks(result, max_output_bytes, newline=True)
    )
    result["source"] = deepcopy(report["source"])
    candidates = {}
    for activity in report["activities"]:
        for call in activity["calls"]:
            for artifact in call["artifacts"]:
                candidate = candidates.get(artifact["path"])
                if candidate is None:
                    candidate = {
                        "path": artifact["path"],
                        "assessment": "needs_review",
                        "activity_ids": [],
                        "observations": [],
                    }
                    remaining = _charge_json(candidate, remaining, comma=bool(candidates))
                    candidates[artifact["path"]] = candidate
                    result["candidates"].append(candidate)
                if activity["activity_id"] not in candidate["activity_ids"]:
                    remaining = _charge_json(
                        activity["activity_id"], remaining, comma=bool(candidate["activity_ids"]),
                    )
                    candidate["activity_ids"].append(activity["activity_id"])
                observation = {
                    "activity_id": activity["activity_id"],
                    "call_id": call["call_id"],
                    "relation": artifact["relation"],
                    "source": artifact["source"],
                }
                if not compact:
                    observation.update({
                        "call_source": call["source"],
                        "claims": call["claims"],
                        "intent": activity["intent"],
                        "observed_intent": activity.get("observed_intent", activity["intent"]),
                        "call_intent": call["intent"],
                        "outcome": call["outcome"],
                        "execution_status": call["execution_status"],
                        "review": activity.get("review"),
                    })
                # Charge expanded evidence before copying it for another path.
                remaining = _charge_json(
                    observation, remaining, comma=bool(candidate["observations"]),
                )
                candidate["observations"].append(deepcopy(observation))
    return result
