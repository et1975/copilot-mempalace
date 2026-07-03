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


class TestExtractSessionId(unittest.TestCase):
    def test_finds_hyphenated_guid(self):
        text = "session note SESSION_ID: 123e4567-e89b-12d3-a456-426614174000 done"
        self.assertEqual(dl.extract_session_id(text), "123e4567-e89b-12d3-a456-426614174000")

    def test_returns_none_when_absent(self):
        self.assertIsNone(dl.extract_session_id("session note without an id"))

    def test_returns_first_when_multiple(self):
        text = (
            "SESSION_ID: 11111111-1111-1111-1111-111111111111 "
            "SESSION_ID: 22222222-2222-2222-2222-222222222222"
        )
        self.assertEqual(dl.extract_session_id(text), "11111111-1111-1111-1111-111111111111")

    def test_label_is_case_insensitive(self):
        self.assertEqual(dl.extract_session_id("session_id: ABCDEF12-3456"), "ABCDEF12-3456")


class TestGroupObservationThemes(unittest.TestCase):
    def _e(self, _id, emb, session_id, text=None):
        return {
            "id": _id,
            "text": text or _id,
            "embedding": emb,
            "session_id": session_id,
            "agent": "copilot",
            "date": "2026-07-03",
            "topic": "dreaming",
        }

    def test_similar_entries_from_distinct_sessions_form_theme(self):
        entries = [
            self._e("a", [1.0, 0.0], "s2"),
            self._e("b", [0.99, 0.01], "s1"),
        ]

        themes = dl.group_observation_themes(entries, tau=0.9, min_support=2)

        self.assertEqual(len(themes), 1)
        self.assertEqual(themes[0]["support"], 2)
        self.assertEqual(themes[0]["support_ids"], ["s1", "s2"])
        self.assertEqual({m["id"] for m in themes[0]["members"]}, {"a", "b"})
        self.assertEqual(themes[0]["pair_sims"][0]["a"], "a")
        self.assertEqual(themes[0]["pair_sims"][0]["b"], "b")

    def test_similar_entries_from_same_session_are_dropped_by_support(self):
        entries = [
            self._e("a", [1.0, 0.0], "s1"),
            self._e("b", [0.99, 0.01], "s1"),
        ]

        self.assertEqual(dl.group_observation_themes(entries, tau=0.9, min_support=2), [])

    def test_dissimilar_entries_do_not_form_theme(self):
        entries = [
            self._e("a", [1.0, 0.0], "s1"),
            self._e("b", [0.0, 1.0], "s2"),
        ]

        self.assertEqual(dl.group_observation_themes(entries, tau=0.9, min_support=2), [])

    def test_support_counts_distinct_sessions(self):
        entries = [
            self._e("a", [1.0, 0.0], "s1"),
            self._e("b", [0.99, 0.01], "s1"),
            self._e("c", [0.98, 0.02], "s2"),
        ]

        themes = dl.group_observation_themes(entries, tau=0.9, min_support=2)

        self.assertEqual(len(themes), 1)
        self.assertEqual(themes[0]["support"], 2)
        self.assertEqual(themes[0]["support_ids"], ["s1", "s2"])
        self.assertEqual(len(themes[0]["members"]), 3)

    def test_ordering_is_deterministic_by_support_then_smallest_member_id(self):
        entries = [
            self._e("z", [1.0, 0.0], "s1"),
            self._e("y", [0.99, 0.01], "s2"),
            self._e("a", [0.0, 1.0], "s3"),
            self._e("b", [0.01, 0.99], "s4"),
            self._e("c", [0.02, 0.98], "s5"),
        ]

        themes = dl.group_observation_themes(entries, tau=0.9, min_support=2)

        self.assertEqual([[m["id"] for m in t["members"]] for t in themes],
                         [["a", "b", "c"], ["z", "y"]])
        self.assertEqual([t["support"] for t in themes], [3, 2])


