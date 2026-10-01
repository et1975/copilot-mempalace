"""Cooperative native coordination through the real journal and MCP boundary."""

from copy import deepcopy
from pathlib import Path
import unittest

from authority_fixture import AUTHORITY, Clock, LogClient, command, create, genesis, state_directory, uid
from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.model import state_from_dict, state_to_dict
from service_fixture import ServiceFixture


SESSION = uid(800)
OTHER = uid(801)


def definition():
    return {
        "project": "demo", "title": "Native goal", "description": "", "acceptance": "Tests pass",
        "planning_task": {"title": "Import exact plan", "description": "", "acceptance": ""},
        "goal_policy": {"scope": "Only opted-in work"},
        "plan_references": ["artifact:plan/exact-version-1"],
    }


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.log, self.clock = LogClient(), Clock()
        self.authority = TaskAuthority(
            AUTHORITY, self.log, Path(self.directory.name) / "runtime",
            clock=self.clock, initialize=True, backoff=lambda _: None).start()
        self.addCleanup(lambda: self.authority.close())
        self.authority.execute_current(genesis())
        self.serial = 10
        self.session = SESSION

    def call(self, action, payload, *, session=None, number=None, epoch=None):
        self.serial += 1
        return self.authority.execute({
            "operation": "native", "action": action, "session_id": session or self.session,
            "command_id": uid(number or self.serial), "payload": payload,
        }, expected_epoch=epoch or self.authority.epoch_id)

    def bootstrap(self):
        receipt = self.call("bootstrap", definition())
        self.goal, self.planner = receipt["goal_id"], receipt["planning_task_id"]
        return receipt

    def task(self, task_id):
        return self.authority.get(task_id)["task"]

    def scope(self, **fields):
        return {"project": "demo", "goal_id": self.goal, **fields}

    def tokens(self, task_id=None):
        task = self.task(task_id or self.planner)
        return self.scope(task_id=task["id"], expected_version=task["version"],
                          attempt_id=task["attempt"]["id"],
                          claim_generation=task["claim_generation"],
                          native_agent_id=task["attempt"]["native_agent_id"])

    def start(self, task_id=None, agent=None):
        task = self.task(task_id or self.planner)
        self.call("claim", self.scope(task_id=task["id"], expected_version=task["version"]))
        return self.call("start", {**self.tokens(task["id"]),
                                  "native_agent_id": agent or self.session})

    def complete(self, task_id=None):
        return self.call("complete", {
            **self.tokens(task_id), "summary": "Verified", "evidence": ["artifact:test-result"],
            "parent_acceptance": "Inspected output against acceptance criteria",
        })

    def assertRejected(self, code, action, payload, **options):
        before = len(self.log.events)
        with self.assertRaises(AuthorityError) as caught:
            self.call(action, payload, **options)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.log.events), before)

    def repository_leaves(self):
        self.bootstrap()
        self.start()
        self.complete()
        result = self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "example/api:contract", "title": "R1: Update contract",
                 "description": "Repository example/api; worktree /worktrees/api",
                 "acceptance": "API contract tests pass"},
                {"intent_key": "example/cli:contract", "title": "R1: Update contract",
                 "description": "Repository example/cli; worktree /worktrees/cli",
                 "acceptance": "CLI contract tests pass"},
                {"intent_key": "example/api:verify", "title": "R2: Verify contract",
                 "description": "Repository example/api; worktree /worktrees/api",
                 "acceptance": "Verify the completed API contract"},
            ], edges=[{"source": "example/api:contract", "target": "example/api:verify",
                       "edge_type": "blocks"}]))
        return result["admitted_task_ids"]

    def test_one_parent_can_publish_concurrent_leaves_in_distinct_repositories(self):
        api, cli, _ = self.repository_leaves()
        ready = self.authority.ready({"goal_id": self.goal})["tasks"]
        self.assertEqual({task["id"] for task in ready}, {api, cli})
        self.start(api, agent="returned-api-worker")
        self.start(cli, agent="returned-cli-worker")

        self.assertNotEqual(self.tokens(api)["attempt_id"], self.tokens(cli)["attempt_id"])
        for task_id, agent in ((api, "returned-api-worker"), (cli, "returned-cli-worker")):
            with self.subTest(task_id=task_id):
                current = self.authority.get(task_id)
                self.assertEqual(current["task"]["status"], "in_progress")
                self.assertEqual(current["task"]["attempt"]["native_agent_id"], agent)
                self.assertEqual(current["task"]["attempt"]["owner"], SESSION)
                self.assertTrue(current["authorization"]["authorized"])
                self.call("checkpoint", {
                    **self.tokens(task_id), "checkpoint_sequence": 1,
                    "reference": f"artifact:verified/{agent}",
                })

        for task_id in (cli, api):
            self.complete(task_id)
            self.assertEqual(self.task(task_id)["status"], "closed")
            self.assertEqual(self.task(task_id)["completion"]["actor"], SESSION)
        self.assertEqual(self.task(self.goal)["native"]["session_id"], SESSION)

    def test_other_repository_session_cannot_claim_leaf_or_revoke_existing_attempt(self):
        api, cli, _ = self.repository_leaves()
        self.start(api, agent="returned-api-worker")
        running, goal = self.task(api), self.task(self.goal)

        self.assertRejected("not_owner", "claim", self.scope(
            task_id=cli, expected_version=self.task(cli)["version"]), session=OTHER)

        self.assertEqual(self.task(api), running)
        self.assertEqual(self.task(self.goal), goal)
        self.assertTrue(self.authority.get(api)["authorization"]["authorized"])
        self.assertTrue(self.authority.get(cli)["eligibility"]["ready"])
        self.assertIsNone(self.task(cli)["attempt"])
        self.call("checkpoint", {
            **self.tokens(api), "checkpoint_sequence": 1, "reference": "artifact:still-authorized",
        })
        self.complete(api)

    def test_goal_enumeration_retains_active_and_blocked_repo_work_across_cursors(self):
        api, cli, verify = self.repository_leaves()
        self.start(api, agent="returned-api-worker")
        self.assertEqual(self.authority.snapshot(
            filters={"goal_id": self.goal, "project": "example/api"})["rows"], [])
        self.assertEqual(
            [task["id"] for task in self.authority.ready({"goal_id": self.goal})["tasks"]],
            [cli])
        with self.assertRaises(AuthorityError) as caught:
            self.authority.get("R1")
        self.assertEqual(caught.exception.code, "not_found")

        page = self.authority.snapshot(filters={"goal_id": self.goal}, limit=1)
        rows = list(page["rows"])
        snapshot_id = page["snapshot_id"]
        for _ in range(4):
            self.assertIsNotNone(page["next_cursor"])
            page = self.authority.snapshot(
                filters={"goal_id": self.goal}, limit=1, cursor=page["next_cursor"])
            self.assertEqual(page["snapshot_id"], snapshot_id)
            self.assertFalse(page["fresh"])
            self.assertEqual(page["reason"], "pinned_snapshot")
            rows.extend(page["rows"])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(len(rows), 5)
        indexed = {row["id"]: row for row in rows}
        self.assertEqual(set(indexed), {self.goal, self.planner, api, cli, verify})
        self.assertEqual({row["project"] for row in rows}, {"demo"})
        self.assertEqual(indexed[self.planner]["status"], "closed")
        self.assertEqual(indexed[api]["status"], "in_progress")
        self.assertFalse(indexed[api]["ready"])
        self.assertEqual(indexed[verify]["blockers"], [api])
        self.assertFalse(indexed[verify]["ready"])
        self.assertTrue(indexed[cli]["ready"])
        self.assertEqual(indexed[api]["title"], indexed[cli]["title"])
        for task_id, intent in ((api, "example/api:contract"), (cli, "example/cli:contract")):
            current = self.authority.get(task_id)
            self.assertTrue(current["fresh"])
            self.assertEqual(current["task"]["intent_key"], intent)
            self.assertIn(intent.split(":")[0], current["task"]["description"])

    def test_bootstrap_atomic_without_native_actor_or_supervisor_and_idempotent(self):
        request = definition()
        result = self.call("bootstrap", request, number=11)
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(result["coordination_mode"], "cooperative_native")
        goal = self.task(result["goal_id"])
        self.assertEqual(goal["native"]["plan_references"], ["artifact:plan/exact-version-1"])
        self.assertEqual(goal["native"]["issuing_session_id"], SESSION)
        self.assertEqual(len(self.log.events), 3)  # epoch, genesis, one atomic bootstrap
        replay = self.call("bootstrap", request, number=11)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["goal_id"], result["goal_id"])
        self.assertRejected("idempotency_conflict", "bootstrap",
                            {**request, "title": "Changed"}, number=11)
        self.assertNotIn(SESSION, self.authority.state.configuration["actors"])

    def test_invalid_bootstrap_rolls_back_goal_and_plan(self):
        self.assertRejected("validation_error", "bootstrap", {
            **definition(), "planning_task": {"title": "", "description": "", "acceptance": ""},
        })
        self.assertEqual(self.authority.state.tasks, {})

    def test_full_workflow_through_real_mcp_schema_dispatch(self):
        with ServiceFixture() as service:
            serial = 100
            def call(action, payload):
                nonlocal serial
                serial += 1
                result = service.sdk("mptask_native", {
                    "action": action, "session_id": SESSION, "command_id": uid(serial),
                    "expected_epoch": service.epoch, "payload": payload,
                })
                self.assertFalse(result.isError, result.structuredContent)
                return result.structuredContent
            def task(task_id):
                return service.authority.get(task_id)["task"]
            boot = call("bootstrap", definition())
            goal, planner = boot["goal_id"], boot["planning_task_id"]
            scope = {"project": "demo", "goal_id": goal}
            def tokens(task_id):
                current = task(task_id)
                return {**scope, "task_id": task_id, "expected_version": current["version"],
                        "attempt_id": current["attempt"]["id"],
                        "claim_generation": current["claim_generation"],
                        "native_agent_id": current["attempt"]["native_agent_id"]}
            for task_id, agent in [(planner, SESSION)]:
                call("claim", {**scope, "task_id": task_id, "expected_version": task(task_id)["version"]})
                call("start", {**tokens(task_id), "native_agent_id": agent})
            expanded = call("expand", {
                **scope, "expected_graph_revision": task(goal)["graph_revision"],
                "source_task_id": planner, **{k: v for k, v in tokens(planner).items()
                                              if k not in {"task_id", "project", "goal_id"}},
                "source_disposition": "complete",
                "summary": "Exact plan imported", "evidence": ["artifact:plan/exact-version-1"],
                "parent_acceptance": "Reviewed imported plan",
                "tasks": [{"intent_key": "implement", "title": "Implement", "description": "",
                           "acceptance": "Tests pass"}],
            })
            child = expanded["admitted_task_ids"][0]
            ready = service.sdk("mptask_ready", {"filters": scope}).structuredContent
            self.assertEqual([row["id"] for row in ready["tasks"]], [child])
            call("claim", {**scope, "task_id": child, "expected_version": task(child)["version"]})
            started = call("start", {**tokens(child), "native_agent_id": "actual-returned-agent"})
            auth = next(row for row in started["authorization"]["tasks"] if row["task_id"] == child)
            self.assertTrue(auth["authorized"])
            self.assertFalse(auth["lease_live"])
            call("checkpoint", {**tokens(child), "checkpoint_sequence": 1,
                                "reference": "artifact:checkpoint-1"})
            call("complete", {**tokens(child), "summary": "Done", "evidence": ["artifact:tests"],
                              "parent_acceptance": "Reviewed tests"})
            closed = call("goal_close", {
                **scope, "expected_version": task(goal)["version"],
                "expected_graph_revision": task(goal)["graph_revision"],
                "summary": "Goal achieved", "evidence": ["artifact:tests"],
                "parent_acceptance": "All goal criteria verified",
            })
            self.assertTrue(task(goal)["sealed"])
            self.assertEqual(closed["outcome"], "committed")
            state_from_dict(state_to_dict(service.authority.state))

    def test_session_project_and_goal_scope_are_checked(self):
        self.bootstrap()
        self.assertRejected("not_owner", "claim", self.scope(
            task_id=self.planner, expected_version=1), session=OTHER)
        self.assertRejected("not_owner", "claim", self.scope(
            project="other", task_id=self.planner, expected_version=1))
        first_goal, first_planner = self.goal, self.planner
        self.bootstrap()
        self.assertRejected("not_owner", "claim", self.scope(
            task_id=first_planner, expected_version=1))
        self.start()
        self.assertEqual(self.task(first_goal)["native"]["session_id"], SESSION)

    def test_cooperative_attempt_does_not_expire_or_require_heartbeat(self):
        from mempalace_tasks.maintenance import LeaseMaintenance
        self.bootstrap()
        self.start()
        self.clock.value = "2026-10-29T00:00:00Z"
        maintenance = LeaseMaintenance(self.authority.bound_current(), self.clock,
                                       system_actor="system", recovery_actor="operator")
        result = maintenance.tick()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["expired"], 0)
        self.assertEqual(self.task(self.planner)["status"], "in_progress")
        self.assertIsNone(self.task(self.planner)["lease_expires_at"])
        self.complete()

    def test_start_and_results_require_current_version_generation_and_agent(self):
        self.bootstrap()
        self.start(agent="returned-agent-1")
        tokens = self.tokens()
        self.assertRejected("invalid_transition", "start",
                            {**tokens, "native_agent_id": "returned-agent-2"})
        complete = {**tokens, "summary": "Done", "evidence": ["artifact:tests"],
                    "parent_acceptance": "Reviewed"}
        self.assertRejected("version_conflict", "complete", {**complete, "expected_version": 1})
        self.assertRejected("stale_generation", "complete", {**complete, "claim_generation": 99})
        self.assertRejected("stale_generation", "complete", {**complete, "native_agent_id": "stale"})
        self.assertRejected("validation_error", "complete", {**complete, "parent_acceptance": ""})
        self.assertRejected("stale_epoch", "complete", complete, epoch=uid(999))
        self.complete()

    def test_release_requires_explicit_reconciliation_never_auto_retry(self):
        self.bootstrap()
        self.start()
        self.call("release", {**self.tokens(), "reason": "Interrupted",
                              "observations": ["Agent state unknown"]})
        task = self.task(self.planner)
        self.assertEqual(task["status"], "recovering")
        self.assertRejected("not_ready", "claim", self.scope(
            task_id=self.planner, expected_version=task["version"]))
        self.call("reconcile", {**self.tokens(), "reason": "Inspected host",
                                "observations": ["No work accepted; checked workspace"],
                                "decision": "hold"})
        self.assertEqual(self.task(self.planner)["status"], "recovering")
        self.call("reconcile", {**self.tokens(), "reason": "Retry deliberately",
                                "observations": ["Previous output discarded"], "decision": "retry"})
        self.start(agent="second-agent")
        self.assertEqual(self.task(self.planner)["claim_generation"], 2)
        self.complete()

    def test_resume_is_cas_transfer_and_never_accepts_old_parent_results(self):
        self.bootstrap()
        started = self.start(agent="old-agent")
        self.assertEqual(started["outcome"], "committed")
        self.assertTrue(self.authority.get(self.planner)["authorization"]["authorized"])
        successful_attempt = self.task(self.planner)
        stale = self.tokens()
        old_goal = self.task(self.goal)
        payload = self.scope(expected_version=old_goal["version"], expected_session_id=SESSION,
                             reason="Explicitly resume opted goal", observations=["Old parent interrupted"])
        self.call("resume", payload, session=OTHER)
        inherited = self.authority.get(self.planner)
        self.assertEqual(inherited["task"], successful_attempt)
        self.assertEqual(inherited["task"]["status"], "in_progress")
        self.assertFalse(inherited["authorization"]["authorized"])
        self.assertEqual(inherited["authorization"]["reason"], "native_reconciliation_required")
        self.assertEqual(self.task(self.goal)["native"]["issuing_session_id"], SESSION)
        self.assertEqual(self.task(self.goal)["native"]["session_id"], OTHER)
        self.assertRejected("version_conflict", "resume", payload, session=uid(802))
        self.assertRejected("not_owner", "checkpoint", {
            **stale, "checkpoint_sequence": 1, "reference": "artifact:late-checkpoint"},
        )
        self.assertRejected("not_owner", "complete", {
            **stale, "summary": "Old result", "evidence": ["artifact:old"], "parent_acceptance": "Old"},
        )
        self.session = OTHER
        self.assertRejected("stale_generation", "complete", {
            **self.tokens(), "summary": "No adoption", "evidence": ["artifact:old"],
            "parent_acceptance": "Old result"})

    def test_restart_fences_interrupted_native_without_managed_recovery(self):
        self.bootstrap()
        self.start()
        epoch, stale = self.authority.epoch_id, self.tokens()
        self.authority.close()
        self.authority = TaskAuthority(
            AUTHORITY, self.log, Path(self.directory.name) / "runtime",
            clock=self.clock, backoff=lambda _: None).start()
        self.assertFalse(self.authority.startup_pending)
        self.assertEqual(self.task(self.planner)["status"], "recovering")
        self.assertRejected("stale_epoch", "checkpoint",
                            {**stale, "checkpoint_sequence": 1, "reference": "artifact:old"}, epoch=epoch)
        self.assertRejected("stale_generation", "complete", {
            **self.tokens(), "summary": "No silent restart", "evidence": ["artifact:old"],
            "parent_acceptance": "Old result"})
        state_from_dict(state_to_dict(self.authority.state))

    def test_managed_and_native_entrypoints_cannot_cross_modes(self):
        self.bootstrap()
        managed = self.authority.execute(command(
            500, "create", **{k: v for k, v in create().items()
                             if k not in {"operation", "command_id", "actor"}}),
            expected_epoch=self.authority.epoch_id)["task_id"]
        self.assertRejected("not_owner", "claim", self.scope(task_id=managed, expected_version=1))
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(command(501, "claim", actor="worker", task_id=self.planner,
                                           expected_version=1, supervisor_id="supervisor"),
                                   expected_epoch=self.authority.epoch_id)
        self.assertEqual(caught.exception.code, "coordination_mode_mismatch")
        with self.assertRaises(AuthorityError) as caught:
            self.authority.execute(command(502, "update", task_id=self.goal,
                                           expected_version=self.task(self.goal)["version"],
                                           patch={"title": "Bypass"}),
                                   expected_epoch=self.authority.epoch_id)
        self.assertEqual(caught.exception.code, "coordination_mode_mismatch")

    def test_expansion_atomic_cycle_rejection_and_yield_prerequisite(self):
        self.bootstrap()
        self.start()
        source = self.tokens()
        base = {**self.scope(), "source_task_id": self.planner,
                **{k: v for k, v in source.items() if k not in {"task_id", "project", "goal_id"}},
                "expected_graph_revision": self.task(self.goal)["graph_revision"],
                "source_disposition": "yield", "checkpoint": {"sequence": 1, "reference": "artifact:yield"},
                "reason": "Discovered prerequisite", "observations": ["Stopped accepting planner output"],
                "tasks": [{"intent_key": "prereq", "title": "Prerequisite", "description": "",
                           "acceptance": ""}],
                "edges": [{"source": "prereq", "target": "$source", "edge_type": "blocks"}]}
        cyclic = deepcopy(base)
        cyclic["edges"].append({"source": "$source", "target": "prereq", "edge_type": "blocks"})
        self.assertRejected("dependency_cycle", "expand", cyclic)
        self.assertEqual(len(self.authority.state.tasks), 2)
        receipt = self.call("expand", base)
        self.assertEqual(self.task(self.planner)["status"], "recovering")
        self.assertEqual(len(receipt["admitted_task_ids"]), 1)

    def test_native_schema_rejects_actor_unknown_fields_and_action_payload_mixups(self):
        with ServiceFixture() as service:
            base = {"action": "bootstrap", "session_id": SESSION, "command_id": uid(55),
                    "expected_epoch": service.epoch, "payload": definition()}
            for mutation in (
                {"actor": "operator"}, {"session_id": "not-a-uuid"},
                {"payload": {**definition(), "unknown": True}},
                {"action": "start"}, {"payload": {**definition(), "planning_task": {
                    **definition()["planning_task"], "execution_class": "isolated"}}},
            ):
                response = service.sdk("mptask_native", {**base, **mutation})
                self.assertTrue(response.isError)
                self.assertEqual(response.structuredContent["error"]["code"], "validation_error")
            self.assertEqual(service.authority.state.tasks, {})

    def test_human_inspection_identifies_cooperative_mode_without_unknown_lease(self):
        from mempalace_tasks.inspection import build_frame, render_text
        self.bootstrap()
        self.start(agent="observed-agent")
        text = render_text(build_frame("show", self.authority.get(self.planner)))
        self.assertIn("cooperative_native", text)
        self.assertIn("physical_supervision=false", text)
        self.assertIn("lease_remaining=not_applicable", text)
        self.assertIn(SESSION, text)

    def test_nested_epic_can_close_only_after_leaf_completion(self):
        self.bootstrap()
        self.start()
        self.complete()
        response = self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "subgoal", "kind": "epic", "title": "Nested", "description": "",
                 "acceptance": ""},
                {"intent_key": "leaf", "title": "Leaf", "description": "", "acceptance": ""},
            ], edges=[{"source": "subgoal", "target": "leaf", "edge_type": "parent_child"}]))
        epic, leaf = response["admitted_task_ids"]
        complete = self.scope(task_id=epic, expected_version=self.task(epic)["version"],
                              summary="Subgoal done", evidence=["artifact:subgoal"],
                              parent_acceptance="Inspected nested goal")
        self.assertRejected("not_ready", "complete", complete)
        self.start(leaf)
        self.complete(leaf)
        self.call("complete", complete)
        self.assertEqual(self.task(epic)["status"], "closed")

    def test_snapshot_rejects_fabricated_native_physical_and_reconciliation_evidence(self):
        from mempalace_tasks.model import DomainError
        self.bootstrap()
        self.start()
        snapshot = state_to_dict(self.authority.state)
        snapshot["tasks"][self.planner]["native"]["reconciliation"] = {
            "reason": "Invented proof", "observations": ["none"], "decision": "retry",
            "at": self.clock.now(), "process_stopped": True,
        }
        with self.assertRaises(DomainError):
            state_from_dict(snapshot)

    def test_release_reserved_attempt_and_reconcile_cancellation(self):
        self.bootstrap()
        self.call("claim", self.scope(task_id=self.planner, expected_version=1))
        self.call("release", {**self.tokens(), "reason": "Dispatch response lost",
                              "observations": ["No native agent ID observed"]})
        self.call("reconcile", {**self.tokens(), "reason": "Abandon work",
                                "observations": ["Reviewed uncertain effects"], "decision": "cancel"})
        self.assertEqual(self.task(self.planner)["status"], "cancelled")
        self.assertRejected("not_ready", "goal_close", self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            summary="Not achieved", evidence=["artifact:cancelled"], parent_acceptance="No work done"))

    def test_goal_close_rejects_unfinished_proposals_and_stale_graph(self):
        self.bootstrap()
        self.start()
        self.complete()
        self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "proposal", "title": "Needs decision", "description": "",
                 "acceptance": "", "admitted": False}]))
        payload = self.scope(expected_version=self.task(self.goal)["version"],
                             expected_graph_revision=self.task(self.goal)["graph_revision"],
                             summary="Goal done", evidence=["artifact:tests"], parent_acceptance="Reviewed")
        self.assertRejected("version_conflict", "goal_close", {**payload, "expected_graph_revision": 1})
        self.assertRejected("not_ready", "goal_close", payload)

    def test_interruption_replay_resumes_new_attempt_without_accepting_old_agent(self):
        self.bootstrap()
        self.start(agent="old-agent")
        old = self.tokens()
        self.call("release", {**old, "reason": "Interrupted", "observations": ["Lost contact"]})
        reconcile = {**self.tokens(), "reason": "Explicit retry", "decision": "retry",
                     "observations": ["Workspace inspected"]}
        self.call("reconcile", reconcile, number=700)
        self.assertTrue(self.call("reconcile", reconcile, number=700)["replayed"])
        self.start(agent="new-agent")
        self.assertRejected("stale_generation", "checkpoint", {
            **old, "expected_version": self.task(self.planner)["version"],
            "checkpoint_sequence": 1, "reference": "artifact:old"})
        self.assertRejected("stale_generation", "complete", {
            **self.tokens(), "native_agent_id": "old-agent", "summary": "Old work",
            "evidence": ["artifact:old"], "parent_acceptance": "Stale"})
        self.complete()

    def test_resume_changes_only_owner_and_active_work_not_entire_completed_graph(self):
        self.bootstrap()
        self.start()
        self.complete()
        before = self.task(self.planner)
        self.call("resume", self.scope(
            expected_version=self.task(self.goal)["version"], expected_session_id=SESSION,
            reason="New parent continues", observations=["Completed plan verified"]), session=OTHER)
        self.assertEqual(self.task(self.planner), before)
        state_from_dict(state_to_dict(self.authority.state))

    def test_resume_of_one_goal_does_not_transfer_another_opted_goal(self):
        self.bootstrap()
        first_goal, first_planner = self.goal, self.planner
        self.bootstrap()
        second_goal, second_planner = self.goal, self.planner
        self.goal, self.planner = first_goal, first_planner
        self.start()
        self.call("resume", self.scope(
            expected_version=self.task(self.goal)["version"], expected_session_id=SESSION,
            reason="Only resume named goal", observations=["Old parent interrupted"]), session=OTHER)
        self.goal, self.planner = second_goal, second_planner
        self.assertRejected("not_owner", "claim", self.scope(
            task_id=second_planner, expected_version=1), session=OTHER)
        self.start()

    def test_service_with_no_execution_profiles_or_supervisors_supports_native_mode(self):
        with ServiceFixture(genesis_fields={
            "execution_profiles": {}, "supervisors": {},
            "actors": {"operator": "operator", "system": "system"},
        }) as service:
            result = service.sdk("mptask_native", {
                "action": "bootstrap", "session_id": SESSION, "command_id": uid(55),
                "expected_epoch": service.epoch, "payload": definition(),
            })
            self.assertFalse(result.isError, result.structuredContent)
            self.assertEqual(len(result.structuredContent["tasks"]), 2)

    def test_cancel_unwanted_proposal_without_admitting_or_dispatching_then_close_goal(self):
        self.bootstrap()
        self.start()
        self.complete()
        expanded = self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "unwanted", "title": "Out of scope", "description": "",
                 "acceptance": "", "admitted": False}]))
        proposal = expanded["proposed_task_ids"][0]
        self.assertRejected("not_ready", "claim", self.scope(
            task_id=proposal, expected_version=self.task(proposal)["version"]))
        cancellation = self.scope(
            task_id=proposal, expected_version=self.task(proposal)["version"],
            reason="Outside explicitly approved scope", observations=["Parent rejected this discovery"])
        result = self.call("cancel", cancellation, number=601)
        task = self.task(proposal)
        self.assertEqual(task["status"], "cancelled")
        self.assertIs(task["admitted"], False)
        self.assertIsNone(task["attempt"])
        self.assertEqual(task["claim_generation"], 0)
        self.assertEqual(task["cancellation_reason"], "Outside explicitly approved scope")
        self.assertTrue(self.call("cancel", cancellation, number=601)["replayed"])
        self.assertEqual(result["response"]["task_id"], proposal)
        record = self.authority.log.accepted_records[-1]
        self.assertEqual(record["event"]["command"]["payload"]["observations"],
                         ["Parent rejected this discovery"])
        self.call("goal_close", self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_graph_revision=self.task(self.goal)["graph_revision"], summary="Accepted work complete",
            evidence=["artifact:accepted-result"], parent_acceptance="Rejected scope is documented"))
        self.assertTrue(self.task(self.goal)["sealed"])
        state_from_dict(state_to_dict(self.authority.state))

    def test_cancel_does_not_bypass_native_attempt_reconciliation_or_root_closure(self):
        self.bootstrap()
        def payload(task_id):
            return self.scope(task_id=task_id, expected_version=self.task(task_id)["version"],
                              reason="Cancel work", observations=["Parent decision"])
        self.assertRejected("invalid_transition", "cancel", payload(self.goal))
        self.call("claim", self.scope(task_id=self.planner, expected_version=1))
        self.assertRejected("invalid_transition", "cancel", payload(self.planner))
        self.call("start", {**self.tokens(), "native_agent_id": SESSION})
        self.assertRejected("invalid_transition", "cancel", payload(self.planner))
        self.call("release", {**self.tokens(), "reason": "Interrupted",
                              "observations": ["Worker effects uncertain"]})
        self.assertRejected("invalid_transition", "cancel", payload(self.planner))
        self.call("reconcile", {**self.tokens(), "reason": "Inspected effects",
                                "observations": ["No output accepted"], "decision": "retry"})
        self.call("cancel", payload(self.planner))
        self.assertRejected("invalid_transition", "cancel", payload(self.planner))
        self.assertRejected("not_ready", "goal_close", self.scope(
            expected_version=self.task(self.goal)["version"],
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            summary="Only cancelled work", evidence=["artifact:cancellation"],
            parent_acceptance="No success can be claimed"))

    def test_cancel_nested_epic_requires_finished_children_and_does_not_cascade(self):
        self.bootstrap()
        response = self.call("expand", self.scope(
            expected_graph_revision=self.task(self.goal)["graph_revision"],
            source_disposition="continue", tasks=[
                {"intent_key": "epic", "kind": "epic", "title": "Nested goal", "description": "",
                 "acceptance": ""},
                {"intent_key": "child", "title": "Child", "description": "", "acceptance": ""},
            ], edges=[{"source": "epic", "target": "child", "edge_type": "parent_child"}]))
        epic, child = response["admitted_task_ids"]
        cancellation = self.scope(task_id=epic, expected_version=self.task(epic)["version"],
                                  reason="Reject nested goal", observations=["Parent scope review"])
        self.assertRejected("not_ready", "cancel", cancellation)
        self.assertEqual(self.task(child)["status"], "open")
        self.start(child)
        self.call("release", {**self.tokens(child), "reason": "Interrupted",
                              "observations": ["Child still uncertain"]})
        self.assertRejected("not_ready", "cancel", cancellation)
        self.call("reconcile", {**self.tokens(child), "reason": "Child reconciled",
                                "observations": ["Results discarded"], "decision": "cancel"})
        self.call("cancel", cancellation)
        self.assertEqual(self.task(epic)["status"], "cancelled")

    def test_cancel_requires_current_session_project_goal_mode_and_version(self):
        self.bootstrap()
        cancellation = self.scope(task_id=self.planner, expected_version=1,
                                  reason="Reject task", observations=["Parent decision"])
        self.assertRejected("not_owner", "cancel", cancellation, session=OTHER)
        self.assertRejected("not_owner", "cancel", {**cancellation, "project": "other"})
        self.assertRejected("version_conflict", "cancel", {**cancellation, "expected_version": 2})
        self.assertRejected("validation_error", "cancel", {**cancellation, "observations": []})
        self.assertRejected("validation_error", "cancel", {**cancellation, "reason": ""})
        self.assertRejected("validation_error", "cancel", {**cancellation, "attempt_id": "invented"})
        managed = self.authority.execute(create(501), expected_epoch=self.authority.epoch_id)["task_id"]
        self.assertRejected("not_owner", "cancel", {**cancellation, "task_id": managed})
        self.bootstrap()
        self.assertRejected("not_owner", "cancel", {**cancellation, "goal_id": self.goal})

    def test_cancel_is_a_real_strict_mcp_action_not_a_managed_transition(self):
        with ServiceFixture() as service:
            def call(action, number, payload):
                response = service.sdk("mptask_native", {
                    "action": action, "session_id": SESSION, "command_id": uid(number),
                    "expected_epoch": service.epoch, "payload": payload,
                })
                self.assertFalse(response.isError, response.structuredContent)
                return response.structuredContent
            boot = call("bootstrap", 51, definition())
            result = call("cancel", 52, {
                "project": "demo", "goal_id": boot["goal_id"], "task_id": boot["planning_task_id"],
                "expected_version": 1, "reason": "Never started",
                "observations": ["Parent withdrew this plan"],
            })
            task = next(task for task in result["tasks"] if task["id"] == boot["planning_task_id"])
            self.assertEqual(task["status"], "cancelled")
            self.assertIsNone(task["attempt"])

    def test_update_unholds_and_undefer_legitimate_native_work_without_replacement(self):
        boot = definition()
        boot["planning_task"].update(hold_reason="Waiting for parent review",
                                     deferred_until="2099-01-01T00:00:00Z")
        receipt = self.call("bootstrap", boot)
        self.goal, self.planner = receipt["goal_id"], receipt["planning_task_id"]
        self.assertRejected("not_ready", "claim", self.scope(task_id=self.planner, expected_version=1))
        updated = self.call("update", self.scope(
            task_id=self.planner, expected_version=1,
            patch={"hold_reason": None, "deferred_until": None, "acceptance": "Review completed"}))
        self.assertEqual(updated["task_id"], self.planner)
        self.assertEqual(self.task(self.planner)["version"], 2)
        self.assertEqual(len(self.authority.state.tasks), 2)
        self.assertEqual(self.task(self.goal)["graph_revision"], 1)
        self.assertTrue(self.authority.get(self.planner)["eligibility"]["ready"])
        self.start()
        self.complete()
        state_from_dict(state_to_dict(self.authority.state))

    def test_native_update_reuses_content_validation_and_denies_execution_or_admission(self):
        self.bootstrap()
        valid = self.scope(task_id=self.planner, expected_version=1, patch={"title": "Reviewed plan"})
        for patch in (
            {}, {"title": ""}, {"description": "é" * 8193}, {"acceptance": "é" * 4097},
            {"deferred_until": "tomorrow"}, {"priority": True}, {"admitted": False},
            {"execution_class": "isolated"}, {"native": {"session_id": OTHER}},
        ):
            self.assertRejected("validation_error", "update", {**valid, "patch": patch})
        self.call("update", {**valid, "patch": {"priority": 0, "description": "", "acceptance": ""}})
        self.assertEqual(self.task(self.planner)["priority"], 0)

    def test_native_update_checks_scope_version_lifecycle_and_new_parent_after_resume(self):
        self.bootstrap()
        def payload(task_id=None):
            task_id = task_id or self.planner
            return self.scope(task_id=task_id, expected_version=self.task(task_id)["version"],
                              patch={"hold_reason": None})
        self.assertRejected("not_owner", "update", payload(), session=OTHER)
        self.assertRejected("not_owner", "update", {**payload(), "project": "other"})
        self.assertRejected("version_conflict", "update", {**payload(), "expected_version": 99})
        self.assertRejected("invalid_transition", "update", payload(self.goal))
        managed = self.authority.execute(create(502), expected_epoch=self.authority.epoch_id)["task_id"]
        self.assertRejected("not_owner", "update", {**payload(), "task_id": managed})
        other_goal = self.call("bootstrap", definition())["goal_id"]
        self.assertRejected("not_owner", "update", {**payload(), "goal_id": other_goal})
        self.start()
        self.assertRejected("invalid_transition", "update", payload())
        self.call("release", {**self.tokens(), "reason": "Interrupted", "observations": ["Review needed"]})
        self.assertRejected("invalid_transition", "update", payload())
        self.call("reconcile", {**self.tokens(), "reason": "Safe to revisit",
                                "observations": ["Workspace reviewed"], "decision": "retry"})
        old = payload()
        self.call("resume", self.scope(
            expected_version=self.task(self.goal)["version"], expected_session_id=SESSION,
            reason="New parent", observations=["Explicit scoped continuation"]), session=OTHER)
        self.assertRejected("not_owner", "update", old)
        self.call("update", old, session=OTHER)
        self.assertRejected("version_conflict", "update", old, session=OTHER)
        self.session = OTHER
        self.start()
        self.complete()
        self.assertRejected("invalid_transition", "update", payload())

    def test_open_nested_epic_content_can_be_updated_through_real_mcp_schema(self):
        with ServiceFixture() as service:
            def call(action, number, payload):
                result = service.sdk("mptask_native", {
                    "action": action, "session_id": SESSION, "command_id": uid(number),
                    "expected_epoch": service.epoch, "payload": payload,
                })
                self.assertFalse(result.isError, result.structuredContent)
                return result.structuredContent
            boot = call("bootstrap", 81, definition())
            scope = {"project": "demo", "goal_id": boot["goal_id"]}
            expanded = call("expand", 82, {
                **scope, "expected_graph_revision": 1, "source_disposition": "continue",
                "tasks": [{"intent_key": "held-epic", "kind": "epic", "title": "Held epic",
                           "description": "", "acceptance": "", "hold_reason": "Parent review"}],
            })
            epic = expanded["admitted_task_ids"][0]
            version = service.authority.get(epic)["task"]["version"]
            result = call("update", 83, {
                **scope, "task_id": epic, "expected_version": version,
                "patch": {"hold_reason": None, "title": "Approved epic"},
            })
            task = next(task for task in result["tasks"] if task["id"] == epic)
            self.assertEqual(task["title"], "Approved epic")
            self.assertIsNone(task["hold_reason"])

    def large_active_graph(self):
        self.bootstrap()
        self.start()
        self.complete()
        task_ids = []
        for index in range(8):
            expanded = self.call("expand", self.scope(
                expected_graph_revision=self.task(self.goal)["graph_revision"],
                source_disposition="continue", tasks=[
                    {"intent_key": f"large-{index}", "title": f"Large task {index}",
                     "description": "x" * 16384, "acceptance": ""}]))
            task_id = expanded["admitted_task_ids"][0]
            self.start(task_id, agent=f"native-agent-{index}")
            task_ids.append(task_id)
        return task_ids

    def test_resume_is_bounded_root_only_cas_with_explicit_inherited_recovery(self):
        from mempalace_tasks.codec import canonical_json
        from mempalace_tasks.protocol import MAX_PAYLOAD_BYTES
        task_ids = self.large_active_graph()
        before = {task_id: self.task(task_id) for task_id in task_ids}
        self.assertGreater(len(canonical_json(list(before.values())).encode()) * 2, MAX_PAYLOAD_BYTES)
        payload = self.scope(
            expected_version=self.task(self.goal)["version"], expected_session_id=SESSION,
            reason="Continue bounded goal", observations=["Prior parent interrupted"])
        result = self.call("resume", payload, session=OTHER, number=701)
        self.assertEqual([task["id"] for task in result["tasks"]], [self.goal])
        self.assertTrue(self.call("resume", payload, session=OTHER, number=701)["replayed"])
        for task_id in task_ids:
            self.assertEqual(self.task(task_id), before[task_id])
            observed = self.authority.get(task_id)
            self.assertFalse(observed["authorization"]["authorized"])
            self.assertEqual(observed["authorization"]["session_id"], OTHER)
            self.assertEqual(observed["authorization"]["reason"], "native_reconciliation_required")
            self.assertIn({"code": "native_reconciliation_required"}, observed["eligibility"]["reasons"])
        snapshot = self.authority.snapshot(filters={"goal_id": self.goal, "needs_attention": True})
        self.assertEqual({row["id"] for row in snapshot["rows"]}, set(task_ids))
        self.assertTrue(all(row["needs_attention"] for row in snapshot["rows"]))
        state_from_dict(state_to_dict(self.authority.state))
        for task_id in task_ids:
            self.assertRejected("not_owner", "complete", {
                **self.tokens(task_id), "summary": "Old parent output", "evidence": ["artifact:old"],
                "parent_acceptance": "Old parent accepted"})
        self.session = OTHER
        self.assertRejected("stale_generation", "complete", {
            **self.tokens(task_ids[0]), "summary": "Cannot adopt", "evidence": ["artifact:old"],
            "parent_acceptance": "New parent cannot adopt running work"})
        for task_id in task_ids:
            self.call("release", {**self.tokens(task_id), "reason": "Inspect inherited work",
                                  "observations": ["Old agent association observed, result not accepted"]})
            self.call("reconcile", {**self.tokens(task_id), "reason": "Effects reviewed",
                                    "observations": ["Work can restart"], "decision": "retry"})
        self.start(task_ids[0], agent="new-generation-agent")
        self.assertEqual(self.task(task_ids[0])["claim_generation"], 2)
        self.complete(task_ids[0])
        state_from_dict(state_to_dict(self.authority.state))

    def test_large_native_restart_interrupts_each_attempt_in_bounded_journal_events(self):
        from mempalace_tasks.codec import canonical_json
        from mempalace_tasks.protocol import MAX_PAYLOAD_BYTES
        task_ids = self.large_active_graph()
        self.call("resume", self.scope(
            expected_version=self.task(self.goal)["version"], expected_session_id=SESSION,
            reason="Transfer before restart", observations=["Old parent interrupted"]), session=OTHER)
        old_epoch = self.authority.epoch_id
        self.authority.close()
        sent = len(self.log.sent)
        self.authority = TaskAuthority(
            AUTHORITY, self.log, Path(self.directory.name) / "runtime",
            clock=self.clock, backoff=lambda _: None).start()
        self.assertFalse(self.authority.startup_pending)
        interrupts = [record for record in self.log.sent[sent:]
                      if record["record_type"] == "mptask.command"]
        self.assertEqual(len(interrupts), 8)
        self.assertTrue(all(len(record["event"]["tasks"]) == 1 for record in interrupts))
        self.assertTrue(all(len(canonical_json(record).encode()) <= MAX_PAYLOAD_BYTES
                            for record in interrupts))
        self.assertTrue(all(self.task(task_id)["status"] == "recovering" for task_id in task_ids))
        self.assertRejected("stale_epoch", "complete", {
            **self.tokens(task_ids[0]), "summary": "Old packet", "evidence": ["artifact:old"],
            "parent_acceptance": "Old parent"}, epoch=old_epoch)
        self.session = OTHER
        for task_id in task_ids:
            self.call("reconcile", {**self.tokens(task_id), "reason": "Restart effects reviewed",
                                    "observations": ["Work deliberately abandoned"], "decision": "cancel"})
        state_from_dict(state_to_dict(self.authority.state))

    def test_root_only_resume_does_not_reauthorize_old_attempt_if_session_uuid_returns(self):
        self.bootstrap()
        self.start(agent="original-agent")
        for old_session, next_session in ((SESSION, OTHER), (OTHER, SESSION)):
            self.call("resume", self.scope(
                expected_version=self.task(self.goal)["version"], expected_session_id=old_session,
                reason="Explicit new parent transfer", observations=["Previous parent interrupted"]),
                session=next_session)
        observed = self.authority.get(self.planner)
        self.assertFalse(observed["authorization"]["authorized"])
        self.assertEqual(observed["authorization"]["reason"], "native_reconciliation_required")
        self.assertRejected("stale_generation", "complete", {
            **self.tokens(), "summary": "Old result from returning UUID", "evidence": ["artifact:old"],
            "parent_acceptance": "Cannot revive inherited execution"})
        self.call("release", {**self.tokens(), "reason": "Discard inherited association",
                              "observations": ["Old attempt belongs to prior transfer generation"]})
        self.call("reconcile", {**self.tokens(), "reason": "Workspace reconciled",
                                "observations": ["New attempt required"], "decision": "retry"})
        self.start(agent="new-agent")
        self.complete()


