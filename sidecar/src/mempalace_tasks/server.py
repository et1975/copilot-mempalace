"""Official SDK stateless JSON MCP over one explicitly owned task authority."""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import hmac
import json
import logging
import re
import time

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from .authority import AuthorityError
from .codec import canonical_json
from .config import validate_binding, validate_token
from .domain import CLASSES, CONTENT, DEFAULT_POLICY, EDGE_TYPES, EXECUTION, ROLES, SCHEMAS, STATUSES
from .projection import ProjectionError, ProjectionRecord


MAX_REQUEST_BYTES = 256 * 1024
MAX_IN_FLIGHT = 32
PUBLIC_OPERATIONS = tuple(name for name in SCHEMAS if name not in {"authority_create", "expire"})
READ_OPERATIONS = ("get", "snapshot", "list", "ready", "history", "health", "wait_ready")
_in_request = ContextVar("mptask_http_request", default=False)


class _RequestLogFilter(logging.Filter):
    def filter(self, record):
        # SDK debug/warning messages can contain untrusted tool names/arguments.
        return not _in_request.get()


@contextmanager
def _protect_sdk_logs():
    protection = _RequestLogFilter()
    loggers = [logging.getLogger(name) for name in list(logging.Logger.manager.loggerDict)
               if name == "mcp" or name.startswith("mcp.")]
    for logger in loggers:
        logger.addFilter(protection)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(protection)


def _object(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": sorted(required), "additionalProperties": False}


def _schemas():
    string = {"type": "string", "minLength": 1}
    positive = {"type": "integer", "minimum": 1}
    references = {"type": "array", "items": string, "maxItems": 20, "uniqueItems": True}
    policy = _object({
        key: {"type": "integer", "minimum": 0 if key == "automatic_retries" else 1,
              "maximum": 3 if key == "automatic_retries" else 86400}
        for key in DEFAULT_POLICY
    })
    evidence = _object({
        "references": {**references, "minItems": 1},
        **{key: {"type": "boolean"} for key in (
            "prepared", "publication_revoked", "process_stopped",
            "effects_reconciled", "fencing_unavailable")},
        **{key: {"type": "object", "additionalProperties": positive}
           for key in ("resource_fences", "installed_fences")},
    }, ("references",))
    fields = {name: string for name in (
        "actor", "command_id", "task_id", "attempt_id", "supervisor_id", "worker_id",
        "project", "title", "description", "acceptance", "execution_profile", "intent_key",
        "goal_id", "reason", "summary", "reference", "source", "target",
        "tag", "text", "source_task_id", "reuse_task_id",
    )}
    fields.update({name: positive for name in (
        "expected_version", "expected_lease_revision", "claim_generation",
        "checkpoint_sequence", "expected_source_version", "expected_target_version",
        "expected_graph_revision",
    )})
    fields.update({
        "description": {"type": "string"},
        "acceptance": {"type": "string"},
        "kind": {"type": "string", "enum": ["task", "epic"]},
        "execution_class": {"type": ["string", "null"], "enum": [None, *sorted(CLASSES)]},
        "execution_profile": {"type": ["string", "null"]},
        "priority": {"type": "integer", "minimum": 0, "maximum": 4},
        "hold_reason": {"type": ["string", "null"]},
        "deferred_until": {"type": ["string", "null"]},
        "goal_id": {"type": ["string", "null"]},
        "intent_key": {"type": ["string", "null"]},
        "admitted": {"type": "boolean"}, "permanent": {"type": "boolean"},
        "additional_retries": {"type": "integer", "minimum": 1, "maximum": 3},
        "resource_keys": {"type": "array", "items": string, "uniqueItems": True, "maxItems": 32},
        "policy": policy, "references": references, "evidence": evidence,
        "edge_type": {"type": "string", "enum": sorted(EDGE_TYPES)},
        "report_kind": {"type": "string", "enum": ["started", "settled", "failed", "recovery_started"]},
        "retry_decision": {"type": "string", "enum": ["preserve", "grant"]},
        "source_disposition": {"type": "string", "enum": ["continue", "yield", "complete"]},
        "checkpoint": _object({"sequence": positive, "reference": string}, ("sequence", "reference")),
        "goal_policy": _object({
            "scope": string, "max_tasks": {"type": "integer", "minimum": 1, "maximum": 10000},
            "max_batch": {"type": "integer", "minimum": 1, "maximum": 50},
        }, ("scope",)),
    })
    fields["patch"] = _object({key: fields[key] for key in CONTENT | EXECUTION})
    fields["planning_task"] = _object(
        {key: fields[key] for key in CONTENT | EXECUTION | {"kind"}},
        ("title", "description", "acceptance"),
    )
    fields["tasks"] = {"type": "array", "maxItems": 50, "items": _object({
        key: fields[key] for key in CONTENT | EXECUTION | {
            "kind", "intent_key", "admitted", "reuse_task_id", "expected_version"}
    }, ("intent_key",))}
    fields["edges"] = {"type": "array", "maxItems": 200, "items": _object({
        key: fields[key] for key in ("source", "target", "edge_type")
    }, ("source", "target", "edge_type"))}
    result = {}
    for name in PUBLIC_OPERATIONS:
        allowed, required = SCHEMAS[name]
        properties = {key: deepcopy(fields[key]) for key in allowed | {"actor", "command_id"}}
        if name == "transition":
            properties["target"] = {"type": "string", "enum": ["closed", "cancelled"]}
        if name in {"transition", "expand", "goal_close"}:
            properties["evidence"] = {**references, "minItems": 1}
        result[name] = _object(properties, required | {"actor", "command_id"})
    filters = {name: string for name in ("project", "goal_id", "assignee")}
    filters.update({"status": {"type": "string", "enum": sorted(STATUSES)},
                    "kind": fields["kind"], "needs_attention": {"type": "boolean"}})
    limit = {"type": "integer", "minimum": 1, "maximum": 500}
    result["get"] = _object({"task_id": string}, ("task_id",))
    result["snapshot"] = _object({"filters": _object(filters), "limit": limit, "cursor": string})
    result["list"] = deepcopy(result["snapshot"])
    ready_filters = _object({
        **filters, "execution_profile": string, "priority_ceiling": fields["priority"], "limit": limit,
    })
    result["ready"] = _object({"filters": ready_filters})
    result["history"] = _object({
        "task_id": string, "limit": limit, "cursor": string,
        "after_record_seq": {"type": "integer", "minimum": 0, "maximum": 2**63 - 1},
    }, ("task_id",))
    result["health"] = _object({})
    result["wait_ready"] = _object({
        "filters": ready_filters, "after_ready_version": string,
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30},
    })
    return result


