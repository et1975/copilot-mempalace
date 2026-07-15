"""Unit tests for Track B phase B3 S4a pure acquire helpers.

Run: MPY=...; cd skills/dreaming/scripts && "$MPY" -m unittest test_b3_acquire -v
"""
import unittest

import dream_acquire as acquire
import dream_lib


TRANSITIVE_RULES = [{
    "id": "transitive:depends_on",
    "family": "transitive",
    "predicate": "depends_on",
    "inverse_predicate": None,
    "enabled": True,
    "max_depth": 8,
}]


def _premise(tid, subject_id, predicate, object_id, **extra):
    row = {
        "triple_id": tid,
        "subject_id": subject_id,
        "predicate": predicate,
        "object_id": object_id,
        "subject": subject_id.upper(),
        "object": object_id.upper(),
        "valid_from": None,
        "valid_to": None,
        "confidence": 1.0,
        "source_drawer_id": f"drawer:{tid}",
        "epistemic_status": "asserted",
    }
    row.update(extra)
    return row


def _closure_candidates(premises):
    return dream_lib.deductive_closure(
        premises,
        TRANSITIVE_RULES,
        max_depth=8,
        max_iterations=50,
        max_candidates=500,
    )


def _query(subject_id, predicate, object_id):
    return {
        "subject_id": subject_id,
        "base_predicate": predicate,
        "object_id": object_id,
    }


def _answer(subject_id, predicate, object_id, value, epistemic_status, **query_extra):
    query = {
        "subject_id": subject_id,
        "predicate": predicate,
        "object_id": object_id,
    }
    query.update(query_extra)
    return {
        "kind": "boolean",
        "query": query,
        "value": value,
        "epistemic_status": epistemic_status,
        "support": [],
        "conditional_on": [],
    }


def _gap(gap_id, subject_id, predicate, object_id, duc, unblocks=None):
    return {
        "kind": "gap",
        "gap_id": gap_id,
        "hypothesis": {
            "subject_id": subject_id,
            "predicate": predicate,
            "object_id": object_id,
            "subject": subject_id.upper(),
            "object": object_id.upper(),
        },
        "rule": {
            "id": "transitive:depends_on",
            "family": "transitive",
            "predicate": dream_lib.normalize_predicate(predicate),
            "derived_predicate": "depends_on_closure",
        },
        "evidence": {
            "duc": duc,
            "unblocks": list(unblocks or []),
        },
        "decision": None,
    }


class ClosurePredicateTests(unittest.TestCase):
    def test_closure_predicate_for_enabled_transitive_rule(self):
        self.assertEqual(
            acquire.closure_predicate_for("depends_on", TRANSITIVE_RULES),
            "depends_on_closure",
        )

    def test_closure_predicate_for_missing_rule_is_none(self):
        self.assertIsNone(acquire.closure_predicate_for("blocks", TRANSITIVE_RULES))
        disabled = [dict(TRANSITIVE_RULES[0], enabled=False)]
        self.assertIsNone(acquire.closure_predicate_for("depends_on", disabled))


class BooleanReachabilityAnswerTests(unittest.TestCase):
    def test_deduced_reachability_from_generated_closure_candidate(self):
        premises = [
            _premise("p:a-b", "a", "depends_on", "b"),
            _premise("p:b-c", "b", "depends_on", "c"),
        ]
        candidates = _closure_candidates(premises)

        answer = acquire.extract_boolean_reachability_answer(
            _query("a", "depends_on", "c"),
            premises,
            candidates,
            TRANSITIVE_RULES,
        )

        self.assertEqual(answer["kind"], "boolean")
        self.assertEqual(answer["query"]["subject_id"], "a")
        self.assertEqual(answer["query"]["predicate"], "depends_on")
        self.assertEqual(answer["query"]["object_id"], "c")
        self.assertIs(answer["value"], True)
        self.assertEqual(answer["epistemic_status"], "deduced")
        self.assertIn("support", answer)
        self.assertEqual(answer["conditional_on"], [])

    def test_unsupported_reachability_when_no_premise_or_candidate_matches(self):
        premises = [
            _premise("p:a-b", "a", "depends_on", "b"),
            _premise("p:b-c", "b", "depends_on", "c"),
        ]
        candidates = _closure_candidates(premises)

        answer = acquire.extract_boolean_reachability_answer(
            _query("a", "depends_on", "z"),
            premises,
            candidates,
            TRANSITIVE_RULES,
        )

        self.assertIs(answer["value"], False)
        self.assertEqual(answer["epistemic_status"], "unsupported")
        self.assertEqual(answer["conditional_on"], [])

    def test_direct_durable_base_edge_is_deduced_without_candidate(self):
        premises = [_premise("p:a-c", "a", "depends_on", "c")]

        answer = acquire.extract_boolean_reachability_answer(
            _query("a", "depends_on", "c"),
            premises,
            _closure_candidates(premises),
            TRANSITIVE_RULES,
        )

        self.assertIs(answer["value"], True)
        self.assertEqual(answer["epistemic_status"], "deduced")
        self.assertIn("support", answer)

    def test_entailed_given_candidate_status_and_condition_are_reported(self):
        candidate = {
            "kind": "derive",
            "candidate_id": "derive:entailed-a-c",
            "conclusion": {
                "subject_id": "a",
                "predicate": "depends_on_closure",
                "object_id": "c",
                "subject": "A",
                "object": "C",
            },
            "rule": {
                "id": "transitive:depends_on",
                "family": "transitive",
                "predicate": "depends_on",
            },
            "proof": {
                "depth": 2,
                "premise_ids": ["p:a-b", "prov:x"],
                "premise_drawer_ids": ["drawer:p:a-b", "drawer:prov:x"],
                "entailed_given": ["prov:x"],
            },
            "evidence": {
                "already_active": False,
                "confidence": 1.0,
                "valid_from": None,
                "valid_to": None,
                "epistemic_status": "entailed_given",
                "inherited_status": "abduced",
            },
            "decision": None,
        }

        answer = acquire.extract_boolean_reachability_answer(
            _query("a", "depends_on", "c"),
            [],
            [candidate],
            TRANSITIVE_RULES,
        )

        self.assertIs(answer["value"], True)
        self.assertEqual(answer["epistemic_status"], "entailed_given")
        self.assertEqual(answer["conditional_on"], ["prov:x"])


