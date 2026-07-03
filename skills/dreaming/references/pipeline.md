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

`Δ : (M_in, S, θ) ↦ M_out`, with `M_in` immutable. The store `M` includes
logical drawers in a wing/room and the palace-local temporal KG.

### Task: dedup / merge (v1)

- Similarity `sim(a,b) = cos(mean_embed(a), mean_embed(b))`.
- Near-duplicate `a ~_τ b ⟺ sim ≥ τ`. Symmetric but **not transitive** →
  clusters are connected components of the `~_τ` graph (union-find).
- Fold `μ(C)` = one synthesised drawer per cluster (the agent's job, Phase 2).
- Soundness constraint: `μ(C)` must preserve every atomic fact in `C`.

### Task: contradiction / staleness

- Detection is mechanical: harvest reads currently-active KG triples and groups
  every `(subject, predicate)` pair with **2+ distinct objects**.
- These groups are only **candidates**. Some predicates are legitimately
  multi-valued (`knows`); others are functional (`lives_in`, `status_is`) where
  multiple current objects are stale or contradictory.
- Harvest provides a recency hint (`newest_object`) by sorting candidates on
  `(valid_from || "", extracted_at || "")` descending, but it never auto-resolves.
- Adjudication is cognitive: the agent decides whether the predicate should be
  functional, which object is authoritative, and which objects to retire.
- Adoption is non-destructive belief revision: `KnowledgeGraph.invalidate(...)`
  sets `valid_to` on retired facts. It does **not** delete rows.
- Fixpoint: re-harvest after adoption should remove the resolved functional
  contradiction. Legitimately multi-valued skipped groups may still surface.

> **KG path safety:** do not write contradictions through the
> `mempalace_kg_invalidate` MCP handler from these scripts. In mempalace 3.5.0,
> the handler resolves the palace-local KG only when the MCP server process was
> started with a CLI `--palace` flag (`_palace_flag_given`). Library imports have
> no such flag, so the handler can target the user's default
> `~/.mempalace/knowledge_graph.sqlite3` regardless of
> `MEMPALACE_PALACE_PATH`. `dream_palace.KgWriter` therefore constructs
> `KnowledgeGraph(db_path=os.path.join(palace_path, "knowledge_graph.sqlite3"))`
> directly and calls `.invalidate(...)`.

### Task: pattern / induce

- This is the **net-new-knowledge** task. `merge` compresses existing memory;
  `contradiction` retires stale beliefs; `pattern` induces a new lesson/rule
  from repeated observations.
- Adoption is **ADD-ONLY**: approved decisions add a surfaced lesson drawer and
  never delete drawers or invalidate KG facts.
- Detection is mechanical: a theme is a connected component of the `≥ τ`
  similarity graph over observations (`τ` defaults to `0.75` for pattern).
- Observation extraction and rule synthesis are cognitive: the agent reads the
  theme members, extracts atomic observations, judges whether a generalizable
  rule exists, and writes the final lesson.
- Groundedness invariant: a surfaced rule must cite at least `min_support`
  **distinct sessions** via `support_ids` (default `3`). `apply_pattern_decisions`
  rejects `surface` decisions with empty `supported_by`.
- `session_id` is the join key. Session identity is host-owned and orthogonal to
  mempalace; it is stamped onto diary entries at write time because a diary entry
  is a memory **about** a host session. `extract_session_id` parses a
  `SESSION_ID:<guid>` token; legacy entries without that token contribute no
  support.
- Two substrates / two modes:
  - **Diary-mode** (portable, mempalace-native, v1): `load_observation_entries`
    reads diary drawers, groups chunks by `parent_entry_id` / `parent_drawer_id`,
    and extracts stamped `session_id`s from text.
  - **Session-mode** (host amplifier): `dream_sessions.py` is a read-only adapter
    over `~/.copilot/session-store.db` (or `COPILOT_SESSION_STORE`) that loads
    sessions, turns, and session-attributed observations. It imports only the
    stdlib, not mempalace, isolating HOST coupling from mempalace coupling.
- Two-tier substrate: because diary carries `session_id`, a diary theme can drill
  down to raw host sessions; induced rules cite exact session ids.
- Weaker fixpoint than merge/contradiction: pattern is add-only, so there is no
  destructive convergence. Convergence comes from excluding already-adopted
  lessons from mining plus a dedup gate during adjudication so high-support
  themes become "covered."

Harvest:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task pattern --wing <wing> \
  --rooms diary --min-support 3 --out worklist.json
```

### Task: prune / forget

- This is the **FORGETTING** task and the only destructive one. Its safety model
  is therefore inverted: **remove carefully, reversibly**. `merge` preserves
  source facts by add-then-delete, `contradiction` soft-invalidates KG facts,
  and `pattern` is add-only; `prune` may delete drawers after approval.
- Ideal salience is `v(d) = usage-freq × recency × KG-degree`. Because
  mempalace v1 has no per-drawer usage counter and only `filed_at` (filing
  time, not last-use time), the shipped score is a composite of observable
  signals: age from `filed_at`, KG source-degree (`source_drawer_id` triple
  count), redundancy (maximum cosine similarity to neighbours), and ephemeral
  marker negatives (`for now`, `one-off`, `scratch`, etc.). True usage-frequency
  is deferred to a session-store oracle or native upstream salience.
- Candidate selection is the guardrail heart: **multi-gate AND**, never OR. A
  drawer is proposed only when `v < v_min` AND `age_days >= age_floor_days` AND
  `kg_degree == 0` AND it is not pinned. The `kg_degree == 0` gate also means the
  pruned drawer sourced no KG triples, so deletion cannot orphan the graph.
- Adoption is archive-not-delete: each pruned drawer is appended as a full record
  (including `salience` and `pruned_at`) to an append-only JSONL cold store,
  flushed and `fsync`ed, and only then deleted through the sanctioned
  `mempalace_delete_drawer` handler, which purges the closet/AAAK index. A
  failed archive deletes nothing; the archive is lossless and reversible.
- Apply has a protected re-check: drawers with `kg_degree > 0` or `pinned` are
  refused even if adjudication said `prune`.
- Steering-sensitive: this is where steering `θ` bites hardest. "Focus on X"
  reweights salience; "preserve X" pins a fixed point that should not be pruned.
- Fixpoint is a maintenance loop, not one-shot convergence: re-harvest should
  remove approved candidates from the current pass, but cold/redundant drawers
  will appear over time as the palace evolves.

Harvest:

```bash
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
"$MPY" dream_harvest.py --palace <palace> --task prune --wing <wing> \
  --room <room> --v-min 0.35 --age-floor-days 30 --out worklist.json
```

Adopt:

```bash
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl --dry-run
"$MPY" dream_adopt.py --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl
```

### Future task shape

Additional worklist `kind`s should keep the same harvest/adjudicate/adopt shape.

## Artifacts (session workspace — never commit)

### `worklist.json` (harvest → agent)

```jsonc
{
  "version": 1,
  "task": "merge",
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

Contradiction worklist:

```jsonc
{
  "version": 1,
  "task": "contradiction",
  "scope": {"palace": "<path>", "task": "contradiction"},
  "params": {},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "contradiction",
      "cluster_id": 0,
      "subject": "Alice",
      "predicate": "lives_in",
      "candidates": [
        {"object": "Seattle", "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        {"object": "Portland", "valid_from": "2024-01-01", "extracted_at": "2024-01-02"}
      ],
      "evidence": {"size": 2, "newest_object": "Seattle"},
      "decision": null
    }
  ]
}
```

Pattern worklist:

```jsonc
{
  "version": 1,
  "task": "pattern",
  "scope": {"palace": "<path>", "wing": "<w>", "rooms": ["diary"], "task": "pattern"},
  "params": {"tau": 0.75, "min_support": 3},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "pattern",
      "cluster_id": 0,
      "members": [
        {"id": "<entry id>", "text": "<diary text>", "session_id": "<session id>",
         "agent": "<agent|null>", "date": "<date|null>", "topic": "<topic|null>"}
      ],
      "evidence": {
        "size": 3,
        "support": 3,
        "support_ids": ["<session id>", "..."],
        "pair_sims": [{"a": "id", "b": "id", "sim": 0.82}]
      },
      "decision": null
    }
  ]
}
```

Prune worklist:

```jsonc
{
  "version": 1,
  "task": "prune",
  "scope": {"palace": "<path>", "wing": "<w>", "room": "<r|null>", "task": "prune"},
  "params": {"v_min": 0.35, "age_floor_days": 30},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "prune",
      "id": "<logical id>",
      "member_ids": ["<physical id>", "..."],
      "text": "<drawer text>",
      "wing": "<w>",
      "room": "<r>",
      "salience": {
        "age_days": 45,
        "kg_degree": 0,
        "redundancy": 0.94,
        "negatives": true,
        "v": 0.12
      },
      "decision": null
    }
  ]
}
```

### `decisions.json` (agent → adopt)

Same document with each `item.decision` set to one of:

Merge:

```jsonc
{"action": "merge", "wing": "<w>", "room": "<r>",
 "text": "<synthesised drawer>", "supersedes": ["<physical id>", ...]}
```

Contradiction:

```jsonc
{"action": "invalidate", "keep": "<object>", "invalidate": ["<stale object>", ...]}
```
If `invalidate` is omitted, adoption invalidates every candidate object except
`keep`. Use this only after judging that the predicate is functional and the kept
object is authoritative.

Pattern:

```jsonc
{"action": "surface", "wing": "<w>", "room": "<r>",
 "text": "<induced rule/lesson>",
 "supported_by": ["<session id>", "..."]}
```

If `wing`/`room` are omitted, adoption falls back to the first member when that
metadata is present, then to the worklist scope. If `supported_by` is omitted,
adoption falls back to `evidence.support_ids`; empty support is rejected.

Prune:

```jsonc
{"action": "prune"}
```

```jsonc
{"action": "keep"}
```

If a prune decision is omitted, adoption defaults to `keep` (conservative). A
`prune` decision may also repeat `id`, `member_ids`, `wing`, `room`, `text`, or
`salience`; omitted values fall back to the worklist item.

```jsonc
{"action": "skip"}
```

`wing`/`room`/`supersedes` default to the item's values if omitted.
For contradiction items, `skip` means the group is legitimately multi-valued or
not safe to adjudicate.
For pattern items, `skip` means the rule is unsupported, not generalizable, or
already covered by an existing filed lesson.
For prune items, use `keep` rather than `skip`; missing or non-`prune` decisions
are treated as keep.

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
- **KG read/write**: the palace-local KG is
  `<palace_path>/knowledge_graph.sqlite3`; active triples are rows where
  `valid_to IS NULL`. For contradiction adoption, use
  `KnowledgeGraph(db_path=<palace-local KG>)` directly instead of the MCP
  `mempalace_kg_invalidate` handler because of the `_palace_flag_given` gate
  described above.

## Invariants

| Invariant | Enforced by |
|-----------|-------------|
| Non-destructiveness / reversibility | harvest read-only; live writes only in adopt, only on approved decisions; failed add skips delete; prune archives full records before delete and a failed archive deletes nothing |
| Provenance | `supersedes` on every merge |
| Groundedness | pattern `support_ids` must cover ≥ `min_support` distinct sessions; empty `supported_by` is rejected |
| Salience-gated protected classes | prune requires `v < v_min` AND age floor AND `kg_degree == 0` AND not pinned; apply refuses KG-connected or pinned drawers |
| Auditability | prune archive records include drawer text, member ids, salience components, and `pruned_at` |
| Idempotence / fixpoint | Phase 5 re-harvest → 0 merge clusters / resolved functional contradictions; pattern and prune are maintenance loops rather than one-shot convergence |
| Bounded cost | scope by wing/room; `tau` gates the pairwise graph |

## Upstream evolution (why harvest imports mempalace)

Harvest reads the ChromaDB collection directly (via `mempalace.palace.get_collection`)
because **no MCP tool exposes raw embeddings or a bulk near-duplicate scan** —
`mempalace_search` is query-based top-N only, and `mempalace dedup` is
destructive keep-longest, not a cluster finder. That direct read is the sole
reason the scripts need a Python that can `import mempalace`.

The clean long-term fix is a **read-only server-side cluster finder** upstream in
MemPalace, which would make this pipeline fully MCP-native (no library/venv
coupling), exact-cosine, and scalable. See
[`upstream-find-duplicates-proposal.md`](upstream-find-duplicates-proposal.md)
for the paste-ready proposal. Until that lands, the script-based harvest here is
the working approach.

For prune, the clean long-term fix is native per-drawer salience dynamics:
MemPalace/mempalace#1921 would add drawer usage-frequency / last-activated
signals so `usage-freq` becomes native instead of proxied by host session data or
observable heuristics. See
[`upstream-drawer-salience-proposal.md`](upstream-drawer-salience-proposal.md).
