"""Fail-closed, standard-library HTTP adapter for one MemPalace task stream.

``base_url`` is the exact JSON-RPC endpoint (including its path). No hub
discovery, redirects, proxies, local writer, or mutation retries are used.

``append_event`` accepts a JSON object with ``record_type`` (mptask.command or
mptask.settle), ``authority_id`` and ``command_id``. The complete object becomes
canonical JSON in the body. The protocol layer, not this adapter, validates
domain semantics, proposal hashes and command-chain progression.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import dataclass, field
import http.client
import json
import math
import socket
import threading
import time
from urllib.parse import urlsplit


_APPEND = "mempalace_event_append"
_LIST = "mempalace_event_list"
_WRITER = "mempalace-tasks"
_KINDS = ("mptask.command", "mptask.settle")
_PAGE_SIZE = 500
_BODY_LIMIT = 240 * 1024
_RESPONSE_LIMIT = 160 * 1024 * 1024
_LEGACY_PROFILE = "mempalace-legacy-append-order-v1"
_ORDERED_PROFILE = "mempalace-ordered-v1"
_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
_LIST_TYPES = {
    "stream": "string", "room": "string", "type": "string",
    "to_agent": "string", "from_agent": "string", "correlation_id": "string",
    "status": "string", "since_event_id": "string", "since_created_at": "string",
    "limit": "integer", "preview": "boolean",
}
_LEGACY_DESCRIPTION = (
    "List agent-coordination events with structured filters, oldest first "
    "(append order, not timestamp order). Use since_event_id as the resume "
    "cursor: it means strictly after that event in append order, so it cannot "
    "skip anything. Do NOT resume with since_created_at \u2014 a peer's event syncs "
    "in whenever it arrives, so it can already be older than a timestamp cursor "
    "and be missed permanently; since_created_at is a time window "
    "('what happened today'), not a cursor. Store the id of the last event you "
    "processed \u2014 that is your whole watcher state. Pass preview=true when "
    "sweeping a busy stream. to_agent=<you> also matches '*' broadcasts, so no "
    "second call is needed. To wait for something that has not happened yet, "
    "use mempalace_event_wait instead of polling this."
)


class PalaceError(Exception):
    """Stable error code and redacted message; ambiguous means a write may exist."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.ambiguous = ambiguous


def _fail(code: str, message: str) -> None:
    raise PalaceError(code, message)


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _decode(raw: str | bytes, code: str = "invalid_response") -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_no_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PalaceError(code, "Expected a complete, unambiguous JSON object") from None
    if not isinstance(value, dict):
        _fail(code, "Expected a JSON object")
    return value


def _json_value(value: object, *, floats: bool = True, depth: int = 0) -> None:
    if depth > 64:
        _fail("invalid_argument", "JSON nesting exceeds the supported depth")
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and floats and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, floats=floats, depth=depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, floats=floats, depth=depth + 1)
        return
    _fail("invalid_argument", "Value is not supported strict JSON")


