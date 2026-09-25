"""Portable CLI consumers against disposable configuration and journal sources."""

from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from authority_fixture import genesis
from mempalace_tasks import cli, launcher
from mempalace_tasks.client import TaskClientError, TaskServiceClient
from mempalace_tasks.discovery import DiscoveryError, connect
from mempalace_tasks.platform_support import LifetimeLock
from portable_lifecycle_fixture import ConfigurationFixture, ROOT_TOKEN
from test_journal_authority import JournalClient
from test_palace import TestHub


class PortableCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ConfigurationFixture()
        self.addCleanup(self.fixture.close)
        self.log = JournalClient()

    def call(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), patch.object(
                cli, "PalaceClient", return_value=self.log):
            code = cli.main(["--config", str(self.fixture.path), *args])
        self.assertNotIn(ROOT_TOKEN, output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_init_uses_only_journal_and_lifetime_lock(self):
        with patch.object(cli, "JsonStore", side_effect=AssertionError("legacy store")):
            result = self.call("init")
        self.assertEqual(result[0], 0, result)
        self.assertEqual([row["record_type"] for row in self.log.sent],
                         ["mptask.epoch", "mptask.command"])
        self.assertEqual({p.name for p in self.fixture.config.runtime_dir.iterdir()},
                         {"authority.lock"})
        self.assertEqual(json.loads(result[1])["outcome"], "committed")

    def test_restart_ignores_future_local_files_without_rewriting_them(self):
        self.assertEqual(self.call("init")[0], 0)
        paths = [self.fixture.config.runtime_dir / name for name in
                 ("clock.json", "pending.json", "verified_head.json", "projection.json")]
        for path in paths:
            path.write_bytes(b"future-not-json")
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(self.fixture.config) as owner:
                self.assertEqual(owner.recovery_mode, "journal")
                self.assertTrue(cli._maintenance(owner, self.fixture.config).tick()["errors"] == [])
                self.assertTrue(owner.health()["fresh"])
        self.assertTrue(all(path.read_bytes() == b"future-not-json" for path in paths))

    def test_missing_journal_is_not_initialized_without_activation(self):
        result = self.call("serve")
        self.assertEqual(result[0], 1, result)
        self.assertIn("not_initialized", result[2])
        self.assertEqual(self.log.sent, [])

    def test_configuration_conflict_precedes_activation(self):
        self.assertEqual(self.call("init")[0], 0)
        before = deepcopy(self.log.sent)
        self.fixture.document["genesis"]["policy"] = {"sweep_seconds": 1}
        self.fixture.save()
        result = self.call("init")
        self.assertEqual(result[0], 1, result)
        self.assertIn("configuration_conflict", result[2])
        self.assertEqual(self.log.sent, before)

    def test_enabled_projection_refused_before_any_authority_writes(self):
        self.fixture.document.update(projections_enabled=True, project_wings={"demo": "demo"})
        self.fixture.save()
        result = self.call("serve")
        self.assertEqual(result[0], 2, result)
        self.assertIn("projection_unsupported", result[2])
        self.assertEqual(self.log.sent, [])
        self.assertFalse(self.fixture.config.runtime_dir.exists())

    def test_explicit_project_refused_in_journal_mode(self):
        result = self.call("project", "--resume")
        self.assertEqual(result[0], 2, result)
        self.assertIn("projection_unsupported", result[2])
        self.assertFalse(self.fixture.config.runtime_dir.exists())

    def test_inspect_and_connect_never_start_an_absent_service(self):
        for operation in ("inspect", "status", "list", "connect"):
            with self.subTest(operation=operation):
                result = self.call(operation)
                self.assertEqual(result[0], 1, result)
                self.assertIn("registry_missing", result[2])
                self.assertFalse(self.fixture.config.runtime_dir.exists())
                self.assertEqual(self.log.sent, [])

    def test_init_again_is_verified_not_another_genesis(self):
        self.assertEqual(self.call("init")[0], 0)
        result = self.call("init")
        self.assertEqual(result[0], 0, result)
        self.assertTrue(json.loads(result[1])["already_initialized"])
        self.assertEqual(sum(row["record_type"] == "mptask.command" for row in self.log.sent), 1)


class PortableChildCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ConfigurationFixture()
        self.addCleanup(self.fixture.close)
        self.hub = self.enterContext(TestHub())
        initial = genesis()
        self.fixture.document.update(
            hub_url=self.hub.url,
            genesis={key: value for key, value in initial.items()
                     if key not in {"operation", "command_id"}},
        )
        self.fixture.save()
        self.children_before = set(launcher._children)
        self.addCleanup(self.close_children)

    def close_children(self):
        children = set(launcher._children) - self.children_before
        if children:
            try:
                info = connect(self.fixture.config)
                launcher.stop(self.fixture.config, expected_instance_id=info.instance_id)
            except DiscoveryError:
                # These are retained owned Popen handles, never registry PIDs.
                for child in children:
                    if child.poll() is None:
                        child.terminate()
            for child in children:
                child.wait(timeout=8)

    def call(self, *args, child=False):
        argv = ["--config", str(self.fixture.path), *args]
        if child:
            result = subprocess.run([sys.executable, "-W", "error", "-m", "mempalace_tasks", *argv],
                                    capture_output=True, text=True, timeout=20, check=False)
            value = result.returncode, result.stdout, result.stderr
        else:
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(argv)
            value = code, output.getvalue(), errors.getvalue()
        self.assertNotIn(ROOT_TOKEN, value[1] + value[2])
        return value

    def test_child_init_start_mcp_graph_stop_restart_same_journal_and_ids(self):
        result = self.call("init", child=True)
        self.assertEqual(result[0], 0, result)
        result = self.call("start", "--timeout", "5s")
        self.assertEqual(result[0], 0, result)
        info = connect(self.fixture.config)
        self.assertEqual(json.loads(result[1]), info.public())
        self.assertNotIn(info.token, result[1] + result[2])
        client = TaskServiceClient(info.url, token=info.token)
        request = {
            "actor": "operator", "command_id": str(uuid4()), "expected_epoch": info.instance_id,
            "project": "demo", "title": "Owned portable goal", "description": "", "acceptance": "",
            "planning_task": {"title": "Plan", "description": "", "acceptance": "",
                              "execution_class": "isolated", "execution_profile": "local"},
            "goal_policy": {"scope": "disposable lifecycle test"},
        }
        bootstrap = client.call_tool("mptask_bootstrap", request)
        task_id = bootstrap["planning_task_id"]
        client.call_tool("mptask_note", {
            "actor": "operator", "command_id": str(uuid4()), "expected_epoch": info.instance_id,
            "task_id": task_id, "expected_version": 1, "tag": "note", "text": "Survives restart",
        })
        expanded = client.call_tool("mptask_expand", {
            "actor": "operator", "command_id": str(uuid4()), "expected_epoch": info.instance_id,
            "goal_id": bootstrap["goal_id"], "expected_graph_revision": 1,
            "source_disposition": "continue", "tasks": [{
                "intent_key": "portable-child", "title": "Child", "description": "", "acceptance": "",
                "execution_class": "isolated", "execution_profile": "local"}],
        })
        ids = {bootstrap["goal_id"], task_id, expanded["admitted_task_ids"][0]}
        for operation in ("inspect", "status", "list"):
            result = self.call(operation, child=True)
            self.assertEqual(result[0], 0, result)
        self.assertEqual(self.call("connect", child=True)[0], 0)
        result = self.call("stop", "--instance-id", info.instance_id, "--timeout", "5s", child=True)
        self.assertEqual(result[0], 0, result)
        self.assertTrue(json.loads(result[1])["stopped"])
        with LifetimeLock(self.fixture.config.runtime_dir / "authority.lock"):
            pass
        result = self.call("connect", "--start", "--timeout", "5s")
        self.assertEqual(result[0], 0, result)
        current = connect(self.fixture.config)
        self.assertNotEqual(current.instance_id, info.instance_id)
        reader = TaskServiceClient(current.url, token=current.token)
        with self.assertRaises(TaskClientError) as caught:
            reader.call_tool("mptask_bootstrap", request)
        self.assertEqual(caught.exception.code, "stale_epoch")
        self.assertEqual({row["id"] for row in reader.call_tool("mptask_snapshot", {})["rows"]}, ids)
        outcome = reader.call_tool("mptask_outcome", {
            "epoch_id": info.instance_id, "command_id": request["command_id"]})
        self.assertEqual(outcome["receipt"]["goal_id"], bootstrap["goal_id"])
        self.assertFalse(outcome["authorization"]["authorized"])
        history = reader.call_tool("mptask_history", {"task_id": task_id})
        self.assertIn("Survives restart", json.dumps(history))
        self.assertFalse(any((self.fixture.config.runtime_dir / name).exists()
                             for name in ("clock.json", "pending.json", "verified_head.json")))

    def test_external_foreground_participates_in_election_without_manager(self):
        self.fixture.document["lifecycle"] = "external"
        self.fixture.save()
        self.assertEqual(self.call("init", child=True)[0], 0)
        result = self.call("start", child=True)
        self.assertEqual(result[0], 1, result)
        self.assertIn("external_lifecycle", result[2])
        with (self.fixture.root / "foreground.log").open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, "-W", "error", "-m", "mempalace_tasks",
                 "--config", str(self.fixture.path), "serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            info = None
            for _ in range(100):
                if child.poll() is not None:
                    break
                try:
                    info = connect(self.fixture.config, timeout=0.2)
                    break
                except DiscoveryError as error:
                    if error.code not in {"registry_missing", "endpoint_unavailable", "not_ready"}:
                        raise
                time.sleep(0.05)
            self.assertIsNotNone(info, (self.fixture.root / "foreground.log").read_text())
            result = self.call("stop", "--instance-id", info.instance_id, "--timeout", "5s", child=True)
            self.assertEqual(result[0], 0, result)
            self.assertEqual(child.wait(timeout=8), 0)
            self.assertTrue((self.fixture.config.runtime_dir / "start.lock").exists())
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=8)

    def test_concurrent_real_launchers_converge_on_one_owner_epoch(self):
        self.assertEqual(self.call("init", child=True)[0], 0)
        before = len(self.hub.events)
        with ThreadPoolExecutor(2) as pool:
            requests = [pool.submit(launcher.start, self.fixture.path, timeout=8) for _ in range(2)]
            infos = [future.result(timeout=12) for future in requests]
        self.assertEqual(infos[0].public(), infos[1].public())
        self.assertEqual(len(self.hub.events), before + 1)
        self.assertEqual(self.hub.events[-1]["type"], "mptask.epoch")
        self.assertNotEqual(json.loads(self.hub.events[-1]["body"])["epoch_id"], "")

    def test_valid_ticket_does_not_bypass_an_existing_authority_lock(self):
        from mempalace_tasks.discovery import binding_fingerprint
        from mempalace_tasks.platform_support import ensure_private_directory

        runtime = ensure_private_directory(self.fixture.config.runtime_dir)
        ticket = launcher.StartupTicket(self.fixture.config.authority_id,
                                        binding_fingerprint(self.fixture.config), "a" * 64)
        with LifetimeLock(runtime / "start.lock"), LifetimeLock(runtime / "authority.lock"):
            result = subprocess.run(
                [sys.executable, "-W", "error", "-m", "mempalace_tasks",
                 "--config", str(self.fixture.path), "serve", "--startup-ticket-stdin"],
                input=ticket.encode(), capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"authority_locked", result.stderr)
        self.assertEqual(self.hub.events, [])

    def test_invalid_ticket_cannot_acquire_authority_or_initialize(self):
        result = subprocess.run(
            [sys.executable, "-W", "error", "-m", "mempalace_tasks",
             "--config", str(self.fixture.path), "serve", "--startup-ticket-stdin"],
            input=b"{}\n", capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"invalid_ticket", result.stderr)
        self.assertFalse(self.fixture.config.runtime_dir.exists())
        self.assertEqual(self.hub.events, [])
