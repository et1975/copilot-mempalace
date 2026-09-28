"""Report-only verifier uses the same actionable candidates as harvest/adopt."""
import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import dream_adopt
import dream_harvest
from test_dream_procedural_palace import installed_palace


class TestMergeVerification(unittest.TestCase):
    def _run(self, clusters=None, error=None, strict=True):
        verifier = importlib.import_module("dream_verify")
        stdout = io.StringIO()
        with mock.patch.object(verifier.dream_palace, "bind_palace", return_value="/bound"), \
             mock.patch.object(dream_harvest, "live_protected_drawer_ids", create=True,
                               return_value=set()), \
             mock.patch.object(dream_harvest.dream_palace, "find_duplicate_clusters",
                               create=True, return_value=clusters, side_effect=error), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            rc = verifier.main(["--palace", "/palace", "--wing", "w", "--room", "r",
                                "--tau", ".92"] + (["--strict"] if strict else []))
        return rc, json.loads(stdout.getvalue())

    def test_strict_json_reports_actionable_residuals(self):
        cluster = {"members": [
            {"id": "a", "member_ids": ["a"], "text": "A", "wing": "w", "room": "r"},
            {"id": "b", "member_ids": ["b"], "text": "B", "wing": "w", "room": "r"},
        ], "pair_sims": [{"a": "a", "b": "b", "sim": .95}]}
        rc, report = self._run([cluster])
        self.assertEqual(rc, 1)
        self.assertFalse(report["converged"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["residual"], 1)
        self.assertEqual(report["scope"], {"palace": "/bound", "wing": "w", "room": "r"})
        self.assertEqual(report["params"]["tau"], .92)

    def test_no_actionable_candidates_means_converged(self):
        rc, report = self._run([])
        self.assertEqual(rc, 0)
        self.assertEqual(report["residual"], 0)
        self.assertTrue(report["converged"])

    def test_failed_or_incomplete_scan_never_reports_convergence(self):
        for error in ("native backend failure", "truncated scan", "vector_disabled"):
            with self.subTest(error=error):
                rc, report = self._run(error=RuntimeError(error), strict=False)
                self.assertNotEqual(rc, 0)
                self.assertFalse(report["complete"])
                self.assertFalse(report["converged"])
                self.assertIsNone(report["residual"])
                self.assertIn(error, report["errors"][0])

    def test_nonfinite_tau_returns_valid_json_error_report(self):
        verifier = importlib.import_module("dream_verify")
        def invalid_constant(value):
            raise ValueError(f"invalid JSON constant: {value}")
        for tau in ("nan", "inf"):
            with self.subTest(tau=tau):
                stdout = io.StringIO()
                with mock.patch.object(verifier.dream_palace, "bind_palace", return_value="/bound"), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    rc = verifier.main(["--palace", "/palace", "--tau", tau, "--strict"])
                self.assertEqual(rc, 2)
                report = json.loads(stdout.getvalue(), parse_constant=invalid_constant)
                self.assertFalse(report["complete"])
                self.assertFalse(report["converged"])

    def test_adopt_and_standalone_share_actionable_harvest(self):
        verifier = importlib.import_module("dream_verify")
        worklist = {"scope": {"wing": "w", "room": "r"}, "params": {"tau": .93}}
        with mock.patch.object(dream_harvest, "harvest_merge_worklist", create=True,
                               return_value={"items": [{"kind": "merge"}]}) as harvest:
            residual = dream_adopt._verify_reharvest("merge", worklist, "/bound")
            report = verifier.verify_merge("/bound", wing="w", room="r", tau=.93)
        self.assertEqual(residual, report["residual"])
        self.assertEqual(residual, 1)
        self.assertEqual(harvest.call_args_list, [
            mock.call("/bound", wing="w", room="r", tau=.93),
            mock.call("/bound", wing="w", room="r", tau=.93),
        ])

    def test_adopt_verify_error_returns_nonzero_not_convergence(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td:
            path = os.path.join(td, "decisions.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"task": "merge", "items": []}, fh)
            stderr = io.StringIO()
            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value=td), \
                 mock.patch.object(dream_adopt, "_preflight_merge_decisions", return_value=([], [])), \
                 mock.patch.object(dream_adopt.dream_palace, "MempalaceWriter"), \
                 mock.patch.object(dream_adopt, "_verify_reharvest",
                                   side_effect=RuntimeError("native unavailable")), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main(["--palace", td, "--decisions", path, "--verify", "--strict"])
        self.assertNotEqual(rc, 0)
        self.assertIn("native unavailable", stderr.getvalue())
        self.assertNotIn("0 residual", stderr.getvalue())


class TestNativeActionableVerification(unittest.TestCase):
    def test_embedded_procedural_metadata_is_not_a_merge_candidate(self):
        verifier = importlib.import_module("dream_verify")
        from mempalace.palace import get_collection
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td, \
             installed_palace(td):
            text = 'Retained procedural record.\n\n<!--dreaming-meta: {"kind":"procedural_event"}-->'
            get_collection(td).add(
                ids=["legacy-event-a", "legacy-event-b"], documents=[text] * 2,
                metadatas=[{"wing": "w", "room": "r"}] * 2)
            report = verifier.verify_merge(td, wing="w")
            self.assertEqual(report["residual"], 0)
            self.assertTrue(report["converged"])

    def test_cross_room_and_protected_singletons_are_not_residuals(self):
        verifier = importlib.import_module("dream_verify")
        from mempalace.palace import get_collection
        with tempfile.TemporaryDirectory(dir=os.environ.get("DREAMING_TEST_TMPDIR")) as td, \
             installed_palace(td):
            text = "Identical source evidence for the native actionable scan."
            get_collection(td).add(
                ids=["retained", "ordinary", "other-room"], documents=[text] * 3,
                metadatas=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"},
                           {"wing": "w", "room": "other"}])
            with mock.patch.object(dream_harvest, "live_protected_drawer_ids",
                                   return_value={"retained"}):
                worklist = dream_harvest.harvest_merge_worklist(td, wing="w", tau=.9)
                report = verifier.verify_merge(td, wing="w", tau=.9)
                residual = dream_adopt._verify_reharvest(
                    "merge", {"scope": {"wing": "w"}, "params": {"tau": .9}}, td)
            self.assertEqual(worklist["items"], [])
            self.assertEqual(report["residual"], residual)
            self.assertEqual(residual, 0)
            self.assertTrue(report["converged"])
