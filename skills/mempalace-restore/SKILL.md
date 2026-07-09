---
name: mempalace-restore
description: Use when the user wants to restore, recover, import, or rebuild a mempalace palace; when the palace is corrupted or lost; for disaster recovery; to roll back memory to a snapshot; or when the user says their memory or palace is broken.
---

# MemPalace Restore

Recover a local MemPalace palace from a `restic` snapshot when `~/.mempalace/`
is lost, corrupted, accidentally changed, or needs to roll back to an earlier
state. MemPalace has no native backup/restore command; the `mempalace-backup`
skill creates `restic` snapshots of `~/.mempalace/` excluding ephemeral
`locks/`.

**Default philosophy: make every restore reversible.** Stop writers, inspect the
snapshot, move the current palace aside, then restore into place. Never
overwrite `~/.mempalace/` as the first move unless this is full-machine disaster
recovery and there is no current palace.

## Restore runbook

Use a local `restic` repository only. Keep the shared env conventions aligned
with `mempalace-backup`:

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass
```

`RESTIC_PASSWORD_FILE` should be `chmod 600`. Never hard-code the password and
avoid `--insecure-no-password`. See
[`mempalace-backup`'s restic cheatsheet](../mempalace-backup/references/restic-cheatsheet.md)
for repo setup and the full command menu.

### 1. Stop writers

```bash
mempalace daemon stop
mempalace daemon status
```

Restore when the palace is not actively being served. After swapping files,
reopen it with MCP `mempalace_reconnect`, or restart the harness/MCP server if
that tool is unavailable.

### 2. Pick a snapshot

```bash
restic snapshots --tag palace
restic ls <snapshot-id>
restic find origin.json
restic find chroma.sqlite3
```

Use `latest` only when you are confident the newest snapshot is the desired
state. For rollback, choose the specific snapshot before the bad write.

### 3. Restore safely, then swap

Safe default: restore into a staging directory, inspect, then move into place.

```bash
restic restore <snapshot-id>:"$HOME/.mempalace" --target /tmp/palace-restore
ls /tmp/palace-restore
test -f /tmp/palace-restore/palace/.mempalace/origin.json

mv ~/.mempalace ~/.mempalace.bak-$(date +%Y%m%d-%H%M%S)
mv /tmp/palace-restore ~/.mempalace
```

Full-machine disaster recovery variant, after confirming there is no live
palace to preserve:

```bash
mkdir -p ~/.mempalace && restic restore <snapshot-id>:"$HOME/.mempalace" --target ~/.mempalace
```

Or restore the snapshot back to its original absolute locations:

```bash
restic restore <snapshot-id> --target /
```

Inspect the restored tree shape. `restic` stores full absolute source paths, so
a plain `restic restore <snapshot-id> --target ~/.mempalace` recreates the whole
path under the target, for example
`~/.mempalace/home/<user>/.mempalace/config.json`. Use the
`<snapshot-id>:"$HOME/.mempalace"` subpath syntax to strip that prefix so
`config.json`, `palace/`, `knowledge_graph.sqlite3`, `wal/`, and `tunnels.json`
land directly under the restore target.

### 4. Rebuild and validate the index

```bash
mempalace repair
mempalace repair-status
```

`repair-status` is read-only. Expect SQLite row count and HNSW element count to
match. If they do not, use
[references/disaster-recovery.md](references/disaster-recovery.md) and
[`mempalace` HNSW recovery](../mempalace/references/hnsw-recovery.md).

If rows were poisoned by an interrupted index update, try:

```bash
mempalace repair --mode max-seq-id
mempalace repair-status
```

### 5. Reopen the running MCP server

Use MCP `mempalace_reconnect` after external file swaps so the server
invalidates caches and reopens the restored palace. If MCP is not available,
restart the harness or MCP server instead.

### 6. Smoke test

```bash
mempalace status
mempalace search "<known term>"
```

Drawer counts should be sane, and a known term should return expected hits. A
healthy `status` with empty or broken search usually points to an embedder
identity problem.

## Critical: embedder identity

`~/.mempalace/palace/.mempalace/origin.json` is **critical**. It records the
embedder identity used to build the vector index. If a restore lacks
`origin.json`, or it mismatches the current embedder, semantic search can return
nothing or fail even when SQLite data exists.

Before declaring recovery complete:

```bash
test -f ~/.mempalace/palace/.mempalace/origin.json
mempalace search "<known term>"
```

If the file is missing or mismatched, restore the matching `origin.json` from the
same snapshot as the palace, or re-embed/rebuild the palace under the intended
embedder. Do not mix an index and origin metadata from different embedder
configurations.

## Verify recovery

Use both structural and behavioral checks:

```bash
mempalace repair-status
mempalace status
mempalace search "<known term>"
restic check
```

- `repair-status`: SQLite rows and HNSW elements match.
- `status`: wings/rooms/drawers are plausible for the expected snapshot.
- `search`: known terms return hits, proving the embedder/origin pairing works.
- `restic check`: the local backup repository is readable for future restores.

If you perform manual SQLite checks and the `sqlite3` CLI is missing, Python's
stdlib `sqlite3` module is enough:

```bash
python3 -c "import sqlite3;sqlite3.connect('DB').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
python3 -c "import sqlite3;print(sqlite3.connect('DB').execute('PRAGMA integrity_check').fetchone())"
```

`locks/` was excluded from backup and is ephemeral; do not try to restore it.

## Import a wing bundle

Whole-palace restic restore has a logical counterpart: importing a **single
wing** produced by the `mempalace-backup` skill's
[`palace_wing.py`](../mempalace-backup/scripts/palace_wing.py) exporter. Use it to
restore, migrate, or clone one wing without touching the rest of the palace. Two
input formats are auto-detected:

- a **JSONL bundle** (`export`, or `export --format jsonl`), and
- a **markdown directory** (`export --format md`) — pass the wing dir or its
  `manifest.json`. The legacy one-file-per-room OneDrive export is also read
  (best-effort: drawers split at `## ` headers, prose KG parsed).

