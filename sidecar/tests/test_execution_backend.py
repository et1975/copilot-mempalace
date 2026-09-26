"""Execution capability boundaries; simulated platforms are not native OS tests."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
import unittest
from unittest.mock import patch

from mempalace_tasks.execution import (
    ExecutionError, LocalProfile, OwnedProcess, read_checkpoint, read_result,
)
from mempalace_tasks.journal import JsonStore
from runtime_support import DomainPort, temporary_directory
from test_execution import profile


class ExecutionBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.port = DomainPort()
        self.task_id = self.port.create()
        self.port.claim(self.task_id, start=False)
        self.task = self.port.state.tasks[self.task_id]

    def fresh_interpreter(self, code):
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run(
            [sys.executable, "-W", "error", "-c", textwrap.dedent(code)],
            env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_core_and_public_execution_imports_do_not_require_linux_backend(self):
        self.fresh_interpreter("""
            import importlib.abc
            import sys

            class NoLinuxExecution(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "mempalace_tasks.execution_linux":
                        raise AssertionError("Core operations imported Linux execution")

            sys.meta_path.insert(0, NoLinuxExecution())
            from mempalace_tasks import config, server
            from mempalace_tasks.execution import LocalProfile, OwnedProcess
            from mempalace_tasks.supervisor import HostSupervisor
            from runtime_support import DomainPort

            assert config.validate_binding("127.0.0.1", 8766) == "127.0.0.1:8766"
            port = DomainPort()
            task_id = port.create()
            assert port.state.tasks[task_id]["status"] == "open"
            assert callable(server.create_app)
            assert "mempalace_tasks.execution_linux" not in sys.modules
        """)

    @unittest.skipUnless(sys.platform == "linux", "Linux process supervision only")
    def test_public_construction_lazily_selects_real_linux_owned_process(self):
        self.fresh_interpreter("""
            import sys
            import time
            from pathlib import Path
            from mempalace_tasks.execution import OwnedProcess
            from mempalace_tasks.supervisor import OwnedProcess as SupervisorProcess
            from runtime_support import DomainPort, temporary_directory
            from test_execution import profile

            assert SupervisorProcess is OwnedProcess
            assert "mempalace_tasks.execution_linux" not in sys.modules
            with temporary_directory() as directory:
                port = DomainPort()
                task_id = port.create()
                port.claim(task_id, start=False)
                adapter = profile(Path(directory) / "work")
                workspace = adapter.prepare(port.state.tasks[task_id])
                process = OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
                try:
                    assert isinstance(process, OwnedProcess)
                    assert "mempalace_tasks.execution_linux" in sys.modules
                    assert type(process).__module__ == "mempalace_tasks.execution_linux"
                    assert process.identity[:2] == (process.process.pid, process.process.pid)
                finally:
                    assert process.stop()
        """)

    def test_unsupported_hosts_fail_before_loading_backend_or_starting_worker(self):
        self.fresh_interpreter("""
            import importlib.abc
            from pathlib import Path
            import sys
            import time
            from unittest.mock import patch
            from mempalace_tasks.execution import ExecutionError, OwnedProcess
            from runtime_support import DomainPort, temporary_directory
            from test_execution import profile

            class NoLinuxExecution(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "mempalace_tasks.execution_linux":
                        raise AssertionError("Unsupported host imported Linux execution")

            sys.meta_path.insert(0, NoLinuxExecution())
            with temporary_directory() as directory:
                port = DomainPort()
                task_id = port.create()
                port.claim(task_id, start=False)
                adapter = profile(Path(directory) / "work")
                workspace = adapter.prepare(port.state.tasks[task_id])
                for platform in ("darwin", "win32", "freebsd14"):
                    with patch.object(sys, "platform", platform), patch(
                        "subprocess.Popen", side_effect=AssertionError("Worker must not start")
                    ):
                        try:
                            OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
                        except ExecutionError as error:
                            assert error.code == "unsupported_platform", error.code
                        else:
                            raise AssertionError("Unsupported supervision did not fail")
                    assert not (workspace.root / "worker.log").exists()
            assert "mempalace_tasks.execution_linux" not in sys.modules
        """)

    def test_profile_and_artifact_validation_do_not_require_process_supervision(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                adapter = profile(self.root / platform)
                workspace = adapter.prepare(self.task)
                identity = {
                    "schema_version": 1, "task_id": self.task_id,
                    "attempt_id": self.task["attempt"]["id"], "claim_generation": 1,
                }
                result = {**identity, "summary": "verified", "evidence": ["artifact:tests"]}
                checkpoint = {**identity, "sequence": 1, "reference": "artifact:checkpoint"}
                JsonStore(workspace.result_path).write(result)
                JsonStore(workspace.checkpoint_path).write(checkpoint)
                self.assertEqual(read_result(workspace, self.task), result)
                self.assertEqual(read_checkpoint(workspace, self.task), checkpoint)
                JsonStore(workspace.result_path).write({**result, "claim_generation": 2})
                with self.assertRaises(ExecutionError) as caught:
                    read_result(workspace, self.task)
                self.assertEqual(caught.exception.code, "invalid_artifact")
                with self.assertRaises(ValueError):
                    LocalProfile("bad", ("relative-worker",), self.root)

    @unittest.skipUnless(sys.platform == "linux", "Linux capability checks only")
    def test_missing_linux_waitid_fails_before_worker_side_effects(self):
        adapter = profile(self.root / "work")
        workspace = adapter.prepare(self.task)
        with patch.object(os, "waitid", None), patch(
            "subprocess.Popen", side_effect=AssertionError("Worker must not start")
        ):
            with self.assertRaises(ExecutionError) as caught:
                OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
        self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertFalse((workspace.root / "worker.log").exists())
