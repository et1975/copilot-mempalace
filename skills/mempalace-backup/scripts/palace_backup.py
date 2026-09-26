#!/usr/bin/env python3
"""Safe, idempotent backup / restore helper for a local MemPalace palace.

This is the deterministic ("mechanical") companion to the ``mempalace-backup``
and ``mempalace-restore`` skills. The skills document *why* and *when*; this
script makes the *how* reproducible: guarded offline task capture, SQLite
inventory/checkpoint, ``restic`` snapshots (excluding control locks), and restore
via restic's absolute-path-stripping subpath syntax.

Design notes
------------
* **HOME is not the data directory.** ``--palace`` is the physical HOME subtree.
  Data is resolved from ``--data-path``, the MemPalace path environment variables,
  HOME/config.json's ``palace_path``, then HOME/palace. Configured paths must be
  absolute; the explicit override also accepts paths relative to the caller.
  Data outside HOME, symlinked storage, and storage under excluded locks fail.
* **Offline tasks only.** Logstream events/artifacts share data/logstream.sqlite3;
  data/replica.json preserves provenance. Task capture requires --offline plus
  real cooperative/SQLite writer guards. The flag acknowledges stopped raw
  writers, peer sync and autostarters; it is not independent proof of that.
* **Private activation before publication.** Stage validation and fresh task
  epochs precede reversible content moves. HOME, locks/ and server/ never move.
  No service/worker/hub is started; lifecycle admission remains an operator duty.
* **Local restic repos only.** Repo and password come from the environment
  (``RESTIC_REPOSITORY`` / ``RESTIC_PASSWORD_FILE``); this script never handles a
  password value and never accepts one on the command line.
* **No ``sqlite3`` CLI dependency.** WAL checkpointing uses Python's stdlib
  ``sqlite3`` module, so it works on minimal machines where the CLI is absent.
* **Pure command builders.** ``build_backup_cmd`` / ``build_restore_cmd`` /
  ``build_check_cmd`` return argv lists with no side effects, so they are unit
  testable without invoking restic.
* **restic stores absolute paths.** Restoring a palace backed up as
  ``~/.mempalace`` therefore uses ``restic restore <snap>:<abs> --target <dir>``
  so files land directly under the target instead of nested under the original
  absolute path.

Usage::

    export RESTIC_REPOSITORY=/mnt/backup/mempalace-restic
    export RESTIC_PASSWORD_FILE=~/.config/mempalace-restic.pass

    ./palace_backup.py checkpoint            # inventory + WAL-checkpoint SQLite DBs
    ./palace_backup.py backup --offline --require-logstream
    ./palace_backup.py restore <snapshot>    # validate private staging only
    ./palace_backup.py restore <snapshot> --in-place --offline
    ./palace_backup.py verify                # restic check (+ palace repair-status)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager, ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_PALACE = Path("~/.mempalace").expanduser()

# Do not snapshot control locks or replace their permanent live inodes.
EXCLUDE_DIRS = ("locks",)

# Critical file: the embedder identity. A restore missing this breaks search.
ORIGIN_JSON = "palace/.mempalace/origin.json"
BACKUP_MANIFEST = ".palace-backup.json"
RESTORE_MARKER = ".palace-restore-incomplete.json"
CONTROL_DIRS = {"locks", "server"}

LOGSTREAM_COLUMNS = {
    "events": {
        "id", "type", "stream", "room", "from_agent", "to_agent", "correlation_id",
        "branch", "base_commit", "status", "body", "created_at", "metadata_json",
    },
    "artifacts": {
        "id", "kind", "sha256", "size_bytes", "content", "created_by",
        "created_at", "metadata_json",
    },
    "event_artifacts": {"event_id", "artifact_id"},
}


class BackupError(RuntimeError):
    """Unsafe or incomplete physical palace inventory; never bypassed by force."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _covered_path(palace: Path, path: Path) -> Path:
    """Reject paths restic cannot physically include, without following links."""
    raw_home, raw_path = palace.expanduser().absolute(), path.expanduser().absolute()
    home, path = _absolute(raw_home), _absolute(raw_path)
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise BackupError(f"Storage path is outside backup subtree {home}: {path}") from None
    if relative.parts and relative.parts[0] in CONTROL_DIRS:
        raise BackupError(f"Storage path is under an excluded/preserved control directory: {path}")
    # Inspect before collapsing "..": link/../data can point outside HOME even
    # when its lexically normalized spelling appears to be covered.
    for raw in (raw_home, raw_path):
        for current in (*reversed(raw.parents), raw):
            if current.is_symlink():
                raise BackupError(f"Storage coverage cannot follow symlink: {current}")
    return path


