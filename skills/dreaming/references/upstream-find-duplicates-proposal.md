# Upstream proposal — read-only `mempalace_find_duplicates` MCP tool

**Filed:** [MemPalace/mempalace#1919](https://github.com/MemPalace/mempalace/issues/1919) (2026-07-03).

Paste-ready GitHub issue for **MemPalace/mempalace**. File at:
<https://github.com/MemPalace/mempalace/issues/new>

Checked 2026-07-03: no existing issue proposes a read-only duplicate/cluster
finder (closest open issues #848 "remove drawers from a wing" and #1637 "editor
identity" are unrelated). `mempalace dedup` exists but is destructive-only.

---

**Title:** `feat: read-only mempalace_find_duplicates MCP tool (non-destructive near-duplicate cluster finder)`

## Summary

Add a **read-only** MCP tool (and matching library/CLI entry point) that returns
**clusters of near-duplicate drawers with pairwise similarities**, without
deleting anything. Today `mempalace dedup` detects near-duplicates but only in a
destructive greedy keep-longest mode, and **no MCP tool exposes the detection or
raw embeddings** — so external consolidation workflows must `import mempalace`
and read the ChromaDB collection directly, which MCP-only clients can't do.

## Motivation

Agent-driven consolidation / "dreaming" pipelines (surface near-dups → let the
agent synthesise a merged drawer → adopt) need the **detection** step — *which
drawers are near-duplicates, and how similar* — as a first-class, non-destructive
query. Current gaps:

- `mempalace dedup` (`dedup.py`) is **destructive** (greedy keep-longest, delete
  the rest) and **source-grouped**; it never surfaces clusters for external
  review or cognitive merge.
- **No MCP tool returns raw embeddings**; `mempalace_search` is query-based
  top-N only. Clustering a whole wing therefore requires
  `from mempalace.palace import get_collection` — a library/venv coupling MCP
  clients can't satisfy.

## Proposed API

```text
mempalace_find_duplicates(
    wing: str | None = None,
    room: str | None = None,
    threshold: float = 0.15,     # cosine DISTANCE, same scale as `mempalace dedup`
    max_clusters: int | None = None,
) -> {
  "clusters": [
    {"drawer_ids": ["id_a", "id_b"],
     "pairs": [{"a": "id_a", "b": "id_b", "distance": 0.07}],
     "size": 2}
  ],
  "params": {"wing": "...", "room": "...", "threshold": 0.15}
}
```

- **Read-only** — no writes, no deletes.
- Returns only **ids + distances** (small payload; never raw vectors, so it
  doesn't flood the client/model context).
- Scoped by `wing`/`room` like the other read tools.
- Optionally expose the same detection via `mempalace dedup --list --json`
  (detection without deletion).

## Why this shape (alternatives rejected)

- **Not** a raw-embedding export tool: returning 384-dim vectors for a whole
  wing floods the client/model context and pushes clustering to the client; the
  server already has the vectors + HNSW index and should do the math.
- **Not** reusing destructive `dedup`: consolidation wants review + synthesis,
  not blind keep-longest-drop-rest.
- Server-side keeps it **exact-cosine, deterministic, and scalable**; the
  workaround of N× `mempalace_search` is hybrid (bm25+vector) scored,
  non-deterministic, and O(N) round-trips.

## Implementation notes

- The pairwise-distance / grouping compute already exists in
  [`mempalace/dedup.py`](../../../) (`dedup_source_group`, backend similarity
  search). This tool can reuse it in a non-destructive "collect clusters" mode
  instead of the keep/delete branch.
- Clusters = connected components of the `distance < threshold` graph
  (similarity is symmetric but **not** transitive).
- Classify as a **READ** tool so it stays out of the write-only daemon queue.

## Backward compatibility

Additive only: a new read tool plus an optional `--list/--json` flag on `dedup`.
No behaviour change to existing `dedup`.

## Context

Proposed while building an agent "dreaming" consolidation pipeline on top of
MemPalace: harvest near-dup clusters → agent synthesises a merged drawer →
adopt (`add_drawer` + `delete_drawer`). This tool would make that pipeline fully
MCP-native and remove the only remaining library/venv coupling.
