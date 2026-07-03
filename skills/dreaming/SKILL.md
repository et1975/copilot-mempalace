---
name: dreaming
description: Use when the user wants to consolidate, deduplicate, clean up, or "dream over" a mempalace palace — merging near-duplicate drawers or resolving KG contradiction/staleness candidates between sessions. Also use when the user says "run a dream", "consolidate the palace", "dedupe drawers", "contradictions", "staleness", or references the dreaming pipeline / worklist / adjudicate / adopt.
---

# Dreaming

Offline consolidation for a mempalace palace, modelled on Anthropic's Claude
"dreaming": between sessions, review the store, merge duplicates, surface KG
contradiction/staleness candidates, and keep it high-signal — **without** the
palace itself needing any model. Cognition lives here (in you, the agent);
mechanics live in Python scripts; storage stays in mempalace.

> **Run this in a dedicated/fresh session, never inline during feature work.**
> Current-task salience contaminates consolidation. Dispatch it as a subagent or
> run it off-hours.

## Architecture (three layers)

```
Cognition  = this skill (you)      → adjudicate the worklist: synthesise merges, judge KG candidates
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
| 0 | Scope | you | pick task: merge (`--wing`, optional `--room`, `--tau`) or contradiction (`--task contradiction`) + optional `--instructions` |
| 1 | Harvest | script | merge: `dream_harvest.py --palace <p> --wing <w> --tau 0.9 --out worklist.json`; contradiction: `dream_harvest.py --palace <p> --task contradiction --out worklist.json` (READ-ONLY) |
| 2 | Adjudicate | **you** | fill each `worklist.json` item's `decision`; save as `decisions.json` |
| 3 | Review | human/auto | diff proposed merge text vs the originals; approve a subset |
| 4 | Adopt | script | `dream_adopt.py --palace <p> --decisions decisions.json` (merge: add merged/delete originals; contradiction: soft-invalidate stale KG facts) |
| 5 | Verify | script | re-run the same harvest; expect resolved merge clusters or functional contradictions to disappear. Non-empty ⇒ didn't converge or was intentionally skipped |

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

For each `"kind": "contradiction"` item, read the `(subject, predicate)` and
the distinct active `candidates[].object` values. The group is only a structural
candidate: first decide whether the predicate is functional or legitimately
multi-valued.

- **Functional / stale contradiction** — keep the newest or most authoritative
  object and invalidate the rest:
  ```json
  {"action": "invalidate", "keep": "<object>", "invalidate": ["<stale object>"]}
  ```
  If you omit `invalidate`, adoption invalidates every candidate except `keep`.
- **Legitimately multi-valued or uncertain** — do not revise the KG:
  ```json
  {"action": "skip"}
  ```

Examples: `lives_in` and `status_is` are usually functional; `knows` and
`depends_on` may be multi-valued. Use the worklist's `newest_object` as a hint,
not as an automatic decision.

## Guarantees (why this is safe)

- **Non-destructive** — nothing touches the live palace until Phase 4, and only
  on approved decisions. Harvest is read-only; a failed add never deletes; KG
  contradiction adoption sets `valid_to` instead of deleting facts.
- **Provenance** — every merge carries `supersedes` (the ids it replaces).
- **Fixpoint** — re-harvesting an adopted wing yields 0 clusters. Use it as a
  test oracle.

## Choosing `tau`

`tau` is the cosine threshold for "near-duplicate". Start at `0.9`. Lower it
(e.g. `0.85`) to catch looser paraphrases at the risk of over-merging; raise it
(`0.95`) to merge only near-identical drawers. Inspect the worklist's
`evidence.pair_sims` before adopting.

## Task scope

Implemented tasks:

- `merge` (default): near-duplicate logical drawers in a wing/room.
- `contradiction`: palace-wide active KG triples sharing `(subject, predicate)`
  with 2+ distinct objects. `--wing`, `--room`, and `--tau` do not apply because
  the KG is global to the palace.

Future `kind`s (`pattern`, `prune`) are reserved — see
[`references/pipeline.md`](references/pipeline.md) for the full contract,
formal task formulations, and the mempalace API facts the scripts rely on. A
shadow palace for search-based preview (instead of file-based review) is a
documented future enhancement.

## Tests

```
cd skills/dreaming/scripts && python3 -m unittest -v
```

The pure core (`dream_lib.py`) is dependency-free and fully unit-tested; the
mempalace-facing adapter is validated by an end-to-end run on a throwaway palace.
