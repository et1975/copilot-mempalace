# HNSW drift / recovery

ChromaDB persists HNSW indexes lazily. If the server restarts while sqlite has rows newer than the on-disk HNSW segment, it **quarantines** the stale segment (renaming to `<id>.drift-YYYYMMDD-HHMMSS`) and rebuilds the index in memory. Search continues to work via the in-memory index; only the on-disk copy is invalidated.

## Symptoms

- `mempalace status` prints "Quarantined corrupt HNSW segment …" at startup
- `mempalace repair-status` shows `hnsw count: (no flushed metadata yet)` and `status: UNKNOWN`
- Multiple `*.drift-*` directories accumulate under `~/.mempalace/palace/`

## Recovery — non-destructive

1. `mempalace repair --mode max-seq-id` — un-poisons any legacy 0.6.x corrupted rows. No-op if clean.
2. `rm -rf ~/.mempalace/palace/*.drift-*` — orphaned quarantined copies, safe to delete after confirming the active (non-drift) segment exists for the same id.
3. Restart any harness whose MCP server is still pointing at the old in-memory index.

## Recovery — full rebuild

- `mempalace repair --mode from-sqlite --archive-existing --backup` rebuilds HNSW from sqlite rows. Stop the MCP server first (kill the `mempalace.mcp_server` PID and let the harness restart it).

## Root cause

Usually a non-graceful shutdown of the MCP server (kill -9, host sleep, OOM). Mitigate by giving the server time to flush before harness reload.
