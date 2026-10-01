"""Independent native-review regression tests against journal and MCP boundaries."""

from copy import deepcopy
from pathlib import Path
import unittest

from authority_fixture import AUTHORITY, uid
from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.model import state_to_dict
from service_fixture import ServiceFixture
import test_native as native_fixture


class NativeReviewBoundaryTests(unittest.TestCase):
    setUp = native_fixture.NativeTests.setUp
    call = native_fixture.NativeTests.call
    bootstrap = native_fixture.NativeTests.bootstrap
    task = native_fixture.NativeTests.task
    scope = native_fixture.NativeTests.scope
    tokens = native_fixture.NativeTests.tokens
    start = native_fixture.NativeTests.start

    def running(self):
        self.bootstrap()
        self.start(agent="reviewed-worker")

    def review_payload(self, **changes):
        return {
            **self.tokens(), "summary": "Acceptance criteria not met",
            "evidence": ["artifact:review/failing-tests"], "next_action": "rework",
            "findings": [
                {"id": "atomicity", "criterion": "Failed completion publishes no graph",
                 "feedback": "Graph revision changed after rejected completion"},
                {"id": "replay", "criterion": "Review survives journal replay",
                 "feedback": "Unresolved findings disappeared on restart"},
            ],
            **changes,
        }

    def request_review(self, **changes):
        return self.call("request_changes", self.review_payload(**changes))

    def resolution(self, **changes):
        review = self.task(self.planner)["native"]["review"]
        return {
            "review_id": review["id"],
            "findings": [
                {"id": finding["id"], "summary": "Correction verified",
                 "evidence": [f"artifact:fixed/{finding['id']}"]}
                for finding in review["findings"]
            ],
            **changes,
        }

    def completion(self, **changes):
        return {
            **self.tokens(), "summary": "Corrected output",
            "evidence": ["artifact:verification"],
            "parent_acceptance": "Inspected each criterion and correction",
            **changes,
        }

    def expansion(self, **changes):
        tokens = self.tokens()
        source = tokens.pop("task_id")
        return {
            **tokens, "source_task_id": source,
            "expected_graph_revision": self.task(self.goal)["graph_revision"],
            "source_disposition": "complete",
            "summary": "Plan corrected", "evidence": ["artifact:corrected-plan"],
            "parent_acceptance": "Verified imported tasks against exact plan",
            "tasks": [{"intent_key": "follow-up", "title": "Follow up",
                       "description": "", "acceptance": "Verified"}],
            **changes,
        }

    def assert_atomic_rejection(self, action, payload, *, code=None, **options):
        before = deepcopy(state_to_dict(self.authority.state))
        count = len(self.log.events)
        with self.assertRaises(AuthorityError) as caught:
            self.call(action, payload, **options)
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.log.events), count)
        self.assertEqual(state_to_dict(self.authority.state), before)

    def attention_ids(self):
        page = self.authority.snapshot(filters={
            "goal_id": self.goal, "needs_attention": True,
        })
        self.assertIsNone(page["next_cursor"])
        return {row["id"] for row in page["rows"]}

    def test_advertised_mcp_schema_accepts_and_dispatches_request_changes(self):
        import jsonschema

        with ServiceFixture() as service:
            serial = 1000
            descriptor = next(tool for tool in service.sdk().tools
                              if tool.name == "mptask_native")
            validator = jsonschema.validators.validator_for(descriptor.inputSchema)(
                descriptor.inputSchema)

            def call(action, payload, *, rejected=False):
                nonlocal serial
                serial += 1
                arguments = {
                    "action": action, "payload": payload, "session_id": native_fixture.SESSION,
                    "command_id": uid(serial), "expected_epoch": service.epoch,
                }
                if not rejected:
                    self.assertTrue(validator.is_valid(arguments),
                                    f"Advertised mptask_native schema rejects {action}")
                before = deepcopy(state_to_dict(service.authority.state))
                count = len(service.log.events)
                result = service.sdk("mptask_native", arguments)
                if rejected:
                    self.assertTrue(result.isError, result.structuredContent)
                    self.assertEqual(state_to_dict(service.authority.state), before)
                    self.assertEqual(len(service.log.events), count)
                else:
                    self.assertFalse(result.isError, result.structuredContent)
                return result.structuredContent

            boot = call("bootstrap", native_fixture.definition())
            goal, planner = boot["goal_id"], boot["planning_task_id"]
            scope = {"project": "demo", "goal_id": goal, "task_id": planner}

            def tokens():
                task = service.authority.get(planner)["task"]
                return {
                    **scope, "expected_version": task["version"],
                    "attempt_id": task["attempt"]["id"],
                    "claim_generation": task["claim_generation"],
                    "native_agent_id": task["attempt"]["native_agent_id"],
                }

            call("claim", {**scope, "expected_version": 1})
            call("start", {**tokens(), "native_agent_id": "sdk-worker"})
            findings = [{"id": "sdk", "criterion": "MCP exposes review",
                         "feedback": "Publish the actual descriptor"}]
            review_payload = {
                **tokens(), "summary": "Request corrections", "evidence": ["artifact:review"],
                "next_action": "rework", "findings": findings,
            }
            for change in (
                {"findings": []}, {"next_action": "complete"}, {"process_stopped": True},
                {"findings": [{**findings[0], "id": "\t"}]},
                {"findings": [{**findings[0], "feedback": "é" * 1025}]},
            ):
                with self.subTest(change=change):
                    call("request_changes", {**review_payload, **change}, rejected=True)
            call("request_changes", review_payload)
            current = service.authority.get(planner)
            review = current["task"]["native"]["review"]
            self.assertEqual(review["id"], uid(serial))
            self.assertEqual(review["status"], "changes_requested")
            self.assertEqual(review["findings"], findings)
            self.assertTrue(current["authorization"]["authorized"])
            attention = service.sdk("mptask_snapshot", {
                "filters": {"goal_id": goal, "needs_attention": True},
            }).structuredContent
            self.assertIn(planner, {row["id"] for row in attention["rows"]})
            completion = {
                **tokens(), "summary": "Fixed", "evidence": ["artifact:tests"],
                "parent_acceptance": "Reviewed corrections",
            }
            call("complete", completion, rejected=True)
            resolution = {
                "review_id": review["id"],
                "findings": [{"id": "sdk", "summary": "Descriptor verified",
                              "evidence": ["artifact:sdk-tests"]}],
            }
            for invalid in (
                {**resolution, "extra": "not permitted"},
                {**resolution, "findings": []},
                {**resolution, "findings": [{**resolution["findings"][0], "evidence": [" "]}]},
            ):
                with self.subTest(resolution=invalid):
                    call("complete", {**completion, "review_resolution": invalid}, rejected=True)
            call("complete", {**completion, "review_resolution": resolution})
            accepted = service.authority.get(planner)["task"]
            self.assertEqual(accepted["status"], "closed")
            self.assertEqual(accepted["native"]["review"]["status"], "accepted")
            persisted = accepted["native"]["review"]["resolution"]
            self.assertEqual(persisted["review_id"], resolution["review_id"])
            self.assertEqual(persisted["findings"], resolution["findings"])
            self.assertEqual(persisted["actor"], native_fixture.SESSION)

    def test_review_is_idempotent_and_survives_authority_restart(self):
        self.running()
        payload = self.review_payload()
        self.call("request_changes", payload, number=9000)
        review = deepcopy(self.task(self.planner)["native"]["review"])
        count = len(self.log.events)
        replay = self.call("request_changes", payload, number=9000)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.log.events), count)
        self.assertEqual(review["id"], uid(9000))
        self.assertEqual(review["actor"], native_fixture.SESSION)
        self.assertEqual(review["at"], self.clock.now())
        self.assertEqual(review["attempt_id"], payload["attempt_id"])
        self.assertEqual(review["claim_generation"], payload["claim_generation"])
        self.assertEqual(review["summary"], payload["summary"])
        self.assertEqual(review["evidence"], payload["evidence"])
        self.assertEqual(review["next_action"], "rework")
        self.assert_atomic_rejection(
            "request_changes", {**payload, "summary": "Different review"},
            number=9000, code="idempotency_conflict")

        old_epoch = self.authority.epoch_id
        self.authority.close()
        self.authority = TaskAuthority(
            AUTHORITY, self.log, Path(self.directory.name) / "restarted",
            clock=self.clock, initialize=False, backoff=lambda _: None).start()
        current = self.authority.get(self.planner)
        self.assertEqual(current["task"]["native"]["review"], review)
        self.assertEqual(current["task"]["status"], "recovering")
        self.assertFalse(current["authorization"]["authorized"])
        self.assertIn(self.planner, self.attention_ids())
        self.assert_atomic_rejection("request_changes", payload,
                                     epoch=old_epoch, code="stale_epoch")
        self.assertEqual(self.task(self.planner)["native"]["review"], review)

    def test_findings_validate_nonblank_utf8_bounds_shape_and_unique_ids(self):
        self.running()
        finding = self.review_payload()["findings"][0]
        invalid_findings = [
            None, {}, [], [finding] * 2,
            [{**finding, "id": str(index)} for index in range(21)],
            [{"id": "missing-feedback", "criterion": "Required"}],
            [{**finding, "unknown": "not allowed"}],
        ]
        for field, overflow in (("id", "é" * 65), ("criterion", "é" * 1025),
                                ("feedback", "é" * 1025)):
            invalid_findings.extend([[{**finding, field: invalid}]
                                     for invalid in ("", " \t\n", None, 42, overflow)])
        for findings in invalid_findings:
            with self.subTest(findings=findings):
                self.assert_atomic_rejection("request_changes",
                                             self.review_payload(findings=findings),
                                             code="validation_error")
        for change in ({"summary": " \t"}, {"summary": "é" * 4097},
                       {"evidence": []}, {"evidence": [" \n"]},
                       {"evidence": ["é" * 1025]}):
            with self.subTest(change=change):
                self.assert_atomic_rejection("request_changes", self.review_payload(**change),
                                             code="validation_error")
        at_limit = [
            {"id": "é" * 64, "criterion": "é" * 1024, "feedback": "é" * 1024},
            *[{"id": str(index), "criterion": "Criterion", "feedback": "Correction"}
              for index in range(19)],
        ]
        self.request_review(findings=at_limit)
        self.assertEqual(self.task(self.planner)["native"]["review"]["findings"], at_limit)

    def test_resolution_requires_exact_ids_nonblank_evidence_and_current_review(self):
        self.running()
        self.request_review()
        valid = self.resolution()
        first, second = valid["findings"]
        malformed = [
            None, {}, {**valid, "review_id": uid(9911)},
            {**valid, "findings": []},
            {**valid, "findings": [first]},
            {**valid, "findings": [first, first]},
            {**valid, "findings": [first, {**second, "id": "unknown"}]},
            {**valid, "unknown": True},
            {**valid, "findings": [{**first, "unknown": True}, second]},
        ]
        for field, values in (
            ("id", ("", " ", "é" * 65, None)),
            ("summary", ("", "\t", "é" * 4097, None)),
            ("evidence", ([], [" "], ["é" * 1025], ["artifact:one"] * 2, None)),
        ):
            malformed.extend({**valid, "findings": [{**first, field: value}, second]}
                             for value in values)
        for resolution in malformed:
            with self.subTest(resolution=resolution):
                self.assert_atomic_rejection(
                    "complete", self.completion(review_resolution=resolution))
        without_acceptance = self.completion(review_resolution=valid)
        del without_acceptance["parent_acceptance"]
        self.assert_atomic_rejection("complete", without_acceptance, code="validation_error")
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=valid, parent_acceptance=" \t"))
        self.call("complete", self.completion(review_resolution={
            **valid, "findings": list(reversed(valid["findings"])),
        }))
        self.assertEqual(self.task(self.planner)["native"]["review"]["status"], "accepted")
        self.assertNotIn(self.planner, self.attention_ids())

    def test_repeat_review_preserves_criteria_and_fences_previous_resolution(self):
        self.running()
        self.request_review()
        original = deepcopy(self.task(self.planner)["native"]["review"])
        old_resolution = self.resolution()
        findings = deepcopy(original["findings"])
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(findings=findings[:1]))
        changed_criterion = deepcopy(findings)
        changed_criterion[0]["criterion"] = "Weaker replacement acceptance"
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(findings=changed_criterion))
        findings[0]["feedback"] = "First fix still leaks graph publication"
        findings.append({"id": "new", "criterion": "Keep current ownership",
                         "feedback": "Old agents must not resolve findings"})
        self.request_review(findings=findings)
        current = self.task(self.planner)["native"]["review"]
        self.assertNotEqual(current["id"], original["id"])
        self.assertEqual(current["findings"], findings)
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=old_resolution))
        self.call("complete", self.completion(review_resolution=self.resolution()))

    def test_expand_completion_gate_rolls_back_all_graph_publication(self):
        self.running()
        self.request_review()
        resolution = self.resolution()
        for change in (
            {}, {"review_resolution": {**resolution, "findings": resolution["findings"][:1]}},
            {"review_resolution": {**resolution, "review_id": uid(9912)}},
            {"review_resolution": resolution, "parent_acceptance": " "},
        ):
            with self.subTest(change=change):
                self.assert_atomic_rejection("expand", self.expansion(**change))
        result = self.call("expand", self.expansion(review_resolution=resolution))
        self.assertEqual(len(result["admitted_task_ids"]), 1)
        self.assertEqual(self.task(result["admitted_task_ids"][0])["status"], "open")
        completed = self.task(self.planner)
        self.assertEqual(completed["status"], "closed")
        self.assertEqual(completed["native"]["review"]["status"], "accepted")
        persisted = completed["native"]["review"]["resolution"]
        self.assertEqual(persisted["review_id"], resolution["review_id"])
        self.assertEqual(persisted["findings"], resolution["findings"])
        self.assertEqual(persisted["actor"], native_fixture.SESSION)

    def test_resolution_cannot_be_smuggled_into_noncompletion_expansion(self):
        self.running()
        self.request_review()
        for disposition in ("continue", "yield"):
            payload = self.expansion(
                source_disposition=disposition, review_resolution=self.resolution(),
                checkpoint={"sequence": 1, "reference": "artifact:checkpoint"},
                reason="Discovered prerequisite", observations=["Output not accepted"],
                edges=[{"source": "follow-up", "target": "$source", "edge_type": "blocks"}])
            with self.subTest(disposition=disposition):
                self.assert_atomic_rejection("expand", payload)
        self.assertEqual(self.task(self.planner)["status"], "in_progress")

    def test_resolution_is_invalid_without_review_on_leaf_epic_and_goal(self):
        self.running()
        resolution = {"review_id": uid(9913), "findings": [
            {"id": "invented", "summary": "Invented acceptance", "evidence": ["artifact:fake"]},
        ]}
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=resolution))
        self.assert_atomic_rejection(
            "expand", self.expansion(review_resolution=resolution))
        self.call("complete", self.completion())
        response = self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "epic", "kind": "epic", "title": "Empty epic",
                 "description": "", "acceptance": "No children remain"},
            ]))
        epic = response["admitted_task_ids"][0]
        outcome = {"summary": "Verified", "evidence": ["artifact:tests"],
                   "parent_acceptance": "Inspected all work"}
        epic_payload = self.scope(task_id=epic, expected_version=self.task(epic)["version"],
                                  **outcome)
        self.assert_atomic_rejection(
            "complete", {**epic_payload, "review_resolution": resolution})
        self.call("complete", epic_payload)
        goal_payload = self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_graph_revision=self.task(self.goal)["graph_revision"], **outcome)
        self.assert_atomic_rejection(
            "goal_close", {**goal_payload, "review_resolution": resolution})
        self.call("goal_close", goal_payload)

    def test_request_changes_requires_running_current_leaf_binding(self):
        self.bootstrap()
        self.call("claim", self.scope(task_id=self.planner, expected_version=1))
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(native_agent_id="not-started"))
        self.call("start", {**self.tokens(), "native_agent_id": "actual-worker"})
        for change, code in (
            ({"expected_version": 1}, "version_conflict"),
            ({"attempt_id": "att_" + uid(9914)}, "stale_generation"),
            ({"claim_generation": 2}, "stale_generation"),
            ({"native_agent_id": "wrong-worker"}, "stale_generation"),
        ):
            with self.subTest(change=change):
                self.assert_atomic_rejection(
                    "request_changes", self.review_payload(**change), code=code)
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(), session=native_fixture.OTHER,
            code="not_owner")
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(), epoch=uid(9915), code="stale_epoch")
        self.assert_atomic_rejection("request_changes", self.review_payload(
            task_id=self.goal, expected_version=self.task(self.goal)["version"]))
        self.call("complete", self.completion())
        self.assert_atomic_rejection("request_changes", self.review_payload())

    def test_hold_retry_preserves_review_without_becoming_readiness_blocker(self):
        self.running()
        old = self.tokens()
        self.request_review(next_action="hold")
        held = self.authority.get(self.planner)
        review = deepcopy(held["task"]["native"]["review"])
        self.assertEqual(held["task"]["status"], "recovering")
        self.assertFalse(held["authorization"]["authorized"])
        self.assertFalse(held["task"]["recovery"]["barrier_satisfied"])
        self.assertIsNone(held["task"]["attempt"]["settlement"])
        self.assertNotIn("process_stopped", held["task"]["recovery"])
        self.assertIn(self.planner, self.attention_ids())
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=self.resolution()))
        self.call("reconcile", {
            **self.tokens(), "reason": "Workspace inspected", "observations": ["Retry approved"],
            "decision": "retry",
        })
        self.assertEqual(self.task(self.planner)["native"]["review"], review)
        self.assertTrue(self.authority.get(self.planner)["eligibility"]["ready"])
        self.assertIn(self.planner, self.attention_ids())
        self.assertIn(self.planner, {
            task["id"] for task in self.authority.ready({"goal_id": self.goal})["tasks"]
        })
        self.start(agent="replacement-worker")
        self.assertEqual(self.task(self.planner)["native"]["review"], review)
        self.assert_atomic_rejection("request_changes", {
            **self.review_payload(), "attempt_id": old["attempt_id"],
            "claim_generation": old["claim_generation"],
            "native_agent_id": old["native_agent_id"],
        }, code="stale_generation")
        self.assert_atomic_rejection("complete", self.completion())
        self.call("complete", self.completion(review_resolution=self.resolution()))
        self.assertEqual(self.task(self.planner)["native"]["review"]["status"], "accepted")

    def test_parent_transfer_preserves_findings_and_fences_inherited_resolution(self):
        self.running()
        self.request_review()
        review = deepcopy(self.task(self.planner)["native"]["review"])
        old_resolution = self.resolution()
        self.call("resume", self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_session_id=native_fixture.SESSION,
            reason="Explicit parent transfer", observations=["Review remains unresolved"]),
            session=native_fixture.OTHER)
        self.assertEqual(self.task(self.planner)["native"]["review"], review)
        self.assert_atomic_rejection("request_changes", self.review_payload(), code="not_owner")
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=old_resolution), code="not_owner")
        self.session = native_fixture.OTHER
        self.assert_atomic_rejection(
            "request_changes", self.review_payload(), code="stale_generation")
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=old_resolution),
            code="stale_generation")
        self.call("release", {
            **self.tokens(), "reason": "Discard inherited execution",
            "observations": ["No inherited result accepted"],
        })
        self.call("reconcile", {
            **self.tokens(), "reason": "Reviewed workspace", "observations": ["Retry explicitly"],
            "decision": "retry",
        })
        self.start(agent="new-parent-worker")
        self.assertEqual(self.task(self.planner)["native"]["review"], review)
        self.assert_atomic_rejection("complete", self.completion())
        self.call("complete", self.completion(review_resolution=old_resolution))
        self.assertEqual(self.task(self.planner)["completion"]["actor"], native_fixture.OTHER)

    def test_cancelled_review_is_not_accepted_or_successful_goal_completion(self):
        self.running()
        self.request_review(next_action="hold")
        review = deepcopy(self.task(self.planner)["native"]["review"])
        self.call("reconcile", {
            **self.tokens(), "reason": "Abandon rejected output",
            "observations": ["Acceptance remains unmet"], "decision": "cancel",
        })
        cancelled = self.task(self.planner)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["completion"])
        self.assertEqual(cancelled["native"]["review"], review)
        self.assert_atomic_rejection(
            "complete", self.completion(review_resolution=self.resolution()))
        self.assert_atomic_rejection("goal_close", self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            summary="Cannot claim success", evidence=["artifact:cancelled"],
            parent_acceptance="Criteria remain unmet"), code="not_ready")
