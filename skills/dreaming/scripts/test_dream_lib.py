"""Unit tests for the dreaming pure core. Stdlib unittest only — no external deps.

Run: cd skills/dreaming/scripts && python3 -m unittest -v
"""
from datetime import datetime
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
    def _d(self, _id, emb, room="r"):
        return {"id": _id, "member_ids": [_id], "text": _id, "embedding": emb,
                "wing": "w", "room": room}

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

    def test_cross_room_cluster_is_partitioned_and_singletons_dropped(self):
        drawers = [
            self._d("a", [1.0, 0.0], room="a"),
            self._d("b", [1.0, 0.0], room="b"),
        ]

        wl = dl.build_worklist(drawers, tau=0.9, scope={"wing": "w"})

        self.assertEqual(wl["items"], [])

    def test_mixed_room_split_keeps_same_room_subcluster(self):
        drawers = [
            self._d("a", [1.0, 0.0], room="a"),
            self._d("b", [1.0, 0.0], room="a"),
            self._d("c", [1.0, 0.0], room="b"),
        ]

        wl = dl.build_worklist(drawers, tau=0.9, scope={"wing": "w"})

        self.assertEqual(len(wl["items"]), 1)
        self.assertEqual([m["id"] for m in wl["items"][0]["members"]], ["a", "b"])
        self.assertTrue(wl["items"][0]["mixed_room_split"])


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

    def test_groups_by_subject_id_not_display_name(self):
        triples = [
            {"triple_id": "t1", "subject": "Alice", "subject_id": "entity-1",
             "predicate": "lives_in", "object": "Portland", "object_id": "city-portland",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-02"},
            {"triple_id": "t2", "subject": "Alice", "subject_id": "entity-2",
             "predicate": "lives_in", "object": "Seattle", "object_id": "city-seattle",
             "valid_from": "2024-01-01", "extracted_at": "2024-01-02"},
            {"triple_id": "t3", "subject": "Alice", "subject_id": "entity-1",
             "predicate": "lives_in", "object": "Vancouver", "object_id": "city-vancouver",
             "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        ]

        clusters = dl.group_contradictions(triples)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["subject"], "Alice")
        self.assertEqual(clusters[0]["subject_id"], "entity-1")
        self.assertEqual([c["object_id"] for c in clusters[0]["candidates"]],
                         ["city-vancouver", "city-portland"])
        self.assertEqual([c["triple_id"] for c in clusters[0]["candidates"]], ["t3", "t1"])
        self.assertEqual([c["triple_ids"] for c in clusters[0]["candidates"]], [["t3"], ["t1"]])

    def test_future_valid_from_is_excluded(self):
        triples = [
            {"triple_id": "t1", "subject": "Alice", "subject_id": "entity-1",
             "predicate": "lives_in", "object": "Portland", "object_id": "city-portland",
             "valid_from": "2026-01-01", "extracted_at": "2025-01-01"},
            {"triple_id": "t2", "subject": "Alice", "subject_id": "entity-1",
             "predicate": "lives_in", "object": "Seattle", "object_id": "city-seattle",
             "valid_from": "2026-07-01", "extracted_at": "2025-01-02"},
        ]

        clusters = dl.group_contradictions(triples, now="2026-02-01T00:00:00")

        self.assertEqual(clusters, [])


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
        self.assertEqual(
            dl.extract_all_session_ids(text),
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        )

    def test_label_is_case_insensitive_and_uuid_is_canonical(self):
        self.assertIsNone(dl.extract_session_id("session_id: deadbeef"))
        self.assertEqual(
            dl.extract_session_id("session_id: ABCDEF12-3456-7890-abcd-EF1234567890"),
            "ABCDEF12-3456-7890-abcd-EF1234567890",
        )

    def test_extract_all_session_ids_dedupes_preserving_order(self):
        text = (
            "SESSION_ID: 11111111-1111-1111-1111-111111111111 "
            "SESSION_ID: 22222222-2222-2222-2222-222222222222 "
            "SESSION_ID: 11111111-1111-1111-1111-111111111111"
        )
        self.assertEqual(
            dl.extract_all_session_ids(text),
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        )


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


class TestComputeRedundancy(unittest.TestCase):
    def test_identical_pair_scores_one_each(self):
        drawers = [
            {"id": "a", "embedding": [1.0, 0.0]},
            {"id": "b", "embedding": [1.0, 0.0]},
        ]

        redundancy = dl.compute_redundancy(drawers)

        self.assertAlmostEqual(redundancy["a"], 1.0)
        self.assertAlmostEqual(redundancy["b"], 1.0)

    def test_orthogonal_pair_scores_zero_each(self):
        drawers = [
            {"id": "a", "embedding": [1.0, 0.0]},
            {"id": "b", "embedding": [0.0, 1.0]},
        ]

        self.assertEqual(dl.compute_redundancy(drawers), {"a": 0.0, "b": 0.0})

    def test_singleton_scores_zero(self):
        self.assertEqual(dl.compute_redundancy([{"id": "a", "embedding": [1.0]}]), {"a": 0.0})


class TestDrawerSalience(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-07-03T20:20:12")

    def _drawer(self, filed_at="2026-01-04T20:20:12", text="durable memory"):
        return {"id": "d1", "text": text, "filed_at": filed_at}

    def test_kg_degree_increases_salience(self):
        low = dl.drawer_salience(self._drawer(), redundancy=0.0, kg_degree=0, now=self.now)
        high = dl.drawer_salience(self._drawer(), redundancy=0.0, kg_degree=5, now=self.now)

        self.assertGreaterEqual(high["v"], low["v"])

    def test_redundancy_decreases_salience(self):
        low = dl.drawer_salience(self._drawer(), redundancy=0.0, kg_degree=0, now=self.now)
        high = dl.drawer_salience(self._drawer(), redundancy=1.0, kg_degree=0, now=self.now)

        self.assertLessEqual(high["v"], low["v"])

    def test_age_decreases_salience(self):
        recent = dl.drawer_salience(
            self._drawer(filed_at="2026-07-01T20:20:12"),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )
        old = dl.drawer_salience(
            self._drawer(filed_at="2025-07-03T20:20:12"),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )

        self.assertLessEqual(old["v"], recent["v"])

    def test_ephemeral_negative_decreases_salience(self):
        durable = dl.drawer_salience(
            self._drawer(text="durable memory"),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )
        ephemeral = dl.drawer_salience(
            self._drawer(text="keep this for now in this session"),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )

        self.assertTrue(ephemeral["negatives"])
        self.assertLess(ephemeral["v"], durable["v"])

    def test_salience_is_clamped_between_zero_and_one(self):
        high = dl.drawer_salience(
            self._drawer(filed_at="2026-07-03T20:20:12"),
            redundancy=0.0,
            kg_degree=100,
            now=self.now,
            weights={"recency": 1.0, "kg_degree": 1.0, "redundancy": 0.0, "negatives": 0.0},
        )
        low = dl.drawer_salience(
            self._drawer(text="throwaway scratch just for this"),
            redundancy=1.0,
            kg_degree=0,
            now=self.now,
            weights={"recency": 0.0, "kg_degree": 0.0, "redundancy": 1.0, "negatives": 1.0},
        )

        self.assertGreaterEqual(high["v"], 0.0)
        self.assertLessEqual(high["v"], 1.0)
        self.assertGreaterEqual(low["v"], 0.0)
        self.assertLessEqual(low["v"], 1.0)

    def test_detects_all_ephemeral_markers(self):
        markers = ["for now", "this session", "temporarily", "one-off", "throwaway", "scratch", "just for this"]
        for marker in markers:
            with self.subTest(marker=marker):
                scored = dl.drawer_salience(self._drawer(text=f"Keep {marker}"), 0.0, 0, self.now)
                self.assertTrue(scored["negatives"])

    def test_age_days_from_filed_at_and_missing_is_zero(self):
        scored = dl.drawer_salience(
            self._drawer(filed_at="2026-06-03T20:20:12Z"),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )
        missing = dl.drawer_salience(
            self._drawer(filed_at=None),
            redundancy=0.0,
            kg_degree=0,
            now=self.now,
        )

        self.assertEqual(scored["age_days"], 30)
        self.assertEqual(missing["age_days"], 0)


class TestSelectPruneCandidates(unittest.TestCase):
    def _drawer(self, _id, v, age_days, kg_degree=0, pinned=False, topic=None, room="r"):
        return {
            "id": _id,
            "text": _id,
            "member_ids": [_id],
            "wing": "w",
            "room": room,
            "pinned": pinned,
            "metadata": {"topic": topic} if topic is not None else {},
            "salience": {
                "id": _id,
                "age_days": age_days,
                "kg_degree": kg_degree,
                "redundancy": 1.0,
                "negatives": True,
                "v": v,
            },
        }

    def test_selects_only_when_all_gates_hold(self):
        selected = dl.select_prune_candidates(
            [
                self._drawer("qualifies", v=0.1, age_days=365),
                self._drawer("has_kg", v=0.1, age_days=365, kg_degree=1),
                self._drawer("too_recent", v=0.1, age_days=10),
                self._drawer("pinned", v=0.1, age_days=365, pinned=True),
                self._drawer("high_value", v=0.9, age_days=365),
            ],
            v_min=0.2,
            age_floor_days=180,
        )

        self.assertEqual([d["id"] for d in selected], ["qualifies"])

    def test_last_drawer_on_topic_is_protected(self):
        selected = dl.select_prune_candidates(
            [
                self._drawer("shared-a", v=0.1, age_days=365, topic="shared"),
                self._drawer("shared-b", v=0.1, age_days=365, topic="shared"),
                self._drawer("unique", v=0.1, age_days=365, topic="unique"),
            ],
            v_min=0.2,
            age_floor_days=180,
        )

        self.assertEqual([d["id"] for d in selected], ["shared-a", "shared-b"])


class TestBuildPruneWorklist(unittest.TestCase):
    def test_shape_and_salience(self):
        candidate = {
            "id": "d1",
            "member_ids": ["p1"],
            "text": "old scratch",
            "wing": "w",
            "room": "r",
            "metadata": {"topic": "dreaming", "pinned": True},
            "salience": {"id": "d1", "age_days": 365, "kg_degree": 0, "redundancy": 1.0, "negatives": True, "v": 0.0},
        }

        wl = dl.build_prune_worklist(
            [candidate],
            scope={"wing": "w"},
            params={"v_min": 0.2, "age_floor_days": 180},
            instructions="review carefully",
        )

        self.assertEqual(wl["version"], dl.WORKLIST_VERSION)
        self.assertEqual(wl["task"], "prune")
        self.assertEqual(wl["scope"], {"wing": "w"})
        self.assertEqual(wl["params"], {"v_min": 0.2, "age_floor_days": 180})
        self.assertEqual(wl["instructions"], "review carefully")
        self.assertEqual(len(wl["items"]), 1)
        item = wl["items"][0]
        self.assertEqual(item["kind"], "prune")
        self.assertEqual(item["id"], "d1")
        self.assertEqual(item["member_ids"], ["p1"])
        self.assertEqual(item["text"], "old scratch")
        self.assertEqual(item["wing"], "w")
        self.assertEqual(item["room"], "r")
        self.assertEqual(item["topic"], "dreaming")
        self.assertTrue(item["pinned"])
        self.assertEqual(item["salience"], candidate["salience"])
        self.assertIsNone(item["decision"])


class _FakeArchiver:
    def __init__(self, fail_ids=None):
        self.calls = []
        self.archived = []
        self.deleted = []
        self.fail_ids = set(fail_ids or [])

    def archive_then_delete(self, record):
        self.calls.append(record)
        if record["id"] in self.fail_ids:
            raise RuntimeError(f"archive failed for {record['id']}")
        self.archived.append(record)
        self.deleted.extend(record["member_ids"])
        return {"archived": record["id"]}


class TestApplyPruneDecisions(unittest.TestCase):
    def _prune_decision(self, _id="d1", kg_degree=0, pinned=False):
        return {
            "action": "prune",
            "id": _id,
            "member_ids": [f"{_id}-p"],
            "wing": "w",
            "room": "r",
            "text": "old scratch",
            "pinned": pinned,
            "salience": {"id": _id, "age_days": 365, "kg_degree": kg_degree, "redundancy": 1.0, "negatives": True, "v": 0.0},
        }

    def test_valid_prune_archives_then_deletes_once(self):
        archiver = _FakeArchiver()

        report = dl.apply_prune_decisions([self._prune_decision()], archiver)

        self.assertEqual(report["pruned"], 1)
        self.assertEqual(report["kept"], 0)
        self.assertEqual(len(archiver.calls), 1)
        self.assertEqual(len(report["archived"]), 1)
        record = archiver.calls[0]
        self.assertEqual(record["id"], "d1")
        self.assertEqual(record["member_ids"], ["d1-p"])
        self.assertEqual(record["wing"], "w")
        self.assertEqual(record["room"], "r")
        self.assertEqual(record["text"], "old scratch")
        self.assertEqual(record["salience"]["kg_degree"], 0)
        self.assertIn("pruned_at", record)
        self.assertEqual(archiver.deleted, ["d1-p"])

    def test_keep_is_counted_without_archiver_call(self):
        archiver = _FakeArchiver()

        report = dl.apply_prune_decisions([{"action": "keep"}], archiver)

        self.assertEqual(report["kept"], 1)
        self.assertEqual(report["pruned"], 0)
        self.assertEqual(archiver.calls, [])

    def test_protected_kg_decision_records_error_without_archiver_call(self):
        archiver = _FakeArchiver()

        report = dl.apply_prune_decisions([self._prune_decision(kg_degree=1)], archiver)

        self.assertEqual(report["pruned"], 0)
        self.assertEqual(archiver.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "protected")
        self.assertEqual(report["errors"][0]["error"], "protected drawer")

    def test_protected_pinned_decision_records_error_without_archiver_call(self):
        archiver = _FakeArchiver()

        report = dl.apply_prune_decisions([self._prune_decision(pinned=True)], archiver)

        self.assertEqual(report["pruned"], 0)
        self.assertEqual(archiver.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "protected")

    def test_archive_failure_is_recorded_and_later_decisions_continue(self):
        archiver = _FakeArchiver(fail_ids={"bad"})
        decisions = [
            self._prune_decision(_id="bad"),
            self._prune_decision(_id="good"),
        ]

        report = dl.apply_prune_decisions(decisions, archiver)

        self.assertEqual(report["pruned"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["stage"], "archive")
        self.assertIn("archive failed for bad", report["errors"][0]["error"])
        self.assertEqual([r["id"] for r in archiver.archived], ["good"])
        self.assertEqual(archiver.deleted, ["good-p"])


class _FakeWriter:
    def __init__(self, fail_add=False):
        self.calls = []
        self.fail_add = fail_add

    def add_drawer(self, wing, room, content, metadata=None):
        self.calls.append(("add", wing, room, content, metadata))
        if self.fail_add:
            raise RuntimeError("boom")
        return {"drawer_id": "new1"}

    def delete_drawer(self, drawer_id):
        self.calls.append(("delete", drawer_id))
        return {"success": True}


class _FakeKgWriter:
    def __init__(self, fail_triple_ids=None):
        self.calls = []
        self.fail_triple_ids = set(fail_triple_ids or [])

    def invalidate_triples(self, triple_ids):
        self.calls.append(list(triple_ids))
        failed = self.fail_triple_ids.intersection(triple_ids)
        if failed:
            raise RuntimeError(f"cannot invalidate {sorted(failed)[0]}")
        return {"invalidated": len(triple_ids)}


class _FakePatternWriter:
    def __init__(self, fail_texts=None):
        self.calls = []
        self.fail_texts = set(fail_texts or [])
        self.delete_called = False

    def add_drawer(self, wing, room, content, metadata=None):
        self.calls.append(("add", wing, room, content, metadata))
        if content in self.fail_texts:
            raise RuntimeError("boom")
        return {"drawer_id": f"new{len(self.calls)}"}

    def delete_drawer(self, drawer_id):
        self.delete_called = True
        self.calls.append(("delete", drawer_id))
        raise AssertionError("delete_drawer must not be called")


class TestApplyDecisions(unittest.TestCase):
    def test_add_then_archive_order(self):
        w = _FakeWriter()
        archiver = _FakeArchiver()
        decisions = [{"action": "merge", "wing": "w", "room": "r", "text": "merged",
                      "supersedes": ["a", "b"]}]
        report = dl.apply_merge_decisions(decisions, w, archiver)
        self.assertEqual(report["merged"], 1)
        self.assertEqual(w.calls[0][0], "add")
        self.assertEqual(w.calls[0][4], {"supersedes": ["a", "b"], "kind": "merged"})
        self.assertEqual([c for c in w.calls if c[0] == "delete"], [])
        self.assertEqual(len(archiver.calls), 1)
        self.assertEqual(archiver.calls[0]["member_ids"], ["a", "b"])
        self.assertEqual(archiver.calls[0]["reason"], "merge")
        self.assertEqual(report["deleted"], ["a", "b"])

    def test_skip_ignored(self):
        w = _FakeWriter()
        archiver = _FakeArchiver()
        report = dl.apply_merge_decisions([{"action": "skip"}], w, archiver)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [])
        self.assertEqual(archiver.calls, [])

    def test_add_failure_skips_archive(self):
        w = _FakeWriter(fail_add=True)
        archiver = _FakeArchiver()
        decisions = [{"action": "merge", "wing": "w", "room": "r", "text": "m",
                      "supersedes": ["a"]}]
        report = dl.apply_merge_decisions(decisions, w, archiver)
        self.assertEqual(report["merged"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(archiver.calls, [])  # non-destructive on add failure

    def test_empty_text_or_supersedes_records_soundness_error_without_writes(self):
        w = _FakeWriter()
        archiver = _FakeArchiver()

        report = dl.apply_merge_decisions(
            [
                {"action": "merge", "wing": "w", "room": "r", "text": "", "supersedes": ["a"]},
                {"action": "merge", "wing": "w", "room": "r", "text": "merged", "supersedes": []},
            ],
            w,
            archiver,
        )

        self.assertEqual(report["merged"], 0)
        self.assertEqual([e["stage"] for e in report["errors"]], ["soundness", "soundness"])
        self.assertEqual(w.calls, [])
        self.assertEqual(archiver.calls, [])


class TestApplyPatternDecisions(unittest.TestCase):
    def test_surface_adds_drawer_and_counts_skip(self):
        w = _FakePatternWriter()
        decisions = [
            {"action": "surface", "wing": "w", "room": "r", "text": "rule",
             "supported_by": ["s1", "s2"]},
            {"action": "skip"},
        ]

        report = dl.apply_pattern_decisions(decisions, w, min_support=2)

        self.assertEqual(report["surfaced"], 1)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [("add", "w", "r", "rule",
                                    {"supported_by": ["s1", "s2"], "kind": "lesson"})])
        self.assertEqual(report["added"], [{"drawer_id": "new1"}])
        self.assertFalse(w.delete_called)

    def test_ungrounded_surface_is_rejected_without_add(self):
        w = _FakePatternWriter()
        decisions = [{"action": "surface", "wing": "w", "room": "r", "text": "rule",
                      "supported_by": []}]

        report = dl.apply_pattern_decisions(decisions, w, min_support=2)

        self.assertEqual(report["surfaced"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(w.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "groundedness")
        self.assertEqual(report["errors"][0]["error"], "unsupported rule")
        self.assertFalse(w.delete_called)

    def test_surface_with_too_few_distinct_support_ids_is_rejected(self):
        w = _FakePatternWriter()
        decisions = [{"action": "surface", "wing": "w", "room": "r", "text": "rule",
                      "supported_by": ["s1", "s1"], "allowed_support": ["s1", "s2"]}]

        report = dl.apply_pattern_decisions(decisions, w, min_support=2)

        self.assertEqual(report["surfaced"], 0)
        self.assertEqual(w.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "groundedness")

    def test_surface_support_must_be_subset_of_allowed_support(self):
        w = _FakePatternWriter()
        decisions = [{"action": "surface", "wing": "w", "room": "r", "text": "rule",
                      "supported_by": ["s1", "s3"], "allowed_support": ["s1", "s2"]}]

        report = dl.apply_pattern_decisions(decisions, w, min_support=2)

        self.assertEqual(report["surfaced"], 0)
        self.assertEqual(w.calls, [])
        self.assertEqual(report["errors"][0]["stage"], "groundedness")

    def test_add_error_is_recorded_and_later_decisions_continue(self):
        w = _FakePatternWriter(fail_texts={"bad"})
        decisions = [
            {"action": "surface", "wing": "w", "room": "r", "text": "bad",
             "supported_by": ["s1", "s2"]},
            {"action": "surface", "wing": "w", "room": "r", "text": "good",
             "supported_by": ["s2", "s3"]},
        ]

        report = dl.apply_pattern_decisions(decisions, w, min_support=2)

        self.assertEqual(report["surfaced"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["stage"], "add")
        self.assertIn("boom", report["errors"][0]["error"])
        self.assertEqual([c[:4] for c in w.calls], [("add", "w", "r", "bad"), ("add", "w", "r", "good")])
        self.assertFalse(w.delete_called)


class TestApplyContradictionDecisions(unittest.TestCase):
    def test_invalidate_calls_writer_for_exact_triple_ids_and_counts_skip(self):
        w = _FakeKgWriter()
        decisions = [
            {"action": "invalidate", "invalidate": ["triple-1", "triple-2"]},
            {"action": "skip"},
        ]

        report = dl.apply_contradiction_decisions(decisions, w)

        self.assertEqual(report["invalidated"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.calls, [["triple-1", "triple-2"]])
        self.assertEqual(report["invalidated_facts"], [
            {"triple_id": "triple-1"},
            {"triple_id": "triple-2"},
        ])

    def test_writer_error_is_recorded_and_later_decisions_still_process(self):
        w = _FakeKgWriter(fail_triple_ids={"triple-1"})
        decisions = [
            {"action": "invalidate", "invalidate": ["triple-1", "triple-2"]},
            {"action": "invalidate", "invalidate": ["triple-3"]},
        ]

        report = dl.apply_contradiction_decisions(decisions, w)

        self.assertEqual(report["invalidated"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["triple_ids"], ["triple-1", "triple-2"])
        self.assertIn("cannot invalidate triple-1", report["errors"][0]["error"])
        self.assertEqual(report["invalidated_facts"], [
            {"triple_id": "triple-3"},
        ])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Task 1: Ontology rule model + config normalization
# ---------------------------------------------------------------------------

import dream_lib as dream_lib  # noqa: E402 (alias for plan-compatible access)


class OntologyConfigTests(unittest.TestCase):
    def test_normalize_predicate_lowercases_and_underscores(self):
        self.assertEqual(dream_lib.normalize_predicate("Depends On"), "depends_on")
        self.assertEqual(dream_lib.normalize_predicate("depends-on"), "depends_on")

    def test_ontology_version_is_stable_content_hash(self):
        rules = [{"id": "transitive:depends_on", "family": "transitive",
                  "predicate": "depends_on", "enabled": True}]
        v1 = dream_lib.ontology_version(rules)
        v2 = dream_lib.ontology_version(list(rules))
        self.assertEqual(v1, v2)
        self.assertNotEqual(v1, dream_lib.ontology_version([]))

    def test_enabled_rules_filters_disabled_and_unknown_family(self):
        rules = [
            {"id": "a", "family": "transitive", "predicate": "p", "enabled": True},
            {"id": "b", "family": "transitive", "predicate": "q", "enabled": False},
            {"id": "c", "family": "bogus", "predicate": "r", "enabled": True},
        ]
        got = [r["id"] for r in dream_lib.enabled_rules(rules)]
        self.assertEqual(got, ["a"])

    def test_derived_predicate_defaults_to_closure_suffix(self):
        rule = {"id": "a", "family": "transitive", "predicate": "depends_on", "enabled": True}
        self.assertEqual(dream_lib.derived_predicate_for(rule), "depends_on_closure")
        rule2 = dict(rule, derived_predicate="reaches")
        self.assertEqual(dream_lib.derived_predicate_for(rule2), "reaches")


# ---------------------------------------------------------------------------
# Task 2: Canonical triple keys + stable candidate id
# ---------------------------------------------------------------------------

class DeriveKeyTests(unittest.TestCase):
    def test_triple_id_key_uses_entity_ids_not_names(self):
        t = {"subject_id": 7, "predicate": "Depends On", "object_id": 9,
             "subject": "A", "object": "B"}
        self.assertEqual(dream_lib.triple_id_key(t), (7, "depends_on", 9))

    def test_candidate_id_is_stable_and_order_independent_on_premises(self):
        concl = {"subject_id": 1, "predicate": "depends_on_closure", "object_id": 3}
        c1 = dream_lib.derive_candidate_id(concl, "transitive:depends_on", [101, 102], "onto:x")
        c2 = dream_lib.derive_candidate_id(concl, "transitive:depends_on", [102, 101], "onto:x")
        self.assertEqual(c1, c2)
        self.assertTrue(c1.startswith("derive:"))

    def test_candidate_id_changes_with_ontology_version(self):
        concl = {"subject_id": 1, "predicate": "depends_on_closure", "object_id": 3}
        a = dream_lib.derive_candidate_id(concl, "r", [1], "onto:x")
        b = dream_lib.derive_candidate_id(concl, "r", [1], "onto:y")
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------------------
# Task 3: Interval-overlap temporal validity
# ---------------------------------------------------------------------------

class IntervalOverlapTests(unittest.TestCase):
    def test_overlap_of_open_intervals_is_max_start_none_end(self):
        got = dream_lib.premise_interval([
            {"valid_from": "2026-01-01", "valid_to": None},
            {"valid_from": "2026-03-01", "valid_to": None},
        ])
        self.assertEqual(got, ("2026-03-01T00:00:00", None))

    def test_overlap_with_bounded_end_takes_min_end(self):
        got = dream_lib.premise_interval([
            {"valid_from": "2026-01-01", "valid_to": "2026-06-01"},
            {"valid_from": "2026-02-01", "valid_to": "2026-05-01"},
        ])
        self.assertEqual(got, ("2026-02-01T00:00:00", "2026-05-01T00:00:00"))

    def test_disjoint_intervals_return_none(self):
        got = dream_lib.premise_interval([
            {"valid_from": "2026-01-01", "valid_to": "2026-02-01"},
            {"valid_from": "2026-03-01", "valid_to": None},
        ])
        self.assertIsNone(got)  # max_start (2026-03) >= min_end (2026-02) => empty

    def test_touching_intervals_are_empty(self):
        # max_start == min_end is a zero-width (empty) intersection
        got = dream_lib.premise_interval([
            {"valid_from": "2026-01-01", "valid_to": "2026-03-01"},
            {"valid_from": "2026-03-01", "valid_to": None},
        ])
        self.assertIsNone(got)

    def test_mixed_aware_and_naive_timestamps_do_not_crash(self):
        got = dream_lib.premise_interval([
            {"valid_from": "2026-01-01T00:00:00+00:00", "valid_to": None},
            {"valid_from": "2026-02-01", "valid_to": None},
        ])
        self.assertEqual(got, ("2026-02-01T00:00:00", None))


# ---------------------------------------------------------------------------
# Task 4: deductive_closure — bounded semi-naive forward chaining
# ---------------------------------------------------------------------------

def _t(tid, s, p, o, sid=None, oid=None, vf=None, vt=None, conf=1.0):
    return {"triple_id": tid, "subject": s, "predicate": p, "object": o,
            "subject_id": sid if sid is not None else s, "object_id": oid if oid is not None else o,
            "valid_from": vf, "valid_to": vt, "confidence": conf, "source_drawer_id": f"d{tid}"}

TRANS_RULES = [{"id": "transitive:depends_on", "family": "transitive",
                "predicate": "depends_on", "enabled": True, "max_depth": 3}]

class DeductiveClosureTests(unittest.TestCase):
    def test_transitive_chain_emits_closure_predicate(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "C")]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=3, max_iterations=10, max_candidates=500)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["conclusion"]["predicate"], "depends_on_closure")
        self.assertEqual((c["conclusion"]["subject"], c["conclusion"]["object"]), ("A", "C"))
        self.assertEqual(sorted(c["proof"]["premise_ids"]), [1, 2])
        self.assertEqual(c["proof"]["depth"], 2)
        self.assertEqual(c["rule"]["id"], "transitive:depends_on")

    def test_proof_depth_reflects_chain_length(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "C"),
                   _t(3, "C", "depends_on", "D")]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=3, max_iterations=10, max_candidates=500)
        depth_by_pair = {(c["conclusion"]["subject"], c["conclusion"]["object"]): c["proof"]["depth"]
                         for c in cands}
        self.assertEqual(depth_by_pair[("A", "D")], 3)
        self.assertEqual(depth_by_pair[("A", "C")], 2)

    def test_longer_chain_reaches_full_closure_via_closure_edges(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "C"),
                   _t(3, "C", "depends_on", "D")]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=3, max_iterations=10, max_candidates=500)
        pairs = {(c["conclusion"]["subject"], c["conclusion"]["object"]) for c in cands}
        self.assertEqual(pairs, {("A", "C"), ("A", "D"), ("B", "D")})

    def test_reflexive_conclusions_suppressed_by_default(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "A")]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=3, max_iterations=10, max_candidates=500)
        for c in cands:
            self.assertNotEqual(c["conclusion"]["subject_id"], c["conclusion"]["object_id"])

    def test_excludes_already_active_closure_fact(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "C"),
                   _t(9, "A", "depends_on_closure", "C")]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=3, max_iterations=10, max_candidates=500)
        self.assertEqual(cands, [])

    def test_inverse_rule_emits_inverse_predicate(self):
        rules = [{"id": "inverse:depends_on:dependency_of", "family": "inverse",
                  "predicate": "depends_on", "inverse_predicate": "dependency_of", "enabled": True}]
        triples = [_t(1, "A", "depends_on", "B")]
        cands = dream_lib.deductive_closure(triples, rules, max_depth=1,
                                            max_iterations=10, max_candidates=500)
        self.assertEqual(len(cands), 1)
        c = cands[0]["conclusion"]
        self.assertEqual((c["subject"], c["predicate"], c["object"]), ("B", "dependency_of", "A"))

    def test_symmetric_rule_emits_swapped_pair(self):
        rules = [{"id": "symmetric:collaborates_with", "family": "symmetric",
                  "predicate": "collaborates_with", "enabled": True}]
        triples = [_t(1, "A", "collaborates_with", "B")]
        cands = dream_lib.deductive_closure(triples, rules, max_depth=1,
                                            max_iterations=10, max_candidates=500)
        self.assertEqual(len(cands), 1)
        c = cands[0]["conclusion"]
        self.assertEqual((c["subject"], c["object"]), ("B", "A"))

    def test_disabled_and_empty_config_yield_nothing(self):
        triples = [_t(1, "A", "depends_on", "B"), _t(2, "B", "depends_on", "C")]
        self.assertEqual(dream_lib.deductive_closure(triples, [], max_depth=3,
                          max_iterations=10, max_candidates=500), [])

    def test_disjoint_temporal_premises_produce_no_candidate(self):
        triples = [_t(1, "A", "depends_on", "B", vf="2026-01-01", vt="2026-02-01"),
                   _t(2, "B", "depends_on", "C", vf="2026-03-01", vt=None)]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES, max_depth=3,
                                            max_iterations=10, max_candidates=500)
        self.assertEqual(cands, [])

    def test_confidence_is_min_of_premises(self):
        triples = [_t(1, "A", "depends_on", "B", conf=0.9), _t(2, "B", "depends_on", "C", conf=0.6)]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES, max_depth=3,
                                            max_iterations=10, max_candidates=500)
        self.assertAlmostEqual(cands[0]["evidence"]["confidence"], 0.6)

    def test_candidate_cap_truncates_and_marks_all(self):
        # 8-node path A..H -> 21 non-adjacent closure pairs; cap far below that
        nodes = [chr(65 + i) for i in range(8)]  # A..H
        triples = [_t(i + 1, nodes[i], "depends_on", nodes[i + 1]) for i in range(7)]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES, max_depth=8,
                                            max_iterations=20, max_candidates=3)
        self.assertEqual(len(cands), 3)
        self.assertTrue(all(c.get("truncated") for c in cands))

    def test_disjoint_short_path_does_not_block_valid_longer_path(self):
        # A->B disjoint with B->D (depth-2 path has empty interval);
        # A->X->Y->D is all open (valid depth-3 path).
        # The closure MUST emit A depends_on_closure D via the valid path.
        triples = [
            _t(1, "A", "depends_on", "B", vf="2026-01-01", vt="2026-02-01"),   # ends Feb
            _t(2, "B", "depends_on", "D", vf="2026-03-01", vt=None),            # starts Mar → disjoint
            _t(3, "A", "depends_on", "X", vf=None, vt=None),
            _t(4, "X", "depends_on", "Y", vf=None, vt=None),
            _t(5, "Y", "depends_on", "D", vf=None, vt=None),
        ]
        cands = dream_lib.deductive_closure(triples, TRANS_RULES,
                                            max_depth=5, max_iterations=10, max_candidates=500)
        pairs = {(c["conclusion"]["subject"], c["conclusion"]["object"]) for c in cands}
        self.assertIn(("A", "D"), pairs)

    def test_null_confidence_treated_as_1_not_crash(self):
        # confidence=None can come from sqlite REAL DEFAULT 1.0 (nullable); must not crash
        triples = [_t(1, "A", "depends_on", "B", conf=None),
                   _t(2, "B", "depends_on", "C", conf=0.8)]
        triples[0]["confidence"] = None  # ensure key present with None value
        cands = dream_lib.deductive_closure(triples, TRANS_RULES, max_depth=3,
                                            max_iterations=10, max_candidates=500)
        self.assertEqual(len(cands), 1)
        self.assertAlmostEqual(cands[0]["evidence"]["confidence"], 0.8)


