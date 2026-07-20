import json
import os
import tempfile
import unittest
from unittest import mock

import dream_survey as ds


def _merge_worklist(n=1):
    items = []
    for i in range(n):
        items.append({
            "kind": "merge",
            "cluster_id": f"c{i}",
            "members": [
                {"id": f"d{i}a", "wing": "avs", "room": "host-tracking", "text": "alpha"},
                {"id": f"d{i}b", "wing": "avs", "room": "host-tracking", "text": "beta"},
            ],
            "evidence": {"pair_sims": [{"a": f"d{i}a", "b": f"d{i}b", "sim": 0.91}], "size": 2},
        })
    return {"task": "merge", "items": items}


def _contradiction_worklist(objs=("x", "y")):
    return {"task": "contradiction", "items": [{
        "kind": "contradiction",
        "subject": "S",
        "predicate": "lives_in",
        "candidates": [{"object": o} for o in objs],
        "evidence": {"size": len(objs), "newest_object": objs[-1]},
    }]}


def _pattern_worklist():
    return {"task": "pattern", "items": [{
        "kind": "pattern",
        "members": [{"text": "observation text here"}],
        "evidence": {"support": 3, "support_ids": ["s1", "s2", "s3"]},
    }]}


def _prune_worklist(wing="icm_automation", n=1):
    return {"task": "prune", "items": [{
        "kind": "prune",
        "id": f"drawer_{wing}_general_{i}",
        "wing": wing,
        "room": "general",
        "topic": "general",
        "pinned": False,
        "salience": {"v": 0.09, "age_days": 38, "kg_degree": 0, "redundancy": 0.76, "negatives": False},
    } for i in range(n)]}


class TestCount(unittest.TestCase):
    def test_count_items(self):
        self.assertEqual(ds.count(_merge_worklist(3)), 3)
        self.assertEqual(ds.count({"items": []}), 0)
        self.assertEqual(ds.count({}), 0)


class TestExamples(unittest.TestCase):
    def test_merge_example(self):
        ex = ds.examples("merge", _merge_worklist(1), n=3)
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["wing"], "avs")
        self.assertEqual(ex[0]["room"], "host-tracking")
        self.assertEqual(ex[0]["size"], 2)
        self.assertAlmostEqual(ex[0]["max_sim"], 0.91)

    def test_contradiction_example(self):
        ex = ds.examples("contradiction", _contradiction_worklist(("a", "b", "c")), n=3)
        self.assertEqual(ex[0]["subject"], "S")
        self.assertEqual(ex[0]["predicate"], "lives_in")
        self.assertEqual(ex[0]["objects"], ["a", "b", "c"])
        self.assertEqual(ex[0]["newest"], "c")

    def test_pattern_example(self):
        ex = ds.examples("pattern", _pattern_worklist(), n=3)
        self.assertEqual(ex[0]["support"], 3)
        self.assertIn("observation", ex[0]["sample"])

    def test_reflect_example(self):
        wl = {"task": "reflect", "items": [{
            "kind": "reflect", "reflect_kind": "generalize", "coverage": 3, "score": 0.82,
            "members": [{"text": "alpha depends on beta for tls"}],
        }]}
        ex = ds.examples("reflect", wl, n=3)
        self.assertEqual(ex[0]["reflect_kind"], "generalize")
        self.assertEqual(ex[0]["coverage"], 3)
        self.assertAlmostEqual(ex[0]["score"], 0.82)
        self.assertIn("alpha depends", ex[0]["sample"])

    def test_prune_example(self):
        ex = ds.examples("prune", _prune_worklist(n=1), n=3)
        self.assertEqual(ex[0]["wing"], "icm_automation")
        self.assertAlmostEqual(ex[0]["v"], 0.09)
        self.assertEqual(ex[0]["age_days"], 38)

    def test_examples_capped_at_n(self):
        ex = ds.examples("merge", _merge_worklist(10), n=3)
        self.assertEqual(len(ex), 3)


class TestPalaceTaskSummary(unittest.TestCase):
    def test_summary_shape(self):
        s = ds.palace_task_summary("contradiction", _contradiction_worklist(), n=3)
        self.assertEqual(s["total"], 1)
        self.assertEqual(len(s["examples"]), 1)

    def test_empty(self):
        s = ds.palace_task_summary("contradiction", {"items": []}, n=3)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["examples"], [])


class TestWingTaskSummary(unittest.TestCase):
    def test_aggregates_across_wings(self):
        by_wing = {
            "avs": _merge_worklist(1),
            "empty_wing": _merge_worklist(0),
            "other": _merge_worklist(2),
        }
        s = ds.wing_task_summary("merge", by_wing, n=5)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_wing"], {"avs": 1, "other": 2})  # zero-count wings omitted
        self.assertEqual(len(s["examples"]), 3)
        for ex in s["examples"]:
            self.assertIn("wing", ex)  # each example annotated with its source wing

    def test_examples_capped(self):
        by_wing = {"w1": _prune_worklist("w1", 20), "w2": _prune_worklist("w2", 20)}
        s = ds.wing_task_summary("prune", by_wing, n=4)
        self.assertEqual(s["total"], 40)
        self.assertEqual(len(s["examples"]), 4)


class TestRulesSummary(unittest.TestCase):
    def test_rules(self):
        rules = [
            {"id": "inverse:a:b", "family": "inverse", "predicate": "a", "enabled": False},
            {"id": "symmetric:c", "family": "symmetric", "predicate": "c", "enabled": False},
        ]
        s = ds.rules_summary(rules)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["rules"][0]["id"], "inverse:a:b")
        self.assertFalse(s["rules"][0]["enabled"])

    def test_empty(self):
        s = ds.rules_summary([])
        self.assertEqual(s["total"], 0)


class TestBuildReport(unittest.TestCase):
    def test_report_structure_and_summary(self):
        collected = {
            "contradiction": _contradiction_worklist(("a", "b")),
            "induce-rules": [{"id": "symmetric:c", "family": "symmetric", "enabled": False}],
            "merge": {"avs": _merge_worklist(1)},
            "pattern": {"copilot-mempalace": _pattern_worklist()},
            "prune": {"icm_automation": _prune_worklist(n=5)},
        }
        report = ds.build_report("/tmp/p", ["avs", "copilot-mempalace", "icm_automation"], collected, n=3)
        self.assertEqual(report["palace"], "/tmp/p")
        self.assertEqual(report["tasks"]["contradiction"]["total"], 1)
        self.assertEqual(report["tasks"]["induce-rules"]["total"], 1)
        self.assertEqual(report["tasks"]["merge"]["total"], 1)
        self.assertEqual(report["tasks"]["prune"]["total"], 5)

        text = ds.summarize_report(report)
        self.assertIn("contradiction", text)
        self.assertIn("merge", text)
        self.assertIn("prune", text)
        # totals surfaced in the human summary
        self.assertRegex(text, r"prune[^\n]*5")


class TestDefaultPalace(unittest.TestCase):
    def test_default_palace_reads_mempalace_config_env(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"palace_path": "~/palace-from-config"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertEqual(ds._default_palace(), os.path.expanduser("~/palace-from-config"))

    def test_default_palace_returns_none_without_palace_path(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as td:
            config_path = os.path.join(td, "config.json")
            with open(config_path, "w", encoding="utf-8") as fh:
                json.dump({"collection_name": "mempalace_drawers"}, fh)

            with mock.patch.dict(os.environ, {"MEMPALACE_CONFIG": config_path}):
                self.assertIsNone(ds._default_palace())


if __name__ == "__main__":
    unittest.main()
