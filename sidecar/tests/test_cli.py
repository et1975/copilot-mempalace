"""CLI lifecycle integration without live hub data, registration or user workers."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from authority_fixture import LogClient, command, create, state_directory
from mempalace_tasks import cli
from mempalace_tasks.config import ConfigError, load_config
from mempalace_tasks.client import TaskServiceClient
from mempalace_tasks.execution import LocalProfile
from mempalace_tasks.journal import AuthorityLock, JournalError, JsonStore
from mempalace_tasks.palace import PalaceError
from mempalace_tasks.projection import TaskProjector
from service_fixture import ServiceFixture, SocketRunner, TOKEN
from test_config import config_document
from test_projection import OwnedTransport


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "config.json"
        self.document = config_document(self.root)
        self.token = self.root / "service.token"
        self.token.write_text(TOKEN + "\n")
        self.token.chmod(0o600)
        self.save()
        self.log = LogClient()

    def save(self):
        self.path.write_text(json.dumps(self.document))

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*argv, "--config", str(self.path)])
        return code, out.getvalue(), err.getvalue()

    def initialize(self):
        with patch.object(cli, "PalaceClient", return_value=self.log):
            result = self.call("init")
        self.assertEqual(result[0], 0, result)
        return result

    def test_init_is_explicit_normalized_idempotent_and_conflicts_fail_closed(self):
        self.initialize()
        originals = deepcopy(self.log.sent)
        self.assertTrue((self.root / "state" / "clock.json").is_file())
        self.assertEqual(len(originals), 1)
        self.initialize()
        self.assertEqual(self.log.sent, originals)
        self.document["genesis"]["execution_profiles"]["local"]["available"] = True
        self.save()
        self.initialize()
        self.assertEqual(self.log.sent, originals)
        self.document["genesis"]["policy"] = {"sweep_seconds": 1}
        self.save()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
        self.assertEqual(code, 1)
        self.assertIn("configuration_conflict", err)
        self.assertEqual(self.log.sent, originals)
        self.assertNotIn(TOKEN, out + err)

    def test_terminally_abandoned_genesis_retry_uses_a_new_persisted_uuid(self):
        self.log.behaviors = ["lost", "ok"]
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
            self.assertEqual(code, 1, (out, err))
            self.assertEqual(json.loads(out)["outcome"], "abandoned")
            abandoned_id = self.log.sent[0]["command_id"]
            code, out, err = self.call("init")
        self.assertEqual(code, 0, (out, err))
        committed = json.loads(out)
        self.assertEqual(committed["outcome"], "committed")
        self.assertNotEqual(committed["command_id"], abandoned_id)
        self.assertFalse((self.root / "state" / "pending.json").exists())
        self.assertEqual(len([row for row in self.log.sent if row["record_type"] == "mptask.command"]), 2)

    def test_pending_init_reconciles_before_retry_and_never_resends_original(self):
        self.log.behaviors = ["lost"] * 4
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("init")[0], 1)
            self.assertTrue((self.root / "state" / "pending.json").exists())
            self.assertEqual(self.call("init")[0], 0)
        originals = [row for row in self.log.sent if row["record_type"] == "mptask.command"]
        self.assertEqual(len(originals), 2)
        self.assertNotEqual(originals[0]["command_id"], originals[1]["command_id"])

    def test_missing_token_generation_requires_explicit_init_flag(self):
        self.token.unlink()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("init")[0], 2)
            self.assertFalse(self.token.exists())
            code, out, err = self.call("init", "--generate-token")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token.read_text().strip(), out + err)

    def test_serve_requires_initialization_and_always_releases_ownership(self):
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("serve")[0], 1)
            self.assertFalse((self.root / "state").exists())
        self.initialize()
        seen = []
        def failing_service(authority, maintenance, **kwargs):
            seen.append((authority, maintenance, kwargs))
            self.assertTrue(authority.health()["started"])
            raise KeyboardInterrupt
        with patch.object(cli, "PalaceClient", return_value=self.log), patch.object(cli, "serve", failing_service):
            self.assertEqual(self.call("serve")[0], 130)
        self.assertFalse(seen[0][0].health()["started"])
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(load_config(self.path)) as authority:
                self.assertTrue(authority.health()["fresh"])

    def test_unexpected_service_failure_is_safe_and_releases_authority(self):
        self.initialize()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with patch.object(cli, "serve", side_effect=RuntimeError(TOKEN)):
                code, out, err = self.call("serve")
            self.assertEqual(code, 1)
            self.assertNotIn(TOKEN, out + err)
            self.assertIn("internal_error", err)
            with cli.owned_authority(load_config(self.path)) as authority:
                self.assertTrue(authority.health()["started"])

    def test_server_startup_system_exit_returns_an_exit_code_and_releases_ownership(self):
        self.initialize()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with patch.object(cli, "serve", side_effect=SystemExit(3)):
                code, _, _ = self.call("serve")
            self.assertEqual(code, 1)
            with cli.owned_authority(load_config(self.path)) as authority:
                self.assertTrue(authority.health()["started"])

    def test_missing_clock_cannot_be_reinitialized_over_existing_recovery_state(self):
        self.initialize()
        clock = self.root / "state" / "clock.json"
        clock.unlink()
        before = deepcopy(self.log.sent)
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("serve")[0], 1)
            self.assertEqual(self.call("init")[0], 1)
        self.assertFalse(clock.exists())
        self.assertEqual(self.log.sent, before)

    def test_init_after_total_local_loss_rejects_remote_active_history_before_artifacts(self):
        self.initialize()
        config = load_config(self.path)
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(config) as authority:
                task_id = authority.execute(create(2))["task_id"]
                task = authority.execute(command(
                    3, "claim", actor="worker", task_id=task_id,
                    expected_version=1, supervisor_id="supervisor"))["tasks"][0]
                started = authority.execute(command(
                    4, "attempt_report", actor="supervisor", task_id=task_id,
                    expected_version=task["version"], attempt_id=task["attempt"]["id"],
                    claim_generation=task["claim_generation"], report_kind="started",
                    evidence={"prepared": True, "references": ["artifact:owned-running-task"]}))
                self.assertTrue(started["authorization"]["tasks"][0]["authorized"])
        original_events = deepcopy(self.log.events)
        original_sent = deepcopy(self.log.sent)
        shutil.rmtree(config.state_dir)
        self.token.unlink()

        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init", "--generate-token")

        self.assertEqual((code, (config.state_dir / "clock.json").exists(), self.token.exists()),
                         (1, False, False), (out, err))
        self.assertIn("missing_clock_state", err)
        self.assertEqual(self.log.events, original_events)
        self.assertEqual(self.log.sent, original_sent)
        self.assertEqual({path.name for path in config.state_dir.iterdir()}, {"authority.lock"})
        with AuthorityLock(config.state_dir, config.authority_id):
            pass

    def test_initial_clock_requires_supported_available_discovery_before_any_artifacts(self):
        for label, discovery in (
            ("unavailable", PalaceError("upstream_unavailable", "Owned unavailable discovery")),
            ("unknown", {"profile": "unrecognized-ordering-profile"}),
            ("malformed", None),
        ):
            with self.subTest(discovery=label):
                state = self.root / f"state-{label}"
                token = self.root / f"{label}.token"
                self.document.update(state_dir=str(state), service_token_file=str(token))
                self.save()
                log = LogClient()
                behavior = ({"side_effect": discovery} if isinstance(discovery, Exception)
                            else {"return_value": discovery})
                with patch.object(log, "discover", **behavior), patch.object(
                        cli, "PalaceClient", return_value=log):
                    code, out, err = self.call("init", "--generate-token")
                self.assertEqual((code, (state / "clock.json").exists(), token.exists(), bool(log.sent)),
                                 (1, False, False, False), (out, err))
                self.assertEqual({path.name for path in state.iterdir()}, {"authority.lock"})

    def test_initial_clock_requires_a_successful_empty_stream_not_malformed_or_unavailable(self):
        for label in ("unavailable", "null-record", "malformed-record"):
            with self.subTest(stream=label):
                state = self.root / f"state-{label}"
                token = self.root / f"{label}.token"
                self.document.update(state_dir=str(state), service_token_file=str(token))
                self.save()
                log = LogClient()
                log.read_error = label == "unavailable"
                if label == "null-record":
                    log.events = [None]
                if label == "malformed-record":
                    log.events = [{"unrecognized": "record"}]
                original_events = deepcopy(log.events)
                with patch.object(cli, "PalaceClient", return_value=log):
                    code, out, err = self.call("init", "--generate-token")
                self.assertEqual((code, (state / "clock.json").exists(), token.exists(), bool(log.sent)),
                                 (1, False, False, False), (out, err))
                self.assertEqual(log.events, original_events)
                self.assertEqual({path.name for path in state.iterdir()}, {"authority.lock"})

    def test_initial_empty_stream_verification_runs_under_ownership_before_clock_creation(self):
        self.token.unlink()
        observations = []
        state = self.root / "state"
        discover, replay = self.log.discover, self.log.replay_events

        def observe(stage):
            with self.assertRaises(JournalError) as caught:
                with AuthorityLock(state, self.document["authority_id"]):
                    pass
            self.assertEqual(caught.exception.code, "authority_locked")
            observations.append((stage, (state / "clock.json").exists(), self.token.exists()))

        def checked_discover():
            observe("discover")
            return discover()

        def checked_replay(cursor=None):
            observe("replay")
            yield from replay(cursor)

        with patch.object(self.log, "discover", checked_discover), patch.object(
                self.log, "replay_events", checked_replay), patch.object(
                cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init", "--generate-token")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(observations[:2], [("discover", False, False), ("replay", False, False)])
        self.assertTrue((state / "clock.json").is_file())
        self.assertTrue(self.token.is_file())
        self.assertEqual(len(self.log.sent), 1)

    def test_fresh_explicit_authority_uuid_can_initialize_an_empty_reserved_stream(self):
        identity = "22222222-2222-4222-8222-222222222222"
        self.document["authority_id"] = identity
        self.save()
        with patch("authority_fixture.AUTHORITY", identity), patch.object(
                cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out)["outcome"], "committed")
        self.assertEqual(len(self.log.events), 1)
        self.assertEqual(self.log.events[0]["stream"], f"mptask/{identity}")
        self.assertEqual(self.log.sent[0]["authority_id"], identity)
        self.assertTrue((self.root / "state" / "clock.json").is_file())

    def test_remote_inspection_uses_actual_frozen_renderer_and_never_opens_writer(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.document["port"] = service.port
            self.save()
            task_id = service.authority.execute(create())["task_id"]
            before = len(service.log.sent)
            with patch.object(cli, "PalaceClient", side_effect=AssertionError("Read started hub client")):
                for operation in ("status", "list", "show", "history", "watch"):
                    args = [operation]
                    if operation in {"show", "history"}:
                        args.append(task_id)
                    args += ["--json"]
                    if operation == "watch":
                        args += ["--duration", "0.15s", "--refresh", "0.1s", "--timeout", "0.1s"]
                    code, out, err = self.call(*args)
                    self.assertEqual(code, 0, (operation, out, err))
                    frame = json.loads(out.splitlines()[0])
                    self.assertEqual(frame["inspection_schema_version"], 1)
                    self.assertEqual(frame["command"], operation)
                self.assertEqual(self.call("inspect")[0], 0)
            self.assertEqual(len(service.log.sent), before)
            self.assertFalse((self.root / "state").exists())

    def test_missing_remote_service_is_nonzero_and_never_falls_back_to_local_authority(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.document["port"] = listener.getsockname()[1]
        self.save()
        with patch.object(cli, "PalaceClient", side_effect=AssertionError("Unexpected writer")):
            code, out, err = self.call("status", "--json", "--timeout", "0.1s")
            self.assertEqual(code, 1)
            frame = json.loads(out)
            self.assertFalse(frame["display"]["current"])
            self.assertIn(frame["error"]["code"], {"transport_error", "http_error", "timeout"})
            self.assertEqual(self.call("inspect", "--timeout", "0.1s")[0], 1)
        self.assertFalse((self.root / "state").exists())

    def test_diagnostic_cli_preserves_config_token_and_shared_parent_permissions(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.document["port"] = service.port
            self.save()
            self.root.chmod(0o775)
            self.path.chmod(0o664)
            paths = (self.root, self.path, self.token)
            modes = {path: path.stat().st_mode for path in paths}
            contents = {path: path.read_bytes() for path in paths if path.is_file()}
            entries = set(self.root.iterdir())
            with patch.object(cli, "PalaceClient", side_effect=AssertionError("Diagnostic started writer")):
                self.assertEqual(self.call("status", "--json")[0], 0)
                self.assertEqual(self.call("inspect")[0], 0)
                self.assertEqual({path: path.stat().st_mode for path in paths}, modes)
                self.assertEqual({path: path.read_bytes() for path in contents}, contents)
                self.assertEqual(set(self.root.iterdir()), entries)
                self.token.chmod(0o644)
                modes[self.token] = self.token.stat().st_mode
                self.assertEqual(self.call("status", "--json")[0], 2)
                self.assertEqual({path: path.stat().st_mode for path in paths}, modes)
                self.assertEqual({path: path.read_bytes() for path in contents}, contents)
                self.assertEqual(set(self.root.iterdir()), entries)
            self.assertFalse((self.root / "state").exists())

    def test_plain_status_and_list_include_attention_unless_explicitly_filtered(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.document["port"] = service.port
            self.save()
            recovering_id = service.authority.execute(create(2))["task_id"]
            claimed = service.authority.execute(command(
                3, "claim", actor="worker", task_id=recovering_id,
                expected_version=1, supervisor_id="supervisor"))["tasks"][0]
            released = service.authority.execute(command(
                4, "release", actor="worker", task_id=recovering_id,
                expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
                claim_generation=claimed["claim_generation"], reason="Owned fixture release"))
            self.assertEqual(released["tasks"][0]["status"], "recovering")

            quarantined_id = service.authority.execute(
                create(5, policy={"automatic_retries": 0}))["task_id"]
            claimed = service.authority.execute(command(
                6, "claim", actor="worker", task_id=quarantined_id,
                expected_version=1, supervisor_id="supervisor"))["tasks"][0]
            service.clock.value = "2026-09-23T00:05:00Z"
            expired = service.authority.execute(command(
                7, "expire", actor="system", task_id=quarantined_id,
                expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
                claim_generation=claimed["claim_generation"],
                expected_lease_revision=claimed["lease_revision"]))
            recovered = service.authority.execute(command(
                8, "recover", task_id=quarantined_id,
                expected_version=expired["tasks"][0]["version"], retry_decision="preserve",
                evidence={"publication_revoked": True,
                          "references": ["artifact:owned-publication-revoked"]}))
            self.assertEqual(recovered["tasks"][0]["status"], "quarantined")
            ordinary_id = service.authority.execute(create(9))["task_id"]
            writes = len(service.log.sent)

            code, text, err = self.call("status")
            self.assertEqual(code, 0, err)
            self.assertIn("Task 2", text)
            self.assertIn("Task 5", text)
            self.assertIn("Task 9", text)
            for operation in ("status", "list"):
                for flags, expected in (
                    ([], {recovering_id, quarantined_id, ordinary_id}),
                    (["--needs-attention"], {recovering_id, quarantined_id}),
                    (["--no-needs-attention"], {ordinary_id}),
                ):
                    with self.subTest(operation=operation, flags=flags):
                        code, out, err = self.call(operation, "--json", *flags)
                        self.assertEqual(code, 0, err)
                        self.assertEqual({row["id"] for row in json.loads(out)["data"]["rows"]},
                                         expected)
            self.assertEqual(len(service.log.sent), writes)

    def test_inspect_preserves_noncurrent_health_with_embedded_error(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.document["port"] = service.port
            self.save()
            service.authority.execute(create())
            service.log.read_error = True
            self.assertEqual(self.call("status", "--json")[0], 1)
            writes = len(service.log.sent)
            code, out, err = self.call("inspect")
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            health = json.loads(out)
            self.assertFalse(health["fresh"])
            self.assertEqual(health["error"]["code"], "upstream_unavailable")
            self.assertIn("protocol", health)
            self.assertIn("maintenance", health)
            self.assertEqual(len(service.log.sent), writes)
            self.assertFalse((self.root / "state").exists())

    def test_filters_paging_duration_parser_and_module_entrypoint(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.document["port"] = service.port
            self.save()
            service.authority.execute(create())
            service.authority.execute(create(3))
            code, out, err = self.call("list", "--project", "demo", "--status", "open",
                                       "--no-needs-attention", "--limit", "1", "--json")
            self.assertEqual(code, 0, (out, err))
            frame = json.loads(out)
            self.assertEqual(len(frame["data"]["rows"]), 1)
            cursor = frame["data"]["next_cursor"]
            code, out, _ = self.call("list", "--cursor", cursor, "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["data"]["reason"], "pinned_snapshot")
            environment = {**os.environ, "MPTASK_CONFIG": str(self.path)}
            completed = subprocess.run(
                [sys.executable, "-W", "error", "-m", "mempalace_tasks", "show",
                 frame["data"]["rows"][0]["id"], "--json"],
                env=environment, capture_output=True, text=True, timeout=5, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["command"], "show")
        self.assertEqual(cli.parse_duration("2m"), 120)
        self.assertEqual(cli.parse_duration("0.5h"), 1800)
        for duration in ("NaN", "inf", "0s", "-1m", "1", "1e309s"):
            self.assertEqual(self.call("watch", "--duration", duration)[0], 2)

    def test_offline_reconcile_and_projection_require_exclusive_existing_authority(self):
        self.initialize()
        config = load_config(self.path)
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(config):
                code, out, err = self.call("reconcile")
                self.assertEqual(code, 1)
                self.assertIn("authority_locked", err)
                self.assertEqual(self.call("project", "--resume")[0], 1)
            self.assertEqual(self.call("reconcile")[0], 0)
            with cli.owned_authority(config) as authority:
                authority.execute(create(3))
        transport = OwnedTransport()
        with patch.object(cli, "PalaceClient", side_effect=[self.log, transport]):
            code, out, err = self.call("project", "--resume", "--batch-size", "1", "--max-batches", "4")
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(json.loads(out)["processed"], 2)
        self.assertEqual(json.loads(out)["checkpoint"]["ordinal"], 2)
        checkpoint = JsonStore(self.root / "state" / "projection.json")
        self.assertEqual(checkpoint.read()["ordinal"], 2)
        before = deepcopy(self.log.sent)
        with patch.object(cli, "PalaceClient", side_effect=[self.log, transport]):
            self.assertEqual(self.call("project", "--rebuild")[0], 0)
        self.assertEqual(self.log.sent, before)

    def test_explicit_programmatic_supervision_is_bounded_and_refuses_second_owner(self):
        self.initialize()
        config = load_config(self.path)
        profile = LocalProfile("local", ("/bin/true",), self.root / "workers")
        with patch.object(cli, "PalaceClient", return_value=self.log):
            result = cli.supervise(
                config, profiles=[profile], supervisor_id="supervisor", worker_id="worker",
                max_steps=1, interval_seconds=0)
            self.assertEqual(result["steps"], 1)
            self.assertTrue(result["health"]["ok"])
            with cli.owned_authority(config):
                with self.assertRaises(Exception) as caught:
                    cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                                  worker_id="worker", max_steps=1)
                self.assertEqual(caught.exception.code, "authority_locked")
        with self.assertRaises(ConfigError):
            cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                          worker_id="worker", max_steps=0)
        with self.assertRaises(ConfigError):
            cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                          worker_id="worker", max_steps=1, interval_seconds=10**1000)

    def test_foreground_cli_serves_real_http_then_stops_and_releases_the_same_authority(self):
        self.initialize()
        runner = SocketRunner()
        self.addCleanup(runner.socket.close)
        self.document["port"] = runner.port
        self.save()
        result = []
        worker = threading.Thread(target=lambda: result.append(
            cli.main(["serve", "--config", str(self.path)])), name="owned-cli-serve")
        with patch.object(cli, "PalaceClient", return_value=self.log), patch("uvicorn.run", runner.run):
            worker.start()
            try:
                self.assertTrue(runner.ready.wait(5), runner.errors)
                self.assertEqual(runner.errors, [])
                client = TaskServiceClient(f"http://127.0.0.1:{runner.port}/mcp", token=TOKEN)
                arguments = {key: value for key, value in create(30).items() if key != "operation"}
                receipt = client.call_tool("mptask_create", arguments)
                self.assertEqual(client.call_tool("mptask_snapshot", {})["summary"]["total"], 1)
                self.assertEqual(self.call("inspect")[0], 0)
                self.assertEqual(self.call("reconcile")[0], 1)
            finally:
                runner.stop()
                worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [0])
            self.assertEqual(runner.errors, [])
            with cli.owned_authority(load_config(self.path)) as authority:
                self.assertEqual(authority.get(receipt["task_id"])["task"]["status"], "open")

    def test_programmatic_host_completes_configured_owned_worker_without_mcp_or_native_fleet(self):
        self.initialize()
        config = load_config(self.path)
        worker_script = Path(__file__).with_name("runtime_worker.py").resolve()
        profile = LocalProfile("local", (sys.executable, str(worker_script), "success"),
                               self.root / "workers")
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(config) as authority:
                task_id = authority.execute(create(30))["task_id"]
            result = cli.supervise(
                config, profiles=[profile], supervisor_id="supervisor", worker_id="worker",
                max_steps=30, interval_seconds=0.05)
            self.assertEqual(result["steps"], 30)
            self.assertEqual(result["health"]["active"], 0)
            with cli.owned_authority(config) as authority:
                self.assertEqual(authority.get(task_id)["task"]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
