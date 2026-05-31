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
/Users/eugene/.local/share/uv/tools/mempalace/bin/python -m mempalace.mcp_server
```

with env `MEMPALACE_PATH=~/.mempalace`.
