"""CLI routing tests for dreaming harvest/adopt."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import dream_adopt
import dream_harvest

try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-cli-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


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
                 mock.patch.object(dream_harvest.dream_palace, "load_premises", return_value=triples), \
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
                {"palace": "/bound", "wing": "wing_copilot-cli", "rooms": ["diary", "signals"], "source": "diary", "task": "pattern"},
            )
            self.assertEqual(worklist["params"], {"tau": 0.8, "min_support": 2})
            self.assertEqual(worklist["items"][0]["kind"], "pattern")
            self.assertIn(
                "harvested 2 observation entries (diary) -> 1 pattern theme(s) spanning >= 2 sessions",
                stderr.getvalue(),
            )

    def test_pattern_task_excludes_already_surfaced_lessons(self):
        entries = [
            {
                "id": "entry-1",
                "text": "SESSION_ID: s1 repeated observation",
                "embedding": [1.0, 0.0],
                "session_id": "s1",
            },
            {
                "id": "entry-2",
                "text": "SESSION_ID: s2 repeated observation again",
                "embedding": [1.0, 0.0],
                "session_id": "s2",
            },
            {
                "id": "lesson-meta",
                "text": "SESSION_ID: s3 surfaced lesson",
                "embedding": [1.0, 0.0],
                "session_id": "s3",
                "metadata": {"kind": "lesson"},
            },
            {
                "id": "lesson-trailer",
                "text": 'SESSION_ID: s4 surfaced lesson\n<!--dreaming-meta: {"kind":"lesson"}-->',
                "embedding": [1.0, 0.0],
                "session_id": "s4",
            },
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_harvest.dream_palace, "load_observation_entries", return_value=entries), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "pattern",
                    "--wing", "wing_copilot-cli",
                    "--min-support", "2",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                worklist = json.load(fh)
            self.assertEqual(worklist["items"][0]["evidence"]["support_ids"], ["s1", "s2"])
            self.assertIn("harvested 2 observation entries (diary) -> 1 pattern theme(s)", stderr.getvalue())


    def test_pattern_task_sessions_source_uses_host_sessions(self):
        session_entries = [
            {
                "id": "session:s1", "member_ids": ["session:s1"],
                "text": "make this repo a copilot marketplace skillset",
                "embedding": [1.0, 0.0], "session_id": "s1",
                "agent": None, "date": "2026-07-01", "topic": "packaging",
                "wing": None, "room": "__session__",
            },
            {
                "id": "session:s2", "member_ids": ["session:s2"],
                "text": "package this repo for the copilot marketplace",
                "embedding": [1.0, 0.0], "session_id": "s2",
                "agent": None, "date": "2026-07-02", "topic": "packaging",
                "wing": None, "room": "__session__",
            },
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_harvest.dream_palace, "load_observation_entries") as load_diary, \
                 mock.patch.object(
                     dream_harvest.dream_palace, "load_session_observation_entries", return_value=session_entries
                 ) as load_sessions, \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "pattern",
                    "--source", "sessions",
                    "--repository", "copilot-mempalace",
                    "--since", "2026-07-01",
                    "--limit-sessions", "50",
                    "--tau", "0.8",
                    "--min-support", "2",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            load_diary.assert_not_called()
            load_sessions.assert_called_once_with(
                "/bound",
                repository="copilot-mempalace",
                since="2026-07-01",
                limit_sessions=50,
            )
            worklist = _load_json(out)
            self.assertEqual(worklist["scope"]["source"], "sessions")
            self.assertEqual(worklist["items"][0]["kind"], "pattern")
            self.assertEqual(worklist["items"][0]["evidence"]["support_ids"], ["s1", "s2"])
            self.assertIn("harvested 2 observation entries (sessions)", stderr.getvalue())

    def test_pattern_task_both_source_unions_diary_and_sessions(self):
        diary_entries = [
            {
                "id": "diary-1", "text": "SESSION_ID: d1 dreaming pipeline design",
                "embedding": [1.0, 0.0], "session_id": "d1",
            },
        ]
        session_entries = [
            {
                "id": "session:s2", "member_ids": ["session:s2"],
                "text": "dreaming pipeline design notes",
                "embedding": [1.0, 0.0], "session_id": "s2", "room": "__session__",
            },
        ]
        with _test_tmpdir() as td:
            out = os.path.join(td, "worklist.json")
            stderr = io.StringIO()
            with mock.patch.object(dream_harvest.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(
                     dream_harvest.dream_palace, "load_observation_entries", return_value=diary_entries
                 ) as load_diary, \
                 mock.patch.object(
                     dream_harvest.dream_palace, "load_session_observation_entries", return_value=session_entries
                 ) as load_sessions, \
                 contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main([
                    "--palace", "/palace",
                    "--task", "pattern",
                    "--source", "both",
                    "--tau", "0.8",
                    "--min-support", "2",
                    "--out", out,
                ])

            self.assertEqual(rc, 0)
            load_diary.assert_called_once()
            load_sessions.assert_called_once()
            worklist = _load_json(out)
            self.assertEqual(worklist["scope"]["source"], "both")
            # theme spans one diary session (d1) and one host session (s2) => support 2
            self.assertEqual(worklist["items"][0]["evidence"]["support_ids"], ["d1", "s2"])
            self.assertIn("harvested 2 observation entries (both)", stderr.getvalue())


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
            self.assertEqual(
                worklist["items"][0]["content_hash"],
                hashlib.sha256("temporary note for now".encode("utf-8")).hexdigest(),
            )
            self.assertIn(
                "harvested 2 drawers -> 1 prune candidate(s) (v<v_min, age>=floor, kg_degree=0)",
                stderr.getvalue(),
            )


class TestHarvestOntologyTasks(unittest.TestCase):
    def _palace_with_kg(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        con = sqlite3.connect(os.path.join(palace, "knowledge_graph.sqlite3"))
        con.executescript(
            """
            CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL,
                source_closet TEXT,
                source_file TEXT,
                source_drawer_id TEXT,
                adapter_name TEXT,
                extracted_at TEXT
            );
            INSERT INTO entities (id, name) VALUES
                ('a', 'Author'), ('x', 'Post'), ('b', 'ModuleB'), ('c', 'ModuleC'),
                ('friend1', 'FriendOne'), ('friend2', 'FriendTwo');
            INSERT INTO triples (
                id, subject, predicate, object, valid_from, valid_to,
                confidence, source_closet, source_file, source_drawer_id,
                adapter_name, extracted_at
            ) VALUES
                ('t1', 'a', 'authored', 'x', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t2', 'x', 'authored_by', 'a', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t3', 'a', 'depends_on', 'b', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t4', 'b', 'depends_on', 'c', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t5', 'friend1', 'collaborates_with', 'friend2', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t6', 'friend2', 'collaborates_with', 'friend1', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01');
            """
        )
        con.commit()
        con.close()
        return palace

    def _run_harvest(self, argv):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = dream_harvest.main(argv)
        return rc, stderr.getvalue()

    def _read_rules(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["rules"]

    def test_suggest_rules_writes_disabled_heuristic_candidates(self):
        with _test_tmpdir() as td:
            palace = self._palace_with_kg(td)
            ontology = os.path.join(td, "ontology.json")

            rc, stderr = self._run_harvest([
                "--task", "suggest-rules",
                "--palace", palace,
                "--ontology-out", ontology,
            ])

            self.assertEqual(rc, 0)
            rules = self._read_rules(ontology)
            self.assertTrue(rules)
            self.assertTrue(all(rule["enabled"] is False for rule in rules))
            self.assertIn("transitive:depends_on", {rule["id"] for rule in rules})
            self.assertIn("inverse:authored:authored_by", {rule["id"] for rule in rules})
            self.assertIn("symmetric:collaborates_with", {rule["id"] for rule in rules})
            self.assertIn("suggest-rules: proposed", stderr)
            self.assertIn("all candidates written DISABLED", stderr)

    def test_induce_rules_writes_disabled_evidence_candidates(self):
        with _test_tmpdir() as td:
            palace = self._palace_with_kg(td)
            ontology = os.path.join(td, "ontology.json")

            rc, stderr = self._run_harvest([
                "--task", "induce-rules",
                "--palace", palace,
                "--min-support", "1",
                "--ontology-out", ontology,
            ])

            self.assertEqual(rc, 0)
            rules = self._read_rules(ontology)
            self.assertTrue(rules)
            self.assertTrue(all(rule["enabled"] is False for rule in rules))
            ids = {rule["id"] for rule in rules}
            self.assertIn("inverse:authored:authored_by", ids)
            self.assertIn("symmetric:collaborates_with", ids)
            self.assertIn("transitive:depends_on", ids)
            self.assertIn("induce-rules: min_support=1 proposed", stderr)
            self.assertIn("all candidates written DISABLED", stderr)

    def test_suggest_rules_is_idempotent_and_reports_existing_skips(self):
        with _test_tmpdir() as td:
            palace = self._palace_with_kg(td)
            ontology = os.path.join(td, "ontology.json")

            rc, stderr = self._run_harvest([
                "--task", "suggest-rules",
                "--palace", palace,
                "--ontology-out", ontology,
            ])
            self.assertEqual(rc, 0)
            with open(ontology, encoding="utf-8") as fh:
                first_doc = json.load(fh)
            first_count = len(first_doc["rules"])

            rc, stderr = self._run_harvest([
                "--task", "suggest-rules",
                "--palace", palace,
                "--ontology-out", ontology,
            ])

            self.assertEqual(rc, 0)
            with open(ontology, encoding="utf-8") as fh:
                second_doc = json.load(fh)
            self.assertEqual(second_doc, first_doc)
            self.assertIn(f"added 0 (skipped {first_count} existing)", stderr)

    def test_suggest_rules_preserves_preexisting_enabled_colliding_rule(self):
        with _test_tmpdir() as td:
            palace = self._palace_with_kg(td)
            ontology = os.path.join(td, "ontology.json")
            existing_rule = {
                "id": "transitive:depends_on",
                "family": "transitive",
                "predicate": "depends_on",
                "derived_predicate": "depends_on_closure",
                "enabled": True,
                "rationale": "human approved",
            }
            with open(ontology, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "rules": [existing_rule]}, fh)

            rc, _stderr = self._run_harvest([
                "--task", "suggest-rules",
                "--palace", palace,
                "--ontology-out", ontology,
            ])

            self.assertEqual(rc, 0)
            rules = self._read_rules(ontology)
            self.assertEqual(rules[0], existing_rule)
            self.assertTrue(rules[0]["enabled"])


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
                        {"object": "Seattle", "object_id": "city-sea", "triple_id": "t-sea", "triple_ids": ["t-sea"]},
                        {"object": "Portland", "object_id": "city-pdx", "triple_id": "t-pdx", "triple_ids": ["t-pdx"]},
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
             "invalidate": ["t-pdx"]},
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
                    "candidates": [
                        {"object": "Portland", "object_id": "city-pdx", "triple_id": "t-pdx", "triple_ids": ["t-pdx"]},
                        {"object": "Seattle", "object_id": "city-sea", "triple_id": "t-sea", "triple_ids": ["t-sea"]},
                    ],
                    "decision": {
                        "action": "invalidate",
                        "keep": "Seattle",
                        "invalidate": ["t-pdx"],
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
            self.assertIn("INVALIDATE_TRIPLES t-pdx", stdout.getvalue())
            self.assertIn("[dry-run] would invalidate 1, skip 0", stderr.getvalue())


class TestAdoptPatternTask(unittest.TestCase):
    def test_pattern_support_subset_rejects_laundering_and_surfaces_valid_subset(self):
        worklist = {
            "task": "pattern",
            "params": {"min_support": 2},
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
                    "evidence": {"support_ids": ["abcdef12", "abcdef13", "abcdef14"]},
                    "decision": {
                        "action": "surface",
                        "text": "Undersupported pattern must be rejected.",
                        "supported_by": ["abcdef12"],
                    },
                },
                {
                    "kind": "pattern",
                    "members": [
                        {
                            "id": "entry-2",
                            "text": "SESSION_ID: abcdef13 repeated observation",
                            "session_id": "abcdef13",
                            "wing": "wing_copilot-cli",
                            "room": "diary",
                        }
                    ],
                    "evidence": {"support_ids": ["abcdef12", "abcdef13", "abcdef14"]},
                    "decision": {
                        "action": "surface",
                        "text": "Recurring pattern worth surfacing.",
                        "supported_by": ["abcdef12", "abcdef14"],
                    },
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

        self.assertEqual(decisions[0]["supported_by"], ["abcdef12"])
        self.assertEqual(decisions[0]["allowed_support"], ["abcdef12", "abcdef13", "abcdef14"])
        self.assertEqual(decisions[1]["supported_by"], ["abcdef12", "abcdef14"])
        writer = mock.Mock()
        writer.add_drawer.return_value = {"drawer_id": "lesson-1"}
        report = dream_adopt.apply_pattern_decisions(decisions, writer, min_support=2)
        self.assertEqual(report["surfaced"], 1)
        self.assertEqual(len(report["errors"]), 1)
        writer.add_drawer.assert_called_once_with(
            "wing_copilot-cli",
            "diary",
            "Recurring pattern worth surfacing.",
            metadata={"supported_by": ["abcdef12", "abcdef14"], "kind": "lesson"},
        )

    def test_dry_run_pattern_adds_only_and_never_deletes(self):
        worklist = {
            "task": "pattern",
            "params": {"min_support": 2},
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
                        "supported_by": ["abcdef12", "abcdef13"],
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


class TestAdoptMergeArchiveAndVerify(unittest.TestCase):
    """Regression: --archive-file must reach the live merge Archiver; --verify re-harvests."""

    def _merge_decisions(self):
        return {
            "task": "merge",
            "scope": {"palace": "/palace", "wing": "wing", "room": "room"},
            "params": {"tau": 0.9},
            "items": [{
                "kind": "merge",
                "members": [
                    {"id": "drawer-1", "member_ids": ["chunk-1"], "wing": "wing", "room": "room", "text": "old A"},
                    {"id": "drawer-2", "member_ids": ["chunk-2"], "wing": "wing", "room": "room", "text": "old B"},
                ],
                "supersedes": ["chunk-1", "chunk-2"],
                "decision": {"action": "merge", "text": "merged fact"},
            }],
        }

    def test_live_merge_passes_archive_file_to_archiver(self):
        report = {"merged": 1, "skipped": 0, "deleted": ["chunk-1", "chunk-2"], "errors": []}
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(self._merge_decisions(), fh)
            archive_path = os.path.join(td, "custom-archive.jsonl")
            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_adopt.dream_palace, "MempalaceWriter", return_value=mock.MagicMock()), \
                 mock.patch.object(dream_adopt.dream_palace, "Archiver") as archiver_cls, \
                 mock.patch.object(dream_adopt, "_preflight_merge_decisions", side_effect=lambda p, d: (d, [])), \
                 mock.patch.object(dream_adopt, "apply_merge_decisions", return_value=report), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = dream_adopt.main([
                    "--palace", "/palace", "--decisions", decisions_path,
                    "--archive-file", archive_path,
                ])
            self.assertEqual(rc, 0)
            _, kwargs = archiver_cls.call_args
            self.assertEqual(kwargs.get("archive_path"), archive_path)

    def test_verify_reharvests_merge_and_reports_residual(self):
        report = {"merged": 1, "skipped": 0, "deleted": ["chunk-1", "chunk-2"], "errors": []}

        def fake_harvest(argv):
            out = argv[argv.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"task": "merge", "items": []}, fh)
            return 0

        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(self._merge_decisions(), fh)
            stderr = io.StringIO()
            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value=td), \
                 mock.patch.object(dream_adopt.dream_palace, "MempalaceWriter", return_value=mock.MagicMock()), \
                 mock.patch.object(dream_adopt.dream_palace, "Archiver", return_value=mock.MagicMock()), \
                 mock.patch.object(dream_adopt, "_preflight_merge_decisions", side_effect=lambda p, d: (d, [])), \
                 mock.patch.object(dream_adopt, "apply_merge_decisions", return_value=report), \
                 mock.patch.object(dream_adopt.dream_harvest, "main", side_effect=fake_harvest) as harvest_main, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", td, "--decisions", decisions_path, "--verify",
                ])
            self.assertEqual(rc, 0)
            self.assertIn("verify: 0 residual", stderr.getvalue())
            # re-harvest reconstructed the merge scope from the worklist
            called_argv = harvest_main.call_args[0][0]
            self.assertIn("--task", called_argv)
            self.assertIn("merge", called_argv)
            self.assertIn("--wing", called_argv)


class TestAdoptMergeTask(unittest.TestCase):
    def test_adopt_uses_mempalace_config_when_palace_is_omitted(self):
        with _test_tmpdir() as td:
            configured_palace = os.path.join(td, "configured-palace")
            config_path = os.path.join(td, "config.json")
            _dump_json(config_path, {"palace_path": configured_palace})
            decisions_path = os.path.join(td, "decisions.json")
            _dump_json(decisions_path, {"task": "merge", "items": []})

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}), \
                 mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value=td) as bind_palace, \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = dream_adopt.main(["--decisions", decisions_path, "--dry-run"])

            self.assertEqual(rc, 0)
            bind_palace.assert_called_once_with(configured_palace)

    def test_default_palace_helper_reads_mempalace_config(self):
        with _test_tmpdir() as td:
            configured_palace = os.path.join(td, "configured-palace")
            config_path = os.path.join(td, "config.json")
            _dump_json(config_path, {"palace_path": configured_palace})

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertEqual(dream_adopt._default_palace_from_config(), configured_palace)

    def test_adopt_without_palace_or_config_returns_clear_error(self):
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            _dump_json(decisions_path, {"task": "merge", "items": []})
            stderr = io.StringIO()

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": os.path.join(td, "missing.json")}), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main(["--decisions", decisions_path])

        self.assertEqual(rc, 2)
        self.assertIn("--palace omitted", stderr.getvalue())
        self.assertIn("palace_path", stderr.getvalue())

    def test_dry_run_merge_archives_without_real_writes_or_deletes(self):
        worklist = {
            "task": "merge",
            "items": [
                {
                    "kind": "merge",
                    "members": [
                        {"id": "drawer-1", "member_ids": ["chunk-1"], "wing": "wing", "room": "room", "text": "old A"},
                        {"id": "drawer-2", "member_ids": ["chunk-2"], "wing": "wing", "room": "room", "text": "old B"},
                    ],
                    "supersedes": ["chunk-1", "chunk-2"],
                    "decision": {"action": "merge", "text": "merged fact"},
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
                 mock.patch.object(dream_adopt.dream_palace, "MempalaceWriter", side_effect=AssertionError("real writer used")), \
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
            self.assertIn("ADD  wing/room: merged fact...", stdout.getvalue())
            self.assertIn("ARCHIVE+DELETE ['chunk-1', 'chunk-2']", stdout.getvalue())
            self.assertNotIn("DEL  ", stdout.getvalue())
            self.assertIn("[dry-run] would merge 1, skip 0, errors 0", stderr.getvalue())


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
                    "content_hash": "hash-1",
                    "pinned": False,
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
                "content_hash": "hash-1",
                "pinned": False,
                "topic": None,
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
                    "content_hash": "expected-hash",
                    "pinned": False,
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
                 mock.patch.object(dream_adopt.dream_palace, "load_drawer_by_id", return_value={
                     "id": "drawer-1",
                     "text": "forgettable",
                     "metadata": {"pinned": False},
                     "content_hash": "expected-hash",
                 }), \
                 mock.patch.object(dream_adopt.dream_palace, "kg_source_degree", return_value={}), \
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

    def test_dry_run_prune_skips_drifted_drawer(self):
        worklist = {
            "task": "prune",
            "items": [
                {
                    "kind": "prune",
                    "id": "drawer-1",
                    "member_ids": ["chunk-1"],
                    "wing": "wing",
                    "room": "room",
                    "text": "old text",
                    "content_hash": "old-hash",
                    "pinned": False,
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
                 mock.patch.object(dream_adopt.dream_palace, "load_drawer_by_id", return_value={
                     "id": "drawer-1",
                     "text": "new text",
                     "metadata": {"pinned": False},
                     "content_hash": "new-hash",
                 }), \
                 mock.patch.object(dream_adopt.dream_palace, "kg_source_degree", return_value={}), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                    "--archive-file", os.path.join(td, "archive.jsonl"),
                    "--dry-run",
                ])

            self.assertEqual(rc, 1)
            self.assertNotIn("PRUNE drawer-1", stdout.getvalue())
            self.assertIn("drift", stderr.getvalue())

    def test_dry_run_prune_aborts_now_pinned_or_kg_connected_drawers(self):
        worklist = {
            "task": "prune",
            "items": [
                {
                    "kind": "prune",
                    "id": "pinned",
                    "member_ids": ["pinned"],
                    "wing": "wing",
                    "room": "room",
                    "text": "still important",
                    "content_hash": "hash-pinned",
                    "pinned": False,
                    "salience": {"v": 0.12, "age_days": 400, "kg_degree": 0},
                    "decision": {"action": "prune"},
                },
                {
                    "kind": "prune",
                    "id": "connected",
                    "member_ids": ["connected"],
                    "wing": "wing",
                    "room": "room",
                    "text": "kg source",
                    "content_hash": "hash-connected",
                    "pinned": False,
                    "salience": {"v": 0.12, "age_days": 400, "kg_degree": 0},
                    "decision": {"action": "prune"},
                },
            ],
        }

        def load_live(_palace, drawer_id):
            return {
                "id": drawer_id,
                "text": drawer_id,
                "metadata": {"pinned": drawer_id == "pinned"},
                "content_hash": f"hash-{drawer_id}",
            }

        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            with open(decisions_path, "w", encoding="utf-8") as fh:
                json.dump(worklist, fh)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value="/bound"), \
                 mock.patch.object(dream_adopt.dream_palace, "load_drawer_by_id", side_effect=load_live), \
                 mock.patch.object(dream_adopt.dream_palace, "kg_source_degree", return_value={"connected": 1}), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                    "--archive-file", os.path.join(td, "archive.jsonl"),
                    "--dry-run",
                ])

            self.assertEqual(rc, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("protected", stderr.getvalue())


class TestAdoptDeriveTask(unittest.TestCase):
    def _derive_item(self):
        return {
            "kind": "derive",
            "candidate_id": "cand-1",
            "conclusion": {
                "subject_id": "A",
                "predicate": "depends_on_closure",
                "object_id": "C",
            },
            "rule": {"id": "transitive:depends_on"},
            "proof": {"premise_ids": ["t1", "t2"], "premise_drawer_ids": ["d1"]},
            "evidence": {"confidence": 1.0, "valid_from": "2026-01-01"},
            "ontology_version": "onto:test",
        }

    def _apply_resolved(self, worklist):
        decisions = dream_adopt._resolve_derive_decisions(worklist)
        return dream_adopt.apply_derive_decisions(decisions, dream_adopt._DryRunKgWriter())[0]

    class _RecordingKgWriter:
        def __init__(self):
            self.added = []

        def add_derived(self, *args, **kwargs):
            self.added.append((args, kwargs))
            return {"ok": True, "triple_id": f"t-derived-{len(self.added)}"}

    def test_entailed_given_materialize_is_rejected_without_writer_call(self):
        item = self._derive_item()
        item["action"] = "materialize"
        item["evidence"]["epistemic_status"] = "entailed_given"
        item["proof"]["entailed_given"] = ["t_x"]
        writer = self._RecordingKgWriter()

        report, markers = dream_adopt.apply_derive_decisions([item], writer)

        self.assertEqual(writer.added, [])
        self.assertEqual(report["materialized"], 0)
        self.assertEqual(markers, [])
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["stage"], "materialize")
        self.assertIn("refused to materialize entailed_given candidate cand-1", report["errors"][0]["error"])

    def test_deduced_materialize_still_calls_writer(self):
        item = self._derive_item()
        item["action"] = "materialize"
        item["evidence"]["epistemic_status"] = "deduced"
        item["proof"]["entailed_given"] = []
        writer = self._RecordingKgWriter()

        report, markers = dream_adopt.apply_derive_decisions([item], writer)

        self.assertEqual(len(writer.added), 1)
        self.assertEqual(report["materialized"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(markers, [])

    def test_nested_derive_decision_materializes(self):
        item = self._derive_item()
        item["decision"] = {"action": "materialize", "reason": "approved"}
        report = self._apply_resolved({"task": "contemplate", "items": [item]})

        self.assertEqual(report["materialized"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["errors"], [])

    def test_top_level_derive_action_still_materializes(self):
        item = self._derive_item()
        item["action"] = "materialize"
        report = self._apply_resolved({"task": "contemplate", "items": [item]})

        self.assertEqual(report["materialized"], 1)
        self.assertEqual(report["errors"], [])

    def test_derive_item_without_decision_or_action_is_noop(self):
        report = self._apply_resolved({"task": "contemplate", "items": [self._derive_item()]})

        self.assertEqual(report["materialized"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["ignored"], 0)
        self.assertEqual(report["errors"], [])

    def test_live_derive_adopt_prints_final_summary_line_to_stderr(self):
        report = {
            "materialized": 1,
            "skipped": 0,
            "ignored": 0,
            "rejected_rules": [],
            "materialized_facts": [],
            "errors": [],
        }
        with _test_tmpdir() as td:
            decisions_path = os.path.join(td, "decisions.json")
            _dump_json(
                decisions_path,
                {"task": "contemplate", "ontology_version": "onto:test", "items": [self._derive_item()]},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            writer = mock.MagicMock()

            with mock.patch.object(dream_adopt.dream_palace, "bind_palace", return_value=td), \
                 mock.patch.object(dream_adopt.dream_palace, "KgDeriveWriter", return_value=writer), \
                 mock.patch.object(dream_adopt, "apply_derive_decisions", return_value=(report, [])), \
                 mock.patch.object(dream_adopt.dream_palace, "append_skip_markers"), \
                 contextlib.redirect_stdout(stdout), \
                 contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--task", "derive",
                    "--palace", "/palace",
                    "--decisions", decisions_path,
                ])

        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(
            stderr.getvalue().splitlines()[-1],
            "adopt: task=derive materialized=1 skipped=0 errors=0",
        )


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class DeriveCliTests(unittest.TestCase):
    def _palace(self, td):
        palace = os.path.join(td, "palace"); os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("B", "depends_on", "C", valid_from="2026-01-01")
        kg.close()
        with open(os.path.join(palace, "ontology.json"), "w") as f:
            json.dump({"version": 1, "rules": [{"id": "transitive:depends_on",
                "family": "transitive", "predicate": "depends_on", "enabled": True,
                "max_depth": 3}]}, f)
        return palace

    def test_harvest_derive_emits_one_closure_candidate(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out)
            self.assertEqual(wl["task"], "contemplate")
            self.assertEqual(len(wl["items"]), 1)
            self.assertEqual(wl["items"][0]["conclusion"]["predicate"], "depends_on_closure")

    def test_adopt_materialize_then_verify_reaches_fixpoint(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out)
            wl["items"][0]["action"] = "materialize"
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            rc = dream_adopt.main(["--task", "derive", "--palace", palace,
                                   "--decisions", dec, "--verify", "--strict"])
            self.assertEqual(rc, 0)
            # re-harvest: candidate now active => 0 residual
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(len(_load_json(out)["items"]), 0)

    def test_adopt_skip_then_reharvest_is_empty_via_skip_marker(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out); wl["items"][0]["action"] = "skip"
            wl["items"][0]["reason"] = "noise"
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            dream_adopt.main(["--task", "derive", "--palace", palace, "--decisions", dec])
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(len(_load_json(out)["items"]), 0)  # skip-marker suppresses

    def test_adopt_reject_rule_suppresses_via_skip_markers(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out); wl["items"][0]["action"] = "reject_rule"
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            dream_adopt.main(["--task", "derive", "--palace", palace, "--decisions", dec])
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(len(_load_json(out)["items"]), 0)  # operational fixpoint

    def test_dry_run_materialize_previews_without_writing(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out); wl["items"][0]["action"] = "materialize"
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            rc = dream_adopt.main(["--task", "derive", "--palace", palace,
                                   "--decisions", dec, "--dry-run"])
            self.assertEqual(rc, 0)
            # palace must be unmutated: re-harvest still yields 1 candidate
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(len(_load_json(out)["items"]), 1)

    def test_live_adopt_materialize_error_returns_nonzero(self):
        # Corrupt object_id so KgDeriveWriter raises; errors should surface in exit code
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out)
            wl["items"][0]["action"] = "materialize"
            wl["items"][0]["conclusion"]["object_id"] = 999999  # bogus entity id
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            rc = dream_adopt.main(["--task", "derive", "--palace", palace, "--decisions", dec])
            self.assertEqual(rc, 1)

    def test_empty_contemplate_worklist_dispatches_without_task_flag(self):
        # Zero-item contemplate worklist should adopt as a clean no-op (rc 0) without --task
        with _test_tmpdir() as td:
            palace = os.path.join(td, "palace"); os.makedirs(palace)
            _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3")).close()
            out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            wl = _load_json(out)
            self.assertEqual(len(wl["items"]), 0)
            dec = os.path.join(td, "dec.json"); _dump_json(dec, wl)
            rc = dream_adopt.main(["--palace", palace, "--decisions", dec])
            self.assertEqual(rc, 0)


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class DeriveHarvestCliTests(unittest.TestCase):
    def _palace(self, td):
        palace = os.path.join(td, "palace"); os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("B", "depends_on", "C", valid_from="2026-01-01")
        kg.close()
        with open(os.path.join(palace, "ontology.json"), "w") as f:
            json.dump({"version": 1, "rules": [{"id": "transitive:depends_on",
                "family": "transitive", "predicate": "depends_on", "enabled": True,
                "max_depth": 3}]}, f)
        return palace

    def test_harvest_derive_emits_one_closure_candidate(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            rc = dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            wl = _load_json(out)
            self.assertEqual(wl["task"], "contemplate")
            self.assertEqual(len(wl["items"]), 1)
            self.assertEqual(wl["items"][0]["conclusion"]["predicate"], "depends_on_closure")

    def test_harvest_derive_empty_config_yields_zero(self):
        with _test_tmpdir() as td:
            palace = os.path.join(td, "palace"); os.makedirs(palace)
            _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3")).close()
            out = os.path.join(td, "wl.json")
            rc = dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            self.assertEqual(len(_load_json(out)["items"]), 0)


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace KnowledgeGraph")
class GapsCliTests(unittest.TestCase):
    def _palace(self, td):
        palace = os.path.join(td, "palace"); os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        # broken chain: A->B and C->D; the sole missing edge B->C would unblock closure facts
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("C", "depends_on", "D", valid_from="2026-01-01")
        kg.close()
        with open(os.path.join(palace, "ontology.json"), "w") as f:
            json.dump({"version": 1, "rules": [{"id": "transitive:depends_on",
                "family": "transitive", "predicate": "depends_on", "enabled": True,
                "max_depth": 3}]}, f)
        return palace

    def test_harvest_gaps_emits_bridging_gap(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = dream_harvest.main(["--task", "gaps", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            wl = _load_json(out)
            self.assertEqual(wl["task"], "gaps")
            self.assertEqual(wl["scope"]["task"], "gaps")
            self.assertIsNone(wl["scope"]["target_subject"])
            edges = {(g["hypothesis"]["subject"], g["hypothesis"]["object"]) for g in wl["items"]}
            self.assertIn(("B", "C"), edges)
            bc = next(g for g in wl["items"] if (g["hypothesis"]["subject"], g["hypothesis"]["object"]) == ("B", "C"))
            self.assertEqual(bc["kind"], "gap")
            self.assertGreaterEqual(bc["evidence"]["duc"], 1)
            self.assertIn("gaps:", stderr.getvalue())

    def test_harvest_gaps_empty_ontology_yields_zero(self):
        with _test_tmpdir() as td:
            palace = os.path.join(td, "palace"); os.makedirs(palace)
            kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
            kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
            kg.close()
            out = os.path.join(td, "wl.json")
            rc = dream_harvest.main(["--task", "gaps", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            self.assertEqual(_load_json(out)["items"], [])

    def test_harvest_gaps_target_subject_filters(self):
        with _test_tmpdir() as td:
            palace = self._palace(td); out = os.path.join(td, "wl.json")
            dream_harvest.main(["--task", "gaps", "--palace", palace,
                                "--target-subject", "A", "--out", out])
            wl = _load_json(out)
            self.assertEqual(wl["scope"]["target_subject"], "A")
            for g in wl["items"]:
                for u in g["evidence"]["unblocks"]:
                    self.assertEqual(u["subject"], "A")


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace KnowledgeGraph")
class B11RewireCliTests(unittest.TestCase):
    def _derive_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("B", "depends_on", "C", valid_from="2026-01-01")
        kg.close()
        with open(os.path.join(palace, "ontology.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rules": [{"id": "transitive:depends_on",
                "family": "transitive", "predicate": "depends_on", "enabled": True,
                "max_depth": 3}]}, fh)
        return palace

    def _gaps_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("C", "depends_on", "D", valid_from="2026-01-01")
        kg.close()
        with open(os.path.join(palace, "ontology.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rules": [{"id": "transitive:depends_on",
                "family": "transitive", "predicate": "depends_on", "enabled": True,
                "max_depth": 3}]}, fh)
        return palace

    def _audit_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("Alice", "lives_in", "Portland", valid_from="2024-01-01")
        kg.add_triple("Alice", "lives_in", "Seattle", valid_from="2025-01-01")
        kg.add_triple("Author", "authored", "Post", valid_from="2026-01-01")
        kg.add_triple("Post", "authored_by", "Author", valid_from="2026-01-01")
        kg.add_triple("ModuleA", "depends_on", "ModuleB", valid_from="2026-01-01")
        kg.add_triple("ModuleB", "depends_on", "ModuleC", valid_from="2026-01-01")
        kg.add_triple("FriendOne", "collaborates_with", "FriendTwo", valid_from="2026-01-01")
        kg.add_triple("FriendTwo", "collaborates_with", "FriendOne", valid_from="2026-01-01")
        kg.close()
        return palace

    def test_durable_derive_harvest_preserves_single_closure_candidate(self):
        with _test_tmpdir() as td:
            palace = self._derive_palace(td)
            out = os.path.join(td, "wl.json")

            with mock.patch.object(
                dream_harvest.dream_palace,
                "load_premises",
                wraps=dream_harvest.dream_palace.load_premises,
            ) as load_premises:
                rc = dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])

            self.assertEqual(rc, 0)
            load_premises.assert_called_once_with(palace, purpose="durable")
            wl = _load_json(out)
            self.assertEqual(wl["task"], "contemplate")
            self.assertEqual(len(wl["items"]), 1)
            conclusion = wl["items"][0]["conclusion"]
            self.assertEqual((conclusion["subject"], conclusion["predicate"], conclusion["object"]),
                             ("A", "depends_on_closure", "C"))

    def test_durable_gaps_harvest_preserves_bridging_gap(self):
        with _test_tmpdir() as td:
            palace = self._gaps_palace(td)
            out = os.path.join(td, "wl.json")

            with mock.patch.object(
                dream_harvest.dream_palace,
                "load_premises",
                wraps=dream_harvest.dream_palace.load_premises,
            ) as load_premises:
                rc = dream_harvest.main(["--task", "gaps", "--palace", palace, "--out", out])

            self.assertEqual(rc, 0)
            load_premises.assert_called_once_with(palace, purpose="durable")
            wl = _load_json(out)
            self.assertEqual(wl["task"], "gaps")
            edges = {(item["hypothesis"]["subject"], item["hypothesis"]["object"]) for item in wl["items"]}
            self.assertIn(("B", "C"), edges)

    def test_durable_derive_materialize_verify_reaches_fixpoint(self):
        with _test_tmpdir() as td:
            palace = self._derive_palace(td)
            out = os.path.join(td, "wl.json")
            rc = dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            wl = _load_json(out)
            wl["items"][0]["action"] = "materialize"
            decisions = os.path.join(td, "decisions.json")
            _dump_json(decisions, wl)
            stderr = io.StringIO()

            with mock.patch.object(
                dream_adopt.dream_palace,
                "load_premises",
                wraps=dream_adopt.dream_palace.load_premises,
            ) as load_premises, contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--task", "derive",
                    "--palace", palace,
                    "--decisions", decisions,
                    "--verify",
                    "--strict",
                ])

            self.assertEqual(rc, 0)
            # adopt now calls the durable loader for BOTH the C4 premise re-validation
            # (during materialize) and the verify re-harvest.
            load_premises.assert_any_call(palace, purpose="durable")
            self.assertIn("verify: 0 residual candidate(s)", stderr.getvalue())

    def test_deduced_derive_adopt_verify_reaches_fixpoint_after_firewall(self):
        with _test_tmpdir() as td:
            palace = self._derive_palace(td)
            out = os.path.join(td, "wl.json")
            rc = dream_harvest.main(["--task", "derive", "--palace", palace, "--out", out])
            self.assertEqual(rc, 0)
            wl = _load_json(out)
            self.assertEqual(wl["items"][0]["evidence"]["epistemic_status"], "deduced")
            self.assertEqual(wl["items"][0]["proof"]["entailed_given"], [])
            wl["items"][0]["action"] = "materialize"
            decisions = os.path.join(td, "decisions.json")
            _dump_json(decisions, wl)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                rc = dream_adopt.main([
                    "--task", "derive",
                    "--palace", palace,
                    "--decisions", decisions,
                    "--verify",
                    "--strict",
                ])

            self.assertEqual(rc, 0)
            self.assertIn("verify: 0 residual candidate(s)", stderr.getvalue())

    def test_audit_contradiction_harvest_preserves_worklist_shape(self):
        with _test_tmpdir() as td:
            palace = self._audit_palace(td)
            out = os.path.join(td, "contradictions.json")

            with mock.patch.object(
                dream_harvest.dream_palace,
                "load_premises",
                wraps=dream_harvest.dream_palace.load_premises,
            ) as load_premises:
                rc = dream_harvest.main(["--task", "contradiction", "--palace", palace, "--out", out])

            self.assertEqual(rc, 0)
            load_premises.assert_called_once_with(palace, purpose="audit")
            wl = _load_json(out)
            self.assertEqual(wl["task"], "contradiction")
            self.assertEqual(wl["items"][0]["kind"], "contradiction")
            self.assertEqual(wl["items"][0]["evidence"]["size"], 2)
            self.assertEqual(len(wl["items"][0]["candidates"]), 2)

    def test_audit_suggest_rules_preserves_rule_doc_shape(self):
        with _test_tmpdir() as td:
            palace = self._audit_palace(td)
            ontology = os.path.join(td, "ontology.json")

            with mock.patch.object(
                dream_harvest.dream_palace,
                "load_premises",
                wraps=dream_harvest.dream_palace.load_premises,
            ) as load_premises:
                rc = dream_harvest.main([
                    "--task", "suggest-rules",
                    "--palace", palace,
                    "--ontology-out", ontology,
                ])

            self.assertEqual(rc, 0)
            load_premises.assert_called_once_with(palace, purpose="audit")
            rules = _load_json(ontology)["rules"]
            self.assertTrue(rules)
            self.assertTrue(all(rule["enabled"] is False for rule in rules))
            self.assertIn("transitive:depends_on", {rule["id"] for rule in rules})

    def test_audit_induce_rules_preserves_rule_doc_shape(self):
        with _test_tmpdir() as td:
            palace = self._audit_palace(td)
            ontology = os.path.join(td, "ontology.json")

            with mock.patch.object(
                dream_harvest.dream_palace,
                "load_premises",
                wraps=dream_harvest.dream_palace.load_premises,
            ) as load_premises:
                rc = dream_harvest.main([
                    "--task", "induce-rules",
                    "--palace", palace,
                    "--min-support", "1",
                    "--ontology-out", ontology,
                ])

            self.assertEqual(rc, 0)
            load_premises.assert_called_once_with(palace, purpose="audit")
            rules = _load_json(ontology)["rules"]
            self.assertTrue(rules)
            self.assertTrue(all(rule["enabled"] is False for rule in rules))
            self.assertIn("transitive:depends_on", {rule["id"] for rule in rules})


class TestHarvestReflectTask(unittest.TestCase):
    def test_reflect_task_writes_reflect_worklist(self):
        import dream_reflect
        seeds = [{"anchor_id": "d1", "member_ids": ["d1", "d2"], "members": [], "snippets": [],
                  "coverage": 2, "score": 0.9},
                 {"anchor_id": "d3", "member_ids": ["d3"], "members": [], "snippets": [],
                  "coverage": 1, "score": 0.99}]  # dropped: coverage<2
        orig = dream_reflect.gather_reflect_seeds
        dream_reflect.gather_reflect_seeds = lambda p, **kw: seeds
        try:
            with _test_tmpdir() as td:
                out = os.path.join(td, "wl.json")
                rc = dream_harvest.main(["--palace", td, "--task", "reflect",
                                         "--max-candidates", "10", "--out", out])
                self.assertEqual(rc, 0)
                wl = json.load(open(out))
                self.assertEqual(wl["task"], "reflect")
                self.assertEqual([i["seed_id"] for i in wl["items"]], ["d1"])  # coverage gate
        finally:
            dream_reflect.gather_reflect_seeds = orig


if __name__ == "__main__":
    unittest.main()
