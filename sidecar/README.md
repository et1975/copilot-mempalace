# MemPalace Tasks

A single-host task authority over an existing MemPalace HTTP hub. The package
provides authenticated Streamable HTTP MCP, a harness-launched stdio MCP frontend,
read-only human inspection, and foreground or opt-in launcher hosting. Schema 2
uses MemPalace as the sole durable task/recovery store. No systemd, launchd or
Windows service is required; Linux worker supervision is a separate, optional integration.
It does not replace MemPalace or install another task database. Cooperative native
Copilot `/fleet` coordination uses the issuing session and existing native parent;
it does not ship a native process supervisor or replace native dispatch.

```text
Harness stdio frontends --------\
Direct HTTP MCP clients --------> one shared task owner --> MemPalace logstream
Human status/history/watch -----/

External host/coordinator --> claims, optional supervision and result delivery
Native parent session -----> cooperative reservations, agent binding and acceptance
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

Managed-host claims have renewable leases and monotonically increasing generations. A claim
is initially **preparing**, not permission to execute. A registered supervisor
provides preparation evidence; mutations from stale generations are rejected.
Default policy is a 300-second lease, renewal every 100 seconds, 15-second
maintenance sweep, 30-minute no-progress cap, two-hour attempt cap and three
automatic retries after the initial attempt.

Native cooperative goals instead use `mptask_native` with the actual issuing
session UUID, no separately registered actor/supervisor and no short-TTL model
heartbeat. Bootstrap enrolls the session atomically. Claims reserve attempts before
native dispatch; starts bind actual returned agent IDs. Explicit release,
reconciliation and session transfer handle unknown/interrupted work; silence is
not automatic retry permission. Epoch/version/attempt/generation checks fence
stale publication, not external processes or effects. Existing managed goals are
not migrated or weakened.

New prerequisites use atomic graph publication with source disposition `yield`.
Goal bootstrap creates the root and planning task together; goal closure checks
unfinished/proposed work and seals intake. An empty ready list is not completion.

This is a **cooperative, single-host** deployment. All clients use one configured
hub, one authority and one canonical runtime directory.
Never move/delete that directory or its stable lock files while an owner can
still run; using another directory bypasses local exclusion. The lock is not a
distributed fence or hub-enforced compare-and-swap. The private bearer token
identifies trusted local clients as a group; its holder can assert registered
actor names or native session identities. A session UUID is not a secret or
additional credential. This is not multi-tenant per-actor/session authentication.

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
The executable registered with the harness must already support
`mempalace-tasks mcp --help`. Use its absolute path if it is not on the harness's
`PATH`; updating a checkout does not update a separately installed, non-editable
package. Provision that package explicitly before registering it.

## Setup entry point

Start with the [end-to-end setup guide](setup.md), not a configuration fragment.
The Python package's `mempalace-tasks setup` command provides `configure`,
`enable` and read-only `check` phases around the installed commands below.
It never installs dependencies or silently replaces an authority. Existing
deployments can begin with `check`; incompatible configurations stop with a
specific blocker.

Setup shares the sidecar's Python/native-platform requirements and uses its
POSIX permission and Windows ACL helpers. It needs no .NET SDK or F# runtime.
`python -m mempalace_tasks setup` is also supported from the installed
environment. Native macOS/Windows validation limits above still apply.

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

Those registered roles/profiles govern managed-host work. Native cooperative
bootstrap enrolls its issuing session through authenticated `mptask_native`; it
does not require adding a native coordinator, worker or supervisor to genesis.
Service initialization/credentials are still required. Do not reinitialize an
existing authority to obtain a native session identity.

For configuration-only validation, without connecting to the hub or creating
files, use:

```bash
mempalace-tasks validate-config --config /absolute/path/tasks.json
```

`--allow-missing-service-token` permits a missing service credential during
deliberate preparation. It does not create it or relax validation of existing
files, permissions, genesis or other configuration fields. The result reports
allowlisted deployment fields, never credential contents. This is not an
authority-health or accepted-genesis check.

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
| `mempalace-tasks mcp --config CONFIG --timeout 10s` | Long-lived stdio MCP frontend; launcher mode starts/reuses the shared owner, external mode only connects. See [harness startup](#harness-launched-stdio-frontend). |

For `start`, `connect` and `stop`, timeout defaults to ten seconds, accepts a positive
`s`/`m`/`h` duration, and is bounded to 300 seconds. Each operation shares one
monotonic deadline across election waits, HTTP exchanges and polling. Identity
checks may use its entire remaining budget, so slower journal freshness checks
are not cut short by a separate per-poll timeout. Failed startup may additionally
take up to two seconds to clean up only the child it launched.
Stop requires the actual
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
Register a **separate** server named `mempalace-tasks`, using either the
harness-launched stdio frontend or direct Streamable HTTP. Both reach the same
shared authority; a stdio process per harness session is not a new task owner.

### Harness-launched stdio frontend

Register the following long-lived command, not the human `start` or `connect`
command:

```bash
mempalace-tasks mcp --config /absolute/path/tasks.json --timeout 10s
```

The configuration must already be schema 2, with accepted genesis, valid private
credentials and the configured MemPalace hub available. The frontend never
initializes an authority, migrates old state, repairs permissions or installs
packages. `MPTASK_CONFIG` is a fallback and `--config` may precede the subcommand;
prefer an explicit absolute config path in harness registrations.

Startup policy comes from that configuration:

- **`lifecycle: "launcher"`** is explicit consent to autostart. Each frontend
  authenticates/reuses the ready owner or starts one through the existing
  election/startup-ticket flow. Concurrent sessions share **one HTTP owner**.
- **`lifecycle: "external"`** (the default) only discovers/connects. If no ready
  owner is available, startup fails; explicitly run `serve` or arrange external
  hosting first. There is no silent launcher fallback or systemd requirement.

For a no-start probe of either configuration, invoke `mcp --no-autostart`.
This invocation-only restriction uses read-only discovery even for launcher
configs. It does not change the configuration or its authenticated binding, and
refuses if the owner disappears rather than starting a replacement.

`--timeout` defaults to ten seconds and accepts positive `s`/`m`/`h` durations up
to 300 seconds. It bounds startup and each upstream exchange, **not the lifetime
of the stdio session**. EOF/cancellation closes only the frontend's connections
and pending local work; even an owner it launched stays running. Human
`start`, `connect` and instance-qualified `stop` remain explicit lifecycle
operations. Disconnecting a harness is not a service stop.

Stdout contains only MCP protocol messages, never human connect metadata or
bearer tokens; diagnostics go to stderr. The frontend forwards upstream
`tools/list` descriptors and `tools/call` arguments/results unchanged, including
schemas, metadata, structured content, errors, freshness and resolved abandoned
outcomes. It does not inject actors/command IDs, rewrite `expected_epoch`, or
automatically retry mutations. A possibly sent mutation can have an ambiguous
outcome after timeout/disconnection; use the original epoch and command ID to
resolve it, not a rewritten request.

The upstream connection is pinned to its authenticated numeric-loopback endpoint
and owner instance. It does not silently adopt a replacement owner or follow
redirects/proxies. After an owner dies or changes, explicitly reconnect/restart
the frontend; an unresolved old mutation stays bound to its original identity.

#### Registration examples

Credential-free generic stdio configuration (adapt the enclosing key to your
harness; for example, VS Code uses `servers` rather than `mcpServers`):

```json
{
  "mcpServers": {
    "mempalace-tasks": {
      "type": "stdio",
      "command": "/absolute/path/to/mempalace-tasks",
      "args": ["mcp", "--config", "/absolute/path/tasks.json", "--timeout", "10s"]
    }
  }
}
```

The executable and configuration paths are placeholders. Tokens stay in the
private files referenced by `tasks.json`; do not add them to the registration.
Configuring this command allows the harness to launch it, including the
configured launcher policy.

**GitHub Copilot CLI:** after explicit package/configuration preparation, this
registration form is documented in
[GitHub's MCP setup guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers#using-the-copilot-mcp-add-subcommand)
and verified against `copilot mcp add --help`:

```bash
copilot mcp add mempalace-tasks -- /absolute/path/to/mempalace-tasks mcp --config /absolute/path/tasks.json --timeout 10s
```

This writes a user MCP registration; it is an operator action, not a step the
task service performs. Everything after `--` belongs to the frontend, so `10s`
is its timeout, not Copilot's separate millisecond timeout option. Registration
syntax verification is not a live harness integration test. Use Copilot's
`/mcp` manager to check the server/tools after configuring it.

### Direct Streamable HTTP alternative

No stdio process is needed for a harness that connects directly over HTTP.
Explicitly host the owner with `serve`, or opt into launcher mode and run
`start` / `connect --start`, before registering its endpoint. In schema 2,
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
There is no automatic direct-HTTP harness-registration/credential-refresh adapter.
The stdio frontend discovers fixed or dynamic endpoints at startup instead of
requiring the harness to store the HTTP URL; it still requires reconnection
after an owner change.
Never put credentials in tasks, repository files, logs or shell history.
Use numeric loopback and the exact `/mcp` path; redirects, DNS hostnames and
public binding are not supported by this service/client profile.

All sessions connect to the **same running service**, not a fresh stateful owner
per agent. Host/Origin checks and token checks protect the endpoint; they
do not make an untrusted shared host or a second independent authority safe.

Install/link the optional [task-safety skill](../skills/mempalace-tasks/SKILL.md)
and [workflow agent](../agents/palace-task-workflow.agent.md) using the pack's
normal [copy/link customization pattern](../README.md#step-1--copy-or-symlink-the-customization-pack).
Their MCP selectors assume server aliases
`mempalace-tasks` and `mempalace`; adjust aliases consistently if your harness
uses different names.

### Tool surface

Discover actual `tools/list` schemas instead of copying argument guesses.

| Group | Tools |
|---|---|
| Cooperative native | `mptask_native` with a strict action-specific payload |
| Lifecycle | `mptask_create`, `update`, `claim`, `renew`, `checkpoint`, `attempt_report`, `release`, `recover`, `transition`, `note` |
| Graph and goals | `mptask_add_dependency`, `remove_dependency`, `bootstrap`, `expand`, `goal_close` |
| Read/observe | `mptask_get`, `snapshot`, `list`, `ready`, `history`, `health`, `wait_ready` |
| Historical resolution | `mptask_outcome` |

Every short name in the table has the `mptask_` prefix. Managed mutations require
`actor`, `command_id`, and **`expected_epoch`**.
Native mutations use
`mptask_native(action, session_id, command_id, expected_epoch, payload)` without
an actor argument. The action selects a strict payload; discover its current
schema rather than reuse managed arguments. Both modes obtain `expected_epoch`
from the current read/health response's `epoch_id`. Clients do not pass internal
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
match the mode-specific session/agent or host-supplied actor/owner, attempt and
generation as well as the epoch.

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

## Per-goal native fleet workflow

The [task-safety skill](../skills/mempalace-tasks/SKILL.md) and
[workflow agent](../agents/palace-task-workflow.agent.md) use `mptask_native` for
**cooperative native session coordination**. The existing native parent remains
the sole dispatcher; this is not another agent scheduler, daemon or supervisor.
Task state uses only the `mempalace-tasks` MCP registration; exact artifacts,
context and memory use only `mempalace` MCP. There is no CLI/SQL storage fallback or
native todo mirror. Fresh MCP observations, not native success messages or memory
drawers, determine tracked task state.

After copying/linking those customizations, explicitly request tracking for the
goal. The tracking phrase routes the active native parent through the workflow;
a separate agent-selection handoff is not required:

```text
/fleet execute and track this as a goal
```

When an approved native plan is present in the active conversation, the workflow
preserves it verbatim as the canonical MemPalace artifact and files a concise
searchable drawer index with its objective, stage outline, artifact ID and
SHA-256. The drawer contains no task status. The workflow then creates a concise
root plus planning/import task and publishes the executable units and their real
dependencies as the durable task graph. Paragraph order is not a dependency;
rationale remains plan memory, and validation is task acceptance/evidence unless
it is independently actionable.

Retain one issuing parent session UUID: use the harness UUID when available,
otherwise issue one UUID and retain the enrolled binding across compaction.
Use project/scope/bounded task/concurrency/credit limits from the request or trusted
configuration. Explicit user limits override defaults. Do not infer identity
from a username or issue a fresh UUID for each command.
Native bootstrap atomically enrolls this session with the new scoped goal and
import task. No separately provisioned coordinator actor or native supervisor is
required. The UUID is an identifier, not a secret or credential; the existing
authenticated MCP connection remains the trust boundary.

The same session retains its UUID through compaction/resume, but must refresh
durable state. A fork or repository session has a distinct UUID; its request to
find a leaf does **not** authorize transfer. Only a deliberately authorized
full-goal successor transfers the named goal through native `resume` with current
compare-and-swap state and reconciliation, after explaining the impact on existing
attempts. It must not claim continuity by copying the old UUID.
Current parent identity comes from the root goal's `native.session_id` or fresh
`authorization.session_id`. Bounded transfer updates only the root, leaving member
snapshots untouched. Inherited attempts may remain stored `in_progress`, but fresh
authorization is false with `native_reconciliation_required`; they cannot publish.
Members' recorded sessions are not current ownership.
The root's internal `session_generation` advances on transfer and is matched
against each attempt's recorded generation, preventing A→B→A UUID reuse from
reviving old work. This is server-maintained fencing, not a caller payload field.
To deliberately transfer the whole tracked goal, name its actual durable ID and
scope the takeover explicitly; this example ID is synthetic:

```text
/fleet Take over the entire tracked MemPalace goal tsk_goal_fixture.
Inspect the affected attempts and explain their recovery before transfer.
```

Selecting the agent, installing the pack, available tools or ordinary `/fleet`
does **not** opt in. Each new goal needs an explicit request; named resume keeps
only the existing goal's scope. Unrelated goals and session planning stay untouched.

| Observed state | Allowed workflow and required report |
|---|---|
| No explicit goal opt-in | Ordinary native fleet/session planning; no task-service setup demand or durable writes. |
| Opted in, service or `mptask_native`/required action schema unavailable | Report the concrete version/setup blocker. No alternate store, invented identity or silent untracked fallback; a healthy old service is insufficient. |
| Native API available, no separately configured actor/supervisor | Proceed with cooperative session bootstrap/resume and native-parent dispatch. Their absence is not a native prerequisite. |
| Named native goal, same session after compaction | Read current mode/binding/epoch/versions and reconcile unresolved commands; no new goal or enrollment. |
| Repository session asks for relevant leaves in a named goal | Read-only goal-scoped discovery and handoff to the existing coordinator; no `resume`, epic claim or borrowed UUID. |
| Deliberately authorized full-goal successor | Explicit `resume` with the new actual session UUID, CAS and inherited-attempt recovery before replacement work. |
| Named managed goal | Preserve managed mode and registered actor/host/lease/fencing requirements; no silent conversion via native API. |
| Unknown stored mode | Fail closed as unsupported mode/schema; no inferred cooperative enrollment. |

### Cross-repository discovery and coordination

Retain **one existing native coordinator per goal**, even when its leaves target
different repositories. The root's current `native.session_id` governs ordinary
mutations including `claim`; neither a task's original/recorded session nor its
`project` grants a different repository parent independent ownership. `project`
is a categorization, not necessarily the repository name. The API does not
support independent repository-parent ownership of leaves within one native goal.
Recovery instructions do not add that protocol or an enforced transfer-consent
guard.

A request such as "find work relevant to this repo from goal G" is valid leaf
discovery, not consent to claim the epic or take over the whole goal:

1. Use the exact goal ID with `mptask_get` to inspect stored mode, root owner and
   scope. Keep managed goals managed.
2. Call `mptask_list(filters={"goal_id": G}, limit=...)` with no status restriction
   and follow **every `next_cursor`**, keeping the filter unchanged. Do not guess
   `project=repository` or restrict discovery to `ready`/`needs_attention`:
   active, blocked, proposed and terminal tasks may identify the intended work.
   Expired/incomplete pagination is not proof of a unique match; refresh or report
   the read-budget blocker. Cursor pages are pinned snapshots.
3. Resolve unknown labels through those candidates' exact-ID `mptask_get` details,
   repository/file/worktree scope, stable intent and plan references. Titles or
   labels need not be unique. `mptask_get` is not a label resolver;
   `mptask_ready` only selects currently ready tasks. Missing/ambiguous scope is a
   blocker, never a reason to invent an ID. Before any claim/dispatch, the current
   coordinator refreshes the root and selected task, checks readiness and uses the
   real task ID, stored project and current version.
4. Present a bounded packet with authority/epoch, goal/current parent, exact task
   IDs/versions, repository/worktree scope, intent/acceptance and observed blockers.
   Hand it to the existing coordinator using only a verified available contact
   path, such as `write_agent` to a known reachable agent. A session UUID is not a
   cross-session messaging address. Without supported contact, report an explicit
   coordination blocker and provide the packet for handoff. Do not invent a
   transport/API, borrow the parent's identity, create a second coordinator or
   resume the root merely to claim a leaf.

The retained coordinator claims/starts both independent leaves, binds distinct
workers to their repository/worktree scopes and publishes their returned evidence.
Workers verify their actual working location before effects; scope text does not
create a worktree, grant access or enforce isolation. Parent-owned work is an
alternative only when the actual coordinator has access and selects it before
claim/start. A different repository session is not that fallback parent.

### Native lifecycle

This is the workflow order. The [native MCP reference](../skills/mempalace-tasks/references/native.md)
provides exact action payloads, receipt/read fields and substitutable JSON examples
checked against the implementation; discover the deployed schema before calls.

1. Preserve exact approved plan and searchable drawer index, then native
   `bootstrap` the concise goal and import/planning task.
2. `claim` the import task; `start` parent work using the actual parent session
   UUID. Native `expand` publishes definitions, membership, provenance and initial
   blockers together. Native definitions omit managed execution-class/profile/
   resource fields; the stored mode is `cooperative_native`.
   Use dependency-closed batches; final import completion
   includes the last required publication and acceptance evidence.
3. Resolve exact task IDs using the discovery procedure above and read the goal's
   ready frontier. Select the execution path before claiming: worker dispatch
   requires verified live support for native `task(mode="background", ...)` and
   subsequent `write_agent` to the returned ID. A synchronous or one-shot worker
   cannot perform the post-return WAIT/start handshake. If the schema does not
   expose that mode/contact capability, select parent-owned work or report the
   blocker, rather than inventing a tool argument.
   `claim` reserves the current task version's attempt/generation **before**
   native dispatch. Send a bounded worker packet
   requiring it to wait for confirmed binding before work.
   If legitimate work is held/deferred, native `update` can clear resolved gates
   with a current-version content patch; do not cancel/recreate it. Null clears
   `hold_reason`/`deferred_until`. Update only accepts open non-root content, not
   admission/execution changes or active/recovering-state bypasses.
4. Dispatch using supported native `task(mode="background", ...)`, then `start`
   binds its **actual returned agent ID**. Confirm start and notify that ID with
   `write_agent`; worker checks the fresh task/agent/attempt/generation binding
   before effects. Parent-owned work
   binds the parent session UUID. Neither a claim nor a guessed agent ID is a
   started worker. Failed/ambiguous dispatch, binding or notification requires
   inspection of the known worker and fresh task, `mptask_outcome` resolution of
   uncertain durable commands, then release/reconciliation before a replacement.
   A completed read-only report is not a successful handshake. Never fabricate a
   returned ID or turn a ghost worker binding into parent work.
   WAIT is an instruction/acknowledgment, not enforced runtime suspension;
   parent-only lifecycle publication is workflow discipline, not per-worker
   authentication under the shared bearer.
5. Preserve meaningful progress with `checkpoint`. Publish independent discoveries
   with expand/continue; dependent work includes its real blockers from first
   visibility. If A needs new B, atomically expand/yield A with checkpoint, reason,
   observations, B, provenance and `blocks(B,A)`. A becomes recovering; explicitly
   reconcile before a new claim. Do not hold A's slot waiting for B.
6. Assess returned results against acceptance and current binding; native
   `complete` requires durable evidence and explicit `parent_acceptance`.
   Expand/complete and goal_close require the same acceptance fields.
   Final discoveries can use atomic
   expand/complete. Stale results remain evidence, not fresh publication authority.
7. For interrupted/unknown work use `release`/`reconcile` with explicit known facts.
   An explicitly authorized full-goal successor uses root-only `resume`;
   inherited active attempts remain stored active but unauthorized.
   Individually release each with current version and
   retained attempt/generation/agent, then reconcile the now-recovering task.
   Already recovering tasks reconcile directly; never adopt old workers.
   Silence, compaction or long reasoning is not proof of death and
   does not trigger automatic retries.
8. Inspect unresolved/blocked/proposed work even if ready is empty. Native
   `cancel` rejects unwanted open non-root proposals/tasks in place with
   version CAS, reason and observations; no fake admission, claim or attempt tokens.
   Active/recovering work must use release/reconcile; nested epic cancellation
   requires finished, recovery-free children and never cascades. The root cannot
   be cancelled through this action.
   Native
   `goal_close` uses current goal/graph revisions and aggregate acceptance evidence;
   report success only after confirmed durable closure and intake sealing.

**Assurance boundary:** native mode records cooperative ownership, checkpoints and
accepted evidence. It does not require short-TTL model heartbeats or an external
timer, and does not prove containment, physical stop or effect settlement.
Native authorization deliberately reports `lease_live=false` and
`physical_supervision=false`; these are not expired managed lease conditions.
Require fresh matching `authorized=true` for executing/publishing running-source
work, not before bootstrap, update, claim, start, cancel, recovery or aggregate closure. Those
actions use their own state/session/version/acceptance checks; reserved/preparing
and recovering attempts intentionally report `authorized=false`.
Epoch/version/attempt/generation checks reject stale task publications; they do not
undo Git/cloud effects or stop old processes. Unknown effects stay explicit until
reconciled, never retried on silence. Work requiring managed-host assurances must
retain that contract rather than be relabeled cooperative.
The optional Linux `HostSupervisor` remains a separate
[managed host integration](#worker-assignment-and-execution).

Use the existing [preinstalled-package preparation](#requirements-and-offline-setup),
[schema-2 initialization/private credentials](#configure-and-initialize) and
[long-lived stdio registration](#harness-launched-stdio-frontend), not another
setup recipe. Register `mempalace-tasks mcp`, not one-shot `start`/`connect`.
Frontends share one HTTP owner: configured launcher consent allows autostart on
frontend connection; external mode is connect-only. Read-only
[status/history inspection](#human-supervision) must not trigger launcher startup
or owner restart.

After frontend failure or owner change, explicitly reconnect the pinned frontend;
retain the original authority, epoch, command ID and exact payload of unresolved
mutations. Reconnection is not new execution authorization. EOF, frontend stop
or native cancellation is neither owner stop nor proof of physical settlement.
Roll back this workflow for new goals by no longer opting them in; this does not
erase tracked state, release active claims or settle effects. Reconcile native
work explicitly; managed work follows the
[host recovery contract](#worker-assignment-and-execution). Use the
[recovery procedure](#recovery-and-coherent-palace-backuprestore) for existing work.

The [workflow scenarios](../skills/mempalace-tasks/references/scenarios.md) separate
the observed failed-session baseline, expected pressure behavior, simulations,
service tests and live harness evidence. Documentation/schema checks alone are
not live enforcement proof. No managed native-supervision claim follows from
successful cooperative coordination. Existing memory recall/reflection/procedural
guidance and hooks remain independent and unchanged.

## Worker assignment and execution

The sidecar determines readiness and enforces claims. In cooperative native mode,
the native parent chooses/dispatches workers through the lifecycle above; `/fleet`
is not automatically subscribed to the queue. The remainder of this section
describes **managed-host execution**, whose choosing, launching and supervising
workers belongs to a configured external host/coordinator.

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

Tests and their worker/fixture helpers are repository-only under `tests/sidecar`,
not included in the installed sidecar or source distribution. Use disposable
storage, never the user's live palace. Run from the repository root; pytest
configuration supplies source and test import paths without `PYTHONPATH`.

Select a preprovisioned Python 3.11+ interpreter as `TEST_PY`, with the sidecar's
declared production dependencies and pytest 8.4.2 from the development-only
`requirements-test.txt`. The complete repository suite additionally needs the
existing MemPalace/model prerequisites, preinstalled `uv` on PATH, and this
sidecar's `[build-system]` prerequisites in `TEST_PY` (`setuptools>=68`, plus
`wheel` if the chosen backend requires it). The root package regression builds
wheel/sdist artifacts and rebuilds a wheel from the sdist offline in external
temporary storage. Build tools remain development-only prerequisites, not runtime
dependencies. A partial environment is not a full-suite pass. No test command
installs dependencies or downloads packages/models.

`SESSION_FILES` must be an existing session artifact directory outside the
repository. Pytest clears its `--basetemp` child: use only a dedicated disposable
child, never `SESSION_FILES` itself.

```bash
export PYTHONDONTWRITEBYTECODE=1
export MPTASK_TEST_TMPDIR="$SESSION_FILES"
export TMPDIR="$SESSION_FILES"
"$TEST_PY" -W error -m pytest --basetemp "$SESSION_FILES/pytest-sidecar" tests/sidecar -q
```

The existing optional real-hub gate requires preinstalled `mempalace-mcp`.
Keep the bytecode export above so child Python processes inherit it:

```bash
MPTASK_LIVE_HUB=1 "$TEST_PY" -W error -m pytest \
  --basetemp "$SESSION_FILES/pytest-live-hub" tests/sidecar/test_live_contract.py -q
```

The real-hub fixture isolates HOME and palace storage, binds port zero, validates
its child-owned registry before connecting, and stops only its own processes.
It never registers a service or writes task records into the user's
existing palace.

See `tests/README.md` in a repository checkout for the full suite matrix, external
temporary-root defaults and distribution acceptance checks. That developer guide
and the tests are not shipped in this package.
