#!/usr/bin/env python3
"""Optional procedural advice; never permission decisions or durable publication.

POSIX only. Native payload schemas follow GitHub's hooks reference, not a claim
of authenticated host validation. See the opt-in examples; no config is off.
Only guidance/draft subprocesses run here. Packet pointers are bookkeeping, not
evidence that the agent read or benefited from a rule.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import traceback
from typing import Callable, Iterator
import unicodedata
import uuid


SEARCH_TOOLS = frozenset({"mempalace-mempalace_search", "mempalace_search"})
DISPATCH_TOOLS = frozenset({
    "task", "Task", "Agent", "functions.task", "run_factory", "functions.run_factory",
    "write_agent", "functions.write_agent",
})
EVENT_NAMES = {
    "prompt": "UserPromptSubmit", "before-tool": "PreToolUse",
    "after-tool": "PostToolUse", "start": "SessionStart",
    "compact": "PreCompact", "stop": "Stop",
}
MAX_INPUT = 1024 * 1024
MAX_STATE = 256 * 1024
MAX_PACKET = 8192
CHILD_TIMEOUT = 25.0
LOCK_TIMEOUT = 0.3


class Unavailable(ValueError):
    """Expected abstention; optional advice must not deny a host tool."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Unavailable(message)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def natural(value: object) -> bool:
    return type(value) is int and value >= 0


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def task_digest(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def parse_json(raw: bytes | str) -> dict:
    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise Unavailable("non-finite JSON number")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    require(isinstance(value, dict), "expected a JSON object")
    return value


def canonical_path(value: object) -> Path:
    require(text(value), "missing absolute path")
    require("\0" not in value, "path contains a null byte")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, "path must be absolute without traversal")
    require(str(path) == str(value), "path must be canonical")
    return path


def trusted_path(value: object, *, directory: bool = False, private: bool = False) -> Path:
    path = canonical_path(value)
    # Every writable ancestor would let another user swap the checked object.
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        require(not stat.S_ISLNK(info.st_mode), "symlink path rejected")
        require(info.st_uid in (0, os.getuid()) and not info.st_mode & 0o022,
                "untrusted path ownership or permissions")
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "expected directory" if directory else "expected regular file")
    if private:
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "path must be private")
    if not directory:
        require(info.st_nlink == 1, "hard-linked file rejected")
    return path


def private_fd(fd: int) -> None:
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and info.st_nlink == 1,
            "state file must be private, regular and singly linked")


def read_private(path: Path, limit: int) -> bytes:
    trusted_path(str(path), private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        private_fd(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        require(len(value) <= limit, "private file exceeds budget")
        return value
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Scope:
    root: Path
    repository: str
    palace: Path
    wing: str
    session_store: Path | None


@dataclass(frozen=True)
class Config:
    python: Path
    procedure: Path
    state_root: Path
    scope: Scope


def repository_key(value: object) -> str:
    # Mirror the small core contract without importing backend startup code.
    require(text(value), "repository must be nonblank owner/repository")
    key = unicodedata.normalize("NFC", value.strip()).lower()
    require(re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", key) is not None,
            "repository must be owner/repository")
    return key


def load_config(path: str | None, cwd: object) -> Config | None:
    if path is None:
        return None
    try:
        raw = read_private(Path(path), 65536)
    except FileNotFoundError:
        # A configured-but-missing file is distinct from an omitted opt-in.
        raise Unavailable("configured file is missing") from None
    data = parse_json(raw)
    require(data.get("schema_version") == 1 and type(data.get("schema_version")) is int,
            "config schema_version must be 1")
    require(data.get("mode") in ("off", "guidance_drafts"), "invalid config mode")
    if data["mode"] == "off":
        return None
    require(set(data) == {"schema_version", "mode", "python", "procedure_script",
                          "state_root", "repositories"}, "unknown or missing config field")
    executable = data["python"]
    require(text(executable) and Path(executable).is_absolute() and ".." not in Path(executable).parts,
            "python must be an absolute executable")
    # Virtualenv interpreters are normally symlinks; state/config/scripts never are.
    python = trusted_path(str(Path(executable).resolve(strict=True)))
    require(os.access(python, os.X_OK), "python must be executable")
    procedure = trusted_path(data["procedure_script"])
    state_root = trusted_path(data["state_root"], directory=True, private=True)
    entries = data["repositories"]
    require(isinstance(entries, list) and bool(entries), "repositories must be a nonempty list")
    scopes = {}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) in (
            {"root", "repository", "palace", "wing"},
            {"root", "repository", "palace", "wing", "session_store"}),
            "invalid repository configuration")
        root = trusted_path(entry["root"], directory=True)
        require((root / ".git").exists() and not (root / ".git").is_symlink(),
                "scope root is not an explicit worktree root")
        repository = repository_key(entry["repository"])
        wing = entry["wing"]
        require(isinstance(wing, str) and re.fullmatch(r"[A-Za-z0-9_-]+", wing) is not None,
                "an explicit canonical project wing is required")
        palace = trusted_path(entry["palace"], directory=True)
        store = canonical_path(entry["session_store"]) if "session_store" in entry else None
        require(str(root) not in scopes, "duplicate repository root")
        scopes[str(root)] = Scope(root, repository, palace, wing, store)
    require(isinstance(cwd, str) and cwd in scopes, "cwd outside exact configured scope")
    return Config(Path(executable), procedure, state_root, scopes[cwd])


