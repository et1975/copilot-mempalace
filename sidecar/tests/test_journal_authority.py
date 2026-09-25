"""Palace-only recovery over disposable ordered sources and local lock fixtures."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from mempalace_tasks import authority as authority_module
from mempalace_tasks.authority import AuthorityError, TaskAuthority
from mempalace_tasks.codec import canonical_json
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.palace import PalaceError
from mempalace_tasks.protocol import LogState, fold_record, make_epoch, make_proposal, make_settlement
from mempalace_tasks.runtime_clock import RuntimeClock
from authority_fixture import (
    AUTHORITY, NOW, Clock, LogClient, command, create, genesis, state_directory, uid,
)
from test_epochs import epoch_raw


class JournalClient(LogClient):
    def append_event(self, payload):
        self.sent.append(deepcopy(payload))
        behavior = self.behaviors.pop(0) if self.behaviors else "ok"
        if behavior in {"lost", "reject"}:
            raise PalaceError("upstream_unavailable", "Unavailable", ambiguous=behavior == "lost")
        event = epoch_raw(payload, len(self.events) + 1)
        self.events.append(event)
        if self.on_append:
            self.on_append(payload)
        if behavior == "commit_lost":
            raise PalaceError("upstream_unavailable", "Response lost", ambiguous=True)
        return deepcopy(event)


def runtime_clock(at=NOW):
    wall = int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()) * 1_000_000_000
    return RuntimeClock(wall_time_ns=lambda: wall, continuous_time_ns=lambda: 0)


class JournalAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.directory = state_directory()
        self.addCleanup(self.directory.cleanup)
        self.client = JournalClient()
        self.seed = LogState(AUTHORITY)
        config = genesis()
        config["execution_profiles"]["shared"] = {"execution_class": "shared_unfenced",
                                                  "supports_reconciliation": True}
        config["supervisors"]["supervisor"]["profiles"].append("shared")
        self.seed_command(config)
        self.delays = []

    def seed_command(self, value, at=NOW):
        proposal = make_proposal(self.seed, value, at)
        event = epoch_raw(proposal, len(self.client.events) + 1)
        self.client.events.append(event)
        fold_record(self.seed, event)
        return proposal

    def authority(self, *, start=True, **options):
        # Import through the consumer surface: missing implementation is a real RED.
        cls = authority_module.JournalAuthority
        value = cls(AUTHORITY, self.client, self.directory.name,
                    clock=options.pop("clock", runtime_clock()),
                    backoff=self.delays.append, **options)
        self.addCleanup(value.close)
        if start:
            value.start()
        return value

    def send(self, owner, value):
        return owner.execute(value, expected_epoch=owner.epoch_id)

    def active_task(self, number=2, *, shared=False, running=False):
        value = create(number)
        if shared:
            value.update(execution_class="shared_unfenced", execution_profile="shared",
                         resource_keys=["database"])
        task = self.seed_command(value)["event"]["tasks"][0]
        task = self.seed_command(command(
            number + 1, "claim", "worker", task_id=task["id"], expected_version=1,
            supervisor_id="supervisor"))["event"]["tasks"][0]
        if running:
            task = self.seed_command(command(
                number + 2, "attempt_report", "supervisor", task_id=task["id"],
                expected_version=task["version"], attempt_id=task["attempt"]["id"],
                claim_generation=task["claim_generation"], report_kind="started",
                evidence={"references": ["artifact:prepared"], "prepared": True}))["event"]["tasks"][0]
        return task

    def assert_error(self, code, action):
        with self.assertRaises(AuthorityError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_legacy_restart_cannot_borrow_an_accepted_journal_epoch(self):
        legacy = TaskAuthority(AUTHORITY, self.client, self.directory.name,
                               clock=Clock(), backoff=self.delays.append).start()
        self.addCleanup(legacy.close)
        created = legacy.execute(create())["tasks"][0]
        legacy.close()
        owner = self.authority()
        claimed = self.send(owner, command(
            3, "claim", "worker", task_id=created["id"], expected_version=1,
            supervisor_id="supervisor"))["tasks"][0]
        self.send(owner, command(
            4, "attempt_report", "supervisor", task_id=created["id"],
            expected_version=claimed["version"], attempt_id=claimed["attempt"]["id"],
            claim_generation=claimed["claim_generation"], report_kind="started",
            evidence={"references": ["artifact:prepared"], "prepared": True}))
        owner.close()
        head = Path(self.directory.name, "verified_head.json").read_bytes()
        sent = len(self.client.sent)
        stale = TaskAuthority(AUTHORITY, self.client, self.directory.name,
                              clock=Clock(), backoff=self.delays.append)
        self.addCleanup(stale.close)
        self.assert_error("migration_required", stale.start)
        self.assertEqual(len(self.client.sent), sent)
        self.assertEqual(Path(self.directory.name, "verified_head.json").read_bytes(), head)
        replacement = self.authority()
        self.assertNotEqual(replacement.epoch_id, owner.epoch_id)
        self.assertFalse(replacement.get(created["id"])["authorization"]["authorized"])

    def test_legacy_refresh_fails_closed_before_writing_under_a_foreign_epoch(self):
        legacy = TaskAuthority(AUTHORITY, self.client, self.directory.name,
                               clock=Clock(), backoff=self.delays.append).start()
        self.addCleanup(legacy.close)
        head = Path(self.directory.name, "verified_head.json").read_bytes()
        barrier = make_epoch(legacy.log, uid(100), uid(101), NOW)
        self.client.events.append(epoch_raw(barrier, len(self.client.events) + 1))
        self.assert_error("migration_required", legacy.refresh)
        self.assertFalse(legacy.health()["fresh"])
        self.assertEqual(legacy.health()["reason"], "migration_required")
        self.assert_error("migration_required", lambda: legacy.execute(create()))
        self.assertEqual(self.client.sent, [])
        self.assertEqual(Path(self.directory.name, "verified_head.json").read_bytes(), head)

    def test_replay_and_execute_use_only_source_and_live_lock_not_legacy_stores(self):
        task = self.seed_command(create())["event"]["tasks"][0]
        self.seed_command(command(3, "note", task_id=task["id"], expected_version=1,
                                  tag="note", text="Source-backed note"))
        records = deepcopy(self.seed.accepted_records)
        with patch("mempalace_tasks.authority.AuthorityLock",
                   side_effect=AssertionError("legacy owner constructed")), patch(
                       "mempalace_tasks.authority.PendingStore",
                       side_effect=AssertionError("pending store constructed")), patch(
                       "mempalace_tasks.authority.HeadStore",
                       side_effect=AssertionError("head store constructed")):
            owner = self.authority(expected_configuration=self.seed.state.configuration)
            self.assertEqual(owner.accepted_records(), records)
            self.assertEqual(owner.state.tasks, self.seed.state.tasks)
            result = self.send(owner, create(4))
            self.assertEqual(result["outcome"], "committed")
            self.assertEqual(result["epoch_id"], owner.epoch_id)
            self.assertTrue(owner.health()["request_epoch_required"])
            self.assertEqual(owner.health()["schema_version"], 1)
            self.assertEqual(owner.recovery_mode, "journal")
        self.assertEqual({p.name for p in Path(self.directory.name).iterdir()}, {"authority.lock"})

    def test_corrupt_future_pending_head_clock_and_projection_are_ignored_unchanged(self):
        paths = [Path(self.directory.name, name) for name in (
            "pending.json", "verified_head.json", "clock.json", "projection.json",
            "pending.archive-future.json")]
        for path in paths:
            path.write_bytes(b"future bytes, not valid JSON")
        owner = self.authority()
        self.send(owner, create())
        owner.reconcile()
        owner.refresh(verify_prefix=True)
        self.assertTrue(owner.snapshot()["fresh"])
        self.assertTrue(all(path.read_bytes() == b"future bytes, not valid JSON" for path in paths))
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), len(paths) + 1)

    def test_absent_expected_stream_fails_before_activation_and_releases_lock(self):
        self.client.events.clear()
        owner = self.authority(start=False)
        self.assert_error("not_initialized", owner.start)
        self.assertEqual(self.client.sent, [])
        self.assertFalse(owner.health()["fresh"])
        initializing = self.authority(initialize=True)
        self.assertIsNotNone(initializing.epoch_id)
        self.assertFalse(initializing.health()["fresh"])
        self.assertEqual(initializing.health()["reason"], "uninitialized")
        initializing.execute_current(genesis())
        self.assertTrue(initializing.health()["fresh"])

    def test_corrupt_or_foreign_journal_never_appends_activation(self):
        original = deepcopy(self.client.events)
        for change in ("body", "stream"):
            self.client.events = deepcopy(original)
            self.client.events[0][change] = "{broken" if change == "body" else "mptask/foreign"
            owner = self.authority(start=False)
            self.assert_error("invariant_violation", owner.start)
        self.assertEqual(self.client.sent, [])

    def test_expected_configuration_is_checked_before_activation_or_initial_genesis(self):
        wrong = deepcopy(self.seed.state.configuration)
        wrong["actors"]["worker"] = "system"
        owner = self.authority(start=False, expected_configuration=wrong)
        self.assert_error("configuration_conflict", owner.start)
        self.assertEqual(self.client.sent, [])
        self.client.events.clear()
        owner = self.authority(initialize=True, expected_configuration=wrong)
        count = len(self.client.sent)
        self.assert_error("configuration_conflict", lambda: owner.execute_current(genesis()))
        self.assertEqual(len(self.client.sent), count)
        self.assertIsNone(owner.state.configuration)

    def test_activation_commit_lost_is_resolved_from_exact_source_identity(self):
        self.client.behaviors = ["commit_lost"]
        owner = self.authority()
        self.assertTrue(owner.health()["fresh"])
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(owner.log.activation_attempts[owner.log.activation_id]["outcome"], "accepted")
        self.assertEqual(owner.log.activation_event_id, self.client.events[-1]["id"])

    def test_activation_unknown_retries_identical_bytes_and_fails_bounded(self):
        self.client.behaviors = ["lost"] * 3
        owner = self.authority(start=False)
        error = self.assert_error("activation_unknown", owner.start)
        self.assertTrue(error.ambiguous)
        self.assertEqual(len(self.client.sent), 3)
        self.assertTrue(all(payload == self.client.sent[0] for payload in self.client.sent))
        self.assertFalse(owner.health()["started"])
        fresh = self.authority()
        self.assertNotEqual(fresh.epoch_id, self.client.sent[0]["epoch_id"])

    def test_rejected_competing_activation_gets_fresh_identity_and_epoch(self):
        append = self.client.append_event
        competitor = make_epoch(self.seed, uid(9001), uid(9002), NOW)
        raced = False

        def race(payload):
            nonlocal raced
            if not raced and payload["record_type"] == "mptask.epoch":
                raced = True
                append(competitor)
            return append(payload)

        with patch.object(self.client, "append_event", side_effect=race):
            owner = self.authority()
        own = self.client.sent[1:]
        self.assertEqual(len(own), 2)
        self.assertNotEqual(own[0]["activation_id"], own[1]["activation_id"])
        self.assertNotEqual(own[0]["epoch_id"], own[1]["epoch_id"])
        self.assertEqual(owner.epoch_id, own[1]["epoch_id"])
        self.assertEqual(owner.log.activation_attempts[own[0]["activation_id"]]["outcome"], "stale")
        self.client.append_event(own[0])
        owner.refresh()
        self.assertTrue(owner.health()["fresh"])

    def test_every_reopened_owner_has_new_epoch_and_old_public_requests_fail_first(self):
        owner = self.authority()
        old_epoch = owner.epoch_id
        result = self.send(owner, create())
        owner.close()
        replacement = self.authority()
        self.assertNotEqual(replacement.epoch_id, old_epoch)
        before = len(self.client.sent)
        self.assert_error("epoch_required", lambda: replacement.execute(create()))
        self.assert_error("stale_epoch", lambda: replacement.execute(create(), expected_epoch=old_epoch))
        self.assert_error("stale_epoch", lambda: replacement.execute(
            {"operation": []}, expected_epoch=old_epoch))
        self.assertEqual(len(self.client.sent), before)
        historical = replacement.outcome(old_epoch, uid(2))
        self.assertEqual(historical["resolution"], "committed")
        self.assertEqual(historical["receipt"]["response"], result["response"])
        self.assertNotIn("outcome", historical)
        self.assertFalse(historical["authorization"]["authorized"])

    def test_delayed_old_proposal_and_settlement_cannot_settle_new_same_uuid(self):
        old_owner = self.authority()
        old_epoch = old_owner.epoch_id
        old = make_proposal(old_owner.log, create(), old_owner.clock_source.now())
        old_owner.close()
        owner = self.authority()
        new = create()
        new["title"] = "New scoped request"
        result = self.send(owner, new)
        self.client.append_event(old)
        self.client.append_event(make_settlement(old))
        owner.refresh()
        self.assertEqual(self.send(owner, new)["response"], result["response"])
        self.assertEqual(owner.outcome(old_epoch, uid(2))["resolution"], "not_recorded")
        self.assertEqual(owner.outcome(owner.epoch_id, uid(2))["resolution"], "committed")
        self.assertEqual(len(owner.state.tasks), 1)

    def test_inherited_active_snapshot_is_revoked_and_old_renew_completion_are_fenced(self):
        task = self.active_task(running=True)
        owner = self.authority()
        current = owner.get(task["id"])
        self.assertEqual(current["task"]["status"], "recovering")
        self.assertFalse(current["authorization"]["authorized"])
        self.assertFalse(current["authorization"]["lease_live"])
        self.assertEqual(current["task"]["automatic_retries_used"], 0)
        self.assertFalse(owner.health()["startup_pending"])
        self.assertEqual(self.client.sent[-1]["event"]["command"]["report_kind"], "recovery_started")
        for old in (
            command(10, "renew", "supervisor", task_id=task["id"], attempt_id=task["attempt"]["id"],
                    claim_generation=1, expected_lease_revision=task["lease_revision"]),
            command(11, "transition", "worker", task_id=task["id"], expected_version=task["version"],
                    attempt_id=task["attempt"]["id"], claim_generation=1, target="closed",
                    summary="Old completion", evidence=["artifact:old"]),
        ):
            before = len(self.client.sent)
            self.assert_error("epoch_required", lambda: owner.execute(old))
            self.assert_error("stale_epoch", lambda: owner.execute(old, expected_epoch=uid(7777)))
            self.assertEqual(len(self.client.sent), before)

    def test_shared_restart_keeps_reservations_and_never_spends_retry_budget(self):
        task = self.active_task(shared=True)
        reservation = deepcopy(self.seed.state.resources["database"]["reservation"])
        owner = self.authority()
        self.assertEqual(owner.state.resources["database"]["reservation"], reservation)
        self.assertEqual(owner.state.tasks[task["id"]]["automatic_retries_used"], 0)
        maintenance = LeaseMaintenance(owner.bound_current(), owner.clock_source,
                                       system_actor="system", recovery_actor="operator")
        self.assertEqual(maintenance.tick()["errors"], [])
        self.assertEqual(owner.state.tasks[task["id"]]["status"], "recovering")
        self.assertEqual(owner.state.resources["database"]["reservation"], reservation)

    def test_isolated_startup_recovery_maintenance_preserves_retries(self):
        task = self.active_task()
        owner = self.authority()
        maintenance = LeaseMaintenance(owner.bound_current(), owner.clock_source,
                                       system_actor="system", recovery_actor="operator")
        result = maintenance.tick()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["recovered"], 1)
        recovered = owner.state.tasks[task["id"]]
        self.assertEqual(recovered["status"], "open")
        self.assertEqual(recovered["automatic_retries_used"], 0)
        self.assertIsNone(recovered["retry_not_before"])

    def test_bounded_startup_remains_non_authorizing_until_all_inherited_attempts_revoked(self):
        a = self.active_task()
        b = self.active_task(10)
        with patch.object(authority_module.JournalAuthority, "STARTUP_BATCH", 1):
            owner = self.authority()
            self.assertTrue(owner.health()["startup_pending"])
            self.assertFalse(owner.health()["fresh"])
            for task in (a, b):
                self.assertFalse(owner.get(task["id"])["authorization"]["authorized"])
            self.assert_error("startup_pending", lambda: self.send(owner, create(20)))
            self.assert_error("not_current", owner.ready)
            owner.reconcile()
        self.assertFalse(owner.health()["startup_pending"])
        self.assertTrue(all(t["status"] == "recovering" for t in owner.state.tasks.values()))

    def test_bound_executor_does_not_upgrade_old_internal_requests_after_same_object_restart(self):
        owner = self.authority()
        bound = owner.bound_current()
        old = owner.epoch_id
        owner.close()
        owner.start()
        self.assertNotEqual(old, owner.epoch_id)
        self.assert_error("stale_epoch", lambda: bound.execute(create()))
        self.assertEqual(owner.state.tasks, {})

    def test_historical_committed_claim_cannot_authorize_restored_matching_attempt(self):
        task = self.active_task(running=True)
        with patch.object(authority_module.JournalAuthority, "STARTUP_BATCH", 1):
            # Two attempts ensure one inherited tuple can remain live in the projection.
            second = self.active_task(10, running=True)
            owner = self.authority()
        unresolved = next(t for t in owner.state.tasks.values() if t["status"] == "in_progress")
        number = 4 if unresolved["id"] == task["id"] else 12
        result = owner.outcome(None, uid(number))
        self.assertEqual(result["resolution"], "committed")
        self.assertFalse(result["authorization"]["authorized"])
        self.assertTrue(all(not row["authorized"]
                            for row in result["receipt"]["authorization"]["tasks"]))
        self.assertIn(unresolved["id"], (task["id"], second["id"]))

    def test_effective_clock_observes_accepted_domain_and_barrier_high_water_before_commands(self):
        future = "2035-01-01T00:00:00Z"
        self.seed_command(create(), at=future)
        barrier = make_epoch(self.seed, uid(7001), uid(7002), "2036-01-01T00:00:00Z")
        self.client.events.append(epoch_raw(barrier, len(self.client.events) + 1))
        fold_record(self.seed, self.client.events[-1])
        source = runtime_clock(NOW)
        owner = self.authority(clock=source)
        result = self.send(owner, create(3))
        self.assertGreaterEqual(owner.health()["as_of"], "2036-01-01")
        self.assertGreaterEqual(result["response"]["tasks"][0]["created_at"], "2036-01-01")
        self.assertFalse(hasattr(source, "pending_reboot"))
        self.assertEqual(datetime.fromisoformat(owner.clock_source.now().replace("Z", "+00:00")),
                         datetime.fromisoformat(source.now().replace("Z", "+00:00")))

    def test_live_rollback_and_changed_prefix_are_latched_failures_without_new_activation(self):
        for change in ("remove", "edit"):
            with self.subTest(change=change):
                owner = self.authority()
                self.send(owner, create(20 if change == "remove" else 21))
                saved = deepcopy(self.client.events)
                count = len(self.client.sent)
                if change == "remove":
                    self.client.events.pop()
                else:
                    self.client.events[0]["created_at"] = "2026-09-23T00:00:01Z"
                self.assert_error("log_rollback", owner.refresh)
                self.client.events = saved
                self.assert_error("log_rollback", owner.reconcile)
                self.assertFalse(owner.snapshot()["fresh"])
                self.assertEqual(len(self.client.sent), count)
                owner.close()

    def test_later_foreign_epoch_fences_live_owner_without_automatic_takeover(self):
        owner = self.authority()
        own_epoch = owner.epoch_id
        self.client.append_event(make_epoch(owner.log, uid(8001), uid(8002), NOW))
        count = len(self.client.sent)
        self.assert_error("epoch_superseded", owner.refresh)
        self.assert_error("epoch_superseded", lambda: owner.execute_current(create()))
        self.assertFalse(owner.snapshot()["fresh"])
        self.assertEqual(owner.epoch_id, own_epoch)
        self.assertEqual(len(self.client.sent), count)

    def test_legacy_live_lock_excludes_journal_owner_without_replacing_inode(self):
        legacy = TaskAuthority(AUTHORITY, self.client, self.directory.name, clock=Clock(),
                               backoff=lambda _: None).start()
        self.addCleanup(legacy.close)
        path = Path(self.directory.name, "authority.lock")
        inode = path.stat().st_ino
        owner = self.authority(start=False)
        self.assert_error("authority_locked", owner.start)
        legacy.close()
        owner.start()
        self.assertEqual(path.stat().st_ino, inode)

    def test_pending_uncertainty_lives_only_in_memory_and_reconcile_uses_identical_settlement(self):
        owner = self.authority()
        self.client.behaviors = ["lost"] * 4
        self.assert_error("outcome_unknown", lambda: self.send(owner, create()))
        controls = deepcopy(self.client.sent[-3:])
        self.assertTrue(all(control == controls[0] for control in controls))
        count = len(self.client.sent)
        self.assertFalse(owner.snapshot()["fresh"])
        self.assertEqual(len(self.client.sent), count)
        self.assertEqual(owner.reconcile()["outcome"], "abandoned")
        self.assertEqual(self.client.sent[-1], controls[0])
        self.assertEqual({p.name for p in Path(self.directory.name).iterdir()}, {"authority.lock"})

    def test_dead_owner_pending_is_not_replayed_and_next_barrier_fences_late_original(self):
        owner = self.authority()
        self.client.behaviors = ["lost"] * 4
        old_epoch = owner.epoch_id
        self.assert_error("outcome_unknown", lambda: self.send(owner, create()))
        old = deepcopy(self.client.sent[-4])
        owner.close()
        owner = self.authority()
        self.client.append_event(old)
        owner.refresh()
        self.assertEqual(owner.state.tasks, {})
        self.assertEqual(owner.outcome(old_epoch, uid(2))["resolution"], "not_recorded")
        self.assertIsNone(owner.health()["pending_command"])
        self.send(owner, create())
        self.assertEqual(len(owner.state.tasks), 1)

    def test_committed_pending_response_loss_survives_new_owner_without_local_files(self):
        owner = self.authority()
        self.client.behaviors = ["commit_lost", "ok"]
        receipt = self.send(owner, create())
        old = owner.epoch_id
        owner.close()
        owner = self.authority()
        result = owner.outcome(old, uid(2))
        self.assertEqual(result["resolution"], "committed")
        self.assertEqual(result["receipt"]["response"], receipt["response"])
        self.assertEqual(canonical_json(owner.log.accepted_records[-1]["event"]),
                         canonical_json(self.client.sent[1]["event"]))

    def test_only_rejected_barrier_timestamps_do_not_move_replay_clock_into_discarded_future(self):
        marker = make_epoch(self.seed, uid(7001), uid(7002), "2099-01-01T00:00:00Z")
        marker["previous_activation_id"] = uid(7999)
        from mempalace_tasks.codec import payload_hash
        marker["payload_hash"] = payload_hash(marker)
        self.client.events.append(epoch_raw(marker, len(self.client.events) + 1))
        owner = self.authority()
        self.assertLess(owner.health()["as_of"], "2027-01-01")

    def test_lost_startup_reads_retry_only_same_barrier_and_replay_first_physical_occurrence(self):
        reads = self.client.replay_events
        calls = 0

        def read(cursor=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PalaceError("upstream_unavailable", "Lost first post-append read")
            yield from reads(cursor)

        with patch.object(self.client, "replay_events", side_effect=read):
            owner = self.authority()
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.sent[0], self.client.sent[1])
        self.assertEqual(owner.log.activation_event_id, "evt-000002")
        self.assertEqual(owner.log.raw_cursor, "evt-000003")
        self.assertTrue(owner.health()["fresh"])

    def test_failed_startup_recovery_is_reconstructed_by_next_owner_not_local_pending(self):
        task = self.active_task()
        self.client.behaviors = ["ok"] + ["lost"] * 4
        failed = self.authority(start=False)
        self.assert_error("outcome_unknown", failed.start)
        self.assertFalse(failed.health()["started"])
        failed_epoch = failed.epoch_id
        original = deepcopy(self.client.sent[-4])
        owner = self.authority()
        self.assertNotEqual(owner.epoch_id, failed_epoch)
        current = deepcopy(owner.state.tasks[task["id"]])
        self.assertEqual(current["status"], "recovering")
        self.assertEqual(current["automatic_retries_used"], 0)
        self.client.append_event(original)
        owner.refresh()
        self.assertEqual(owner.state.tasks[task["id"]], current)
        self.assertEqual(owner.log.history[-1]["disposition"], "stale")

    def test_quiesced_older_palace_restore_accepts_selected_prefix_not_future_local_files(self):
        owner = self.authority()
        first = self.send(owner, create())["tasks"][0]
        selected = deepcopy(self.client.events)
        self.send(owner, create(3))
        future_epoch = owner.epoch_id
        owner.close()
        self.client.events = selected
        Path(self.directory.name, "verified_head.json").write_text(
            '{"future":"unrelated checkpoint"}', encoding="utf-8")
        restored = self.authority()
        self.assertNotEqual(restored.epoch_id, future_epoch)
        self.assertEqual(set(restored.state.tasks), {first["id"]})
        self.assertEqual(restored.outcome(future_epoch, uid(3))["resolution"], "not_recorded")
        self.assertTrue(restored.health()["fresh"])

    def test_same_uuid_committed_outcomes_are_retained_in_each_epoch_without_authority_upgrade(self):
        owner = self.authority()
        task = self.send(owner, create())["tasks"][0]
        first_epoch = owner.epoch_id
        first = self.send(owner, command(3, "note", task_id=task["id"], expected_version=1,
                                         tag="note", text="First scope"))
        owner.close()
        owner = self.authority()
        second = self.send(owner, command(3, "note", task_id=task["id"], expected_version=2,
                                          tag="note", text="Second scope"))
        historic = owner.outcome(first_epoch, uid(3))
        current = owner.outcome(owner.epoch_id, uid(3))
        self.assertEqual(historic["receipt"]["response"], first["response"])
        self.assertEqual(current["receipt"]["response"], second["response"])
        historic["receipt"]["tasks"][0]["title"] = "Caller mutation"
        self.assertEqual(owner.state.tasks[task["id"]]["title"], "Task 2")

    def test_clock_and_actor_configuration_fail_explicitly_before_acquiring_or_appending(self):
        for options in (
            {"clock": Clock()}, {"clock": object()},
            {"system_actor": []}, {"recovery_actor": ""}, {"initialize": 1},
        ):
            with self.subTest(options=options):
                with self.assertRaises(AuthorityError):
                    self.authority(start=False, **options)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
