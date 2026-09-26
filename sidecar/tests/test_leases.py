import unittest
from pathlib import Path

from mempalace_tasks.journal import JsonStore
from mempalace_tasks.maintenance import LeaseMaintenance
from runtime_support import DomainPort, ManualClock, PortError, RealAuthorityFixture, temporary_directory


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.port = DomainPort(self.clock)
        self.maintenance = LeaseMaintenance(self.port, self.clock,
                                           system_actor="sys", recovery_actor="recovery")

    def test_exact_deadline_expires_without_worker_or_user_turn_and_recovers(self):
        tid = self.port.create()
        self.port.claim(tid)
        self.clock.seconds = 299
        self.assertEqual(self.maintenance.tick()["expired"], 0)
        self.clock.seconds = 300
        result = self.maintenance.tick()
        self.assertEqual((result["expired"], result["recovered"]), (1, 1))
        task = self.port.state.tasks[tid]
        self.assertEqual((task["status"], task["automatic_retries_used"]), ("open", 1))
        self.assertEqual(task["retry_not_before"], "2026-09-23T12:05:30Z")
        self.assertEqual(self.maintenance.tick()["expired"], 0)

    def test_progress_deadline_and_hard_cap_are_independent_due_triggers(self):
        for progress, hard in ((120, 900), (120, 120)):
            with self.subTest(progress=progress, hard=hard):
                clock = ManualClock()
                port = DomainPort(clock)
                tid = port.create(policy={"progress_timeout_seconds": progress,
                                          "hard_timeout_seconds": hard})
                port.claim(tid)
                clock.seconds = 120
                result = LeaseMaintenance(port, clock, system_actor="sys",
                                          recovery_actor="recovery").tick()
                self.assertEqual(result["expired"], 1)

    def test_raced_revision_does_not_revoke_renewed_lease(self):
        tid = self.port.create()
        self.port.claim(tid)
        self.clock.seconds = 300
        def race(command):
            if command["operation"] == "expire":
                self.port.before_execute = None
                self.clock.seconds = 299
                task = self.port.state.tasks[tid]
                self.port.send("renew", actor="sup", task_id=tid,
                               attempt_id=task["attempt"]["id"],
                               claim_generation=task["claim_generation"],
                               expected_lease_revision=task["lease_revision"])
                self.clock.seconds = 300
        self.port.before_execute = race
        result = self.maintenance.tick()
        self.assertEqual(result["raced"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.port.state.tasks[tid]["status"], "in_progress")

    def test_shared_reservation_does_not_block_unrelated_isolated_recovery(self):
        shared = self.port.create("shared", resource_keys=["database"])
        isolated = self.port.create()
        for tid in (shared, isolated):
            self.port.claim(tid)
        self.clock.seconds = 300
        result = self.maintenance.tick()
        self.assertEqual((result["expired"], result["recovered"]), (2, 1))
        self.assertEqual(self.port.state.tasks[shared]["status"], "recovering")
        self.assertIsNotNone(self.port.state.resources["database"]["reservation"])
        self.assertEqual(self.port.state.tasks[isolated]["status"], "open")
        proofs = [c["evidence"] for c in self.port.commands if c["operation"] == "recover"]
        self.assertEqual(len(proofs), 1)
        self.assertTrue(proofs[0]["publication_revoked"])
        self.assertNotIn("process_stopped", proofs[0])

    def test_unknown_request_retains_id_but_abandonment_gets_fresh_id(self):
        tid = self.port.create()
        self.port.claim(tid)
        self.clock.seconds = 300
        requests = []
        def fault(command):
            if command["operation"] == "expire":
                requests.append(command["command_id"])
                if len(requests) == 1:
                    raise PortError("outcome_unknown")
                if len(requests) == 2:
                    self.port.outcomes[command["command_id"]] = {
                        "ok": False, "outcome": "abandoned"}
        self.port.before_execute = fault
        self.assertEqual(self.maintenance.tick()["unknown"], 1)
        self.assertEqual(self.maintenance.tick()["abandoned"], 1)
        self.maintenance.tick()
        self.assertEqual(requests[0], requests[1])
        self.assertNotEqual(requests[1], requests[2])
        self.assertEqual(self.port.state.tasks[tid]["status"], "open")

    def test_explicit_post_commit_ambiguity_preserves_original_expiry_id(self):
        tid = self.port.create()
        self.port.claim(tid)
        self.clock.seconds = 300
        def lost(command):
            if command["operation"] == "expire":
                self.port.before_execute = None
                self.port.execute(command)
                error = PortError("clock_sample_failed")
                error.ambiguous = True
                raise error
        self.port.before_execute = lost
        result = self.maintenance.tick()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(len(self.maintenance.requests.pending), 1)
        self.maintenance.tick()
        expiries = [c for c in self.port.commands if c["operation"] == "expire"]
        self.assertEqual(len({c["command_id"] for c in expiries}), 1)
        self.assertEqual(len(self.maintenance.requests.pending), 0)
        self.assertEqual(self.port.state.tasks[tid]["automatic_retries_used"], 1)

    def test_retry_exhaustion_remains_quarantined_without_repeated_recovery(self):
        tid = self.port.create(policy={"automatic_retries": 0})
        self.port.claim(tid)
        self.clock.seconds = 300
        self.maintenance.tick()
        task = self.port.state.tasks[tid]
        self.assertEqual(task["status"], "quarantined")
        self.assertTrue(task["recovery"]["barrier_satisfied"])
        self.assertEqual(self.maintenance.tick()["recovered"], 0)

    def test_three_default_failure_retries_preserve_exponential_backoff_then_quarantine(self):
        tid = self.port.create()
        for start, expiry, next_time, used in (
                (0, 300, "2026-09-23T12:05:30Z", 1),
                (330, 630, "2026-09-23T12:11:30Z", 2),
                (690, 990, "2026-09-23T12:18:30Z", 3),
                (1110, 1410, None, 3)):
            self.clock.seconds = start
            self.port.claim(tid)
            self.clock.seconds = expiry
            self.maintenance.tick()
            task = self.port.state.tasks[tid]
            self.assertEqual(task["automatic_retries_used"], used)
            self.assertEqual(task["retry_not_before"], next_time)
        self.assertEqual(task["status"], "quarantined")
        self.assertEqual(task["escalation"]["code"], "retry_budget_exhausted")

    def test_unexpected_error_is_visible_and_does_not_block_other_tasks(self):
        broken = self.port.create()
        good = self.port.create()
        for tid in (broken, good):
            self.port.claim(tid)
        self.clock.seconds = 300
        def fault(command):
            if command["task_id"] == broken:
                raise PortError("storage_failure")
        self.port.before_execute = fault
        result = self.maintenance.tick()
        self.assertTrue(result["errors"])
        self.assertEqual(self.port.state.tasks[good]["status"], "open")
        self.assertFalse(self.maintenance.health()["ok"])

    def test_configured_privileged_identities_are_validated(self):
        for system, recovery in (("op", "recovery"), ("sys", "worker"),
                                 ("missing", "recovery"), ("sys", "sys")):
            with self.subTest(system=system, recovery=recovery):
                with self.assertRaises(ValueError):
                    LeaseMaintenance(self.port, self.clock, system_actor=system,
                                     recovery_actor=recovery)

    def test_batch_limit_bounds_commands_and_work_is_eventually_drained(self):
        for _ in range(3):
            self.port.claim(self.port.create())
        self.clock.seconds = 300
        maintenance = LeaseMaintenance(self.port, self.clock, system_actor="sys",
                                       recovery_actor="recovery", batch_limit=2)
        before = len(self.port.commands)
        first = maintenance.tick()
        self.assertLessEqual(len(self.port.commands) - before, 2)
        self.assertGreater(first["unfinished"], 0)
        for _ in range(4):
            maintenance.tick()
        self.assertTrue(all(t["status"] == "open" for t in self.port.state.tasks.values()))

    def test_real_authority_settlement_abandonment_requires_new_expiry_command(self):
        with temporary_directory() as directory:
            fixture = RealAuthorityFixture(directory, self.clock)
            self.addCleanup(fixture.authority.close)
            tid = fixture.create()
            fixture.claim(tid)
            maintenance = LeaseMaintenance(fixture.bound, self.clock,
                                           system_actor="sys", recovery_actor="recovery")
            self.clock.seconds = 300
            fixture.client.behaviors = ["lost", "lost", "lost", "lost"]
            self.assertEqual(maintenance.tick()["unknown"], 1)
            self.assertEqual(maintenance.tick()["abandoned"], 1)
            result = maintenance.tick()
            self.assertEqual((result["expired"], result["recovered"]), (1, 1))
            originals = [p for p in fixture.client.sent if p["record_type"] == "mptask.command"
                         and p["event"]["command"]["operation"] == "expire"]
            self.assertEqual(len(originals), 2)
            self.assertNotEqual(originals[0]["command_id"], originals[1]["command_id"])
            self.assertEqual(fixture.state.tasks[tid]["automatic_retries_used"], 1)

    def test_real_authority_restart_fences_previous_executor_and_preserves_retries(self):
        from mempalace_tasks.authority import AuthorityError, TaskAuthority
        with temporary_directory() as directory:
            fixture = RealAuthorityFixture(Path(directory) / "state", self.clock)
            tid = fixture.create()
            fixture.claim(tid)
            authority_id = fixture.state.authority_id
            fixture.authority.close()
            restarted = TaskAuthority(authority_id, fixture.client, Path(directory) / "state",
                                      clock=self.clock, backoff=lambda _: None,
                                      system_actor="sys", recovery_actor="recovery").start()
            self.addCleanup(restarted.close)
            fixture.authority = restarted
            with self.assertRaises(AuthorityError) as caught:
                fixture.send("renew", actor="sup", task_id=tid,
                             attempt_id=fixture.state.tasks[tid]["attempt"]["id"],
                             claim_generation=1, expected_lease_revision=1)
            self.assertEqual(caught.exception.code, "not_started")
            fixture.bound = restarted.bound_current()
            maintenance = LeaseMaintenance(fixture.bound, self.clock, system_actor="sys",
                                           recovery_actor="recovery")
            result = maintenance.tick()
            self.assertEqual(result["errors"], [])
            self.assertFalse(restarted.health()["startup_pending"])
            self.assertEqual(fixture.state.tasks[tid]["status"], "open")
            self.assertEqual(fixture.state.tasks[tid]["automatic_retries_used"], 0)
