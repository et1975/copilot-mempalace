# MemPalace disaster recovery

Use this when a local palace is lost, corrupted, partially restored, or needs to
roll back after a bad restore. The backup source is a local `restic` repository
created by the `mempalace-backup` skill. The repository may live on an external
drive; no cloud backend is assumed.

Shared environment:

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass
```

Never hard-code a password. Keep the password file `chmod 600`, and avoid
`--insecure-no-password`.

## Total loss / new machine

If the machine or home directory is gone, the `restic` repository is the only
thing that had to survive.

1. Install MemPalace and `restic` using your approved machine setup.
2. Attach the external drive or mount that contains the local restic repo.
3. Set `RESTIC_REPOSITORY` and `RESTIC_PASSWORD_FILE`.
4. Inspect available snapshots:

   ```bash
   restic snapshots --tag palace
   restic ls latest
   restic find origin.json
   ```

5. Restore the latest palace into the palace directory:

   ```bash
   mkdir -p ~/.mempalace
   restic restore latest:"$HOME/.mempalace" --target ~/.mempalace
   ```

   The `latest:"$HOME/.mempalace"` subpath form strips the stored absolute path
   prefix so files land directly under `~/.mempalace/`. Alternative: restore to
   original absolute locations with `restic restore latest --target /`. If the
   restored tree lands somewhere unexpected, move the restored palace contents
   so `config.json`, `tunnels.json`, `knowledge_graph.sqlite3`, `palace/`, and
   `wal/` are directly under `~/.mempalace/`.

6. Rebuild and verify:

   ```bash
   mempalace repair
   mempalace repair-status
   mempalace status
   mempalace search "<known term>"
   ```

7. Reopen the running server with MCP `mempalace_reconnect`, or restart the
   harness/MCP server if MCP is unavailable.

## Partial corruption: HNSW index bad, SQLite fine

Symptoms:

- `mempalace status` shows plausible drawer counts.
- `mempalace repair-status` reports SQLite row count and HNSW element count do
  not match.
- Search is missing expected hits, or Chroma/HNSW errors appear.

Prefer repair when SQLite data is intact:

```bash
mempalace daemon stop
mempalace repair
mempalace repair-status
```

If rows were poisoned by an interrupted sequence-id update:

```bash
mempalace repair --mode max-seq-id
mempalace repair-status
```

Prefer restore when:

- SQLite files are missing or unreadable.
- `wal/` replay cannot recover expected writes.
- `repair` completes but `repair-status` still mismatches.
- You need to roll back to a known-good point before corruption.

For focused vector-index recovery, see
[`mempalace` HNSW recovery](../../mempalace/references/hnsw-recovery.md).

## origin.json / embedder mismatch

Critical file:

```text
~/.mempalace/palace/.mempalace/origin.json
```

Symptoms after restore:

- `mempalace status` looks sane, but `mempalace search "<known term>"` returns
  nothing.
- Search raises embedder or collection errors.
- `repair-status` can pass, but semantic search is still wrong.

Cause: the restored vector store and current embedder identity do not match, or
the restored palace lacks `origin.json`.

Fix:

1. Restore `palace/.mempalace/origin.json` from the same snapshot as the palace.
2. Do not mix `origin.json`, HNSW `.bin` files, and SQLite metadata from
   different embedder configurations.
3. If the original embedder cannot be restored, re-embed/rebuild the palace under
   the intended embedder, then run:

   ```bash
   mempalace repair
   mempalace repair-status
   mempalace search "<known term>"
   ```

## restic repo problems

Start read-only:

```bash
restic check
restic snapshots --tag palace
restic ls <snapshot-id>
```

Common repair commands:

```bash
restic unlock
restic repair index
restic repair snapshots
restic recover
restic check
```

- `restic unlock`: remove stale repository locks after an interrupted restic run.
- `restic repair index`: rebuild repository indexes.
- `restic repair snapshots`: repair snapshot metadata when possible.
- `restic recover`: recover data from unreferenced packs into a new snapshot.
- `restic check`: verify repository consistency after any repair.

Use `restic dump <snapshot-id> <path>` to inspect one file without restoring the
whole palace:

```bash
restic dump <snapshot-id> "$HOME/.mempalace/palace/.mempalace/origin.json"
restic dump <snapshot-id> "$HOME/.mempalace/config.json"
```

## Rollback of a bad restore

The safe restore runbook moves the previous palace aside first:

```bash
mv ~/.mempalace ~/.mempalace.bad-restore-$(date +%Y%m%d-%H%M%S)
mv ~/.mempalace.bak-<timestamp> ~/.mempalace
mempalace repair
mempalace repair-status
```

Then reopen with MCP `mempalace_reconnect`, or restart the harness/MCP server.
Smoke test:

```bash
mempalace status
mempalace search "<known term>"
```

Keep the bad restore directory until you are sure no unique data needs manual
salvage.

## Decision table

| Symptom | Prefer | Why |
|---|---|---|
| `repair-status` count mismatch, SQLite counts look plausible | `mempalace repair` | HNSW can usually be rebuilt from SQLite. |
| `repair` still mismatches after retry | Restore | The on-disk vector metadata may be inconsistent beyond repair. |
| SQLite database missing, corrupt, or wrong drawer count | Restore | The source of truth is damaged or incomplete. |
| Search returns no hits after structurally clean restore | Restore matching `origin.json` or re-embed | Embedder identity likely mismatches the restored vector store. |
| A bad import or mining run polluted memory | Restore snapshot before the write | Roll back the whole palace to a known-good point. |
| Only HNSW drift directories exist and search still works | Repair/cleanup | Full restore is unnecessary; see HNSW recovery. |
| Restic repository reports stale locks | `restic unlock`, then `restic check` | The repo may only have an abandoned lock. |
| Restic index or snapshot metadata errors | `restic repair index` / `restic repair snapshots` | Repair repository metadata before attempting restore. |

## Final validation checklist

```bash
mempalace repair-status
mempalace status
mempalace search "<known term>"
restic check
```

Recovery is complete only when the index counts match, drawer counts are sane,
known-term search works, and the backup repository still checks clean.

If you perform manual SQLite checks and the `sqlite3` CLI is missing, Python's
stdlib `sqlite3` module is enough:

```bash
python3 -c "import sqlite3;sqlite3.connect('DB').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
python3 -c "import sqlite3;print(sqlite3.connect('DB').execute('PRAGMA integrity_check').fetchone())"
```
