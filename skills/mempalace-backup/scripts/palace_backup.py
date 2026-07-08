#!/usr/bin/env python3
"""Safe, idempotent backup / restore helper for a local MemPalace palace.

This is the deterministic ("mechanical") companion to the ``mempalace-backup``
and ``mempalace-restore`` skills. The skills document *why* and *when*; this
script makes the *how* reproducible: quiesce writers, checkpoint both SQLite
WALs, snapshot with ``restic`` (excluding ephemeral locks), verify, and restore
via restic's absolute-path-stripping subpath syntax.

Design notes
------------
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

    ./palace_backup.py checkpoint            # WAL-checkpoint both SQLite DBs
    ./palace_backup.py backup                # quiesce + checkpoint + snapshot + verify
    ./palace_backup.py restore <snapshot>    # reversible staged restore + swap
    ./palace_backup.py verify                # restic check (+ palace repair-status)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_PALACE = Path("~/.mempalace").expanduser()

# SQLite databases inside the palace, relative to the palace root. Both are
# checkpointed before a snapshot so restic captures a single consistent file.
SQLITE_DBS = (
    "knowledge_graph.sqlite3",
    "palace/chroma.sqlite3",
)

# Never restore stale locks; they are process-local and harmful if resurrected.
EXCLUDE_DIRS = ("locks",)

# Critical file: the embedder identity. A restore missing this breaks search.
ORIGIN_JSON = "palace/.mempalace/origin.json"


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


def mempalace_cmd(palace: Path, *args: str) -> list[str]:
    """Build a ``mempalace`` argv explicitly bound to ``palace``.

    ``--palace`` is a *global* flag that must precede the subcommand. Forwarding
    it is critical: without it, ``mempalace repair`` / ``daemon stop`` silently
    target the default palace (``~/.mempalace``) instead of the one this script
    is operating on.
    """
    return ["mempalace", "--palace", str(palace), *args]


def default_tags() -> list[str]:
    """Standard tags applied to palace snapshots: ``palace`` + the hostname."""
    return ["palace", os.uname().nodename]


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
    checkpoint completed without a blocking writer.
    """
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    return tuple(row)  # type: ignore[return-value]


def integrity_check(db_path: Path) -> str:
    """Return the SQLite ``integrity_check`` result (``"ok"`` when healthy)."""
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def checkpoint_all(palace: Path) -> bool:
    """Checkpoint every palace SQLite DB. Return True if all checkpointed clean."""
    all_clean = True
    for rel in SQLITE_DBS:
        db = palace / rel
        if not db.exists():
            print(f"  skip (absent): {rel}", file=sys.stderr)
            continue
        busy, log, ckpt = checkpoint_db(db)
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


def quiesce(palace: Path, *, dry_run: bool = False) -> None:
    """Stop the opt-in daemon and warn on active mines before a snapshot."""
    if has_cmd("mempalace"):
        run(mempalace_cmd(palace, "daemon", "stop"), dry_run=dry_run)
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
def cmd_checkpoint(args: argparse.Namespace) -> int:
    palace = args.palace
    print(f"Checkpointing SQLite WALs under {palace}", file=sys.stderr)
    clean = checkpoint_all(palace)
    return 0 if clean else 1


def cmd_backup(args: argparse.Namespace) -> int:
    palace: Path = args.palace
    if not palace.is_dir():
        sys.exit(f"Palace not found: {palace}")
    require_restic_env()
    if not args.no_quiesce:
        quiesce(palace, dry_run=args.dry_run)
    print("Checkpointing before snapshot…", file=sys.stderr)
    if not checkpoint_all(palace) and not args.force:
        sys.exit("A SQLite WAL is held by a writer; refusing to snapshot an "
                 "inconsistent palace. Re-run once writers stop, or pass --force.")
    tags = default_tags() + list(args.tag or [])
    rc = run(build_backup_cmd(palace, tags), dry_run=args.dry_run)
    if rc != 0:
        return rc
    run(["restic", "snapshots", "--tag", "palace"], dry_run=args.dry_run)
    return run(build_check_cmd(), dry_run=args.dry_run)


