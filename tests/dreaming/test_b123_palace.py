"""B1.2/B1.3 palace-side firewall tests."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

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
class B123PalaceTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _add_triple(
        self,
        palace: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        status: str = "asserted",
        valid_from: str = "2026-01-01",
    ) -> str:
        db_path = self._db_path(palace)
        kg = _RealKG(db_path=db_path)
        try:
            triple_id = str(
                kg.add_triple(
                    subject,
                    predicate,
                    object_,
                    valid_from=valid_from,
                    confidence=1.0,
                    source_drawer_id=f"drawer:{subject}:{predicate}:{object_}",
                    adapter_name="test:b123",
                )
            )
        finally:
            kg.close()
        dream_palace.ensure_firewall_schema(db_path)
        source_trust = "trusted_rule" if status == "deduced" else "trusted_legacy"
        support_id = f"support:{status}:{triple_id}"
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    support_id,
                    triple_id,
                    status,
                    source_trust,
                    status,
                    "[]",
                    "durable",
                    "test",
                    support_id,
                    valid_from,
                    None,
                    "2026-01-01T00:00:00+00:00",
                    None,
                ),
            )
            con.commit()
        finally:
            con.close()
        return triple_id

    def _add_asserted_support(self, palace: str, triple_id: str) -> None:
        con = sqlite3.connect(self._db_path(palace))
        try:
            con.execute(
                "INSERT OR REPLACE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"support:asserted-extra:{triple_id}",
                    triple_id,
                    "asserted",
                    "trusted_legacy",
                    "asserted",
                    "[]",
                    "durable",
                    "test",
                    "asserted-extra",
                    "2026-01-01",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    None,
                ),
            )
            con.commit()
        finally:
            con.close()

    def _add_derivation(self, palace: str, conclusion_id: str, premise_ids: list[str], *, candidate: str) -> None:
        con = sqlite3.connect(self._db_path(palace))
        try:
            cur = con.execute(
                "INSERT INTO kg_derivations(candidate_id, conclusion_triple_id, rule_id,"
                " ontology_version, premise_triple_ids, premise_drawer_ids, confidence, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    candidate,
                    conclusion_id,
                    "rule:test",
                    "onto:test",
                    json.dumps(premise_ids),
                    "[]",
                    1.0,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            derivation_id = cur.lastrowid
            for premise_id in premise_ids:
                con.execute(
                    "INSERT INTO kg_derivation_premises(derivation_id, premise_triple_id) VALUES (?,?)",
                    (derivation_id, premise_id),
                )
            con.commit()
        finally:
            con.close()

    def _triple_valid_to(self, palace: str, triple_id: str) -> str | None:
        con = sqlite3.connect(self._db_path(palace))
        try:
            return con.execute("SELECT valid_to FROM triples WHERE id=?", (triple_id,)).fetchone()[0]
        finally:
            con.close()

    def _active_support_count(self, palace: str, triple_id: str) -> int:
        con = sqlite3.connect(self._db_path(palace))
        try:
            return con.execute(
                "SELECT COUNT(*) FROM kg_triple_supports s"
                " WHERE s.triple_id=? AND s.ended_at IS NULL AND s.valid_to IS NULL",
                (triple_id,),
            ).fetchone()[0]
        finally:
            con.close()

    def test_add_derived_rejects_provisional_premise(self):
        with _test_tmpdir() as palace:
            a_b = self._add_triple(palace, "A", "depends_on", "B")
            ids = self._entity_ids(palace)
            writer = dream_palace.KgDeriveWriter(palace)
            try:
                with self.assertRaisesRegex(ValueError, "premise not grounded/eligible: prov:p1"):
                    writer.add_derived(
                        {"subject_id": ids["A"], "predicate": "depends_on_closure", "object_id": ids["B"]},
                        "rule",
                        [a_b, "prov:p1"],
                        [],
                        "onto:test",
                        1.0,
                        "2026-01-01",
                        None,
                    )
            finally:
                writer.close()

    def test_add_derived_rejects_ineligible_premise(self):
        with _test_tmpdir() as palace:
            self._add_triple(palace, "A", "depends_on", "B")
            ids = self._entity_ids(palace)
            writer = dream_palace.KgDeriveWriter(palace)
            try:
                with self.assertRaisesRegex(ValueError, "premise not grounded/eligible: missing-premise"):
                    writer.add_derived(
                        {"subject_id": ids["A"], "predicate": "depends_on_closure", "object_id": ids["B"]},
                        "rule",
                        ["missing-premise"],
                        [],
                        "onto:test",
                        1.0,
                        "2026-01-01",
                        None,
                    )
            finally:
                writer.close()

    def _entity_ids(self, palace: str) -> dict[str, str]:
        con = sqlite3.connect(self._db_path(palace))
        try:
            return {name: str(entity_id) for entity_id, name in con.execute("SELECT id, name FROM entities")}
        finally:
            con.close()

    def test_cascade_single_proof_lost_ends_conclusion(self):
        with _test_tmpdir() as palace:
            a_b = self._add_triple(palace, "A", "depends_on", "B")
            b_c = self._add_triple(palace, "B", "depends_on", "C")
            a_c = self._add_triple(palace, "A", "depends_on_closure", "C", status="deduced")
            self._add_derivation(palace, a_c, [a_b, b_c], candidate="single")

            result = dream_palace.invalidate_triples_cascade(palace, [b_c], "2026-02-01T00:00:00+00:00")

            self.assertEqual(result["roots_ended"], [b_c])
            self.assertEqual(result["cascade_invalidated"], [a_c])
            self.assertEqual(self._triple_valid_to(palace, a_c), "2026-02-01T00:00:00+00:00")
            self.assertEqual(self._active_support_count(palace, a_c), 0)

    def test_cascade_alternate_proof_survives(self):
        with _test_tmpdir() as palace:
            a_b = self._add_triple(palace, "A", "depends_on", "B")
            b_c = self._add_triple(palace, "B", "depends_on", "C")
            a_d = self._add_triple(palace, "A", "depends_on", "D")
            d_c = self._add_triple(palace, "D", "depends_on", "C")
            a_c = self._add_triple(palace, "A", "depends_on_closure", "C", status="deduced")
            self._add_derivation(palace, a_c, [a_b, b_c], candidate="lost")
            self._add_derivation(palace, a_c, [a_d, d_c], candidate="survives")

            result = dream_palace.invalidate_triples_cascade(palace, [b_c], "2026-02-01T00:00:00+00:00")

            self.assertEqual(result["cascade_invalidated"], [])
            self.assertEqual(result["survived_by_alternate_proof"], [a_c])
            self.assertIsNone(self._triple_valid_to(palace, a_c))
            self.assertGreater(self._active_support_count(palace, a_c), 0)

    def test_cascade_independently_asserted_triple_survives_lapsed_derivation(self):
        with _test_tmpdir() as palace:
            a_b = self._add_triple(palace, "A", "depends_on", "B")
            b_c = self._add_triple(palace, "B", "depends_on", "C")
            a_c = self._add_triple(palace, "A", "depends_on_closure", "C", status="deduced")
            self._add_asserted_support(palace, a_c)
            self._add_derivation(palace, a_c, [a_b, b_c], candidate="asserted-survives")

            result = dream_palace.invalidate_triples_cascade(palace, [b_c], "2026-02-01T00:00:00+00:00")

            self.assertEqual(result["cascade_invalidated"], [])
            self.assertIsNone(self._triple_valid_to(palace, a_c))
            self.assertGreater(self._active_support_count(palace, a_c), 0)

    def test_cascade_pure_cycle_without_external_seed_ends(self):
        with _test_tmpdir() as palace:
            seed = self._add_triple(palace, "P", "supports", "A")
            a = self._add_triple(palace, "A", "cycle", "fact", status="deduced")
            b = self._add_triple(palace, "B", "cycle", "fact", status="deduced")
            self._add_derivation(palace, a, [seed], candidate="seed-to-a")
            self._add_derivation(palace, a, [b], candidate="b-to-a")
            self._add_derivation(palace, b, [a], candidate="a-to-b")

            result = dream_palace.invalidate_triples_cascade(palace, [seed], "2026-02-01T00:00:00+00:00")

            self.assertEqual(result["cascade_invalidated"], sorted([a, b]))
            self.assertEqual(self._active_support_count(palace, a), 0)
            self.assertEqual(self._active_support_count(palace, b), 0)

    def test_cascade_anchored_cycle_survives(self):
        with _test_tmpdir() as palace:
            lapsed_seed = self._add_triple(palace, "P", "supports", "A")
            anchor = self._add_triple(palace, "S", "supports", "A")
            a = self._add_triple(palace, "A", "cycle", "fact", status="deduced")
            b = self._add_triple(palace, "B", "cycle", "fact", status="deduced")
            self._add_derivation(palace, a, [lapsed_seed], candidate="lapsed-to-a")
            self._add_derivation(palace, a, [anchor], candidate="anchor-to-a")
            self._add_derivation(palace, a, [b], candidate="b-to-a")
            self._add_derivation(palace, b, [a], candidate="a-to-b")

            result = dream_palace.invalidate_triples_cascade(palace, [lapsed_seed], "2026-02-01T00:00:00+00:00")

            self.assertEqual(result["cascade_invalidated"], [])
            self.assertEqual(result["survived_by_alternate_proof"], sorted([a, b]))
            self.assertIsNone(self._triple_valid_to(palace, a))
            self.assertIsNone(self._triple_valid_to(palace, b))

    def test_cascade_malformed_lineage_json_rolls_back(self):
        with _test_tmpdir() as palace:
            root = self._add_triple(palace, "P", "supports", "A")
            conclusion = self._add_triple(palace, "A", "depends_on_closure", "C", status="deduced")
            con = sqlite3.connect(self._db_path(palace))
            try:
                cur = con.execute(
                    "INSERT INTO kg_derivations(candidate_id, conclusion_triple_id, rule_id,"
                    " ontology_version, premise_triple_ids, premise_drawer_ids, confidence, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "bad-json",
                        conclusion,
                        "rule:test",
                        "onto:test",
                        "[not-json",
                        "[]",
                        1.0,
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                con.execute(
                    "INSERT INTO kg_derivation_premises(derivation_id, premise_triple_id) VALUES (?,?)",
                    (cur.lastrowid, root),
                )
                con.commit()
            finally:
                con.close()

            with self.assertRaises(ValueError):
                dream_palace.invalidate_triples_cascade(palace, [root], "2026-02-01T00:00:00+00:00")

            self.assertIsNone(self._triple_valid_to(palace, root))
            self.assertIsNone(self._triple_valid_to(palace, conclusion))
            self.assertGreater(self._active_support_count(palace, root), 0)
            self.assertGreater(self._active_support_count(palace, conclusion), 0)


if __name__ == "__main__":
    unittest.main()
