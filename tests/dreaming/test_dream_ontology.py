"""Unit tests for ontology rule-candidate generation and ontology.json IO."""
import json
import os
import tempfile
import unittest

import dream_ontology as onto


def _tempfile_root():
    root = os.path.expanduser("~/.copilot/session-state/dream-ontology-tests")
    os.makedirs(root, exist_ok=True)
    return root


class TestSuggestRulesFromPredicates(unittest.TestCase):
    def test_suggests_transitive_and_symmetric_name_heuristics_disabled(self):
        rules = onto.suggest_rules_from_predicates(["depends_on", "collaborates_with"])

        self.assertEqual([r["id"] for r in rules], ["symmetric:collaborates_with", "transitive:depends_on"])
        transitive = next(r for r in rules if r["family"] == "transitive")
        symmetric = next(r for r in rules if r["family"] == "symmetric")
        self.assertEqual(transitive["derived_predicate"], "depends_on_closure")
        self.assertFalse(transitive["enabled"])
        self.assertFalse(symmetric["enabled"])
        self.assertIn("REVIEW", transitive["rationale"])
        self.assertIn("REVIEW", symmetric["rationale"])

    def test_suggests_inverse_by_suffix_only_when_both_members_present(self):
        rules = onto.suggest_rules_from_predicates(["authored", "authored_by", "fixed_by"])

        self.assertEqual([r["id"] for r in rules], ["inverse:authored:authored_by"])
        self.assertEqual(rules[0]["predicate"], "authored")
        self.assertEqual(rules[0]["inverse_predicate"], "authored_by")
        self.assertFalse(rules[0]["enabled"])

    def test_suggests_inverse_of_pairs_once_in_canonical_order(self):
        rules = onto.suggest_rules_from_predicates(["child_of", "parent_of", "descendant_of", "ancestor_of"])

        self.assertEqual(
            [r["id"] for r in rules if r["family"] == "inverse"],
            ["inverse:ancestor_of:descendant_of", "inverse:parent_of:child_of"],
        )
        self.assertEqual(len({r["id"] for r in rules}), len(rules))

    def test_suggestions_are_deterministic_unique_and_sorted_by_id(self):
        predicates = ["collaborates", "depends_on", "authored_by", "authored", "depends_on"]

        first = onto.suggest_rules_from_predicates(predicates)
        second = onto.suggest_rules_from_predicates(list(reversed(predicates)))

        self.assertEqual(first, second)
        self.assertEqual([r["id"] for r in first], sorted(r["id"] for r in first))
        self.assertEqual(len({r["id"] for r in first}), len(first))


