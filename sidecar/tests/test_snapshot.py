"""Offline snapshot verification against real SQLite and the shared task fold."""

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
from uuid import UUID, uuid5

from mempalace_tasks.codec import canonical_json, payload_hash
from mempalace_tasks.protocol import (
    LogState, MAX_PAYLOAD_BYTES, fold_record, make_epoch, make_proposal, make_settlement,
)


AUTHORITY = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NOW = "2026-09-23T00:00:00Z"
REPLICA = "rep_0123456789ab"
SCHEMA = """
CREATE TABLE events (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, stream TEXT NOT NULL,
    room TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT,
    correlation_id TEXT, branch TEXT, base_commit TEXT, status TEXT,
    body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    origin_replica TEXT, origin_seq INTEGER, hlc TEXT
);
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL, content TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}', origin_replica TEXT
);
CREATE TABLE event_artifacts (
    event_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
    PRIMARY KEY (event_id, artifact_id)
);
"""


def uid(number):
    return str(UUID(int=number))


def command(number, operation, **fields):
    return {"command_id": uid(number), "operation": operation, "actor": "operator", **fields}


def genesis(number=1):
    return command(number, "authority_create", actors={"operator": "operator"},
                   execution_profiles={"local": {"execution_class": "isolated"}},
                   supervisors={})


def create(number, **fields):
    return command(number, "create", project="demo", kind="task", title=f"Task {number}",
                   description="Complete bounded task", acceptance="Verified",
                   execution_class="isolated", execution_profile="local", **fields)


