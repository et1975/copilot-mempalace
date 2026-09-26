"""Explicit deployment configuration; loading never starts a writer or service.

Schema 2 uses the palace journal for recovery; its runtime directory is only
disposable coordination. Launcher consent is explicit.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from .domain import decide
from .model import DomainError, new_state
from . import platform_support


class ConfigError(Exception):
    def __init__(self, message, *, code="invalid_configuration"):
        super().__init__(message)
        self.code = code


def real_path(value):
    """Validate native path identity without following symlinks/reparse points."""
    try:
        return platform_support.canonical_path(value)
    except platform_support.PlatformError as error:
        raise ConfigError(error.message, code=error.code) from None


def validate_binding(host, port, *, allow_dynamic=False):
    if type(allow_dynamic) is not bool:
        raise ConfigError("allow_dynamic must be a boolean")
    try:
        address = ipaddress.ip_address(host) if type(host) is str else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback or str(address) != host:
        raise ConfigError("Service host must be a canonical numeric loopback address")
    minimum = 0 if allow_dynamic else 1
    if type(port) is not int or not minimum <= port <= 65535:
        raise ConfigError(f"Service port must be an integer in {minimum}..65535")
    return f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"


def validate_token(token):
    if type(token) is not str or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None:
        raise ConfigError("Credential must be a nonempty ASCII bearer token")
    return token


def _read_regular(path, *, private=False, maximum=1024 * 1024):
    """Validate the opened native object without creating/repairing any path."""
    try:
        return platform_support.read_regular(path, private=private, maximum=maximum)
    except platform_support.PlatformError as error:
        raise ConfigError(error.message, code=error.code) from None


def read_token(path):
    try:
        text = _read_regular(path, private=True, maximum=4096).decode("ascii")
    except UnicodeError:
        raise ConfigError("Credential must be ASCII") from None
    return validate_token(text.removesuffix("\n"))


def create_service_token(path):
    """Explicit init-only creation. Existing files, including links, are untouched."""
    path = real_path(path)
    try:
        platform_support.create_private(path, (secrets.token_urlsafe(48) + "\n").encode("ascii"))
    except platform_support.PlatformError as error:
        raise ConfigError(error.message, code=error.code) from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite")


def _endpoint(value):
    if (type(value) is not str or not value or any(ord(c) <= 32 for c in value)
            or "\\" in value):
        raise ConfigError("Hub URL must be an exact HTTP(S) endpoint")
    try:
        url = urlsplit(value)
        valid = (url.scheme in {"http", "https"} and url.hostname
                 and url.username is None and url.password is None
                 and not url.query and not url.fragment and "?" not in value and "#" not in value
                 and url.path.startswith("/") and url.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError("Hub URL must be an exact HTTP(S) endpoint without credentials")
    return value


@dataclass(frozen=True)
class ServiceConfig:
    """Current deployment inputs and disposable runtime coordination."""

    authority_id: str
    hub_url: str
    service_token_file: Path
    runtime_dir: Path
    genesis: dict = field(repr=False)
    configuration: dict = field(repr=False)
    maintenance_actor: str
    recovery_actor: str
    host: str = "127.0.0.1"
    port: int = 8766
    hub_token_file: Path | None = None
    service_token: str | None = field(default=None, repr=False)
    hub_token: str | None = field(default=None, repr=False)
    schema_version: int = field(default=2, init=False)
    lifecycle: str = "external"

    @property
    def service_url(self):
        if type(self.port) is int and self.port == 0:
            raise ConfigError("Discover the bound service endpoint before connecting",
                              code="discovery_required")
        return f"http://{validate_binding(self.host, self.port)}/mcp"


def load_config(path=None, *, environ=None, allow_missing_service_token=False):
    if path is None:
        path = (os.environ if environ is None else environ).get("MPTASK_CONFIG")
    if path is None:
        raise ConfigError("Supply --config or MPTASK_CONFIG")
    try:
        document = json.loads(_read_regular(path), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError("Configuration must be unambiguous JSON") from None
    if type(document) is not dict:
        raise ConfigError("Configuration has missing or unknown fields")
    version = document.get("schema_version")
    if type(version) is not int or version != 2:
        raise ConfigError("Unsupported configuration schema version")
    required = {"schema_version", "authority_id", "hub_url", "service_token_file",
                "runtime_dir", "genesis", "maintenance_actor", "recovery_actor"}
    allowed = required | {"host", "port", "hub_token_file", "lifecycle"}
    if document.keys() - allowed or required - document.keys():
        raise ConfigError("Configuration has missing or unknown fields")
    lifecycle = document.get("lifecycle", "external")
    if type(lifecycle) is not str or lifecycle not in {"external", "launcher"}:
        raise ConfigError("Lifecycle must be external or launcher")
    identity = document["authority_id"]
    try:
        if type(identity) is not str or str(UUID(identity)) != identity:
            raise ValueError("identity")
    except (ValueError, AttributeError):
        raise ConfigError("Authority identity must be a canonical UUID") from None
    host, port = document.get("host", "127.0.0.1"), document.get("port", 8766)
    validate_binding(host, port, allow_dynamic=True)
    hub_url = _endpoint(document["hub_url"])
    runtime_dir = real_path(document["runtime_dir"])
    if runtime_dir.exists() and not runtime_dir.is_dir():
        raise ConfigError("Runtime path must be a directory")
    genesis = document["genesis"]
    if (type(genesis) is not dict or set(genesis) - {
            "actor", "actors", "execution_profiles", "supervisors", "policy"}):
        raise ConfigError("Genesis has unknown fields")
    try:
        event = decide(new_state(identity), {
            **genesis, "operation": "authority_create",
            "command_id": "00000000-0000-0000-0000-000000000001",
        }, "2000-01-01T00:00:00Z")
        normalized = event["configuration"]
    except DomainError:
        raise ConfigError("Genesis actors, profiles, supervisors or policy are invalid") from None
    system, recovery = document["maintenance_actor"], document["recovery_actor"]
    if (type(system) is not str or type(recovery) is not str or system == recovery
            or normalized["actors"].get(system) != "system"
            or normalized["actors"].get(recovery) != "operator"):
        raise ConfigError("Distinct registered system and operator maintenance identities required")
    service_path = real_path(document["service_token_file"])
    service_token = (None if allow_missing_service_token and not service_path.exists()
                     else read_token(service_path))
    hub_path = document.get("hub_token_file")
    hub_path = real_path(hub_path) if hub_path is not None else None
    hub_token = read_token(hub_path) if hub_path is not None else None
    return ServiceConfig(
        authority_id=identity, hub_url=hub_url, service_token_file=service_path,
        runtime_dir=runtime_dir, genesis=deepcopy(genesis), configuration=normalized,
        maintenance_actor=system, recovery_actor=recovery, host=host, port=port,
        hub_token_file=hub_path,
        service_token=service_token, hub_token=hub_token,
        lifecycle=lifecycle,
    )
