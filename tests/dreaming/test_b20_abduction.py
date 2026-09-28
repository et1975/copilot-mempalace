"""B2.0 abduction substrate acceptance and firewall invariant tests."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

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


def _entity_id(name: str) -> str:
    return name.lower().replace(" ", "_").replace(chr(39), "")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B20AbductionSubstrateTests(unittest.TestCase):
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
        self.assertEqual(len(rows), 1, f"expected exactly one row for SQL: {sql!r}; got {len(rows)}")
        return rows[0]

    def _scalar(self, palace: str, sql: str, args: tuple = ()) -> int:
        row = self._row(palace, sql, args)
        return int(row[0])

    def _add_triple(
        self,
        palace: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        source_drawer_id: str | None = None,
        adapter_name: str = "test:b20",
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
                    source_drawer_id=source_drawer_id or f"drawer:{subject}:{predicate}:{object_}",
                    adapter_name=adapter_name,
                )
            )
        finally:
            kg.close()

    def _seed_entities(self, palace: str, *names: str) -> None:
        for name in names:
            self._add_triple(palace, name, "b20_seed", f"{name} Seed Target")

    def _create_run(self, palace: str, now: datetime | None = None) -> str:
        return dream_palace.create_or_resume_run(
            palace,
            now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def _assert_provisional(
        self,
        palace: str,
        run_id: str,
        subject: str,
        predicate: str,
        object_: str,
        **kwargs,
    ) -> str:
        assert_provisional = getattr(dream_palace, "assert_provisional", None)
        if assert_provisional is None:
            self.fail("dream_palace.assert_provisional is missing")
        return str(assert_provisional(palace, run_id, subject, predicate, object_, **kwargs))

    def _promote(
        self,
        palace: str,
        provisional_id: str,
        *,
        confirmation_token: str,
        run_id: str,
        **kwargs,
    ) -> dict:
        promote = getattr(dream_palace, "assert_user_fact_from_provisional", None)
        if promote is None:
            self.fail("dream_palace.assert_user_fact_from_provisional is missing")
        return dict(
            promote(
                palace,
                provisional_id,
                confirmation_token=confirmation_token,
                run_id=run_id,
                **kwargs,
            )
        )

    def _claim_digest(self, subject: str, predicate: str, object_: str) -> str:
        return _sha256(
            "|".join(
                [
                    _entity_id(subject),
                    dream_lib.normalize_predicate(predicate),
                    _entity_id(object_),
                ]
            )
        )

    def _confirmation_token(self, provisional_id: str, subject: str, predicate: str, object_: str, run_id: str) -> str:
        return _sha256("|".join([provisional_id, self._claim_digest(subject, predicate, object_), run_id]))

    def _active_triple_rows(self, palace: str, subject: str, predicate: str, object_: str) -> list[sqlite3.Row]:
        return self._rows(
            palace,
            """
            SELECT id, subject, predicate, object, valid_to
            FROM triples
            WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL
            ORDER BY id
            """,
            (_entity_id(subject), dream_lib.normalize_predicate(predicate), _entity_id(object_)),
        )

    def _support_rows(self, palace: str, triple_id: str) -> list[sqlite3.Row]:
        return self._rows(
            palace,
            """
            SELECT support_id, triple_id, status, source_trust, inherited_status,
                   conditional_on_triple_ids, scope, source_kind, source_ref,
                   ended_at, expires_at
            FROM kg_triple_supports
            WHERE triple_id=?
            ORDER BY support_id
            """,
            (triple_id,),
        )

    def _trusted_user_supports(self, palace: str, triple_id: str) -> list[sqlite3.Row]:
        return [
            row
            for row in self._support_rows(palace, triple_id)
            if row["status"] == "asserted"
            and row["source_trust"] == "trusted_user"
            and row["inherited_status"] == "asserted"
            and row["ended_at"] is None
        ]

    def test_assert_provisional_rejects_missing_entities_and_stores_resolved_endpoint_ids(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run_id = self._create_run(palace, now)
            self._seed_entities(palace, "Existing Subject", "Existing Object")

            with self.assertRaises(ValueError):
                self._assert_provisional(palace, run_id, "Missing Subject", "depends_on", "Existing Object", now=now)
            with self.assertRaises(ValueError):
                self._assert_provisional(palace, run_id, "Existing Subject", "depends_on", "Missing Object", now=now)

            provisional_id = self._assert_provisional(
                palace,
                run_id,
                "Existing Subject",
                "depends_on",
                "Existing Object",
                source_kind="test",
                source_ref="b20-1",
                now=now,
            )

            row = self._row(
                palace,
                """
                SELECT subject, predicate, object, subject_id, object_id, fact_status
                FROM contemplate_provisional_facts
                WHERE provisional_id=?
                """,
                (provisional_id,),
            )
            self.assertEqual((row["subject"], row["predicate"], row["object"]), ("Existing Subject", "depends_on", "Existing Object"))
            self.assertEqual((row["subject_id"], row["object_id"]), ("existing_subject", "existing_object"))
            self.assertEqual(row["fact_status"], "active")

    def test_simulation_overlay_uses_resolved_ids_for_provisional_chaining_and_is_not_durable_or_audit(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            durable_ab = self._add_triple(palace, "A", "depends_on", "B")
            self._seed_entities(palace, "C")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "B", "depends_on", "C", now=now)

            simulation = dream_palace.load_premises(palace, purpose="simulation", run_id=run_id)
            prov_row = next(row for row in simulation if row["triple_id"] == f"prov:{provisional_id}")
            self.assertEqual((prov_row["subject_id"], prov_row["object_id"]), ("b", "c"))

            rules = [
                {
                    "id": "transitive:depends_on",
                    "family": "transitive",
                    "predicate": "depends_on",
                    "enabled": True,
                    "max_depth": 3,
                }
            ]
            candidates = dream_lib.deductive_closure(
                simulation,
                rules,
                max_depth=3,
                max_iterations=10,
                max_candidates=20,
            )
            ac = [
                cand
                for cand in candidates
                if cand["conclusion"]["subject_id"] == "a"
                and cand["conclusion"]["predicate"] == "depends_on_closure"
                and cand["conclusion"]["object_id"] == "c"
            ]

            self.assertEqual(len(ac), 1)
            self.assertEqual(ac[0]["evidence"]["epistemic_status"], "entailed_given")
            self.assertIn(f"prov:{provisional_id}", ac[0]["proof"]["entailed_given"])
            self.assertIn(durable_ab, ac[0]["proof"]["premise_ids"])

            for purpose in ("durable", "audit"):
                with self.subTest(purpose=purpose):
                    premise_ids = {str(row["triple_id"]) for row in dream_palace.load_premises(palace, purpose=purpose)}
                    self.assertNotIn(f"prov:{provisional_id}", premise_ids)

    def test_promoting_confirmed_provisional_creates_trusted_user_premise_and_verification_event(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Human Claim", "Trusted Premise")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Human Claim", "depends_on", "Trusted Premise", now=now)
            token = self._confirmation_token(provisional_id, "Human Claim", "depends_on", "Trusted Premise", run_id)

            result = self._promote(
                palace,
                provisional_id,
                confirmation_token=token,
                run_id=run_id,
                evidence_ref="user:acceptance",
                evidence_quote="I confirm this claim.",
                now=now,
            )

            self.assertTrue(result["promoted"])
            triple_id = result["triple_id"]
            support_id = result["support_id"]
            self.assertEqual([row["id"] for row in self._active_triple_rows(palace, "Human Claim", "depends_on", "Trusted Premise")], [triple_id])

            support = self._row(
                palace,
                """
                SELECT support_id, triple_id, status, source_trust, inherited_status,
                       conditional_on_triple_ids, scope, source_kind, source_ref, ended_at
                FROM kg_triple_supports
                WHERE support_id=?
                """,
                (support_id,),
            )
            self.assertEqual(support["triple_id"], triple_id)
            self.assertEqual((support["status"], support["source_trust"], support["inherited_status"]), ("asserted", "trusted_user", "asserted"))
            self.assertEqual(support["conditional_on_triple_ids"], "[]")
            self.assertEqual(support["scope"], "durable")
            self.assertEqual((support["source_kind"], support["source_ref"]), ("contemplate:user_assert", f"promote:{provisional_id}"))
            self.assertIsNone(support["ended_at"])

            durable_ids = {str(row["triple_id"]) for row in dream_palace.load_premises(palace, purpose="durable")}
            self.assertIn(triple_id, durable_ids)

            event = self._row(
                palace,
                """
                SELECT new_support_id, triple_id, verification_kind, claim_digest,
                       run_id, evidence_ref, evidence_quote
                FROM kg_verification_events
                WHERE new_support_id=? AND triple_id=?
                """,
                (support_id, triple_id),
            )
            self.assertEqual(event["verification_kind"], "user_confirmed")
            self.assertEqual(event["claim_digest"], self._claim_digest("Human Claim", "depends_on", "Trusted Premise"))
            self.assertEqual((event["run_id"], event["evidence_ref"], event["evidence_quote"]), (run_id, "user:acceptance", "I confirm this claim."))

            prov = self._row(
                palace,
                "SELECT fact_status, expired_at FROM contemplate_provisional_facts WHERE provisional_id=?",
                (provisional_id,),
            )
            self.assertEqual(prov["fact_status"], "promoted")
            self.assertIsNotNone(prov["expired_at"])

    def test_forged_confirmation_token_rejects_without_writing_any_durable_fact_or_support(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Forged Subject", "Forged Object")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Forged Subject", "depends_on", "Forged Object", now=now)

            triple_count_before = self._scalar(palace, "SELECT COUNT(*) FROM triples")
            support_count_before = self._scalar(palace, "SELECT COUNT(*) FROM kg_triple_supports")

            with self.assertRaises(ValueError):
                self._promote(
                    palace,
                    provisional_id,
                    confirmation_token="not-the-token",
                    run_id=run_id,
                    now=now,
                )

            self.assertEqual(self._scalar(palace, "SELECT COUNT(*) FROM triples"), triple_count_before)
            self.assertEqual(self._scalar(palace, "SELECT COUNT(*) FROM kg_triple_supports"), support_count_before)
            self.assertEqual(self._active_triple_rows(palace, "Forged Subject", "depends_on", "Forged Object"), [])
            prov = self._row(palace, "SELECT fact_status, expired_at FROM contemplate_provisional_facts WHERE provisional_id=?", (provisional_id,))
            self.assertEqual(prov["fact_status"], "active")
            self.assertIsNone(prov["expired_at"])

    def test_confirmation_token_is_one_use_and_replay_does_not_duplicate_triple_or_support(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Replay Subject", "Replay Object")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Replay Subject", "depends_on", "Replay Object", now=now)
            token = self._confirmation_token(provisional_id, "Replay Subject", "depends_on", "Replay Object", run_id)
            first = self._promote(palace, provisional_id, confirmation_token=token, run_id=run_id, now=now)

            triple_count = len(self._active_triple_rows(palace, "Replay Subject", "depends_on", "Replay Object"))
            support_count = len(self._trusted_user_supports(palace, first["triple_id"]))

            with self.assertRaises(ValueError):
                self._promote(palace, provisional_id, confirmation_token=token, run_id=run_id, now=now)

            self.assertEqual(len(self._active_triple_rows(palace, "Replay Subject", "depends_on", "Replay Object")), triple_count)
            self.assertEqual(len(self._trusted_user_supports(palace, first["triple_id"])), support_count)
            self.assertEqual(triple_count, 1)
            self.assertEqual(support_count, 1)

    def test_pre_promotion_firewall_keeps_provisional_out_of_durable_and_audit_premises(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Firewall Subject", "Firewall Object")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Firewall Subject", "depends_on", "Firewall Object", now=now)

            simulation_ids = {str(row["triple_id"]) for row in dream_palace.load_premises(palace, purpose="simulation", run_id=run_id)}
            self.assertIn(f"prov:{provisional_id}", simulation_ids)

            for purpose in ("durable", "audit"):
                with self.subTest(purpose=purpose):
                    premise_ids = {str(row["triple_id"]) for row in dream_palace.load_premises(palace, purpose=purpose)}
                    self.assertNotIn(f"prov:{provisional_id}", premise_ids)
                    self.assertEqual(self._active_triple_rows(palace, "Firewall Subject", "depends_on", "Firewall Object"), [])

    def test_promotion_does_not_end_or_alter_unrelated_supports(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            unrelated_id = self._add_triple(palace, "Unrelated", "states", "Survivor")
            self._seed_entities(palace, "Promoted Subject", "Promoted Object")
            dream_palace.reconcile_firewall_provenance(palace)
            unrelated_before = self._row(
                palace,
                """
                SELECT support_id, status, source_trust, inherited_status, ended_at
                FROM kg_triple_supports
                WHERE triple_id=?
                """,
                (unrelated_id,),
            )
            self.assertIsNone(unrelated_before["ended_at"])

            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Promoted Subject", "depends_on", "Promoted Object", now=now)
            token = self._confirmation_token(provisional_id, "Promoted Subject", "depends_on", "Promoted Object", run_id)
            self._promote(palace, provisional_id, confirmation_token=token, run_id=run_id, now=now)

            unrelated_after = self._row(
                palace,
                """
                SELECT support_id, status, source_trust, inherited_status, ended_at
                FROM kg_triple_supports
                WHERE support_id=?
                """,
                (unrelated_before["support_id"],),
            )
            self.assertEqual(
                (
                    unrelated_after["support_id"],
                    unrelated_after["status"],
                    unrelated_after["source_trust"],
                    unrelated_after["inherited_status"],
                    unrelated_after["ended_at"],
                ),
                (
                    unrelated_before["support_id"],
                    unrelated_before["status"],
                    unrelated_before["source_trust"],
                    unrelated_before["inherited_status"],
                    None,
                ),
            )

    def test_promotion_dedupes_existing_active_spo_and_adds_user_support_to_existing_triple(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            existing_id = self._add_triple(palace, "Existing SPO", "depends_on", "Existing Object")
            dream_palace.reconcile_firewall_provenance(palace)
            run_id = self._create_run(palace, now)
            provisional_id = self._assert_provisional(palace, run_id, "Existing SPO", "depends_on", "Existing Object", now=now)
            token = self._confirmation_token(provisional_id, "Existing SPO", "depends_on", "Existing Object", run_id)

            result = self._promote(palace, provisional_id, confirmation_token=token, run_id=run_id, now=now)

            self.assertEqual(result["triple_id"], existing_id)
            rows = self._active_triple_rows(palace, "Existing SPO", "depends_on", "Existing Object")
            self.assertEqual([row["id"] for row in rows], [existing_id])
            trusted_user = self._trusted_user_supports(palace, existing_id)
            self.assertEqual(len(trusted_user), 1)
            self.assertEqual(trusted_user[0]["source_ref"], f"promote:{provisional_id}")
            legacy_or_user_active = [
                row
                for row in self._support_rows(palace, existing_id)
                if row["ended_at"] is None and row["status"] == "asserted"
            ]
            self.assertGreaterEqual(len(legacy_or_user_active), 2)


if __name__ == "__main__":
    unittest.main()


class NormalizeDtForKgRegressionTests(unittest.TestCase):
    """Regression: _normalize_dt_for_kg must drop fractional seconds so add_triple accepts it
    (caught by the B2.0 live promotion demo — a microsecond timestamp reached add_triple)."""

    def test_strips_fractional_seconds_to_add_triple_format(self):
        from mempalace.config import sanitize_iso_temporal
        for raw in (
            "2026-07-15T14:40:02.926380+00:00",
            "2026-07-15T14:40:02.926380Z",
            "2026-07-15T14:40:02.5",
        ):
            got = dream_palace._normalize_dt_for_kg(raw)
            # must not raise (i.e. add_triple would accept it)
            sanitize_iso_temporal(got, "valid_from")
            self.assertNotIn(".", got)
