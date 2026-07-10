#!/usr/bin/env python3
"""Copilot CLI -> MemPalace save-hook adapter.

MemPalace's built-in ``mempalace hook run --harness claude-code`` writes a diary
entry (silent_save) from Stop/PreCompact hooks. But its transcript parsers only
understand Claude Code (``{"type":"user","message":{"role":"user",...}}``) and
Codex (``event_msg``) schemas. Copilot CLI writes ``events.jsonl`` in a third
schema (``{"type":"user.message","data":{"content":...}}`` with ``cwd`` nested
under ``session.start``), so wiring the CLI hook directly is a silent no-op —
0 messages counted, no wing derived, nothing saved.

This adapter bridges the gap: it reads Copilot's Stop/PreCompact stdin JSON,
translates ``events.jsonl`` into a temporary Claude-format JSONL (top-level
``cwd`` on every line so wing derivation works), then invokes
``mempalace hook run`` with a synthesized stdin pointing at the temp file. All
of mempalace's save/theme/ingest logic is reused unchanged. Its stdout (e.g. a
``systemMessage``) is passed through.

Failure mode: any error -> print ``{}`` and exit 0. This is a save helper, never
a gate; it must not block the agent.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        role = _ROLE_BY_TYPE.get(entry.get("type"))
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
                }
            )
        )
    return out


def map_hook(event_name: str) -> Optional[str]:
    """Map a Copilot ``hook_event_name`` to a ``mempalace hook run`` flag."""
    return _HOOK_FLAGS.get(str(event_name or "").lower())


def _mempalace_bin() -> Optional[str]:
    """Locate the mempalace launcher (env override wins, else PATH)."""
    env = os.environ.get("MEMPALACE_BIN", "")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    return shutil.which("mempalace")


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return 0
    if not isinstance(payload, dict):
        print("{}")
        return 0

    flag = map_hook(payload.get("hook_event_name", ""))
    transcript_path = str(payload.get("transcript_path", ""))
    session_id = str(payload.get("session_id", ""))
    cwd = str(payload.get("cwd", ""))

    if not flag or not transcript_path or not os.path.isfile(transcript_path):
        print("{}")
        return 0

    mp = _mempalace_bin()
    if not mp:
        print("{}")
        return 0

    tmp_path = None
    try:
        translated = translate_events(_read_lines(transcript_path), cwd)
        fd, tmp_path = tempfile.mkstemp(prefix="copilot-claude-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            tf.write("\n".join(translated))
            if translated:
                tf.write("\n")

        proc = subprocess.run(
            [mp, "hook", "run", "--hook", flag, "--harness", SUPPORTED_HARNESS],
            input=json.dumps(
                {
                    "hook_event_name": payload.get("hook_event_name", ""),
                    "session_id": session_id,
                    "transcript_path": tmp_path,
                    "stop_hook_active": payload.get("stop_hook_active", False),
                }
            ),
            capture_output=True,
            text=True,
            timeout=25,
        )
        out = proc.stdout.strip()
        print(out if out else "{}")
    except Exception:
        print("{}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
