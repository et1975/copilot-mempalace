# MemPalace Tasks

A single-host task authority over an existing MemPalace HTTP hub. The package
provides authenticated Streamable HTTP MCP, read-only human inspection, and
explicit foreground or opt-in launcher hosting. Schema 2 uses MemPalace as the
sole durable task/recovery store. No systemd, launchd or Windows service is
required; Linux worker supervision is a separate, optional integration.
It does not replace MemPalace, install another task database, or ship a native
Copilot `/fleet` execution adapter.

```text
Copilot / other MCP clients ----\
                                > task sidecar --> MemPalace logstream
Human status/history/watch ----/

External host/coordinator --> claims, optional supervision and result delivery
```

## What is authoritative

The reserved `mptask/<authority_id>` logstream contains commands, settlement
records and accepted startup epochs. `TaskAuthority`
reconstructs tasks, dependencies, generations, resource reservations and scoped
command outcomes from that journal.

There is one authority implementation and one supported configuration contract:
schema 2 with `runtime_dir`, journal-only recovery, and `lifecycle` set to
`external` (default) or `launcher`. A fixed loopback port or `0` for dynamic
binding is supported. There is no recovery-mode selector or file-backed
authority alternative.

Runtime locks, registry and launch logs are disposable
coordination, not a matching task-recovery backup set. Config and credentials
are reprovisionable local inputs, not versioned task state.

Every owner startup accepts a fresh epoch before readiness,
including exclusive administration, not just machine reboots. It fences old
attempt authorization immediately and records inherited attempts as requiring
recovery in bounded batches. Execution is not automatically resumed. Effective
time is rebuilt from accepted journal timestamps and a process-local,
suspend-inclusive clock, never a restored local clock file.

Claims have renewable leases and monotonically increasing generations. A claim
is initially **preparing**, not permission to execute. A registered supervisor
provides preparation evidence; mutations from stale generations are rejected.
Default policy is a 300-second lease, renewal every 100 seconds, 15-second
maintenance sweep, 30-minute no-progress cap, two-hour attempt cap and three
automatic retries after the initial attempt.

New prerequisites use atomic graph publication with source disposition `yield`.
Goal bootstrap creates the root and planning task together; goal closure checks
unfinished/proposed work and seals intake. An empty ready list is not completion.

This is a **cooperative, single-host** deployment. All clients use one configured
hub, one authority and one canonical runtime directory.
Never move/delete that directory or its stable lock files while an owner can
still run; using another directory bypasses local exclusion. The lock is not a
distributed fence or hub-enforced compare-and-swap. The private bearer token
identifies trusted local clients as a group; its holder can assert registered
actor names. This is not multi-tenant per-actor authentication.

## Requirements and offline setup

- Python **3.11+**. Validation to date ran on **Linux/Python 3.12**.
- Native Linux, macOS and Windows hosting code paths exist; **macOS/Windows
  native certification remains unrun**. A universal lock is not platform test
  evidence. Managed-process containment/supervision currently requires Linux;
  ordinary HTTP hosting does not initialize that execution backend.
- An already installed MemPalace hub with the supported append/list contract.
- Official Python MCP SDK **1.30.0**, pinned with its dependencies in
  `requirements.lock`; the direct Windows-only dependency is **`pywin32==311`**
  (`sys_platform == 'win32'`).
- Git only for execution profiles that create isolated repository worktrees.

From the repository root, with preinstalled Python/uv and the required artifacts
already in the local cache:

```bash
uv venv --offline --python 3.12 sidecar/.venv
uv pip sync --offline --require-hashes --python sidecar/.venv/bin/python sidecar/requirements.lock
uv pip install --offline --no-deps --python sidecar/.venv/bin/python -e ./sidecar
sidecar/.venv/bin/mempalace-tasks --help
```

These are offline environment-preparation commands, not service-startup steps.
If a cached dependency/build tool or interpreter is missing, provision it from
an approved source before retrying. Service startup neither downloads nor builds
packages; there is no install/network-bootstrap fallback. Windows environments
use their native executable paths rather than the POSIX `.venv/bin` examples.