def cmd_restore(args: argparse.Namespace) -> int:
    palace: Path = args.palace
    require_restic_env()
    if has_cmd("mempalace"):
        run(mempalace_cmd(palace, "daemon", "stop"), dry_run=args.dry_run)
    stage = Path(args.target).expanduser() if args.target else Path(
        f"/tmp/palace-restore-{datetime.now():%Y%m%d-%H%M%S}")
    rc = run(build_restore_cmd(args.snapshot, palace, stage), dry_run=args.dry_run)
    if rc != 0:
        return rc
    origin = stage / ORIGIN_JSON
    if not args.dry_run and not origin.exists():
        sys.exit(f"Restored tree is missing {ORIGIN_JSON} (embedder identity). "
                 f"Refusing to swap — inspect {stage} manually.")
    print(f"Restored into staging: {stage}", file=sys.stderr)
    if args.in_place:
        bak = timestamped_backup_dir(palace)
        print(f"Moving current palace aside -> {bak}", file=sys.stderr)
        if not args.dry_run:
            if palace.exists():
                palace.rename(bak)
            Path(stage).rename(palace)
        if has_cmd("mempalace"):
            run(mempalace_cmd(palace, "repair"), dry_run=args.dry_run)
            run(mempalace_cmd(palace, "repair-status"), dry_run=args.dry_run)
        print("Swap complete. Reopen with MCP mempalace_reconnect (or restart the "
              "MCP server), then smoke test: mempalace status && mempalace search "
              "\"<known term>\".", file=sys.stderr)
    else:
        print("Staging only (no swap). Inspect it, then re-run with --in-place, or "
              "move it into ~/.mempalace yourself after moving the current palace "
              "aside.", file=sys.stderr)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    require_restic_env()
    rc = run(build_check_cmd(args.read_data_subset), dry_run=args.dry_run)
    if has_cmd("mempalace"):
        run(mempalace_cmd(args.palace, "repair-status"), dry_run=args.dry_run)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="palace_backup.py",
        description="Safe restic backup/restore for a local MemPalace palace.")
    p.add_argument("--palace", type=lambda s: Path(s).expanduser(),
                   default=DEFAULT_PALACE,
                   help="Palace root (default: ~/.mempalace).")
    p.add_argument("--dry-run", action="store_true",
                   help="Echo commands without executing side effects.")
    sub = p.add_subparsers(dest="command", required=True)

    sc = sub.add_parser("checkpoint", help="WAL-checkpoint both SQLite DBs.")
    sc.set_defaults(func=cmd_checkpoint)

    sb = sub.add_parser("backup", help="Quiesce, checkpoint, snapshot, verify.")
    sb.add_argument("--tag", action="append", help="Extra restic tag (repeatable).")
    sb.add_argument("--no-quiesce", action="store_true",
                    help="Skip daemon stop / active-mine check.")
    sb.add_argument("--force", action="store_true",
                    help="Snapshot even if a WAL checkpoint reported busy.")
    sb.set_defaults(func=cmd_backup)

    sr = sub.add_parser("restore", help="Reversible staged restore (+ optional swap).")
    sr.add_argument("snapshot", help="restic snapshot id (or 'latest').")
    sr.add_argument("--target", help="Staging dir (default: /tmp/palace-restore-<ts>).")
    sr.add_argument("--in-place", action="store_true",
                    help="After staging, move current palace aside and swap in the "
                         "restore, then run mempalace repair.")
    sr.set_defaults(func=cmd_restore)

    sv = sub.add_parser("verify", help="restic check (+ palace repair-status).")
    sv.add_argument("--read-data-subset", help="e.g. 5%% for a deeper sampled check.")
    sv.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
