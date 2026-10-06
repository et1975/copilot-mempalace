"""Sanctioned local MemPalace dispatch; never switch writers after uncertainty."""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager


class TransportError(RuntimeError):
    """A dispatch or tool failure, without proof that no effect occurred."""


class NotDispatchedError(TransportError):
    """Positive local proof that the requested handler/request was not sent."""


class UncertainWriteError(TransportError):
    """A request may have committed; reconcile rather than dispatch again."""

    uncertain = True


_EMBEDDED_LOCK = threading.RLock()
_CONTROL_TOOLS = frozenset({
    "mempalace_artifact_put", "mempalace_artifact_get",
    "mempalace_event_append", "mempalace_event_list",
})


@contextmanager
def mutation_lock(palace: str, *, timeout_seconds: float = 5):
    """Share the existing reentrant, directory-flock local mutation authority."""
    from dream_palace import palace_mutation_lock

    with palace_mutation_lock(palace, timeout_seconds=timeout_seconds):
        yield


def _result(value):
    if not isinstance(value, dict):
        raise TransportError("native tool returned a non-object response")
    if value.get("success") is False or value.get("error"):
        raise TransportError(f"native tool refused: {value.get('error', 'unsuccessful result')}")
    return value


def _embedded_call(palace, name, arguments):
    from dream_palace import _embedded_mcp_server

    with _EMBEDDED_LOCK:
        server = _embedded_mcp_server(palace)
        if os.path.realpath(server._config.palace_path) != os.path.realpath(palace):
            raise NotDispatchedError("embedded native palace binding mismatch")
        entry = server.TOOLS.get(name)
        if not entry or not callable(entry.get("handler")):
            raise NotDispatchedError(f"native tool capability unavailable: {name}")
        already_owned = server._MCP_WRITER_LOCK_CM is not None
        try:
            refusal = server._mcp_tool_preflight_refusal(None, name)
            if refusal is not None:
                raise NotDispatchedError(f"native mutation refused: {refusal.get('error', refusal)}")
            try:
                result = entry["handler"](**arguments)
            except NotDispatchedError as exc:
                raise TransportError("native handler failed after dispatch; no no-dispatch proof") from exc
            return _result(result)
        finally:
            if not already_owned:
                server._release_mcp_writer_lock()


def initialize_logstream(palace: str):
    """Explicit bootstrap using the native constructor and mutation preflight."""
    from dream_palace import _embedded_mcp_server

    with _EMBEDDED_LOCK:
        server = _embedded_mcp_server(palace)
        already_owned = server._MCP_WRITER_LOCK_CM is not None
        try:
            refusal = server._mcp_tool_preflight_refusal(None, "mempalace_event_append")
            if refusal is not None:
                raise NotDispatchedError("native initialization refused")
            initialize = getattr(server, "_get_logstream", None)
            if not callable(initialize):
                raise NotDispatchedError("native logstream initialization capability unavailable")
            initialize()
        finally:
            if not already_owned:
                server._release_mcp_writer_lock()


def _hub_info(palace):
    from mempalace import server_registry

    info = server_registry.read_live_serverinfo(palace)
    if info is None:
        return None
    if os.path.realpath(info.get("palace_path", "")) != os.path.realpath(palace):
        raise NotDispatchedError("discovered hub is not bound to the same palace")
    if info.get("read_only"):
        raise NotDispatchedError("discovered palace hub is read-only")
    url = urllib.parse.urlsplit(server_registry.client_base_url(info))
    try:
        local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
    except (ValueError, TypeError):
        local = False
    if not local or url.scheme not in {"http", "https"} or url.username or url.password:
        raise NotDispatchedError("only an authenticated discovered local palace hub is supported")
    return info


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise TransportError("palace hub redirects are not permitted")


def _hub_call(palace, info, name, arguments):
    from mempalace import server_registry

    token = server_registry.load_server_token(palace)
    if not token:
        raise NotDispatchedError("discovered palace hub has no authentication token")
    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    request = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}},
                        ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers=headers,
    )
    try:
        # Disable redirects so the local bearer token cannot follow an external URL.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("id") != 1:
            raise ValueError("unrecognized JSON-RPC response")
        if payload.get("error"):
            raise TransportError("palace hub explicitly refused the tool request")
        result = payload["result"]
        if result.get("isError"):
            raise TransportError("palace hub reported a tool error")
        contents = result["content"]
        if len(contents) != 1 or contents[0].get("type") != "text":
            raise ValueError("unrecognized tool response")
        tool_result = json.loads(contents[0]["text"])
        if not isinstance(tool_result, dict):
            raise ValueError("unrecognized native tool result")
        return _result(tool_result)
    except TransportError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise UncertainWriteError(
            "palace hub reply uncertain; reconcile the original write, no alternate writer"
        ) from exc


def call_tool(palace: str, name: str, arguments: dict, *, vector: bool = False) -> dict:
    """Control writes stay synchronous; vector writes use the discovered owner."""
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments must be an object")
    palace = os.path.realpath(os.path.expanduser(palace))
    if name in _CONTROL_TOOLS:
        return _embedded_call(palace, name, arguments)
    if not vector:
        raise NotDispatchedError("non-control tool requires explicit vector dispatch")
    info = _hub_info(palace)
    if info is not None:
        return _hub_call(palace, info, name, arguments)
    return _embedded_call(palace, name, arguments)
