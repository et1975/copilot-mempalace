# MemPalace-first reflex

Two hard rules every turn. Full details in copilot-instructions.md "Memory (MemPalace)".

## Read first
Before any of these, call `mempalace_search` once:
- `fetch_webpage`, `open_browser_page`, `read_page`, `github_repo`, `github_text_search`
- `semantic_search`, second-or-later `grep_search`/`file_search` on same topic, `Explore` subagents
- Broad terminal probes (`find`, `grep -r`, `ls -R`, `locate`, package-manager queries)

If palace answers, skip the external call. Skip the palace call only for: pure syntax Q&A, single trivial edits, or user said "don't check memory".

## Save every new fact
Triggers (any one → save before ending turn): verified project fact, debug root cause + fix, decision with rationale, user preference, workflow that worked after friction, gotcha/edge case, cross-project link, atomic `X relation Y` triple.

Flow: `mempalace_check_duplicate` → `mempalace_add_drawer` (+ `mempalace_kg_add` if atomic) → one-line confirmation.

## End-of-turn (non-trivial turns)
One-line `mempalace_diary_write`:
```
session: <topic>
recalled: <query> -> <N hits, useful?>   (or "none — lapse")
saved: <wing/room, title>   (or "none")
```
