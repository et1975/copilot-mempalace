"""Bounded, owned HTTP fixtures; no task authority, palace, or live configuration."""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from mempalace_tasks import platform_support as platform
from mempalace_tasks.config import load_config
from mempalace_tasks.server_identity import InstanceIdentity, instance_bearer, make_proof


ROOT_TOKEN = "owned-portable-root-token-NOT-FOR-THE-WIRE"
AUTHORITY = "11111111-1111-1111-1111-111111111111"


class ConfigurationFixture:
    def __init__(self, *, lifecycle="launcher"):
        self.directory = tempfile.TemporaryDirectory(
            prefix="discovery-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.root = platform.ensure_private_directory(
            Path(self.directory.name).resolve() / "private")
        self.path = self.root / "config.json"
        self.document = {
            "schema_version": 2, "authority_id": AUTHORITY,
            "hub_url": "http://127.0.0.1:1/mcp",
            "service_token_file": str(self.root / "service.token"),
            "runtime_dir": str(self.root / "runtime"),
            "genesis": {"actor": "operator",
                        "actors": {"operator": "operator", "system": "system"},
                        "execution_profiles": {}, "supervisors": {}},
            "maintenance_actor": "system", "recovery_actor": "operator",
            "host": "127.0.0.1", "port": 0, "lifecycle": lifecycle,
        }
        platform.create_private(self.root / "service.token", (ROOT_TOKEN + "\n").encode())
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        self.config = load_config(self.path)

    def close(self):
        self.directory.cleanup()


