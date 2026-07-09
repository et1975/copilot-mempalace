# Contemplate derive — contract & reference

Contract for the shipped `contemplate` v1 task: bounded deductive KG closure
(Track A). It is a cognition frontend over the existing dreaming mechanics:
`skills/dreaming/scripts/dream_harvest.py --task derive` and
`skills/dreaming/scripts/dream_adopt.py --task derive`.

## Layered responsibilities

- **Substrate — mempalace**: palace-local temporal KG at
  `<palace>/knowledge_graph.sqlite3`.
- **Mechanics — shared Python scripts**: `dream_lib.py`, `dream_palace.py`,
  `dream_harvest.py`, and `dream_adopt.py` under `skills/dreaming/scripts/`.
- **Cognition — the contemplate skill**: approve ontology rules and adjudicate
  derived-fact candidates.

## Pipeline contract

| Phase | Reads | Writes |
|-------|-------|--------|
| Harvest | active KG triples (`valid_to IS NULL`), `<palace>/ontology.json` or `--rules`, skip-markers | `worklist.json` only. Harvest writes nothing to the palace |
| Adjudicate | `worklist.json`, rule rationales, user intent | `decisions.json` (same document with actions filled) |
| Adopt | `decisions.json`, active KG, ontology, skip-markers | approved derived triples, `kg_derivations`, skip-markers |
| Verify | active KG, ontology, skip-markers | no palace writes; reports residual candidates |

Run:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" skills/dreaming/scripts/dream_harvest.py --task derive \
  --palace <p> --rules <p>/ontology.json --out worklist.json
# fill actions in worklist.json and save as decisions.json
"$MPY" skills/dreaming/scripts/dream_adopt.py --task derive \
  --palace <p> --decisions decisions.json --verify
