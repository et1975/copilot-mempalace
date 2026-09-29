# Set up task tracking

This is the supported path from an installed package to usable task MCP tools.
Run the repository's `scripts/setup-tasks.fsx` from this checkout. It delegates
authority operations to the installed task CLI, not a second writer or a custom
task database.

**Do not equate installed files with readiness.** These are separate results:

| Result | What it establishes |
|---|---|
| Package available | The executable and its dependencies are installed |
| Configuration valid | Schema, roles, paths and credential files satisfy the installed validator |
| Task MCP ready | The intended authority is reachable and the stdio frontend advertises working task tools |
| Session connected | The current Copilot session has loaded that registration |
| Native execution ready | A separately supplied compatible supervisor can authorize and supervise native workers |

The setup helper does not provide the last result. Healthy MCP supports task
planning and inspection; it does not prove native execution support.

## Prerequisites

The helper currently targets **Linux**. Preinstall:

- A .NET SDK with F# Interactive (`dotnet fsi`).
- The task package built from a version of this repository that includes
  `mempalace-tasks validate-config` and `mcp --no-autostart`. Follow the
  [offline package preparation instructions](README.md#requirements-and-offline-setup).
- Copilot CLI with `copilot mcp get --json` and `copilot mcp add`.
- A running, explicitly chosen MemPalace HTTP hub with the task journal API.
  A stdio-only `mempalace` registration is not an HTTP hub endpoint.

The helper does not download packages, install interpreters, start a memory hub,
or replace a non-editable installed package when the checkout changes. A
missing prerequisite is an error, not an invitation to fetch it at startup.
The task service itself remains Python-based; .NET is needed only for this
optional repository setup helper.

All three setup phases accept `--task-executable /absolute/path/mempalace-tasks`
and `--copilot-executable /absolute/path/copilot` overrides. Use these for
preprovisioned virtual environments or when the harness PATH differs from your
terminal. Otherwise the helper resolves the installed executables from PATH.

You supply the **hub endpoint** and **configuration path**. If the hub requires
authentication, also supply an existing private credential-file path with
`--hub-token-file`; never put a token value in an argument, config JSON, log or
chat.
Fresh configuration requires a numeric-loopback HTTP(S) hub endpoint, such as
`http://127.0.0.1:8765/mcp`; `localhost` is not resolved or guessed.

## New installation

The example hub endpoint below is illustrative. Use the actual endpoint for
your intended palace. The config path must be absolute and in private storage
outside the checkout.

```bash
./scripts/setup-tasks.fsx configure \
  --config "$HOME/.config/mempalace-tasks/config.json" \
  --hub-url http://127.0.0.1:8765/mcp \
  --new-authority
```

`configure` prepares a private schema-2 deployment. It generates a new authority
UUID, chooses a separate runtime directory and service-token path, uses
`127.0.0.1` with dynamic port `0`, and selects `lifecycle: launcher`. It includes
the standard actor roles and an isolated execution profile. Review the
configuration before initialization: accepted genesis fixes these roles and
profiles; editing JSON afterward does not change accepted authorization.

This phase creates no task history, starts no owner and adds no MCP registration.
Repeating it preserves the existing compatible configuration and identity.
Contradictory supplied values or unsupported existing configurations are
refused, not silently ignored.
New config/credential files use `0600` and private directories use `0700`.
Existing paths must meet the required permissions; setup does not chmod or
repair shared directories to force installation through.

Then deliberately authorize initialization and registration:

```bash
./scripts/setup-tasks.fsx enable \
  --config "$HOME/.config/mempalace-tasks/config.json" \
  --initialize \
  --generate-token \
  --register-copilot
```

The flags are separate consent:

- `--initialize` permits the installed `init` operation for the selected
  authority. It does not authorize migration or replacement of other history.
- `--generate-token` permits exclusive creation of a missing service credential
  through `init`; it never rotates an existing credential.
- `--register-copilot` permits adding the separate `mempalace-tasks` user MCP
  registration. Other servers remain untouched.

For launcher configurations, `enable` explicitly starts or reuses the shared
owner. It then verifies the stdio protocol, required tool descriptors and fresh
health. It does not create demonstration tasks or claim a native supervisor is
present. A matching live owner and registration are reused on later runs.

Finally, reload the registration in Copilot's `/mcp` manager or restart the
affected session. Registration written on disk does not prove an already-open
session has loaded its tools. Confirm that session can discover the
server-qualified `mptask_*` schemas before attempting tracked work.

## Existing installation

Start with an inspection:

```bash
./scripts/setup-tasks.fsx check \
  --config "$HOME/.config/mempalace-tasks/config.json"
```

`check` is non-mutating with respect to deployment and authority state: no
initialization, credentials, permission repair, owner startup or registration
changes. It uses authenticated read-only discovery rather than treating config
port `0` as an endpoint. If no owner is running, it reports that blocker and does
not start a frontend.

For a ready owner, it checks the actual Copilot registration and probes its
executable with the original config and the invocation-only `mcp --no-autostart`
flag. Authority, runtime, lifecycle and credential references remain identical,
preserving the native authenticated discovery binding. The flag prevents a race
with owner shutdown from turning inspection into launcher startup.

If configuration and genesis are already prepared but registration is missing:

```bash
./scripts/setup-tasks.fsx enable \
  --config "$HOME/.config/mempalace-tasks/config.json" \
  --register-copilot
```

Omitting `--initialize` never permits creating genesis because some local file
is absent. Missing accepted history remains an error.

For configuration validation without any network/discovery step:

```bash
mempalace-tasks validate-config \
  --config "$HOME/.config/mempalace-tasks/config.json"
```

Setup phases return JSON: exit `0` means the requested phase succeeded, `1`
means an operational refusal, and `2` means invalid invocation.
Read the phase result: a valid config alone is not task-MCP readiness, and
completed earlier phases may remain in place after a later failure.

An `enable` invocation without `--register-copilot` can validate the service
without adding a missing registration. Its `registration_verified: false`
does not mean Copilot is connected. For a full Copilot installation, explicitly
register and finish with `check`.

## Unsupported or conflicting configuration

**Schema 1 is not upgraded by renaming fields.** Preserve its configuration,
credentials and history. This repository has no supported schema-1 task
migration. Deliberately choose a different config path and a new authority only
if you want an independent task store; old tasks are not carried over.

```bash
./scripts/setup-tasks.fsx configure \
  --config "$HOME/.config/mempalace-tasks-v2/config.json" \
  --hub-url http://127.0.0.1:8765/mcp \
  --new-authority
```

Do not reuse the old runtime namespace or overwrite the old config to make an
error disappear. For recovery of supported current-format history, use the
[recovery contract](README.md#recovery-and-coherent-palace-backuprestore), not
fresh initialization.

A different existing `mempalace-tasks` registration is also a refusal. Inspect it
with `copilot mcp get mempalace-tasks`; decide whether it is another deployment
you still need. If you intentionally replace that registration, remove only
that named entry through Copilot's MCP manager, then rerun
`enable --register-copilot`. The helper never overwrites custom commands,
arguments or restricted tool selections.
Registrations with the default timeout omitted or explicitly set to
`--timeout 10s` are supported; arbitrary custom arguments are not treated as
equivalent.

## Completion and troubleshooting

| Blocker | Action |
|---|---|
| Missing executable, `validate-config`, or `mcp --no-autostart` support | Prepare the current package; updating a checkout is not reinstalling a non-editable package |
| Unsupported schema or malformed config | Preserve it and resolve the deployment choice; do not initialize over existing history |
| Missing/private-file error | Supply the intended credential or explicitly authorize new token creation; do not loosen permissions |
| Hub unavailable | Restore the configured hub separately; setup does not guess another palace |
| Owner not running | Deliberately run `enable` for launcher mode or arrange external hosting |
| Registration absent or mismatched | Add the intended registration explicitly; preserve other deployments |
| Tools/list or health fails | Treat MCP as unavailable even if a port is listening |
| Tools absent only in an existing session | Reload/reconnect MCP in that session or restart it |
| No compatible native supervisor | Use authorized planning/inspection only; tracked execution remains blocked |

There is no automatic destructive `undo-all`: accepted genesis and task history
are durable, not installer scratch. Unregistering a frontend does not stop its
shared owner. Stopping an owner requires the explicit instance-qualified
`mempalace-tasks stop` operation; never stop an unrelated owner or delete its
runtime/history during setup cleanup.
