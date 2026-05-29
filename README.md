# AI-assisted memory (MemPalace)

Copilot customization pack that turns [MemPalace](https://github.com/mempalace/mempalace) into the agent's default memory.
Three hard rules — recall before external fact-finding, save every new fact, end-of-turn diary — plus a `PreToolUse`
audit hook that nags when an external tool is about to run without a prior `mempalace_search`.

## What's Included

- **[copilot-instructions.md](copilot-instructions.md)** — Rule 1 (read-first), Rule 2 (save-every-fact),
  Rule 3 (end-of-turn diary), routing between MemPalace and Copilot's built-in `/memories/`, and a "memory
  checkup" workflow. Drop into your home or repo `copilot-instructions.md` (concatenate or replace).
- **[skills/mempalace/SKILL.md](skills/mempalace/SKILL.md)** — full skill: 30 MCP tools (read/write/tunnels/KG/diary),
  proactive vs reactive use, mining hygiene, HNSW drift recovery, auto-save hook notes.
- **[hooks/palace-reflex.json](hooks/palace-reflex.json) + [hooks/palace-reflex.py](hooks/palace-reflex.py)** —
  `PreToolUse` audit hook. Maintains a per-session ring buffer of recent tool calls under `$TMPDIR`. When a
  trigger tool (`fetch_webpage`, `open_browser_page`, `read_page`, `github_repo`, `github_text_search`, or
  `runSubagent` with `agentName == "Explore"`) fires without a recent `mempalace_search`, it injects a one-line
  reminder via `hookSpecificOutput.additionalContext`. Non-blocking — audit, not gate.
- **[memories/mempalace-first.md](memories/mempalace-first.md)** — terse auto-loaded reflex stub designed for
  Copilot's user memory (`/memories/`). First 200 lines of user memory are auto-loaded into every conversation,
  so the rule is in context even when the full instructions get pushed out.

## Requirements

- [MemPalace](https://github.com/mempalace/mempalace) installed and exposed as an MCP server in your harness.
  Verify with `mempalace status` and confirm the harness lists `mempalace_*` tools.
- Python 3 on `PATH` (for the hook). The hook fails silently if Python is missing.

## Install

Copy or symlink into your Copilot config. For VS Code / Copilot Chat the config root is `~/.copilot/`.

```bash
# 1. Instructions (concatenate or replace your existing copilot-instructions.md)
cat copilot-instructions.md >> ~/.copilot/copilot-instructions.md

# 2. Skill
mkdir -p ~/.copilot/skills
ln -s "$(pwd)/skills/mempalace" ~/.copilot/skills/mempalace

# 3. Hook (both files together so the JSON's relative reference resolves)
mkdir -p ~/.copilot/hooks
ln -s "$(pwd)/hooks/palace-reflex.json" ~/.copilot/hooks/palace-reflex.json
ln -s "$(pwd)/hooks/palace-reflex.py"   ~/.copilot/hooks/palace-reflex.py
```

The hook JSON references `python3 ~/.copilot/hooks/palace-reflex.py`. If you'd rather keep the script outside
`~/.copilot/hooks/`, edit the `command` field accordingly.

### Seeding Copilot user memory

The auto-loaded reflex stub in `memories/mempalace-first.md` is meant for Copilot's `/memories/` store,
which is managed by the agent (not the filesystem). Ask Copilot once: *"create `/memories/mempalace-first.md`
with the content of `memories/mempalace-first.md` from this repo."*

## Verifying the hook

```bash
# Trigger condition: fetch_webpage without a prior mempalace_search → should print the reminder JSON
echo '{"session_id":"test","tool_name":"fetch_webpage","tool_input":{},"hook_event_name":"PreToolUse"}' \
  | python3 hooks/palace-reflex.py

# Satisfied: after a mempalace_search, the same call is silent
echo '{"session_id":"test","tool_name":"mempalace_search","tool_input":{},"hook_event_name":"PreToolUse"}' \
  | python3 hooks/palace-reflex.py
echo '{"session_id":"test","tool_name":"fetch_webpage","tool_input":{},"hook_event_name":"PreToolUse"}' \
  | python3 hooks/palace-reflex.py
```

The first call prints the reminder; the second pair is silent.

## Harness compatibility

The hook protocol matches Claude Code's: stdin JSON with `tool_name` / `tool_input` / `session_id`, stdout
JSON with `hookSpecificOutput.additionalContext` to inject context. Verified with VS Code Copilot Chat and
Copilot CLI. Should work in any harness that consumes the same shape (Claude Code, Cursor).

## License

Apache 2.0 — see [LICENSE.md](LICENSE.md).
