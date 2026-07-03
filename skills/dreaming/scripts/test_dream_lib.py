"""Unit tests for the dreaming pure core. Stdlib unittest only — no external deps.

Run: cd skills/dreaming/scripts && python3 -m unittest -v
"""
import unittest

import dream_lib as dl


class TestCosine(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertAlmostEqual(dl.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0)

    def test_orthogonal_is_zero(self):
        self.assertAlmostEqual(dl.cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_zero_vector_is_zero(self):
        self.assertEqual(dl.cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            dl.cosine_similarity([1.0], [1.0, 2.0])


class TestGroupLogical(unittest.TestCase):
    def test_chunks_merge_by_parent_ordered(self):
        rows = [
            {"id": "c1", "text": "beta", "embedding": [0.0, 2.0],
             "metadata": {"parent_drawer_id": "p", "chunk_index": 1, "wing": "w", "room": "r"}},
            {"id": "c0", "text": "alpha", "embedding": [2.0, 0.0],
             "metadata": {"parent_drawer_id": "p", "chunk_index": 0, "wing": "w", "room": "r"}},
        ]
        logical = dl.group_logical_drawers(rows)
        self.assertEqual(len(logical), 1)
        d = logical[0]
        self.assertEqual(d["id"], "p")
        self.assertEqual(d["member_ids"], ["c0", "c1"])
        self.assertEqual(d["text"], "alpha\nbeta")   # ordered by chunk_index
        self.assertEqual(d["embedding"], [1.0, 1.0])  # mean
        self.assertEqual(d["wing"], "w")

    def test_singleton_passes_through(self):
        rows = [{"id": "x", "text": "solo", "embedding": [1.0],
                 "metadata": {"wing": "w", "room": "r"}}]
        logical = dl.group_logical_drawers(rows)
        self.assertEqual(len(logical), 1)
        self.assertEqual(logical[0]["id"], "x")
        self.assertEqual(logical[0]["member_ids"], ["x"])


class TestCluster(unittest.TestCase):
    def _d(self, _id, emb):
        return {"id": _id, "member_ids": [_id], "text": _id, "embedding": emb,
                "wing": "w", "room": "r"}

    def test_identical_pair_clusters(self):
        drawers = [self._d("a", [1.0, 0.0]), self._d("b", [1.0, 0.0])]
        clusters = dl.cluster_duplicates(drawers, tau=0.9)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({m["id"] for m in clusters[0]["members"]}, {"a", "b"})

    def test_orthogonal_not_clustered(self):
        drawers = [self._d("a", [1.0, 0.0]), self._d("b", [0.0, 1.0])]
        self.assertEqual(dl.cluster_duplicates(drawers, tau=0.9), [])

    def test_singleton_dropped(self):
        drawers = [self._d("a", [1.0, 0.0]), self._d("b", [1.0, 0.0]), self._d("c", [0.0, 1.0])]
        clusters = dl.cluster_duplicates(drawers, tau=0.9)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({m["id"] for m in clusters[0]["members"]}, {"a", "b"})

    def test_non_transitive_chain_one_component(self):
        # a~b and b~c by threshold, a and c below threshold: still one component.
        drawers = [
            self._d("a", [1.0, 0.0]),
            self._d("b", [0.92, 0.39]),   # close to a and to c
            self._d("c", [0.7, 0.71]),
        ]
        clusters = dl.cluster_duplicates(drawers, tau=0.9)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({m["id"] for m in clusters[0]["members"]}, {"a", "b", "c"})


class TestBuildWorklist(unittest.TestCase):
    def _d(self, _id, emb):
        return {"id": _id, "member_ids": [_id], "text": _id, "embedding": emb,
                "wing": "w", "room": "r"}

    def test_shape_and_null_decision(self):
        drawers = [self._d("a", [1.0, 0.0]), self._d("b", [1.0, 0.0])]
        wl = dl.build_worklist(drawers, tau=0.9, scope={"wing": "w"}, instructions="focus")
        self.assertEqual(wl["version"], dl.WORKLIST_VERSION)
        self.assertEqual(wl["scope"], {"wing": "w"})
        self.assertEqual(wl["params"]["tau"], 0.9)
        self.assertEqual(wl["instructions"], "focus")
        self.assertEqual(len(wl["items"]), 1)
        item = wl["items"][0]
        self.assertEqual(item["kind"], "merge")
        self.assertIsNone(item["decision"])
        self.assertEqual(item["evidence"]["size"], 2)


class TestGroupContradictions(unittest.TestCase):
    def test_groups_same_subject_predicate_with_distinct_objects(self):
        triples = [
            {"subject": "Alice", "predicate": "lives_in", "object": "Portland",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-02"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
            {"subject": "Bob", "predicate": "lives_in", "object": "Portland",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
            {"subject": "Alice", "predicate": "works_at", "object": "Contoso",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        ]

        clusters = dl.group_contradictions(triples)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["subject"], "Alice")
        self.assertEqual(clusters[0]["predicate"], "lives_in")
        self.assertEqual(clusters[0]["newest_object"], "Seattle")
        self.assertEqual([c["object"] for c in clusters[0]["candidates"]], ["Seattle", "Portland"])

    def test_distinct_groups_stay_separate_and_order_deterministically(self):
        triples = [
            {"subject": "Zoe", "predicate": "status_is", "object": "active",
             "valid_from": None, "extracted_at": "2025-01-02"},
            {"subject": "Zoe", "predicate": "status_is", "object": "paused",
             "valid_from": None, "extracted_at": "2025-01-01"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-01"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Portland",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-01"},
        ]

        clusters = dl.group_contradictions(triples)

        self.assertEqual([(c["subject"], c["predicate"]) for c in clusters],
                         [("Alice", "lives_in"), ("Zoe", "status_is")])
        self.assertEqual(clusters[1]["newest_object"], "active")


class TestBuildContradictionWorklist(unittest.TestCase):
    def test_shape_and_evidence(self):
        triples = [
            {"subject": "Alice", "predicate": "lives_in", "object": "Portland",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-02"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        ]

        wl = dl.build_contradiction_worklist(
            triples,
            scope={"palace": "/p", "task": "contradiction"},
            instructions="prefer sourced facts",
        )

        self.assertEqual(wl["version"], dl.WORKLIST_VERSION)
        self.assertEqual(wl["task"], "contradiction")
        self.assertEqual(wl["scope"], {"palace": "/p", "task": "contradiction"})
        self.assertEqual(wl["params"], {})
        self.assertEqual(wl["instructions"], "prefer sourced facts")
        self.assertEqual(len(wl["items"]), 1)
        item = wl["items"][0]
        self.assertEqual(item["kind"], "contradiction")
        self.assertIsNone(item["decision"])
        self.assertEqual(item["evidence"]["size"], 2)
        self.assertEqual(item["evidence"]["newest_object"], "Seattle")


class _FakeWriter:
    def __init__(self, fail_add=False):
        self.calls = []
        self.fail_add = fail_add

    def add_drawer(self, wing, room, content):
        self.calls.append(("add", wing, room, content))
        if self.fail_add:
            raise RuntimeError("boom")
        return {"drawer_id": "new1"}

    def delete_drawer(self, drawer_id):
        self.calls.append(("delete", drawer_id))
        return {"success": True}


class _FakeKgWriter:
    def __init__(self, fail_objects=None):
        self.calls = []
        self.fail_objects = set(fail_objects or [])

    def invalidate(self, subject, predicate, object, ended=None):
        self.calls.append((subject, predicate, object, ended))
        if object in self.fail_objects:
            raise RuntimeError(f"cannot invalidate {object}")
        return {"invalidated": 1}


class TestApplyDecisions(unittest.TestCase):
    def test_add_then_delete_order(self):
        w = _FakeWriter()
        decisions = [{"action": "merge", "wing": "w", "room": "r", "text": "merged",
                      "supersedes": ["a", "b"]}]
        report = dl.apply_merge_decisions(decisions, w)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(w.calls[0][0], "add")
        self.assertEqual([c for c in w.calls if c[0] == "delete"],
                         [("delete", "a"), ("delete", "b")])
        self.assertEqual(report["deleted"], ["a", "b"])

    def test_skip_ignored(self):
        w = _FakeWriter()
        report = dl.apply_merge_decisions([{"action": "skip"}], w)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [])

    def test_add_failure_skips_delete(self):
        w = _FakeWriter(fail_add=True)
        decisions = [{"action": "merge", "wing": "w", "room": "r", "text": "m",
                      "supersedes": ["a"]}]
        report = dl.apply_merge_decisions(decisions, w)
        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertNotIn(("delete", "a"), w.calls)  # non-destructive on add failure


class TestApplyContradictionDecisions(unittest.TestCase):
    def test_invalidate_calls_writer_for_each_object_and_counts_skip(self):
        w = _FakeKgWriter()
        decisions = [
            {"action": "invalidate", "subject": "Alice", "predicate": "lives_in",
             "invalidate": ["Portland", "Seattle"]},
            {"action": "skip"},
        ]

        report = dl.apply_contradiction_decisions(decisions, w)

        self.assertEqual(report["invalidated"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [
            ("Alice", "lives_in", "Portland", None),
            ("Alice", "lives_in", "Seattle", None),
        ])
        self.assertEqual(report["invalidated_facts"], [
            {"subject": "Alice", "predicate": "lives_in", "object": "Portland"},
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle"},
        ])

    def test_writer_error_is_recorded_and_later_objects_still_process(self):
        w = _FakeKgWriter(fail_objects={"Portland"})
        decisions = [
            {"action": "invalidate", "subject": "Alice", "predicate": "lives_in",
             "invalidate": ["Portland", "Seattle"]},
        ]

        report = dl.apply_contradiction_decisions(decisions, w)

        self.assertEqual(report["invalidated"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["object"], "Portland")
        self.assertIn("cannot invalidate Portland", report["errors"][0]["error"])
        self.assertEqual(report["invalidated_facts"], [
            {"subject": "Alice", "predicate": "lives_in", "object": "Seattle"},
        ])


if __name__ == "__main__":
    unittest.main()
