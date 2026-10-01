"""Parent-owned criterion review through real native journal transitions."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

import test_native
from authority_fixture import uid
from mempalace_tasks.authority import AuthorityError
from mempalace_tasks.inspection import build_frame, render_text
from mempalace_tasks.model import DomainError, state_from_dict, state_to_dict
from mempalace_tasks.server import _Runtime


class NativeReviewTests(unittest.TestCase):
    setUp = test_native.NativeTests.setUp
    call = test_native.NativeTests.call
    bootstrap = test_native.NativeTests.bootstrap
    task = test_native.NativeTests.task
    scope = test_native.NativeTests.scope
    tokens = test_native.NativeTests.tokens
    start = test_native.NativeTests.start
    assertRejected = test_native.NativeTests.assertRejected

    def running(self):
        self.bootstrap()
        self.start(agent="observed-worker")

    def review_payload(self, **fields):
        return {
            **self.tokens(), "summary": "Result needs corrections",
            "evidence": ["artifact:failed-test"], "next_action": "rework",
            "findings": [{"id": "coverage", "criterion": "All cases pass",
                          "feedback": "The empty case fails"}], **fields,
        }

    def completion(self, **fields):
        return {
            **self.tokens(), "summary": "Corrected", "evidence": ["artifact:passing-test"],
            "parent_acceptance": "Inspected corrected empty case", **fields,
        }

    def resolution(self, review_id=None, findings=None):
        return {
            "review_id": review_id or self.task(self.planner)["native"]["review"]["id"],
            "findings": findings if findings is not None else [
                {"id": "coverage", "summary": "Empty case corrected",
                 "evidence": ["artifact:empty-case-passes"]}],
        }

    def expansion(self, **fields):
        return {
            **{k: v for k, v in self.tokens().items() if k != "task_id"},
            "source_task_id": self.planner,
            "expected_graph_revision": self.task(self.goal)["graph_revision"],
            "source_disposition": "complete", "tasks": [],
            "summary": "Imported corrected plan", "evidence": ["artifact:plan"],
            "parent_acceptance": "Reviewed plan corrections", **fields,
        }

    def test_rework_preserves_binding_and_persists_current_review_with_attention(self):
        self.running()
        before = self.task(self.planner)
        request = self.review_payload()
        self.call("request_changes", request, number=901)
        current = self.task(self.planner)
        self.assertEqual(current["status"], "in_progress")
        self.assertEqual(current["attempt"], before["attempt"])
        self.assertEqual(current["claim_generation"], before["claim_generation"])
        self.assertEqual(current["native"]["review"], {
            "id": uid(901), "status": "changes_requested",
            "summary": request["summary"], "evidence": request["evidence"],
            "next_action": "rework", "findings": request["findings"],
            "actor": self.session, "at": self.clock.now(),
            "attempt_id": before["attempt"]["id"], "claim_generation": before["claim_generation"],
        })
        self.assertTrue(self.authority.get(self.planner)["authorization"]["authorized"])
        rows = self.authority.snapshot(filters={"needs_attention": True})["rows"]
        self.assertEqual([row["id"] for row in rows], [self.planner])
        self.assertEqual(rows[0]["native"]["review"], current["native"]["review"])
        self.assertTrue(self.call("request_changes", request, number=901)["replayed"])
        state_from_dict(state_to_dict(self.authority.state))

    def test_completion_requires_exact_review_and_finding_coverage(self):
        self.running()
        self.call("request_changes", self.review_payload())
        self.assertRejected("validation_error", "complete", self.completion())
        invalid = [
            self.resolution(review_id=uid(999)),
            self.resolution(findings=[]),
            self.resolution(findings=[{"id": "other", "summary": "Fixed", "evidence": ["artifact:x"]}]),
            self.resolution(findings=self.resolution()["findings"] * 2),
            self.resolution(findings=[{"id": "coverage", "summary": " ", "evidence": ["artifact:x"]}]),
            self.resolution(findings=[{"id": "coverage", "summary": "Fixed", "evidence": []}]),
            self.resolution(findings=[{"id": "coverage", "summary": "Fixed", "evidence": [" "]}]),
        ]
        for resolution in invalid:
            with self.subTest(resolution=resolution):
                self.assertRejected("validation_error", "complete",
                                    self.completion(review_resolution=resolution))
        resolution = self.resolution()
        self.call("complete", self.completion(review_resolution=resolution))
        current = self.task(self.planner)
        self.assertEqual(current["status"], "closed")
        self.assertEqual(current["native"]["review"]["status"], "accepted")
        saved = current["native"]["review"]["resolution"]
        self.assertEqual(saved["review_id"], resolution["review_id"])
        self.assertEqual(saved["findings"], resolution["findings"])
        self.assertEqual(saved["actor"], self.session)
        self.assertEqual(saved["attempt_id"], current["attempt"]["id"])
        self.assertEqual(current["native"]["parent_acceptance"], "Inspected corrected empty case")
        self.assertEqual(self.authority.snapshot(filters={"needs_attention": True})["rows"], [])
        state_from_dict(state_to_dict(self.authority.state))

    def test_repeated_review_cannot_forget_or_redefine_outstanding_criteria(self):
        self.running()
        self.call("request_changes", self.review_payload(), number=901)
        first = self.task(self.planner)["native"]["review"]
        self.assertRejected("validation_error", "request_changes", self.review_payload(findings=[
            {"id": "new", "criterion": "New case", "feedback": "Missing"}]))
        self.assertRejected("validation_error", "request_changes", self.review_payload(findings=[
            {"id": "coverage", "criterion": "Different criterion", "feedback": "Changed"}]))
        findings = [
            {**first["findings"][0], "feedback": "Empty and null cases fail"},
            {"id": "null", "criterion": "Null input passes", "feedback": "Null case still fails"},
        ]
        self.call("request_changes", self.review_payload(findings=findings), number=902)
        self.assertEqual(self.task(self.planner)["native"]["review"]["id"], uid(902))
        self.assertRejected("validation_error", "complete", self.completion(
            review_resolution=self.resolution(review_id=uid(901))))
        resolution = self.resolution(findings=[
            {"id": "null", "summary": "Null fixed", "evidence": ["artifact:null"]},
            {"id": "coverage", "summary": "Empty fixed", "evidence": ["artifact:empty"]},
        ])
        self.call("complete", self.completion(review_resolution=resolution))
        self.assertEqual(self.task(self.planner)["native"]["review"]["findings"], findings)

    def test_hold_requires_reconciliation_then_retry_preserves_completion_gate(self):
        self.running()
        old = self.tokens()
        self.call("request_changes", self.review_payload(next_action="hold"))
        held = self.task(self.planner)
        review = held["native"]["review"]
        self.assertEqual(held["status"], "recovering")
        self.assertEqual(held["attempt"]["status"], "revoked")
        self.assertIs(held["recovery"]["barrier_satisfied"], False)
        self.assertNotIn("assignee", held)
        self.assertFalse(self.authority.get(self.planner)["authorization"]["authorized"])
        self.assertRejected("stale_generation", "complete", self.completion(
            review_resolution=self.resolution()))
        self.call("reconcile", {**self.tokens(), "decision": "retry",
                               "reason": "Parent inspected uncertain effects",
                               "observations": ["No outstanding effects observed"]})
        self.assertEqual(self.task(self.planner)["native"]["review"], review)
        self.assertTrue(self.authority.get(self.planner)["eligibility"]["ready"])
        self.assertEqual([row["id"] for row in self.authority.snapshot(
            filters={"needs_attention": True})["rows"]], [self.planner])
        self.start(agent="replacement-worker")
        self.assertNotEqual(self.tokens()["attempt_id"], old["attempt_id"])
        self.assertRejected("validation_error", "complete", self.completion())
        self.call("complete", self.completion(review_resolution=self.resolution()))
        accepted = self.task(self.planner)["native"]["review"]
        self.assertEqual(accepted["attempt_id"], old["attempt_id"])
        self.assertEqual(accepted["resolution"]["attempt_id"], self.tokens()["attempt_id"])

    def test_expand_complete_uses_same_gate_and_rolls_back_admission_on_rejection(self):
        self.running()
        self.call("request_changes", self.review_payload())
        tasks = [{"intent_key": "followup", "title": "Followup", "description": "", "acceptance": ""}]
        before = state_to_dict(self.authority.state)
        self.assertRejected("validation_error", "expand", self.expansion(tasks=tasks))
        self.assertEqual(state_to_dict(self.authority.state), before)
        result = self.call("expand", self.expansion(
            tasks=tasks, review_resolution=self.resolution()))
        self.assertEqual(len(result["admitted_task_ids"]), 1)
        self.assertEqual(self.task(self.planner)["native"]["review"]["status"], "accepted")

    def test_completion_resolution_is_invalid_without_pending_review(self):
        self.running()
        self.assertRejected("validation_error", "complete", self.completion(
            review_resolution=self.resolution(review_id=uid(999))))
        self.assertRejected("validation_error", "expand", self.expansion(
            review_resolution=self.resolution(review_id=uid(999))))

    def test_noncompletion_expansion_cannot_smuggle_review_resolution(self):
        self.running()
        self.call("request_changes", self.review_payload())
        for disposition in ("continue", "yield"):
            payload = self.expansion(source_disposition=disposition, review_resolution=self.resolution())
            for key in ("summary", "evidence", "parent_acceptance"):
                del payload[key]
            if disposition == "yield":
                payload.update(checkpoint={"sequence": 1, "reference": "artifact:checkpoint"},
                               reason="Await corrections", observations=["Uncertain worker"])
            self.assertRejected("validation_error", "expand", payload)

    def test_rejection_remains_a_rejection_after_cancellation(self):
        self.running()
        self.call("request_changes", self.review_payload(next_action="hold"))
        review = self.task(self.planner)["native"]["review"]
        self.call("reconcile", {**self.tokens(), "decision": "cancel",
                               "reason": "Abandon rejected result", "observations": ["Parent decision"]})
        current = self.task(self.planner)
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["native"]["review"], review)
        self.assertIsNone(current["completion"])
        self.assertNotIn("parent_acceptance", current["native"])
        state_from_dict(state_to_dict(self.authority.state))

    def test_review_requires_running_current_parent_and_agent_binding(self):
        self.bootstrap()
        self.call("claim", self.scope(task_id=self.planner, expected_version=1))
        self.assertRejected("validation_error", "request_changes", self.review_payload())
        self.start_bound()
        self.assertRejected("not_owner", "request_changes", self.review_payload(), session=test_native.OTHER)
        self.assertRejected("stale_generation", "request_changes",
                            self.review_payload(native_agent_id="another-worker"))
        self.assertRejected("version_conflict", "request_changes", self.review_payload(expected_version=1))

    def start_bound(self):
        self.call("start", {**self.tokens(), "native_agent_id": "observed-worker"})

    def test_review_text_and_finding_bounds_are_utf8_nonblank_and_unique(self):
        self.running()
        bad_findings = [
            [], [{"id": " ", "criterion": "Case", "feedback": "Fail"}],
            [{"id": "é" * 65, "criterion": "Case", "feedback": "Fail"}],
            [{"id": "a", "criterion": "é" * 1025, "feedback": "Fail"}],
            [{"id": "a", "criterion": "Case", "feedback": "é" * 1025}],
            [{"id": "a", "criterion": " ", "feedback": "Fail"}],
            [{"id": "a", "criterion": "Case", "feedback": "\n"}],
            [{"id": "a", "criterion": "Case", "feedback": "Fail"}] * 2,
            [{"id": str(i), "criterion": "Case", "feedback": "Fail"} for i in range(21)],
        ]
        for findings in bad_findings:
            with self.subTest(findings=findings):
                self.assertRejected("validation_error", "request_changes",
                                    self.review_payload(findings=findings))
        for fields in ({"summary": " "}, {"evidence": [" "]}, {"next_action": "restart"}):
            self.assertRejected("validation_error", "request_changes", self.review_payload(**fields))
        findings = [{"id": "é" * 64, "criterion": "é" * 1024, "feedback": "é" * 1024}]
        self.call("request_changes", self.review_payload(findings=findings))
        self.assertEqual(self.task(self.planner)["native"]["review"]["findings"], findings)

    def test_snapshot_rejects_malformed_or_inconsistent_review_records(self):
        self.running()
        self.call("request_changes", self.review_payload())
        snapshot = state_to_dict(self.authority.state)
        mutations = [
            {"id": "bad"}, {"status": "completed"}, {"findings": []},
            {"actor": "worker"}, {"at": "2999-01-01T00:00:00Z"},
            {"claim_generation": 0}, {"claim_generation": 2}, {"attempt_id": "bad"},
            {"attempt_id": "att_" + uid(987)}, {"next_action": "restart"},
            {"resolution": self.resolution()}, {"physical_stop": True},
            {"findings": [{"id": "coverage", "criterion": "Case", "feedback": " "}]},
        ]
        for patch in mutations:
            with self.subTest(patch=patch):
                broken = deepcopy(snapshot)
                broken["tasks"][self.planner]["native"]["review"].update(patch)
                with self.assertRaises(DomainError):
                    state_from_dict(broken)
        self.call("complete", self.completion(review_resolution=self.resolution()))
        accepted = state_to_dict(self.authority.state)
        for patch in ({"status": "changes_requested"}, {"resolution": None}):
            broken = deepcopy(accepted)
            broken["tasks"][self.planner]["native"]["review"].update(patch)
            with self.assertRaises(DomainError):
                state_from_dict(broken)

    def test_human_inspection_exposes_rejection_findings_without_terminal_controls(self):
        self.running()
        self.call("request_changes", self.review_payload(findings=[
            {"id": "coverage", "criterion": "All cases pass",
             "feedback": "Failure:\x1b[31mempty case"},
        ]))
        for command, result in (("show", self.authority.get(self.planner)),
                                ("list", self.authority.snapshot())):
            with self.subTest(command=command):
                output = render_text(build_frame(command, result))
                self.assertIn("changes_requested", output)
                self.assertIn("All cases pass", output)
                self.assertIn("empty case", output)
                self.assertNotIn("\x1b", output)

    def test_snapshot_review_requires_started_work_and_a_well_formed_completion(self):
        self.running()
        self.call("request_changes", self.review_payload())
        snapshot = state_to_dict(self.authority.state)
        snapshot["tasks"][self.planner]["attempt"].update(status="preparing", native_agent_id=None)
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)
        self.call("complete", self.completion(review_resolution=self.resolution()))
        snapshot = state_to_dict(self.authority.state)
        for completion in (None, {}, [], {"summary": "Untrusted"}):
            with self.subTest(completion=completion):
                broken = deepcopy(snapshot)
                broken["tasks"][self.planner]["completion"] = completion
                with self.assertRaises(DomainError):
                    state_from_dict(broken)

    def test_draining_allows_parent_rejection_but_not_new_native_dispatch(self):
        runtime = _Runtime(self.authority, None, b"", SimpleNamespace(phase="draining"))
        for next_action in ("rework", "hold"):
            runtime.admit_mutation("native", {"action": "request_changes",
                                             "payload": {"next_action": next_action}})
        for action in ("bootstrap", "claim", "start", "expand"):
            with self.subTest(action=action), self.assertRaises(AuthorityError) as caught:
                runtime.admit_mutation("native", {"action": action})
            self.assertEqual(caught.exception.code, "service_draining")


if __name__ == "__main__":
    unittest.main()
