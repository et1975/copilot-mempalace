# Memory (MemPalace) — memory-first reflex

The MemPalace MCP server exposes `mempalace_*` tools and is the **primary** store for the user's projects, decisions, and learnings. Treat it as the first place to look and the first place to write. Two hard rules, plus an end-of-turn check.

## Rule 1 — Read first: `mempalace_search` precedes external fact-finding

Before invoking any of the following, call `mempalace_search` once with a concise query (plus `wing` if the project is obvious):

- **Web / external:** `fetch_webpage`, `open_browser_page`, `read_page`, `github_repo`, `github_text_search`
- **Workspace exploration past a single targeted lookup:** `semantic_search`, a second-or-later `grep_search` / `file_search` on the same topic, or any `Explore` / similar subagent
- **Terminal probes of system state:** broad `find`, `grep -r`, `ls -R`, `locate`, package-manager queries

Skim hits silently. If the palace answers the question, use it and **skip the external call**. If hits are partial, proceed with the external tool but cite which gap you're filling.

Skip Rule 1 only when:
- Pure language/syntax Q&A with no project context ("what's the regex for X")
- A single trivial edit to a known file
- The user explicitly says "don't check memory"

A `PreToolUse` audit hook (`hooks/palace-reflex.json` → `palace-reflex.py`) reinforces this rule: when a trigger tool fires without a prior `mempalace_search` in the recent session window, the hook injects a one-line reminder via `additionalContext`. The hook never blocks — it's an audit trail, not a gate.

## Rule 2 — Write on every new fact

A "new fact" is anything you'd want to recall next time the topic comes up. Concrete triggers — if **any** fires in a turn, save before ending the turn:

- Verified project fact: build command, deploy step, env var, dependency, version pin, file layout
- Debugging conclusion: root cause + fix (not the chase, the answer)
- Decision with rationale or tradeoff (chosen approach + why over the alternative)
- User preference / convention surfaced this turn and not already on file
- Workflow or command sequence that worked after friction
- Gotcha, edge case, or surprising behavior just discovered
- Cross-project link worth a tunnel (`mempalace_create_tunnel`)
- Structured atomic fact (`X depends_on Y`, `X authored_by Y`) → also `mempalace_kg_add`

Flow per fact: `mempalace_check_duplicate` → if novel, `mempalace_add_drawer` with the right wing/room → one-line confirmation in the reply ("saved: wing X / room Y").

**Discover schemas before writing.** `mempalace_*` tools are deferred — their parameter schemas aren't in context until you look them up. Before the first call to any mempalace *write* tool in a session (`add_drawer`, `kg_add`, `diary_write`, …), run the harness tool-search on that exact tool name to load its live schema, then use those param names verbatim. Don't call a write tool from memory: they have non-obvious required fields and reject unknown params.

Skip only: trivia, restatements of well-known programming facts, routine code edits that taught nothing project-level.

## Rule 3 — End-of-turn checklist (non-trivial turns)

Before composing the final reply, answer silently:

1. **Did I gather facts?** If yes, was Rule 1 honored? If I skipped the palace, note it in the diary.
2. **Did I establish any fact matching a Rule 2 trigger?** If yes, is it saved? If not, save now.
3. **Recall and save both resolved?** Write a one-line `mempalace_diary_write` entry:

```
SESSION_ID: <copilot-session-id>
session: <brief topic>
recalled: <query used> -> <N hits, useful? yes/no/partial>   (or "none — see lapse")
saved: <wing/room, one-line drawer summary>   (or "none")
notes: <one-sentence assessment>   (optional)
```

The `SESSION_ID` line lets the dreaming `pattern` task count exact distinct-session support.

Skip the diary for trivial turns where Rules 1 and 2 both legitimately did not fire.

## Style

One-line mentions, not narration: "palace: 2 hits in wing X" or "saved: wing Y / room Z". Don't restate queries unless relevant. If MCP is unavailable, fall back silently to the `mempalace` CLI; skip diary/KG ops (no CLI equivalent).

## Routing: MemPalace vs Copilot's built-in memory

Copilot also exposes three native memory scopes (`/memories/`, `/memories/session/`, `/memories/repo/`). They do not overlap with MemPalace and must not be used interchangeably. Route writes as follows:

| Content | Destination | Rationale |
|---|---|---|
| Short, universal instincts that should influence every turn regardless of topic (e.g. "always check for symlinks") | Copilot **user memory** `/memories/` | Auto-loaded into context every conversation |
| In-progress plans, current task scratch, transient todos | Copilot **session memory** `/memories/session/` | Ephemeral, dies with the conversation |
| Conventions / build commands / verified facts for the **current** repo | Copilot **repo memory** `/memories/repo/` | Auto-loaded only when in that repo |
| Project-specific decisions, conversation history, code rationale, anything bulky, anything only relevant in some contexts | **MemPalace** (`mempalace_add_drawer`) | Embedding-searchable, scales, recalled on demand |

Heuristic: *if it must fire automatically every turn, it goes in Copilot memory; if it only matters when the topic comes up, it goes in MemPalace.* When the two need to point at each other, cross-reference explicitly (e.g. a Copilot memory note ending with "see palace wing `<name>` for details").

Before writing anywhere, check the destination for existing notes to avoid duplicates: `mempalace_check_duplicate` for the palace; view existing files for Copilot memory.

## Memory health checkup (on demand)

When the user asks for a "memory checkup" / "palace checkup":

1. `mempalace_status` + `mempalace_kg_stats` + `mempalace_graph_stats` → growth & connectivity snapshot.
2. `mempalace_diary_read` (recent entries) → scan for: searches that returned no useful hits (recall gaps), repeated saves on the same topic (consolidation candidates), sessions with zero activity that should have had some.
3. Report concisely: growth deltas, top recall gaps, suggested consolidations. Offer to act on each.

For deeper workflow docs (init, mine, full search/status walkthrough), invoke the `mempalace` skill.
