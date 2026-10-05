"""Read-only task-bound procedural delivery and cooperative current-use checks.

Witnesses are caller observations, not authenticated host facts or durable grants.
This module neither captures sources nor enrolls rules nor publishes receipts.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from dream_metadata import canonical_json, content_hash, strict_json
from dream_procedural import Policy, project_rules, repository_key, utc_datetime
from dream_procedural_guidance import validate_guidance
from dream_procedural_palace import (
    GuidanceLimits, _wing, embed_texts, get_task_guidance, nonmutating_read,
    read_events, revalidate_sources,
)

MAX_CONTEXT_BYTES = 20 * 1024
MAX_CURRENT_BYTES = 21 * 1024
MAX_PERMISSION_BYTES = 8192
MAX_PACKET_BYTES = 32 * 1024
MAX_APPLICABILITY_BYTES = 32 * 1024
CONTEXT_FIELDS = {"repository", "wing", "session_id", "actor_id", "task_id", "revision",
                  "task", "constraints", "mode"}
PERMISSION_FIELDS = {"repository", "wing", "session_id", "actor_id", "checked_at",
                     "advice", "receipts", "trials", "advice_ref", "receipts_ref"}
PACKET_FIELDS = {"schema_version", "kind", "status", "request_id", "context",
                 "context_digest", "guidance", "guidance_digest"}
RULE_CONTENT_FIELDS = ("rule_id", "rule_type", "statement", "applies_when", "exceptions", "evidence")


class AdviceWithheld(ValueError):
    """Healthy non-delivery, not source/storage unavailability."""


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(value, fields, label):
    _require(isinstance(value, dict) and value.keys() == fields, f"invalid {label} fields")


def _text(value, label, *, chars=512, blank=False):
    _require(isinstance(value, str) and (blank or bool(value.strip())) and len(value) <= chars,
             f"invalid {label}")
    value.encode("utf-8")


def _uuid(value, label):
    _text(value, label)
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    _require(valid, f"{label} must be a canonical UUID")


def _bounded(value, limit, label, *, newline=False):
    encoded = (canonical_json(value) + ("\n" if newline else "")).encode("utf-8")
    _require(len(encoded) <= limit, f"{label} exceeds {limit}-byte budget")
    return encoded


def hash_data(value):
    return content_hash(canonical_json(value))


def read_json(path, limit):
    """Bound actual bytes, reject special files/symlinks, never repair input."""
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "input must be a regular file")
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, "input exceeds byte budget")
    return strict_json(raw.decode("utf-8"))


def recent(stamp, now):
    _require(isinstance(stamp, str), "checked_at must be a UTC timestamp")
    checked = utc_datetime(stamp)
    _require(0 <= (utc_datetime(now) - checked).total_seconds() <= 60,
             "current observation expired or future")
    return checked


def validate_context(context):
    _object(context, CONTEXT_FIELDS, "context")
    for key in ("repository", "wing"):
        _text(context[key], key)
        _require(len(context[key].encode("utf-8")) <= 256, f"{key} exceeds UTF-8 budget")
    _require(repository_key(context["repository"]) == context["repository"],
             "context repository must be canonical")
    _wing(context["wing"])
    for key in ("session_id", "actor_id", "task_id"):
        _uuid(context[key], key)
    _require(type(context["revision"]) is int and context["revision"] >= 0, "invalid context revision")
    _text(context["task"], "task", chars=16384)
    _require(len(context["task"].encode("utf-8")) <= 16384, "task exceeds UTF-8 budget")
    constraints = context["constraints"]
    _require(isinstance(constraints, list) and len(constraints) <= 8, "invalid context constraints")
    for constraint in constraints:
        _text(constraint, "constraint", blank=True)
    _require(context["mode"] in ("established", "approved_candidate_trial"), "invalid context mode")
    _bounded(context, MAX_CONTEXT_BYTES, "context")
    return context


def validate_current(current, now):
    _object(current, {"context", "checked_at"}, "current")
    validate_context(current["context"])
    recent(current["checked_at"], now)
    _bounded(current, MAX_CURRENT_BYTES, "current", newline=True)
    return current["context"]


def consent(context, witness, now, *, receipt=False):
    validate_context(context)
    _object(witness, PERMISSION_FIELDS, "permission")
    _bounded(witness, MAX_PERMISSION_BYTES, "permission", newline=True)
    recent(witness["checked_at"], now)
    for key in ("repository", "wing", "session_id", "actor_id"):
        _require(witness[key] == context[key], "permission scope mismatch")
    for key in ("advice", "receipts", "trials"):
        _require(witness[key] in ("allow", "deny", "unknown"), f"invalid {key} permission")
    for key in ("advice", "receipts"):
        reference = witness[key + "_ref"]
        if reference is not None:
            _text(reference, f"{key} permission reference")
        _require(witness[key] != "allow" or reference is not None,
                 f"{key} permission requires an actual user-message reference")
    required = ("advice", "receipts") if receipt else ("advice",)
    for key in required:
        _require(witness[key] == "allow", f"{key} permission absent or revoked")
    if receipt:
        _require(witness["advice_ref"] != witness["receipts_ref"],
                 "receipt permission requires a separate reference")
    elif context["mode"] == "approved_candidate_trial":
        _require(witness["trials"] == "allow", "trial not authorized")


def _guidance(data, context, now):
    return validate_guidance((canonical_json(data) + "\n").encode("utf-8"),
                             context["repository"], mode=context["mode"], as_of=now)


def validate_packet(packet, now):
    _object(packet, PACKET_FIELDS, "packet")
    _require(type(packet["schema_version"]) is int and packet["schema_version"] == 1,
             "invalid packet schema_version")
    _require(packet["kind"] == "procedural_delivery_packet" and packet["status"] == "bound_guidance",
             "invalid delivery packet kind/status")
    _uuid(packet["request_id"], "request_id")
    context = validate_context(packet["context"])
    _guidance(packet["guidance"], context, now)
    for key in ("context", "guidance"):
        _require(packet[key + "_digest"] == hash_data(packet[key]), "packet digest mismatch")
    _bounded(packet, MAX_PACKET_BYTES, "packet", newline=True)
    return packet


def build_packet(current, witness, request_id, refresh, now, *, clock=None):
    current, witness = deepcopy(current), deepcopy(witness)
    context = validate_current(current, now)
    consent(context, witness, now)
    _uuid(request_id, "request_id")
    guidance = _guidance(refresh(deepcopy(context), now), context, now)
    packet = dict(schema_version=1, kind="procedural_delivery_packet", status="bound_guidance",
                  request_id=request_id, context=context, context_digest=hash_data(context),
                  guidance=guidance, guidance_digest=hash_data(guidance))
    end = clock() if clock else now
    validate_current(current, end)
    consent(context, witness, end)
    return validate_packet(packet, end)


def rule_content_digest(item):
    """Bind fit to full advice and source locators, not volatile scores."""
    return hash_data({key: item[key] for key in RULE_CONTENT_FIELDS})


def check_applicability(witness, context, items, now):
    """Validate explicit human/agent reasoning, never infer semantic truth.

    Unknown/incompatible assessments withhold use. Exact coverage binds the
    assessment to all current constraints/conditions/exceptions; locality alone
    or a high similarity score is insufficient.
    """
    if witness is None:
        raise AdviceWithheld("applicability not assessed")
    _object(witness, {"schema_version", "kind", "authority", "context_digest", "checked_at", "assessments"},
            "applicability")
    _require(type(witness["schema_version"]) is int and witness["schema_version"] == 1,
             "invalid applicability schema_version")
    _require(witness["kind"] == "procedural_applicability_witness"
             and witness["authority"] == "agent_reported", "invalid applicability provenance")
    _bounded(witness, MAX_APPLICABILITY_BYTES, "applicability", newline=True)
    recent(witness["checked_at"], now)
    _require(witness["context_digest"] == hash_data(context), "applicability context mismatch")
    assessments = witness["assessments"]
    _require(isinstance(assessments, list) and len(assessments) == len(items),
             "applicability requires exact selected-rule coverage")
    by_id = {}
    compatible = True

    def check(value, expected, verdicts, positive):
        _object(value, {"text", "verdict", "reason"}, "applicability check")
        _require(value["text"] == expected, "applicability omits or changes complete current text")
        _require(value["verdict"] in verdicts, "invalid applicability verdict")
        _text(value["reason"], "applicability reason")
        return value["verdict"] == positive

    for entry in assessments:
        _object(entry, {"rule_id", "rule_digest", "condition", "exceptions", "constraints",
                        "supported_context"}, "applicability assessment")
        _require(isinstance(entry["rule_id"], str) and entry["rule_id"] not in by_id,
                 "duplicate or invalid applicability rule")
        by_id[entry["rule_id"]] = entry
    _require(set(by_id) == {item["rule_id"] for item in items}, "applicability rule mismatch")
    for item in items:
        entry = by_id[item["rule_id"]]
        _require(entry["rule_digest"] == rule_content_digest(item), "applicability rule content changed")
        compatible &= check(entry["condition"], item["applies_when"],
                            ("satisfied", "not_satisfied", "unknown"), "satisfied")
        for key, expected, verdicts, positive in (
            ("exceptions", item["exceptions"], ("absent", "present", "unknown"), "absent"),
            ("constraints", context["constraints"], ("compatible", "incompatible", "unknown"), "compatible"),
        ):
            checks = entry[key]
            _require(isinstance(checks, list) and len(checks) == len(expected),
                     "applicability requires complete condition/constraint coverage")
            for value, text in zip(checks, expected):
                compatible &= check(value, text, verdicts, positive)
        supported = entry["supported_context"]
        _object(supported, {"verdict", "reason"}, "supported context")
        _require(supported["verdict"] in ("compatible", "incompatible", "unknown"),
                 "invalid supported-context applicability")
        _text(supported["reason"], "supported-context reason")
        compatible &= supported["verdict"] == "compatible"
    if not compatible:
        raise AdviceWithheld("applicability incompatible or materially unknown")


def preflight_use(packet, current, witness, selected, refresh, now, *,
                  applicability=None, clock=None):
    packet, current, witness = deepcopy(packet), deepcopy(current), deepcopy(witness)
    validate_packet(packet, now)
    context = validate_current(current, now)
    consent(context, witness, now)
    _require(context == packet["context"], "context changed")
    _require(isinstance(selected, list) and 1 <= len(selected) <= 5
             and all(isinstance(rid, str) and re.fullmatch(r"proc:[0-9a-f]{64}", rid)
                     for rid in selected) and len(set(selected)) == len(selected),
             "invalid selected rule IDs")

    def indexed(guidance):
        return {item["rule_id"]: item for section in ("rules", "anti_patterns", "trials")
                for item in guidance[section]}

    old = indexed(packet["guidance"])
    fresh = _guidance(refresh(deepcopy(context), now), context, now)
    latest = indexed(fresh)
    end = clock() if clock else now
    validate_current(current, end)
    consent(context, witness, end)
    if any(rid not in old or rid not in latest for rid in selected):
        raise AdviceWithheld("selected rule is not currently deliverable")
    items = [latest[rid] for rid in selected]
    check_applicability(applicability, context, items, end)
    return items


def refresh_guidance(palace, context, now):
    """Fresh projection and source index per invocation, including unchanged tasks."""
    import dream_palace
    from dream_procedural_validate import EvidenceReader
    validate_context(context)
    with nonmutating_read(palace):
        projection = project_rules(read_events(palace, context["wing"]), as_of=now, policy=Policy())
        if projection.errors:
            raise RuntimeError("procedural projection integrity failed")
        projection, _ = revalidate_sources(
            projection, evidence_reader=EvidenceReader(palace, context["wing"]),
            as_of=now, repository=context["repository"])
        result = get_task_guidance(projection,
            task=canonical_json({"task": context["task"], "constraints": context["constraints"]}),
            repository=context["repository"], as_of=now,
            include_candidates=context["mode"] == "approved_candidate_trial",
            limits=GuidanceLimits(max_bytes=8192),
            embedder=lambda texts: embed_texts(dream_palace.procedural_collection(palace), texts))
        return _guidance(result.data, context, now)
