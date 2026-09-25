from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import multiprocessing
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.platform_support import LifetimeLock, PlatformError
from mempalace_tasks.protocol import make_proposal, make_settlement
from authority_fixture import (
    AUTHORITY, Clock, LogClient, command, create, genesis, state_directory, uid,
)


def probe_inherited_authority(authority, channel):
    outcomes = []
    for action in (authority.health, authority.close, lambda: authority.execute(create())):
        try:
            action()
        except AuthorityError as error:
            outcomes.append(error.code)
    channel.send(outcomes)
    channel.close()


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.client = LogClient()
        self.clock = Clock()
        self.authority = TaskAuthority(AUTHORITY, self.client, self.directory.name,
                                       clock=self.clock, backoff=lambda _: None, initialize=True)
        self.authority.start()
        self.addCleanup(self.authority.close)
        self.send(genesis())

    def send(self, value):
        return self.authority.execute(value, expected_epoch=self.authority.epoch_id)

    def task(self, number=2):
        return self.send(create(number))["tasks"][0]

    def claim(self, task, number=3, worker="worker"):
        return command(number, "claim", worker, task_id=task["id"],
                       expected_version=task["version"], supervisor_id="supervisor")

    def test_committed_receipt_is_durable_and_retry_keeps_original_response(self):
        result = self.send(create())
        self.assertTrue(result["ok"])
        self.assertFalse(result["replayed"])
        self.assertEqual(result["ordinal"], 2)
        self.assertEqual(result["event_id"], "evt-000003")
        task = result["tasks"][0]
        self.send(command(3, "update", task_id=task["id"],
                                       expected_version=1, patch={"title": "New title"}))
        replay = self.send(create())
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], result["response"])
        self.assertEqual(replay["tasks"][0]["title"], "Task 2")
        self.assertEqual(self.authority.get(task["id"])["task"]["title"], "New title")
        self.assertEqual(len(self.client.sent), 4)

    def test_same_id_changed_content_and_stale_command_never_dispatch(self):
        task = self.task()
        before = len(self.client.sent)
        changed = create()
        changed["title"] = "Changed"
        with self.assertRaises(AuthorityError) as caught:
            self.send(changed)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        with self.assertRaises(AuthorityError) as caught:
            self.send(command(3, "update", task_id=task["id"],
                                           expected_version=99, patch={"title": "Changed"}))
        self.assertEqual(caught.exception.code, "version_conflict")
        self.assertEqual(len(self.client.sent), before)

    def test_detached_state_log_receipts_and_snapshot_cannot_mutate_authority(self):
        result = self.send(create())
        tid = result["tasks"][0]["id"]
        result["tasks"][0]["title"] = "Receipt tamper"
        self.authority.state.tasks[tid]["title"] = "State tamper"
        self.authority.log.state.tasks[tid]["title"] = "Log tamper"
        snapshot = self.authority.snapshot()
        snapshot["rows"][0]["title"] = "Snapshot tamper"
        self.assertEqual(self.authority.get(tid)["task"]["title"], "Task 2")
        self.assertEqual(self.send(create())["tasks"][0]["title"], "Task 2")

    def test_old_claim_receipt_does_not_authorize_expired_generation(self):
        task = self.task()
        original = self.claim(task)
        result = self.send(original)
        self.assertTrue(result["authorization"]["tasks"][0]["lease_live"])
        self.assertFalse(result["authorization"]["tasks"][0]["authorized"])
        self.clock.value = "2026-09-23T00:05:00Z"
        retry = self.send(original)
        self.assertEqual(retry["response"], result["response"])
        self.assertFalse(retry["authorization"]["tasks"][0]["lease_live"])
        self.assertFalse(retry["authorization"]["tasks"][0]["authorized"])
        self.assertEqual(self.authority.get(task["id"])["task"]["status"], "in_progress")
        self.assertEqual(self.authority.ready()["tasks"], [])

    def test_two_threads_competing_claims_produce_one_generation(self):
        task = self.task()
        barrier = threading.Barrier(2)

        def claim(number, worker):
            barrier.wait(timeout=5)
            try:
                return self.send(self.claim(task, number, worker))
            except AuthorityError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(claim, 3, "worker")
            b = pool.submit(claim, 4, "worker2")
            results = [a.result(timeout=10), b.result(timeout=10)]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn("version_conflict", results)
        self.assertEqual(self.authority.state.tasks[task["id"]]["claim_generation"], 1)
        self.assertEqual(len(self.client.sent), 4)

    def test_snapshots_are_stable_paginated_and_summary_covers_entire_scope(self):
        self.task(2)
        self.task(3)
        self.task(4)
        first = self.authority.snapshot({"project": "demo"}, limit=1)
        self.assertEqual(first["summary"]["total"], 3)
        self.assertEqual(first["summary"]["ready"], 3)
        self.task(5)
        second = self.authority.snapshot(limit=2, cursor=first["next_cursor"])
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["domain_head"], first["domain_head"])
        self.assertEqual(second["summary"]["total"], 3)
        self.assertFalse(second["fresh"])
        self.assertEqual(len(second["rows"]), 2)
        self.assertEqual(len({row["id"] for row in first["rows"] + second["rows"]}), 3)
        self.assertIsNone(second["next_cursor"])

    def test_cursor_expiry_and_tampering_are_explicit(self):
        self.task(2)
        self.task(3)
        with patch("mempalace_tasks.authority.time.monotonic", return_value=100):
            first = self.authority.snapshot(limit=1)
        with self.assertRaises(AuthorityError):
            self.authority.snapshot(cursor=first["next_cursor"] + "x")
        with patch("mempalace_tasks.authority.time.monotonic", return_value=160):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.snapshot(cursor=first["next_cursor"])
        self.assertEqual(caught.exception.code, "snapshot_expired")

    def test_unknown_filters_limits_and_task_ids_fail_explicitly(self):
        for filters in ({"typo": "x"}, {"needs_attention": 1}, {"status": "bogus"}):
            with self.subTest(filters=filters), self.assertRaises(AuthorityError):
                self.authority.snapshot(filters)
        for limit in (0, 501, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(AuthorityError):
                self.authority.snapshot(limit=limit)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.get("tsk_missing")
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(AuthorityError):
            self.authority.history("tsk_missing")

    def test_history_fixed_upper_raw_sequence_and_duplicate_labels(self):
        task = self.task()
        self.send(command(3, "note", task_id=task["id"], expected_version=1,
                                       tag="note", text="Observed"))
        self.client.append_event(self.client.sent[2])
        first = self.authority.history(task["id"], limit=1)
        self.assertEqual(first["upper_record_seq"], 5)
        self.send(command(4, "note", task_id=task["id"], expected_version=2,
                                       tag="note", text="Later"))
        second = self.authority.history(task["id"], limit=100, cursor=first["next_cursor"])
        self.assertEqual(second["upper_record_seq"], 5)
        self.assertEqual([row["disposition"] for row in second["rows"]], ["accepted", "duplicate"])
        bounded = self.authority.history(task["id"], after_record_seq=5)
        self.assertEqual([row["record_seq"] for row in bounded["rows"]], [6])

    def test_unavailable_snapshot_is_noncurrent_but_ready_rejects(self):
        self.task()
        self.client.read_error = True
        snapshot = self.authority.snapshot()
        self.assertFalse(snapshot["fresh"])
        self.assertEqual(len(snapshot["rows"]), 1)
        self.assertEqual(snapshot["reason"], "upstream_unavailable")
        with self.assertRaises(AuthorityError):
            self.authority.ready()

    def test_payload_limit_is_checked_before_pending_or_append(self):
        before = len(self.client.sent)
        with patch("mempalace_tasks.protocol.MAX_PAYLOAD_BYTES", 2500):
            with self.assertRaises(AuthorityError) as caught:
                self.task()
        self.assertEqual(caught.exception.code, "payload_too_large")
        self.assertEqual(len(self.client.sent), before)
        self.assertIsNone(self.authority.health()["pending_command"])

    def test_no_implicit_genesis_and_closed_authority_rejects_mutation(self):
        with state_directory() as directory:
            authority = TaskAuthority(AUTHORITY, LogClient(), directory, clock=self.clock)
            with self.assertRaises(AuthorityError) as caught:
                authority.start()
            self.assertEqual(caught.exception.code, "not_initialized")
            authority = TaskAuthority(AUTHORITY, LogClient(), directory, clock=self.clock,
                                      initialize=True)
            with authority:
                self.assertFalse(authority.health()["fresh"])
                self.assertEqual(authority.health()["reason"], "uninitialized")
                with self.assertRaises(AuthorityError):
                    authority.execute(create())
                self.assertEqual(authority.state.tasks, {})
            with self.assertRaises(AuthorityError) as caught:
                authority.execute(genesis())
            self.assertEqual(caught.exception.code, "not_started")

    def test_forked_child_cannot_use_or_release_parent_authority(self):
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=probe_inherited_authority, args=(self.authority, child))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), ["wrong_process"] * 3)
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
            with self.assertRaises(PlatformError):
                LifetimeLock(Path(self.directory.name) / "authority.lock").acquire()
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            parent.close()

    def test_snapshot_retention_is_bounded_and_cursor_is_query_bound(self):
        self.task(2)
        self.task(3)
        first = self.authority.snapshot(limit=1)
        with self.assertRaises(AuthorityError):
            self.authority.snapshot({"project": "other"}, cursor=first["next_cursor"])
        with self.assertRaises(AuthorityError):
            self.authority.history(first["rows"][0]["id"], cursor=first["next_cursor"])
        for _ in range(100):
            self.authority.snapshot(limit=1)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.snapshot(cursor=first["next_cursor"])
        self.assertEqual(caught.exception.code, "snapshot_expired")

    def test_ready_accepts_common_filters_without_ignoring_unknown_fields(self):
        task = self.task()
        ready = self.authority.ready({"project": "demo", "status": "open", "kind": "task",
                                      "needs_attention": False, "execution_profile": "local",
                                      "priority_ceiling": 2, "limit": 1})
        self.assertEqual([row["id"] for row in ready["tasks"]], [task["id"]])
        self.assertEqual(self.authority.ready({"kind": "epic"})["tasks"], [])
        self.assertEqual(self.authority.ready({"assignee": "worker"})["tasks"], [])
        with self.assertRaises(AuthorityError):
            self.authority.ready({"ignored": True})

    def test_old_successful_renewal_retains_receipt_but_not_revoked_authorization(self):
        task = self.send(self.claim(self.task()))["tasks"][0]
        task = self.send(command(
            4, "attempt_report", "supervisor", task_id=task["id"], expected_version=task["version"],
            attempt_id=task["attempt"]["id"], claim_generation=1, report_kind="started",
            evidence={"prepared": True, "references": ["workspace:1"]}))["tasks"][0]
        renewal = command(5, "renew", "supervisor", task_id=task["id"],
                          attempt_id=task["attempt"]["id"], claim_generation=1,
                          expected_lease_revision=task["lease_revision"])
        receipt = self.send(renewal)
        self.assertTrue(receipt["authorization"]["tasks"][0]["authorized"])
        current = receipt["tasks"][0]
        self.send(command(
            6, "release", "worker", task_id=task["id"], expected_version=current["version"],
            attempt_id=task["attempt"]["id"], claim_generation=1, reason="Yield safely"))
        replay = self.send(renewal)
        self.assertEqual(replay["response"], receipt["response"])
        self.assertFalse(replay["authorization"]["tasks"][0]["authorized"])
        self.assertFalse(replay["authorization"]["tasks"][0]["lease_live"])

    def test_growing_renewal_history_remains_consistent_under_full_prefix_audits(self):
        task = self.send(self.claim(self.task()))["tasks"][0]
        task = self.send(command(
            4, "attempt_report", "supervisor", task_id=task["id"], expected_version=task["version"],
            attempt_id=task["attempt"]["id"], claim_generation=1, report_kind="started",
            evidence={"prepared": True, "references": ["workspace:1"]}))["tasks"][0]
        self.client.replay_cursors.clear()
        self.client.records_read = 0
        started = time.perf_counter()
        for index in range(32):
            task = self.send(command(
                10 + index, "renew", "supervisor", task_id=task["id"],
                attempt_id=task["attempt"]["id"], claim_generation=1,
                expected_lease_revision=task["lease_revision"]))["tasks"][0]
            self.assertTrue(self.authority.snapshot()["fresh"])
            self.assertEqual(self.authority.ready()["tasks"], [])
            self.assertEqual(self.authority.get(task["id"])["task"]["lease_revision"],
                             task["lease_revision"])
        elapsed = time.perf_counter() - started
        if os.environ.get("MPTASK_TEST_METRICS") == "1":
            print(f"authority_tail_metrics records={self.client.records_read} "
                  f"replay_calls={len(self.client.replay_cursors)} elapsed={elapsed:.6f}s")
        self.assertGreater(self.client.records_read, 32)
        self.assertEqual(len(self.client.replay_cursors), 160)
        self.assertEqual(set(self.client.replay_cursors), {None})

    def test_healthy_repeated_reads_revalidate_source_without_writes(self):
        task = self.task()
        self.client.replay_cursors.clear()
        self.client.records_read = 0
        for _ in range(10):
            self.authority.snapshot()
            self.authority.history(task["id"])
            self.authority.refresh()
            self.authority.reconcile()
        self.assertEqual(self.client.records_read, 120)
        self.assertEqual(self.client.replay_cursors, [None] * 40)

    def test_repeated_operation_prefixes_reject_without_disk_or_log_effect(self):
        for count in (2, 3, 4):
            with self.subTest(count=count), state_directory() as directory:
                client = LogClient()
                authority = TaskAuthority(AUTHORITY, client, directory, clock=self.clock,
                                          backoff=lambda _: None, initialize=True)
                with authority:
                    authority.execute_current(genesis())
                    before_head = authority.log.checkpoint()
                    bad = create()
                    bad["operation"] = "mptask_" * count + "create"
                    with self.assertRaises(AuthorityError) as caught:
                        authority.execute(bad, expected_epoch=authority.epoch_id)
                    self.assertEqual(caught.exception.code, "validation_error")
                    self.assertFalse(caught.exception.ambiguous)
                    self.assertEqual(len(client.sent), 2)
                    self.assertFalse(Path(directory, "pending.json").exists())
                    self.assertEqual(authority.log.checkpoint(), before_head)
                    self.assertEqual(authority.state.tasks, {})
                    valid = create()
                    valid["operation"] = "mptask_create"
                    self.assertTrue(authority.execute(valid, expected_epoch=authority.epoch_id)["ok"])
                    self.assertTrue(authority.execute(create(), expected_epoch=authority.epoch_id)["replayed"])
                with authority:
                    self.assertEqual(len(authority.state.tasks), 1)

    def test_generated_envelope_is_statically_validated_before_pending_or_dispatch(self):
        valid = make_proposal(self.authority.log, create(), self.clock())["event"]
        valid["command"]["operation"] = "mptask_create"
        head = self.authority.log.checkpoint()
        with patch("mempalace_tasks.protocol.decide", return_value=valid):
            with self.assertRaises(AuthorityError) as caught:
                self.send(create())
        self.assertEqual(caught.exception.code, "invariant_violation")
        self.assertFalse(caught.exception.ambiguous)
        self.assertEqual(len(self.client.sent), 2)
        self.assertFalse(Path(self.directory.name, "pending.json").exists())
        self.assertEqual(self.authority.log.checkpoint(), head)

    def test_get_attempt_authorization_tracks_preparing_running_and_expiry_read_only(self):
        task = self.send(self.claim(self.task()))["tasks"][0]
        before = len(self.client.sent)
        preparing = self.authority.get(task["id"])
        auth = preparing["authorization"]
        self.assertFalse(auth["authorized"])
        self.assertTrue(auth["lease_live"])
        self.assertEqual(auth["reason"], "not_running")
        self.assertEqual(auth["as_of"], preparing["as_of"])
        self.assertEqual(auth["fresh"], preparing["fresh"])
        self.assertEqual(auth["attempt_id"], task["attempt"]["id"])
        self.assertEqual(auth["claim_generation"], task["claim_generation"])
        self.assertIn("eligibility", preparing)
        self.assertIn("relations", preparing)
        self.assertEqual(len(self.client.sent), before)

        started = self.send(command(
            4, "attempt_report", "supervisor", task_id=task["id"], expected_version=task["version"],
            attempt_id=task["attempt"]["id"], claim_generation=1, report_kind="started",
            evidence={"prepared": True, "references": ["workspace:1"]}))
        before = len(self.client.sent)
        head = self.authority.log.checkpoint()
        running = self.authority.get(task["id"])
        auth = running["authorization"]
        self.assertTrue(auth["authorized"])
        self.assertFalse(running["eligibility"]["ready"])
        self.assertEqual({key: auth[key] for key in started["authorization"]["tasks"][0]},
                         started["authorization"]["tasks"][0])
        self.assertEqual(auth["as_of"], running["as_of"])
        self.assertEqual(auth["fresh"], running["fresh"])

        self.clock.value = "2026-09-23T00:05:00Z"
        expired = self.authority.get(task["id"])
        self.assertTrue(expired["fresh"])
        self.assertFalse(expired["authorization"]["authorized"])
        self.assertFalse(expired["authorization"]["lease_live"])
        self.assertEqual(expired["authorization"]["reason"], "lease_expired")
        self.assertEqual(expired["task"]["status"], "in_progress")
        self.assertEqual(len(self.client.sent), before)
        self.assertEqual(self.authority.log.checkpoint(), head)
        self.assertFalse(Path(self.directory.name, "pending.json").exists())

    def test_get_attempt_authorization_is_false_on_stale_unverified_and_restarted_views(self):
        task = self.send(self.claim(self.task()))["tasks"][0]
        task = self.send(command(
            4, "attempt_report", "supervisor", task_id=task["id"], expected_version=task["version"],
            attempt_id=task["attempt"]["id"], claim_generation=1, report_kind="started",
            evidence={"prepared": True, "references": ["workspace:1"]}))["tasks"][0]
        self.client.read_error = True
        stale = self.authority.get(task["id"])
        self.assertFalse(stale["fresh"])
        self.assertFalse(stale["authorization"]["fresh"])
        self.assertFalse(stale["authorization"]["authorized"])
        self.assertFalse(stale["authorization"]["lease_live"])
        self.assertEqual(stale["authorization"]["reason"], stale["reason"])
        self.assertEqual(stale["authorization"]["as_of"], stale["as_of"])

        self.client.read_error = False
        self.client.events[-1]["created_at"] = "2026-09-23T00:00:01Z"
        unverified = self.authority.get(task["id"])
        self.assertEqual(unverified["reason"], "log_rollback")
        self.assertFalse(unverified["authorization"]["authorized"])
        self.assertEqual(unverified["authorization"]["reason"], "log_rollback")
        self.client.events[-1]["created_at"] = "2026-09-23T00:00:00Z"

        self.authority.close()
        self.authority = TaskAuthority(AUTHORITY, self.client, self.directory.name,
                                       clock=Clock(), backoff=lambda _: None).start()
        self.addCleanup(self.authority.close)
        restored = self.authority.get(task["id"])
        self.assertEqual(restored["task"]["status"], "recovering")
        self.assertFalse(restored["authorization"]["authorized"])
        self.assertFalse(restored["authorization"]["lease_live"])

    def test_accepted_feed_excludes_controls_duplicates_and_abandoned_proposals(self):
        self.task()
        accepted = self.client.sent[-1]
        self.client.append_event(accepted)
        self.client.append_event(make_settlement(accepted))
        abandoned = make_proposal(self.authority.log, create(3), self.clock())
        self.client.append_event(make_settlement(abandoned))
        self.authority.refresh()
        self.task(4)
        self.client.append_event(abandoned)
        self.authority.refresh()
        first = self.authority.accepted_records(limit=2)
        self.assertEqual([item["ordinal"] for item in first], [1, 2])
        self.assertEqual([item["event_id"] for item in first], ["evt-000002", "evt-000003"])
        self.assertTrue(all(set(item) == {"event_id", "ordinal", "event"} for item in first))
        second = self.authority.accepted_records(after_ordinal=2, limit=1)
        self.assertEqual([item["ordinal"] for item in second], [3])
        self.assertEqual(second[0]["event_id"], "evt-000007")
        self.assertEqual(second[0]["event"]["command"]["command_id"], uid(4))
        self.assertEqual(self.authority.accepted_records(after_ordinal=3), [])
        self.assertEqual(self.authority.accepted_records(after_ordinal=500), [])
        self.assertEqual(self.authority.log.domain_ordinal, 3)
        self.assertEqual(len(self.authority.log.history), 8)
        before = first + second
        self.authority.close()
        self.authority.start()
        self.assertEqual(self.authority.accepted_records(), before)

    def test_accepted_feed_copies_only_requested_batch_and_never_scans_history(self):
        for number in range(2, 12):
            self.task(number)
        head = self.authority.log.checkpoint()
        calls = len(self.client.sent), len(self.client.replay_cursors)

        class NoHistoryAccess:
            def __iter__(self):
                raise AssertionError("Historical scan")

            def __getitem__(self, key):
                raise AssertionError("Historical lookup")

            def __deepcopy__(self, memo):
                raise AssertionError("Historical deep copy")

        with patch.object(self.authority._log, "history", NoHistoryAccess()), \
                patch("mempalace_tasks.authority.deepcopy", wraps=deepcopy) as copying:
            batch = self.authority.accepted_records(after_ordinal=8, limit=2)
            self.assertEqual([item["ordinal"] for item in batch], [9, 10])
            self.assertEqual(copying.call_count, 1)
            self.assertEqual(len(copying.call_args.args[0]), 2)
        with patch.object(self.authority._log, "accepted_records", NoHistoryAccess()), \
                patch("mempalace_tasks.authority.deepcopy",
                      side_effect=AssertionError("Idle copy")), \
                patch.object(self.authority.clock_source, "now", side_effect=AssertionError("Feed clock I/O")):
            self.assertEqual(self.authority.accepted_records(after_ordinal=11), [])
            self.assertEqual(self.authority.accepted_records(after_ordinal=12), [])
        self.assertEqual((len(self.client.sent), len(self.client.replay_cursors)), calls)
        self.assertEqual(self.authority.log.checkpoint(), head)
        self.assertFalse(Path(self.directory.name, "pending.json").exists())

    def test_accepted_feed_returns_detached_events_without_affecting_truth(self):
        task = self.task()
        batch = self.authority.accepted_records(after_ordinal=1)
        batch[0]["event"]["tasks"][0]["title"] = "Tampered"
        batch[0]["event"]["command"]["title"] = "Tampered input"
        batch[0]["event_id"] = "Tampered event"
        again = self.authority.accepted_records(after_ordinal=1)
        self.assertEqual(again[0]["event"]["tasks"][0]["title"], "Task 2")
        self.assertEqual(again[0]["event"]["command"]["title"], "Task 2")
        self.assertEqual(again[0]["event_id"], "evt-000003")
        self.assertEqual(self.authority.state.tasks[task["id"]]["title"], "Task 2")
        self.assertEqual(self.send(create())["tasks"][0]["title"], "Task 2")

    def test_accepted_feed_validates_bounds_and_process_ownership(self):
        for after in (-1, True, "0", None, 1.5):
            with self.subTest(after=after):
                with self.assertRaises(AuthorityError) as caught:
                    self.authority.accepted_records(after_ordinal=after)
                self.assertEqual(caught.exception.code, "validation_error")
        for limit in (0, 501, True, "100", None):
            with self.subTest(limit=limit):
                with self.assertRaises(AuthorityError) as caught:
                    self.authority.accepted_records(limit=limit)
                self.assertEqual(caught.exception.code, "validation_error")
        with patch.object(self.authority, "_process", -1):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.accepted_records()
        self.assertEqual(caught.exception.code, "wrong_process")
        self.authority.close()
        with self.assertRaises(AuthorityError) as caught:
            self.authority.accepted_records()
        self.assertEqual(caught.exception.code, "not_started")


if __name__ == "__main__":
    unittest.main()
