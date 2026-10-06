# Contemplate derive — contract & reference

Contract for the shipped `contemplate` v1 task: bounded deductive KG closure
(Track A). It is a cognition frontend over the existing dreaming mechanics:
`skills/dreaming/scripts/dream_harvest.py --task derive` and
`skills/dreaming/scripts/dream_adopt.py --task derive`.

For examples, set `MPY` to the absolute path of the provisioned Python that
imports MemPalace, and `DREAM_SCRIPTS` to the absolute path of the dreaming
skill's `scripts/` directory. Select that interpreter explicitly rather than
parsing a launcher shebang. Run from the external session workspace to keep
relative worklists and decisions outside the checkout and installed skill.

## Layered responsibilities

- **Substrate — mempalace**: palace-local temporal KG at
  `<palace>/knowledge_graph.sqlite3`, plus native artifacts/events for ontology
  configuration/candidates and derive skip markers.
- **Mechanics — shared Python scripts**: `dream_lib.py`, `dream_palace.py`,
  `dream_harvest.py`, and `dream_adopt.py` under `skills/dreaming/scripts/`.
- **Cognition — the contemplate skill**: approve ontology rules and adjudicate
  derived-fact candidates.

## Pipeline contract

| Phase | Reads | Writes |
|-------|-------|--------|
| Harvest | active KG triples (`valid_to IS NULL`), native ontology, native skip-markers | `worklist.json` export; no conclusion adoption, but legacy premise loading may reconcile provenance |
| Adjudicate | `worklist.json`, rule rationales, user intent | `decisions.json` (same document with actions filled) |
| Adopt | `decisions.json`, active KG, ontology, skip-markers | approved derived triples, `kg_derivations`, skip-markers |
| Verify | active KG, ontology, skip-markers | reports residual candidates; legacy premise-loading caveat still applies |

Run:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --task derive \
  --palace <p> --out worklist.json
# fill actions in worklist.json and save as decisions.json
"$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --task derive \
  --palace <p> --decisions decisions.json --verify
```

`--verify`/`--strict` apply to **live adoption only**: they re-harvest after the writes and check for
an operational fixpoint. Under `--dry-run` nothing is written, so the residual/`--strict` check is
skipped; a dry run still exits non-zero if any decision produced an error (the preview surfaces
materialize failures).

## Ontology configuration

The ontology config is explicit and stored natively. No configured rules in
healthy native storage ⇒ zero candidates; missing/corrupt native storage is an
error, not permission to reset state. File flags remain explicit legacy imports
and exports, not default sidecar authority or implicit enablement. The top-level
document is versioned; the scripts also compute
an `ontology_version` content hash from enabled-rule semantics and echo it into
worklists, skip-markers, and derivation lineage.

A coherent full-palace backup/restore retains native artifacts and event history
for ontology and derive skips; a wing-only export does not. Native inspection is
read-only and does not bootstrap storage. Explicit initialization is for a
genuinely new control store in a valid palace, never repair for a failed restore.
Original evidence, approved-rule semantics and separate procedural opt-in gates
remain unchanged.

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

## Native ontology file compatibility

The `dream_contemplate.py` driver defaults to native ontology and skip records.
Its `--rules` and `--skips` flags are explicit read-only input overrides during
reconnaissance. Approved enable/disable with `--rules` imports the resulting
approved configuration natively under the shared lock; it does not modify the
legacy input file. Bootstrap's optional `--out` export is a working copy:
disabled candidates are always retained natively. File selection alone neither
enables rules nor enrolls procedural learning.

For `dream_adopt.py --task derive`, non-dry adoption imports explicit `--rules`
and any existing `--skips` input natively before KG writer creation or effects.
A supplied missing rules file is an error; a missing skips file is allowed as
a new optional export target. Dry-run imports nothing, and harvest previews do
not publish their compatibility inputs.

## Bootstrapping ontology rules

The native ontology may start empty. That is deliberate: predicate names are not
semantics, and no configured rules must emit zero deductive candidates
instead of guessing. Two generator tasks can populate disabled candidate rules
for review:

```bash
# name-heuristic bootstrap
"$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --task suggest-rules --palace <p>
# evidence induction
"$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --task induce-rules --palace <p> --min-support 2
# then a HUMAN reviews native candidates and explicitly enables approved rules
```

- `suggest-rules` scans distinct predicate names and proposes transitive,
  inverse, or symmetric candidates from naming patterns. It is a day-1
  bootstrap, not evidence of semantics.
- `induce-rules` scans observed base triples and proposes inverse, symmetric,
  or transitive candidates from actual co-occurrence at `--min-support`.

Generator output uses the ontology document schema above rather than a
separate worklist schema. Each generated rule is a disabled candidate:
`enabled: false`, with a `rationale` describing the name heuristic or evidence
support. Humans review candidates and enable only approved rules;
generators never approve rules themselves. `--ontology-out` requests an explicit
file export and is output only even if the file exists; its contents are never
imported. `--rules` is the sole ontology file import input. Editing an export
alone does not update native configuration.

Guardrails:

- **Never auto-enable** — generated rules are always `enabled: false`. A wrong
  enabled rule pollutes the KG, and closure can amplify that error.
- **Base-triples only** — `induce-rules` reads observed base facts and excludes
  derived `*_closure` triples and derivation lineage, avoiding a
  self-reinforcing feedback loop.
- **Support threshold** — `induce-rules` requires `--min-support`
  co-occurrences. Sparse KGs legitimately yield few or no candidates.

An eval gate is deferred, not shipped here: later work should measure whether
enabling induced rules improves multi-session task success more than it adds
drift, using LongMemEval/LoCoMo-style methodology.

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
`ontology_version`; the durable fix is to explicitly disable or update the native
rule, not edit an exported file alone.

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

This reference covers facts-only bounded deductive materialization. It does not
cover standalone gap reconnaissance, drawer-text insight synthesis,
subproperty/type/composition rules, query-time virtual derivation, or ontology
learning.