class TestInduceRulesFromTriples(unittest.TestCase):
    def test_induces_symmetric_candidate_at_support_threshold(self):
        triples = [
            {"subject": "Ada", "predicate": "collaborates_with", "object": "Grace"},
            {"subject": "Grace", "predicate": "collaborates_with", "object": "Ada"},
            {"subject": "Ada", "predicate": "collaborates_with", "object": "Linus"},
            {"subject": "Linus", "predicate": "collaborates_with", "object": "Ada"},
        ]

        rules = onto.induce_rules_from_triples(triples, min_support=2)

        rule = self._one_rule(rules, "symmetric:collaborates_with")
        self.assertEqual(rule["family"], "symmetric")
        self.assertFalse(rule["enabled"])
        self.assertIn("2 symmetric pair(s)", rule["rationale"])
        self.assertIn("<->", rule["rationale"])

    def test_does_not_induce_below_min_support(self):
        triples = [
            {"subject": "Ada", "predicate": "collaborates_with", "object": "Grace"},
            {"subject": "Grace", "predicate": "collaborates_with", "object": "Ada"},
        ]

        self.assertEqual(onto.induce_rules_from_triples(triples, min_support=2), [])

    def test_induces_candidate_above_min_support(self):
        triples = [
            {"subject": "Ada", "predicate": "collaborates_with", "object": "Grace"},
            {"subject": "Grace", "predicate": "collaborates_with", "object": "Ada"},
            {"subject": "Ada", "predicate": "collaborates_with", "object": "Linus"},
            {"subject": "Linus", "predicate": "collaborates_with", "object": "Ada"},
            {"subject": "Grace", "predicate": "collaborates_with", "object": "Linus"},
            {"subject": "Linus", "predicate": "collaborates_with", "object": "Grace"},
        ]

        rule = self._one_rule(onto.induce_rules_from_triples(triples, min_support=2), "symmetric:collaborates_with")

        self.assertIn("3 symmetric pair(s)", rule["rationale"])

    def test_induces_inverse_candidate_and_dedupes_reverse_direction(self):
        triples = [
            {"subject": "Ada", "predicate": "authored", "object": "Paper"},
            {"subject": "Paper", "predicate": "authored_by", "object": "Ada"},
            {"subject": "Grace", "predicate": "authored", "object": "Compiler"},
            {"subject": "Compiler", "predicate": "authored_by", "object": "Grace"},
        ]

        rules = onto.induce_rules_from_triples(triples, min_support=2)

        inverse_ids = [r["id"] for r in rules if r["family"] == "inverse"]
        self.assertEqual(inverse_ids, ["inverse:authored:authored_by"])
        rule = rules[inverse_ids.index("inverse:authored:authored_by")]
        self.assertFalse(rule["enabled"])
        self.assertIn("2 inverse co-occurrence(s)", rule["rationale"])
        self.assertIn("Ada", rule["rationale"])

    def test_induces_transitive_candidate_from_distinct_chains(self):
        triples = [
            {"subject": "A", "predicate": "depends_on", "object": "B"},
            {"subject": "B", "predicate": "depends_on", "object": "C"},
            {"subject": "D", "predicate": "depends_on", "object": "E"},
            {"subject": "E", "predicate": "depends_on", "object": "F"},
        ]

        rules = onto.induce_rules_from_triples(triples, min_support=2)

        rule = self._one_rule(rules, "transitive:depends_on")
        self.assertEqual(rule["derived_predicate"], "depends_on_closure")
        self.assertFalse(rule["enabled"])
        self.assertIn("2 chain(s) observed", rule["rationale"])
        self.assertIn("CANNOT be confirmed", rule["rationale"])

    def test_induction_ignores_duplicate_triples_self_edges_and_missing_keys(self):
        triples = [
            {"subject": "A", "predicate": "related_to", "object": "A"},
            {"subject": "A", "predicate": "depends_on", "object": "B"},
            {"subject": "A", "predicate": "depends_on", "object": "B"},
            {"subject": "B", "predicate": "depends_on", "object": "C"},
            {"subject": "B", "object": "C"},
        ]

        rules = onto.induce_rules_from_triples(triples, min_support=2)

        self.assertEqual(rules, [])

    def test_induction_accepts_subject_id_and_object_id_when_names_absent(self):
        triples = [
            {"subject_id": 1, "predicate": "precedes", "object_id": 2},
            {"subject_id": 2, "predicate": "precedes", "object_id": 3},
        ]

        rules = onto.induce_rules_from_triples(triples, min_support=1)

        rule = self._one_rule(rules, "transitive:precedes")
        self.assertIn("1->2->3", rule["rationale"])

    def test_induced_rules_are_deterministic_unique_and_sorted(self):
        triples = [
            {"subject": "B", "predicate": "q", "object": "A"},
            {"subject": "A", "predicate": "p", "object": "B"},
            {"subject": "D", "predicate": "q", "object": "C"},
            {"subject": "C", "predicate": "p", "object": "D"},
            {"subject": "A", "predicate": "near", "object": "B"},
            {"subject": "B", "predicate": "near", "object": "A"},
            {"subject": "C", "predicate": "near", "object": "D"},
            {"subject": "D", "predicate": "near", "object": "C"},
        ]

        first = onto.induce_rules_from_triples(triples, min_support=2)
        second = onto.induce_rules_from_triples(list(reversed(triples)), min_support=2)

        self.assertEqual(first, second)
        self.assertEqual([r["id"] for r in first], sorted(r["id"] for r in first))
        self.assertEqual(len({r["id"] for r in first}), len(first))

    def test_disabled_invariant_for_both_generators(self):
        suggested = onto.suggest_rules_from_predicates(["depends_on", "collaborates_with", "authored", "authored_by"])
        induced = onto.induce_rules_from_triples(
            [
                {"subject": "A", "predicate": "related_to", "object": "B"},
                {"subject": "B", "predicate": "related_to", "object": "A"},
                {"subject": "C", "predicate": "related_to", "object": "D"},
                {"subject": "D", "predicate": "related_to", "object": "C"},
            ],
            min_support=2,
        )

        self.assertTrue(suggested)
        self.assertTrue(induced)
        self.assertTrue(all(rule["enabled"] is False for rule in suggested + induced))

    def _one_rule(self, rules, rule_id):
        matches = [r for r in rules if r["id"] == rule_id]
        self.assertEqual(len(matches), 1, rules)
        return matches[0]


