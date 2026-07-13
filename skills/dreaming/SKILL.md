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

## Execution model — keep it unattended

A dream is meant to run **unattended**: the only judgement calls are *semantic*
(synthesise a merge, keep/invalidate a fact, surface/skip a pattern,
prune/keep). The mechanical phases (harvest, adopt, verify) must **not** turn
into a stream of per-phase approval prompts.

- **Dispatch the dream as a background subagent** (`task` tool). The subagent
  runs the scripts in its own context and reports back — you are not asked to
  approve each script invocation. Running the pipeline inline in an interactive
  session instead will prompt once per command; that is the wrong way to run it.
- **Collapse the mechanics to ~2 calls**, cognition in between:
  1. **Harvest everything once** with the read-only survey, dumping worklists:
     `dream_survey.py --palace <p> --worklists-dir <dir>` (all tasks × wings in
     one process — see "Fast reconnaissance" below).
  2. **Adjudicate** the dumped worklists in-context; write `decisions.json`.
  3. **Adopt + verify in one call**: `dream_adopt.py --palace <p> --decisions
     <d> --verify` (optionally `--archive-file <f>`). `--verify` re-harvests the
     same scope after adopting and prints the residual count, so you do not run a
     separate verify command. Use `--dry-run` only when a human wants to preview
     before an *attended* run.

Reserve human interaction for genuine semantic sign-off (e.g. approving a
destructive merge/prune), never for "may I run this script".


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
| 0 | Scope | you | pick task: merge (`--wing`, optional `--room`, `--tau`), contradiction (`--task contradiction`), pattern (`--task pattern`, `--wing`, `--rooms`, `--min-support`, `--source {diary,sessions,both}`), rule induction (`--task induce-rules`, `--min-support`, `--ontology-out`), or prune (`--task prune`, `--wing`, optional `--room`, `--v-min`, `--age-floor-days`) + optional `--instructions` |
| 1 | Harvest | script | merge: `dream_harvest.py --palace <p> --wing <w> --tau 0.9 --out worklist.json`; contradiction: `dream_harvest.py --palace <p> --task contradiction --out worklist.json`; pattern: `dream_harvest.py --palace <p> --task pattern --wing <w> --rooms diary --min-support 3 --out worklist.json`; rule induction: `dream_harvest.py --palace <p> --task induce-rules --min-support 2 --ontology-out <p>/ontology.json`; prune: `dream_harvest.py --palace <p> --task prune --wing <w> --room <r> --v-min 0.35 --age-floor-days 30 --out worklist.json` (READ-ONLY except ontology candidate writes for `induce-rules`) |
| 2 | Adjudicate | **you** | fill each `worklist.json` item's `decision`; save as `decisions.json` |
| 3 | Review | human/auto | diff proposed merge text vs the originals; approve a subset |
| 4 | Adopt | script | `dream_adopt.py --palace <p> --decisions decisions.json [--verify]` (merge: add merged/delete originals; contradiction: soft-invalidate stale KG facts; pattern: add surfaced lessons only; prune: archive to JSONL then delete) |
| 5 | Verify | script | pass `--verify` to Phase 4 to re-harvest the same scope in the *same* call and print the residual count (no separate command). Expect resolved merge clusters or functional contradictions to disappear. For pattern and prune, treat this as a maintenance loop. Non-empty ⇒ didn't converge or was intentionally skipped |

For an *attended* run, `dream_adopt.py --dry-run` first previews the exact
writes. For an *unattended* dream (the default), skip the separate dry-run and
adopt with `--verify` in one call — the adjudication already is the decision.

For a mempalace tool install, prefer the interpreter that owns the package:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task pattern --wing <wing> \
  --rooms diary --min-support 3 --out worklist.json
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json --verify
```

### Pattern observation source (`--source`)

By default (`--source diary`) the `pattern` task mines only diary rooms — themes
across lessons the agent chose to journal. Two other sources mine the **raw
Copilot host session store** (`~/.copilot/session-store.db`, or
`COPILOT_SESSION_STORE`) so themes can be induced from what actually happened in
past sessions — repeated user corrections, converged tool sequences, restated
preferences — even when nothing was journaled:

```bash
# raw host sessions only
"$MPY" dream_harvest.py --palace <palace> --task pattern --source sessions \
  --repository <repo-substr> --since 2026-01-01 --limit-sessions 200 \
  --min-support 2 --out worklist.json
# union of diary + raw sessions (support-counted across both by distinct session_id)
"$MPY" dream_harvest.py --palace <palace> --task pattern --source both \
  --rooms diary --repository <repo-substr> --min-support 2 --out worklist.json
```

Raw session text is stripped of injected framework boilerplate
(`<skill-context…>`, hook/system-reminder blocks) and embedded in the palace's
own space before clustering, so session and diary observations cluster together.
Support counting keys on the real host-minted `session_id`, so `--source both`
never double-counts a session that appears in both a diary entry and its raw
turns. Output volume tracks history volume: sparse or topically-diverse history
legitimately yields few themes.

Prune and merge both archive superseded/deleted records to an append-only JSONL
before the sanctioned delete; `--archive-file` sets the path for either (default
`<palace>/dream-archive.jsonl`):

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task prune --wing <wing> \
  --room <room> --v-min 0.35 --age-floor-days 30 --out worklist.json
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl --verify
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
- `pattern`: cross-session observations grouped into themes, requiring
  `--min-support` distinct sessions before the agent may surface a general lesson.
  Source is selectable with `--source {diary,sessions,both}` (default `diary`):
  `sessions`/`both` mine raw Copilot host-session turns, not just journaled diary
  entries.
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
