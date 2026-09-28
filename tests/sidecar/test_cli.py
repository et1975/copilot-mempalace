"""Current CLI consumers: palace-only ownership and authenticated discovery."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from authority_fixture import LogClient, command, create, state_directory
from mempalace_tasks import cli
from mempalace_tasks.config import ConfigError, load_config
from mempalace_tasks.execution import LocalProfile
from service_fixture import ServiceFixture, TOKEN
from test_config import config_document


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
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def initialize(self):
        with patch.object(cli, "PalaceClient", return_value=self.log):
            result = self.call("init")
        self.assertEqual(result[0], 0, result)
        return result

    def attach(self, service):
        self.path = service.config_path
        self.root = self.path.parent
        self.token = service.config.service_token_file
        self.document = json.loads(self.path.read_text())

    def test_init_is_explicit_normalized_and_conflicts_precede_activation(self):
        self.initialize()
        self.assertEqual([row["record_type"] for row in self.log.sent],
                         ["mptask.epoch", "mptask.command"])
        self.assertEqual({path.name for path in (self.root / "runtime").iterdir()}, {"authority.lock"})
        self.document["genesis"]["execution_profiles"]["local"]["available"] = True
        self.save()
        result = self.initialize()
        self.assertTrue(json.loads(result[1])["already_initialized"])
        self.assertEqual(sum(row["record_type"] == "mptask.command" for row in self.log.sent), 1)
        originals = deepcopy(self.log.sent)
        self.document["genesis"]["policy"] = {"sweep_seconds": 1}
        self.save()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
        self.assertEqual(code, 1)
        self.assertIn("configuration_conflict", err)
        self.assertEqual(self.log.sent, originals)

    def test_abandoned_init_is_explicit_and_next_init_uses_new_epoch_and_command(self):
        self.log.behaviors = ["ok", "lost", "ok"]
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
            self.assertEqual(code, 1, (out, err))
            abandoned = json.loads(out)
            self.assertEqual(abandoned["outcome"], "abandoned")
            code, out, err = self.call("init")
        self.assertEqual(code, 0, (out, err))
        committed = json.loads(out)
        self.assertEqual(committed["outcome"], "committed")
        self.assertNotEqual(committed["command_id"], abandoned["command_id"])
        self.assertNotEqual(committed["epoch_id"], abandoned["epoch_id"])
        self.assertEqual({path.name for path in (self.root / "runtime").iterdir()}, {"authority.lock"})

    def test_missing_token_generation_requires_explicit_init_and_never_prints_secret(self):
        self.token.unlink()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("init")[0], 2)
            self.assertEqual(self.log.sent, [])
            self.assertFalse(self.token.exists())
            code, out, err = self.call("init", "--generate-token")
        self.assertEqual(code, 0, (out, err))
        self.assertNotIn(self.token.read_text().strip(), out + err)
        original = self.token.read_bytes()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("init", "--generate-token")[0], 0)
        self.assertEqual(self.token.read_bytes(), original)

    def test_serve_requires_genesis_and_failure_always_releases_ownership(self):
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, _, err = self.call("serve")
            self.assertEqual(code, 1)
            self.assertIn("not_initialized", err)
        self.assertEqual(self.log.sent, [])
        self.initialize()
        seen = []

        def interrupted(authority, maintenance, **kwargs):
            seen.append(authority)
            self.assertTrue(authority.health()["fresh"])
            raise KeyboardInterrupt

        with patch.object(cli, "PalaceClient", return_value=self.log), patch.object(cli, "serve", interrupted):
            self.assertEqual(self.call("serve")[0], 130)
        self.assertFalse(seen[0].health()["started"])
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(load_config(self.path)) as owner:
                self.assertTrue(owner.health()["fresh"])

    def test_service_exceptions_are_safe_and_release_authority(self):
        self.initialize()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            for failure, code in ((RuntimeError(TOKEN), "internal_error"),
                                  (SystemExit(3), "service_exit")):
                with self.subTest(code=code), patch.object(cli, "serve", side_effect=failure):
                    result = self.call("serve")
                    self.assertEqual(result[0], 1, result)
                    self.assertIn(code, result[2])
                with cli.owned_authority(load_config(self.path)) as owner:
                    self.assertTrue(owner.health()["started"])

    def test_unavailable_source_fails_before_activation_or_token_creation(self):
        self.token.unlink()
        self.log.discover_error = True
        with patch.object(cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init", "--generate-token")
        self.assertEqual(code, 1, (out, err))
        self.assertIn("upstream_unavailable", err)
        self.assertFalse(self.token.exists())
        self.assertEqual(self.log.sent, [])
        self.assertEqual({path.name for path in (self.root / "runtime").iterdir()}, {"authority.lock"})

    def test_unavailable_or_corrupt_history_never_appends_initial_activation(self):
        for corruption in ("unavailable", "null-record", "malformed-record"):
            with self.subTest(corruption=corruption):
                log = LogClient()
                log.read_error = corruption == "unavailable"
                log.events = ([None] if corruption == "null-record" else
                              [{"unrecognized": "record"}] if corruption == "malformed-record" else [])
                before = deepcopy(log.events)
                with patch.object(cli, "PalaceClient", return_value=log):
                    code, out, err = self.call("init")
                self.assertEqual(code, 1, (out, err))
                self.assertEqual(log.events, before)
                self.assertEqual(log.sent, [])

    def test_new_identity_initializes_only_its_explicit_stream(self):
        identity = "22222222-2222-4222-8222-222222222222"
        self.document["authority_id"] = identity
        self.save()
        with patch("authority_fixture.AUTHORITY", identity), patch.object(
                cli, "PalaceClient", return_value=self.log):
            code, out, err = self.call("init")
        self.assertEqual(code, 0, (out, err))
        self.assertTrue(all(event["stream"] == f"mptask/{identity}" for event in self.log.events))
        self.assertEqual(json.loads(out)["outcome"], "committed")

    def test_remote_inspection_uses_frozen_renderer_without_constructing_owner(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.attach(service)
            task_id = service.execute(create())["task_id"]
            before = len(service.log.sent)
            with patch.object(cli, "PalaceClient", side_effect=AssertionError("Read started hub client")):
                for operation in ("status", "list", "show", "history", "watch"):
                    args = [operation, "--json"]
                    if operation in {"show", "history"}:
                        args.append(task_id)
                    if operation == "watch":
                        args += ["--duration", "0.6s", "--refresh", "0.1s", "--timeout", "0.3s"]
                    code, out, err = self.call(*args)
                    self.assertEqual(code, 0, (operation, out, err))
                    frame = json.loads(out.splitlines()[0])
                    self.assertEqual(frame["inspection_schema_version"], 1)
                    self.assertEqual(frame["command"], operation)
                self.assertEqual(self.call("inspect")[0], 0)
            self.assertEqual(len(service.log.sent), before)

    def test_absent_discovery_never_uses_fixed_port_or_local_authority(self):
        with patch.object(cli, "PalaceClient", side_effect=AssertionError("Unexpected writer")):
            for operation in ("status", "inspect", "list", "watch"):
                result = self.call(operation, "--timeout", "0.1s")
                self.assertEqual(result[0], 1, result)
                self.assertEqual(result[1], "")
                self.assertIn("registry_missing", result[2])
        self.assertFalse((self.root / "runtime").exists())

    def test_diagnostic_cli_preserves_config_token_and_shared_parent_permissions(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.attach(service)
            self.root.chmod(0o775)
            self.path.chmod(0o664)
            paths = (self.root, self.path, self.token)
            modes = {path: path.stat().st_mode for path in paths}
            contents = {path: path.read_bytes() for path in paths if path.is_file()}
            entries = set(self.root.iterdir())
            try:
                with patch.object(cli, "PalaceClient", side_effect=AssertionError("Diagnostic writer")):
                    self.assertEqual(self.call("status", "--json")[0], 0)
                    self.assertEqual(self.call("inspect")[0], 0)
                    self.assertEqual({path: path.stat().st_mode for path in paths}, modes)
                    self.token.chmod(0o644)
                    modes[self.token] = self.token.stat().st_mode
                    self.assertEqual(self.call("status", "--json")[0], 2)
                    self.assertEqual({path: path.stat().st_mode for path in paths}, modes)
                    self.assertEqual({path: path.read_bytes() for path in contents}, contents)
                    self.assertEqual(set(self.root.iterdir()), entries)
            finally:
                self.token.chmod(0o600)

    def test_plain_status_and_list_include_attention_unless_explicitly_filtered(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.attach(service)
            recovering_id = service.execute(create(2))["task_id"]
            claimed = service.execute(command(3, "claim", actor="worker", task_id=recovering_id,
                                              expected_version=1, supervisor_id="supervisor"))["tasks"][0]
            service.execute(command(
                4, "release", actor="worker", task_id=recovering_id,
                expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
                claim_generation=claimed["claim_generation"], reason="Owned fixture release"))
            quarantined_id = service.execute(create(5, policy={"automatic_retries": 0}))["task_id"]
            claimed = service.execute(command(6, "claim", actor="worker", task_id=quarantined_id,
                                              expected_version=1, supervisor_id="supervisor"))["tasks"][0]
            service.clock.value = "2026-09-23T00:05:00Z"
            expired = service.execute(command(
                7, "expire", actor="system", task_id=quarantined_id,
                expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
                claim_generation=claimed["claim_generation"],
                expected_lease_revision=claimed["lease_revision"]))
            service.execute(command(
                8, "recover", task_id=quarantined_id, expected_version=expired["tasks"][0]["version"],
                retry_decision="preserve", evidence={"publication_revoked": True,
                                                   "references": ["artifact:owned-revocation"]}))
            ordinary_id = service.execute(create(9))["task_id"]
            writes = len(service.log.sent)
            code, text, err = self.call("status")
            self.assertEqual(code, 0, err)
            for title in ("Task 2", "Task 5", "Task 9"):
                self.assertIn(title, text)
            for operation in ("status", "list"):
                for flags, expected in (
                    ([], {recovering_id, quarantined_id, ordinary_id}),
                    (["--needs-attention"], {recovering_id, quarantined_id}),
                    (["--no-needs-attention"], {ordinary_id}),
                ):
                    with self.subTest(operation=operation, flags=flags):
                        code, out, err = self.call(operation, "--json", *flags)
                        self.assertEqual(code, 0, err)
                        self.assertEqual({row["id"] for row in json.loads(out)["data"]["rows"]}, expected)
            self.assertEqual(len(service.log.sent), writes)

    def test_inspect_does_not_bypass_noncurrent_identity_proof(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.attach(service)
            service.execute(create())
            service.log.read_error = True
            writes = len(service.log.sent)
            try:
                for operation in ("status", "inspect"):
                    code, out, err = self.call(operation)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("not_ready", err)
            finally:
                service.log.read_error = False
            self.assertEqual(len(service.log.sent), writes)

    def test_filters_paging_duration_parser_and_module_entrypoint(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            self.attach(service)
            service.execute(create())
            service.execute(create(3))
            code, out, err = self.call("list", "--project", "demo", "--status", "open",
                                       "--no-needs-attention", "--limit", "1", "--json")
            self.assertEqual(code, 0, (out, err))
            frame = json.loads(out)
            self.assertEqual(len(frame["data"]["rows"]), 1)
            code, out, _ = self.call("list", "--cursor", frame["data"]["next_cursor"], "--json")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["data"]["reason"], "pinned_snapshot")
            completed = subprocess.run(
                [sys.executable, "-W", "error", "-m", "mempalace_tasks", "show",
                 frame["data"]["rows"][0]["id"], "--json"],
                env={**os.environ, "MPTASK_CONFIG": str(self.path)}, capture_output=True,
                text=True, timeout=5, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["command"], "show")
        self.assertEqual(cli.parse_duration("2m"), 120)
        self.assertEqual(cli.parse_duration("0.5h"), 1800)
        for duration in ("NaN", "inf", "0s", "-1m", "1", "1e309s"):
            self.assertEqual(self.call("watch", "--duration", duration)[0], 2)

    def test_offline_reconcile_requires_exclusive_initialized_authority(self):
        with patch.object(cli, "PalaceClient", return_value=self.log):
            self.assertEqual(self.call("reconcile")[0], 1)
        self.assertEqual(self.log.sent, [])
        self.initialize()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(load_config(self.path)):
                code, out, err = self.call("reconcile")
                self.assertEqual(code, 1)
                self.assertIn("authority_locked", err)
            code, out, err = self.call("reconcile")
            self.assertEqual(code, 0, (out, err))
            self.assertTrue(json.loads(out)["authority"]["fresh"])

    def test_explicit_supervision_is_bounded_and_refuses_second_owner(self):
        self.initialize()
        config = load_config(self.path)
        profile = LocalProfile("local", (sys.executable, "-V"), self.root / "workers")
        with patch.object(cli, "PalaceClient", return_value=self.log):
            result = cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                                   worker_id="worker", max_steps=1, interval_seconds=0)
            self.assertEqual(result["steps"], 1)
            self.assertTrue(result["health"]["ok"])
            with cli.owned_authority(config):
                with self.assertRaises(Exception) as caught:
                    cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                                  worker_id="worker", max_steps=1)
                self.assertEqual(caught.exception.code, "authority_locked")
        for options in ({"max_steps": 0}, {"max_steps": 1, "interval_seconds": 10**1000}):
            with self.assertRaises(ConfigError):
                cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                              worker_id="worker", **options)

    def test_programmatic_host_completes_configured_worker_without_native_fleet(self):
        self.initialize()
        config = load_config(self.path)
        profile = LocalProfile(
            "local", (sys.executable, str(Path(__file__).with_name("runtime_worker.py").resolve()), "success"),
            self.root / "workers")
        with patch.object(cli, "PalaceClient", return_value=self.log):
            with cli.owned_authority(config) as owner:
                task_id = owner.execute(create(30), expected_epoch=owner.epoch_id)["task_id"]
            result = cli.supervise(config, profiles=[profile], supervisor_id="supervisor",
                                   worker_id="worker", max_steps=30, interval_seconds=0.05)
            self.assertEqual(result["steps"], 30)
            self.assertEqual(result["health"]["active"], 0)
            with cli.owned_authority(config) as owner:
                self.assertEqual(owner.get(task_id)["task"]["status"], "closed")


if __name__ == "__main__":
    unittest.main()
