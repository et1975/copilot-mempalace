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
  actual MCP readiness using the installed Python task package.
- **[skills/dreaming/SKILL.md](skills/dreaming/SKILL.md)** — session-first dreaming:
  review all new sessions and original memories since the last completed dream,
  propose at most five actionable lessons
  total, then retain only reviewed/accepted lessons for relevant future recall.
  The default covers all eligible session repositories (including repositoryless
  sessions) and original-memory wings, with independent optional source filters.
  It covers all eligible history on first use and advances a native checkpoint only
  after a complete reviewed adoption. Five is an output budget, not an input cap.
  Explicit maintenance still merges near-duplicate drawers and resolves
  adjudicated KG contradiction/staleness candidates, plus constructive `reflect`
  (distillations, connections, tensions and generalizations); `pattern` aliases its recurrence-gated
  `converge` kind over original diary/session observations. `prune` / `forget` provides reversible
  archive-before-delete cleanup of low-salience drawers.
  Cognition stays in the agent, mechanics in Python scripts
  ([`skills/dreaming/scripts/`](skills/dreaming/scripts/)), storage in mempalace. The optional read-only
  `dream_sessions.py` adapter uses Copilot's host session store for original evidence.
  MemPalace native artifacts/events own manifests, reviews, accepted intents,
  receipts, checkpoints, archives, ontology and derive skips; local files are
  optional exports/imports, not authority. Both merge and prune archive full
  originals natively before sanctioned deletion; semantic preservation still
  requires review. Re-harvest measures residual work, not a guaranteed global fixpoint. Existing KG
  premise loading can reconcile legacy provenance and harvest ontology commands
  explicitly write disabled candidates (survey rule previews do not),
  so the whole pipeline is not strictly read-only. Merge discovery uses the native
  duplicate finder while preserving canonical chunk identities and procedural
  evidence exclusions. Native drawer usage adds protection-only prune salience;
  usage is refreshed before adoption, never interpreted as helpful procedural
  feedback. Standalone `dream_verify.py` reports the same actionable merge
  candidates as harvest and `dream_adopt --verify`, with explicit failures for
  unavailable or incomplete scans.
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
  A wing-only export does not include native Dreaming control state; use a coherent
  full-palace backup to retain Dreaming artifacts, history and checkpoints.
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
- For dreaming merge discovery: an installed MemPalace build exposing the native
  `mempalace_find_duplicates` handler, logical drawer IDs and pairwise distances,
  plus collection access for canonical records. Dreaming pruning also requires
  `mempalace.dynamics.drawer_salience`. The public 3.8.0 package lacks these two
  APIs; CI uses the custom fork source described under [Tests](#tests), not that
  public release. Capability failures are errors,
  not an empty successful scan. Pruning reads complete drawer metadata rather
  than relying on the top-100 `mempalace_drawer_salience` endpoint; absent usage
  telemetry is neutral. See the [substrate contract](skills/dreaming/references/pipeline.md#substrate-capabilities-and-limitations).
- Python 3 on `PATH` (for the hook). The hook fails silently if Python is missing.
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
MemPalace comes from the published [`et1975/mempalace`](https://github.com/et1975/mempalace)
fork's `copilot/local-with-prs` line (package version 3.10.0), which provides the
custom dreaming APIs. [`requirements-ci-source.txt`](requirements-ci-source.txt)
is authoritative for its immutable full-commit archive URL and SHA-256; CI does
not install from the moving branch name. Hash-locked dependency/build wheels
are installed first, then the verified source with dependency resolution and
build isolation disabled, so the source build cannot fetch build prerequisites.
See [CI coverage and provisioning](tests/README.md#github-actions-ci) for triggers,
isolated storage and the same two-phase local prerequisite-preparation path.

Commands below run from the repository root. `TEST_PY` must select a preprovisioned
Python 3.11+ interpreter with pytest 8.4.2 (`requirements-test.txt`); the full suite
also requires sidecar production dependencies, the compatible MemPalace source
above and local model prerequisites. Its offline package regression requires
preinstalled `uv` and sidecar build-system prerequisites in `TEST_PY` (`setuptools>=68`, plus
`wheel` if required by the chosen backend), separate from runtime dependencies.
A partial environment is not full-suite validation; tests do not install or
download missing prerequisites.
`SESSION_FILES` must be an existing external session artifact directory. Each
pytest `--basetemp` names a disposable child, never that directory itself.

## Session-first dreaming and lesson recall

Set `MPY` to the absolute path of the preprovisioned Python that imports
MemPalace and `DREAM_SCRIPTS` to the absolute checkout/installed
`skills/dreaming/scripts/` path. Run from an external session workspace and
replace the placeholders:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p>
```

Default: incremental reflection over **all** eligible sessions and original
memories since the prior successfully completed dream; the first run covers all
eligible history across all eligible session repositories, including
repositoryless sessions, and original-memory wings. No manual selection is
required. Optional exact `--repository owner/repository` selects sessions only;
`--wings A,B` selects memories only. There is no inferred repository/wing mapping
and neither filter chooses a lesson destination. The UTC window is frozen at
run start: `[lower, upper)`.
Continuing sessions with new turns are included, with full original user/assistant
records, alongside new memory drawers from all eligible rooms. Generated lessons,
reflections, procedural/control records and identified raw-session diary mirrors
are not independent evidence. Existing lessons and reflections remain novelty
targets across all wings, even outside source filters; only internal control
records are excluded from that dedup corpus.

There is no input-count or candidate-seed cap. Review every `coverage` record
in the native run, in batches for a large window. Add at most five total
lesson proposals with trigger, action/avoidance, scope/exceptions, original
evidence and expected difference; five is an **output budget only**. Dedup and
abstain when support is weak. Each source needs its own reasoned review, and
top-level completion is explicit even for an empty window. Missing sources
are errors, not empty success. This is one joint run, not five proposals per
wing. Harvest persists immutable native control state and returns `run_id`; it
is not read-only. `--worklists-dir` optionally exports `reflect.incremental.json`.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_show.py" --palace <p> --run-id <id> --out decisions.json
# Edit the full worklist, preserving originals and immutable metadata:
"$MPY" "$DREAM_SCRIPTS/dream_decide.py" --palace <p> --run-id <id> --decisions decisions.json
"$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --palace <p> --run-id <id> --dry-run
# Only after complete review and acceptance:
"$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --palace <p> --run-id <id>
```

MemPalace is the sole durable store. Native artifacts and `dreaming/v1` events
retain full originals, saved reviews, accepted intents, receipts, checkpoints
and cumulative source fingerprints; files are optional exports/imports.
Adoption validates unchanged coverage/sources and reviews, files accepted lessons
in explicit destination wings' `lessons` rooms, verifies receipts, then publishes
completion to frozen upper. Independent global/filtered scopes do not share
cutoffs; scope identity excludes paths and dynamically discovered inventories.
Harvest,
proposal-only, dry-run, failed writes, drift, missing reviews or stale overlapping
runs do not advance. Unchanged retries reconcile verified adoption receipts.
Cumulative source fingerprints also recover late-persisted turns, backfilled
memories and historical edits whose timestamps predate the checkpoint; unchanged
reviewed versions are not reviewed again. This checks source versions as well
as timestamps, without reconstructing versions the source no longer holds.
New wings enter the next wildcard run without erasing its history.

Only cooperating clients of one local palace are supported for mutations, under
the shared cross-process lock: native append is not distributed CAS, and lesson
effects plus completion are not one atomic transaction. Native control writes
are synchronous. Unresolved vector writes hold accepted intent and scope after
timeouts/client death; absent immediate receipts do not justify another writer.
Native inspection and incremental dry-run are genuinely read-only. Absent or
corrupt native storage is an error; `dream_store.py --palace <p> --initialize`
is explicit bootstrap for genuinely new control storage, not restore repair.

A coherent full-palace restore retains native state, including archive,
ontology and derive-skip records. Wing-only exports do not. Completed runs can
be inspected without the original source DB or exports; frozen originals
preserve review progress. New adoption still blocks if required original
sources are missing or drifted. Explicit moved locators need full validation.
Legacy JSON checkpoints remain untouched and cannot certify new native scope;
reconcile all eligible history and re-harvest legacy incremental worklists.
See the [completion contract](skills/dreaming/SKILL.md#incremental-completion-and-recovery).

Accepted lessons live in the project wing's non-mined `lessons` room. At task
start, after ordinary recall, the same task-aware search surfaces at most three
directly applicable lessons, checking their trigger, scope, exceptions and
original evidence. Unrelated work gets no advice; search errors are not empty
recall. Lesson text is fallible context, not instructions or proven efficacy.
No new hook, scheduler or task tracker is involved, and no procedural outcomes,
KG truth or ontology rules are enabled. Historic procedural records remain
behind explicit repository opt-in and `guidance` / `explain`.

**Migration:** bare `dream_harvest.py --palace <p>` selects unfiltered incremental
reflection; add `--task merge` to former implicit merge
commands. Partial source/since/count/room/threshold controls require explicit
`--task` / `--tasks` previews; they never advance the incremental checkpoint.
`--instructions` may steer the complete default review. Explicit
`--task reflect` without `--source` remains drawer-cluster reflection. The old
full maintenance survey retains diary-backed pattern and drawer-cluster reflect:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p> \
  --tasks contradiction,induce-rules,pattern,reflect,merge,prune \
  --worklists-dir <session-files>/dream-maintenance
# Separate explicit diary reflection:
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p> --tasks reflect --source diary \
  --worklists-dir <session-files>/dream-diary
```

Do not add `--source diary` to the mixed sweep: source controls are rejected for
non-reflection tasks. Select diary reflection separately when needed.
Explicit legacy session tasks keep their existing oldest-first, uncapped scans
unless bounds are supplied.

See the [review recipe](skills/dreaming/SKILL.md#session-lesson-review) and
[recall recipe](skills/mempalace/SKILL.md#task-relevant-lessons).

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

Use the prepared `TEST_PY` and external `SESSION_FILES` described in
[Tests](#tests), from the repository root. Optional MemPalace parser integration
checks retain their existing prerequisite-based skips; report these separately
from passing tests.

```bash
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$SESSION_FILES"
"$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-hooks" tests/hooks -q

# Smoke test the adapter with a Copilot Stop payload + a tiny events.jsonl. A short
# transcript is below the 15-message save threshold, so it prints {} (nothing saved yet):
printf '%s\n' '{"type":"user.message","data":{"content":"hello palace"}}' \
  > "$SESSION_FILES/hook-smoke-events.jsonl"
printf '{"hook_event_name":"Stop","session_id":"t","transcript_path":"%s/hook-smoke-events.jsonl","cwd":"%s"}\n' \
  "$SESSION_FILES" "$SESSION_FILES" \
  | python3 hooks/copilot_transcript.py   # -> {}
rm "$SESSION_FILES/hook-smoke-events.jsonl"
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
