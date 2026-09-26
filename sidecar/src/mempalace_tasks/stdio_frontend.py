"""Tools-only SDK stdio gateway to one authenticated shared HTTP owner.

The connection snapshot is fixed for the entire frontend lifetime. Each
exchange uses an isolated SDK session against that snapshot: transport failures
cannot cancel unrelated stdio handlers or silently reconnect a mutation.
"""

import asyncio
import json
import os

import anyio
import httpx
from mcp import ClientSession, McpError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import discovery, launcher
from .client import TaskClientError, _READ_TOOLS, _map_error, _quiet_sdk_logs, _redact
from .config import load_config, validate_token
from .server_identity import InstanceIdentity


MAX_IN_FLIGHT = 32


def _failure(code, *, ambiguous=False):
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": "Task frontend exchange failed; inspect the command outcome before retrying",
            "details": {},
            "ambiguous": ambiguous,
        },
    }


class _Gateway:
    def __init__(self, connection, timeout, secrets):
        InstanceIdentity(connection.authority_id, connection.instance_id, connection.url)
        validate_token(connection.token)
        self.connection = connection
        self.timeout = timeout
        self.secrets = tuple(secret for secret in secrets if secret)
        self.slots = anyio.CapacityLimiter(MAX_IN_FLIGHT)
        self.failed = False
        self.server = Server("mempalace-tasks")
        # The convenience decorators validate/normalize arguments and may
        # refresh tool caches. A gateway instead preserves the typed wire model.
        self.server.request_handlers[types.ListToolsRequest] = self.list_tools
        self.server.request_handlers[types.CallToolRequest] = self.call_tool

    async def _check_response(self, response):
        # SDK 1.30 can follow same-origin redirects even when httpx forbids them.
        # Reject before the SDK's redirect/session-recovery handling runs.
        if not 200 <= response.status_code < 300:
            raise TaskClientError("http_error", "Unexpected upstream HTTP status")
        if "mcp-session-id" in response.headers:
            raise TaskClientError("invalid_response", "Expected stateless task HTTP")
        if response.status_code != 202 and (
                response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"):
            raise TaskClientError("invalid_response", "Expected task JSON transport")

    async def exchange(self, request, result_type):
        if self.failed:
            raise TaskClientError("connection_closed", "Restart frontend to reconnect")
        try:
            self.slots.acquire_nowait()
        except anyio.WouldBlock:
            raise TaskClientError("busy", "Frontend admission limit reached") from None
        dispatched = False
        incoming_error = None

        async def message_handler(message):
            nonlocal incoming_error
            if isinstance(message, Exception):
                incoming_error = _map_error(message)
                deadline.cancel()

        try:
            with anyio.fail_after(self.timeout) as deadline:
                async with httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + self.connection.token},
                    timeout=httpx.Timeout(self.timeout),
                    trust_env=False, follow_redirects=False,
                    event_hooks={"response": [self._check_response]},
                ) as http:
                    async with streamable_http_client(
                            self.connection.url, http_client=http) as (reader, writer, _):
                        async with ClientSession(
                                reader, writer, message_handler=message_handler) as session:
                            await session.initialize()
                            if self.failed:
                                raise TaskClientError("connection_closed", "Restart frontend to reconnect")
                            # Received SDK request models retain JSON-RPC
                            # envelope extras; the upstream session owns its IDs.
                            forwarded = type(request).model_validate(request.model_dump(
                                mode="json", by_alias=True, exclude={"jsonrpc", "id"}))
                            dispatched = True
                            # ClientSession.call_tool validates outputSchema and
                            # may fetch tools/list. send_request preserves error,
                            # abandoned, noncurrent and future result variants.
                            result = await session.send_request(
                                types.ClientRequest(forwarded), result_type)
                            document = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                            if any(_redact(document, secret) != document for secret in self.secrets):
                                raise TaskClientError("invalid_response", "Upstream reflected a credential")
            if incoming_error is not None:
                raise incoming_error
            return result
        except Exception as exc:
            self.failed = True
            error = _map_error(exc)
            raise TaskClientError(
                error.code, "Upstream exchange failed",
                ambiguous=(dispatched and isinstance(request, types.CallToolRequest)
                           and request.params.name not in _READ_TOOLS),
            ) from None
        finally:
            self.slots.release()

    async def list_tools(self, request):
        try:
            return types.ServerResult(await self.exchange(request, types.ListToolsResult))
        except Exception as error:
            safe = _map_error(error)
            raise McpError(types.ErrorData(
                code=-32603, message="Task frontend discovery failed",
                data=_failure(safe.code),
            )) from None

    async def call_tool(self, request):
        try:
            return types.ServerResult(await self.exchange(request, types.CallToolResult))
        except Exception as error:
            safe = _map_error(error)
            payload = _failure(safe.code, ambiguous=safe.ambiguous)
            return types.ServerResult(types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload, isError=True,
            ))

    async def run(self):
        with _quiet_sdk_logs():
            async with stdio_server() as (reader, writer):
                await self.server.run(reader, writer, self.server.create_initialization_options())


def main(config_path=None, *, timeout=10.0):
    """Run until stdio EOF; startup/exchanges are bounded, session lifetime is not.

    Configuration is explicit (or MPTASK_CONFIG). Only launcher lifecycle may
    start an owner; external lifecycle is connect-only. This function never
    initializes an authority, prints endpoint JSON, or stops the shared owner.
    The CLI owns safe stderr diagnostics and exit-code mapping.
    """
    discovery._Deadline(timeout)
    config_path = config_path or os.environ.get("MPTASK_CONFIG")
    config = load_config(config_path)
    connection = (launcher.start(config_path, timeout=timeout)
                  if config.lifecycle == "launcher"
                  else discovery.connect(config, timeout=timeout))
    return asyncio.run(_run(connection, timeout, (
        connection.token, config.service_token, config.hub_token)))


async def _run(connection, timeout, secrets):
    await _Gateway(connection, timeout, secrets).run()
    return 0
