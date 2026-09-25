"""Disposable discovery for one explicitly configured, loopback task authority.

``publish_registry`` is an owner-only integration hook: call it after acquiring
the authority lock and binding the listener, retain both until shutdown, and
answer /identity even while unready. A record grants neither ownership nor
authorization. Missing records never initialize an authority or start a hub.

GET /identity?nonce=<64 lowercase hex> accepts no bearer and returns the exact
``server_identity.make_proof`` document. Readiness means journal recovery,
configuration checks, and the activation barrier have completed. Each connect
authenticates that proof before deriving an instance-scoped MCP/control bearer.

In journal mode, instance_id is the authority's accepted epoch_id, not a
launcher-generated incarnation. Only the activated authority can supply it.
ConnectionInfo is an immutable observation: callers freeze this epoch with each
logical mutation. Discovery neither injects expected_epoch into commands nor
refreshes it on retries; a later explicit connect produces a separate snapshot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlsplit

import httpx

from . import platform_support as platform
from .client import _quiet_sdk_logs
from .codec import canonical_json
from .config import ServiceConfig, _constant, _pairs
from .server_identity import IdentityError, InstanceIdentity, instance_bearer, verify_proof


_REGISTRY_LIMIT = 8192
_RESPONSE_LIMIT = 16384
_REGISTRY_FIELDS = {"schema_version", "authority_id", "instance_id", "endpoint", "binding", "pid"}


class DiscoveryError(Exception):
    """Safe diagnostics; ``ambiguous`` means a control outcome is unconfirmed."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.code, self.message, self.ambiguous = code, message, ambiguous


@dataclass(frozen=True)
class ConnectionInfo:
    """Authenticated connection snapshot; instance_id is the accepted journal epoch."""

    url: str
    authority_id: str
    instance_id: str
    token: str = field(repr=False)
    ready: bool

    def public(self) -> dict:
        """CLI/status output deliberately omits credentials."""
        return {"url": self.url, "authority_id": self.authority_id,
                "instance_id": self.instance_id, "ready": self.ready}


class _Deadline:
    def __init__(self, timeout: float):
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise DiscoveryError("invalid_timeout", "Timeout must be in (0, 300] seconds")
        self.end = time.monotonic() + timeout

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise DiscoveryError("timeout", "Operation exceeded its deadline")
        return remaining

    def pause(self) -> None:
        time.sleep(min(0.05, self.remaining()))


def binding_fingerprint(config: ServiceConfig) -> str:
    """Hash the nonsecret deployment binding, never credential contents.

    Changing credentials revokes proofs without making the cache a credential
    authority. Local paths identify this deployment, not a durable recovery set.
    """
    fields = {
        "schema_version": config.schema_version, "authority_id": config.authority_id,
        "hub_url": config.hub_url,
        "runtime_dir": str(platform.canonical_path(config.runtime_dir)),
        "service_token_file": str(platform.canonical_path(config.service_token_file)),
        "hub_token_file": (str(platform.canonical_path(config.hub_token_file))
                           if config.hub_token_file is not None else None),
        "host": config.host, "port": config.port, "lifecycle": config.lifecycle,
        "recovery_mode": config.recovery_mode, "configuration": config.configuration,
        "maintenance_actor": config.maintenance_actor, "recovery_actor": config.recovery_actor,
        "project_wings": config.project_wings, "projections_enabled": config.projections_enabled,
    }
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def _bound(config: ServiceConfig, identity: InstanceIdentity) -> bool:
    endpoint = urlsplit(identity.endpoint)
    return (identity.authority_id == config.authority_id
            and endpoint.scheme == "http" and endpoint.hostname == config.host
            and (config.port == 0 or endpoint.port == config.port))


def publish_registry(config: ServiceConfig, identity: InstanceIdentity) -> None:
    """Atomically publish private *disposable* bytes; caller owns lock/listener.

    This does not acquire ownership, create runtime directories, or certify
    readiness. The parent CLI/server must enforce the ownership precondition
    and use the accepted authority.epoch_id as identity.instance_id in journal
    mode, only after the activation barrier has completed.
    """
    if not isinstance(identity, InstanceIdentity) or not _bound(config, identity):
        raise DiscoveryError("binding_mismatch", "Listener identity differs from configuration")
    document = {
        "schema_version": 1, "authority_id": identity.authority_id,
        "instance_id": identity.instance_id, "endpoint": identity.endpoint,
        "binding": binding_fingerprint(config), "pid": os.getpid(),
    }
    platform.atomic_write_cache(config.runtime_dir / "serverinfo.json",
                                canonical_json(document).encode("utf-8"))


def _decode(data: bytes, code: str = "invalid_response") -> dict:
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if type(result) is not dict:
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise DiscoveryError(code, "Expected a bounded, unambiguous JSON object") from None


