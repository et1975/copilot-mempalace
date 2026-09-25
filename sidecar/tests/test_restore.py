"""Private restore activation and cooperative guards; no live palace/services."""

from contextlib import closing
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mempalace_tasks.codec import canonical_json
from mempalace_tasks.model import state_to_dict
from mempalace_tasks.protocol import LogState, fold_record, make_epoch, make_proposal
import test_snapshot as fixtures


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="restore-test-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.addCleanup(self.directory.cleanup)
        self.user_home = Path(self.directory.name) / "user"
        self.user_home.mkdir()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.user_home), "USERPROFILE": str(self.user_home),
            "MEMPALACE_PALACE_PATH": "", "MEMPAL_PALACE_PATH": "",
            "MEMPALACE_DAEMON_STATE_ROOT": "",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fixture = fixtures.SnapshotTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.first, self.second = self.fixture.graph()
        text = "Complete restore source\n" * 100
        self.fixture.send(fixtures.command(
            6, "note", task_id=self.first, expected_version=2,
            tag="source", text=text, references=["git:abc", "file:/source-plan"]))
        self.fixture.artifact()
        self.fixture.sql("INSERT INTO event_artifacts VALUES ('evt-000006', 'art-source')")
        self.logs = {fixtures.AUTHORITY: self.fixture.log}
        self.data = self.fixture.data

    @property
    def restore(self):
        return importlib.import_module("mempalace_tasks.restore")

    def append(self, data, payload):
        self.assertEqual(data, self.data)
        raw = fixtures.raw_record(payload, self.fixture.next_row)
        fold_record(self.logs[payload["authority_id"]], raw)
        self.fixture.store(raw)
        self.fixture.next_row += 1
        return raw

    def prepare(self, **kwargs):
        return self.restore.prepare_task_restore(
            self.data, private_stage=True, append=self.append, **kwargs)

    def test_stage_activation_preserves_task_graph_notes_artifacts_and_provenance(self):
        before = self.fixture.verify()
        replica = (self.data / "replica.json").read_bytes()
        result = self.prepare(expected_authorities=[fixtures.AUTHORITY], now=fixtures.NOW)
        after = self.fixture.verify()
        self.assertEqual(result["status"], "prepared")
        self.assertIs(result["publishable"], True)
        detail = result["authorities"][fixtures.AUTHORITY]
        self.assertNotEqual(detail["epoch_id"], None)
        self.assertNotEqual(detail["activation_id"], None)
        self.assertEqual(detail["domain_ordinal"], 6)
        a, b = before["authorities"][fixtures.AUTHORITY], after["authorities"][fixtures.AUTHORITY]
        for key in ("tasks", "task_ids", "goal_ids", "edges", "notes", "configuration",
                    "domain_head", "domain_ordinal"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual(before["artifacts"], after["artifacts"])
        self.assertEqual(before["artifact_link_count"], after["artifact_link_count"])
        self.assertEqual((self.data / "replica.json").read_bytes(), replica)
        self.assertNotIn("Complete restore source", json.dumps(result))
        self.assertNotIn("operator review", json.dumps(result))

    def test_staged_epoch_fences_old_packets_before_publication(self):
        old = make_proposal(self.fixture.log, fixtures.create(20), fixtures.NOW)
        self.prepare()
        log = self.restore.read_task_logs(self.data)[fixtures.AUTHORITY]
        before = state_to_dict(log.state)
        fold_record(log, fixtures.raw_record(old, 1000))
        self.assertEqual(log.history[-1]["disposition"], "stale")
        self.assertEqual(state_to_dict(log.state), before)
        self.assertIsNone(log.historical_outcome(old["epoch_id"], fixtures.uid(20)))

    def test_restore_ignores_missing_or_discarded_future_sidecar_files(self):
        outside = self.fixture.root / "future"
        outside.mkdir()
        for name in ("pending.json", "verified_head.json", "clock.json"):
            (outside / name).write_text("invalid future", encoding="utf-8")
        first = self.prepare()
        shutil.rmtree(outside)
        second = self.prepare()
        self.assertNotEqual(first["authorities"][fixtures.AUTHORITY]["epoch_id"],
                            second["authorities"][fixtures.AUTHORITY]["epoch_id"])
        self.assertEqual(self.fixture.verify()["authorities"][fixtures.AUTHORITY]["task_count"], 4)

    def test_activation_time_is_no_earlier_than_replayed_domain_and_epoch_high_water(self):
        later = "2099-01-01T00:00:00Z"
        self.fixture.append(make_epoch(self.fixture.log, fixtures.uid(100), fixtures.uid(200), later))
        result = self.prepare(now=fixtures.NOW)
        self.assertEqual(result["authorities"][fixtures.AUTHORITY]["activation_at"], later)

    def test_partial_append_failure_is_unpublishable_but_can_retry_private_stage(self):
        other = LogState(fixtures.OTHER)
        self.fixture.send(fixtures.genesis(), log=other)
        self.logs[fixtures.OTHER] = other
        calls = []

        def failing(data, payload):
            calls.append(payload["authority_id"])
            if len(calls) == 2:
                raise self.restore.RestoreError("append_failed", "synthetic second append failure")
            return self.append(data, payload)

        with self.assertRaises(self.restore.RestoreError) as caught:
            self.restore.prepare_task_restore(self.data, private_stage=True, append=failing)
        self.assertFalse(caught.exception.details["publishable"])
        self.assertTrue((self.data / self.restore.PREPARE_MARKER).exists())
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.prepare()["status"], "prepared")
        self.assertFalse((self.data / self.restore.PREPARE_MARKER).exists())

    def test_response_loss_and_mismatching_receipt_do_not_publish_partial_stage(self):
        def lost(data, payload):
            self.append(data, payload)
            raise self.restore.RestoreError("append_uncertain", "response lost")

        with self.assertRaises(self.restore.RestoreError) as caught:
            self.restore.prepare_task_restore(self.data, private_stage=True, append=lost)
        self.assertEqual(caught.exception.details["activation_state"], "accepted_current")
        self.assertFalse(caught.exception.details["publishable"])
        self.assertIsNotNone(self.fixture.verify()["authorities"][fixtures.AUTHORITY]["epoch_id"])
        self.assertTrue((self.data / self.restore.PREPARE_MARKER).exists())
        self.assertEqual(self.prepare()["status"], "prepared")

    def test_adapter_cannot_change_artifact_or_domain_truth(self):
        def corrupt(data, payload):
            receipt = self.append(data, payload)
            self.fixture.sql("UPDATE artifacts SET content = 'tampered'")
            return receipt

        with self.assertRaises(self.restore.RestoreError):
            self.restore.prepare_task_restore(self.data, private_stage=True, append=corrupt)
        self.assertTrue((self.data / self.restore.PREPARE_MARKER).exists())

    def test_adapter_cannot_change_native_artifact_metadata_or_links(self):
        def corrupt(data, payload):
            receipt = self.append(data, payload)
            self.fixture.sql("UPDATE artifacts SET metadata_json = '{\"changed\":true}'")
            return receipt

        with self.assertRaises(self.restore.RestoreError) as caught:
            self.restore.prepare_task_restore(self.data, private_stage=True, append=corrupt)
        self.assertEqual(caught.exception.code, "changed_history")

    def test_private_stage_acknowledgement_and_schema_validation_precede_append(self):
        with self.assertRaises(self.restore.RestoreError) as caught:
            self.restore.prepare_task_restore(self.data, append=self.append)
        self.assertEqual(caught.exception.code, "private_stage_required")
        self.fixture.db.unlink()
        with patch.object(self, "append", side_effect=AssertionError("must not append")):
            with self.assertRaises(self.restore.RestoreError):
                self.prepare()
        self.assertFalse(self.fixture.db.exists())

    def test_missing_expected_or_uninitialized_authority_is_refused(self):
        with self.assertRaises(self.restore.RestoreError):
            self.prepare(expected_authorities=[fixtures.OTHER])
        other = LogState(fixtures.OTHER)
        self.fixture.append(make_epoch(other, fixtures.uid(100), fixtures.uid(200), fixtures.NOW),
                            log=other)
        with self.assertRaises(self.restore.RestoreError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "uninitialized_authority")

    def test_symlink_or_hardlink_to_live_storage_is_refused(self):
        alias = self.fixture.root / "alias"
        alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(self.restore.RestoreError):
            self.restore.prepare_task_restore(alias, private_stage=True, append=self.append)
        linked = self.fixture.root / "linked-log"
        os.link(self.fixture.db, linked)
        with self.assertRaises(self.restore.RestoreError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "unsafe_path")

    def test_configured_live_data_path_is_never_an_activation_target(self):
        with patch.dict(os.environ, {"MEMPALACE_PALACE_PATH": str(self.data)}):
            with self.assertRaises(self.restore.RestoreError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, "unsafe_stage")

    def server_record(self, data=None, value=None):
        data = self.data if data is None else data
        path = self.restore.serverinfo_path(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value if value is not None else {
            "pid": os.getpid(), "host": "127.0.0.1", "port": 4321, "scheme": "http",
            "read_only": True, "palace_path": str(data),
        }), encoding="utf-8")
        return path

    def test_active_and_indeterminate_hubs_are_refusals_not_stops(self):
        record = self.server_record()
        with self.assertRaises(self.restore.RestoreError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "active_writer")
        record.write_text("{", encoding="utf-8")
        with self.assertRaises(self.restore.RestoreError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "indeterminate_writer")
        self.assertEqual(record.read_text(encoding="utf-8"), "{")

    def test_dead_pid_does_not_make_a_malformed_hub_record_safe(self):
        self.server_record(value={"pid": 123, "host": "localhost", "port": 1234,
                                  "palace_path": str(self.data), "read_only": "not-a-bool"})
        with patch.object(self.restore, "_pid_alive", return_value=False):
            with self.assertRaises(self.restore.RestoreError) as caught:
                self.prepare()
        self.assertEqual(caught.exception.code, "indeterminate_writer")

    def test_windows_liveness_never_uses_os_kill(self):
        with patch.object(os, "kill", side_effect=AssertionError("unsafe Windows signal")):
            with self.assertRaises(self.restore.RestoreError) as caught:
                self.restore.windows_pid_state(os.getpid())
        self.assertEqual(caught.exception.code, "indeterminate_writer")

    def test_main_writer_lease_matches_current_home_path_and_keeps_inode_bytes(self):
        key = hashlib.sha256(os.path.normcase(os.path.realpath(self.data)).encode()).hexdigest()[:16]
        expected = self.user_home / ".mempalace/locks" / f"mine_palace_{key}.lock"
        self.assertEqual(self.restore.writer_lock_path(self.data), expected)
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"\0existing diagnostic body")
        before = (expected.stat().st_ino, expected.read_bytes())
        with self.restore.writer_lease(self.data):
            with self.assertRaises(self.restore.RestoreError) as caught:
                with self.restore.writer_lease(self.data):
                    self.fail("second lease acquired")
            self.assertEqual(caught.exception.code, "busy_writer")
        self.assertEqual((expected.stat().st_ino, expected.read_bytes()), before)

    def test_sqlite_guard_rejects_busy_writer_and_blocks_new_sqlite_writes(self):
        with closing(sqlite3.connect(self.fixture.db, timeout=0)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(self.restore.RestoreError) as caught:
                with self.restore.sqlite_write_guard([self.fixture.db]):
                    self.fail("busy writer accepted")
            self.assertEqual(caught.exception.code, "busy_writer")
            writer.rollback()
            with self.restore.sqlite_write_guard([self.fixture.db]):
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("UPDATE artifacts SET content = 'blocked'")
        self.assertEqual(self.fixture.verify()["artifact_count"], 1)

    def test_sqlite_guard_reports_corruption_as_indeterminate_not_a_busy_writer(self):
        self.fixture.db.write_bytes(b"damaged live SQLite")
        with self.assertRaises(self.restore.RestoreError) as caught:
            with self.restore.sqlite_write_guard([self.fixture.db]):
                self.fail("cannot prove exclusion against damaged SQLite")
        self.assertEqual(caught.exception.code, "indeterminate_writer")
        self.assertEqual(self.fixture.db.read_bytes(), b"damaged live SQLite")

    def test_real_preinstalled_cli_appends_private_stage_barrier_only(self):
        binary = shutil.which("mempalace")
        self.assertIsNotNone(binary, "preinstalled MemPalace CLI is required for this integration")
        before = self.fixture.verify()
        result = self.restore.prepare_task_restore(
            self.data, private_stage=True, mempalace=binary, now=fixtures.NOW)
        after = self.fixture.verify()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(after["event_count"], before["event_count"] + 1)
        self.assertEqual(after["authorities"][fixtures.AUTHORITY]["tasks"],
                         before["authorities"][fixtures.AUTHORITY]["tasks"])
        self.assertEqual(after["authorities"][fixtures.AUTHORITY]["notes"],
                         before["authorities"][fixtures.AUTHORITY]["notes"])
        self.assertEqual(after["artifacts"], before["artifacts"])
        self.assertFalse((self.user_home / ".mempalace/server").exists())


if __name__ == "__main__":
    unittest.main()