def _identifier_map(field, value):
    if not isinstance(value, dict):
        return False
    if field in {"resource_fences", "installed_fences"}:
        return all(type(counter) is int and counter > 0 for counter in value.values())
    if field == "actors":
        return all(type(role) is str and role in ROLES for role in value.values())
    if field == "resources":
        return all(isinstance(record, dict) and set(record) == {"counter", "reservation"}
                   and type(record["counter"]) is int and record["counter"] >= 0
                   and (record["reservation"] is None or isinstance(record["reservation"], dict))
                   for record in value.values())
    if field == "execution_profiles":
        return all(isinstance(profile, dict) and type(profile.get("execution_class")) is str
                   and profile["execution_class"] in CLASSES for profile in value.values())
    if field == "supervisors":
        return all(isinstance(supervisor, dict) and set(supervisor) == {"profiles", "workers"}
                   and all(isinstance(supervisor[key], list)
                           and all(type(name) is str for name in supervisor[key])
                           for key in ("profiles", "workers"))
                   for supervisor in value.values())
    if field == "tasks":
        return all(isinstance(task, dict) and {"id", "version", "kind", "status", "project"} <= task.keys()
                   and type(task["id"]) is str and type(task["version"]) is int
                   and task["kind"] in ("task", "epic") and type(task["status"]) is str
                   and task["status"] in STATUSES and type(task["project"]) is str
                   for task in value.values())
    return False


def _attempt_authorization(value):
    if (not isinstance(value, dict) or type(value.get("as_of")) is not str
            or type(value.get("fresh")) is not bool):
        return False
    if set(value) == {"as_of", "fresh", "tasks"}:
        return isinstance(value["tasks"], list)
    return set(value) == {
        "as_of", "fresh", "task_id", "attempt_id", "claim_generation", "matches_current",
        "lease_live", "authorized", "current_lease_revision", "lease_expires_at", "reason",
    }


def _safe(value, token, *, identifier_keys=False):
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, list):
        return [_safe(item, token) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            credential = (not identifier_keys
                          and re.search(r"token|secret|password|authorization|credential", key, re.I)
                          and not (key == "authorization" and _attempt_authorization(item)))
            result[_safe(key, token)] = ("[redacted]" if credential else _safe(
                item, token, identifier_keys=_identifier_map(key, item)))
        return result
    return value


def _result(payload, *, error=False):
    return types.CallToolResult(
        structuredContent=payload,
        content=[types.TextContent(type="text", text=json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))],
        isError=error,
    )