def resolve_data_path(
    palace: Path, data_path: Path | None = None, *, allow_missing: bool = False,
) -> Path:
    """Resolve data without importing MemPalace or reading another HOME's config.

    ``allow_missing`` is for restore command construction before the tree exists,
    not for inventory/checkpoint. No directories or configuration are created.
    """
    home = _covered_path(palace, palace)
    env_path = os.environ.get("MEMPALACE_PALACE_PATH") or os.environ.get("MEMPAL_PALACE_PATH")
    if data_path is not None:
        selected = Path(data_path)
    elif env_path:
        selected = Path(env_path)
    else:
        config_file = _covered_path(home, home / "config.json")
        try:
            config = json.loads(config_file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            config = {}
        except (OSError, ValueError) as exc:
            raise BackupError(f"Cannot read config {config_file}: {exc}") from exc
        if not isinstance(config, dict):
            raise BackupError(f"Config must be a JSON object: {config_file}")
        if "palace_path" in config:
            value = config["palace_path"]
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise BackupError(f"Invalid palace_path in config: {config_file}")
            selected = Path(value).expanduser()
            if not selected.is_absolute():
                raise BackupError(
                    f"Ambiguous relative palace_path in config {config_file}; "
                    "supply an explicit --data-path.") from None
        else:
            selected = home / "palace"
    selected = _covered_path(home, selected)
    if selected.exists() and not selected.is_dir():
        raise BackupError(f"Data path is not a directory: {selected}")
    if not allow_missing and not selected.is_dir():
        raise BackupError(f"Data directory not found: {selected}")
    return selected


def sqlite_paths(palace: Path, data_path: Path) -> tuple[Path, ...]:
    """Database locations: KG stays HOME-relative, Chroma/logstream use data."""
    return (palace / "knowledge_graph.sqlite3",
            data_path / "chroma.sqlite3", data_path / "logstream.sqlite3")


def _validate_logstream(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as con:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, required in LOGSTREAM_COLUMNS.items():
            if table not in tables:
                raise BackupError(f"Invalid logstream {db_path}: missing table {table}")
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                missing = ", ".join(sorted(required - columns))
                raise BackupError(f"Invalid logstream {db_path}: {table} missing {missing}")
    replica = db_path.parent / "replica.json"
    try:
        value = json.loads(replica.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"Missing or invalid logstream replica {replica}: {exc}") from exc
    replica_id = value.get("replica_id") if isinstance(value, dict) else None
    if not isinstance(replica_id, str) or not re.fullmatch(
            r"rep_(?:[0-9a-f]{12}|[0-9a-f]{32})", replica_id):
        raise BackupError(f"Invalid logstream replica identity: {replica}")


def backup_inventory(
    palace: Path, data_path: Path | None = None, *, require_logstream: bool = False,
) -> tuple[Path, ...]:
    """Validate physical SQLite/provenance coverage and return existing files.

    Events AND artifact bodies live in logstream.sqlite3; no external task
    recovery directory is needed. Absent logstream is allowed for legacy palaces
    unless the caller declares task storage with ``require_logstream``. This
    checks SQLite structure, not task envelopes, hashes, or referenced content.
    The lifecycle controller must hold exclusion while using this inventory;
    checking paths/SQLite cannot prevent a concurrent writer or symlink swap.
    """
    home = _covered_path(palace, palace)
    data = resolve_data_path(home, data_path)
    databases = sqlite_paths(home, data)
    candidates = tuple(
        Path(str(db) + suffix) for db in databases for suffix in ("", "-wal", "-shm")
    ) + (data / "replica.json",)
    files = []
    for candidate in candidates:
        _covered_path(home, candidate)
        if candidate.exists():
            if not candidate.is_file():
                raise BackupError(f"Expected a storage file: {candidate}")
            files.append(candidate)
    logstream = data / "logstream.sqlite3"
    if require_logstream and logstream not in files:
        raise BackupError(f"Required logstream database is missing: {logstream}")
    for db in databases:
        if db not in files:
            if any(Path(str(db) + suffix) in files for suffix in ("-wal", "-shm")):
                raise BackupError(f"Database is missing but SQLite sidecars remain: {db}")
            continue
        try:
            result = integrity_check(db)
            if result != "ok":
                raise BackupError(f"SQLite integrity failure in {db}: {result}")
            if db == logstream:
                _validate_logstream(db)
        except sqlite3.Error as exc:
            raise BackupError(f"Invalid SQLite database {db}: {exc}") from exc
    return tuple(files)


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects) — unit testable without restic or a palace.
# --------------------------------------------------------------------------- #
def build_backup_cmd(palace: Path, tags: list[str]) -> list[str]:
    """restic argv to snapshot ``palace``, excluding ephemeral lock dirs."""
    cmd = ["restic", "backup", str(palace)]
    for d in EXCLUDE_DIRS:
        cmd += ["--exclude", str(palace / d)]
    for tag in tags:
        cmd += ["--tag", tag]
    return cmd


def build_restore_cmd(snapshot: str, palace: Path, target: Path) -> list[str]:
    """restic argv restoring the palace subtree directly under ``target``.

    Uses ``<snapshot>:<absolute-palace-path>`` so restic strips the stored
    absolute prefix and writes palace contents (``config.json``, ``palace/`` …)
    straight into ``target`` rather than nested under the original path.
    """
    return ["restic", "restore", f"{snapshot}:{palace}", "--target", str(target)]


def build_check_cmd(read_data_subset: str | None = None) -> list[str]:
    """restic argv to verify the repository; optional sampled data read."""
    cmd = ["restic", "check"]
    if read_data_subset:
        cmd.append(f"--read-data-subset={read_data_subset}")
    return cmd


def mempalace_cmd(data_path: Path, *args: str) -> list[str]:
    """Build a ``mempalace`` argv bound to the resolved DATA path, not HOME.

    ``--palace`` is a *global* flag that must precede the subcommand. Forwarding
    it is critical: without it, ``mempalace repair`` / ``daemon stop`` silently
    target a different data directory/daemon instead of the one being backed up.
    """
    return ["mempalace", "--palace", str(data_path), *args]


def default_tags() -> list[str]:
    """Standard tags applied to palace snapshots: ``palace`` + the hostname."""
    return ["palace", socket.gethostname()]


def timestamped_backup_dir(palace: Path, now: datetime | None = None) -> Path:
    """Sibling ``<palace>.bak-YYYYmmdd-HHMMSS`` path for reversible swaps."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return palace.with_name(palace.name + f".bak-{stamp}")


# --------------------------------------------------------------------------- #
# SQLite checkpointing (stdlib module — no sqlite3 CLI required).
# --------------------------------------------------------------------------- #
def checkpoint_db(db_path: Path) -> tuple[int, int, int]:
    """WAL-checkpoint one SQLite DB in TRUNCATE mode; return the pragma result.

    Returns ``(busy, log_frames, checkpointed_frames)``. ``busy == 0`` means the
    checkpoint completed without a blocking writer at that instant, not that
    all writers are quiesced. ``mode=rw`` refuses to create a missing database.
    """
    con = sqlite3.connect(_absolute(db_path).as_uri() + "?mode=rw", uri=True, timeout=0.25)
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    return tuple(row)  # type: ignore[return-value]


def integrity_check(db_path: Path) -> str:
    """Return the SQLite ``integrity_check`` result (``"ok"`` when healthy)."""
    con = sqlite3.connect(_absolute(db_path).as_uri() + "?mode=ro", uri=True)
    try:
        return "\n".join(row[0] for row in con.execute("PRAGMA integrity_check"))
    finally:
        con.close()


def checkpoint_all(
    palace: Path, data_path: Path | None = None, *, require_logstream: bool = False,
) -> bool:
    """Validate inventory, then checkpoint; True is NOT all-writer exclusion.

    This is the lifecycle integration hook: hold writer exclusion before calling
    and through the subsequent physical snapshot. Invalid/missing declared data
    raises BackupError; only a busy checkpoint returns False.
    """
    palace = _covered_path(palace, palace)
    data = resolve_data_path(palace, data_path)
    files = backup_inventory(palace, data, require_logstream=require_logstream)
    all_clean = True
    for db in sqlite_paths(palace, data):
        rel = db.relative_to(palace)
        if db not in files:
            print(f"  skip (absent): {rel}", file=sys.stderr)
            continue
        try:
            busy, log, ckpt = checkpoint_db(db)
        except sqlite3.Error as exc:
            raise BackupError(f"Cannot checkpoint SQLite database {db}: {exc}") from exc
        state = "clean" if busy == 0 else "BUSY (writer holds WAL)"
        print(f"  checkpoint {rel}: busy={busy} log={log} ckpt={ckpt} -> {state}",
              file=sys.stderr)
        all_clean = all_clean and busy == 0
    return all_clean


# --------------------------------------------------------------------------- #
# Environment / process helpers.
# --------------------------------------------------------------------------- #
def require_restic_env() -> None:
    """Fail fast if the local restic repo env is not configured."""
    if not os.environ.get("RESTIC_REPOSITORY"):
        sys.exit("RESTIC_REPOSITORY is not set. Point it at your local restic repo.")
    if not (os.environ.get("RESTIC_PASSWORD_FILE") or os.environ.get("RESTIC_PASSWORD")):
        sys.exit("Set RESTIC_PASSWORD_FILE (recommended) so restic can unlock the repo.")


def has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def run(cmd: list[str], *, dry_run: bool = False) -> int:
    """Echo and run a command; in dry-run just echo. Return the exit code."""
    print("+ " + " ".join(cmd), file=sys.stderr)
    if dry_run:
        return 0
    return subprocess.call(cmd)


def fresh_mine_locks(palace: Path, max_age_s: int = 900) -> list[Path]:
    """Return mine locks modified within ``max_age_s`` — a sign of active work."""
    locks_dir = palace / "locks"
    if not locks_dir.is_dir():
        return []
    now = datetime.now().timestamp()
    fresh = []
    for p in locks_dir.glob("mine_palace_*.lock"):
        try:
            if now - p.stat().st_mtime < max_age_s:
                fresh.append(p)
        except OSError:
            continue
    return fresh


def quiesce(
    palace: Path, data_path: Path | None = None, *, dry_run: bool = False,
) -> None:
    """Request daemon stop and check mine locks; not an all-writer drain proof."""
    data = resolve_data_path(palace, data_path)
    if has_cmd("mempalace"):
        run(mempalace_cmd(data, "daemon", "stop"), dry_run=dry_run)
    else:
        print("  mempalace CLI not found; skipping daemon stop", file=sys.stderr)
    fresh = fresh_mine_locks(palace)
    if fresh:
        names = ", ".join(p.name for p in fresh)
        sys.exit(f"Active mine lock(s) detected ({names}). A mid-mine snapshot is "
                 f"inconsistent — wait for mining to finish, then retry.")


# --------------------------------------------------------------------------- #
# Subcommands.
# --------------------------------------------------------------------------- #
@contextmanager
def task_safety():
    try:
        from mempalace_tasks import restore, snapshot
    except ImportError as exc:
        raise BackupError("Task-safe backup/restore requires the preinstalled epoch-aware "
                          "mempalace_tasks package on this interpreter's PYTHONPATH.") from exc
    try:
        yield restore, snapshot
    except (restore.RestoreError, snapshot.SnapshotError) as exc:
        raise BackupError(f"{exc.code}: {exc}") from exc


def load_backup_manifest(home):
    path = _covered_path(home, home / BACKUP_MANIFEST)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"Invalid backup inventory manifest: {path}") from exc
    fields = {"schema_version", "data_relative", "logstream_required", "authorities",
              "snapshot_sha256"}
    if (type(value) is not dict or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["data_relative"]) is not str
            or type(value["logstream_required"]) is not bool
            or type(value["authorities"]) is not list
            or any(type(item) is not str for item in value["authorities"])
            or type(value["snapshot_sha256"]) is not str
            or re.fullmatch("[0-9a-f]{64}", value["snapshot_sha256"]) is None):
        raise BackupError(f"Unsupported backup inventory manifest: {path}")
    relative = Path(value["data_relative"])
    if relative.is_absolute() or ".." in relative.parts or not value["data_relative"]:
        raise BackupError("Backup manifest data path escapes the restored HOME")
    _covered_path(home, home / relative)
    return value


def task_snapshot_digest(summary):
    """Bind recovered domain content, not deliberately replaced epoch controls."""
    authorities = {}
    controls = {"epoch_id", "activation_id", "activation_at",
                "raw_record_count", "stale_record_count"}
    for key, value in summary["authorities"].items():
        authorities[key] = {field: item for field, item in value.items() if field not in controls}
    content = {"replica_id": summary["replica_id"], "authorities": authorities,
               "artifacts": summary["artifacts"], "artifact_link_count": summary["artifact_link_count"]}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _snapshot_commands(args, palace):
    rc = run(build_backup_cmd(palace, default_tags() + list(args.tag or [])), dry_run=args.dry_run)
    if rc:
        return rc
    rc = run(["restic", "snapshots", "--tag", "palace"], dry_run=args.dry_run)
    return rc if rc else run(build_check_cmd(), dry_run=args.dry_run)


def cmd_checkpoint(args: argparse.Namespace) -> int:
    palace = args.palace
    print(f"Checkpointing SQLite WALs under {palace}", file=sys.stderr)
    clean = checkpoint_all(palace, args.data_path, require_logstream=args.require_logstream)
    return 0 if clean else 1


def cmd_backup(args: argparse.Namespace) -> int:
    palace = _covered_path(args.palace, args.palace)
    if not palace.is_dir():
        sys.exit(f"Palace not found: {palace}")
    if (palace / RESTORE_MARKER).exists():
        raise BackupError("Interrupted publication marker present; resolve it before backup")
    data = resolve_data_path(palace, args.data_path)
    manifest = load_backup_manifest(palace)
    required = (args.require_logstream or bool(args.expected_authority)
                or bool(manifest and (manifest["logstream_required"] or manifest["authorities"])))
    backup_inventory(palace, data, require_logstream=required)
    task_storage = required or (data / "logstream.sqlite3").exists()
    if task_storage:
        if not args.offline:
            raise BackupError("Task backup requires --offline: stop task/hub/raw writers, peer sync "
                              "and autostarters. --force/--no-quiesce cannot bypass this.")
        if manifest and data.relative_to(palace).as_posix() != manifest["data_relative"]:
            raise BackupError("Configured data path differs from the declared backup inventory")
        require_restic_env()
        if args.dry_run:
            print("Dry-run only: no writer leases, manifest or snapshot created.", file=sys.stderr)
            return _snapshot_commands(args, palace)
        with task_safety() as (restore, snapshot):
            if (data / restore.PREPARE_MARKER).exists():
                raise BackupError("Incomplete task restore activation must be resolved before backup")
            restore.ensure_private_tree(palace)
            restore.refuse_known_writers(data)
            with restore.writer_lease(data):
                restore.refuse_known_writers(data)
                if not checkpoint_all(palace, data, require_logstream=required):
                    raise BackupError("Busy SQLite writer; task backup refuses even --force")
                databases = [path for path in sqlite_paths(palace, data) if path.exists()]
                with restore.sqlite_write_guard(databases):
                    expected = sorted(set(args.expected_authority or [])
                                      | set(manifest["authorities"] if manifest else []))
                    summary = snapshot.validate_task_snapshot(
                        data, require_logstream=True, expected_authorities=expected)
                    if any(not row["initialized"] for row in summary["authorities"].values()):
                        raise BackupError("Task backup contains an uninitialized authority")
                    restore.write_private_json(palace / BACKUP_MANIFEST, {
                        "schema_version": 1, "data_relative": data.relative_to(palace).as_posix(),
                        "logstream_required": True, "authorities": sorted(summary["authorities"]),
                        "snapshot_sha256": task_snapshot_digest(summary),
                    })
                    print("Offline capture: cooperative lease and SQLite writer exclusions held; "
                          "raw file replacement/new launches remain prohibited.", file=sys.stderr)
                    return _snapshot_commands(args, palace)
    require_restic_env()
    if not args.no_quiesce:
        quiesce(palace, data, dry_run=args.dry_run)
    print("Checkpointing before snapshot…", file=sys.stderr)
    if not checkpoint_all(palace, data, require_logstream=args.require_logstream) and not args.force:
        sys.exit("A SQLite WAL is held by a writer; refusing to snapshot an "
                 "inconsistent palace. Re-run once writers stop, or pass --force.")
    return _snapshot_commands(args, palace)


def staged_data_path(stage, palace, override=None):
    """Resolve relative layout from the staged snapshot, never old live config/env."""
    manifest = load_backup_manifest(stage)
    if override is not None:
        relative = _covered_path(palace, override).relative_to(palace)
    elif manifest:
        relative = Path(manifest["data_relative"])
    else:
        config = _covered_path(stage, stage / "config.json")
        value = {}
        if config.exists():
            try:
                value = json.loads(config.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as exc:
                raise BackupError("Invalid staged config.json") from exc
            if type(value) is not dict:
                raise BackupError("Invalid staged config.json object")
        configured = value.get("palace_path")
        if configured is None:
            relative = Path("palace")
        else:
            if type(configured) is not str or not Path(configured).expanduser().is_absolute():
                raise BackupError("Staged config palace_path must be absolute; use --data-path")
            relative = _covered_path(palace, Path(configured)).relative_to(palace)
    if manifest and relative.as_posix() != manifest["data_relative"]:
        raise BackupError("Data override conflicts with the snapshot's declared data layout")
    data = _covered_path(stage, stage / relative)
    if not data.is_dir():
        raise BackupError(f"Restored data directory not found: {data}")
    return data


def _validate_stage(stage, data, *, required=False, expected=(), allow_incomplete_preparation=False):
    if (stage / RESTORE_MARKER).exists():
        raise BackupError("Incomplete publication marker found in staged HOME; inspect before recovery")
    manifest = load_backup_manifest(stage)
    required = required or bool(manifest and manifest["logstream_required"])
    expected = sorted(set(expected) | set(manifest["authorities"] if manifest else []))
    backup_inventory(stage, data, require_logstream=required or bool(expected))
    origin = _covered_path(stage, data / ".mempalace/origin.json")
    if not origin.is_file():
        raise BackupError(f"Restored tree lacks embedder identity: {origin}")
    if not (data / "logstream.sqlite3").exists():
        return None
    with task_safety() as (restore, snapshot):
        if not allow_incomplete_preparation and (data / restore.PREPARE_MARKER).exists():
            raise BackupError("Incomplete task restore activation; inspect or retry offline publication")
        summary = snapshot.validate_task_snapshot(data, require_logstream=required,
                                                  expected_authorities=expected)
        if manifest and task_snapshot_digest(summary) != manifest["snapshot_sha256"]:
            raise BackupError("Staged task contents do not match the captured inventory digest")
        return summary


def _disjoint(palace, stage):
    if palace == stage or palace in stage.parents or stage in palace.parents:
        raise BackupError("Staging and target HOME must be disjoint trees")


def _move_entry(source, target):
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite unexpected publication entry: {target}")
    source.rename(target)


def publish_stage(palace, stage):
    """Reversible content publication; caller holds offline and cooperative exclusion.

    HOME itself and its control directories never move. The durable marker
    describes crash recovery; exceptions attempt rollback without deleting data.
    """
    palace, stage = _covered_path(palace, palace), _covered_path(stage, stage)
    _disjoint(palace, stage)
    with task_safety() as (restore, _):
        restore.ensure_private_tree(stage)
        if not palace.exists():
            palace.mkdir(mode=0o700)
        restore.checked_path(palace, directory=True)
        if palace.stat().st_dev != stage.stat().st_dev:
            raise BackupError("In-place publication requires same-filesystem staging")
        canonical_locks = _absolute(Path.home() / ".mempalace/locks")
        if canonical_locks.is_relative_to(palace) and canonical_locks.relative_to(palace) != Path("locks"):
            raise BackupError("Publication would relocate the canonical locks namespace")
        marker = palace / RESTORE_MARKER
        if marker.exists() or marker.is_symlink() or (stage / RESTORE_MARKER).exists():
            raise BackupError(f"Interrupted publication requires inspection: {marker}")
        backup = timestamped_backup_dir(palace)
        backup = backup.with_name(backup.name + "-" + uuid4().hex[:8])
        backup.mkdir(mode=0o700)
        restore.sync_directory(backup.parent)
        restore.sync_directory(stage.parent)
        old = sorted(p.name for p in palace.iterdir() if p.name not in CONTROL_DIRS)
        new = sorted(p.name for p in stage.iterdir() if p.name not in CONTROL_DIRS)
        moved_old, moved_new = [], []
        state = {"schema_version": 1, "phase": "publishing", "target": str(palace),
                 "stage": str(stage), "backup": str(backup), "old": old, "new": new,
                 "moved_old": moved_old, "moved_new": moved_new}
        restore.write_private_json(marker, state)
        try:
            for name in old:
                _move_entry(palace / name, backup / name)
                moved_old.append(name)
                restore.sync_directory(backup)
                restore.sync_directory(palace)
                restore.write_private_json(marker, state)
            for name in new:
                _move_entry(stage / name, palace / name)
                moved_new.append(name)
                restore.sync_directory(stage)
                restore.sync_directory(palace)
                restore.write_private_json(marker, state)
            marker.unlink()
            restore.sync_directory(palace)
            return backup
        except BaseException as exc:
            errors = []
            for name in reversed(moved_new):
                try:
                    _move_entry(palace / name, stage / name)
                except OSError as error:
                    errors.append(str(error))
            for name in reversed(moved_old):
                try:
                    _move_entry(backup / name, palace / name)
                except OSError as error:
                    errors.append(str(error))
            if errors:
                state["phase"] = "rollback_incomplete"
                state["errors"] = errors
                try:
                    restore.write_private_json(marker, state)
                except OSError as marker_error:
                    raise BackupError(
                        f"Publication failed; rollback incomplete and marker update failed: {marker_error}. "
                        f"Keep target {palace}, stage {stage}, backup {backup}; inspect {marker}.") from exc
                raise BackupError(f"Publication failed; rollback incomplete. Keep target {palace}, "
                                  f"stage {stage}, backup {backup}; inspect {marker}.") from exc
            try:
                if marker.exists():
                    marker.unlink()
                restore.sync_directory(palace)
                restore.sync_directory(stage)
            except OSError as cleanup_error:
                raise BackupError(
                    f"Content rolled back, but recovery-marker cleanup/durability failed: {cleanup_error}. "
                    f"Inspect target {palace}, stage {stage}, backup {backup}, marker {marker}.") from exc
            raise BackupError(f"Publication failed and rolled back; original HOME intact. "
                              f"Stage retained at {stage}, rollback directory {backup}: {exc}") from exc


def cmd_restore(args: argparse.Namespace) -> int:
    palace = _covered_path(args.palace, args.palace)
    if args.in_place and not args.offline:
        raise BackupError("In-place restore requires --offline and suppressed task/hub/raw writers/autostarters")
    if args.from_stage and not args.target:
        raise BackupError("--from-stage requires an explicit --target")
    if args.target:
        stage = _covered_path(Path(args.target), Path(args.target))
    elif args.dry_run:
        stage = palace.with_name(palace.name + ".restore-preview")
    else:
        stage = Path(tempfile.mkdtemp(prefix=palace.name + ".restore-", dir=palace.parent))
    _disjoint(palace, stage)
    if args.dry_run:
        print("Dry-run only: staging/publication/activation not performed.", file=sys.stderr)
        return 0 if args.from_stage else run(
            build_restore_cmd(args.snapshot, palace, stage), dry_run=True)
    if not args.from_stage:
        if stage.exists() and any(stage.iterdir()):
            raise BackupError("Restic staging target must be empty; use --from-stage for an existing stage")
        stage.mkdir(mode=0o700, exist_ok=True)
        require_restic_env()
        rc = run(build_restore_cmd(args.snapshot, palace, stage), dry_run=False)
        if rc:
            print(f"Materialization failed; target untouched, partial stage retained: {stage}", file=sys.stderr)
            return rc
    with task_safety() as (restore, snapshot):
        restore.ensure_private_tree(stage)
        data = staged_data_path(stage, palace, args.data_path)
        summary = _validate_stage(stage, data, required=args.require_logstream,
                                  expected=args.expected_authority or [],
                                  allow_incomplete_preparation=args.in_place)
        if not args.in_place:
            print(f"Validated private stage at {stage}; not published or activated. "
                  "Use --from-stage --in-place --offline only after stopping writers/new launches.",
                  file=sys.stderr)
            return 0
        if stage.stat().st_dev != palace.parent.stat().st_dev:
            raise BackupError("In-place publication requires same-filesystem staging")
        if (palace / RESTORE_MARKER).exists():
            raise BackupError("Interrupted publication marker present; inspect it before retrying")
        target_data = palace / data.relative_to(stage)
        old_data = resolve_data_path(palace, allow_missing=True)
        live_manifest = load_backup_manifest(palace)
        paths = {target_data, old_data}
        if live_manifest:
            paths.add(_covered_path(palace, palace / live_manifest["data_relative"]))
        paths = sorted(paths)
        with ExitStack() as stack:
            for path in paths:
                restore.refuse_known_writers(path)
                stack.enter_context(restore.writer_lease(path))
                restore.refuse_known_writers(path)
            target_databases = sorted({db for path in paths for db in sqlite_paths(palace, path)
                                       if db.exists()})
            with restore.sqlite_write_guard(target_databases):
                # Current membership can be newer than the selected snapshot.
                expected = set(args.expected_authority or [])
                known_tasks = bool(live_manifest and (
                    live_manifest["logstream_required"] or live_manifest["authorities"]))
                for path in paths:
                    if (path / "logstream.sqlite3").exists():
                        known_tasks = True
                        snapshot.validate_task_snapshot(path, require_logstream=True)
                summary = _validate_stage(stage, data, required=args.require_logstream or known_tasks,
                                          expected=expected, allow_incomplete_preparation=True)
            # SQLite handles close before native Windows renames. The permanent
            # writer leases remain held; raw writers/new launches must stay offline.
            preparation = (restore.task_restore_preparation(
                data, private_stage=True, expected_authorities=sorted(summary["authorities"]),
                live_data_path=target_data) if summary is not None else nullcontext(None))
            with preparation:
                stage_dbs = [db for db in sqlite_paths(stage, data) if db.exists()]
                with restore.sqlite_write_guard(stage_dbs):
                    _validate_stage(stage, data, required=summary is not None,
                                    expected=sorted(summary["authorities"]) if summary else [])
                for path in paths:
                    restore.refuse_known_writers(path)
                backup = publish_stage(palace, stage)
        print(f"Published offline; previous contents retained at {backup}. No service started. "
              "Keep admission stopped until the epoch-aware sidecar starts with a fresh successor "
              "and reconciles inherited attempts/external effects.", file=sys.stderr)
        return 0


def cmd_verify(args: argparse.Namespace) -> int:
    data = resolve_data_path(args.palace, args.data_path, allow_missing=True)
    require_restic_env()
    rc = run(build_check_cmd(args.read_data_subset), dry_run=args.dry_run)
    if has_cmd("mempalace"):
        run(mempalace_cmd(data, "repair-status"), dry_run=args.dry_run)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="palace_backup.py",
        description="Safe restic backup/restore for a local MemPalace palace.")
    p.add_argument("--palace", type=lambda s: Path(s).expanduser(),
                   default=DEFAULT_PALACE,
                   help="Physical palace HOME root, not data (default: ~/.mempalace).")
    p.add_argument("--data-path", type=lambda s: Path(s).expanduser(),
                   help="Data directory override; must be physically inside --palace. "
                        "Default: path environment/config, then HOME/palace.")
    p.add_argument("--dry-run", action="store_true",
                   help="Echo commands without executing side effects.")
    sub = p.add_subparsers(dest="command", required=True)

    sc = sub.add_parser("checkpoint", help="Inventory and WAL-checkpoint SQLite DBs.")
    sc.add_argument("--require-logstream", action="store_true",
                    help="Fail if task logstream/provenance is missing; not a "
                         "quiescence or task-history verification guarantee.")
    sc.set_defaults(func=cmd_checkpoint)

    sb = sub.add_parser("backup", help="Quiesce, checkpoint, snapshot, verify.")
    sb.add_argument("--tag", action="append", help="Extra restic tag (repeatable).")
    sb.add_argument("--no-quiesce", action="store_true",
                    help="Skip daemon stop / active-mine check.")
    sb.add_argument("--force", action="store_true",
                    help="Snapshot even if a WAL checkpoint reported busy.")
    sb.add_argument("--require-logstream", action="store_true",
                    help="Require task logstream/provenance; never bypassed by --force.")
    sb.add_argument("--offline", action="store_true",
                    help="Acknowledge all task/hub/raw writers, peer sync and autostarters are stopped.")
    sb.add_argument("--expected-authority", action="append",
                    help="Require an initialized authority UUID (repeatable).")
    sb.set_defaults(func=cmd_backup)

    sr = sub.add_parser("restore", help="Reversible staged restore (+ optional swap).")
    sr.add_argument("snapshot", help="restic snapshot id (or 'latest').")
    sr.add_argument("--target", help="Private staging directory (default: unique sibling of HOME).")
    sr.add_argument("--in-place", action="store_true",
                    help="Publish offline with reversible content moves, preserving control locks.")
    sr.add_argument("--from-stage", action="store_true",
                    help="Use existing --target staging without invoking restic.")
    sr.add_argument("--offline", action="store_true",
                    help="Acknowledge stopped task/hub/raw writers, peer sync and suppressed new launches.")
    sr.add_argument("--require-logstream", action="store_true",
                    help="Refuse missing task storage, even in a legacy snapshot without a manifest.")
    sr.add_argument("--expected-authority", action="append",
                    help="Require an initialized authority UUID (repeatable).")
    sr.set_defaults(func=cmd_restore)

    sv = sub.add_parser("verify", help="restic check (+ palace repair-status).")
    sv.add_argument("--read-data-subset", help="e.g. 5%% for a deeper sampled check.")
    sv.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
