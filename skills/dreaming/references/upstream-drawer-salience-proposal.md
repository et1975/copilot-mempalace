> Filed upstream as MemPalace/mempalace#1921 (https://github.com/MemPalace/mempalace/issues/1921)

# Upstream proposal — per-drawer salience dynamics

Paste-ready GitHub issue for **MemPalace/mempalace**. File at:
<https://github.com/MemPalace/mempalace/issues/new>

Checked 2026-07-03: MemPalace already ships cognitive dynamics for **connections**
(halls/tunnels), but drawers themselves do not carry native access-frequency or
last-accessed state.

---

**Title:** `feat: per-drawer salience dynamics for retrieval frequency and recency`

## Problem / motivation

Agent-driven "dreaming" pipelines need a first-class way to identify drawers that
are valuable enough to keep versus cold enough to forget. The planned `prune`
task wants a value signal like:

```text
v(d) = usage-frequency × recency-of-use × KG-degree
```

Today the pipeline can compute KG-degree, but it cannot ask MemPalace "how often
has this drawer actually been retrieved?" or "when was it last useful?" As a
result, prune must proxy usage with weaker signals: redundancy, KG source-degree,
ephemeral marker text, or host-specific session-store evidence.

A native per-drawer usage counter would make salience-based forgetting a
first-class MemPalace capability, not a host-coupled heuristic. It would also
benefit general ranking/reinforcement: hot drawers can be surfaced, cooled
drawers can be de-emphasised, and consumers can make explicit salience decisions.

## Current state

`mempalace/dynamics.py` already implements the right cognitive math:

- Hebbian potentiation: strength grows on co-access.
- Ebbinghaus exponential decay: strength fades with time since last activation.
- Cepeda spacing effect: stability grows with spaced reinforcement.

The existing fields are:

```text
strength: float
stability: float
last_activated: str
access_count: int
```

`initialize_dynamics_fields(connection, *, now=None)` safely backfills missing
fields, and `potentiate(connection, ...)` increments `access_count`, refreshes
`last_activated`, and grows `strength` / `stability` using the existing model.

The limitation is scope: the module docstring says it is "Living-connection math
for halls + tunnels", and callers live in `hallways.py` / `palace_graph.py`.
Those are palace-graph **edges**. Drawers are not currently treated as
strengthened/decayed entities.

On drawer write, `mempalace_add_drawer` stores metadata such as:

```text
wing
room
source_file
added_by
filed_at
id_recipe
```

There is no drawer-level `access_count`, and `filed_at` is filing time rather
than last retrieval time. The `mempalace_search` path returns search results but
does not currently potentiate drawer metadata when a drawer is surfaced.

## Proposed change

Extend the existing dynamics model from connections to drawers.

1. **Store dynamics fields in drawer metadata**
   - Add `strength`, `stability`, `last_activated`, and `access_count` to drawer
     metadata.
   - Use `filed_at` as the natural fallback for `last_activated` when lazily
     initialising old drawers.
   - Keep the fields additive and default-safe: existing drawers without these
     fields are neutral until first read/backfill.

2. **Potentiate on retrieval**
   - When `mempalace_search` returns/surfaces a drawer, call the same dynamics
     helper on that drawer's metadata.
   - A returned drawer increments `access_count`, updates `last_activated`, grows
     `strength`, and grows `stability` only when reinforcement is spaced, exactly
     like existing hall/tunnel dynamics.
   - For chunked drawers, update either the logical parent salience or all
     physical chunks consistently; prefer a logical parent-level salience if the
     search result is rejoined before being returned to the client.

3. **Expose a read path**
   - Include salience fields in `mempalace_search` / `mempalace_get_drawer`
     results, or add a small read helper such as `mempalace_drawer_salience`.
   - Consumers should be able to rank or filter by `access_count`,
     `last_activated`, decayed `strength`, and/or an already-decayed salience
     snapshot.

This reuses code MemPalace already has instead of inventing a second scoring
model for drawers.

## API sketch

Additive metadata shape:

```jsonc
{
  "id": "drawer_id",
  "wing": "copilot-mempalace",
  "room": "dreaming",
  "filed_at": "2026-07-03T12:34:56",
  "strength": 1.15,
  "stability": 1.2,
  "last_activated": "2026-07-03T20:12:00+00:00",
  "access_count": 3
}
```

Possible search result shape:

```jsonc
{
  "text": "...",
  "wing": "...",
  "room": "...",
  "id": "drawer_id",
  "distance": 0.31,
  "salience": {
    "strength": 1.15,
    "stability": 1.2,
    "last_activated": "2026-07-03T20:12:00+00:00",
    "access_count": 3
  }
}
```

Optional dedicated read tool:

```text
mempalace_drawer_salience(
    wing: str | None = None,
    room: str | None = None,
    limit: int = 100,
    order_by: "strength" | "access_count" | "last_activated" = "strength",
) -> {
  "drawers": [
    {"id": "...", "wing": "...", "room": "...",
     "strength": 1.15, "stability": 1.2,
     "last_activated": "...", "access_count": 3}
  ]
}
```

Implementation note: if search needs to remain strictly read-only in some modes,
the potentiation write could be guarded behind config or queued through the
existing write path. The important semantic event is "drawer was surfaced to a
consumer", not "query was issued".

## Alternatives considered

- **Use the host session store as the usage oracle.** This works for Copilot
  dreaming prototypes, but it couples MemPalace pruning to one harness's
  telemetry schema. MemPalace should know which drawers it returned without
  requiring external session databases.

- **Keep proxy-only salience.** Redundancy, KG source-degree, and text markers are
  useful hints, but they are lossy. A drawer can be frequently retrieved without
  having high KG degree, and a highly connected drawer can be stale or unused.

- **Add a separate drawer scoring model.** This would duplicate
  `dynamics.py`. Reusing the existing Hebbian/Ebbinghaus/Cepeda implementation
  keeps connection and drawer reinforcement semantics aligned.

## Backward compatibility

Additive only:

- Existing drawers continue to work without salience fields.
- `initialize_dynamics_fields` can lazily backfill missing drawer fields, using
  `filed_at` as the drawer analogue of connection `created_at`.
- Existing search callers can ignore the extra metadata.
- Existing hall/tunnel dynamics remain unchanged.

## Scope / non-goals

- This proposal does **not** define pruning policy or delete any drawers.
- It does **not** make search ranking automatically salience-first; exposing the
  signal is enough for consumers to opt in.
- It does **not** require raw embedding export or host telemetry access.
- It does **not** replace KG-degree; it supplies the missing usage-frequency and
  recency terms so consumers can combine them explicitly.

> Filed upstream as MemPalace/mempalace#1921 (https://github.com/MemPalace/mempalace/issues/1921)
