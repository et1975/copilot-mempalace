"""Runtime fixtures: real domain transitions, deterministic time, owned files."""
import copy
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from mempalace_tasks.domain import apply_event, decide
from mempalace_tasks.model import new_state


def temporary_directory():
    root = os.environ["MPTASK_TEST_TMPDIR"]
    return tempfile.TemporaryDirectory(prefix="mptask-runtime-", dir=root)


class ManualClock:
    def __init__(self):
        self.seconds = 0
        self.pending_reboot = False

    def now(self):
        return (datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
                + timedelta(seconds=self.seconds)).isoformat().replace("+00:00", "Z")

    def acknowledge_reboot(self, *, active_attempts):
        if active_attempts != 0:
            raise ValueError("active attempts")
        self.pending_reboot = False


class PortError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DomainPort:
    def __init__(self, clock=None):
        self.clock_source = clock or ManualClock()
        self._state = new_state(str(uuid4()))
        self.lock = threading.RLock()
        self.commands = []
        self.outcomes = {}
        self.before_execute = None
        self.reconcile_error = None
        self.send("authority_create", actors={
            "op": "operator", "recovery": "operator", "sys": "system",
            "sup": "supervisor", "worker": "worker",
        }, execution_profiles={
            "local": {"execution_class": "isolated"},
            "shared": {"execution_class": "shared_unfenced", "supports_reconciliation": True},
            "fenced": {"execution_class": "resource_fenced", "supports_reconciliation": True,
                       "supports_fencing": True},
        }, supervisors={"sup": {"profiles": ["local", "shared", "fenced"],
                               "workers": ["worker"]}})

    @property
    def state(self):
        return copy.deepcopy(self._state)

    def serialized(self):
        return self.lock

    def execute(self, command):
        with self.lock:
            self.commands.append(copy.deepcopy(command))
            if self.before_execute:
                self.before_execute(command)
            if command["command_id"] in self.outcomes:
                return copy.deepcopy(self.outcomes[command["command_id"]])
            event = decide(self._state, command, self.clock_source.now())
            self._state = apply_event(self._state, event)
            result = {"ok": True, "outcome": "committed", "command_id": command["command_id"],
                      **event["response"]}
            self.outcomes[command["command_id"]] = result
            return copy.deepcopy(result)

    def reconcile(self):
        if self.reconcile_error:
            raise self.reconcile_error
        return {"ok": True}

    def health(self):
        reboot = self.clock_source.pending_reboot
        error = None if self.reconcile_error is None else {"code": self.reconcile_error.code}
        return {"ok": error is None and not reboot, "fresh": error is None and not reboot,
                "pending_command": None, "pending_reboot": reboot, "error": error,
                "reason": error["code"] if error else "reboot_pending" if reboot else None}

    def send(self, operation, actor="op", **fields):
        return self.execute({"operation": operation, "actor": actor,
                             "command_id": str(uuid4()), **fields})

    def create(self, profile="local", **fields):
        execution_class = {"local": "isolated", "shared": "shared_unfenced",
                           "fenced": "resource_fenced"}[profile]
        return self.send("create", project="project", kind="task", title="Task",
                         description="", acceptance="verified",
                         execution_class=execution_class, execution_profile=profile,
                         **fields)["task_id"]

    def tokens(self, task_id):
        task = self.state.tasks[task_id]
        return {"task_id": task_id, "expected_version": task["version"],
                "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"]}

    def claim(self, task_id, start=True):
        task = self.state.tasks[task_id]
        self.send("claim", actor="sup", task_id=task_id, expected_version=task["version"],
                  supervisor_id="sup", worker_id="worker")
        if start:
            task = self.state.tasks[task_id]
            evidence = {"references": ["artifact:prepared"], "prepared": True}
            if task["execution_class"] == "resource_fenced":
                evidence["resource_fences"] = task["attempt"]["resource_fences"]
            self.send("attempt_report", actor="sup", **self.tokens(task_id),
                      report_kind="started", evidence=evidence)


class RealAuthorityFixture(DomainPort):
    """Use helper commands with the actual authority/protocol/journal underneath."""
    def __init__(self, directory, clock=None):
        from authority_fixture import AUTHORITY, LogClient
        from mempalace_tasks.authority import TaskAuthority
        self.clock_source = clock or ManualClock()
        self.client = LogClient()
        self.authority = TaskAuthority(AUTHORITY, self.client, directory,
                                       clock=self.clock_source.now, backoff=lambda _: None)
        self.authority.start(clock_factory=lambda: self.clock_source)
        self.authority.execute(DomainPort(self.clock_source).commands[0])

    @property
    def state(self):
        return self.authority.state

    def execute(self, command):
        return self.authority.execute(command)

    def serialized(self):
        return self.authority.serialized()

    def reconcile(self):
        return self.authority.reconcile()

    def health(self):
        return self.authority.health()
