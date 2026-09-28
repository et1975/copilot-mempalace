"""Current-epoch ambiguity and crash recovery against disposable source journals."""

from copy import deepcopy
import multiprocessing
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.palace import PalaceClient, PalaceError
from mempalace_tasks.protocol import make_proposal, make_settlement
from mempalace_tasks.runtime_clock import ClockError
from authority_fixture import AUTHORITY, NOW, Clock, LogClient, create, genesis, state_directory, uid


def crash_after_source_append(url, directory):
    class CrashClient(PalaceClient):
        def append_event(self, payload):
            result = super().append_event(payload)
            if payload.get("command_id") == uid(2) and payload["record_type"] == "mptask.command":
                os._exit(42)
            return result
    client = CrashClient(url, f"mptask/{AUTHORITY}", timeout=2)
    with TaskAuthority(AUTHORITY, client, directory, clock=Clock(), backoff=lambda _: None) as owner:
        owner.execute(create(), expected_epoch=owner.epoch_id)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.client = LogClient()
        self.clock = Clock()
        self.delays = []
        self.authority = self.owner(initialize=True)
        self.authority.execute_current(genesis())

    def owner(self, **options):
        owner = TaskAuthority(AUTHORITY, self.client, self.directory.name, clock=self.clock,
                              backoff=self.delays.append, **options).start()
        self.addCleanup(owner.close)
        return owner

    def send(self, value):
        return self.authority.execute(value, expected_epoch=self.authority.epoch_id)

    def error(self, code, action):
        with self.assertRaises(AuthorityError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_lost_original_response_settles_committed_without_original_retry(self):
        self.client.behaviors = ["commit_lost", "ok"]
        result = self.send(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual([p["record_type"] for p in self.client.sent[-2:]],
                         ["mptask.command", "mptask.settle"])
        self.assertEqual(result["event_id"], "evt-000003")
        self.assertIsNone(self.authority.health()["pending_command"])
        self.assertEqual(self.authority.log.raw_cursor, "evt-000004")

    def test_lost_original_absent_is_abandoned_only_by_ordered_control(self):
        self.client.behaviors = ["lost", "ok"]
        result = self.send(create())
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "abandoned")
        proposal = self.client.sent[-2]
        self.send(create(3))
        self.client.append_event(proposal)
        self.authority.refresh()
        self.assertEqual(len(self.authority.state.tasks), 1)
        self.assertEqual(self.send(create())["outcome"], "abandoned")

    def test_lost_settlement_response_replays_committed_control(self):
        self.client.behaviors = ["lost", "commit_lost"]
        self.assertEqual(self.send(create())["outcome"], "abandoned")
        self.assertEqual(len(self.client.sent), 4)
        self.assertIsNone(self.authority.health()["pending_command"])

    def test_uncertainty_is_bounded_and_reconcile_repeats_only_identical_control(self):
        self.client.behaviors = ["lost"] * 4
        error = self.error("outcome_unknown", lambda: self.send(create()))
        self.assertTrue(error.ambiguous)
        controls = self.client.sent[-3:]
        self.assertTrue(all(control == controls[0] for control in controls))
        self.assertEqual(self.delays, [1, 2, 4])
        self.assertEqual(self.authority.reconcile()["outcome"], "abandoned")
        self.assertEqual(self.client.sent[-1], controls[0])

    def test_diagnostic_reads_do_not_settle_pending_or_write_local_files(self):
        self.client.behaviors = ["lost"] * 4
        self.error("outcome_unknown", lambda: self.send(create()))
        before = len(self.client.sent)
        self.assertFalse(self.authority.snapshot()["fresh"])
        self.error("not_current", self.authority.ready)
        self.assertEqual(len(self.client.sent), before)
        self.assertEqual({p.name for p in Path(self.directory.name).iterdir()}, {"authority.lock"})

    def test_changed_uuid_payload_cannot_resolve_or_replace_uncertain_request(self):
        self.client.behaviors = ["lost"] * 4
        self.error("outcome_unknown", lambda: self.send(create()))
        changed = create()
        changed["title"] = "Changed"
        before = len(self.client.sent)
        self.error("idempotency_conflict", lambda: self.send(changed))
        self.assertEqual(len(self.client.sent), before)
        self.assertIsNotNone(self.authority.health()["pending_command"])

    def test_delayed_original_before_settlement_wins_without_reapplication(self):
        self.client.behaviors = ["lost", "ok"]
        append = self.client.append_event
        def delayed(payload):
            if payload["record_type"] == "mptask.settle":
                original = self.client.sent[-1]
                append(original)
            return append(payload)
        with patch.object(self.client, "append_event", side_effect=delayed):
            result = self.send(create())
        self.assertEqual(result["outcome"], "committed")
        self.assertEqual(self.authority.log.domain_ordinal, 2)
        self.assertEqual(len(self.authority.state.tasks), 1)

    def test_control_read_failure_retries_identical_bytes_without_new_proposal(self):
        self.client.behaviors = ["lost", "commit_lost", "commit_lost", "commit_lost"]
        reads = self.client.replay_events
        def unavailable(cursor=None):
            if len(self.client.sent) > 2:
                raise PalaceError("upstream_unavailable", "Lost reconciliation read")
            yield from reads(cursor)
        with patch.object(self.client, "replay_events", side_effect=unavailable):
            self.error("outcome_unknown", lambda: self.send(create()))
        self.assertEqual(self.client.sent[-1], self.client.sent[-2])
        self.assertEqual(self.authority.reconcile()["outcome"], "abandoned")

    def test_receipt_clock_failure_after_commit_retains_ambiguity_and_scoped_identity(self):
        with patch.object(self.authority, "_receipt",
                          side_effect=ClockError("clock_sample_failed", "Receipt sample failed")):
            error = self.error("clock_sample_failed", lambda: self.send(create()))
        self.assertTrue(error.ambiguous)
        count = len(self.client.sent)
        self.assertTrue(self.send(create())["replayed"])
        self.assertEqual(len(self.client.sent), count)

    def test_definitive_input_error_does_not_inherit_another_commands_ambiguity(self):
        self.client.behaviors = ["lost"] * 4
        self.error("outcome_unknown", lambda: self.send(create()))
        invalid = create(3)
        invalid["operation"] = "mptask_mptask_create"
        self.assertFalse(self.error("validation_error", lambda: self.send(invalid)).ambiguous)

    def test_live_missing_or_modified_prefix_fails_closed_and_is_not_automatically_reset(self):
        self.send(create())
        original = deepcopy(self.client.events)
        self.client.events.pop()
        self.error("log_rollback", self.authority.refresh)
        self.client.events = original
        count = len(self.client.sent)
        self.error("log_rollback", self.authority.reconcile)
        self.assertFalse(self.authority.snapshot()["fresh"])
        self.assertEqual(len(self.client.sent), count)

    def test_disconnection_requires_source_prefix_verification_before_new_dispatch(self):
        self.send(create())
        self.client.read_error = True
        self.assertFalse(self.authority.snapshot()["fresh"])
        self.client.read_error = False
        self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
        count = len(self.client.sent)
        self.error("log_rollback", lambda: self.send(create(3)))
        self.assertEqual(len(self.client.sent), count)

    def test_partial_replay_failure_never_publishes_an_unverified_suffix(self):
        proposal = make_proposal(self.authority.log, create(), NOW)
        self.client.append_event(proposal)
        read = self.client.replay_events
        def partial(cursor=None):
            yield from read(cursor)
            raise PalaceError("upstream_unavailable", "Suffix not confirmed")
        with patch.object(self.client, "replay_events", side_effect=partial):
            self.assertFalse(self.authority.snapshot()["fresh"])
            self.assertEqual(self.authority.state.tasks, {})
        self.authority.refresh()
        self.assertEqual(len(self.authority.state.tasks), 1)

    def test_duplicate_and_control_prefix_hash_matches_new_owner_replay(self):
        self.send(create())
        proposal = self.client.sent[-1]
        self.client.append_event(proposal)
        self.client.append_event(make_settlement(proposal))
        self.authority.refresh()
        old = self.authority.epoch_id
        receipt = self.authority.outcome(old, uid(2))["receipt"]
        accepted = self.authority.accepted_records()
        self.authority.close()
        self.authority = self.owner()
        self.assertEqual(self.authority.accepted_records(), accepted)
        self.assertEqual(self.authority.outcome(old, uid(2))["receipt"]["response"],
                         receipt["response"])
        self.assertNotEqual(self.authority.epoch_id, old)

    def test_actual_process_death_after_append_recovers_from_palace_only(self):
        from test_palace import TestHub
        self.authority.close()
        with TestHub() as hub, state_directory() as directory:
            client = PalaceClient(hub.url, f"mptask/{AUTHORITY}", timeout=2)
            with TaskAuthority(AUTHORITY, client, directory, clock=Clock(),
                               initialize=True, backoff=lambda _: None) as initializer:
                initializer.execute_current(genesis())
            process = multiprocessing.get_context("spawn").Process(
                target=crash_after_source_append, args=(hub.url, directory))
            process.start()
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
                self.fail("Disposable crash worker did not exit")
            self.assertEqual(process.exitcode, 42)
            with TaskAuthority(AUTHORITY, client, directory, clock=Clock(),
                               backoff=lambda _: None) as recovered:
                self.assertEqual(len(recovered.state.tasks), 1)
                self.assertEqual(recovered.log.domain_ordinal, 2)
                self.assertEqual({p.name for p in Path(directory).iterdir()}, {"authority.lock"})
