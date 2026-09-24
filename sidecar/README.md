# MemPalace Tasks

A single-host task authority over an existing MemPalace HTTP hub. The package
provides an authenticated MCP endpoint, read-only human inspection, and an
optional managed-process supervision API. It does not replace MemPalace, start
Copilot fleets automatically, or install another authoritative database.

```text
Copilot / other MCP clients ----\
                                > task sidecar --> MemPalace logstream
Human status/history/watch ----/        |
                                       +--> optional historical drawer/KG views

External host/coordinator --> claims, worker supervision and result delivery
```

## What is authoritative

The reserved `mptask/<authority_id>` logstream contains immutable commands and
settlement records. The sidecar reconstructs tasks, dependencies, generations,
resource reservations and outcomes from that journal. Local pending/head/clock
files are recovery metadata; drawers and KG facts are historical projections.

Claims have renewable leases and monotonically increasing generations. A claim
is initially **preparing**, not permission to execute. A registered supervisor
provides preparation evidence; mutations from stale generations are rejected.
Default policy is a 300-second lease, renewal every 100 seconds, 15-second
maintenance sweep, 30-minute no-progress cap, two-hour attempt cap and three
automatic retries after the initial attempt.

New prerequisites use atomic graph publication with source disposition `yield`.
Goal bootstrap creates the root and planning task together; goal closure checks
unfinished/proposed work and seals intake. An empty ready list is not completion.

This is a **cooperative, single-host** deployment. All clients use one service and
one canonical state directory. Its file lock is not a distributed fence. The
private bearer token identifies trusted local clients as a group; its holder can
assert registered actor names. This is not multi-tenant per-actor authentication.

## Requirements and offline setup

- Linux for the service clock, process ownership and supervision.
- Python 3.11+ for the package; the checked dependency lock and SDK integration
  environment use Linux/Python 3.12.
- An already installed MemPalace hub with the supported append/list contract.
- Official Python MCP SDK **1.30.0**, pinned with its dependencies in
  `requirements.lock`.
- Git only for execution profiles that create isolated repository worktrees.

From the repository root, with preinstalled Python/uv and the required artifacts
already in the local cache:

```bash
uv venv --offline --python 3.12 sidecar/.venv
uv pip sync --offline --require-hashes --python sidecar/.venv/bin/python sidecar/requirements.lock
uv pip install --offline --no-deps --python sidecar/.venv/bin/python -e ./sidecar
sidecar/.venv/bin/mempalace-tasks --help
```

If a cached dependency/build tool is missing, provision it from an approved
source before retrying. Do not add a download/install fallback to service startup.
The hash-pinned requirements lock is intentional: the available offline cache
supports this environment but not universal `uv lock` resolution. Other platform
or interpreter combinations need their own dependency validation.

Copying the customization pack alone does **not** install or start this package.
Use a prebuilt, approved package/environment for deployment; do not launch it
through a command that fetches packages on every MCP connection.

## Configure and initialize

Create a private credential/config directory using your normal administration
tools. Credential parents must already exist. Choose a new authority UUID, an
unused loopback port, and a dedicated state directory; do not reuse a repository
or another application's state directory.

Example configuration below is a template, not a deployment. Replace the UUID
and `/srv/...` paths with your own real, absolute, non-symlink paths. Obtain the
existing hub credential through its normal administration flow; never paste a
token into a task, repository, chat or log.

```json
{
  "schema_version": 1,
  "authority_id": "11111111-1111-1111-1111-111111111111",
  "hub_url": "http://127.0.0.1:8765/mcp",
  "hub_token_file": "/srv/mptask-private/hub.token",
  "service_token_file": "/srv/mptask-private/service.token",
  "state_dir": "/srv/mptask-state",
  "host": "127.0.0.1",
  "port": 8766,
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
    },
    "policy": {
      "lease_ttl_seconds": 300,
      "renewal_seconds": 100,
      "sweep_seconds": 15,
      "progress_timeout_seconds": 1800,
      "hard_timeout_seconds": 7200,
      "automatic_retries": 3
    }
  },
  "maintenance_actor": "system",
  "recovery_actor": "operator",
  "project_wings": {"demo": "demo-wing"},
  "projections_enabled": false
}
```

Omit `hub_token_file` only when the explicitly configured hub does not require
one. Token files must be singly linked regular files owned by the current user,
with no group/other permissions. Configuration/token reads never repair modes,
create parents, or chmod a shared directory.

```bash
sidecar/.venv/bin/mempalace-tasks init --config /srv/mptask-private/config.json --generate-token
sidecar/.venv/bin/mempalace-tasks serve --config /srv/mptask-private/config.json
```

