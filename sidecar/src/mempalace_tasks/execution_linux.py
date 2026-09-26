"""Optional Linux containment for registered local managed-process profiles.

Only explicit supervision/preflight selects this backend. This is cooperative
process supervision, not a hostile-code sandbox or native Copilot adapter.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .execution import (
    DeadlineClock, ExecutionError, LocalProfile, OwnedProcess as PublicOwnedProcess, Workspace,
)
from .journal import JsonStore


def require_process_supervision():
    if (sys.platform != "linux"
            or not all(callable(getattr(os, name, None)) for name in ("waitid", "killpg", "setsid"))
            or not all(hasattr(os, name) for name in (
                "P_PID", "WEXITED", "WNOHANG", "WNOWAIT", "CLD_EXITED"))):
        raise ExecutionError("unsupported_platform", "Owned process supervision requires Linux waitid")
    try:
        with Path("/proc/self/stat").open("rb") as current:
            if not current.read(1):
                raise ExecutionError("unsupported_platform", "Owned process supervision requires readable proc")
        with os.scandir("/proc") as entries:
            if next(entries, None) is None:
                raise ExecutionError("unsupported_platform", "Owned process supervision requires readable proc")
    except OSError as error:
        raise ExecutionError("unsupported_platform", "Owned process supervision requires readable proc") from error


def _process_stat(pid):
    data = Path(f"/proc/{pid}/stat").read_bytes()
    fields = data[data.rindex(b")") + 2:].split()
    return fields[0].decode("ascii"), int(fields[2]), int(fields[19])


class OwnedProcess(PublicOwnedProcess):
    """Linux owned session/group with an independent confirmed-lease watchdog.

    Keep the group leader unreaped with waitid(WNOWAIT) until group stop, so
    its identity cannot be recycled between observing exit and signalling.
    No adoption of persisted PIDs and no name-based or arbitrary PID killing.
    This is cooperative process supervision, not a hostile-code sandbox.
    """
    def __init__(self, profile: LocalProfile, workspace: Workspace, *, deadline: float,
                 elapsed_time: Callable[[], float] | None = None):
        require_process_supervision()
        source = time.monotonic if elapsed_time is None else elapsed_time
        self.elapsed_time = source if isinstance(source, DeadlineClock) else DeadlineClock(source)
        self._deadline = deadline
        self._condition = threading.Condition()
        self._stop_lock = threading.Lock()
        self._stopped = threading.Event()
        self._stop_error = None
        self.deadline_expired = False
        task = JsonStore(workspace.input_path).read()
        environment = {**os.environ, "MPTASK_INPUT_PATH": str(workspace.input_path),
                       "MPTASK_RESULT_PATH": str(workspace.result_path),
                       "MPTASK_CHECKPOINT_PATH": str(workspace.checkpoint_path),
                       "MPTASK_TASK_ID": task["id"], "MPTASK_ATTEMPT_ID": task["attempt"]["id"],
                       "MPTASK_CLAIM_GENERATION": str(task["claim_generation"])}
        if deadline <= self.elapsed_time():
            raise ExecutionError("lease_expired", "No confirmed execution time remains")
        with (workspace.root / "worker.log").open("xb") as output:
            self.process = subprocess.Popen(profile.argv, cwd=workspace.working_directory,
                                            env=environment, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        try:
            _, group, start = _process_stat(self.process.pid)
            self.identity = (self.process.pid, group, start)
            self._watchdog = threading.Thread(target=self._watch, name="mptask-lease-watchdog", daemon=True)
            self._watchdog.start()
        except BaseException:
            # This newly created, unreaped child pins the session leader PID even
            # if /proc or thread creation fails before the identity is recorded.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=2)
            raise

    @property
    def stopped(self):
        """Confirmed owned-group quiescence, independent of clock diagnostics."""
        return self._stopped.is_set() and self._stop_error is None

    @property
    def clock_error(self):
        return self.elapsed_time.error

    @property
    def error(self):
        return self._stop_error or self.clock_error

    def confirm_deadline(self, deadline: float):
        failure = None
        with self._condition:
            try:
                now = self.elapsed_time()
            except ExecutionError as error:
                failure = error
            else:
                if self.deadline_expired or now >= min(self._deadline, deadline):
                    self.deadline_expired = True
                    failure = ExecutionError("lease_expired", "Confirmed execution time has ended")
                elif not self._stopped.is_set():
                    self._deadline = deadline
                    self._condition.notify_all()
        if failure is not None:
            self.stop()
            raise failure

    def observe(self):
        if self.process.returncode is not None:
            return self.process.returncode
        result = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status

    def _signal(self, value):
        pid, group, start = self.identity
        _, current_group, current_start = _process_stat(pid)
        if (current_group, current_start) != (group, start) or group != pid:
            raise ExecutionError("process_identity_changed", "Refusing to signal unverified execution")
        try:
            os.killpg(group, value)
        except ProcessLookupError:
            pass

    def _group_running(self):
        group = self.identity[1]
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    state, process_group, _ = _process_stat(int(entry.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if process_group == group and state not in {"Z", "X"}:
                    return True
        return False

    def stop(self, *, grace_seconds=0.1):
        if not 0 <= grace_seconds <= 5:
            raise ValueError("grace_seconds must be in 0..5")
        with self._stop_lock:
            if self._stopped.is_set():
                return self.stopped
            try:
                self._signal(signal.SIGTERM)
                end = time.monotonic() + grace_seconds
                try:
                    while time.monotonic() < end and self._group_running():
                        time.sleep(0.005)
                finally:
                    # Enumeration failure is not quiescence and must not prevent
                    # escalation against the still-verified, unreaped owned group.
                    self._signal(signal.SIGKILL)
                end = time.monotonic() + 2
                while time.monotonic() < end and self._group_running():
                    time.sleep(0.005)
                if self._group_running():
                    raise ExecutionError("stop_unconfirmed", "Owned group did not quiesce")
                self.process.wait(timeout=2)
                self._stop_error = None
                self._stopped.set()
            except Exception as error:
                self._stop_error = error
                raise
            finally:
                with self._condition:
                    self._condition.notify_all()
        return True

    def wait_stopped(self, timeout):
        return self._stopped.wait(timeout) and self.stopped

    def _watch(self):
        with self._condition:
            while not self._stopped.is_set():
                try:
                    remaining = self._deadline - self.elapsed_time()
                except ExecutionError:
                    # The shared clock retains the diagnostic independently of
                    # whether the physical stop below can establish quiescence.
                    break
                if remaining <= 0:
                    self.deadline_expired = True
                    break
                # Condition waits may exclude suspend; resample the authorization
                # clock promptly after resume, even with no supervisor/network turn.
                self._condition.wait(timeout=min(remaining, 0.1))
        if not self._stopped.is_set():
            try:
                self.stop()
            except Exception as error:
                self._stop_error = error
