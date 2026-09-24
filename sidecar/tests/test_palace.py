"""HTTP-boundary tests; every hub and event here is owned by the test."""

import copy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from mempalace_tasks.palace import PalaceClient, PalaceError


STREAM = "mptask/11111111-1111-1111-1111-111111111111"
LEGACY_DESCRIPTION = (
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


def tool_fixture(ordered=True):
    """Coordination schemas include unused fields to detect unknown profiles."""
    strings = {
        "stream": "Filter by stream (optional)",
        "room": "Filter by room (optional)",
        "type": "Filter by event type (optional)",
        "to_agent": "Filter by target agent; also matches '*' broadcasts (optional)",
        "from_agent": "Filter by writer (optional)",
        "correlation_id": "Filter by correlation id (optional)",
        "status": "Filter by status (optional)",
        "since_event_id": "Return only events strictly after this event id (optional)",
        "since_created_at": (
            "Time window filter, inclusive: events created at or after this time "
            "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional). NOT a resume cursor "
            "\u2014 use since_event_id for that; a timestamp cursor silently drops "
            "peer events that sync in late. Dedup by id when using this."
        ),
    }
    properties = {
        name: {"type": "string", "description": description}
        for name, description in strings.items()
    }
    properties.update({
        "limit": {"type": "integer", "description": "Max events to return (default 50)"},
        "preview": {
            "type": "boolean",
            "description": (
                "Truncate each event body to a short excerpt (marks "
                "body_truncated + body_length) so scanning many events stays "
                "cheap. since_event_id is strictly AFTER that id, so do not pass "
                "the truncated event's own id to re-fetch it \u2014 repeat the "
                "original filters with preview=false (default false)"
            ),
        },
    })
    if ordered:
        properties.update({
            "topic": {"type": "string", "description": "Filter by topic (optional)"},
            "before_event_id": {
                "type": "string", "description": "Return events before this event id",
            },
            "order": {
                "type": "string", "enum": ["asc", "desc"],
                "description": "Append order; first page defaults to desc",
            },
        })
    append_properties = {
        "stream": {"type": "string", "description": "Logical stream"},
        "room": {"type": "string", "description": "Sub-channel"},
        "type": {"type": "string", "description": "Event type"},
        "from_agent": {"type": "string", "description": "Writer agent identity"},
        "to_agent": {"type": "string", "description": "Target agent, or '*'"},
        "correlation_id": {"type": "string", "description": "Task/conversation id"},
        "body": {
            "type": "string",
            "description": "Verbatim human-readable content (optional, max 256 KiB)",
        },
        "metadata": {"type": "object", "description": "Extra structured fields"},
        "artifact_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Ids of already-stored artifacts",
        },
        "branch": {"type": "string", "description": "Git branch"},
        "base_commit": {"type": "string", "description": "Git commit"},
        "status": {
            "type": "string",
            "description": "One of: open, claimed, ready, applied, blocked, failed, superseded",
        },
    }
    return [
        {
            "name": "mempalace_event_append",
            "description": "Append an immutable agent-coordination event to the logstream",
            "inputSchema": {
                "type": "object", "properties": append_properties,
                "required": ["stream", "room", "type", "from_agent"],
            },
        },
        {
            "name": "mempalace_event_list",
            "description": (
                LEGACY_DESCRIPTION if not ordered else
                "List events in append order. First page defaults to descending "
                "order; since_event_id means strictly after that event in append order."
            ),
            "inputSchema": {"type": "object", "properties": properties},
        },
        {
            "name": "mempalace_add_drawer",
            "description": "Add a derived memory projection",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "wing": {"type": "string"}, "room": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["wing", "room", "content"],
            },
        },
    ]


def proposal(command_id="command-1"):
    return {
        "record_type": "mptask.command",
        "authority_id": STREAM.removeprefix("mptask/"),
        "command_id": command_id,
        "payload_hash": "example-hash-validated-by-protocol-not-adapter",
        "event": {"kind": "TaskCreated", "title": "Preserve caf\u00e9 exactly"},
    }


