from copy import deepcopy
import multiprocessing
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.journal import HeadStore, JournalError, JsonStore, PendingStore
from mempalace_tasks.leases import ClockError
from mempalace_tasks.palace import PalaceError
from mempalace_tasks.protocol import make_proposal, make_settlement
from authority_fixture import (
    AUTHORITY, NOW, Clock, LogClient, create, genesis, raw, state_directory, uid,
)


def crash_after_append(directory, events, log_path):
    client = LogClient()
    client.events = events
    authority = TaskAuthority(AUTHORITY, client, directory, clock=Clock(), backoff=lambda _: None)
    authority.start()

    def crash(payload):
        JsonStore(log_path).write(client.events)
        os._exit(42)

    client.on_append = crash
    authority.execute(create())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.client = LogClient()
        self.clock = Clock()
        self.delays = []
        self.authority = self.new_authority()
        self.authority.start()
        self.addCleanup(self.authority.close)
        self.authority.execute(genesis())

    def new_authority(self):
        return TaskAuthority(AUTHORITY, self.client, self.directory.name,
                             clock=self.clock, backoff=self.delays.append)

    def restart(self):
        self.authority.close()
        self.authority = self.new_authority()
        self.addCleanup(self.authority.close)
        return self.authority.start()

    def test_lost_original_response_settles_committed_without_original_retry(self):
        self.client.behaviors = ["commit_lost", "ok"]
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual([p["record_type"] for p in self.client.sent[1:]],
                         ["mptask.command", "mptask.settle"])
        self.assertEqual(result["event_id"], "evt-000002")
        self.assertIsNone(PendingStore(self.directory.name).read())
        self.assertEqual(HeadStore(self.directory.name).read()["raw_cursor"], "evt-000003")

    def test_lost_original_not_present_is_abandoned_only_by_ordered_control(self):
        self.client.behaviors = ["lost", "ok"]
        result = self.authority.execute(create())
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "abandoned")
        proposal = self.client.sent[1]
        self.assertEqual(self.authority.state.tasks, {})
        self.authority.execute(create(3))
        self.client.append_event(proposal)
        self.authority.refresh()
        self.assertEqual(len(self.authority.state.tasks), 1)
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")

    def test_lost_settlement_response_replays_committed_control(self):
        self.client.behaviors = ["lost", "commit_lost"]
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "abandoned")
        self.assertEqual(len(self.client.sent), 3)
        self.assertIsNone(PendingStore(self.directory.name).read())

    def test_uncertainty_is_bounded_persisted_and_later_control_bytes_identical(self):
        self.client.behaviors = ["lost", "lost", "lost", "lost"]
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(create())
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(self.client.sent), 5)
        controls = self.client.sent[2:]
        self.assertTrue(all(control == controls[0] for control in controls))
        pending = PendingStore(self.directory.name).read()
        self.assertEqual(pending["settlement"], controls[0])
        self.assertEqual(self.delays, [1, 2, 4])
        result = self.authority.reconcile()
        self.assertEqual(result["outcome"], "abandoned")
        self.assertEqual(self.client.sent[-1], controls[0])
        self.assertIsNone(PendingStore(self.directory.name).read())

    def test_snapshot_does_not_settle_clear_or_persist_pending(self):
        self.client.behaviors = ["lost", "lost", "lost", "lost"]
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        pending_path = Path(self.directory.name, "pending.json")
        head_path = Path(self.directory.name, "verified_head.json")
        before = pending_path.read_bytes(), head_path.read_bytes(), len(self.client.sent)
        snapshot = self.authority.snapshot()
        self.assertFalse(snapshot["fresh"])
        self.assertEqual(snapshot["reason"], "pending_command")
        self.assertEqual(before, (pending_path.read_bytes(), head_path.read_bytes(),
                                  len(self.client.sent)))
        with self.assertRaises(AuthorityError):
            self.authority.ready()
        self.assertEqual(len(self.client.sent), before[2])

    def test_restart_recovers_pending_before_dispatch_without_resending_original(self):
        proposal = make_proposal(self.authority.log, create(), NOW)
        PendingStore(self.directory.name).write(proposal)
        self.restart()
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")
        self.assertEqual([p["record_type"] for p in self.client.sent],
                         ["mptask.command", "mptask.settle"])

    def test_restart_after_committed_append_before_ack_recovers_original_receipt(self):
        proposal = make_proposal(self.authority.log, create(), NOW)
        PendingStore(self.directory.name).write(proposal)
        self.client.append_event(proposal)
        self.restart()
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["event_id"], "evt-000002")
        self.assertEqual(len(self.client.sent), 2)

    def test_pending_write_failure_dispatches_nothing(self):
        with patch.object(PendingStore, "write", side_effect=JournalError("io_error", "Disk")):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "io_error")
        self.assertEqual(len(self.client.sent), 1)

    def test_head_write_failure_after_commit_returns_error_and_retry_recovers(self):
        with patch.object(HeadStore, "write", side_effect=JournalError("io_error", "Disk")):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "io_error")
        self.assertIsNotNone(PendingStore(self.directory.name).read())
        self.assertEqual(self.authority.execute(create())["outcome"], "committed")
        self.assertEqual(len(self.client.sent), 2)

    def test_clear_failure_after_unlink_never_acknowledges_before_later_sync(self):
        original = PendingStore.clear

        def fail_after_clear(store):
            original(store)
            raise JournalError("io_error", "Directory fsync", ambiguous=True)

        with patch.object(PendingStore, "clear", fail_after_clear):
            with self.assertRaises(AuthorityError):
                self.authority.execute(create())
        self.assertFalse(self.authority.snapshot()["fresh"])
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(len(self.client.sent), 2)
        self.assertTrue(self.authority.health()["fresh"])

    def test_settlement_persistence_failure_never_dispatches_unjournaled_control(self):
        self.client.behaviors = ["lost"]
        with patch.object(PendingStore, "attach_settlement",
                          side_effect=JournalError("io_error", "Disk")):
            with self.assertRaises(AuthorityError):
                self.authority.execute(create())
        self.assertEqual(len(self.client.sent), 2)
        self.assertIsNotNone(PendingStore(self.directory.name).read())
        self.assertEqual(self.authority.reconcile()["outcome"], "abandoned")

    def test_saved_checkpoint_verified_at_exact_prefix_not_final_head(self):
        self.authority.execute(create())
        saved = HeadStore(self.directory.name).read()
        self.client.append_event(self.client.sent[1])
        self.restart()
        self.assertEqual(self.authority.log.domain_head, saved["domain_head"])
        self.assertEqual(self.authority.log.raw_cursor, "evt-000003")
        self.assertTrue(self.authority.health()["fresh"])

    def test_rollback_or_modified_prefix_refuses_start_and_releases_own_lock(self):
        self.authority.execute(create())
        saved_events = deepcopy(self.client.events)
        for mode in ("missing", "changed"):
            with self.subTest(mode=mode):
                self.authority.close()
                self.client.events = deepcopy(saved_events)
                if mode == "missing":
                    self.client.events.pop()
                else:
                    self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
                failed = self.new_authority()
                with self.assertRaises(AuthorityError) as caught:
                    failed.start()
                self.assertEqual(caught.exception.code, "log_rollback")
                self.client.events = deepcopy(saved_events)
                self.authority = self.new_authority()
                self.authority.start()
                self.addCleanup(self.authority.close)

    def test_live_refresh_rollback_retains_diagnostic_projection_but_not_freshness(self):
        self.authority.execute(create())
        self.client.events.pop()
        with self.assertRaises(AuthorityError) as caught:
            self.authority.refresh()
        self.assertEqual(caught.exception.code, "log_rollback")
        snapshot = self.authority.snapshot()
        self.assertEqual(len(snapshot["rows"]), 1)
        self.assertFalse(snapshot["fresh"])

    def test_corrupt_pending_and_head_are_not_defaults(self):
        self.authority.close()
        for filename in ("pending.json", "verified_head.json"):
            path = Path(self.directory.name, filename)
            saved = path.read_bytes() if path.exists() else None
            path.write_text("{broken", encoding="utf-8")
            failed = self.new_authority()
            with self.assertRaises(AuthorityError) as caught:
                failed.start()
            self.assertEqual(caught.exception.code, "corrupt_state")
            if saved is None:
                path.unlink()
            else:
                path.write_bytes(saved)

    def test_failed_start_uncertainty_keeps_pending_and_releases_owner(self):
        proposal = make_proposal(self.authority.log, create(), NOW)
        PendingStore(self.directory.name).write(proposal)
        self.authority.close()
        self.client.behaviors = ["lost"] * 3
        failed = self.new_authority()
        with self.assertRaises(AuthorityError) as caught:
            failed.start()
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIsNotNone(PendingStore(self.directory.name).read())
        self.authority = self.new_authority()
        self.addCleanup(self.authority.close)
        self.authority.start()
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")

    def test_missing_unresolved_pending_file_fails_closed_without_new_original(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        Path(self.directory.name, "pending.json").unlink()
        before = len(self.client.sent)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(create(3))
        self.assertEqual(caught.exception.code, "missing_state")
        self.assertEqual(len(self.client.sent), before)

    def test_known_pending_conflict_cannot_clear_or_serve_fresh_diagnostics(self):
        self.authority.execute(create())
        changed = create()
        changed["title"] = "different"
        # A separately valid proposal bearing a known ID is still corrupt metadata.
        from mempalace_tasks.protocol import LogState, fold_record
        base = LogState(AUTHORITY)
        fold_record(base, self.client.events[0])
        pending = make_proposal(base, changed, NOW)
        PendingStore(self.directory.name).write(pending)
        diagnostic = self.authority.snapshot()
        self.assertFalse(diagnostic["fresh"])
        with self.assertRaises(AuthorityError) as caught:
            self.authority.reconcile()
        self.assertEqual(caught.exception.code, "invariant_violation")
        self.assertIsNotNone(PendingStore(self.directory.name).read())

    def test_delayed_original_wins_if_inserted_before_control_and_never_reapplies(self):
        self.client.behaviors = ["lost", "ok"]
        original_append = self.client.append_event

        def append(payload):
            if payload["record_type"] == "mptask.settle":
                self.client.events.append(raw(self.client.sent[-1], 2))
            return original_append(payload)

        with patch.object(self.client, "append_event", side_effect=append):
            result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["event_id"], "evt-000002")
        self.assertEqual(self.authority.log.domain_ordinal, 2)
        self.assertEqual(len(self.authority.state.tasks), 1)

    def test_lost_control_response_and_read_failure_retry_only_identical_controls(self):
        self.client.behaviors = ["lost", "commit_lost", "commit_lost", "commit_lost"]
        original_append = self.client.append_event

        def append(payload):
            if payload["record_type"] == "mptask.settle":
                self.client.read_error = True
            return original_append(payload)

        with patch.object(self.client, "append_event", side_effect=append):
            with self.assertRaises(AuthorityError):
                self.authority.execute(create())
        self.client.read_error = False
        result = self.authority.reconcile()
        self.assertEqual(result["outcome"], "abandoned")
        self.assertEqual(self.authority.log.domain_ordinal, 1)
        self.assertEqual([row["disposition"] for row in self.authority.log.history],
                         ["accepted", "settled", "duplicate", "duplicate"])
        self.assertEqual(len(self.client.sent), 5)

    def test_real_pending_directory_fsync_failure_then_retry_never_resends_original(self):
        real_replace, real_fsync = os.replace, os.fsync
        fail_sync = False

        def replace(src, dst, **kwargs):
            nonlocal fail_sync
            result = real_replace(src, dst, **kwargs)
            fail_sync = dst == "pending.json"
            return result

        def fsync(fd):
            nonlocal fail_sync
            if fail_sync:
                fail_sync = False
                raise OSError("Injected pending directory fsync failure")
            return real_fsync(fd)

        with patch("mempalace_tasks.journal.os.replace", side_effect=replace), \
                patch("mempalace_tasks.journal.os.fsync", side_effect=fsync):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "io_error")
        self.assertEqual(len(self.client.sent), 1)
        self.assertIsNotNone(PendingStore(self.directory.name).read())
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "abandoned")
        self.assertEqual([p["record_type"] for p in self.client.sent],
                         ["mptask.command", "mptask.settle"])

    def test_actual_process_death_after_append_recovers_from_durable_pending(self):
        self.authority.close()
        log_path = str(Path(self.directory.name, "fixture-events.json"))
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=crash_after_append,
                                  args=(self.directory.name, self.client.events, log_path))
        process.start()
        try:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 42)
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        self.client.events = JsonStore(log_path).read()
        self.authority = self.new_authority()
        self.addCleanup(self.authority.close)
        self.authority.start()
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(result["event_id"], "evt-000002")
        self.assertEqual(len(self.client.events), 2)
        self.assertIsNone(PendingStore(self.directory.name).read())

    def test_changed_valid_pending_proposal_is_not_silently_adopted(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        alternative = make_proposal(self.authority.log, create(3), NOW)
        JsonStore(Path(self.directory.name, "pending.json")).write(
            {"proposal": alternative, "settlement": None})
        before = len(self.client.sent)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.reconcile()
        self.assertEqual(caught.exception.code, "invariant_violation")
        self.assertEqual(len(self.client.sent), before)

    def test_real_head_replace_fsync_failure_never_acknowledges_then_retries_safely(self):
        real_replace, real_fsync = os.replace, os.fsync
        fail_sync = False

        def replace(src, dst, **kwargs):
            nonlocal fail_sync
            result = real_replace(src, dst, **kwargs)
            fail_sync = dst == "verified_head.json"
            return result

        def fsync(fd):
            nonlocal fail_sync
            if fail_sync:
                fail_sync = False
                raise OSError("Injected head directory fsync failure")
            return real_fsync(fd)

        with patch("mempalace_tasks.journal.os.replace", side_effect=replace), \
                patch("mempalace_tasks.journal.os.fsync", side_effect=fsync):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "io_error")
        self.assertIsNotNone(PendingStore(self.directory.name).read())
        self.assertEqual(len(self.client.sent), 2)
        result = self.authority.execute(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(len(self.client.sent), 2)

    def test_post_clear_clock_error_preserves_explicit_ambiguity_and_original_id(self):
        cleared = False
        clear = PendingStore.clear

        def clear_then_fail_clock(store):
            nonlocal cleared
            clear(store)
            cleared = True

        def clock():
            if cleared:
                raise ClockError("clock_state_changed", "Clock state changed")
            return NOW

        with patch.object(PendingStore, "clear", clear_then_fail_clock), \
                patch.object(self.authority, "_clock", side_effect=clock):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "clock_state_changed")
        self.assertTrue(caught.exception.ambiguous)
        self.assertTrue(caught.exception.to_dict()["ambiguous"])
        self.assertIsNone(PendingStore(self.directory.name).read())
        replay = self.authority.execute(create())
        self.assertEqual(replay["outcome"], "committed")
        self.assertEqual(replay["command_id"], uid(2))
        self.assertEqual(len(self.client.sent), 2)

    def test_definitive_input_error_does_not_inherit_another_commands_ambiguity(self):
        self.authority.execute(create())
        changed = create()
        changed["title"] = "Conflict"
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(changed)
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertFalse(caught.exception.ambiguous)
        self.assertFalse(caught.exception.to_dict()["ambiguous"])

    def test_unresolved_settlement_error_has_explicit_ambiguity(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(create())
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertTrue(caught.exception.ambiguous)

    def test_retry_clock_failure_does_not_forget_previously_committed_identity(self):
        self.authority.execute(create())
        with patch.object(self.authority, "_clock",
                          side_effect=ClockError("clock_state_changed", "Clock state changed")):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertTrue(caught.exception.ambiguous)

    def test_historical_rewrite_is_detected_by_explicit_full_audit_not_healthy_tail(self):
        self.authority.execute(create())
        self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
        self.client.replay_cursors.clear()
        self.assertTrue(self.authority.refresh()["fresh"])
        self.assertEqual(self.client.replay_cursors, ["evt-000002"])
        with self.assertRaises(AuthorityError) as caught:
            self.authority.refresh(verify_prefix=True)
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertEqual(self.client.replay_cursors[-1], None)

    def test_disconnection_requires_full_prefix_verification_before_resuming_tail(self):
        self.authority.execute(create())
        self.client.read_error = True
        self.assertFalse(self.authority.snapshot()["fresh"])
        self.client.read_error = False
        self.client.replay_cursors.clear()
        self.assertTrue(self.authority.refresh()["fresh"])
        self.assertEqual(self.client.replay_cursors, [None])
        self.authority.refresh()
        self.assertEqual(self.client.replay_cursors[-1], "evt-000002")

    def test_disconnected_historical_tampering_fails_closed_on_recovery(self):
        self.authority.execute(create())
        self.client.read_error = True
        self.assertFalse(self.authority.snapshot()["fresh"])
        self.client.read_error = False
        self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
        with self.assertRaises(AuthorityError) as caught:
            self.authority.refresh()
        self.assertEqual(caught.exception.code, "log_rollback")

    def test_changed_valid_local_checkpoint_fails_before_tail_or_new_dispatch(self):
        self.authority.execute(create())
        head = HeadStore(self.directory.name).read()
        head["raw_hash"] = "0" * 64
        HeadStore(self.directory.name).write(head)
        self.client.replay_cursors.clear()
        before = len(self.client.sent)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(create(3))
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertEqual(self.client.replay_cursors, [])
        self.assertEqual(len(self.client.sent), before)

    def test_cursor_loss_never_falls_back_to_empty_or_fresh_state(self):
        self.authority.execute(create())
        self.client.events.clear()
        self.client.replay_cursors.clear()
        with self.assertRaises(AuthorityError) as caught:
            self.authority.refresh()
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertEqual(self.client.replay_cursors, ["evt-000002"])
        self.assertEqual(len(self.authority.state.tasks), 1)
        self.assertFalse(self.authority.health()["fresh"])

    def test_uncertain_append_recovery_audits_full_prefix(self):
        self.client.replay_cursors.clear()
        self.client.behaviors = ["commit_lost", "ok"]
        self.assertEqual(self.authority.execute(create())["outcome"], "committed")
        self.assertEqual(self.client.replay_cursors, ["evt-000001", None])

    def test_partial_tail_failure_keeps_coherent_noncurrent_prefix_and_forces_audit(self):
        proposal = make_proposal(self.authority.log, create(), NOW)
        self.client.append_event(proposal)
        original = self.client.replay_events

        def partial(cursor=None):
            yield from original(cursor)
            raise PalaceError("upstream_unavailable", "Final page unavailable")

        with patch.object(self.client, "replay_events", side_effect=partial):
            with self.assertRaises(AuthorityError):
                self.authority.refresh()
        self.assertFalse(self.authority.health()["fresh"])
        self.client.replay_cursors.clear()
        self.authority.refresh()
        self.assertEqual(self.client.replay_cursors, [None])
        self.assertEqual(len(self.authority.state.tasks), 1)
        self.assertEqual(self.authority.log.domain_ordinal, 2)

    def test_local_checkpoint_change_during_append_is_not_overwritten_or_acknowledged(self):
        def change_checkpoint(payload):
            head = HeadStore(self.directory.name).read()
            head["raw_hash"] = "0" * 64
            HeadStore(self.directory.name).write(head)

        self.client.on_append = change_checkpoint
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(create())
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(HeadStore(self.directory.name).read()["raw_hash"], "0" * 64)
        self.assertIsNotNone(PendingStore(self.directory.name).read())

    def test_local_checkpoint_rollback_to_valid_older_prefix_still_fails_closed(self):
        old = HeadStore(self.directory.name).read()
        self.authority.execute(create())
        HeadStore(self.directory.name).write(old)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.refresh(verify_prefix=True)
        self.assertEqual(caught.exception.code, "log_rollback")

    def test_tail_prefix_hash_matches_full_audit_after_duplicate_and_control(self):
        self.authority.execute(create())
        proposal = self.client.sent[-1]
        self.client.append_event(proposal)
        self.client.append_event(make_settlement(proposal))
        self.authority.refresh()
        tail = self.authority.log.checkpoint()
        self.authority.refresh(verify_prefix=True)
        self.assertEqual(self.authority.log.checkpoint(), tail)
        self.assertEqual(tail["domain_head"], "evt-000002")
        self.assertEqual(tail["raw_cursor"], "evt-000004")

    def test_checkpoint_change_after_tail_read_is_not_overwritten_by_head_persistence(self):
        changed = False

        def clock():
            nonlocal changed
            if len(self.client.events) == 2 and not changed:
                head = HeadStore(self.directory.name).read()
                head["raw_hash"] = "0" * 64
                HeadStore(self.directory.name).write(head)
                changed = True
            return NOW

        with patch.object(self.authority, "_clock", side_effect=clock):
            with self.assertRaises(AuthorityError) as caught:
                self.authority.execute(create())
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(HeadStore(self.directory.name).read()["raw_hash"], "0" * 64)

    def test_newly_discovered_pending_recovery_requires_full_prefix_audit(self):
        self.authority.execute(create())
        PendingStore(self.directory.name).write(self.client.sent[-1])
        self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
        with self.assertRaises(AuthorityError) as caught:
            self.authority.reconcile()
        self.assertEqual(caught.exception.code, "log_rollback")
        self.assertIsNotNone(PendingStore(self.directory.name).read())

    def test_reacquiring_after_another_valid_owner_reloads_checkpoint_under_full_audit(self):
        self.authority.close()
        other = self.new_authority()
        with other:
            other.execute(create())
        self.client.replay_cursors.clear()
        self.authority.start()
        self.assertEqual(self.client.replay_cursors, [None])
        self.assertEqual(len(self.authority.state.tasks), 1)
        self.assertEqual(self.authority.log.domain_ordinal, 2)

    def test_reacquiring_accepts_pending_cleared_by_next_owner_only_after_terminal_audit(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        self.authority.close()
        with self.new_authority() as other:
            self.assertEqual(other.execute(create())["outcome"], "abandoned")
        self.assertIsNone(PendingStore(self.directory.name).read())
        self.client.replay_cursors.clear()
        self.authority.start()
        self.assertEqual(self.client.replay_cursors, [None])
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")
        self.assertTrue(self.authority.execute(create(3))["ok"])

    def test_reacquiring_accepts_replacement_pending_only_after_prior_terminal_audit(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        self.authority.close()
        with self.new_authority() as other:
            self.client.behaviors = ["lost"] * 4
            with self.assertRaises(AuthorityError):
                other.execute(create(3))
        self.assertEqual(PendingStore(self.directory.name).read()["proposal"]["command_id"], uid(3))
        self.authority.start()
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")
        self.assertEqual(self.authority.execute(create(3))["outcome"], "abandoned")

    def test_reacquiring_still_rejects_missing_or_replaced_unresolved_pending(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        alternative = make_proposal(self.authority.log, create(3), NOW)
        old = PendingStore(self.directory.name).read()
        self.authority.close()
        for pending in (None, {"proposal": alternative, "settlement": None}):
            with self.subTest(replacement=pending is not None):
                store = JsonStore(Path(self.directory.name, "pending.json"))
                if pending is None:
                    store.clear()
                else:
                    store.write(pending)
                before = len(self.client.sent)
                with self.assertRaises(AuthorityError):
                    self.authority.start()
                self.assertEqual(len(self.client.sent), before)
                store.write(old)
        self.authority.start()
        self.assertEqual(self.authority.execute(create())["outcome"], "abandoned")

    def test_reacquiring_after_next_owner_confirms_committed_pending_receipt(self):
        self.client.behaviors = ["commit_lost", "lost", "lost", "lost"]

        def disconnect(payload):
            self.client.read_error = True

        self.client.on_append = disconnect
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        self.authority.close()
        self.client.read_error = False
        self.client.on_append = None
        with self.new_authority() as other:
            confirmed = other.execute(create())
        self.authority.start()
        replay = self.authority.execute(create())
        self.assertEqual(replay["outcome"], "committed")
        self.assertEqual(replay["response"], confirmed["response"])
        self.assertEqual(replay["event_id"], "evt-000002")
        self.assertEqual(sum(payload["record_type"] == "mptask.command"
                             and payload["command_id"] == uid(2) for payload in self.client.sent), 1)

    def test_reacquisition_cannot_discard_prior_pending_for_mismatched_terminal_identity(self):
        self.client.behaviors = ["lost"] * 4
        with self.assertRaises(AuthorityError):
            self.authority.execute(create())
        changed = create()
        changed["title"] = "Different content under same ID"
        conflicting = make_proposal(self.authority.log, changed, NOW)
        self.authority.close()
        PendingStore(self.directory.name).clear()
        self.client.append_event(conflicting)
        before = len(self.client.sent)
        with self.assertRaises(AuthorityError) as caught:
            self.authority.start()
        self.assertEqual(caught.exception.code, "invariant_violation")
        self.assertEqual(len(self.client.sent), before)


if __name__ == "__main__":
    unittest.main()
