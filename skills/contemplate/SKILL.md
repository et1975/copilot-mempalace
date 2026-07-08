---
name: contemplate
description: Use when the user wants deliberate, on-demand deductive reasoning over the MemPalace knowledge graph — derive, infer, reason, contemplate, "what follows from X", or "what can we conclude". Runs the shared dreaming scripts with `--task derive`.
---

# Contemplate

On-demand reasoning for a mempalace palace. Where `dreaming` is unattended
off-hours consolidation, `contemplate` is deliberate inline cognition: derive
what follows from the active KG under explicitly-approved rules, then decide
which entailed facts are worth materializing.

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

## Deferred (Track B)

v1 ships Track A only: bounded deductive closure over active KG facts. The
ACQUIRE loop, gap/question worklists, external research, clarification queues,
and abduction/best-explanation reasoning are deferred future work. Do not claim
that `contemplate` v1 asks questions, researches gaps, or performs abduction.

See [`references/derive.md`](references/derive.md) for the contract, schemas,
and guardrails.