class AnswerEquivalenceTests(unittest.TestCase):
    def test_same_value_tier_and_normalized_query_are_equivalent(self):
        a = _answer("a", "Depends-On", "c", True, "deduced", subject="A")
        b = _answer("a", "depends_on", "c", True, "deduced", subject="Renamed A")

        self.assertTrue(acquire.answers_equivalent(a, b))
        self.assertFalse(acquire.answer_changed(a, b))

    def test_unsupported_to_entailed_given_is_a_change(self):
        unsupported = _answer("a", "depends_on", "c", False, "unsupported")
        entailed = _answer("a", "depends_on", "c", True, "entailed_given")

        self.assertFalse(acquire.answers_equivalent(unsupported, entailed))
        self.assertTrue(acquire.answer_changed(unsupported, entailed))

    def test_deduced_and_entailed_given_are_distinct_tiers(self):
        deduced = _answer("a", "depends_on", "c", True, "deduced")
        entailed = _answer("a", "depends_on", "c", True, "entailed_given")

        self.assertFalse(acquire.answers_equivalent(deduced, entailed))
        self.assertTrue(acquire.answer_changed(deduced, entailed))


class GapRankingTests(unittest.TestCase):
    def test_rank_gaps_sorts_by_duc_desc_without_mutating_input(self):
        gaps = [
            _gap("gap:one", "a", "depends_on", "b", 1),
            _gap("gap:five", "c", "depends_on", "d", 5),
            _gap("gap:three", "e", "depends_on", "f", 3),
        ]

        ranked = acquire.rank_gaps(gaps)

        self.assertEqual([g["evidence"]["duc"] for g in ranked], [5, 3, 1])
        self.assertEqual([g["gap_id"] for g in gaps], ["gap:one", "gap:five", "gap:three"])

    def test_rank_gaps_preserves_input_order_for_equal_duc(self):
        gaps = [
            _gap("gap:first", "z", "depends_on", "z2", 5),
            _gap("gap:second", "a", "depends_on", "a2", 5),
        ]

        ranked_once = acquire.rank_gaps(gaps)
        ranked_twice = acquire.rank_gaps(gaps)

        self.assertEqual([g["gap_id"] for g in ranked_once], ["gap:first", "gap:second"])
        self.assertEqual([g["gap_id"] for g in ranked_once], [g["gap_id"] for g in ranked_twice])

    def test_gap_hypothesis_key_normalizes_predicate(self):
        gap = _gap("gap:key", "a", "Depends On", "b", 1)

        self.assertEqual(
            acquire.gap_hypothesis_key(gap),
            ("a", "depends_on", "b"),
        )


class SelectGapTests(unittest.TestCase):
    def test_select_gap_skips_acquired_and_attempted_keys(self):
        acquired = _gap("gap:acquired", "a", "depends_on", "b", 5)
        attempted = _gap("gap:attempted", "b", "depends_on", "c", 4)
        remaining = _gap("gap:remaining", "c", "depends_on", "d", 3)

        selected = acquire.select_gap(
            [acquired, attempted, remaining],
            acquired_keys={acquire.gap_hypothesis_key(acquired)},
            attempted_keys={acquire.gap_hypothesis_key(attempted)},
        )

        self.assertEqual(selected["gap_id"], "gap:remaining")

    def test_select_gap_returns_none_when_all_gaps_are_skipped(self):
        gap = _gap("gap:done", "a", "depends_on", "b", 5)

        selected = acquire.select_gap(
            [gap],
            acquired_keys={acquire.gap_hypothesis_key(gap)},
        )

        self.assertIsNone(selected)

    def test_select_gap_prefers_gap_unblocking_query_over_higher_duc_gap(self):
        query_target = {
            "subject_id": "a",
            "predicate": "depends_on_closure",
            "object_id": "target",
        }
        high_duc = _gap(
            "gap:high",
            "x",
            "depends_on",
            "y",
            10,
            unblocks=[{
                "subject": "Q",
                "subject_id": "q",
                "predicate": "depends_on_closure",
                "object": "R",
                "object_id": "r",
            }],
        )
        preferred = _gap(
            "gap:preferred",
            "b",
            "depends_on",
            "target",
            1,
            unblocks=[dict(query_target, subject="A", object="TARGET")],
        )

        selected = acquire.select_gap([high_duc, preferred], query=query_target)

        self.assertEqual(selected["gap_id"], "gap:preferred")

    def test_select_gap_returns_top_ranked_gap_when_no_gap_unblocks_query(self):
        high_duc = _gap("gap:high", "x", "depends_on", "y", 10)
        low_duc = _gap("gap:low", "a", "depends_on", "b", 1)
        query_target = {
            "subject_id": "missing",
            "predicate": "depends_on_closure",
            "object_id": "target",
        }

        selected = acquire.select_gap([low_duc, high_duc], query=query_target)

        self.assertEqual(selected["gap_id"], "gap:high")


if __name__ == "__main__":
    unittest.main()
