"""B3 S2 non-bypassable broker tests for controlled provisional writes."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

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


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3BrokerTests(unittest.TestCase):
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

    def _create_legacy_run(self, palace: str, now: datetime | None = None) -> str:
        return dream_palace.create_or_resume_run(
            palace,
            now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def _create_controlled_run(
        self,
        palace: str,
        *,
        now: datetime | None = None,
        lease_ttl_seconds: int = 300,
    ) -> dict:
        create = getattr(dream_palace, "create_or_resume_controlled_run", None)
        if create is None:
            self.fail("dream_palace.create_or_resume_controlled_run is missing")
        return dict(
            create(
                palace,
                lease_ttl_seconds=lease_ttl_seconds,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _controller_step(
        self,
        palace: str,
        run: dict,
        action: str,
        *,
        expected_version: int,
        now: datetime | None = None,
    ) -> dict:
        step = getattr(dream_palace, "controller_step", None)
        if step is None:
            self.fail("dream_palace.controller_step is missing")
        return dict(
            step(
                palace,
                run["run_id"],
                action,
                owner_token=run["owner_token"],
                expected_version=expected_version,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _drive_to_deduced(self, palace: str, run: dict, *, now: datetime | None = None) -> dict:
        start = self._controller_step(palace, run, "start_deduction", expected_version=0, now=now)
        self.assertEqual({"ok": start["ok"], "state": start["state"], "version": start["version"]}, {"ok": True, "state": "deducing", "version": 1})
        record = self._controller_step(palace, run, "record_deduction", expected_version=1, now=now)
        self.assertEqual({"ok": record["ok"], "state": record["state"], "version": record["version"]}, {"ok": True, "state": "deduced", "version": 2})
        return {**run, "state": "deduced", "version": 2}

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

    def _issue_approval(
        self,
        palace: str,
        run: dict,
        *,
        expected_version: int,
        canonical_args: dict,
        owner_token: str | None = None,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict:
        issue = getattr(dream_palace, "issue_approval", None)
        if issue is None:
            self.fail("dream_palace.issue_approval is missing")
        return dict(
            issue(
                palace,
                run["run_id"],
                owner_token=owner_token or run["owner_token"],
                expected_version=expected_version,
                approval_kind="assert_provisional",
                tool_name="assert_provisional",
                canonical_args=canonical_args,
                ttl_seconds=ttl_seconds,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _broker_assert_provisional(
        self,
        palace: str,
        run: dict,
        approval_token: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        expected_version: int,
        status: str = "acquired",
        confidence: float | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        broker = getattr(dream_palace, "broker_assert_provisional", None)
        if broker is None:
            self.fail("dream_palace.broker_assert_provisional is missing")
        return dict(
            broker(
                palace,
                run["run_id"],
                owner_token=run["owner_token"],
                expected_version=expected_version,
                approval_token=approval_token,
                subject=subject,
                predicate=predicate,
                object=object_,
                status=status,
                confidence=confidence,
                source_kind=source_kind,
                source_ref=source_ref,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _canonical_args(
        self,
        subject: str,
        predicate: str,
        object_: str,
        *,
        status: str = "acquired",
        source_kind: str | None = None,
        source_ref: str | None = None,
    ) -> dict:
        return {
            "subject": subject,
            "predicate": predicate,
            "object": object_,
            "status": status,
            "source_kind": source_kind,
            "source_ref": source_ref,
        }

    def _provisional_rows(self, palace: str) -> list[sqlite3.Row]:
        return self._rows(
            palace,
            """
            SELECT provisional_id, run_id, subject, predicate, object,
                   subject_id, object_id, status, source_kind, source_ref, fact_status
            FROM contemplate_provisional_facts
            ORDER BY created_at, provisional_id
            """,
        )

    def _run_row(self, palace: str, run_id: str) -> sqlite3.Row:
        return self._row(
            palace,
            """
            SELECT run_id, state, version
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        )

    def _approval_row(self, palace: str, approval_id: str) -> sqlite3.Row:
        con = self._connect(palace)
        try:
            tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            for table_row in tables:
                table_name = str(table_row["name"])
                quoted = '"' + table_name.replace('"', '""') + '"'
                columns = {str(row["name"]) for row in con.execute(f"PRAGMA table_info({quoted})").fetchall()}
                if {"approval_id", "status"}.issubset(columns):
                    row = con.execute(f"SELECT * FROM {quoted} WHERE approval_id=?", (approval_id,)).fetchone()
                    if row is not None:
                        return row
        finally:
            con.close()
        self.fail(f"could not find approval row with approval_id={approval_id!r}")

    def _assert_refusal_code(self, result: dict, code: str) -> None:
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal"]["code"], code)

    def test_controlled_run_direct_assert_provisional_is_non_bypassable_and_writes_no_row(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Bypass Subject", "Bypass Object")
            run = self._create_controlled_run(palace, now=now)

            with self.assertRaisesRegex(ValueError, "broker"):
                self._assert_provisional(
                    palace,
                    run["run_id"],
                    "Bypass Subject",
                    "depends_on",
                    "Bypass Object",
                    status="acquired",
                    source_kind="test",
                    source_ref="b3-bypass",
                    now=now,
                )

            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_legacy_run_direct_assert_provisional_still_writes_row(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Legacy Subject", "Legacy Object")
            run_id = self._create_legacy_run(palace, now=now)

            provisional_id = self._assert_provisional(
                palace,
                run_id,
                "Legacy Subject",
                "depends_on",
                "Legacy Object",
                source_kind="test",
                source_ref="b3-legacy",
                now=now,
            )

            rows = self._provisional_rows(palace)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["provisional_id"], provisional_id)
            self.assertEqual((rows[0]["subject"], rows[0]["predicate"], rows[0]["object"]), ("Legacy Subject", "depends_on", "Legacy Object"))

    def test_broker_happy_path_consumes_approval_and_advances_run(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Happy Subject", "Happy Object")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            args = self._canonical_args(
                "Happy Subject",
                "depends_on",
                "Happy Object",
                source_kind="test",
                source_ref="b3-happy",
            )

            approval = self._issue_approval(palace, run, expected_version=2, canonical_args=args, now=now)

            self.assertTrue(approval["ok"])
            self.assertTrue(approval["approval_id"])
            self.assertTrue(approval["approval_token"])
            self.assertTrue(approval["expires_at"])
            self.assertEqual((self._run_row(palace, run["run_id"])["state"], self._run_row(palace, run["run_id"])["version"]), ("deduced", 2))

            result = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "Happy Subject",
                "depends_on",
                "Happy Object",
                expected_version=2,
                source_kind="test",
                source_ref="b3-happy",
                now=now,
            )

            self.assertEqual({"ok": result["ok"], "run_id": result["run_id"], "state": result["state"], "version": result["version"]}, {"ok": True, "run_id": run["run_id"], "state": "asserted", "version": 3})
            rows = self._provisional_rows(palace)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["provisional_id"], result["provisional_id"])
            self.assertEqual((rows[0]["subject"], rows[0]["predicate"], rows[0]["object"]), ("Happy Subject", "depends_on", "Happy Object"))
            self.assertEqual((rows[0]["subject_id"], rows[0]["object_id"]), (_entity_id("Happy Subject"), _entity_id("Happy Object")))
            self.assertEqual((rows[0]["status"], rows[0]["source_kind"], rows[0]["source_ref"], rows[0]["fact_status"]), ("acquired", "test", "b3-happy", "active"))
            self.assertEqual((self._run_row(palace, run["run_id"])["state"], self._run_row(palace, run["run_id"])["version"]), ("asserted", 3))
            self.assertEqual(self._approval_row(palace, approval["approval_id"])["status"], "consumed")

    def test_approval_token_is_one_use_and_second_consume_writes_no_second_row(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "One Use Subject", "One Use Object")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            args = self._canonical_args("One Use Subject", "depends_on", "One Use Object")
            approval = self._issue_approval(palace, run, expected_version=2, canonical_args=args, now=now)
            self.assertTrue(approval["ok"])
            first = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "One Use Subject",
                "depends_on",
                "One Use Object",
                expected_version=2,
                now=now,
            )
            self.assertTrue(first["ok"])

            second = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "One Use Subject",
                "depends_on",
                "One Use Object",
                expected_version=3,
                now=now,
            )

            self._assert_refusal_code(second, "approval_consumed")
            self.assertEqual(len(self._provisional_rows(palace)), 1)

    def test_forged_approval_token_refuses_approval_invalid_without_write(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Forged Subject", "Forged Object")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)

            result = self._broker_assert_provisional(
                palace,
                run,
                f"forged-{uuid4()}",
                "Forged Subject",
                "depends_on",
                "Forged Object",
                expected_version=2,
                now=now,
            )

            self._assert_refusal_code(result, "approval_invalid")
            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_broker_refuses_args_mismatch_without_write(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Mismatch Subject", "Mismatch Object B", "Mismatch Object C")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            approval = self._issue_approval(
                palace,
                run,
                expected_version=2,
                canonical_args=self._canonical_args("Mismatch Subject", "depends_on", "Mismatch Object B"),
                now=now,
            )
            self.assertTrue(approval["ok"])

            result = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "Mismatch Subject",
                "depends_on",
                "Mismatch Object C",
                expected_version=2,
                now=now,
            )

            self._assert_refusal_code(result, "args_mismatch")
            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_broker_refuses_expired_approval_without_write(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Expired Subject", "Expired Object")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            args = self._canonical_args("Expired Subject", "depends_on", "Expired Object")
            approval = self._issue_approval(palace, run, expected_version=2, canonical_args=args, ttl_seconds=5, now=now)
            self.assertTrue(approval["ok"])

            result = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "Expired Subject",
                "depends_on",
                "Expired Object",
                expected_version=2,
                now=now + timedelta(seconds=6),
            )

            self._assert_refusal_code(result, "approval_expired")
            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_broker_refuses_stale_approval_after_run_version_advances_without_consuming(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Stale Subject", "Stale Object")
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            args = self._canonical_args("Stale Subject", "depends_on", "Stale Object")
            approval = self._issue_approval(palace, run, expected_version=2, canonical_args=args, now=now)
            self.assertTrue(approval["ok"])
            advanced = self._controller_step(palace, run, "select_gap", expected_version=2, now=now)
            self.assertEqual({"ok": advanced["ok"], "state": advanced["state"], "version": advanced["version"]}, {"ok": True, "state": "gap_selected", "version": 3})

            result = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "Stale Subject",
                "depends_on",
                "Stale Object",
                expected_version=3,
                now=now,
            )

            self.assertFalse(result["ok"])
            self.assertIn(result["refusal"]["code"], {"approval_stale", "cas_conflict"})
            self.assertNotEqual(self._approval_row(palace, approval["approval_id"])["status"], "consumed")
            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_broker_refuses_bad_transition_from_open_without_write(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self._seed_entities(palace, "Open Subject", "Open Object")
            run = self._create_controlled_run(palace, now=now)
            args = self._canonical_args("Open Subject", "depends_on", "Open Object")
            approval = self._issue_approval(palace, run, expected_version=0, canonical_args=args, now=now)
            self.assertTrue(approval["ok"])

            result = self._broker_assert_provisional(
                palace,
                run,
                approval["approval_token"],
                "Open Subject",
                "depends_on",
                "Open Object",
                expected_version=0,
                now=now,
            )

            self._assert_refusal_code(result, "bad_transition")
            self.assertEqual(len(self._provisional_rows(palace)), 0)

    def test_issue_approval_refuses_cas_conflict_and_lease_lost(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run = self._create_controlled_run(palace, now=now)
            args = self._canonical_args("Issue Subject", "depends_on", "Issue Object")

            wrong_version = self._issue_approval(palace, run, expected_version=99, canonical_args=args, now=now)
            self._assert_refusal_code(wrong_version, "cas_conflict")

            wrong_owner = self._issue_approval(
                palace,
                run,
                expected_version=0,
                canonical_args=args,
                owner_token="wrong-owner-token",
                now=now,
            )
            self._assert_refusal_code(wrong_owner, "lease_lost")

    def test_broker_checks_missing_kg_endpoints_without_write(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run = self._drive_to_deduced(palace, self._create_controlled_run(palace, now=now), now=now)
            args = self._canonical_args("Missing Subject", "depends_on", "Missing Object")
            approval = self._issue_approval(palace, run, expected_version=2, canonical_args=args, now=now)
            self.assertTrue(approval["ok"])

            try:
                result = self._broker_assert_provisional(
                    palace,
                    run,
                    approval["approval_token"],
                    "Missing Subject",
                    "depends_on",
                    "Missing Object",
                    expected_version=2,
                    now=now,
                )
            except ValueError:
                pass
            else:
                self.assertFalse(result["ok"])
            self.assertEqual(len(self._provisional_rows(palace)), 0)


if __name__ == "__main__":
    unittest.main()