def _encode(value: dict) -> bytes:
    _json_value(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise PalaceError("invalid_argument", "Value cannot be encoded as JSON") from None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _schema(schema: object) -> tuple[dict, list]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        _fail("unsupported_schema", "Tool input schema must be an object schema")
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (not isinstance(properties, dict) or not isinstance(required, list)
            or any(not isinstance(key, str) or key not in properties for key in required)
            or len(required) != len(set(required))
            or any(not isinstance(item, dict) for item in properties.values())):
        _fail("unsupported_schema", "Tool properties or required fields are malformed")
    return properties, required


def _validate(value: object, schema: dict) -> None:
    """Validate the simple JSON Schema vocabulary used by negotiated MCP tools."""
    if not isinstance(schema, dict):
        _fail("unsupported_schema", "Tool field schema must be an object")
    supported = {
        "type", "properties", "required", "additionalProperties", "items", "enum",
        "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
        "description", "title", "default", "examples", "$schema",
    }
    if set(schema) - supported:
        _fail("unsupported_schema", "Tool schema uses unsupported validation keywords")
    kind = schema.get("type")
    types = {
        "string": (str,), "integer": (int,), "number": (int, float),
        "boolean": (bool,), "object": (dict,), "array": (list,), "null": (type(None),),
    }
    if not isinstance(kind, str) or kind not in types:
        _fail("unsupported_schema", "Tool schema has an unsupported type")
    if type(value) not in types[kind]:
        _fail("invalid_argument", "Tool argument has the wrong JSON type")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            _fail("unsupported_schema", "Tool enum must be an array")
        if not any(type(value) is type(choice) and value == choice for choice in choices):
            _fail("invalid_argument", "Tool argument is outside its advertised enum")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or type(additional) not in (bool, dict)):
            _fail("unsupported_schema", "Object schema is malformed")
        if not set(required) <= set(value):
            _fail("invalid_argument", "Required tool argument is missing")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
            elif schema.get("additionalProperties", True) is False:
                _fail("invalid_argument", "Unknown tool argument")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate(item, schema["additionalProperties"])
    if kind == "array":
        if "items" not in schema or not isinstance(schema["items"], dict):
            _fail("unsupported_schema", "Array schema must declare its items")
        for item in value:
            _validate(item, schema["items"])
    bounds = {
        "string": ("minLength", "maxLength"),
        "array": ("minItems", "maxItems"),
        "integer": ("minimum", "maximum"),
        "number": ("minimum", "maximum"),
    }
    if kind in bounds:
        lower, upper = bounds[kind]
        measured = len(value) if kind in ("string", "array") else value
        for name in (lower, upper):
            if name in schema and type(schema[name]) not in (int, float):
                _fail("unsupported_schema", "Tool bound must be numeric")
        if ((lower in schema and measured < schema[lower])
                or (upper in schema and measured > schema[upper])):
            _fail("invalid_argument", "Tool argument exceeds its advertised bounds")


def _upstream_failure(payload: object) -> None:
    # Classify known diagnostics without including upstream text (which may echo
    # authorization headers, URLs, event bodies or other private data).
    text = json.dumps(payload, ensure_ascii=True).lower()
    if (("cursor" in text or "since_event_id" in text)
            and any(word in text for word in ("unknown", "not found", "does not exist"))):
        _fail("unknown_cursor", "Upstream does not recognize the event cursor")
    if "order" in text and any(word in text for word in ("invalid", "unsupported")):
        _fail("invalid_order", "Upstream rejected the required replay order")
    if "truncat" in text:
        _fail("truncated_body", "Upstream reported incomplete event content")
    _fail("upstream_error", "Upstream tool reported failure")


