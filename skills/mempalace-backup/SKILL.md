---
name: mempalace-backup
description: Use when the user wants to back up, snapshot, protect, export, or make a second copy of a local MemPalace palace; mentions restic, backup retention, palace safety, or recovering from future disk loss. Use for the backup side only; use mempalace-restore for recovery.
---

# MemPalace Backup

MemPalace has no first-class backup command: `sync` prunes ignored sources and
`repair` rebuilds indexes after restore. Back up the local palace with `restic`
instead. Scope is **local restic repos only** (external drive or another folder),
on demand, with no cron/systemd installed by this skill.

## What's in the palace

Everything lives under `~/.mempalace/`.

| Path | Back up? | Notes |
|---|---:|---|
| `config.json`, `tunnels.json` | include | Small JSON config/link state. |
| `knowledge_graph.sqlite3` plus `-wal`, `-shm` | include | SQLite KG store; checkpoint before snapshot. |
| `palace/chroma.sqlite3` plus `-wal`, `-shm` | include | Chroma metadata; checkpoint before snapshot. |
| `palace/<uuid>/*.bin` | include | HNSW vector index binaries. |
| `palace/<uuid>.drift-*/` | include | Drift snapshots; keep for recovery context. |
| `palace/.mempalace/origin.json` | **include** | Critical embedder identity; restoring without it breaks search. |
| `wal/` | include | MemPalace write-ahead log. |
| `locks/` | **exclude** | Ephemeral `mine_palace_*.lock` / `*.lock`; stale restores are harmful. |

Never add an exclude that can hide `palace/.mempalace/origin.json`.

## Backup safety model

### 1. Quiesce writers

Stop the opt-in long-lived daemon if it is running:

```bash
mempalace daemon status
mempalace daemon stop
mempalace daemon wait
```

Check for active mining/sweeping before the snapshot:

```bash
mempalace status
ls -lt ~/.mempalace/locks/mine_palace_*.lock 2>/dev/null | head
```

If a mine lock is fresh or `mempalace status` shows active work, wait or abort.
Do **not** take a mid-mine snapshot. The live MCP server is hosted by the current
harness and should not be killed mid-session; avoiding active mines plus SQLite
WAL checkpointing is the mitigation.

### 2. Checkpoint SQLite WALs

Flush both SQLite databases so restic captures a single consistent main DB file:

```bash
sqlite3 ~/.mempalace/knowledge_graph.sqlite3 'PRAGMA wal_checkpoint(TRUNCATE);'
sqlite3 ~/.mempalace/palace/chroma.sqlite3 'PRAGMA wal_checkpoint(TRUNCATE);'
```

If `command -v sqlite3` fails, use the Python fallback below instead; Python's
`sqlite3` module is part of the standard library.

```bash
python3 - <<'PY'
import os
import sqlite3

palace = os.path.expanduser("~/.mempalace")
for db in ("knowledge_graph.sqlite3", "palace/chroma.sqlite3"):
    con = sqlite3.connect(os.path.join(palace, db))
    print(db, con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    con.close()
PY
```

If checkpointing cannot truncate because a writer still holds the WAL, prefer to
abort and retry after writers stop. Emergency fallback: make explicit consistent
SQLite copies and include them in the same restic snapshot with a clear tag:

```bash
STAGE=~/mempalace-backup-staging
mkdir -p "$STAGE/palace"
sqlite3 ~/.mempalace/knowledge_graph.sqlite3 "VACUUM INTO '$STAGE/knowledge_graph.sqlite3';"
sqlite3 ~/.mempalace/palace/chroma.sqlite3 "VACUUM INTO '$STAGE/palace/chroma.sqlite3';"
```

Without the CLI, use the same Python module and parameterized `VACUUM INTO`:

```bash
python3 - <<'PY'
import os
import sqlite3

palace = os.path.expanduser("~/.mempalace")
stage = os.path.expanduser("~/mempalace-backup-staging")
os.makedirs(os.path.join(stage, "palace"), exist_ok=True)
for src, dest in (
    ("knowledge_graph.sqlite3", "knowledge_graph.sqlite3"),
    ("palace/chroma.sqlite3", "palace/chroma.sqlite3"),
):
    con = sqlite3.connect(os.path.join(palace, src))
    con.execute("VACUUM INTO ?", (os.path.join(stage, dest),))
    con.close()
PY
```

### 3. Snapshot

Use environment variables for the local repo and password file; never hard-code
the password and do not use `--insecure-no-password`.

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass

restic backup ~/.mempalace \
  --exclude ~/.mempalace/locks \
  --tag palace \
  --tag "$(hostname)"
