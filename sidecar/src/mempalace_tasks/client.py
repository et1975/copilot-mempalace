"""Synchronous, one-shot client for the task sidecar's SDK MCP endpoint."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from ipaddress import ip_address
import json
import logging
import math
import re
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import httpx
from mcp import ClientSession, McpError, types
from mcp.client.streamable_http import streamable_http_client


_READ_TOOLS = frozenset({
    "mptask_snapshot", "mptask_get", "mptask_list", "mptask_ready",
    "mptask_history", "mptask_health", "mptask_wait_ready",
})
_CLIENT_IO = ContextVar("task_client_io", default=False)


class TaskClientError(Exception):
    """A safe caller-facing failure; ambiguous mutations must not be retried blindly."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.ambiguous = ambiguous
        self.details = {} if details is None else details


class _TypedToolError(TaskClientError):
    """A validated authority error whose explicit ambiguity is authoritative."""


class _QuietClientIO(logging.Filter):
    def filter(self, record):
        return not _CLIENT_IO.get() or not (
            record.name in ("root", "mcp")
            or record.name.startswith(("mcp.", "httpx", "httpcore"))
        )


@contextmanager
def _quiet_sdk_logs():
    # SDK debug/error logs can contain entire messages, including reflected secrets.
    # Context-local filtering leaves unrelated threads and library users alone.
    active = _CLIENT_IO.set(True)
    log_filter = _QuietClientIO()
    loggers = [
        logger for logger in list(logging.Logger.manager.loggerDict.values())
        if isinstance(logger, logging.Logger)
        and (logger.name == "mcp" or logger.name.startswith(("mcp.", "httpx", "httpcore")))
    ]
    loggers.append(logging.getLogger())
    targets = set(loggers)
    targets.update(handler for logger in loggers for handler in logger.handlers)
    for target in targets:
        target.addFilter(log_filter)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(log_filter)
        _CLIENT_IO.reset(active)


def _json_value(value):
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _json_value(child)
    elif type(value) is list:
        for child in value:
            _json_value(child)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError("Not a JSON value")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _encode(value):
    _json_value(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _redact(value, token):
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, dict):
        return {_redact(key, token): _redact(child, token) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, token) for child in value]
    return value


def _valid_abandonment(payload, requested_command_id):
    if set(payload) != {
        "ok", "outcome", "command_id", "event_id", "ordinal", "replayed",
        "response", "tasks", "authorization",
    }:
        return False
    command_id = payload["command_id"]
    event_id = payload["event_id"]
    authorization = payload["authorization"]
    if not (
        payload["ok"] is False
        and type(command_id) is str and command_id == requested_command_id
        and type(event_id) is str and bool(event_id.strip())
        and all(ord(char) >= 32 and ord(char) != 127 for char in event_id)
        and payload["ordinal"] is None and payload["response"] is None
        and type(payload["replayed"]) is bool
        and type(payload["tasks"]) is list and not payload["tasks"]
        and type(authorization) is dict and set(authorization) == {"as_of", "fresh", "tasks"}
        and type(authorization["fresh"]) is bool
        and type(authorization["tasks"]) is list and not authorization["tasks"]
        and type(authorization["as_of"]) is str
        and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)",
            authorization["as_of"],
        ) is not None
    ):
        return False
    try:
        datetime.fromisoformat(authorization["as_of"])
        return str(UUID(command_id)) == command_id
    except ValueError:
        return False


def _valid_read_diagnostic(payload):
    required = {
        "schema_version", "authority_id", "as_of", "last_verified_at",
        "raw_cursor", "domain_head", "domain_ordinal", "fresh", "reason",
    }
    if not required <= payload.keys() or not (
        type(payload["schema_version"]) is int and payload["schema_version"] == 1
        and type(payload["authority_id"]) is str
        and payload["fresh"] is False
        and type(payload["reason"]) is str and bool(payload["reason"].strip())
        and type(payload["domain_ordinal"]) is int and payload["domain_ordinal"] >= 0
        and ("ok" not in payload or type(payload["ok"]) is bool)
    ):
        return False
    for key in ("raw_cursor", "domain_head"):
        if payload[key] is not None and (
            type(payload[key]) is not str or not payload[key].strip()
        ):
            return False
    try:
        if str(UUID(payload["authority_id"])) != payload["authority_id"]:
            return False
        for key in ("as_of", "last_verified_at"):
            value = payload[key]
            if value is not None:
                if type(value) is not str or re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value,
                ) is None:
                    return False
                datetime.fromisoformat(value)
    except ValueError:
        return False
    error = payload.get("error")
    if error is not None and not (
        type(error) is dict and type(error.get("code")) is str and bool(error["code"].strip())
    ):
        return False
    return (
        ("started" not in payload or type(payload["started"]) is bool)
        and (payload.get("pending_reboot") is None or type(payload["pending_reboot"]) is bool)
        and ("protocol" not in payload or type(payload["protocol"]) is dict)
        and (payload.get("pending_command") is None or type(payload["pending_command"]) is dict)
    )


