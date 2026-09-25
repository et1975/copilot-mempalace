"""Explicit, cooperative background ownership; no service manager or task store.

Integration contract
--------------------
``start`` executes exactly::

    sys.executable -m mempalace_tasks --config ABSOLUTE_PATH serve --startup-ticket-stdin

The child reads at most 2048 bytes from its private stdin pipe, then EOF. The
parent writes one UTF-8 JSON line and closes the pipe. The exact schema is
``{schema_version:1, authority_id, binding, nonce}``: the canonical authority
UUID, the discovery binding fingerprint, and 64 lowercase hexadecimal nonce
digits. There are no credentials. This ticket is local coordination, NOT an
authorization capability or a substitute for the child's OWN authority.lock.

Both foreground and launched children enter ``startup_election`` before
authority acquisition, recovery, listener binding and registry publication.
Only a validated stdin ticket permits bypass of the parent's currently held
start.lock. In journal mode, InstanceIdentity.instance_id is the authority's
accepted epoch_id, known only after activation against the configured MemPalace
hub. Neither this ticket nor the launcher chooses or authorizes that epoch.
Release the election after publication, not the authority lock. The parent
authenticates the published identity while retaining its election and child
handle; no registry PID is used to infer ownership or child identity.
No alternative hub, authority, configuration, port or recovery path is supplied.

POST /control/stop uses the instance bearer and exact body
``{schema_version:1, instance_id:<expected>, drain:true, nonce:<fresh hex>}``.
The server checks the instance, stops admission and makes readiness false, then
completes the drain of accepted work BEFORE returning its exact 202 JSON:
``{schema_version:1, authority_id, instance_id, accepted:true, drained:true, proof}``.
Here proof is ``make_proof(identity, root_token, request_nonce, ready=False)``;
the fresh control challenge prevents trusting a stale port's forged response.
It closes its listener and ONLY THEN releases authority ownership. Acceptance
alone is not success: ``stop`` requires the drain-complete acknowledgment, holds
start.lock, and observes the original listener become unreachable AND
authority.lock become available. Lost/invalid responses and unconfirmed release
are explicit unknown outcomes, never reasons to kill a registry PID or
automatically retry the POST. A mere ``draining:true`` acknowledgment followed
by process death is insufficient evidence of a completed drain.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from . import platform_support as platform
from .codec import canonical_json
from .config import ServiceConfig, load_config
from .discovery import (
    ConnectionInfo, DiscoveryError, _Deadline, _decode, _probe, _read_registry,
    _request, binding_fingerprint,
)
from .server_identity import IdentityError, InstanceIdentity, verify_proof


_TICKET_LIMIT = 2048
_POLL_LIMIT = 6001
_children: set[subprocess.Popen] = set()
_children_guard = threading.Lock()


@dataclass(frozen=True)
class StartupTicket:
    authority_id: str
    binding: str
    nonce: str

    def encode(self) -> bytes:
        return (canonical_json({"schema_version": 1, **self.__dict__}) + "\n").encode("utf-8")


def parse_startup_ticket(config: ServiceConfig, data: bytes) -> StartupTicket:
    """Validate already-bounded stdin bytes; no reads, locks or durable writes."""
    if (type(data) is not bytes or not 1 <= len(data) <= _TICKET_LIMIT
            or not data.endswith(b"\n") or data.count(b"\n") != 1
            or config.lifecycle != "launcher" or config.recovery_mode != "journal"):
        raise DiscoveryError("invalid_ticket", "Invalid launcher startup ticket")
    value = _decode(data, "invalid_ticket")
    if (set(value) != {"schema_version", "authority_id", "binding", "nonce"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["authority_id"] != config.authority_id
            or value["binding"] != binding_fingerprint(config)
            or type(value["nonce"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["nonce"]) is None):
        raise DiscoveryError("invalid_ticket", "Startup ticket differs from configuration")
    return StartupTicket(config.authority_id, value["binding"], value["nonce"])


def _try_lock(path: Path) -> platform.LifetimeLock | None:
    try:
        return platform.LifetimeLock(path).acquire()
    except platform.PlatformError as error:
        if error.code != "lock_busy":
            raise
        return None


def _owner_busy(config: ServiceConfig) -> bool:
    lock = _try_lock(config.runtime_dir / "authority.lock")
    if lock is None:
        return True
    lock.release()
    return False


@contextmanager
def _election(config: ServiceConfig, deadline: _Deadline):
    for _ in range(_POLL_LIMIT):
        try:
            deadline.remaining()
        except DiscoveryError:
            break
        lock = _try_lock(config.runtime_dir / "start.lock")
        if lock is not None:
            try:
                yield
            finally:
                lock.release()
            return
        try:
            deadline.pause()
        except DiscoveryError:
            break
    raise DiscoveryError("election_timeout", "Another lifecycle operation holds the startup election")


@contextmanager
def startup_election(config: ServiceConfig, *, ticket: bytes | None = None,
                     timeout: float = 5.0):
    """Server integration hook; yield validated ticket or None for foreground.

    The child must read the ticket only with the internal CLI flag, from its
    inherited stdin pipe, before entering here. Never accept tickets from HTTP,
    environment variables, config files, argv values, or registry contents.
    """
    deadline = _Deadline(timeout)
    parsed = parse_startup_ticket(config, ticket) if ticket is not None else None
    platform.ensure_private_directory(config.runtime_dir)
    if parsed is not None:
        probe = _try_lock(config.runtime_dir / "start.lock")
        if probe is not None:
            probe.release()
            raise DiscoveryError("expired_ticket", "Startup ticket has no active parent election")
        yield parsed
    else:
        with _election(config, deadline):
            yield None


def _optional_registry(config: ServiceConfig) -> InstanceIdentity | None:
    try:
        return _read_registry(config)
    except DiscoveryError as error:
        if error.code != "registry_missing":
            raise
        return None


def _poll_deadline(deadline: _Deadline) -> _Deadline:
    return _Deadline(min(0.25, deadline.remaining()))


def _wait_owner(config: ServiceConfig, deadline: _Deadline) -> ConnectionInfo:
    for _ in range(_POLL_LIMIT):
        try:
            deadline.remaining()
            if not _owner_busy(config):
                raise DiscoveryError("owner_unready", "Existing owner exited before becoming ready")
            identity = _optional_registry(config)
            if identity is not None:
                info = _probe(config, identity, _poll_deadline(deadline))
                if not _owner_busy(config):
                    raise DiscoveryError("owner_unready", "Existing owner released its lock")
                return info
        except DiscoveryError as error:
            if error.code not in {"not_ready", "endpoint_unavailable", "timeout"}:
                raise
        try:
            deadline.pause()
        except DiscoveryError:
            break
    raise DiscoveryError("owner_unready", "Occupied authority did not become ready; no child was started")


def _windows_in_job() -> bool:
    try:
        import pywintypes
        import win32api
        import win32job
    except ImportError:
        raise DiscoveryError("unsupported_launcher", "Native Windows Job inspection is required") from None
    try:
        return bool(win32job.IsProcessInJob(win32api.GetCurrentProcess(), None))
    except (OSError, pywintypes.error):
        raise DiscoveryError("unsupported_launcher", "Cannot inspect Windows Job restrictions") from None


def _launch_options() -> dict:
    if os.name == "posix" and sys.platform in {"linux", "darwin"}:
        return {"start_new_session": True}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if _windows_in_job():
            # Do not silently remain in a kill-on-parent-exit Job. A Job that
            # refuses breakaway makes CreateProcess fail; no attached fallback.
            flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
        return {"creationflags": flags}
    raise DiscoveryError("unsupported_launcher", "Background launch is unsupported on this platform")


def _spawn(config_path: Path, config: ServiceConfig) -> subprocess.Popen:
    argv = [sys.executable, "-m", "mempalace_tasks", "--config", str(config_path),
            "serve", "--startup-ticket-stdin"]
    options = _launch_options()
    try:
        # Exclusive native temporary-file creation; Windows inherits only the
        # already-validated private runtime DACL. This log is disposable too.
        with tempfile.NamedTemporaryFile(mode="ab", prefix="launch-", suffix=".log",
                                         dir=config.runtime_dir, delete=False) as log:
            return subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                shell=False, close_fds=True, bufsize=0, **options)
    except OSError:
        raise DiscoveryError(
            "spawn_failed", "Cannot launch child; inspect local permissions or Windows Job restrictions",
        ) from None


def _stop_child(child: subprocess.Popen) -> None:
    """Only an actual Popen handle owned by this invocation may be terminated."""
    until = time.monotonic() + 2
    try:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=max(0.001, until - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        _retain_child(child)
        raise DiscoveryError("startup_unknown", "Cannot confirm owned child cleanup",
                             ambiguous=True) from None


def _retain_child(child: subprocess.Popen) -> None:
    # A successful detached child still needs a retained native handle and reaper.
    # No polling/idle shutdown loop and no PID-file authority are involved.
    with _children_guard:
        _children.add(child)

    def reap():
        try:
            child.wait()
        finally:
            with _children_guard:
                _children.discard(child)

    threading.Thread(target=reap, name="mptask-child-reaper", daemon=True).start()


def _wait_child(config: ServiceConfig, previous: InstanceIdentity | None,
                child: subprocess.Popen, deadline: _Deadline) -> ConnectionInfo:
    for _ in range(_POLL_LIMIT):
        try:
            deadline.remaining()
            if child.poll() is not None:
                raise DiscoveryError("child_exited", "Owned child exited before confirmed readiness")
            identity = _optional_registry(config)
            if identity is not None and identity != previous:
                info = _probe(config, identity, _poll_deadline(deadline))
                if child.poll() is not None or not _owner_busy(config):
                    raise DiscoveryError("startup_unknown", "Child readiness lost before confirmation",
                                         ambiguous=True)
                return info
        except DiscoveryError as error:
            if error.code not in {"not_ready", "endpoint_unavailable", "timeout"}:
                raise
        try:
            deadline.pause()
        except DiscoveryError:
            break
    raise DiscoveryError("startup_timeout", "Child did not reach confirmed readiness before the deadline")


def start(config_path: str | os.PathLike[str], *, timeout: float = 10.0) -> ConnectionInfo:
    """Explicit opt-in start/reuse; timeout plus at most 2s of failed-child cleanup.

    Configuration/credentials stay local. No installation, hub fallback, service
    registration, alternate task storage, or implicit authority initialization.
    """
    deadline = _Deadline(timeout)
    path = platform.canonical_path(config_path)
    config = load_config(path)
    if config.lifecycle != "launcher" or config.recovery_mode != "journal":
        raise DiscoveryError("external_lifecycle", "Configuration does not authorize launcher lifecycle")
    platform.ensure_private_directory(config.runtime_dir)
    with _election(config, deadline):
        previous = _optional_registry(config)
        if _owner_busy(config):
            return _wait_owner(config, deadline)
        if previous is not None:
            try:
                _probe(config, previous, _poll_deadline(deadline), require_ready=False)
            except DiscoveryError as error:
                if error.code != "endpoint_unavailable":
                    raise
            else:
                raise DiscoveryError("owner_unlocked", "Authenticated listener has no authority ownership")
        deadline.remaining()
        ticket = StartupTicket(config.authority_id, binding_fingerprint(config),
                               secrets.token_hex(32))
        child = _spawn(path, config)
        try:
            try:
                # Under PIPE_BUF even on its smallest supported value; a new,
                # empty private pipe cannot fill before this one ticket is sent.
                data = ticket.encode()
                if child.stdin.write(data) != len(data):
                    raise OSError("incomplete startup ticket")
                child.stdin.close()
            except OSError:
                raise DiscoveryError("startup_channel_failed", "Could not deliver private startup ticket") from None
            info = _wait_child(config, previous, child, deadline)
            _retain_child(child)
            return info
        except BaseException:
            _stop_child(child)
            raise


def stop(config: ServiceConfig, *, expected_instance_id: str, timeout: float = 10.0) -> dict:
    """Drain exactly the verified instance and observe release; never kill a PID."""
    deadline = _Deadline(timeout)
    identity = _read_registry(config)
    if identity.instance_id != expected_instance_id:
        raise DiscoveryError("instance_changed", "Discovered instance differs from stop expectation")
    with _election(config, deadline):
        if _read_registry(config) != identity:
            raise DiscoveryError("instance_changed", "Instance changed before stop could be requested")
        info = _probe(config, identity, deadline, require_ready=False)
        if not _owner_busy(config):
            raise DiscoveryError("owner_unlocked", "Cannot stop a listener without authority ownership")
        try:
            nonce = secrets.token_hex(32)
            reply = _request(
                identity, deadline, method="POST", path="/control/stop", token=info.token,
                body={"schema_version": 1, "instance_id": identity.instance_id,
                      "drain": True, "nonce": nonce},
                status=202)
            expected = {"schema_version": 1, "authority_id": identity.authority_id,
                        "instance_id": identity.instance_id, "accepted": True, "drained": True}
            if (set(reply) != set(expected) | {"proof"}
                    or any(reply[key] != value for key, value in expected.items())
                    or type(reply.get("schema_version")) is not int
                    or reply.get("accepted") is not True or reply.get("drained") is not True):
                raise DiscoveryError("invalid_response", "Invalid drain acknowledgment")
            try:
                if verify_proof(identity, config.service_token, nonce, reply["proof"]):
                    raise DiscoveryError("invalid_response", "Drain proof must show an unready owner")
            except IdentityError as error:
                raise DiscoveryError(error.code, error.message) from None
            for _ in range(_POLL_LIMIT):
                deadline.remaining()
                current = _optional_registry(config)
                if current is not None and current != identity:
                    raise DiscoveryError("instance_changed", "Instance changed during drain")
                try:
                    observed = _probe(config, identity, _poll_deadline(deadline), require_ready=False)
                except DiscoveryError as error:
                    if error.code not in {"endpoint_unavailable", "transport_error", "timeout"}:
                        raise
                    if error.code == "endpoint_unavailable" and not _owner_busy(config):
                        return {"stopped": True, "authority_id": identity.authority_id,
                                "instance_id": identity.instance_id}
                else:
                    if observed.ready:
                        raise DiscoveryError("invalid_response", "Owner did not disable readiness for drain")
                deadline.pause()
        except (DiscoveryError, platform.PlatformError) as error:
            raise DiscoveryError(
                "stop_unknown",
                f"Stop requested but same-instance drain and release are unconfirmed ({error.code})",
                ambiguous=True,
            ) from None
    raise DiscoveryError("stop_unknown", "Stop observation limit exceeded", ambiguous=True)
