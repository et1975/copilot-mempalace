# Harness-specific MCP config locations

The same MemPalace server binary is registered in different files per harness:

| Harness | Config file |
|---|---|
| VS Code / Copilot Chat in VS Code | `~/Library/Application Support/Code/User/mcp.json` |
| GitHub Copilot CLI | `~/.copilot/mcp-config.json` (managed via `copilot mcp add/list/get/remove`) |
| Claude Code | `~/.claude/mcp.json` |
| Cursor | `~/.cursor/mcp.json` |

Canonical stdio command (any harness):

```
mempalace-mcp
```

Installed on `PATH` by `uv tool install mempalace` / `pip install mempalace`. Optional `--palace /path/to/palace`
to override the default `~/.mempalace`. Run `mempalace mcp` for the authoritative setup snippet.

## Optional task sidecar

The task service is an additional registration, not a replacement for
`mempalace-mcp`. Provision its installed package/environment separately, with
schema-2 configuration, accepted genesis and private credentials. Copying the
customization pack does not install, initialize or register the task service.
For the Python-native `mempalace-tasks setup configure` / `enable` / `check`
workflow, see the repository's [task setup guide](../../../sidecar/setup.md).
It requires the already prepared Python package. Finish with
`check`, then load the registration in the affected harness session; a copied
skill, installed executable or saved registration alone does not prove readiness.

For harness-managed startup, register a stdio server named `mempalace-tasks`
using the preinstalled executable (an absolute executable path is recommended):

```text
mempalace-tasks mcp --config /absolute/path/tasks.json --timeout 10s
```

See the [generic stdio snippet and verified Copilot CLI example](../../../sidecar/README.md#registration-examples).
Do not register human `start` or `connect` commands as stdio servers: they emit
connection metadata, whereas `mcp` keeps stdout protocol-only. The config's
`lifecycle: "launcher"` opts into autostart/reuse of one shared HTTP owner;
`external` (default) only connects and fails if no ready owner exists. There is
no implicit init, migration, package installation or systemd requirement.

Each harness session runs a frontend, not a new stateful authority. EOF leaves
the shared owner running; human lifecycle operations remain explicit. The
optional timeout defaults to ten seconds per startup/upstream exchange, not
session lifetime. Tool schemas, arguments and results are forwarded unchanged;
no automatic mutation retry or epoch rewriting occurs. An owner change requires
explicit frontend reconnection, even with a fixed HTTP port.

[Direct Streamable HTTP](../../../sidecar/README.md#direct-streamable-http-alternative)
remains an alternative: explicitly start/host the owner, then register its exact
numeric-loopback `/mcp` URL. Fixed ports permit fixed URLs; `port: 0` needs fresh
authenticated discovery after restart. Stdio performs that discovery at frontend
startup. The direct HTTP registration needs a securely supplied
Bearer header from its private service-token file. Use the harness's supported
credential/configuration mechanism; do not commit tokens or paste them into
tasks. Copilot CLI exposes its MCP configuration manager through `/mcp`.

All sessions share one owner. The workflow agent's tool selectors assume
`mempalace-tasks` and `mempalace` aliases; keep those names aligned with actual
registrations. MCP connectivity alone does not supply a native fleet dispatcher
or host heartbeat/recovery adapter.

## Optional procedural context adapter — off and unregistered

`hooks/procedural_context.py` is separate from MCP registration and the ordinary
recall/save hooks, including session-finalization capture. It is POSIX-only and
uses the preinstalled MemPalace Python environment, an existing SQLite-exact
palace and the existing local MiniLM cache. Keep the complete
`skills/dreaming/scripts/` directory together. No dependency download, backend
conversion, task-service registration, rule publication or outcome follows.

The two files `hooks/procedural-context.config.example.json` and
`hooks/procedural-context.json.example` are **examples only**. The configuration
ships with `mode: "off"`; do not install the hook example into active runtime
configuration as part of foundation setup. Authenticated native-host delivery
requires separate approval and isolated capability evidence before activation.
Synthetic subprocess tests do not establish that capability.

For a separately authorized isolated assessment, use exact absolute
interpreter/script paths and a worktree-root/repository/palace/wing mapping.
Repository keys normalize to lowercase; wing spelling is preserved. The state
root must already be private (0700), outside the palace and repository; the
config must be private (0600). Shared/symlinked state or configuration is
refused; normal virtualenv interpreter symlinks are supported. Never concatenate
the example into an existing hook object or remove ordinary memory hooks.

The optional session-store path is ingestion input, not an operational
dependency for guidance/bookkeeping. Its configured absolute spelling remains
stable when the file or parent directory disappears. Before drafting, existing
components are checked for unsafe ownership, writable ancestors and symlinks;
an existing store must be private, regular and singly linked. Missing input
goes unchanged to core's pending-evidence/captured-source fallback.

Only exact search names `mempalace_search` and `mempalace-mempalace_search` are
recognized. Unknown wrappers or semantically failed search results abstain.
Pre-tool bookkeeping preserves start-generation identity; post-tool output
offers a bounded private-packet pointer, not an acknowledgment or replacement
search result. Stop/precompact can prepare a nonpublishing draft without forcing
another turn. Shared or ambiguous worker identity latches automatic delivery
off; a new prompt does not prove older workers stopped. Manual commands remain
available.

Errors use `[procedural-context] unavailable:`; unavailable advice is not
healthy empty guidance. To stop optional behavior, retain `mode: "off"` or remove
only its separately installed registration. Keep palace source/event records
and adverse history. Local transport receipts are not durable learning,
application reports or task state.

### Manual delivery and recovery without automatic registration

Ordinary recall and its before-decision context view work in untracked sessions,
with advice off, or when this adapter is unavailable. No task service or host
certification is needed for that path. Use the existing explicit `task-guidance`
and `use-check` commands only under current target consent; an emitted pointer
is an offer to read, not reading, intent, action or helpfulness.

After reading full conditions/exceptions and checking applicability, acknowledge
intent visibly. Recheck independently current identity/context/permissions and
sources immediately before each advised action if any tool/event/time intervened.
Task/constraint/repository/actor changes and compaction require a refreshed
context and packet, never adoption of another actor's packet or receipt.
Unknown procedural identity withholds procedures, not ordinary work.

This manual protocol does not certify native delivery or pinned cache slots.
Cached epochs cannot grant permission or defer revocation. Separately consented
historical receipts are optional; unwritten acknowledgment remains valid. An
uncertain receipt append retries the exact immutable artifact, and Stop does
not attribute helpfulness. The examples remain off and unregistered.
