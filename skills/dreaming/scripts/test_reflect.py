import unittest
import dream_reflect
from dream_reflect import (
    REFLECT_KINDS, validate_reflect, nearest_drawer_distance, is_novel,
    admit_structural,
)

MEMBERS = {"d1": "alpha depends on beta for tls", "d2": "beta wraps gamma crypto"}

def _cand(kind="generalize", premises=None):
    return {
        "conclusion": {"text": "alpha transitively relies on gamma crypto",
                       "kind": kind,
                       "decision_or_prediction": "treat gamma as a supply-chain dep"},
        "premises": premises if premises is not None else [
            {"drawer_id": "d1", "quote": "depends on beta"},
            {"drawer_id": "d2", "quote": "wraps gamma crypto"},
        ],
    }

class ValidateReflectTests(unittest.TestCase):
    def test_wellformed_generalize_passes(self):
        self.assertEqual(validate_reflect(_cand(), MEMBERS), {"ok": True, "rejects": []})

    def test_fabricated_quote_ungrounded(self):
        bad = _cand(premises=[{"drawer_id": "d1", "quote": "not present"},
                              {"drawer_id": "d2", "quote": "wraps gamma crypto"}])
        out = validate_reflect(bad, MEMBERS)
        self.assertFalse(out["ok"]); self.assertIn("ungrounded", out["rejects"])

    def test_single_drawer_rejected(self):
        bad = _cand(premises=[{"drawer_id": "d1", "quote": "depends on beta"}])
        out = validate_reflect(bad, MEMBERS)
        self.assertFalse(out["ok"]); self.assertIn("not_cross_drawer", out["rejects"])

    def test_unknown_kind_rejected(self):
        out = validate_reflect(_cand(kind="banana"), MEMBERS)
        self.assertFalse(out["ok"]); self.assertIn("bad_kind", out["rejects"])

    def test_name_gap_is_valid_kind(self):
        out = validate_reflect(_cand(kind="name_gap"), MEMBERS)
        self.assertTrue(out["ok"])

    def test_malformed_does_not_raise(self):
        self.assertFalse(validate_reflect("x", MEMBERS)["ok"])

class NoveltyTests(unittest.TestCase):
    def test_distance_one_when_no_existing(self):
        self.assertEqual(nearest_drawer_distance([1.0, 0.0], []), 1.0)

    def test_identical_is_not_novel(self):
        self.assertFalse(is_novel([1.0, 0.0], [[1.0, 0.0]], margin=0.15))

    def test_orthogonal_is_novel(self):
        self.assertTrue(is_novel([1.0, 0.0], [[0.0, 1.0]], margin=0.15))

    def test_empty_cand_vec_is_not_novel(self):
        self.assertFalse(is_novel([], [[1.0, 0.0]], margin=0.15))
        self.assertFalse(is_novel(None, [[1.0, 0.0]], margin=0.15))

    def test_empty_cand_vec_distance_is_zero(self):
        self.assertEqual(nearest_drawer_distance([], [[1.0, 0.0]]), 0.0)

    def test_mismatched_dim_existing_vectors_are_skipped(self):
        # cand is 1-D, only existing vec is 2-D -> no comparable vector -> distance 1.0
        self.assertEqual(nearest_drawer_distance([1.0], [[1.0, 0.0]]), 1.0)
        self.assertTrue(is_novel([1.0], [[1.0, 0.0]], margin=0.15))

class AdmitStructuralTests(unittest.TestCase):
    def test_filters_low_coverage_and_caps(self):
        cands = [
            {"id": "a", "coverage": 3, "score": 0.9},
            {"id": "b", "coverage": 1, "score": 0.95},   # dropped: coverage < 2
            {"id": "c", "coverage": 2, "score": 0.5},
        ]
        out = admit_structural(cands, min_coverage=2, top_k=1)
        self.assertEqual([c["id"] for c in out], ["a"])  # coverage>=2, top score

    def test_stable_order_by_score_desc(self):
        cands = [{"id": "a", "coverage": 2, "score": 0.2},
                 {"id": "b", "coverage": 2, "score": 0.8}]
        self.assertEqual([c["id"] for c in admit_structural(cands, top_k=5)], ["b", "a"])

class GatherReflectSeedsTests(unittest.TestCase):
    def test_builds_cluster_seeds_with_full_member_text(self):
        fake_drawers = [
            {"id": "d1", "text": "alpha depends on beta", "embedding": [1.0, 0.0, 0.0],
             "wing": "w", "room": "r"},
            {"id": "d2", "text": "beta wraps gamma", "embedding": [0.6, 0.4, 0.0],
             "wing": "w", "room": "r"},
            {"id": "d3", "text": "unrelated note", "embedding": [0.0, 0.0, 1.0],
             "wing": "w", "room": "r"},
        ]
        orig = dream_reflect.load_logical_drawers
        dream_reflect.load_logical_drawers = lambda p, wing=None, room=None: fake_drawers
        try:
            seeds = dream_reflect.gather_reflect_seeds("P", k=5, top_n=10)
        finally:
            dream_reflect.load_logical_drawers = orig
        self.assertTrue(seeds)
        seed = seeds[0]
        self.assertIn("anchor_id", seed)
        self.assertGreaterEqual(len(seed["member_ids"]), 2)          # anchor + >=1 neighbor
        self.assertIn(seed["anchor_id"], seed["member_ids"])
        # members must carry FULL drawer text (not just truncated snippets) so the
        # adjudicating agent can produce exact-substring quotes:
        self.assertEqual(len(seed["members"]), len(seed["member_ids"]))
        self.assertTrue(all(isinstance(m["text"], str) and m["text"] for m in seed["members"]))
        self.assertGreaterEqual(seed["coverage"], 2)

class ConvergeSeedsFromRecurrenceTests(unittest.TestCase):
    def test_converge_builds_seeds_with_support_and_reflect_kind(self):
        entries = [
            {
                "id": "entry-1",
                "text": "repeated observation across sessions",
                "embedding": [1.0, 0.0],
                "session_id": "session-a",
                "date": "2026-07-01",
                "topic": "test",
            },
            {
                "id": "entry-2",
                "text": "repeated observation across sessions again",
                "embedding": [1.0, 0.0],
                "session_id": "session-b",
                "date": "2026-07-02",
                "topic": "test",
            },
        ]
        seeds = dream_reflect.converge_seeds_from_recurrence(entries, tau=0.9, min_support=2)
        self.assertEqual(len(seeds), 1)
        seed = seeds[0]
        self.assertEqual(seed["reflect_kind"], "converge")
        self.assertEqual(seed["evidence"]["support"], 2)
        self.assertGreaterEqual(seed["coverage"], 2)
        self.assertEqual(len(seed["members"]), 2)
        self.assertTrue(all("text" in m and m["text"] for m in seed["members"]))
        self.assertTrue(all("session_id" in m for m in seed["members"]))
        self.assertEqual(seed["evidence"]["support_ids"], ["session-a", "session-b"])

if __name__ == "__main__":
    unittest.main()