The hash-pinned universal requirements lock was regenerated using approved PyPI
metadata/wheel inspection, including the conditional pywin32 dependency. Its
recorded generation command can run offline with a preprovisioned cache:

```bash
uv pip compile sidecar/pyproject.toml --offline --only-binary=:all: --no-python-downloads --no-sources --universal --python-version 3.11 --generate-hashes --output-file sidecar/requirements.lock
```

Copying the customization pack alone does **not** install or start this package.
Use a prebuilt, approved package/environment for deployment; do not launch it
through a command that fetches packages on every MCP connection.

## Configure and initialize

Create a private credential/config directory using your normal administration
tools. Credential and runtime parents must already exist. For a **new authority**,
choose a new UUID and a dedicated runtime directory; do not reuse a repository
or another application's directory. Reopening a current-format authority retains
its UUID and accepted genesis; it does not initialize replacement task history.
Older experimental configurations and journal formats are not supported or
automatically migrated.

The configuration below is synthetic, not a deployment. Replace the UUID and
`/srv/...` paths with absolute native paths without symlinks/reparse points.
Obtain the existing hub credential through its normal administration flow;
never paste a token into a task, repository, chat or log.

```json
{
  "schema_version": 2,
  "authority_id": "11111111-1111-1111-1111-111111111111",
  "hub_url": "http://127.0.0.1:8765/mcp",
  "hub_token_file": "/srv/mptask-private/hub.token",
  "service_token_file": "/srv/mptask-private/service.token",
  "runtime_dir": "/srv/mptask-runtime",
  "lifecycle": "external",
  "host": "127.0.0.1",
  "port": 0,
  "genesis": {
    "actor": "operator",
    "actors": {
      "operator": "operator",
      "coordinator": "coordinator",
      "worker": "worker",
      "supervisor": "supervisor",
      "system": "system"
    },
    "execution_profiles": {
      "local": {"execution_class": "isolated"}
    },
    "supervisors": {
      "supervisor": {
        "profiles": ["local"],
        "workers": ["worker"]
      }
    }
  },
  "maintenance_actor": "system",
  "recovery_actor": "operator"
}
```

Omit `hub_token_file` only when the explicitly configured hub does not require
one. Token files must be private, singly linked regular files validated by the
native platform layer (owner-only permissions on POSIX, private ACLs on Windows).
Configuration/token reads never repair permissions, create parents, or chmod a
shared directory. The bind host must be canonical numeric loopback, such as
`127.0.0.1` or `::1`. Schema 2 accepts `port: 0` or a fixed port in `1..65535`.

Only explicit `mempalace-tasks init --config CONFIG` may create new genesis.
Add `--generate-token` to exclusively create a missing private service token;
it never displays or overwrites the token or replaces accepted configuration.
Initialization is idempotent for the same normalized genesis, but opens an owner
and is **not** read-only inspection or the restore procedure. Actors, supervisors
and declared capabilities are fixed by that genesis; changing JSON later does
not change accepted authorization.

### Foreground and launcher lifecycle

The following is command syntax, not an automatic deployment sequence.
`CONFIG` is an absolute configuration path; alternatively set `MPTASK_CONFIG`.

| Command | Behavior |
|---|---|
| `mempalace-tasks serve --config CONFIG` | Standard foreground owner; requires accepted genesis. Works with `lifecycle: "external"` without a service manager. |
| `mempalace-tasks start --config CONFIG --timeout 10s` | Explicit start/reuse; requires schema 2 and `lifecycle: "launcher"`. No implicit init. |
| `mempalace-tasks connect --config CONFIG --timeout 10s` | Read-only discovery of an authenticated, ready owner; never starts one. |
| `mempalace-tasks connect --config CONFIG --start --timeout 10s` | Explicit launcher start/reuse, under the same configured consent as `start`. |
| `mempalace-tasks stop --config CONFIG --instance-id UUID --timeout 10s` | Drain exactly the previously observed instance, then confirm listener closure and ownership release. |

