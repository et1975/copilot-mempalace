---
name: mempalace
description: Local AI memory across sessions and projects (ChromaDB + SQLite under `~/.mempalace/`). Load this skill BOTH on explicit requests ("save/search/remember", "palace", "wings/rooms/drawers", "/mempalace:*") AND proactively at the start of non-trivial work that touches a project, library, or topic the user may have worked on before. The MemPalace MCP server exposes 30 `mempalace_*` tools; if they aren't present in the current harness, fall back to the `mempalace` CLI. This skill teaches the full read/save/diary/KG loop, not just one-off lookups.
---

# MemPalace

Local-first AI memory: ChromaDB + SQLite stored under `~/.mempalace/`. No cloud, no API key.

## Architecture

```
Wings (projects/people)
  └── Rooms (topics)
        └── Closets (summaries — auto-generated from drawers)
              └── Drawers (verbatim memories)

Halls   = connections between rooms in the same wing
Tunnels = connections between rooms across wings
KG      = SQLite triple store at ~/.mempalace/knowledge_graph.sqlite3
Diary   = drawers in `wing_<agent-name>` / room `diary` (one per agent identity)
```

## Detecting whether MCP is available

Before doing anything, decide which surface to use:

1. **MCP available** — if tools whose names start with `mempalace_` appear in your toolset, prefer them. This is the case in any harness wired to the MemPalace MCP server.
2. **MCP not available** — fall back to the `mempalace` CLI silently. Search, status, and mine all work via the CLI. Diary writes and KG ops have **no CLI equivalent**, so they should be skipped (not faked with workarounds) when MCP is absent.

If you're unsure, run `mempalace status` once — it works in both worlds.

## Harness-specific MCP config locations

The same server binary is registered in different files per harness:

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

## MCP Tools (30, prefixed `mempalace_*`)

The MCP server exposes 30 tools; the table below groups them. **Discoverable** via the MCP `list-tools` capability — the count and surface may grow.

### Palace — read
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

### Palace — write
| Tool | Purpose |
|---|---|
| `mempalace_add_drawer` | Add a new memory (drawer) |
| `mempalace_update_drawer` | Update an existing drawer in place |
| `mempalace_delete_drawer` | Delete a memory (drawer) |

### Tunnels (cross-wing connections)
| Tool | Purpose |
|---|---|
| `mempalace_list_tunnels` | List all tunnels |
| `mempalace_find_tunnels` | Find tunnels between two wings |
| `mempalace_create_tunnel` | Add a tunnel (room↔room across wings) |
| `mempalace_delete_tunnel` | Remove a tunnel |
| `mempalace_follow_tunnels` | Traverse tunnels from a room |
| `mempalace_traverse` | Walk halls + tunnels from a room |
| `mempalace_graph_stats` | Connectivity stats |

### Knowledge Graph (triples)
| Tool | Purpose |
|---|---|
| `mempalace_kg_query` | Query KG triples |
| `mempalace_kg_add` | Add a triple |
| `mempalace_kg_invalidate` | Invalidate a triple (soft-delete with timestamp) |
| `mempalace_kg_timeline` | View triple lifecycle history |
| `mempalace_kg_stats` | Triple/entity/relationship counts |

### Agent diary
| Tool | Purpose |
|---|---|
| `mempalace_diary_write` | Persist a diary entry (per-agent journal) |
| `mempalace_diary_read` | Read prior diary entries |

### Maintenance
| Tool | Purpose |
|---|---|
| `mempalace_sync` | Prune drawers whose source files are gitignored/deleted |
| `mempalace_reconnect` | Re-open the chroma client (after drift) |
| `mempalace_hook_settings` | Inspect/adjust auto-save hook configuration |
| `mempalace_memories_filed_away` | Recently-filed drawer summary (also under Read) |

## When to use this skill — proactive vs reactive

### Proactive (no explicit request, agent's own initiative)

Two hard rules; matched to the global instructions in `~/.copilot/copilot-instructions.md`.

