"""Pure task domain. Transport, persistence, and host execution are separate."""

from .codec import canonical_json, command_hash, payload_hash, task_id_for
from .model import DomainError, State, new_state, state_from_dict, state_to_dict
from .domain import apply_event, decide, ready_tasks, task_eligibility

__all__ = [
    "DomainError", "State", "new_state", "state_to_dict", "state_from_dict",
    "decide", "apply_event", "task_eligibility", "ready_tasks",
    "canonical_json", "command_hash", "payload_hash", "task_id_for",
]