def raw_record(payload, number):
    authority = payload["authority_id"]
    identity = "activation_id" if payload["record_type"] == "mptask.epoch" else "command_id"
    metadata = {"authority_id": authority, identity: payload[identity]}
    if payload["schema_version"] == 2:
        metadata["epoch_id"] = payload["epoch_id"]
    return {
        "id": f"evt-{number:06}", "seq": number, "type": payload["record_type"],
        "stream": f"mptask/{authority}", "room": "tasks", "from_agent": "mempalace-tasks",
        "to_agent": None, "correlation_id": payload[identity], "branch": None,
        "base_commit": None, "status": None, "body": canonical_json(payload),
        "created_at": NOW, "metadata": metadata, "artifact_ids": [],
        "origin_replica": REPLICA, "origin_seq": number, "hlc": None,
    }


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="snapshot-test-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = self.data / "logstream.sqlite3"
        self.log = LogState(AUTHORITY)
        self.next_row = 1
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript(SCHEMA)
        (self.data / "replica.json").write_text(
            json.dumps({"replica_id": REPLICA}), encoding="utf-8")

    def verify(self, data=None, **kwargs):
        snapshot = importlib.import_module("mempalace_tasks.snapshot")
        return snapshot.validate_task_snapshot(self.data if data is None else data, **kwargs)

    def error(self, code, data=None, **kwargs):
        snapshot = importlib.import_module("mempalace_tasks.snapshot")
        with self.assertRaises(snapshot.SnapshotError) as caught:
            self.verify(data, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(caught.exception.message)

    def sql(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute(sql, parameters)
            con.commit()

    def store(self, raw):
        self.sql("""INSERT INTO events VALUES (
            :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
            :branch, :base_commit, :status, :body, :created_at, :metadata_json,
            :origin_replica, :origin_seq, :hlc)""",
                 {**raw, "metadata_json": canonical_json(raw["metadata"])})

    def append(self, payload, *, log=None):
        log = self.log if log is None else log
        raw = raw_record(payload, self.next_row)
        fold_record(log, raw)
        self.store(raw)
        self.next_row += 1
        return payload

    def send(self, cmd, *, log=None):
        log = self.log if log is None else log
        return self.append(make_proposal(log, cmd, NOW), log=log)

    def graph(self):
        self.send(genesis())
        self.send(command(2, "bootstrap", project="demo", title="Deliver",
                          description="Full source goal", acceptance="Evidence retained",
                          planning_task={"title": "Plan", "description": "Decompose",
                                         "acceptance": "Bounded tasks",
                                         "execution_class": "isolated",
                                         "execution_profile": "local"},
                          goal_policy={"scope": "demo"}))
        self.send(create(3, hold_reason="operator review"))
        self.send(create(4))
        first = "tsk_" + str(uuid5(UUID(AUTHORITY), uid(3)))
        second = "tsk_" + str(uuid5(UUID(AUTHORITY), uid(4)))
        self.send(command(5, "add_dependency", source=first, target=second,
                          edge_type="blocks", expected_source_version=1,
                          expected_target_version=1))
        return first, second

    def artifact(self, content="Complete source \u2603", artifact_id="art-source"):
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        self.sql("INSERT INTO artifacts VALUES (?, 'note', ?, ?, ?, 'operator', ?, '{}', ?)",
                 (artifact_id, digest, len(encoded), content, NOW, REPLICA))
        return digest, len(encoded)

    def image(self, data=None):
        data = self.data if data is None else data
        return {str(p.relative_to(data)): (p.read_bytes(), p.stat().st_mode)
                for p in data.iterdir() if p.is_file()}

    def test_legacy_graph_configuration_ids_status_holds_and_edges(self):
        first, second = self.graph()
        result = self.verify(expected_authorities=[AUTHORITY], require_logstream=True)
        self.assertEqual(result["status"], "valid")
        self.assertIs(result["quiescence_verified"], False)
        self.assertIs(result["external_references_verified"], False)
        self.assertEqual(result["replica_id"], REPLICA)
        authority = result["authorities"][AUTHORITY]
        self.assertIs(authority["initialized"], True)
        self.assertIsNone(authority["epoch_id"])
        self.assertEqual(authority["domain_ordinal"], 5)
        self.assertEqual(authority["domain_head"], "evt-000005")
        self.assertEqual(authority["task_count"], 4)
        self.assertEqual(authority["goal_count"], 1)
        self.assertEqual(authority["configuration"]["actors"], {"operator": "operator"})
        self.assertEqual(authority["tasks"][first]["status"], "open")
        self.assertEqual(authority["tasks"][first]["hold_reason"], "operator review")
        self.assertEqual(authority["tasks"][second]["title"], "Task 4")
        self.assertIn({"source": first, "target": second, "edge_type": "blocks"},
                      authority["edges"])
        goal = "tsk_" + str(uuid5(UUID(AUTHORITY), uid(2)))
        planner = "tsk_" + str(uuid5(UUID(AUTHORITY), str(uuid5(UUID(uid(2)), "planning"))))
        self.assertEqual(authority["goal_ids"], [goal])
        self.assertEqual(authority["task_ids"], sorted([goal, planner, first, second]))
        self.assertEqual(authority["edges"], [
            {"source": goal, "target": planner, "edge_type": "parent_child"},
            {"source": first, "target": second, "edge_type": "blocks"},
        ])
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_v2_epoch_preserves_history_and_stale_frames_are_audit_only(self):
        first, second = self.graph()
        obsolete = make_proposal(self.log, create(7), NOW)
        barrier = self.append(make_epoch(self.log, uid(100), uid(200), NOW))
        self.append(obsolete)
        self.append(make_settlement(obsolete))
        self.send(command(7, "update", task_id=second, expected_version=2,
                          patch={"title": "Current epoch task"}))
        self.append(barrier)
        authority = self.verify()["authorities"][AUTHORITY]
        self.assertEqual(authority["epoch_id"], uid(100))
        self.assertEqual(authority["activation_id"], uid(200))
        self.assertEqual(authority["activation_at"], NOW)
        self.assertEqual(authority["domain_ordinal"], 6)
        self.assertEqual(authority["raw_record_count"], 10)
        self.assertEqual(authority["stale_record_count"], 2)
        self.assertEqual(authority["task_count"], 4)
        self.assertEqual(authority["tasks"][second]["title"], "Current epoch task")
        self.assertEqual(authority["tasks"][first]["hold_reason"], "operator review")

    def test_notes_keep_full_source_content_hashes_and_external_references(self):
        first, _ = self.graph()
        text = "source \u2603\n" * 600
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        tag = f"source-plan/{digest}/part-1-of-1"
        payload = self.send(command(6, "note", task_id=first, expected_version=2,
                                    tag=tag, text=text,
                                    references=["file:/original/plan", "git:abc", "https://example.test"]))
        notes = self.verify()["authorities"][AUTHORITY]["notes"]
        self.assertEqual(notes, [{
            "event_id": "evt-000006", "ordinal": 6, "epoch_id": None,
            "command_id": uid(6), "task_id": first, "tag": tag, "text": text,
            "text_sha256": digest, "payload_hash": payload["payload_hash"],
            "references": ["file:/original/plan", "git:abc", "https://example.test"],
        }])

    def test_missing_or_future_local_recovery_metadata_is_irrelevant(self):
        self.graph()
        before = self.verify()
        future = self.root / "future-recovery"
        future.mkdir()
        for name in ("pending.json", "verified_head.json", "clock.json", "projection.json"):
            (future / name).write_text("invalid discarded future", encoding="utf-8")
        self.assertEqual(self.verify(), before)
        shutil.rmtree(future)
        self.assertEqual(self.verify(), before)

    def test_earlier_coherent_database_is_valid_on_fresh_verification(self):
        self.send(genesis())
        earlier = self.root / "earlier"
        shutil.copytree(self.data, earlier)
        self.send(create(2))
        self.assertEqual(self.verify()["authorities"][AUTHORITY]["task_count"], 1)
        prior = self.verify(earlier, expected_authorities=[AUTHORITY])
        self.assertEqual(prior["authorities"][AUTHORITY]["task_count"], 0)
        self.assertEqual(prior["authorities"][AUTHORITY]["domain_ordinal"], 1)

    def test_multiple_authorities_interleave_without_folding_native_task_events(self):
        self.send(genesis())
        other = LogState(OTHER)
        self.send(genesis(), log=other)
        native = raw_record(make_proposal(self.log, create(2), NOW), self.next_row)
        native.update(type="task.create", stream="native/tasks", from_agent="native",
                      body="ordinary non-protocol task text", correlation_id=None, metadata={})
        self.store(native)
        self.next_row += 1
        self.send(create(2), log=other)
        result = self.verify(expected_authorities=[AUTHORITY, OTHER])
        self.assertEqual(result["event_count"], 4)
        self.assertEqual(result["task_event_count"], 3)
        self.assertEqual(result["authorities"][AUTHORITY]["task_count"], 0)
        self.assertEqual(result["authorities"][OTHER]["task_count"], 1)

    def test_local_rowid_order_not_timestamps_or_origin_sequence(self):
        self.graph()
        self.sql("UPDATE events SET created_at = CASE id WHEN 'evt-000001' "
                 "THEN '2099-01-01T00:00:00Z' ELSE '2000-01-01T00:00:00Z' END, "
                 "origin_seq = 100 - rowid")
        self.assertEqual(self.verify()["authorities"][AUTHORITY]["domain_ordinal"], 5)

    def test_missing_expected_authority_is_an_error(self):
        self.send(genesis())
        self.error("missing_authority", expected_authorities=[AUTHORITY, OTHER])

    def test_expected_authorities_must_be_canonical_uuids_not_a_bare_string(self):
        for value in (AUTHORITY, ["bad"], [None], [AUTHORITY.upper().replace("1", "A")]):
            with self.subTest(value=value):
                self.error("invalid_argument", expected_authorities=value)

    def test_data_path_and_required_flag_reject_invalid_inputs_explicitly(self):
        for value in ("", "\0", 3, True):
            with self.subTest(path=value):
                self.error("invalid_argument", value)
        self.error("invalid_argument", require_logstream="yes")

    def test_uninitialized_epoch_stream_is_explicit_and_not_expected_recovery(self):
        self.append(make_epoch(self.log, uid(100), uid(200), NOW))
        authority = self.verify()["authorities"][AUTHORITY]
        self.assertIs(authority["initialized"], False)
        self.assertEqual(authority["task_count"], 0)
        self.error("uninitialized_authority", expected_authorities=[AUTHORITY])

    def test_optional_legacy_absence_is_distinct_and_creates_nothing(self):
        self.db.unlink()
        (self.data / "replica.json").unlink()
        self.assertEqual(self.verify(), {
            "schema_version": 1, "status": "absent", "replica_id": None,
            "event_count": 0, "task_event_count": 0, "artifact_count": 0,
            "artifact_link_count": 0, "artifacts": {}, "authorities": {},
            "quiescence_verified": False, "external_references_verified": False,
        })
        self.assertEqual(list(self.data.iterdir()), [])
        self.error("missing_logstream", require_logstream=True)
        self.error("missing_logstream", expected_authorities=[AUTHORITY])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_corrupt_database_or_wrong_schema_never_becomes_empty_tasks(self):
        self.db.write_bytes(b"not SQLite")
        self.error("invalid_database")
        self.db.unlink()
        self.sql("CREATE TABLE unrelated (value TEXT)")
        self.error("invalid_database")

    def test_unknown_protocol_and_broken_chain_fail_explicitly(self):
        self.send(genesis())
        original = make_proposal(self.log, create(2), NOW)
        for field, value in (("schema_version", 99), ("previous_event_id", "missing"),
                             ("ordinal", 100), ("payload_hash", "0" * 64)):
            with self.subTest(field=field):
                payload = deepcopy(original)
                payload[field] = value
                if field != "payload_hash":
                    payload["payload_hash"] = payload_hash(payload)
                event = raw_record(payload, 2)
                self.store(event)
                self.error("invalid_protocol")
                self.sql("DELETE FROM events WHERE id = 'evt-000002'")

    def test_domain_snapshot_forgery_fails_even_with_valid_payload_hash(self):
        self.send(genesis())
        payload = make_proposal(self.log, create(2), NOW)
        payload["event"]["tasks"][0]["title"] = "forged"
        payload["event"]["response"]["tasks"][0]["title"] = "forged"
        payload["payload_hash"] = payload_hash(payload)
        self.store(raw_record(payload, 2))
        self.error("invalid_protocol")

    def test_malformed_reserved_route_and_metadata_are_errors(self):
        self.send(genesis())
        for field, value in (("stream", "mptask/not-a-uuid"), ("room", "wrong"),
                             ("from_agent", "wrong"), ("type", "mptask.unknown"),
                             ("stream", "other"), ("metadata_json", '{"authority_id":1}')):
            with self.subTest(field=field):
                saved = {
                    "stream": f"mptask/{AUTHORITY}", "room": "tasks",
                    "from_agent": "mempalace-tasks", "type": "mptask.command",
                    "metadata_json": canonical_json(
                        {"authority_id": AUTHORITY, "command_id": uid(1)}),
                }[field]
                self.sql(f"UPDATE events SET {field} = ?", (value,))
                self.error("invalid_protocol")
                self.sql(f"UPDATE events SET {field} = ?", (saved,))

    def test_duplicate_metadata_keys_are_not_silently_collapsed(self):
        self.send(genesis())
        self.sql("UPDATE events SET metadata_json = ?",
                 ('{"authority_id":"wrong","authority_id":"' + AUTHORITY
                  + '","command_id":"' + uid(1) + '"}',))
        self.error("invalid_protocol")

    def test_body_and_metadata_limits_fail_instead_of_truncating(self):
        self.send(genesis())
        self.sql("UPDATE events SET body = ?", ("x" * (MAX_PAYLOAD_BYTES + 1),))
        self.error("size_limit")
        self.sql("UPDATE events SET body = '{}', metadata_json = ?", ("x" * 65537,))
        self.error("size_limit")

    def test_valid_native_artifacts_and_links_are_verified(self):
        self.send(genesis())
        digest, size = self.artifact()
        self.sql("INSERT INTO event_artifacts VALUES ('evt-000001', 'art-source')")
        result = self.verify()
        self.assertEqual(result["artifact_count"], 1)
        self.assertEqual(result["artifact_link_count"], 1)
        self.assertEqual(result["artifacts"], {
            "art-source": {"sha256": digest, "size_bytes": size, "kind": "note"}})

    def test_missing_linked_artifact_and_dangling_event_link_are_errors(self):
        self.send(genesis())
        self.sql("INSERT INTO event_artifacts VALUES ('evt-000001', 'absent')")
        self.error("invalid_artifact_link")
        self.sql("DELETE FROM event_artifacts")
        self.artifact()
        self.sql("INSERT INTO event_artifacts VALUES ('absent-event', 'art-source')")
        self.error("invalid_artifact_link")

    def test_changed_artifact_content_hash_and_size_are_errors(self):
        self.send(genesis())
        for column, value in (("content", "changed"), ("sha256", "0" * 64),
                              ("size_bytes", 1), ("size_bytes", -1)):
            with self.subTest(column=column, value=value):
                self.sql("DELETE FROM artifacts")
                self.artifact()
                self.sql(f"UPDATE artifacts SET {column} = ?", (value,))
                self.error("invalid_artifact")

    def test_artifact_content_bound_is_explicit(self):
        self.sql("INSERT INTO artifacts VALUES "
                 "('large', 'note', ?, ?, ?, 'operator', ?, '{}', ?)",
                 ("0" * 64, 4 * 1024 * 1024 + 1, "x" * (4 * 1024 * 1024 + 1),
                  NOW, REPLICA))
        self.error("size_limit")

    def test_malformed_or_missing_replica_never_mints_replacement(self):
        replica = self.data / "replica.json"
        for content in ("{", "[]", "{}", '{"replica_id":"wrong"}',
                        '{"replica_id":"rep_0123456789ab","replica_id":"rep_abcdefabcdef"}'):
            with self.subTest(content=content):
                replica.write_text(content, encoding="utf-8")
                self.error("invalid_replica")
                self.assertEqual(replica.read_text(encoding="utf-8"), content)
        replica.unlink()
        self.error("invalid_replica")
        self.assertFalse(replica.exists())

    def test_regular_storage_paths_only_and_symlinks_are_not_followed(self):
        alias = self.root / "alias"
        alias.symlink_to(self.data, target_is_directory=True)
        self.error("unsafe_path", alias)
        for name in ("logstream.sqlite3", "logstream.sqlite3-wal",
                     "logstream.sqlite3-shm", "replica.json"):
            with self.subTest(name=name):
                source = self.data / name
                saved = source.read_bytes() if source.exists() else None
                if source.exists():
                    source.unlink()
                source.symlink_to(self.root / "absent-external-file")
                self.error("unsafe_path")
                source.unlink()
                if saved is not None:
                    source.write_bytes(saved)

    def test_symlink_before_parent_traversal_cannot_be_normalized_away(self):
        (self.data / "link").symlink_to(self.root, target_is_directory=True)
        self.error("unsafe_path", self.data / "link/../data")

    def test_wrong_path_type_and_orphan_wal_fail_without_creating_database(self):
        self.error("unsafe_path", self.root / "missing")
        self.error("unsafe_path", self.db)
        self.db.unlink()
        self.db.mkdir()
        self.error("unsafe_path")
        self.db.rmdir()
        (self.data / "logstream.sqlite3-wal").write_bytes(b"orphan")
        self.error("missing_logstream")
        self.assertFalse(self.db.exists())

    def test_no_database_replica_or_sidecar_bytes_or_modes_change(self):
        self.graph()
        self.data.chmod(0o750)
        self.db.chmod(0o640)
        before = self.image()
        mode = self.data.stat().st_mode
        self.verify()
        self.assertEqual(self.image(), before)
        self.assertEqual(self.data.stat().st_mode, mode)

    def test_committed_wal_is_read_without_checkpointing_or_creation(self):
        self.send(genesis())
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE events SET status = 'stored-in-wal'")
            writer.commit()
            self.send(create(2))
            stage = self.root / "staged"
            shutil.copytree(self.data, stage)
            before = self.image(stage)
            self.assertGreater(len(before["logstream.sqlite3-wal"][0]), 0)
            result = self.verify(stage)
            self.assertEqual(result["authorities"][AUTHORITY]["task_count"], 1)
            self.assertEqual(self.image(stage), before)

    def test_wal_without_captured_shm_is_refused_without_reconstructing_files(self):
        self.send(genesis())
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE events SET status = 'stored-in-wal'")
            writer.commit()
            stage = self.root / "staged"
            shutil.copytree(self.data, stage)
            (stage / "logstream.sqlite3-shm").unlink()
            before = self.image(stage)
            self.assertGreater(len(before["logstream.sqlite3-wal"][0]), 0)
            self.error("unsafe_path", stage)
            self.assertEqual(self.image(stage), before)

    def test_settlement_before_proposal_does_not_invent_a_task(self):
        self.send(genesis())
        proposal = make_proposal(self.log, create(2), NOW)
        self.append(make_settlement(proposal))
        self.append(proposal)
        authority = self.verify()["authorities"][AUTHORITY]
        self.assertEqual(authority["domain_ordinal"], 1)
        self.assertEqual(authority["raw_record_count"], 3)
        self.assertEqual(authority["tasks"], {})

    def test_closed_wal_mode_database_does_not_get_new_sidecars(self):
        self.send(genesis())
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("PRAGMA journal_mode=WAL")
        self.assertFalse((self.data / "logstream.sqlite3-wal").exists())
        before = self.image()
        self.verify()
        self.assertEqual(self.image(), before)

    def test_each_verification_uses_one_read_transaction_for_all_tables(self):
        self.graph()
        self.artifact()
        self.sql("INSERT INTO event_artifacts VALUES ('evt-000001', 'art-source')")
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("UPDATE events SET status = 'stored-in-wal'")
            writer.commit()
            original_connect = sqlite3.connect
            traces = []
            changed = []

            def connect(*args, **kwargs):
                self.assertIn("mode=ro", args[0])
                con = original_connect(*args, **kwargs)

                def trace(statement):
                    traces.append(statement)
                    if ("FROM artifacts" in statement and not changed
                            and not statement.lstrip().startswith("PRAGMA")):
                        self.assertTrue(con.in_transaction)
                        changed.append(True)
                        writer.execute("DELETE FROM artifacts")
                        writer.commit()

                con.set_trace_callback(trace)
                return con

            with patch("mempalace_tasks.snapshot.sqlite3.connect", side_effect=connect):
                result = self.verify()
            self.assertTrue(changed)
            self.assertEqual(result["artifact_count"], 1)
            self.assertEqual(result["artifact_link_count"], 1)
            self.assertIn("PRAGMA query_only = ON", traces)
            self.assertEqual(traces.count("BEGIN"), 1)
        self.error("invalid_artifact_link")

    def test_snapshot_row_limit_is_an_error_not_partial_history(self):
        self.graph()
        snapshot = importlib.import_module("mempalace_tasks.snapshot")
        with patch.object(snapshot, "MAX_SNAPSHOT_ROWS", 3):
            self.error("size_limit")


if __name__ == "__main__":
    unittest.main()