**Rule 1 — Recall before external fact-finding.** Call `mempalace_search` once (concise query, `wing` filter if obvious) **before** invoking any of:

- Web / external: `fetch_webpage`, `open_browser_page`, `read_page`, `github_repo`, `github_text_search`
- Workspace exploration past a single targeted lookup: `semantic_search`, a second-or-later `grep_search` / `file_search` on the same topic, `Explore` or similar subagents
- Broad terminal probes: `find`, `grep -r`, `ls -R`, `locate`, package-manager queries

If hits answer the question, use them and skip the external call. If hits are partial, proceed with the external tool and note which gap you're filling.

Skip recall only for: pure syntax / language Q&A with no project context, a single trivial edit to a known file, or when the user said "don't check memory".

**Rule 2 — Save every new fact.** Triggers — if any fires in a turn, persist before ending:

- Verified project fact (build command, deploy step, env var, dependency, version pin, layout)
- Debugging conclusion: root cause + fix
- Decision with rationale or tradeoff
- User preference / convention not already on file
- Workflow / command sequence that worked after friction
- Gotcha, edge case, surprising behavior
- Cross-project link worth a tunnel (`mempalace_create_tunnel`)
- Atomic structured fact (`X depends_on Y`) → also `mempalace_kg_add`

Flow:
1. `mempalace_check_duplicate` with the candidate content
2. If novel → `mempalace_add_drawer` with the correct wing/room (and `mempalace_kg_add` if atomic)
3. One-line confirmation in the reply ("saved: wing X / room Y")

Skip: trivia, restatements of well-known facts, routine edits with no project-level lesson.