@dataclass(frozen=True)
class Event:
    mode: str
    session_id: str
    snake: bool
    tool: str
    args: dict
    payload: dict
    shared: bool

    @property
    def key(self) -> str:
        return digest([self.tool, self.args])


def normalize(mode: str, payload: dict) -> Event:
    snake = "session_id" in payload
    require(not ("session_id" in payload and "sessionId" in payload), "mixed host identity dialect")
    session = payload.get("session_id" if snake else "sessionId")
    require(text(session) and len(session.encode("utf-8")) <= 512, "host session identity unavailable")
    if "hook_event_name" in payload:
        accepted = {EVENT_NAMES[mode]}
        if mode == "after-tool":
            accepted.add("PostToolUseFailure")
        require(payload["hook_event_name"] in accepted, "host event mismatch")
    other = ("toolName", "toolArgs", "toolResult") if snake else ("tool_name", "tool_input", "tool_result")
    require(not any(key in payload for key in other), "mixed host tool dialect")
    tool = payload.get("tool_name" if snake else "toolName", "")
    args = payload.get("tool_input" if snake else "toolArgs", {})
    if isinstance(args, str):
        args = parse_json(args)
    require(isinstance(tool, str) and isinstance(args, dict), "invalid host tool fields")
    shared = any(key in payload for key in (
        "agent_id", "agentId", "agent_type", "agentType", "agent_name", "agentName",
        "parent_session_id", "parentSessionId",
    ))
    return Event(mode, session, snake, tool, args, payload, shared)


def dispatch(tool: str, args: dict) -> bool:
    if tool in DISPATCH_TOOLS:
        return True
    if tool == "multi_tool_use.parallel":
        # We cannot bind starts inside an aggregate result, so search children
        # are never promoted. Recognize dispatch children conservatively.
        uses = args.get("tool_uses")
        return not isinstance(uses, list) or any(
            not isinstance(item, dict)
            or item.get("recipient_name") in DISPATCH_TOOLS
            or item.get("recipient_name") == "multi_tool_use.parallel" for item in uses)
    return False


