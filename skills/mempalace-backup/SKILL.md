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

The helper's `--palace` is the **HOME** subtree, default `~/.mempalace`.
DATA defaults to HOME/palace but can be configured elsewhere. `--data-path`
overrides the path environment/configuration. External data roots, links,
hardlinked task storage and data under preserved control directories are
refused rather than silently omitted. The table shows the default layout.

| Path | Back up? | Notes |
|---|---:|---|
| `config.json`, `tunnels.json` | include | Small JSON config/link state. |
| `knowledge_graph.sqlite3` plus `-wal`, `-shm` | include | SQLite KG store; checkpoint before snapshot. |
| `palace/chroma.sqlite3` plus `-wal`, `-shm` | include | Chroma metadata; checkpoint before snapshot. |
| `palace/logstream.sqlite3` plus `-wal`, `-shm` | include | Events, complete task protocol bodies, artifacts and their links share this database. |
| `palace/replica.json` | include | Stable provenance identity; never rotate it as a restore epoch. |
| `.palace-backup.json` | include | Captured layout, expected authorities and task-content digest; not another task authority store. |
| `palace/<uuid>/*.bin` | include | HNSW vector index binaries. |
| `palace/<uuid>.drift-*/` | include | Drift snapshots; keep for recovery context. |
| `palace/.mempalace/origin.json` | **include** | Critical embedder identity; restoring without it breaks search. |
| `wal/` | include | MemPalace write-ahead log. |
| `locks/` | **exclude** | Permanent cooperative lock inodes must remain at their canonical paths; never unlink or restore copies. |

Never add an exclude that can hide `palace/.mempalace/origin.json`.

## Backup safety model

### 1. Establish an offline maintenance boundary

Stop task admission/workers, writable and read-only hubs, CLI/stdio writers,
daemon work and peer sync through their owners. Suppress new launches and
autostarters for the entire capture. Do not kill/adopt discovered PIDs.
`--offline` acknowledges this external condition; it does not prove it.

Daemon-stop acknowledgement, mine-lock age, registry absence and successful WAL
checkpointing are **not** all-writer quiescence. There is no
`daemon wait` wait-for-stop command: that subcommand requires a job ID.

### 2. Use the guarded helper for logstream/task storage

The selected Python must already import the epoch-aware `mempalace_tasks`
package. In a repository checkout, `PYTHONPATH=<repo>/sidecar/src` selects it.
Require preinstalled tools; do not download/install during recovery.

The helper refuses active or malformed/unreadable hub/daemon records, acquires
the real current-user HOME-relative MemPalace writer lease, checkpoints existing
KG/Chroma/logstream databases, then holds bounded-acquisition SQLite
`BEGIN IMMEDIATE` writer exclusions through restic capture and verification.
This keeps each main DB/WAL pair stable. It validates task history/artifacts and
writes `.palace-backup.json` into the same HOME subtree before snapshotting.

The lease does not cover raw CLI/peer/file writes. SQLite exclusions cover
ordinary database writes, not file replacement, HNSW mutation by an uncooperative
process, or arbitrary new launches. Those remain prohibited by the offline
maintenance boundary. Online task backup is not supported.

### 3. Capture

Use environment variables for the local repo and password file; never hard-code
the password and do not use `--insecure-no-password`.

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass

python3 scripts/palace_backup.py --palace ~/.mempalace \
  backup --offline --require-logstream
```

Add `--expected-authority <canonical-uuid>` for each known authority. A captured
manifest also declares known task storage, so later missing data cannot silently
become a legacy empty palace. `--force` and `--no-quiesce` do not bypass any task
safety check. A busy database, corrupt logstream, missing declared storage or
incomplete restore marker is an error.

Genuinely legacy palaces without logstream retain the old best-effort
daemon/mine/checkpoint path and emergency `--force` behavior. That path is not
a task-safe capture. Missing optional legacy logstream creates no database.

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

Wing exports do not export the task journal. Durable tasks require the
whole-palace logstream/artifact recovery unit above.

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

The helper uses stdlib SQLite, preserves HOME versus DATA semantics, and passes
the actual DATA directory to MemPalace CLI operations. Restore defaults to
validation in private staging, without stopping/touching the live target.
Publication preserves HOME's `locks/` and `server/` control directories.

```bash
export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass

./scripts/palace_backup.py backup --offline --require-logstream
./scripts/palace_backup.py checkpoint --require-logstream  # not quiescence proof
./scripts/palace_backup.py verify                 # restic check + repair-status
./scripts/palace_backup.py --dry-run backup --offline
```

Restore lives in the same script (see the `mempalace-restore` skill):
`./scripts/palace_backup.py restore <snapshot> --in-place --offline`.
Tests use the helper's standalone runner, not unittest discovery:
`python3 -W error scripts/test_palace_backup.py`, with the repository's
`sidecar/src` and `sidecar/tests` on `PYTHONPATH` and an external temporary root.

## See also

- [`scripts/palace_backup.py`](scripts/palace_backup.py) — tested Python backup/restore helper.
- [`scripts/palace_wing.py`](scripts/palace_wing.py) — wing-scoped logical export/import.
- [`references/palace-layout.md`](references/palace-layout.md) — HOME vs `palace/` layout and the Chroma-vs-KG path footgun (read before touching `--palace`).
- [`references/restic-cheatsheet.md`](references/restic-cheatsheet.md) — compact restic command reference.
- `mempalace-restore` — recovery workflow, including restore-side `mempalace repair` / `repair-status`.
- `skills/mempalace/references/hnsw-recovery.md` — HNSW drift/index recovery background.