class _Boundary:
    def __init__(self, app, token, binding):
        self.app, self.token, self.binding = app, token.encode("ascii"), binding

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope["headers"]
            auth = [value for key, value in headers if key.lower() == b"authorization"]
            expected = b"Bearer " + self.token
            if len(auth) != 1 or not hmac.compare_digest(auth[0], expected):
                return await Response("Unauthorized", 401)(scope, receive, send)
            hosts = [value for key, value in headers if key.lower() == b"host"]
            origins = [value for key, value in headers if key.lower() == b"origin"]
            if hosts != [self.binding.encode("ascii")]:
                return await Response("Invalid Host", 421)(scope, receive, send)
            if origins and origins != [f"http://{self.binding}".encode("ascii")]:
                return await Response("Invalid Origin", 403)(scope, receive, send)
        marker = _in_request.set(scope["type"] == "http")
        try:
            await self.app(scope, receive, send)
        finally:
            _in_request.reset(marker)


class _MCPRoute:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


class ProjectionPump:
    """Single-driver bounded delivery over one owned authority/projector pair."""

    def __init__(self, authority, projector):
        self.authority, self.projector = authority, projector
        self._verified_checkpoint = None

    def run_batch(self, *, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Projection batch limit must be in 1..100")
        checkpoint = self.projector.health()["checkpoint"]
        ordinal = 0 if checkpoint is None else checkpoint["ordinal"]
        if checkpoint is not None and checkpoint != self._verified_checkpoint:
            previous = self.authority.accepted_records(after_ordinal=ordinal - 1, limit=1)
            if (checkpoint["authority_id"] != self.authority.authority_id or not previous
                    or previous[0]["ordinal"] != ordinal
                    or checkpoint["event_id"] != previous[0]["event_id"]
                    or checkpoint["event_hash"] != hashlib.sha256(
                        canonical_json(previous[0]["event"]).encode()).hexdigest()):
                raise ProjectionError("projection_history_mismatch",
                                      "Projection checkpoint does not match accepted history")
        accepted = self.authority.accepted_records(after_ordinal=ordinal, limit=limit)
        # The feed copies only this bounded batch, then releases authority serialization.
        records = (ProjectionRecord(row["event_id"], row["ordinal"], row["event"])
                   for row in accepted)
        result = self.projector.run_batch(records, limit=limit)
        self._verified_checkpoint = deepcopy(result["checkpoint"])
        return result


def project_batch(authority, projector, *, limit=10):
    """One-shot delivery; retain a ProjectionPump for repeated timer/batch work."""
    return ProjectionPump(authority, projector).run_batch(limit=limit)


class _Runtime:
    def __init__(self, authority, maintenance, token, projector):
        self.authority, self.maintenance, self.token = authority, maintenance, token
        self.projector = projector
        self.projection_pump = ProjectionPump(authority, projector) if projector is not None else None
        self.projection_status = {"enabled": projector is not None, "paused": False,
                                  "checkpoint": None, "last_error": None}
        self.sweep = authority.state.configuration["policy"]["sweep_seconds"]
        self.last_tick = float("-inf")
        self.maintenance_status = {"ok": False, "ticks": 0, "reason": "not_started"}
        self.in_flight = 0

    async def start(self):
        self.request_limit = anyio.CapacityLimiter(4)
        self.maintenance_limit = anyio.CapacityLimiter(1)
        self.projection_limit = anyio.CapacityLimiter(1)
        self.tick_lock = anyio.Lock()
        await self.tick_if_due()
        if self.projector is not None:
            self.projection_status = _safe(await anyio.to_thread.run_sync(
                self.projector.health, limiter=self.projection_limit), self.token)

    async def dispatch(self, function, *args, **kwargs):
        return await anyio.to_thread.run_sync(
            lambda: function(*args, **kwargs), limiter=self.request_limit)

    async def tick_if_due(self):
        async with self.tick_lock:
            if time.monotonic() - self.last_tick < self.sweep:
                return
            try:
                def tick():
                    self.maintenance.tick()
                    return self.maintenance.health()
                status = await anyio.to_thread.run_sync(tick, limiter=self.maintenance_limit)
                self.maintenance_status = _safe(status, self.token)
            except Exception:
                self.maintenance_status = {"ok": False, "error": {
                    "code": "maintenance_failed", "message": "Maintenance tick failed"}}
            self.last_tick = time.monotonic()

    async def maintain(self):
        while True:
            await anyio.sleep(max(0.01, self.sweep - (time.monotonic() - self.last_tick)))
            await self.tick_if_due()

    async def project(self):
        while True:
            if not self.projection_status["paused"]:
                try:
                    def batch():
                        result = self.projection_pump.run_batch()
                        return result, self.projector.health()
                    result, health = await anyio.to_thread.run_sync(batch, limiter=self.projection_limit)
                    self.projection_status = _safe(health, self.token)
                except Exception as error:
                    try:
                        self.projection_status = _safe(await anyio.to_thread.run_sync(
                            self.projector.health, limiter=self.projection_limit), self.token)
                    except Exception:
                        self.projection_status = {**self.projection_status, "health_unavailable": True}
                    self.projection_status = {
                        **self.projection_status, "paused": True, "last_error": {
                            "code": error.code if isinstance(error, ProjectionError) else "projection_failed",
                            "message": "Projection paused; stop service and explicitly resume or rebuild",
                        },
                    }
            await anyio.sleep(1)

    async def ready(self, arguments):
        await self.tick_if_due()
        result = await self.dispatch(self.authority.ready, **arguments)
        # Time itself is not a version: elapsed deferral/backoff changes the eligible set.
        identity = {"head": result["domain_head"], "tasks": result["tasks"],
                    "filters": arguments.get("filters", {})}
        result["ready_version"] = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return result

    async def wait_ready(self, arguments):
        deadline = time.monotonic() + arguments.get("timeout_seconds", 30)
        query = {"filters": arguments["filters"]} if "filters" in arguments else {}
        for _ in range(122):
            result = await self.ready(query)
            if result["ready_version"] != arguments.get("after_ready_version"):
                return {**result, "timed_out": False}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**result, "timed_out": True}
            await anyio.sleep(min(0.25, remaining))
        return {**result, "timed_out": True}

    async def call(self, name, arguments):
        if name in PUBLIC_OPERATIONS:
            if name == "claim":
                await self.tick_if_due()
            return await self.dispatch(self.authority.execute, {"operation": name, **arguments})
        if name == "ready":
            return await self.ready(arguments)
        if name == "wait_ready":
            return await self.wait_ready(arguments)
        if name == "health":
            return {**await self.dispatch(self.authority.health),
                    "maintenance": deepcopy(self.maintenance_status),
                    "projection": deepcopy(self.projection_status)}
        method = self.authority.snapshot if name in {"snapshot", "list"} else getattr(self.authority, name)
        return await self.dispatch(method, **arguments)