def _unwrap(result: dict) -> dict:
    if type(result.get("isError", False)) is not bool:
        _fail("invalid_response", "MCP isError must be a boolean")
    if result.get("isError"):
        _upstream_failure(result)
    content = result.get("content", [])
    if not isinstance(content, list):
        _fail("invalid_response", "MCP content must be an array")
    text_result = None
    if content:
        if (len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            _fail("invalid_response", "Expected one JSON text content block")
        text_result = _decode(content[0]["text"])
    structured = result.get("structuredContent")
    if structured is not None and not isinstance(structured, dict):
        _fail("invalid_response", "MCP structured content must be an object")
    if structured is not None and text_result is not None and structured != text_result:
        _fail("invalid_response", "MCP content representations disagree")
    payload = structured if structured is not None else text_result
    if payload is None:
        _fail("invalid_response", "MCP result has no JSON content")
    for _ in range(16):
        if ("success" in payload and type(payload["success"]) is not bool):
            _fail("invalid_response", "Tool success flag must be a boolean")
        if payload.get("success") is False or payload.get("error") is not None:
            _upstream_failure(payload)
        wrappers = [key for key in ("result", "data") if key in payload]
        if not wrappers:
            return payload
        if len(wrappers) != 1 or not set(payload) <= {wrappers[0], "success"}:
            _fail("invalid_response", "Ambiguous nested tool result")
        payload = payload[wrappers[0]]
        if not isinstance(payload, dict):
            _fail("invalid_response", "Nested tool result must be an object")
    _fail("invalid_response", "Tool result nesting exceeds the supported depth")


@dataclass
class _Exchange:
    deadline: float
    aborted: threading.Event = field(default_factory=threading.Event)
    connection: http.client.HTTPConnection | None = None
    connected_socket: socket.socket | None = None
    mutation_attempted: bool = False

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if self.aborted.is_set() or remaining <= 0:
            raise PalaceError("timeout", "Upstream operation exceeded its deadline",
                              ambiguous=self.mutation_attempted)
        return remaining


class PalaceClient:
    """One configured hub and reserved stream; one in-flight operation per client.

    The timeout covers the whole public operation, including lazy discovery.
    Replay applies that bound separately to each page, and ends only on an empty
    page. Each failed append has zero automatic retries.

    A single daemon I/O worker bounds even an OS resolver stall from the caller's
    perspective. Timed-out sockets are shut down. If an uncancellable resolver
    remains stuck, this client refuses further operations rather than spawning
    unbounded workers; the worker checks the deadline before sending any bytes.
    """

    def __init__(self, base_url: str, stream: str, *, token: str | None = None,
                 timeout: float = 10.0, legacy_profile: str | None = None):
        try:
            if not isinstance(base_url, str):
                raise ValueError
            url = urlsplit(base_url)
            port = url.port
            if (url.scheme not in ("http", "https") or not url.hostname
                    or url.username is not None or url.password is not None
                    or url.query or url.fragment or any(ord(c) < 33 for c in base_url)):
                raise ValueError
            if (not _nonempty(stream) or not stream.startswith("mptask/")
                    or not stream.removeprefix("mptask/") or len(stream) > 256):
                raise ValueError
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
            if token is not None and (
                not isinstance(token, str) or not token
                or any(ord(c) < 33 or ord(c) > 126 for c in token)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise PalaceError("invalid_configuration", "Invalid hub configuration") from None
        self.stream = stream
        self._host = url.hostname
        self._port = port
        self._path = url.path or "/"
        self._https = url.scheme == "https"
        self._token = token
        self._timeout = float(timeout)
        self._legacy_profile = legacy_profile
        self._busy = threading.Lock()
        self._initialized = False
        self._protocol_version = None
        self._session_id = None
        self._server_info = None
        self._descriptor = None
        self._request_id = 0

    def _run(self, action: Callable[[_Exchange], object]):
        if not self._busy.acquire(blocking=False):
            _fail("upstream_busy", "A previous upstream operation has not finished")
        exchange = _Exchange(time.monotonic() + self._timeout)
        future = Future()

        def work():
            try:
                result = action(exchange)
                exchange.remaining()
            except BaseException as error:
                # Transfer exceptions, including programming errors, unchanged.
                self._busy.release()
                future.set_exception(error)
            else:
                self._busy.release()
                future.set_result(result)

        threading.Thread(target=work, name="mempalace-http", daemon=True).start()
        try:
            return future.result(timeout=max(0, exchange.deadline - time.monotonic()))
        except FutureTimeout:
            exchange.aborted.set()
            connection = exchange.connection
            active_socket = exchange.connected_socket
            if active_socket is None and connection is not None:
                active_socket = connection.sock
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    # The worker may already have closed the timed-out socket.
                    pass
            raise PalaceError("timeout", "Upstream operation exceeded its deadline",
                              ambiguous=exchange.mutation_attempted) from None
        except PalaceError as error:
            if exchange.mutation_attempted:
                error.ambiguous = True
            raise

    def _rpc(self, exchange: _Exchange, method: str, params: dict, *,
             mutation: bool = False, notification: bool = False) -> dict:
        self._request_id += 1
        request_id = self._request_id
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            request["id"] = request_id
        encoded = _encode(request)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        if self._protocol_version is not None:
            headers["Mcp-Protocol-Version"] = self._protocol_version
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        connection_type = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        connection = connection_type(self._host, self._port, timeout=exchange.remaining())
        exchange.connection = connection
        response = None
        try:
            connection.connect()
            # getresponse() may detach this socket from HTTPConnection when the
            # response owns a Connection: close / HTTP/1.0 body stream.
            exchange.connected_socket = connection.sock
            remaining = exchange.remaining()
            connection.sock.settimeout(remaining)
            if mutation:
                exchange.mutation_attempted = True
            connection.request("POST", self._path, body=encoded, headers=headers)
            response = connection.getresponse()
            if response.status not in ((200, 202, 204) if notification else (200,)):
                _fail("http_error", f"Upstream HTTP request failed with status {response.status}")
            length = response.getheader("Content-Length")
            content_length = None
            if length is not None:
                if not length.isascii() or not length.isdigit():
                    _fail("invalid_response", "Invalid upstream Content-Length")
                normalized_length = length.lstrip("0") or "0"
                limit = str(_RESPONSE_LIMIT)
                if (len(normalized_length) > len(limit)
                        or (len(normalized_length) == len(limit) and normalized_length > limit)):
                    _fail("body_too_large", "Upstream HTTP body exceeds the response limit")
                content_length = int(normalized_length)
            if response.getheader("Content-Encoding", "identity") != "identity":
                _fail("invalid_response", "Unsupported upstream HTTP content encoding")
            body = response.read(_RESPONSE_LIMIT + 1)
            if len(body) > _RESPONSE_LIMIT:
                _fail("body_too_large", "Upstream HTTP body exceeds the response limit")
            if content_length is not None and len(body) != content_length:
                _fail("invalid_response", "Upstream HTTP body is incomplete")
            exchange.remaining()
            if notification and not body:
                return {}
            media_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            if media_type != "application/json":
                _fail("invalid_response", "Expected an upstream JSON HTTP response")
            data = _decode(body)
            if (data.get("jsonrpc") != "2.0" or type(data.get("id")) is not int
                    or data["id"] != request_id or ("result" in data) == ("error" in data)):
                _fail("invalid_response", "Invalid JSON-RPC response envelope")
            if "error" in data:
                _fail("rpc_error", "Upstream JSON-RPC request failed")
            if not isinstance(data["result"], dict):
                _fail("invalid_response", "JSON-RPC result must be an object")
            if method == "initialize":
                session_id = response.getheader("Mcp-Session-Id")
                if session_id is not None:
                    if not session_id or any(ord(c) < 33 or ord(c) > 126 for c in session_id):
                        _fail("invalid_response", "Invalid MCP session header")
                    self._session_id = session_id
            return data["result"]
        except (TimeoutError, socket.timeout):
            raise PalaceError("timeout", "Upstream operation exceeded its deadline") from None
        except http.client.IncompleteRead:
            raise PalaceError("invalid_response", "Upstream HTTP body is incomplete") from None
        except (OSError, http.client.HTTPException):
            raise PalaceError("transport_error", "Upstream HTTP transport failed") from None
        finally:
            try:
                try:
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
            except OSError:
                raise PalaceError("transport_error", "Upstream HTTP cleanup failed") from None
            finally:
                exchange.connection = None
                exchange.connected_socket = None

    def discover(self) -> dict:
        """Fetch tools/list anew and return a detached negotiated descriptor."""
        return self._run(self._discover)

    def _discover(self, exchange: _Exchange) -> dict:
        self._descriptor = None
        if not self._initialized:
            initialized = self._rpc(exchange, "initialize", {
                "protocolVersion": _PROTOCOL_VERSIONS[0], "capabilities": {},
                "clientInfo": {"name": "mempalace-tasks", "version": "1"},
            })
            version = initialized.get("protocolVersion")
            info = initialized.get("serverInfo")
            capabilities = initialized.get("capabilities")
            if (version not in _PROTOCOL_VERSIONS or not isinstance(info, dict)
                    or info.get("name") not in ("mempalace", "mempalace-mcp")
                    or not _nonempty(info.get("version"))
                    or not isinstance(capabilities, dict)
                    or not isinstance(capabilities.get("tools"), dict)):
                _fail("unsupported_profile", "Unsupported MCP initialization contract")
            self._protocol_version = version
            self._server_info = info
            self._rpc(exchange, "notifications/initialized", {}, notification=True)
            self._initialized = True
        tools = {}
        params = {}
        cursors = set()
        for _ in range(64):
            page = self._rpc(exchange, "tools/list", params)
            if not isinstance(page.get("tools"), list):
                _fail("unsupported_schema", "MCP tools/list must return a tool array")
            for tool in page["tools"]:
                if (not isinstance(tool, dict) or not _nonempty(tool.get("name"))
                        or tool["name"] in tools):
                    _fail("unsupported_schema", "MCP tool names must be unique nonempty strings")
                _schema(tool.get("inputSchema"))
                tools[tool["name"]] = tool
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not _nonempty(cursor) or cursor in cursors:
                _fail("unsupported_schema", "Invalid tools/list pagination cursor")
            cursors.add(cursor)
            params = {"cursor": cursor}
        else:
            _fail("unsupported_schema", "Too many tools/list pages")
        profile = self._negotiate(tools)
        descriptor = {
            "profile": profile, "protocol_version": self._protocol_version,
            "server_info": self._server_info, "tools": tools,
        }
        exchange.remaining()
        self._descriptor = descriptor
        return deepcopy(descriptor)

    def _negotiate(self, tools: dict) -> str:
        if _APPEND not in tools or _LIST not in tools:
            _fail("unsupported_schema", "Required MemPalace event tools are missing")
        append_properties, append_required = _schema(tools[_APPEND]["inputSchema"])
        expected = {
            "stream": "string", "room": "string", "type": "string",
            "from_agent": "string", "body": "string", "metadata": "object",
            "correlation_id": "string",
        }
        if (any(append_properties.get(key, {}).get("type") != kind
                for key, kind in expected.items())
                or not set(append_required) <= set(expected)):
            _fail("unsupported_schema", "Required append fields are not supported")
        properties, required = _schema(tools[_LIST]["inputSchema"])
        if (any(properties.get(key, {}).get("type") != kind
                for key, kind in _LIST_TYPES.items())
                or not set(required) <= {"stream", "limit", "preview", "order"}):
            _fail("unsupported_schema", "Required list fields are not supported")
        if self._legacy_profile not in (None, _LEGACY_PROFILE):
            _fail("unsupported_profile", "Unknown configured legacy profile")
        if "order" in properties:
            expected_ordered = {**_LIST_TYPES, "topic": "string",
                                "before_event_id": "string", "order": "string"}
            if set(properties) != set(expected_ordered):
                _fail("unsupported_profile", "Unknown ordered event-list profile")
            orders = properties["order"].get("enum")
            if (any(properties[key].get("type") != kind
                    for key, kind in expected_ordered.items())
                    or not isinstance(orders, list)
                    or any(not isinstance(order, str) for order in orders)
                    or set(orders) != {"asc", "desc"}):
                _fail("unsupported_schema", "Required ascending order is not supported")
            profile = _ORDERED_PROFILE
        else:
            description = tools[_LIST].get("description", "")
            if (set(properties) != set(_LIST_TYPES) or required
                    or not isinstance(description, str)
                    or " ".join(description.split()) != " ".join(_LEGACY_DESCRIPTION.split())):
                _fail("unsupported_profile", "Unknown legacy event-list contract")
            profile = _LEGACY_PROFILE
        sample_list = {"stream": self.stream, "limit": _PAGE_SIZE, "preview": False,
                       "since_event_id": "probe", "correlation_id": "probe"}
        if profile == _ORDERED_PROFILE:
            sample_list["order"] = "asc"
        try:
            _validate(sample_list, tools[_LIST]["inputSchema"])
            _validate("x" * _BODY_LIMIT, append_properties["body"])
            for kind in _KINDS:
                _validate({
                    "stream": self.stream, "room": "tasks", "type": kind,
                    "from_agent": _WRITER, "body": "{}",
                    "metadata": {
                        "authority_id": self.stream.removeprefix("mptask/"),
                        "command_id": "probe",
                    },
                    "correlation_id": "probe",
                }, tools[_APPEND]["inputSchema"])
        except PalaceError:
            raise PalaceError("unsupported_schema", "Required tool arguments are unsupported") from None
        return profile

    def _call(self, exchange: _Exchange, name: str, arguments: dict) -> dict:
        if self._descriptor is None:
            self._discover(exchange)
        if not isinstance(name, str) or name not in self._descriptor["tools"]:
            _fail("unsupported_tool", "Tool was not advertised by this hub")
        if not isinstance(arguments, dict):
            _fail("invalid_argument", "Tool arguments must be a JSON object")
        _json_value(arguments)
        schema = self._descriptor["tools"][name]["inputSchema"]
        properties, _ = _schema(schema)
        if set(arguments) - set(properties):
            _fail("invalid_argument", "Unknown tool argument")
        _validate(arguments, schema)
        result = self._rpc(exchange, "tools/call", {"name": name, "arguments": arguments},
                           mutation=name != _LIST)
        return _unwrap(result)

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a discovered tool once; projection tools are negotiated separately."""
        return self._run(lambda exchange: self._call(exchange, name, arguments))

    def append_event(self, payload: dict) -> dict:
        if (not isinstance(payload, dict) or payload.get("record_type") not in _KINDS
                or not _nonempty(payload.get("command_id"))
                or payload.get("authority_id") != self.stream.removeprefix("mptask/")):
            _fail("invalid_argument", "Invalid task-event routing fields")
        _json_value(payload, floats=False)
        body = _encode(payload)
        if len(body) > _BODY_LIMIT:
            _fail("body_too_large", "Task-event body exceeds 240 KiB")
        metadata = {"authority_id": payload["authority_id"], "command_id": payload["command_id"]}
        if len(_encode(metadata)) >= 4096:
            _fail("body_too_large", "Task-event routing metadata must be below 4 KiB")
        arguments = {
            "stream": self.stream, "room": "tasks", "type": payload["record_type"],
            "from_agent": _WRITER, "correlation_id": payload["command_id"],
            "body": body.decode("utf-8"), "metadata": metadata,
        }

        def append(exchange):
            result = self._call(exchange, _APPEND, arguments)
            event = result.get("event")
            self._validate_event(event)
            if any(event.get(key) != value for key, value in arguments.items()):
                _fail("invalid_event", "Append receipt does not match the submitted event")
            return event

        return self._run(append)

    def _validate_event(self, event: object) -> None:
        if not isinstance(event, dict):
            _fail("invalid_event", "Stored event must be an object")
        if event.get("body_truncated"):
            _fail("truncated_body", "Stored event body was truncated")
        if (not _nonempty(event.get("id")) or event.get("stream") != self.stream
                or event.get("room") != "tasks" or event.get("from_agent") != _WRITER
                or event.get("type") not in _KINDS or not _nonempty(event.get("correlation_id"))
                or not isinstance(event.get("metadata"), dict)
                or not isinstance(event.get("body"), str)
                or not _nonempty(event.get("created_at"))):
            _fail("invalid_event", "Stored event violates the reserved-stream contract")
        _decode(event["body"], code="invalid_event")
        if "body_length" in event:
            length = event["body_length"]
            if type(length) is not int or length not in (
                len(event["body"]), len(event["body"].encode("utf-8")),
            ):
                _fail("truncated_body", "Stored event body length does not match")

    def list_events(self, cursor: str | None = None,
                    correlation_id: str | None = None) -> list[dict]:
        if any(value is not None and not _nonempty(value) for value in (cursor, correlation_id)):
            _fail("invalid_argument", "Event cursor and correlation ID must be nonempty strings")

        def read(exchange):
            if self._descriptor is None:
                self._discover(exchange)
            arguments = {"stream": self.stream, "limit": _PAGE_SIZE, "preview": False}
            if cursor is not None:
                arguments["since_event_id"] = cursor
            if correlation_id is not None:
                arguments["correlation_id"] = correlation_id
            if self._descriptor["profile"] == _ORDERED_PROFILE:
                arguments["order"] = "asc"
            result = self._call(exchange, _LIST, arguments)
            events = result.get("events")
            if (not isinstance(events, list) or len(events) > _PAGE_SIZE
                    or type(result.get("count")) is not int or result["count"] != len(events)):
                _fail("invalid_response", "Invalid event-list page or count")
            if result.get("order", "asc") != "asc":
                _fail("invalid_order", "Upstream returned a non-ascending page")
            seen = {cursor} if cursor is not None else set()
            for event in events:
                self._validate_event(event)
                if event["id"] in seen:
                    _fail("invalid_order", "Event cursor did not advance strictly")
                if correlation_id is not None and event["correlation_id"] != correlation_id:
                    _fail("invalid_event", "Upstream ignored the correlation filter")
                seen.add(event["id"])
            return events

        return self._run(read)

    def replay_events(self, cursor: str | None = None) -> Iterator[dict]:
        """Preserve every physical event, including duplicate command payloads."""
        seen = {cursor} if cursor is not None else set()
        for _ in range(100_000):
            page = self.list_events(cursor)
            if not page:
                return
            for event in page:
                if event["id"] in seen:
                    _fail("invalid_order", "Replay repeated an earlier physical event")
                seen.add(event["id"])
            for event in page:
                yield event
            cursor = page[-1]["id"]
        _fail("replay_limit", "Replay exceeded its bounded page count")
