---
name: dreaming
description: Use when the user wants to consolidate, deduplicate, clean up, forget, or "dream over" a mempalace palace — merging near-duplicate drawers, resolving KG contradiction/staleness candidates, inducing cross-session patterns, or pruning low-salience drawers. Also use when the user says "run a dream", "consolidate the palace", "dedupe drawers", "contradictions", "staleness", "patterns", "induce", "prune", "forget", or references the dreaming pipeline / worklist / adjudicate / adopt.
---

# Dreaming

Offline consolidation for a mempalace palace, modelled on Anthropic's Claude
"dreaming": between sessions, review the store, merge duplicates, surface KG
contradiction/staleness candidates, induce recurring cross-session lessons, and
prune stale low-salience drawers to keep it high-signal — **without** the palace
itself needing any model. Cognition lives here (in you, the agent);
mechanics live in Python scripts; storage stays in mempalace.

> **Run this in a dedicated/fresh session, never inline during feature work.**
> Current-task salience contaminates consolidation. Dispatch it as a subagent or
> run it off-hours.

## Architecture (three layers)

```
Cognition  = this skill (you)      → adjudicate the worklist: synthesise merges, judge KG candidates, induce patterns, decide prune/keep
Mechanics  = scripts/*.py          → cluster (harvest), apply (adopt), verify
Substrate  = mempalace             → passive: embeddings, search, add/delete
```

mempalace deliberately has no model. Never push judgement into it.

## The 5-phase pipeline

Scripts live in `skills/dreaming/scripts/`. Run them with a Python that can
import `mempalace` (e.g. the interpreter from `uv tool install mempalace`).
Artifacts go in the session workspace — never commit them.

> **Fast reconnaissance first.** Before hand-running per-task/per-wing
> harvests, get a whole-palace picture in one call with the read-only survey
> driver — it runs every read-only task across every wing in a single process
> and prints one aggregated report (no adopt, ever):
>
> ```bash
> MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
> "$MPY" dream_survey.py --palace <p>                 # summary of all tasks/wings
> "$MPY" dream_survey.py --palace <p> --format json --out survey.json
> "$MPY" dream_survey.py --palace <p> --tasks merge,prune --wings avs,icm_automation \
>     --worklists-dir ./wl   # also dump non-empty worklists for adjudication
> ```
>
> Use it to decide *which* task/wing is worth the full 5-phase pipeline below.
> `dream_survey.py` is read-only: `induce-rules` candidates go to a throwaway
> ontology, and it never adopts. Adjudication/adopt still use the per-task
> `dream_harvest.py` + `dream_adopt.py` flow.

| # | Phase | Who | Command / action |
|---|-------|-----|------------------|
| 0 | Scope | you | pick task: merge (`--wing`, optional `--room`, `--tau`), contradiction (`--task contradiction`), pattern (`--task pattern`, `--wing`, `--rooms`, `--min-support`), rule induction (`--task induce-rules`, `--min-support`, `--ontology-out`), or prune (`--task prune`, `--wing`, optional `--room`, `--v-min`, `--age-floor-days`) + optional `--instructions` |
| 1 | Harvest | script | merge: `dream_harvest.py --palace <p> --wing <w> --tau 0.9 --out worklist.json`; contradiction: `dream_harvest.py --palace <p> --task contradiction --out worklist.json`; pattern: `dream_harvest.py --palace <p> --task pattern --wing <w> --rooms diary --min-support 3 --out worklist.json`; rule induction: `dream_harvest.py --palace <p> --task induce-rules --min-support 2 --ontology-out <p>/ontology.json`; prune: `dream_harvest.py --palace <p> --task prune --wing <w> --room <r> --v-min 0.35 --age-floor-days 30 --out worklist.json` (READ-ONLY except ontology candidate writes for `induce-rules`) |
| 2 | Adjudicate | **you** | fill each `worklist.json` item's `decision`; save as `decisions.json` |
| 3 | Review | human/auto | diff proposed merge text vs the originals; approve a subset |
| 4 | Adopt | script | `dream_adopt.py --palace <p> --decisions decisions.json` (merge: add merged/delete originals; contradiction: soft-invalidate stale KG facts; pattern: add surfaced lessons only; prune: archive to JSONL then delete) |
| 5 | Verify | script | re-run the same harvest; expect resolved merge clusters or functional contradictions to disappear. For pattern and prune, treat this as a maintenance loop. Non-empty ⇒ didn't converge or was intentionally skipped |

Always `dream_adopt.py --dry-run` first to preview the exact writes.

For a mempalace tool install, prefer the interpreter that owns the package:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task pattern --wing <wing> \
  --rooms diary --min-support 3 --out worklist.json
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json --dry-run
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json
```

Prune uses the same interpreter and an explicit archive file:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task prune --wing <wing> \
  --room <room> --v-min 0.35 --age-floor-days 30 --out worklist.json
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl --dry-run
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl
```

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

