"""Strict configuration and private credential boundary; no upstream I/O."""

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from authority_fixture import AUTHORITY, genesis, state_directory
from mempalace_tasks.config import ConfigError, load_config, create_service_token


def config_document(root):
    initial = genesis()
    return {
        "schema_version": 1, "authority_id": AUTHORITY,
        "hub_url": "http://127.0.0.1:8765/mcp",
        "service_token_file": str(root / "service.token"),
        "state_dir": str(root / "state"),
        "genesis": {key: value for key, value in initial.items()
                    if key not in {"command_id", "operation"}},
        "maintenance_actor": "system", "recovery_actor": "operator",
        "project_wings": {"demo": "owned-test-wing"},
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "config.json"
        self.document = config_document(self.root)
        self.token = self.root / "service.token"
        self.token.write_text("owned-service-token\n")
        self.token.chmod(0o600)
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.document))

    def test_explicit_config_normalizes_genesis_without_creating_state(self):
        config = load_config(self.path)
        self.assertEqual(config.service_url, "http://127.0.0.1:8766/mcp")
        self.assertEqual(config.service_token, "owned-service-token")
        self.assertFalse(config.projections_enabled)
        self.assertEqual(config.configuration["policy"]["renewal_seconds"], 100)
        self.assertFalse((self.root / "state").exists())
        self.assertNotIn("owned-service-token", repr(config))
        self.assertEqual(load_config(environ={"MPTASK_CONFIG": str(self.path)}), config)
        with self.assertRaises(ConfigError):
            load_config(environ={})

    def test_config_and_token_reads_preserve_shared_parent_modes_without_repair(self):
        hub_token = self.root / "hub.token"
        hub_token.write_text("owned-hub-token\n")
        hub_token.chmod(0o400)
        self.document["hub_token_file"] = str(hub_token)
        self.save()
        self.root.chmod(0o775)
        self.path.chmod(0o664)
        paths = (self.root, self.path, self.token, hub_token)
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in paths}
        contents = {path: path.read_bytes() for path in paths if path.is_file()}
        entries = set(self.root.iterdir())

        config = load_config(self.path)

        self.assertEqual(config.hub_token, "owned-hub-token")
        self.assertEqual({path: stat.S_IMODE(path.stat().st_mode) for path in paths}, modes)
        self.assertEqual({path: path.read_bytes() for path in contents}, contents)
        self.assertEqual(set(self.root.iterdir()), entries)
        self.assertFalse((self.root / "state").exists())

        self.token.chmod(0o644)
        modes[self.token] = 0o644
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.assertEqual({path: stat.S_IMODE(path.stat().st_mode) for path in paths}, modes)
        self.assertEqual({path: path.read_bytes() for path in contents}, contents)
        self.assertEqual(set(self.root.iterdir()), entries)
        with self.assertRaises(ConfigError):
            load_config(self.root / "missing-parent" / "config.json")
        self.assertFalse((self.root / "missing-parent").exists())
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o775)

    def test_unknown_fields_invalid_identity_roles_policy_and_ports_fail(self):
        variants = [
            {"schema_version": True}, {"schema_version": 2}, {"extra": True},
            {"authority_id": "not-a-uuid"}, {"port": 0}, {"port": True},
            {"port": 65536}, {"host": "localhost"}, {"host": "0.0.0.0"},
            {"state_dir": "relative"}, {"state_dir": str(self.root / ".." / "state")},
            {"hub_url": "http://user:secret@127.0.0.1:8765/mcp"},
            {"hub_url": "http://127.0.0.1:8765/mcp?token=secret"},
            {"hub_token": "secret"}, {"projections_enabled": "yes"},
            {"maintenance_actor": "operator"}, {"recovery_actor": "system"},
            {"project_wings": {"demo": ""}},
        ]
        original = deepcopy(self.document)
        for update in variants:
            with self.subTest(update=update):
                self.document = {**original, **update}
                self.save()
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.path)
                self.assertNotIn("secret", str(caught.exception))
        self.document = deepcopy(original)
        self.document["genesis"]["policy"] = {"renewal_seconds": 300}
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.assertFalse((self.root / "state").exists())

    def test_tokens_must_be_private_owned_regular_files_not_urls_or_links(self):
        for mode in (0o644, 0o640, 0o666):
            self.token.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                load_config(self.path)
        self.token.chmod(0o600)
        for contents in ("", "has space", "token\ninjected", "nonascii-\u2603"):
            self.token.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(ConfigError):
                load_config(self.path)
        self.token.unlink()
        self.token.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.token.unlink()
        os.mkfifo(self.token, 0o600)
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_symlinked_ancestors_state_and_config_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "alias"
        link.symlink_to(real, target_is_directory=True)
        self.document["state_dir"] = str(link / "state")
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.path)
        self.document["state_dir"] = str(self.root / "state")
        self.save()
        config_link = self.root / "config-link.json"
        config_link.symlink_to(self.path)
        with self.assertRaises(ConfigError):
            load_config(config_link)

    def test_missing_token_requires_explicit_generation_and_never_overwrites(self):
        self.token.unlink()
        with self.assertRaises(ConfigError):
            load_config(self.path)
        config = load_config(self.path, allow_missing_service_token=True)
        self.assertIsNone(config.service_token)
        self.assertFalse(self.token.exists())
        create_service_token(config.service_token_file)
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        value = self.token.read_bytes()
        self.assertGreaterEqual(len(value), 32)
        with self.assertRaises(ConfigError):
            create_service_token(config.service_token_file)
        self.assertEqual(self.token.read_bytes(), value)
        self.assertIsNotNone(load_config(self.path).service_token)

    def test_malformed_duplicate_and_oversized_config_are_safe_errors(self):
        for content in ('{"schema_version":1,"schema_version":1}', '{"x":NaN}',
                        '[]', "{", " " * (1024 * 1024 + 1)):
            self.path.write_text(content)
            with self.subTest(length=len(content)), self.assertRaises(ConfigError):
                load_config(self.path)

    def test_token_ancestor_swapped_to_symlink_during_open_is_not_followed(self):
        private = self.root / "private"
        private.mkdir()
        token = private / "token"
        token.write_text("private-owned-token")
        token.chmod(0o600)
        self.document["service_token_file"] = str(token)
        self.save()
        moved = self.root / "moved"
        original = os.open
        swapped = False
        def racing_open(path, *args, **kwargs):
            nonlocal swapped
            if not swapped and (path == token or path == "private"):
                swapped = True
                private.rename(moved)
                private.symlink_to(moved, target_is_directory=True)
            return original(path, *args, **kwargs)
        with patch("mempalace_tasks.config.os.open", racing_open):
            with self.assertRaises(ConfigError):
                load_config(self.path)
        self.assertTrue(swapped)


if __name__ == "__main__":
    unittest.main()