```bash
# Import needs mempalace importable — run under the interpreter where mempalace
# is installed (e.g. the uv-tool venv), not necessarily system python3.
# --palace is the mempalace HOME dir (~/.mempalace), NOT the nested palace/ dir.
./scripts/palace_wing.py import wing-<wing>.jsonl --palace ~/.mempalace

# Markdown directory (or its manifest.json), incl. legacy OneDrive exports:
./scripts/palace_wing.py import backups/mempalace-wings/<wing> --palace ~/.mempalace

# Preview without writing anything:
./scripts/palace_wing.py import wing-<wing>.jsonl --dry-run

# Clone into a different wing name (implies no dedup):
./scripts/palace_wing.py import wing-<wing>.jsonl --into-wing <new-wing>
```

> **`--palace` = HOME, and the stray-palace guard.** `--palace` must be the
> mempalace HOME (`~/.mempalace`), never the nested `~/.mempalace/palace` DB dir.
> Chroma resolves under `palace_path` while the KG is HOME-level; pointing
> `--palace` at the DB dir used to silently create a second, invisible palace.
> Import now **aborts** if the target has no existing Chroma DB (pass
> `--create-new-palace` to intentionally initialize a fresh one) and prints the
> resolved Chroma/KG paths + drawer counts before writing.

Behavior and caveats:

- **Replay, not byte restore.** Drawers are re-added (new IDs, re-embedded).
  Metadata `add_drawer` cannot set (topic/hall/type/date) is preserved via a
  content trailer.
- **Idempotent merge.** Re-importing into the same wing skips near-duplicates via
  `check_duplicate` (`--dup-threshold`). `--into-wing`/`--force-add` bypass dedup
  (palace-wide dedup would otherwise skip a clone).
- **KG is best-effort.** Only triples whose `source_drawer_id` resolved to the
  wing were exported; on import `source_drawer_id` is dropped (old IDs are
  invalid). KG triples are written directly to the palace KG.
- **Tunnels import last** and only if both endpoint rooms exist; the handler
  validates rooms and skipped tunnels are reported (dangling tunnels are
  impossible).
- **Stop the daemon / reopen after.** Import writes through a live ChromaDB
  client. Stop `mempalace daemon`, then reopen with MCP `mempalace_reconnect`
  (or restart the MCP server). Freshly added drawers materialize on flush, so
  don't expect them in `embedding_metadata` the instant import returns.

## See also

- [`../mempalace-backup/scripts/palace_wing.py`](../mempalace-backup/scripts/palace_wing.py)
  — wing bundle export/import (this skill covers `import`).
- [`../mempalace-backup/scripts/palace_backup.py`](../mempalace-backup/scripts/palace_backup.py)
  — the same helper also restores: `palace_backup.py restore <snapshot> --in-place`.
- [Disaster recovery](references/disaster-recovery.md) — scenario-based restore,
  repair, repo recovery, and rollback runbooks.
- [`mempalace-backup`](../mempalace-backup/SKILL.md) — creates the `restic`
  snapshots this skill restores.
- [`mempalace-backup` restic cheatsheet](../mempalace-backup/references/restic-cheatsheet.md)
  — repo/env setup and full restic command menu.
- [`mempalace` HNSW recovery](../mempalace/references/hnsw-recovery.md) —
  focused vector-index drift and rebuild guidance.
