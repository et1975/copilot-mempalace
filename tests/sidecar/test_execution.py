import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mempalace_tasks.execution import (
    ExecutionError, LocalProfile, OwnedProcess, read_checkpoint, read_result,
)
from mempalace_tasks.journal import JsonStore
from runtime_support import DomainPort, temporary_directory


WORKER = str(Path(__file__).with_name("runtime_worker.py"))


def profile(root, mode="sleep", **options):
    return LocalProfile("local", (sys.executable, WORKER, mode), Path(root), **options)


class FakeTarget:
    """An actual in-memory fencing target: commits check RESOURCE-global tokens."""
    def __init__(self):
        self.tokens = {}
        self.effects = []

    def install(self, fences):
        for key, value in fences.items():
            if value < self.tokens.get(key, 0):
                raise ExecutionError("stale_fence", "Old fence rejected")
            self.tokens[key] = value
        return ["target:installed"]

    def commit(self, resource, token, value):
        if token != self.tokens.get(resource):
            raise ExecutionError("stale_fence", "Commit rejected")
        self.effects.append(value)

    def reconcile(self, task, workspace, *, process_stopped, recovering, required_fences):
        if recovering:
            self.install(required_fences)
        return {"references": ["target:reconciled"], "effects_reconciled": True,
                **({"installed_fences": required_fences} if recovering else {})}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.port = DomainPort()
        self.tid = self.port.create()
        self.port.claim(self.tid, start=False)
        self.task = self.port.state.tasks[self.tid]

    def test_private_attempt_workspace_preserves_checkpoint_and_rejects_reuse(self):
        self.task["attempt"]["checkpoint"] = {"sequence": 4, "reference": "artifact:old",
                                               "at": self.port.clock_source.now()}
        adapter = profile(self.root / "work")
        workspace = adapter.prepare(self.task)
        stored = JsonStore(workspace.input_path).read()
        self.assertEqual(stored["attempt"]["checkpoint"]["sequence"], 4)
        self.assertTrue(workspace.evidence["prepared"])
        self.assertNotEqual(workspace.root, workspace.working_directory)
        with self.assertRaises(ExecutionError):
            adapter.prepare(self.task)

    def test_repo_worktree_starts_at_explicit_immutable_base_without_touching_source(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args],
                                           stderr=subprocess.STDOUT, text=True).strip()
        git("init", "-q")
        git("config", "user.name", "Runtime Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "tracked").write_text("base", encoding="utf-8")
        git("add", "tracked")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD")
        (repo / "tracked").write_text("uncommitted source", encoding="utf-8")
        workspace = profile(self.root / "work", repository=repo, base_commit=base).prepare(self.task)
        self.assertEqual((workspace.working_directory / "tracked").read_text(), "base")
        self.assertEqual((repo / "tracked").read_text(), "uncommitted source")
        self.assertEqual(git("worktree", "list", "--porcelain").count("worktree "), 2)
        with self.assertRaises(ValueError):
            profile(self.root / "bad", repository=repo, base_commit="HEAD")
        with self.assertRaises(ValueError):
            profile(self.root / "bad", repository=repo)

    def test_unsafe_paths_and_symlink_root_are_rejected_before_publication(self):
        outside = self.root / "outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ExecutionError, ValueError)):
            profile(alias).prepare(self.task)
        self.assertEqual(list(outside.iterdir()), [])
        self.task["attempt"]["id"] = "../../outside"
        with self.assertRaises(ExecutionError):
            profile(self.root / "safe").prepare(self.task)

    def test_owned_process_group_is_stopped_including_stubborn_descendants(self):
        workspace = profile(self.root / "work", "descendant").prepare(self.task)
        process = OwnedProcess(profile(self.root / "work", "descendant"), workspace,
                               deadline=time.monotonic() + 20)
        self.addCleanup(process.stop)
        # waitid leaves the dead leader owned/unreaped, preserving group identity.
        deadline = time.monotonic() + 2
        while process.observe() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(process.observe(), 0)
        self.assertTrue(process.stop(grace_seconds=0.02))
        self.assertTrue(process.stopped)
        self.assertTrue(process.stop())

    def test_watchdog_stops_without_supervisor_turn_or_network_reply(self):
        adapter = profile(self.root / "work", "stubborn")
        process = OwnedProcess(adapter, adapter.prepare(self.task),
                               deadline=time.monotonic() + 0.15)
        self.addCleanup(process.stop)
        self.assertTrue(process.wait_stopped(timeout=2))
        self.assertIsNotNone(process.process.returncode)

    def test_non_ascii_unrelated_process_name_cannot_break_owned_group_stop(self):
        unrelated_adapter = profile(self.root / "unrelated", "unicode-stubborn")
        unrelated_workspace = unrelated_adapter.prepare(self.task)
        unrelated = subprocess.Popen(
            unrelated_adapter.argv, cwd=unrelated_workspace.working_directory,
            env={**os.environ, "MPTASK_INPUT_PATH": str(unrelated_workspace.input_path)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.addCleanup(unrelated.wait, timeout=3)
        self.addCleanup(unrelated.kill)
        deadline = time.monotonic() + 3
        while not (unrelated_workspace.working_directory / "worker-ready").exists():
            self.assertLess(time.monotonic(), deadline, "Unrelated owned fixture did not become ready")
            time.sleep(0.005)
        self.assertIn("ö".encode(), Path(f"/proc/{unrelated.pid}/stat").read_bytes())
        adapter = profile(self.root / "owned", "stubborn")
        workspace = adapter.prepare(self.task)
        process = OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
        self.addCleanup(process.stop)
        deadline = time.monotonic() + 3
        while not (workspace.working_directory / "worker-ready").exists():
            self.assertLess(time.monotonic(), deadline, "Owned fixture did not become ready")
            time.sleep(0.005)
        try:
            self.assertTrue(process.stop(grace_seconds=0.02))
            self.assertEqual(process.process.returncode, -signal.SIGKILL)
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(timeout=3)

    def test_proc_enumeration_failure_cannot_skip_verified_group_kill(self):
        adapter = profile(self.root / "owned", "stubborn")
        workspace = adapter.prepare(self.task)
        process = OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
        self.addCleanup(process.stop)
        deadline = time.monotonic() + 3
        while not (workspace.working_directory / "worker-ready").exists():
            self.assertLess(time.monotonic(), deadline, "Owned fixture did not become ready")
            time.sleep(0.005)
        with patch.object(process, "_group_running", side_effect=OSError("proc enumeration unavailable")):
            with self.assertRaises(OSError):
                process.stop(grace_seconds=0.02)
            deadline = time.monotonic() + 1
            while process.observe() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(process.observe(), -signal.SIGKILL)
            self.assertFalse(process.stopped, "Enumeration failure must not attest quiescence")
            self.assertIsNotNone(process.error)
        self.assertTrue(process.stop())

    def test_watchdog_enumeration_failure_still_kills_without_claiming_quiescence(self):
        adapter = profile(self.root / "owned", "stubborn")
        workspace = adapter.prepare(self.task)
        process = OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
        self.addCleanup(process.stop)
        deadline = time.monotonic() + 3
        while not (workspace.working_directory / "worker-ready").exists():
            self.assertLess(time.monotonic(), deadline, "Owned fixture did not become ready")
            time.sleep(0.005)
        with patch.object(process, "_group_running", side_effect=OSError("proc enumeration unavailable")):
            process.confirm_deadline(time.monotonic() + 0.05)
            process._watchdog.join(timeout=2)
            self.assertFalse(process._watchdog.is_alive())
            deadline = time.monotonic() + 1
            while process.observe() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(process.observe(), -signal.SIGKILL)
            self.assertFalse(process.stopped)
            self.assertIsNotNone(process.error)
        self.assertTrue(process.stop())

    def test_result_requires_current_identity_explicit_acceptance_and_strict_shape(self):
        workspace = profile(self.root / "work").prepare(self.task)
        self.assertIsNone(read_result(workspace, self.task))
        value = {"schema_version": 1, "task_id": self.tid,
                 "attempt_id": self.task["attempt"]["id"],
                 "claim_generation": 1, "summary": "verified", "evidence": ["artifact:tests"]}
        JsonStore(workspace.result_path).write(value)
        self.assertEqual(read_result(workspace, self.task)["summary"], "verified")
        for patch in ({"claim_generation": 0}, {"attempt_id": "old"}, {"evidence": []},
                      {"claim_generation": True}, {"summary": ""}, {"extra": "ignored?"}):
            with self.subTest(patch=patch):
                JsonStore(workspace.result_path).write({**value, **patch})
                with self.assertRaises(ExecutionError):
                    read_result(workspace, self.task)
        workspace.result_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(Exception):
            read_result(workspace, self.task)

    def test_checkpoint_shape_does_not_mistake_output_for_progress(self):
        workspace = profile(self.root / "work").prepare(self.task)
        self.assertIsNone(read_checkpoint(workspace, self.task))
        value = {"schema_version": 1, "task_id": self.tid,
                 "attempt_id": self.task["attempt"]["id"], "claim_generation": 1,
                 "sequence": 1, "reference": "artifact:checkpoint"}
        JsonStore(workspace.checkpoint_path).write(value)
        self.assertEqual(read_checkpoint(workspace, self.task)["sequence"], 1)
        JsonStore(workspace.checkpoint_path).write({**value, "sequence": True})
        with self.assertRaises(ExecutionError):
            read_checkpoint(workspace, self.task)

    def test_remote_or_unimplemented_profiles_fail_closed(self):
        with self.assertRaises(ValueError):
            profile(self.root / "remote", execution_class="shared_unfenced")
        with self.assertRaises(ValueError):
            profile(self.root / "fenced", execution_class="resource_fenced", local_only=True)
        with self.assertRaises(ValueError):
            profile(self.root / "bad", execution_class="native_fleet")
        with self.assertRaises(ValueError):
            profile(self.root / "unimplemented", execution_class="resource_fenced",
                    reconciler=object(), fence_target=object())

    def test_missing_repository_is_not_created_by_preparation(self):
        absent = self.root / "missing-repository"
        adapter = profile(self.root / "work", repository=absent, base_commit="a" * 40)
        with self.assertRaises(ExecutionError):
            adapter.prepare(self.task)
        self.assertFalse(absent.exists())

    def test_settlement_does_not_falsely_claim_isolated_publication_is_revoked(self):
        adapter = profile(self.root / "work")
        workspace = adapter.prepare(self.task)
        proof = adapter.settlement(self.task, workspace, process_stopped=True)
        self.assertNotIn("publication_revoked", proof)
        with self.assertRaises(ExecutionError):
            adapter.recovery(self.task, workspace, process_stopped=True, required_fences={})

    def test_failed_identity_check_does_not_prevent_a_verified_stop_retry(self):
        adapter = profile(self.root / "work")
        process = OwnedProcess(adapter, adapter.prepare(self.task),
                               deadline=time.monotonic() + 20)
        self.addCleanup(process.process.wait, timeout=3)
        self.addCleanup(process.process.kill)
        self.addCleanup(process.stop)
        with patch("mempalace_tasks.execution_linux._process_stat", return_value=("S", 999, 999)):
            with self.assertRaises(ExecutionError):
                process.stop()
        self.assertIsNone(process.observe())
        self.assertTrue(process.stop())
        self.assertIsNotNone(process.process.returncode)

    def test_process_setup_failure_does_not_leave_a_live_owned_child(self):
        adapter = profile(self.root / "work")
        workspace = adapter.prepare(self.task)
        children = []
        original = subprocess.Popen
        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            self.addCleanup(child.wait, timeout=3)
            self.addCleanup(child.kill)
            return child
        with patch("mempalace_tasks.execution_linux.subprocess.Popen", side_effect=capture):
            with patch("mempalace_tasks.execution_linux._process_stat", side_effect=OSError("stat unavailable")):
                with self.assertRaises(OSError):
                    OwnedProcess(adapter, workspace, deadline=time.monotonic() + 20)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_local_quiescence_is_limited_to_declared_owned_local_effects(self):
        adapter = profile(self.root / "work", execution_class="shared_unfenced",
                          local_only=True)
        workspace = adapter.prepare(self.task)
        with self.assertRaises(ExecutionError):
            adapter.settlement(self.task, workspace, process_stopped=False)
        evidence = adapter.settlement(self.task, workspace, process_stopped=True)
        self.assertTrue(evidence["process_stopped"])
        self.assertTrue(evidence["effects_reconciled"])

    def test_resource_fences_are_global_across_task_ids_at_commit(self):
        target = FakeTarget()
        first = self.port.create("fenced", resource_keys=["shared-db"])
        self.port.claim(first, start=False)
        old = self.port.state.tasks[first]
        adapter = profile(self.root / "fences", execution_class="resource_fenced",
                          reconciler=target, fence_target=target)
        workspace = adapter.prepare(old)
        token = old["attempt"]["resource_fences"]["shared-db"]
        target.commit("shared-db", token, "old-valid")
        proof = adapter.recovery(old, workspace, process_stopped=False,
                                 required_fences={"shared-db": token + 1})
        self.assertEqual(proof["installed_fences"], {"shared-db": token + 1})
        second = self.port.create("fenced", resource_keys=["shared-db"])
        self.port.send("release", actor="sup", **self.port.tokens(first), reason="yield")
        self.port.send("recover", actor="sup", task_id=first,
                       expected_version=self.port.state.tasks[first]["version"],
                       retry_decision="preserve", evidence=proof)
        self.port.claim(second, start=False)
        fresh = self.port.state.tasks[second]
        adapter.prepare(fresh)
        self.assertEqual(fresh["attempt"]["resource_fences"]["shared-db"], token + 2)
        with self.assertRaises(ExecutionError):
            target.commit("shared-db", token, "old-stale")
        target.commit("shared-db", token + 2, "new-valid")
        self.assertEqual(target.effects, ["old-valid", "new-valid"])
