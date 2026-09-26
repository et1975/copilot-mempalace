from copy import deepcopy
import unittest

from mempalace_tasks.codec import canonical_json, payload_hash
from mempalace_tasks.model import DomainError, state_to_dict
from mempalace_tasks.protocol import (
    LogState, ProtocolError, fold_record, make_proposal, make_settlement,
)
from authority_fixture import AUTHORITY, EPOCH, NOW, activate, command, create, genesis, raw, uid


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.log = LogState(AUTHORITY)
        self.activation = activate(self.log)
        self.genesis = make_proposal(self.log, genesis(), NOW)
        fold_record(self.log, raw(self.genesis, 1))

    def proposal(self, number=2):
        return make_proposal(self.log, create(number), NOW)

    def test_proposal_requires_an_accepted_epoch_before_genesis(self):
        with self.assertRaises(ProtocolError):
            make_proposal(LogState(AUTHORITY), genesis(), NOW)

    def test_obsolete_v1_envelope_is_rejected_not_replayed_or_fenced(self):
        legacy = deepcopy(self.genesis)
        legacy.pop("epoch_id", None)
        legacy["schema_version"] = 1
        legacy["payload_hash"] = payload_hash(legacy)
        event = raw(self.genesis, 20)
        event["body"] = canonical_json(legacy)
        for log in (LogState(AUTHORITY), deepcopy(self.log)):
            with self.subTest(activated=log.epoch_id is not None), self.assertRaises(ProtocolError):
                fold_record(log, event)

    def test_historical_lookup_rejects_null_epoch(self):
        with self.assertRaises(DomainError) as caught:
            self.log.historical_outcome(None, uid(1))
        self.assertEqual(caught.exception.code, "validation_error")

    def test_proposal_does_not_mutate_state_and_hashes_complete_envelope(self):
        before = state_to_dict(self.log.state)
        proposal = self.proposal()
        self.assertEqual(state_to_dict(self.log.state), before)
        self.assertEqual(proposal["ordinal"], 2)
        self.assertEqual(proposal["previous_event_id"], "evt-000001")
        self.assertEqual(proposal["payload_hash"], payload_hash(proposal))
        self.assertEqual(proposal["task_ids"], [proposal["event"]["tasks"][0]["id"]])

    def test_accepted_command_then_control_keeps_original_response(self):
        proposal = self.proposal()
        fold_record(self.log, raw(proposal, 2))
        original = deepcopy(self.log.outcomes[uid(2)])
        fold_record(self.log, raw(make_settlement(proposal), 3))
        self.assertEqual(self.log.outcomes[uid(2)], original)
        self.assertEqual(original["outcome"], "committed")
        self.assertEqual(original["event_id"], "evt-000002")
        self.assertEqual(self.log.domain_ordinal, 2)
        self.assertEqual(self.log.raw_cursor, "evt-000003")
        self.assertEqual(self.log.history[-1]["disposition"], "settled")

    def test_control_before_late_obsolete_proposal_permanently_abandons(self):
        proposal = self.proposal()
        fold_record(self.log, raw(make_settlement(proposal), 2))
        newer = self.proposal(3)
        fold_record(self.log, raw(newer, 3))
        fold_record(self.log, raw(proposal, 4))
        self.assertEqual(self.log.domain_head, "evt-000003")
        self.assertEqual(self.log.domain_ordinal, 2)
        self.assertEqual(self.log.outcomes[uid(2)]["outcome"], "abandoned")
        self.assertEqual(len(self.log.state.tasks), 1)
        self.assertEqual(self.log.history[-1]["disposition"], "abandoned")

    def test_duplicate_proposals_and_controls_only_advance_raw_head(self):
        proposal = self.proposal()
        settlement = make_settlement(proposal)
        for payload, index in [(proposal, 2), (proposal, 3), (settlement, 4), (settlement, 5)]:
            fold_record(self.log, raw(payload, index))
        self.assertEqual(self.log.domain_head, "evt-000002")
        self.assertEqual(self.log.domain_ordinal, 2)
        self.assertEqual([row["disposition"] for row in self.log.history],
                         ["activated", "accepted", "accepted", "duplicate", "settled", "duplicate"])

    def test_reused_id_or_changed_control_is_invariant_violation(self):
        proposal = self.proposal()
        fold_record(self.log, raw(proposal, 2))
        changed = deepcopy(proposal)
        changed["event"]["command"]["title"] = "Different"
        changed["payload_hash"] = payload_hash(changed)
        with self.assertRaises(ProtocolError):
            fold_record(self.log, raw(changed, 3))
        control = make_settlement(proposal)
        fold_record(self.log, raw(control, 3))
        changed = deepcopy(control)
        changed["task_ids"] = []
        changed["payload_hash"] = payload_hash(changed)
        with self.assertRaises(ProtocolError) as caught:
            fold_record(self.log, raw(changed, 4))
        self.assertEqual(caught.exception.code, "invariant_violation")
        self.assertEqual(self.log.raw_cursor, "evt-000003")

    def test_tombstone_rejects_same_command_id_with_different_proposal(self):
        proposal = self.proposal()
        fold_record(self.log, raw(make_settlement(proposal), 2))
        changed = deepcopy(proposal)
        changed["ordinal"] = 55
        changed["payload_hash"] = payload_hash(changed)
        with self.assertRaises(ProtocolError) as caught:
            fold_record(self.log, raw(changed, 3))
        self.assertEqual(caught.exception.code, "invariant_violation")

    def test_chain_and_domain_tampering_fail_without_partial_mutation(self):
        for change in ("ordinal", "previous_event_id", "task_snapshot", "task_ids"):
            with self.subTest(change=change):
                proposal = self.proposal()
                if change == "ordinal":
                    proposal["ordinal"] = 3
                elif change == "previous_event_id":
                    proposal["previous_event_id"] = "other"
                elif change == "task_snapshot":
                    proposal["event"]["tasks"][0]["title"] = "Forged"
                else:
                    proposal["task_ids"] = []
                proposal["payload_hash"] = payload_hash(proposal)
                before = deepcopy(self.log)
                with self.assertRaises(ProtocolError):
                    fold_record(self.log, raw(proposal, 2))
                self.assertEqual(self.log.raw_hash, before.raw_hash)
                self.assertEqual(self.log.domain_ordinal, before.domain_ordinal)
                self.assertEqual(state_to_dict(self.log.state), state_to_dict(before.state))

    def test_route_writer_schema_and_strict_json_rejected(self):
        original = raw(self.proposal(), 2)
        variants = []
        for key, value in (("stream", "mptask/other"), ("room", "other"),
                           ("from_agent", "stranger"), ("type", "other"),
                           ("correlation_id", uid(400)), ("id", "")):
            variants.append({**original, key: value})
        variants += [{**original, "body": '{"record_type":1,"record_type":2}'},
                     {**original, "body": '{"x":NaN}'},
                     {**original, "body_truncated": True}]
        payload = self.proposal()
        payload["schema_version"] = True
        payload["payload_hash"] = payload_hash(payload)
        variants.append(raw(payload, 2))
        for event in variants:
            with self.subTest(event=event), self.assertRaises(ProtocolError):
                fold_record(self.log, event)

    def test_physical_id_repeat_rejected_even_for_identical_payload(self):
        with self.assertRaises(ProtocolError):
            fold_record(self.log, raw(self.genesis, 1))

    def test_raw_hash_commits_entire_prefix_not_just_latest_payload(self):
        a = LogState(AUTHORITY)
        b = LogState(AUTHORITY)
        activate(a)
        activate(b)
        fold_record(a, raw(self.genesis, 1))
        changed = raw(self.genesis, 1)
        changed["created_at"] = "2026-09-23T00:00:01Z"
        fold_record(b, changed)
        proposal = self.proposal()
        fold_record(a, raw(proposal, 2))
        fold_record(b, raw(proposal, 2))
        self.assertNotEqual(a.raw_hash, b.raw_hash)

    def test_alias_only_normalization_preserves_omission_and_null(self):
        aliased = create()
        aliased["operation"] = "mptask_create"
        self.assertEqual(make_proposal(self.log, aliased, NOW), self.proposal())
        nullable = create(hold_reason=None)
        self.assertNotEqual(make_proposal(self.log, nullable, NOW)["command_hash"],
                            self.proposal()["command_hash"])

    def test_control_is_identical_and_independent_of_current_domain(self):
        proposal = self.proposal()
        before = canonical_json(make_settlement(proposal))
        fold_record(self.log, raw(self.proposal(3), 2))
        self.assertEqual(canonical_json(make_settlement(proposal)), before)
        self.assertNotIn("ordinal", make_settlement(proposal))
        self.assertNotIn("event", make_settlement(proposal))

    def test_wrong_field_types_are_protocol_errors_not_python_exceptions(self):
        for key, value in (("record_type", []), ("schema_version", {}),
                           ("command_id", None), ("ordinal", "2"), ("task_ids", [[]]),
                           ("event", []), ("payload_hash", {})):
            with self.subTest(key=key):
                proposal = self.proposal()
                proposal[key] = value
                if key != "payload_hash":
                    proposal["payload_hash"] = payload_hash(proposal)
                event = raw(self.proposal(), 2)
                event["body"] = canonical_json(proposal)
                with self.assertRaises(ProtocolError):
                    fold_record(self.log, event)

    def test_abandoned_late_record_still_requires_valid_static_event_schema(self):
        for key, value in (("at", None), ("response", []), ("kind", None), ("changes", []),
                           ("edges_added", None), ("resources", None)):
            with self.subTest(key=key):
                proposal = self.proposal()
                proposal["event"][key] = value
                proposal["payload_hash"] = payload_hash(proposal)
                control = {
                    "record_type": "mptask.settle", "schema_version": 2, "epoch_id": EPOCH,
                    "authority_id": AUTHORITY, "command_id": uid(2),
                    "command_hash": proposal["command_hash"],
                    "proposal_hash": proposal["payload_hash"], "task_ids": proposal["task_ids"],
                }
                from uuid import UUID, uuid5
                control["control_id"] = str(uuid5(UUID(AUTHORITY),
                    f"mptask.settle:{EPOCH}:{uid(2)}:{proposal['payload_hash']}"))
                control["payload_hash"] = payload_hash(control)
                log = deepcopy(self.log)
                fold_record(log, raw(control, 2))
                with self.assertRaises(ProtocolError):
                    fold_record(log, raw(proposal, 3))


if __name__ == "__main__":
    unittest.main()
