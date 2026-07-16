"""Tests for the drawer-only insight-synthesis gates (anti-slop core)."""
from __future__ import annotations

import unittest

import dream_insight


ANCHOR_TEXT = (
    "AVS investigation: never cite a statistic from a session summary without "
    "re-querying; a context compaction summary claimed a 22-tenant sweep that was "
    "never run and it got propagated into the PR."
)
NEIGHBOR_TEXT = (
    "Fleet-mode gotcha: a fix committed only on the integration branch can be "
    "MISSING from an individual PR branch; run per-issue invariant tests on the "
    "branch itself, not just the integration branch."
)

MEMBERS = {"d_anchor": ANCHOR_TEXT, "d_neighbor": NEIGHBOR_TEXT}


def _candidate(**overrides):
    base = {
        "conclusion": {
            "text": (
                "Both cases are one structure: a convenient aggregate view diverges "
                "from per-unit ground truth and is only caught by re-deriving each unit."
            ),
            "kind": "shared_constraint",
            "decision_or_prediction": (
                "Add a standing rule: re-derive any claim taken from an aggregate "
                "(summary, integration branch, subagent report) at the per-artifact source."
            ),
        },
        "premises": [
            {"drawer_id": "d_anchor", "quote": "a context compaction summary claimed a 22-tenant sweep that was never run"},
            {"drawer_id": "d_neighbor", "quote": "a fix committed only on the integration branch can be MISSING from an individual PR branch"},
        ],
    }
    base.update(overrides)
    return base


class ValidateInsightTests(unittest.TestCase):
    def test_wellformed_two_drawer_candidate_passes(self):
        result = dream_insight.validate_insight(_candidate(), MEMBERS)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["rejects"], [])

    def test_fabricated_quote_is_ungrounded(self):
        cand = _candidate(premises=[
            {"drawer_id": "d_anchor", "quote": "this exact sentence is not in the drawer at all"},
            {"drawer_id": "d_neighbor", "quote": "a fix committed only on the integration branch can be MISSING from an individual PR branch"},
        ])
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("ungrounded", result["rejects"])

    def test_unknown_drawer_id_is_ungrounded(self):
        cand = _candidate(premises=[
            {"drawer_id": "d_missing", "quote": "anything"},
            {"drawer_id": "d_neighbor", "quote": "run per-issue invariant tests on the branch itself"},
        ])
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("ungrounded", result["rejects"])

    def test_single_drawer_is_not_cross_drawer(self):
        cand = _candidate(premises=[
            {"drawer_id": "d_anchor", "quote": "a context compaction summary claimed a 22-tenant sweep that was never run"},
        ])
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("not_cross_drawer", result["rejects"])

    def test_two_premises_same_drawer_is_not_cross_drawer(self):
        cand = _candidate(premises=[
            {"drawer_id": "d_anchor", "quote": "never cite a statistic from a session summary without re-querying"},
            {"drawer_id": "d_anchor", "quote": "a context compaction summary claimed a 22-tenant sweep that was never run"},
        ])
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("not_cross_drawer", result["rejects"])

    def test_missing_decision_or_prediction_rejected(self):
        cand = _candidate()
        cand["conclusion"] = {**cand["conclusion"], "decision_or_prediction": "   "}
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("no_decision_or_prediction", result["rejects"])

    def test_restatement_of_single_member_rejected(self):
        cand = _candidate()
        # conclusion is literally a substring of the anchor drawer -> not novel
        cand["conclusion"] = {
            **cand["conclusion"],
            "text": "never cite a statistic from a session summary without re-querying",
        }
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("restatement", result["rejects"])

    def test_bad_kind_rejected(self):
        cand = _candidate()
        cand["conclusion"] = {**cand["conclusion"], "kind": "causal"}
        result = dream_insight.validate_insight(cand, MEMBERS)
        self.assertFalse(result["ok"])
        self.assertIn("bad_kind", result["rejects"])

    def test_malformed_candidate_does_not_raise(self):
        for bad in [None, {}, {"premises": []}, {"conclusion": {}}, {"conclusion": {"text": "x"}}]:
            result = dream_insight.validate_insight(bad, MEMBERS)
            self.assertFalse(result["ok"])
            self.assertTrue(result["rejects"])

    def test_nfc_normalized_quote_matches(self):
        # composed vs decomposed unicode should still match after NFC
        import unicodedata
        member = "café decision: verify at the source before acting"  # composed é
        decomposed_quote = unicodedata.normalize("NFD", "café decision")
        cand = _candidate(premises=[
            {"drawer_id": "d_cafe", "quote": decomposed_quote},
            {"drawer_id": "d_neighbor", "quote": "run per-issue invariant tests on the branch itself"},
        ])
        members = {"d_cafe": member, "d_neighbor": NEIGHBOR_TEXT}
        result = dream_insight.validate_insight(cand, members)
        self.assertNotIn("ungrounded", result["rejects"])

    def test_cosmetic_two_drawer_is_not_load_bearing(self):
        # both premise quotes are grounded in their own drawer, but a THIRD member
        # drawer happens to contain BOTH quotes -> the evidence collapses into one
        # drawer, so the multi-drawer citation is cosmetic.
        q1 = "a context compaction summary claimed a 22-tenant sweep that was never run"
        q2 = "a fix committed only on the integration branch can be MISSING from an individual PR branch"
        superset = f"combined notes: {q1} and separately {q2} -- both in one drawer"
        members = {"d_anchor": ANCHOR_TEXT, "d_neighbor": NEIGHBOR_TEXT, "d_super": superset}
        result = dream_insight.validate_insight(_candidate(), members)
        self.assertFalse(result["ok"])
        self.assertIn("not_load_bearing", result["rejects"])

    def test_genuine_two_drawer_is_load_bearing(self):
        # no single member contains both quotes -> load-bearing, passes
        result = dream_insight.validate_insight(_candidate(), MEMBERS)
        self.assertNotIn("not_load_bearing", result["rejects"])
        self.assertTrue(result["ok"])


class InsightDuplicateTests(unittest.TestCase):
    def test_duplicate_when_above_tau(self):
        result = dream_insight.insight_is_duplicate(
            [1.0, 0.0], [[0.99, 0.01], [0.0, 1.0]], tau_dup=0.9
        )
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["nearest_index"], 0)

    def test_not_duplicate_when_below_tau(self):
        result = dream_insight.insight_is_duplicate(
            [1.0, 0.0], [[0.2, 0.98], [0.0, 1.0]], tau_dup=0.9
        )
        self.assertFalse(result["duplicate"])

    def test_no_existing_insights_not_duplicate(self):
        result = dream_insight.insight_is_duplicate([1.0, 0.0], [], tau_dup=0.9)
        self.assertFalse(result["duplicate"])
        self.assertIsNone(result["nearest_index"])

    def test_skips_empty_vectors(self):
        result = dream_insight.insight_is_duplicate(
            [1.0, 0.0], [[], [0.95, 0.05]], tau_dup=0.9
        )
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["nearest_index"], 1)


if __name__ == "__main__":
    unittest.main()
