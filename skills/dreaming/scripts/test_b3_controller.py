"""B3 S1 deterministic controlled-run controller tests."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dream_palace


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-palace-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG  # noqa: F401
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3ControlledRunControllerTests(unittest.TestCase):
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

    def _run_row(self, palace: str, run_id: str) -> sqlite3.Row:
        return self._row(
            palace,
            """
            SELECT run_id, state, version, owner_token_hash, lease_expires_at
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        )

    def _create_controlled_run(
        self,
        palace: str,
        *,
        run_id: str | None = None,
        lease_ttl_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> dict:
        create = getattr(dream_palace, "create_or_resume_controlled_run", None)
        if create is None:
            self.fail("dream_palace.create_or_resume_controlled_run is missing")
        return dict(
            create(
                palace,
                run_id=run_id,
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
        expected_version: int | None = None,
        owner_token: str | None = None,
        now: datetime | str | None = None,
        payload: dict | None = None,
    ) -> dict:
        step = getattr(dream_palace, "controller_step", None)
        if step is None:
            self.fail("dream_palace.controller_step is missing")
        return dict(
            step(
                palace,
                run["run_id"],
                action,
                payload=payload,
                owner_token=owner_token or run["owner_token"],
                expected_version=run["version"] if expected_version is None else expected_version,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _renew_lease(
        self,
        palace: str,
        run: dict,
        *,
        expected_version: int | None = None,
        owner_token: str | None = None,
        now: datetime | str | None = None,
    ) -> dict:
        renew = getattr(dream_palace, "renew_lease", None)
        if renew is None:
            self.fail("dream_palace.renew_lease is missing")
        return dict(
            renew(
                palace,
                run["run_id"],
                owner_token=owner_token or run["owner_token"],
                expected_version=run["version"] if expected_version is None else expected_version,
                now=now or datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        )

    def _assert_refusal_code(self, result: dict, code: str) -> None:
        self.assertFalse(result["ok"])
        self.assertEqual(result["refusal"]["code"], code)

    def test_create_or_resume_controlled_run_fresh_run_stores_hashed_owner_token(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)

            self.assertEqual(run["state"], "open")
            self.assertEqual(run["version"], 0)
            self.assertTrue(run["run_id"])
            self.assertTrue(run["lease_expires_at"])
            self.assertIsInstance(run["owner_token"], str)
            self.assertTrue(run["owner_token"])
            self.assertFalse(run["resumed"])
            self.assertFalse(run["took_over"])

            row = self._run_row(palace, run["run_id"])
            self.assertEqual(row["state"], "open")
            self.assertEqual(row["version"], 0)
            self.assertEqual(row["lease_expires_at"], run["lease_expires_at"])
            self.assertTrue(row["owner_token_hash"])
            self.assertNotEqual(row["owner_token_hash"], run["owner_token"])

    def test_happy_path_start_record_and_select_gap_increments_versions(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)

            start = self._controller_step(palace, run, "start_deduction")
            self.assertEqual({"ok": start["ok"], "state": start["state"], "version": start["version"]}, {"ok": True, "state": "deducing", "version": 1})

            record = self._controller_step(palace, run, "record_deduction", expected_version=1)
            self.assertEqual({"ok": record["ok"], "state": record["state"], "version": record["version"]}, {"ok": True, "state": "deduced", "version": 2})

            select = self._controller_step(palace, run, "select_gap", expected_version=2)
            self.assertEqual({"ok": select["ok"], "state": select["state"], "version": select["version"]}, {"ok": True, "state": "gap_selected", "version": 3})

            row = self._run_row(palace, run["run_id"])
            self.assertEqual((row["state"], row["version"]), ("gap_selected", 3))

    def test_finish_fixpoint_from_deduced_makes_terminal_and_subsequent_step_refuses_terminal(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)
            self._controller_step(palace, run, "start_deduction")
            self._controller_step(palace, run, "record_deduction", expected_version=1)

            finish = self._controller_step(palace, run, "finish_fixpoint", expected_version=2)
            self.assertEqual({"ok": finish["ok"], "state": finish["state"], "version": finish["version"]}, {"ok": True, "state": "fixpoint", "version": 3})

            refused = self._controller_step(palace, run, "abandon", expected_version=3)
            self._assert_refusal_code(refused, "terminal")

    def test_wrong_expected_version_refuses_cas_conflict_without_mutation(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)
            before = self._run_row(palace, run["run_id"])

            result = self._controller_step(palace, run, "start_deduction", expected_version=99)

            self._assert_refusal_code(result, "cas_conflict")
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))

    def test_wrong_owner_token_refuses_lease_lost_without_mutation(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)
            before = self._run_row(palace, run["run_id"])

            result = self._controller_step(palace, run, "start_deduction", owner_token="wrong-owner-token")

            self._assert_refusal_code(result, "lease_lost")
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))

    def test_expired_lease_refuses_lease_lost_with_correct_owner_and_version(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run = self._create_controlled_run(palace, lease_ttl_seconds=10, now=now)
            before = self._run_row(palace, run["run_id"])

            result = self._controller_step(palace, run, "start_deduction", now=now + timedelta(seconds=11))

            self._assert_refusal_code(result, "lease_lost")
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))

    def test_unknown_run_id_refuses_unknown_run(self):
        with _test_tmpdir() as palace:
            run = {"run_id": "missing-run", "owner_token": "token", "version": 0}

            result = self._controller_step(palace, run, "start_deduction")

            self._assert_refusal_code(result, "unknown_run")
            self.assertEqual(result["run_id"], "missing-run")

    def test_unsupported_action_refuses_without_mutation(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)
            before = self._run_row(palace, run["run_id"])

            result = self._controller_step(palace, run, "issue_approval")

            self._assert_refusal_code(result, "unsupported_action")
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))

    def test_bad_transition_select_gap_from_open_refuses_without_mutation(self):
        with _test_tmpdir() as palace:
            run = self._create_controlled_run(palace)
            before = self._run_row(palace, run["run_id"])

            result = self._controller_step(palace, run, "select_gap")

            self._assert_refusal_code(result, "bad_transition")
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))

    def test_expired_lease_takeover_returns_new_owner_and_old_owner_loses_lease(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            original = self._create_controlled_run(palace, run_id="controlled-run", lease_ttl_seconds=10, now=now)

            takeover = self._create_controlled_run(
                palace,
                run_id=original["run_id"],
                lease_ttl_seconds=10,
                now=now + timedelta(seconds=11),
            )

            self.assertTrue(takeover["took_over"])
            self.assertFalse(takeover["resumed"])
            self.assertEqual(takeover["version"], original["version"] + 1)
            self.assertNotEqual(takeover["owner_token"], original["owner_token"])

            refused = self._controller_step(
                palace,
                takeover,
                "start_deduction",
                expected_version=takeover["version"],
                owner_token=original["owner_token"],
                now=now + timedelta(seconds=12),
            )
            self._assert_refusal_code(refused, "lease_lost")

    def test_unexpired_lease_steal_raises_value_error(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run = self._create_controlled_run(palace, run_id="controlled-run", lease_ttl_seconds=300, now=now)

            with self.assertRaises(ValueError):
                self._create_controlled_run(palace, run_id=run["run_id"], now=now + timedelta(seconds=299))

    def test_renew_lease_extends_lease_without_bumping_version_and_refuses_wrong_owner_or_version(self):
        with _test_tmpdir() as palace:
            now = datetime(2026, 1, 1, tzinfo=timezone.utc)
            run = self._create_controlled_run(palace, lease_ttl_seconds=60, now=now)
            before = self._run_row(palace, run["run_id"])

            renewed = self._renew_lease(palace, run, now=now + timedelta(seconds=30))

            self.assertTrue(renewed["ok"])
            self.assertEqual(renewed["version"], run["version"])
            self.assertGreater(renewed["lease_expires_at"], before["lease_expires_at"])
            after = self._run_row(palace, run["run_id"])
            self.assertEqual((after["state"], after["version"]), (before["state"], before["version"]))
            self.assertEqual(after["lease_expires_at"], renewed["lease_expires_at"])

            wrong_owner = self._renew_lease(palace, run, owner_token="wrong-owner-token", now=now + timedelta(seconds=31))
            self._assert_refusal_code(wrong_owner, "lease_lost")

            wrong_version = self._renew_lease(palace, run, expected_version=99, now=now + timedelta(seconds=32))
            self._assert_refusal_code(wrong_version, "cas_conflict")

    def test_legacy_create_or_resume_run_still_works_after_schema_migration(self):
        with _test_tmpdir() as palace:
            self._create_controlled_run(palace)

            legacy_run_id = dream_palace.create_or_resume_run(
                palace,
                now=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            )

            self.assertTrue(legacy_run_id)


if __name__ == "__main__":
    unittest.main()
