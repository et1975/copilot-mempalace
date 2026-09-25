---
name: mempalace-restore
description: Use when the user wants to restore, recover, import, or rebuild a mempalace palace; when the palace is corrupted or lost; for disaster recovery; to roll back memory to a snapshot; or when the user says their memory or palace is broken.
---

# MemPalace Restore

Recover a local MemPalace palace from a `restic` snapshot when `~/.mempalace/`
is lost, corrupted, accidentally changed, or needs to roll back to an earlier
state. MemPalace has no native backup/restore command; the `mempalace-backup`
skill creates `restic` snapshots of the HOME subtree excluding `locks/`.
Task events, accepted history, epoch controls, complete source notes and native
artifacts live in DATA/logstream.sqlite3 with DATA/replica.json. No matching
external pending/head/clock recovery directory is required.

**Default: validate private staging without touching the live target.**
Publication is explicit, offline and reversible. Never restore directly over
live files or rename the whole HOME directory: that relocates its cooperative
lock namespace and allows a second lock inode.

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

### 1. Prepare the offline boundary before publication

Staging alone does not require stopping the live target. Before `--in-place`,
stop task admission/workers, hubs (including read-only hubs), daemon/CLI/stdio
writers and peer sync, and suppress all new launches/autostarters. Use their
owners; never kill/adopt a discovered PID. Keep this boundary through later
fresh sidecar activation and recovery.

The helper requires `--offline`, rejects active/indeterminate registered writers,
and acquires real cooperative leases. The flag and missing registry records are
not proofs of quiescence. Daemon-stop acknowledgement is not drain completion,
and `daemon wait` requires a job ID, not a wait-for-stop invocation.

### 2. Pick a snapshot

```bash
restic snapshots --tag palace
restic ls <snapshot-id>
restic find origin.json
restic find chroma.sqlite3
```

Use `latest` only when you are confident the newest snapshot is the desired
state. For rollback, choose the specific snapshot before the bad write.

### 3. Materialize and validate private staging

Use a preinstalled Python that can import the epoch-aware `mempalace_tasks`
package (`PYTHONPATH=<repo>/sidecar/src` in a checkout), plus preinstalled restic
and MemPalace CLI. Never install tools implicitly during restore.

```bash
python3 ../mempalace-backup/scripts/palace_backup.py --palace ~/.mempalace \
  restore <snapshot-id> --target ~/.mempalace-restore-stage --require-logstream
```

The default target is a unique sibling of HOME; explicit targets must be disjoint
and empty. Custom DATA layout comes from the snapshot's manifest/staged config,
not old live config or ambient path variables. Global `--data-path` can select
an in-HOME layout for old snapshots. Task history is replayed with the shared
v1/v2 protocol, and replica, artifact hashes/sizes and links are verified.
Use repeatable `--expected-authority <uuid>` when no captured manifest declares
the expected authorities. Missing required task data is never a legacy fallback.
Required membership comes from the selected snapshot and explicit requirements,
not newer live state. Authorities created after that snapshot may legitimately
be absent; explicitly requiring one still rejects the older snapshot.

Inspect the restored tree shape. `restic` stores full absolute source paths, so
a plain `restic restore <snapshot-id> --target ~/.mempalace` recreates the whole
path under the target, for example
`~/.mempalace/home/<user>/.mempalace/config.json`. Use the
`<snapshot-id>:"$HOME/.mempalace"` subpath syntax to strip that prefix so
`config.json`, `palace/`, `knowledge_graph.sqlite3`, `wal/`, and `tunnels.json`
land directly under the restore target.

### 4. Activate in private staging, then publish offline

```bash
python3 ../mempalace-backup/scripts/palace_backup.py --palace ~/.mempalace \
  restore <snapshot-id> --target ~/.mempalace-restore-stage \
  --from-stage --in-place --offline --require-logstream
```

`--from-stage` skips restic and uses the existing private tree. While holding
cooperative leases, the helper appends a fresh v2 epoch for every initialized
authority using the installed direct `mempalace --palace <stage-DATA> logstream
append` API, then replays to prove each epoch accepted/current. Task IDs,
accepted domain history, holds, full notes, artifact hashes/metadata/links and
replica identity must remain unchanged. The timestamp is no earlier than
replayed effective-time high water. No task claims/workers are created.

Only then are contents published using reversible same-filesystem moves.
HOME itself, canonical lock inodes and existing `locks/`/`server/` directories
stay in place. Old contents remain in a unique sibling `.bak-*` directory.
SQLite writer probes are bounded; database handles close before native Windows
renames while cooperative leases remain held. Uncooperative raw file writers or
new launches invalidate the required offline boundary.

Any partial append or publication is an error, not success. Keep staging and
rollback contents for inspection. See the administrative marker recovery steps
in [disaster recovery](references/disaster-recovery.md). Nothing is started
automatically, including repair, a hub, daemon, worker or service.

### 5. Deliberately reopen and recover

After publication, an epoch-aware sidecar starts with another fresh successor
and fences/reconciles inherited attempts before execution admission. Old v1
sidecars cannot serve the new journal. Old pending/head/clock files, whether
missing or from a discarded future, are ignored; never replay them into the
restored palace. Reconnect is not a restore barrier or permission to leave an old
hub running through publication.

External effects still need reconciliation. Mesh origin-sequence rollback,
transparent replicated failover and independent rollback detection are not
provided; fresh task epochs do not repair replication provenance collisions.
Do not resume peer reinjection for a restored single-hub authority.

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

The helper uses stdlib SQLite; no sqlite3 executable is required. A checkpoint
is not an exclusion proof. `locks/` is excluded from snapshots but its canonical
live inodes are permanent coordination objects: never unlink/replace them.
If index repair is needed afterward, use the actual DATA path with
`mempalace --palace <DATA> repair` / `repair-status` during a separate controlled
maintenance window.

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
