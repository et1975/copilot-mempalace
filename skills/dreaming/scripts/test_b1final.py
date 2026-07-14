"""B1.4/R7 finish-firewall acceptance tests."""
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
class B1FinalFirewallTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _connect(self, palace: str) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path(palace))
        con.row_factory = sqlite3.Row
        return con

    def _rows(self, palace: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = self._connect(palace)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _row(self, palace: str, sql: str, args: tuple = ()) -> sqlite3.Row:
        rows = self._rows(palace, sql, args)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _entity_ids(self, palace: str) -> dict[str, str]:
        return {
            str(row["name"]): str(row["id"])
            for row in self._rows(palace, "SELECT id, name FROM entities")
        }

    def _add_triple(
        self,
        palace: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        source_drawer_id: str | None = None,
        adapter_name: str | None = "test:b1final",
    ) -> str:
        kg = _RealKG(db_path=self._db_path(palace))
        try:
            return str(
                kg.add_triple(
                    subject,
                    predicate,
                    object_,
                    valid_from="2026-01-01",
                    confidence=1.0,
                    source_drawer_id=source_drawer_id,
                    adapter_name=adapter_name,
                )
            )
        finally:
            kg.close()

    def _reconcile(self, palace: str) -> dict[str, int]:
        reconcile = getattr(dream_palace, "reconcile_firewall_provenance", None)
        if reconcile is None:
            self.fail("dream_palace.reconcile_firewall_provenance is missing")
        return reconcile(palace)

    def _kg_protection_degree(self, palace: str) -> dict[str, int]:
        kg_protection_degree = getattr(dream_palace, "kg_protection_degree", None)
        if kg_protection_degree is None:
            self.fail("dream_palace.kg_protection_degree is missing")
        return kg_protection_degree(palace)

    def _insert_raw_triple(
        self,
        palace: str,
        triple_id: str,
        subject_name: str,
        predicate: str,
        object_name: str,
        *,
        source_drawer_id: str | None = None,
        adapter_name: str | None = None,
    ) -> str:
        entity_ids = self._entity_ids(palace)
        con = self._connect(palace)
        try:
            con.execute(
                """
                INSERT INTO triples(
                    id, subject, predicate, object, valid_from, valid_to,
                    confidence, source_drawer_id, adapter_name, extracted_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    triple_id,
                    entity_ids[subject_name],
                    predicate,
                    entity_ids[object_name],
                    "2026-02-01",
                    None,
                    1.0,
                    source_drawer_id,
                    adapter_name,
                    "2026-02-01T00:00:00+00:00",
                ),
            )
            con.commit()
            return triple_id
        finally:
            con.close()

    def _insert_derivation(
        self,
        palace: str,
        conclusion_triple_id: str,
        *,
        candidate_id: str,
        premise_triple_ids: str = "[]",
        premise_drawer_ids: str = "[]",
    ) -> None:
        dream_palace.ensure_firewall_schema(self._db_path(palace))
        con = self._connect(palace)
        try:
            con.execute(
                """
                INSERT INTO kg_derivations(
                    candidate_id, conclusion_triple_id, rule_id, ontology_version,
                    premise_triple_ids, premise_drawer_ids, confidence, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id,
                    conclusion_triple_id,
                    "rule:b1final",
                    "ontology:b1final",
                    premise_triple_ids,
                    premise_drawer_ids,
                    1.0,
                    "2026-02-01T00:00:00+00:00",
                ),
            )
            con.commit()
        finally:
            con.close()

    def _expire_triple(self, palace: str, triple_id: str) -> None:
        con = self._connect(palace)
        try:
            con.execute(
                "UPDATE triples SET valid_to=? WHERE id=?",
                ("2026-03-01T00:00:00+00:00", triple_id),
            )
            con.commit()
        finally:
            con.close()

    def _support_row(self, palace: str, triple_id: str) -> sqlite3.Row:
        return self._row(
            palace,
            """
            SELECT triple_id, status, source_trust
            FROM kg_triple_supports
            WHERE triple_id=?
            """,
            (triple_id,),
        )

    def test_r7_bootstrap_reconcile_preserves_legacy_classification_and_sets_epoch(self):
        with _test_tmpdir() as palace:
            asserted_id = self._add_triple(palace, "Legacy", "states", "Fact")
            derived_id = self._add_triple(
                palace,
                "Derived",
                "depends_on_closure",
                "Fact",
                source_drawer_id="derive:bootstrap",
                adapter_name="contemplate:derive",
            )

            self._reconcile(palace)

            asserted = self._support_row(palace, asserted_id)
            derived = self._support_row(palace, derived_id)
            epoch = self._row(
                palace,
                "SELECT value FROM kg_firewall_meta WHERE key='epoch_committed_at'",
            )

        self.assertEqual((asserted["status"], asserted["source_trust"]), ("asserted", "trusted_legacy"))
        self.assertEqual((derived["status"], derived["source_trust"]), ("deduced", "trusted_rule"))
        self.assertTrue(epoch["value"])

    def test_r7_post_epoch_forged_derive_orphan_is_quarantined_and_not_durable(self):
        with _test_tmpdir() as palace:
            self._add_triple(palace, "A", "depends_on", "B")
            self._reconcile(palace)
            forged_id = self._insert_raw_triple(
                palace,
                "forged-post-epoch",
                "A",
                "depends_on_closure",
                "B",
                source_drawer_id="derive:forged",
                adapter_name="contemplate:derive",
            )

            self._reconcile(palace)

            support = self._support_row(palace, forged_id)
            durable_ids = {
                str(row["triple_id"])
                for row in dream_palace.load_premises(palace, purpose="durable")
            }

        self.assertEqual((support["status"], support["source_trust"]), ("unknown", "unknown"))
        self.assertNotEqual((support["status"], support["source_trust"]), ("deduced", "trusted_rule"))
        self.assertNotEqual((support["status"], support["source_trust"]), ("asserted", "trusted_legacy"))
        self.assertNotIn(forged_id, durable_ids)

    def test_r7_post_epoch_orphan_with_derivation_row_is_quarantined_uniformly(self):
        with _test_tmpdir() as palace:
            self._add_triple(palace, "A", "depends_on", "B")
            self._reconcile(palace)
            orphan_id = self._insert_raw_triple(
                palace,
                "post-epoch-with-derivation",
                "A",
                "depends_on_closure",
                "B",
                adapter_name="contemplate:derive",
            )
            self._insert_derivation(
                palace,
                orphan_id,
                candidate_id="post-epoch-real-derivation-row",
                premise_drawer_ids=json.dumps(["drawer-premise"]),
            )

            self._reconcile(palace)

            support = self._support_row(palace, orphan_id)

        self.assertEqual((support["status"], support["source_trust"]), ("unknown", "unknown"))

    def test_b14_live_derivation_premise_drawer_protects_without_source_degree(self):
        with _test_tmpdir() as palace:
            conclusion_id = self._add_triple(
                palace,
                "A",
                "depends_on_closure",
                "B",
                source_drawer_id="drawer-conclusion",
                adapter_name="contemplate:derive",
            )
            premise_drawer = "drawer-premise-only"
            self._insert_derivation(
                palace,
                conclusion_id,
                candidate_id="live-premise-drawer",
                premise_drawer_ids=json.dumps([premise_drawer]),
            )

            source_degree = dream_palace.kg_source_degree(palace)
            protection_degree = self._kg_protection_degree(palace)

        self.assertEqual(source_degree.get(premise_drawer, 0), 0)
        self.assertGreater(protection_degree.get(premise_drawer, 0), 0)

    def test_b14_inactive_conclusion_derivation_premises_do_not_protect(self):
        with _test_tmpdir() as palace:
            conclusion_id = self._add_triple(
                palace,
                "Inactive",
                "depends_on_closure",
                "Result",
                source_drawer_id="drawer-inactive-conclusion",
                adapter_name="contemplate:derive",
            )
            self._insert_derivation(
                palace,
                conclusion_id,
                candidate_id="inactive-conclusion",
                premise_drawer_ids=json.dumps(["drawer-inactive-premise"]),
            )
            self._expire_triple(palace, conclusion_id)

            protection_degree = self._kg_protection_degree(palace)

        self.assertEqual(protection_degree.get("drawer-inactive-premise", 0), 0)

    def test_b14_malformed_derivation_drawer_json_is_skipped_without_crashing(self):
        with _test_tmpdir() as palace:
            good_source = "drawer-direct-survives-malformed"
            conclusion_id = self._add_triple(
                palace,
                "Malformed",
                "depends_on_closure",
                "Result",
                source_drawer_id=good_source,
                adapter_name="contemplate:derive",
            )
            self._insert_derivation(
                palace,
                conclusion_id,
                candidate_id="malformed-drawer-json",
                premise_drawer_ids="[not-json",
            )

            protection_degree = self._kg_protection_degree(palace)

        self.assertGreater(protection_degree.get(good_source, 0), 0)

    def test_b14_direct_active_source_drawer_still_protects(self):
        with _test_tmpdir() as palace:
            direct_source = "drawer-direct-source"
            self._add_triple(
                palace,
                "Direct",
                "states",
                "Fact",
                source_drawer_id=direct_source,
            )

            protection_degree = self._kg_protection_degree(palace)

        self.assertGreater(protection_degree.get(direct_source, 0), 0)


if __name__ == "__main__":
    unittest.main()
