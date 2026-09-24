"""Explicit deployment configuration; loading never starts a writer or service."""

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
from urllib.parse import urlsplit
from uuid import UUID

from .domain import decide
from .model import DomainError, new_state


class ConfigError(Exception):
    def __init__(self, message, *, code="invalid_configuration"):
        super().__init__(message)
        self.code = code


def real_path(value):
    """Reject lexical aliases and symlinks, including existing ancestors."""
    if not isinstance(value, (str, Path)):
        raise ConfigError("Expected an absolute filesystem path")
    text = str(value)
    path = Path(text)
    if (not path.is_absolute() or str(path) != text
            or ".." in path.parts or any(ord(c) < 32 for c in text)):
        raise ConfigError("Expected an absolute, non-aliased filesystem path")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ConfigError("Symlink paths are not supported")
        if component != path and component.exists() and not component.is_dir():
            raise ConfigError("Path ancestor must be a directory")
    return path


def validate_binding(host, port):
    try:
        address = ipaddress.ip_address(host) if type(host) is str else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback or str(address) != host:
        raise ConfigError("Service host must be a canonical numeric loopback address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("Service port must be an integer in 1..65535")
    return f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"


def validate_token(token):
    if type(token) is not str or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None:
        raise ConfigError("Credential must be a nonempty ASCII bearer token")
    return token


@contextmanager
def _parent_directory(path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_regular(path, *, private=False, maximum=1024 * 1024):
    """Validate without creating or repairing files/directories.

    JsonStore's permission enforcement belongs to owned recovery state, never
    configuration or credential reads in a repository/shared parent directory.
    """
    path = real_path(path)
    try:
        with _parent_directory(path) as directory:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (private and (opened.st_mode & 0o077 or opened.st_uid != os.geteuid()))):
                    raise ConfigError("Credential/config file is not a safe regular file")
                data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ConfigError("Credential/config file exceeds its size limit")
        return data
    except OSError:
        raise ConfigError("Cannot read the credential/config file") from None


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
        with _parent_directory(path) as directory:
            descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(secrets.token_urlsafe(48) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
    except OSError:
        raise ConfigError("Cannot exclusively create the service credential") from None


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
    authority_id: str
    hub_url: str
    service_token_file: Path
    state_dir: Path
    genesis: dict = field(repr=False)
    configuration: dict = field(repr=False)
    maintenance_actor: str
    recovery_actor: str
    host: str = "127.0.0.1"
    port: int = 8766
    hub_token_file: Path | None = None
    project_wings: dict = field(default_factory=dict)
    projections_enabled: bool = False
    service_token: str | None = field(default=None, repr=False)
    hub_token: str | None = field(default=None, repr=False)

    @property
    def service_url(self):
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
    required = {"schema_version", "authority_id", "hub_url", "service_token_file",
                "state_dir", "genesis", "maintenance_actor", "recovery_actor"}
    allowed = required | {"host", "port", "hub_token_file", "project_wings", "projections_enabled"}
    if type(document) is not dict or document.keys() - allowed or required - document.keys():
        raise ConfigError("Configuration has missing or unknown fields")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ConfigError("Unsupported configuration schema version")
    identity = document["authority_id"]
    try:
        if type(identity) is not str or str(UUID(identity)) != identity:
            raise ValueError("identity")
    except (ValueError, AttributeError):
        raise ConfigError("Authority identity must be a canonical UUID") from None
    host, port = document.get("host", "127.0.0.1"), document.get("port", 8766)
    validate_binding(host, port)
    hub_url = _endpoint(document["hub_url"])
    state_dir = real_path(document["state_dir"])
    if state_dir.exists() and not state_dir.is_dir():
        raise ConfigError("State path must be a directory")
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
    wings = document.get("project_wings", {})
    if (type(wings) is not dict or any(type(k) is not str or not k.strip()
                                     or type(v) is not str or not v.strip()
                                     for k, v in wings.items())):
        raise ConfigError("Project wings must be explicit nonblank string mappings")
    enabled = document.get("projections_enabled", False)
    if type(enabled) is not bool:
        raise ConfigError("projections_enabled must be a boolean")
    service_path = real_path(document["service_token_file"])
    service_token = (None if allow_missing_service_token and not service_path.exists()
                     else read_token(service_path))
    hub_path = document.get("hub_token_file")
    hub_path = real_path(hub_path) if hub_path is not None else None
    hub_token = read_token(hub_path) if hub_path is not None else None
    return ServiceConfig(
        authority_id=identity, hub_url=hub_url, service_token_file=service_path,
        state_dir=state_dir, genesis=deepcopy(genesis), configuration=normalized,
        maintenance_actor=system, recovery_actor=recovery, host=host, port=port,
        hub_token_file=hub_path, project_wings=deepcopy(wings), projections_enabled=enabled,
        service_token=service_token, hub_token=hub_token,
    )
