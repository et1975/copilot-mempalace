import copy
import json
import unittest

from mempalace_tasks.codec import canonical_json, command_hash, payload_hash, task_id_for
from mempalace_tasks.model import DomainError, State, new_state, state_from_dict, state_to_dict


AUTHORITY = "11111111-1111-4111-8111-111111111111"
COMMAND = "22222222-2222-4222-8222-222222222222"


class ModelTests(unittest.TestCase):
    def test_canonical_encoding_is_order_independent_and_utf8(self):
        self.assertEqual(canonical_json({"z": [True, None], "a": "\u00e9"}),
                         '{"a":"\u00e9","z":[true,null]}')
        self.assertEqual(command_hash({"a": 1, "b": 2}), command_hash({"b": 2, "a": 1}))
        self.assertNotEqual(command_hash({"a": None}), command_hash({}))

    def test_rejects_non_json_values_floats_and_nonstring_keys(self):
        for value in (1.0, float("nan"), {1: "x"}, {"x": set()}, ("a",), "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(DomainError):
                canonical_json(value)

    def test_payload_hash_excludes_only_its_own_top_level_field(self):
        self.assertEqual(payload_hash({"a": 1}), payload_hash({"a": 1, "payload_hash": "ignored"}))
        self.assertNotEqual(payload_hash({"a": {"payload_hash": "a"}}),
                            payload_hash({"a": {"payload_hash": "b"}}))

    def test_ids_are_deterministic_and_authority_scoped(self):
        first = task_id_for(AUTHORITY, COMMAND)
        self.assertEqual(first, task_id_for(AUTHORITY, COMMAND))
        self.assertTrue(first.startswith("tsk_"))
        self.assertNotEqual(first, task_id_for(COMMAND, COMMAND))
        with self.assertRaises(DomainError):
            task_id_for(AUTHORITY, "not-a-command-uuid")

    def test_state_serialization_is_json_and_detached(self):
        state = new_state(AUTHORITY)
        self.assertIsInstance(state, State)
        encoded = state_to_dict(state)
        restored = state_from_dict(json.loads(json.dumps(encoded)))
        restored.tasks["other"] = {"nested": []}
        self.assertEqual(state.tasks, {})
        self.assertEqual(encoded["tasks"], {})
        clone = copy.deepcopy(state)
        clone.resources["x"] = {"counter": 1}
        self.assertEqual(state.resources, {})

    def test_state_rejects_unknown_or_missing_fields(self):
        encoded = state_to_dict(new_state(AUTHORITY))
        with self.assertRaises(DomainError):
            state_from_dict({**encoded, "unknown": True})
        del encoded["tasks"]
        with self.assertRaises(DomainError):
            state_from_dict(encoded)

    def test_error_details_do_not_alias_caller(self):
        details = {"nested": []}
        error = DomainError("validation_error", "bad", details)
        details["nested"].append("changed")
        result = error.to_dict()
        result["details"]["nested"].append("changed-again")
        self.assertEqual(error.details, {"nested": []})
        self.assertEqual(error.code, "validation_error")


if __name__ == "__main__":
    unittest.main()
