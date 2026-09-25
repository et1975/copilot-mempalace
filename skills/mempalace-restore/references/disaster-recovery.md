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

## Supported task recovery boundary

Use an already installed epoch-aware sidecar package, MemPalace CLI and restic.
Task truth is DATA/logstream.sqlite3 (events, artifacts and links), together
with DATA/replica.json. There is no matching external pending/head/clock state.
The helper's `--palace` names backup HOME; the MemPalace CLI's `--palace` names
DATA. Custom layout is resolved from the staged snapshot.

The supported workflow is **private staging, then explicit offline publication**.
Stop all task/hub/daemon/raw writers, peer sync and autostarters through their
owners. Maintain no-new-launch exclusion through publication and the next
sidecar's fresh activation/recovery. `--offline`, a missing registry, daemon-stop
acknowledgement and checkpoint success are not independent proof of that condition.
The helper additionally refuses active/indeterminate registry records, acquires
the canonical cooperative writer leases and performs bounded SQLite writer probes.
On Windows, a registry PID is conservatively indeterminate without a verified
native liveness query; no `os.kill(pid, 0)` signal is sent.

Live/online task replacement, raw file replacement during maintenance and mesh
rollback/failover are unsupported. Corrupted live SQLite/configuration that
prevents exclusion/identification is refused rather than hidden by `--force`;
staging-only validation remains available for inspection and controlled salvage.
Native Windows/macOS publication needs platform validation; the automated
SQLite, lock, rollback and real staging-CLI integration checks run on Linux.

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

5. Materialize and validate a private, disjoint stage:

   ```bash
   python3 ../mempalace-backup/scripts/palace_backup.py --palace ~/.mempalace \
     restore latest --target ~/.mempalace-restore-stage --require-logstream
   ```

   The helper uses restic's `snapshot:HOME` subpath form so no original absolute
   path is nested underneath staging. A captured `.palace-backup.json` declares
   required logstream/authorities and the restored domain-content digest.
   For older task snapshots, supply `--require-logstream` and repeatable
   `--expected-authority <uuid>`. Omit those only for genuinely legacy palaces.

6. After establishing the offline boundary, activate and publish:

   ```bash
   python3 ../mempalace-backup/scripts/palace_backup.py --palace ~/.mempalace \
     restore latest --target ~/.mempalace-restore-stage --from-stage \
     --in-place --offline --require-logstream
   ```

   A fresh epoch per initialized authority is appended through the direct
   preinstalled MemPalace CLI while the stage is private. Replay proves each
   accepted/current activation and unchanged domain/artifact/provenance state.
   Only then are contents published with rollback tracking. HOME, `locks/` and
   `server/` controls are not renamed or replaced; previous contents are retained.

7. Nothing is started automatically. Deliberately reopen only the completed
   tree, then let an epoch-aware sidecar activate another successor and recover
   inherited attempts before task admission. Independently reconcile external
   effects. Replica identity is preserved; task epochs do not fix mesh origin
   sequence collisions. Keep peer reinjection excluded.

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

Never swap whole HOME directories: doing so relocates the canonical lock inode.
Keep the original `.bak-*` contents and the unsuccessful stage until recovery
has been verified. Copy the desired old contents to a new private staging
directory on the same filesystem and use `--from-stage --in-place --offline`;
recovery replays the older coherent history and adds new epoch barriers rather
than restoring old execution authorization.

### Interrupted activation

`DATA/.task-restore-incomplete.json` is an administrative marker, not task truth.
Any partial append/ambiguous response is a failed, unpublishable stage.
Preserve it. After fixing the cause, retry the same private stage with
`--from-stage --in-place --offline`. The journal resolves committed history;
fresh successor epochs complete preparation. Never import an old local
pending/head/clock file to settle it.

### Interrupted publication

`HOME/.palace-restore-incomplete.json` records target, stage, rollback directory,
planned old/new names and completed moves. Ordinary exceptions attempt to move
published new entries back to staging and restore old entries from rollback.
If rollback succeeds, the original HOME remains intact and the marker is removed.
If it cannot complete, the command fails with the exact recovery paths and keeps
the marker with `phase=rollback_incomplete`.

After process/power interruption, do not start anything or blindly rerun the
helper. Inspect that marker and all three trees under the offline boundary.
Reconcile entries by their actual locations against the recorded move plan;
never overwrite an existing entry or touch canonical control/lock files.
The marker may lag a completed rename by one step. Preserve all copies until
the chosen tree is complete and independently validated. This is recoverable
multi-entry publication, not an atomic whole-filesystem transaction.

Smoke tests after deliberate reopening:

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

The helper uses Python's stdlib SQLite for inventory/checkpoint/exclusion and
the shared protocol for semantic task validation. Do not use a plain SQLite
open that creates a missing database while investigating lost task storage.