class TestFilterBaseTriples(unittest.TestCase):
    def test_filters_closure_predicates_and_rule_outputs(self):
        triples = [
            {"subject": "A", "predicate": "depends_on", "object": "B"},
            {"subject": "A", "predicate": "depends_on_closure", "object": "C"},
            {"subject": "B", "predicate": "dependency_of", "object": "A"},
        ]
        rules = [
            {"id": "transitive:depends_on", "derived_predicate": "depends_on_closure"},
            {"id": "inverse:depends_on:dependency_of", "inverse_predicate": "dependency_of"},
        ]

        self.assertEqual(onto.filter_base_triples(triples, rules), [triples[0]])

    def test_induction_after_filter_ignores_closure_predicates(self):
        triples = [
            {"subject": "A", "predicate": "depends_on_closure", "object": "B"},
            {"subject": "B", "predicate": "depends_on_closure", "object": "C"},
        ]

        self.assertEqual(onto.induce_rules_from_triples(onto.filter_base_triples(triples), min_support=1), [])


class TestMergeAndDocs(unittest.TestCase):
    def test_merge_preserves_existing_rule_verbatim_and_adds_sorted_new_rules(self):
        existing_rule = {
            "id": "transitive:depends_on",
            "family": "transitive",
            "predicate": "depends_on",
            "derived_predicate": "depends_on_closure",
            "enabled": True,
            "max_depth": 3,
            "rationale": "approved",
        }
        duplicate_candidate = dict(existing_rule, enabled=False, rationale="candidate")
        new_rules = [
            {"id": "symmetric:related_to", "family": "symmetric", "predicate": "related_to", "enabled": False},
            duplicate_candidate,
            {"id": "inverse:authored:authored_by", "family": "inverse", "predicate": "authored", "inverse_predicate": "authored_by", "enabled": False},
        ]

        merged, stats = onto.merge_ontology_candidates([existing_rule], new_rules)

        self.assertEqual(stats, {"added": 2, "skipped_existing": 1})
        self.assertIs(merged[0], existing_rule)
        self.assertTrue(merged[0]["enabled"])
        self.assertEqual([r["id"] for r in merged], [
            "transitive:depends_on",
            "inverse:authored:authored_by",
            "symmetric:related_to",
        ])
        self.assertEqual(len({r["id"] for r in merged}), len(merged))

    def test_build_read_write_ontology_doc_round_trip_and_missing_empty_defaults(self):
        doc = onto.build_ontology_doc([
            {"id": "symmetric:related_to", "family": "symmetric", "predicate": "related_to", "enabled": False}
        ])

        with tempfile.TemporaryDirectory(dir=_tempfile_root()) as td:
            path = os.path.join(td, "nested", "ontology.json")
            missing_path = os.path.join(td, "missing.json")
            empty_path = os.path.join(td, "empty.json")
            os.makedirs(os.path.dirname(empty_path), exist_ok=True)
            open(empty_path, "w", encoding="utf-8").close()

            self.assertEqual(onto.read_ontology_doc(missing_path), {"version": 1, "rules": []})
            self.assertEqual(onto.read_ontology_doc(empty_path), {"version": 1, "rules": []})
            onto.write_ontology_doc(path, doc)
            read_back = onto.read_ontology_doc(path)

        self.assertEqual(read_back, doc)
        self.assertEqual(json.dumps(read_back, ensure_ascii=False), json.dumps(doc, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
