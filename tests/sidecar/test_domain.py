import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

from mempalace_tasks.domain import apply_event, decide, ready_tasks, task_eligibility
from mempalace_tasks.model import DomainError, new_state, state_from_dict, state_to_dict


AUTHORITY = "11111111-1111-4111-8111-111111111111"
NOW = "2026-09-23T12:00:00Z"


def later(seconds):
    return (datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
            + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(AUTHORITY)
        self.number = 0
        self.send("authority_create", execution_profiles={
            "isolated": {"execution_class": "isolated"},
            "shared": {"execution_class": "shared_unfenced", "supports_reconciliation": True},
            "fenced": {"execution_class": "resource_fenced", "supports_fencing": True,
                       "supports_reconciliation": True},
            "unsafe": {"execution_class": "shared_unfenced"},
        }, actors={"op": "operator", "coord": "coordinator", "worker": "worker",
                   "other": "worker", "sup": "supervisor", "clock": "system"},
                  supervisors={"sup": {"profiles": ["isolated", "shared", "fenced", "unsafe"],
                                       "workers": ["worker", "other"]}})

    def command(self, operation, actor="op", **fields):
        self.number += 1
        return {"operation": operation, "command_id": str(UUID(int=self.number)),
                "actor": actor, **fields}

    def send(self, operation, at=NOW, actor="op", **fields):
        original_state = self.state
        before = state_to_dict(original_state)
        command = self.command(operation, actor=actor, **fields)
        event = decide(original_state, command, at)
        self.assertEqual(state_to_dict(original_state), before)
        self.state = apply_event(original_state, event)
        self.assertEqual(state_to_dict(original_state), before)
        self.assertEqual(state_to_dict(state_from_dict(state_to_dict(self.state))),
                         state_to_dict(self.state))
        return event

    def create(self, title="Task", kind="task", execution_class="isolated",
               execution_profile="isolated", **fields):
        args = {"project": "project", "kind": kind, "title": title,
                "description": "Verbatim description", "acceptance": "Tests pass", **fields}
        if kind == "task":
            args.update(execution_class=execution_class, execution_profile=execution_profile)
        event = self.send("create", **args)
        return event["response"]["task_id"]

    def task(self, task_id):
        return self.state.tasks[task_id]

    def current(self, task_id):
        task = self.task(task_id)
        return {"task_id": task_id, "expected_version": task["version"]}

    def execution(self, task_id):
        task = self.task(task_id)
        return {**self.current(task_id), "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"]}

    def claim(self, task_id, at=NOW):
        self.send("claim", at=at, actor="worker", supervisor_id="sup", **self.current(task_id))

    def start(self, task_id, at=NOW):
        self.send("attempt_report", actor="sup", at=at, report_kind="started",
                  evidence={"references": ["artifact:prepared"], "prepared": True},
                  **self.execution(task_id))

    def close(self, task_id, at=NOW, **fields):
        return self.send("transition", actor="worker", at=at, target="closed",
                         summary="Verified", evidence=["commit:abc"], **self.execution(task_id),
                         **fields)

    def edge(self, source, target, edge_type="blocks", **fields):
        return self.send("add_dependency", source=source, target=target, edge_type=edge_type,
                         expected_source_version=self.task(source)["version"],
                         expected_target_version=self.task(target)["version"], **fields)

    def assert_error(self, code, operation, at=NOW, actor="op", **fields):
        before = state_to_dict(self.state)
        with self.assertRaises(DomainError) as raised:
            decide(self.state, self.command(operation, actor=actor, **fields), at)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(state_to_dict(self.state), before)

    def bootstrap(self, **policy):
        event = self.send("bootstrap", actor="coord", project="project", title="Goal",
                          description="Verbatim goal", acceptance="Deliver and validate",
                          planning_task={"title": "Plan", "description": "Decompose",
                                         "acceptance": "Publish bounded work",
                                         "execution_class": "isolated",
                                         "execution_profile": "isolated"},
                          goal_policy={"scope": "project", **policy})
        return event["response"]["goal_id"], event["response"]["planning_task_id"]

    def expand(self, goal, source=None, actor="worker", at=NOW, **fields):
        args = {"goal_id": goal, "expected_graph_revision": self.task(goal)["graph_revision"],
                "tasks": [{"intent_key": "followup", "title": "Followup",
                           "description": "", "acceptance": "Verified",
                           "execution_class": "isolated", "execution_profile": "isolated"}],
                "source_disposition": "continue", **fields}
        if source:
            args.update(source_task_id=source, expected_version=self.task(source)["version"],
                        attempt_id=self.task(source)["attempt"]["id"],
                        claim_generation=self.task(source)["claim_generation"])
        return self.send("expand", at=at, actor=actor, **args)

    def test_create_is_pure_deterministic_and_replay_detached(self):
        command = self.command("create", project="project", title="title", kind="task",
                               description="", acceptance="pass", execution_class="isolated",
                               execution_profile="isolated")
        before = state_to_dict(self.state)
        event = decide(self.state, command, NOW)
        self.assertEqual(event, decide(self.state, command, NOW))
        self.assertEqual(state_to_dict(self.state), before)
        result = apply_event(self.state, event)
        event["tasks"][0]["title"] = "Mutated"
        self.assertEqual(next(iter(result.tasks.values()))["title"], "title")
        self.assertEqual(state_to_dict(self.state), before)
        self.assertEqual(state_to_dict(state_from_dict(state_to_dict(result))), state_to_dict(result))

    def test_invalid_inputs_never_silently_default(self):
        base = {"project": "project", "kind": "task", "title": "t", "description": "",
                "acceptance": "", "execution_class": "isolated", "execution_profile": "isolated"}
        for patch in ({"priority": True}, {"priority": 5}, {"priority": 1.0},
                      {"title": ""}, {"title": "x" * 257}, {"surprise": 1},
                      {"description": "\u00e9" * 8193}, {"resource_keys": "x"}, {"kind": "bug"}):
            with self.subTest(patch=patch):
                self.assert_error("validation_error", "create", **{**base, **patch})
        self.assert_error("validation_error", "create", at="2026-09-23T12:00:00", **base)
        self.assert_error("validation_error", "create", at="2026-09-23T12:00:00+01:00", **base)

    def test_unknown_actor_cannot_create_or_impersonate_role(self):
        self.assert_error("not_owner", "create", actor="unregistered", project="project",
                          kind="task", title="x", description="", acceptance="",
                          execution_class="isolated", execution_profile="isolated")
        self.assert_error("validation_error", "create",
                          actor={"id": "worker", "role": "operator"}, project="project",
                          kind="task", title="x", description="", acceptance="",
                          execution_class="isolated", execution_profile="isolated")

    def test_update_preserves_omission_clears_nullable_and_checks_version(self):
        tid = self.create(hold_reason="Wait")
        self.send("update", patch={"title": "Edited"}, **self.current(tid))
        self.assertEqual(self.task(tid)["hold_reason"], "Wait")
        self.send("update", patch={"hold_reason": None}, **self.current(tid))
        self.assertIsNone(self.task(tid)["hold_reason"])
        self.assert_error("version_conflict", "update", task_id=tid, expected_version=1,
                          patch={"title": "Old"})
        self.assert_error("validation_error", "update", **self.current(tid), patch={"title": None})

    def test_active_worker_cannot_change_policy_or_introduce_hold(self):
        tid = self.create()
        self.claim(tid)
        for patch in ({"hold_reason": "Wait"}, {"deferred_until": later(100)},
                      {"policy": {"hard_timeout_seconds": 10000}},
                      {"execution_profile": "shared"}):
            with self.subTest(patch=patch):
                self.assert_error("invalid_transition", "update", actor="worker",
                                  **self.execution(tid), patch=patch)
        self.send("update", actor="worker", **self.execution(tid), patch={"title": "Clarified"})
        self.assertEqual(self.task(tid)["title"], "Clarified")

    def test_ready_has_structured_reasons_and_stable_priority_order(self):
        blocked = self.create(title="Blocked", hold_reason="Approval")
        delayed = self.create(title="Later", deferred_until=later(20))
        self.create(title="Epic", kind="epic")
        urgent = self.create(title="Urgent", priority=0)
        self.assertEqual([t["id"] for t in ready_tasks(self.state, {"project": "project"}, NOW)],
                         [urgent])
        self.assertEqual(task_eligibility(self.state, self.task(blocked), NOW)["reasons"][0]["code"],
                         "held")
        self.assertTrue(task_eligibility(self.state, self.task(delayed), later(20))["ready"])

    def test_ready_results_are_detached_and_epics_never_claimable(self):
        tid = self.create()
        epic = self.create(kind="epic")
        result = ready_tasks(self.state, {}, NOW)
        result[0]["title"] = "Changed outside domain"
        self.assertEqual(self.task(tid)["title"], "Task")
        self.assert_error("not_ready", "claim", actor="worker", supervisor_id="sup",
                          **self.current(epic))

    def test_dependency_and_parent_combined_cycle_is_rejected(self):
        epic = self.create(kind="epic")
        child = self.create()
        self.edge(epic, child, "parent_child")
        self.assert_error("dependency_cycle", "add_dependency", source=epic, target=child,
                          edge_type="blocks", expected_source_version=self.task(epic)["version"],
                          expected_target_version=self.task(child)["version"])

    def test_relations_bump_both_versions_and_cancelled_blocker_does_not_satisfy(self):
        a, b = self.create(), self.create()
        self.edge(a, b)
        self.assertEqual((self.task(a)["version"], self.task(b)["version"]), (2, 2))
        self.send("transition", **self.current(a), target="cancelled", reason="Not doing")
        self.assertFalse(task_eligibility(self.state, self.task(b), NOW)["ready"])
        self.send("remove_dependency", source=a, target=b, edge_type="blocks", reason="Replace",
                  expected_source_version=self.task(a)["version"],
                  expected_target_version=self.task(b)["version"])
        self.assertTrue(task_eligibility(self.state, self.task(b), NOW)["ready"])

    def test_graph_rejects_cross_project_self_edges_and_second_parent(self):
        a, b = self.create(), self.create(project="other-project")
        self.assert_error("validation_error", "add_dependency", source=a, target=b,
                          edge_type="related", expected_source_version=1, expected_target_version=1)
        self.assert_error("validation_error", "add_dependency", source=a, target=a,
                          edge_type="related", expected_source_version=1, expected_target_version=1)
        parent, other_parent = self.create(kind="epic"), self.create(kind="epic")
        self.edge(parent, a, "parent_child")
        self.assert_error("invalid_transition", "add_dependency", source=other_parent, target=a,
                          edge_type="parent_child", expected_source_version=1,
                          expected_target_version=self.task(a)["version"])

    def test_related_normalizes_and_rejects_duplicate(self):
        a, b = self.create(), self.create()
        self.edge(a, b, "related")
        self.assert_error("already_exists", "add_dependency", source=b, target=a,
                          edge_type="related", expected_source_version=self.task(b)["version"],
                          expected_target_version=self.task(a)["version"])

    def test_cannot_add_operational_blocker_to_active_task(self):
        a, b = self.create(), self.create()
        self.claim(b)
        self.assert_error("invalid_transition", "add_dependency", source=a, target=b,
                          edge_type="blocks", expected_source_version=self.task(a)["version"],
                          expected_target_version=self.task(b)["version"])

    def test_claim_requires_registered_supervisor_and_preparation(self):
        tid = self.create()
        self.assert_error("unsupported_execution_profile", "claim", actor="worker",
                          supervisor_id="missing", **self.current(tid))
        self.claim(tid)
        task = self.task(tid)
        self.assertEqual(task["claim_generation"], 1)
        self.assertEqual(task["lease_revision"], 1)
        self.assertEqual(task["lease_expires_at"], later(300))
        self.assertEqual(task["attempt"]["progress_deadline"], later(1800))
        self.assertEqual(task["attempt"]["hard_deadline"], later(7200))
        self.assertEqual(task["attempt"]["status"], "preparing")
        self.assert_error("invalid_transition", "transition", actor="worker", target="closed",
                          summary="done", evidence=["artifact:result"], **self.execution(tid))
        self.start(tid)
        self.assertEqual(self.task(tid)["attempt"]["status"], "running")

    def test_shared_task_requires_keys_and_resource_fenced_profile_requires_capability(self):
        self.assert_error("validation_error", "create", project="project", kind="task",
                          title="Shared", description="", acceptance="",
                          execution_class="shared_unfenced", execution_profile="shared")
        self.assert_error("unsupported_execution_profile", "create", project="project",
                          kind="task", title="Fenced", description="", acceptance="",
                          execution_class="resource_fenced", execution_profile="shared",
                          resource_keys=["db"])

    def test_renew_changes_lease_not_task_version_and_rejects_stale_revision(self):
        tid = self.create()
        self.claim(tid)
        version = self.task(tid)["version"]
        execution = self.execution(tid)
        execution.pop("expected_version")
        self.send("renew", actor="sup", at=later(100), expected_lease_revision=1, **execution)
        self.assertEqual(self.task(tid)["version"], version)
        self.assertEqual(self.task(tid)["lease_revision"], 2)
        self.assertEqual(self.task(tid)["lease_expires_at"], later(400))
        self.assert_error("version_conflict", "renew", actor="sup", at=later(100),
                          expected_lease_revision=1, **execution)

    def test_expired_or_wrong_generation_cannot_renew_or_complete(self):
        tid = self.create()
        self.claim(tid)
        self.start(tid)
        execution = self.execution(tid)
        execution.pop("expected_version")
        self.assert_error("lease_expired", "renew", actor="sup", at=later(300),
                          expected_lease_revision=1, **execution)
        self.assert_error("stale_generation", "renew", actor="sup", at=later(10),
                          expected_lease_revision=1, **{**execution, "claim_generation": 99})
        self.assert_error("not_owner", "transition", actor="other", target="closed",
                          summary="done", evidence=["artifact:result"], **self.execution(tid))
        self.assert_error("lease_expired", "transition", actor="worker", at=later(300),
                          target="closed", summary="late", evidence=["artifact:result"],
                          **self.execution(tid))

    def test_checkpoints_are_fresh_supervisor_progress_and_notes_are_not(self):
        tid = self.create()
        self.claim(tid)
        self.start(tid)
        self.send("note", actor="worker", **self.execution(tid), tag="log", text="busy")
        self.assertEqual(self.task(tid)["attempt"]["progress_deadline"], later(1800))
        self.send("checkpoint", actor="sup", at=later(100), checkpoint_sequence=1,
                  reference="artifact:changed", **self.execution(tid))
        self.assertEqual(self.task(tid)["attempt"]["progress_deadline"], later(1900))
        self.assert_error("validation_error", "checkpoint", actor="sup", at=later(110),
                          checkpoint_sequence=1, reference="artifact:changed", **self.execution(tid))
        self.assert_error("validation_error", "checkpoint", actor="sup", at=later(110),
                          checkpoint_sequence=2, reference="artifact:changed", **self.execution(tid))
        self.assert_error("not_owner", "checkpoint", actor="worker", at=later(110),
                          checkpoint_sequence=2, reference="artifact:new", **self.execution(tid))

    def test_checkpoint_cannot_extend_hard_deadline_and_renew_caps_progress(self):
        tid = self.create(policy={"lease_ttl_seconds": 30, "renewal_seconds": 10,
                                 "progress_timeout_seconds": 40, "hard_timeout_seconds": 70})
        self.claim(tid)
        self.start(tid)
        execution = self.execution(tid)
        execution.pop("expected_version")
        self.send("renew", actor="sup", at=later(20), expected_lease_revision=1, **execution)
        self.assertEqual(self.task(tid)["lease_expires_at"], later(40))
        self.send("checkpoint", actor="sup", at=later(30), checkpoint_sequence=1,
                  reference="artifact:first", **self.execution(tid))
        self.send("renew", actor="sup", at=later(35), expected_lease_revision=2, **execution)
        self.send("checkpoint", actor="sup", at=later(50), checkpoint_sequence=2,
                  reference="artifact:second", **self.execution(tid))
        self.assertEqual(self.task(tid)["attempt"]["progress_deadline"], later(70))
        self.send("renew", actor="sup", at=later(55), expected_lease_revision=3, **execution)
        self.assertEqual(self.task(tid)["lease_expires_at"], later(70))

    def test_expiry_candidate_cannot_revoke_renewed_lease(self):
        tid = self.create()
        self.claim(tid)
        candidate = {**self.execution(tid), "expected_lease_revision": 1}
        renewal = {key: value for key, value in candidate.items() if key != "expected_version"}
        self.send("renew", actor="sup", at=later(100), **renewal)
        self.assert_error("version_conflict", "expire", actor="clock", at=later(300), **candidate)
        self.assertEqual(self.task(tid)["status"], "in_progress")

    def test_isolated_expiry_recovery_backoff_and_new_generation(self):
        tid = self.create()
        self.claim(tid)
        old_attempt = self.task(tid)["attempt"]["id"]
        self.send("expire", actor="clock", at=later(300), expected_lease_revision=1,
                  **self.execution(tid))
        self.assertEqual(self.task(tid)["status"], "recovering")
        self.send("recover", at=later(300), retry_decision="preserve",
                  evidence={"references": ["artifact:discard"], "publication_revoked": True},
                  **self.current(tid))
        self.assertEqual(self.task(tid)["automatic_retries_used"], 1)
        self.assertFalse(task_eligibility(self.state, self.task(tid), later(329))["ready"])
        self.claim(tid, at=later(330))
        self.assertEqual(self.task(tid)["claim_generation"], 2)
        self.assertNotEqual(self.task(tid)["attempt"]["id"], old_attempt)

    def test_shared_recovery_retains_resource_until_quiescent(self):
        a = self.create(execution_class="shared_unfenced", execution_profile="shared",
                        resource_keys=["db"])
        b = self.create(execution_class="shared_unfenced", execution_profile="shared",
                        resource_keys=["db"])
        self.claim(a)
        self.send("release", actor="worker", reason="Yield", **self.execution(a))
        self.assertFalse(task_eligibility(self.state, self.task(b), NOW)["ready"])
        self.assert_error("recovery_required", "recover", **self.current(a),
                          retry_decision="preserve", evidence={"references": ["artifact:stopped"],
                                                               "process_stopped": True})
        self.send("recover", **self.current(a), retry_decision="preserve",
                  evidence={"references": ["artifact:reconciled"], "process_stopped": True,
                            "effects_reconciled": True})
        self.claim(b)
        self.assertEqual(self.state.resources["db"]["counter"], 2)
        self.assertEqual(self.task(b)["attempt"]["resource_fences"], {"db": 2})
        self.assertEqual(self.task(a)["automatic_retries_used"], 0)

    def test_worker_cannot_forge_recovery_and_unlock_shared_resources(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="shared",
                          resource_keys=["db"])
        self.claim(tid)
        self.send("release", actor="worker", reason="Stop", **self.execution(tid))
        self.assert_error("not_owner", "recover", actor="worker", **self.current(tid),
                          retry_decision="preserve", evidence={"references": ["artifact:claim"],
                          "process_stopped": True, "effects_reconciled": True})
        self.assertIsNotNone(self.state.resources["db"]["reservation"])

    def test_fenced_recovery_advances_authority_wide_fence(self):
        tid = self.create(execution_class="resource_fenced", execution_profile="fenced",
                          resource_keys=["db"])
        self.claim(tid)
        self.send("release", actor="worker", reason="Stop", **self.execution(tid))
        self.assert_error("recovery_required", "recover", **self.current(tid),
                          retry_decision="preserve", evidence={"references": ["artifact:r"],
                          "effects_reconciled": True, "installed_fences": {"db": 1}})
        self.send("recover", **self.current(tid), retry_decision="preserve",
                  evidence={"references": ["artifact:r"], "effects_reconciled": True,
                            "installed_fences": {"db": 2}})
        self.assertEqual(self.state.resources["db"]["counter"], 2)
        self.claim(tid)
        self.assertEqual(self.task(tid)["attempt"]["resource_fences"]["db"], 3)

    def test_no_adapter_quarantines_but_unrelated_work_is_ready(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="unsafe",
                          resource_keys=["db"])
        unrelated = self.create()
        self.claim(tid)
        self.send("expire", actor="clock", at=later(300), expected_lease_revision=1,
                  **self.execution(tid))
        self.assertEqual(self.task(tid)["status"], "quarantined")
        self.assertIsNotNone(self.state.resources["db"]["reservation"])
        self.assertTrue(task_eligibility(self.state, self.task(unrelated), later(300))["ready"])

    def test_four_failed_attempts_exhaust_three_retry_budget(self):
        tid = self.create()
        now = 0
        for index in range(4):
            self.claim(tid, at=later(now))
            now += 300
            self.send("expire", actor="clock", at=later(now), expected_lease_revision=1,
                      **self.execution(tid))
            self.send("recover", at=later(now), retry_decision="preserve",
                      evidence={"references": ["artifact:discard"], "publication_revoked": True},
                      **self.current(tid))
            if index < 3:
                now += [30, 60, 120][index]
        self.assertEqual(self.task(tid)["automatic_retries_used"], 3)
        self.assertEqual(self.task(tid)["status"], "quarantined")
        self.assertEqual(self.task(tid)["escalation"]["code"], "retry_budget_exhausted")

    def test_supervisor_failure_report_revokes_authorization(self):
        tid = self.create()
        self.claim(tid)
        self.start(tid)
        self.send("attempt_report", actor="sup", report_kind="failed", reason="Process exited",
                  evidence={"references": ["artifact:exit"]}, **self.execution(tid))
        self.assertEqual(self.task(tid)["status"], "recovering")
        self.assert_error("stale_generation", "transition", actor="worker", target="closed",
                          summary="Late result", evidence=["artifact:late"], **self.execution(tid))

    def test_close_needs_evidence_and_terminal_content_is_immutable(self):
        tid = self.create()
        self.claim(tid)
        self.start(tid)
        self.assert_error("validation_error", "transition", actor="worker", target="closed",
                          summary="done", evidence=[], **self.execution(tid))
        self.close(tid)
        self.assertEqual(self.task(tid)["completion"]["evidence"], ["commit:abc"])
        self.assert_error("invalid_transition", "update", **self.current(tid), patch={"title": "No"})
        self.send("note", **self.current(tid), tag="audit", text="Accepted")

    def test_shared_completion_requires_supervisor_settlement(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="shared",
                          resource_keys=["db"])
        self.claim(tid)
        self.start(tid)
        self.assert_error("recovery_required", "transition", actor="worker", target="closed",
                          summary="done", evidence=["artifact:r"], **self.execution(tid))
        self.send("attempt_report", actor="sup", report_kind="settled",
                  evidence={"references": ["artifact:settled"], "process_stopped": True,
                            "effects_reconciled": True}, **self.execution(tid))
        self.close(tid)
        self.assertIsNone(self.state.resources["db"]["reservation"])

    def test_active_cancel_reconciles_before_becoming_terminal(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="shared",
                          resource_keys=["db"])
        self.claim(tid)
        self.send("transition", target="cancelled", reason="Stop requested", **self.execution(tid))
        self.assertEqual(self.task(tid)["status"], "recovering")
        self.assertEqual(self.task(tid)["recovery"]["disposition"], "cancel")
        self.send("recover", **self.current(tid), retry_decision="preserve",
                  evidence={"references": ["artifact:settled"], "process_stopped": True,
                            "effects_reconciled": True})
        self.assertEqual(self.task(tid)["status"], "cancelled")

    def test_epic_close_waits_for_children_but_does_not_block_their_claims(self):
        epic, child = self.create(kind="epic"), self.create()
        self.edge(epic, child, "parent_child")
        self.assertTrue(task_eligibility(self.state, self.task(child), NOW)["ready"])
        self.assert_error("not_ready", "transition", **self.current(epic), target="closed",
                          summary="Too early", evidence=["artifact:r"])
        self.send("transition", **self.current(child), target="cancelled", reason="Not required")
        self.send("transition", **self.current(epic), target="closed", summary="Accepted scope",
                  evidence=["artifact:acceptance"])
        self.assertEqual(self.task(epic)["status"], "closed")

    def test_bootstrap_atomically_creates_root_and_ready_planner(self):
        goal, planner = self.bootstrap()
        self.assertEqual(self.task(goal)["kind"], "epic")
        self.assertEqual(self.task(goal)["goal_id"], goal)
        self.assertEqual(self.task(planner)["goal_id"], goal)
        self.assertEqual([t["id"] for t in ready_tasks(self.state, {"goal_id": goal}, NOW)],
                         [planner])
        self.assert_error("not_ready", "goal_close", goal_id=goal,
                          expected_version=self.task(goal)["version"],
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          summary="Nothing ready", evidence=["artifact:acceptance"])

    def test_independent_discovery_keeps_source_live_and_deduplicates_intent(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        first = self.expand(goal, planner)
        discovered = first["response"]["admitted_task_ids"][0]
        self.assertEqual(self.task(planner)["status"], "in_progress")
        self.assertTrue(task_eligibility(self.state, self.task(discovered), NOW)["ready"])
        second = self.expand(goal, planner)
        self.assertEqual(second["response"]["admitted_task_ids"], [discovered])
        self.assertEqual(len(self.state.tasks), 3)

    def test_dependent_discovery_is_never_ready_before_source_closes(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, edges=[
            {"edge_type": "blocks", "source": "$source", "target": "followup"}])
        discovered = event["response"]["admitted_task_ids"][0]
        self.assertFalse(task_eligibility(self.state, self.task(discovered), NOW)["ready"])
        self.close(planner)
        self.assertTrue(task_eligibility(self.state, self.task(discovered), NOW)["ready"])

    def test_prerequisite_discovery_yields_atomically_without_retry_charge(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        old_execution = self.execution(planner)
        event = self.expand(goal, planner, source_disposition="yield", checkpoint={
            "sequence": 1, "reference": "artifact:work-so-far"}, reason="Need prerequisite",
            edges=[{"edge_type": "blocks", "source": "followup", "target": "$source"}])
        prerequisite = event["response"]["admitted_task_ids"][0]
        self.assertEqual(self.task(planner)["status"], "recovering")
        self.assertTrue(task_eligibility(self.state, self.task(prerequisite), NOW)["ready"])
        self.assert_error("stale_generation", "transition", actor="worker", target="closed",
                          summary="Late", evidence=["artifact:late"], **{
                              **old_execution, "expected_version": self.task(planner)["version"]})
        self.send("recover", **self.current(planner), retry_decision="preserve",
                  evidence={"references": ["artifact:discard"], "publication_revoked": True})
        self.assertEqual(self.task(planner)["automatic_retries_used"], 0)
        self.assertFalse(task_eligibility(self.state, self.task(planner), NOW)["ready"])

    def test_expansion_cannot_attach_prerequisite_without_yield(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        before = state_to_dict(self.state)
        with self.assertRaises(DomainError) as raised:
            self.expand(goal, planner, edges=[
                {"edge_type": "blocks", "source": "followup", "target": "$source"}])
        self.assertEqual(raised.exception.code, "invalid_transition")
        self.assertEqual(state_to_dict(self.state), before)

    def test_stale_worker_cannot_admit_or_create_goal_work(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        stale = self.execution(planner)
        self.send("release", actor="worker", reason="Stop", **stale)
        self.assert_error("stale_generation", "expand", actor="worker", goal_id=goal,
                          source_task_id=planner, expected_version=self.task(planner)["version"],
                          attempt_id=stale["attempt_id"], claim_generation=stale["claim_generation"],
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          tasks=[], source_disposition="continue")
        self.assert_error("not_owner", "create", actor="worker", project="project", title="Late",
                          kind="task", description="", acceptance="", goal_id=goal,
                          execution_class="isolated", execution_profile="isolated")

    def test_source_completion_and_final_discovery_are_one_event(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, source_disposition="complete",
                            summary="Plan done", evidence=["artifact:plan"])
        self.assertEqual(self.task(planner)["status"], "closed")
        self.assertEqual(len(event["response"]["admitted_task_ids"]), 1)
        self.assert_error("not_ready", "goal_close", goal_id=goal,
                          expected_version=self.task(goal)["version"],
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          summary="Too early", evidence=["artifact:goal"])

    def test_goal_close_seals_intake_and_requires_current_graph_revision(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        self.close(planner)
        self.assert_error("version_conflict", "goal_close", goal_id=goal,
                          expected_version=self.task(goal)["version"], expected_graph_revision=0,
                          summary="Done", evidence=["artifact:acceptance"])
        self.send("goal_close", actor="coord", goal_id=goal,
                  expected_version=self.task(goal)["version"],
                  expected_graph_revision=self.task(goal)["graph_revision"],
                  summary="Goal accepted", evidence=["artifact:acceptance"])
        self.assertTrue(self.task(goal)["sealed"])
        self.assert_error("invalid_transition", "expand", actor="coord", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"], tasks=[],
                          source_disposition="continue")

    def test_proposals_are_not_ready_and_prevent_goal_close_until_decided(self):
        goal, planner = self.bootstrap(max_tasks=1)
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, tasks=[{
            "intent_key": "proposal", "title": "Out of scope", "description": "",
            "acceptance": "Needs approval", "execution_class": "isolated",
            "execution_profile": "isolated", "admitted": False}])
        proposal = event["response"]["proposed_task_ids"][0]
        self.assertFalse(task_eligibility(self.state, self.task(proposal), NOW)["ready"])
        self.close(planner)
        self.assert_error("not_ready", "goal_close", goal_id=goal,
                          expected_version=self.task(goal)["version"],
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          summary="Done", evidence=["artifact:acceptance"])
        self.send("transition", **self.current(proposal), target="cancelled", reason="Out of scope")
        self.send("goal_close", goal_id=goal, expected_version=self.task(goal)["version"],
                  expected_graph_revision=self.task(goal)["graph_revision"],
                  summary="Scoped goal accepted", evidence=["artifact:acceptance"])

    def test_goal_growth_budget_rejects_entire_publication(self):
        goal, planner = self.bootstrap(max_tasks=1)
        self.claim(planner)
        self.start(planner)
        before = state_to_dict(self.state)
        with self.assertRaises(DomainError) as raised:
            self.expand(goal, planner)
        self.assertEqual(raised.exception.code, "validation_error")
        self.assertEqual(state_to_dict(self.state), before)

    def test_goal_filter_prevents_unrelated_fleet_candidates(self):
        goal, planner = self.bootstrap()
        other_goal, other_planner = self.bootstrap()
        self.assertEqual([t["id"] for t in ready_tasks(self.state, {"goal_id": goal}, NOW)],
                         [planner])
        self.assertEqual([t["id"] for t in ready_tasks(self.state, {"goal_id": other_goal}, NOW)],
                         [other_planner])

    def test_expansion_survives_json_state_restart_without_duplicate_intent(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        first = self.expand(goal, planner)
        restored = json.loads(json.dumps(state_to_dict(self.state)))
        self.state = state_from_dict(restored)
        second = self.expand(goal, planner)
        self.assertEqual(first["response"]["admitted_task_ids"], second["response"]["admitted_task_ids"])
        self.assertEqual(len(self.state.tasks), 3)

    def test_apply_rejects_forged_snapshots_and_wrong_authority(self):
        tid = self.create()
        event = decide(self.state, self.command("update", **self.current(tid),
                                               patch={"title": "Updated"}), NOW)
        forged = copy.deepcopy(event)
        forged["tasks"][0]["version"] = 500
        with self.assertRaises(DomainError) as raised:
            apply_event(self.state, forged)
        self.assertEqual(raised.exception.code, "invariant_violation")
        event["authority_id"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(DomainError):
            apply_event(self.state, event)

    def test_event_contains_only_affected_tasks_and_is_json_native(self):
        a, b = self.create(), self.create()
        event = self.send("update", **self.current(a), patch={"title": "Updated"})
        self.assertEqual([task["id"] for task in event["tasks"]], [a])
        self.assertNotIn(b, json.dumps(event))
        self.assertEqual(json.loads(json.dumps(event)), event)

    def test_goal_provenance_and_notes_remain_allowed_after_sealing(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        self.close(planner)
        self.send("goal_close", goal_id=goal, expected_version=self.task(goal)["version"],
                  expected_graph_revision=self.task(goal)["graph_revision"],
                  summary="Accepted", evidence=["artifact:acceptance"])
        other = self.create()
        self.edge(planner, other, "related")
        self.send("note", **self.current(planner), tag="audit", text="Post-acceptance note")
        self.assertTrue(self.task(goal)["sealed"])

    def test_explicit_reuse_cannot_change_task_definition_silently(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        first = self.expand(goal, planner)
        tid = first["response"]["admitted_task_ids"][0]
        self.assert_error("validation_error", "expand", actor="coord", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          source_disposition="continue", tasks=[{
                              "reuse_task_id": tid, "expected_version": self.task(tid)["version"],
                              "intent_key": "followup", "title": "Silently ignored replacement"}])

    def test_admin_admits_existing_proposal_but_worker_cannot(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        first = self.expand(goal, planner, tasks=[{
            "intent_key": "proposal", "title": "Review", "description": "", "acceptance": "",
            "execution_class": "isolated", "execution_profile": "isolated", "admitted": False}])
        tid = first["response"]["proposed_task_ids"][0]
        spec = {"intent_key": "proposal", "reuse_task_id": tid,
                "expected_version": self.task(tid)["version"], "admitted": True}
        self.assert_error("not_owner", "expand", actor="worker", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          source_task_id=planner, expected_version=self.task(planner)["version"],
                          attempt_id=self.task(planner)["attempt"]["id"],
                          claim_generation=self.task(planner)["claim_generation"],
                          source_disposition="continue", tasks=[spec])
        self.expand(goal, actor="coord", tasks=[spec])
        self.assertTrue(task_eligibility(self.state, self.task(tid), NOW)["ready"])

    def test_snapshot_rejects_broken_attempt_missing_reservation_and_second_parent(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="shared",
                          resource_keys=["db"])
        self.claim(tid)
        for field, value in (("attempt", {}), ("claim_generation", True)):
            snapshot = state_to_dict(self.state)
            snapshot["tasks"][tid][field] = value
            with self.subTest(field=field), self.assertRaises(DomainError):
                state_from_dict(snapshot)
        snapshot = state_to_dict(self.state)
        snapshot["resources"]["db"]["reservation"] = None
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)
        snapshot = state_to_dict(self.state)
        snapshot["tasks"][tid]["attempt"]["unexpected"] = True
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)

    def test_snapshot_rejects_unsealed_closed_goal_and_orphan_member(self):
        goal, planner = self.bootstrap()
        snapshot = state_to_dict(self.state)
        snapshot["tasks"][goal]["status"] = "closed"
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)
        snapshot = state_to_dict(self.state)
        snapshot["edges"] = []
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)

    def test_out_of_range_utc_addition_is_a_domain_error(self):
        tid = self.create()
        self.assert_error("validation_error", "claim", actor="worker", supervisor_id="sup",
                          at="9999-12-31T23:59:59Z", **self.current(tid))

    def test_expansion_yield_retains_unsafe_resource_until_recovery(self):
        goal, planner = self.bootstrap()
        self.send("update", **self.current(planner), patch={
            "execution_class": "shared_unfenced", "execution_profile": "shared",
            "resource_keys": ["db"]})
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, source_disposition="yield", reason="Need migration",
                            checkpoint={"sequence": 1, "reference": "artifact:checkpoint"},
                            tasks=[{"intent_key": "migration", "title": "Migrate", "description": "",
                                    "acceptance": "", "execution_class": "shared_unfenced",
                                    "execution_profile": "shared", "resource_keys": ["db"]}],
                            edges=[{"source": "migration", "target": "$source", "edge_type": "blocks"}])
        tid = event["response"]["admitted_task_ids"][0]
        self.assertEqual(self.task(planner)["status"], "recovering")
        reasons = task_eligibility(self.state, self.task(tid), NOW)["reasons"]
        self.assertIn("resource_busy", [reason["code"] for reason in reasons])
        self.assertEqual(self.task(planner)["automatic_retries_used"], 0)

    def test_unused_conditional_inputs_are_rejected(self):
        tid = self.create()
        self.claim(tid)
        self.assert_error("validation_error", "attempt_report", actor="sup", report_kind="started",
                          reason="Silently ignored", evidence={"references": ["artifact:prep"],
                          "prepared": True}, **self.execution(tid))

    def test_internal_reboot_recovery_revokes_a_not_yet_expired_attempt(self):
        tid = self.create()
        self.claim(tid)
        self.send("attempt_report", actor="clock", report_kind="recovery_started",
                  reason="Reboot invalidated execution handles",
                  evidence={"references": ["artifact:boot-2"]}, **self.execution(tid))
        self.assertEqual(self.task(tid)["status"], "recovering")
        self.assertEqual(self.task(tid)["automatic_retries_used"], 0)

    def test_fresh_default_renewal_at_100_seconds_precedes_300_second_expiry(self):
        tid = self.create()
        self.assertEqual(self.task(tid)["policy"]["renewal_seconds"], 100)
        self.claim(tid)
        args = self.execution(tid)
        del args["expected_version"]
        self.send("renew", actor="sup", at=later(100), expected_lease_revision=1, **args)
        self.assertEqual(self.task(tid)["lease_expires_at"], later(400))

    def test_operator_can_grant_bounded_retry_budget_after_exhaustion(self):
        tid = self.create(policy={"automatic_retries": 0})
        self.claim(tid)
        self.send("expire", actor="clock", at=later(300), expected_lease_revision=1,
                  **self.execution(tid))
        evidence = {"references": ["artifact:discard"], "publication_revoked": True}
        self.send("recover", at=later(300), retry_decision="preserve", evidence=evidence,
                  **self.current(tid))
        self.assertEqual(self.task(tid)["status"], "quarantined")
        self.send("recover", at=later(300), retry_decision="grant", additional_retries=1,
                  evidence=evidence, **self.current(tid))
        self.assertEqual(self.task(tid)["status"], "open")
        self.assertEqual(self.task(tid)["automatic_retries_used"], 1)
        self.claim(tid, at=later(330))
        self.assertEqual(self.task(tid)["claim_generation"], 2)

    def test_proposed_task_cannot_be_used_as_worker_source(self):
        goal, planner = self.bootstrap()
        self.assert_error("not_owner", "expand", actor="worker", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"], tasks=[],
                          source_disposition="continue")

    def test_reuse_proposal_requires_boolean_admission_not_truthy_integer(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        tid = self.expand(goal, planner)["response"]["admitted_task_ids"][0]
        self.assert_error("validation_error", "expand", actor="coord", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          tasks=[{"reuse_task_id": tid, "expected_version": self.task(tid)["version"],
                                  "intent_key": "followup", "admitted": 1}],
                          source_disposition="continue")

    def test_publication_racing_close_requires_new_goal_revision(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        old_close = self.command("goal_close", goal_id=goal,
                                 expected_version=self.task(goal)["version"],
                                 expected_graph_revision=self.task(goal)["graph_revision"],
                                 summary="Done", evidence=["artifact:goal"])
        self.expand(goal, planner, source_disposition="complete", summary="Planned",
                    evidence=["artifact:plan"])
        with self.assertRaises(DomainError) as raised:
            decide(self.state, old_close, NOW)
        self.assertEqual(raised.exception.code, "version_conflict")

    def test_reverse_blocking_cycle_is_rejected(self):
        a, b = self.create(), self.create()
        self.edge(a, b)
        self.assert_error("dependency_cycle", "add_dependency", source=b, target=a,
                          edge_type="blocks", expected_source_version=self.task(b)["version"],
                          expected_target_version=self.task(a)["version"])

    def test_complete_cancelled_only_goal_is_rejected(self):
        goal, planner = self.bootstrap()
        self.send("transition", **self.current(planner), target="cancelled", reason="Skipped")
        self.assert_error("not_ready", "goal_close", goal_id=goal,
                          expected_version=self.task(goal)["version"],
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          summary="No implementation", evidence=["artifact:acceptance"])

    def test_task_snapshots_reject_unknown_goal_fields_as_domain_error(self):
        goal, _ = self.bootstrap()
        snapshot = state_to_dict(self.state)
        del snapshot["tasks"][goal]["goal_policy"]
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)

    def test_resource_release_for_stale_attempt_cannot_unlock_replacement(self):
        tid = self.create(execution_class="shared_unfenced", execution_profile="shared",
                          resource_keys=["db"])
        self.claim(tid)
        stale = self.execution(tid)
        self.send("release", actor="worker", reason="handoff", **stale)
        self.send("recover", **self.current(tid), retry_decision="preserve",
                  evidence={"references": ["artifact:stopped"], "process_stopped": True,
                            "effects_reconciled": True})
        self.claim(tid)
        stale["expected_version"] = self.task(tid)["version"]
        self.assert_error("stale_generation", "release", actor="worker", reason="late", **stale)
        self.assertEqual(self.state.resources["db"]["reservation"]["claim_generation"], 2)

    def test_quarantined_unsafe_yield_can_still_atomically_publish_prerequisite(self):
        goal, planner = self.bootstrap()
        self.send("update", **self.current(planner), patch={
            "execution_class": "shared_unfenced", "execution_profile": "unsafe",
            "resource_keys": ["db"]})
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, source_disposition="yield", reason="Need investigation",
                            checkpoint={"sequence": 1, "reference": "artifact:checkpoint"},
                            edges=[{"edge_type": "blocks", "source": "followup", "target": "$source"}])
        self.assertEqual(self.task(planner)["status"], "quarantined")
        self.assertEqual(len(event["response"]["admitted_task_ids"]), 1)
        self.assertIsNotNone(self.state.resources["db"]["reservation"])

    def test_domain_event_body_has_conservative_payload_limit(self):
        goal, planner = self.bootstrap(max_batch=50)
        self.claim(planner)
        self.start(planner)
        specs = [{"intent_key": f"work-{n}", "title": f"Work {n}", "description": "d" * 16384,
                  "acceptance": "a" * 8192, "execution_class": "isolated",
                  "execution_profile": "isolated"} for n in range(8)]
        before = state_to_dict(self.state)
        with self.assertRaises(DomainError) as raised:
            self.expand(goal, planner, tasks=specs)
        self.assertEqual(raised.exception.code, "validation_error")
        self.assertEqual(state_to_dict(self.state), before)

    def test_repeated_exhausted_recovery_cannot_emit_new_escalation(self):
        tid = self.create(policy={"automatic_retries": 0})
        self.claim(tid)
        self.send("expire", actor="clock", at=later(300), expected_lease_revision=1,
                  **self.execution(tid))
        evidence = {"references": ["artifact:discard"], "publication_revoked": True}
        self.send("recover", at=later(300), retry_decision="preserve", evidence=evidence,
                  **self.current(tid))
        self.assert_error("retry_budget_exhausted", "recover", at=later(310),
                          retry_decision="preserve", evidence=evidence, **self.current(tid))

    def test_inactive_task_edit_rejects_stale_execution_tokens(self):
        tid = self.create()
        self.claim(tid)
        self.send("release", actor="worker", reason="Hand off", **self.execution(tid))
        self.send("recover", **self.current(tid), retry_decision="preserve",
                  evidence={"references": ["artifact:discard"], "publication_revoked": True})
        self.assert_error("validation_error", "update", **self.execution(tid), patch={"title": "New"})

    def test_state_deserialization_validates_all_goal_shapes_before_following_references(self):
        goal, planner = self.bootstrap()
        snapshot = state_to_dict(self.state)
        del snapshot["tasks"][goal]["goal_policy"]
        snapshot["tasks"] = {planner: snapshot["tasks"][planner], goal: snapshot["tasks"][goal]}
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)

    def test_atomic_expansion_can_publish_nested_epic_children(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, tasks=[
            {"intent_key": "group", "kind": "epic", "title": "Group", "description": "",
             "acceptance": "Children accepted"},
            {"intent_key": "step", "title": "Step", "description": "", "acceptance": "",
             "execution_class": "isolated", "execution_profile": "isolated"}],
            edges=[{"edge_type": "parent_child", "source": "group", "target": "step"}])
        group, step = event["response"]["admitted_task_ids"]
        parents = [e["source"] for e in self.state.edges
                   if e["edge_type"] == "parent_child" and e["target"] == step]
        self.assertEqual(parents, [group])
        self.assertTrue(task_eligibility(self.state, self.task(step), NOW)["ready"])

    def test_mutating_implicit_reuse_requires_its_expected_version(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        target = self.expand(goal, planner)["response"]["admitted_task_ids"][0]
        self.assert_error("validation_error", "expand", actor="coord", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          tasks=[{"intent_key": "followup"}], source_disposition="continue",
                          edges=[{"edge_type": "related", "source": "$goal", "target": "followup"}])
        self.expand(goal, actor="coord", tasks=[{
            "intent_key": "followup", "expected_version": self.task(target)["version"]}],
            edges=[{"edge_type": "related", "source": "$goal", "target": "followup"}])

    def test_optional_administrative_tokens_cannot_be_silently_ignored(self):
        tid = self.create()
        self.assert_error("validation_error", "note", **self.current(tid), tag="audit", text="Note",
                          attempt_id="att_invalid", claim_generation=1)

    def test_unsupported_execution_can_be_proposed_but_not_admitted(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        event = self.expand(goal, planner, tasks=[{
            "intent_key": "needs-adapter", "title": "Remote job", "description": "",
            "acceptance": "Needs operator support", "execution_class": "shared_unfenced",
            "execution_profile": "not-installed", "resource_keys": ["remote"], "admitted": False}])
        tid = event["response"]["proposed_task_ids"][0]
        self.assertFalse(task_eligibility(self.state, self.task(tid), NOW)["ready"])
        self.assert_error("unsupported_execution_profile", "expand", actor="coord", goal_id=goal,
                          expected_graph_revision=self.task(goal)["graph_revision"],
                          source_disposition="continue", tasks=[{
                              "reuse_task_id": tid, "expected_version": self.task(tid)["version"],
                              "intent_key": "needs-adapter", "admitted": True}])
        self.send("update", **self.current(tid), patch={"execution_profile": "shared"})
        self.expand(goal, actor="coord", tasks=[{
            "reuse_task_id": tid, "expected_version": self.task(tid)["version"],
            "intent_key": "needs-adapter", "admitted": True}])
        self.assertTrue(task_eligibility(self.state, self.task(tid), NOW)["ready"])

    def test_failed_fencing_recovery_falls_back_only_to_proven_quiescence(self):
        tid = self.create(execution_class="resource_fenced", execution_profile="fenced",
                          resource_keys=["db"])
        self.claim(tid)
        self.send("release", actor="worker", reason="Adapter lost fencing", **self.execution(tid))
        self.assert_error("recovery_required", "recover", **self.current(tid),
                          retry_decision="preserve", evidence={"references": ["artifact:reconcile"],
                          "fencing_unavailable": True, "effects_reconciled": True})
        self.send("recover", **self.current(tid), retry_decision="preserve",
                  evidence={"references": ["artifact:quiescent"], "fencing_unavailable": True,
                            "process_stopped": True, "effects_reconciled": True})
        self.assertIsNone(self.state.resources["db"]["reservation"])
        self.claim(tid)
        self.assertEqual(self.task(tid)["attempt"]["resource_fences"], {"db": 2})
        self.assert_error("recovery_required", "attempt_report", actor="sup", report_kind="started",
                          evidence={"references": ["artifact:prep"], "prepared": True},
                          **self.execution(tid))

    def test_snapshot_rejects_invalid_historical_shared_settlement(self):
        for execution_class, profile in (("shared_unfenced", "shared"), ("resource_fenced", "fenced")):
            with self.subTest(execution_class=execution_class):
                tid = self.create(execution_class=execution_class, execution_profile=profile,
                                  resource_keys=["db"])
                self.claim(tid)
                preparation = {"references": ["artifact:prepared"], "prepared": True}
                if execution_class == "resource_fenced":
                    preparation["resource_fences"] = self.task(tid)["attempt"]["resource_fences"]
                self.send("attempt_report", actor="sup", report_kind="started",
                          evidence=preparation, **self.execution(tid))
                self.send("attempt_report", actor="sup", report_kind="settled",
                          evidence={"references": ["artifact:settled"], "process_stopped": True,
                                    "effects_reconciled": True}, **self.execution(tid))
                for stage in ("settled", "completed"):
                    if stage == "completed":
                        self.close(tid)
                    valid = state_to_dict(self.state)
                    for evidence in (None, {"references": ["artifact:not-a-settlement"]},
                                     {"references": ["artifact:incomplete"], "process_stopped": True,
                                      "effects_reconciled": False}):
                        corrupt = copy.deepcopy(valid)
                        corrupt["tasks"][tid]["attempt"]["settlement"] = evidence
                        with self.subTest(stage=stage, evidence=evidence), self.assertRaises(DomainError):
                            state_from_dict(corrupt)
                    self.assertEqual(state_to_dict(state_from_dict(valid)), valid)

    def test_shared_settled_source_can_expand_and_complete_atomically(self):
        for execution_class, profile in (("shared_unfenced", "shared"), ("resource_fenced", "fenced")):
            with self.subTest(execution_class=execution_class):
                goal, planner = self.bootstrap()
                self.send("update", **self.current(planner), patch={
                    "execution_class": execution_class, "execution_profile": profile,
                    "resource_keys": [profile]})
                self.claim(planner)
                preparation = {"references": ["artifact:prepared"], "prepared": True}
                if execution_class == "resource_fenced":
                    preparation["resource_fences"] = self.task(planner)["attempt"]["resource_fences"]
                self.send("attempt_report", actor="sup", report_kind="started",
                          evidence=preparation, **self.execution(planner))
                with self.assertRaises(DomainError) as raised:
                    self.expand(goal, planner, source_disposition="complete",
                                summary="Before settlement", evidence=["artifact:work"])
                self.assertEqual(raised.exception.code, "recovery_required")
                self.send("attempt_report", actor="sup", report_kind="settled",
                          evidence={"references": ["artifact:settled"], "process_stopped": True,
                                    "effects_reconciled": True}, **self.execution(planner))
                with self.assertRaises(DomainError) as raised:
                    self.expand(goal, planner)
                self.assertEqual(raised.exception.code, "invalid_transition")
                event = self.expand(goal, planner, source_disposition="complete",
                                    summary="Settled work", evidence=["artifact:work"])
                self.assertEqual(self.task(planner)["status"], "closed")
                self.assertIsNone(self.state.resources[profile]["reservation"])
                self.assertEqual(len(event["response"]["admitted_task_ids"]), 1)
                published = event["response"]["admitted_task_ids"][0]
                self.assertTrue(task_eligibility(self.state, self.task(published), NOW)["ready"])

    def test_intent_reuse_normalizes_partial_policy_and_preserves_omitted_fields(self):
        goal, planner = self.bootstrap()
        self.claim(planner)
        self.start(planner)
        spec = {"intent_key": "long-work", "title": "Long work", "description": "", "acceptance": "",
                "execution_class": "isolated", "execution_profile": "isolated",
                "policy": {"hard_timeout_seconds": 10000},
                "deferred_until": "2026-09-23T12:00:10+00:00"}
        first = self.expand(goal, planner, tasks=[spec])
        tid = first["response"]["admitted_task_ids"][0]
        second = self.expand(goal, planner, tasks=[spec])
        self.assertEqual(second["response"]["admitted_task_ids"], [tid])
        self.assertEqual(second["tasks"], [])
        partial = self.expand(goal, planner, tasks=[{"intent_key": "long-work"}])
        self.assertEqual(partial["response"]["admitted_task_ids"], [tid])
        self.assertEqual(self.task(tid)["policy"]["hard_timeout_seconds"], 10000)
        with self.assertRaises(DomainError) as raised:
            self.expand(goal, planner, tasks=[{**spec, "policy": {"hard_timeout_seconds": 11000}}])
        self.assertEqual(raised.exception.code, "already_exists")

    def test_snapshot_rejects_incomplete_policy_before_claim(self):
        tid = self.create()
        valid = state_to_dict(self.state)
        for policy in ({}, {"hard_timeout_seconds": 10000}, {"lease_ttl_seconds": 300},
                       None, {"hard_timeout_seconds": "7200"}):
            corrupt = copy.deepcopy(valid)
            corrupt["tasks"][tid]["policy"] = policy
            with self.subTest(policy=policy), self.assertRaises(DomainError):
                state_from_dict(corrupt)
        restored = state_from_dict(valid)
        event = decide(restored, self.command("claim", actor="worker", supervisor_id="sup",
                                              **self.current(tid)), NOW)
        self.assertEqual(event["response"]["tasks"][0]["attempt"]["hard_deadline"], later(7200))


if __name__ == "__main__":
    unittest.main()