def _read_registry(config: ServiceConfig) -> InstanceIdentity:
    try:
        document = _decode(platform.read_regular(
            config.runtime_dir / "serverinfo.json", private=True, maximum=_REGISTRY_LIMIT),
            "invalid_registry")
    except platform.PlatformError as error:
        code = "registry_missing" if error.code == "not_found" else "invalid_registry"
        raise DiscoveryError(code, "Discovery record is missing or unsafe") from None
    if (set(document) != _REGISTRY_FIELDS or type(document["schema_version"]) is not int
            or document["schema_version"] != 1 or type(document["pid"]) is not int
            or not 0 < document["pid"] <= 2**32 - 1):
        raise DiscoveryError("invalid_registry", "Invalid discovery record schema")
    if document["binding"] != binding_fingerprint(config):
        raise DiscoveryError("binding_mismatch", "Discovery record belongs to another configuration")
    try:
        identity = InstanceIdentity(document["authority_id"], document["instance_id"],
                                    document["endpoint"])
    except IdentityError:
        raise DiscoveryError("invalid_registry", "Invalid discovery record identity") from None
    if not _bound(config, identity):
        raise DiscoveryError("invalid_registry", "Discovery endpoint differs from configuration")
    return identity


async def _exchange(identity: InstanceIdentity, deadline: _Deadline, *, method: str,
                    path: str, token: str | None, body: dict | None,
                    status: int) -> dict:
    # Reuse httpx's native cancellation/transport and the existing log privacy
    # boundary. No resolver, proxy, redirects, SDK retries, or reusable root token.
    async with asyncio.timeout(deadline.remaining()):
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        encoded = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            encoded = canonical_json(body).encode("utf-8")
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(deadline.remaining()),
        ) as client:
            async with client.stream(
                method, identity.endpoint.removesuffix("/mcp") + path,
                headers=headers, content=encoded,
            ) as response:
                if response.status_code != status:
                    raise DiscoveryError("http_error", "Unexpected discovery/control HTTP status")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise DiscoveryError("invalid_response", "Encoded HTTP bodies are unsupported")
                if (response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        != "application/json"):
                    raise DiscoveryError("invalid_response", "Expected a JSON HTTP response")
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        raise DiscoveryError("invalid_response", "Invalid HTTP content length")
                    trimmed = length.lstrip("0") or "0"
                    if len(trimmed) > 5 or int(trimmed) > _RESPONSE_LIMIT:
                        raise DiscoveryError("body_too_large", "HTTP response exceeds its limit")
                chunks = bytearray()
                async for chunk in response.aiter_raw():
                    if len(chunks) + len(chunk) > _RESPONSE_LIMIT:
                        raise DiscoveryError("body_too_large", "HTTP response exceeds its limit")
                    chunks.extend(chunk)
                if length is not None and len(chunks) != int(trimmed):
                    raise DiscoveryError("invalid_response", "Incomplete HTTP response")
                deadline.remaining()
                return _decode(bytes(chunks))


def _request(identity: InstanceIdentity, deadline: _Deadline, *, method="GET",
             path: str, token=None, body=None, status=200) -> dict:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise DiscoveryError("invalid_context", "Synchronous discovery requires no running event loop")
    try:
        with _quiet_sdk_logs():
            return asyncio.run(_exchange(identity, deadline, method=method, path=path,
                                         token=token, body=body, status=status))
    except (TimeoutError, httpx.TimeoutException):
        raise DiscoveryError("timeout", "Operation exceeded its deadline") from None
    except httpx.ConnectError:
        raise DiscoveryError("endpoint_unavailable", "Discovered endpoint is unavailable") from None
    except httpx.RequestError:
        raise DiscoveryError("transport_error", "Discovery/control transport failed") from None


def _probe(config: ServiceConfig, identity: InstanceIdentity, deadline: _Deadline, *,
           require_ready=True) -> ConnectionInfo:
    nonce = secrets.token_hex(32)
    response = _request(identity, deadline, path="/identity?nonce=" + nonce)
    try:
        ready = verify_proof(identity, config.service_token, nonce, response)
        if require_ready and not ready:
            raise DiscoveryError("not_ready", "Authenticated owner is not ready")
        token = instance_bearer(identity, config.service_token)
    except IdentityError as error:
        raise DiscoveryError(error.code, error.message) from None
    deadline.remaining()
    return ConnectionInfo(identity.endpoint, identity.authority_id, identity.instance_id,
                          token, ready)


def connect(config: ServiceConfig, *, timeout: float = 5.0) -> ConnectionInfo:
    """Read-only, ready-only connect, bounded end-to-end; never auto-starts."""
    deadline = _Deadline(timeout)
    identity = _read_registry(config)
    return _probe(config, identity, deadline)
