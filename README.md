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
- **[MemPalace Tasks sidecar](sidecar/README.md)** — optional, separately installed
  Python Streamable HTTP MCP service with a harness-launched stdio frontend for
  durable tasks, dependencies, atomic claims/discoveries and renewable fenced
  leases. Schema 2 replays the MemPalace logstream as its sole durable task/recovery
  store, with a fresh fenced epoch
  per owner startup and disposable local runtime/discovery files. Foreground
  hosting requires no service manager. `mempalace-tasks mcp --config CONFIG`
  uses the schema-2 lifecycle policy: launcher mode opts into autostart/reuse of
  one shared HTTP owner; external mode only connects. Closing a frontend leaves
  that owner running.
  Includes authenticated `connect`, read-only `status` / `list` / `show` /
  `history` / bounded `watch`, a
  [task-safety skill](skills/mempalace-tasks/SKILL.md), and an opt-in
  [workflow agent](agents/palace-task-workflow.agent.md). The
  [per-goal native fleet workflow](sidecar/README.md#per-goal-native-fleet-workflow)
  keeps native `/fleet` as orchestrator and requires explicit tracking for each
  goal; selecting the agent or installing this pack does not enroll ordinary work.
  Without a separately supplied compatible native supervisor, it supports
  durable planning and execution-blocker reporting, not tracked execution.
  Worker dispatch remains a host responsibility; connecting MCP does not
  automatically wire native `/fleet`.
  No native `/fleet` execution adapter is shipped. Linux process supervision is
  optional and separate; macOS/Windows hosting code exists but native
  certification remains unrun. Only the current journal-backed authority is
  supported; there is no original-runtime or old-format compatibility mode.
  Ordinary memory filing does not require this service. See its [recovery
  contract](sidecar/README.md#recovery-and-coherent-palace-backuprestore) before
  changing an existing deployment.
- **[skills/dreaming/SKILL.md](skills/dreaming/SKILL.md)** — offline consolidation ("dreaming"): a 5-phase
  pipeline (harvest → adjudicate → review → adopt → verify) that merges near-duplicate drawers and resolves
  adjudicated KG contradiction/staleness candidates between sessions, plus constructive `reflect`
  (distillations, connections, tensions and generalizations); `pattern` aliases its recurrence-gated
  `converge` kind over original diary/session observations. `prune` / `forget` provides reversible
  archive-before-delete cleanup of low-salience drawers.
  Cognition stays in the agent, mechanics in Python scripts
  ([`skills/dreaming/scripts/`](skills/dreaming/scripts/)), storage in mempalace. The optional read-only
  `dream_sessions.py` adapter uses Copilot's host session store as a richer pattern substrate.
  Both merge and prune archive full originals before sanctioned deletion; semantic preservation still
  requires review. Re-harvest measures residual work, not a guaranteed global fixpoint. Existing KG
  premise loading can reconcile legacy provenance and ontology candidate commands explicitly write,
  so the whole pipeline is not strictly read-only. Native drawer usage-frequency
  is proposed upstream as MemPalace/mempalace#1921.
  See [`skills/dreaming/references/pipeline.md`](skills/dreaming/references/pipeline.md) for the contract.
- **[Drawer-backed procedural learning](skills/dreaming/references/procedural.md)** — an optional
  `dream_procedure.py` CLI (`propose`, `validate`, `review`, `outcome`, `guidance`, `explain`).
  Immutable event drawers retain original evidence, explicit reviewed outcomes and full lineage;
  deterministic decay/maturity is observed usefulness, never KG authority or logical truth.
  Guidance is exact-repository, read-only and bounded to five combined items / 6,000 serialized
  characters. No automatic enrollment, inferred feedback, textual inversion, new database or model
  download. Supported boundary: existing SQLite-exact palace, installed local MiniLM; WAL-aware
  read-only access works with an open writer. Other backends/loaders are refused, not converted.
- **[skills/contemplate/SKILL.md](skills/contemplate/SKILL.md)** — on-demand deductive reasoning over the
  MemPalace KG: run `derive` inline when the user asks what follows, then adjudicate
  `materialize` / `skip` / `reject_rule` candidates. It shares the same Python mechanics in
  [`skills/dreaming/scripts/`](skills/dreaming/scripts/) and documents the derive contract in
  [`skills/contemplate/references/derive.md`](skills/contemplate/references/derive.md).
- **[skills/mempalace-backup/SKILL.md](skills/mempalace-backup/SKILL.md)** — safely back up the local palace with
  [`restic`](https://restic.net/): quiesce writers, capture a coherent physical
  palace cut including the resolved data directory and committed SQLite/WAL state,
  preserve palace provenance (including `palace/.mempalace/origin.json`), verify,
  and prune. The current helper refuses data roots outside the selected HOME
  backup root rather than omitting them. Local repos
  only, on demand. Ships a tested Python helper
  ([`scripts/palace_backup.py`](skills/mempalace-backup/scripts/palace_backup.py) + `test_palace_backup.py`) that
  needs no `sqlite3` CLI, plus a [restic cheatsheet](skills/mempalace-backup/references/restic-cheatsheet.md).
  For **per-wing** archival/migration/cloning (which restic cannot do, since wings share physical storage), it also
  ships [`scripts/palace_wing.py`](skills/mempalace-backup/scripts/palace_wing.py) — a logical wing export/import that
  reads the palace SQLite directly into a portable JSONL bundle and replays it back.
- **[skills/mempalace-restore/SKILL.md](skills/mempalace-restore/SKILL.md)** — restore / disaster-recovery
  counterpart: offline, reversible private-staging restore with explicit
  validation before publication/reconnection.
  For journal-mode tasks, offline exclusion and a staged epoch barrier precede
  publication and fresh owner activation. Preserve `logstream.sqlite3`, required
  artifacts and `replica.json`; no matching sidecar pending/head/clock backup set
  is required. Restore does not undo external effects. The runbook owns the
  exact helper flags and current platform/refusal limits.
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
- For the optional Tasks sidecar: Python **3.11+**, MCP SDK **1.30.0**, and
  Windows-only **`pywin32==311`** in its hash-pinned universal requirements lock.
  Actual validation ran on Linux/Python 3.12, not native macOS/Windows.
  [Offline preparation](sidecar/README.md#requirements-and-offline-setup) uses
  preprovisioned artifacts; service startup never builds/downloads packages.

## Opt-in procedural rollout

Ordinary recall remains the baseline. First run the deterministic procedural
tests on throwaway storage, using the already installed MemPalace interpreter:

```bash
cd skills/dreaming/scripts
PYTHONDONTWRITEBYTECODE=1 DREAMING_TEST_TMPDIR="$SESSION_FILES" TMPDIR="$SESSION_FILES" \
  "$MPY" -m unittest test_dream_procedure test_procedural_replay -q
```

After separate user approval, enroll a small set of repository-specific rules
with three distinct original supporting sessions. Retrieve a bounded support/
contrast packet, explicitly review each item, then try approved candidates only
with `guidance --include-candidates` on safely bounded work. Record specific
attributed outcomes, not task-success or retrieval counts. Default guidance
waits for eligible established/proven maturity; use `explain` for abstention.
The [reference](skills/dreaming/references/procedural.md) includes executable
commands, exact artifact schemas and `--prepare` digest handling.

Chronological synthetic replay checks no lookahead, unsafe delivery, feedback
inflation or output-budget violations. It reports coverage/abstention against
an unscored evidence-only baseline, **not production superiority or CASS parity**.
Stop using procedural commands to roll back behavior; retain events and protected
source drawers, including retired rules and counterexamples. External deletion
is not prevented and no cross-drawer transaction/global snapshot is claimed.

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

#### Optional Tasks registration

The task service is a separate, opt-in registration named `mempalace-tasks`.
First provision its [installed package/environment](sidecar/README.md#requirements-and-offline-setup),
schema-2 configuration, accepted genesis and private credentials. Register
`mempalace-tasks mcp --config /absolute/path/tasks.json` as the long-lived stdio
command; see the [generic configuration and verified Copilot CLI registration
example](sidecar/README.md#registration-examples). Copying this pack does not
install it, initialize task state or register a server.

The config's `launcher` lifecycle permits autostart/reuse; default `external`
requires an already running owner. [Direct HTTP registration](sidecar/README.md#direct-streamable-http-alternative)
remains available. Neither transport automatically wires native `/fleet`
execution, and no service manager is required.

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

For the optional [per-goal task workflow](sidecar/README.md#per-goal-native-fleet-workflow),
copy or link `skills/mempalace-tasks/` into `~/.copilot/skills/` and
`agents/palace-task-workflow.agent.md` into `~/.copilot/agents/` using the same
customization pattern. These are prompt guidance, not a supervisor or task-service
installation; [Tasks registration](#optional-tasks-registration) and each goal's
tracking opt-in are separate.

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
