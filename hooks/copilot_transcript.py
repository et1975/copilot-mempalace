#!/usr/bin/env python3
"""Capture Copilot observations through the provisioned MemPalace tool API.

Stop captures every 15 new user messages; PreCompact and SessionEnd flush the
remaining tail, including assistant-only tails. The upstream hook diary is a
bounded user-only summary and its miner is detached, so neither is a receipt for
exact capture. The companion worker files exact, deterministic observation
envelopes under the original project wing, using the installed hooks routing.
Session-start context owns the destination; callback context must agree. Legacy
records without context require an explicit absolute callback cwd. The chosen
destination is pinned across captures, including resumed sessions.

Per-session ``mempalace-save/status.json`` retains a confirmed prefix digest,
non-sensitive result and pending intent. Uncertain writes require reconciliation,
not automatic republishing. stdout is always the non-blocking hook response {};
stderr is one content-free JSON diagnostic. MEMPALACE_PYTHON must explicitly
name an already-provisioned interpreter. No packages, daemon, feedback or
procedural-learning workflow are installed or started here.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from datetime import datetime
from typing import Iterable, Optional

# Copilot events.jsonl message type -> Claude transcript role.
_ROLE_BY_TYPE = {
    "user.message": "user",
    "assistant.message": "assistant",
}

# Copilot hook_event_name -> `mempalace hook run --hook` flag value.
_HOOK_FLAGS = {
    "stop": "stop",
    "precompact": "precompact",
    "sessionstart": "session-start",
    "sessionend": "session-end",
}

SUPPORTED_HARNESS = "claude-code"
SAVE_INTERVAL = 15
SAVE_TIMEOUT = 25
HOOK_BUDGET = 28
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_REASONS = {"complete", "error", "abort", "timeout", "user_exit"}


class CaptureError(Exception):
    """A content-free, safe-to-display validation or storage failure."""


def translate_events(lines: Iterable[str], cwd: str) -> list[str]:
    """Translate Copilot ``events.jsonl`` lines into Claude-format JSONL lines.

    Only ``user.message`` / ``assistant.message`` events become message lines;
    every other event type is dropped. The clean ``data.content`` is used (never
    ``transformedContent``, which carries injected system reminders). Empty or
    whitespace-only content is skipped, as are malformed lines. Each emitted line
    carries a top-level ``cwd`` so mempalace's wing derivation succeeds.
    """
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        event_type = entry.get("type")
        role = _ROLE_BY_TYPE.get(event_type) if isinstance(event_type, str) else None
        if role is None:
            continue
        data = entry.get("data") or {}
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, str) or not content.strip():
            continue
        out.append(
            json.dumps(
                {
                    "type": role,
                    "message": {"role": role, "content": content},
                    "cwd": cwd,
                    **({"uuid": entry["id"]} if "id" in entry else {}),
                    **({"timestamp": entry["timestamp"]} if "timestamp" in entry else {}),
                }
            )
        )
    return out


def map_hook(event_name: str) -> Optional[str]:
    """Map a Copilot ``hook_event_name`` to a ``mempalace hook run`` flag."""
    return _HOOK_FLAGS.get(str(event_name or "").lower())


def _mempalace_python() -> Optional[str]:
    """Require an explicit interpreter; never infer it from a CLI launcher."""
    path = os.environ.get("MEMPALACE_PYTHON", "")
    if path and Path(path).is_absolute() and os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _safe_path(path: Path) -> Path:
    if ".." in path.parts or "\x00" in str(path):
        raise CaptureError("unsafe_path")
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise CaptureError("symlink_path")
    return path


def _open_regular(path: Path, flags: int = os.O_RDONLY):
    _safe_path(path)
    fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise CaptureError("not_regular_file")
    if flags & os.O_TRUNC:
        os.ftruncate(fd, 0)
    return os.fdopen(fd, "r+" if flags & os.O_RDWR else "r", encoding="utf-8")


def _read_json(path: Path):
    with _open_regular(path) as source:
        return json.load(source)


def _atomic_json(path: Path, value: dict) -> None:
    staging = path.with_suffix(".new")
    _safe_path(path)
    with _open_regular(staging, os.O_CREAT | os.O_RDWR | os.O_TRUNC) as target:
        json.dump(value, target, sort_keys=True)
        target.flush()
        os.fsync(target.fileno())
    os.replace(staging, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _project_path(value) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CaptureError("invalid_project_context")
    path = Path(value)
    if not path.is_absolute():
        raise CaptureError("invalid_project_context")
    path = path.resolve()
    if not path.name:
        raise CaptureError("invalid_project_context")
    return str(path)


def _messages(path: Path, session_id: str, callback_cwd) -> tuple[list[dict], str]:
    with _open_regular(path) as source:
        entries = [json.loads(line) for line in source if line.strip()]
    if any(not isinstance(entry, dict) for entry in entries):
        raise CaptureError("invalid_transcript")
    identities = [
        entry.get("data", {}).get("sessionId")
        for entry in entries
        if entry.get("type") == "session.start" and isinstance(entry.get("data"), dict)
    ]
    if not identities or any(identity != session_id for identity in identities):
        raise CaptureError("session_identity_mismatch")
    projects = set()
    for entry in entries:
        if entry.get("type") != "session.start":
            continue
        data = entry.get("data", {})
        if "context" in data:
            context = data["context"]
            if not isinstance(context, dict):
                raise CaptureError("invalid_project_context")
            projects.add(_project_path(context.get("cwd")))
    callback = _project_path(callback_cwd) if callback_cwd not in (None, "") else None
    if len(projects) > 1:
        raise CaptureError("project_context_conflict")
    if projects:
        project = projects.pop()
        if callback is not None and callback != project:
            raise CaptureError("project_context_conflict")
    elif callback is not None:
        # True legacy records omit the context member entirely. A malformed
        # modern context never falls back to callback-controlled attribution.
        project = callback
    else:
        raise CaptureError("missing_project_context")
    translated = [json.loads(line) for line in translate_events(map(json.dumps, entries), project)]
    seen = set()
    for message in translated:
        event_id, timestamp = message.get("uuid"), message.get("timestamp")
        if not isinstance(event_id, str) or not _SESSION_ID.fullmatch(event_id):
            raise CaptureError("invalid_event_identity")
        if event_id in seen:
            raise CaptureError("duplicate_event_identity")
        seen.add(event_id)
        if not isinstance(timestamp, str):
            raise CaptureError("invalid_event_timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise CaptureError("invalid_event_timestamp") from error
        if parsed.tzinfo is None:
            raise CaptureError("invalid_event_timestamp")
        message["sessionId"] = session_id
    return translated, project


def _digest(messages: list[dict]) -> str:
    # Destination is independently validated and pinned in the session state.
    content = [{key: value for key, value in item.items() if key != "cwd"} for item in messages]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()


def _report(outcome: str, code: str, **fields) -> dict:
    return {"outcome": outcome, "code": code, **fields}


def _capture_locked(
    payload: dict, directory: Path, transcript: Path, session_id: str, deadline: float,
) -> dict:
    state_path = directory / "status.json"
    state = {
        "version": 3,
        "session_id": session_id,
        "cursor": {"messages": 0, "users": 0, "sha256": _digest([])},
        "pending": None,
    }
    if state_path.exists():
        previous = _read_json(state_path)
        if (
            not isinstance(previous, dict) or previous.get("version") != 3
            or previous.get("session_id") != session_id
            or not isinstance(previous.get("cursor"), dict)
        ):
            raise CaptureError("invalid_save_state")
        state = previous
    state.update(
        hook_event_name=payload["hook_event_name"],
        timestamp=payload.get("timestamp"),
    )
    if map_hook(payload["hook_event_name"]) == "session-end":
        state["termination_reason"] = payload["reason"]

    def finish(outcome, code, **fields):
        result = _report(outcome, code, session_id=session_id, **fields)
        for name in ("captured_messages", "stderr_present", "returncode"):
            state.pop(name, None)
        state.update(result)
        _atomic_json(state_path, state)
        return result

    if state.get("pending") is not None:
        return finish("unknown", "unsettled_previous_attempt")
    try:
        messages, project = _messages(transcript, session_id, payload.get("cwd"))
    except CaptureError as error:
        return finish("failed", str(error))
    except (json.JSONDecodeError, UnicodeError):
        return finish("failed", "invalid_json_or_encoding")
    except OSError as error:
        return finish("failed", "filesystem_error", errno=error.errno)
    if state.get("project_cwd", project) != project:
        return finish("failed", "project_context_changed")
    state["project_cwd"] = project
    cursor = state["cursor"]
    count = cursor.get("messages")
    if type(count) is not int or count < 0 or type(cursor.get("users")) is not int:
        raise CaptureError("invalid_save_state")
    if count > len(messages) or _digest(messages[:count]) != cursor.get("sha256"):
        return finish("failed", "transcript_rewritten")
    tail = messages[count:]
    if not tail:
        return finish("skipped", "already_captured" if messages else "empty")
    users = sum(item["type"] == "user" for item in messages)
    if map_hook(payload["hook_event_name"]) == "stop" and users - cursor["users"] < SAVE_INTERVAL:
        return finish("skipped", "below_interval")
    interpreter = _mempalace_python()
    if not interpreter:
        return finish("failed", "mempalace_python_unavailable")
    worker = _safe_path(Path(__file__).with_name("copilot_capture.py"))
    if not worker.is_file():
        return finish("failed", "capture_worker_unavailable")
    remaining = min(SAVE_TIMEOUT, deadline - time.monotonic())
    if remaining <= 0:
        return finish("failed", "hook_budget_exhausted")

    # Retain exact intent for reconciliation rather than deleting a tempfile
    # while a submitted daemon write may still be unsettled.
    snapshot = directory / "pending.jsonl"
    with _open_regular(snapshot, os.O_CREAT | os.O_RDWR | os.O_TRUNC) as target:
        target.write("\n".join(map(json.dumps, tail)) + "\n")
        target.flush()
        os.fsync(target.fileno())
    state["pending"] = {"messages": len(tail), "sha256": _digest(tail)}
    finish("unknown", "save_in_progress")
    try:
        proc = subprocess.run(
            [interpreter, str(worker)],
            input=json.dumps({
                "protocol": 1, "session_id": session_id, "snapshot": str(snapshot),
                "source_file": str(transcript), "sha256": _digest(tail),
                "timeout": remaining, "project_cwd": project,
            }),
            capture_output=True, text=True, timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        return finish("unknown", "save_timeout")
    except OSError:
        state["pending"] = None
        return finish("failed", "save_not_started")
    if proc.returncode:
        # A nonzero exit can follow a committed partial batch.
        return finish(
            "unknown", "save_process_failed", returncode=proc.returncode,
            stderr_present=bool(proc.stderr),
        )
    try:
        receipt = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return finish("unknown", "unrecognized_save_receipt")
    if (
        not isinstance(receipt, dict) or receipt.get("protocol") != 1
        or receipt.get("sha256") != _digest(tail)
        or receipt.get("messages") != len(tail)
    ):
        return finish("unknown", "incomplete_save_receipt")
    outcome = receipt.get("outcome")
    code = receipt.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        return finish("unknown", "unrecognized_save_receipt")
    if outcome in ("failed", "skipped") and receipt.get("write_started") is False:
        state["pending"] = None
        return finish(outcome, code)
    drawer_ids = receipt.get("drawer_ids")
    if outcome != "saved":
        return finish("unknown", code)
    if (
        type(receipt.get("parts")) is not int or receipt["parts"] < len(tail)
        or not isinstance(drawer_ids, list) or len(drawer_ids) != receipt["parts"]
        or any(not isinstance(value, str) or not value for value in drawer_ids)
        or len(set(drawer_ids)) != receipt["parts"]
        or not isinstance(receipt.get("wing"), str) or not receipt["wing"]
        or receipt.get("room") != "diary" or receipt.get("route") != "mcp"
    ):
        return finish("unknown", "incomplete_save_receipt")
    state["cursor"] = {"messages": len(messages), "users": users, "sha256": _digest(messages)}
    state["pending"] = None
    state["receipt"] = {
        "sha256": _digest(tail), "first_drawer_id": drawer_ids[0],
        "last_drawer_id": drawer_ids[-1], "wing": receipt["wing"],
        "room": receipt["room"], "route": receipt["route"], "parts": receipt["parts"],
    }
    return finish(
        "saved", "confirmed", captured_messages=len(tail),
        stderr_present=bool(proc.stderr), wing=receipt["wing"], room="diary",
    )


def capture(payload: dict) -> dict:
    deadline = time.monotonic() + HOOK_BUDGET
    flag = map_hook(payload.get("hook_event_name", ""))
    if flag not in ("stop", "precompact", "session-end"):
        return _report("skipped", "unsupported_event")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise CaptureError("invalid_session_id")
    if flag == "session-end":
        reason = payload.get("reason")
        if not isinstance(reason, str) or reason not in _REASONS:
            raise CaptureError("invalid_termination_reason")
    home = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    session = _safe_path(home / "session-state" / session_id)
    explicit = payload.get("transcript_path")
    if explicit is not None and (not isinstance(explicit, str) or not explicit):
        raise CaptureError("invalid_transcript_path")
    transcript = _safe_path(Path(explicit) if explicit else session / "events.jsonl")
    directory = _safe_path(session / "mempalace-save")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    import fcntl

    with _open_regular(directory / "save.lock", os.O_CREAT | os.O_RDWR) as lock:
        # An exit/compaction snapshot can contain a tail absent from an in-flight
        # Stop. Wait within the hook's budget rather than silently dropping it.
        for _ in range(600):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if flag == "stop":
                    return _report("skipped", "save_in_progress", session_id=session_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
            else:
                return _capture_locked(payload, directory, transcript, session_id, deadline)
        return _report("failed", "save_lock_timeout", session_id=session_id)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise CaptureError("invalid_payload")
        result = capture(payload)
    except CaptureError as error:
        result = _report("failed", str(error))
    except (json.JSONDecodeError, UnicodeError):
        result = _report("failed", "invalid_json_or_encoding")
    except OSError as error:
        result = _report("failed", "filesystem_error", errno=error.errno)
    print(json.dumps(result), file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
