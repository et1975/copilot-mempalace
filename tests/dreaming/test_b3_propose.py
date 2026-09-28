"""B3 plain-language ontology rule proposal/approval tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import dream_contemplate
import dream_ontology
import dream_palace


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-palace-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG

    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3PlainLanguageProposalTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _rules_path(self, palace: str) -> str:
        return os.path.join(palace, "ontology.json")

    def _write_ontology(self, palace: str, rules: list[dict] | None = None) -> str:
        rules_path = self._rules_path(palace)
        with open(rules_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rules": list(rules or [])}, fh)
            fh.write("\n")
        return rules_path

    def _add_durable_triple(self, palace: str, subject: str, predicate: str, object_: str) -> str:
        db_path = self._db_path(palace)
        kg = _RealKG(db_path=db_path)
        try:
            triple_id = str(
                kg.add_triple(
                    subject,
                    predicate,
                    object_,
                    valid_from="2026-01-01",
                    confidence=1.0,
                    source_drawer_id=f"drawer:{subject}:{predicate}:{object_}",
                    adapter_name="test:b3-propose",
                )
            )
        finally:
            kg.close()
        dream_palace.ensure_firewall_schema(db_path)
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"support:asserted:{triple_id}",
                    triple_id,
                    "asserted",
                    "trusted_legacy",
                    "asserted",
                    "[]",
                    "durable",
                    "test",
                    f"support:asserted:{triple_id}",
                    "2026-01-01",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    None,
                ),
            )
            con.commit()
        finally:
            con.close()
        return triple_id

    def _seed_depends_on_chains(self, palace: str) -> None:
        self._add_durable_triple(palace, "service_x", "depends_on", "service_y")
        self._add_durable_triple(palace, "service_y", "depends_on", "service_z")
        self._add_durable_triple(palace, "service_a", "depends_on", "service_b")
        self._add_durable_triple(palace, "service_b", "depends_on", "service_c")

    def _make_palace_with_depends_on_chains(self) -> tempfile.TemporaryDirectory:
        tmp = _test_tmpdir()
        self.addCleanup(tmp.cleanup)
        self._write_ontology(tmp.name)
        self._seed_depends_on_chains(tmp.name)
        return tmp

    def assert_plain_text_without_jargon(self, text: str, *, must_contain: list[str]) -> None:
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())
        lowered = text.lower()
        for banned in ("transitive", "inverse", "symmetric", "family"):
            self.assertNotIn(banned, lowered)
        for expected in must_contain:
            self.assertIn(expected, text)

    def test_describe_rule_candidate_describes_transitive_rule_without_jargon(self):
        described = dream_ontology.describe_rule_candidate(
            {"id": "transitive:depends_on", "family": "transitive", "predicate": "depends_on"}
        )

        self.assertEqual(described["id"], "transitive:depends_on")
        self.assertEqual(described["family"], "transitive")
        self.assert_plain_text_without_jargon(described["plain_question"], must_contain=["depends_on"])
        self.assert_plain_text_without_jargon(described["effect"], must_contain=["depends_on"])

    def test_describe_rule_candidate_describes_inverse_rule_without_jargon(self):
        described = dream_ontology.describe_rule_candidate(
            {
                "id": "inverse:authored:authored_by",
                "family": "inverse",
                "predicate": "authored",
                "inverse_predicate": "authored_by",
            }
        )

        self.assertEqual(described["id"], "inverse:authored:authored_by")
        self.assert_plain_text_without_jargon(
            described["plain_question"],
            must_contain=["authored", "authored_by"],
        )
        self.assert_plain_text_without_jargon(described["effect"], must_contain=["authored", "authored_by"])

    def test_describe_rule_candidate_describes_symmetric_rule_without_jargon(self):
        described = dream_ontology.describe_rule_candidate(
            {"id": "symmetric:related_to", "family": "symmetric", "predicate": "related_to"}
        )

        self.assertEqual(described["id"], "symmetric:related_to")
        self.assert_plain_text_without_jargon(described["plain_question"], must_contain=["related_to"])
        self.assert_plain_text_without_jargon(described["effect"], must_contain=["related_to"])

    def test_propose_surfaces_transitive_candidate_from_durable_data(self):
        tmp = self._make_palace_with_depends_on_chains()

        report = dream_contemplate.propose_rules(tmp.name)
        proposals = {proposal["id"]: proposal for proposal in report["proposals"]}

        proposal = proposals["transitive:depends_on"]
        self.assertIs(proposal["would_derive_now"], True)
        self.assertIn("--enable-rule transitive:depends_on", proposal["accept_command"])

    def test_propose_excludes_already_enabled_rule(self):
        tmp = _test_tmpdir()
        self.addCleanup(tmp.cleanup)
        self._write_ontology(
            tmp.name,
            [
                {
                    "id": "transitive:depends_on",
                    "family": "transitive",
                    "predicate": "depends_on",
                    "derived_predicate": "depends_on_closure",
                    "enabled": True,
                }
            ],
        )
        self._seed_depends_on_chains(tmp.name)

        report = dream_contemplate.propose_rules(tmp.name)

        self.assertIn("transitive:depends_on", report["already_enabled"])
        self.assertNotIn("transitive:depends_on", {proposal["id"] for proposal in report["proposals"]})

    def test_enable_rules_adds_and_enables_rule_then_stops_proposing_it(self):
        tmp = self._make_palace_with_depends_on_chains()

        result = dream_contemplate.enable_rules(tmp.name, ["transitive:depends_on"])

        self.assertIn("transitive:depends_on", result["enabled"])
        self.assertGreaterEqual(result["now_enabled_count"], 1)
        rules = dream_palace.load_ontology_config(self._rules_path(tmp.name))
        enabled_rule = next(rule for rule in rules if rule.get("id") == "transitive:depends_on")
        self.assertIs(enabled_rule.get("enabled"), True)
        reproposed = dream_contemplate.propose_rules(tmp.name)
        self.assertNotIn("transitive:depends_on", {proposal["id"] for proposal in reproposed["proposals"]})

    def test_enable_rules_reports_unknown_id_without_enabling_it(self):
        tmp = self._make_palace_with_depends_on_chains()

        result = dream_contemplate.enable_rules(tmp.name, ["transitive:bogus_predicate_xyz"])

        self.assertIn("transitive:bogus_predicate_xyz", result["unknown"])
        self.assertNotIn("transitive:bogus_predicate_xyz", result["enabled"])

    def test_cli_propose_and_enable_rule_json_flows(self):
        tmp = self._make_palace_with_depends_on_chains()
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            propose_code = dream_contemplate.main(["--palace", tmp.name, "--propose", "--format", "json"])

        self.assertEqual(propose_code, 0)
        propose_payload = json.loads(stdout.getvalue())
        self.assertIn("proposals", propose_payload)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            enable_code = dream_contemplate.main(
                ["--palace", tmp.name, "--enable-rule", "transitive:depends_on", "--format", "json"]
            )

        self.assertEqual(enable_code, 0)
        enable_payload = json.loads(stdout.getvalue())
        self.assertIn("transitive:depends_on", enable_payload["enabled"])
        rules = dream_palace.load_ontology_config(self._rules_path(tmp.name))
        self.assertTrue(any(rule.get("id") == "transitive:depends_on" and rule.get("enabled") for rule in rules))

    def test_summarize_proposals_avoids_rule_family_jargon(self):
        report = {
            "palace": "/example/palace",
            "rules_path": "/example/palace/ontology.json",
            "triple_count": 2,
            "proposals": [
                {
                    "id": "transitive:depends_on",
                    "family": "transitive",
                    "plain_question": "Should A depends_on B and B depends_on C mean A depends_on C?",
                    "effect": "New missing depends_on links could be suggested from two-step chains.",
                    "evidence": ["service_x->service_y->service_z"],
                    "enabled": False,
                    "would_derive_now": True,
                    "accept_command": "--enable-rule transitive:depends_on",
                }
            ],
            "already_enabled": [],
        }

        summary = dream_contemplate.summarize_proposals(report)

        self.assertNotIn("transitive", summary.lower())


if __name__ == "__main__":
    unittest.main()
