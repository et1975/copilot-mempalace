"""JSON-native domain state and strict, shared value validation."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import UUID


class DomainError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(details) if details is not None else {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": deepcopy(self.details)}


def require(condition, message, code="validation_error", **details):
    if not condition:
        raise DomainError(code, message, details)


def object_fields(value, allowed, required=(), name="object"):
    require(type(value) is dict, f"{name} must be an object")
    require(set(value) <= set(allowed), f"Unknown {name} fields",
            fields=sorted(set(value) - set(allowed)))
    require(set(required) <= set(value), f"Missing {name} fields",
            fields=sorted(set(required) - set(value)))
    return value


def text(value, name, maximum=256, *, empty=False, byte_limit=False):
    require(type(value) is str, f"{name} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DomainError("validation_error", f"{name} must be valid Unicode") from error
    require(empty or bool(value.strip()), f"{name} must not be empty")
    require((len(encoded) if byte_limit else len(value)) <= maximum, f"{name} exceeds limit")
    return value


def integer(value, name, minimum=0, maximum=2**63 - 1):
    require(type(value) is int and minimum <= value <= maximum,
            f"{name} must be an integer from {minimum} to {maximum}")
    return value


def identifier(value, name):
    text(value, name, 36)
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise DomainError("validation_error", f"{name} must be a UUID") from error
    require(str(parsed) == value, f"{name} must be a canonical UUID")
    return value


_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)")


def instant(value, name="now"):
    require(type(value) is str and _UTC.fullmatch(value) is not None,
            f"{name} must be an explicit UTC timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DomainError("validation_error", f"Invalid {name}") from error


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class State:
    authority_id: str
    tasks: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    configuration: dict | None = None
    last_event_at: str | None = None

    def __post_init__(self):
        identifier(self.authority_id, "authority_id")


def new_state(authority_id):
    return State(authority_id)


def state_to_dict(state):
    require(isinstance(state, State), "Expected State")
    return deepcopy({"authority_id": state.authority_id, "tasks": state.tasks,
                     "edges": state.edges, "resources": state.resources,
                     "configuration": state.configuration, "last_event_at": state.last_event_at})


def state_from_dict(data):
    from .codec import canonical_json
    from .domain import validate_state

    canonical_json(data)
    fields = {"authority_id", "tasks", "edges", "resources", "configuration", "last_event_at"}
    object_fields(data, fields, fields, "state")
    state = State(**deepcopy(data))
    validate_state(state)
    return state