def stored_event(index, body=None):
    return {
        "id": f"evt-{index:06}",
        "stream": STREAM,
        "room": "tasks",
        "type": "mptask.command",
        "from_agent": "mempalace-tasks",
        "to_agent": "*",
        "correlation_id": f"command-{index}",
        "body": json.dumps(proposal(f"command-{index}")) if body is None else body,
        "metadata": {"authority_id": STREAM.removeprefix("mptask/")},
        "artifact_ids": [],
        "branch": None,
        "base_commit": None,
        "status": None,
        # Deliberately reverse dates and repeat timestamps across page boundaries.
        "created_at": "2026-09-22T00:00:00Z" if index > 500 else "2026-09-23T00:00:00Z",
    }


def mcp_result(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


@dataclass
class WireResponse:
    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    declared_length: int | str | None = None
    delay: float = 0
    drip: float = 0
    disconnect: bool = False


class TestHub:
    def __init__(self, ordered=True):
        self.tools = tool_fixture(ordered)
        self.events = []
        self.requests = []
        self.headers = []
        self.paths = []
        self.hook = None
        self.session_id = None
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                hub.requests.append(request)
                hub.headers.append(dict(self.headers))
                hub.paths.append(self.path)
                response = hub.hook(request) if hub.hook else None
                if response is None:
                    response = hub.respond(request)
                if response.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                time.sleep(response.delay)
                try:
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header(
                        "Content-Length",
                        str(len(response.body) if response.declared_length is None
                            else response.declared_length),
                    )
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    if response.drip:
                        for byte in response.body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(response.drip)
                    else:
                        self.wfile.write(response.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/mcp"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    @staticmethod
    def rpc(request, result):
        return WireResponse(body=json.dumps({
            "jsonrpc": "2.0", "id": request["id"], "result": result,
        }).encode())

    def respond(self, request):
        method = request["method"]
        if method == "initialize":
            response = self.rpc(request, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mempalace", "version": "test-contract"},
            })
            if self.session_id:
                response.headers["Mcp-Session-Id"] = self.session_id
            return response
        if method == "notifications/initialized":
            return WireResponse(status=202)
        if method == "tools/list":
            return self.rpc(request, {"tools": self.tools})
        arguments = request["params"]["arguments"]
        name = request["params"]["name"]
        if name == "mempalace_event_list":
            events = self.events
            cursor = arguments.get("since_event_id")
            if cursor is not None:
                positions = [i for i, event in enumerate(events) if event["id"] == cursor]
                if not positions:
                    return self.rpc(request, mcp_result({
                        "success": False, "error": "Unknown since_event_id cursor",
                    }))
                events = events[positions[0] + 1:]
            if "correlation_id" in arguments:
                events = [
                    event for event in events
                    if event["correlation_id"] == arguments["correlation_id"]
                ]
            if "order" in self.tools[1]["inputSchema"]["properties"]:
                if arguments.get("order", "desc") == "desc":
                    events = list(reversed(events))
            events = events[:arguments["limit"]]
            return self.rpc(request, mcp_result({"events": events, "count": len(events)}))
        if name == "mempalace_event_append":
            event = stored_event(len(self.events) + 1)
            event.update(arguments)
            self.events.append(event)
            return self.rpc(request, mcp_result({"success": True, "event": event}))
        return self.rpc(request, mcp_result({"success": True, "drawer_id": "test-drawer"}))

    def calls(self, name):
        return [
            request["params"]["arguments"] for request in self.requests
            if request["method"] == "tools/call" and request["params"]["name"] == name
        ]


class PalaceTests(unittest.TestCase):
    def setUp(self):
        self.hub = self.enterContext(TestHub())
        self.client = PalaceClient(self.hub.url, STREAM, timeout=1)

    def assert_palace_error(self, code, action, ambiguous=False):
        with self.assertRaises(PalaceError) as raised:
            action()
        self.assertEqual(code, raised.exception.code)
        self.assertEqual(ambiguous, raised.exception.ambiguous)
        return raised.exception

    def call_response(self, payload):
        self.hub.hook = lambda request: (
            self.hub.rpc(request, payload) if request["method"] == "tools/call" else None
        )

    def test_discovers_initialize_and_uncached_schema(self):
        first = self.client.discover()
        self.assertEqual("mempalace-ordered-v1", first["profile"])
        self.assertIn("mempalace_event_append", first["tools"])
        self.assertEqual(
            ["initialize", "notifications/initialized", "tools/list"],
            [request["method"] for request in self.hub.requests],
        )
        self.hub.tools[1]["inputSchema"]["properties"].pop("preview")
        self.assert_palace_error("unsupported_schema", self.client.discover)
        self.assertEqual(2, sum(r["method"] == "tools/list" for r in self.hub.requests))
        # A failed rediscovery must not leave an obsolete working profile.
        self.assert_palace_error("unsupported_schema", self.client.list_events)

    def test_new_profile_replays_1201_in_append_order_through_empty_page(self):
        self.hub.events = [stored_event(i) for i in range(1, 1202)]
        self.hub.events[700]["body"] = self.hub.events[699]["body"]
        events = list(self.client.replay_events())
        self.assertEqual([f"evt-{i:06}" for i in range(1, 1202)], [e["id"] for e in events])
        calls = self.hub.calls("mempalace_event_list")
        self.assertEqual([None, "evt-000500", "evt-001000", "evt-001201"],
                         [a.get("since_event_id") for a in calls])
        for arguments in calls:
            self.assertEqual({"stream", "preview", "limit", "order"},
                             set(arguments) - {"since_event_id"})
            self.assertEqual("asc", arguments["order"])
            self.assertIs(False, arguments["preview"])
            self.assertEqual(500, arguments["limit"])

    def test_known_legacy_fingerprint_omits_order(self):
        self.hub.tools = tool_fixture(ordered=False)
        self.hub.events = [stored_event(1)]
        profile = self.client.discover()
        self.assertEqual("mempalace-legacy-append-order-v1", profile["profile"])
        self.assertEqual(["evt-000001"], [e["id"] for e in self.client.replay_events()])
        self.assertNotIn("order", self.hub.calls("mempalace_event_list")[0])

    def test_absence_of_order_and_config_override_do_not_prove_ascending(self):
        self.hub.tools = tool_fixture(ordered=False)
        self.hub.tools[1]["description"] = "List events."
        for override in (None, "mempalace-legacy-append-order-v1", "anything"):
            with self.subTest(override=override):
                client = PalaceClient(self.hub.url, STREAM, legacy_profile=override)
                self.assert_palace_error("unsupported_profile", client.discover)
        self.assertEqual([], self.hub.calls("mempalace_event_list"))

    def test_legacy_unknown_shape_rejected_despite_ascending_description(self):
        self.hub.tools = tool_fixture(ordered=False)
        self.hub.tools[1]["inputSchema"]["properties"]["offset"] = {"type": "integer"}
        self.assert_palace_error("unsupported_profile", self.client.discover)

    def test_required_schemas_are_strict(self):
        mutations = [
            lambda tools: tools.pop(0),
            lambda tools: tools[0]["inputSchema"]["properties"].pop("body"),
            lambda tools: tools[0]["inputSchema"]["properties"].update(body={"type": "number"}),
            lambda tools: tools[0]["inputSchema"]["properties"]["body"].update(maxLength=1024),
            lambda tools: tools[0]["inputSchema"]["properties"]["metadata"].update(
                additionalProperties=False,
            ),
            lambda tools: tools[0]["inputSchema"]["properties"]["metadata"].update(
                additionalProperties="false",
            ),
            lambda tools: tools[0]["inputSchema"]["required"].append("to_agent"),
            lambda tools: tools[1]["inputSchema"].update(required=["since_created_at"]),
            lambda tools: tools[1]["inputSchema"]["properties"]["order"].update(enum=["desc"]),
            lambda tools: tools[1]["inputSchema"]["properties"]["limit"].update(maximum=10),
            lambda tools: tools.append(copy.deepcopy(tools[0])),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.hub.tools = tool_fixture()
                mutate(self.hub.tools)
                self.assert_palace_error("unsupported_schema", self.client.discover)

    def test_tools_list_pagination_and_session_headers(self):
        self.hub.session_id = "test-session"

        def paginated(request):
            if request["method"] != "tools/list":
                return None
            if request.get("params", {}).get("cursor") == "next":
                return self.hub.rpc(request, {"tools": self.hub.tools[1:]})
            return self.hub.rpc(request, {"tools": self.hub.tools[:1], "nextCursor": "next"})

        self.hub.hook = paginated
        self.client.discover()
        self.assertEqual("test-session", self.hub.headers[-1]["Mcp-Session-Id"])
        self.assertEqual("2024-11-05", self.hub.headers[-1]["Mcp-Protocol-Version"])

    def test_append_one_canonical_full_body_and_returns_stored_event(self):
        value = proposal()
        stored = self.client.append_event(value)
        arguments, = self.hub.calls("mempalace_event_append")
        self.assertEqual("evt-000001", stored["id"])
        self.assertEqual(value, json.loads(stored["body"]))
        self.assertEqual(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            arguments["body"],
        )
        self.assertEqual("mempalace-tasks", arguments["from_agent"])
        self.assertEqual("tasks", arguments["room"])
        self.assertEqual(STREAM, arguments["stream"])
        self.assertEqual("command-1", arguments["correlation_id"])
        self.assertNotIn("status", arguments)
        self.assertNotIn("payload_hash", arguments["metadata"])

    def test_append_settlement_preserves_record_kind(self):
        value = proposal()
        value["record_type"] = "mptask.settle"
        stored = self.client.append_event(value)
        self.assertEqual("mptask.settle", stored["type"])

    def test_payload_size_invalid_json_and_routing_fail_before_mutation(self):
        for value in (
            [], {"record_type": "mptask.command"},
            {**proposal(), "record_type": "task.request"},
            {**proposal(), "authority_id": "another-authority"},
            {**proposal(), "event": {"number": float("nan")}},
            {**proposal(), "event": {"content": "x" * (240 * 1024)}},
        ):
            with self.subTest(value_type=type(value)):
                with self.assertRaises(PalaceError) as error:
                    self.client.append_event(value)
                self.assertFalse(error.exception.ambiguous)
        self.assertEqual([], self.hub.calls("mempalace_event_append"))

    def test_correlation_list_and_resume_use_only_event_id(self):
        self.hub.events = [stored_event(1), stored_event(2)]
        events = self.client.list_events("evt-000001", "command-2")
        self.assertEqual(["evt-000002"], [e["id"] for e in events])
        arguments, = self.hub.calls("mempalace_event_list")
        self.assertEqual("command-2", arguments["correlation_id"])
        self.assertNotIn("since_created_at", arguments)

    def test_unknown_cursor_is_explicit_not_empty_history(self):
        self.assert_palace_error("unknown_cursor", lambda: self.client.list_events("missing"))

    def test_page_and_record_errors_fail_closed(self):
        event = stored_event(1)
        pages = [
            ("invalid_response", {"events": [event], "count": 0}),
            ("invalid_response", {"events": [event]}),
            ("invalid_order", {"events": [event, event], "count": 2}),
            ("invalid_order", {"events": [event], "count": 1, "order": "desc"}),
            ("invalid_response", {"events": [event] * 501, "count": 501}),
            ("truncated_body", {"events": [{**event, "body_truncated": True}], "count": 1}),
            ("invalid_event", {"events": [{**event, "body": "not JSON"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "body": "{} trailing"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "body": "[]"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "metadata": None}], "count": 1}),
            ("invalid_event", {"events": [{**event, "stream": "other"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "room": "other"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "from_agent": "replica"}], "count": 1}),
            ("invalid_event", {"events": [{**event, "type": "task.request"}], "count": 1}),
        ]
        for code, page in pages:
            with self.subTest(code=code, page=list(page)):
                self.call_response(mcp_result(page))
                self.assert_palace_error(code, self.client.list_events)

    def test_replay_repeated_physical_cursor_fails_without_silent_dedup(self):
        self.call_response(mcp_result({"events": [stored_event(1)], "count": 1}))
        iterator = self.client.replay_events()
        self.assertEqual("evt-000001", next(iterator)["id"])
        self.assert_palace_error("invalid_order", lambda: next(iterator))
        self.assertEqual(2, len(self.hub.calls("mempalace_event_list")))

    def test_mcp_error_and_nested_failure_envelopes_are_not_success(self):
        envelopes = [
            {"content": [{"type": "text", "text": "server failure"}], "isError": True},
            mcp_result({"success": False, "error": "not stored"}),
            mcp_result({"error": {"message": "not stored"}}),
            mcp_result({"result": {"success": False, "error": "not stored"}}),
            mcp_result({"success": True, "data": {"error": "not stored"}}),
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                self.call_response(envelope)
                self.assert_palace_error("upstream_error", self.client.list_events)

    def test_nested_success_and_structured_content_unwrap(self):
        for envelope in (
            mcp_result({"success": True, "data": {"events": [], "count": 0}}),
            {"structuredContent": {"events": [], "count": 0}, "content": [], "isError": False},
        ):
            with self.subTest(envelope=envelope):
                self.call_response(envelope)
                self.assertEqual([], self.client.list_events())

    def test_invalid_mcp_bodies_fail_explicitly(self):
        envelopes = [
            {}, {"content": []}, {"content": [{"type": "text", "text": "not JSON"}]},
            {"content": [{"type": "image", "data": "bad"}]},
            mcp_result([]), mcp_result({"events": [], "count": 0}) | {"isError": "false"},
            {
                **mcp_result({"events": [], "count": 0}),
                "structuredContent": {"events": [stored_event(1)], "count": 1},
            },
        ]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                self.call_response(envelope)
                self.assert_palace_error("invalid_response", self.client.list_events)

    def test_projection_mutation_uses_only_test_hub(self):
        result = self.client.call_tool(
            "mempalace_add_drawer", {"wing": "test", "room": "tasks", "content": "derived"},
        )
        self.assertEqual("test-drawer", result["drawer_id"])
        self.assertEqual(1, len(self.hub.calls("mempalace_add_drawer")))

    def test_unknown_tool_and_invalid_arguments_fail_before_dispatch(self):
        self.assert_palace_error(
            "unsupported_tool", lambda: self.client.call_tool("missing", {}),
        )
        for arguments in (
            {"wing": "test"}, {"wing": 2, "room": "tasks", "content": "bad"},
            {"wing": "test", "room": "tasks", "content": "ok", "extra": True},
        ):
            with self.subTest(arguments=arguments):
                self.assert_palace_error(
                    "invalid_argument",
                    lambda: self.client.call_tool("mempalace_add_drawer", arguments),
                )
        self.assertEqual([], self.hub.calls("mempalace_add_drawer"))

    def test_http_failure_does_not_retry_append_or_leak_auth(self):
        secret = "test-secret-never-in-errors"
        self.client = PalaceClient(self.hub.url, STREAM, token=secret, timeout=1)
        self.client.discover()
        self.hub.hook = lambda request: WireResponse(status=503, body=secret.encode())
        error = self.assert_palace_error(
            "http_error", lambda: self.client.append_event(proposal()), ambiguous=True,
        )
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertEqual(1, len(self.hub.calls("mempalace_event_append")))
        self.assertEqual(f"Bearer {secret}", self.hub.headers[-1]["Authorization"])
        self.assertEqual(["/mcp"], sorted(set(self.hub.paths)))

    def test_remote_tool_failure_does_not_leak_token_in_error(self):
        secret = "credential-echoed-by-upstream"
        self.client = PalaceClient(self.hub.url, STREAM, token=secret)
        self.call_response(mcp_result({"success": False, "error": secret}))
        error = self.assert_palace_error("upstream_error", self.client.list_events)
        self.assertNotIn(secret, str(error))

    def test_redirects_are_not_followed_with_credentials(self):
        other = self.enterContext(TestHub())
        self.hub.hook = lambda request: WireResponse(
            status=307, headers={"Location": other.url},
        )
        self.client = PalaceClient(self.hub.url, STREAM, token="private")
        self.assert_palace_error("http_error", self.client.discover)
        self.assertEqual([], other.requests)

    def test_dropped_append_reply_is_ambiguous_and_never_retried(self):
        self.client.discover()

        def lost_reply(request):
            self.hub.respond(request)  # Simulate commit before loss of response.
            return WireResponse(disconnect=True)

        self.hub.hook = lost_reply
        self.assert_palace_error(
            "transport_error", lambda: self.client.append_event(proposal()), ambiguous=True,
        )
        self.assertEqual(1, len(self.hub.events))
        self.assertEqual(1, len(self.hub.calls("mempalace_event_append")))

    def test_timeout_during_mutation_is_bounded_and_not_retried(self):
        self.client = PalaceClient(self.hub.url, STREAM, timeout=0.08)
        self.client.discover()
        self.hub.hook = lambda request: WireResponse(delay=0.4)
        start = time.monotonic()
        self.assert_palace_error(
            "timeout", lambda: self.client.append_event(proposal()), ambiguous=True,
        )
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(1, len(self.hub.calls("mempalace_event_append")))

    def test_slow_drip_is_bounded_by_total_not_per_read_timeout(self):
        self.client = PalaceClient(self.hub.url, STREAM, timeout=0.12)
        self.client.discover()
        self.hub.hook = lambda request: WireResponse(body=b"x" * 100, drip=0.025)
        start = time.monotonic()
        self.assert_palace_error("timeout", self.client.list_events)
        self.assertLess(time.monotonic() - start, 0.35)

    def test_slow_drip_timeout_closes_detached_response_and_allows_reuse(self):
        for protocol in ("HTTP/1.0", "HTTP/1.1"):
            with self.subTest(protocol=protocol):
                self.hub.server.RequestHandlerClass.protocol_version = protocol
                self.hub.hook = None
                self.client = PalaceClient(self.hub.url, STREAM, timeout=0.08)
                self.client.discover()
                self.hub.hook = lambda request: WireResponse(
                    body=b"x" * 80, drip=0.025, headers={"Connection": "close"},
                )
                responses = []
                original = http.client.HTTPConnection.getresponse

                def observe(connection):
                    response = original(connection)
                    responses.append(response)
                    return response

                with patch.object(http.client.HTTPConnection, "getresponse", observe):
                    self.assert_palace_error("timeout", self.client.list_events)
                    released = self.client._busy.acquire(timeout=0.25)
                    if released:
                        self.client._busy.release()
                    self.assertTrue(released, "Timed-out response kept the I/O worker busy")
                    self.assertEqual(1, len(responses))
                    self.assertTrue(responses[0].closed, "HTTPResponse was not closed explicitly")
                self.hub.hook = None
                self.assertEqual([], self.client.list_events())

    def test_committed_append_with_huge_content_length_is_typed_and_ambiguous(self):
        secret = "private-malformed-header-token"
        self.client = PalaceClient(self.hub.url, STREAM, token=secret)
        self.client.discover()

        def commit_then_malformed_header(request):
            response = self.hub.respond(request)
            response.declared_length = "9" * 5000
            return response

        self.hub.hook = commit_then_malformed_header
        error = self.assert_palace_error(
            "body_too_large", lambda: self.client.append_event(proposal()), ambiguous=True,
        )
        self.assertNotIn(secret, str(error))
        self.assertNotIn("9" * 100, str(error))
        self.assertEqual(1, len(self.hub.events))
        self.assertEqual(1, len(self.hub.calls("mempalace_event_append")))

    def test_discovery_entire_exchange_shares_one_deadline(self):
        self.client = PalaceClient(self.hub.url, STREAM, timeout=0.12)

        def slow(request):
            response = self.hub.respond(request)
            response.delay = 0.055
            return response

        self.hub.hook = slow
        start = time.monotonic()
        self.assert_palace_error("timeout", self.client.discover)
        self.assertLess(time.monotonic() - start, 0.3)

    def test_transport_and_json_rpc_corruption_is_explicit(self):
        self.client.discover()
        responses = [
            ("invalid_response", WireResponse(body=b"not-json")),
            ("invalid_response", WireResponse(body=b"\xff")),
            ("invalid_response", WireResponse(body=b'{"jsonrpc":"2.0","id":123,"result":{}}')),
            ("invalid_response", WireResponse(body=b'{"jsonrpc":"1.0","id":1,"result":{}}')),
            ("invalid_response", WireResponse(body=b'{"a":1,"a":2}')),
            ("body_too_large", WireResponse(declared_length=200 * 1024 * 1024)),
            ("invalid_response", WireResponse(body=b"{}", declared_length=100)),
        ]
        for code, response in responses:
            with self.subTest(code=code, body=response.body):
                self.hub.hook = lambda request, response=response: response
                self.assert_palace_error(code, self.client.list_events)

    def test_rpc_error_is_not_tool_success(self):
        self.client.discover()
        self.hub.hook = lambda request: WireResponse(body=json.dumps({
            "jsonrpc": "2.0", "id": request["id"],
            "error": {"code": -32603, "message": "secret upstream detail"},
        }).encode())
        error = self.assert_palace_error("rpc_error", self.client.list_events)
        self.assertNotIn("secret upstream detail", str(error))

    def test_configuration_rejects_credentials_fragments_and_nonpositive_timeout(self):
        for url, kwargs in (
            ("file:///palace", {}), ("http://user:password@localhost/mcp", {}),
            ("http://localhost/mcp#fragment", {}), ("http://localhost/mcp?token=secret", {}),
            (self.hub.url, {"timeout": 0}), (self.hub.url, {"timeout": float("inf")}),
            (self.hub.url, {"token": "header\r\ninjection"}),
        ):
            with self.subTest(url=url):
                self.assert_palace_error(
                    "invalid_configuration", lambda: PalaceClient(url, STREAM, **kwargs),
                )

    def test_malformed_schema_enum_is_a_typed_failure(self):
        for choices in (None, [{"bad": "asc"}], 7):
            with self.subTest(choices=choices):
                self.hub.tools = tool_fixture()
                self.hub.tools[1]["inputSchema"]["properties"]["order"]["enum"] = choices
                self.assert_palace_error("unsupported_schema", self.client.discover)

    def test_legacy_replays_1201_without_timestamp_or_order_arguments(self):
        self.hub.tools = tool_fixture(ordered=False)
        self.hub.events = [stored_event(i) for i in range(1, 1202)]
        self.assertEqual(
            [f"evt-{i:06}" for i in range(1, 1202)],
            [event["id"] for event in self.client.replay_events()],
        )
        self.assertEqual(4, len(self.hub.calls("mempalace_event_list")))
        for arguments in self.hub.calls("mempalace_event_list"):
            self.assertNotIn("order", arguments)
            self.assertNotIn("since_created_at", arguments)
            self.assertFalse(arguments["preview"])

    def test_exact_utf8_body_limit_is_enforced_before_append(self):
        value = proposal()
        value["padding"] = ""
        fixed_bytes = len(json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8"))
        value["padding"] = "x" * (240 * 1024 - fixed_bytes)
        event = self.client.append_event(value)
        self.assertEqual(240 * 1024, len(event["body"].encode("utf-8")))
        value["padding"] += "x"
        self.assert_palace_error("body_too_large", lambda: self.client.append_event(value))
        self.assertEqual(1, len(self.hub.calls("mempalace_event_append")))

    def test_append_receipt_corruption_is_ambiguous_without_retries(self):
        for result in (
            {"success": False, "error": "failure after dispatch"},
            {"success": True, "event": {"id": "receipt-only"}},
            {"success": True, "event": stored_event(999)},
        ):
            with self.subTest(result=result):
                self.call_response(mcp_result(result))
                before = len(self.hub.calls("mempalace_event_append"))
                with self.assertRaises(PalaceError) as error:
                    self.client.append_event(proposal())
                self.assertTrue(error.exception.ambiguous)
                self.assertEqual(before + 1, len(self.hub.calls("mempalace_event_append")))

    def test_server_order_error_and_nested_unknown_cursor_are_explicit(self):
        for code, payload in (
            ("invalid_order", {"success": False, "error": "invalid order"}),
            ("unknown_cursor", {"result": {
                "success": False, "error": {"message": "since_event_id not found"},
            }}),
        ):
            with self.subTest(code=code):
                self.call_response(mcp_result(payload))
                self.assert_palace_error(code, self.client.list_events)

    def test_tools_pagination_cycle_is_not_retried_forever(self):
        self.hub.hook = lambda request: (
            self.hub.rpc(request, {"tools": [], "nextCursor": "loop"})
            if request["method"] == "tools/list" else None
        )
        self.assert_palace_error("unsupported_schema", self.client.discover)
        self.assertEqual(2, sum(r["method"] == "tools/list" for r in self.hub.requests))

    def test_stalled_dns_is_bounded_and_cannot_dispatch_late_mutation(self):
        self.client = PalaceClient(self.hub.url, STREAM, timeout=0.04)
        self.client.discover()
        release = threading.Event()
        returned = threading.Event()
        original = socket.getaddrinfo

        def stalled(*args, **kwargs):
            if not release.wait(1):
                raise TimeoutError("Test DNS release was not signaled")
            result = original(*args, **kwargs)
            returned.set()
            return result

        with patch("socket.getaddrinfo", side_effect=stalled):
            try:
                start = time.monotonic()
                self.assert_palace_error("timeout", lambda: self.client.append_event(proposal()))
                self.assertLess(time.monotonic() - start, 0.3)
                self.assert_palace_error("upstream_busy", self.client.list_events)
                self.assertEqual([], self.hub.calls("mempalace_event_append"))
            finally:
                release.set()
                self.assertTrue(returned.wait(1))
        # Wait for that single I/O worker to release the operation guard.
        self.assertTrue(self.client._busy.acquire(timeout=1))
        self.client._busy.release()
        self.assertEqual([], self.hub.calls("mempalace_event_append"))

    def test_non_json_media_type_and_content_length_are_rejected(self):
        self.client.discover()
        for headers in (
            {"Content-Type": "text/event-stream"},
            {"Content-Encoding": "gzip"},
            {"Content-Length": "-1"},
        ):
            with self.subTest(headers=headers):
                self.hub.hook = lambda request, headers=headers: WireResponse(
                    body=b"{}", headers=headers,
                )
                self.assert_palace_error("invalid_response", self.client.list_events)


if __name__ == "__main__":
    unittest.main()
