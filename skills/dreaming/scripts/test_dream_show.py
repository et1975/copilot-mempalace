"""CLI rendering tests for dream_show worklist digests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

import dream_show


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-show-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


def _dump_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


def _render(worklist):
    with _test_tmpdir() as td:
        path = os.path.join(td, "worklist.json")
        _dump_json(path, worklist)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = dream_show.main(["--worklist", path])
        return rc, stdout.getvalue()


class TestDreamShowDerive(unittest.TestCase):
    def test_renders_nested_conclusion_and_proof_depth(self):
        worklist = {
            "version": 1,
            "task": "contemplate",
            "ontology_version": "onto:abcdef1234567890",
            "items": [
                {
                    "kind": "derive",
                    "candidate_id": "derive:0123456789abcdef",
                    "conclusion": {
                        "subject": "Common@1.2.560",
                        "predicate": "depends_on_closure",
                        "object": "Posh-SSH@3.2.7",
                    },
                    "rule": {"id": "transitive:depends_on"},
                    "proof": {"depth": 2, "premise_ids": ["t1", "t2"]},
                    "evidence": {"confidence": 1.0},
                    "decision": None,
                    "ontology_version": "onto:abcdef1234567890",
                }
            ],
        }

        rc, out = _render(worklist)

        self.assertEqual(rc, 0)
        self.assertIn("task=derive", out)
        self.assertIn("depth=2", out)
        self.assertIn("Common@1.2.560", out)
        self.assertIn("depends_on_closure", out)
        self.assertIn("Posh-SSH@3.2.7", out)
        self.assertIn("rule transitive:depends_on", out)


class TestDreamShowPrune(unittest.TestCase):
    def test_renders_salience_and_room(self):
        worklist = {
            "version": 1,
            "task": "prune",
            "items": [
                {
                    "kind": "prune",
                    "id": "drawer-abcdef123456",
                    "text": "temporary note for now with a lot of extra words",
                    "wing": "wing",
                    "room": "diary",
                    "salience": {
                        "v": 0.12,
                        "age_days": 45,
                        "kg_degree": 0,
                        "redundancy": 0.94,
                        "negatives": True,
                    },
                    "decision": None,
                }
            ],
        }

        rc, out = _render(worklist)

        self.assertEqual(rc, 0)
        self.assertIn("task=prune", out)
        self.assertIn("diary/drawer-", out)
        self.assertIn("v=0.12", out)
        self.assertIn("age=45d", out)
        self.assertIn("kg_degree=0", out)
        self.assertIn("neg=True", out)


class TestDreamShowContradiction(unittest.TestCase):
    def test_renders_all_candidate_objects(self):
        worklist = {
            "version": 1,
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
                    "evidence": {"newest_object": "Seattle"},
                    "decision": None,
                }
            ],
        }

        rc, out = _render(worklist)

        self.assertEqual(rc, 0)
        self.assertIn("Alice -lives_in->", out)
        self.assertIn("Seattle", out)
        self.assertIn("Portland", out)
        self.assertIn("newest=Seattle", out)

