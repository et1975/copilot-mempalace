#!/usr/bin/env python3
"""Tests for palace_backup.py.

Split into pure-function unit tests (no external tools) and SQLite integration
tests (stdlib sqlite3 module only — no CLI, no restic, no live palace).

Run: ``python3 -m pytest test_palace_backup.py`` or ``python3 test_palace_backup.py``.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_backup as pb  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure command builders.
# --------------------------------------------------------------------------- #
def test_backup_cmd_excludes_locks_and_tags():
    palace = Path("/home/u/.mempalace")
    cmd = pb.build_backup_cmd(palace, ["palace", "hostx"])
    assert cmd[:3] == ["restic", "backup", "/home/u/.mempalace"]
    # locks excluded via absolute path
    assert "--exclude" in cmd
    assert "/home/u/.mempalace/locks" in cmd
    # tags preserved in order
    assert cmd[-4:] == ["--tag", "palace", "--tag", "hostx"]


def test_backup_cmd_never_excludes_origin_json():
    cmd = pb.build_backup_cmd(Path("/p/.mempalace"), [])
    joined = " ".join(cmd)
    assert "origin.json" not in joined
    assert ".mempalace/locks" in joined  # only locks are excluded


def test_restore_cmd_uses_subpath_to_strip_absolute_prefix():
    # This is the key correctness property: restic stores absolute paths, so the
    # <snapshot>:<abs> form is required for files to land directly under target.
    cmd = pb.build_restore_cmd("abc123", Path("/home/u/.mempalace"), Path("/tmp/out"))
    assert cmd == ["restic", "restore", "abc123:/home/u/.mempalace",
                   "--target", "/tmp/out"]


def test_restore_cmd_latest():
    cmd = pb.build_restore_cmd("latest", Path("/p/.mempalace"), Path("/p/.mempalace"))
    assert cmd[2] == "latest:/p/.mempalace"


def test_check_cmd_optional_subset():
    assert pb.build_check_cmd() == ["restic", "check"]
    assert pb.build_check_cmd("5%") == ["restic", "check", "--read-data-subset=5%"]


def test_mempalace_cmd_forwards_palace_as_global_flag():
    # --palace MUST precede the subcommand, else mempalace targets the default
    # palace (~/.mempalace) instead of the one we operate on.
    cmd = pb.mempalace_cmd(Path("/tmp/p"), "repair")
    assert cmd == ["mempalace", "--palace", "/tmp/p", "repair"]
    cmd2 = pb.mempalace_cmd(Path("/tmp/p"), "daemon", "stop")
    assert cmd2 == ["mempalace", "--palace", "/tmp/p", "daemon", "stop"]


def test_default_tags_include_palace_and_host():
    tags = pb.default_tags()
    assert tags[0] == "palace"
    assert tags[1]  # hostname non-empty


def test_timestamped_backup_dir_is_sibling():
    now = datetime(2026, 7, 7, 20, 23, 46)
    bak = pb.timestamped_backup_dir(Path("/home/u/.mempalace"), now)
    assert bak == Path("/home/u/.mempalace.bak-20260707-202346")


# --------------------------------------------------------------------------- #
# SQLite integration (stdlib only).
# --------------------------------------------------------------------------- #
def _make_wal_db(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(id INTEGER)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def test_checkpoint_db_truncates_clean(tmp_path):
    db = tmp_path / "kg.sqlite3"
    _make_wal_db(db, 5)
    busy, log, ckpt = pb.checkpoint_db(db)
    assert busy == 0  # no blocking writer


def test_integrity_check_ok(tmp_path):
    db = tmp_path / "kg.sqlite3"
    _make_wal_db(db, 3)
    assert pb.integrity_check(db) == "ok"


def test_checkpoint_all_skips_absent(tmp_path):
    palace = tmp_path / ".mempalace"
    (palace / "palace").mkdir(parents=True)
    _make_wal_db(palace / "knowledge_graph.sqlite3", 2)
    # palace/chroma.sqlite3 intentionally absent
    assert pb.checkpoint_all(palace) is True  # present DB clean, absent skipped


# --------------------------------------------------------------------------- #
# Lock freshness heuristic.
# --------------------------------------------------------------------------- #
def test_fresh_mine_locks_detects_recent(tmp_path):
    palace = tmp_path / ".mempalace"
    locks = palace / "locks"
    locks.mkdir(parents=True)
    recent = locks / "mine_palace_deadbeef.lock"
    recent.write_text("")
    assert pb.fresh_mine_locks(palace, max_age_s=900) == [recent]


def test_fresh_mine_locks_ignores_old(tmp_path):
    palace = tmp_path / ".mempalace"
    locks = palace / "locks"
    locks.mkdir(parents=True)
    old = locks / "mine_palace_old.lock"
    old.write_text("")
    import os
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    assert pb.fresh_mine_locks(palace, max_age_s=900) == []


def test_fresh_mine_locks_no_locks_dir(tmp_path):
    assert pb.fresh_mine_locks(tmp_path / "nope") == []


# --------------------------------------------------------------------------- #
# Argument parsing.
# --------------------------------------------------------------------------- #
def test_parser_backup_defaults():
    args = pb.build_parser().parse_args(["backup"])
    assert args.command == "backup"
    assert args.palace == pb.DEFAULT_PALACE
    assert args.func is pb.cmd_backup


def test_parser_restore_requires_snapshot():
    args = pb.build_parser().parse_args(["restore", "latest", "--in-place"])
    assert args.snapshot == "latest"
    assert args.in_place is True


def test_parser_dry_run_global():
    args = pb.build_parser().parse_args(["--dry-run", "checkpoint"])
    assert args.dry_run is True


# --------------------------------------------------------------------------- #
# Minimal runner when pytest is unavailable.
# --------------------------------------------------------------------------- #
def _run_without_pytest() -> int:
    import inspect
    import tempfile

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        try:
            if "tmp_path" in params:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_without_pytest())
