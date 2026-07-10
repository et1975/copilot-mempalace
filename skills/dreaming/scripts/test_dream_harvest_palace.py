import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import dream_harvest as dh


class TestDefaultPalace(unittest.TestCase):
    def test_default_palace_reads_mempalace_config_env(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"palace_path": "~/palace-from-config"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertEqual(dh._default_palace(), os.path.expanduser("~/palace-from-config"))

    def test_default_palace_returns_none_without_palace_path(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"collection_name": "mempalace_drawers"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertIsNone(dh._default_palace())

    def test_default_palace_returns_none_for_missing_config(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            missing_config = os.path.join(td, "missing.json")

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": missing_config}):
                self.assertIsNone(dh._default_palace())

    def test_main_errors_cleanly_when_no_palace_can_be_resolved(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            missing_config = os.path.join(td, "missing.json")
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": missing_config}):
                with contextlib.redirect_stderr(stderr):
                    try:
                        rc = dh.main([])
                    except SystemExit as ex:
                        rc = ex.code

            self.assertNotEqual(rc, 0)
            self.assertIn("no --palace given", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
