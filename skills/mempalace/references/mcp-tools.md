# MemPalace MCP tools

The MCP server exposes ~30 tools, all prefixed `mempalace_*`. Discoverable via the MCP `list-tools` capability — the count and surface may grow.

## Palace — read

| Tool | Purpose |
|---|---|
| `mempalace_status` | Palace status & stats (wings/rooms/drawers) |
| `mempalace_list_wings` | List all wings |
| `mempalace_list_rooms` | List rooms in a wing |
| `mempalace_list_drawers` | List drawers in a wing/room |
| `mempalace_get_drawer` | Fetch a single drawer by id |
| `mempalace_get_taxonomy` | Full wing/room/drawer tree |
| `mempalace_search` | Semantic search (args: `query`, optional `wing`, `room`) |
| `mempalace_check_duplicate` | Check whether a memory already exists before adding |
| `mempalace_memories_filed_away` | Recently-filed drawer summary |
| `mempalace_get_aaak_spec` | Retrieve the AAAK compression dialect spec |

## Palace — write

| Tool | Purpose |
|---|---|
| `mempalace_add_drawer` | Add a new memory (drawer) |
| `mempalace_update_drawer` | Update an existing drawer in place |
| `mempalace_delete_drawer` | Delete a memory (drawer) |

## Tunnels (cross-wing connections)

| Tool | Purpose |
|---|---|
| `mempalace_list_tunnels` | List all tunnels |
| `mempalace_find_tunnels` | Find tunnels between two wings |
| `mempalace_create_tunnel` | Add a tunnel (room↔room across wings) |
| `mempalace_delete_tunnel` | Remove a tunnel |
| `mempalace_follow_tunnels` | Traverse tunnels from a room |
| `mempalace_traverse` | Walk halls + tunnels from a room |
| `mempalace_graph_stats` | Connectivity stats |

## Knowledge Graph (triples)

| Tool | Purpose |
|---|---|
| `mempalace_kg_query` | Query KG triples |
| `mempalace_kg_add` | Add a triple |
| `mempalace_kg_invalidate` | Invalidate a triple (soft-delete with timestamp) |
| `mempalace_kg_timeline` | View triple lifecycle history |
| `mempalace_kg_stats` | Triple/entity/relationship counts |

## Agent diary

| Tool | Purpose |
|---|---|
| `mempalace_diary_write` | Persist a diary entry (per-agent journal) |
| `mempalace_diary_read` | Read prior diary entries |

## Maintenance

| Tool | Purpose |
|---|---|
| `mempalace_sync` | Prune drawers whose source files are gitignored/deleted |
| `mempalace_reconnect` | Re-open the chroma client (after drift) |
| `mempalace_hook_settings` | Inspect/adjust auto-save hook configuration |
