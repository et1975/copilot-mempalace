"""Public MCP behavior over real SDK HTTP, domain, protocol and durable journals."""

from concurrent.futures import ThreadPoolExecutor
import json
import io
import logging
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from authority_fixture import command, create, uid
from mempalace_tasks.authority import AuthorityError
from mempalace_tasks.client import TaskClientError, TaskServiceClient
from mempalace_tasks.domain import SCHEMAS
from mempalace_tasks.model import state_to_dict
from service_fixture import ServiceFixture, TOKEN


class ServerTests(unittest.TestCase):
    def test_public_schemas_are_current_only_without_runtime_mode_flags(self):
        from jsonschema import Draft202012Validator
        from mempalace_tasks.server import _schemas, PUBLIC_OPERATIONS

        schemas = _schemas()
        for operation in PUBLIC_OPERATIONS:
            self.assertIn("expected_epoch", schemas[operation]["required"])
        validator = Draft202012Validator(schemas["outcome"])
        self.assertTrue(validator.is_valid({"epoch_id": uid(1), "command_id": uid(2)}))
        self.assertFalse(validator.is_valid({"epoch_id": None, "command_id": uid(2)}))

    def test_current_health_reports_startup_epoch_without_projection_or_boot_gate(self):
        with ServiceFixture() as service:
            result = TaskServiceClient(service.url, token=TOKEN).call_tool("mptask_health", {})
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["epoch_id"], service.epoch)
            self.assertIs(result["startup_pending"], False)
            self.assertIs(result["request_epoch_required"], True)
            self.assertNotIn("projection", result)
            self.assertNotIn("pending_reboot", result)
            self.assertNotIn("recovery_mode", result)

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
                           resource_keys=resource_keys, expected_epoch=service.epoch)
            task_id = client.call_tool("mptask_create", request)["task_id"]
            claimed = client.call_tool("mptask_claim", {
                "expected_epoch": service.epoch,
                "command_id": uid(3), "actor": "worker", "task_id": task_id,
                "expected_version": 1, "supervisor_id": "supervisor"})
            task = claimed["tasks"][0]
            self.assertEqual(task["attempt"]["resource_fences"], dict.fromkeys(resource_keys, 1))
            observed = service.sdk("mptask_get", {"task_id": task_id}).structuredContent
            self.assertEqual(observed["task"]["attempt"]["resource_fences"],
                             dict.fromkeys(resource_keys, 1))

            started = client.call_tool("mptask_attempt_report", {
                "expected_epoch": service.epoch,
                "command_id": uid(4), "actor": "supervisor", "task_id": task_id,
                "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"], "report_kind": "started",
                "evidence": {"prepared": True, "references": ["artifact:owned-installed-fences"],
                             "resource_fences": observed["task"]["attempt"]["resource_fences"]},
            })
            self.assertTrue(started["authorization"]["tasks"][0]["authorized"])
            task = started["tasks"][0]
            released = client.call_tool("mptask_release", {
                "expected_epoch": service.epoch,
                "command_id": uid(5), "actor": "worker", "task_id": task_id,
                "expected_version": task["version"], "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"], "reason": "Owned fixture release",
            })
            client.call_tool("mptask_recover", {
                "expected_epoch": service.epoch,
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
            service.execute(request)
            state = state_to_dict(service.authority.state)
            self.assertEqual(_safe(state, TOKEN), state)

    def test_create_accepts_empty_text_and_domain_utf8_byte_limits_remain_enforced(self):
        with ServiceFixture() as service:
            client = TaskServiceClient(service.url, token=TOKEN)
            request = {key: value for key, value in create().items() if key != "operation"}
            request.update(description="", acceptance="", expected_epoch=service.epoch)
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
            task_id = service.execute(create())["task_id"]
            result = service.sdk("mptask_update", {
                "expected_epoch": service.epoch,
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
                "expected_epoch": service.epoch,
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
            goal_id = service.execute(command(
                2, "bootstrap", project="demo", title="Owned goal", description="", acceptance="",
                planning_task={"title": "Owned plan", "description": "", "acceptance": "",
                               "execution_class": "isolated", "execution_profile": "local"},
                goal_policy={"scope": "Owned fixture"}))["goal_id"]
            result = service.sdk("mptask_expand", {
                "expected_epoch": service.epoch,
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
                self.assertEqual(set(schema["properties"]), allowed | {"actor", "command_id", "expected_epoch"})
                self.assertEqual(set(schema["required"]), required | {"actor", "command_id", "expected_epoch"})
                self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(tools["mptask_transition"].inputSchema["properties"]["evidence"]["type"],
                             "array")
            self.assertEqual(tools["mptask_attempt_report"].inputSchema["properties"]["evidence"]["type"],
                             "object")
            self.assertNotIn("expected_version", tools["mptask_renew"].inputSchema["properties"])

    def test_two_real_clients_share_one_claim_authority_and_typed_rejection(self):
        with ServiceFixture() as service:
            task_id = service.execute(create())["task_id"]
            clients = [TaskServiceClient(service.url, token=TOKEN) for _ in range(2)]
            def claim(index):
                request = command(10 + index, "claim", actor=f"worker{index or ''}",
                                  task_id=task_id, expected_version=1, supervisor_id="supervisor")
                request["expected_epoch"] = service.epoch
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
                ("mptask_create", {"actor": "operator", "command_id": uid(5), "password": TOKEN,
                                   "expected_epoch": service.epoch}),
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
            args["expected_epoch"] = service.epoch
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
            self.assertEqual(http.post(service.url.removesuffix("/mcp") + "/control/stop",
                                       json={}, headers=headers).status_code, 401)
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
            task_id = service.execute(create())["task_id"]
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
            task_id = service.execute(create())["task_id"]
            service.execute(command(3, "claim", actor="worker", task_id=task_id,
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
            task_id = service.execute(create(deferred_until="2026-09-23T00:01:00Z"))["task_id"]
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

    def test_noncurrent_health_remains_data_and_diagnostics_do_not_reconcile(self):
        with ServiceFixture(policy={"sweep_seconds": 86400}) as service:
            task_id = service.execute(create())["task_id"]
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
            task_id = service.execute(create())["task_id"]
            claimed = service.execute(command(
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
            service.execute(command(
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
                arguments = {key: value for key, value in create(10).items() if key != "operation"}
                arguments["expected_epoch"] = service.epoch
                future = pool.submit(client.call_tool, "mptask_create",
                                     arguments)
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
