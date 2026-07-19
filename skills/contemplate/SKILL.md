---
name: contemplate
description: Use when the user wants deliberate, on-demand deductive reasoning over the MemPalace knowledge graph — derive, infer, reason, contemplate, "what follows from X", or "what can we conclude". Runs the shared dreaming scripts with `--task derive`.
---

# Contemplate

On-demand **read-only** reasoning for a mempalace palace. Where `dreaming` is
unattended off-hours consolidation, `contemplate` is deliberate inline
cognition: derive what follows from the active KG under explicitly-approved
rules, query for relevant past sessions, propose ontology rules, and report
knowledge gaps — **without writing KG facts or materializing drawers**.

**Scope:**

- `--task derive` — bounded deductive closure over active KG facts under
  explicitly enabled ontology rules (read-only reconnaissance; adjudication +
  materialization use the 5-phase `dream_harvest.py` / `dream_adopt.py` flow).
- `--recall` — relevance-ranked past-session reconnaissance for inline
  grounding; retrieves top-k most relevant Copilot host sessions by embedding
  cosine similarity (read-only, does not materialize anything).
- `--task gaps` — standalone read-only gap reconnaissance; ranks missing KG
  edges but does not retrieve sources or assert facts.
- `--propose` / `--enable-rule` / `--disable-rule` — ontology proposal review
  and deliberate rule toggling.

**Constructive synthesis** (distill, generalize, name_gap, connect, tension,
shared_constraint) is **not** part of contemplate. Use the dreaming skill's
`reflect` task for on-demand meditation or scheduled generative consolidation.

> **Run this when reasoning is the user's current task.**
> Do not dispatch it as an off-hours dream: this skill is allowed to run inline
> during focused work because its job is deliberate reasoning, not salience-free
> consolidation.

## Architecture (shared mechanics)

```
Cognition  = this skill (you)      → approve rules, adjudicate candidates, curate materializations
Mechanics  = dreaming/scripts/*.py → harvest closure, apply decisions, verify
Substrate  = mempalace             → active KG triples + derived lineage table
```

`contemplate` does **not** have its own scripts directory. It shares
`skills/dreaming/scripts/` and runs the `derive` task through the same
`dream_harvest.py` / `dream_adopt.py` rails.

## One-shot driver (fewer prompts)

Prefer the one-shot driver for inline reconnaissance:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p>
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p> --bootstrap
```

`dream_contemplate.py` runs the read-only derive scan in one in-process call
instead of the multi-command harvest flow, minimizing per-command approval
prompts. `--bootstrap` only writes disabled ontology rule candidates for review;
it never enables rules and never adopts derived KG facts. For a fully
unattended/zero-prompt run, dispatch the driver as a background subagent.

## Relevance recall driver (session reconnaissance)

Use `--recall` when the current reasoning task needs grounding from past Copilot
host sessions:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p> \
  --recall "<reasoning query>" [--k 5] [--repository <substr>] \
  [--since <iso>] [--limit-sessions N] [--min-similarity 0.0] \
  [--format summary|json]
```

Given a reasoning query, `--recall` retrieves the top-`k` most relevant past
Copilot host sessions from the session store, relevance-ranked by embedding
cosine similarity in the palace's own embedding space. It is read-only
reconnaissance: it surfaces session context for the agent to use as
grounding/premises while reasoning inline. It does **not** materialize anything
and does **not** run the deductive derive scan; the no-`--recall` driver remains
the read-only derive path.

Summary output is intentionally skim-friendly, for example:

```text
1. score=0.84 session=01J... repo=copilot-mempalace updated=2026-07-12 — discussed gap ranking and ontology-rule boundaries
```

### Same session substrate, opposite access pattern

- **Dreaming = mine for recurrence** — offline/aggregate, cluster-all, with a
  `min_support` gate over distinct sessions before promoting durable lessons.
- **Contemplation = query for relevance** — inline/on-demand, query-conditioned
  k-NN, with no support gate; a single relevant session (`n=1`) is a valid
  result for grounding the current question.

This is the key contrast with the `dreaming` skill's `pattern --source sessions`
task: pattern mining looks for themes that recur across `>= min_support`
distinct sessions, while `--recall` asks which sessions are most relevant to
this specific reasoning query.

## Gap reconnaissance driver (standalone read-only)

`--task gaps` is **read-only** reconnaissance for the highest-value *missing*
facts. Given the active KG and the enabled ontology rules, it reports
hypothesised edges whose addition would unblock currently-underivable `_closure`
conclusions, ranked by **DUC** (how many conclusions each gap would unblock).

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_harvest.py --palace <p> --task gaps \
  [--target-subject "<entity id or name>"] [--rules <p>/ontology.json] \
  [--max-candidates 500] --out worklist.json