For `start`, `connect` and `stop`, timeout defaults to ten seconds, accepts a positive
`s`/`m`/`h` duration, and is bounded to 300 seconds. Stop requires the actual
`instance_id` from a prior connection, not the stable authority UUID. Blocked
drain, lost replies or unconfirmed release are explicit failures/unknown outcomes,
not permission to kill a registry PID or automatically retry the stop request.

Foreground and launcher startup share local election and lifetime ownership.
The launcher uses the preinstalled interpreter and a private-stdin startup ticket
internally; the ticket is not a user-facing credential or a public launch API.
It never picks another hub, authority or recovery path. Optional service-manager
wrappers can run the same foreground command, but no service, hub or MCP
registration is installed or changed automatically.

## Connect MCP clients

Keep the existing MemPalace MCP registration for ordinary memory operations.
Use a **separate Streamable HTTP** server named `mempalace-tasks`. In schema 2,
`connect` validates the private disposable registry's configuration binding,
then challenges the listener for an authenticated identity/readiness proof.
The accepted epoch is its `instance_id`; a registry pathname or PID alone is
not ownership or readiness. Port zero resolves to the actual bound endpoint.

CLI `connect` returns only `url`, `authority_id`, `instance_id` and `ready`, never
a token. Programmatic `discovery.connect(config)` returns an immutable
`ConnectionInfo` with an instance-scoped bearer for
`TaskServiceClient(info.url, token=info.token)`. It neither starts a writer nor
injects/updates mutation epochs. Reconnect explicitly after an owner change;
do not apply the new epoch to an unresolved old request.

For harness HTTP registration, supply the endpoint and bearer through the
harness's supported credential mechanism. MCP accepts the provisioned service
bearer or an instance-scoped bearer; lifecycle stop requires the latter.
A fixed port permits a fixed URL such as `http://127.0.0.1:8766/mcp`;
dynamic-port registrations must follow newly verified discovery after restart.
There is no automatic harness-registration/credential-refresh adapter.
Never put credentials in tasks, repository files, logs or shell history.
Use numeric loopback and the exact `/mcp` path; redirects, DNS hostnames and
public binding are not supported by this service/client profile.

All sessions connect to the **same running service**, not a fresh stateful server
process per agent. Host/Origin checks and token checks protect the endpoint; they
do not make an untrusted shared host or a second independent authority safe.

Install/link the optional [task-safety skill](../skills/mempalace-tasks/SKILL.md)
and [workflow agent](../agents/palace-task-workflow.agent.md) using the pack's
normal customization mechanism. Their MCP selectors assume server aliases
`mempalace-tasks` and `mempalace`; adjust aliases consistently if your harness
uses different names.

### Tool surface

Discover actual `tools/list` schemas instead of copying argument guesses.

| Group | Tools |
|---|---|
| Lifecycle | `mptask_create`, `update`, `claim`, `renew`, `checkpoint`, `attempt_report`, `release`, `recover`, `transition`, `note` |
| Graph and goals | `mptask_add_dependency`, `remove_dependency`, `bootstrap`, `expand`, `goal_close` |
| Read/observe | `mptask_get`, `snapshot`, `list`, `ready`, `history`, `health`, `wait_ready` |
| Historical resolution | `mptask_outcome` |

Every short name in the table has the `mptask_` prefix. Public mutations require
`actor`, `command_id`, and **`expected_epoch`**
from the current read/health response's `epoch_id`. Clients do not pass
`operation`. Missing or stale epochs are rejected before mutation, even for a
known command ID. Internal genesis and
expiry commands are not exposed as public MCP tools.

Freeze **authority + epoch + command ID + exact payload** for a logical request.
Its journal identity is `(authority_id, epoch_id, command_id)`. Do not refresh
`expected_epoch` on retry, silently resubmit under a new epoch, or infer failure
from a timeout. The client performs no automatic mutation retries.

