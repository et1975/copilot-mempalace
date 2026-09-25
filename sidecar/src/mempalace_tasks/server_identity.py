"""Instance-scoped discovery proofs, not task execution authorization."""

import base64
from dataclasses import dataclass
import hashlib
import hmac
from ipaddress import ip_address
import re
from urllib.parse import urlsplit
from uuid import UUID

from .codec import canonical_json
from .config import ConfigError, validate_token


_HEX_256 = re.compile(r"[0-9a-f]{64}")
_PROOF_FIELDS = {
    "schema_version", "authority_id", "instance_id", "endpoint", "nonce", "ready", "mac",
}


class IdentityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _uuid(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise IdentityError("invalid_identity", "Identity requires canonical UUIDs") from None


def _endpoint(value):
    try:
        if (type(value) is not str or len(value) > 2048
                or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
            raise ValueError
        parts = urlsplit(value)
        address = ip_address(parts.hostname)
        port = parts.port
        host = f"[{address}]" if address.version == 6 else str(address)
        if (parts.scheme not in {"http", "https"} or not address.is_loopback
                or port is None or not 1 <= port <= 65535
                or value != f"{parts.scheme}://{host}:{port}/mcp"):
            raise ValueError
    except (ValueError, TypeError):
        raise IdentityError(
            "invalid_endpoint", "Identity requires an exact numeric loopback MCP endpoint",
        ) from None


def _secret(value):
    if type(value) is not str or not 1 <= len(value) <= 4096:
        raise IdentityError("invalid_credential", "A bounded service credential is required")
    try:
        return validate_token(value).encode("ascii")
    except ConfigError:
        raise IdentityError("invalid_credential", "Invalid service credential") from None


def _nonce(value):
    if type(value) is not str or _HEX_256.fullmatch(value) is None:
        raise IdentityError("invalid_nonce", "A fresh 256-bit hexadecimal nonce is required")


def _fields(identity):
    if not isinstance(identity, InstanceIdentity):
        raise IdentityError("invalid_identity", "A validated instance identity is required")
    return {"authority_id": identity.authority_id, "instance_id": identity.instance_id,
            "endpoint": identity.endpoint}


@dataclass(frozen=True)
class InstanceIdentity:
    authority_id: str
    instance_id: str
    endpoint: str

    def __post_init__(self):
        _uuid(self.authority_id)
        _uuid(self.instance_id)
        _endpoint(self.endpoint)


def make_proof(identity, secret, nonce, *, ready):
    _nonce(nonce)
    if type(ready) is not bool:
        raise IdentityError("invalid_readiness", "Readiness must be an explicit boolean")
    body = {"schema_version": 1, **_fields(identity), "nonce": nonce, "ready": ready}
    message = b"mptask/identity-proof/v1\0" + canonical_json(body).encode("utf-8")
    return {**body, "mac": hmac.new(_secret(secret), message, hashlib.sha256).hexdigest()}


def verify_proof(identity, secret, nonce, response):
    _nonce(nonce)
    if (type(response) is not dict or set(response) != _PROOF_FIELDS
            or type(response["schema_version"]) is not int or response["schema_version"] != 1
            or type(response["ready"]) is not bool or type(response["mac"]) is not str
            or _HEX_256.fullmatch(response["mac"]) is None):
        raise IdentityError("invalid_identity_response", "Malformed identity proof")
    if (response["nonce"] != nonce
            or any(response[key] != value for key, value in _fields(identity).items())):
        raise IdentityError("identity_mismatch", "Identity proof does not match the expected instance")
    expected = make_proof(identity, secret, nonce, ready=response["ready"])
    if not hmac.compare_digest(response["mac"], expected["mac"]):
        raise IdentityError("identity_mismatch", "Identity proof authentication failed")
    return response["ready"]


def instance_bearer(identity, secret):
    message = b"mptask/instance-bearer/v1\0" + canonical_json(_fields(identity)).encode("utf-8")
    digest = hmac.new(_secret(secret), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
