"""Current epoch task-log transport fixture; no local authority recovery store."""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
from uuid import UUID

from mempalace_tasks.codec import canonical_json
from mempalace_tasks.palace import PalaceError
from mempalace_tasks.model import instant, utc


AUTHORITY = "11111111-1111-1111-1111-111111111111"
NOW = "2026-09-23T00:00:00Z"
EPOCH = "00000000-0000-0000-0000-0000000003e8"


def uid(number):
    return str(UUID(int=number))


def command(number, operation="create", actor="operator", **fields):
    return {"operation": operation, "command_id": uid(number), "actor": actor, **fields}


def genesis(number=1):
    return command(number, "authority_create", actors={
        "operator": "operator", "coordinator": "coordinator",
        "worker": "worker", "worker2": "worker", "supervisor": "supervisor",
        "system": "system",
    }, execution_profiles={"local": {"execution_class": "isolated"}},
        supervisors={"supervisor": {"profiles": ["local"], "workers": ["worker", "worker2"]}})


def create(number=2, **fields):
    return command(number, project="demo", kind="task", title=f"Task {number}",
                   description="A bounded task", acceptance="Verified output",
                   execution_class="isolated", execution_profile="local", **fields)


def raw(payload, index):
    identity = "activation_id" if payload["record_type"] == "mptask.epoch" else "command_id"
    return {"id": f"evt-{index:06}", "stream": f"mptask/{AUTHORITY}",
            "room": "tasks", "type": payload["record_type"],
            "from_agent": "mempalace-tasks", "to_agent": "*",
            "correlation_id": payload[identity], "body": canonical_json(payload),
            "metadata": {"authority_id": AUTHORITY, identity: payload[identity],
                         "epoch_id": payload["epoch_id"]},
            "artifact_ids": [], "branch": None, "base_commit": None, "status": None,
            "created_at": NOW}

def activate(log, *, index=0):
    from mempalace_tasks.protocol import fold_record, make_epoch
    marker = make_epoch(log, EPOCH, uid(2000), NOW)
    fold_record(log, raw(marker, index))
    return marker


class Clock:
    def __init__(self):
        self.value = NOW
        self._observed = NOW

    def now(self):
        return utc(max(instant(self.value), instant(self._observed)))

    def observe(self, timestamp):
        self._observed = utc(max(instant(timestamp), instant(self._observed)))

    __call__ = now


class LogClient:
    def __init__(self):
        self.events = []
        self.sent = []
        self.behaviors = []
        self.read_error = False
        self.discover_error = False
        self.on_append = None
        self.replay_cursors = []
        self.records_read = 0

    def discover(self):
        if self.discover_error:
            raise PalaceError("upstream_unavailable", "Unavailable")
        return {"profile": "mempalace-ordered-v1"}

    def append_event(self, payload):
        self.sent.append(deepcopy(payload))
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior in {"lost", "reject"}:
            raise PalaceError("upstream_unavailable", "Unavailable",
                              ambiguous=behavior == "lost")
        event = raw(payload, len(self.events) + 1)
        self.events.append(event)
        if self.on_append:
            self.on_append(payload)
        if behavior == "commit_lost":
            raise PalaceError("upstream_unavailable", "Response lost", ambiguous=True)
        return deepcopy(event)

    def replay_events(self, cursor=None):
        self.replay_cursors.append(cursor)
        if self.read_error:
            raise PalaceError("upstream_unavailable", "Unavailable")
        events = deepcopy(self.events)
        start = 0
        if cursor is not None:
            matches = [i for i, event in enumerate(events) if event["id"] == cursor]
            if not matches:
                raise PalaceError("unknown_cursor", "Cursor missing")
            start = matches[0] + 1
        for event in events[start:]:
            self.records_read += 1
            yield event


def state_directory():
    root = os.environ["MPTASK_TEST_TMPDIR"]
    return tempfile.TemporaryDirectory(prefix="authority-test-", dir=Path(root))
