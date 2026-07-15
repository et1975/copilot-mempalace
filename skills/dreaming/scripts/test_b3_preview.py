"""B3 contemplate rule-proposal derivation preview + disable-rule tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import dream_contemplate
import dream_palace


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-preview-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG

    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


TRANSITIVE_DEPENDS_ON = {
    "id": "transitive:depends_on",
    "family": "transitive",
    "predicate": "depends_on",
    "derived_predicate": "depends_on_closure",
    "enabled": True,
    "max_depth": 8,
}


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3PreviewAndDisableTests(unittest.TestCase):
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
                    adapter_name="test:b3-preview",
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

    def _make_palace(self, rules: list[dict] | None = None) -> str:
        tmp = _test_tmpdir()
        self.addCleanup(tmp.cleanup)
        self._write_ontology(tmp.name, rules)
        return tmp.name

    def _proposal_by_id(self, report: dict, rule_id: str) -> dict | None:
        for proposal in report.get("proposals") or []:
            if proposal.get("id") == rule_id:
                return proposal
        return None

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def test_preview_surfaces_nonsensical_reverse_for_co_authored(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")

        report = dream_contemplate.propose_rules(palace)
        proposal = self._proposal_by_id(report, "symmetric:co_authored")
        self.assertIsNotNone(proposal, "expected a symmetric:co_authored proposal")
        self.assertTrue(proposal["would_derive_now"])
        examples = [e.lower() for e in proposal.get("example_derivations") or []]
        self.assertIn("sort_benchmark co_authored kenneth_norman", examples)

    def test_preview_helper_returns_closure_conclusion(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "service_x", "depends_on", "service_y")
        self._add_durable_triple(palace, "service_y", "depends_on", "service_z")
        triples = dream_palace.load_premises(palace, purpose="durable")
        previews = dream_contemplate._preview_candidate_derivations(triples, TRANSITIVE_DEPENDS_ON)
        self.assertTrue(previews)
        self.assertTrue(any("service_x" in p and "service_z" in p for p in previews))

    def test_preview_helper_empty_when_nothing_derives(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "service_a", "depends_on", "service_b")
        triples = dream_palace.load_premises(palace, purpose="durable")
        previews = dream_contemplate._preview_candidate_derivations(triples, TRANSITIVE_DEPENDS_ON)
        self.assertEqual(previews, [])

    def test_would_derive_now_tracks_preview(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")
        report = dream_contemplate.propose_rules(palace)
        for proposal in report.get("proposals") or []:
            self.assertEqual(
                bool(proposal.get("would_derive_now")),
                bool(proposal.get("example_derivations")),
            )

    def test_summary_shows_example_and_stays_jargon_free(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")
        report = dream_contemplate.propose_rules(palace)
        summary = dream_contemplate.summarize_proposals(report)
        self.assertIn("sort_benchmark co_authored kenneth_norman", summary)
        lowered = summary.lower()
        self.assertNotIn("symmetric", lowered)
        self.assertNotIn("transitive", lowered)

    # ------------------------------------------------------------------
    # Disable
    # ------------------------------------------------------------------
    def _enabled_ids(self, palace: str) -> list[str]:
        rules = dream_palace.load_ontology_config(self._rules_path(palace))
        return [str(r.get("id")) for r in rules if bool(r.get("enabled", False))]

    def test_disable_rules_flips_enabled_false(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")
        dream_contemplate.enable_rules(palace, ["symmetric:co_authored"])
        self.assertIn("symmetric:co_authored", self._enabled_ids(palace))

        result = dream_contemplate.disable_rules(palace, ["symmetric:co_authored"])
        self.assertIn("symmetric:co_authored", result["disabled"])
        self.assertNotIn("symmetric:co_authored", self._enabled_ids(palace))

    def test_disable_rules_reports_unknown(self):
        palace = self._make_palace()
        result = dream_contemplate.disable_rules(palace, ["symmetric:bogus_xyz"])
        self.assertIn("symmetric:bogus_xyz", result["unknown"])
        self.assertEqual(result["disabled"], [])

    def test_cli_disable_rule_returns_zero(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")
        dream_contemplate.enable_rules(palace, ["symmetric:co_authored"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = dream_contemplate.main(
                ["--palace", palace, "--disable-rule", "symmetric:co_authored", "--format", "json"]
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("symmetric:co_authored", self._enabled_ids(palace))

    def test_enable_then_disable_reproposes(self):
        palace = self._make_palace()
        self._add_durable_triple(palace, "kenneth_norman", "co_authored", "sort_benchmark")
        dream_contemplate.enable_rules(palace, ["symmetric:co_authored"])
        after_enable = dream_contemplate.propose_rules(palace)
        self.assertIsNone(self._proposal_by_id(after_enable, "symmetric:co_authored"))

        dream_contemplate.disable_rules(palace, ["symmetric:co_authored"])
        after_disable = dream_contemplate.propose_rules(palace)
        self.assertIsNotNone(self._proposal_by_id(after_disable, "symmetric:co_authored"))


if __name__ == "__main__":
    unittest.main()