Resolve historical requests with
`mptask_outcome(epoch_id=ORIGINAL_EPOCH, command_id=ORIGINAL_ID)`.
The original epoch UUID is required; a null or newly substituted epoch is invalid.
The lookup is read-only and can return `resolution="not_recorded"` with no receipt;
absence is not confirmed abandonment or evidence of absent external effects.
Only a confirmed terminal-abandoned outcome permits a new ID for a still-needed
request after fresh state/authorization checks. An old receipt is historical,
not current execution permission. Use current `mptask_get.authorization` and
match the host-supplied actor/owner, attempt and generation as well as the epoch.

## Human supervision

```bash
sidecar/.venv/bin/mempalace-tasks connect --config /srv/mptask-private/config.json --timeout 10s
sidecar/.venv/bin/mempalace-tasks status --config /srv/mptask-private/config.json --project demo
sidecar/.venv/bin/mempalace-tasks list --config /srv/mptask-private/config.json --needs-attention
sidecar/.venv/bin/mempalace-tasks show TASK_ID --config /srv/mptask-private/config.json
sidecar/.venv/bin/mempalace-tasks history TASK_ID --config /srv/mptask-private/config.json --json
sidecar/.venv/bin/mempalace-tasks watch --config /srv/mptask-private/config.json --project demo --refresh 5s --duration 10m
sidecar/.venv/bin/mempalace-tasks inspect --config /srv/mptask-private/config.json
```

These commands discover/query the running service. They never claim, renew,
expire, reconcile, start a writer, or modify user config/token permissions.
`inspect` reports authority and runtime-lane health; task views show scope/counts,
owner, generation, deadlines/progress, blockers, resources, retry/recovery and evidence.

Text distinguishes **CURRENT**, **STALE**, **OUTCOME UNKNOWN** and pinned
**HISTORICAL SNAPSHOT**. JSON preserves these distinctions and source metadata.
Stored `in_progress` with an expired lease is not falsely displayed as an already
committed requeue. Counts cover the selected scope, not just the page.

`--project`, `--goal`, `--status`, `--assignee`, `--kind`, attention filters,
`--limit` and `--cursor` support focused observation. `--json` produces one JSON
object for a query and one object per watch frame. Pinned pages have a fixed
snapshot and are historical, not fresh execution authorization.

Watch defaults to five-second refresh and a ten-minute maximum duration. It has
one read in flight, skips missed ticks and never changes shared client timeouts.
It stops before a read cannot fit its remaining duration; a duration shorter
than the configured client timeout may produce no read. No cached countdown is
represented as live after disconnection.

Exit status: `0` current/successful; `1` operational/non-current failure;
`2` invalid arguments/configuration; `130` interruption. Tasks needing attention
do not themselves make an otherwise current snapshot fail.

## Worker assignment and execution

The sidecar determines readiness and enforces claims. **Choosing, launching and
supervising workers belongs to an external host/coordinator.** Native Copilot
`/fleet` is not automatically subscribed to this queue.

The optional **Linux** integration includes `HostSupervisor`, `LocalProfile` and
an explicit bounded programmatic helper, `mempalace_tasks.cli.supervise`. The
helper acquires exclusive local authority ownership and cannot run alongside a
separate `serve` owner. It is not an advertised shell subcommand or a native
Copilot integration. Applications assembling both MCP and supervision must share
the same owned authority rather than open a second writer.

Profiles use preconfigured absolute executable paths. They receive paths and
identity through `MPTASK_INPUT_PATH`, `MPTASK_RESULT_PATH`,
`MPTASK_CHECKPOINT_PATH`, `MPTASK_TASK_ID`, `MPTASK_ATTEMPT_ID` and
`MPTASK_CLAIM_GENERATION`. They must emit the validated JSON result/checkpoint
contract implemented in `execution.py`; process exit code zero alone is not
completion evidence. Repository profiles require an immutable full commit and
create separate worktrees under an explicit root, conventionally below `~/s`.
There is no fetch, automatic patch application or worktree deletion.

| Class | Recovery requirement |
|---|---|
| `isolated` | Authority revocation fences old publication; replacement uses a distinct attempt workspace |
| `shared_unfenced` | Proven process stop and effect reconciliation; a dead shell does not prove a cloud job stopped |
| `resource_fenced` | A real target adapter enforces resource-wide fencing and reconciliation; no production cloud adapter is bundled |