class NativeRestoreTests(unittest.TestCase):
    def test_private_restore_preserves_root_only_transfer_and_inherited_attempt_uncertainty(self):
        from mempalace_tasks.protocol import fold_record, make_proposal
        from mempalace_tasks.restore import prepare_task_restore, read_task_logs
        import test_snapshot as fixtures

        fixture = fixtures.SnapshotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.send(fixtures.genesis())
        def send(number, action, payload, session=SESSION):
            return fixture.send({"operation": "native", "command_id": uid(number),
                                 "action": action, "session_id": session, "payload": payload})
        boot = send(2, "bootstrap", definition())["event"]["response"]
        goal, planner = boot["goal_id"], boot["planning_task_id"]
        scope = {"project": "demo", "goal_id": goal}
        send(3, "claim", {**scope, "task_id": planner, "expected_version": 1})
        task = fixture.log.state.tasks[planner]
        tokens = {**scope, "task_id": planner, "expected_version": task["version"],
                  "attempt_id": task["attempt"]["id"], "claim_generation": task["claim_generation"],
                  "native_agent_id": "old-agent"}
        send(4, "start", tokens)
        send(5, "resume", {
            **scope, "expected_version": fixture.log.state.tasks[goal]["version"],
            "expected_session_id": SESSION, "reason": "Transferred before snapshot",
            "observations": ["Old parent interrupted"],
        }, session=OTHER)
        task = fixture.log.state.tasks[planner]
        stale_release = make_proposal(fixture.log, {
            "operation": "native", "command_id": uid(6), "action": "release", "session_id": OTHER,
            "payload": {**tokens, "expected_version": task["version"], "reason": "Inspect inherited work",
                        "observations": ["Original association remains uncertain"]},
        }, fixtures.NOW)
        before = state_to_dict(fixture.log.state)
        def append(data, payload):
            self.assertEqual(data, fixture.data)
            number = fixture.next_row
            fixture.append(payload)
            return fixtures.raw_record(payload, number)
        prepared = prepare_task_restore(fixture.data, private_stage=True, append=append, now=fixtures.NOW)
        self.assertTrue(prepared["publishable"])
        log = read_task_logs(fixture.data)[AUTHORITY]
        self.assertEqual(state_to_dict(log.state), before)
        self.assertEqual(log.state.tasks[goal]["native"]["session_id"], OTHER)
        self.assertEqual(log.state.tasks[goal]["native"]["session_generation"], 2)
        self.assertEqual(log.state.tasks[planner]["attempt"]["session_generation"], 1)
        state_from_dict(state_to_dict(log.state))
        fold_record(log, fixtures.raw_record(stale_release, 1000))
        self.assertEqual(log.history[-1]["disposition"], "stale")
        self.assertEqual(state_to_dict(log.state), before)

    def test_private_restore_retains_native_session_plan_and_fences_old_packets(self):
        from mempalace_tasks.protocol import fold_record, make_proposal
        from mempalace_tasks.restore import prepare_task_restore, read_task_logs
        import test_snapshot as fixtures

        fixture = fixtures.SnapshotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.send(fixtures.genesis())
        boot = fixture.send({"operation": "native", "command_id": uid(2), "action": "bootstrap",
                             "session_id": SESSION, "payload": definition()})
        response = boot["event"]["response"]
        goal, planner = response["goal_id"], response["planning_task_id"]
        claim = {"operation": "native", "command_id": uid(3), "action": "claim",
                 "session_id": SESSION, "payload": {
                     "project": "demo", "goal_id": goal, "task_id": planner, "expected_version": 1}}
        old_packet = make_proposal(fixture.log, claim, fixtures.NOW)
        before = state_to_dict(fixture.log.state)

        def append(data, payload):
            self.assertEqual(data, fixture.data)
            number = fixture.next_row
            fixture.append(payload)
            return fixtures.raw_record(payload, number)

        restored = prepare_task_restore(fixture.data, private_stage=True, append=append, now=fixtures.NOW)
        self.assertTrue(restored["publishable"])
        log = read_task_logs(fixture.data)[AUTHORITY]
        self.assertEqual(state_to_dict(log.state), before)
        self.assertEqual(log.state.tasks[goal]["native"]["plan_references"],
                         ["artifact:plan/exact-version-1"])
        fold_record(log, fixtures.raw_record(old_packet, 1000))
        self.assertEqual(log.history[-1]["disposition"], "stale")
        self.assertEqual(state_to_dict(log.state), before)
