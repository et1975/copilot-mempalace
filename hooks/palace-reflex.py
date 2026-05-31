#!/usr/bin/env python3
"""PreToolUse audit hook: nag when external fact-finding bypasses mempalace_search.

Reads Copilot's hook JSON from stdin. Maintains a per-session ring buffer of
recent tool names under $TMPDIR. If the current tool is in the "needs recall"
set and no mempalace_search occurred in the recent window, injects a one-line
reminder via hookSpecificOutput.additionalContext (non-blocking).

Failure mode: any error → exit 0 silently. This is an audit, not a gate.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

TRIGGER_TOOLS = frozenset({
    "fetch_webpage",
    "open_browser_page",
    "read_page",
    "github_repo",
    "github_text_search",
    "semantic_search",
})

# Workspace search tools: only fire on the second-or-later call in the recent
# window. First targeted lookup is allowed without recall (matches the skill).
REPEAT_SEARCH_TOOLS = frozenset({"grep_search", "file_search"})

SUBAGENT_TOOLS = frozenset({"runSubagent", "Explore"})
RECALL_AGENT_NAMES = frozenset({"explore"})

# Broad terminal probes inside run_in_terminal. Patterns match what the skill
# enumerates: `find ./|/|~`, `grep -r/-R`, `ls -*R`, `locate `,
# `(apt-cache|brew|npm|pip|cargo|gem) search`.
BROAD_PROBE_PATTERNS = (
    re.compile(r"(?<![\w-])find\s+[./~]"),
    re.compile(r"(?<![\w-])grep\s+-[a-zA-Z]*[rR]"),
    re.compile(r"(?<![\w-])ls\s+-[a-zA-Z]*R"),
    re.compile(r"(?<![\w-])locate\s+\S"),
    re.compile(r"(?<![\w-])(?:apt-cache|brew|npm|pip|cargo|gem)\s+search\b"),
)

TERMINAL_TOOL = "run_in_terminal"

SATISFY_PATTERN = re.compile(r"mempalace_search")

WINDOW = 6
SAFE_SESSION = re.compile(r"[^A-Za-z0-9_.-]")


def buffer_path(session_id: str) -> Path:
    sid = SAFE_SESSION.sub("_", session_id or "default")[:64]
    return Path(tempfile.gettempdir()) / f"copilot-palace-reflex-{sid}.log"


def load_recent(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()[-WINDOW:]
    except FileNotFoundError:
        return []
    except OSError:
        return []


def append(path: Path, name: str) -> None:
    try:
        recent = load_recent(path)
        recent.append(name)
        recent = recent[-WINDOW:]
        path.write_text("\n".join(recent) + "\n")
    except OSError:
        pass


def is_trigger(tool_name: str, tool_input: dict, recent: list[str]) -> bool:
    if tool_name in TRIGGER_TOOLS:
        return True
    if tool_name in REPEAT_SEARCH_TOOLS and tool_name in recent:
        return True
    if tool_name in SUBAGENT_TOOLS:
        agent = str(tool_input.get("agentName", "")).lower()
        return agent in RECALL_AGENT_NAMES
    if tool_name == TERMINAL_TOOL:
        command = str(tool_input.get("command", ""))
        return any(p.search(command) for p in BROAD_PROBE_PATTERNS)
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    session_id = str(payload.get("session_id", ""))
    if not tool_name:
        return 0

    path = buffer_path(session_id)
    recent = load_recent(path)
    satisfied = any(SATISFY_PATTERN.search(name) for name in recent)
    triggered = is_trigger(tool_name, tool_input if isinstance(tool_input, dict) else {}, recent)

    append(path, tool_name)

    if triggered and not satisfied:
        msg = (
            f"[palace-reflex] About to call '{tool_name}' without a recent "
            "mempalace_search in this session. Consider calling mempalace_search "
            "first; skip only for pure syntax Q&A, a single trivial edit, or if "
            "the user said 'don't check memory'."
        )
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": msg,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
