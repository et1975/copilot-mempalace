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
- **[skills/dreaming/SKILL.md](skills/dreaming/SKILL.md)** — offline consolidation ("dreaming"): a 5-phase
  pipeline (harvest → adjudicate → review → adopt → verify) that merges near-duplicate drawers and resolves
  adjudicated KG contradiction/staleness candidates between sessions, plus `pattern` / `induce` for surfacing
  grounded cross-session lessons from stamped diary observations, and `prune` / `forget` for reversible
  archive-before-delete cleanup of low-salience drawers.
  Cognition stays in the agent, mechanics in Python scripts
  ([`skills/dreaming/scripts/`](skills/dreaming/scripts/)), storage in mempalace. The optional read-only
  `dream_sessions.py` adapter uses Copilot's host session store as a richer pattern substrate. Non-destructive
  (nothing writes the live palace until an approved `decisions.json` is adopted; prune writes a lossless JSONL
  archive before sanctioned delete) with a fixpoint re-harvest as the verify oracle. Native drawer usage-frequency
  is proposed upstream as MemPalace/mempalace#1921.
  See [`skills/dreaming/references/pipeline.md`](skills/dreaming/references/pipeline.md) for the contract.
- **[skills/contemplate/SKILL.md](skills/contemplate/SKILL.md)** — on-demand deductive reasoning over the
  MemPalace KG: run `derive` inline when the user asks what follows, then adjudicate
  `materialize` / `skip` / `reject_rule` candidates. It shares the same Python mechanics in
  [`skills/dreaming/scripts/`](skills/dreaming/scripts/) and documents the derive contract in
  [`skills/contemplate/references/derive.md`](skills/contemplate/references/derive.md).