class IdentityServer:
    def __init__(self, config, *, mode="normal", ready=True, own=True, instance_id=None):
        self.config, self.mode, self.ready = config, mode, ready
        self.requests = []
        self.owner = None
        self.owner_released = threading.Event()
        self.closed = threading.Event()
        self.close_guard = threading.Lock()
        self.drain_delay = 0.05
        self.release_delay = 0
        self.previous_proof = None
        self.redirect_url = "http://127.0.0.1:1/steal"
        self.after = time.monotonic()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, body, *, status=200, headers=None):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                fixture.requests.append(("GET", self.path, dict(self.headers), None))
                nonce = parse_qs(urlsplit(self.path).query).get("nonce", [""])[0]
                if fixture.mode == "stop-reset" and not fixture.ready:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if fixture.mode == "redirect":
                    self.reply(b"", status=307, headers={"Location": fixture.redirect_url})
                    return
                if fixture.mode == "unrelated":
                    self.reply({"hello": "not the sidecar"})
                    return
                if fixture.mode == "malformed":
                    self.reply(b'{"ready":true,"ready":false}')
                    return
                if fixture.mode == "oversized":
                    self.reply(b" " * 16385)
                    return
                if fixture.mode == "nonfinite":
                    self.reply(b'{"ready":NaN}')
                    return
                if fixture.mode == "slow":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "200")
                    self.end_headers()
                    for _ in range(30):
                        try:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        time.sleep(0.03)
                    return
                if fixture.mode == "delayed":
                    time.sleep(0.2)
                identity = fixture.identity
                if fixture.mode == "wrong-authority":
                    identity = replace(identity, authority_id=str(uuid4()))
                if fixture.mode == "wrong-instance":
                    identity = replace(identity, instance_id=str(uuid4()))
                if fixture.mode == "wrong-endpoint":
                    identity = replace(identity, endpoint="http://127.0.0.1:1/mcp")
                proof = make_proof(
                    identity, ROOT_TOKEN, nonce,
                    ready=fixture.ready and time.monotonic() >= fixture.after)
                if fixture.mode == "replay" and fixture.previous_proof:
                    proof = fixture.previous_proof
                fixture.previous_proof = proof
                if fixture.mode == "utf16":
                    self.reply(json.dumps(proof).encode("utf-16"))
                elif fixture.mode == "chunked-oversized":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for _ in range(5):
                            self.wfile.write(b"1000\r\n" + b"x" * 4096 + b"\r\n")
                        self.wfile.write(b"0\r\n\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    self.reply(proof)

            def do_POST(self):
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                data = json.loads(self.rfile.read(length))
                fixture.requests.append(("POST", self.path, dict(self.headers), data))
                expected = "Bearer " + instance_bearer(fixture.identity, ROOT_TOKEN)
                if (self.path != "/control/stop" or self.headers.get("Authorization") != expected
                        or data != {"schema_version": 1,
                                    "instance_id": fixture.identity.instance_id, "drain": True,
                                    "nonce": data.get("nonce")}):
                    self.reply({}, status=403)
                    return
                if fixture.mode == "stop-unimplemented":
                    self.reply({}, status=404)
                    return
                fixture.ready = False
                completed = fixture.mode not in {"stop-accepted-only", "stop-never"}
                if completed:
                    time.sleep(fixture.drain_delay)
                reply = {"schema_version": 1, "authority_id": config.authority_id,
                         "instance_id": fixture.identity.instance_id, "accepted": True,
                         ("drained" if completed else "draining"): True}
                if fixture.mode != "stop-unproven":
                    proof = make_proof(fixture.identity, ROOT_TOKEN, data["nonce"], ready=False)
                    reply["proof"] = (fixture.previous_proof
                                      if fixture.mode == "stop-replayed-proof" else proof)
                self.reply(reply, status=202)
                if fixture.mode == "stop-listener-held":
                    fixture.owner.release()
                    fixture.owner = None
                    fixture.owner_released.set()
                elif fixture.mode != "stop-never":
                    def finish():
                        if fixture.mode == "stop-reset":
                            time.sleep(0.15)
                        fixture.close()
                    threading.Thread(target=finish, daemon=True).start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        if own:
            platform.ensure_private_directory(config.runtime_dir)
            self.owner = platform.LifetimeLock(config.runtime_dir / "authority.lock").acquire()
        self.identity = InstanceIdentity(
            config.authority_id, instance_id or str(uuid4()),
            f"http://127.0.0.1:{self.server.server_port}/mcp")
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()

    def publish(self):
        from mempalace_tasks.discovery import publish_registry
        publish_registry(self.config, self.identity)

    def close(self):
        with self.close_guard:
            if self.closed.is_set():
                return
            self.closed.set()
            self.server.shutdown()
            self.thread.join(2)
            self.server.server_close()
            if self.owner is not None:
                time.sleep(self.release_delay)
                self.owner.release()
                self.owner = None
            self.owner_released.set()


def main():
    """Controlled launcher child for identity/transport faults, not task recovery."""
    import argparse
    import sys
    from mempalace_tasks.launcher import startup_election

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("command", choices=["serve"])
    parser.add_argument("--startup-ticket-stdin", action="store_true", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    ticket_bytes = sys.stdin.buffer.read(2049)
    sys.stdin.close()
    mode = os.environ.get("MPTASK_FIXTURE_MODE", "normal")
    if mode == "exit":
        return 7
    time.sleep(float(os.environ.get("MPTASK_FIXTURE_DELAY", "0")))
    with startup_election(config, ticket=ticket_bytes):
        # Controlled stand-in for the epoch accepted by journal activation, not
        # an epoch allocated by the launcher or a claim of testing the journal.
        fixture = IdentityServer(
            config, ready=mode != "never-ready",
            instance_id=os.environ.get("MPTASK_FIXTURE_ACCEPTED_EPOCH"))
        fixture.publish()
        diagnostic_pid = os.environ.get("MPTASK_FIXTURE_DIAGNOSTIC_PID")
        if diagnostic_pid is not None:
            path = config.runtime_dir / "serverinfo.json"
            document = json.loads(platform.read_regular(path, private=True))
            platform.atomic_write_cache(
                path, json.dumps({**document, "pid": int(diagnostic_pid)}).encode())
    try:
        print(json.dumps({"argv": sys.argv[1:], "ticket": json.loads(ticket_bytes),
                          "pid": os.getpid(), "accepted_epoch": fixture.identity.instance_id}),
              flush=True)
        if not fixture.owner_released.wait(8):
            fixture.close()
    finally:
        fixture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
