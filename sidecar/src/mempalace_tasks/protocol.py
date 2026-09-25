"""Ordered, epoch-fenced journal fold. No transport, clock, or filesystem effects.

LogState belongs to one serialized owner. fold_record mutates it only after an
entire record validates and returns it for convenience. Consumers must receive
a detached copy, never the owner's mutable instance. V1 history occupies the
implicit None epoch; activation closes that scope without rewriting its bytes.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from uuid import UUID, uuid5

from .codec import canonical_json, command_hash, payload_hash
from .domain import apply_event, decide
from .model import DomainError, identifier, instant, new_state


MAX_PAYLOAD_BYTES = 240 * 1024
_COMMON = {"record_type", "schema_version", "authority_id", "command_id",
           "command_hash", "task_ids", "payload_hash"}
_PROPOSAL = _COMMON | {"ordinal", "previous_event_id", "event"}
_SETTLEMENT = _COMMON | {"proposal_hash", "control_id"}
_EPOCH = {"record_type", "schema_version", "authority_id", "epoch_id", "activation_id",
          "previous_activation_id", "at", "payload_hash"}
_EVENT = {"schema_version", "authority_id", "kind", "command", "at", "tasks",
          "edges_added", "edges_removed", "resources", "configuration", "changes", "response"}
_HASH = re.compile("[0-9a-f]{64}")


class ProtocolError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(details) if details is not None else {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": deepcopy(self.details)}


def _require(condition, message):
    if not condition:
        raise ProtocolError("invariant_violation", message)


def normalize_command(command):
    """Only the operation prefix is an alias; absent and null remain distinct."""
    canonical_json(command)
    if type(command) is not dict:
        raise DomainError("validation_error", "command must be an object")
    result = deepcopy(command)
    operation = result.get("operation")
    if type(operation) is not str or not operation:
        raise DomainError("validation_error", "operation must be text")
    if operation.startswith("mptask_mptask_"):
        raise DomainError("validation_error", "operation allows at most one mptask_ prefix")
    result["operation"] = operation.removeprefix("mptask_")
    identifier(result.get("command_id"), "command_id")
    return result


@dataclass
class LogState:
    authority_id: str
    state: object = field(init=False)
    epoch_id: str | None = field(default=None, init=False)
    activation_id: str | None = field(default=None, init=False)
    activation_at: str | None = field(default=None, init=False)
    activation_event_id: str | None = field(default=None, init=False)
    activation_attempts: dict = field(default_factory=dict, init=False)
    used_epochs: dict = field(default_factory=dict, init=False)
    scoped_outcomes: dict = field(default_factory=dict, init=False)
    outcomes: dict = field(default_factory=dict, init=False)
    proposals: dict = field(default_factory=dict, init=False)
    controls: dict = field(default_factory=dict, init=False)
    raw_cursor: str | None = field(default=None, init=False)
    raw_hash: str | None = field(default=None, init=False)
    domain_head: str | None = field(default=None, init=False)
    domain_ordinal: int = field(default=0, init=False)
    history: list = field(default_factory=list, init=False)
    accepted_records: list = field(default_factory=list, init=False)
    raw_ids: set = field(default_factory=set, init=False)

    def __post_init__(self):
        self.state = new_state(self.authority_id)

    def checkpoint(self):
        return {"raw_cursor": self.raw_cursor, "raw_hash": self.raw_hash,
                "domain_head": self.domain_head, "domain_ordinal": self.domain_ordinal}

    def historical_outcome(self, epoch_id, command_id):
        """Resolve exactly one scope, never grant current execution authorization."""
        if epoch_id is not None:
            identifier(epoch_id, "epoch_id")
        identifier(command_id, "command_id")
        return deepcopy(self.scoped_outcomes.get((self.authority_id, epoch_id, command_id)))


def _seal(payload):
    try:
        payload["payload_hash"] = payload_hash(payload)
        if len(canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ProtocolError("payload_too_large", "Complete task envelope exceeds 240 KiB")
    except DomainError as error:
        raise ProtocolError("invariant_violation", "Invalid protocol value",
                            {"cause": error.code}) from error
    return _validate_payload(payload, payload["authority_id"])


def make_epoch(log, epoch_id, activation_id, now):
    """Build an attempt against the current activation; the host supplies fresh UUIDs."""
    return _seal({"record_type": "mptask.epoch", "schema_version": 2,
                  "authority_id": log.authority_id, "epoch_id": epoch_id,
                  "activation_id": activation_id, "previous_activation_id": log.activation_id,
                  "at": now})


def make_proposal(log, command, now):
    normalized = normalize_command(command)
    event = decide(log.state, normalized, now)
    scope = {} if log.epoch_id is None else {"epoch_id": log.epoch_id}
    return _seal({"record_type": "mptask.command",
                  "schema_version": 1 if log.epoch_id is None else 2, **scope,
                  "authority_id": log.authority_id, "command_id": normalized["command_id"],
                  "command_hash": command_hash(normalized), "ordinal": log.domain_ordinal + 1,
                  "previous_event_id": log.domain_head, "event": event,
                  "task_ids": sorted(task["id"] for task in event["tasks"])})


def _control_id(authority_id, command_id, proposal_hash, epoch_id=None):
    scope = "" if epoch_id is None else f"{epoch_id}:"
    return str(uuid5(UUID(authority_id), f"mptask.settle:{scope}{command_id}:{proposal_hash}"))


def make_settlement(proposal):
    _validate_payload(proposal, proposal.get("authority_id") if type(proposal) is dict else None)
    _require(proposal["record_type"] == "mptask.command", "Settlement requires a proposal")
    scope = {} if proposal["schema_version"] == 1 else {"epoch_id": proposal["epoch_id"]}
    return _seal({"record_type": "mptask.settle",
                  "schema_version": proposal["schema_version"], **scope,
                  "authority_id": proposal["authority_id"],
                  "command_id": proposal["command_id"], "command_hash": proposal["command_hash"],
                  "proposal_hash": proposal["payload_hash"],
                  "task_ids": deepcopy(proposal["task_ids"]),
                  "control_id": _control_id(proposal["authority_id"], proposal["command_id"],
                                            proposal["payload_hash"], proposal.get("epoch_id"))})


def _validate_payload(payload, authority_id):
    try:
        encoded = canonical_json(payload)
        _require(type(payload) is dict, "Protocol payload must be an object")
        record_type = payload.get("record_type")
        _require(type(record_type) is str
                 and record_type in {"mptask.command", "mptask.settle", "mptask.epoch"},
                 "Unknown record type")
        version = payload.get("schema_version")
        _require(type(version) is int and version in (1, 2), "Unsupported protocol schema")
        if record_type == "mptask.epoch":
            _require(version == 2, "Epoch activation requires protocol v2")
            expected = _EPOCH
        else:
            expected = _PROPOSAL if record_type == "mptask.command" else _SETTLEMENT
            if version == 2:
                expected = expected | {"epoch_id"}
        _require(set(payload) == expected, "Missing or unknown protocol fields")
        identifier(payload["authority_id"], "authority_id")
        _require(payload["authority_id"] == authority_id, "Foreign authority")
        if version == 2:
            identifier(payload["epoch_id"], "epoch_id")
        _require(type(payload["payload_hash"]) is str
                 and _HASH.fullmatch(payload["payload_hash"]) is not None, "Malformed hash")
        _require(payload_hash(payload) == payload["payload_hash"], "Payload hash mismatch")
        _require(len(encoded.encode("utf-8")) <= MAX_PAYLOAD_BYTES, "Oversized protocol record")
        if record_type == "mptask.epoch":
            identifier(payload["activation_id"], "activation_id")
            if payload["previous_activation_id"] is not None:
                identifier(payload["previous_activation_id"], "previous_activation_id")
            instant(payload["at"], "activation time")
            return payload
        identifier(payload["command_id"], "command_id")
        _require(type(payload["command_hash"]) is str
                 and _HASH.fullmatch(payload["command_hash"]) is not None, "Malformed hash")
        ids = payload["task_ids"]
        _require(type(ids) is list and all(type(tid) is str for tid in ids), "Malformed task IDs")
        _require(ids == sorted(set(ids)), "Task IDs must be sorted and distinct")
        for tid in ids:
            _require(tid.startswith("tsk_"), "Malformed task ID")
            identifier(tid[4:], "task_id")
        if record_type == "mptask.command":
            _require(type(payload["ordinal"]) is int and payload["ordinal"] > 0, "Invalid ordinal")
            _require(payload["previous_event_id"] is None
                     or _routing_value(payload["previous_event_id"]), "Invalid predecessor")
            event = payload["event"]
            _require(type(event) is dict and set(event) == _EVENT, "Malformed domain event")
            _require(type(event["schema_version"]) is int and event["schema_version"] == 1
                     and event["authority_id"] == authority_id, "Foreign domain event")
            instant(event["at"], "event time")
            _require(type(event["kind"]) is str and bool(event["kind"]), "Invalid domain kind")
            for key in ("response", "resources", "changes"):
                _require(type(event[key]) is dict, "Invalid domain event object")
            for key in ("edges_added", "edges_removed"):
                _require(type(event[key]) is list and all(
                    type(edge) is dict and set(edge) == {"source", "target", "edge_type"}
                    and all(type(value) is str for value in edge.values()) for edge in event[key]),
                    "Invalid domain event edges")
            _require(event["configuration"] is None or type(event["configuration"]) is dict,
                     "Invalid domain configuration")
            _require(set(event["changes"]) == {
                "task_versions", "lease_revisions", "resource_counters", "resource_reservations"}
                and all(type(values) is list and all(type(value) is str for value in values)
                        for values in event["changes"].values()), "Invalid domain changes")
            normalized = normalize_command(event["command"])
            _require(canonical_json(normalized) == canonical_json(event["command"]),
                     "Noncanonical command operation")
            _require(normalized["command_id"] == payload["command_id"], "Command ID mismatch")
            _require(command_hash(normalized) == payload["command_hash"], "Command hash mismatch")
            _require(type(event["tasks"]) is list
                     and all(type(task) is dict and type(task.get("id")) is str
                             for task in event["tasks"]), "Malformed domain tasks")
            _require(sorted(task["id"] for task in event["tasks"]) == ids,
                     "Affected task IDs mismatch")
            _require(canonical_json(event["response"].get("tasks")) == canonical_json(event["tasks"]),
                     "Domain response snapshots mismatch")
        else:
            _require(type(payload["proposal_hash"]) is str
                     and _HASH.fullmatch(payload["proposal_hash"]) is not None,
                     "Malformed proposal hash")
            identifier(payload["control_id"], "control_id")
            _require(payload["control_id"] == _control_id(
                authority_id, payload["command_id"], payload["proposal_hash"],
                payload.get("epoch_id")),
                "Unstable settlement identity")
    except DomainError as error:
        raise ProtocolError("invariant_violation", "Invalid protocol value",
                            {"cause": error.code}) from error
    return payload


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(value):
    raise ProtocolError("invariant_violation", "Non-integral JSON number")


def _routing_value(value):
    return (type(value) is str and bool(value) and value == value.strip()
            and len(value) <= 256 and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _decode_raw(log, raw):
    try:
        canonical_json(raw)
        _require(type(raw) is dict, "Raw record must be an object")
        for key in ("id", "stream", "room", "type", "from_agent", "correlation_id", "body",
                    "metadata", "created_at"):
            _require(key in raw, "Incomplete raw record")
        for key in ("id", "stream", "room", "type", "from_agent", "correlation_id", "created_at"):
            _require(_routing_value(raw[key]), "Invalid raw routing value")
        _require(raw["id"] not in log.raw_ids, "Repeated raw event ID")
        _require(raw["stream"] == f"mptask/{log.authority_id}" and raw["room"] == "tasks"
                 and raw["from_agent"] == "mempalace-tasks", "Foreign route or writer")
        _require(raw.get("body_truncated", False) is False, "Truncated record")
        _require(type(raw["body"]) is str, "Raw body must be JSON text")
        _require(len(raw["body"].encode("utf-8")) <= MAX_PAYLOAD_BYTES, "Oversized raw body")
        payload = json.loads(raw["body"], object_pairs_hook=_pairs, parse_float=_invalid_number,
                             parse_constant=_invalid_number)
        _validate_payload(payload, log.authority_id)
        identity = "activation_id" if payload["record_type"] == "mptask.epoch" else "command_id"
        _require(raw["type"] == payload["record_type"]
                 and raw["correlation_id"] == payload[identity], "Raw routing mismatch")
        _require(type(raw["metadata"]) is dict
                 and raw["metadata"].get("authority_id") == log.authority_id
                 and raw["metadata"].get(identity) == payload[identity],
                 "Raw metadata mismatch")
        if payload["schema_version"] == 2:
            _require(raw["metadata"].get("epoch_id") == payload["epoch_id"],
                     "Raw epoch metadata mismatch")
        _require(len(canonical_json(raw["metadata"]).encode("utf-8")) <= 64 * 1024,
                 "Oversized raw metadata")
        return payload
    except (DomainError, ValueError, RecursionError) as error:
        raise ProtocolError("invariant_violation", "Invalid raw JSON record") from error


def _raw_digest(log, raw_event):
    return hashlib.sha256(canonical_json({
        "previous_raw_hash": log.raw_hash, "record": raw_event,
    }).encode("utf-8")).hexdigest()


def _advance_raw(log, raw_event, digest, history):
    log.raw_ids.add(raw_event["id"])
    log.raw_cursor, log.raw_hash = raw_event["id"], digest
    log.history.append(history)
    return log


def _fold_epoch(log, raw_event, payload):
    activation_id = payload["activation_id"]
    epoch_id = payload["epoch_id"]
    known = log.activation_attempts.get(activation_id)
    if known is not None:
        _require(canonical_json(known["payload"]) == canonical_json(payload),
                 "Activation ID reused with different content")
        disposition, attempt = "duplicate", known
    else:
        accepted = (payload["previous_activation_id"] == log.activation_id
                    and epoch_id not in log.used_epochs)
        disposition = "activated" if accepted else "stale"
        attempt = {"outcome": "accepted" if accepted else "stale",
                   "activation_id": activation_id, "epoch_id": epoch_id,
                   "event_id": raw_event["id"], "at": payload["at"],
                   "payload": deepcopy(payload)}
    digest = _raw_digest(log, raw_event)
    history = {"record_seq": len(log.history) + 1, "event_id": raw_event["id"],
               "record_type": payload["record_type"], "activation_id": activation_id,
               "epoch_id": epoch_id, "task_ids": [], "disposition": disposition,
               "outcome": attempt["outcome"], "payload": deepcopy(payload)}
    if known is None:
        log.activation_attempts[activation_id] = attempt
        log.used_epochs.setdefault(epoch_id, activation_id)
    if disposition == "activated":
        log.epoch_id, log.activation_id = epoch_id, activation_id
        log.activation_at, log.activation_event_id = payload["at"], raw_event["id"]
        log.outcomes, log.proposals, log.controls = {}, {}, {}
    return _advance_raw(log, raw_event, digest, history)


def fold_record(log, raw_event):
    payload = _decode_raw(log, raw_event)
    if payload["record_type"] == "mptask.epoch":
        return _fold_epoch(log, raw_event, payload)
    epoch_id = payload.get("epoch_id")
    command_id = payload["command_id"]
    if epoch_id != log.epoch_id:
        history = {"record_seq": len(log.history) + 1, "event_id": raw_event["id"],
                   "record_type": payload["record_type"], "command_id": command_id,
                   "epoch_id": epoch_id, "task_ids": deepcopy(payload["task_ids"]),
                   "disposition": "stale", "outcome": "stale", "payload": deepcopy(payload)}
        return _advance_raw(log, raw_event, _raw_digest(log, raw_event), history)
    known = log.outcomes.get(command_id)
    proposal = payload["record_type"] == "mptask.command"
    proposal_digest = payload["payload_hash"] if proposal else payload["proposal_hash"]
    if known is not None:
        _require((known["command_hash"], known["proposal_hash"], known["task_ids"])
                 == (payload["command_hash"], proposal_digest, payload["task_ids"]),
                 "Command ID reused with different content")

    encoded = canonical_json(payload)
    state = log.state
    outcome = None
    if proposal:
        previous = log.proposals.get(command_id)
        if previous is not None:
            _require(previous == encoded, "Changed duplicate proposal")
            disposition = "duplicate"
        elif known is not None and known["outcome"] == "abandoned":
            disposition = "abandoned"
        else:
            _require(payload["ordinal"] == log.domain_ordinal + 1
                     and payload["previous_event_id"] == log.domain_head,
                     "Broken accepted command chain")
            try:
                state = apply_event(log.state, payload["event"])
            except DomainError as error:
                raise ProtocolError("invariant_violation", "Invalid domain decision",
                                    {"cause": error.code}) from error
            disposition = "accepted"
            outcome = {"outcome": "committed", "command_id": command_id,
                       "command_hash": payload["command_hash"],
                       "proposal_hash": proposal_digest, "task_ids": deepcopy(payload["task_ids"]),
                       "event_id": raw_event["id"], "ordinal": payload["ordinal"],
                       "response": deepcopy(payload["event"]["response"]),
                       "command": deepcopy(payload["event"]["command"])}
    else:
        previous = log.controls.get(payload["control_id"])
        if previous is not None:
            _require(previous == encoded, "Changed duplicate settlement")
            disposition = "duplicate"
        else:
            disposition = "settled"
            if known is None:
                outcome = {"outcome": "abandoned", "command_id": command_id,
                           "command_hash": payload["command_hash"], "proposal_hash": proposal_digest,
                           "task_ids": deepcopy(payload["task_ids"]), "event_id": raw_event["id"],
                           "ordinal": None, "response": None, "command": None}

    raw_digest = _raw_digest(log, raw_event)
    history = {"record_seq": len(log.history) + 1, "event_id": raw_event["id"],
               "record_type": payload["record_type"], "command_id": command_id,
               "task_ids": deepcopy(payload["task_ids"]), "disposition": disposition,
               "outcome": (outcome or known)["outcome"], "payload": deepcopy(payload)}
    if epoch_id is not None:
        history["epoch_id"] = epoch_id
        if outcome is not None:
            outcome["authority_id"] = log.authority_id
            outcome["epoch_id"] = epoch_id
    log.state = state
    if outcome is not None:
        log.outcomes[command_id] = outcome
        log.scoped_outcomes[(log.authority_id, epoch_id, command_id)] = outcome
    if proposal:
        log.proposals[command_id] = encoded
    else:
        log.controls[payload["control_id"]] = encoded
    if disposition == "accepted":
        log.domain_head = raw_event["id"]
        log.domain_ordinal = payload["ordinal"]
        log.accepted_records.append({"event_id": raw_event["id"], "ordinal": payload["ordinal"],
                                     "event": history["payload"]["event"]})
    return _advance_raw(log, raw_event, raw_digest, history)