```

- **Transitive-only.** A "missing premise" only exists for the transitive family
  (the sole multi-premise rule); inverse/symmetric rules never yield gaps.
- **No hallucinated entities.** Both endpoints of a proposed gap edge must already
  exist in the KG.
- **Goal-directed (optional).** `--target-subject` restricts gaps to conclusions
  about that subject; without it, gaps are ranked across the whole KG.
- **Empty ontology ⇒ zero gaps**, exactly like `--task derive`.

Each `gap` item carries a `hypothesis` (the missing edge), the `rule`, and
`evidence.duc` + `evidence.unblocks` (the conclusions it would enable). This is
**reconnaissance only**: it writes nothing to the KG, retrieves no sources, and
asserts no facts. It just tells the agent *which* missing fact is worth
investigating separately.

## Focused inquiry (instruction-only deep-dive)

When the user wants to **focus** contemplation on a topic/question and go
**deeper than a single shallow search**, do not reach for a script — the depth
comes from *you issuing better queries*, not from machinery. This is a
cognition-only protocol: run it inline with `mempalace_search`, then use the
dreaming skill's `reflect` task to promote any load-bearing findings.

A by-hand benchmark (structured inquiry vs a plain-search baseline) showed the
value is created almost entirely by **agent-driven query expansion** —
world-knowledge concepts and one contrastive query surface load-bearing drawers
that a direct search misses and can *change the conclusion*. Multi-hop neighbor
traversal, KG bridging, and a similarity floor added no measured value on this
palace (the KG is too sparse, ~40 triples) and are deliberately **not** part of
this protocol.

Steps:

1. **Direct pass.** `mempalace_search` the focus as stated; skim the top hits.
2. **Conceptual expansion (the depth mechanism).** Issue 2–4 more searches for
   *adjacent/implied concepts you know are related but that the focus wording
   does not contain* — bridge the vocabulary gap using your own knowledge, not
   the focus's words.
3. **One contrastive query.** Search explicitly for a counterexample,
   contradiction, or constraint on the focus ("when is X false / when did the
   opposite hold"). This is what flips a one-sided finding into a real tension.
4. **Evidence-led follow-up (optional).** If one drawer is clearly pivotal,
   run one query seeded by *its* specific vocabulary. Do not fan out
   automatically.
5. **Dedup + brief.** Deduplicate drawer IDs in context and write a short,
   **quote-grounded** brief: findings, tensions, connections, open questions —
   every asserted finding must carry an exact drawer quote. Label the brief a
   set of hypotheses, not conclusions. If grounding is thin, **abstain** rather
   than pad it.
6. **Promote the load-bearing findings.** For any finding that names a changed
   decision or a falsifiable prediction and *requires ≥2 distinct drawers*,
   materialize it via the dreaming skill's `reflect` task (invoke that skill
   and follow its `--task reflect` flow: harvest → adjudicate → adopt). Only
   gate-passing insights get written to the palace.

The brief itself is a **session artifact** — keep it in context or the session
workspace; do not materialize it as a drawer. Only gate-passing insights get
written to the palace.

## The 5-phase pipeline

Artifacts go in the session workspace — never commit them. Use the interpreter
that owns the `mempalace` package:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_harvest.py --task derive \
  --palace <p> --rules <p>/ontology.json --out worklist.json
```

| # | Phase | Who | Command / action |
|---|-------|-----|------------------|
| 1 | Harvest | script | Read active KG triples and `ontology.json`; compute bounded closure; write `worklist.json`. **Read-only**: writes nothing to the palace |
| 2 | Adjudicate | **you** | For each `derive` item, choose `materialize`, `skip`, or `reject_rule` |
| 3 | Approve rules | human/config | If a rule is wrong, do not trust the candidate. `reject_rule` suppresses this worklist under the current ontology; edit `ontology.json` for the durable fix |
| 4 | Adopt | script | Materialize approved facts and lineage, append skip-markers for skips/rejected rules |
| 5 | Verify | script | Re-harvest under the same rules; with approved materializations and skip-markers, expect an operational fixpoint |

Adopt and verify:

```bash
"$MPY" skills/dreaming/scripts/dream_adopt.py --task derive \
  --palace <p> --decisions decisions.json --verify
```

## Phase 2 — how you adjudicate

Every candidate is a proposed derived KG triple with a proof:

- **Materialize** — only when the rule is approved for this palace and the
  shortcut is useful enough to store:
  ```json
  {"action": "materialize"}
  ```
- **Skip** — the conclusion is valid under the approved rule but not worth
  storing, or it is too noisy:
  ```json
  {"action": "skip", "reason": "<why>"}
  ```
- **Reject rule** — the rule itself is not valid for this predicate/domain:
  ```json
  {"action": "reject_rule", "reason": "<why>"}
  ```

