#!/usr/bin/env python3
"""PreToolUse audit hook: nag when external fact-finding bypasses mempalace_search.

Reads Copilot's hook JSON from stdin. Maintains a per-session ring buffer of
recent canonical capability tokens under $TMPDIR. If the current tool needs
recall and no mempalace_search occurred in the recent window, injects a
one-line reminder via hookSpecificOutput.additionalContext (non-blocking).

Tool names are canonicalized to harness-neutral capability ids so the hook
fires identically under VS Code Copilot Chat and Copilot CLI.

Failure mode: any error → exit 0 silently. This is an audit, not a gate.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

_I = re.IGNORECASE

# Capability id -> alias pattern covering both harnesses (VS Code Copilot Chat
# and Copilot CLI). Anchored + case-insensitive so PascalCase CLI names
# (Bash, Grep, ...) and snake_case VS Code names collapse onto one capability.
CAPABILITY_PATTERNS = {
    # always-fire: external fact-finding
    "web": re.compile(
        r"^(?:web_fetch|web_search|fetch_webpage|open_browser_page|read_page)$", _I
    ),
    "github": re.compile(
        r"^(?:github_repo|github_text_search|github-mcp-server-.+)$", _I
    ),
    "semantic": re.compile(r"^semantic_search$", _I),
    # repeat-fire: workspace search (only 2nd+ use in the window)
    "ws_grep": re.compile(r"^(?:grep_search|grep)$", _I),
    "ws_files": re.compile(r"^(?:file_search|glob)$", _I),
    # conditional: subagent (only recall-type agents)
    "subagent": re.compile(r"^(?:runsubagent|explore|task)$", _I),
    # conditional: terminal broad-probe
    "terminal": re.compile(r"^(?:run_in_terminal|bash)$", _I),
}

# Capabilities that always warrant a recall check.
ALWAYS_FIRE = frozenset({"web", "github", "semantic"})
# Workspace-search capabilities: fire only on the second-or-later call in the
# recent window. First targeted lookup is allowed without recall.
REPEAT_FIRE = frozenset({"ws_grep", "ws_files"})

# Only these agent identities count as recall-type subagents. CLI passes
# agent_type=explore; VS Code passes agentName=Explore (both lowercase equal).
RECALL_AGENT_NAMES = frozenset({"explore"})

# Broad terminal probes inside the terminal capability. Patterns match what the
# skill enumerates: `find ./|/|~`, `grep -r/-R`, `ls -*R`, `locate `,
# `(apt-cache|brew|npm|pip|cargo|gem) search`.
BROAD_PROBE_PATTERNS = (
    re.compile(r"(?<![\w-])find\s+[./~]"),
    re.compile(r"(?<![\w-])grep\s+-[a-zA-Z]*[rR]"),
    re.compile(r"(?<![\w-])ls\s+-[a-zA-Z]*R"),
    re.compile(r"(?<![\w-])locate\s+\S"),
    re.compile(r"(?<![\w-])(?:apt-cache|brew|npm|pip|cargo|gem)\s+search\b"),
)

# Matched by substring on the raw tool name (CLI: mempalace-mempalace_search;
# VS Code: mempalace_search) before it is canonicalized into the buffer.
SATISFY_PATTERN = re.compile(r"mempalace_search")
SATISFY_TOKEN = "satisfy"

# Rule 2 (routing audit): store_memory persists to Copilot's built-in memory,
# which is best for short always-on instincts. Project/code-scoped facts and
# decisions usually belong in a MemPalace drawer instead. When a store_memory
# call looks project-scoped and no MemPalace activity is in the window, nudge.
STORE_MEMORY_PATTERN = re.compile(r"^(?:store_memory|manage_memory)$", _I)

# Any recent buffer token mentioning mempalace (a drawer/kg/diary write, whose
# raw name is e.g. mempalace-mempalace_add_drawer) indicates the palace is
# already in use this window; combined with the search "satisfy" token it
# suppresses the routing nudge.
MEMPALACE_TOKEN = re.compile(r"mempalace")

# High-precision signals that a fact is about a codebase (paths, file names with
# a code extension, or a commit-ish hash) → drawer material, not an always-on
# instinct. Kept conservative to avoid nagging on genuine universal preferences.
PROJECT_SCOPE_PATTERNS = (
    re.compile(r"(?<![\w-])[\w.-]+/[\w./-]*\w"),  # a slash-joined path
    re.compile(
        r"\.(?:py|fsx?|fsi|fsproj|tsx?|jsx?|go|rs|rb|json|ya?ml|md|sh|toml|cs|cpp|c|h|sql)\b",
        _I,
    ),  # a code/config file extension
    re.compile(
        r"(?<![\w])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}(?![\w])"
    ),  # commit-ish hash (mixed hex: needs a letter a-f AND a digit)
)

WINDOW = 6
SAFE_SESSION = re.compile(r"[^A-Za-z0-9_.-]")


def capability_of(tool_name: str) -> str | None:
    for cap, pat in CAPABILITY_PATTERNS.items():
        if pat.match(tool_name):
            return cap
    return None


def canonical_token(tool_name: str) -> str:
    """Harness-neutral buffer token: satisfy marker, capability id, or raw name.

    Storing the capability id (not the raw name) lets repeat detection work
    across harnesses: a CLI `Grep` followed by a VS Code `grep_search` both
    become `ws_grep`.
    """
    if SATISFY_PATTERN.search(tool_name):
        return SATISFY_TOKEN
    return capability_of(tool_name) or tool_name


def arg(tool_input: dict, *keys: str) -> str:
    """First present key wins — absorbs harness arg-name drift."""
    for k in keys:
        if k in tool_input:
            return str(tool_input[k])
    return ""


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


def is_trigger(tool_name: str, tool_input: dict, recent_caps: list[str]) -> bool:
    cap = capability_of(tool_name)
    if cap in ALWAYS_FIRE:
        return True
    if cap in REPEAT_FIRE and cap in recent_caps:
        return True
    if cap == "subagent":
        return arg(tool_input, "agentName", "agent_type").lower() in RECALL_AGENT_NAMES
    if cap == "terminal":
        command = arg(tool_input, "command")
        return any(p.search(command) for p in BROAD_PROBE_PATTERNS)
    return False


def is_project_scoped_memory(tool_input: dict) -> bool:
    """True if a store_memory fact reads as project/code-scoped (drawer material)."""
    text = " ".join(
        arg(tool_input, k) for k in ("fact", "citations", "reason", "subject")
    )
    return any(p.search(text) for p in PROJECT_SCOPE_PATTERNS)


def reminder(tool_name: str) -> str:
    return (
        f"[palace-reflex] About to call '{tool_name}' without a recent "
        "mempalace_search in this session. Consider calling mempalace_search "
        "first; skip only for pure syntax Q&A, a single trivial edit, or if "
        "the user said 'don't check memory'."
    )


ROUTING_REMINDER = (
    "[palace-reflex] This store_memory looks project/code-scoped (paths, file "
    "names, or a commit hash). Project decisions and code rationale usually "
    "belong in a MemPalace drawer (mempalace_add_drawer) — store_memory is best "
    "for short, always-on universal instincts. Consider filing a drawer too; "
    "skip if this really is a brief universal preference."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = str(payload.get("session_id", ""))
    if not tool_name:
        return 0

    path = buffer_path(session_id)
    recent = load_recent(path)  # canonical tokens from earlier calls in window
    satisfied = SATISFY_TOKEN in recent
    palace_engaged = satisfied or any(MEMPALACE_TOKEN.search(t) for t in recent)

    msg: str | None = None
    if is_trigger(tool_name, tool_input, recent) and not satisfied:
        msg = reminder(tool_name)
    elif (
        STORE_MEMORY_PATTERN.match(tool_name)
        and is_project_scoped_memory(tool_input)
        and not palace_engaged
    ):
        msg = ROUTING_REMINDER

    append(path, canonical_token(tool_name))

    if msg is not None:
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