**Rule 3 — End-of-turn diary** (see [Diary workflow](#diary-workflow) below). Required on any non-trivial turn that triggered Rule 1 or Rule 2; note the lapse if the palace was bypassed.

**Save before context loss.** When wrapping up a long session, before likely compaction, or when the user signals end of work, persist the key takeaways.

### Reactive (explicit user request)

| Request | Action |
|---|---|
| "what do I remember about X" / "search my notes/palace for X" | `mempalace_search` |
| "save this", "remember this", "file this away" | `check_duplicate` → `add_drawer` |
| "what projects have I worked on", "list my wings" | `list_wings` / `list_rooms` / `get_taxonomy` |
| "how are X and Y connected" | `find_tunnels` / `traverse` / `kg_query` |
| "/mempalace:init / :search / :mine / :status / :help" | run workflow below |
| "memory checkup" / "palace checkup" | see [Health checkup](#health-checkup) |

## Workflows

Canonical instruction bodies are served by the CLI for the freshest version:

```bash
mempalace instructions <init|search|mine|status|help>
```

### Search (most common)
1. Parse query → extract wing/room hints and semantic terms.
2. If unsure of taxonomy, call `mempalace_list_wings` / `mempalace_list_rooms` / `mempalace_get_taxonomy` first.
3. Call `mempalace_search(query, wing?, room?)`.
4. Present results with **wing → room → drawer** attribution and similarity scores; group by room.
5. Offer follow-ups: drill deeper, `mempalace_traverse` for related rooms, `mempalace_find_tunnels` for cross-domain links.

### Add a drawer
1. `mempalace_check_duplicate` with the candidate content.
2. If novel → `mempalace_add_drawer` with wing/room and content.
3. Briefly confirm placement (one line: "saved to wing X / room Y").

### Diary workflow

The diary is the audit trail of whether memory is actually helping. Without diary entries, recall gaps are invisible and we can't tell if saves get reused.

**When:** at the end of any session that performed a recall or a save. Skip for trivial turns (no recall, no save).

**Format options:**

1. **Plain-English audit schema** (recommended for human-readable diaries):
   ```
   session: <brief topic>
   recalled: <query used> -> <N hits, useful? yes/no/partial>
   saved: <wing/room, drawer title>   (or "none")
   notes: <one-sentence assessment>   (optional)
   ```
2. **AAAK compressed dialect** (what the MCP server description encourages — example: `SESSION:2026-04-04|built.palace.graph+diary.tools|ALC.req:agent.diaries.in.aaak|★★★`). Retrieve the spec with `mempalace_get_aaak_spec`. AAAK is denser but degrades semantic embedding quality, so prefer plain English unless the user has explicitly opted in to AAAK.

**Where it goes:** wing `wing_<agent-name-lowercased>`, room `diary`. Different agent identities (claude, copilot-cli, copilot-planning-agent) each get their own diary wing.

**Reading later:** `mempalace_diary_read` accepts an agent name (case-insensitive). Useful during health checkups.

### KG workflow — when to add a triple vs. a drawer

Drawers are paragraph-sized memories that get embedded for semantic search. Triples are structured `(subject, predicate, object)` facts that get joined / aggregated.

Add a triple via `mempalace_kg_add` when the fact is:
- **Atomic** — fits as subject/predicate/object (e.g., `voxexmachina depends_on FsShelter`)
- **Queryable as structured data** — you'd want to ask "what does X depend on?" or "list everything authored by Y"
- **Invalidatable** — the relationship can become false; KG supports invalidation with `kg_invalidate` (timeline preserved)

Use a drawer when the memory is:
- Prose/code/markdown narrative
- Multiple facts bundled together
- Rationale that wouldn't reduce to a triple cleanly

Hybrid pattern: add a drawer for the full reasoning, then a few triples for the structured edges so KG queries can find the drawer.

### Tunnels — cross-wing links

Tunnels connect a room in one wing to a room in another wing (e.g., `voxexmachina/design → voxnovel/general` because the new project was inspired by the old). Create them sparingly via `mempalace_create_tunnel` after noticing a real cross-project relationship. They're navigation aids, not search results.

### Status
1. `mempalace_status` → wings/rooms/drawers counts.
2. `mempalace_kg_stats` + `mempalace_graph_stats` for KG/connectivity.
3. Suggest one next action: empty → mine; data but no KG → add triples; healthy → search.

### Health checkup

When the user asks for a "memory checkup" / "palace checkup":

1. `mempalace_status` + `mempalace_kg_stats` + `mempalace_graph_stats` → growth & connectivity snapshot.
2. `mempalace_diary_read` (recent entries) → scan for:
   - searches that returned no useful hits (**recall gaps**)
   - repeated saves on the same topic (**consolidation candidates**)
   - sessions that should have had memory activity but had none (**proactive-use lapses**)
3. Inspect `~/.mempalace/wal/write_log.jsonl` for write op breakdown (add_drawer / update_drawer / diary_write / kg_add).
4. Check `~/.mempalace/palace/` for `.drift-*` directories (HNSW quarantines) — see [HNSW drift](#hnsw-drift-recovery).
5. Report concisely: deltas, top recall gaps, suggested consolidations. Offer to act on each.

### Mine (CLI-only — no MCP tool)
| Mode | Command |
|---|---|
| Project files | `mempalace mine <dir>` |
| Conversation exports | `mempalace mine <dir> --mode convos` |
| Auto-classify | add `--extract general` |
| Pre-split mega files | `mempalace split <dir>` first |
| Tag wing explicitly | `--wing <name>` |
| Prune deleted/gitignored sources | `mempalace sync` |

**Mining hygiene — what NOT to mine:**
- `~/.copilot/` — session-state, logs, command history; transient, will balloon the palace with noise (already 7000+ noise drawers in the `.copilot` wing).
- Cache/build dirs (`node_modules`, `target`, `dist`, `.venv`).
- Any directory the user hasn't committed to keeping; mined drawers persist past source deletion (use `mempalace sync` to clean up, but better not to mine in the first place).
- Generated/derived files (lockfiles, minified bundles, generated docs).

Good targets: source code roots, design notes, decision logs, conversation exports from coherent projects.

### Init (CLI-only)
Walk: Python ≥3.9 → `uv tool install mempalace` (or `pip`) → `mempalace init --yes <dir>` → register MCP for each harness (`copilot mcp add …`, edit VS Code `mcp.json`, etc.) → `mempalace status` verification. See `mempalace instructions init` for the full step-by-step with error handling.

## HNSW drift / recovery

ChromaDB persists HNSW indexes lazily. If the server restarts while sqlite has rows newer than the on-disk HNSW segment, it **quarantines** the stale segment (renaming to `<id>.drift-YYYYMMDD-HHMMSS`) and rebuilds the index in memory. Search continues to work via the in-memory index; only the on-disk copy is invalidated.

**Symptoms:**
- `mempalace status` prints "Quarantined corrupt HNSW segment …" at startup
- `mempalace repair-status` shows `hnsw count: (no flushed metadata yet)` and `status: UNKNOWN`
- Multiple `*.drift-*` directories accumulate under `~/.mempalace/palace/`

**Recovery (non-destructive):**
1. `mempalace repair --mode max-seq-id` — un-poisons any legacy 0.6.x corrupted rows. No-op if clean.
2. `rm -rf ~/.mempalace/palace/*.drift-*` — orphaned quarantined copies, safe to delete after confirming the active (non-drift) segment exists for the same id.
3. Restart any harness whose MCP server is still pointing at the old in-memory index.

**Recovery (full rebuild):**
- `mempalace repair --mode from-sqlite --archive-existing --backup` rebuilds HNSW from sqlite rows. Stop the MCP server first (kill the `mempalace.mcp_server` PID and let the harness restart it).

**Root cause:** usually a non-graceful shutdown of the MCP server (kill -9, host sleep, OOM). Mitigate by giving the server time to flush before harness reload.

## Auto-save hooks (where most writes actually come from)

MemPalace ships Stop and PreCompact hooks (`mempalace hook run --hook stop|precompact --harness <name>`) that auto-save memories every ~15 human messages and on context-compaction. These are **harness-side** — invoked by Claude Code / Copilot Chat / etc., not by this skill or by tool calls inside a conversation.

**Verifying hooks are wired:** call `mempalace_hook_settings` for the current configuration. If recent sessions completed but the WAL (`~/.mempalace/wal/write_log.jsonl`) shows no `add_drawer` ops, the hook is probably not registered in the harness.

**Implication:** if you want a learning preserved with high confidence, write it yourself via `mempalace_add_drawer` — don't rely on the hook to catch it. The hook is best-effort end-of-session capture.

## Output style

- Concise. Numbers and short labels beat paragraphs.
- Always attribute results to **wing / room / drawer**.
- Surface memory operations as one-line mentions, not narration: "checked the palace — 2 hits in wing X" or "saved to wing X / room Y". Don't restate the search query unless it's relevant.
- If MCP is unavailable, fall back to the CLI silently. Mention the fallback once if it matters; don't repeat per command.

## Routing: MemPalace vs Copilot's built-in `/memories/`

| Content | Destination | Why |
|---|---|---|
| Short, universal instincts that influence every turn (e.g. "always check for symlinks") | Copilot user memory `/memories/` | Auto-loaded into every conversation |
| In-progress plans, transient todos | Copilot session memory `/memories/session/` | Ephemeral |
| Conventions / build commands / verified facts for the **current** repo | Copilot repo memory `/memories/repo/` | Auto-loaded only in that repo |
| Project-specific decisions, conversation history, code rationale, anything bulky, anything only relevant in some contexts | **MemPalace drawer** | Embedding-searchable, scales, recalled on demand |
| Atomic structured facts ("X depends on Y") that you'll want to aggregate | **MemPalace KG triple** | Joinable, invalidatable, timelined |

Heuristic: *if it must fire automatically every turn, it goes in Copilot memory; if it only matters when the topic comes up, it goes in MemPalace.* When the two need to point at each other, cross-reference explicitly.

## Related slash commands (Claude Code harness)

`/mempalace:init` · `/mempalace:search` · `/mempalace:mine` · `/mempalace:status` · `/mempalace:help`

In VS Code or Copilot CLI, invoke the equivalent MCP tools directly.