For each `"kind": "pattern"` item, read the theme's `members[]` entries and
extract the atomic observations they share. Decide whether those observations
support a generalizable rule across at least `min_support` **distinct**
`session_id`s (`evidence.support_ids`).

- **Surface** — only after deduping against already-filed lessons and confirming
  the rule is grounded:
  ```json
  {"action": "surface", "wing": "<w>", "room": "<r>",
   "text": "<induced rule/lesson>",
   "supported_by": ["<session_id>", "..."]}
  ```
  If you omit `supported_by`, adoption falls back to `evidence.support_ids`.
- **Skip** — unsupported, too specific, already covered, or not actually
  generalizable:
  ```json
  {"action": "skip"}
  ```

Anti-proliferation discipline: never surface an unsupported generalization, and
never re-surface a lesson that already exists. Pattern is the only net-new
knowledge task; keep it high-signal.

For `--task induce-rules`, the pattern-family induction target is
`ontology.json`, not drawer text. It scans observed base KG triples for
inverse, symmetric, and transitive co-occurrence at `--min-support`, then writes
candidate ontology rules through the same `dream_harvest.py` /
`dream_ontology.py` rails.

- **Never auto-enable** — candidates are always written with `enabled: false`.
  A human must review the rationale/evidence and flip only approved rules to
  `enabled: true`.
- **Base-triples only** — induction excludes derived `*_closure` triples and
  derivation lineage so generated rules do not feed on their own closure.
- **Support threshold** — sparse KGs legitimately yield few or no candidates
  until enough observed co-occurrences accumulate.

This is rule induction for human approval, not proof of sound semantics. An
eval gate that measures task-success improvement versus drift using
LongMemEval/LoCoMo-style methodology is deferred.

For each `"kind": "prune"` item, read the drawer text and salience components
(`age_days`, `kg_degree`, `redundancy`, `negatives`, `v`). Default to **KEEP**:
omitted decisions are treated as keep, and pruning should be deliberate even
though it is archived to JSONL and reversible.

- **Prune** — only for clearly low-value, stale, redundant, or one-off drawers:
  ```json
  {"action": "prune"}
  ```
- **Keep** — anything uncertain or still useful:
  ```json
  {"action": "keep"}
  ```

Never prune drawers that are pinned, KG-connected, recent, or the last drawer on
a topic. Consider steering `θ`: "focus on X" can lower priority elsewhere, but
"preserve X" is a fixed point. The script also re-checks protected classes at
apply time, but do not rely on the script to make the judgement for you.

## Session-stamp convention

Every diary entry this skill writes must embed the current Copilot session id so
pattern support-counting is exact:

```text
SESSION_ID:<current-copilot-session-guid>
session: <brief topic>
recalled: <query used> -> <N hits, useful? yes/no/partial>
saved: <wing/room, one-line drawer summary>
```

`SESSION_ID:<guid>` is parsed from diary text by `extract_session_id`; legacy
entries without it contribute no pattern support.

## Guarantees (why this is safe)

- **Safe writes** — nothing touches the live palace until Phase 4, and only on
  approved decisions. Harvest is read-only; a failed add never deletes; KG
  contradiction adoption sets `valid_to` instead of deleting facts; pattern
  adoption is add-only. Prune is the only destructive task, but it archives the
  full record to append-only JSONL (fsynced) before sanctioned delete, and a
  failed archive deletes nothing.
- **Provenance** — every merge carries `supersedes` (the ids it replaces).
- **Groundedness** — pattern lessons cite exact supporting session ids; empty
  support is rejected.
- **Fixpoint** — re-harvesting an adopted wing yields 0 clusters. Use it as a
  test oracle. Pattern's fixpoint is weaker: exclude adopted lessons from mining
  and dedup during adjudication so covered themes stop producing new lessons.
  Prune's fixpoint is a maintenance loop: approved candidates disappear from the
  current pass, but new low-salience drawers can appear over time.

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
- `pattern`: cross-session diary observations grouped into themes, requiring
  `--min-support` distinct stamped sessions before the agent may surface a
  general lesson.
- `induce-rules`: pattern-family ontology induction over observed base KG
  triples. It writes disabled transitive/inverse/symmetric rule candidates to
  `--ontology-out` and never auto-enables them.
- `prune` / `forget`: low-salience drawer candidates selected by a conservative
  multi-gate AND (`v < v_min`, age floor, `kg_degree == 0`, not pinned). Adoption
  archives to JSONL before deleting through the sanctioned handler.

Future `kind`s are reserved — see [`references/pipeline.md`](references/pipeline.md)
for the full contract, formal task formulations, and the mempalace API facts the
scripts rely on. A shadow palace for search-based preview (instead of file-based
review) and upstream native drawer salience (MemPalace/mempalace#1921) are
documented future enhancements.

## Tests

```
cd skills/dreaming/scripts && python3 -m unittest -v
```

The pure core (`dream_lib.py`) is dependency-free and fully unit-tested; the
mempalace-facing adapter is validated by an end-to-end run on a throwaway palace.
