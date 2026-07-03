# Dreaming pipeline — contract & reference

Design basis for the dreaming scripts. Filed in the palace under wing
`copilot-mempalace`, room `dreaming` / `api`; summarised here for offline use.

## Layered responsibilities

- **Substrate — mempalace** (passive): stores drawers + embeddings + KG; serves
  read (`get_collection`) and write (MCP tool handlers). No cognition.
- **Mechanics — Python scripts**: `dream_lib.py` (pure core), `dream_palace.py`
  (mempalace adapter), `dream_harvest.py`, `dream_adopt.py`.
- **Cognition — the dreaming skill**: the agent, in its own fresh context.

## The dream as a function

`Δ : (M_in, S, θ) ↦ M_out`, with `M_in` immutable. v1 realises only the
dedup/merge task; the store `M` is the set of logical drawers in a wing/room.

### Task: dedup / merge (v1)

- Similarity `sim(a,b) = cos(mean_embed(a), mean_embed(b))`.
- Near-duplicate `a ~_τ b ⟺ sim ≥ τ`. Symmetric but **not transitive** →
  clusters are connected components of the `~_τ` graph (union-find).
- Fold `μ(C)` = one synthesised drawer per cluster (the agent's job, Phase 2).
- Soundness constraint: `μ(C)` must preserve every atomic fact in `C`.

### Reserved future tasks (not in v1)

- `contradiction` — same-`(subject,predicate)` KG triples with different
  objects → keep newest, `kg_invalidate` the rest (AGM belief revision).
- `pattern` — frequent observations across sessions/diary → surface a rule.
- `prune` — drop low-salience drawers.

These are additional worklist `kind`s; the harvest/adjudicate/adopt shape is the
same.

## Artifacts (session workspace — never commit)

### `worklist.json` (harvest → agent)

```jsonc
{
  "version": 1,
  "scope": {"palace": "<path>", "wing": "<w>", "room": "<r|null>"},
  "params": {"tau": 0.9},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "merge",
      "cluster_id": 0,
      "members": [
        {"id": "<logical id>", "member_ids": ["<physical id>", ...],
         "text": "<drawer text>", "wing": "<w>", "room": "<r>"}
      ],
      "supersedes": ["<physical id>", ...],   // union of all member_ids
      "evidence": {"pair_sims": [{"a": "id", "b": "id", "sim": 0.97}], "size": 2},
      "decision": null                        // agent fills this
    }
  ]
}
```

### `decisions.json` (agent → adopt)

Same document with each `item.decision` set to one of:

```jsonc
{"action": "merge", "wing": "<w>", "room": "<r>",
 "text": "<synthesised drawer>", "supersedes": ["<physical id>", ...]}
```
```jsonc
{"action": "skip"}
```

`wing`/`room`/`supersedes` default to the item's values if omitted.

## Verified mempalace API facts (mempalace 3.5.0)

- **Read**: `from mempalace.palace import get_collection;
  col = get_collection(palace_path)`. `col.get(include=["documents",
  "metadatas", "embeddings"], where=<filter>)` returns 384-dim (minilm)
  embeddings. `where` is equality-only (`$and` of `{wing:..},{room:..}`); there
  is **no** `$ne`/`$nin`, so an unscoped search surfaces every wing — the reason
  any shadow/candidate content must live in a separate `palace_path`, not a
  `<wing>__dream` wing of the live palace.
- **Chunking**: large drawers split into physical rows sharing
  `metadata["parent_drawer_id"]` with `chunk_index`. `group_logical_drawers`
  rebuilds logical drawers; `tool_delete_drawer(id)` works on **physical** ids,
  not the logical group handle.
- **Write** (the sanctioned path): `from mempalace.mcp_server import TOOLS;
  TOOLS["mempalace_add_drawer"]["handler"](wing, room, content, added_by=...)`
  and `TOOLS["mempalace_delete_drawer"]["handler"](drawer_id=...)`. The durable
  alternative `mempalace.service.run_mcp_tool` accepts write-classified tools
  only.
- **Palace targeting**: handlers resolve the palace via
  `MEMPALACE_PALACE_PATH`; set it before importing mempalace
  (`dream_palace.bind_palace(path)`).

## Invariants

| Invariant | Enforced by |
|-----------|-------------|
| Non-destructiveness | harvest read-only; live writes only in adopt, only on approved decisions; failed add skips delete |
| Provenance | `supersedes` on every merge |
| Idempotence / fixpoint | Phase 5 re-harvest → 0 clusters |
| Bounded cost | scope by wing/room; `tau` gates the pairwise graph |
