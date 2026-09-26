"""Opt-in contract test against a disposable, preinstalled MemPalace hub."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import asynccontextmanager, closing, contextmanager
from datetime import timedelta
from uuid import uuid4

from mempalace_tasks.palace import PalaceClient


@unittest.skipUnless(
    os.environ.get("MPTASK_LIVE_HUB") == "1",
    "set MPTASK_LIVE_HUB=1 to exercise a disposable real MemPalace hub",
)
class LiveContractTests(unittest.TestCase):
    def setUp(self):
        self.executable = shutil.which("mempalace-mcp")
        if self.executable is None:
            self.fail("The live contract requires preinstalled mempalace-mcp")
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-live-", dir=os.environ.get("MPTASK_TEST_TMPDIR")
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.palace = self.root / "palace"
        self.palace.mkdir()
        self.authority = str(uuid4())
        self.transport_epoch = str(uuid4())
        self.stream = f"mptask/{self.authority}"
        self.process = None
        self.log = None
        self.addCleanup(self.stop_hub)

    def start_hub(self):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        palace_path = os.path.normcase(os.path.realpath(self.palace))
        key = hashlib.sha256(palace_path.encode("utf-8")).hexdigest()[:24]
        registry = home / ".mempalace" / "server" / key / "serverinfo.json"
        environment = dict(os.environ)
        environment.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_STATE_HOME=str(home / ".local" / "state"),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            MEMPALACE_MCP_IDLE_HOURS="0",
            MEMPALACE_MCP_HTTP_TOKEN="isolated-contract-test",
            MEMPALACE_MCP_READ_ONLY="0",
        )
        self.log = (self.root / "hub.log").open("ab")
        self.process = subprocess.Popen(
            [
                self.executable,
                "--palace", str(self.palace),
                "--backend", "sqlite_exact",
                "--transport", "http",
                "--host", "127.0.0.1",
                "--port", "0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
            env=environment,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(
                    "Disposable hub exited during startup:\n"
                    + (self.root / "hub.log").read_text(errors="replace")[-6000:]
                )
            try:
                info = json.loads(registry.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                info = None
            if isinstance(info, dict) and info.get("pid") == self.process.pid:
                self.assertEqual(palace_path, os.path.normcase(info["palace_path"]))
                self.assertEqual("127.0.0.1", info["host"])
                self.assertEqual("http", info["scheme"])
                self.assertIs(info["read_only"], False)
                self.assertIs(type(info["port"]), int)
                self.assertGreater(info["port"], 0)
                self.assertLessEqual(info["port"], 65535)
                self.hub_url = f"http://127.0.0.1:{info['port']}/mcp"
                return PalaceClient(
                    self.hub_url,
                    self.stream,
                    token="isolated-contract-test",
                    timeout=10,
                )
            time.sleep(0.05)
        self.fail("Disposable hub did not become responsive within 60 seconds")

    def stop_hub(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def test_journal_only_restore_over_real_hub_discards_future_local_state(self):
        from authority_fixture import command, create, genesis
        from mempalace_tasks.authority import AuthorityError
        from mempalace_tasks.authority import TaskAuthority

        client = self.start_hub()
        runtime = self.root / "runtime"
        owner = TaskAuthority(self.authority, client, runtime, initialize=True).start()
        self.addCleanup(owner.close)
        owner.execute_current(genesis())
        created = owner.execute(create(), expected_epoch=owner.epoch_id)["tasks"][0]
        task_id = created["id"]
        claimed = owner.execute(command(
            3, "claim", "worker", task_id=task_id, expected_version=1,
            supervisor_id="supervisor"), expected_epoch=owner.epoch_id)["tasks"][0]
        owner.execute(command(
            4, "attempt_report", "supervisor", task_id=task_id,
            expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
            claim_generation=claimed["claim_generation"], report_kind="started",
            evidence={"references": ["fixture:prepared"], "prepared": True}),
            expected_epoch=owner.epoch_id)
        self.assertTrue(owner.get(task_id)["authorization"]["authorized"])
        original_epoch = owner.epoch_id
        owner.close()
        self.stop_hub()

        frozen = self.root / "snapshot.sqlite3"
        source = self.palace / "logstream.sqlite3"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(frozen)) as writer:
                reader.backup(writer)
                self.assertEqual(writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0],
                                 "delete")
        replica = (self.palace / "replica.json").read_bytes()

        client = self.start_hub()
        future = TaskAuthority(self.authority, client, runtime).start()
        self.addCleanup(future.close)
        current = future.get(task_id)["task"]
        note = command(5, "note", task_id=task_id, expected_version=current["version"],
                       tag="future", text="This event is after the chosen snapshot.")
        future.execute(note, expected_epoch=future.epoch_id)
        future_proposal = json.loads(future.log.proposals[note["command_id"]])
        future_epoch = future.epoch_id
        future.close()
        self.stop_hub()

        shutil.copyfile(frozen, source)
        for suffix in ("-wal", "-shm"):
            source.with_name(source.name + suffix).unlink(missing_ok=True)
        (self.palace / "replica.json").write_bytes(replica)
        for name in ("pending.json", "verified_head.json", "clock.json", "projection.json"):
            (runtime / name).write_bytes(b"discarded future; deliberately invalid JSON")

        client = self.start_hub()
        restored = TaskAuthority(self.authority, client, runtime).start()
        self.addCleanup(restored.close)
        self.assertNotIn(restored.epoch_id, (original_epoch, future_epoch))
        self.assertEqual(set(restored.state.tasks), {task_id})
        self.assertFalse(restored.get(task_id)["authorization"]["authorized"])
        self.assertEqual(restored.state.tasks[task_id]["automatic_retries_used"], 0)
        with self.assertRaises(AuthorityError) as stale:
            restored.execute(note, expected_epoch=future_epoch)
        self.assertEqual(stale.exception.code, "stale_epoch")
        self.assertIsNone(restored.outcome(future_epoch, note["command_id"])["receipt"])

        client.append_event(future_proposal)
        restored.refresh()
        self.assertTrue(restored.health()["fresh"])
        self.assertIsNone(restored.outcome(future_epoch, note["command_id"])["receipt"])
        self.assertEqual(restored.log.history[-1]["disposition"], "stale")
        restored.close()

        fresh_runtime = self.root / "fresh-runtime"
        fresh = TaskAuthority(self.authority, client, fresh_runtime).start()
        self.addCleanup(fresh.close)
        self.assertEqual(set(fresh.state.tasks), {task_id})
        self.assertFalse(fresh.get(task_id)["authorization"]["authorized"])
        self.assertEqual({path.name for path in fresh_runtime.iterdir()}, {"authority.lock"})
        self.assertTrue(all(
            (runtime / name).read_bytes() == b"discarded future; deliberately invalid JSON"
            for name in ("pending.json", "verified_head.json", "clock.json", "projection.json")
        ))

    def payload(self, label):
        return {
            "record_type": "mptask.command",
            "schema_version": 2,
            "epoch_id": self.transport_epoch,
            "authority_id": self.authority,
            "command_id": str(uuid4()),
            "event": {"kind": "ContractFixture", "value": label},
        }

    def run_cli(self, config, *arguments):
        repository = Path(__file__).resolve().parents[2]
        environment = dict(os.environ, PYTHONPATH=str(repository / "sidecar" / "src"))
        return subprocess.run(
            [sys.executable, "-W", "error", "-m", "mempalace_tasks",
             *arguments, "--config", str(config)],
            cwd=repository, env=environment, capture_output=True, text=True,
            timeout=30, check=False,
        )

    @contextmanager
    def task_service(self, config_path):
        from mempalace_tasks.cli import owned_authority
        from mempalace_tasks.client import TaskServiceClient
        from mempalace_tasks.config import load_config
        from mempalace_tasks.lifecycle import HttpLifecycle
        from mempalace_tasks.maintenance import LeaseMaintenance
        from mempalace_tasks.server import create_app
        from mempalace_tasks.server_identity import InstanceIdentity
        from service_fixture import SocketRunner

        runner = SocketRunner()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["port"] = runner.port
        config_path.write_text(json.dumps(data), encoding="utf-8")
        configuration = load_config(str(config_path))
        try:
            with owned_authority(configuration) as authority:
                maintenance = LeaseMaintenance(
                    authority.bound_current(), authority.clock_source,
                    system_actor="system", recovery_actor="operator",
                )
                lifecycle = HttpLifecycle(
                    configuration,
                    InstanceIdentity(self.authority, authority.epoch_id,
                                     f"http://127.0.0.1:{runner.port}/mcp"),
                    shutdown=runner.stop,
                )
                app = create_app(
                    authority, maintenance, token=configuration.service_token,
                    port=runner.port, lifecycle=lifecycle,
                )
                thread = threading.Thread(target=runner.run, args=(app,))
                thread.start()
                try:
                    self.assertTrue(runner.ready.wait(8), "Task service did not start")
                    self.assertEqual([], runner.errors)
                    deadline = time.monotonic() + 3
                    while not runner.server.started and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(runner.server.started, "Task listener did not start")
                    lifecycle.publish()
                    client = TaskServiceClient(
                        f"http://127.0.0.1:{runner.port}/mcp",
                        token=configuration.service_token,
                    )
                    self.assertTrue(client.call_tool("mptask_health", {})["fresh"])
                    yield client
                finally:
                    runner.stop()
                    thread.join(10)
                    self.assertFalse(thread.is_alive(), "Task service did not stop")
                    self.assertEqual([], runner.errors)
                    lifecycle.clear()
        finally:
            runner.socket.close()

    def task_config(self, *, lifecycle="external", port=8766):
        hub_token = self.root / "hub.token"
        hub_token.write_text("isolated-contract-test", encoding="ascii")
        hub_token.chmod(0o600)
        config_path = self.root / "tasks.json"
        configuration = {
            "schema_version": 2,
            "authority_id": self.authority,
            "hub_url": self.hub_url,
            "hub_token_file": str(hub_token),
            "service_token_file": str(self.root / "service.token"),
            "runtime_dir": str(self.root / "task-runtime"),
            "host": "127.0.0.1",
            "port": port,
            "lifecycle": lifecycle,
            "genesis": {
                "actor": "operator",
                "actors": {
                    "operator": "operator", "coordinator": "coordinator",
                    "worker": "worker", "supervisor": "supervisor", "system": "system",
                },
                "execution_profiles": {"local": {"execution_class": "isolated"}},
                "supervisors": {"supervisor": {"profiles": ["local"], "workers": ["worker"]}},
                "policy": {"sweep_seconds": 1},
            },
            "maintenance_actor": "system",
            "recovery_actor": "operator",
        }
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        initialized = self.run_cli(config_path, "init", "--generate-token")
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertTrue(json.loads(initialized.stdout)["ok"])
        return config_path

    def test_stdio_frontends_autostart_share_owner_and_leave_it_running(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mempalace_tasks.config import load_config
        from mempalace_tasks.discovery import connect
        from mempalace_tasks.launcher import _owner_busy, stop

        self.start_hub()
        config_path = self.task_config(lifecycle="launcher", port=0)
        config = load_config(str(config_path))
        expected_owner = None
        self.assertFalse(_owner_busy(config))
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-W", "error", "-m", "mempalace_tasks", "mcp",
                  "--config", str(config_path), "--timeout", "5s"],
            env=environment,
        )
        logs = []

        @asynccontextmanager
        async def frontend():
            path = self.root / f"frontend-{len(logs)}.log"
            logs.append(path)
            with path.open("w", encoding="utf-8") as errors:
                async with stdio_client(params, errlog=errors) as (reader, writer):
                    async with ClientSession(
                        reader, writer, read_timeout_seconds=timedelta(seconds=12),
                    ) as session:
                        await session.initialize()
                        yield session

        async def scenario():
            nonlocal expected_owner
            async with frontend() as first:
                health = await first.call_tool("mptask_health", {})
                self.assertFalse(health.isError)
                epoch = health.structuredContent["epoch_id"]
                expected_owner = epoch
                first_tools = await first.list_tools()
                descriptors = {tool.name: tool.model_dump() for tool in first_tools.tools}
                self.assertIn("mptask_outcome", descriptors)
                self.assertIn("expected_epoch",
                              descriptors["mptask_create"]["inputSchema"]["required"])
                async with frontend() as second:
                    second_health = await second.call_tool("mptask_health", {})
                    self.assertEqual(epoch, second_health.structuredContent["epoch_id"])
                    self.assertEqual(
                        descriptors,
                        {tool.name: tool.model_dump() for tool in (await second.list_tools()).tools},
                    )
                    fields = {
                        "actor": "operator", "command_id": str(uuid4()),
                        "expected_epoch": epoch,
                        "project": "frontend-test", "kind": "task",
                        "title": "Created through stdio", "description": "", "acceptance": "",
                        "execution_class": "isolated", "execution_profile": "local",
                    }
                    created = await second.call_tool("mptask_create", fields)
                    self.assertFalse(created.isError)
                    result = created.structuredContent
                    self.assertEqual(result, json.loads(created.content[0].text))
                    self.assertEqual(fields["command_id"], result["command_id"])
                    task_id = result["tasks"][0]["id"]
                    fetched = await first.call_tool("mptask_get", {"task_id": task_id})
                    self.assertEqual(fields["title"], fetched.structuredContent["task"]["title"])
                    rejected = await second.call_tool(
                        "mptask_create", {**fields, "command_id": str(uuid4()),
                                          "expected_epoch": str(uuid4())},
                    )
                    self.assertTrue(rejected.isError)
                    self.assertEqual("stale_epoch", rejected.structuredContent["error"]["code"])
                    self.assertIs(rejected.structuredContent["error"]["ambiguous"], False)
                    missing_epoch = dict(fields)
                    del missing_epoch["expected_epoch"]
                    rejected = await second.call_tool("mptask_create", missing_epoch)
                    self.assertTrue(rejected.isError)
                remaining = await first.call_tool("mptask_health", {})
                self.assertEqual(epoch, remaining.structuredContent["epoch_id"])
                listed = await first.call_tool(
                    "mptask_snapshot", {"filters": {"project": "frontend-test"}},
                )
                self.assertEqual(1, listed.structuredContent["summary"]["total"])
            discovered = await asyncio.to_thread(connect, config, timeout=5)
            self.assertEqual(epoch, discovered.instance_id)
            self.assertTrue(discovered.ready)

        try:
            asyncio.run(asyncio.wait_for(scenario(), timeout=45))
            for path in logs:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(config.service_token, text)
                self.assertNotIn("isolated-contract-test", text)
                self.assertNotIn("Traceback (most recent call last)", text)
        finally:
            if _owner_busy(config):
                info = connect(config, timeout=5)
                if expected_owner is not None:
                    self.assertEqual(expected_owner, info.instance_id)
                stop(config, expected_instance_id=info.instance_id, timeout=10)
            self.assertFalse(_owner_busy(config))

    def test_goal_discovery_inspection_and_service_replay_over_real_hub(self):
        from mempalace_tasks.client import TaskClientError

        self.start_hub()
        config_path = self.task_config()

        with self.task_service(config_path) as client:
            epoch = client.call_tool("mptask_health", {})["epoch_id"]

            def send(operation, actor="worker", **fields):
                return client.call_tool(f"mptask_{operation}", {
                    "actor": actor, "command_id": str(uuid4()),
                    "expected_epoch": epoch, **fields,
                })

            def task(task_id):
                return client.call_tool("mptask_get", {"task_id": task_id})["task"]

            def tokens(task_id):
                value = task(task_id)
                return {
                    "task_id": task_id, "expected_version": value["version"],
                    "attempt_id": value["attempt"]["id"],
                    "claim_generation": value["claim_generation"],
                }

            def start(task_id):
                send("claim", task_id=task_id, expected_version=task(task_id)["version"],
                     supervisor_id="supervisor")
                send("attempt_report", actor="supervisor", report_kind="started",
                     evidence={"references": ["fixture:prepared"], "prepared": True},
                     **tokens(task_id))

            created = send(
                "bootstrap", actor="coordinator", project="live-test",
                title="End-to-end goal", description="Exercise task discovery",
                acceptance="Both tasks verified and intake sealed",
                planning_task={
                    "title": "Plan and integrate", "description": "Discover a prerequisite",
                    "acceptance": "Validate prerequisite and integration",
                    "execution_class": "isolated", "execution_profile": "local",
                },
                goal_policy={"scope": "owned integration fixture", "max_tasks": 5},
            )
            goal_id, planner_id = created["goal_id"], created["planning_task_id"]
            start(planner_id)
            source = tokens(planner_id)
            expanded = send(
                "expand", goal_id=goal_id,
                expected_graph_revision=task(goal_id)["graph_revision"],
                source_task_id=planner_id,
                expected_version=source["expected_version"],
                attempt_id=source["attempt_id"],
                claim_generation=source["claim_generation"],
                tasks=[{
                    "intent_key": "prerequisite", "title": "Verify prerequisite",
                    "description": "Independent prerequisite", "acceptance": "Verified",
                    "execution_class": "isolated", "execution_profile": "local",
                }],
                edges=[
                    {"source": "prerequisite", "target": "$source", "edge_type": "blocks"},
                    {"source": "prerequisite", "target": "$source", "edge_type": "discovered_from"},
                ],
                source_disposition="yield",
                checkpoint={"sequence": 1, "reference": "fixture:checkpoint"},
                reason="Prerequisite must finish first",
            )
            prerequisite_id = expanded["admitted_task_ids"][0]
            ready = client.call_tool("mptask_ready", {"filters": {"goal_id": goal_id}})
            self.assertEqual([prerequisite_id], [item["id"] for item in ready["tasks"]])
            start(prerequisite_id)
            send("transition", target="closed", summary="Prerequisite verified",
                 evidence=["fixture:prerequisite-result"], **tokens(prerequisite_id))
            ready = client.call_tool("mptask_ready", {"filters": {"goal_id": goal_id}})
            deadline = time.monotonic() + 3
            while not ready["tasks"] and time.monotonic() < deadline:
                time.sleep(0.05)
                ready = client.call_tool("mptask_ready", {"filters": {"goal_id": goal_id}})
            self.assertEqual([planner_id], [item["id"] for item in ready["tasks"]])
            start(planner_id)
            self.assertEqual(2, task(planner_id)["claim_generation"])
            send("transition", target="closed", summary="Integration verified",
                 evidence=["fixture:integration-result"], **tokens(planner_id))
            goal = task(goal_id)
            send("goal_close", actor="coordinator", goal_id=goal_id,
                 expected_version=goal["version"], expected_graph_revision=goal["graph_revision"],
                 summary="Goal accepted", evidence=["fixture:goal-acceptance"])
            status = self.run_cli(config_path, "status", "--project", "live-test", "--json")
            self.assertEqual(0, status.returncode, status.stderr)
            inspected = json.loads(status.stdout)
            self.assertEqual("CURRENT", inspected["display"]["state"])
            self.assertEqual(3, inspected["data"]["summary"]["total"])
            self.assertEqual({"closed": 3}, inspected["data"]["summary"]["statuses"])

        self.stop_hub()
        self.start_hub()
        updated = json.loads(config_path.read_text(encoding="utf-8"))
        updated["hub_url"] = self.hub_url
        config_path.write_text(json.dumps(updated), encoding="utf-8")
        with self.task_service(config_path) as client:
            goal = client.call_tool("mptask_get", {"task_id": goal_id})["task"]
            self.assertEqual("closed", goal["status"])
            self.assertTrue(goal["sealed"])
            with self.assertRaises(TaskClientError) as rejected:
                client.call_tool("mptask_expand", {
                    "command_id": str(uuid4()), "actor": "coordinator",
                    "expected_epoch": client.call_tool("mptask_health", {})["epoch_id"],
                    "goal_id": goal_id, "expected_graph_revision": goal["graph_revision"],
                    "tasks": [], "source_disposition": "continue",
                })
            self.assertFalse(rejected.exception.ambiguous)
            self.assertEqual("invalid_transition", rejected.exception.code)

    def test_append_order_cursor_and_exact_body_survive_hub_restart(self):
        client = self.start_hub()
        profile = client.discover()
        self.assertIn(
            profile["profile"],
            ("mempalace-ordered-v1", "mempalace-legacy-append-order-v1"),
        )
        self.assertEqual([], list(client.replay_events()))
        first_payload = self.payload("first exact caf\u00e9\n")
        second_payload = self.payload("second")
        first = client.append_event(first_payload)
        second = client.append_event(second_payload)
        self.assertEqual(first_payload, json.loads(first["body"]))
        self.assertEqual(second_payload, json.loads(second["body"]))
        self.assertEqual(
            [first["id"], second["id"]],
            [event["id"] for event in client.replay_events()],
        )
        self.assertEqual(
            [second["id"]],
            [event["id"] for event in client.replay_events(first["id"])],
        )

        self.stop_hub()
        reopened = self.start_hub()
        reopened.discover()
        events = list(reopened.replay_events())
        self.assertEqual([first["id"], second["id"]], [event["id"] for event in events])
        self.assertEqual(first_payload, json.loads(events[0]["body"]))
        third = reopened.append_event(self.payload("after restart"))
        self.assertEqual(
            [third["id"]],
            [event["id"] for event in reopened.replay_events(second["id"])],
        )


if __name__ == "__main__":
    unittest.main()