class State:
    def __init__(self, config: Config, event: Event):
        scope = config.scope
        self.identity = {
            "root": str(scope.root), "repository": scope.repository,
            "palace": str(scope.palace), "wing": scope.wing,
            "session_store": str(scope.session_store) if scope.session_store else None,
            "session_id": event.session_id,
        }
        self.path = config.state_root / digest(self.identity)
        self.path.mkdir(mode=0o700, exist_ok=True)
        trusted_path(str(self.path), directory=True, private=True)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.publication: bytes | None = None

    def close(self) -> None:
        os.close(self.fd)

    def mark_uncertain(self) -> None:
        # A missed pre-hook cannot wait for the ledger lock: another completion
        # could otherwise be mispaired with an older, identical outstanding call.
        try:
            fd = os.open("uncertain", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
        except FileExistsError:
            self.read("uncertain")
        else:
            try:
                os.fsync(fd)
                os.fsync(self.fd)
            finally:
                os.close(fd)

    def uncertain(self) -> bool:
        try:
            self.read("uncertain")
            return True
        except FileNotFoundError:
            return False

    def read(self, name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            private_fd(fd)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                value = stream.read(MAX_STATE + 1)
            require(len(value) <= MAX_STATE, "state exceeds budget")
            return value
        finally:
            os.close(fd)

    def write(self, name: str, raw: bytes) -> None:
        require("/" not in name and name not in (".", ".."), "invalid private artifact name")
        try:
            self.read(name)  # Reject a hostile destination rather than replacing it.
        except FileNotFoundError:
            pass
        temporary = "." + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    @contextmanager
    def locked(self) -> Iterator[dict]:
        fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=self.fd)
        try:
            private_fd(fd)
            until = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    require(time.monotonic() < until, "state lock busy")
                    time.sleep(0.01)
            try:
                value = parse_json(self.read("state.json"))
            except FileNotFoundError:
                value = {
                    "schema_version": 1, "identity": self.identity, "generation": 0,
                    "task": None, "task_digest": None, "starts": {}, "disabled": False,
                    "attempt": None, "receipt": None, "drafted": False,
                }
            require(value.get("schema_version") == 1 and value.get("identity") == self.identity
                    and natural(value.get("generation")) and isinstance(value.get("starts"), dict)
                    and type(value.get("disabled")) is bool
                    and type(value.get("drafted")) is bool,
                    "invalid or mismatched pairing state")
            if self.uncertain():
                value["disabled"] = True
            yield value
            raw = json_bytes(value)
            require(len(raw) <= MAX_STATE, "state exceeds budget")
            self.write("state.json", raw)
            if self.publication is not None:
                require(not self.uncertain(), "pairing became uncertain during guidance")
                sys.stdout.buffer.write(self.publication)
                sys.stdout.buffer.flush()
                self.publication = None
        finally:
            os.close(fd)


def run_child(argv: list[str], cwd: Path, limit: int, timeout: float = CHILD_TIMEOUT) -> bytes:
    """Bound pipes and lifetime; kill only the invocation's newly created group."""
    environment = os.environ.copy()
    environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       HF_HUB_DISABLE_TELEMETRY="1")
    proc = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, umask=0o077)
    output = bytearray()
    stderr_size = 0
    until = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, True)
            selector.register(proc.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = until - time.monotonic()
                require(remaining > 0, "backend timeout")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data:
                        output.extend(chunk)
                        require(len(output) <= limit, "backend output exceeds byte budget")
                    else:
                        stderr_size += len(chunk)
                        require(stderr_size <= 16384, "backend stderr exceeds budget")
            remaining = until - time.monotonic()
            require(remaining > 0, "backend timeout")
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise Unavailable("backend timeout") from None
        require(code == 0, f"backend exited {code}; response withheld")
        return bytes(output)
    finally:
        # A descendant may have closed its pipes while continuing to run.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


Runner = Callable[[list[str], Path, int], bytes]


def utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    except ValueError:
        return False


def validate_guidance(raw: bytes, repository: str) -> dict:
    require(len(raw) <= MAX_PACKET, "guidance exceeds byte budget")
    serialized = raw.decode("utf-8")
    require(serialized.endswith("\n") and len(serialized) <= 6000,
            "guidance exceeds character budget or lacks serialized newline")
    data = parse_json(serialized)
    require(not any(key in data for key in ("error", "errors"))
            and data.get("isError") is not True and data.get("success") is not False,
            "guidance evidence unavailable")
    require(data.get("repository") == repository, "guidance repository scope mismatch")
    require(text(data.get("policy_version")) and utc(data.get("as_of")), "invalid guidance policy or time")
    require(data.get("status") in ("ok", "no_rules", "no_eligible_rules"),
            "guidance status unavailable")
    require(data.get("trials") == [], "automatic candidate trials forbidden")
    require(isinstance(data.get("rules"), list) and isinstance(data.get("anti_patterns"), list),
            "invalid guidance sections")
    items = data["rules"] + data["anti_patterns"]
    require(natural(data.get("item_count")) and data["item_count"] == len(items) <= 5
            and natural(data.get("omitted_count")), "invalid combined guidance count")
    require((data["status"] == "ok") == bool(items), "inconsistent guidance status")
    ids = set()
    for item in items:
        require(isinstance(item, dict), "invalid guidance item")
        require(all(text(item.get(k)) for k in ("rule_id", "statement", "applies_when")),
                "incomplete guidance rule")
        require(item["rule_id"] not in ids, "duplicate guidance rule")
        ids.add(item["rule_id"])
        require(item.get("maturity") in ("established", "proven")
                and item.get("delivery") == "guidance", "ineligible automatic guidance")
        require(isinstance(item.get("exceptions"), list)
                and all(isinstance(v, str) for v in item["exceptions"]), "invalid rule exceptions")
        require(all(type(item.get(k)) in (int, float) and math.isfinite(item[k])
                    for k in ("effective_score", "relevance")), "invalid guidance scores")
        require(utc(item.get("latest_validation")), "guidance validation unavailable")
        require(isinstance(item.get("evidence"), list) and bool(item["evidence"])
                and all(isinstance(ref, dict) and text(ref.get("source_kind"))
                        and text(ref.get("source_id")) for ref in item["evidence"]),
                "guidance evidence unavailable")
    return data


def search_succeeded(event: Event) -> bool:
    if "error" in event.payload:
        return False
    result = event.payload.get("tool_result" if event.snake else "toolResult")
    if not isinstance(result, dict):
        return False
    if result.get("result_type" if event.snake else "resultType") != "success":
        return False
    raw = result.get("text_result_for_llm" if event.snake else "textResultForLlm")
    if not isinstance(raw, str):
        return False
    try:
        data = parse_json(raw)
    except (ValueError, UnicodeError):
        return False
    return (not any(k in data for k in ("error", "errors"))
            and data.get("isError") is not True and data.get("success") is not False
            and data.get("status", "ok") == "ok"
            and data.get("query") == event.args.get("query")
            and text(data.get("query"))
            and isinstance(data.get("results"), list)
            and all(isinstance(hit, dict) for hit in data["results"]))


def invalidate(value: dict, task: str | None = None) -> None:
    value["generation"] += 1
    value.update(task=task, task_digest=task_digest(task) if task else None,
                 attempt=None, receipt=None, drafted=False)
    # Starts and the shared-session latch survive prompts, resume and compact.


def base_command(config: Config, command: str) -> list[str]:
    scope = config.scope
    return [str(config.python), str(config.procedure), command,
            "--palace", str(scope.palace), "--wing", scope.wing,
            "--repository", scope.repository]


def context_output(event: Event, message: str) -> bytes:
    body = ({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}
            if event.snake else {"additionalContext": message})
    raw = json_bytes(body)
    require(len(raw) <= 1024, "packet pointer exceeds host output budget")
    return raw


def draft(config: Config, store: State, receipt: dict, runner: Runner) -> None:
    if config.scope.session_store is not None:
        try:
            trusted_path(str(config.scope.session_store), private=True)
        except FileNotFoundError:
            # Existing prefixes were checked; absence belongs to core's
            # pending/captured fallback, without changing correlation identity.
            pass
    nonce = uuid.uuid4().hex
    input_name = f"receipt-{nonce}.json"
    output_name = f"draft-{nonce}.json"
    stage_name = f".draft-stage-{nonce}"
    store.write(input_name, json_bytes(receipt))
    argv = base_command(config, "draft")
    if config.scope.session_store is not None:
        argv += ["--session-store", str(config.scope.session_store)]
    argv += ["--input", str(store.path / input_name), "--out", str(store.path / stage_name)]
    try:
        runner(argv, config.scope.root, 65536)
        raw = store.read(stage_name)
        require(len(raw) <= 65536, "draft exceeds byte budget")
        result = parse_json(raw)
        require(type(result.get("schema_version")) is int and result["schema_version"] == 1
                and result.get("kind") == "procedural_draft"
                and result.get("status") in ("requires_review", "pending_original_evidence"),
                "invalid draft output")
        require(all(result.get(key) == receipt[key] for key in (
            "repository", "session_id", "task_digest", "delivered_rule_ids")), "draft scope mismatch")
        require(all(isinstance(result.get(key), list) for key in (
            "original_references", "lineage_references", "missing_requirements")), "invalid draft references")
        store.write(output_name, raw)
    finally:
        try:
            os.unlink(stage_name, dir_fd=store.fd)
        except FileNotFoundError:
            pass
    print(f"[procedural-context] review-only draft: {store.path / output_name}", file=sys.stderr)


def checked_receipt(config: Config, store: State, value: dict) -> dict:
    receipt = value["receipt"]
    require(isinstance(receipt, dict) and receipt.get("schema_version") == 1
            and receipt.get("repository") == config.scope.repository
            and receipt.get("session_id") == store.identity["session_id"]
            and text(value["task"])
            and receipt.get("task") == value["task"]
            and receipt.get("task_digest") == value["task_digest"] == task_digest(value["task"])
            and receipt.get("generation") == value["generation"],
            "receipt does not match current task and scope")
    attempt = value["attempt"]
    require(isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt) is not None,
            "receipt has no delivery attempt")
    packet = validate_guidance(store.read(f"guidance-{attempt}.json"), config.scope.repository)
    require(receipt.get("guidance_as_of") == packet["as_of"]
            and receipt.get("delivered_rule_ids") == [
                item["rule_id"] for item in packet["rules"] + packet["anti_patterns"]],
            "receipt does not match delivered packet")
    return receipt


