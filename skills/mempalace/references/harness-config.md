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
