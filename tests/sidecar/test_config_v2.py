"""Portable journal-only configuration; fixtures never touch a live deployment."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mempalace_tasks.config import (
    ConfigError, ServiceConfig, create_service_token, load_config, read_token,
    real_path, validate_binding,
)
from mempalace_tasks import platform_support


def portable_document(root):
    return {
        "schema_version": 2,
        "authority_id": "11111111-1111-1111-1111-111111111111",
        "hub_url": "http://127.0.0.1:8765/mcp",
        "service_token_file": str(root / "service.token"),
        "runtime_dir": str(root / "runtime"),
        "genesis": {
            "actor": "operator",
            "actors": {"operator": "operator", "system": "system"},
            "execution_profiles": {},
            "supervisors": {},
        },
        "maintenance_actor": "system",
        "recovery_actor": "operator",
    }


class PortableConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-config-v2-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / "config.json"
        self.document = portable_document(self.root)
        platform_support.create_private(self.root / "service.token", b"owned-service-token\n")
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")

    def test_schema_two_loads_journal_runtime_without_creating_coordination_state(self):
        before = set(self.root.iterdir())
        config = load_config(self.path)
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.lifecycle, "external")
        self.assertEqual(config.runtime_dir, self.root / "runtime")
        self.assertNotIn("state_dir", config.__dataclass_fields__)
        self.assertNotIn("recovery_mode", config.__dataclass_fields__)
        self.assertEqual(config.service_url, "http://127.0.0.1:8766/mcp")
        self.assertEqual(config.configuration["policy"]["renewal_seconds"], 100)
        self.assertEqual(config.service_token, "owned-service-token")
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertNotIn("owned-service-token", repr(config))

    def test_schema_one_is_explicitly_unsupported_before_credential_reads(self):
        self.document["schema_version"] = 1
        self.document["state_dir"] = self.document.pop("runtime_dir")
        self.save()
        with patch("mempalace_tasks.config.read_token",
                   side_effect=AssertionError("Unsupported config read credentials")):
            with self.assertRaises(ConfigError) as caught:
                load_config(self.path)
        self.assertIn("Unsupported configuration schema", str(caught.exception))
        self.assertFalse((self.root / "runtime").exists())

    def test_constructor_has_only_current_runtime_fields(self):
        config = ServiceConfig(
            "11111111-1111-1111-1111-111111111111",
            "http://127.0.0.1:8765/mcp", self.root / "service.token",
            self.root / "runtime", {}, {}, "system", "operator")
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.lifecycle, "external")
        self.assertEqual(config.runtime_dir, self.root / "runtime")
        self.assertFalse(hasattr(config, "state_dir"))
        self.assertFalse(hasattr(config, "recovery_mode"))
        self.assertEqual(config.service_url, "http://127.0.0.1:8766/mcp")

    def test_launcher_consent_is_explicit_and_no_action_is_performed(self):
        self.document["lifecycle"] = "launcher"
        self.save()
        before = set(self.root.iterdir())
        config = load_config(self.path)
        self.assertEqual(config.lifecycle, "launcher")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_schema_two_rejects_legacy_storage_and_recovery_override_fields(self):
        original = deepcopy(self.document)
        for update in ({"state_dir": str(self.root / "legacy")}, {"recovery_mode": "legacy"},
                       {"recovery_mode": "journal"}, {"pending_file": "anything"},
                       {"project_wings": {}}, {"projections_enabled": False},
                       {"lifecycle": "auto"}, {"lifecycle": True}, {"lifecycle": None},
                       {"schema_version": 3}, {"schema_version": True}):
            with self.subTest(update=update):
                self.document = {**original, **update}
                self.save()
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        self.document = {**original, "state_dir": original["runtime_dir"]}
        del self.document["runtime_dir"]
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_runtime_path_is_required_absolute_and_directory_if_present(self):
        original = deepcopy(self.document)
        for value in ("relative", str(self.root / ".." / "runtime"), str(self.path), None):
            with self.subTest(value=value):
                self.document = {**original, "runtime_dir": value}
                self.save()
                with self.assertRaises(ConfigError):
                    load_config(self.path)
        self.document = dict(original)
        del self.document["runtime_dir"]
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.assertFalse((self.root / "runtime").exists())

    def test_dynamic_port_only_in_schema_two_never_produces_connectable_url(self):
        self.document.update(port=0, host="::1", lifecycle="launcher")
        self.save()
        config = load_config(self.path)
        self.assertEqual(config.port, 0)
        with self.assertRaises(ConfigError) as caught:
            _ = config.service_url
        self.assertEqual(caught.exception.code, "discovery_required")
        bound = replace(config, port=34567)
        self.assertEqual(bound.service_url, "http://[::1]:34567/mcp")
        self.document["schema_version"] = 1
        self.document["state_dir"] = self.document.pop("runtime_dir")
        del self.document["lifecycle"]
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_external_v2_can_declare_dynamic_binding_without_implying_launch_consent(self):
        self.document["port"] = 0
        self.save()
        config = load_config(self.path)
        self.assertEqual(config.lifecycle, "external")
        with self.assertRaises(ConfigError) as caught:
            _ = config.service_url
        self.assertEqual(caught.exception.code, "discovery_required")

    def test_validate_binding_stays_strict_unless_explicitly_dynamic(self):
        with self.assertRaises(ConfigError):
            validate_binding("127.0.0.1", 0)
        self.assertEqual(validate_binding("127.0.0.1", 0, allow_dynamic=True), "127.0.0.1:0")
        for port in (False, -1, 65536, "0"):
            with self.subTest(port=port), self.assertRaises(ConfigError):
                validate_binding("127.0.0.1", port, allow_dynamic=True)
        for host in ("localhost", "0.0.0.0", "::", "127.000.0.1", "[::1]"):
            with self.subTest(host=host), self.assertRaises(ConfigError):
                validate_binding(host, 0, allow_dynamic=True)

    def test_v2_preserves_explicit_hub_credentials_and_environment_selection(self):
        hub = self.root / "hub.token"
        platform_support.create_private(hub, b"owned-hub-token\n")
        self.document.update(hub_token_file=str(hub))
        self.save()
        config = load_config(environ={"MPTASK_CONFIG": str(self.path)})
        self.assertEqual(config.hub_url, "http://127.0.0.1:8765/mcp")
        self.assertEqual(config.hub_token_file, hub)
        self.assertEqual(config.hub_token, "owned-hub-token")
        self.assertNotIn("owned-hub-token", repr(config))
        self.assertEqual(load_config(self.path, environ={"MPTASK_CONFIG": "wrong"}), config)
        with self.assertRaises(ConfigError):
            load_config(environ={})

    def test_v2_retains_strict_json_endpoint_uuid_and_actor_validation(self):
        original = deepcopy(self.document)
        for update in ({"authority_id": "not-a-uuid"}, {"host": "localhost"},
                       {"hub_url": "http://127.0.0.1:8765/mcp?token=private-value"},
                       {"hub_url": "http://127.0.0.1:0/mcp"}, {"port": True},
                       {"maintenance_actor": "operator"}, {"recovery_actor": "system"},
                       {"project_wings": {"demo": ""}}, {"projections_enabled": 1}):
            with self.subTest(update=update):
                self.document = {**original, **update}
                self.save()
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.path)
                self.assertNotIn("private-value", str(caught.exception))
        for content in ('{"schema_version":2,"schema_version":2}', '{"x":NaN}', "[]", "{"):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_v2_missing_service_token_still_requires_explicit_initialization(self):
        token = self.root / "service.token"
        token.unlink()
        with self.assertRaises(ConfigError):
            load_config(self.path)
        config = load_config(self.path, allow_missing_service_token=True)
        self.assertIsNone(config.service_token)
        self.assertFalse(token.exists())
        create_service_token(token)
        contents = token.read_bytes()
        self.assertGreaterEqual(len(contents), 32)
        self.assertEqual(read_token(token), contents.decode("ascii").rstrip("\n"))
        with self.assertRaises(ConfigError):
            create_service_token(token)
        self.assertEqual(token.read_bytes(), contents)

    def test_missing_regular_read_has_safe_platform_failure_code(self):
        missing = self.root / "missing" / "secret"
        with self.assertRaises(ConfigError) as caught:
            read_token(missing)
        self.assertEqual(caught.exception.code, "not_found")
        self.assertNotIn(str(missing), str(caught.exception))
        self.assertFalse(missing.parent.exists())

    def test_portable_read_and_creation_failures_remain_config_errors(self):
        unavailable = platform_support.PlatformError(
            "unsupported_platform", "Native security validation is unavailable")
        with patch.object(platform_support, "read_regular", side_effect=unavailable):
            with self.assertRaises(ConfigError) as caught:
                read_token(self.root / "service.token")
        self.assertEqual(caught.exception.code, "unsupported_platform")
        with patch.object(platform_support, "create_private", side_effect=unavailable):
            with self.assertRaises(ConfigError) as caught:
                create_service_token(self.root / "not-created")
        self.assertEqual(caught.exception.code, "unsupported_platform")
        self.assertFalse((self.root / "not-created").exists())

    def test_real_path_maps_portable_path_validation_errors(self):
        with self.assertRaises(ConfigError) as caught:
            real_path(self.root / ".." / "alias")
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertEqual(real_path(self.root / "not-created"), self.root / "not-created")


if __name__ == "__main__":
    unittest.main()