def handle(config: Config, event: Event, runner: Runner = run_child) -> bytes:
    store = State(config, event)
    snapshot = None
    receipt_to_draft = None
    try:
        with store.locked() as value:
            if event.shared or dispatch(event.tool, event.args):
                value["disabled"] = True
            if event.mode == "prompt":
                task = event.payload.get("prompt")
                require(text(task) and len(task.encode("utf-8")) <= 16384, "invalid or oversized prompt")
                invalidate(value, task)
            elif event.mode in ("stop", "compact", "start"):
                if (event.mode != "start" and value["receipt"] is not None
                        and not value["drafted"] and not value["disabled"]):
                    receipt_to_draft = checked_receipt(config, store, value)
                    value["drafted"] = True
                if event.mode in ("compact", "start"):
                    invalidate(value)
            elif event.mode == "before-tool" and event.tool in SEARCH_TOOLS and not value["disabled"]:
                starts = value["starts"]
                if len(starts) >= 256 or sum(s["count"] for s in starts.values()) >= 1024:
                    value["disabled"] = True
                    print("[procedural-context] unavailable: unresolved search capacity exceeded",
                          file=sys.stderr)
                else:
                    previous = starts.get(event.key)
                    starts[event.key] = {
                        "count": previous["count"] + 1 if previous else 1,
                        "generation": previous["generation"] if previous else value["generation"],
                        "ambiguous": previous is not None,
                    }
            elif event.mode == "after-tool" and event.tool in SEARCH_TOOLS:
                previous = value["starts"].get(event.key)
                if previous:
                    count = previous["count"]
                    if count == 1:
                        del value["starts"][event.key]
                    else:
                        previous["count"] -= 1
                    eligible = (count == 1 and not previous["ambiguous"]
                                and previous["generation"] == value["generation"]
                                and value["task"] is not None and value["attempt"] is None
                                and not value["disabled"])
                    if eligible and search_succeeded(event):
                        value["attempt"] = uuid.uuid4().hex
                        snapshot = {key: value[key] for key in (
                            "generation", "task", "task_digest", "attempt")}
                    elif eligible:
                        print("[procedural-context] unavailable: native search did not return valid success JSON",
                              file=sys.stderr)
            if value["disabled"]:
                print("[procedural-context] unavailable: session isolation or pairing unverified; "
                      "use manual guidance", file=sys.stderr)
        if receipt_to_draft is not None:
            draft(config, store, receipt_to_draft, runner)
        if snapshot is None:
            return b""
        argv = base_command(config, "guidance") + [
            "--task", snapshot["task"], "--max-items", "5", "--max-chars", "6000",
            "--max-bytes", "8192",
        ]
        raw = runner(argv, config.scope.root, MAX_PACKET)
        data = validate_guidance(raw, config.scope.repository)
        name = f"guidance-{snapshot['attempt']}.json"
        output = context_output(event, f"[procedural-context] {data['status']}; complete packet: {store.path / name}")
        with store.locked() as value:
            if value["disabled"] or any(value[key] != snapshot[key] for key in (
                    "generation", "task_digest", "attempt")):
                print("[procedural-context] unavailable: generation changed during guidance", file=sys.stderr)
                return b""
            store.write(name, raw)
            value["receipt"] = {
                "schema_version": 1, "repository": config.scope.repository,
                "session_id": event.session_id, "generation": snapshot["generation"],
                "task": snapshot["task"], "task_digest": snapshot["task_digest"],
                "delivered_rule_ids": [item["rule_id"] for item in data["rules"] + data["anti_patterns"]],
                "guidance_as_of": data["as_of"],
            }
            # Commit the receipt, then publish before releasing the CAS lock.
            store.publication = output
        return b""
    except (ValueError, OSError, UnicodeError):
        if event.mode in ("before-tool", "prompt", "start", "compact"):
            store.mark_uncertain()
        raise
    finally:
        store.close()


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise Unavailable("invalid adapter arguments: " + message)


def main() -> int:
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--config")
        parser.add_argument("--event", required=True, choices=tuple(EVENT_NAMES))
        args = parser.parse_args()
        if args.config is None:
            return 0
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT, "host input exceeds budget")
        payload = parse_json(raw)
        config = load_config(args.config, payload.get("cwd"))
        if config is None:
            return 0
        require(Path.cwd() == config.scope.root, "actual hook cwd does not match configured scope")
        event = normalize(args.event, payload)
        handle(config, event)
        return 0
    except (ValueError, OSError, UnicodeError, RecursionError) as error:
        print(f"[procedural-context] unavailable: {error}", file=sys.stderr)
        return 0
    except Exception:
        # The native pre-tool host denies on any nonzero exit. Preserve a full
        # diagnostic for bugs without converting optional advice to permission.
        traceback.print_exc(file=sys.stderr)
        print("[procedural-context] unavailable: unexpected adapter failure", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
