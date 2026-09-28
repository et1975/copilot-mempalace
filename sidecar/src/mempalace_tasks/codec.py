"""Canonical encoding and deterministic, authority-scoped identifiers."""

import hashlib
import json
from uuid import UUID, uuid5

from .model import DomainError, identifier, require


def _json_value(value, active, depth):
    require(depth <= 64, "JSON nesting exceeds 64 levels")
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise DomainError("validation_error", "JSON contains invalid Unicode") from error
        return
    require(type(value) in (dict, list), "Only JSON values without floats are supported")
    require(id(value) not in active, "JSON must not contain cycles")
    active.add(id(value))
    if type(value) is dict:
        for key, item in value.items():
            require(type(key) is str, "JSON object keys must be strings")
            _json_value(key, active, depth + 1)
            _json_value(item, active, depth + 1)
    else:
        for item in value:
            _json_value(item, active, depth + 1)
    active.remove(id(value))


def canonical_json(value) -> str:
    _json_value(value, set(), 0)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def command_hash(command) -> str:
    require(type(command) is dict, "command must be an object")
    return hashlib.sha256(canonical_json(command).encode("utf-8")).hexdigest()


def payload_hash(payload) -> str:
    require(type(payload) is dict, "payload must be an object")
    return command_hash({key: value for key, value in payload.items() if key != "payload_hash"})


def task_id_for(authority_id, command_id) -> str:
    identifier(authority_id, "authority_id")
    identifier(command_id, "command_id")
    return "tsk_" + str(uuid5(UUID(authority_id), command_id))
