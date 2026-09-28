"""Real owned workers with injected authorization time, never native OS claims."""

import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mempalace_tasks.execution import ExecutionError, OwnedProcess
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.runtime_clock import RuntimeClock
from mempalace_tasks.supervisor import HostSupervisor
from runtime_support import DomainPort, temporary_directory
from test_execution import profile


class ElapsedSource:
    def __init__(self, value=1000.0):
        self.value = value
        self.error = None
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            if self.error is not None:
                raise self.error
            return self.value

    def change(self, *, value=None, error=None):
        with self.lock:
            if value is not None:
                self.value = value
            self.error = error


class SupervisorClockTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.port = DomainPort()
        self.clock = self.port.clock_source
        self.elapsed = ElapsedSource()

    def host(self, *, pool_size=1, **options):
        maintenance = LeaseMaintenance(self.port, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(
            self.port, self.clock, maintenance, supervisor_id="sup", worker_id="worker",
            profiles=[profile(self.root / "work", "stubborn")],
            pool_size=pool_size, **options,
        )
        self.addCleanup(host.close)
        return host

    def worker(self, **options):
        task_id = self.port.create()
        self.port.claim(task_id, start=False)
        adapter = profile(self.root / task_id, "stubborn")
        workspace = adapter.prepare(self.port.state.tasks[task_id])
        process = OwnedProcess(adapter, workspace, **options)
        self.addCleanup(process.stop)
        return process

    def test_host_and_worker_share_injected_elapsed_deadline_basis(self):
        task_id = self.port.create()
        host = self.host(elapsed_time=self.elapsed)
        self.assertEqual(host.step()["started"], 1)
        process = host.workers[task_id].process
        self.assertIsInstance(process, OwnedProcess)
        self.assertIs(process.elapsed_time, host.elapsed_time)
        self.assertEqual(process._deadline, 1295)
        self.assertIsNone(process.observe())
        self.elapsed.change(value=1295)
        self.assertTrue(process.wait_stopped(timeout=2))
        self.assertTrue(process.deadline_expired)

    def test_default_host_uses_continuous_clock_without_mixing_monotonic_epoch(self):
        task_id = self.port.create()
        with patch("mempalace_tasks.supervisor.continuous_time_ns",
                   side_effect=lambda: int(self.elapsed() * 1_000_000_000)):
            host = self.host()
            self.assertEqual(host.step()["started"], 1)
            process = host.workers[task_id].process
            self.assertEqual(process._deadline, 1295)
            self.elapsed.change(value=1295)
            self.assertTrue(process.wait_stopped(timeout=2))
            self.assertTrue(process.deadline_expired)
            host.close()

    def test_suspend_consumes_authorization_despite_wall_clock_rollback(self):
        wall = [1790164800000000000]
        runtime = RuntimeClock(
            wall_time_ns=lambda: wall[0],
            continuous_time_ns=lambda: int(self.elapsed() * 1_000_000_000),
        )
        self.clock = SimpleNamespace(now=runtime.now, observe=runtime.observe)
        self.port = DomainPort(self.clock)
        task_id = self.port.create()
        host = self.host(elapsed_time=self.elapsed)
        self.assertEqual(host.step()["started"], 1)
        process = host.workers[task_id].process
        wall[0] -= 3_600_000_000_000
        self.elapsed.change(value=1300)
        self.assertTrue(process.wait_stopped(timeout=2), "No host/network turn may be required")
        self.assertTrue(process.deadline_expired)
        self.assertEqual(runtime.now(), "2026-09-23T12:05:00.000000Z")

    def test_watchdog_clock_failure_stops_but_preserves_clock_diagnostic(self):
        process = self.worker(deadline=1300, elapsed_time=self.elapsed)
        self.elapsed.change(error=OSError("clock unavailable"))
        self.assertTrue(process.wait_stopped(timeout=2))
        self.assertEqual(process.error.code, "clock_unavailable")
        self.assertIs(process.error, process.clock_error)
        self.assertFalse(process.deadline_expired)
        self.assertIsNotNone(process.process.returncode)
        self.assertTrue(process.stop())
        self.assertEqual(process.error.code, "clock_unavailable")
        self.elapsed.change(value=1500)
        with self.assertRaises(ExecutionError) as caught:
            process.confirm_deadline(1800)
        self.assertEqual(caught.exception.code, "clock_unavailable")
        self.assertEqual(process._deadline, 1300)

    def test_watchdog_regression_stops_without_treating_clock_error_as_stop_failure(self):
        process = self.worker(deadline=1300, elapsed_time=self.elapsed)
        self.elapsed.change(value=999)
        self.assertTrue(process.wait_stopped(timeout=2))
        self.assertEqual(process.error.code, "continuous_discontinuity")
        self.assertTrue(process.stopped)
        self.assertFalse(process.deadline_expired)
        self.assertTrue(process.stop())
        self.assertEqual(process.error.code, "continuous_discontinuity")

    def test_step_clock_failure_stops_all_work_without_reconciliation_mutations(self):
        tasks = [self.port.create(), self.port.create()]
        host = self.host(pool_size=2, elapsed_time=self.elapsed)
        self.assertEqual(host.step()["started"], 2)
        processes = [host.workers[task_id].process for task_id in tasks]
        commands = len(self.port.commands)
        self.elapsed.change(error=OSError("clock unavailable"))
        result = host.step()
        self.assertIn("clock_unavailable", [error["code"] for error in result["errors"]])
        self.assertTrue(all(process.stopped for process in processes))
        self.assertFalse(host.health()["ok"])
        self.assertEqual(len(self.port.commands), commands)
        self.elapsed.change(value=1001)
        result = host.step()
        self.assertIn("clock_unavailable", [error["code"] for error in result["errors"]])
        self.assertEqual(len(self.port.commands), commands)

    def test_host_observed_fault_is_visible_on_worker_before_watchdog_samples(self):
        task_id = self.port.create()
        host = self.host(elapsed_time=self.elapsed)
        self.assertEqual(host.step()["started"], 1)
        process = host.workers[task_id].process
        with process._condition:
            self.elapsed.change(error=OSError("clock unavailable"))
            result = host.step()
            self.assertIn("clock_unavailable", [error["code"] for error in result["errors"]])
            self.assertTrue(process.stopped)
            self.assertIsNotNone(process.clock_error)
            self.assertEqual(process.clock_error.code, "clock_unavailable")
            self.assertIs(process.error, process.clock_error)

    def test_clock_fault_does_not_hide_unconfirmed_stop_or_prevent_stop_retry(self):
        process = self.worker(deadline=1300, elapsed_time=self.elapsed)
        with patch.object(process, "_group_running", side_effect=OSError("enumeration unavailable")):
            self.elapsed.change(error=OSError("clock unavailable"))
            process._watchdog.join(timeout=2)
            self.assertFalse(process._watchdog.is_alive())
            self.assertFalse(process.stopped)
            self.assertIsInstance(process.error, OSError)
            self.assertEqual(process.clock_error.code, "clock_unavailable")
        self.assertTrue(process.stop())
        self.assertIs(process.error, process.clock_error)
        self.assertTrue(process.stopped)

    def test_failed_clock_cannot_be_laundered_by_renewal_confirmation(self):
        process = self.worker(deadline=1300, elapsed_time=self.elapsed)
        self.elapsed.change(value=999)
        with self.assertRaises(ExecutionError) as caught:
            process.confirm_deadline(1600)
        self.assertEqual(caught.exception.code, "continuous_discontinuity")
        self.assertTrue(process.stopped)
        self.assertEqual(process._deadline, 1300)

    def test_expired_clock_cannot_be_laundered_by_renewal_confirmation(self):
        process = self.worker(deadline=1300, elapsed_time=self.elapsed)
        self.elapsed.change(value=1300)
        with self.assertRaises(ExecutionError) as caught:
            process.confirm_deadline(1600)
        self.assertEqual(caught.exception.code, "lease_expired")
        self.assertTrue(process.stopped)
        self.assertEqual(process._deadline, 1300)

    def test_direct_owned_process_uses_explicit_monotonic_deadline(self):
        process = self.worker(deadline=time.monotonic() + 0.15)
        self.assertTrue(process.wait_stopped(timeout=2))
        self.assertTrue(process.deadline_expired)
        self.assertIsNone(process.error)

    def test_unsupported_supervisor_fails_before_claim_or_workspace_preparation(self):
        task_id = self.port.create()
        commands = len(self.port.commands)
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                with self.assertRaises(ExecutionError) as caught:
                    self.host()
                self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertEqual(len(self.port.commands), commands)
        self.assertEqual(self.port.state.tasks[task_id]["status"], "open")
        self.assertFalse((self.root / "work").exists())

    def test_missing_required_linux_api_rejects_supervisor_before_claim(self):
        self.port.create()
        commands = len(self.port.commands)
        with patch.object(os, "waitid", None):
            with self.assertRaises(ExecutionError) as caught:
                self.host()
        self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertEqual(len(self.port.commands), commands)
        self.assertFalse((self.root / "work").exists())

    def test_unreadable_proc_rejects_supervisor_before_claim(self):
        self.port.create()
        commands = len(self.port.commands)
        real_open = Path.open

        def unavailable(path, *args, **kwargs):
            if str(path) == "/proc/self/stat":
                raise PermissionError("Owned proc unavailable")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", unavailable):
            with self.assertRaises(ExecutionError) as caught:
                self.host()
        self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertEqual(len(self.port.commands), commands)
        self.assertFalse((self.root / "work").exists())
