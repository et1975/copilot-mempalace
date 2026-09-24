"""Client contract tests against owned, real SDK servers, never a task authority."""

import asyncio
import io
import json
import logging
import socket
import threading
import time
import unittest
from unittest.mock import patch

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

from mempalace_tasks.client import TaskClientError, TaskServiceClient


TOKEN = "owned-client-fixture-token"
PATH = "/owned/tasks/mcp"
COMMAND_ID = "11111111-1111-4111-8111-111111111111"


def abandoned_receipt():
    return {
        "ok": False, "outcome": "abandoned", "command_id": COMMAND_ID,
        "event_id": "owned-settlement-event", "ordinal": None, "replayed": False,
        "response": None, "tasks": [],
        "authorization": {"as_of": "2026-09-23T22:00:00Z", "fresh": True, "tasks": []},
    }


def authority_error(*, code="version_conflict", ambiguous=False):
    return {
        "code": code, "message": "The authority rejected this command",
        "details": {}, "ambiguous": ambiguous,
    }


def noncurrent_health():
    return {
        "schema_version": 1, "authority_id": "22222222-2222-4222-8222-222222222222",
        "as_of": "2026-09-24T12:00:00Z", "last_verified_at": "2026-09-24T11:59:00Z",
        "raw_cursor": "owned-event-3", "domain_head": "owned-event-1", "domain_ordinal": 1,
        "fresh": False, "reason": "pending_command", "started": True, "pending_reboot": False,
        "protocol": {
            "raw_hash": "a" * 64, "record_count": 3, "outcomes": {"committed": 1, "abandoned": 1},
        },
        "pending_command": {"command_id": COMMAND_ID, "settlement_recorded": True},
        "error": {"code": "upstream_unavailable"},
    }


def text_result(payload, *, error=False):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        isError=error,
    )


class OwnedServer:
    """SDK MCP application with HTTP-boundary faults and fixed tool results."""

    def __init__(self):
        self.requests = []
        self.calls = []
        self.delays = {}
        self.disconnected = threading.Event()
        self.http_status = None
        self.http_failure_method = "initialize"
        self.location = "/elsewhere"
        self.rpc_error_method = None
        self.extra_headers = []
        self.raw_response = None
        self.raw_content_type = b"application/json"
        self.result = {"ok": True, "task": {"id": "owned-task", "title": "caf\u00e9"}}
        self.response = "both"
        self.ready = threading.Event()
        self.errors = []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(32)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}{PATH}"
        sdk = FastMCP(
            "owned-client-fixture",
            log_level="ERROR",
            json_response=True,
            stateless_http=True,
            streamable_http_path=PATH,
            transport_security=TransportSecuritySettings(
                allowed_hosts=["127.0.0.1:*"],
                allowed_origins=["http://127.0.0.1:*"],
            ),
        )

        async def tool(value: dict | None = None, command_id: str | None = None):
            self.calls.append({"value": value, "command_id": command_id})
            await asyncio.sleep(self.delays.get("tool", 0))
            if isinstance(self.response, types.CallToolResult):
                return self.response
            if self.response == "text":
                return text_result(self.result)
            return types.CallToolResult(
                structuredContent=self.result,
                content=(
                    [types.TextContent(type="text", text=json.dumps(self.result))]
                    if self.response == "both" else []
                ),
            )

        for name in (
            "mptask_snapshot", "mptask_get", "mptask_list", "mptask_ready",
            "mptask_history", "mptask_health", "mptask_wait_ready",
            "mptask_create", "mptask_future_write",
        ):
            sdk.add_tool(tool, name=name, structured_output=False)
        self.app = sdk.streamable_http_app()
        self.server = uvicorn.Server(uvicorn.Config(
            self.dispatch, host="127.0.0.1", port=0, log_config=None,
            log_level="critical", access_log=False, ws="none",
            timeout_graceful_shutdown=2, interface="asgi3", lifespan="on",
        ))
        self.thread = threading.Thread(target=self.run, name="owned-mcp-server")

    async def dispatch(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def ready_send(message):
                await send(message)
                if message["type"] == "lifespan.startup.complete":
                    self.ready.set()
            return await self.app(scope, receive, ready_send)
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        rpc = json.loads(body) if body else {}
        method = rpc.get("method")
        headers = dict(scope["headers"])
        self.requests.append({
            "path": scope["path"], "query": scope["query_string"],
            "headers": headers, "rpc": rpc, "method": scope["method"],
        })
        status = self.http_status if method == self.http_failure_method else None
        if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
            status = 401
        await asyncio.sleep(self.delays.get(method, 0))
        if status is not None:
            await send({
                "type": "http.response.start", "status": status,
                "headers": [(b"location", self.location.encode())],
            })
            await send({"type": "http.response.body", "body": TOKEN.encode()})
            return
        if self.raw_response is not None and method == "tools/call":
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", self.raw_content_type)],
            })
            await send({"type": "http.response.body", "body": self.raw_response})
            return
        if method == self.rpc_error_method:
            error = types.JSONRPCError(
                jsonrpc="2.0", id=rpc["id"],
                error=types.ErrorData(code=-32603, message=f"failure {TOKEN}"),
            )
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": error.model_dump_json().encode(),
            })
            return
        delivered = False

        async def wait_for_disconnect():
            message = await receive()
            if message["type"] == "http.disconnect":
                self.disconnected.set()
            return message
        disconnected = asyncio.create_task(wait_for_disconnect())

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await asyncio.shield(disconnected)
        async def response_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": message["headers"] + self.extra_headers}
            await send(message)
        try:
            await self.app(scope, replay_receive, response_send)
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

    def run(self):
        try:
            asyncio.run(self.server.serve(sockets=[self.socket]))
        except BaseException as exc:
            self.errors.append(exc)
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(5) or self.errors:
            self.__exit__(None, None, None)
            raise AssertionError("Owned SDK server did not start")
        return self

    def __exit__(self, *_args):
        self.server.should_exit = True
        self.thread.join(5)
        self.socket.close()
        if self.thread.is_alive():
            raise AssertionError("Owned SDK server did not stop")
        if self.errors:
            raise self.errors[0]

    def client(self, **kwargs):
        return TaskServiceClient(self.url, token=TOKEN, **kwargs)


