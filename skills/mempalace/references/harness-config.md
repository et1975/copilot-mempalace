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
`mempalace-mcp`. Install its package/environment separately and explicitly
initialize/serve its configuration; see the repository's `sidecar/README.md`.

Configure a Streamable HTTP server named `mempalace-tasks` with the exact numeric
loopback endpoint, normally `http://127.0.0.1:8766/mcp`, and a securely supplied
Bearer header from its private service-token file. Use the harness's supported
credential/configuration mechanism; do not commit tokens or paste them into
tasks. Copilot CLI exposes its MCP configuration manager through `/mcp`.

All sessions share that one running service. Do not configure a new stateful
sidecar process per agent. The workflow agent's tool selectors assume
`mempalace-tasks` and `mempalace` aliases; keep those names aligned with actual
registrations. MCP connectivity alone does not supply a native fleet dispatcher
or host heartbeat/recovery adapter.