`init --generate-token` exclusively creates a missing private service token
without displaying it. It does not overwrite existing tokens or accepted
configuration. Initialization is idempotent for the same normalized genesis.
Actors, supervisors and declared capabilities are fixed by that genesis; changing
the JSON later is not an implicit authorization/configuration migration.

`serve` is foreground and requires prior initialization. It never creates a
replacement clock, chooses another palace, or starts an embedded storage writer
when the configured hub is unavailable. Use your normal user-service manager
for unattended operation, with the preinstalled executable and explicit config.
No service or MCP registration is installed automatically.

## Connect MCP clients

Keep the existing MemPalace MCP registration for ordinary memory operations.
Add a **separate Streamable HTTP** server named `mempalace-tasks`, pointing to the
configured endpoint, for example `http://127.0.0.1:8766/mcp`.

Configure its `Authorization: Bearer ...` header securely from the private
service token using the harness's supported credential mechanism. Do not commit
the header value or expose it in shell history. In Copilot CLI, use the `/mcp`
configuration manager. Use numeric loopback addresses, the exact `/mcp` path,
and the configured port; redirects, DNS hostnames and public binding are not
supported by this service/client profile.

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

Every short name in the table has the `mptask_` prefix. Public mutations require
`actor` and `command_id`; clients do not pass `operation`. Internal genesis and
expiry commands are not exposed as public MCP tools.

For uncertain outcomes, retain the exact command ID/payload. Ordered settlement
either observes its commit or permanently abandons a delayed original. Only a
confirmed terminal-abandoned outcome permits a new ID for the still-needed
request. A successful old receipt remains historical; use current
`get.authorization` and match the host-supplied owner/attempt/generation.

## Human supervision

```bash
sidecar/.venv/bin/mempalace-tasks status --config /srv/mptask-private/config.json --project demo
sidecar/.venv/bin/mempalace-tasks list --config /srv/mptask-private/config.json --needs-attention
sidecar/.venv/bin/mempalace-tasks show TASK_ID --config /srv/mptask-private/config.json
sidecar/.venv/bin/mempalace-tasks history TASK_ID --config /srv/mptask-private/config.json --json
sidecar/.venv/bin/mempalace-tasks watch --config /srv/mptask-private/config.json --project demo --refresh 5s --duration 10m
sidecar/.venv/bin/mempalace-tasks inspect --config /srv/mptask-private/config.json
```

These commands query the running service. They never claim, renew, expire,
reconcile, start a writer, or modify user config/token permissions. `inspect`
reports authority and runtime-lane health; task views show scope/counts, owner,
generation, deadlines/progress, blockers, resources, retry/recovery and evidence.

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

The package includes `HostSupervisor`, `LocalProfile` and an explicit bounded
programmatic helper, `mempalace_tasks.cli.supervise`. The helper acquires exclusive
local authority ownership and cannot run alongside a separate `serve` owner.
It is not an advertised shell subcommand or a native Copilot integration.
Applications assembling both MCP and supervision must share the same owned
authority rather than open a second writer.

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

## Recovery, projections and upgrades

The service automatically reconciles ordinary lost append responses. Genuine
storage loss/corruption, unsupported upstream contracts, or unknown external
effects remain explicit barriers; the software does not guess that an absent
record or dead local process proves safety.

With `serve` stopped, exclusive administration is available:

```bash
sidecar/.venv/bin/mempalace-tasks reconcile --config /srv/mptask-private/config.json
sidecar/.venv/bin/mempalace-tasks project --config /srv/mptask-private/config.json --resume --max-batches 10
sidecar/.venv/bin/mempalace-tasks project --config /srv/mptask-private/config.json --rebuild --max-batches 10
```

Projection is off by default. When enabled, it runs through a separate client
outside the authority lock. Only accepted domain records are projected;
heartbeats are not embedded. Delivery is explicitly at least once. Large source
records are losslessly partitioned; immutable revision nodes preserve provenance.
MemPalace's legacy KG validity is whole-second: derived boundaries use a
conservative ceiling while exact source timestamps remain in content/`source_at`.
Historical KG/drawers are never current task authority.

A failed projection pauses visibly without rolling back tasks. A bounded manual
resume/rebuild may leave backlog; rebuilding touches only the derived checkpoint,
not task history. Quiesce workers and stop the service before replacing its
package or restoring storage. Preserve the MemPalace logstream together with
local pending/head/clock recovery files. Removing the package must not delete
these records.

Healthy operations consume verified append-only tails; startup, uncertainty
recovery and explicit full audits verify the stored prefix. In-place history
rewrites are outside the cooperative contract and are detected at full audits,
not on every healthy read. No log compaction, distributed failover or Beads CLI/
Dolt/formula compatibility is claimed.

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
It never registers a service or writes task/projection records into the user's
existing palace.
