"""Versioned journal fencing; all histories are disposable in-memory fixtures."""

from copy import deepcopy
import unittest
from uuid import UUID, uuid5

from mempalace_tasks import protocol
from mempalace_tasks.codec import canonical_json, payload_hash
from mempalace_tasks.model import state_to_dict
from authority_fixture import AUTHORITY, EPOCH, NOW, activate, command, create, genesis, raw, uid


LATER = "2026-09-23T00:00:10Z"


def epoch_raw(payload, index):
    """Mirror the adapter routing without treating correlation as uniqueness."""
    return raw(payload, index)


class EpochTests(unittest.TestCase):
    def setUp(self):
        self.log = protocol.LogState(AUTHORITY)
        self.initial_activation = activate(self.log)
        self.genesis = protocol.make_proposal(self.log, genesis(), NOW)
        self.fold(self.genesis)

    def fold(self, payload, log=None):
        log = self.log if log is None else log
        return protocol.fold_record(log, epoch_raw(payload, len(log.history)))

    def activate(self, number, log=None, now=NOW):
        log = self.log if log is None else log
        barrier = protocol.make_epoch(log, uid(1000 + number), uid(2000 + number), now)
        self.fold(barrier, log)
        return barrier

    def assert_unchanged(self, before, log=None):
        log = self.log if log is None else log
        self.assertEqual(log, before)

    def test_activation_preserves_domain_history_and_exposes_canonical_acceptance(self):
        before = deepcopy(self.log)
        barrier = protocol.make_epoch(self.log, uid(1001), uid(2001), LATER)
        self.assert_unchanged(before)
        self.assertEqual(barrier, {
            "record_type": "mptask.epoch", "schema_version": 2,
            "authority_id": AUTHORITY, "epoch_id": uid(1001),
            "activation_id": uid(2001), "previous_activation_id": uid(2000),
            "at": LATER, "payload_hash": payload_hash(barrier),
        })
        self.fold(barrier)
        self.assertEqual(self.log.epoch_id, uid(1001))
        self.assertEqual(self.log.activation_id, uid(2001))
        self.assertEqual(self.log.activation_at, LATER)
        self.assertEqual(self.log.activation_event_id, "evt-000002")
        self.assertEqual(self.log.domain_head, before.domain_head)
        self.assertEqual(self.log.domain_ordinal, before.domain_ordinal)
        self.assertEqual(self.log.accepted_records, before.accepted_records)
        self.assertEqual(state_to_dict(self.log.state), state_to_dict(before.state))
        self.assertEqual(self.log.outcomes, {})
        self.assertEqual(self.log.proposals, {})
        self.assertEqual(self.log.controls, {})
        self.assertEqual(self.log.historical_outcome(EPOCH, uid(1)), before.outcomes[uid(1)])
        self.assertEqual(self.log.activation_attempts[uid(2001)]["outcome"], "accepted")
        self.assertEqual(self.log.history[-1]["disposition"], "activated")

    def test_old_proposal_before_barrier_stays_committed_but_after_barrier_is_stale(self):
        proposal = protocol.make_proposal(self.log, create(), NOW)
        after = deepcopy(self.log)
        self.fold(proposal)
        receipt = deepcopy(self.log.outcomes[uid(2)])
        self.activate(1)
        self.fold(proposal)
        self.assertEqual(self.log.historical_outcome(EPOCH, uid(2)), receipt)
        self.assertNotIn(uid(2), self.log.outcomes)
        self.assertEqual(self.log.history[-1]["disposition"], "stale")
        self.assertEqual(len(self.log.state.tasks), 1)
        self.activate(1, after)
        self.fold(proposal, after)
        self.assertEqual(after.state.tasks, {})
        self.assertIsNone(after.historical_outcome(EPOCH, uid(2)))
        self.assertEqual(after.history[-1]["outcome"], "stale")

    def test_stale_proposal_and_settlement_do_not_compare_new_scope_same_uuid(self):
        self.activate(1)
        old = protocol.make_proposal(self.log, create(), NOW)
        self.activate(2)
        new_command = create()
        new_command["title"] = "New request, same command UUID"
        new = protocol.make_proposal(self.log, new_command, NOW)
        self.fold(new)
        receipt = deepcopy(self.log.outcomes[uid(2)])
        before = deepcopy(self.log)
        for payload in (old, protocol.make_settlement(old)):
            self.fold(payload)
            self.assertEqual(self.log.outcomes[uid(2)], receipt)
            self.assertEqual(self.log.proposals, before.proposals)
            self.assertEqual(self.log.controls, before.controls)
            self.assertEqual(self.log.accepted_records, before.accepted_records)
            self.assertEqual(state_to_dict(self.log.state), state_to_dict(before.state))
            self.assertEqual(self.log.history[-1]["disposition"], "stale")
        self.assertIsNone(self.log.historical_outcome(uid(1001), uid(2)))
        self.assertEqual(self.log.historical_outcome(uid(1002), uid(2)), receipt)
        self.assertNotEqual(self.log.raw_hash, before.raw_hash)
        self.assertNotEqual(self.log.raw_cursor, before.raw_cursor)

    def test_unknown_discarded_future_and_retired_records_bypass_chain_validation(self):
        initial = protocol.make_proposal(self.log, create(), NOW)
        future = deepcopy(self.log)
        self.activate(99, future)
        unknown = protocol.make_proposal(future, create(), NOW)
        unknown["previous_event_id"] = "discarded-future-predecessor"
        unknown["ordinal"] = 500
        unknown["payload_hash"] = payload_hash(unknown)
        self.activate(1)
        before = deepcopy(self.log)
        for payload in (initial, unknown, protocol.make_settlement(initial),
                        protocol.make_settlement(unknown)):
            self.fold(payload)
            self.assertEqual(self.log.history[-1]["outcome"], "stale")
        self.assertEqual(self.log.outcomes, {})
        self.assertEqual(self.log.controls, {})
        self.assertEqual(self.log.proposals, {})
        self.assertEqual(self.log.scoped_outcomes, before.scoped_outcomes)
        self.assertEqual(self.log.accepted_records, before.accepted_records)

    def test_same_scope_missing_predecessor_still_fails_before_barrier(self):
        proposal = protocol.make_proposal(self.log, create(), NOW)
        proposal["previous_event_id"] = "discarded-future"
        proposal["payload_hash"] = payload_hash(proposal)
        before = deepcopy(self.log)
        with self.assertRaises(protocol.ProtocolError):
            self.fold(proposal)
        self.assert_unchanged(before)

    def test_exact_activation_duplicates_never_reactivate_old_epoch(self):
        a = self.activate(1)
        self.activate(2, now=LATER)
        before = deepcopy(self.log)
        self.fold(a)
        self.fold(a)
        self.assertEqual(self.log.epoch_id, uid(1002))
        self.assertEqual(self.log.activation_id, uid(2002))
        self.assertEqual(self.log.activation_at, LATER)
        self.assertEqual(self.log.activation_event_id, "evt-000003")
        self.assertEqual(self.log.activation_attempts, before.activation_attempts)
        self.assertEqual(self.log.history[-1]["disposition"], "duplicate")
        self.assertEqual(len(self.log.accepted_records), 1)

    def test_rejected_activation_is_terminal_even_when_predecessor_later_appears(self):
        a = protocol.make_epoch(self.log, uid(1001), uid(2001), NOW)
        future = deepcopy(self.log)
        self.fold(a, future)
        b = protocol.make_epoch(future, uid(1002), uid(2002), NOW)
        self.fold(b)
        self.assertEqual(self.log.epoch_id, EPOCH)
        rejection = deepcopy(self.log.activation_attempts[uid(2002)])
        self.assertEqual(rejection["outcome"], "stale")
        self.fold(a)
        self.fold(b)
        self.assertEqual(self.log.epoch_id, uid(1001))
        self.assertEqual(self.log.activation_attempts[uid(2002)], rejection)
        # Rejected attempts reserve their epochs as well as their activation IDs.
        reuse = protocol.make_epoch(self.log, uid(1002), uid(2003), NOW)
        self.fold(reuse)
        self.assertEqual(self.log.epoch_id, uid(1001))
        self.assertEqual(self.log.activation_attempts[uid(2003)]["outcome"], "stale")
        self.activate(4)
        self.assertEqual(self.log.epoch_id, uid(1004))

    def test_first_competing_successor_wins_and_loser_cannot_be_retried(self):
        self.activate(1)
        b = protocol.make_epoch(self.log, uid(1002), uid(2002), NOW)
        c = protocol.make_epoch(self.log, uid(1003), uid(2003), NOW)
        self.fold(b)
        self.fold(c)
        self.fold(c)
        self.assertEqual(self.log.epoch_id, uid(1002))
        self.assertEqual(self.log.activation_attempts[uid(2003)]["outcome"], "stale")
        self.assertEqual(self.log.activation_attempts[uid(2003)]["event_id"], "evt-000004")
        self.assertEqual(self.log.history[-1]["disposition"], "duplicate")

    def test_changed_accepted_or_rejected_activation_identity_fails_atomically(self):
        a = self.activate(1)
        rejected = protocol.make_epoch(self.log, uid(1002), uid(2002), NOW)
        rejected["previous_activation_id"] = uid(9999)
        rejected["payload_hash"] = payload_hash(rejected)
        self.fold(rejected)
        for original in (a, rejected):
            changed = deepcopy(original)
            changed["at"] = LATER
            changed["payload_hash"] = payload_hash(changed)
            before = deepcopy(self.log)
            with self.assertRaises(protocol.ProtocolError):
                self.fold(changed)
            self.assert_unchanged(before)

    def test_repeated_barrier_append_keeps_first_event_identity_and_time(self):
        a = self.activate(1, now=LATER)
        canonical = deepcopy(self.log.activation_attempts[uid(2001)])
        self.fold(a)
        self.assertEqual(self.log.activation_event_id, "evt-000002")
        self.assertEqual(self.log.activation_at, LATER)
        self.assertEqual(self.log.activation_attempts[uid(2001)], canonical)
        self.assertEqual(self.log.raw_cursor, "evt-000003")

    def test_current_epoch_settlement_before_proposal_retains_abandonment(self):
        self.activate(1)
        proposal = protocol.make_proposal(self.log, create(), NOW)
        control = protocol.make_settlement(proposal)
        self.assertEqual(proposal["schema_version"], 2)
        self.assertEqual(control["schema_version"], 2)
        self.assertEqual(proposal["epoch_id"], uid(1001))
        self.assertEqual(control["epoch_id"], uid(1001))
        self.assertNotIn("epoch_id", proposal["event"])
        self.assertNotIn("epoch_id", proposal["event"]["command"])
        self.fold(control)
        self.fold(proposal)
        self.fold(control)
        self.assertEqual(self.log.outcomes[uid(2)]["outcome"], "abandoned")
        self.assertEqual(self.log.state.tasks, {})
        self.assertEqual(self.log.domain_ordinal, 1)
        self.assertEqual(self.log.history[-1]["disposition"], "duplicate")

    def test_settlement_identity_namespaces_each_epoch(self):
        initial = protocol.make_proposal(self.log, create(), NOW)
        initial_control = protocol.make_settlement(initial)
        self.assertEqual(initial_control["control_id"], str(uuid5(
            UUID(AUTHORITY), f"mptask.settle:{EPOCH}:{uid(2)}:{initial['payload_hash']}")))
        self.activate(1)
        v2 = protocol.make_proposal(self.log, create(), NOW)
        control = protocol.make_settlement(v2)
        self.assertEqual(control["control_id"], str(uuid5(
            UUID(AUTHORITY), f"mptask.settle:{uid(1001)}:{uid(2)}:{v2['payload_hash']}")))
        self.assertEqual(v2["event"], initial["event"])
        self.assertEqual(v2["task_ids"], initial["task_ids"])
        self.assertNotEqual(control["control_id"], initial_control["control_id"])

    def test_historical_lookup_is_explicit_detached_and_scoped(self):
        old = protocol.make_proposal(self.log, create(), NOW)
        self.fold(protocol.make_settlement(old))
        abandoned = deepcopy(self.log.outcomes[uid(2)])
        self.activate(1)
        changed = create()
        changed["title"] = "A new scope may reuse an abandoned UUID"
        self.fold(protocol.make_proposal(self.log, changed, NOW))
        committed = deepcopy(self.log.outcomes[uid(2)])
        self.assertEqual(self.log.historical_outcome(EPOCH, uid(2)), abandoned)
        self.assertEqual(self.log.historical_outcome(uid(1001), uid(2)), committed)
        self.assertIsNone(self.log.historical_outcome(uid(9999), uid(2)))
        self.assertEqual(self.log.scoped_outcomes[(AUTHORITY, EPOCH, uid(2))], abandoned)
        self.assertEqual(self.log.scoped_outcomes[(AUTHORITY, uid(1001), uid(2))], committed)
        detached = self.log.historical_outcome(uid(1001), uid(2))
        detached["response"]["tasks"][0]["title"] = "Caller mutation"
        self.assertEqual(self.log.outcomes[uid(2)], committed)

    def test_changed_duplicate_in_current_epoch_is_not_excused_as_stale(self):
        self.activate(1)
        proposal = protocol.make_proposal(self.log, create(), NOW)
        self.fold(proposal)
        changed = deepcopy(proposal)
        changed["previous_event_id"] = "changed"
        changed["payload_hash"] = payload_hash(changed)
        before = deepcopy(self.log)
        with self.assertRaises(protocol.ProtocolError):
            self.fold(changed)
        self.assert_unchanged(before)

    def test_both_scopes_keep_committed_outcomes_for_same_uuid_and_different_commands(self):
        task = protocol.make_proposal(self.log, create(), NOW)
        self.fold(task)
        task_id = task["task_ids"][0]
        self.activate(1)
        first = protocol.make_proposal(self.log, command(
            3, "update", task_id=task_id, expected_version=1, patch={"title": "Epoch A"}), NOW)
        self.fold(first)
        first_receipt = deepcopy(self.log.outcomes[uid(3)])
        self.activate(2)
        second = protocol.make_proposal(self.log, command(
            3, "update", task_id=task_id, expected_version=2, patch={"title": "Epoch B"}), NOW)
        self.fold(second)
        second_receipt = deepcopy(self.log.outcomes[uid(3)])
        for old in (first, protocol.make_settlement(first)):
            self.fold(old)
        self.assertEqual(self.log.historical_outcome(uid(1001), uid(3)), first_receipt)
        self.assertEqual(self.log.historical_outcome(uid(1002), uid(3)), second_receipt)
        self.assertEqual(self.log.state.tasks[task_id]["title"], "Epoch B")
        self.assertEqual(self.log.state.tasks[task_id]["version"], 3)
        self.assertEqual(self.log.domain_ordinal, 4)

    def test_accepted_replay_bytes_and_task_identity_are_preserved_across_activation(self):
        proposal = protocol.make_proposal(self.log, create(), NOW)
        settlement = protocol.make_settlement(proposal)
        self.fold(proposal)
        self.fold(settlement)
        initial_history = deepcopy(self.log.history)
        accepted = canonical_json(self.log.accepted_records)
        receipt = deepcopy(self.log.outcomes[uid(2)])
        barrier = self.activate(1)
        replay = protocol.LogState(AUTHORITY)
        activate(replay)
        for payload in (self.genesis, proposal, settlement, barrier):
            self.fold(payload, replay)
        self.assertEqual(replay.history[:4], initial_history)
        self.assertEqual(canonical_json(replay.accepted_records), accepted)
        self.assertEqual(replay.historical_outcome(EPOCH, uid(2)), receipt)
        self.assertEqual(replay, self.log)
        self.assertNotIn("epoch_id", replay.accepted_records[1]["event"])

    def test_v2_request_before_any_activation_does_not_create_or_abandon_state(self):
        branch = deepcopy(self.log)
        self.activate(1, branch)
        proposal = protocol.make_proposal(branch, create(), NOW)
        self.log = protocol.LogState(AUTHORITY)
        before = deepcopy(self.log)
        self.fold(protocol.make_settlement(proposal))
        self.fold(proposal)
        self.assertIsNone(self.log.epoch_id)
        self.assertEqual(self.log.outcomes, before.outcomes)
        self.assertEqual(self.log.proposals, before.proposals)
        self.assertEqual(self.log.controls, before.controls)
        self.assertEqual(self.log.accepted_records, before.accepted_records)
        self.assertEqual([row["disposition"] for row in self.log.history[-2:]], ["stale", "stale"])

    def test_superseded_epoch_cannot_be_reactivated_by_a_new_activation_identity(self):
        self.activate(1)
        self.activate(2)
        alias = protocol.make_epoch(self.log, uid(1001), uid(2003), NOW)
        self.fold(alias)
        self.assertEqual(self.log.epoch_id, uid(1002))
        self.assertEqual(self.log.activation_attempts[uid(2003)]["outcome"], "stale")
        self.assertEqual(self.log.used_epochs[uid(1001)], uid(2001))

    def test_stale_envelopes_still_require_strict_hashes_fields_and_metadata(self):
        self.activate(1)
        proposal = protocol.make_proposal(self.log, create(), NOW)
        control = protocol.make_settlement(proposal)
        self.activate(2)
        malformed = []
        for payload in (proposal, control):
            changed = deepcopy(payload)
            changed["epoch_id"] = "not-a-uuid"
            changed["payload_hash"] = payload_hash(changed)
            malformed.append(epoch_raw(changed, 4))
            changed = deepcopy(payload)
            changed["payload_hash"] = "0" * 64
            malformed.append(epoch_raw(changed, 4))
            changed = deepcopy(payload)
            changed["extra"] = "unexpected"
            changed["payload_hash"] = payload_hash(changed)
            malformed.append(epoch_raw(changed, 4))
        changed = deepcopy(control)
        changed["control_id"] = uid(9000)
        changed["payload_hash"] = payload_hash(changed)
        malformed.append(epoch_raw(changed, 4))
        event = epoch_raw(control, 4)
        event["metadata"]["epoch_id"] = uid(9999)
        malformed.append(event)
        event = epoch_raw(control, 4)
        event["body"] = event["body"][:-1] + ',"epoch_id":"' + uid(1001) + '"}'
        malformed.append(event)
        for event in malformed:
            with self.subTest(body=event["body"]):
                before = deepcopy(self.log)
                with self.assertRaises(protocol.ProtocolError):
                    protocol.fold_record(self.log, event)
                self.assert_unchanged(before)

    def test_epoch_builder_and_fold_reject_invalid_values_without_mutating_inputs(self):
        for epoch_id, activation_id, now in (
            ("bad", uid(2001), NOW), (uid(1001), "bad", NOW),
            (uid(1001), uid(2001), "yesterday"), (uid(1001), uid(2001), None),
        ):
            before = deepcopy(self.log)
            with self.subTest(epoch_id=epoch_id, activation_id=activation_id, now=now):
                with self.assertRaises(protocol.ProtocolError):
                    protocol.make_epoch(self.log, epoch_id, activation_id, now)
                self.assert_unchanged(before)
        barrier = protocol.make_epoch(self.log, uid(1001), uid(2001), NOW)
        for key, value in (("schema_version", True), ("schema_version", 1),
                           ("previous_activation_id", "bad"), ("at", None),
                           ("activation_id", [])):
            changed = deepcopy(barrier)
            changed[key] = value
            changed["payload_hash"] = payload_hash(changed)
            event = epoch_raw(barrier, 2)
            event["body"] = canonical_json(changed)
            with self.subTest(key=key, value=value):
                before = deepcopy(self.log)
                with self.assertRaises(protocol.ProtocolError):
                    protocol.fold_record(self.log, event)
                self.assert_unchanged(before)

    def test_raw_routing_caps_and_unicode_fail_even_for_stale_activation(self):
        barrier = self.activate(1)
        self.activate(2)
        for key, value in (("id", "x" * 257), ("id", " padded "),
                           ("id", "bad\x00id"), ("created_at", None)):
            event = epoch_raw(barrier, 4)
            event[key] = value
            before = deepcopy(self.log)
            with self.subTest(key=key), self.assertRaises(protocol.ProtocolError):
                protocol.fold_record(self.log, event)
            self.assert_unchanged(before)
        event = epoch_raw(barrier, 4)
        event["metadata"]["padding"] = "x" * (64 * 1024)
        with self.assertRaises(protocol.ProtocolError):
            protocol.fold_record(self.log, event)
        event = epoch_raw(barrier, 4)
        event["metadata"]["padding"] = "\ud800"
        with self.assertRaises(protocol.ProtocolError):
            protocol.fold_record(self.log, event)

    def test_snapshot_payload_and_raw_caller_objects_are_never_mutated_or_retained(self):
        barrier = protocol.make_epoch(self.log, uid(1001), uid(2001), NOW)
        event = epoch_raw(barrier, 2)
        before_barrier, before_event = deepcopy(barrier), deepcopy(event)
        protocol.fold_record(self.log, event)
        self.assertEqual(barrier, before_barrier)
        self.assertEqual(event, before_event)
        event["metadata"]["epoch_id"] = uid(9999)
        barrier["at"] = LATER
        self.assertEqual(self.log.activation_at, NOW)
        self.assertEqual(self.log.activation_attempts[uid(2001)]["payload"], before_barrier)
        before = deepcopy(self.log)
        proposal = protocol.make_proposal(self.log, create(), NOW)
        original = deepcopy(proposal)
        control = protocol.make_settlement(proposal)
        control["task_ids"].clear()
        self.assertEqual(proposal, original)
        self.assert_unchanged(before)


if __name__ == "__main__":
    unittest.main()