Rule rejection is two-step discipline: the current adoption writes skip-markers
so verify can converge now; the durable fix is to disable or edit the rule in
`<palace>/ontology.json`, which changes `ontology_version`.

## Rule-approval discipline

The ontology problem is the crux. The KG stores predicate names, not predicate
semantics.

- **Never infer** that a predicate is transitive, symmetric, or inverse-bearing
  from its name. `depends_on` might be transitive in one palace and not in
  another.
- Harvest applies only enabled rules from the explicit ontology config. Empty
  config ⇒ zero candidates.
- Every materialization is human/agent-approved through `decisions.json`.
- The honest claim is **rule-based materialization, not soundness**. Conclusions
  are sound only relative to approved rules and MemPalace's name-keyed entity
  identity.

## Bootstrapping the ontology

The ontology starts empty on purpose: predicate names are not semantics. An
empty ontology yields zero deductive candidates rather than guessing that a name
like `depends_on` is transitive, inverse-bearing, or symmetric in this palace.

Two generator tasks can populate review candidates in `<palace>/ontology.json`:

- `suggest-rules` — day-1 name-heuristic bootstrap. It scans distinct KG
  predicate names and proposes candidate transitive, inverse, and symmetric
  rules from naming patterns. It works immediately but has lower precision.
- `induce-rules` — evidence-based dreaming induction. It scans observed base
  triples for inverse, symmetric, and transitive co-occurrence with a
  `--min-support` threshold. It is higher precision only after enough data has
  accumulated.

Both generators write disabled candidates only: `enabled: false` plus a
`rationale` explaining the heuristic or evidence. The workflow is always
generate → human review → edit approved rules to `enabled: true` → run
`derive`. Never auto-enable generated rules; a wrong rule pollutes the KG and
closure amplifies the mistake.

Generator guardrails:

- **Never auto-enable** — enabling a rule is a deliberate human edit.
- **Base-triples only** — induction reads observed facts and excludes derived
  `*_closure` triples / derivation lineage, avoiding self-reinforcing loops.
- **Support threshold** — induction requires `--min-support` co-occurrences;
  sparse KGs legitimately produce few or no candidates.

An eval gate for induced rules is deferred: future work should measure whether
enabling candidates improves multi-session task success more than it adds drift,
using LongMemEval/LoCoMo-style methodology. This change only proposes rules for
human review.

### Plain-language ontology proposals

For inline review of ontology candidates, prefer the proposal commands:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p> --propose
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p> --enable-rule <rule-id>
"$MPY" skills/dreaming/scripts/dream_contemplate.py --palace <p> --disable-rule <rule-id>
```

`--propose` shows plain-language disabled ontology candidates for review.
`--enable-rule` and `--disable-rule` are deliberate operator choices; generated
rules are never auto-enabled.

## Invariants to preserve

- **Entity identity** — closure keys on entity IDs, not display names. Current
  MemPalace writes are still name-keyed, so homonyms collapse at the MemPalace
  layer; derive inherits that limitation rather than fixing it.
- **Temporal overlap** — a conclusion is valid only over the non-empty interval
  `[max(premise.valid_from), min(premise.valid_to)]`.
- **Distinct closure predicate** — transitive facts materialize to
  `<predicate>_closure` by default (or the rule's `derived_predicate`), while
  chaining over both base and closure predicates.
- **Bounded closure** — `max_depth`, `max_iterations`, and `max_candidates`
  bound cost; capped results are marked `truncated`.
- **Anti-reflexive** — v1 suppresses `A p A` unconditionally. There is no
  `allow_reflexive` config.
- **No stale premises** — harvest reads active triples only and excludes already
  active conclusions.
- **Operational fixpoint** — skip-markers are keyed by
  `candidate_id + ontology_version`, so deliberate skips stop resurfacing until
  the ontology changes.

## Scope limits

`contemplate` is **strictly read-only**. It has four retained KG/reconnaissance
surfaces:

- `--task derive` — bounded deductive closure over active KG facts under
  explicitly enabled ontology rules (read-only reconnaissance; adjudication +
  materialization use the 5-phase `dream_harvest.py` / `dream_adopt.py` flow).
- `--task gaps` — standalone read-only gap reconnaissance; it ranks missing KG
  edges but does not retrieve sources or assert facts.
- `--recall` — relevance-ranked past-session reconnaissance for inline
  grounding; it does not run the derive scan or materialize anything.
- `--propose` / `--enable-rule` / `--disable-rule` — ontology proposal review
  and deliberate rule toggling.

**Constructive synthesis** (distill, generalize, name_gap, connect, tension,
shared_constraint, converge) is **not** part of contemplate. Use the dreaming
skill's `reflect` task for on-demand meditation or scheduled generative
consolidation.

See [`references/derive.md`](references/derive.md) for the contract, schemas,
and guardrails.