def _decode_result(result, token, requested_command_id, name):
    try:
        structured = result.structuredContent
        text = None
        if result.content:
            if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
                raise ValueError("Expected one JSON text block")
            text = json.loads(result.content[0].text, object_pairs_hook=_unique_object)
            if type(text) is not dict:
                raise ValueError("Expected a JSON object")
            _json_value(text)
        if structured is not None:
            _json_value(structured)
            if text is not None and _encode(structured) != _encode(text):
                raise ValueError("Conflicting structured and text results")
            payload = structured
        else:
            payload = text
        if type(payload) is not dict:
            raise ValueError("Missing result object")
    except (ValueError, TypeError, RecursionError):
        if result.isError and result.structuredContent is None:
            raise TaskClientError("tool_error", "Task service tool failed") from None
        raise TaskClientError("invalid_response", "Task service returned an invalid result") from None

    diagnostic = name in _READ_TOOLS and not result.isError and _valid_read_diagnostic(payload)
    if payload.get("outcome") == "abandoned":
        if result.isError or not _valid_abandonment(payload, requested_command_id):
            raise TaskClientError("invalid_response", "Task service returned an invalid abandoned receipt")
    elif not diagnostic and (
        result.isError or payload.get("ok") is False or payload.get("error") is not None
    ):
        error = payload.get("error")
        if not isinstance(error, dict):
            error = payload
        code = error.get("code")
        message = error.get("message")
        if (
            set(error) == {"code", "message", "details", "ambiguous"}
            and type(code) is str and bool(code.strip())
            and type(message) is str and bool(message.strip())
            and type(error["details"]) is dict and type(error["ambiguous"]) is bool
            and (
                (set(payload) == {"ok", "error"} and payload["ok"] is False)
                or (result.isError and (error is payload or set(payload) == {"error"}))
            )
        ):
            raise _TypedToolError(
                code, message, ambiguous=error["ambiguous"], details=error["details"],
            )
        raise TaskClientError(
            code if isinstance(code, str) and code else "tool_error",
            message if isinstance(message, str) and message else "Task service tool failed",
            details=error.get("details", {}),
        )
    if _redact(payload, token) != payload:
        raise TaskClientError("invalid_response", "Task service reflected credentials in its result")
    return payload


def _exceptions(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _exceptions(child)]
    return [error]


def _map_error(error):
    errors = _exceptions(error)
    for candidate in errors:
        if isinstance(candidate, TaskClientError):
            return candidate
    if any(isinstance(candidate, (TimeoutError, httpx.TimeoutException)) for candidate in errors):
        return TaskClientError("timeout", "Task service operation exceeded its deadline")
    for candidate in errors:
        if isinstance(candidate, httpx.HTTPStatusError):
            return TaskClientError(
                "http_error", "Task service HTTP request failed",
                details={"status_code": candidate.response.status_code},
            )
    if any(isinstance(candidate, (httpx.RequestError, anyio.BrokenResourceError, anyio.EndOfStream))
           for candidate in errors):
        return TaskClientError("transport_error", "Could not communicate with the task service")
    for candidate in errors:
        if isinstance(candidate, McpError):
            if candidate.error.code == httpx.codes.REQUEST_TIMEOUT:
                return TaskClientError("timeout", "Task service operation exceeded its deadline")
            return TaskClientError(
                "rpc_error", "Task service MCP request failed",
                details={"rpc_code": candidate.error.code},
            )
    return TaskClientError("invalid_response", "Task service returned an invalid MCP response")