def create_app(authority, maintenance, *, token, host="127.0.0.1", port=8766, projector=None):
    """Borrow started authority ownership; the caller closes it after ASGI shutdown."""
    binding = validate_binding(host, port)
    validate_token(token)
    runtime = _Runtime(authority, maintenance, token, projector)
    schemas = _schemas()
    validators = {key: Draft202012Validator(value) for key, value in schemas.items()}
    sdk = Server("mempalace-tasks", version="1")

    @sdk.list_tools()
    async def list_tools():
        return [types.Tool(
            name=f"mptask_{name}", description=(
                "Read diagnostic task state; never authorizes execution."
                if name in READ_OPERATIONS else
                f"Apply {name}; retain command_id on ambiguous outcomes. Actor must be registered."),
            inputSchema=deepcopy(schema),
        ) for name, schema in schemas.items()]

    @sdk.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        operation = name.removeprefix("mptask_")
        dispatched = False
        admitted = False
        try:
            if name != f"mptask_{operation}" or operation not in schemas:
                raise AuthorityError("unknown_tool", "Unknown task tool")
            if next(validators[operation].iter_errors(arguments), None) is not None:
                raise AuthorityError("validation_error", "Arguments do not match this tool's schema")
            if runtime.in_flight >= MAX_IN_FLIGHT:
                raise AuthorityError("service_busy", "Task service concurrency limit reached")
            runtime.in_flight += 1
            admitted = True
            dispatched = operation in PUBLIC_OPERATIONS
            return _result(_safe(await runtime.call(operation, arguments), token))
        except AuthorityError as error:
            return _result({"error": _safe(error.to_dict(), token)}, error=True)
        except Exception:
            return _result({"error": {
                "code": "internal_error", "message": "Internal task service failure",
                "details": {}, "ambiguous": dispatched,
            }}, error=True)
        finally:
            if admitted:
                runtime.in_flight -= 1

    manager = StreamableHTTPSessionManager(
        sdk, json_response=True, stateless=True, max_request_body_size=MAX_REQUEST_BYTES,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[binding],
            allowed_origins=[f"http://{binding}"],
        ),
    )

    @asynccontextmanager
    async def lifespan(_):
        with _protect_sdk_logs():
            async with manager.run(), anyio.create_task_group() as workers:
                await runtime.start()
                workers.start_soon(runtime.maintain)
                if projector is not None:
                    workers.start_soon(runtime.project)
                try:
                    yield
                finally:
                    workers.cancel_scope.cancel()

    app = Starlette(routes=[Route("/mcp", _MCPRoute(manager))], lifespan=lifespan)
    app.router.redirect_slashes = False
    app.add_middleware(_Boundary, token=token, binding=binding)
    return app


def serve(authority, maintenance, *, token, host="127.0.0.1", port=8766, projector=None):
    """Foreground server. Ownership remains with the caller's context manager."""
    import uvicorn

    app = create_app(authority, maintenance, token=token, host=host, port=port, projector=projector)
    uvicorn.run(app, host=host, port=port, access_log=False, log_level="warning",
                ws="none", limit_concurrency=128, timeout_graceful_shutdown=30)
