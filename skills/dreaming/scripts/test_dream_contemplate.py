"""Tests for the one-shot contemplate driver."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import dream_contemplate as dc


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-contemplate-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


def _derive_worklist(n=1):
    items = []
    for i in range(n):
        items.append({
            "kind": "derive",
            "candidate_id": f"derive-{i}",
            "conclusion": {"subject": f"Subject{i}", "predicate": "authored_by", "object": f"Object{i}"},
            "proof": {"depth": i + 1},
            "rule": {"id": "inverse:authored:authored_by", "family": "inverse", "predicate": "authored"},
        })
    return {"task": "contemplate", "items": items}


class TestReportBuilding(unittest.TestCase):
    def test_report_counts_formats_examples_and_marks_example_truncation(self):
        report = dc.build_report(
            palace="/palace",
            rules_path="/palace/ontology.json",
            enabled_rule_count=1,
            worklist=_derive_worklist(6),
            ontology_rules=[{"id": "inverse:authored:authored_by", "enabled": True}],
        )

        self.assertEqual(report["derive_candidate_count"], 6)
        self.assertEqual(len(report["examples"]), 5)
        self.assertEqual(report["examples"][0], "Subject0 -authored_by-> Object0 (depth 1)")
        self.assertTrue(report["truncated"])

        text = dc.summarize_report(report)
        self.assertIn("derive candidates: 6", text)
        self.assertIn("Subject0 -authored_by-> Object0 (depth 1)", text)

    def test_empty_ontology_message_explains_names_are_not_semantics(self):
        report = dc.build_report(
            palace="/palace",
            rules_path="/palace/ontology.json",
            enabled_rule_count=0,
            worklist={"task": "contemplate", "items": []},
            ontology_rules=[],
        )

        self.assertEqual(report["derive_candidate_count"], 0)
        self.assertTrue(report["empty_ontology"])
        self.assertTrue(any("predicate names are not predicate semantics" in msg for msg in report["messages"]))
        self.assertIn("run with --bootstrap", dc.summarize_report(report))

    def test_disabled_only_ontology_message_does_not_call_file_empty(self):
        report = dc.build_report(
            palace="/palace",
            rules_path="/palace/ontology.json",
            enabled_rule_count=0,
            worklist={"task": "contemplate", "items": []},
            ontology_rules=[{"id": "inverse:authored:authored_by", "enabled": False}],
        )

        self.assertFalse(report["empty_ontology"])
        self.assertTrue(any("0 enabled rules" in msg for msg in report["messages"]))
        self.assertFalse(any("ontology is empty" in msg for msg in report["messages"]))

    def test_bootstrap_proposals_are_listed_with_id_family_and_rationale(self):
        bootstrap = {
            "target": "/palace/ontology.json",
            "stats": {"added": 1, "skipped_existing": 0},
            "proposed_disabled_rules": [
                {
                    "id": "inverse:authored:authored_by",
                    "family": "inverse",
                    "enabled": False,
                    "rationale": "heuristic: predicate names suggest inverse relationship — REVIEW before enabling",
                }
            ],
        }

        report = dc.build_report(
            palace="/palace",
            rules_path="/palace/ontology.json",
            enabled_rule_count=0,
            worklist={"task": "contemplate", "items": []},
            ontology_rules=[],
            bootstrap=bootstrap,
        )

        self.assertEqual(report["bootstrap"]["proposed_disabled_rules"][0]["id"], "inverse:authored:authored_by")
        text = dc.summarize_report(report)
        self.assertIn("bootstrap target: /palace/ontology.json", text)
        self.assertIn("inverse:authored:authored_by [inverse]", text)
        self.assertIn("heuristic: predicate names suggest inverse relationship", text)


class TestCliSmoke(unittest.TestCase):
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
                ('author-1', 'Author One'),
                ('post-1', 'Post One'),
                ('author-2', 'Author Two'),
                ('post-2', 'Post Two'),
                ('module-a', 'Module A'),
                ('module-b', 'Module B');
            INSERT INTO triples (
                id, subject, predicate, object, valid_from, valid_to,
                confidence, source_closet, source_file, source_drawer_id,
                adapter_name, extracted_at
            ) VALUES
                ('t-authored-1', 'author-1', 'authored', 'post-1', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t-authored-by-1', 'post-1', 'authored_by', 'author-1', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t-authored-2', 'author-2', 'authored', 'post-2', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01'),
                ('t-depends', 'module-a', 'depends_on', 'module-b', '2026-01-01', NULL, 1.0, NULL, NULL, NULL, NULL, '2026-01-01');
            """
        )
        con.commit()
        con.close()
        return palace

    def _run_json(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = dc.main(argv)
        self.assertEqual(rc, 0, stderr.getvalue())
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def test_empty_enabled_bootstrap_and_enabled_rule_paths(self):
        with _test_tmpdir() as td:
            palace = self._palace_with_kg(td)

            report, _stderr = self._run_json(["--palace", palace, "--format", "json"])
            self.assertEqual(report["enabled_rule_count"], 0)
            self.assertEqual(report["derive_candidate_count"], 0)
            self.assertTrue(report["empty_ontology"])

            ontology_path = os.path.join(palace, "ontology.json")
            existing_enabled = {
                "id": "inverse:authored:authored_by",
                "family": "inverse",
                "predicate": "authored",
                "inverse_predicate": "authored_by",
                "enabled": True,
                "rationale": "human approved",
            }
            with open(ontology_path, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "rules": [existing_enabled]}, fh)

            report, _stderr = self._run_json(["--palace", palace, "--format", "json"])
            self.assertGreaterEqual(report["derive_candidate_count"], 1)

            report, _stderr = self._run_json(["--palace", palace, "--bootstrap", "--format", "json"])
            with open(ontology_path, encoding="utf-8") as fh:
                ontology = json.load(fh)
            self.assertEqual(ontology["rules"][0], existing_enabled)
            disabled = [rule for rule in ontology["rules"][1:] if rule.get("enabled") is False]
            self.assertTrue(disabled)
            self.assertEqual(len(disabled), len(ontology["rules"]) - 1)
            self.assertTrue(all(rule.get("enabled") is False for rule in report["bootstrap"]["proposed_disabled_rules"]))


if __name__ == "__main__":
    unittest.main()