These are cooperative execution contracts, not an OS sandbox. A local-only
profile excludes remote effects and escaping/daemonized processes. External
effects require an explicit reconciliation adapter or operator action. Unsafe
work retains its reservations while unrelated eligible work may continue.

## Recovery and coherent palace backup/restore

The service automatically reconciles ordinary lost append responses. Genuine
storage loss/corruption, unsupported upstream contracts, or unknown external
effects remain explicit barriers; the software does not guess that an absent
record or dead local process proves safety.

The authority checks the live, in-memory verified prefix on refresh. Prefix
rollback/corruption or a superseding epoch fences that owner closed. There is
**no independent cross-process rollback anchor**: after process death, a
self-consistent older snapshot cannot by itself prove whether rollback was
intentional. Restore therefore requires an explicit offline procedure, not a
live file swap or a silent restart to bypass a detected integrity failure.

Use the [backup](../skills/mempalace-backup/SKILL.md) and
[restore](../skills/mempalace-restore/SKILL.md) runbooks for the helper interface
and its current platform/refusal limits. At a high level:

1. Quiesce task owners, workers/effects, hub writers and outstanding mutations;
   maintain an operator-controlled no-new-launch boundary. Stopping one task
   listener, checkpointing SQLite or finding no registry is not proof that all
   palace writers are excluded.
2. Capture a coherent physical palace cut covering the **resolved data
   directory**, including `logstream.sqlite3`, committed WAL state, referenced
   artifacts and `replica.json`, plus the other palace stores/provenance.
   Verify path coverage: the current helper refuses DATA roots outside its
   selected HOME backup root rather than silently omitting them. A wing export
   is not a task-journal recovery snapshot.
3. Restore to private staging, validate journal/authority and required artifacts,
   and establish the staged epoch barrier before publishing under offline
   exclusion, preserving canonical lock namespaces. Resume only through a fresh
   owner activation; inherited attempts require recovery and retained
   client/worker packets remain fenced.

The snapshot retains the task IDs, edges, holds and source-plan/artifact content
it actually contains; post-snapshot work may be absent. Do not replay pending
commands or restore sidecar cache files from a discarded future.
Reprovision config/tokens as needed and rebuild disposable discovery/runtime
state only while owners are quiescent. Neither palace restore nor task fencing
undoes Git/cloud effects or target-side fence counters; reconcile those before
unsafe work can be repeated.

### Supported data and administration

Only the current schema-2 configuration and epoch-bound journal envelopes are
accepted. The original experimental authority, schema-1 configuration and
pre-epoch journal formats have no compatibility or migration path. Unsupported
records fail explicitly; they are never silently skipped or replaced with a new
authority. Removing this code does not delete stored palace records.

With all other owners stopped, `mempalace-tasks reconcile --config CONFIG`
is exclusive **mutating** administration, not inspection; it also opens a new
epoch. Package replacement likewise requires quiescence.
Removing the package must not delete the accepted palace records.

No log compaction, distributed failover, native `/fleet` dispatch or Beads
CLI/Dolt/formula compatibility is claimed.

## Development checks

Use disposable storage, never the user's live palace. From the repository root:

```bash
PYTHONPATH=sidecar/src MPTASK_TEST_TMPDIR="$SESSION_FILES" \
  sidecar/.venv/bin/python -W error -m unittest discover -s sidecar/tests -v
```

`SESSION_FILES` is a pre-existing test/session artifact directory outside the
repository. The optional real-hub gate requires preinstalled `mempalace-mcp`:

```bash
MPTASK_LIVE_HUB=1 MPTASK_TEST_TMPDIR="$SESSION_FILES" PYTHONPATH=sidecar/src \
  sidecar/.venv/bin/python -W error -m unittest discover -s sidecar/tests -p test_live_contract.py -v
```

The real-hub fixture isolates HOME and palace storage, binds port zero, validates
its child-owned registry before connecting, and stops only its own processes.
It never registers a service or writes task records into the user's
existing palace.
