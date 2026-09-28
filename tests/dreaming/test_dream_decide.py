"""Tests for the dream_decide helper CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

import dream_decide


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-decide-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


def _dump_json(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _derive_worklist():
    return {
        "version": 1,
        "task": "contemplate",
        "items": [
            {"kind": "derive", "candidate_id": "derive:a", "conclusion": {"subject_id": 1}},
            {"kind": "derive", "candidate_id": "derive:b", "conclusion": {"subject_id": 2}},
        ],
    }


class DreamDecideTests(unittest.TestCase):
    def _run(self, worklist, args):
        with _test_tmpdir() as td:
            worklist_path = os.path.join(td, "worklist.json")
            out_path = os.path.join(td, "decisions.json")
            _dump_json(worklist_path, worklist)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = dream_decide.main(["--worklist", worklist_path, "--out", out_path, *args])
            output = _load_json(out_path) if os.path.exists(out_path) else None
            return rc, output, stderr.getvalue()

    def test_all_materialize_on_derive_sets_nested_decisions(self):
        rc, output, stderr = self._run(_derive_worklist(), ["--all", "materialize"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            [item["decision"]["action"] for item in output["items"]],
            ["materialize", "materialize"],
        )
        self.assertIn("decide: task=derive action=materialize applied=2/2", stderr)

    def test_only_action_materialize_applies_only_to_matching_candidate_id(self):
        rc, output, _stderr = self._run(
            _derive_worklist(),
            ["--only", "derive:b", "--action", "materialize"],
        )

        self.assertEqual(rc, 0)
        self.assertNotIn("decision", output["items"][0])
        self.assertEqual(output["items"][1]["decision"], {"action": "materialize"})

    def test_except_excludes_matching_candidate_id(self):
        rc, output, _stderr = self._run(
            _derive_worklist(),
            ["--all", "materialize", "--except", "derive:a"],
        )

        self.assertEqual(rc, 0)
        self.assertNotIn("decision", output["items"][0])
        self.assertEqual(output["items"][1]["decision"], {"action": "materialize"})

    def test_prune_keep_and_prune_are_supported(self):
        for action in ("keep", "prune"):
            with self.subTest(action=action):
                worklist = {
                    "version": 1,
                    "task": "prune",
                    "items": [
                        {"kind": "prune", "id": "drawer-a", "text": "a"},
                        {"kind": "prune", "id": "drawer-b", "text": "b"},
                    ],
                }

                rc, output, stderr = self._run(worklist, ["--all", action])

                self.assertEqual(rc, 0)
                self.assertEqual(
                    [item["decision"]["action"] for item in output["items"]],
                    [action, action],
                )
                self.assertIn(f"decide: task=prune action={action} applied=2/2", stderr)

    def test_illegal_actions_exit_nonzero_with_helpful_message(self):
        cases = [
            (
                {"version": 1, "task": "merge", "items": [{"kind": "merge", "id": "m1"}]},
                "materialize",
                "merge requires manual text",
            ),
            (
                {
                    "version": 1,
                    "task": "contradiction",
                    "items": [{"kind": "contradiction", "cluster_id": 0}],
                },
                "invalidate",
                "contradiction blanket decisions only support: skip",
            ),
        ]
        for worklist, action, expected in cases:
            with self.subTest(action=action):
                rc, output, stderr = self._run(worklist, ["--all", action])

                self.assertNotEqual(rc, 0)
                self.assertIsNone(output)
                self.assertIn(expected, stderr)


if __name__ == "__main__":
    unittest.main()
