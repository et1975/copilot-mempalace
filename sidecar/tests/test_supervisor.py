import copy
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mempalace_tasks.execution import LocalProfile
from mempalace_tasks.journal import JournalError, JsonStore
from mempalace_tasks.leases import EffectiveClock
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.supervisor import HostSupervisor
from runtime_support import DomainPort, ManualClock, PortError, RealAuthorityFixture, temporary_directory
from test_execution import WORKER, profile
import sys


class RemoteStillRunning:
    def reconcile(self, task, workspace, *, process_stopped, recovering, required_fences):
        return {"references": ["remote:job-still-running"], "process_stopped": process_stopped,
                "effects_reconciled": False}


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = temporary_directory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.clock = ManualClock()
        self.port = DomainPort(self.clock)
        self.maintenance = LeaseMaintenance(self.port, self.clock, system_actor="sys",
                                           recovery_actor="recovery")

    def host(self, mode="sleep", *, pool_size=1, profiles=None, **options):
        host = HostSupervisor(self.port, self.clock, self.maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=profiles or [
                                  profile(self.root / ("work-" + mode), mode)],
                              pool_size=pool_size, **options)
        self.addCleanup(host.close)
        return host

    def await_exit(self, process):
        deadline = time.monotonic() + 3
        while process.observe() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(process.observe(), "Owned fixture failed to exit in bounded time")

    def drive(self, host, *, steps=20):
        for _ in range(steps):
            host.step()
            if not host.workers:
                return
            time.sleep(0.01)

    def test_bounded_pool_requeries_ready_work_and_completes_explicit_results(self):
        one, two = self.port.create(priority=0), self.port.create(priority=1)
        host = self.host("success")
        first = host.step()
        self.assertEqual(first["started"], 1)
        self.assertEqual(len(host.workers), 1)
        self.assertEqual(self.port.state.tasks[two]["status"], "open")
        self.drive(host, steps=30)
        self.assertEqual(self.port.state.tasks[one]["status"], "closed")
        self.assertEqual(self.port.state.tasks[two]["status"], "closed")
        self.assertEqual(host.health()["peak_workers"], 1)

    def test_no_success_from_exit_zero_without_result_or_with_old_generation(self):
        for mode in ("empty", "stale", "crash"):
            with self.subTest(mode=mode):
                tid = self.port.create()
                host = self.host(mode)
                host.step()
                self.await_exit(host.workers[tid].process)
                self.drive(host)
                task = self.port.state.tasks[tid]
                self.assertNotEqual(task["status"], "closed")
                self.assertEqual(task["automatic_retries_used"], 1)
                host.close()

    def test_isolated_failure_recovery_uses_configured_operator_service_identity(self):
        tid = self.port.create()
        host = self.host("crash")
        host.step()
        self.await_exit(host.workers[tid].process)
        self.drive(host)
        recoveries = [command for command in self.port.commands
                      if command["operation"] == "recover"]
        self.assertEqual([command["actor"] for command in recoveries], ["recovery"])

    def test_renewal_cadence_uses_confirmed_lease_and_is_not_output_progress(self):
        tid = self.port.create()
        host = self.host("spam")
        host.step()
        before = copy.deepcopy(self.port.state.tasks[tid]["attempt"])
        self.clock.seconds = 99
        host.step()
        self.assertEqual(self.port.state.tasks[tid]["lease_revision"], 1)
        self.clock.seconds = 100
        result = host.step()
        self.assertEqual(result["renewed"], 1)
        task = self.port.state.tasks[tid]
        self.assertEqual(task["lease_revision"], 2)
        self.assertEqual(task["lease_expires_at"], "2026-09-23T12:06:40Z")
        self.assertEqual(task["attempt"]["progress_deadline"], before["progress_deadline"])

    def test_live_hung_worker_hits_progress_cap_despite_successful_renewals(self):
        tid = self.port.create(policy={"progress_timeout_seconds": 220,
                                      "hard_timeout_seconds": 500})
        host = self.host("spam")
        host.step()
        process = host.workers[tid].process
        for value in (100, 200, 220):
            self.clock.seconds = value
            host.step()
        self.assertTrue(process.stopped)
        self.assertEqual(self.port.state.tasks[tid]["status"], "open")
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 1)

    def test_checkpoint_renews_progress_only_once_and_continues_after_new_generation(self):
        tid = self.port.create()
        host = self.host("checkpoint")
        host.step()
        old = host.workers[tid]
        deadline = time.monotonic() + 3
        while not old.workspace.checkpoint_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.clock.seconds = 50
        self.assertEqual(host.step()["checkpoints"], 1)
        self.clock.seconds = 60
        self.assertEqual(host.step()["checkpoints"], 0)
        original = self.port.state.tasks[tid]["attempt"]["checkpoint"]
        self.assertEqual(original["sequence"], 1)
        self.port.send("release", actor="sup", **self.port.tokens(tid), reason="checkpoint yield")
        self.maintenance.tick()
        host.step()
        replacement = host.workers[tid]
        self.assertNotEqual(replacement.attempt_id, old.attempt_id)
        self.assertTrue(old.process.stopped)
        saved = JsonStore(replacement.workspace.input_path).read()
        self.assertEqual(saved["attempt"]["checkpoint"], original)
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 0)

    def _replacement_with_pending_recovery(self):
        class Reconciler:
            ready = False
            def reconcile(self, task, workspace, *, process_stopped, recovering, required_fences):
                return {"references": ["local:reconciled"], "process_stopped": process_stopped,
                        "effects_reconciled": self.ready}
        reconciler = Reconciler()
        shared = LocalProfile("shared", (sys.executable, WORKER, "sleep"), self.root / "shared",
                              execution_class="shared_unfenced", reconciler=reconciler)
        tid = self.port.create("shared", resource_keys=["local-resource"], priority=0)
        host = self.host(profiles=[shared, profile(self.root / "local")], pool_size=1)
        host.step()
        old = host.workers[tid]
        self.addCleanup(old.process.stop)
        self.port.send("release", actor="sup", **self.port.tokens(tid), reason="replace")
        host.step()
        self.assertTrue(any(worker is old for worker in host._recoveries.values()))
        self.assertTrue(old.process.stopped)
        reconciler.ready = True
        def lost(command):
            if command["operation"] == "recover":
                self.port.before_execute = None
                self.port.execute(command)
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        host.step()
        replacement = host.workers[tid]
        self.addCleanup(replacement.process.stop)
        self.assertEqual(replacement.generation, 2)
        self.assertIsNotNone(old.pending)
        return host, tid, old, replacement, reconciler

    def test_delayed_old_recovery_receipt_cannot_untrack_running_replacement(self):
        host, tid, old, replacement, _ = self._replacement_with_pending_recovery()
        other = self.port.create(priority=1)
        result = host.step()
        for worker in host.workers.values():
            if worker.process:
                self.addCleanup(worker.process.stop)
        self.assertEqual(result["errors"], [])
        self.assertIs(host.workers.get(tid), replacement)
        self.assertFalse(replacement.process.stopped)
        self.assertEqual(self.port.state.tasks[other]["status"], "open")
        self.assertEqual(len(host.workers), 1)
        self.assertEqual(len(host._recoveries), 0)
        self.assertEqual(len(host.requests.pending), 0)
        host.close()
        self.assertTrue(replacement.process.stopped)

    def test_recoveries_for_two_generations_retain_independent_pending_identity(self):
        host, tid, old, replacement, reconciler = self._replacement_with_pending_recovery()
        reconciler.ready = False
        self.port.send("release", actor="sup", **self.port.tokens(tid), reason="new recovery")
        def unresolved_old_receipt(command):
            if (command["operation"] == "recover"
                    and command["expected_version"] == old.pending["expected_version"]):
                raise PortError("outcome_unknown")
        self.port.before_execute = unresolved_old_receipt
        host.step()
        self.assertEqual(len(host._recoveries), 2)
        self.assertTrue(any(worker is old for worker in host._recoveries.values()))
        self.assertTrue(any(worker is replacement for worker in host._recoveries.values()))
        self.assertTrue(old.process.stopped)
        self.assertTrue(replacement.process.stopped)
        self.port.before_execute = None
        host.step()
        self.assertEqual(list(host._recoveries.values()), [replacement])
        self.assertEqual(len(host.requests.pending), 0)
        self.assertIsNone(old.pending)
        self.assertEqual(self.port.state.tasks[tid]["status"], "recovering")
        self.assertIsNotNone(self.port.state.resources["local-resource"]["reservation"])

    def _persist_delayed_authorization(self, operation):
        elapsed = [0]
        monotonic_base = time.monotonic()
        epoch_ns = 1790164800000000000
        store = JsonStore(self.root / "clock.json")
        clock = EffectiveClock(store, wall_time_ns=lambda: epoch_ns + elapsed[0] * 1000000000,
                               monotonic_ns=lambda: elapsed[0] * 1000000000,
                               boot_id="runtime-latency-test", initialize=True)
        port = DomainPort(clock)
        tid = port.create()
        maintenance = LeaseMaintenance(port, clock, system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(port, clock, maintenance, supervisor_id="sup", worker_id="worker",
                              profiles=[profile(self.root / "latency")],
                              elapsed_time=lambda: monotonic_base + elapsed[0])
        self.addCleanup(host.close)
        if operation == "renew":
            host.step()
            elapsed[0] = 100
        armed = False
        real_execute, real_write = port.execute, store.write
        def execute_then_delay_snapshot(command):
            nonlocal armed
            response = real_execute(command)
            if command.get("report_kind", command["operation"]) == operation:
                armed = True
            return response
        def persist_with_elapsed_time(value):
            nonlocal armed
            real_write(value)
            if armed:
                armed = False
                elapsed[0] += 20
        with patch.object(port, "execute", side_effect=execute_then_delay_snapshot):
            with patch.object(store, "write", side_effect=persist_with_elapsed_time):
                host.step()
        worker = host.workers[tid]
        self.assertIsNotNone(worker.process)
        self.assertEqual(elapsed[0], 120 if operation == "renew" else 20)
        return worker, monotonic_base

    def test_spawn_cutoff_excludes_effective_clock_persistence_latency(self):
        worker, anchor = self._persist_delayed_authorization("started")
        self.assertAlmostEqual(worker.process._deadline, anchor + 295, places=6)

    def test_renewal_cutoff_excludes_effective_clock_persistence_latency(self):
        worker, anchor = self._persist_delayed_authorization("renew")
        self.assertAlmostEqual(worker.process._deadline, anchor + 395, places=6)

    def test_fresh_checkpoint_each_step_cannot_starve_due_renewal(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        renewals = []
        for sequence, at in enumerate((100, 200, 300, 400), 1):
            self.clock.seconds = at
            JsonStore(worker.workspace.checkpoint_path).write({
                "schema_version": 1, "task_id": tid, "attempt_id": worker.attempt_id,
                "claim_generation": worker.generation, "sequence": sequence,
                "reference": f"artifact:progress-{sequence}"})
            renewals.append(host.step()["renewed"])
        self.assertEqual(renewals, [1, 1, 1, 1])
        self.assertIs(host.workers[tid], worker)
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 0)
        self.assertEqual(self.port.state.tasks[tid]["attempt"]["checkpoint"]["sequence"], 4)
        self.assertEqual(self.port.state.tasks[tid]["lease_expires_at"], "2026-09-23T12:11:40Z")
        self.assertFalse(worker.process.stopped)

    def test_unknown_checkpoint_stays_pending_before_due_renewal(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        JsonStore(worker.workspace.checkpoint_path).write({
            "schema_version": 1, "task_id": tid, "attempt_id": worker.attempt_id,
            "claim_generation": worker.generation, "sequence": 1,
            "reference": "artifact:progress-1"})
        def lost(command):
            if command["operation"] == "checkpoint":
                self.port.before_execute = None
                self.port.execute(command)
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        self.clock.seconds = 100
        self.assertEqual(host.step()["renewed"], 0)
        self.assertEqual(worker.pending["operation"], "checkpoint")
        self.assertEqual(self.port.state.tasks[tid]["lease_revision"], 1)
        self.assertEqual(host.step()["renewed"], 1)
        checkpoints = [c for c in self.port.commands if c["operation"] == "checkpoint"]
        self.assertEqual(len({c["command_id"] for c in checkpoints}), 1)
        self.assertIsNone(worker.pending)

    def test_lost_renew_response_never_extends_local_authorization_until_confirmed(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        requests = []
        def lost(command):
            if command["operation"] == "renew":
                requests.append(command["command_id"])
                self.port.before_execute = None
                self.port.execute(command)
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        self.clock.seconds = 100
        host.step()
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:05:00+00:00")
        self.assertIsNotNone(worker.pending)
        host.step()
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:06:40+00:00")
        renews = [c for c in self.port.commands if c["operation"] == "renew"]
        self.assertEqual(len({c["command_id"] for c in renews}), 1)
        self.assertIsNone(worker.pending)

    def test_explicit_post_commit_ambiguity_retains_renewal_without_false_confirmation(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        def lost(command):
            if command["operation"] == "renew":
                self.port.before_execute = None
                self.port.execute(command)
                error = PortError("clock_sample_failed")
                error.ambiguous = True
                raise error
        self.port.before_execute = lost
        self.clock.seconds = 100
        host.step()
        self.assertIsNotNone(worker.pending)
        self.assertFalse(worker.process.stopped)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:05:00+00:00")
        host.step()
        renewals = [c for c in self.port.commands if c["operation"] == "renew"]
        self.assertEqual(len({c["command_id"] for c in renewals}), 1)
        self.assertIsNone(worker.pending)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:06:40+00:00")

    def test_lost_settlement_response_does_not_turn_confirmed_completion_into_failure(self):
        tid = self.port.create()
        host = self.host("success")
        host.step()
        worker = host.workers[tid]
        self.await_exit(worker.process)
        def lost(command):
            if command.get("report_kind") == "settled":
                self.port.before_execute = None
                self.port.execute(command)
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        host.step()
        self.assertIsNotNone(worker.pending)
        self.drive(host)
        self.assertEqual(self.port.state.tasks[tid]["status"], "closed")
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 0)

    def test_abandoned_start_uses_new_request_without_reusing_workspace_or_claim(self):
        tid = self.port.create()
        host = self.host()
        requests = []
        def abandon(command):
            if command.get("report_kind") == "started":
                requests.append(command["command_id"])
                if len(requests) == 1:
                    self.port.outcomes[command["command_id"]] = {"ok": False, "outcome": "abandoned"}
        self.port.before_execute = abandon
        host.step()
        self.assertIsNone(host.workers[tid].process)
        first_path = host.workers[tid].workspace.root
        host.step()
        self.assertIsNotNone(host.workers[tid].process)
        self.assertEqual(host.workers[tid].workspace.root, first_path)
        self.assertEqual(self.port.state.tasks[tid]["claim_generation"], 1)
        self.assertNotEqual(requests[0], requests[1])

    def test_hard_cap_holds_even_with_new_durable_checkpoints(self):
        tid = self.port.create(policy={"progress_timeout_seconds": 180,
                                      "hard_timeout_seconds": 320})
        host = self.host()
        host.step()
        worker = host.workers[tid]
        for sequence, at in enumerate((100, 200, 300), 1):
            self.clock.seconds = at
            JsonStore(worker.workspace.checkpoint_path).write({
                "schema_version": 1, "task_id": tid, "attempt_id": worker.attempt_id,
                "claim_generation": worker.generation, "sequence": sequence,
                "reference": f"artifact:checkpoint-{sequence}"})
            host.step()
            host.step()
        self.clock.seconds = 320
        host.step()
        self.assertTrue(worker.process.stopped)
        self.assertEqual(self.port.state.tasks[tid]["status"], "open")
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 1)

    def test_old_generation_result_cannot_complete_replacement(self):
        tid = self.port.create()
        host = self.host("success")
        host.step()
        old = host.workers[tid]
        self.await_exit(old.process)
        old_result = JsonStore(old.workspace.result_path).read()
        self.port.send("release", actor="sup", **self.port.tokens(tid), reason="replace")
        self.maintenance.tick()
        host.step()
        new = host.workers[tid]
        self.await_exit(new.process)
        JsonStore(new.workspace.result_path).write(old_result)
        self.drive(host)
        self.assertNotEqual(self.port.state.tasks[tid]["status"], "closed")
        self.assertTrue(old.process.stopped)

    def test_unresolved_renewal_stops_owned_process_before_last_confirmed_lease(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        def lost(command):
            if command["operation"] == "renew":
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        self.clock.seconds = 100
        host.step()
        self.port.reconcile_error = PortError("upstream_unavailable")
        self.clock.seconds = 295
        host.step()
        self.assertTrue(worker.process.stopped)
        self.assertNotEqual(self.port.state.tasks[tid]["status"], "closed")
        self.assertFalse(host.health()["ok"])
        self.port.reconcile_error = None
        self.port.before_execute = None

    def test_definitive_renewal_failure_stops_immediately_not_on_next_turn(self):
        tid = self.port.create()
        host = self.host()
        host.step()
        worker = host.workers[tid]
        def failure(command):
            if command["operation"] == "renew":
                raise PortError("permission_denied")
        self.port.before_execute = failure
        self.clock.seconds = 100
        result = host.step()
        self.assertTrue(result["errors"])
        self.assertTrue(worker.process.stopped)
        self.port.before_execute = None

    def test_remote_effect_remains_after_exit_and_does_not_block_unrelated_work(self):
        shared = self.port.create("shared", resource_keys=["remote-db"], priority=0)
        good = self.port.create(priority=1)
        profiles = [LocalProfile("shared", (sys.executable, WORKER, "remote"),
                                 self.root / "remote", execution_class="shared_unfenced",
                                 reconciler=RemoteStillRunning()),
                    profile(self.root / "local", "success")]
        host = self.host(profiles=profiles)
        host.step()
        self.await_exit(host.workers[shared].process)
        for _ in range(25):
            host.step()
            time.sleep(0.01)
        self.assertIn(self.port.state.tasks[shared]["status"], {"recovering", "quarantined"})
        self.assertIsNotNone(self.port.state.resources["remote-db"]["reservation"])
        self.assertEqual(self.port.state.tasks[good]["status"], "closed")

    def test_shared_local_completion_requires_settlement_before_close(self):
        tid = self.port.create("shared", resource_keys=["local-db"])
        local = LocalProfile("shared", (sys.executable, WORKER, "success"),
                             self.root / "local", execution_class="shared_unfenced",
                             local_only=True)
        host = self.host(profiles=[local])
        host.step()
        self.drive(host)
        task = self.port.state.tasks[tid]
        self.assertEqual(task["status"], "closed")
        self.assertTrue(task["attempt"]["settlement"]["process_stopped"])
        self.assertIsNone(self.port.state.resources["local-db"]["reservation"])

    def test_filters_and_reboot_gate_prevent_unauthorized_launch(self):
        chosen, ignored = self.port.create(), self.port.create()
        host = self.host(filters={"task_ids": [chosen], "project": "project"})
        self.clock.pending_reboot = True
        self.port.reconcile_error = PortError("upstream_unavailable")
        self.assertEqual(host.step()["started"], 0)
        self.port.reconcile_error = None
        host.step()
        self.assertEqual(set(host.workers), {chosen})
        self.assertEqual(self.port.state.tasks[ignored]["status"], "open")

    def test_task_filter_is_not_hidden_behind_an_unrelated_ready_page(self):
        from mempalace_tasks.domain import ready_tasks as domain_ready
        self.port.create(priority=0)
        chosen = self.port.create(priority=1)
        host = self.host(filters={"task_ids": [chosen]})
        def capped(state, filters, now):
            return domain_ready(state, {**filters, "limit": 1}, now)
        # Shrink the real domain's bounded ready page without faking transitions.
        with patch("mempalace_tasks.supervisor.ready_tasks", side_effect=capped):
            host.step()
        self.assertEqual(set(host.workers), {chosen})

    def test_registered_profile_and_supervisor_bindings_are_enforced(self):
        with self.assertRaises(ValueError):
            self.host(profiles=[LocalProfile("unregistered", (sys.executable, WORKER, "sleep"),
                                            self.root / "bad")])
        with self.assertRaises(ValueError):
            self.host(pool_size=0)
        with self.assertRaises(ValueError):
            self.host(filters={"ignored_filter": True})

    def test_bounded_runner_stops_and_intentionally_releases_owned_work(self):
        tid = self.port.create()
        host = self.host()
        result = host.run(max_steps=2, interval_seconds=0)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(self.port.state.tasks[tid]["status"], "open")
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 0)
        self.assertEqual(len(host.workers), 0)

    def test_shutdown_resolves_unknown_claim_without_launch_or_failure_retry(self):
        tid = self.port.create()
        host = self.host()
        def lost(command):
            if command["operation"] == "claim":
                self.port.before_execute = None
                self.port.execute(command)
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        host.step()
        self.assertEqual(len(host.workers), 0)
        self.assertEqual(host.health()["pending_claims"], 1)
        self.assertFalse(host.health()["ok"])
        host.close()
        self.assertEqual(self.port.state.tasks[tid]["status"], "open")
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 0)
        self.assertEqual(host.health()["pending_claims"], 0)
        self.assertFalse(any(c.get("report_kind") == "started" for c in self.port.commands))

    def test_shutdown_unknown_claim_is_not_reported_as_healthy(self):
        self.port.create()
        host = self.host()
        def lost(command):
            if command["operation"] == "claim":
                raise PortError("outcome_unknown")
        self.port.before_execute = lost
        host.step()
        self.port.reconcile_error = PortError("upstream_unavailable")
        result = host.close()
        self.assertTrue(result["errors"])
        self.assertFalse(host.health()["ok"])
        self.port.reconcile_error = None
        self.port.before_execute = None

    def test_two_worker_limit_includes_newly_queued_tasks(self):
        ids = [self.port.create(), self.port.create(), self.port.create()]
        host = self.host(pool_size=2)
        host.step()
        self.assertEqual(len(host.workers), 2)
        fourth = self.port.create()
        host.step()
        self.assertEqual(len(host.workers), 2)
        self.assertEqual(self.port.state.tasks[fourth]["status"], "open")
        self.assertEqual(sum(self.port.state.tasks[tid]["status"] == "open" for tid in ids), 1)
        self.assertEqual(host.health()["peak_workers"], 2)

    def test_shared_quarantine_retains_resource_but_frees_pool_for_unrelated_work(self):
        shared = self.port.create("shared", priority=0, resource_keys=["resource"])
        other = self.port.create(priority=1)
        host = self.host(profiles=[
            LocalProfile("shared", (sys.executable, WORKER, "sleep"), self.root / "remote",
                         execution_class="shared_unfenced", reconciler=RemoteStillRunning()),
            profile(self.root / "local", "success")])
        host.step()
        process = host.workers[shared].process
        self.port.send("attempt_report", actor="sup", **self.port.tokens(shared),
                       report_kind="failed", reason="permanent remote failure", permanent=True,
                       evidence={"references": ["remote:failure"]})
        for _ in range(20):
            host.step()
            time.sleep(0.01)
        self.assertTrue(process.stopped)
        self.assertEqual(self.port.state.tasks[shared]["status"], "quarantined")
        self.assertIsNotNone(self.port.state.resources["resource"]["reservation"])
        self.assertEqual(self.port.state.tasks[other]["status"], "closed")

    def test_actual_authority_process_completion_replays_as_closed(self):
        fixture = RealAuthorityFixture(self.root / "authority", self.clock)
        self.addCleanup(fixture.authority.close)
        tid = fixture.create()
        maintenance = LeaseMaintenance(fixture.authority, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(fixture.authority, self.clock, maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=[profile(self.root / "real", "success")])
        self.addCleanup(host.close)
        host.step()
        self.drive(host)
        self.assertEqual(fixture.authority.get(tid)["task"]["status"], "closed")
        self.assertTrue(fixture.authority.health()["fresh"])
        fixture.authority.refresh()
        self.assertEqual(fixture.authority.state.tasks[tid]["status"], "closed")

    def test_actual_authority_unknown_renewal_stops_then_recovers_owned_process(self):
        fixture = RealAuthorityFixture(self.root / "authority", self.clock)
        self.addCleanup(fixture.authority.close)
        tid = fixture.create()
        maintenance = LeaseMaintenance(fixture.authority, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(fixture.authority, self.clock, maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=[profile(self.root / "real")])
        self.addCleanup(host.close)
        host.step()
        worker = host.workers[tid]
        def lost(payload):
            if (payload["record_type"] == "mptask.command"
                    and payload["event"]["command"]["operation"] == "renew"):
                fixture.client.read_error = True
                fixture.client.behaviors = ["lost", "lost", "lost"]
        fixture.client.on_append = lost
        self.clock.seconds = 100
        host.step()
        self.assertIsNotNone(worker.pending)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:05:00+00:00")
        self.clock.seconds = 295
        host.step()
        self.assertTrue(worker.process.stopped)
        fixture.client.on_append = None
        fixture.client.read_error = False
        self.drive(host)
        self.assertEqual(fixture.state.tasks[tid]["status"], "open")
        self.assertEqual(fixture.state.tasks[tid]["automatic_retries_used"], 1)

    def test_actual_authority_post_clear_clock_error_keeps_original_renewal_request(self):
        from mempalace_tasks.leases import ClockError
        fixture = RealAuthorityFixture(self.root / "authority", self.clock)
        self.addCleanup(fixture.authority.close)
        tid = fixture.create()
        maintenance = LeaseMaintenance(fixture.authority, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(fixture.authority, self.clock, maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=[profile(self.root / "real")])
        self.addCleanup(host.close)
        host.step()
        worker = host.workers[tid]
        original_clear = fixture.authority._pending_store.clear
        original_clock = fixture.authority._clock
        armed = False
        def clear_then_fail_receipt():
            nonlocal armed
            original_clear()
            armed = True
        def receipt_clock():
            nonlocal armed
            if armed:
                armed = False
                raise ClockError("clock_sample_failed", "Receipt clock unavailable after pending clear")
            return original_clock()
        self.clock.seconds = 100
        with patch.object(fixture.authority._pending_store, "clear", side_effect=clear_then_fail_receipt):
            with patch.object(fixture.authority, "_clock", side_effect=receipt_clock):
                host.step()
        self.assertIsNotNone(worker.pending)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:05:00+00:00")
        self.assertFalse(worker.process.stopped)
        host.step()
        self.assertIsNone(worker.pending)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:06:40+00:00")
        originals = [p for p in fixture.client.sent if p["record_type"] == "mptask.command"
                     and p["event"]["command"]["operation"] == "renew"]
        self.assertEqual(len(originals), 1)

    def test_confirmed_renewal_cannot_adopt_other_threads_unpersisted_extension(self):
        from mempalace_tasks.authority import AuthorityError
        fixture = RealAuthorityFixture(self.root / "authority", self.clock)
        self.addCleanup(fixture.authority.close)
        tid = fixture.create()
        maintenance = LeaseMaintenance(fixture.authority, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(fixture.authority, self.clock, maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=[profile(self.root / "real")])
        self.addCleanup(host.close)
        host.step()
        worker = host.workers[tid]
        original_cutoff = worker.process._deadline
        a_succeeded, b_finished = threading.Event(), threading.Event()
        receipts, failures = [], []
        real_execute = fixture.authority.execute
        real_head_write = fixture.authority._head_store.write

        def renew_b():
            try:
                if not a_succeeded.wait(timeout=5):
                    raise AssertionError("Renewal A did not reach its successful receipt barrier")
                with fixture.authority.serialized():
                    self.clock.seconds = 150
                    task = fixture.authority.state.tasks[tid]
                fixture.send("renew", actor="sup", task_id=tid,
                             attempt_id=task["attempt"]["id"], claim_generation=1,
                             expected_lease_revision=task["lease_revision"])
            except Exception as error:
                failures.append(error)
            finally:
                b_finished.set()

        competing = threading.Thread(target=renew_b, name="runtime-unconfirmed-renewal")

        def receipt_barrier(command):
            receipt = real_execute(command)
            if command["operation"] == "renew" and threading.current_thread() is not competing:
                receipts.append(receipt)
                a_succeeded.set()
                if not b_finished.wait(timeout=5):
                    raise AssertionError("Renewal B did not reach its persistence failure")
            return receipt

        def fail_b_head(value):
            if threading.current_thread() is competing:
                raise JournalError("io_error", "Unconfirmed B head persistence", ambiguous=True)
            return real_head_write(value)

        self.clock.seconds = 100
        with patch.object(fixture.authority, "execute", side_effect=receipt_barrier):
            with patch.object(fixture.authority._head_store, "write", side_effect=fail_b_head):
                competing.start()
                try:
                    result = host.step()
                finally:
                    competing.join(timeout=6)
        self.assertFalse(competing.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AuthorityError)
        self.assertTrue(failures[0].ambiguous)
        self.assertEqual(receipts[0]["tasks"][0]["lease_expires_at"], "2026-09-23T12:06:40Z")
        self.assertEqual(fixture.authority.state.tasks[tid]["lease_expires_at"],
                         "2026-09-23T12:07:30Z")
        self.assertFalse(fixture.authority.health()["fresh"])
        self.assertEqual(result["renewed"], 0)
        self.assertTrue(result["errors"])
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:05:00+00:00")
        self.assertEqual(worker.process._deadline, original_cutoff)
        self.assertIsNotNone(worker.pending)
        self.assertFalse(worker.process.stopped)

        result = host.step()
        self.assertTrue(fixture.authority.health()["fresh"])
        self.assertEqual(result["renewed"], 1)
        self.assertIsNone(worker.pending)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:06:40+00:00")
        renewals = [p for p in fixture.client.sent if p["record_type"] == "mptask.command"
                    and p["event"]["command"]["operation"] == "renew"]
        self.assertEqual(len(renewals), 2, "Confirming A must not create another mutation")
        self.clock.seconds = 250
        self.assertEqual(host.step()["renewed"], 1)
        self.assertEqual(worker.confirmed_lease.isoformat(), "2026-09-23T12:09:10+00:00")

    def test_watchdog_confirmation_is_serialized_with_authority_freshness(self):
        fixture = RealAuthorityFixture(self.root / "authority", self.clock)
        self.addCleanup(fixture.authority.close)
        tid = fixture.create()
        maintenance = LeaseMaintenance(fixture.authority, self.clock,
                                       system_actor="sys", recovery_actor="recovery")
        host = HostSupervisor(fixture.authority, self.clock, maintenance, supervisor_id="sup",
                              worker_id="worker", profiles=[profile(self.root / "real")])
        self.addCleanup(host.close)
        host.step()
        worker = host.workers[tid]
        ownership = []
        confirm = worker.process.confirm_deadline
        def confirm_under_owner_lock(deadline):
            ownership.append(fixture.authority._mutex._is_owned())
            confirm(deadline)
        self.clock.seconds = 100
        with patch.object(worker.process, "confirm_deadline", side_effect=confirm_under_owner_lock):
            self.assertEqual(host.step()["renewed"], 1)
        self.assertEqual(ownership, [True])

    def test_claim_and_start_receipts_wait_for_current_authority_before_launch(self):
        from mempalace_tasks.authority import AuthorityError
        for phase in ("claim", "started"):
            with self.subTest(phase=phase):
                clock = ManualClock()
                fixture = RealAuthorityFixture(self.root / phase / "authority", clock)
                self.addCleanup(fixture.authority.close)
                tid = fixture.create()
                maintenance = LeaseMaintenance(fixture.authority, clock,
                                               system_actor="sys", recovery_actor="recovery")
                host = HostSupervisor(fixture.authority, clock, maintenance, supervisor_id="sup",
                                      worker_id="worker", profiles=[profile(self.root / phase / "work")])
                self.addCleanup(host.close)
                execute = fixture.authority.execute
                injected = False
                def inject_unconfirmed_extension(command):
                    nonlocal injected
                    receipt = execute(command)
                    if command.get("report_kind", command["operation"]) == phase and not injected:
                        injected = True
                        clock.seconds = 150
                        task = fixture.authority.state.tasks[tid]
                        with patch.object(fixture.authority._head_store, "write",
                                          side_effect=JournalError("io_error", "Unconfirmed head",
                                                                   ambiguous=True)):
                            with self.assertRaises(AuthorityError):
                                fixture.send("renew", actor="sup", task_id=tid,
                                             attempt_id=task["attempt"]["id"], claim_generation=1,
                                             expected_lease_revision=task["lease_revision"])
                    return receipt
                with patch.object(fixture.authority, "execute", side_effect=inject_unconfirmed_extension):
                    self.assertEqual(host.step()["started"], 0)
                self.assertTrue(injected)
                self.assertFalse(fixture.authority.health()["fresh"])
                self.assertFalse(host.health()["ok"])
                self.assertTrue(all(w.process is None for w in host.workers.values()))
                self.assertEqual(host.step()["started"], 1)
                worker = host.workers[tid]
                self.assertIsNone(worker.pending)
                self.assertEqual(worker.confirmed_lease.isoformat(),
                                 "2026-09-23T12:07:30+00:00" if phase == "claim"
                                 else "2026-09-23T12:05:00+00:00")
                proposals = [p for p in fixture.client.sent if p["record_type"] == "mptask.command"]
                self.assertEqual(sum(p["event"]["command"].get(
                    "report_kind", p["event"]["command"]["operation"]) == phase for p in proposals), 1)
                host.close()
