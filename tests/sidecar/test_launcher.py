"""Explicit start and drain observations using actual owned child processes."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from mempalace_tasks import discovery, launcher
from mempalace_tasks import platform_support as platform
from mempalace_tasks.server_identity import make_proof
from portable_lifecycle_fixture import ConfigurationFixture, IdentityServer, ROOT_TOKEN


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ConfigurationFixture()
        self.addCleanup(self.fixture.close)
        self.config = self.fixture.config
        self.children = []
        self.calls = []
        actual = subprocess.Popen

        def spawn(argv, **kwargs):
            self.calls.append((list(argv), kwargs.copy()))
            # Only substitute the installed entry module with a controlled
            # executable; native Popen, stdin, handles, HTTP, locks all stay real.
            self.assertEqual(argv[:3], [sys.executable, "-m", "mempalace_tasks"])
            argv = [*argv[:2], "portable_lifecycle_fixture", *argv[3:]]
            child = actual(argv, **kwargs)
            self.children.append(child)
            return child

        self.addCleanup(self.clean_children)
        patcher = patch.object(launcher.subprocess, "Popen", side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def clean_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(3)

    def server(self, **kwargs):
        platform.ensure_private_directory(self.config.runtime_dir)
        fixture = IdentityServer(self.config, **kwargs)
        self.addCleanup(fixture.close)
        fixture.publish()
        return fixture

    def error(self, code, action):
        with self.assertRaises(discovery.DiscoveryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(ROOT_TOKEN, repr(caught.exception))
        return caught.exception

    def test_external_lifecycle_refuses_before_runtime_creation(self):
        self.fixture.document["lifecycle"] = "external"
        self.fixture.save()
        self.error("external_lifecycle", lambda: launcher.start(self.fixture.path))
        self.assertFalse(self.config.runtime_dir.exists())
        self.assertFalse(self.children)

    def test_explicit_start_uses_exact_private_ticket_and_child_owned_lock(self):
        accepted_epoch = "22222222-2222-2222-2222-222222222222"
        with patch.dict(os.environ, {"MPTASK_FIXTURE_ACCEPTED_EPOCH": accepted_epoch}):
            info = launcher.start(self.fixture.path, timeout=3)
        self.assertEqual(info.instance_id, accepted_epoch)
        self.assertTrue(info.ready)
        self.assertEqual(len(self.children), 1)
        child = self.children[0]
        self.assertIsNone(child.poll())
        argv, kwargs = self.calls[0]
        self.assertEqual(argv[3:], ["--config", str(self.fixture.path),
                                    "serve", "--startup-ticket-stdin"])
        self.assertNotIn(ROOT_TOKEN, repr(argv) + repr(kwargs))
        self.assertTrue(kwargs["close_fds"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        if os.name == "posix":
            self.assertTrue(kwargs["start_new_session"])
        with self.assertRaises(platform.PlatformError) as caught:
            platform.LifetimeLock(self.config.runtime_dir / "authority.lock").acquire()
        self.assertEqual(caught.exception.code, "lock_busy")
        with platform.LifetimeLock(self.config.runtime_dir / "start.lock"):
            pass
        log_paths = list(self.config.runtime_dir.glob("launch-*.log"))
        self.assertEqual(len(log_paths), 1)
        log = json.loads(platform.read_regular(log_paths[0], private=True))
        self.assertEqual(log["accepted_epoch"], accepted_epoch)
        self.assertEqual(log["ticket"]["authority_id"], self.config.authority_id)
        self.assertEqual(set(log["ticket"]),
                         {"schema_version", "authority_id", "binding", "nonce"})
        self.assertNotIn(accepted_epoch, json.dumps(log["ticket"]))
        self.assertNotIn(ROOT_TOKEN, json.dumps(log))
        self.assertNotIn(info.token, json.dumps(log))
        self.assertEqual(child.pid, log["pid"])
        launcher.stop(self.config, expected_instance_id=info.instance_id, timeout=3)
        self.assertEqual(child.wait(3), 0)

    def test_concurrent_start_election_converges_on_one_child_and_instance(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: launcher.start(self.fixture.path, timeout=4),
                                    range(4)))
        self.assertEqual(len(self.children), 1)
        self.assertEqual(len({item.instance_id for item in results}), 1)
        self.assertTrue(all(item.ready for item in results))
        launcher.stop(self.config, expected_instance_id=results[0].instance_id, timeout=3)

    def test_ready_existing_owner_is_reused_without_spawning(self):
        server = self.server()
        info = launcher.start(self.fixture.path, timeout=1)
        self.assertEqual(info.instance_id, server.identity.instance_id)
        self.assertFalse(self.children)

    def test_slow_authenticated_existing_owner_is_reused_without_spawning(self):
        server = self.server(identity_delay=0.4)
        direct = discovery.connect(self.config, timeout=2)
        info = launcher.start(self.fixture.path, timeout=2)
        self.assertEqual(info, direct)
        self.assertEqual(len(server.requests), 2)
        self.assertFalse(self.children)
        self.assertFalse(server.closed.is_set())
        self.assertTrue(launcher._owner_busy(self.config))

    def test_slow_existing_owner_timeout_is_bounded_and_never_stops_the_owner(self):
        server = self.server(identity_delay=0.4)
        began = time.monotonic()
        self.error("owner_unready", lambda: launcher.start(self.fixture.path, timeout=0.15))
        self.assertLess(time.monotonic() - began, 0.7)
        self.assertFalse(self.children)
        self.assertFalse(server.closed.is_set())
        self.assertTrue(launcher._owner_busy(self.config))
        self.assertFalse(any(item[0] == "POST" for item in server.requests))

    def test_slow_identity_with_wrong_token_or_instance_is_not_reused(self):
        server = self.server(identity_delay=0.4)
        for mode, token in (("normal", "other-test-only-token"), ("wrong-instance", ROOT_TOKEN)):
            with self.subTest(mode=mode):
                server.mode = mode
                self.config.service_token_file.write_text(token, encoding="utf-8")
                self.error("identity_mismatch", lambda: launcher.start(self.fixture.path, timeout=2))
                self.assertFalse(self.children)
                self.assertFalse(server.closed.is_set())
                self.assertTrue(launcher._owner_busy(self.config))

    def test_readiness_retries_share_one_overall_deadline(self):
        server = self.server()
        for timeout, ready, elapsed in ((0.8, False, 0.8), (1.0, True, 0.85)):
            with self.subTest(timeout=timeout):
                clock = SimpleNamespace(now=100.0)

                def sleep(duration):
                    clock.now += duration

                def exchange(identity, deadline, *, path):
                    # Model a 400ms HTTP exchange without replacing proof validation.
                    sleep(min(0.4, deadline.remaining()))
                    deadline.remaining()
                    return make_proof(identity, ROOT_TOKEN, path.split("nonce=")[1],
                                      ready=clock.now >= 100.7)

                fake_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
                with patch.object(discovery, "time", fake_time), patch.object(
                        discovery, "_request", side_effect=exchange):
                    if ready:
                        info = launcher.start(self.fixture.path, timeout=timeout)
                        self.assertEqual(info.instance_id, server.identity.instance_id)
                        self.assertTrue(info.ready)
                    else:
                        self.error("owner_unready", lambda: launcher.start(
                            self.fixture.path, timeout=timeout))
                self.assertAlmostEqual(clock.now - 100.0, elapsed)
        self.assertFalse(self.children)
        self.assertTrue(launcher._owner_busy(self.config))

    def test_held_owner_lock_without_registry_never_starts_a_competing_child(self):
        platform.ensure_private_directory(self.config.runtime_dir)
        with platform.LifetimeLock(self.config.runtime_dir / "authority.lock"):
            self.error("owner_unready", lambda: launcher.start(self.fixture.path, timeout=0.2))
        self.assertFalse(self.children)

    def test_unready_owner_can_become_ready_without_spawning(self):
        server = self.server()
        server.after = time.monotonic() + 0.15
        info = launcher.start(self.fixture.path, timeout=1)
        self.assertEqual(info.instance_id, server.identity.instance_id)
        self.assertFalse(self.children)

    def test_authenticated_listener_without_ownership_is_not_reused(self):
        server = self.server(own=False)
        self.error("owner_unlocked", lambda: launcher.start(self.fixture.path, timeout=1))
        self.assertFalse(self.children)
        self.assertFalse(server.closed.is_set())

    def test_slow_authenticated_listener_without_ownership_is_not_replaced(self):
        server = self.server(own=False, identity_delay=0.4)
        self.error("owner_unlocked", lambda: launcher.start(self.fixture.path, timeout=2))
        self.assertFalse(self.children)
        self.assertFalse(server.closed.is_set())

    def test_delayed_child_readiness_still_uses_parent_election(self):
        with patch.dict(os.environ, {"MPTASK_FIXTURE_DELAY": "0.15"}):
            info = launcher.start(self.fixture.path, timeout=3)
        self.assertTrue(info.ready)
        launcher.stop(self.config, expected_instance_id=info.instance_id, timeout=3)

    def test_slow_authenticated_child_identity_is_confirmed_within_start_deadline(self):
        accepted_epoch = "22222222-2222-2222-2222-222222222222"
        with patch.dict(os.environ, {"MPTASK_FIXTURE_IDENTITY_DELAY": "0.4",
                                     "MPTASK_FIXTURE_ACCEPTED_EPOCH": accepted_epoch}):
            info = launcher.start(self.fixture.path, timeout=3)
        self.assertEqual(info.instance_id, accepted_epoch)
        self.assertTrue(info.ready)
        self.assertEqual(len(self.children), 1)
        self.assertIsNone(self.children[0].poll())
        launcher.stop(self.config, expected_instance_id=info.instance_id, timeout=3)
        self.assertEqual(self.children[0].wait(3), 0)

    def test_slow_child_identity_timeout_cleans_up_only_owned_child_within_bound(self):
        began = time.monotonic()
        with patch.dict(os.environ, {"MPTASK_FIXTURE_IDENTITY_DELAY": "2"}):
            self.error("startup_timeout", lambda: launcher.start(self.fixture.path, timeout=1))
        self.assertLess(time.monotonic() - began, 3.5)
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())
        with platform.LifetimeLock(self.config.runtime_dir / "authority.lock"):
            pass
        with platform.LifetimeLock(self.config.runtime_dir / "start.lock"):
            pass

    def test_slow_wrong_child_identity_is_rejected_and_owned_child_reaped(self):
        with patch.dict(os.environ, {"MPTASK_FIXTURE_IDENTITY_DELAY": "0.4",
                                     "MPTASK_FIXTURE_MODE": "wrong-instance"}):
            self.error("identity_mismatch", lambda: launcher.start(self.fixture.path, timeout=3))
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())
        with platform.LifetimeLock(self.config.runtime_dir / "authority.lock"):
            pass

    def test_failed_readiness_stops_and_reaps_only_parent_owned_child(self):
        with patch.dict(os.environ, {"MPTASK_FIXTURE_MODE": "never-ready"}):
            self.error("startup_timeout", lambda: launcher.start(self.fixture.path, timeout=0.8))
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())
        with platform.LifetimeLock(self.config.runtime_dir / "authority.lock"):
            pass
        with platform.LifetimeLock(self.config.runtime_dir / "start.lock"):
            pass

    def test_child_exit_is_an_explicit_failure_not_a_ready_result(self):
        with patch.dict(os.environ, {"MPTASK_FIXTURE_MODE": "exit"}):
            self.error("child_exited", lambda: launcher.start(self.fixture.path, timeout=3))
        self.assertEqual(self.children[0].returncode, 7)

    def test_timeout_before_child_publication_still_cleans_up_the_actual_child(self):
        with patch.dict(os.environ, {"MPTASK_FIXTURE_DELAY": "1"}):
            self.error("startup_timeout", lambda: launcher.start(self.fixture.path, timeout=0.15))
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(self.children[0].poll())
        self.assertFalse((self.config.runtime_dir / "serverinfo.json").exists())

    def test_stale_unreachable_cache_can_be_replaced_but_not_a_new_authority(self):
        server = self.server()
        old = discovery.connect(self.config)
        server.close()
        new = launcher.start(self.fixture.path, timeout=3)
        self.assertEqual(new.authority_id, old.authority_id)
        self.assertNotEqual(new.instance_id, old.instance_id)
        self.assertNotEqual(new.token, old.token)
        launcher.stop(self.config, expected_instance_id=new.instance_id, timeout=3)

    def test_failed_start_never_kills_an_unrelated_registry_pid(self):
        other = ConfigurationFixture()
        self.addCleanup(other.close)
        live = launcher.start(other.path, timeout=3)
        unrelated = self.children[0]
        self.addCleanup(lambda: launcher.stop(
            other.config, expected_instance_id=live.instance_id, timeout=3))
        with patch.dict(os.environ, {"MPTASK_FIXTURE_MODE": "never-ready",
                                     "MPTASK_FIXTURE_DIAGNOSTIC_PID": str(unrelated.pid)}):
            self.error("startup_timeout", lambda: launcher.start(self.fixture.path, timeout=0.8))
        self.assertEqual(len(self.children), 2)
        self.assertIsNotNone(self.children[1].poll())
        self.assertIsNone(unrelated.poll())
        self.assertTrue(discovery.connect(other.config).ready)

    def test_spawn_os_restriction_fails_explicitly_without_attached_fallback(self):
        with patch.object(launcher.subprocess, "Popen", side_effect=PermissionError("denied")):
            self.error("spawn_failed", lambda: launcher.start(self.fixture.path, timeout=1))
        with platform.LifetimeLock(self.config.runtime_dir / "start.lock"):
            pass
        self.assertFalse(self.children)

    def test_failed_child_cleanup_reports_unknown_and_retains_actual_child_handle(self):
        spawn = launcher._spawn

        def denied_cleanup(path, config):
            child = spawn(path, config)
            patcher = patch.object(child, "terminate", side_effect=PermissionError("denied"))
            patcher.start()
            self.addCleanup(patcher.stop)
            return child

        with patch.object(launcher, "_spawn", side_effect=denied_cleanup):
            with patch.dict(os.environ, {"MPTASK_FIXTURE_MODE": "never-ready"}):
                error = self.error("startup_unknown", lambda: launcher.start(
                    self.fixture.path, timeout=0.5))
        self.assertTrue(error.ambiguous)
        child = self.children[0]
        self.assertIsNone(child.poll())
        self.assertIn(child, launcher._children)

    def test_windows_flag_contract_requests_breakaway_only_for_a_job(self):
        # Contract-only simulation, not a claim of native Windows execution.
        flags = {"DETACHED_PROCESS": 8, "CREATE_NEW_PROCESS_GROUP": 512,
                 "CREATE_BREAKAWAY_FROM_JOB": 0x01000000}
        with patch.object(launcher.os, "name", "nt"):
            with patch.multiple(launcher.subprocess, create=True, **flags):
                with patch.object(launcher, "_windows_in_job", return_value=False):
                    self.assertEqual(launcher._launch_options(), {"creationflags": 520})
                with patch.object(launcher, "_windows_in_job", return_value=True):
                    self.assertEqual(launcher._launch_options(),
                                     {"creationflags": 0x01000000 | 520})

    def test_windows_native_job_inspection_failure_is_an_explicit_refusal(self):
        class NativeError(Exception):
            pass

        def failure(*_):
            raise NativeError("native job API failure")

        modules = {
            "win32api": SimpleNamespace(GetCurrentProcess=lambda: -1),
            "win32job": SimpleNamespace(IsProcessInJob=failure),
            "pywintypes": SimpleNamespace(error=NativeError),
        }
        with patch.dict(sys.modules, modules):
            self.error("unsupported_launcher", launcher._windows_in_job)

    def test_corrupt_or_wrong_binding_registry_is_not_silently_replaced(self):
        server = self.server()
        path = self.config.runtime_dir / "serverinfo.json"
        document = json.loads(platform.read_regular(path, private=True))
        document["binding"] = "0" * 64
        platform.atomic_write_cache(path, json.dumps(document).encode())
        self.error("binding_mismatch", lambda: launcher.start(self.fixture.path))
        self.assertFalse(self.children)
        self.assertFalse(server.closed.is_set())

    def test_election_contention_is_bounded_and_never_uses_registry_pid(self):
        platform.ensure_private_directory(self.config.runtime_dir)
        with platform.LifetimeLock(self.config.runtime_dir / "start.lock"):
            self.error("election_timeout", lambda: launcher.start(self.fixture.path, timeout=0.15))
        self.assertFalse(self.children)

    def test_ticket_bypass_requires_live_election_and_exact_binding(self):
        platform.ensure_private_directory(self.config.runtime_dir)
        document = {"schema_version": 1, "authority_id": self.config.authority_id,
                    "binding": discovery.binding_fingerprint(self.config),
                    "nonce": "a" * 64}
        ticket = json.dumps(document).encode() + b"\n"
        with launcher.startup_election(self.config, timeout=0.5):
            with launcher.startup_election(self.config, ticket=ticket) as parsed:
                self.assertEqual(parsed.authority_id, document["authority_id"])
                self.assertEqual(parsed.nonce, document["nonce"])
            bad = json.dumps({**document, "binding": "0" * 64}).encode() + b"\n"
            self.error("invalid_ticket", lambda: launcher.parse_startup_ticket(self.config, bad))
        with self.assertRaises(discovery.DiscoveryError) as caught:
            with launcher.startup_election(self.config, ticket=ticket):
                self.fail("Expired ticket must not bypass the election")
        self.assertEqual(caught.exception.code, "expired_ticket")

    def test_ticket_is_bounded_and_strict_and_carries_no_credentials(self):
        for data in (b"{}", b"{}\n", b" " * 2049, b'{"schema_version":NaN}\n',
                     b'{"schema_version":1,"schema_version":1}\n'):
            with self.subTest(data=data[:30]):
                self.error("invalid_ticket", lambda: launcher.parse_startup_ticket(self.config, data))

    def test_ticket_rejects_preassigned_instance_or_epoch(self):
        document = {"schema_version": 1, "authority_id": self.config.authority_id,
                    "binding": discovery.binding_fingerprint(self.config), "nonce": "a" * 64}
        for field in ("instance_id", "epoch_id", "expected_epoch"):
            ticket = json.dumps({**document, field: str(uuid4())}).encode() + b"\n"
            with self.subTest(field=field):
                self.error("invalid_ticket", lambda: launcher.parse_startup_ticket(self.config, ticket))

    def test_stop_requires_expected_instance_and_sends_only_scoped_credential(self):
        server = self.server()
        self.error("instance_changed", lambda: launcher.stop(
            self.config, expected_instance_id=str(uuid4()), timeout=1))
        self.assertFalse(any(item[0] == "POST" for item in server.requests))
        info = discovery.connect(self.config)
        result = launcher.stop(self.config, expected_instance_id=info.instance_id, timeout=2)
        self.assertEqual(result, {"stopped": True, "authority_id": self.config.authority_id,
                                  "instance_id": info.instance_id})
        post = next(item for item in server.requests if item[0] == "POST")
        self.assertEqual(post[2]["Authorization"], "Bearer " + info.token)
        self.assertRegex(post[3]["nonce"], r"^[0-9a-f]{64}$")
        self.assertNotIn(post[3]["nonce"], [item[1].split("nonce=")[-1]
                                          for item in server.requests if item[0] == "GET"])
        self.assertNotIn(ROOT_TOKEN, repr(server.requests))
        self.assertTrue(server.owner_released.is_set())

    def test_stop_acceptance_without_drain_and_lock_release_is_not_success(self):
        server = self.server(mode="stop-never")
        error = self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=0.25))
        self.assertTrue(error.ambiguous)
        self.assertFalse(server.owner_released.is_set())
        self.assertFalse(self.children)

    def test_acceptance_then_disappearance_without_drain_completion_is_unknown(self):
        server = self.server(mode="stop-accepted-only")
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=1))

    def test_control_completion_without_fresh_server_proof_is_not_trusted(self):
        server = self.server(mode="stop-unproven")
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=1))

    def test_control_completion_cannot_replay_the_prior_identity_proof(self):
        server = self.server(mode="stop-replayed-proof")
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=1))

    def test_stop_waits_for_listener_close_and_ownership_release(self):
        server = self.server()
        server.drain_delay = 0.2
        began = time.monotonic()
        launcher.stop(self.config, expected_instance_id=server.identity.instance_id, timeout=2)
        self.assertGreaterEqual(time.monotonic() - began, 0.2)
        with platform.LifetimeLock(self.config.runtime_dir / "authority.lock"):
            pass

    def test_stop_observes_release_despite_connection_reset_during_listener_shutdown(self):
        server = self.server(mode="stop-reset")
        result = launcher.stop(self.config, expected_instance_id=server.identity.instance_id,
                               timeout=2)
        self.assertTrue(result["stopped"])
        self.assertTrue(server.owner_released.is_set())

    def test_drain_complete_and_closed_listener_with_held_lock_is_not_stopped(self):
        server = self.server()
        server.release_delay = 0.4
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=0.2))
        self.assertFalse(server.owner_released.is_set())

    def test_drain_complete_and_released_lock_with_live_listener_is_not_stopped(self):
        server = self.server(mode="stop-listener-held")
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=0.2))
        self.assertTrue(server.owner_released.is_set())
        self.assertFalse(server.closed.is_set())

    def test_slow_stop_observations_share_deadline_and_require_listener_release(self):
        server = self.server(mode="stop-listener-held", identity_delay=0.4)
        began = time.monotonic()
        error = self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=1.2))
        self.assertLess(time.monotonic() - began, 1.8)
        self.assertTrue(error.ambiguous)
        self.assertTrue(server.owner_released.is_set())
        self.assertFalse(server.closed.is_set())
        self.assertEqual(len([item for item in server.requests if item[0] == "POST"]), 1)
        self.assertFalse(self.children)

    def test_missing_control_route_is_explicit_not_fake_production_success(self):
        server = self.server(mode="stop-unimplemented")
        self.error("stop_unknown", lambda: launcher.stop(
            self.config, expected_instance_id=server.identity.instance_id, timeout=0.5))
        self.assertFalse(server.closed.is_set())