# ---------------------------------------------------------------------------
# Task 5: build_contemplate_worklist + skip-marker filtering
# ---------------------------------------------------------------------------

class ContemplateWorklistTests(unittest.TestCase):
    def test_worklist_shape_and_version(self):
        cands = [{"kind": "derive", "candidate_id": "derive:x", "conclusion": {}, "decision": None}]
        wl = dream_lib.build_contemplate_worklist(cands, scope={"palace": "/p"},
                  params={"max_depth": 3}, rules=[], onto_version="onto:v")
        self.assertEqual(wl["task"], "contemplate")
        self.assertEqual(wl["version"], dream_lib.WORKLIST_VERSION)
        self.assertEqual(wl["ontology_version"], "onto:v")
        self.assertEqual(len(wl["items"]), 1)

    def test_filter_skipped_candidates_removes_by_id(self):
        cands = [{"candidate_id": "derive:a"}, {"candidate_id": "derive:b"}]
        skips = [{"candidate_id": "derive:a", "ontology_version": "onto:v"}]
        got = dream_lib.filter_skipped(cands, skips, "onto:v")
        self.assertEqual([c["candidate_id"] for c in got], ["derive:b"])

    def test_filter_skipped_ignores_markers_from_other_ontology_version(self):
        cands = [{"candidate_id": "derive:a"}]
        skips = [{"candidate_id": "derive:a", "ontology_version": "onto:OLD"}]
        got = dream_lib.filter_skipped(cands, skips, "onto:v")
        self.assertEqual([c["candidate_id"] for c in got], ["derive:a"])


