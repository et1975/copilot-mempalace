# AI-assisted memory (MemPalace)

[![CI](https://github.com/et1975/copilot-mempalace/actions/workflows/ci.yml/badge.svg)](https://github.com/et1975/copilot-mempalace/actions/workflows/ci.yml)

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
  durable tasks, dependencies, atomic claims/discoveries, cooperative native
  session ownership and managed-host renewable fenced leases. Schema 2 replays the MemPalace logstream as its sole durable task/recovery
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
  goal; `/fleet execute and track this as a goal` is sufficient opt-in, while
  ordinary fleet prompts remain untracked. An approved native plan is retained as
  an exact artifact plus a searchable memory index, then materialized through
  atomic task/dependency publications rather than one giant prose goal.
  Task and MemPalace storage interaction uses the separate, server-qualified MCP
  tools.
  Cooperative native mode uses `mptask_native`: the issuing session UUID is
  enrolled atomically with a goal, the native parent reserves attempts before
  dispatch and binds actual returned agent IDs. No separate coordinator, actor
  provisioning or supervisor is required. The UUID is an identifier, not a secret;
  existing authenticated MCP remains the trust boundary. Explicit reconciliation
  and session transfer handle interruption without heartbeat timers or retry on
  silence. Acceptance evidence and sealed durable closure finish the goal.
  Rejected open proposals can be cancelled in place without admitting or
  dispatching them; active/uncertain attempts still require reconciliation.
  Content-only native updates clear resolved holds/defer on wanted open work.
  Session takeover is a bounded root-only ownership change; inherited active
  attempts need individual release/reconciliation, never silent adoption.
  Missing native API/version is a blocker, not a reason to use another store.
  This is cooperative workflow support, not physical containment/effect-settlement
  proof or automatic queue subscription. Existing managed goals keep their own
  actor/host/lease/fencing requirements and are never silently converted.
  Ordinary untracked `/fleet` remains unchanged. Linux process supervision is
  optional and separate; macOS/Windows hosting code exists but native
  certification remains unrun. Only the current journal-backed authority is
  supported; there is no original-runtime or old-format compatibility mode.
  Ordinary memory filing does not require this service. See its [recovery
  contract](sidecar/README.md#recovery-and-coherent-palace-backuprestore) before
  changing an existing deployment.
  For installation, use the [task setup quick-start](sidecar/setup.md): one
  Python-native `mempalace-tasks setup configure` / `enable` / `check` workflow
  covers configuration, explicit initialization, Copilot registration and
  actual MCP readiness. No additional .NET runtime is needed.
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
  ([`scripts/palace_backup.py`](skills/mempalace-backup/scripts/palace_backup.py)) that
  needs no `sqlite3` CLI, plus a [restic cheatsheet](skills/mempalace-backup/references/restic-cheatsheet.md).
  Its [backup tests](tests/mempalace-backup/test_palace_backup.py) are repository-only,
  not shipped with the installed skill.
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
- **[hooks/mempalace-save.json](hooks/mempalace-save.json) +
  [hooks/copilot_transcript.py](hooks/copilot_transcript.py) +
  [hooks/copilot_capture.py](hooks/copilot_capture.py)** —
  `Stop` + `PreCompact` + `SessionEnd` **save** hooks (the automated counterpart to the read-first nudge).
  The stdlib adapter validates Copilot's `events.jsonl` and invokes its companion helper with an
  explicitly configured, already-provisioned `MEMPALACE_PYTHON` interpreter. The helper files
  deterministic `copilot-observation-v2` envelopes through MemPalace's installed stdio MCP dispatcher,
  not raw tool handlers. Bounded, lossless parts retain original user/assistant text,
  session/message IDs, roles, timestamps and content hashes.
  It uses `data.content`, not injected `transformedContent`; tool events are not captured.
  This is not the sweep CLI, the upstream hook's bounded diary snippet, or detached mining.
  `Stop` means the main agent finished a **turn**, not that the CLI exited; capture runs after
  15 new user messages since the last confirmed capture. `PreCompact` and `SessionEnd` flush
  pending messages regardless of that interval, including an assistant-only tail.
  Observations go into the source session's derived project wing and `diary` room.
  The installed **hooks** policy must permit the supported MCP route, whose normal writer
  admission still applies. A daemon-selected route is explicitly unsupported; there is no
  silent fallback or daemon auto-start.
  The version-1 registration uses PascalCase event names and snake_case payloads.
  POSIX-only (`python3` `command`); Windows is untested. See
  [save-hook verification](#verifying-the-save-hook) for lifecycle and notification limits.
- **[memories/mempalace-first.md](memories/mempalace-first.md)** — terse auto-loaded reflex stub designed for
  Copilot's user memory (`/memories/`). First 200 lines of user memory are auto-loaded into every conversation,
  so the rule is in context even when the full instructions get pushed out.

## Requirements

- [MemPalace](https://github.com/mempalace/mempalace) installed (`uv tool install mempalace` or `pip install mempalace`).
  Verify with `mempalace status`.
- MemPalace exposed as an MCP server in your harness — see [Step 0](#step-0--register-mempalace-as-an-mcp-server) below.
- Python 3 on `PATH` (for the hook). If Python is missing, the adapter cannot run or report capture status.
- For save hooks, `MEMPALACE_PYTHON` must name the absolute path of an executable Python
  interpreter with the compatible MemPalace APIs and model prerequisites already provisioned.
  Configure it persistently in each installed save-hook entry as shown below. Neither the
  adapter nor helper downloads/installs dependencies or guesses an interpreter from a CLI launcher.
- For the backup/restore skills only: [`restic`](https://restic.net/) on `PATH`
  (`zypper in restic`, `apt install restic`, `brew install restic`, …).
- For the optional Tasks sidecar: Python **3.11+**, MCP SDK **1.30.0**, and
  Windows-only **`pywin32==311`** in its hash-pinned universal requirements lock.
  Actual validation ran on Linux/Python 3.12, not native macOS/Windows.
  [Offline preparation](sidecar/README.md#requirements-and-offline-setup) uses
  preprovisioned artifacts; service startup never builds/downloads packages.

## Tests

All tests and test-only helpers live under the repository-root [`tests/`](tests/README.md),
separate from deployed hooks, skills and sidecar packages. Pytest is the canonical
runner; root configuration supplies import paths without `PYTHONPATH` or changing
into production directories. See the [test guide](tests/README.md) for setup,
suite selectors, existing integration gates and package-content checks.

[GitHub Actions CI](.github/workflows/ci.yml) runs the full suite serially on
Ubuntu 24.04 / Python 3.12, including the offline wheel/sdist roundtrip.
See [CI coverage and provisioning](tests/README.md#github-actions-ci) for triggers,
isolated storage and the separate prerequisite-preparation step.

Commands below run from the repository root. `TEST_PY` must select a preprovisioned
Python 3.11+ interpreter with pytest 8.4.2 (`requirements-test.txt`); the full suite
also requires sidecar production dependencies and the existing MemPalace/local
model prerequisites. Its offline package regression requires preinstalled `uv`
and sidecar build-system prerequisites in `TEST_PY` (`setuptools>=68`, plus
`wheel` if required by the chosen backend), separate from runtime dependencies.
A partial environment is not full-suite validation; tests do not install or
download missing prerequisites.
`SESSION_FILES` must be an existing external session artifact directory. Each
pytest `--basetemp` names a disposable child, never that directory itself.

## Opt-in procedural rollout

Ordinary recall remains the baseline. First run the deterministic procedural
tests on throwaway storage, using `TEST_PY` with the existing MemPalace/model
prerequisites:

```bash
export PYTHONDONTWRITEBYTECODE=1
DREAMING_TEST_TMPDIR="$SESSION_FILES" TMPDIR="$SESSION_FILES" \
  "$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-procedural" \
  tests/dreaming/test_dream_procedure.py \
  tests/dreaming/test_procedural_replay.py -q
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

### Choose the installation scope

**Memory-only:** follow the MCP and customization-pack steps below.
**Task tracking:** also follow the [task setup quick-start](sidecar/setup.md).
Installing an executable or copying a skill does not configure a task authority,
register its tools, or prove that a native worker supervisor exists. A task
installation is ready only after the setup check confirms the configured
authority and its advertised MCP tools; an already-open session must load the
new registration separately.

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

# 1.3 Hook (the JSON command expects this ~/.copilot/hooks/ script path)
mkdir -p ~/.copilot/hooks
ln -s "$(pwd)/hooks/palace-reflex.json" ~/.copilot/hooks/palace-reflex.json
ln -s "$(pwd)/hooks/palace-reflex.py"   ~/.copilot/hooks/palace-reflex.py

# 1.4 Save hooks: turn-stop, pre-compaction, and final session end.
#     Install all three files together; configure MEMPALACE_PYTHON below.
COPILOT_HOOKS_DIR="${COPILOT_HOME:-$HOME/.copilot}/hooks"
mkdir -p "$COPILOT_HOOKS_DIR"
cp hooks/mempalace-save.json hooks/copilot_transcript.py hooks/copilot_capture.py \
  "$COPILOT_HOOKS_DIR/"
```

The read-first audit hook JSON references `python3 ~/.copilot/hooks/palace-reflex.py`. If you'd rather keep
that script outside `~/.copilot/hooks/`, edit its `command` field accordingly.
The save-hook commands instead use the quoted
`"${COPILOT_HOME:-$HOME/.copilot}/hooks/copilot_transcript.py"` path, so they follow
the CLI's configured home without interpreting spaces or shell metacharacters in it.
The other customization examples above retain their default `~/.copilot/` locations.
Keep the filenames `copilot_transcript.py` and `copilot_capture.py`, with both
Python files in the same directory. Installing only the JSON or adapter is incomplete.
The save-hook example uses copies: the helper's safe-path checks reject symlinked
helper paths, and the installed JSON needs a local interpreter setting. For upgrades,
preserve local customizations and replace old save-hook symlinks before copying;
do not copy through links into the checkout.
Update all three files together;
do not add a second copy of the registrations in another loaded hooks directory.
Copilot combines registrations from multiple sources, so duplicate entries can invoke the adapter twice.

In the **installed copy** of `mempalace-save.json`, add this field to each existing
command object under `Stop`, `PreCompact` and `SessionEnd`, replacing the placeholder
with your already-provisioned interpreter's absolute path:

```json
"env": {
  "MEMPALACE_PYTHON": "/absolute/path/to/preprovisioned/bin/python"
}
```

This per-hook setting persists across fresh CLI launches; keep it when updating
the installed JSON. Do not add it at the JSON root or create duplicate event entries.
The shipped commands also inherit `MEMPALACE_PYTHON` from the CLI's environment,
but a one-time shell export does not configure future CLI launches from other shells.
The generic repository JSON deliberately contains no machine-specific interpreter.
The helper is executed directly with this interpreter, without shell evaluation,
launcher-shebang inference, package installation or daemon auto-start.
`MEMPALACE_BIN` is not used by this save path.

For the optional [per-goal task workflow](sidecar/README.md#per-goal-native-fleet-workflow),
copy or link `skills/mempalace-tasks/` into `~/.copilot/skills/` and
`agents/palace-task-workflow.agent.md` into `~/.copilot/agents/` using the same
customization pattern. These are prompt guidance, not a supervisor or task-service
installation; [Tasks registration](#optional-tasks-registration) and each goal's
tracking opt-in are separate.

Keep this checkout's skill, reference files and agent as the canonical sources.
When using copies, update the installed skill directory and agent together and
compare them with the checkout before declaring deployment complete; editing only
`~/.copilot/` does not deliver a repository change. Preserve local customizations
before replacing copies. Refresh the MemPalace section of merged instructions
without overwriting instructions contributed by other packs.

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

Use the prepared `TEST_PY` and external `SESSION_FILES` described in
[Tests](#tests), from the repository root. Optional MemPalace parser integration
checks retain their existing prerequisite-based skips; report these separately
from passing tests.

```bash
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$SESSION_FILES"
export DREAMING_TEST_TMPDIR="$SESSION_FILES"
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-hooks" tests/hooks -q

# After installation, verify both Python files match this checkout.
COPILOT_HOOKS_DIR="${COPILOT_HOME:-$HOME/.copilot}/hooks"
cmp hooks/copilot_transcript.py "$COPILOT_HOOKS_DIR/copilot_transcript.py"
cmp hooks/copilot_capture.py "$COPILOT_HOOKS_DIR/copilot_capture.py"
# Expected local JSON differences: the per-hook MEMPALACE_PYTHON env fields only.
diff -u hooks/mempalace-save.json "$COPILOT_HOOKS_DIR/mempalace-save.json"
```

The JSON comparison normally exits 1 for those intentional interpreter additions;
review any other difference rather than overwriting local settings blindly.
The config regression covers version 1, all three event registrations, and command
resolution against an isolated fixture adapter under default, empty and custom
`COPILOT_HOME` values. It does not invoke live memory writes or prove that an
already-running CLI has loaded the registration. Start a fresh disposable CLI
session after installation when checking actual lifecycle dispatch.
Isolated SQLiteExact integration exercises the actual fresh-process installed
MCP dispatcher and writer admission, substituting only deterministic embeddings.
This validates that runtime boundary, not production-model behavior or observation
of a live host's exit callback.

The [official hook contract](https://docs.github.com/en/copilot/reference/hooks-reference)
distinguishes these events:

| Registration | Capture opportunity | What it does not prove |
| --- | --- | --- |
| `Stop` | Main-agent turn completion; capture after 15 unsaved user messages | CLI exit or capture of a short session's final tail |
| `PreCompact` | Flush pending messages before context compaction, including assistant-only tails | A session-end callback |
| `SessionEnd` | Flush pending messages at session termination, including graceful CLI exit | Delivery after an abrupt kill, crash or power loss |

`SessionEnd` supplies `hook_event_name`, `session_id`, ISO `timestamp`, `cwd`
and `reason`; the documented payload does **not** include `transcript_path`.
Register the PascalCase event to retain the adapter's snake_case input contract.
The adapter resolves the transcript as
`$COPILOT_HOME/session-state/<session_id>/events.jsonl` (default home: `~/.copilot`).
Legacy payloads with an explicit `transcript_path` remain supported. Session identity,
message IDs, timezone-aware timestamps and safe paths are validated before capture; an explicit
path does not bypass those checks.
The authoritative project comes from `session.start.data.context.cwd`, not the
helper's working directory. A supplied callback `cwd` must agree after path
normalization; conflicting context fails instead of redirecting capture.
Only legacy sessions that entirely omit the `context` member may use an explicit
absolute callback `cwd`. Malformed modern context does not take that fallback.
The chosen project destination is pinned in session state across captures and
resumes; a changed destination is refused.
Final callbacks are best-effort; keeping incremental `Stop` and `PreCompact`
capture matters because abrupt termination cannot guarantee exit callbacks.

Observation envelopes use schema `copilot-observation-v2`. A message is split
deterministically into content parts of at most 10,000 characters, retaining its
session/event identity, role, timestamp, zero-based `part_index`, `part_count` and
the complete original text's `content_sha256`. To verify a message, gather every
part for that original event, order by index, concatenate the part content and
check the SHA-256 of its UTF-8 text. Parts are lossless storage units, not separate
independent observations or summaries.

The adapter writes `{}` to stdout and returns exit 0 for handled failures as well
as successful captures: it is not an agent-blocking policy gate. A content-free
JSON diagnostic on stderr reports `saved`, `skipped`, `failed` or `unknown`.
The per-session durable result, when available, is recorded in
`$COPILOT_HOME/session-state/<session_id>/mempalace-save/status.json`
(under `~/.copilot` by default); input, filesystem or locking failures may only
report stderr.
Exit 0 or `{}` alone is **not** evidence that capture succeeded.

`saved` means the adapter accepted a complete helper receipt for the pending
batch and advanced its confirmed transcript prefix. Status format **version 3**
retains the prefix digest, pinned project and bounded receipt metadata: batch
digest, first/last drawer IDs, wing, room, route `mcp` and part count. A multipart
message produces more parts than original messages; those counts are not interchangeable.
This is separate from the hook
registration's version 1. Incompatible saved state is refused, not silently reset.
A repeated unchanged callback skips already captured messages.
Timeout, a nonzero helper exit or an incomplete/unrecognized receipt can
leave an **unknown** write outcome. The adapter retains the pending intent in
`status.json` and the snapshot in the same directory's `pending.jsonl`, and blocks
automatic retry until explicit reconciliation. Do not delete these files to force
a retry: the earlier attempt may already have written some or all messages.
The snapshot contains original message text and can remain after confirmed capture;
protect it like the source transcript. Its existence alone does not mean an attempt
is unsettled: inspect the status's pending intent and outcome.
Recovery needs independent evidence of what actually committed before uncertain
state can be reconciled. This pack does not automatically settle unknown writes
or migrate older status formats.

The helper honors MemPalace's auto-save setting and resolves the **hooks**, not CLI,
write-routing policy. If policy requires a missing daemon, it reports
`daemon_required_unavailable`. If policy selects a running daemon, it reports
`daemon_capture_unavailable`: that installed runtime's daemon tool path lacks the
required writer admission. Neither case starts a daemon or falls back silently.

For a policy-permitted non-daemon route, the helper uses the installed stdio MCP dispatcher,
with its existing hub forwarding or guarded local writer admission. It does not
invoke a raw add-drawer handler or replace a collection writer. Read-only policy
and peer writer locks remain effective: an active non-hub writer can prevent
capture, yielding `writer_unavailable`; read-only rejection before any write yields
`capture_read_only`. Do not delete locks or bypass the dispatcher to force a save.
Resolve writer ownership and deployment policy explicitly; having a working
interactive MCP connection does not prove a fresh hook process can acquire a writer.

Missing interpreter/helper/APIs or palace are reported rather than installed or
initialized. A confirmed no-write failure or disabled auto-save can clear pending
intent without claiming a save; after the prerequisite is corrected, a future
callback can try again. There is no automatic retry scheduler, and an unknown
partial attempt remains held even after its original error disappears.
This path does not call the upstream
`mempalace hook run` silent-save/toast path. There is no promised desktop toast or
`systemMessage`. The CLI can display progress messages while a command hook runs,
but `SessionEnd` output is not processed into the model. Do not use visible exit
text or an injected follow-up message as the success criterion.
For end-to-end verification, use an isolated test palace and inspect persisted
observations and capture status after a turn, compaction and graceful exit rather
than sending synthetic save events to your live palace. Verify the recorded
project wing, `diary` room and drawer receipt against the original observation
envelopes; hook settings and registration alone do not prove persistence.

Captured transcripts are source evidence, not automatic reflection, accepted
lessons, procedural outcomes or causal credit for retrieved advice. The adapter
does not start feedback or dreaming. Review and any separately opted-in learning
workflow remain explicit.

## Harness compatibility

The **read-first audit** hook protocol matches Claude Code's: stdin JSON with `tool_name` / `tool_input` / `session_id`, stdout
JSON with `hookSpecificOutput.additionalContext` to inject context. Verified with VS Code Copilot Chat and
Copilot CLI. Should work in any harness that consumes the same shape (Claude Code, Cursor).
This context-injection behavior does not apply to the `SessionEnd` save hook.

The **save** hook is Copilot-CLI-specific: it validates Copilot session state and
uses the companion helper to file original observations through MemPalace's
installed stdio MCP dispatcher and writer-admission path. The upstream
`mempalace hook run --harness claude-code` configuration
for other harnesses is a different capture route; its diary/mining behavior is not
the full-message receipt contract described here.

## License

Apache 2.0 — see [LICENSE.md](LICENSE.md).