class ClientTests(unittest.TestCase):
    def test_routes_arguments_and_exact_endpoint_with_bearer(self):
        with OwnedServer() as server:
            result = server.client().call_tool(
                "mptask_create", {"command_id": "owned-command", "value": {"title": "caf\u00e9"}},
            )
            self.assertEqual(result, {
                "ok": True, "task": {"id": "owned-task", "title": "caf\u00e9"},
            })
            self.assertEqual(server.calls, [{
                "command_id": "owned-command", "value": {"title": "caf\u00e9"},
            }])
            self.assertEqual(server.requests[0]["rpc"]["method"], "initialize")
            for request in server.requests:
                self.assertEqual(request["path"], PATH)
                self.assertEqual(request["query"], b"")
                self.assertEqual(request["headers"][b"authorization"], f"Bearer {TOKEN}".encode())
            self.assertEqual(
                [r["rpc"]["method"] for r in server.requests].count("tools/call"), 1,
            )

    def test_reads_text_only_and_structured_only_dictionaries(self):
        with OwnedServer() as server:
            for response in ("text", "structured"):
                with self.subTest(response=response):
                    server.response = response
                    self.assertEqual(
                        server.client().call_tool("mptask_get", {}),
                        {"ok": True, "task": {"id": "owned-task", "title": "caf\u00e9"}},
                    )

    def test_rejects_unsafe_endpoint_without_contacting_server(self):
        with OwnedServer() as server:
            port = server.socket.getsockname()[1]
            invalid = (
                f"ftp://127.0.0.1:{port}{PATH}",
                f"http://user:password@127.0.0.1:{port}{PATH}",
                f"{server.url}?token=secret", f"{server.url}?",
                f"{server.url}#secret", f"{server.url}#",
                f" {server.url}", f"{server.url}\n",
                f"http://127.0.0.1:{port}/first/../owned/tasks/mcp",
                f"http://127.0.0.1:{port}/back\\slash",
                "http://192.0.2.1/mcp", "http://example.invalid/mcp",
                "http://localhost.attacker.invalid/mcp",
                "http://127.0.0.1:0/mcp", "http://127.0.0.1:99999/mcp",
                "http://127.0.0.1:/mcp", "http://[::1%25zone]/mcp",
            )
            for url in invalid:
                with self.subTest(url=url):
                    with self.assertRaises(TaskClientError) as raised:
                        TaskServiceClient(url, token=TOKEN)
                    self.assertEqual(raised.exception.code, "invalid_configuration")
                    self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(server.requests, [])

    def test_rejects_invalid_credentials_and_timeouts(self):
        for token in ("", None, b"secret", "Bearer secret", "a\nX-Header: secret", "a\rsecret", "caf\u00e9"):
            with self.subTest(token=token):
                with self.assertRaises(TaskClientError) as raised:
                    TaskServiceClient("http://127.0.0.1/mcp", token=token)
                self.assertEqual(raised.exception.code, "invalid_configuration")
        for timeout in (0, -1, float("nan"), float("inf"), True, "1", None, 10**1000):
            with self.subTest(timeout=timeout):
                with self.assertRaises(TaskClientError) as raised:
                    TaskServiceClient("http://127.0.0.1/mcp", token=TOKEN, timeout=timeout)
                self.assertEqual(raised.exception.code, "invalid_configuration")

    def test_rejects_non_json_arguments_before_dispatch(self):
        with OwnedServer() as server:
            for arguments in (None, [], {"value": {1: "one"}}, {"value": (1, 2)}, {"value": float("nan")}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", arguments)
                    self.assertEqual(raised.exception.code, "invalid_arguments")
                    self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(server.requests, [])

    def test_rejects_non_task_and_unknown_tools_without_dispatch(self):
        with OwnedServer() as server:
            for name in ("mempalace_task_create", "mptask_", "mptask_get\n", "", None):
                with self.subTest(name=name):
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool(name, {})
                    self.assertEqual(raised.exception.code, "unknown_tool")
            self.assertEqual(server.requests, [])
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_not_advertised", {})
            self.assertEqual(raised.exception.code, "unknown_tool")
            self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(server.calls, [])
            self.assertNotIn("tools/call", [r["rpc"].get("method") for r in server.requests])

    def test_preserves_non_current_diagnostics_and_raw_views(self):
        with OwnedServer() as server:
            server.result = {
                "fresh": False, "health": "RECONCILING", "cursor": None,
                "diagnostics": {"reason": "upstream unavailable"},
                "snapshot": {"custom_authority_shape": [1, None, False]},
            }
            self.assertEqual(server.client().call_tool("mptask_snapshot", {}), {
                "fresh": False, "health": "RECONCILING", "cursor": None,
                "diagnostics": {"reason": "upstream unavailable"},
                "snapshot": {"custom_authority_shape": [1, None, False]},
            })

    def test_rejects_missing_malformed_or_conflicting_content(self):
        invalid_results = (
            types.CallToolResult(content=[]),
            types.CallToolResult(content=[types.TextContent(type="text", text="not json")]),
            text_result([]), text_result(None),
            types.CallToolResult(content=[types.TextContent(type="text", text='{"value":NaN}')]),
            types.CallToolResult(content=[types.TextContent(type="text", text='{"value":1,"value":2}')]),
            types.CallToolResult(content=[types.ImageContent(type="image", data="AA==", mimeType="image/png")]),
            types.CallToolResult(content=[
                types.TextContent(type="text", text='{"one":1}'),
                types.TextContent(type="text", text='{"two":2}'),
            ]),
            types.CallToolResult(
                structuredContent={"value": 1},
                content=[types.TextContent(type="text", text='{"value":2}')],
            ),
            types.CallToolResult(
                structuredContent={"value": True},
                content=[types.TextContent(type="text", text='{"value":1}')],
            ),
            types.CallToolResult(
                structuredContent={"value": 1},
                content=[types.TextContent(type="text", text="a diagnostic, not the result")],
            ),
        )
        with OwnedServer() as server:
            for response in invalid_results:
                with self.subTest(response=response):
                    server.response = response
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_get", {})
                    self.assertEqual(raised.exception.code, "invalid_response")
                    self.assertFalse(raised.exception.ambiguous)

    def test_maps_tool_errors_and_redacts_credentials_recursively(self):
        with OwnedServer() as server:
            server.response = text_result({
                "ok": False,
                "error": {
                    "code": "command_conflict", "message": f"Rejected {TOKEN}",
                    "details": {"nested": [TOKEN, {TOKEN: "also " + TOKEN}]},
                    "ambiguous": False,
                },
            }, error=True)
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_create", {"command_id": "same-command"})
            error = raised.exception
            self.assertEqual(error.code, "command_conflict")
            self.assertFalse(error.ambiguous)
            self.assertIn("[redacted]", error.message)
            self.assertNotIn(TOKEN, str(error))
            self.assertNotIn(TOKEN, json.dumps(error.details))
            self.assertEqual(len(server.calls), 1)

    def test_maps_unflagged_domain_errors_and_plain_iserror(self):
        with OwnedServer() as server:
            for response, code in (
                (text_result({"ok": False, "error": {"code": "not_ready", "message": "Not ready"}}), "not_ready"),
                (text_result({"error": {"code": "not_found", "message": "Missing"}}), "not_found"),
                (text_result({"ok": False}), "tool_error"),
                (types.CallToolResult(
                    isError=True, content=[types.TextContent(type="text", text=f"broken {TOKEN}")],
                ), "tool_error"),
            ):
                with self.subTest(code=code):
                    server.response = response
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_get", {})
                    self.assertEqual(raised.exception.code, code)
                    self.assertFalse(raised.exception.ambiguous)
                    self.assertNotIn(TOKEN, str(raised.exception))

    def test_maps_http_failures_without_echoing_response(self):
        with OwnedServer() as server:
            for method, ambiguous in (("initialize", False), ("tools/call", True)):
                for status in (401, 403, 404, 503):
                    with self.subTest(method=method, status=status):
                        server.http_status = status
                        server.http_failure_method = method
                        with self.assertRaises(TaskClientError) as raised:
                            server.client().call_tool("mptask_create", {})
                        self.assertEqual(raised.exception.code, "http_error")
                        self.assertEqual(raised.exception.details["status_code"], status)
                        self.assertEqual(raised.exception.ambiguous, ambiguous)
                        self.assertNotIn(TOKEN, str(raised.exception))
            self.assertEqual(server.calls, [])

    def test_never_follows_same_origin_redirects(self):
        with OwnedServer() as server:
            server.http_status = 307
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_get", {})
            self.assertEqual(raised.exception.code, "http_error")
            self.assertEqual(raised.exception.details["status_code"], 307)
            self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0]["path"], PATH)

    def test_does_not_use_environment_proxy_or_certificate_settings(self):
        with OwnedServer() as server, OwnedServer() as proxy:
            with patch.dict("os.environ", {
                "HTTP_PROXY": proxy.url, "HTTPS_PROXY": proxy.url, "ALL_PROXY": proxy.url,
                "NO_PROXY": "", "http_proxy": proxy.url, "all_proxy": proxy.url, "no_proxy": "",
                "SSL_CERT_FILE": "/nonexistent-client-test-certificate",
            }):
                self.assertEqual(server.client().call_tool("mptask_get", {})["task"]["id"], "owned-task")
            self.assertEqual(proxy.requests, [])

    def test_maps_rpc_error_after_dispatch_conservatively(self):
        with OwnedServer() as server:
            server.rpc_error_method = "tools/call"
            for name, ambiguous in (("mptask_get", False), ("mptask_future_write", True)):
                with self.subTest(name=name):
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool(name, {})
                    self.assertEqual(raised.exception.code, "rpc_error")
                    self.assertEqual(raised.exception.details["rpc_code"], -32603)
                    self.assertEqual(raised.exception.ambiguous, ambiguous)
                    self.assertNotIn(TOKEN, str(raised.exception))

    def test_connect_failure_is_not_ambiguous_before_dispatch(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            client = TaskServiceClient(
                f"http://127.0.0.1:{reserved.getsockname()[1]}/mcp", token=TOKEN,
            )
            with self.assertRaises(TaskClientError) as raised:
                client.call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "transport_error")
            self.assertFalse(raised.exception.ambiguous)

    def test_overall_deadline_includes_handshake_and_call(self):
        with OwnedServer() as server:
            server.delays = {"initialize": 0.20, "tool": 0.25}
            started = time.monotonic()
            with self.assertRaises(TaskClientError) as raised:
                server.client(timeout=0.35).call_tool("mptask_create", {})
            elapsed = time.monotonic() - started
            self.assertEqual(raised.exception.code, "timeout")
            self.assertTrue(raised.exception.ambiguous)
            self.assertGreaterEqual(elapsed, 0.30)
            self.assertLess(elapsed, 0.65)
            self.assertEqual(len(server.calls), 1)

    def test_timeout_during_initialize_does_not_dispatch(self):
        with OwnedServer() as server:
            server.delays = {"initialize": 0.3}
            with self.assertRaises(TaskClientError) as raised:
                server.client(timeout=0.1).call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "timeout")
            self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(server.calls, [])

    def test_timeout_closes_connection_without_threads_or_retry(self):
        with OwnedServer() as server:
            server.delays = {"tool": 0.4}
            threads = {thread.ident for thread in threading.enumerate()}
            with self.assertRaises(TaskClientError) as raised:
                server.client(timeout=0.15).call_tool(
                    "mptask_future_write", {"command_id": "never-automatically-repeat"},
                )
            self.assertEqual(raised.exception.code, "timeout")
            self.assertTrue(raised.exception.ambiguous)
            self.assertTrue(server.disconnected.wait(1))
            self.assertEqual(server.calls, [{
                "value": None, "command_id": "never-automatically-repeat",
            }])
            self.assertEqual({thread.ident for thread in threading.enumerate()}, threads)

    def test_read_timeouts_are_not_ambiguous(self):
        with OwnedServer() as server:
            server.delays = {"tool": 0.2}
            for name in (
                "mptask_snapshot", "mptask_get", "mptask_list", "mptask_ready",
                "mptask_history", "mptask_health", "mptask_wait_ready",
            ):
                with self.subTest(name=name):
                    with self.assertRaises(TaskClientError) as raised:
                        server.client(timeout=0.1).call_tool(name, {})
                    self.assertEqual(raised.exception.code, "timeout")
                    self.assertFalse(raised.exception.ambiguous)

    def test_sync_api_rejects_running_event_loop_without_leaking_coroutine(self):
        client = TaskServiceClient("http://127.0.0.1/mcp", token=TOKEN)

        async def already_running():
            with self.assertRaises(TaskClientError) as raised:
                client.call_tool("mptask_get", {})
            self.assertEqual(raised.exception.code, "invalid_configuration")
        asyncio.run(already_running())

    def test_rejects_stateful_profile_without_starting_reconnect_stream(self):
        with OwnedServer() as server:
            server.extra_headers = [(b"mcp-session-id", b"not-our-stateless-service")]
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertFalse(raised.exception.ambiguous)
            self.assertEqual([r["method"] for r in server.requests], ["POST"])

    def test_never_exposes_bearer_in_results_or_sdk_debug_logs(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("mcp.client.streamable_http")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(logger.removeHandler, handler)
        with OwnedServer() as server:
            server.result = {"task": {"description": f"credential reflected: {TOKEN}"}}
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_get", {})
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertNotIn(TOKEN, stream.getvalue())

    def test_malformed_wire_response_does_not_leak_sdk_exception_details(self):
        with OwnedServer() as server:
            server.raw_response = f'{{"invalid-{TOKEN}"'.encode()
            with self.assertNoLogs("mcp.client.streamable_http", level="DEBUG"):
                with self.assertRaises(TaskClientError) as raised:
                    server.client().call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertTrue(raised.exception.ambiguous)
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertEqual(
                sum(r["rpc"].get("method") == "tools/call" for r in server.requests), 1,
            )

    def test_rejects_sse_profile_instead_of_reconnecting(self):
        with OwnedServer() as server:
            server.raw_content_type = b"text/event-stream"
            server.raw_response = b"id: reconnect-cursor\nretry: 1\ndata: \n\n"
            with self.assertRaises(TaskClientError) as raised:
                server.client(timeout=0.3).call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertTrue(raised.exception.ambiguous)
            self.assertNotIn("GET", [request["method"] for request in server.requests])

    def test_discovery_failure_does_not_make_a_mutation_ambiguous(self):
        with OwnedServer() as server:
            server.http_failure_method = "tools/list"
            server.http_status = 503
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_future_write", {})
            self.assertEqual(raised.exception.code, "http_error")
            self.assertFalse(raised.exception.ambiguous)
            self.assertEqual(server.calls, [])

    def test_empty_dictionary_is_valid_when_actually_supplied(self):
        with OwnedServer() as server:
            server.result = {}
            self.assertEqual(server.client().call_tool("mptask_get", {}), {})

    def test_returns_matching_terminal_abandonment_as_a_resolved_receipt(self):
        with OwnedServer() as server:
            for representation, replayed, fresh in (
                ("both", False, True), ("structured", True, True), ("text", True, False),
            ):
                with self.subTest(representation=representation, replayed=replayed, fresh=fresh):
                    server.response = representation
                    server.result = abandoned_receipt()
                    server.result["replayed"] = replayed
                    server.result["authorization"]["fresh"] = fresh
                    result = server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertEqual(result, {
                        "ok": False, "outcome": "abandoned", "command_id": COMMAND_ID,
                        "event_id": "owned-settlement-event", "ordinal": None, "replayed": replayed,
                        "response": None, "tasks": [],
                        "authorization": {
                            "as_of": "2026-09-23T22:00:00Z", "fresh": fresh, "tasks": [],
                        },
                    })
            self.assertEqual(len(server.calls), 3)

    def test_malformed_abandonment_never_authorizes_a_new_command_id(self):
        malformed = [
            {"ok": True}, {"ok": 0},
            {"command_id": "22222222-2222-4222-8222-222222222222"},
            {"command_id": None}, {"command_id": []}, {"command_id": ""},
            {"event_id": None}, {"event_id": ""}, {"event_id": []}, {"event_id": " \t"},
            {"event_id": "owned\ninjected-event"},
            {"ordinal": 0}, {"ordinal": False},
            {"replayed": None}, {"replayed": 0}, {"replayed": "false"},
            {"response": {}}, {"response": {"ok": True}}, {"response": False},
            {"tasks": {}}, {"tasks": [{"id": "unexpected-task"}]},
            {"authorization": None}, {"authorization": {}},
            {"authorization": {"as_of": "not-a-time", "fresh": True, "tasks": []}},
            {"authorization": {"as_of": "2026-09-23T22:00:00Z", "fresh": 1, "tasks": []}},
            {"authorization": {"as_of": "2026-09-23T22:00:00Z", "fresh": True, "tasks": [{}]}},
            {"error": authority_error()},
        ]
        with OwnedServer() as server:
            for changes in malformed:
                with self.subTest(changes=changes):
                    server.result = {**abandoned_receipt(), **changes}
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertEqual(raised.exception.code, "invalid_response")
                    self.assertTrue(raised.exception.ambiguous)
            for missing in abandoned_receipt():
                with self.subTest(missing=missing):
                    server.result = abandoned_receipt()
                    del server.result[missing]
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertTrue(raised.exception.ambiguous)

    def test_abandonment_requires_a_canonical_requested_command_identity(self):
        with OwnedServer() as server:
            for command_id in (None, "", "owned-command-not-a-uuid", "11111111111141118111111111111111"):
                with self.subTest(command_id=command_id):
                    server.result = abandoned_receipt()
                    server.result["command_id"] = command_id
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", {"command_id": command_id})
                    self.assertEqual(raised.exception.code, "invalid_response")
                    self.assertTrue(raised.exception.ambiguous)
            server.result = abandoned_receipt()
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_create", {})
            self.assertEqual(raised.exception.code, "invalid_response")
            self.assertTrue(raised.exception.ambiguous)

    def test_abandonment_cannot_override_an_mcp_error_or_reflect_the_token(self):
        with OwnedServer() as server:
            for payload, is_error in (
                (abandoned_receipt(), True),
                ({**abandoned_receipt(), "event_id": f"reflection-{TOKEN}"}, False),
                ({**abandoned_receipt(), "unexpected": {TOKEN: TOKEN}}, False),
                ({**abandoned_receipt(), "authorization": {
                    "as_of": TOKEN, "fresh": False, "tasks": [],
                }}, False),
            ):
                with self.subTest(payload=payload, is_error=is_error):
                    server.response = text_result(payload, error=is_error)
                    with self.assertNoLogs("mcp.client.streamable_http", level="DEBUG"):
                        with self.assertRaises(TaskClientError) as raised:
                            server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertEqual(raised.exception.code, "invalid_response")
                    self.assertTrue(raised.exception.ambiguous)
                    self.assertNotIn(TOKEN, str(raised.exception))
                    self.assertNotIn(TOKEN, json.dumps(raised.exception.details))

    def test_typed_authority_errors_preserve_explicit_ambiguity(self):
        with OwnedServer() as server:
            for name, code, ambiguous in (
                ("mptask_create", "version_conflict", False),
                ("mptask_future_write", "clock_error", True),
                ("mptask_get", "unknown_io_failure", True),
            ):
                for representation in ("both", "text", "structured"):
                    with self.subTest(name=name, code=code, representation=representation):
                        server.response = representation
                        server.result = {
                            "ok": False, "error": authority_error(code=code, ambiguous=ambiguous),
                        }
                        with self.assertRaises(TaskClientError) as raised:
                            server.client().call_tool(name, {"command_id": COMMAND_ID})
                        self.assertEqual(raised.exception.code, code)
                        self.assertIs(raised.exception.ambiguous, ambiguous)
            server.response = text_result(authority_error(), error=True)
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
            self.assertEqual(raised.exception.code, "version_conflict")
            self.assertFalse(raised.exception.ambiguous)

    def test_malformed_authority_error_keeps_conservative_mutation_ambiguity(self):
        with OwnedServer() as server:
            invalid_errors = [
                {**authority_error(), "ambiguous": value}
                for value in (None, 0, 1, "false", [], {})
            ] + [
                {**authority_error(), "code": ""},
                {**authority_error(), "code": {TOKEN: TOKEN}},
                {**authority_error(), "message": []},
                {**authority_error(), "details": [TOKEN]},
                {"code": "version_conflict", "message": "Rejected", "ambiguous": False},
                {"ambiguous": False},
                {"code": "old_error", "message": f"Legacy {TOKEN}", "details": {}},
            ]
            for error in invalid_errors:
                with self.subTest(error=error):
                    server.result = {"ok": False, "error": error}
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertTrue(raised.exception.ambiguous)
                    self.assertNotIn(TOKEN, str(raised.exception))
                    self.assertNotIn(TOKEN, json.dumps(raised.exception.details))

    def test_conflicting_error_envelope_is_not_a_definitive_rejection(self):
        with OwnedServer() as server:
            for payload in (
                {"ok": True, "error": authority_error()},
                {"ok": 0, "error": authority_error()},
                {"ok": False, "error": authority_error(), "outcome": "committed"},
                {"ok": False, "error": authority_error(), "response": {"ok": True}},
            ):
                with self.subTest(payload=payload):
                    server.result = payload
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool("mptask_create", {"command_id": COMMAND_ID})
                    self.assertTrue(raised.exception.ambiguous)

    def test_typed_authority_error_redaction_keeps_true_ambiguity(self):
        with OwnedServer() as server:
            server.result = {
                "ok": False,
                "error": {
                    "code": f"uncertain_{TOKEN}", "message": f"Failure {TOKEN}",
                    "details": {"cause": [TOKEN, {TOKEN: TOKEN}]}, "ambiguous": True,
                },
            }
            with self.assertNoLogs("mcp.client.streamable_http", level="DEBUG"):
                with self.assertRaises(TaskClientError) as raised:
                    server.client().call_tool("mptask_get", {})
            self.assertTrue(raised.exception.ambiguous)
            self.assertNotIn(TOKEN, raised.exception.code)
            self.assertNotIn(TOKEN, str(raised.exception))
            self.assertNotIn(TOKEN, json.dumps(raised.exception.details))

    def test_known_reads_preserve_noncurrent_authority_health_errors(self):
        with OwnedServer() as server:
            for name in (
                "mptask_health", "mptask_snapshot", "mptask_get", "mptask_list",
                "mptask_ready", "mptask_history", "mptask_wait_ready",
            ):
                for representation in ("both", "text", "structured"):
                    with self.subTest(name=name, representation=representation):
                        server.response = representation
                        server.result = noncurrent_health()
                        self.assertEqual(server.client().call_tool(name, {}), noncurrent_health())

    def test_health_diagnostics_preserve_ok_false_and_not_started_metadata(self):
        with OwnedServer() as server:
            server.result = {**noncurrent_health(), "ok": False}
            result = server.client().call_tool("mptask_health", {})
            self.assertEqual(result, {**noncurrent_health(), "ok": False})
            server.result = {
                **noncurrent_health(), "as_of": None, "last_verified_at": None,
                "fresh": False, "reason": "not_started", "started": False,
                "pending_reboot": None, "pending_command": None,
            }
            self.assertEqual(server.client().call_tool("mptask_health", {}), {
                **noncurrent_health(), "as_of": None, "last_verified_at": None,
                "fresh": False, "reason": "not_started", "started": False,
                "pending_reboot": None, "pending_command": None,
            })

    def test_diagnostic_shape_does_not_hide_mcp_or_mutation_errors(self):
        with OwnedServer() as server:
            server.response = text_result(noncurrent_health(), error=True)
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_health", {})
            self.assertFalse(raised.exception.ambiguous)
            server.response = "both"
            for name in ("mptask_create", "mptask_future_write"):
                with self.subTest(name=name):
                    server.result = {**noncurrent_health(), "ok": False}
                    with self.assertRaises(TaskClientError) as raised:
                        server.client().call_tool(name, {"command_id": COMMAND_ID})
                    self.assertTrue(raised.exception.ambiguous)
            server.result = {**noncurrent_health(), "outcome": "abandoned"}
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_health", {})
            self.assertEqual(raised.exception.code, "invalid_response")

    def test_malformed_diagnostics_are_not_promoted_to_valid_reads(self):
        with OwnedServer() as server:
            for changes in (
                {"schema_version": True}, {"schema_version": 2},
                {"authority_id": None}, {"authority_id": "bad-identity"},
                {"as_of": "not-a-time"}, {"last_verified_at": []},
                {"fresh": 0}, {"fresh": "false"}, {"fresh": None},
                {"reason": None}, {"reason": []}, {"reason": ""},
                {"raw_cursor": []}, {"domain_head": {}}, {"domain_ordinal": False},
                {"domain_ordinal": -1}, {"ok": "false"},
                {"error": []}, {"error": {"code": None}},
                {"protocol": []}, {"pending_command": "broken"},
                {"started": "false"}, {"pending_reboot": "false"},
            ):
                with self.subTest(changes=changes):
                    server.result = {**noncurrent_health(), **changes}
                    with self.assertRaises(TaskClientError):
                        server.client().call_tool("mptask_health", {})
            for missing in (
                "schema_version", "authority_id", "as_of", "last_verified_at",
                "fresh", "reason", "raw_cursor", "domain_head", "domain_ordinal",
            ):
                with self.subTest(missing=missing):
                    server.result = noncurrent_health()
                    del server.result[missing]
                    with self.assertRaises(TaskClientError):
                        server.client().call_tool("mptask_health", {})

    def test_diagnostic_results_still_require_consistent_json_and_no_token_reflection(self):
        with OwnedServer() as server:
            for payload in (
                {**noncurrent_health(), "reason": f"failed {TOKEN}"},
                {**noncurrent_health(), "error": {"code": TOKEN}},
                {**noncurrent_health(), "protocol": {"details": [{TOKEN: TOKEN}]}},
                {**noncurrent_health(), "pending_command": {"command_id": TOKEN}},
            ):
                with self.subTest(payload=payload):
                    server.result = payload
                    with self.assertNoLogs("mcp.client.streamable_http", level="DEBUG"):
                        with self.assertRaises(TaskClientError) as raised:
                            server.client().call_tool("mptask_health", {})
                    self.assertEqual(raised.exception.code, "invalid_response")
                    self.assertNotIn(TOKEN, str(raised.exception))
                    self.assertNotIn(TOKEN, json.dumps(raised.exception.details))
            server.response = types.CallToolResult(
                structuredContent=noncurrent_health(),
                content=[types.TextContent(
                    type="text", text=json.dumps({**noncurrent_health(), "fresh": True}),
                )],
            )
            with self.assertRaises(TaskClientError) as raised:
                server.client().call_tool("mptask_health", {})
            self.assertEqual(raised.exception.code, "invalid_response")


if __name__ == "__main__":
    unittest.main()
