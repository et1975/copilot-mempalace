"""Real SDK stdio clients and private, test-owned HTTP services only."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from uuid import uuid4

from mcp import ClientSession, McpError, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mempalace_tasks import discovery, launcher, platform_support
from mempalace_tasks.server_identity import InstanceIdentity, instance_bearer, make_proof
from portable_lifecycle_fixture import ConfigurationFixture, IdentityServer, ROOT_TOKEN
from service_fixture import ServiceFixture, SocketRunner
from test_client import abandoned_receipt, authority_error, noncurrent_health
from test_palace import TestHub


def wire(value):
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


class GatewayService:
    """Official SDK server with paginated descriptors and controlled transport faults."""

    def __init__(self):
        self.configuration = ConfigurationFixture(lifecycle="external")
        self.config = self.configuration.config
        self.runner = SocketRunner()
        self.identity = InstanceIdentity(
            self.config.authority_id, str(uuid4()), f"http://127.0.0.1:{self.runner.port}/mcp")
        self.token = instance_bearer(self.identity, ROOT_TOKEN)
        self.requests = []
        self.calls = []
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.delay = 0
        self.status = None
        self.extra_headers = {}
        self.rpc_error = False
        self.reflect = False
        self.raw_response = None
        self.disconnect = False
        self.result = types.CallToolResult(
            content=[types.TextContent(type="text", text="exact text, not normalized JSON")],
            structuredContent={"ok": True, "value": "café"},
            _meta={"upstream": "receipt-metadata"},
        )
        self.tools = [
            types.Tool(name="mptask_future_write", title="Future write",
                       description="Forward without locally validating or enriching arguments",
                       inputSchema={"type": "object", "required": ["upstream_only"]},
                       outputSchema={"type": "object", "required": ["ordinary_success"]},
                       annotations=types.ToolAnnotations(
                           readOnlyHint=False, destructiveHint=True, idempotentHint=False),
                       _meta={"origin": "fixture"}, icons=[{"src": "https://example.invalid/icon"}]),
            types.Tool(name="mptask_health", inputSchema={"type": "object"},
                       annotations=types.ToolAnnotations(readOnlyHint=True)),
        ]
        self.sdk = Server("owned-gateway-upstream")

        async def list_tools(request):
            cursor = request.params.cursor if request.params else None
            return types.ServerResult(types.ListToolsResult(
                tools=[self.tools[0 if cursor is None else 1]],
                nextCursor="second" if cursor is None else None,
                _meta={"page": cursor or "first"},
            ))

        async def call_tool(request):
            self.calls.append(wire(request.params))
            self.started.set()
            await asyncio.sleep(self.delay)
            return types.ServerResult(self.result)

        self.sdk.request_handlers[types.ListToolsRequest] = list_tools
        self.sdk.request_handlers[types.CallToolRequest] = call_tool
        manager = StreamableHTTPSessionManager(self.sdk, stateless=True, json_response=True)

        @asynccontextmanager
        async def lifespan(_):
            async with manager.run():
                yield

        async def identity(request):
            return JSONResponse(make_proof(
                self.identity, ROOT_TOKEN, request.query_params["nonce"], ready=True))

        self.app = Starlette(
            routes=[Route("/identity", identity), Route("/mcp", self,
                                                       methods=["GET", "POST", "DELETE"])],
            lifespan=lifespan,
        )
        self.manager = manager
        platform_support.ensure_private_directory(self.config.runtime_dir)
        self.owner = platform_support.LifetimeLock(self.config.runtime_dir / "authority.lock").acquire()
        self.thread = threading.Thread(target=self.runner.run, args=(self.app,))

    async def __call__(self, scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        rpc = json.loads(body) if body else {}
        self.requests.append(rpc)
        method = rpc.get("method")
        if dict(scope["headers"]).get(b"authorization") != ("Bearer " + self.token).encode():
            return await Response(ROOT_TOKEN, status_code=401)(scope, receive, send)
        if method == "tools/call" and self.status:
            return await Response(ROOT_TOKEN, status_code=self.status,
                                  headers={"location": self.identity.endpoint})(scope, receive, send)
        if method == "tools/call" and self.rpc_error:
            return await JSONResponse({
                "jsonrpc": "2.0", "id": rpc["id"],
                "error": {"code": -32603, "message": ROOT_TOKEN + self.token},
            })(scope, receive, send)
        if method == "tools/call" and self.raw_response is not None:
            return await Response(self.raw_response, media_type="application/json")(scope, receive, send)
        if method == "tools/call" and self.disconnect:
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/json"), (b"content-length", b"100")]})
            await send({"type": "http.response.body", "body": b"{"})
            return
        if method == "tools/call" and self.reflect:
            self.result = types.CallToolResult(
                content=[], structuredContent={"reflected": ROOT_TOKEN + self.token})
        delivered = False

        async def observe_disconnect():
            message = await receive()
            if message["type"] == "http.disconnect" and method == "tools/call":
                self.disconnected.set()
            return message

        disconnected = asyncio.create_task(observe_disconnect())

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body)}
            return await asyncio.shield(disconnected)

        async def respond(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": message["headers"] + [
                    (key.encode(), value.encode()) for key, value in self.extra_headers.items()]}
            await send(message)

        try:
            return await self.manager.handle_request(scope, replay, respond)
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

    def __enter__(self):
        self.thread.start()
        if not self.runner.ready.wait(5) or self.runner.errors:
            raise AssertionError("Test SDK service did not start")
        discovery.publish_registry(self.config, self.identity)
        return self

    def __exit__(self, *_):
        self.runner.stop()
        self.thread.join(6)
        self.runner.socket.close()
        self.owner.release()
        self.configuration.close()
        if self.thread.is_alive() or self.runner.errors:
            raise AssertionError("Test SDK service did not stop")


class StdioFrontendTests(unittest.TestCase):
    def parameters(self, path, timeout="2s", *, env=None):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "mempalace_tasks", "mcp",
                  *(["--config", str(path)] if path is not None else []), "--timeout", timeout],
            env={**os.environ, **(env or {})},
        )

    @asynccontextmanager
    async def frontend(self, path, timeout="2s", *, env=None):
        error_path = Path(os.environ["MPTASK_TEST_TMPDIR"]) / f"stdio-{uuid4()}.stderr"
        incoming = []

        async def observe(message):
            if isinstance(message, Exception):
                incoming.append(message)

        try:
            with error_path.open("w+") as errors:
                async with stdio_client(self.parameters(path, timeout, env=env), errlog=errors) as (r, w):
                    async with ClientSession(
                            r, w, read_timeout_seconds=timedelta(seconds=8),
                            message_handler=observe) as session:
                        initialized = await session.initialize()
                        self.assertIsNotNone(initialized.capabilities.tools)
                        self.assertIsNone(initialized.capabilities.resources)
                        self.assertIsNone(initialized.capabilities.prompts)
                        yield session
                errors.seek(0)
                diagnostic = errors.read()
                self.assertNotIn(ROOT_TOKEN, diagnostic)
                self.assertNotIn("Traceback", diagnostic)
                self.assertEqual(incoming, [], "Non-MCP stdout is not allowed")
        finally:
            error_path.unlink(missing_ok=True)

    async def raw_call(self, session, name, arguments, *, meta=None):
        return await session.send_request(types.ClientRequest(types.CallToolRequest(
            params=types.CallToolRequestParams(name=name, arguments=arguments, _meta=meta),
        )), types.CallToolResult)

    @asynccontextmanager
    async def raw_frontend(self, path, timeout="2s"):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mempalace_tasks", "mcp", "--config", str(path),
            "--timeout", timeout, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            process.stdin.write((json.dumps({
                "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                    "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                    "capabilities": {}, "clientInfo": {"name": "eof-test", "version": "1"},
                },
            }) + "\n").encode())
            await process.stdin.drain()
            initialized = await asyncio.wait_for(process.stdout.readline(), 5)
            self.assertEqual(json.loads(initialized)["id"], 0)
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()
            yield process
        finally:
            if process.returncode is None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 4)
                except TimeoutError:
                    process.terminate()
                    await process.wait()
            output, errors = await process.communicate()
            self.assertNotIn(ROOT_TOKEN.encode(), output + errors)
            self.assertNotIn(b"Traceback", errors)
            self.assertEqual(process.returncode, 0, errors.decode())

    def test_eof_cancels_pending_exchange_without_stopping_shared_owner(self):
        with GatewayService() as server:
            server.delay = 1.5

            async def scenario():
                async with self.raw_frontend(server.configuration.path, "10s") as process:
                    process.stdin.write((json.dumps({
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "mptask_future_write",
                                   "arguments": {"command_id": "only-once"}},
                    }) + "\n").encode())
                    await process.stdin.drain()
                    self.assertTrue(await asyncio.to_thread(server.started.wait, 3))
                    began = time.monotonic()
                    process.stdin.close()
                    await asyncio.wait_for(process.wait(), 2)
                    self.assertLess(time.monotonic() - began, 1)
                    self.assertEqual(process.returncode, 0)
                    self.assertTrue(await asyncio.to_thread(server.disconnected.wait, 0.5))
                self.assertEqual(len(server.calls), 1)
                info = await asyncio.to_thread(discovery.connect, server.config)
                self.assertEqual(info.instance_id, server.identity.instance_id)
            asyncio.run(scenario())

    def test_request_cancel_closes_only_local_exchange_and_session_stays_usable(self):
        with GatewayService() as server:
            server.delay = 1.5

            async def scenario():
                async with self.raw_frontend(server.configuration.path, "10s") as process:
                    process.stdin.write((json.dumps({
                        "jsonrpc": "2.0", "id": 15, "method": "tools/call",
                        "params": {"name": "mptask_future_write",
                                   "arguments": {"command_id": "cancelled-not-retried"}},
                    }) + "\n").encode())
                    await process.stdin.drain()
                    self.assertTrue(await asyncio.to_thread(server.started.wait, 3))
                    process.stdin.write((json.dumps({
                        "jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 15},
                    }) + "\n").encode())
                    await process.stdin.drain()
                    response = json.loads(await asyncio.wait_for(process.stdout.readline(), 1))
                    self.assertEqual(response["id"], 15)
                    self.assertIn("error", response)
                    self.assertTrue(await asyncio.to_thread(server.disconnected.wait, 0.5))
                    process.stdin.write(b'{"jsonrpc":"2.0","id":16,"method":"tools/list"}\n')
                    await process.stdin.drain()
                    response = json.loads(await asyncio.wait_for(process.stdout.readline(), 1))
                    self.assertEqual(response["id"], 16)
                    self.assertIn("tools", response["result"])
                self.assertEqual(len(server.calls), 1)
            asyncio.run(scenario())

    def test_timeout_is_ambiguous_no_mutation_retry_and_requires_explicit_reconnect(self):
        with GatewayService() as server:
            server.delay = 0.5

            async def scenario():
                async with self.frontend(server.configuration.path, "0.2s") as session:
                    began = time.monotonic()
                    result = await self.raw_call(session, "mptask_future_write", {"command_id": "once"})
                    self.assertTrue(result.isError)
                    self.assertTrue(result.structuredContent["error"]["ambiguous"])
                    self.assertEqual(result.structuredContent["error"]["code"], "timeout")
                    self.assertLess(time.monotonic() - began, 1)
                    self.assertEqual(len(server.calls), 1)
                    count = len(server.requests)
                    result = await self.raw_call(session, "mptask_future_write", {"command_id": "once"})
                    self.assertEqual(result.structuredContent["error"]["code"], "connection_closed")
                    self.assertFalse(result.structuredContent["error"]["ambiguous"])
                    self.assertEqual(len(server.requests), count)
            asyncio.run(scenario())

    def test_http_redirect_session_recovery_rpc_and_parse_errors_are_safe_and_not_retried(self):
        for fault in ["redirect", "session", "rpc", "malformed", "reflection", "disconnect"]:
            with self.subTest(fault=fault), GatewayService() as server:
                if fault == "redirect":
                    server.status = 307
                elif fault == "session":
                    server.extra_headers = {"mcp-session-id": "must-not-reconnect"}
                elif fault == "rpc":
                    server.rpc_error = True
                elif fault == "malformed":
                    server.raw_response = b'{"jsonrpc":'
                elif fault == "disconnect":
                    server.disconnect = True
                else:
                    server.reflect = True

                async def scenario():
                    async with self.frontend(server.configuration.path) as session:
                        result = await self.raw_call(session, "mptask_future_write", {"command_id": "once"})
                        self.assertTrue(result.isError)
                        self.assertNotIn(ROOT_TOKEN, json.dumps(wire(result)))
                        self.assertNotIn(server.token, json.dumps(wire(result)))
                        count = sum(request.get("method") == "tools/call" for request in server.requests)
                        self.assertEqual(count, 0 if fault == "session" else 1)
                        if fault != "session":
                            self.assertTrue(result.structuredContent["error"]["ambiguous"])
                asyncio.run(scenario())

    def test_environment_proxies_are_ignored_and_timeout_does_not_limit_session_lifetime(self):
        with GatewayService() as server:
            async def scenario():
                async with self.frontend(server.configuration.path, "0.3s", env={
                    "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                    "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
                }) as session:
                    await asyncio.sleep(0.4)
                    self.assertEqual((await session.list_tools()).nextCursor, "second")
            asyncio.run(scenario())

    def test_bounded_admission_rejects_excess_without_dispatch(self):
        with GatewayService() as server:
            server.delay = 0.35

            async def scenario():
                async with self.frontend(server.configuration.path, "3s") as session:
                    results = await asyncio.gather(*[
                        self.raw_call(session, "mptask_future_write", {"command_id": str(index)})
                        for index in range(40)])
                    rejected = [result for result in results if result.isError]
                    self.assertEqual(len(rejected), 8)
                    self.assertTrue(all(result.structuredContent["error"]["code"] == "busy"
                                        for result in rejected))
                    self.assertTrue(all(not result.structuredContent["error"]["ambiguous"]
                                        for result in rejected))
                    self.assertEqual(len(server.calls), 32)
                    self.assertEqual(len({call["arguments"]["command_id"] for call in server.calls}), 32)
            asyncio.run(scenario())

    def test_owner_replacement_never_changes_existing_frontend_binding(self):
        with GatewayService() as original, GatewayService() as replacement:
            async def scenario():
                async with self.frontend(original.configuration.path) as session:
                    await session.list_tools()
                    old_token = original.token
                    original.identity = InstanceIdentity(
                        original.identity.authority_id, str(uuid4()), original.identity.endpoint)
                    original.token = instance_bearer(original.identity, ROOT_TOKEN)
                    discovery.publish_registry(original.config, replacement.identity)
                    result = await self.raw_call(session, "mptask_future_write", {
                        "expected_epoch": "original-epoch", "command_id": "never-retry"})
                    self.assertTrue(result.isError)
                    self.assertEqual(original.calls, [])
                    self.assertEqual(replacement.requests, [])
                    self.assertNotIn(old_token, json.dumps(wire(result)))
                    result = await self.raw_call(session, "mptask_future_write", {})
                    self.assertEqual(result.structuredContent["error"]["code"], "connection_closed")
            asyncio.run(scenario())

    def test_launcher_without_genesis_does_not_implicitly_initialize(self):
        fixture = ConfigurationFixture()
        self.addCleanup(fixture.close)
        with TestHub() as hub:
            fixture.document["hub_url"] = hub.url
            fixture.save()
            result = self.call_cli("mcp", "--config", str(fixture.path), "--timeout", "3s")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(hub.events, [])
            self.assertFalse((fixture.config.runtime_dir / "serverinfo.json").exists())
            with platform_support.LifetimeLock(fixture.config.runtime_dir / "authority.lock"):
                pass

    def test_concurrent_frontends_start_one_shared_owner_and_eof_keeps_it_running(self):
        fixture = ConfigurationFixture()
        self.addCleanup(fixture.close)
        with TestHub() as hub:
            fixture.document["hub_url"] = hub.url
            fixture.save()
            initialized = self.call_cli("init", "--config", str(fixture.path))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            async def scenario():
                ready = [asyncio.Event(), asyncio.Event()]
                release = [asyncio.Event(), asyncio.Event()]
                epochs = []

                async def frontend(index):
                    async with self.frontend(
                            None, "5s", env={"MPTASK_CONFIG": str(fixture.path)}) as session:
                        result = await self.raw_call(session, "mptask_health", {})
                        self.assertFalse(result.isError, wire(result))
                        epochs.append(result.structuredContent["epoch_id"])
                        ready[index].set()
                        await release[index].wait()
                        result = await self.raw_call(session, "mptask_health", {})
                        self.assertEqual(result.structuredContent["epoch_id"], epochs[0])

                async with asyncio.TaskGroup() as group:
                    first = group.create_task(frontend(0))
                    second = group.create_task(frontend(1))
                    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 10)
                    self.assertEqual(len(set(epochs)), 1)
                    self.assertEqual(len(list(fixture.config.runtime_dir.glob("launch-*.log"))), 1)
                    release[0].set()
                    await first
                    info = await asyncio.to_thread(discovery.connect, fixture.config)
                    self.assertEqual(info.instance_id, epochs[0])
                    release[1].set()
                    await second
                info = await asyncio.to_thread(discovery.connect, fixture.config)
                self.assertEqual(info.instance_id, epochs[0])
                with self.assertRaises(platform_support.PlatformError):
                    platform_support.LifetimeLock(fixture.config.runtime_dir / "authority.lock").acquire()

            try:
                asyncio.run(scenario())
            finally:
                try:
                    info = discovery.connect(fixture.config)
                except discovery.DiscoveryError as error:
                    if error.code != "registry_missing":
                        raise
                else:
                    launcher.stop(fixture.config, expected_instance_id=info.instance_id, timeout=5)

    def test_launcher_reuses_ready_owner_without_launching_another(self):
        with GatewayService() as server:
            server.configuration.document["lifecycle"] = "launcher"
            server.configuration.save()
            server.config = server.configuration.config
            discovery.publish_registry(server.config, server.identity)

            async def scenario():
                async with self.frontend(server.configuration.path) as session:
                    self.assertEqual((await session.list_tools()).nextCursor, "second")
                self.assertEqual(list(server.config.runtime_dir.glob("launch-*.log")), [])
                info = await asyncio.to_thread(discovery.connect, server.config)
                self.assertEqual(info.instance_id, server.identity.instance_id)
            asyncio.run(scenario())

    def test_real_stdio_http_pagination_descriptors_and_exact_results(self):
        with GatewayService() as server:
            async def scenario():
                async with self.frontend(server.configuration.path) as session:
                    try:
                        first = await session.list_tools()
                    except McpError as error:
                        self.fail(f"Catalog failed: {wire(error.error)}; requests={server.requests}")
                    second = await session.list_tools(
                        params=types.PaginatedRequestParams(cursor=first.nextCursor))
                    self.assertEqual(wire(first), {
                        "tools": [wire(server.tools[0])], "nextCursor": "second", "_meta": {"page": "first"}})
                    self.assertEqual(wire(second), {
                        "tools": [wire(server.tools[1])], "_meta": {"page": "second"}})
                    results = [
                        server.result,
                        types.CallToolResult(content=[types.TextContent(type="text", text="rejected")],
                                             structuredContent={"ok": False, "error": authority_error()},
                                             isError=True, _meta={"rejection": "definite"}),
                        types.CallToolResult(content=[], structuredContent=abandoned_receipt()),
                        types.CallToolResult(content=[], structuredContent=noncurrent_health()),
                    ]
                    arguments = {"actor": "harness-actor", "command_id": str(uuid4()),
                                 "expected_epoch": "stale-epoch-must-not-be-rewritten",
                                 "nested": {"a": [None, False, 0, "café"]}}
                    for expected in results:
                        server.result = expected
                        result = await self.raw_call(session, "mptask_future_write", arguments,
                                                     meta={"trace": "unchanged"})
                        self.assertEqual(wire(result), wire(expected))
                    self.assertEqual(server.calls, [
                        {"name": "mptask_future_write", "arguments": arguments,
                         "_meta": {"trace": "unchanged"}}] * len(results))
                    await self.raw_call(session, "mptask_future_write", {})
                    self.assertEqual(server.calls[-1], {"name": "mptask_future_write", "arguments": {}})
                    await self.raw_call(session, "mptask_future_write", None)
                    self.assertEqual(server.calls[-1], {"name": "mptask_future_write"})
                    server.tools[0].description = "Changed upstream descriptor"
                    self.assertEqual((await session.list_tools()).tools[0].description,
                                     "Changed upstream descriptor")
            asyncio.run(scenario())

    def test_real_authority_preserves_stale_epoch_error_and_health(self):
        with ServiceFixture() as service:
            async def scenario():
                async with self.frontend(service.config_path) as session:
                    result = await self.raw_call(session, "mptask_health", {})
                    self.assertFalse(result.isError, wire(result))
                    self.assertEqual(result.structuredContent["epoch_id"], service.epoch)
                    self.assertTrue(result.structuredContent["fresh"])
                    args = {"actor": "operator", "command_id": str(uuid4()),
                            "expected_epoch": str(uuid4()), "task_id": "absent", "expected_version": 1,
                            "tag": "note", "text": "Must not refresh a stale epoch"}
                    expected = await asyncio.to_thread(service.sdk, "mptask_note", args)
                    result = await self.raw_call(session, "mptask_note", args)
                    self.assertEqual(wire(result), wire(expected))
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "stale_epoch")
            asyncio.run(scenario())

    def call_cli(self, *args, env=None, input=""):
        return subprocess.run(
            [sys.executable, "-m", "mempalace_tasks", *args],
            input=input, capture_output=True, text=True, timeout=15,
            env={**os.environ, **(env or {})}, check=False)

    def test_cli_help_config_positions_environment_and_duration_validation(self):
        help_result = self.call_cli("mcp", "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--timeout", help_result.stdout)
        fixture = ConfigurationFixture(lifecycle="external")
        self.addCleanup(fixture.close)
        for args in [
            ["mcp", "--config", str(fixture.path)],
            ["--config", str(fixture.path), "mcp"],
            ["mcp"],
        ]:
            result = self.call_cli(*args, env={"MPTASK_CONFIG": str(fixture.path)})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("registry_missing", result.stderr)
        for duration in ["0s", "-1s", "nan", "301s", "1h"]:
            result = self.call_cli("mcp", "--config", str(fixture.path), "--timeout", duration)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
        self.assertFalse(fixture.config.runtime_dir.exists())

    def test_missing_invalid_config_and_external_mode_never_initialize_or_start(self):
        fixture = ConfigurationFixture(lifecycle="external")
        self.addCleanup(fixture.close)
        for path in [fixture.root / "missing.json", fixture.path]:
            if path == fixture.path:
                path.write_text('{"credential": "' + ROOT_TOKEN + '"}')
            result = self.call_cli("mcp", "--config", str(path))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertNotIn(ROOT_TOKEN, result.stderr)
            self.assertNotIn(str(fixture.root), result.stderr)
        self.assertFalse(fixture.config.runtime_dir.exists())

    def test_wrong_identity_and_token_fail_without_protocol_or_secret_output(self):
        fixture = ConfigurationFixture(lifecycle="external")
        self.addCleanup(fixture.close)
        server = IdentityServer(fixture.config)
        self.addCleanup(server.close)
        server.publish()
        for mode, token in [("wrong-instance", ROOT_TOKEN), ("normal", "wrong-credential")]:
            server.mode = mode
            fixture.config.service_token_file.write_text(token)
            result = self.call_cli("mcp", "--config", str(fixture.path))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("identity_mismatch", result.stderr)
            self.assertNotIn(token, result.stderr)
            self.assertFalse(any(row[0] == "POST" for row in server.requests))


if __name__ == "__main__":
    unittest.main()