```

If using the `VACUUM INTO` fallback, include the staging path too:

```bash
restic backup ~/.mempalace ~/mempalace-backup-staging \
  --exclude ~/.mempalace/locks \
  --tag palace \
  --tag "$(hostname)" \
  --tag sqlite-vacuum-copy
```

### 4. Verify

```bash
restic snapshots --tag palace
restic check
```

Periodically run a deeper sampled check:

```bash
restic check --read-data-subset=5%
```

### 5. Retain

Retention is manual/on-demand only. Do not install a scheduler from this skill.

```bash
restic forget --keep-last N --keep-daily D --keep-weekly W --prune
```

## First-time setup

Create a local restic repository, usually on an external drive or another local
folder. Store the password in a file with mode `600`; never paste it into docs,
logs, commands, or committed files.

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass
chmod 600 "$RESTIC_PASSWORD_FILE"

restic init
```

## Wing-scoped logical export

`restic` backup is **physical and whole-palace only** — wings share the same
ChromaDB collections, HNSW index, and SQLite databases, so a file snapshot cannot
isolate one wing. When you need *one wing* (to archive, move, or clone it), use
the **logical** exporter [`scripts/palace_wing.py`](scripts/palace_wing.py), which
reads a wing's contents straight from the palace SQLite and writes either a
portable JSONL bundle or a human-readable markdown directory.

```bash
# Export needs NO restic and NO mempalace import — it reads the palace SQLite directly.
# --palace is the mempalace HOME dir (~/.mempalace), NOT the nested palace/ dir.
./scripts/palace_wing.py export <wing> --out wing-<wing>.jsonl
./scripts/palace_wing.py export copilot-mempalace --palace ~/.mempalace

# Human-readable, git/OneDrive-friendly markdown directory (lossless round-trip):
./scripts/palace_wing.py export <wing> --format md --out backups/mempalace-wings
```

A bundle contains: **drawers** (multi-chunk drawers reassembled), **best-effort
KG triples** (only those whose `source_drawer_id` resolves to the wing — others
are counted and skipped in the manifest `kg_note`), and **tunnels** that touch the
wing. Closets are **not** exported — they regenerate from drawers on import.

`--format md` writes `<out>/<wing>/` with **one markdown file per drawer**
(verbatim content under an HTML metadata header), structured `kg.jsonl` /
`tunnels.jsonl`, and a `manifest.json` index. One-file-per-drawer keeps drawer
boundaries unambiguous — unlike the legacy one-file-per-room OneDrive export,
which merged drawers and forced heuristic re-splitting on import. Both formats
import via the same `palace_wing.py import` (auto-detected); see
`mempalace-restore`.

**This is a logical bundle, not a byte snapshot.** It is complementary to restic:
use restic for whole-palace disaster recovery, and the wing bundle for
per-wing archival, migration, or cloning. Import is documented in the
`mempalace-restore` skill.

## Helper script

For a deterministic, idempotent version of the whole flow, use the bundled
Python helper [`scripts/palace_backup.py`](scripts/palace_backup.py). It quiesces
writers, checkpoints both SQLite WALs via Python's stdlib `sqlite3` (no CLI
dependency), snapshots with the correct excludes/tags, and verifies — and it
restores with restic's absolute-path-stripping subpath syntax. It forwards
`--palace` to every `mempalace` subcommand so it never touches the wrong palace.

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass

./scripts/palace_backup.py backup                 # quiesce + checkpoint + snapshot + verify
./scripts/palace_backup.py checkpoint             # WAL-checkpoint both DBs only
./scripts/palace_backup.py verify                 # restic check + repair-status
./scripts/palace_backup.py --dry-run backup       # echo commands without running them
```

Restore lives in the same script (see the `mempalace-restore` skill):
`./scripts/palace_backup.py restore <snapshot> --in-place`. Tests:
`python3 scripts/test_palace_backup.py`.

## See also

- [`scripts/palace_backup.py`](scripts/palace_backup.py) — tested Python backup/restore helper.
- [`scripts/palace_wing.py`](scripts/palace_wing.py) — wing-scoped logical export/import.
- [`references/palace-layout.md`](references/palace-layout.md) — HOME vs `palace/` layout and the Chroma-vs-KG path footgun (read before touching `--palace`).
- [`references/restic-cheatsheet.md`](references/restic-cheatsheet.md) — compact restic command reference.
- `mempalace-restore` — recovery workflow, including restore-side `mempalace repair` / `repair-status`.
- `skills/mempalace/references/hnsw-recovery.md` — HNSW drift/index recovery background.
