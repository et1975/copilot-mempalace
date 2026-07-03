"""CLI routing tests for dreaming harvest/adopt."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import dream_adopt
import dream_harvest


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-cli-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


class TestHarvestContradictionTask(unittest.TestCase):
    def test_contradiction_task_writes_contradiction_worklist(self):
        triples = [
            {"subject": "Alice", "predicate": "lives_in", "object": "Portland",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-02"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_harvest.dream_palace, "load_active_triples", return_value=triples), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "contradiction",
                    "--wing", "ignored-for-kg",
                    "--tau", "0.1",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                worklist = json.load(fh)
            self.assertEqual(worklist["task"], "contradiction")
            self.assertEqual(worklist["scope"], {"palace": "/bound", "task": "contradiction"})
            self.assertEqual(worklist["items"][0]["kind"], "contradiction")
            self.assertIn("harvested 2 active triples -> 1 contradiction candidate group(s)", stderr.getvalue())


class TestHarvestPatternTask(unittest.TestCase):
    def test_pattern_task_writes_pattern_worklist(self):
        entries = [
            {
                "id": "entry-1",
                "text": "SESSION_ID: abcdef12 repeated observation",
                "embedding": [1.0, 0.0],
                "session_id": "abcdef12",
                "agent": "Copilot CLI",
                "date": "2026-07-03",
                "topic": "dreaming",
            },
            {
                "id": "entry-2",
                "text": "SESSION_ID: abcdef13 repeated observation again",
                "embedding": [1.0, 0.0],
                "session_id": "abcdef13",
                "agent": "Copilot CLI",
                "date": "2026-07-04",
                "topic": "dreaming",
            },
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_harvest.dream_palace, "load_observation_entries", return_value=entries) as load_entries, \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "pattern",
                    "--wing", "wing_copilot-cli",
                    "--rooms", "diary,signals",
                    "--tau", "0.8",
                    "--min-support", "2",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            load_entries.assert_called_once_with(
                "/bound",
                wing="wing_copilot-cli",
                rooms=("diary", "signals"),
            )
            with open(out, encoding="utf-8") as fh:
                worklist = json.load(fh)
            self.assertEqual(worklist["task"], "pattern")
            self.assertEqual(
                worklist["scope"],
                {"palace": "/bound", "wing": "wing_copilot-cli", "rooms": ["diary", "signals"], "task": "pattern"},
            )
            self.assertEqual(worklist["params"], {"tau": 0.8, "min_support": 2})
            self.assertEqual(worklist["items"][0]["kind"], "pattern")
            self.assertIn(
                "harvested 2 observation entries -> 1 pattern theme(s) spanning >= 2 sessions",
                stderr.getvalue(),
            )


class TestHarvestPruneTask(unittest.TestCase):
    def test_prune_task_writes_prune_worklist(self):
        drawers = [
            {
                "id": "drawer-1",
                "member_ids": ["drawer-1"],
                "text": "temporary note for now",
                "embedding": [1.0, 0.0],
                "metadata": {"filed_at": "2000-01-01T00:00:00", "pinned": False},
                "wing": "wing",
                "room": "room",
            },
            {
                "id": "drawer-2",
                "member_ids": ["drawer-2"],
                "text": "temporary note for now duplicate",
                "embedding": [1.0, 0.0],
                "metadata": {"filed_at": "2000-01-01T00:00:00", "pinned": True},
                "wing": "wing",
                "room": "room",
            },
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_harvest.dream_palace, "load_logical_drawers", return_value=drawers) as load_drawers, \
                 mock.patch.object(dream_harvest.dream_palace, "kg_source_degree", return_value={}), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "prune",
                    "--wing", "wing",
                    "--room", "room",
                    "--v-min", "0.35",
                    "--age-floor-days", "30",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            load_drawers.assert_called_once_with("/bound", wing="wing", room="room")
            with open(out, encoding="utf-8") as fh:
                worklist = json.load(fh)
            self.assertEqual(worklist["task"], "prune")
            self.assertEqual(
                worklist["scope"],
                {"palace": "/bound", "wing": "wing", "room": "room", "task": "prune"},
            )
            self.assertEqual(worklist["params"], {"v_min": 0.35, "age_floor_days": 30})
            self.assertEqual([item["id"] for item in worklist["items"]], ["drawer-1"])
            self.assertEqual(worklist["items"][0]["kind"], "prune")
            self.assertEqual(worklist["items"][0]["salience"]["kg_degree"], 0)
            self.assertIn(
                "harvested 2 drawers -> 1 prune candidate(s) (v<v_min, age>=floor, kg_degree=0)",
                stderr.getvalue(),
            )


class TestAdoptContradictionTask(unittest.TestCase):
    def test_resolve_defaults_invalidate_to_all_candidates_except_keep(self):
        worklist = {
            "task": "contradiction",
            "items": [
                {
                    "kind": "contradiction",
                    "subject": "Alice",
                    "predicate": "lives_in",
                    "candidates": [
                        {"object": "Seattle"},
                        {"object": "Portland"},
                    ],
                    "decision": {"action": "invalidate", "keep": "Seattle"},
                },
                {
                    "kind": "contradiction",
                    "subject": "Alice",
                    "predicate": "knows",
                    "candidates": [
                        {"object": "Bob"},
                        {"object": "Carol"},
                    ],
                    "decision": {"action": "skip"},
                },
            ],
        }

        decisions = dream_adopt._resolve_contradiction_decisions(worklist)

        self.assertEqual(decisions, [
            {"action": "invalidate", "subject": "Alice", "predicate": "lives_in",
             "invalidate": ["Portland"]},
            {"action": "skip"},
        ])

    def test_dry_run_prints_contradiction_invalidations(self):
        worklist = {
            "task": "contradiction",
            "items": [
                {
                    "kind": "contradiction",
                    "subject": "Alice",
                    "predicate": "lives_in",
                    "candidates": [{"object": "Portland"}, {"object": "Seattle"}],
                    "decision": {
                        "action": "invalidate",
                        "keep": "Seattle",
                        "invalidate": ["Portland"],
                    },
                }
            ],
        }
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(worklist, fh)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value="/bound"), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                    "--dry-run",
                ])

            self.assertEqual(rc, 0)
            self.assertIn("INVALIDATE Alice lives_in=Portland", stdout.getvalue())
            self.assertIn("[dry-run] would invalidate 1, skip 0", stderr.getvalue())


class TestAdoptPatternTask(unittest.TestCase):
    def test_resolve_pattern_decisions_defaults_surface_and_skip(self):
        worklist = {
            "task": "pattern",
            "items": [
                {
                    "kind": "pattern",
                    "members": [
                        {
                            "id": "entry-1",
                            "text": "SESSION_ID: abcdef12 repeated observation",
                            "session_id": "abcdef12",
                            "wing": "wing_copilot-cli",
                            "room": "diary",
                        }
                    ],
                    "evidence": {"support_ids": ["abcdef12", "abcdef13"]},
                    "decision": {"action": "surface", "text": "Recurring pattern worth surfacing."},
                },
                {
                    "kind": "pattern",
                    "members": [],
                    "evidence": {"support_ids": ["abcdef14", "abcdef15"]},
                    "decision": {"action": "skip"},
                },
            ],
        }

        decisions = dream_adopt._resolve_pattern_decisions(worklist)

        self.assertEqual(decisions, [
            {
                "action": "surface",
                "wing": "wing_copilot-cli",
                "room": "diary",
                "text": "Recurring pattern worth surfacing.",
                "supported_by": ["abcdef12", "abcdef13"],
            },
            {"action": "skip"},
        ])

    def test_dry_run_pattern_adds_only_and_never_deletes(self):
        worklist = {
            "task": "pattern",
            "items": [
                {
                    "kind": "pattern",
                    "members": [
                        {
                            "id": "entry-1",
                            "text": "SESSION_ID: abcdef12 repeated observation",
                            "session_id": "abcdef12",
                            "wing": "wing_copilot-cli",
                            "room": "diary",
                        }
                    ],
                    "evidence": {"support_ids": ["abcdef12", "abcdef13"]},
                    "decision": {
                        "action": "surface",
                        "text": "Recurring pattern worth surfacing.",
                    },
                }
            ],
        }
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(worklist, fh)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value="/bound"), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                    "--dry-run",
                ])

            self.assertEqual(rc, 0)
            self.assertIn("ADD  wing_copilot-cli/diary: Recurring pattern worth surfacing....", stdout.getvalue())
            self.assertNotIn("DEL", stdout.getvalue())
            self.assertIn("[dry-run] would surface 1, skip 0, errors 0", stderr.getvalue())


class TestAdoptPruneTask(unittest.TestCase):
    def test_resolve_prune_decisions_defaults_to_item_fields_and_keeps_by_default(self):
        salience = {"v": 0.12, "age_days": 400, "kg_degree": 0}
        worklist = {
            "task": "prune",
            "items": [
                {
                    "kind": "prune",
                    "id": "drawer-1",
                    "member_ids": ["chunk-1", "chunk-2"],
                    "wing": "wing",
                    "room": "room",
                    "text": "forgettable",
                    "salience": salience,
                    "decision": {"action": "prune"},
                },
                {
                    "kind": "prune",
                    "id": "drawer-2",
                    "member_ids": ["drawer-2"],
                    "wing": "wing",
                    "room": "room",
                    "text": "conservative default",
                    "salience": salience,
                    "decision": {"action": "keep"},
                },
                {
                    "kind": "prune",
                    "id": "drawer-3",
                    "member_ids": ["drawer-3"],
                    "wing": "wing",
                    "room": "room",
                    "text": "omitted decision",
                    "salience": salience,
                    "decision": None,
                },
            ],
        }

        decisions = dream_adopt._resolve_prune_decisions(worklist)

        self.assertEqual(decisions, [
            {
                "action": "prune",
                "id": "drawer-1",
                "member_ids": ["chunk-1", "chunk-2"],
                "wing": "wing",
                "room": "room",
                "text": "forgettable",
                "salience": salience,
            },
            {"action": "keep"},
            {"action": "keep"},
        ])

    def test_dry_run_prune_records_archive_delete_plan_without_real_archiver(self):
        worklist = {
            "task": "prune",
            "items": [
                {
                    "kind": "prune",
                    "id": "drawer-1",
                    "member_ids": ["chunk-1"],
                    "wing": "wing",
                    "room": "room",
                    "text": "forgettable",
                    "salience": {"v": 0.12, "age_days": 400, "kg_degree": 0},
                    "decision": {"action": "prune"},
                }
            ],
        }
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(worklist, fh)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_adopt.dream_palace, "Archiver", side_effect=AssertionError("real archiver used")), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                    "--archive-file", os.path.join(td, "archive.jsonl"),
                    "--dry-run",
                ])

            self.assertEqual(rc, 0)
            self.assertIn("PRUNE drawer-1 (archive+delete)", stdout.getvalue())
            self.assertIn("[dry-run] would prune 1, keep 0, errors 0", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
