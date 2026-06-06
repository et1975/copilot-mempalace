---
name: mempalace
description: Use when the user mentions save/search/remember/palace/wings/rooms/drawers/mining/tunnels/diary/KG/`/mempalace:*`; when starting non-trivial work on a project, library, or topic that may have prior context; when about to invoke external fact-finding (web fetch, github search, semantic_search, Explore subagent, broad terminal probes) without recent recall; or when wrapping up a session that produced a verified project fact, debugging root cause, decision rationale, or workflow lesson worth keeping.
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

Different harnesses register the same server binary in different files. Full table in [references/harness-config.md](references/harness-config.md).

## MCP tools

The MCP server exposes ~30 tools prefixed `mempalace_*`, grouped as read / write / tunnels / knowledge graph / diary / maintenance. Full catalog in [references/mcp-tools.md](references/mcp-tools.md). The current toolset is discoverable via MCP `list-tools`.

## When to use this skill — proactive vs reactive

### Proactive (no explicit request, agent's own initiative)

Two hard rules; matched to the global instructions in `~/.copilot/copilot-instructions.md`.

**Rule 1 — Recall before external fact-finding.** Call `mempalace_search` once (concise query, `wing` filter if obvious) **before** invoking any of:

- Web / external: `fetch_webpage`, `open_browser_page`, `read_page`, `github_repo`, `github_text_search`
- Workspace exploration past a single targeted lookup: `semantic_search`, a second-or-later `grep_search` / `file_search` on the same topic, `Explore` or similar subagents
- Broad terminal probes: `find`, `grep -r`, `ls -R`, `locate`, package-manager queries

If hits answer the question, use them and skip the external call. If hits are partial, proceed with the external tool and note which gap you're filling.

Skip recall only for: pure syntax / language Q&A with no project context, a single trivial edit to a known file, or when the user said "don't check memory".

**Audit hook coverage.** The `palace-reflex.py` hook audits a subset of the rule and injects a reminder when it sees a violation: web/github tools, `semantic_search`, `Explore` subagents, and `run_in_terminal` commands matching broad-probe patterns (`find ./…`, `grep -r/-R`, `ls -*R`, `locate`, `(apt-cache|brew|npm|pip|cargo|gem) search`). The rest — second-or-later `grep_search`/`file_search` on the same topic, ad-hoc shell probes outside those patterns — is on the honor system.

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
3. Call `mempalace_search(query, wing?, room?, max_distance?)`.
4. Present results with **wing → room → drawer** attribution and similarity scores; group by room.
5. Offer follow-ups: drill deeper, `mempalace_traverse` for related rooms, `mempalace_find_tunnels` for cross-domain links.

**Query craft — what actually moves recall quality:**

- **Name the entity, not the category.** Use terms that live in the drawers: file paths (`AttributorBolt.fs`), symbols (`UMXArr.tag`), error strings, version pins. Category words ("skill", "fact", "memory", "drawer", "pattern") match the high-frequency noise wings (`.copilot` has 7000+ mined drawers) and crowd out real hits.
- **Always set `wing` when the project is obvious.** Past wins in the diary all had wing filters; misses didn't. Filtering halves recall noise for free.
- **Tighten `max_distance` for specific lookups.** Default 1.5 is permissive. Use `~0.5` when asking "is there a drawer about X"; leave it open for exploratory "what do I have on this area" queries.
- **Keep queries ≤ ~8 tokens, no stop-words.** Embeddings dilute fast. Three named terms beat a sentence.
- **If recall fails, don't reword and retry blindly** — check the taxonomy (`list_wings` / `list_rooms`) or fall through to the external tool and save the gap to the diary.

**Drawer hygiene (compounds on every future search):** lead the drawer with a one-line title-like sentence using the searchable terms (entity, file path, error string). Avoid pasting long boilerplate (license headers, full markdown sections) — that's how generic docs files become the noise champion in unfiltered searches.

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
- **Atomic** — fits as subject/predicate/object (e.g., `nextjs depends_on react`)
- **Queryable as structured data** — you'd want to ask "what does X depend on?" or "list everything authored by Y"
- **Invalidatable** — the relationship can become false; KG supports invalidation with `kg_invalidate` (timeline preserved)

Use a drawer when the memory is:
- Prose/code/markdown narrative
- Multiple facts bundled together
- Rationale that wouldn't reduce to a triple cleanly

Hybrid pattern: add a drawer for the full reasoning, then a few triples for the structured edges so KG queries can find the drawer.

### Tunnels — cross-wing links

Tunnels connect a room in one wing to a room in another wing (e.g., `react/hooks → preact/general` because the new project borrowed an idea from the old). Create them sparingly via `mempalace_create_tunnel` after noticing a real cross-project relationship. They're navigation aids, not search results.

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

ChromaDB quarantines stale on-disk HNSW segments after non-graceful shutdowns and rebuilds in memory; search keeps working. Symptoms, non-destructive cleanup, and full-rebuild recovery in [references/hnsw-recovery.md](references/hnsw-recovery.md).

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
