import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import dream_harvest as dh
from test_dream_procedural_palace import DrawerCollection


class TestNativeMergeHarvest(unittest.TestCase):
    def test_canonical_native_members_supply_hashes_and_physical_ids(self):
        cluster = {"members": [
            {"id": "parent", "member_ids": ["p0", "p1"], "text": "full canonical text",
             "wing": "w", "room": "r", "metadata": {"filed_at": "2020-01-01"}},
            {"id": "other", "member_ids": ["other"], "text": "other canonical text",
             "wing": "w", "room": "r", "metadata": {}},
        ], "pair_sims": [{"a": "parent", "b": "other", "sim": .96}]}
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
            out = os.path.join(td, "wl.json")
            with mock.patch.object(dh.dream_palace, "find_duplicate_clusters", create=True,
                                   return_value=[cluster]) as find, \
                 mock.patch.object(dh.dream_palace, "bind_palace", return_value=td), \
                 mock.patch.object(dh.dream_palace, "protection_collection", return_value=DrawerCollection()), \
                 mock.patch.object(dh.dream_palace, "load_logical_drawers",
                                   side_effect=AssertionError("local reclustering must not run")), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = dh.main(["--palace", td, "--task", "merge", "--wing", "w", "--room", "r",
                              "--tau", ".94", "--out", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                worklist = json.load(fh)
        item = worklist["items"][0]
        self.assertEqual(item["supersedes"], ["p0", "p1", "other"])
        self.assertEqual(item["members"][0]["text"], "full canonical text")
        self.assertEqual(item["members"][0]["metadata"], {"filed_at": "2020-01-01"})
        self.assertEqual(item["content_hashes"]["parent"], dh._content_hash("full canonical text"))
        self.assertEqual(item["evidence"], {"pair_sims": cluster["pair_sims"], "size": 2})
        self.assertEqual(worklist["params"]["tau"], .94)
        self.assertAlmostEqual(find.call_args.kwargs["threshold"], .06)
        self.assertNotIn("max_clusters", find.call_args.kwargs)

    def test_retained_source_ids_reach_native_filter_before_components(self):
        with mock.patch.object(dh, "live_protected_drawer_ids", create=True,
                               return_value={"p0", "parent"}), \
             mock.patch.object(dh.dream_palace, "find_duplicate_clusters", create=True,
                               return_value=[]) as find:
            worklist = dh.harvest_merge_worklist("/palace", wing="w", room="r", tau=.9)
        self.assertEqual(worklist["items"], [])
        self.assertEqual(find.call_args.kwargs["exclude_ids"], {"p0", "parent"})

    def test_native_scan_error_is_not_an_empty_worklist(self):
        with mock.patch.object(dh, "live_protected_drawer_ids", create=True, return_value=set()), \
             mock.patch.object(dh.dream_palace, "find_duplicate_clusters", create=True,
                               side_effect=RuntimeError("incomplete native scan")):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                dh.harvest_merge_worklist("/palace")


class TestDefaultPalace(unittest.TestCase):
    def test_default_palace_reads_mempalace_config_env(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"palace_path": "~/palace-from-config"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertEqual(dh._default_palace(), os.path.expanduser("~/palace-from-config"))

    def test_default_palace_returns_none_without_palace_path(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"collection_name": "mempalace_drawers"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertIsNone(dh._default_palace())

    def test_default_palace_returns_none_for_missing_config(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
            missing_config = os.path.join(td, "missing.json")

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": missing_config}):
                self.assertIsNone(dh._default_palace())

    def test_main_errors_cleanly_when_no_palace_can_be_resolved(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
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
