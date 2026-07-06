"""Tests for report-only merge convergence verification."""
from __future__ import annotations

import contextlib
import io
import json
import types
import unittest
from unittest import mock

import dream_verify


class TestBuildConvergenceReport(unittest.TestCase):
    def test_zero_clusters_converges_with_true_closure(self):
        report = dream_verify.build_convergence_report(
            [],
            scope={"palace": "/palace", "wing": "wing-a", "room": None},
            params={"tau": 0.9, "max_clusters": None},
        )

        self.assertTrue(report["converged"])
        self.assertEqual(report["closure"], "true")
        self.assertEqual(report["residual_clusters"], 0)
        self.assertEqual(report["residuals"], [])

    def test_residual_clusters_are_summarized(self):
        report = dream_verify.build_convergence_report(
            [
                {
                    "members": [{"id": "drawer-a"}, {"id": "drawer-b"}],
                    "pair_sims": [{"a": "drawer-a", "b": "drawer-b", "sim": 0.91}],
                    "size": 2,
                },
                {
                    "members": [{"id": "drawer-c"}, {"id": "drawer-d"}, {"id": "drawer-e"}],
                    "pair_sims": [
                        {"a": "drawer-c", "b": "drawer-d", "sim": 0.93},
                        {"a": "drawer-d", "b": "drawer-e", "sim": 0.95},
                    ],
                    "size": 3,
                },
            ],
            scope={"palace": "/palace", "wing": "wing-a", "room": "room-a"},
            params={"tau": 0.9, "max_clusters": None},
        )

        self.assertFalse(report["converged"])
        self.assertEqual(report["residual_clusters"], 2)
        self.assertEqual(report["closure"], "true")
        self.assertEqual(
            report["residuals"],
            [
                {"drawer_ids": ["drawer-a", "drawer-b"], "size": 2, "max_sim": 0.91},
                {"drawer_ids": ["drawer-c", "drawer-d", "drawer-e"], "size": 3, "max_sim": 0.95},
            ],
        )

    def test_max_clusters_at_residual_count_marks_bounded_partial_closure(self):
        report = dream_verify.build_convergence_report(
            [
                {
                    "members": [{"id": "drawer-a"}, {"id": "drawer-b"}],
                    "pair_sims": [{"a": "drawer-a", "b": "drawer-b", "sim": 0.91}],
                    "size": 2,
                },
            ],
            scope={"palace": "/palace", "wing": None, "room": None},
            params={"tau": 0.9, "max_clusters": 1},
        )

        self.assertEqual(report["closure"], "bounded_partial")


class TestMain(unittest.TestCase):
    def _run_main(self, args, clusters):
        calls = []

        def bind_palace(palace):
            calls.append(("bind", palace))
            return f"/bound/{palace.strip('/')}"

        def find_duplicate_clusters(path, wing=None, room=None, tau=0.9, max_clusters=None):
            calls.append(
                (
                    "find",
                    {
                        "path": path,
                        "wing": wing,
                        "room": room,
                        "tau": tau,
                        "max_clusters": max_clusters,
                    },
                )
            )
            return clusters

        fake_palace = types.SimpleNamespace(
            bind_palace=bind_palace,
            find_duplicate_clusters=find_duplicate_clusters,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(dream_verify, "dream_palace", fake_palace), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = dream_verify.main(args)
        return code, stdout.getvalue(), stderr.getvalue(), calls

    def test_main_converged_scope_prints_summary_and_exits_zero(self):
        code, stdout, stderr, calls = self._run_main(
            ["--palace", "palace", "--wing", "wing-a", "--room", "room-a"],
            [],
        )

        self.assertEqual(code, 0)
        self.assertIn("merge convergence: true — 0 residual cluster(s) [closure=true]", stderr)
        report = json.loads(stdout)
        self.assertTrue(report["converged"])
        self.assertEqual(report["scope"], {"palace": "/bound/palace", "wing": "wing-a", "room": "room-a"})
        self.assertEqual(
            calls,
            [
                ("bind", "palace"),
                (
                    "find",
                    {
                        "path": "/bound/palace",
                        "wing": "wing-a",
                        "room": "room-a",
                        "tau": 0.9,
                        "max_clusters": None,
                    },
                ),
            ],
        )

    def test_main_residual_scope_exits_zero_by_default_but_nonzero_under_strict(self):
        clusters = [
            {
                "members": [{"id": "drawer-a"}, {"id": "drawer-b"}],
                "pair_sims": [{"a": "drawer-a", "b": "drawer-b", "sim": 0.91}],
                "size": 2,
            },
        ]

        default_code, _, default_stderr, _ = self._run_main(["--palace", "palace"], clusters)
        strict_code, _, strict_stderr, _ = self._run_main(["--palace", "palace", "--strict"], clusters)

        self.assertEqual(default_code, 0)
        self.assertIn("merge convergence: false — 1 residual cluster(s) [closure=true]", default_stderr)
        self.assertEqual(strict_code, 1)
        self.assertIn("merge convergence: false — 1 residual cluster(s) [closure=true]", strict_stderr)

    def test_main_writes_json_report_to_out_path(self):
        handle = mock.mock_open()
        with mock.patch("builtins.open", handle):
            code, stdout, _, _ = self._run_main(
                ["--palace", "palace", "--tau", "0.82", "--max-clusters", "1", "--out", "report.json"],
                [
                    {
                        "members": [{"id": "drawer-a"}, {"id": "drawer-b"}],
                        "pair_sims": [{"a": "drawer-a", "b": "drawer-b", "sim": 0.91}],
                        "size": 2,
                    },
                ],
            )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        handle.assert_called_once_with("report.json", "w", encoding="utf-8")
        written = "".join(call.args[0] for call in handle().write.call_args_list)
        report = json.loads(written)
        self.assertEqual(report["params"], {"tau": 0.82, "max_clusters": 1})
        self.assertEqual(report["closure"], "bounded_partial")


if __name__ == "__main__":
    unittest.main()
