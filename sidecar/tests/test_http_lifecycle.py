"""Owned journal/SDK HTTP integration; no live palace or registered service."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from authority_fixture import AUTHORITY, command, create, genesis, state_directory, uid
from mempalace_tasks.authority import JournalAuthority
from mempalace_tasks import cli, server
from mempalace_tasks.client import TaskClientError, TaskServiceClient
from mempalace_tasks.config import ConfigError
from mempalace_tasks.discovery import connect, DiscoveryError
from mempalace_tasks.launcher import stop
from mempalace_tasks.maintenance import LeaseMaintenance
from mempalace_tasks.platform_support import LifetimeLock, PlatformError
from mempalace_tasks.server import create_app, PUBLIC_OPERATIONS
from mempalace_tasks.server_identity import InstanceIdentity, verify_proof
from portable_lifecycle_fixture import ConfigurationFixture, ROOT_TOKEN
from service_fixture import SocketRunner, TOKEN
from test_journal_authority import JournalClient


@contextmanager
def journal_service(log=None, directory=None):
    owned_directory = state_directory() if directory is None else None
    directory = owned_directory.name if owned_directory is not None else directory
    log = JournalClient() if log is None else log
    owner = JournalAuthority(AUTHORITY, log, directory, initialize=True,
                             backoff=lambda _: None).start()
    if owner.state.configuration is None:
        owner.execute_current(genesis())
    maintenance = LeaseMaintenance(owner.bound_current(), owner.clock_source,
                                   system_actor="system", recovery_actor="operator")
    runner = SocketRunner()
    thread = threading.Thread(target=runner.run, args=(
        create_app(owner, maintenance, token=TOKEN, port=runner.port),))
    try:
        thread.start()
        if not runner.ready.wait(5) or runner.errors:
            raise AssertionError(runner.errors)
        yield owner, log, f"http://127.0.0.1:{runner.port}/mcp"
    finally:
        runner.stop()
        thread.join(8)
        runner.socket.close()
        if thread.is_alive():
            raise AssertionError("Owned server did not stop")
        owner.close()
        if owned_directory is not None:
            owned_directory.cleanup()


def sdk_tools(url, token=TOKEN):
    async def run():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                     trust_env=False) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
    return asyncio.run(run())


def sdk_call(url, token, name, arguments):
    async def run():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                     trust_env=False) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool(name, arguments)
    return asyncio.run(run())


class JournalHttpTests(unittest.TestCase):
    def test_embedded_journal_app_also_refuses_legacy_projection_checkpoint(self):
        with journal_service() as (owner, _, _):
            maintenance = LeaseMaintenance(owner.bound_current(), owner.clock_source,
                                           system_actor="system", recovery_actor="operator")
            with self.assertRaises(ConfigError) as caught:
                create_app(owner, maintenance, token=TOKEN, port=12345, projector=object())
            self.assertEqual(caught.exception.code, "projection_unsupported")

    def test_discovery_requires_epoch_for_all_public_journal_mutations(self):
        with journal_service() as (_, _, url):
            tools = sdk_tools(url)
            for name in PUBLIC_OPERATIONS:
                with self.subTest(operation=name):
                    self.assertIn("expected_epoch", tools["mptask_" + name]["required"])
                    self.assertEqual(tools["mptask_" + name]["properties"]["expected_epoch"]["type"],
                                     "string")
            self.assertEqual(set(tools["mptask_outcome"]["required"]), {"epoch_id", "command_id"})
            self.assertEqual(tools["mptask_outcome"]["properties"]["epoch_id"]["type"],
                             ["string", "null"])

    def test_missing_stale_epoch_rejected_before_write_and_valid_epoch_is_not_domain_input(self):
        with journal_service() as (owner, log, url):
            client = TaskServiceClient(url, token=TOKEN)
            request = {key: value for key, value in create().items() if key != "operation"}
            before = deepcopy(log.sent)
            for extra, code in (({}, "epoch_required"), ({"expected_epoch": uid(99)}, "stale_epoch")):
                with self.assertRaises(TaskClientError) as caught:
                    client.call_tool("mptask_create", {**request, **extra})
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.ambiguous)
            self.assertEqual(log.sent, before)
            result = client.call_tool("mptask_create", {**request, "expected_epoch": owner.epoch_id})
            self.assertEqual(result["outcome"], "committed")
            self.assertEqual(result["epoch_id"], owner.epoch_id)
            self.assertNotIn("expected_epoch", log.sent[-1]["event"]["command"])

    def test_old_receipt_is_read_only_scoped_evidence_after_restart(self):
        with state_directory() as directory:
            log = JournalClient()
            with journal_service(log, directory) as (owner, _, url):
                old_epoch = owner.epoch_id
                request = {key: value for key, value in create().items() if key != "operation"}
                receipt = TaskServiceClient(url, token=TOKEN).call_tool(
                    "mptask_create", {**request, "expected_epoch": old_epoch})
            with journal_service(log, directory) as (owner, _, url):
                self.assertNotEqual(owner.epoch_id, old_epoch)
                client = TaskServiceClient(url, token=TOKEN)
                before = deepcopy(log.sent)
                with self.assertRaises(TaskClientError) as caught:
                    client.call_tool("mptask_create", {**request, "expected_epoch": old_epoch})
                self.assertEqual(caught.exception.code, "stale_epoch")
                observed = client.call_tool("mptask_outcome", {
                    "epoch_id": old_epoch, "command_id": request["command_id"]})
                self.assertEqual(observed["resolution"], "committed")
                self.assertEqual(observed["receipt"]["task_id"], receipt["task_id"])
                self.assertFalse(observed["authorization"]["authorized"])
                self.assertEqual(log.sent, before)

    def test_journal_abandonment_remains_resolved_and_replayable_not_ambiguous(self):
        with journal_service() as (owner, log, url):
            client = TaskServiceClient(url, token=TOKEN)
            log.behaviors = ["lost", "ok"]
            request = {key: value for key, value in create().items() if key != "operation"}
            request["expected_epoch"] = owner.epoch_id
            result = client.call_tool("mptask_create", request)
            self.assertEqual(result["outcome"], "abandoned")
            self.assertEqual(result["epoch_id"], owner.epoch_id)
            self.assertEqual(client.call_tool("mptask_create", request)["outcome"], "abandoned")

    def test_outcome_transport_failure_never_claims_mutation_ambiguity(self):
        with journal_service() as (owner, _, url), patch.object(
                owner, "outcome", side_effect=RuntimeError("owned failure")):
            client = TaskServiceClient(url, token=TOKEN)
            with self.assertRaises(TaskClientError) as caught:
                client.call_tool("mptask_outcome", {"epoch_id": None, "command_id": uid(10)})
            self.assertFalse(caught.exception.ambiguous)

    def test_stale_claim_does_not_trigger_request_driven_maintenance(self):
        with journal_service() as (owner, _, url):
            task_id = owner.execute(create(), expected_epoch=owner.epoch_id)["task_id"]
            # The independent timer remains asleep; only claim dispatch can invoke this tick.
            with patch.object(server._Runtime, "tick_if_due",
                              side_effect=AssertionError("Stale request ran maintenance")):
                with self.assertRaises(TaskClientError) as caught:
                    TaskServiceClient(url, token=TOKEN).call_tool("mptask_claim", {
                        "actor": "worker", "command_id": uid(3), "expected_epoch": uid(99),
                        "task_id": task_id, "expected_version": 1, "supervisor_id": "supervisor"})
                self.assertEqual(caught.exception.code, "stale_epoch")
                self.assertFalse(caught.exception.ambiguous)

    def test_null_epoch_resolves_imported_legacy_history_without_authorizing_it(self):
        from authority_fixture import raw
        from mempalace_tasks.protocol import LogState, fold_record, make_proposal

        log, state = JournalClient(), LogState(AUTHORITY)
        for request in (genesis(), create()):
            event = raw(make_proposal(state, request, "2026-09-23T00:00:00Z"), len(log.events) + 1)
            log.events.append(event)
            fold_record(state, event)
        with journal_service(log) as (_, _, url):
            reader = TaskServiceClient(url, token=TOKEN)
            result = reader.call_tool("mptask_outcome", {"epoch_id": None, "command_id": uid(2)})
            self.assertEqual(result["resolution"], "committed")
            self.assertIsNone(result["receipt"]["epoch_id"])
            self.assertFalse(result["authorization"]["authorized"])
            log.read_error = True
            stale = reader.call_tool("mptask_outcome", {"epoch_id": None, "command_id": uid(2)})
            self.assertFalse(stale["fresh"])
            self.assertEqual(stale["reason"], "upstream_unavailable")


class PortableServer:
    """Real foreground listener plus actual journal owner, released after ASGI."""

    def __init__(self, *, host="127.0.0.1"):
        self.fixture = ConfigurationFixture(lifecycle="external")
        self.fixture.document["host"] = host
        initial = genesis()
        self.fixture.document["genesis"] = {key: value for key, value in initial.items()
                                            if key not in {"operation", "command_id"}}
        self.fixture.save()
        self.config = self.fixture.config
        self.log = JournalClient()
        with patch.object(cli, "PalaceClient", return_value=self.log):
            cli.initialize(self.config)
        self.ready = threading.Event()
        self.errors = []
        self.owner = None
        self.thread = threading.Thread(target=self.run)

    def run(self):
        try:
            with patch.object(cli, "PalaceClient", return_value=self.log):
                with cli.owned_authority(self.config) as owner:
                    self.owner = owner
                    server.serve(owner, cli._maintenance(owner, self.config),
                                 token=ROOT_TOKEN, host=self.config.host, port=0,
                                 config=self.config, on_ready=self.ready.set)
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(5) or self.errors:
            self.__exit__(None, None, None)
            raise AssertionError(f"Portable foreground failed: {self.errors}")
        self.info = connect(self.config)
        self.identity = InstanceIdentity(self.info.authority_id, self.info.instance_id, self.info.url)
        self.client = TaskServiceClient(self.info.url, token=self.info.token)
        return self

    def __exit__(self, *_):
        if self.thread.is_alive():
            stop(self.config, expected_instance_id=self.owner.epoch_id)
            self.thread.join(8)
        if self.thread.is_alive():
            raise AssertionError("Portable server did not stop")
        self.fixture.close()


class PortableHttpTests(unittest.TestCase):
    def test_ipv6_listener_uses_actual_bracketed_endpoint(self):
        with PortableServer(host="::1") as service:
            self.assertTrue(service.info.url.startswith("http://[::1]:"))
            self.assertTrue(service.client.call_tool("mptask_health", {})["fresh"])

    def test_port_zero_publishes_bound_endpoint_accepted_epoch_and_no_extra_state(self):
        with PortableServer() as service:
            self.assertEqual(service.info.instance_id, service.owner.epoch_id)
            self.assertNotIn(":0/", service.info.url)
            self.assertEqual(service.config.port, 0)
            self.assertTrue(service.info.ready)
            self.assertEqual({p.name for p in service.config.runtime_dir.iterdir()},
                             {"authority.lock", "serverinfo.json"})
            self.assertTrue(service.client.call_tool("mptask_health", {})["fresh"])
            with self.assertRaises(PlatformError):
                LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()

    def test_identity_is_public_bounded_exact_origin_and_live_freshness(self):
        with PortableServer() as service, httpx.Client(trust_env=False) as http:
            base = service.info.url.removesuffix("/mcp")
            nonce = "a" * 64
            response = http.get(base + "/identity?nonce=" + nonce)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(verify_proof(service.identity, ROOT_TOKEN, nonce, response.json()))
            for suffix, extra, expected in (
                ("/identity?nonce=" + nonce, {"Host": "evil.invalid"}, 421),
                ("/identity?nonce=" + nonce, {"Origin": "null"}, 403),
                ("/identity?nonce=bad", {}, 400),
                ("/identity?nonce=" + nonce + "&nonce=" + nonce, {}, 400),
                ("/identity?nonce=" + nonce + "&other=1", {}, 400),
            ):
                self.assertEqual(http.get(base + suffix, headers=extra).status_code, expected)
            service.log.read_error = True
            response = http.get(base + "/identity?nonce=" + nonce)
            self.assertFalse(verify_proof(service.identity, ROOT_TOKEN, nonce, response.json()))
            with self.assertRaises(DiscoveryError) as caught:
                connect(service.config)
            self.assertEqual(caught.exception.code, "not_ready")
            service.log.read_error = False

    def test_control_requires_instance_bearer_exact_body_and_proves_completed_drain(self):
        with PortableServer() as service, httpx.Client(trust_env=False) as http:
            url = service.info.url.removesuffix("/mcp") + "/control/stop"
            request = {"schema_version": 1, "instance_id": service.info.instance_id,
                       "drain": True, "nonce": "b" * 64}
            self.assertEqual(http.post(url, json=request,
                                       headers={"Authorization": "Bearer " + ROOT_TOKEN}).status_code, 401)
            headers = {"Authorization": "Bearer " + service.info.token}
            for changes, status in (({"instance_id": str(uuid4())}, 409),
                                    ({"drain": False}, 400), ({"extra": True}, 400),
                                    ({"schema_version": True}, 400)):
                self.assertEqual(http.post(url, json={**request, **changes},
                                           headers=headers).status_code, status)
            response = http.post(url, content=b"x" * 2049,
                                 headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            response = http.post(url, json=request, headers=headers)
            self.assertEqual(response.status_code, 202)
            body = response.json()
            self.assertEqual(set(body), {"schema_version", "authority_id", "instance_id",
                                         "accepted", "drained", "proof"})
            self.assertIs(body["drained"], True)
            self.assertFalse(verify_proof(service.identity, ROOT_TOKEN, request["nonce"], body["proof"]))
            service.thread.join(8)
            self.assertFalse(service.thread.is_alive())
            self.assertFalse((service.config.runtime_dir / "serverinfo.json").exists())
            with LifetimeLock(service.config.runtime_dir / "authority.lock"):
                pass

    def test_nonempty_held_tasks_do_not_block_stop(self):
        with PortableServer() as service:
            request = {key: value for key, value in create(10).items() if key != "operation"}
            receipt = service.client.call_tool("mptask_create", {
                **request, "hold_reason": "not admitted yet", "expected_epoch": service.info.instance_id})
            self.assertEqual(receipt["tasks"][0]["hold_reason"], "not admitted yet")
            result = stop(service.config, expected_instance_id=service.info.instance_id)
            self.assertTrue(result["stopped"])
            service.thread.join(8)

    def test_stop_waits_for_inflight_mutation_without_releasing_owner_and_blocks_intake(self):
        with PortableServer() as service, ThreadPoolExecutor(2) as pool:
            entered, release = threading.Event(), threading.Event()

            def hold_append(payload):
                if payload.get("event", {}).get("command", {}).get("operation") == "create":
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("Test append was not released")

            service.log.on_append = hold_append
            request = {key: value for key, value in create(20).items() if key != "operation"}
            request["expected_epoch"] = service.info.instance_id
            writing = pool.submit(service.client.call_tool, "mptask_create", request)
            self.assertTrue(entered.wait(2))
            stopping = pool.submit(stop, service.config, expected_instance_id=service.info.instance_id)
            try:
                with httpx.Client(trust_env=False) as http:
                    for _ in range(60):
                        proof = http.get(service.info.url.removesuffix("/mcp")
                                         + "/identity?nonce=" + "c" * 64).json()
                        if proof["ready"] is False:
                            break
                        time.sleep(0.01)
                self.assertFalse(proof["ready"])
                self.assertFalse(stopping.done())
                with self.assertRaises(PlatformError):
                    LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()
                with self.assertRaises(TaskClientError) as caught:
                    service.client.call_tool("mptask_create", {**request, "command_id": uid(21)})
                self.assertEqual(caught.exception.code, "service_draining")
                self.assertFalse(caught.exception.ambiguous)
            finally:
                release.set()
            self.assertEqual(writing.result(timeout=5)["outcome"], "committed")
            self.assertTrue(stopping.result(timeout=8)["stopped"])
            service.thread.join(8)

    def test_disconnected_mutation_keeps_owner_until_background_append_finishes(self):
        with PortableServer() as service, ThreadPoolExecutor(2) as pool:
            entered, release = threading.Event(), threading.Event()

            def hold_append(payload):
                if payload.get("event", {}).get("command", {}).get("operation") == "create":
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("Test append was not released")

            service.log.on_append = hold_append
            request = {key: value for key, value in create(60).items() if key != "operation"}
            request["expected_epoch"] = service.info.instance_id
            short_client = TaskServiceClient(service.info.url, token=service.info.token, timeout=0.2)
            writing = pool.submit(short_client.call_tool, "mptask_create", request)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(TaskClientError) as caught:
                writing.result(timeout=2)
            self.assertEqual(caught.exception.code, "timeout")
            self.assertTrue(caught.exception.ambiguous)
            stopping = pool.submit(stop, service.config, expected_instance_id=service.info.instance_id)
            try:
                time.sleep(0.15)
                self.assertFalse(stopping.done())
                with self.assertRaises(PlatformError):
                    LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()
            finally:
                release.set()
            self.assertTrue(stopping.result(timeout=8)["stopped"])
            service.thread.join(8)

    def test_uncertain_source_blocks_drain_without_releasing_owner(self):
        with PortableServer() as service, httpx.Client(trust_env=False, timeout=8) as http:
            service.log.read_error = True
            try:
                response = http.post(
                    service.info.url.removesuffix("/mcp") + "/control/stop",
                    headers={"Authorization": "Bearer " + service.info.token},
                    json={"schema_version": 1, "instance_id": service.info.instance_id,
                          "drain": True, "nonce": "d" * 64})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "drain_blocked")
                self.assertFalse(response.json()["error"]["details"]["fresh"])
                with self.assertRaises(PlatformError):
                    LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()
            finally:
                service.log.read_error = False

    def test_cleanup_leaves_a_different_instance_registry_untouched(self):
        from mempalace_tasks.discovery import publish_registry

        with PortableServer() as service, httpx.Client(trust_env=False) as http:
            foreign = InstanceIdentity(service.info.authority_id, str(uuid4()), service.info.url)
            publish_registry(service.config, foreign)
            path = service.config.runtime_dir / "serverinfo.json"
            replaced = path.read_bytes()
            response = http.post(
                service.info.url.removesuffix("/mcp") + "/control/stop",
                headers={"Authorization": "Bearer " + service.info.token},
                json={"schema_version": 1, "instance_id": service.info.instance_id,
                      "drain": True, "nonce": "e" * 64})
            self.assertEqual(response.status_code, 202)
            service.thread.join(8)
            self.assertFalse(service.thread.is_alive())
            self.assertEqual(path.read_bytes(), replaced)

    def test_active_attempt_times_out_drain_but_completion_remains_possible(self):
        with PortableServer() as service, httpx.Client(trust_env=False, timeout=8) as http:
            epoch = service.info.instance_id
            def send(value):
                return service.client.call_tool("mptask_" + value["operation"],
                                               {key: item for key, item in
                                                {**value, "expected_epoch": epoch}.items()
                                                if key != "operation"})
            task_id = send(create(30))["task_id"]
            task = send(command(31, "claim", "worker", task_id=task_id,
                                expected_version=1, supervisor_id="supervisor"))["tasks"][0]
            response = http.post(
                service.info.url.removesuffix("/mcp") + "/control/stop",
                headers={"Authorization": "Bearer " + service.info.token},
                json={"schema_version": 1, "instance_id": epoch, "drain": True, "nonce": "d" * 64})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "drain_blocked")
            self.assertEqual(response.json()["error"]["details"]["active_attempt_count"], 1)
            with self.assertRaises(PlatformError):
                LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()
            send(command(32, "renew", "supervisor", task_id=task_id,
                         expected_lease_revision=task["lease_revision"], attempt_id=task["attempt"]["id"],
                         claim_generation=task["claim_generation"]))
            released = send(command(33, "release", "worker", task_id=task_id,
                                    expected_version=task["version"], attempt_id=task["attempt"]["id"],
                                    claim_generation=task["claim_generation"], reason="Controlled drain"))
            self.assertEqual(released["outcome"], "committed")
            self.assertTrue(stop(service.config, expected_instance_id=epoch)["stopped"])
            service.thread.join(8)

    def test_root_and_instance_credentials_redact_in_typed_errors(self):
        from mempalace_tasks.authority import AuthorityError

        with PortableServer() as service:
            error = AuthorityError("owned_failure", "Diagnostic " + service.info.token,
                                   {"detail": ROOT_TOKEN + service.info.token})
            with patch.object(service.owner, "get", side_effect=error):
                result = sdk_call(service.info.url, service.info.token, "mptask_get", {"task_id": uid(40)})
                self.assertNotIn(ROOT_TOKEN, str(result))
                self.assertNotIn(service.info.token, str(result))
                with self.assertRaises(TaskClientError) as caught:
                    service.client.call_tool("mptask_get", {"task_id": uid(40)})
            self.assertEqual(caught.exception.code, "owned_failure")
            self.assertNotIn(ROOT_TOKEN, caught.exception.message + str(caught.exception.details))
            self.assertNotIn(service.info.token, caught.exception.message + str(caught.exception.details))

    def test_direct_static_token_still_requires_explicit_epoch_and_discovered_token_is_scoped(self):
        with PortableServer() as service:
            request = {key: value for key, value in create(50).items() if key != "operation"}
            static = TaskServiceClient(service.info.url, token=ROOT_TOKEN)
            with self.assertRaises(TaskClientError) as caught:
                static.call_tool("mptask_create", request)
            self.assertEqual(caught.exception.code, "epoch_required")
            receipt = static.call_tool("mptask_create", {
                **request, "expected_epoch": service.info.instance_id})
            self.assertEqual(receipt["outcome"], "committed")
            from mempalace_tasks.server_identity import instance_bearer
            obsolete = InstanceIdentity(service.info.authority_id, str(uuid4()), service.info.url)
            with self.assertRaises(TaskClientError) as caught:
                TaskServiceClient(service.info.url, token=instance_bearer(obsolete, ROOT_TOKEN)).call_tool(
                    "mptask_health", {})
            self.assertEqual(caught.exception.details["status_code"], 401)

    def test_registry_publication_failure_cleans_listener_workers_before_owner_release(self):
        from mempalace_tasks import lifecycle

        service = PortableServer()
        self.addCleanup(service.fixture.close)
        endpoints = []

        def fail_publication(config, identity):
            endpoints.append(identity.endpoint)
            raise DiscoveryError("registry_failed", "Controlled private-cache failure")

        with patch.object(lifecycle, "publish_registry", fail_publication):
            service.thread.start()
            service.thread.join(8)
        self.assertFalse(service.thread.is_alive())
        self.assertEqual(len(service.errors), 1)
        self.assertEqual(service.errors[0].code, "registry_failed")
        self.assertFalse((service.config.runtime_dir / "serverinfo.json").exists())
        with httpx.Client(trust_env=False, timeout=1) as http:
            with self.assertRaises(httpx.ConnectError):
                http.get(endpoints[0].removesuffix("/mcp") + "/identity?nonce=" + "e" * 64)
        with LifetimeLock(service.config.runtime_dir / "authority.lock"):
            pass

    def test_unexpected_identity_or_drain_failure_is_explicit_without_secret_logs(self):
        with PortableServer() as service, httpx.Client(trust_env=False) as http:
            base = service.info.url.removesuffix("/mcp")
            with patch.object(service.owner, "refresh", side_effect=RuntimeError(ROOT_TOKEN)):
                response = http.get(base + "/identity?nonce=" + "f" * 64)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "identity_failed")
            self.assertNotIn(ROOT_TOKEN, response.text)
            with patch.object(server._Runtime, "drain", side_effect=RuntimeError(ROOT_TOKEN)):
                response = http.post(base + "/control/stop",
                                     headers={"Authorization": "Bearer " + service.info.token},
                                     json={"schema_version": 1, "instance_id": service.info.instance_id,
                                           "drain": True, "nonce": "a" * 64})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "drain_failed")
            self.assertNotIn(ROOT_TOKEN, response.text)
            with self.assertRaises(PlatformError):
                LifetimeLock(service.config.runtime_dir / "authority.lock").acquire()
