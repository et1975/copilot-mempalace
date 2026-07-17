"""Tests for the drawer-only insight-synthesis gates (anti-slop core)."""
from __future__ import annotations

import unittest
from unittest import mock

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


class SurveyRankerTests(unittest.TestCase):
    def _drawers(self):
        return [
            {"id": "a", "text": "verification drift lesson", "embedding": [1.0, 0.0, 0.0], "wing": "W1", "room": "r"},
            {"id": "b", "text": "branch hygiene lesson", "embedding": [0.7, 0.5, 0.0], "wing": "W1", "room": "r"},
            {"id": "c", "text": "pipeline contract lesson", "embedding": [0.6, 0.55, 0.1], "wing": "W2", "room": "r"},
            {"id": "z", "text": "totally unrelated", "embedding": [0.0, 0.0, 1.0], "wing": "W3", "room": "r"},
        ]

    def _reference_rank_survey_clusters(self, drawers, *, min_sim=0.25, max_sim=0.85, k=5, top_n=10):
        prepared = []
        for drawer in drawers or []:
            if not isinstance(drawer, dict):
                continue
            embedding = drawer.get("embedding") or []
            if not embedding:
                continue
            try:
                vector = [float(value) for value in embedding]
            except (TypeError, ValueError):
                continue
            if not vector:
                continue
            norm = sum(value * value for value in vector) ** 0.5
            if norm == 0.0:
                continue
            prepared.append(
                {
                    "id": str(drawer.get("id")),
                    "text": str(drawer.get("text") or ""),
                    "embedding": vector,
                    "norm": norm,
                    "wing": drawer.get("wing"),
                    "room": drawer.get("room"),
                }
            )

        clusters = []
        neighbor_limit = max(0, int(k))
        for anchor in prepared:
            neighbors = []
            for candidate in prepared:
                if candidate["id"] == anchor["id"]:
                    continue
                if len(anchor["embedding"]) != len(candidate["embedding"]):
                    continue
                sim = sum(
                    a * b
                    for a, b in zip(anchor["embedding"], candidate["embedding"])
                ) / (anchor["norm"] * candidate["norm"])
                if float(min_sim) <= sim <= float(max_sim):
                    neighbors.append(
                        {
                            "id": candidate["id"],
                            "text": candidate["text"],
                            "wing": candidate["wing"],
                            "sim": sim,
                        }
                    )
            neighbors.sort(key=lambda item: item["sim"], reverse=True)
            neighbors = neighbors[:neighbor_limit]
            if not neighbors:
                continue

            sims = [float(item["sim"]) for item in neighbors]
            wings = sorted(
                {
                    wing
                    for wing in [anchor.get("wing")] + [item.get("wing") for item in neighbors]
                    if wing
                }
            )
            distinct_wings = len(wings)
            neighbor_count = len(neighbors)
            mean_sim = sum(sims) / len(sims)
            score = 1.0 * (distinct_wings - 1) + 0.5 * min(neighbor_count, 3) + mean_sim
            clusters.append(
                {
                    "anchor_id": anchor["id"],
                    "anchor_wing": anchor.get("wing"),
                    "anchor_snippet": dream_insight._snippet(anchor.get("text"), limit=120),
                    "wings": wings,
                    "cross_wing": distinct_wings >= 2,
                    "neighbor_ids": [item["id"] for item in neighbors],
                    "neighbor_snippets": [
                        {"id": item["id"], "snippet": dream_insight._snippet(item.get("text"), limit=120), "sim": item["sim"]}
                        for item in neighbors
                    ],
                    "neighbor_count": neighbor_count,
                    "score": score,
                }
            )

        clusters.sort(key=lambda item: (-float(item["score"]), str(item["anchor_id"])))
        return clusters[: max(0, int(top_n))]

    def _assert_clusters_close(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for actual_cluster, expected_cluster in zip(actual, expected):
            for key in ("score",):
                self.assertAlmostEqual(actual_cluster[key], expected_cluster[key], places=6)
            comparable_actual = {k: v for k, v in actual_cluster.items() if k != "score"}
            comparable_expected = {k: v for k, v in expected_cluster.items() if k != "score"}
            self.assertEqual(len(comparable_actual["neighbor_snippets"]), len(comparable_expected["neighbor_snippets"]))
            for actual_neighbor, expected_neighbor in zip(comparable_actual["neighbor_snippets"], comparable_expected["neighbor_snippets"]):
                self.assertEqual(
                    {k: v for k, v in actual_neighbor.items() if k != "sim"},
                    {k: v for k, v in expected_neighbor.items() if k != "sim"},
                )
                self.assertAlmostEqual(actual_neighbor["sim"], expected_neighbor["sim"], places=6)
            comparable_actual["neighbor_snippets"] = [
                {k: v for k, v in item.items() if k != "sim"}
                for item in comparable_actual["neighbor_snippets"]
            ]
            comparable_expected["neighbor_snippets"] = [
                {k: v for k, v in item.items() if k != "sim"}
                for item in comparable_expected["neighbor_snippets"]
            ]
            self.assertEqual(comparable_actual, comparable_expected)

    def test_empty_input_returns_no_clusters(self):
        self.assertEqual(dream_insight.rank_survey_clusters([], min_sim=0.25, max_sim=0.85), [])
        self.assertEqual(dream_insight.rank_survey_clusters(None, min_sim=0.25, max_sim=0.85), [])

    def test_returns_clusters_with_neighbors_in_band(self):
        clusters = dream_insight.rank_survey_clusters(self._drawers(), min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        self.assertTrue(clusters)
        top = clusters[0]
        self.assertIn("anchor_id", top)
        self.assertIn("neighbor_ids", top)
        self.assertGreaterEqual(top["neighbor_count"], 1)
        for cluster in clusters:
            self.assertNotIn("z", cluster["neighbor_ids"])

    def test_cross_wing_flag_and_wings(self):
        clusters = dream_insight.rank_survey_clusters(self._drawers(), min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        by_anchor = {c["anchor_id"]: c for c in clusters}
        self.assertIn("a", by_anchor)
        self.assertTrue(by_anchor["a"]["cross_wing"])
        self.assertEqual(by_anchor["a"]["wings"], ["W1", "W2"])

    def test_near_duplicates_excluded_by_max_sim(self):
        drawers = [
            {"id": "a", "text": "x", "embedding": [1.0, 0.0], "wing": "W1", "room": "r"},
            {"id": "a_dup", "text": "x", "embedding": [1.0, 0.0], "wing": "W1", "room": "r"},
        ]
        clusters = dream_insight.rank_survey_clusters(drawers, min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        self.assertEqual(clusters, [])

    def test_ranked_by_score_desc(self):
        clusters = dream_insight.rank_survey_clusters(self._drawers(), min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        scores = [c["score"] for c in clusters]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_n_truncation(self):
        clusters = dream_insight.rank_survey_clusters(self._drawers(), min_sim=0.25, max_sim=0.85, k=5, top_n=1)
        self.assertEqual(len(clusters), 1)

    def test_top_k_truncates_neighbors_after_similarity_sort(self):
        clusters = dream_insight.rank_survey_clusters(self._drawers(), min_sim=0.25, max_sim=0.85, k=1, top_n=10)
        self.assertTrue(clusters)
        for cluster in clusters:
            self.assertEqual(cluster["neighbor_count"], 1)
            self.assertEqual(len(cluster["neighbor_ids"]), 1)

    def test_missing_embedding_skipped_no_raise(self):
        drawers = [
            {"id": "a", "text": "x", "embedding": [1.0, 0.0], "wing": "W1"},
            {"id": "b", "text": "y", "embedding": [0.6, 0.6], "wing": "W2"},
            {"id": "noemb", "text": "z"},
        ]
        clusters = dream_insight.rank_survey_clusters(drawers, min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        for cluster in clusters:
            self.assertNotIn("noemb", cluster["neighbor_ids"])
            self.assertNotEqual(cluster["anchor_id"], "noemb")

    def test_input_not_mutated(self):
        drawers = self._drawers()
        snapshot = [dict(d) for d in drawers]
        dream_insight.rank_survey_clusters(drawers, min_sim=0.25, max_sim=0.85, k=5, top_n=10)
        self.assertEqual(drawers, snapshot)

    def test_matches_reference_pairwise_ranking(self):
        drawers = [
            "not a drawer",
            *self._drawers(),
            {"id": "bad", "text": "bad vector", "embedding": ["na"], "wing": "W4", "room": "r"},
            {"id": "empty", "text": "empty vector", "embedding": [], "wing": "W4", "room": "r"},
            {"id": "zero", "text": "empty norm", "embedding": [0.0, 0.0, 0.0], "wing": "W4", "room": "r"},
            {"id": "short_a", "text": "short vector a", "embedding": [1.0, 0.0], "wing": "W5", "room": "r"},
            {"id": "short_b", "text": "short vector b", "embedding": [0.5, 0.5], "wing": "W6", "room": "r"},
        ]
        actual = dream_insight.rank_survey_clusters(drawers, min_sim=0.25, max_sim=0.85, k=2, top_n=10)
        expected = self._reference_rank_survey_clusters(drawers, min_sim=0.25, max_sim=0.85, k=2, top_n=10)
        self._assert_clusters_close(actual, expected)

    def test_top_n_tie_breaks_by_anchor_id(self):
        drawers = [
            {"id": "c", "text": "first pair c", "embedding": [0.0, 0.0, 1.0, 0.0], "wing": "W1"},
            {"id": "d", "text": "first pair d", "embedding": [0.0, 0.0, 0.5, 0.8660254037844386], "wing": "W2"},
            {"id": "b", "text": "second pair b", "embedding": [0.5, 0.8660254037844386, 0.0, 0.0], "wing": "W2"},
            {"id": "a", "text": "second pair a", "embedding": [1.0, 0.0, 0.0, 0.0], "wing": "W1"},
        ]
        clusters = dream_insight.rank_survey_clusters(drawers, min_sim=0.49, max_sim=0.51, k=1, top_n=2)
        self.assertEqual([cluster["anchor_id"] for cluster in clusters], ["a", "b"])

    def test_survey_insight_clusters_caps_unscoped_drawers_but_not_scoped(self):
        drawers = [
            {"id": "a", "text": "a", "embedding": [1.0, 0.0], "wing": "W1", "room": "r"},
            {"id": "b", "text": "b", "embedding": [0.5, 0.5], "wing": "W2", "room": "r"},
            {"id": "c", "text": "c", "embedding": [0.0, 1.0], "wing": "W3", "room": "r"},
            {"id": "d", "text": "d", "embedding": [0.5, -0.5], "wing": "W4", "room": "r"},
        ]
        with mock.patch.object(dream_insight, "load_logical_drawers", return_value=drawers):
            unscoped = dream_insight.survey_insight_clusters(
                "palace", min_sim=0.0, max_sim=1.0, k=5, top_n=10, max_drawers=2
            )
            scoped = dream_insight.survey_insight_clusters(
                "palace", wing="W1", min_sim=0.0, max_sim=1.0, k=5, top_n=10, max_drawers=2
            )

        self.assertEqual(unscoped["total_drawers"], 4)
        self.assertEqual(unscoped["ranked_drawers"], 2)
        self.assertTrue(unscoped["truncated"])
        self.assertEqual(unscoped["max_drawers"], 2)
        self.assertIn("truncated", unscoped["note"])
        self.assertEqual({cluster["anchor_id"] for cluster in unscoped["clusters"]}, {"a", "b"})

        self.assertEqual(scoped["total_drawers"], 4)
        self.assertEqual(scoped["ranked_drawers"], 4)
        self.assertFalse(scoped["truncated"])
        self.assertEqual(scoped["max_drawers"], 2)


class InsightFlowPersistenceTests(unittest.TestCase):
    def test_resume_persists_nearest_existing_from_duplicate_scan_when_not_duplicate(self):
        session = {
            "run_id": "r1",
            "status": "awaiting_synthesis",
            "candidates": {
                "anchor": {"id": "d_anchor", "text": ANCHOR_TEXT},
                "neighbors": [{"id": "d_neighbor", "text": NEIGHBOR_TEXT}],
            },
            "member_ids": ["d_anchor", "d_neighbor"],
        }
        nearest = {"id": "insight-1", "text": "prior insight", "sim": 0.72}
        persisted = {}

        def capture_persist(_kg_path, updated_session, *, now=None):
            persisted.update(updated_session)

        with (
            mock.patch.object(dream_insight, "_insight_db_path", return_value="unused.sqlite"),
            mock.patch.object(dream_insight, "ensure_firewall_schema"),
            mock.patch.object(dream_insight, "_ensure_insight_schema"),
            mock.patch.object(dream_insight, "_require_session", return_value=session),
            mock.patch.object(dream_insight, "check_insight_duplicate", return_value={"duplicate": False, "nearest_insight": nearest}),
            mock.patch.object(dream_insight, "_persist_session", side_effect=capture_persist),
        ):
            result = dream_insight.insight_resume("palace", "r1", candidate=_candidate())

        self.assertEqual(result["status"], "awaiting_critic")
        self.assertEqual(persisted["nearest_existing"], nearest)
        self.assertTrue(persisted["nearest_existing_checked"])

    def test_critique_reuses_persisted_nearest_existing_without_rescan(self):
        nearest = {"id": "insight-1", "text": "prior insight", "sim": 0.72}
        session = {
            "run_id": "r1",
            "status": "awaiting_critic",
            "candidate": _candidate(),
            "nearest_existing": nearest,
            "nearest_existing_checked": True,
        }

        with (
            mock.patch.object(dream_insight, "_insight_db_path", return_value="unused.sqlite"),
            mock.patch.object(dream_insight, "ensure_firewall_schema"),
            mock.patch.object(dream_insight, "_ensure_insight_schema"),
            mock.patch.object(dream_insight, "_require_session", return_value=session),
            mock.patch.object(dream_insight, "_persist_session"),
            mock.patch.object(dream_insight, "_nearest_existing_note", side_effect=AssertionError("unexpected full-palace rescan")),
        ):
            result = dream_insight.insight_critique("palace", "r1", verdict="supported")

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["nearest_existing"], nearest)


if __name__ == "__main__":
    unittest.main()
