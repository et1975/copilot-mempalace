"""B1.5 run lifecycle, provisional store, and R5 expiry acceptance tests."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dream_lib
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
class B15LifecycleTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _connect(self, palace: str) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path(palace))
        con.row_factory = sqlite3.Row
        return con

    def _table_count(self, palace: str, table: str) -> int:
        con = self._connect(palace)
        try:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                return 0
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()

    def _row(self, palace: str, sql: str, args: tuple = ()) -> sqlite3.Row:
        con = self._connect(palace)
        try:
            row = con.execute(sql, args).fetchone()
            self.assertIsNotNone(row)
            return row
        finally:
            con.close()

    def _rows(self, palace: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = self._connect(palace)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _add_triple(self, palace: str, subject: str, predicate: str, object_: str) -> str:
        kg = _RealKG(db_path=self._db_path(palace))
        try:
            return str(
                kg.add_triple(
                    subject,
                    predicate,
                    object_,
                    valid_from="2026-01-01",
                    confidence=1.0,
                    source_drawer_id=f"drawer:{subject}:{predicate}:{object_}",
                    adapter_name="test:b15",
                )
            )
        finally:
            kg.close()

    def _entity_ids(self, palace: str) -> dict[str, str]:
        return {str(row["name"]): str(row["id"]) for row in self._rows(palace, "SELECT id, name FROM entities")}

    def _support_rows(self, palace: str, triple_id: str) -> dict[str, sqlite3.Row]:
        rows = self._rows(
            palace,
            """
            SELECT support_id, status, source_trust, ended_at, expires_at
            FROM kg_triple_supports
            WHERE triple_id=?
            ORDER BY support_id
            """,
            (triple_id,),
        )
        return {str(row["support_id"]): row for row in rows}

    def _insert_materialized_abduced_support(
        self,
        palace: str,
        triple_id: str,
        support_id: str,
        *,
        expires_at: str = "2026-01-01T00:00:00+00:00",
    ) -> None:
        con = self._connect(palace)
        try:
            con.execute(
                """
                INSERT INTO kg_triple_supports(
                    support_id, triple_id, status, source_trust, inherited_status,
                    conditional_on_triple_ids, scope, source_kind, source_ref,
                    valid_from, valid_to, created_at, ended_at, expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    support_id,
                    triple_id,
                    "materialized_abduced",
                    "hypothesis",
                    "abduced",
                    f'["prov:{support_id}"]',
                    "durable",
                    "test",
                    support_id,
                    "2026-01-01",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    None,
                    expires_at,
                ),
            )
            con.commit()
        finally:
            con.close()

    def _delete_supports(self, palace: str, triple_id: str) -> None:
        con = self._connect(palace)
        try:
            con.execute("DELETE FROM kg_triple_supports WHERE triple_id=?", (triple_id,))
            con.commit()
        finally:
            con.close()

    def test_create_or_resume_run_persists_row_updates_last_seen_and_rejects_expired_resume(self):
        with _test_tmpdir() as palace:
            first_seen = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            run_id = dream_palace.create_or_resume_run(palace, now=first_seen)

            row = self._row(
                palace,
                "SELECT run_id, status, created_at, last_seen_at, expires_at FROM contemplate_runs WHERE run_id=?",
                (run_id,),
            )
            self.assertEqual(row["run_id"], run_id)
            self.assertEqual(row["status"], "active")
            self.assertIn("2026-01-01T12:00:00", row["created_at"])
            self.assertEqual(row["created_at"], row["last_seen_at"])
            self.assertIn("2026-01-02T12:00:00", row["expires_at"])

            later = first_seen + timedelta(hours=2)
            resumed = dream_palace.create_or_resume_run(palace, run_id=run_id, now=later)

            self.assertEqual(resumed, run_id)
            updated = self._row(
                palace,
                "SELECT created_at, last_seen_at FROM contemplate_runs WHERE run_id=?",
                (run_id,),
            )
            self.assertEqual(updated["created_at"], row["created_at"])
            self.assertIn("2026-01-01T14:00:00", updated["last_seen_at"])

            short_run = dream_palace.create_or_resume_run(
                palace,
                ttl_hours=0.01,
                now=first_seen,
            )
            with self.assertRaises(ValueError):
                dream_palace.create_or_resume_run(
                    palace,
                    run_id=short_run,
                    now=first_seen + timedelta(hours=1),
                )

    def test_assert_provisional_writes_only_provisional_row_and_rejects_trusted_or_inactive_runs(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run_id = dream_palace.create_or_resume_run(palace, now=now)
            for entity_name in ("A", "B", "C", "D", "asserted", "deduced"):
                self._add_triple(palace, entity_name, "b15_seed", f"{entity_name} Seed Target")
            triple_count = self._table_count(palace, "triples")
            support_count = self._table_count(palace, "kg_triple_supports")

            provisional_id = dream_palace.assert_provisional(
                palace,
                run_id,
                "A",
                "depends_on",
                "B",
                confidence=0.42,
                source_kind="test",
                source_ref="case-2",
                now=now,
            )

            row = self._row(
                palace,
                """
                SELECT provisional_id, run_id, subject, predicate, object, status,
                       confidence, source_kind, source_ref, fact_status
                FROM contemplate_provisional_facts
                WHERE provisional_id=?
                """,
                (provisional_id,),
            )
            self.assertEqual(row["run_id"], run_id)
            self.assertEqual((row["subject"], row["predicate"], row["object"]), ("A", "depends_on", "B"))
            self.assertEqual(row["status"], "abduced")
            self.assertAlmostEqual(row["confidence"], 0.42)
            self.assertEqual((row["source_kind"], row["source_ref"], row["fact_status"]), ("test", "case-2", "active"))
            self.assertEqual(self._table_count(palace, "triples"), triple_count)
            self.assertEqual(self._table_count(palace, "kg_triple_supports"), support_count)

            for trusted_status in ("asserted", "deduced"):
                with self.subTest(status=trusted_status):
                    with self.assertRaises(ValueError):
                        dream_palace.assert_provisional(
                            palace,
                            run_id,
                            "A",
                            "depends_on",
                            trusted_status,
                            status=trusted_status,
                            now=now,
                        )

            with self.assertRaises(ValueError):
                dream_palace.assert_provisional(
                    palace,
                    "missing-run",
                    "A",
                    "depends_on",
                    "C",
                    now=now,
                )

            expired_run = dream_palace.create_or_resume_run(palace, ttl_hours=0.01, now=now)
            with self.assertRaises(ValueError):
                dream_palace.assert_provisional(
                    palace,
                    expired_run,
                    "A",
                    "depends_on",
                    "D",
                    now=now + timedelta(hours=1),
                )

    def test_load_premises_simulation_overlays_only_active_provisionals_for_that_run(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            durable_id = self._add_triple(palace, "DurableA", "depends_on", "DurableB")
            self._add_triple(palace, "HypothesisA", "b15_seed", "HypothesisA Seed Target")
            self._add_triple(palace, "HypothesisB", "b15_seed", "HypothesisB Seed Target")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = dream_palace.create_or_resume_run(palace, now=now)
            other_run_id = dream_palace.create_or_resume_run(palace, now=now)
            provisional_id = dream_palace.assert_provisional(
                palace,
                run_id,
                "HypothesisA",
                "depends_on",
                "HypothesisB",
                status="abduced",
                now=now,
            )

            simulation = dream_palace.load_premises(palace, purpose="simulation", run_id=run_id)
            simulation_ids = {row["triple_id"] for row in simulation}

            self.assertIn(durable_id, simulation_ids)
            self.assertIn(f"prov:{provisional_id}", simulation_ids)
            prov_row = next(row for row in simulation if row["triple_id"] == f"prov:{provisional_id}")
            self.assertEqual(
                {
                    "subject": prov_row["subject"],
                    "predicate": prov_row["predicate"],
                    "object": prov_row["object"],
                    "epistemic_status": prov_row["epistemic_status"],
                    "inherited_status": prov_row["inherited_status"],
                    "conditional_on": prov_row["conditional_on"],
                    "source_trust": prov_row["source_trust"],
                    "tainted": prov_row["tainted"],
                },
                {
                    "subject": "HypothesisA",
                    "predicate": "depends_on",
                    "object": "HypothesisB",
                    "epistemic_status": "abduced",
                    "inherited_status": "abduced",
                    "conditional_on": "[]",
                    "source_trust": "hypothesis",
                    "tainted": True,
                },
            )

            for purpose in ("durable", "audit"):
                with self.subTest(purpose=purpose):
                    self.assertFalse(
                        any(str(row["triple_id"]).startswith("prov:") for row in dream_palace.load_premises(palace, purpose=purpose))
                    )
            self.assertNotIn(
                f"prov:{provisional_id}",
                {row["triple_id"] for row in dream_palace.load_premises(palace, purpose="simulation", run_id=other_run_id)},
            )

            con = self._connect(palace)
            try:
                con.execute(
                    """
                    UPDATE contemplate_provisional_facts
                    SET expires_at=?
                    WHERE provisional_id=?
                    """,
                    ("2026-01-01T00:30:00+00:00", provisional_id),
                )
                con.commit()
            finally:
                con.close()
            dream_palace.startup_cleanup(palace, now=now + timedelta(hours=1))

            after_cleanup = dream_palace.load_premises(palace, purpose="simulation", run_id=run_id)
            self.assertNotIn(f"prov:{provisional_id}", {row["triple_id"] for row in after_cleanup})

    def test_provisional_premise_taints_closure_and_writer_rejects_prov_premise(self):
        with _test_tmpdir() as palace:
            a_b = self._add_triple(palace, "A", "depends_on", "B")
            dream_palace.reconcile_firewall_provenance(palace)
            ids = self._entity_ids(palace)
            rules = [
                {
                    "id": "inverse:depends_on:dependency_of",
                    "family": "inverse",
                    "predicate": "depends_on",
                    "inverse_predicate": "dependency_of",
                    "enabled": True,
                }
            ]
            premises = [
                {
                    "triple_id": "prov:p1",
                    "subject": "A",
                    "subject_id": ids["A"],
                    "predicate": "depends_on",
                    "object": "B",
                    "object_id": ids["B"],
                    "valid_from": None,
                    "valid_to": None,
                    "confidence": 1.0,
                    "source_drawer_id": None,
                    "epistemic_status": "abduced",
                    "inherited_status": "abduced",
                    "conditional_on": "[]",
                }
            ]

            candidates = dream_lib.deductive_closure(
                premises,
                rules,
                max_depth=1,
                max_iterations=10,
                max_candidates=10,
            )

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["evidence"]["epistemic_status"], "entailed_given")
            self.assertEqual(candidates[0]["proof"]["entailed_given"], ["prov:p1"])

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

    def test_startup_cleanup_r5_expires_materialized_abduced_support_without_killing_trusted_survivor(self):
        with _test_tmpdir() as palace:
            mixed_id = self._add_triple(palace, "MixedA", "depends_on", "MixedB")
            dream_palace.reconcile_firewall_provenance(palace)
            self._insert_materialized_abduced_support(palace, mixed_id, "support:expired-abduced:mixed")

            first = dream_palace.startup_cleanup(palace, now=datetime(2026, 1, 2, tzinfo=timezone.utc))

            rows = self._support_rows(palace, mixed_id)
            trusted_rows = [row for row in rows.values() if row["status"] in ("asserted", "deduced")]
            self.assertEqual(first["abduced_supports_expired"], 1)
            self.assertIsNotNone(rows["support:expired-abduced:mixed"]["ended_at"])
            self.assertEqual(len(trusted_rows), 1)
            self.assertIsNone(trusted_rows[0]["ended_at"])
            self.assertIsNone(
                self._row(palace, "SELECT valid_to FROM triples WHERE id=?", (mixed_id,))["valid_to"]
            )

            sole_id = self._add_triple(palace, "SoleA", "depends_on", "SoleB")
            dream_palace.reconcile_firewall_provenance(palace)
            self._delete_supports(palace, sole_id)
            self._insert_materialized_abduced_support(palace, sole_id, "support:expired-abduced:sole")

            second = dream_palace.startup_cleanup(palace, now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc))
            self.assertEqual(second["abduced_supports_expired"], 1)
            self.assertIsNotNone(
                self._row(palace, "SELECT valid_to FROM triples WHERE id=?", (sole_id,))["valid_to"]
            )

            third = dream_palace.startup_cleanup(palace, now=datetime(2026, 1, 2, 2, tzinfo=timezone.utc))
            self.assertEqual(third["abduced_supports_expired"], 0)

    def test_startup_cleanup_expires_stale_runs_and_provisionals_idempotently(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._add_triple(palace, "A", "b15_seed", "A Seed Target")
            self._add_triple(palace, "B", "b15_seed", "B Seed Target")
            stale_run = dream_palace.create_or_resume_run(palace, ttl_hours=0.01, now=now)
            active_run = dream_palace.create_or_resume_run(palace, now=now)
            stale_prov = dream_palace.assert_provisional(
                palace,
                active_run,
                "A",
                "depends_on",
                "B",
                now=now,
            )
            con = self._connect(palace)
            try:
                con.execute(
                    "UPDATE contemplate_provisional_facts SET expires_at=? WHERE provisional_id=?",
                    ("2026-01-01T00:30:00+00:00", stale_prov),
                )
                con.commit()
            finally:
                con.close()

            first = dream_palace.startup_cleanup(palace, now=now + timedelta(hours=1))

            self.assertEqual(first["runs_expired"], 1)
            self.assertEqual(first["provisional_expired"], 1)
            self.assertEqual(
                self._row(palace, "SELECT status FROM contemplate_runs WHERE run_id=?", (stale_run,))["status"],
                "expired",
            )
            self.assertEqual(
                self._row(
                    palace,
                    "SELECT fact_status FROM contemplate_provisional_facts WHERE provisional_id=?",
                    (stale_prov,),
                )["fact_status"],
                "expired",
            )

            second = dream_palace.startup_cleanup(palace, now=now + timedelta(hours=2))
            self.assertEqual(second["runs_expired"], 0)
            self.assertEqual(second["provisional_expired"], 0)


if __name__ == "__main__":
    unittest.main()
