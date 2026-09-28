"""Official SDK stateless JSON MCP over one explicitly owned task authority."""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import hmac
from ipaddress import ip_address
import json
import logging
import re
import socket
import time

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .authority import AuthorityError
from .config import ConfigError, _constant, _pairs, validate_binding, validate_token
from .domain import CLASSES, CONTENT, DEFAULT_POLICY, EDGE_TYPES, EXECUTION, ROLES, SCHEMAS, STATUSES
from .lifecycle import HttpLifecycle
from .server_identity import InstanceIdentity, make_proof


MAX_REQUEST_BYTES = 256 * 1024
MAX_IN_FLIGHT = 32
PUBLIC_OPERATIONS = tuple(name for name in SCHEMAS if name not in {"authority_create", "expire"})
READ_OPERATIONS = ("get", "snapshot", "list", "ready", "history", "health", "wait_ready", "outcome")
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
    uuid = {"type": "string",
            "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
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
        result[name]["properties"]["expected_epoch"] = deepcopy(uuid)
        result[name]["required"].append("expected_epoch")
    result["outcome"] = _object({
        "epoch_id": deepcopy(uuid), "command_id": deepcopy(uuid),
    }, ("epoch_id", "command_id"))
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
    if isinstance(value, dict) and set(value) == {"authorized"} and value["authorized"] is False:
        return True
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
    def __init__(self, app, token, binding, lifecycle=None):
        self.app, self.token, self.binding = app, token.encode("ascii"), binding
        self.lifecycle = lifecycle

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope["headers"]
            auth = [value for key, value in headers if key.lower() == b"authorization"]
            expected = b"Bearer " + self.token
            public = self.lifecycle is not None and scope["path"] == "/identity"
            instance = (None if self.lifecycle is None else
                        b"Bearer " + self.lifecycle.instance_token.encode("ascii"))
            accepted = len(auth) == 1 and (
                hmac.compare_digest(auth[0], expected)
                and (self.lifecycle is None or scope["path"] != "/control/stop")
                or instance is not None and hmac.compare_digest(auth[0], instance))
            if not public and not accepted:
                return await Response("Unauthorized", 401)(scope, receive, send)
            hosts = [value for key, value in headers if key.lower() == b"host"]
            origins = [value for key, value in headers if key.lower() == b"origin"]
            if hosts != [self.binding.encode("ascii")]:
                return await Response("Invalid Host", 421)(scope, receive, send)
            if origins and origins != [f"http://{self.binding}".encode("ascii")]:
                return await Response("Invalid Origin", 403)(scope, receive, send)
            if self.lifecycle is not None:
                try:
                    loopback = scope.get("client") and ip_address(scope["client"][0]).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    return await Response("Loopback required", 403)(scope, receive, send)
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


class _Runtime:
    def __init__(self, authority, maintenance, token, lifecycle=None):
        self.authority, self.maintenance, self.token = authority, maintenance, token
        self.sweep = authority.state.configuration["policy"]["sweep_seconds"]
        self.last_tick = float("-inf")
        self.maintenance_status = {"ok": False, "ticks": 0, "reason": "not_started"}
        self.in_flight = 0
        self.mutations = 0
        self.lifecycle = lifecycle

    async def start(self):
        self.request_limit = anyio.CapacityLimiter(4)
        self.maintenance_limit = anyio.CapacityLimiter(1)
        self.tick_lock = anyio.Lock()
        self.stop_lock = anyio.Lock()
        await self.tick_if_due()
        if self.lifecycle is not None:
            for _ in range(10000):
                health = await self.dispatch(self.authority.health)
                if not health.get("startup_pending"):
                    break
                await self.tick_if_due(force=True)
            if not health["fresh"] or not self.maintenance_status["ok"]:
                raise AuthorityError("not_ready", "Authority startup did not complete")

    async def dispatch(self, function, *args, **kwargs):
        return await anyio.to_thread.run_sync(
            lambda: function(*args, **kwargs), limiter=self.request_limit)

    async def tick_if_due(self, *, force=False):
        async with self.tick_lock:
            if not force and time.monotonic() - self.last_tick < self.sweep:
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
            command = dict(arguments)
            epoch = command.pop("expected_epoch")
            return await self.dispatch(self.authority.execute, {"operation": name, **command},
                                       expected_epoch=epoch)
        if name == "ready":
            return await self.ready(arguments)
        if name == "wait_ready":
            return await self.wait_ready(arguments)
        if name == "health":
            return {**await self.dispatch(self.authority.health),
                    "maintenance": deepcopy(self.maintenance_status),
                    **({"lifecycle": {"state": self.lifecycle.phase,
                                      "failure": self.lifecycle.failure}}
                       if self.lifecycle is not None else {})}
        method = self.authority.snapshot if name in {"snapshot", "list"} else getattr(self.authority, name)
        return await self.dispatch(method, **arguments)

    def admit_mutation(self, name, arguments):
        if self.lifecycle is None or self.lifecycle.phase == "ready":
            return
        completion = (name in {"renew", "checkpoint", "release", "recover", "transition"}
                      or name == "attempt_report" and arguments.get("report_kind") != "started")
        if self.lifecycle.phase != "draining" or not completion:
            raise AuthorityError("service_draining", "New task intake is disabled")

    async def identity_ready(self):
        if self.lifecycle.phase != "ready" or self.mutations:
            return False
        try:
            await self.dispatch(self.authority.refresh, verify_prefix=True)
            health = await self.dispatch(self.authority.health)
        except AuthorityError as error:
            self.lifecycle.failure = error.code
            return False
        ready = (health["started"] and health["fresh"] and not health.get("startup_pending")
                 and health.get("epoch_id") == self.lifecycle.identity.instance_id
                 and self.maintenance_status["ok"])
        self.lifecycle.failure = None if ready else health.get("reason") or "maintenance_failed"
        return ready

    async def drain(self):
        self.lifecycle.phase = "draining"
        deadline = time.monotonic() + self.lifecycle.drain_timeout
        blockers = {"in_flight_mutations": self.mutations}
        for _ in range(6001):
            if not self.mutations:
                await self.tick_if_due(force=True)

                def inspect():
                    with self.authority.serialized():
                        health = self.authority.health()
                        tasks = self.authority.state.tasks.values()
                        active = [task["id"] for task in tasks if task["status"] == "in_progress"]
                        recovery = [task["id"] for task in tasks if task["recovery"] is not None
                                    and not task["recovery"]["barrier_satisfied"]]
                        return health, active, recovery

                health, active, recovery = await self.dispatch(inspect)
                blockers = {
                    "in_flight_mutations": self.mutations, "active_attempts": active[:100],
                    "active_attempt_count": len(active), "recovery_required": recovery[:100],
                    "recovery_required_count": len(recovery), "fresh": health["fresh"],
                    "reason": health["reason"],
                    "pending_command": health["pending_command"],
                    "maintenance": deepcopy(self.maintenance_status),
                }
                if (not self.mutations and not active and not recovery and health["fresh"]
                        and self.maintenance_status["ok"]
                        and not self.maintenance_status.get("pending_requests")):
                    self.lifecycle.phase = "drained"
                    self.lifecycle.failure = None
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await anyio.sleep(min(0.05, remaining))
        self.lifecycle.failure = "drain_blocked"
        raise AuthorityError("drain_blocked", "Drain has active work or unresolved uncertainty", blockers)


async def _bounded_body(request, maximum):
    lengths = request.headers.getlist("content-length")
    if (len(lengths) > 1 or lengths and (
            not lengths[0].isascii() or not lengths[0].isdigit())):
        raise ValueError("Invalid length")
    if lengths and (len(lengths[0]) > 6 or int(lengths[0]) > maximum):
        raise OverflowError("Body too large")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise ValueError("Encoded body")
    body = bytearray()
    with anyio.fail_after(5):
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise OverflowError("Body too large")
            body.extend(chunk)
    if lengths and len(body) != int(lengths[0]):
        raise ValueError("Incomplete body")
    return bytes(body)


def create_app(authority, maintenance, *, token, host="127.0.0.1", port=8766,
               lifecycle=None):
    """Borrow started authority ownership; the caller closes it after ASGI shutdown."""
    binding = validate_binding(host, port)
    validate_token(token)
    if lifecycle is not None and (
            lifecycle.identity.instance_id != authority.epoch_id
            or lifecycle.identity.authority_id != authority.authority_id
            or lifecycle.identity.endpoint != f"http://{binding}/mcp"):
        raise AuthorityError("identity_mismatch", "Listener must use the accepted authority epoch")
    runtime = _Runtime(authority, maintenance, token, lifecycle)
    schemas = _schemas()
    validators = {key: Draft202012Validator(value) for key, value in schemas.items()}
    sdk = Server("mempalace-tasks", version="1")

    @sdk.list_tools()
    async def list_tools():
        return [types.Tool(
            name=f"mptask_{name}", description=(
                "Read diagnostic task state; never authorizes execution."
                if name in READ_OPERATIONS else
                f"Apply {name}; retain command_id and frozen expected_epoch; "
                "never upgrade an ambiguous request. Actor must be registered."),
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
            if operation in PUBLIC_OPERATIONS and isinstance(arguments, dict) and (
                    "expected_epoch" not in arguments):
                raise AuthorityError("epoch_required", "A frozen request epoch is required")
            if next(validators[operation].iter_errors(arguments), None) is not None:
                raise AuthorityError("validation_error", "Arguments do not match this tool's schema")
            if operation in PUBLIC_OPERATIONS:
                if arguments["expected_epoch"] != authority.epoch_id:
                    raise AuthorityError("stale_epoch", "Request epoch differs from this owner")
                runtime.admit_mutation(operation, arguments)
            if runtime.in_flight >= MAX_IN_FLIGHT:
                raise AuthorityError("service_busy", "Task service concurrency limit reached")
            runtime.in_flight += 1
            admitted = True
            dispatched = operation in PUBLIC_OPERATIONS
            if dispatched:
                runtime.mutations += 1
            payload = _safe(await runtime.call(operation, arguments), token)
            if lifecycle is not None:
                payload = _safe(payload, lifecycle.instance_token)
            return _result(payload)
        except AuthorityError as error:
            payload = _safe(error.to_dict(), token)
            if lifecycle is not None:
                payload = _safe(payload, lifecycle.instance_token)
            return _result({"error": payload}, error=True)
        except Exception:
            return _result({"error": {
                "code": "internal_error", "message": "Internal task service failure",
                "details": {}, "ambiguous": dispatched,
            }}, error=True)
        finally:
            if admitted:
                runtime.in_flight -= 1
                if dispatched:
                    runtime.mutations -= 1

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
                try:
                    yield
                finally:
                    workers.cancel_scope.cancel()

    async def identity(request):
        if re.fullmatch(rb"nonce=[0-9a-f]{64}", request.scope["query_string"]) is None:
            return Response("Invalid challenge", 400)
        try:
            await _bounded_body(request, 0)
        except OverflowError:
            return Response("Body too large", 413)
        except ValueError:
            return Response("Invalid request", 400)
        except TimeoutError:
            return Response("Request timeout", 408)
        nonce = request.scope["query_string"][6:].decode("ascii")
        try:
            ready = await runtime.identity_ready()
        except Exception:
            lifecycle.failure = "identity_failed"
            return JSONResponse({"error": {
                "code": "identity_failed", "message": "Cannot verify current owner readiness",
                "details": {}, "ambiguous": False,
            }}, status_code=503)
        return JSONResponse(make_proof(lifecycle.identity, token, nonce, ready=ready))

    async def control_stop(request):
        try:
            if (request.scope["query_string"]
                    or request.headers.get("content-type", "").split(";", 1)[0] != "application/json"):
                raise ValueError("Invalid request")
            raw = await _bounded_body(request, 2048)
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            if (type(data) is not dict
                    or set(data) != {"schema_version", "instance_id", "drain", "nonce"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1
                    or data["drain"] is not True or type(data["nonce"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", data["nonce"]) is None):
                raise ValueError("Invalid request")
        except OverflowError:
            return Response("Body too large", 413)
        except (ValueError, UnicodeError, RecursionError):
            return Response("Invalid control request", 400)
        except TimeoutError:
            return Response("Request timeout", 408)
        if data["instance_id"] != lifecycle.identity.instance_id:
            return Response("Instance changed", 409)
        if runtime.stop_lock.locked() or lifecycle.phase == "drained":
            return Response("Drain already requested", 409)
        async with runtime.stop_lock:
            try:
                await runtime.drain()
            except AuthorityError as error:
                return JSONResponse({"error": _safe(_safe(error.to_dict(), token),
                                                    lifecycle.instance_token)}, status_code=409)
            except Exception:
                lifecycle.phase, lifecycle.failure = "draining", "drain_failed"
                return JSONResponse({"error": {
                    "code": "drain_failed", "message": "Drain completion could not be verified",
                    "details": {}, "ambiguous": True,
                }}, status_code=503)
            return JSONResponse({
                "schema_version": 1, "authority_id": lifecycle.identity.authority_id,
                "instance_id": lifecycle.identity.instance_id, "accepted": True, "drained": True,
                "proof": make_proof(lifecycle.identity, token, data["nonce"], ready=False),
            }, status_code=202, background=BackgroundTask(lifecycle.shutdown))

    routes = [Route("/mcp", _MCPRoute(manager))]
    if lifecycle is not None:
        routes.extend([Route("/identity", identity, methods=["GET"]),
                       Route("/control/stop", control_stop, methods=["POST"])])
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.runtime = runtime
    app.router.redirect_slashes = False
    app.add_middleware(_Boundary, token=token, binding=binding, lifecycle=lifecycle)
    return app


def serve(authority, maintenance, *, token, host="127.0.0.1", port=8766,
          config, on_ready=None):
    """Foreground server; caller retains ownership until ASGI and writers finish.

    The configured listener enables identity/control and binds port zero exactly once.
    on_ready releases startup election only after listener/authority readiness
    and registry publication. No upstream startup or idle shutdown occurs here.
    """
    import uvicorn

    validate_binding(host, port, allow_dynamic=True)
    if (host, port, token) != (config.host, config.port, config.service_token):
        raise ConfigError("Listener differs from configured binding")
    family = socket.AF_INET6 if ip_address(host).version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, port))
        listener.listen(128)
        listener.setblocking(False)
        bound_port = listener.getsockname()[1]
        identity = InstanceIdentity(authority.authority_id, authority.epoch_id,
                                    f"http://{validate_binding(host, bound_port)}/mcp")

        def shutdown():
            runner.should_exit = True

        lifecycle = HttpLifecycle(config, identity, shutdown=shutdown, on_ready=on_ready)
        app = create_app(authority, maintenance, token=token, host=host, port=bound_port,
                         lifecycle=lifecycle)

        class PortableServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if not self.started or self.should_exit:
                    raise AuthorityError("startup_failed", "HTTP listener startup failed")
                try:
                    lifecycle.publish()
                except BaseException:
                    await self.shutdown(sockets=sockets)
                    raise

        runner = PortableServer(uvicorn.Config(
            app, host=host, port=bound_port, access_log=False, log_level="warning",
            ws="none", proxy_headers=False, limit_concurrency=128, timeout_graceful_shutdown=None))
        try:
            runner.run(sockets=[listener])
        except BaseException:
            lifecycle.phase = "failed"
            lifecycle.failure = "listener_failed"
            raise
        finally:
            lifecycle.clear()
        lifecycle.phase = "stopped"