class TestBuildPatternWorklist(unittest.TestCase):
    def test_shape_evidence_and_params(self):
        themes = [
            {
                "members": [
                    {"id": "a", "text": "alpha", "session_id": "s1",
                     "agent": "copilot", "date": "2026-07-03", "topic": "t"},
                    {"id": "b", "text": "beta", "session_id": "s2",
                     "agent": "copilot", "date": "2026-07-04", "topic": "t"},
                ],
                "support": 2,
                "support_ids": ["s1", "s2"],
                "pair_sims": [{"a": "a", "b": "b", "sim": 0.98}],
            }
        ]
        params = {"tau": 0.65, "min_support": 2}

        wl = dl.build_pattern_worklist(
            themes,
            scope={"wing": "w"},
            params=params,
            instructions="induce rules",
        )

        self.assertEqual(wl["version"], dl.WORKLIST_VERSION)
        self.assertEqual(wl["task"], "pattern")
        self.assertEqual(wl["scope"], {"wing": "w"})
        self.assertIs(wl["params"], params)
        self.assertEqual(wl["instructions"], "induce rules")
        self.assertEqual(len(wl["items"]), 1)
        item = wl["items"][0]
        self.assertEqual(item["kind"], "pattern")
        self.assertEqual(item["cluster_id"], 0)
        self.assertIsNone(item["decision"])
        self.assertEqual(item["evidence"]["size"], 2)
        self.assertEqual(item["evidence"]["support"], 2)
        self.assertEqual(item["evidence"]["support_ids"], ["s1", "s2"])
        self.assertEqual(item["members"][0]["session_id"], "s1")


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


class _FakePatternWriter:
    def __init__(self, fail_texts=None):
        self.calls = []
        self.fail_texts = set(fail_texts or [])
        self.delete_called = False

    def add_drawer(self, wing, room, content):
        self.calls.append(("add", wing, room, content))
        if content in self.fail_texts:
            raise RuntimeError("boom")
        return {"drawer_id": f"new{len(self.calls)}"}

    def delete_drawer(self, drawer_id):
        self.delete_called = True
        self.calls.append(("delete", drawer_id))
        raise AssertionError("delete_drawer must not be called")


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


class TestApplyPatternDecisions(unittest.TestCase):
    def test_surface_adds_drawer_and_counts_skip(self):
        w = _FakePatternWriter()
        decisions = [
            {"action": "surface", "wing": "w", "room": "r", "text": "rule",
             "supported_by": ["s1", "s2"]},
            {"action": "skip"},
        ]

        report = dl.apply_pattern_decisions(decisions, w)

        self.assertEqual(report["surfaced"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [("add", "w", "r", "rule")])
        self.assertEqual(report["added"], [{"drawer_id": "new1"}])
        self.assertFalse(w.delete_called)

    def test_ungrounded_surface_is_rejected_without_add(self):
        w = _FakePatternWriter()
        decisions = [{"action": "surface", "wing": "w", "room": "r", "text": "rule",
                      "supported_by": []}]

        report = dl.apply_pattern_decisions(decisions, w)

        self.assertEqual(report["surfaced"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(w.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "groundedness")
        self.assertEqual(report["errors"][0]["error"], "unsupported rule")
        self.assertFalse(w.delete_called)

    def test_add_error_is_recorded_and_later_decisions_continue(self):
        w = _FakePatternWriter(fail_texts={"bad"})
        decisions = [
            {"action": "surface", "wing": "w", "room": "r", "text": "bad",
             "supported_by": ["s1"]},
            {"action": "surface", "wing": "w", "room": "r", "text": "good",
             "supported_by": ["s2"]},
        ]

        report = dl.apply_pattern_decisions(decisions, w)

        self.assertEqual(report["surfaced"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["stage"], "add")
        self.assertIn("boom", report["errors"][0]["error"])
        self.assertEqual(w.calls, [("add", "w", "r", "bad"), ("add", "w", "r", "good")])
        self.assertFalse(w.delete_called)


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