```

`--verify`/`--strict` apply to **live adoption only**: they re-harvest after the writes and check for
an operational fixpoint. Under `--dry-run` nothing is written, so the residual/`--strict` check is
skipped; a dry run still exits non-zero if any decision produced an error (the preview surfaces
materialize failures).

## `ontology.json`

The ontology config is explicit. Empty or missing config ⇒ no enabled rules ⇒
zero candidates. The top-level document is versioned; the scripts also compute
an `ontology_version` content hash from enabled-rule semantics and echo it into
worklists, skip-markers, and derivation lineage.

```jsonc
{
  "version": 1,
  "rules": [
    {
      "id": "transitive:depends_on",
      "family": "transitive",
      "predicate": "depends_on",
      "derived_predicate": "depends_on_closure",
      "enabled": true,
      "max_depth": 3,
      "rationale": "Approved dependency closure semantics for this palace."
    },
    {
      "id": "inverse:depends_on:dependency_of",
      "family": "inverse",
      "predicate": "depends_on",
      "inverse_predicate": "dependency_of",
      "enabled": true
    },
    {
      "id": "symmetric:collaborates_with",
      "family": "symmetric",
      "predicate": "collaborates_with",
      "enabled": true
    }
  ]
}
```

Fields:

- `id` — stable rule id. Used in proofs, candidate ids, and lineage.
- `family` — v1 supports `transitive`, `inverse`, and `symmetric`.
- `predicate` — base predicate, canonicalized by the scripts.
- `derived_predicate` — transitive materialization predicate. Defaults to
  `<predicate>_closure`.
- `inverse_predicate` — required for `inverse`.
- `enabled` — only enabled rules participate.
- `max_depth` — optional transitive depth cap; global CLI bounds still apply.
- `rationale` — human documentation; not interpreted by harvest.

There is no `allow_reflexive`: v1 closure is unconditionally anti-reflexive.

## `worklist.json`

Harvest emits:

```jsonc
{
  "version": 1,
  "task": "contemplate",
  "scope": {"palace": "<path>"},
  "params": {"max_depth": 3, "max_iterations": 10, "max_candidates": 500},
  "ontology_version": "onto:<content-hash>",
  "rules": [/* enabled/loaded rules */],
  "instructions": null,
  "items": [
    {
      "kind": "derive",
      "candidate_id": "derive:<sha256>",
      "conclusion": {
        "subject_id": 1,
        "subject": "A",
        "predicate": "depends_on_closure",
        "object_id": 3,
        "object": "C"
      },
      "rule": {
        "id": "transitive:depends_on",
        "family": "transitive",
        "predicate": "depends_on"
      },
      "proof": {
        "depth": 2,
        "premise_ids": ["<triple id>", "<triple id>"],
        "premise_drawer_ids": ["<drawer id>", "<drawer id>"]
      },
      "evidence": {
        "already_active": false,
        "confidence": 0.7,
        "valid_from": "2026-01-01T00:00:00",
        "valid_to": null
      },
      "ontology_version": "onto:<content-hash>",
      "truncated": false,
      "decision": null
    }
  ]
}
```

`candidate_id` is stable over conclusion `(subject_id, predicate, object_id)`,
rule id, premise triple ids, and `ontology_version`. It is intentionally tied
to the ontology so changed rules can resurface candidates.

## `decisions.json`

Adoption reads the same document shape with selected items carrying an `action`
field:

```jsonc
{"action": "materialize"}
```

```jsonc
{"action": "skip", "reason": "cheaply re-derivable / too noisy"}
```

```jsonc
{"action": "reject_rule", "reason": "predicate is not transitive in this palace"}
```

`materialize` writes the derived triple and lineage. `skip` writes a
skip-marker. `reject_rule` writes skip-markers for every current-worklist
candidate from that rule, giving an operational fixpoint for the current
`ontology_version`; the durable fix is to edit `ontology.json`.

## `kg_derivations`

Adopt writes a lineage row next to each materialized derived triple. Columns:

| Column | Meaning |
|--------|---------|
| `id` | internal row id |
| `candidate_id` | UNIQUE stable candidate key |
| `conclusion_triple_id` | triple id returned by `KnowledgeGraph.add_triple` |
| `rule_id` | applied ontology rule |
| `ontology_version` | content hash of the rule semantics |
| `premise_triple_ids` | JSON array of KG triple ids |
| `premise_drawer_ids` | JSON array of source drawer ids |
| `confidence` | min premise confidence propagated to the conclusion |
| `created_at` | UTC creation timestamp |

The `candidate_id UNIQUE` constraint is the first idempotence gate; the KG
writer's own active-triple de-duplication is a second gate.

## Invariants / guardrails

| Invariant | Shipped behavior |
|-----------|------------------|
| Rule-based materialization, not absolute soundness | Conclusions are only as good as the approved rules and the KG's entity identity |
| Explicit ontology | No rule is inferred from a predicate name; empty config emits zero candidates |
| Bounds | `max_depth`, `max_iterations`, and `max_candidates`; capped outputs carry `truncated` |
| Anti-reflexive | `A p A` conclusions are suppressed unconditionally |
| Active premises only | Invalidated triples (`valid_to` set) are not premises |
| Exclude active | Already-active conclusions, including pre-existing closure facts, are not emitted |
| Interval-overlap temporal | Conclusion validity is `[max(starts), min(ends)]`; empty/touching intervals produce no candidate |
| Distinct closure predicate | Transitivity emits `<pred>_closure` by default and chains over base + closure edges |
| Idempotent adopt | `candidate_id UNIQUE` plus KG de-dupe prevents repeated materialization |
| Operational fixpoint | Skip-markers are keyed by `candidate_id + ontology_version` |

## Scope limits

The entity IDs used by closure come from MemPalace's name-keyed KG. Because the
write API resolves entities by name, homonyms still collapse at the MemPalace
layer; derive does not solve that in v1.

Track B is deferred: ACQUIRE loops, gap/question worklists, external research,
clarification queues, abduction, subproperty/type/composition rules, query-time
virtual derivation, and ontology learning are future work. v1 ships facts-only
bounded deductive materialization.
