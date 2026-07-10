# Derive / contemplate worklist reference

## Derive worklist item schema

Harvest writes derive candidates in the contemplate worklist with
`task: "contemplate"` and per-item `kind: "derive"`. The conclusion is nested
under `item["conclusion"]`; do not look for top-level
`subject` / `predicate` / `object` fields.

```jsonc
{
  "kind": "derive",
  "candidate_id": "derive:<stable hash>",
  "conclusion": {
    "subject_id": "<entity id>",
    "subject": "<display name|null>",
    "predicate": "<derived predicate>",
    "object_id": "<entity id>",
    "object": "<display name|null>",
    "valid_from": "<optional timestamp>",
    "valid_to": "<optional timestamp|null>"
  },
  "rule": {
    "id": "<ontology rule id>",
    "family": "inverse|symmetric|transitive",
    "predicate": "<base predicate>"
  },
  "proof": {
    "depth": 2,
    "premise_ids": ["<kg triple id>", "..."],
    "premise_drawer_ids": ["<source drawer id|null>", "..."]
  },
  "evidence": {
    "already_active": false,
    "confidence": 1.0,
    "valid_from": "<premise interval start|null>",
    "valid_to": "<premise interval end|null>"
  },
  "decision": null,
  "ontology_version": "onto:<hash>"
}
```

Current harvest code stores `valid_from` and `valid_to` in
`item["evidence"]`; materialization passes those values to the KG writer. If a
future producer also copies them into `item["conclusion"]`, consumers should
still treat `item["conclusion"]` as the canonical subject/predicate/object
container.

## Derive decision format

Adjudication writes the decision into the nested `item["decision"]` field on
each derive item. Do not put action fields at the worklist root or as sibling
top-level fields on the item.

Materialize an approved derived fact:

```jsonc
item["decision"] = {"action": "materialize"}
```

Skip one candidate while preserving a skip marker for this ontology version:

```jsonc
item["decision"] = {"action": "skip", "reason": "<why this candidate is not sound>"}
```

Reject every candidate from the rule in the current worklist:

```jsonc
item["decision"] = {"action": "reject_rule", "reason": "<why the rule is unsound>"}
```

During adoption, derive decisions are flattened mechanically by copying the item
and merging in `item["decision"]`, so the nested `conclusion`, `rule`, `proof`,
`evidence`, `candidate_id`, and `ontology_version` remain available to
`apply_derive_decisions`.

