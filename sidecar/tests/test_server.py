"""Public MCP behavior over real SDK HTTP, domain, protocol and durable journals."""

from concurrent.futures import ThreadPoolExecutor
import json
import io
import logging
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import PropertyMock, patch

import httpx

from authority_fixture import command, create, uid
from mempalace_tasks.authority import AuthorityError
from mempalace_tasks.client import TaskClientError, TaskServiceClient
from mempalace_tasks.domain import SCHEMAS
from mempalace_tasks.journal import JsonStore
from mempalace_tasks.model import state_to_dict
from mempalace_tasks.projection import ProjectionError, TaskProjector
from service_fixture import ServiceFixture, TOKEN
from test_projection import ADD, OwnedTransport


class ServerTests(unittest.TestCase):
    def test_fenced_resource_identifiers_round_trip_into_valid_supervisor_preparation(self):
        profiles = {
            "local": {"execution_class": "isolated"},
            "fenced": {"execution_class": "resource_fenced", "supports_fencing": True,
                       "supports_reconciliation": True},
        }
        with ServiceFixture(policy={"sweep_seconds": 86400}, genesis_fields={
            "execution_profiles": profiles,
            "supervisors": {"supervisor": {"profiles": ["local", "fenced"],
                                           "workers": ["worker", "worker2"]}},
        }) as service:
            client = TaskServiceClient(service.url, token=TOKEN)
            resource_keys = ["authorization", "credential-key", "password", "secrets/db", "token"]
            request = {key: value for key, value in create().items() if key != "operation"}
            request.update(execution_class="resource_fenced", execution_profile="fenced",
                           resource_keys=resource_keys)
            task_id = client.call_tool("mptask_create", request)["task_id"]
            claimed = client.call_tool("mptask_claim", {
                "command_id": uid(3), "actor": "worker", "task_id": task_id,
                "expected_version": 1, "supervisor_id": "supervisor"})
            task = claimed["tasks"][0]
            self.assertEqual(task["attempt"]["resource_fences"], dict.fromkeys(resource_keys, 1))
            observed = service.sdk("mptask_get", {"task_id": task_id}).structuredContent
            self.assertEqual(observed["task"]["attempt"]["resource_fences"],
                             dict.fromkeys(resource_keys, 1))

            started = client.call_tool("mptask_attempt_report", {
                "command_id": uid(4), "actor": "supervisor", "task_id": task_id,
                "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"], "report_kind": "started",
                "evidence": {"prepared": True, "references": ["artifact:owned-installed-fences"],
                             "resource_fences": observed["task"]["attempt"]["resource_fences"]},
            })
            self.assertTrue(started["authorization"]["tasks"][0]["authorized"])
            task = started["tasks"][0]
            released = client.call_tool("mptask_release", {
                "command_id": uid(5), "actor": "worker", "task_id": task_id,
                "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"], "reason": "Owned fixture release",
            })
            client.call_tool("mptask_recover", {
                "command_id": uid(6), "actor": "operator", "task_id": task_id,
                "expected_version": released["tasks"][0]["version"], "retry_decision": "preserve",
                "evidence": {"effects_reconciled": True,
                             "references": ["artifact:owned-recovery-fences"],
                             "installed_fences": dict.fromkeys(resource_keys, 2)},
            })
            rows = client.call_tool("mptask_history", {"task_id": task_id})["rows"]
            events = {row["payload"]["event"]["command"]["operation"]: row["payload"]["event"]
                      for row in rows if row["record_type"] == "mptask.command"}
            self.assertEqual(events["recover"]["command"]["evidence"]["installed_fences"],
                             dict.fromkeys(resource_keys, 2))
            self.assertEqual(events["claim"]["resources"]["secrets/db"]["counter"], 1)
            self.assertEqual(events["recover"]["resources"]["secrets/db"]["counter"], 2)

    def test_actual_credentials_still_redact_even_when_disguised_as_untyped_maps(self):
        with ServiceFixture() as service:
            failure = AuthorityError("owned_failure", f"Failed with {TOKEN}", {
                "password": "owned-sensitive-password",
                "authorization": {"value": "owned-sensitive-header"},
                "resource_fences": {"password": "owned-invalid-counter-secret"},
                "resources": {"credentials": "owned-invalid-resource-secret"},
            })
            with patch.object(service.authority, "get", side_effect=failure):
                result = service.sdk("mptask_get", {"task_id": "owned-task"})
            self.assertTrue(result.isError)
            encoded = json.dumps(result.structuredContent)
            self.assertNotIn(TOKEN, encoded)
            self.assertNotIn("owned-sensitive", encoded)
            self.assertNotIn("owned-invalid", encoded)

    def test_typed_actor_profile_and_supervisor_identifiers_are_not_credentials(self):
        from mempalace_tasks.server import _safe

        with ServiceFixture(genesis_fields={
            "actors": {"operator": "operator", "system": "system",
                       "token-worker": "worker", "password-supervisor": "supervisor"},
            "execution_profiles": {"secrets/profile": {"execution_class": "isolated"}},
            "supervisors": {"password-supervisor": {"profiles": ["secrets/profile"],
                                                    "workers": ["token-worker"]}},
        }) as service:
            request = create()
            request["execution_profile"] = "secrets/profile"
            service.authority.execute(request)
            state = state_to_dict(service.authority.state)
            self.assertEqual(_safe(state, TOKEN), state)

    def test_create_accepts_empty_text_and_domain_utf8_byte_limits_remain_enforced(self):
        with ServiceFixture() as service:
            client = TaskServiceClient(service.url, token=TOKEN)
            request = {key: value for key, value in create().items() if key != "operation"}
            request.update(description="", acceptance="")
            result = client.call_tool("mptask_create", request)
            self.assertEqual(result["tasks"][0]["description"], "")
            self.assertEqual(result["tasks"][0]["acceptance"], "")
            writes = len(service.log.sent)
            for number, update in (
                (3, {"description": "é" * 8193}),
                (4, {"acceptance": "é" * 4097}),
                (5, {"title": ""}),
            ):
                with self.subTest(update_field=next(iter(update))):
                    with self.assertRaises(TaskClientError) as caught:
                        client.call_tool("mptask_create", {**request, "command_id": uid(number), **update})
                    self.assertEqual(caught.exception.code, "validation_error")
                    self.assertFalse(caught.exception.ambiguous)
            self.assertEqual(len(service.log.sent), writes)

    def test_update_can_clear_description_and_acceptance_over_sdk(self):
        with ServiceFixture() as service:
            task_id = service.authority.execute(create())["task_id"]
            result = service.sdk("mptask_update", {
                "actor": "operator", "command_id": uid(3), "task_id": task_id,
                "expected_version": 1, "patch": {"description": "", "acceptance": ""},
            })
            self.assertFalse(result.isError, result.structuredContent)
            task = TaskServiceClient(service.url, token=TOKEN).call_tool(
                "mptask_get", {"task_id": task_id})["task"]
            self.assertEqual((task["description"], task["acceptance"], task["version"]), ("", "", 2))

    def test_bootstrap_allows_empty_goal_and_planning_text_over_sdk(self):
        with ServiceFixture() as service:
            result = service.sdk("mptask_bootstrap", {
                "actor": "operator", "command_id": uid(2), "project": "demo",
                "title": "Owned goal", "description": "", "acceptance": "",
                "planning_task": {
                    "title": "Owned plan", "description": "", "acceptance": "",
                    "execution_class": "isolated", "execution_profile": "local",
                },
                "goal_policy": {"scope": "Owned fixture"},
            })
            self.assertFalse(result.isError, result.structuredContent)
            self.assertEqual(len(result.structuredContent["tasks"]), 2)
            for task in result.structuredContent["tasks"]:
                self.assertEqual((task["description"], task["acceptance"]), ("", ""))

    def test_expansion_allows_empty_nested_task_text_over_sdk(self):
        with ServiceFixture() as service:
            goal_id = service.authority.execute(command(
                2, "bootstrap", project="demo", title="Owned goal", description="", acceptance="",
                planning_task={"title": "Owned plan", "description": "", "acceptance": "",
                               "execution_class": "isolated", "execution_profile": "local"},
                goal_policy={"scope": "Owned fixture"}))["goal_id"]
            result = service.sdk("mptask_expand", {
                "actor": "operator", "command_id": uid(3), "goal_id": goal_id,
                "expected_graph_revision": 1, "source_disposition": "continue",
                "tasks": [{"intent_key": "empty-text-child", "title": "Owned child",
                           "description": "", "acceptance": "", "execution_class": "isolated",
                           "execution_profile": "local"}],
            })
            self.assertFalse(result.isError, result.structuredContent)
            task_id = result.structuredContent["admitted_task_ids"][0]
            task = TaskServiceClient(service.url, token=TOKEN).call_tool(
                "mptask_get", {"task_id": task_id})["task"]
            self.assertEqual((task["description"], task["acceptance"]), ("", ""))

    def test_tools_have_per_operation_domain_fields_and_no_internal_writers(self):
        with ServiceFixture() as service:
            tools = {tool.name: tool for tool in service.sdk().tools}
            self.assertNotIn("mptask_authority_create", tools)
            self.assertNotIn("mptask_expire", tools)
            for operation, (allowed, required) in SCHEMAS.items():
                if operation in {"authority_create", "expire"}:
                    continue
                schema = tools[f"mptask_{operation}"].inputSchema
                self.assertEqual(set(schema["properties"]), allowed | {"actor", "command_id"})
                self.assertEqual(set(schema["required"]), required | {"actor", "command_id"})
                self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(tools["mptask_transition"].inputSchema["properties"]["evidence"]["type"],
                             "array")
            self.assertEqual(tools["mptask_attempt_report"].inputSchema["properties"]["evidence"]["type"],
                             "object")
            self.assertNotIn("expected_version", tools["mptask_renew"].inputSchema["properties"])

    def test_two_real_clients_share_one_claim_authority_and_typed_rejection(self):
        with ServiceFixture() as service:
            task_id = service.authority.execute(create())["task_id"]
            clients = [TaskServiceClient(service.url, token=TOKEN) for _ in range(2)]
            def claim(index):
                request = command(10 + index, "claim", actor=f"worker{index or ''}",
                                  task_id=task_id, expected_version=1, supervisor_id="supervisor")
                if index == 1:
                    request["actor"] = "worker2"
                try:
                    return clients[index].call_tool("mptask_claim",
                                                   {k: v for k, v in request.items() if k != "operation"})
                except TaskClientError as error:
                    return error
            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(claim, range(2)))
            receipts = [result for result in outcomes if isinstance(result, dict)]
            errors = [result for result in outcomes if isinstance(result, TaskClientError)]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].code, "version_conflict")
            self.assertFalse(errors[0].ambiguous)
            self.assertFalse(receipts[0]["authorization"]["tasks"][0]["authorized"])
            observed = clients[0].call_tool("mptask_get", {"task_id": task_id})
            self.assertEqual(observed["task"]["claim_generation"], 1)

    def test_structured_validation_and_business_errors_do_not_reflect_input(self):
        with ServiceFixture() as service:
            for name, arguments in (
                ("mptask_create", {"actor": "operator", "command_id": uid(5), "password": TOKEN}),
                ("mptask_expire", {"actor": "system", "command_id": uid(5)}),
                ("mptask_get", {"task_id": "unknown"}),
            ):
                result = service.sdk(name, arguments)
                self.assertTrue(result.isError)
                payload = result.structuredContent
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(set(payload["error"]), {"code", "message", "details", "ambiguous"})
                self.assertIs(payload["error"]["ambiguous"], False)
                self.assertNotIn(TOKEN, json.dumps(payload))

    def test_abandoned_is_a_resolved_receipt_but_unknown_remains_ambiguous(self):
        with ServiceFixture() as service:
            client = TaskServiceClient(service.url, token=TOKEN)
            service.log.behaviors = ["lost", "ok"]
            args = {k: v for k, v in create(20).items() if k != "operation"}
            result = client.call_tool("mptask_create", args)
            self.assertFalse(result["ok"])
            self.assertEqual(result["outcome"], "abandoned")
            self.assertIsNone(result["ordinal"])
            self.assertEqual(client.call_tool("mptask_create", args)["outcome"], "abandoned")
            service.log.behaviors = ["lost"] * 4
            args["command_id"] = uid(21)
            with self.assertRaises(TaskClientError) as caught:
                client.call_tool("mptask_create", args)
            self.assertTrue(caught.exception.ambiguous)
            self.assertEqual(caught.exception.code, "outcome_unknown")

    def test_auth_exact_route_host_origin_and_request_size(self):
        with ServiceFixture() as service, httpx.Client(trust_env=False) as http:
            for suffix in ("/mcp", "/mcp/", "/unknown"):
                response = http.post(service.url.removesuffix("/mcp") + suffix, json={})
                self.assertEqual(response.status_code, 401)
                self.assertNotIn(TOKEN, response.text)
            headers = {"Authorization": f"Bearer {TOKEN}"}
            self.assertEqual(http.post(service.url + "/", json={}, headers=headers).status_code, 404)
            for extra, status in (
                ({"Host": "127.0.0.1:1"}, 421), ({"Host": "evil.invalid"}, 421),
                ({"Origin": "http://127.0.0.1:1"}, 403), ({"Origin": "null"}, 403),
            ):
                response = http.post(service.url, json={}, headers={**headers, **extra})
                self.assertEqual(response.status_code, status)
            response = http.post(service.url, content=b"x" * (256 * 1024 + 1),
                                 headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            chunks = iter([b"x" * (128 * 1024), b"x" * (128 * 1024 + 1)])
            response = http.post(service.url, content=chunks,
                                 headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)

    def test_diagnostic_reads_do_not_sweep_or_write_task_recovery_metadata(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            task_id = service.authority.execute(create())["task_id"]
            files = {path.name: path.read_bytes() for path in Path(service.directory.name).iterdir()
                     if path.is_file()}
            writes = len(service.log.sent)
            ticks = service.maintenance.health()["ticks"]
            client = TaskServiceClient(service.url, token=TOKEN)
            for name, args in (("snapshot", {}), ("list", {}), ("get", {"task_id": task_id}),
                               ("history", {"task_id": task_id}), ("health", {})):
                self.assertTrue(client.call_tool(f"mptask_{name}", args)["fresh"])
            self.assertEqual(len(service.log.sent), writes)
            self.assertEqual(service.maintenance.health()["ticks"], ticks)
            self.assertEqual(files, {path.name: path.read_bytes()
                                   for path in Path(service.directory.name).iterdir() if path.is_file()})

    def test_expiry_runs_without_requests_and_releases_isolated_task(self):
        expired = threading.Event()
        with ServiceFixture() as service:
            task_id = service.authority.execute(create())["task_id"]
            service.authority.execute(command(3, "claim", actor="worker", task_id=task_id,
                                              expected_version=1, supervisor_id="supervisor"))
            service.log.on_append = lambda payload: (
                expired.set() if payload.get("event", {}).get("command", {}).get("operation") == "recover"
                else None)
            service.clock.value = "2026-09-23T00:05:00Z"
            self.assertTrue(expired.wait(4), "No independent expiry/recovery tick")
            self.assertEqual(service.authority.get(task_id)["task"]["status"], "open")

    def test_blocking_authority_read_does_not_block_sdk_discovery_or_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        with ServiceFixture() as service:
            original = service.authority.snapshot
            def blocked(*args, **kwargs):
                entered.set()
                if not release.wait(4):
                    raise AssertionError("Test read not released")
                return original(*args, **kwargs)
            with patch.object(service.authority, "snapshot", blocked), ThreadPoolExecutor(1) as pool:
                future = pool.submit(service.sdk, "mptask_snapshot")
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertGreater(len(service.sdk().tools), 10)
                finally:
                    release.set()
                self.assertFalse(future.result(4).isError)
        self.assertFalse(service.thread.is_alive())

    def test_wait_ready_is_bounded_and_time_changes_eligibility_without_new_head(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            task_id = service.authority.execute(create(deferred_until="2026-09-23T00:01:00Z"))["task_id"]
            client = TaskServiceClient(service.url, token=TOKEN)
            first = client.call_tool("mptask_ready", {})
            self.assertEqual(first["tasks"], [])
            service.clock.value = "2026-09-23T00:01:00Z"
            result = client.call_tool("mptask_wait_ready", {
                "after_ready_version": first["ready_version"], "timeout_seconds": 1})
            self.assertEqual(result["tasks"][0]["id"], task_id)
            self.assertNotEqual(result["ready_version"], first["ready_version"])
            self.assertEqual(result["domain_head"], first["domain_head"])
            started = time.monotonic()
            result = client.call_tool("mptask_wait_ready", {
                "after_ready_version": result["ready_version"], "timeout_seconds": 0.05})
            self.assertTrue(result["timed_out"])
            self.assertLess(time.monotonic() - started, 2)
            with self.assertRaises(TaskClientError) as caught:
                client.call_tool("mptask_wait_ready", {"timeout_seconds": 31})
            self.assertFalse(caught.exception.ambiguous)

    def test_projection_uses_only_accepted_domain_ordinals_and_resumes(self):
        from mempalace_tasks.server import project_batch

        transport = OwnedTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory)
        # Offline delivery exercises the same production batch used by the worker.
        try:
            service.log.behaviors = ["lost", "ok"]
            self.assertEqual(service.authority.execute(create(2))["outcome"], "abandoned")
            committed = service.authority.execute(create(3))
            result = project_batch(service.authority, service.projector)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["checkpoint"]["ordinal"], 2)
            self.assertEqual(result["checkpoint"]["event_id"], committed["event_id"])
            self.assertEqual(transport.count(ADD), 1)
            self.assertEqual(project_batch(service.authority, service.projector)["processed"], 0)
            self.assertEqual(transport.count(ADD), 1)
            service.projector.reset()
            self.assertEqual(project_batch(service.authority, service.projector)["processed"], 2)
        finally:
            service.__exit__(None, None, None)

    def test_projection_batch_uses_bounded_feed_without_reading_full_log(self):
        from mempalace_tasks.server import project_batch

        transport = OwnedTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory)
        try:
            for number in range(2, 8):
                service.authority.execute(create(number))
            with patch.object(type(service.authority), "log", new_callable=PropertyMock,
                              side_effect=AssertionError("Projection copied full authority log")):
                result = project_batch(service.authority, service.projector, limit=2)
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["checkpoint"]["ordinal"], 2)
            self.assertEqual(transport.count(ADD), 1)
        finally:
            service.__exit__(None, None, None)

    def test_projection_timer_caught_up_ticks_copy_no_accepted_events(self):
        transport = OwnedTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory, policy={"sweep_seconds": 86400})
        self.addCleanup(service.__exit__, None, None, None)
        service.authority.execute(create())
        calls, caught_up = [], threading.Event()
        read_feed = service.authority.accepted_records

        def observed(after_ordinal=0, limit=100):
            rows = read_feed(after_ordinal=after_ordinal, limit=limit)
            calls.append((after_ordinal, limit, len(rows)))
            if len(calls) >= 3:
                caught_up.set()
            return rows

        with patch.object(service.authority, "accepted_records", observed), patch.object(
            type(service.authority), "log", new_callable=PropertyMock,
            side_effect=AssertionError("Timer copied full authority log"),
        ), service:
            self.assertTrue(caught_up.wait(4))
            self.assertEqual(calls[:3], [(0, 10, 2), (2, 10, 0), (2, 10, 0)])
            self.assertEqual(transport.count(ADD), 1)
            health = TaskServiceClient(service.url, token=TOKEN).call_tool("mptask_health", {})
            self.assertFalse(health["projection"]["paused"])
            self.assertEqual(health["projection"]["checkpoint"]["ordinal"], 2)

    def test_projection_checkpoint_validation_uses_one_prior_record_and_rejects_mismatch(self):
        from mempalace_tasks.server import project_batch

        transport = OwnedTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory)
        try:
            service.authority.execute(create())
            project_batch(service.authority, service.projector)
            store = JsonStore(Path(service.directory.name) / "service-projection.json")
            checkpoint = store.read()
            remote_calls = len(transport.calls)
            read_feed = service.authority.accepted_records
            calls = []

            def observed(after_ordinal=0, limit=100):
                rows = read_feed(after_ordinal=after_ordinal, limit=limit)
                calls.append((after_ordinal, limit, len(rows)))
                return rows

            with patch.object(service.authority, "accepted_records", observed), patch.object(
                type(service.authority), "log", new_callable=PropertyMock,
                side_effect=AssertionError("Validation copied full authority log"),
            ):
                for update, expected_calls in (
                    ({"event_hash": "a" * 64}, [(1, 1, 1)]),
                    ({"ordinal": 100}, [(99, 1, 0)]),
                ):
                    with self.subTest(update=update):
                        store.write({**checkpoint, **update})
                        projector = TaskProjector(transport, store, {"demo": "owned-test-wing"})
                        calls.clear()
                        with self.assertRaises(ProjectionError) as caught:
                            project_batch(service.authority, projector)
                        self.assertEqual(caught.exception.code, "projection_history_mismatch")
                        self.assertEqual(calls, expected_calls)
            self.assertEqual(len(transport.calls), remote_calls)
        finally:
            service.__exit__(None, None, None)

    def test_slow_projection_does_not_hold_authority_lock_or_starve_expiry(self):
        from mempalace_tasks.server import project_batch

        entered, release, recovered = threading.Event(), threading.Event(), threading.Event()
        class SlowTransport(OwnedTransport):
            blocked = False

            def call_tool(self, name, arguments):
                if name == ADD and self.blocked:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("Projection was not released")
                return super().call_tool(name, arguments)
        transport = SlowTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory)
        task_id = service.authority.execute(create())["task_id"]
        service.authority.execute(command(3, "claim", actor="worker", task_id=task_id,
                                          expected_version=1, supervisor_id="supervisor"))
        project_batch(service.authority, service.projector)
        service.authority.execute(create(4))
        transport.blocked = True
        with service:
            try:
                self.assertTrue(entered.wait(2))
                service.clock.value = "2026-09-23T00:05:00Z"
                service.log.on_append = lambda payload: (
                    recovered.set()
                    if payload.get("event", {}).get("command", {}).get("operation") == "recover"
                    else None)
                client = TaskServiceClient(service.url, token=TOKEN)
                health = client.call_tool("mptask_health", {})
                self.assertTrue(health["projection"]["enabled"])
                self.assertEqual(health["projection"]["checkpoint"]["ordinal"], 3)
                self.assertTrue(recovered.wait(3))
                self.assertEqual(client.call_tool("mptask_get", {"task_id": task_id})["task"]["status"],
                                 "open")
            finally:
                release.set()

    def test_failed_maintenance_is_visible_and_independent_next_tick_retries(self):
        with ServiceFixture() as service:
            failed = threading.Event()
            original = service.maintenance.tick
            def broken():
                failed.set()
                raise RuntimeError(TOKEN)
            with patch.object(service.maintenance, "tick", broken):
                self.assertTrue(failed.wait(3))
                health = TaskServiceClient(service.url, token=TOKEN).call_tool("mptask_health", {})
                self.assertFalse(health["maintenance"]["ok"])
                self.assertNotIn(TOKEN, json.dumps(health))
            retried = threading.Event()
            def observed():
                result = original()
                retried.set()
                return result
            with patch.object(service.maintenance, "tick", observed):
                self.assertTrue(retried.wait(3))
                self.assertTrue(service.maintenance.health()["ok"])

    def test_sdk_logs_do_not_reflect_credentials_or_full_untrusted_requests(self):
        with ServiceFixture() as service:
            stream = io.StringIO()
            logger = logging.getLogger("mcp.server.lowlevel.server")
            handler = logging.StreamHandler(stream)
            previous = logger.level
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)
            try:
                result = service.sdk(f"mptask_{TOKEN}", {"password": TOKEN})
                self.assertTrue(result.isError)
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous)
                handler.close()
            self.assertNotIn(TOKEN, stream.getvalue())
            self.assertNotIn("password", stream.getvalue())

    def test_nonfinite_wait_deadline_rejects_before_work_and_typed_errors_stay_safe(self):
        with ServiceFixture() as service:
            # Exercise the SDK's JSON parsing, which accepts nonfinite JSON extensions.
            with httpx.Client(trust_env=False) as http:
                response = http.post(service.url, headers={
                    "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }, content=b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
                           b'{"name":"mptask_wait_ready","arguments":{"timeout_seconds":NaN}}}')
            result = response.json()["result"]
            self.assertTrue(result["isError"])
            self.assertEqual(result["structuredContent"]["error"]["code"], "validation_error")

    def test_failed_projection_pauses_with_last_confirmed_checkpoint_visible(self):
        transport = OwnedTransport()
        factory = lambda service: TaskProjector(
            transport, JsonStore(Path(service.directory.name) / "service-projection.json"),
            {"demo": "owned-test-wing"})
        service = ServiceFixture(projector=factory)
        service.authority.execute(create())
        other = create(3)
        other["project"] = "unmapped"
        service.authority.execute(other)
        with service:
            client = TaskServiceClient(service.url, token=TOKEN)
            for _ in range(30):
                health = client.call_tool("mptask_health", {})
                if health["projection"]["paused"]:
                    break
                time.sleep(0.05)
            self.assertTrue(health["projection"]["paused"])
            self.assertEqual(health["projection"]["checkpoint"]["ordinal"], 2)
            self.assertIsNotNone(health["projection"]["last_error"])
            calls = len(transport.calls)
            time.sleep(1.1)
            self.assertEqual(len(transport.calls), calls, "Paused lane must not silently retry indefinitely")

    def test_noncurrent_health_remains_data_and_diagnostics_do_not_reconcile(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            task_id = service.authority.execute(create())["task_id"]
            service.log.read_error = True
            client = TaskServiceClient(service.url, token=TOKEN)
            observed = client.call_tool("mptask_get", {"task_id": task_id})
            self.assertFalse(observed["fresh"])
            before = len(service.log.sent)
            health = client.call_tool("mptask_health", {})
            self.assertFalse(health["fresh"])
            self.assertEqual(health["error"]["code"], "upstream_unavailable")
            self.assertEqual(len(service.log.sent), before)

    def test_get_preserves_singular_attempt_authorization_across_sdk_and_client(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            task_id = service.authority.execute(create())["task_id"]
            claimed = service.authority.execute(command(
                3, "claim", actor="worker", task_id=task_id,
                expected_version=1, supervisor_id="supervisor"))
            client = TaskServiceClient(service.url, token=TOKEN)
            prepared = client.call_tool("mptask_get", {"task_id": task_id})
            expected_fields = {
                "as_of", "fresh", "task_id", "attempt_id", "claim_generation",
                "matches_current", "lease_live", "authorized", "current_lease_revision",
                "lease_expires_at", "reason",
            }
            self.assertEqual(set(prepared["authorization"]), expected_fields)
            self.assertFalse(prepared["authorization"]["authorized"])
            self.assertTrue(prepared["authorization"]["lease_live"])

            task = claimed["tasks"][0]
            service.authority.execute(command(
                4, "attempt_report", actor="supervisor", task_id=task_id,
                expected_version=task["version"], attempt_id=task["attempt"]["id"],
                claim_generation=task["claim_generation"], report_kind="started",
                evidence={"prepared": True, "references": ["artifact:owned-preparation"]}))
            writes = len(service.log.sent)
            wire = service.sdk("mptask_get", {"task_id": task_id})
            self.assertFalse(wire.isError)
            live = client.call_tool("mptask_get", {"task_id": task_id})
            self.assertEqual(wire.structuredContent["authorization"], live["authorization"])
            self.assertEqual(json.loads(wire.content[0].text)["authorization"], live["authorization"])
            self.assertTrue(live["authorization"]["authorized"])
            self.assertTrue(live["authorization"]["matches_current"])
            self.assertEqual(live["authorization"]["task_id"], task_id)
            self.assertEqual(live["authorization"]["attempt_id"], live["task"]["attempt"]["id"])
            self.assertEqual(live["authorization"]["claim_generation"], live["task"]["claim_generation"])

            service.clock.value = "2026-09-23T00:05:00Z"
            expired = client.call_tool("mptask_get", {"task_id": task_id})
            self.assertFalse(expired["authorization"]["authorized"])
            self.assertFalse(expired["authorization"]["lease_live"])
            self.assertEqual(expired["task"]["status"], "in_progress")
            service.log.read_error = True
            stale = client.call_tool("mptask_get", {"task_id": task_id})
            self.assertFalse(stale["fresh"])
            self.assertFalse(stale["authorization"]["authorized"])
            for result in (prepared, live, expired, stale):
                self.assertEqual(set(result["authorization"]), expected_fields)
                self.assertEqual(result["authorization"]["as_of"], result["as_of"])
                self.assertEqual(result["authorization"]["fresh"], result["fresh"])
            self.assertEqual(len(service.log.sent), writes)

    def test_shutdown_drains_an_admitted_mutation_before_authority_is_released(self):
        entered, release = threading.Event(), threading.Event()
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            def blocked_append(_):
                entered.set()
                if not release.wait(4):
                    raise AssertionError("Test mutation was not released")
            service.log.on_append = blocked_append
            with ThreadPoolExecutor(1) as pool:
                client = TaskServiceClient(service.url, token=TOKEN)
                future = pool.submit(client.call_tool, "mptask_create",
                                     {key: value for key, value in create(10).items() if key != "operation"})
                try:
                    self.assertTrue(entered.wait(2))
                    service.server.should_exit = True
                    service.thread.join(0.1)
                    self.assertTrue(service.thread.is_alive())
                finally:
                    release.set()
                self.assertEqual(future.result(4)["outcome"], "committed")
        self.assertFalse(service.authority.health()["started"])


if __name__ == "__main__":
    unittest.main()
