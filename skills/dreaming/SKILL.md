---
name: dreaming
description: Use when the user wants to consolidate, deduplicate, clean up, or "dream over" a mempalace palace — merging near-duplicate drawers between sessions. Also use when the user says "run a dream", "consolidate the palace", "dedupe drawers", or references the dreaming pipeline / worklist / adjudicate / adopt.
---

# Dreaming

Offline consolidation for a mempalace palace, modelled on Anthropic's Claude
"dreaming": between sessions, review the store, merge duplicates, and keep it
high-signal — **without** the palace itself needing any model. Cognition lives
here (in you, the agent); mechanics live in Python scripts; storage stays in
mempalace.

> **Run this in a dedicated/fresh session, never inline during feature work.**
> Current-task salience contaminates consolidation. Dispatch it as a subagent or
> run it off-hours.

## Architecture (three layers)

```
Cognition  = this skill (you)      → adjudicate the worklist, synthesise merges
Mechanics  = scripts/*.py          → cluster (harvest), apply (adopt), verify
Substrate  = mempalace             → passive: embeddings, search, add/delete
```

mempalace deliberately has no model. Never push judgement into it.

## The 5-phase pipeline

Scripts live in `skills/dreaming/scripts/`. Run them with a Python that can
import `mempalace` (e.g. the interpreter from `uv tool install mempalace`).
Artifacts go in the session workspace — never commit them.

| # | Phase | Who | Command / action |
|---|-------|-----|------------------|
| 0 | Scope | you | pick a `--wing` (and optional `--room`) + `--tau` + optional `--instructions` |
| 1 | Harvest | script | `dream_harvest.py --palace <p> --wing <w> --tau 0.9 --out worklist.json` (READ-ONLY) |
| 2 | Adjudicate | **you** | fill each `worklist.json` item's `decision`; save as `decisions.json` |
| 3 | Review | human/auto | diff proposed merge text vs the originals; approve a subset |
| 4 | Adopt | script | `dream_adopt.py --palace <p> --decisions decisions.json` (add merged, delete originals) |
| 5 | Verify | script | re-run harvest; expect **0 clusters** (the fixpoint). Non-empty ⇒ didn't converge |

Always `dream_adopt.py --dry-run` first to preview the exact adds/deletes.

## Phase 2 — how you adjudicate (the cognitive step)

For each `"kind": "merge"` item in `worklist.json`, read the `members[].text`
(near-duplicate drawers, cosine ≥ `tau`) and set `item["decision"]`:

- **Merge** — synthesise ONE drawer that preserves every distinct fact across
  the members (soundness: lose no atomic fact), drop the redundancy:
  ```json
  {"action": "merge", "wing": "<w>", "room": "<r>",
   "text": "<your synthesised, deduplicated drawer>",
   "supersedes": ["<all member physical ids>"]}
  ```
  Default `wing`/`room`/`supersedes` come from the item if you omit them.
- **Skip** — the members only *look* similar but shouldn't be merged:
  ```json
  {"action": "skip"}
  ```

Honour the worklist's `instructions` if present (focus areas, what to preserve,
what to drop). Do **not** invent facts not present in the members.

## Guarantees (why this is safe)

- **Non-destructive** — nothing touches the live palace until Phase 4, and only
  on approved decisions. Harvest is read-only; a failed add never deletes.
- **Provenance** — every merge carries `supersedes` (the ids it replaces).
- **Fixpoint** — re-harvesting an adopted wing yields 0 clusters. Use it as a
  test oracle.

## Choosing `tau`

`tau` is the cosine threshold for "near-duplicate". Start at `0.9`. Lower it
(e.g. `0.85`) to catch looser paraphrases at the risk of over-merging; raise it
(`0.95`) to merge only near-identical drawers. Inspect the worklist's
`evidence.pair_sims` before adopting.

## Scope of v1

v1 implements the **merge** task only. The worklist schema reserves other
`kind`s (`contradiction`, `pattern`, `prune`) for future phases — see
[`references/pipeline.md`](references/pipeline.md) for the full contract, formal
task formulations, and the mempalace API facts the scripts rely on. A shadow
palace for search-based preview (instead of file-based review) is a documented
future enhancement.

## Tests

```
cd skills/dreaming/scripts && python3 -m unittest -v
```

The pure core (`dream_lib.py`) is dependency-free and fully unit-tested; the
mempalace-facing adapter is validated by an end-to-end run on a throwaway palace.