- **[skills/mempalace-backup/SKILL.md](skills/mempalace-backup/SKILL.md)** — safely back up the local palace with
  [`restic`](https://restic.net/): quiesce writers, checkpoint both SQLite WALs, snapshot `~/.mempalace/`
  (excluding ephemeral `locks/`, always keeping `palace/.mempalace/origin.json`), verify, and prune. Local repos
  only, on demand. Ships a tested Python helper
  ([`scripts/palace_backup.py`](skills/mempalace-backup/scripts/palace_backup.py) + `test_palace_backup.py`) that
  needs no `sqlite3` CLI, plus a [restic cheatsheet](skills/mempalace-backup/references/restic-cheatsheet.md).
  For **per-wing** archival/migration/cloning (which restic cannot do, since wings share physical storage), it also
  ships [`scripts/palace_wing.py`](skills/mempalace-backup/scripts/palace_wing.py) — a logical wing export/import that
  reads the palace SQLite directly into a portable JSONL bundle and replays it back.
- **[skills/mempalace-restore/SKILL.md](skills/mempalace-restore/SKILL.md)** — restore / disaster-recovery
  counterpart: reversible staged restore (move the current palace aside first), restic's absolute-path-stripping
  subpath syntax so files land directly, then `mempalace repair` / `repair-status` and MCP `mempalace_reconnect`.
  Scenario runbook in [references/disaster-recovery.md](skills/mempalace-restore/references/disaster-recovery.md).
- **[hooks/palace-reflex.json](hooks/palace-reflex.json) + [hooks/palace-reflex.py](hooks/palace-reflex.py)** —
  `PreToolUse` audit hook. Maintains a per-session ring buffer of recent tool calls under `$TMPDIR`. Fires when
  a trigger tool runs without a recent `mempalace_search`, injecting a one-line reminder via
  `hookSpecificOutput.additionalContext`. Non-blocking — audit, not gate. Triggers: `fetch_webpage`,
  `open_browser_page`, `read_page`, `github_repo`, `github_text_search`, `semantic_search`, `runSubagent` with
  `agentName == "Explore"`, second-or-later `grep_search`/`file_search` in the same window, and
  `run_in_terminal` commands matching broad-probe patterns (`find ./…`, `grep -r/-R`, `ls -*R`, `locate`,
  `(apt-cache|brew|npm|pip|cargo|gem) search`).
- **[hooks/mempalace-save.json](hooks/mempalace-save.json) + [hooks/copilot_transcript.py](hooks/copilot_transcript.py)** —
  `Stop` + `PreCompact` **save** hook (the automated counterpart to the read-first nudge). MemPalace's built-in
  `mempalace hook run --harness claude-code` writes a diary entry via silent-save, but its transcript parsers only
  understand Claude Code and Codex schemas — Copilot CLI writes `events.jsonl` in a third schema
  (`{"type":"user.message","data":{"content":…}}`, `cwd` nested under `session.start`), so wiring the CLI hook
  directly is a **silent no-op**: 0 messages counted, no wing derived, nothing saved. `copilot_transcript.py`
  bridges that: it translates `events.jsonl` into a temporary Claude-format JSONL (top-level `cwd` on every line so
  wing derivation works), then invokes `mempalace hook run`, reusing all of mempalace's count/theme/save/ingest
  logic. Any error → `{}` exit 0; a save helper, never a gate. Posix-only (`python3` `command`), matching
  the `palace-reflex` hook; Windows is untested. Fires the actual save every `SAVE_INTERVAL` (15) human messages;
  `PreCompact` is the emergency save before context loss.
- **[memories/mempalace-first.md](memories/mempalace-first.md)** — terse auto-loaded reflex stub designed for
  Copilot's user memory (`/memories/`). First 200 lines of user memory are auto-loaded into every conversation,
  so the rule is in context even when the full instructions get pushed out.

## Requirements

- [MemPalace](https://github.com/mempalace/mempalace) installed (`uv tool install mempalace` or `pip install mempalace`).
  Verify with `mempalace status`.
- MemPalace exposed as an MCP server in your harness — see [Step 0](#step-0--register-mempalace-as-an-mcp-server) below.
- Python 3 on `PATH` (for the hook). The hook fails silently if Python is missing.
- For the backup/restore skills only: [`restic`](https://restic.net/) on `PATH`
  (`zypper in restic`, `apt install restic`, `brew install restic`, …).

## Install

### Step 0 — Register MemPalace as an MCP server

The skill, hook, and instructions all assume the agent can see `mempalace_*` tools. They aren't wired by default —
register the server once per harness. The canonical command (`mempalace mcp` prints the latest form) is:

```
mempalace-mcp
```

This binary ships with the `mempalace` install and is on `PATH` after `uv tool install` / `pip install`.

**GitHub Copilot CLI:**

```bash
copilot mcp add mempalace -- mempalace-mcp
# verify
copilot mcp list | grep mempalace
```

**VS Code / Copilot Chat** — edit `~/Library/Application Support/Code/User/mcp.json` (macOS) /
`%APPDATA%\Code\User\mcp.json` (Windows) / `~/.config/Code/User/mcp.json` (Linux):

```jsonc
{
  "servers": {
    "mempalace": {
      "type": "stdio",
      "command": "mempalace-mcp"
    }
  }
}
```

Restart the harness, then confirm `mempalace_*` tools appear in the agent's toolset (in VS Code: open the Copilot
Chat tool picker; in the CLI: `copilot mcp get mempalace`). Other harnesses (Claude Code, Cursor) use the same
`mempalace-mcp` command in their respective config files — see [`skills/mempalace/references/harness-config.md`](skills/mempalace/references/harness-config.md).

Optional: pin a non-default palace location with `mempalace-mcp --palace /path/to/palace`.

### Step 1 — Copy or symlink the customization pack

For VS Code / Copilot Chat the config root is `~/.copilot/`.

```bash
# 1.1 Instructions (concatenate or replace your existing copilot-instructions.md)
cat copilot-instructions.md >> ~/.copilot/copilot-instructions.md

# 1.2 Skill
mkdir -p ~/.copilot/skills
ln -s "$(pwd)/skills/mempalace" ~/.copilot/skills/mempalace

# 1.3 Hook (both files together so the JSON's relative reference resolves)
mkdir -p ~/.copilot/hooks
ln -s "$(pwd)/hooks/palace-reflex.json" ~/.copilot/hooks/palace-reflex.json
ln -s "$(pwd)/hooks/palace-reflex.py"   ~/.copilot/hooks/palace-reflex.py

# 1.4 Save hook (Stop + PreCompact → automated diary save). Requires the
#     mempalace binary on PATH (or set MEMPALACE_BIN). Both files together.
ln -s "$(pwd)/hooks/mempalace-save.json"   ~/.copilot/hooks/mempalace-save.json
ln -s "$(pwd)/hooks/copilot_transcript.py" ~/.copilot/hooks/copilot_transcript.py
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

# Broad terminal probe inside run_in_terminal → reminder fires (use a fresh session id)
echo '{"session_id":"probe","tool_name":"run_in_terminal","tool_input":{"command":"grep -r foo ."},"hook_event_name":"PreToolUse"}' \
  | python3 hooks/palace-reflex.py
```

The first call prints the reminder; the second pair is silent; the broad-probe call prints the reminder.

## Verifying the save hook

```bash
# Unit + integration tests (integration needs the mempalace interpreter):
cd hooks
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" -m unittest test_copilot_transcript -v   # 13 tests, all pass

# Smoke test the adapter with a Copilot Stop payload. A short transcript is below
# Smoke test the adapter with a Copilot Stop payload + a tiny events.jsonl. A short
# transcript is below the 15-message save threshold, so it prints {} (nothing saved yet):
printf '%s\n' '{"type":"user.message","data":{"content":"hello palace"}}' > /tmp/ev.jsonl
echo '{"hook_event_name":"Stop","session_id":"t","transcript_path":"/tmp/ev.jsonl","cwd":"/tmp"}' \
  | python3 copilot_transcript.py   # -> {}
```

Once installed, a real session that crosses 15 human messages prints
`✦ N memories woven into the palace — …` at a turn end (the `Stop` hook), and `PreCompact` forces a save
before compaction. The save is silent (writes a diary entry directly); it never blocks the agent.

## Harness compatibility

The hook protocol matches Claude Code's: stdin JSON with `tool_name` / `tool_input` / `session_id`, stdout
JSON with `hookSpecificOutput.additionalContext` to inject context. Verified with VS Code Copilot Chat and
Copilot CLI. Should work in any harness that consumes the same shape (Claude Code, Cursor).

The **save** hook is Copilot-CLI-specific: it bridges Copilot's `events.jsonl` transcript schema to the
`claude-code` transcript schema mempalace expects (see the `hooks/copilot_transcript.py` bullet above). VS Code
Copilot writes a Claude-compatible transcript, so there the upstream `mempalace hook run --harness claude-code`
config from [discussion #1419](https://github.com/MemPalace/mempalace/discussions/1419) works without the adapter.

## License

Apache 2.0 — see [LICENSE.md](LICENSE.md).