class TaskServiceClient:
    """One-shot SDK client for the sidecar's stateless JSON Streamable HTTP profile.

    Only numeric loopback endpoints are supported: no DNS, environment proxies,
    redirects, reconnects, writer startup, or automatic command retries. Call from
    synchronous code, not an already-running event loop. ``timeout`` bounds the
    handshake, discovery, call, and managed SDK/HTTP cleanup together.
    """

    def __init__(self, url: str, *, token: str, timeout: float = 10.0):
        try:
            if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
                raise ValueError
            if any(c in url for c in ("@", "?", "#", "\\")):
                raise ValueError
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError
            if "%" in parsed.netloc or not ip_address(parsed.hostname).is_loopback:
                raise ValueError
            if parsed.netloc.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535):
                raise ValueError
            endpoint = httpx.URL(url)
            if endpoint.raw_path.decode("ascii") != (parsed.path or "/"):
                raise ValueError
        except (ValueError, TypeError, httpx.InvalidURL):
            raise TaskClientError(
                "invalid_configuration", "Expected an exact HTTP(S) numeric loopback endpoint without credentials, query, or fragment",
            ) from None
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None:
            raise TaskClientError("invalid_configuration", "Expected a nonempty bearer token without header delimiters")
        try:
            if type(timeout) not in (int, float):
                raise ValueError
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
        except (ValueError, OverflowError):
            raise TaskClientError("invalid_configuration", "Timeout must be a positive finite number") from None
        self.url = url
        self._token = token
        self.timeout = timeout

    def call_tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or re.fullmatch(r"mptask_[a-z][a-z0-9_]*", name) is None:
            raise TaskClientError("unknown_tool", "Expected an advertised mptask_ tool")
        try:
            if type(arguments) is not dict:
                raise ValueError
            arguments = json.loads(_encode(arguments))
        except (ValueError, TypeError, RecursionError):
            raise TaskClientError("invalid_arguments", "Tool arguments must be a JSON object") from None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise TaskClientError("invalid_configuration", "The synchronous task client cannot run inside an event loop")
        with _quiet_sdk_logs():
            return asyncio.run(self._call_tool(name, arguments))

    async def _call_tool(self, name, arguments):
        dispatched = False
        incoming_error = None

        async def message_handler(message):
            nonlocal incoming_error
            if isinstance(message, Exception):
                # The SDK's default handler ignores parse errors and waits for timeout.
                incoming_error = _map_error(message)
                deadline.cancel()

        async def check_response(response):
            # SDK 1.30 follows same-origin redirects even with follow_redirects=False.
            # Reject them before its redirect/reconnection handling sees the response.
            if not 200 <= response.status_code < 300:
                raise TaskClientError(
                    "http_error", "Task service HTTP request failed",
                    details={"status_code": response.status_code},
                )
            if "mcp-session-id" in response.headers:
                raise TaskClientError("invalid_response", "Expected the stateless task service transport")
            if response.status_code != 202:
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise TaskClientError("invalid_response", "Expected the task service JSON transport")

        try:
            with anyio.fail_after(self.timeout) as deadline:
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=httpx.Timeout(self.timeout),
                    trust_env=False,
                    follow_redirects=False,
                    event_hooks={"response": [check_response]},
                ) as http_client:
                    async with streamable_http_client(
                        self.url, http_client=http_client,
                    ) as (read, write, _):
                        async with ClientSession(
                            read, write, message_handler=message_handler,
                        ) as session:
                            await session.initialize()
                            cursor = None
                            cursors = set()
                            for _ in range(64):
                                tools = await session.list_tools(
                                    params=types.PaginatedRequestParams(cursor=cursor) if cursor else None,
                                )
                                if any(tool.name == name for tool in tools.tools):
                                    break
                                if not tools.nextCursor:
                                    raise TaskClientError("unknown_tool", "Task service does not advertise this tool")
                                if tools.nextCursor in cursors:
                                    raise TaskClientError("invalid_response", "Task service repeated a discovery cursor")
                                cursor = tools.nextCursor
                                cursors.add(cursor)
                            else:
                                raise TaskClientError("invalid_response", "Task service discovery exceeded its page limit")
                            dispatched = True
                            result = await session.call_tool(name, arguments)
                            payload = _decode_result(result, self._token, arguments.get("command_id"), name)
            if incoming_error is not None:
                raise incoming_error
            return payload
        except Exception as exc:
            error = _map_error(exc)
            raise TaskClientError(
                _redact(error.code, self._token),
                _redact(error.message, self._token),
                ambiguous=(
                    error.ambiguous if isinstance(error, _TypedToolError)
                    else dispatched and name not in _READ_TOOLS
                ),
                details=_redact(error.details, self._token),
            ) from None
