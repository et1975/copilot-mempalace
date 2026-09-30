"""Characterize paginated native recovery against the real offline authority."""

from pathlib import Path
import unittest

from authority_fixture import AUTHORITY, Clock, LogClient, genesis, state_directory, uid
from mempalace_tasks.authority import AuthorityError, TaskAuthority


ORIGINAL_PARENT = uid(810)
CURRENT_PARENT = uid(811)


class NativeRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.log = LogClient()
        self.authority = TaskAuthority(
            AUTHORITY, self.log, Path(self.directory.name) / "runtime",
            clock=Clock(), initialize=True, backoff=lambda _: None,
        ).start()
        self.addCleanup(self.authority.close)
        self.authority.execute_current(genesis())
        self.serial = 1000
        self.session = ORIGINAL_PARENT
        boot = self.call("bootstrap", {
            "project": "demo", "title": "Recovery workflow", "description": "",
            "acceptance": "Explicit recovery preserves task-local readiness",
            "planning_task": {"title": "Import", "description": "", "acceptance": ""},
            "goal_policy": {"scope": "Offline recovery fixtures", "max_tasks": 120, "max_batch": 50},
        })
        self.goal, planner = boot["goal_id"], boot["planning_task_id"]
        self.claim(planner, agent=self.session)
        tokens, epoch = self.bound(planner)
        self.call("complete", {
            **tokens, "summary": "Fixture import complete", "evidence": ["artifact:fixture-plan"],
            "parent_acceptance": "Fixture graph scope reviewed",
        }, epoch=epoch)

    def call(self, action, payload, *, session=None, epoch=None):
        self.serial += 1
        return self.authority.execute({
            "operation": "native", "action": action, "session_id": session or self.session,
            "command_id": uid(self.serial), "payload": payload,
        }, expected_epoch=epoch or self.authority.epoch_id)

    def current(self, task_id):
        root = self.authority.get(self.goal)
        observed = self.authority.get(task_id)
        self.assertTrue(root["fresh"])
        self.assertTrue(observed["fresh"])
        self.assertEqual(root["epoch_id"], observed["epoch_id"])
        self.assertEqual(root["task"]["native"]["session_id"], self.session)
        self.assertEqual(observed["authorization"]["session_id"], self.session)
        return observed

    def bound(self, task_id):
        observed = self.current(task_id)
        task = observed["task"]
        return {
            "project": task["project"], "goal_id": self.goal, "task_id": task_id,
            "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
            "claim_generation": task["claim_generation"],
            "native_agent_id": task["attempt"]["native_agent_id"],
        }, observed["epoch_id"]

    def claim(self, task_id, *, agent=None):
        observed = self.current(task_id)
        receipt = self.call("claim", {
            "project": observed["task"]["project"], "goal_id": self.goal, "task_id": task_id,
            "expected_version": observed["task"]["version"],
        }, epoch=observed["epoch_id"])
        if agent is not None:
            tokens, epoch = self.bound(task_id)
            receipt = self.call("start", {**tokens, "native_agent_id": agent}, epoch=epoch)
        return receipt

    def expand(self, tasks, *, edges=()):
        root = self.current(self.goal)
        receipt = self.call("expand", {
            "project": "demo", "goal_id": self.goal,
            "expected_graph_revision": root["task"]["graph_revision"],
            "source_disposition": "continue", "tasks": tasks, "edges": list(edges),
        }, epoch=root["epoch_id"])
        return {task["intent_key"]: task["id"] for task in receipt["tasks"]
                if task["id"] in receipt["admitted_task_ids"]}

    def resume(self):
        root = self.current(self.goal)
        receipt = self.call("resume", {
            "project": "demo", "goal_id": self.goal,
            "expected_version": root["task"]["version"], "expected_session_id": self.session,
            "reason": "Explicit fixture goal transfer",
            "observations": ["Previous parent's attempts have not been reconciled"],
        }, session=CURRENT_PARENT, epoch=root["epoch_id"])
        self.assertEqual([task["id"] for task in receipt["tasks"]], [self.goal])
        self.session = CURRENT_PARENT

    def recover(self, action, task_id, *, decision=None):
        tokens, epoch = self.bound(task_id)
        payload = {
            **tokens, "reason": "Inspect uncertain fixture work",
            "observations": ["No accepted result; external effects remain unknown"],
        }
        if decision is not None:
            payload["decision"] = decision
        return self.call(action, payload, epoch=epoch)

    def test_attention_cursor_exhausts_over_100_mixed_inherited_and_recovering_rows(self):
        task_ids = []
        for start in range(0, 103, 50):
            tasks = [
                {"intent_key": f"attention-{index}", "title": f"Attention {index}",
                 "description": "", "acceptance": ""}
                for index in range(start, min(start + 50, 103))
            ]
            admitted = self.expand(tasks)
            task_ids.extend(admitted[task["intent_key"]] for task in tasks)
        for task_id in task_ids:
            self.claim(task_id)
        tokens, epoch = self.bound(task_ids[0])
        self.call("start", {**tokens, "native_agent_id": "old-running-worker"}, epoch=epoch)
        for task_id in task_ids[-2:]:
            self.recover("release", task_id)
        self.recover("reconcile", task_ids[-1], decision="hold")
        self.resume()

        filters = {"goal_id": self.goal, "needs_attention": True}
        first = self.authority.snapshot(filters=filters)
        self.assertTrue(first["fresh"])
        self.assertEqual(first["summary"]["total"], 103)
        self.assertEqual(len(first["rows"]), 100)
        self.assertIsNotNone(first["next_cursor"])
        second = self.authority.snapshot(cursor=first["next_cursor"])
        self.assertEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["as_of"], first["as_of"])
        self.assertFalse(second["fresh"])
        self.assertEqual(second["reason"], "pinned_snapshot")
        self.assertEqual(len(second["rows"]), 3)
        self.assertIsNone(second["next_cursor"])
        rows = first["rows"] + second["rows"]
        self.assertEqual(len({row["id"] for row in rows}), 103)
        self.assertEqual({row["id"] for row in rows}, set(task_ids))
        inherited = {
            row["id"] for row in rows
            if row["status"] == "in_progress"
            and {"code": "native_reconciliation_required"} in row["reasons"]
        }
        recovering = {row["id"] for row in rows if row["status"] == "recovering"}
        self.assertEqual(inherited, set(task_ids[:101]))
        self.assertEqual(recovering, set(task_ids[-2:]))
        held = next(row for row in rows if row["id"] == task_ids[-1])
        self.assertEqual(held["native"]["reconciliation"]["decision"], "hold")
        self.assertIsNotNone(held["recovery"])

    def test_recovery_hold_allows_unrelated_claim_but_preserves_dependency_blocker(self):
        ids = self.expand([
            {"intent_key": name, "title": name, "description": "", "acceptance": ""}
            for name in ("uncertain", "dependent", "independent")
        ] + [{
            "intent_key": "open-held", "title": "Await scope review", "description": "",
            "acceptance": "", "hold_reason": "Scope not yet approved",
        }], edges=[{"source": "uncertain", "target": "dependent", "edge_type": "blocks"}])
        self.claim(ids["uncertain"], agent="old-uncertain-worker")
        self.resume()
        self.recover("release", ids["uncertain"])
        released = self.current(ids["uncertain"])
        self.assertEqual(released["task"]["status"], "recovering")
        self.recover("reconcile", ids["uncertain"], decision="hold")
        held = self.current(ids["uncertain"])
        self.assertEqual(held["task"]["status"], "recovering")
        self.assertIsNotNone(held["task"]["recovery"])
        self.assertFalse(held["authorization"]["authorized"])

        ready = self.authority.ready(filters={"goal_id": self.goal})
        self.assertEqual([task["id"] for task in ready["tasks"]], [ids["independent"]])
        self.claim(ids["independent"])
        claimed = self.current(ids["independent"])["task"]
        self.assertEqual(claimed["status"], "in_progress")
        self.assertEqual(claimed["attempt"]["owner"], CURRENT_PARENT)
        blocked = self.current(ids["dependent"])
        self.assertIn({"code": "blocked", "task_id": ids["uncertain"]},
                      blocked["eligibility"]["reasons"])
        with self.assertRaises(AuthorityError) as caught:
            self.claim(ids["dependent"])
        self.assertEqual(caught.exception.code, "not_ready")
        attention = self.authority.snapshot(filters={"goal_id": self.goal, "needs_attention": True})
        self.assertEqual([row["id"] for row in attention["rows"]], [ids["uncertain"]])
        open_held = self.current(ids["open-held"])
        self.assertIsNone(open_held["task"]["attempt"])
        self.assertIn({"code": "held", "reason": "Scope not yet approved"},
                      open_held["eligibility"]["reasons"])

    def test_stale_attention_page_is_refreshed_before_selecting_recovery_action(self):
        ids = self.expand([
            {"intent_key": name, "title": name, "description": "", "acceptance": "",
             "priority": priority}
            for priority, name in enumerate(("first", "changed"))
        ])
        self.claim(ids["first"])
        self.claim(ids["changed"], agent="old-changed-worker")
        self.resume()
        filters = {"goal_id": self.goal, "needs_attention": True}
        first = self.authority.snapshot(filters=filters, limit=1)
        self.assertEqual(first["rows"][0]["id"], ids["first"])
        self.recover("release", ids["changed"])
        self.recover("reconcile", ids["changed"], decision="hold")

        page = self.authority.snapshot(filters=filters, cursor=first["next_cursor"], limit=1)
        self.assertFalse(page["fresh"])
        self.assertEqual(page["reason"], "pinned_snapshot")
        self.assertIsNone(page["next_cursor"])
        row = page["rows"][0]
        self.assertEqual(row["id"], ids["changed"])
        self.assertEqual(row["status"], "in_progress")
        before = len(self.log.events)
        with self.assertRaises(AuthorityError) as caught:
            self.call("release", {
                "project": row["project"], "goal_id": self.goal, "task_id": row["id"],
                "expected_version": row["version"], "attempt_id": row["attempt_id"],
                "claim_generation": row["claim_generation"], "native_agent_id": row["native_agent_id"],
                "reason": "Stale page must not authorize release",
                "observations": ["Pinned page still describes inherited active work"],
            }, epoch=page["epoch_id"])
        self.assertEqual(caught.exception.code, "version_conflict")
        self.assertEqual(len(self.log.events), before)

        current = self.current(row["id"])
        self.assertEqual(current["task"]["status"], "recovering")
        self.assertGreater(current["task"]["version"], row["version"])
        self.recover("reconcile", row["id"], decision="hold")
        refreshed = self.current(row["id"])
        self.assertEqual(refreshed["task"]["status"], "recovering")
        self.assertEqual(refreshed["task"]["claim_generation"], row["claim_generation"])
        self.assertEqual(refreshed["task"]["native"]["reconciliation"]["decision"], "hold")