# ---------------------------------------------------------------------------
# Task 6: apply_derive_decisions (pure)
# ---------------------------------------------------------------------------

class FakeDeriveWriter:
    def __init__(self): self.added = []
    def add_derived(self, conclusion, rule_id, premise_ids, premise_drawer_ids,
                    onto_version, confidence, valid_from, valid_to):
        self.added.append((conclusion["subject_id"], conclusion["predicate"],
                           conclusion["object_id"], rule_id, tuple(premise_ids), valid_from, valid_to))
        return {"ok": True}

class ApplyDeriveTests(unittest.TestCase):
    def test_materialize_calls_writer_and_counts(self):
        w = FakeDeriveWriter()
        decisions = [{"action": "materialize", "candidate_id": "derive:a",
            "conclusion": {"subject_id": 1, "predicate": "depends_on_closure", "object_id": 3},
            "rule": {"id": "transitive:depends_on"},
            "proof": {"premise_ids": [1, 2], "premise_drawer_ids": ["d1", "d2"]},
            "evidence": {"confidence": 0.7, "valid_from": "2026-01-01", "valid_to": None},
            "ontology_version": "onto:v"}]
        report, skips = dream_lib.apply_derive_decisions(decisions, w)
        self.assertEqual(report["materialized"], 1)
        self.assertEqual(len(w.added), 1)
        self.assertEqual(skips, [])

    def test_materialize_propagates_valid_to(self):
        w = FakeDeriveWriter()
        decisions = [{"action": "materialize", "candidate_id": "derive:a",
            "conclusion": {"subject_id": 1, "predicate": "p", "object_id": 3},
            "rule": {"id": "r"}, "proof": {"premise_ids": [1], "premise_drawer_ids": ["d1"]},
            "evidence": {"confidence": 1.0, "valid_from": "2026-01-01", "valid_to": "2026-05-01"},
            "ontology_version": "onto:v"}]
        dream_lib.apply_derive_decisions(decisions, w)
        self.assertEqual(w.added[0][-1], "2026-05-01")

    def test_skip_emits_marker_no_write(self):
        w = FakeDeriveWriter()
        decisions = [{"action": "skip", "candidate_id": "derive:a", "ontology_version": "onto:v",
                      "reason": "cheaply re-derivable"}]
        report, skips = dream_lib.apply_derive_decisions(decisions, w)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(w.added, [])
        self.assertEqual(skips, [{"candidate_id": "derive:a", "ontology_version": "onto:v",
                                  "reason": "cheaply re-derivable"}])

    def test_reject_rule_recorded_no_write(self):
        w = FakeDeriveWriter()
        decisions = [{"action": "reject_rule", "rule": {"id": "transitive:depends_on"},
                      "reason": "not transitive here"}]
        report, skips = dream_lib.apply_derive_decisions(decisions, w)
        self.assertEqual(report["rejected_rules"], ["transitive:depends_on"])
        self.assertEqual(w.added, [])

    def test_materialize_writer_error_recorded_soft(self):
        class Boom(FakeDeriveWriter):
            def add_derived(self, *a, **k): raise RuntimeError("db locked")
        decisions = [{"action": "materialize", "candidate_id": "derive:a",
            "conclusion": {"subject_id": 1, "predicate": "p", "object_id": 3},
            "rule": {"id": "r"}, "proof": {"premise_ids": [1], "premise_drawer_ids": ["d1"]},
            "evidence": {"confidence": 1.0, "valid_from": None, "valid_to": None},
            "ontology_version": "onto:v"}]
        report, skips = dream_lib.apply_derive_decisions(decisions, Boom())
        self.assertEqual(report["materialized"], 0)
        self.assertEqual(report["errors"][0]["stage"], "materialize")

    def test_unknown_action_skips_softly(self):
        report, skips = dream_lib.apply_derive_decisions([{"action": "frobnicate"}], FakeDeriveWriter())
        self.assertEqual(report["ignored"], 1)

    def test_skip_markers_for_rejected_rules_covers_all_matching_items(self):
        items = [
            {"candidate_id": "derive:a", "rule": {"id": "transitive:depends_on"}},
            {"candidate_id": "derive:b", "rule": {"id": "transitive:depends_on"}},
            {"candidate_id": "derive:c", "rule": {"id": "symmetric:x"}},
        ]
        markers = dream_lib.skip_markers_for_rejected_rules(
            items, ["transitive:depends_on"], "onto:v")
        self.assertEqual({m["candidate_id"] for m in markers}, {"derive:a", "derive:b"})
        self.assertTrue(all(m["ontology_version"] == "onto:v" for m in markers))
        self.assertTrue(all(m.get("reason") == "reject_rule" for m in markers))
