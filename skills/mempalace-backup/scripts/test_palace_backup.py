#!/usr/bin/env python3
"""Tests for palace_backup.py.

Split into pure-function unit tests (no external tools) and SQLite integration
tests (stdlib sqlite3 module only — no CLI, no restic, no live palace).

Run: ``python3 -m pytest test_palace_backup.py`` or ``python3 test_palace_backup.py``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_backup as pb  # noqa: E402


def _assert_raises(error, action, text):
    try:
        action()
    except error as exc:
        assert text.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"Expected {error.__name__}: {text}")


def _make_logstream(data: Path) -> sqlite3.Connection:
    """Keep a real committed WAL open, without importing MemPalace."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "replica.json").write_text(
        json.dumps({"replica_id": "rep_0123456789ab"}), encoding="utf-8")
    con = sqlite3.connect(data / "logstream.sqlite3")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.executescript("""
        CREATE TABLE events (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, stream TEXT NOT NULL,
            room TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT,
            correlation_id TEXT, branch TEXT, base_commit TEXT, status TEXT,
            body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            origin_replica TEXT, origin_seq INTEGER, hlc TEXT
        );
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, content TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}', origin_replica TEXT
        );
        CREATE TABLE event_artifacts (
            event_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
            PRIMARY KEY (event_id, artifact_id)
        );
        INSERT INTO events
            (id, type, stream, room, from_agent, body, created_at)
            VALUES ('event-1', 'mptask.command', 'mptask/test', 'tasks',
                    'mempalace-tasks', '{"task_id":"task-1"}', '2026-09-25');
        INSERT INTO artifacts
            (id, kind, sha256, size_bytes, content, created_by, created_at)
            VALUES ('artifact-1', 'note',
                    '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
                    5, 'hello', 'mempalace-tasks', '2026-09-25');
        INSERT INTO event_artifacts VALUES ('event-1', 'artifact-1');
    """)
    con.commit()
    return con


def _make_task_palace(home, data_name="palace", *, configured_home=None):
    import test_snapshot as fixtures
    from mempalace_tasks.protocol import LogState, fold_record, make_proposal
    from mempalace_tasks.codec import canonical_json

    data = home / data_name
    (data / ".mempalace").mkdir(parents=True)
    (data / ".mempalace/origin.json").write_text("{}", encoding="utf-8")
    (home / "config.json").write_text(json.dumps({
        "palace_path": str((configured_home or home) / data_name)}), encoding="utf-8")
    (data / "replica.json").write_text(
        json.dumps({"replica_id": fixtures.REPLICA}), encoding="utf-8")
    log = LogState(fixtures.AUTHORITY)
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
        con.executescript(fixtures.SCHEMA)
        for index, command in enumerate((fixtures.genesis(), fixtures.create(2)), start=1):
            payload = make_proposal(log, command, fixtures.NOW)
            raw = fixtures.raw_record(payload, index)
            fold_record(log, raw)
            con.execute("""INSERT INTO events VALUES (
                :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
                :branch, :base_commit, :status, :body, :created_at, :metadata_json,
                :origin_replica, :origin_seq, :hlc)""",
                        {**raw, "metadata_json": canonical_json(raw["metadata"])})
        con.commit()
    return data


def _add_task_authority(data, authority):
    import test_snapshot as fixtures
    from mempalace_tasks.protocol import LogState, fold_record, make_proposal
    from mempalace_tasks.codec import canonical_json

    log = LogState(authority)
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
        start = con.execute("SELECT coalesce(max(rowid), 0) + 1 FROM events").fetchone()[0]
        for index, command in enumerate((fixtures.genesis(), fixtures.create(2)), start=start):
            payload = make_proposal(log, command, fixtures.NOW)
            raw = fixtures.raw_record(payload, index)
            fold_record(log, raw)
            con.execute("""INSERT INTO events VALUES (
                :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
                :branch, :base_commit, :status, :body, :created_at, :metadata_json,
                :origin_replica, :origin_seq, :hlc)""",
                        {**raw, "metadata_json": canonical_json(raw["metadata"])})
        con.commit()


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

def test_default_tags_do_not_require_posix_uname():
    with patch("socket.gethostname", return_value="portable-host"):
        assert pb.default_tags() == ["palace", "portable-host"]


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


def test_checkpoint_all_flushes_nested_logstream_and_preserves_permissions(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    with contextlib.closing(_make_logstream(data)):
        home.chmod(0o755)
        data.chmod(0o750)
        wal = data / "logstream.sqlite3-wal"
        assert wal.stat().st_size > 0
        assert pb.checkpoint_all(home)
        assert wal.stat().st_size == 0
        assert home.stat().st_mode & 0o777 == 0o755
        assert data.stat().st_mode & 0o777 == 0o750
        with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
            assert con.execute("SELECT body FROM events").fetchall() == [
                ('{"task_id":"task-1"}',)]
            assert con.execute("SELECT content FROM artifacts").fetchall() == [("hello",)]
            assert con.execute("SELECT * FROM event_artifacts").fetchall() == [
                ("event-1", "artifact-1")]


def test_busy_logstream_reader_prevents_clean_checkpoint_until_released(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    with contextlib.closing(_make_logstream(data)) as writer:
        with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT count(*) FROM events").fetchone() == (1,)
            writer.execute("UPDATE events SET status = 'after-reader'")
            writer.commit()
            assert pb.checkpoint_all(home) is False
            assert (data / "logstream.sqlite3-wal").stat().st_size > 0
            reader.rollback()
        assert pb.checkpoint_all(home) is True
        assert (data / "logstream.sqlite3-wal").stat().st_size == 0


def test_data_resolution_default_and_configured_paths(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    (home / "custom").mkdir()
    assert pb.resolve_data_path(home) == home / "palace"
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(home / "custom")}), encoding="utf-8")
    assert pb.resolve_data_path(home) == home / "custom"


def test_explicit_data_path_wins_environment_and_file_config(tmp_path):
    home = tmp_path / "home"
    for name in ("chosen", "from-env", "from-file"):
        (home / name).mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(home / "from-file")}), encoding="utf-8")
    with patch.dict(os.environ, {"MEMPALACE_PALACE_PATH": str(home / "from-env")}):
        assert pb.resolve_data_path(home) == home / "from-env"
        assert pb.resolve_data_path(home, home / "chosen") == home / "chosen"


def test_path_configuration_accepts_bom_without_rewriting_it(tmp_path):
    home = tmp_path / "home"
    (home / "custom").mkdir(parents=True)
    config = home / "config.json"
    original = ("\ufeff" + json.dumps({"palace_path": str(home / "custom")})
                ).encode("utf-8")
    config.write_bytes(original)
    assert pb.resolve_data_path(home) == home / "custom"
    assert config.read_bytes() == original


def test_legacy_environment_data_path_alias(tmp_path):
    home = tmp_path / "home"
    (home / "alias").mkdir(parents=True)
    with patch.dict(os.environ, {"MEMPAL_PALACE_PATH": str(home / "alias")}):
        assert pb.resolve_data_path(home) == home / "alias"


def test_custom_data_inventory_keeps_kg_home_relative(tmp_path):
    home = tmp_path / "home"
    data = home / "custom"
    with contextlib.closing(_make_logstream(data)):
        _make_wal_db(home / "knowledge_graph.sqlite3", 1)
        _make_wal_db(data / "chroma.sqlite3", 1)
        (home / "config.json").write_text(
            json.dumps({"palace_path": str(data)}), encoding="utf-8")
        files = pb.backup_inventory(home, require_logstream=True)
        assert set(files) == {
            home / "knowledge_graph.sqlite3", data / "chroma.sqlite3",
            data / "logstream.sqlite3", data / "logstream.sqlite3-wal",
            data / "logstream.sqlite3-shm", data / "replica.json",
        }
        assert pb.checkpoint_all(home, require_logstream=True)
        assert (data / "logstream.sqlite3-wal").stat().st_size == 0
        assert not (home / "palace").exists()
        assert not (data / "knowledge_graph.sqlite3").exists()


def test_explicit_data_path_checkpoint_uses_selected_database(tmp_path):
    home = tmp_path / "home"
    with contextlib.closing(_make_logstream(home / "selected")):
        assert pb.checkpoint_all(home, home / "selected", require_logstream=True)
        assert (home / "selected/logstream.sqlite3-wal").stat().st_size == 0
        assert not (home / "palace").exists()


def test_out_of_root_config_and_override_are_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(outside)}), encoding="utf-8")
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "outside")
    _assert_raises(pb.BackupError,
                   lambda: pb.resolve_data_path(home, outside), "outside")
    assert list(outside.iterdir()) == []


def test_data_under_excluded_locks_is_refused(tmp_path):
    home = tmp_path / "home"
    data = home / "locks/data"
    data.mkdir(parents=True)
    _assert_raises(pb.BackupError,
                   lambda: pb.resolve_data_path(home, data), "excluded")


def test_symlink_data_and_home_roots_are_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "palace").symlink_to(outside, target_is_directory=True)
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "symlink")
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(alias), "symlink")


def test_configured_symlink_cannot_be_hidden_by_parent_path_normalization(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "palace").mkdir()
    (home / "link").symlink_to(outside / "nested", target_is_directory=True)
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(home / "link/../palace")}), encoding="utf-8")
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "symlink")


def test_inventory_does_not_normalize_away_a_symlink_in_home(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    (home / "link").symlink_to(home, target_is_directory=True)
    unsafe_home = home / "link/.."
    _assert_raises(pb.BackupError, lambda: pb.backup_inventory(unsafe_home), "symlink")
    _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(unsafe_home), "symlink")


def test_symlink_database_sidecars_and_replica_are_refused(tmp_path):
    for name in ("logstream.sqlite3", "logstream.sqlite3-wal",
                 "logstream.sqlite3-shm", "replica.json", "chroma.sqlite3",
                 "knowledge_graph.sqlite3"):
        home = tmp_path / name
        data = home / "palace"
        data.mkdir(parents=True)
        outside = tmp_path / f"external-{name}"
        outside.write_text("do not follow", encoding="utf-8")
        target = home / name if name == "knowledge_graph.sqlite3" else data / name
        target.symlink_to(outside)
        _assert_raises(pb.BackupError, lambda: pb.backup_inventory(home), "symlink")
        assert outside.read_text(encoding="utf-8") == "do not follow"


def test_dangling_symlink_and_symlinked_config_are_not_absent(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    (home / "config.json").symlink_to(tmp_path / "missing-config")
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "symlink")
    (home / "config.json").unlink()
    (home / "palace/logstream.sqlite3").symlink_to(tmp_path / "missing-db")
    _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "symlink")


def test_storage_directory_cannot_masquerade_as_database(tmp_path):
    home = tmp_path / "home"
    (home / "palace/logstream.sqlite3").mkdir(parents=True)
    _assert_raises(pb.BackupError, lambda: pb.backup_inventory(home), "storage file")


def test_invalid_and_missing_data_configuration_are_explicit_errors(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    config = home / "config.json"
    for invalid in ("{", "[]", '{"palace_path": null}', '{"palace_path": ""}',
                    '{"palace_path": 17}', '{"palace_path": "relative"}'):
        config.write_text(invalid, encoding="utf-8")
        _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "config")
    config.write_text(json.dumps({"palace_path": str(home / "missing")}),
                      encoding="utf-8")
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home), "not found")
    assert not (home / "missing").exists()


def test_absent_optional_logstream_never_creates_database_or_replica(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    data.mkdir(parents=True)
    assert pb.checkpoint_all(home)
    assert pb.backup_inventory(home) == ()
    assert list(data.iterdir()) == []


def test_declared_logstream_missing_is_not_optional(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    _assert_raises(pb.BackupError,
                   lambda: pb.checkpoint_all(home, require_logstream=True),
                   "required")
    assert not (home / "palace/logstream.sqlite3").exists()


def test_orphan_logstream_wal_is_not_optional_legacy_storage(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    data.mkdir(parents=True)
    (data / "logstream.sqlite3-wal").write_bytes(b"orphan")
    _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "missing")
    assert not (data / "logstream.sqlite3").exists()


def test_corrupt_existing_logstream_fails_before_any_checkpoint(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    data.mkdir(parents=True)
    db = data / "logstream.sqlite3"
    db.write_bytes(b"not a SQLite database")
    with patch.object(pb, "checkpoint_db", side_effect=AssertionError("too late")):
        _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "logstream")
    assert db.read_bytes() == b"not a SQLite database"


def test_wrong_sqlite_database_cannot_masquerade_as_logstream(tmp_path):
    home = tmp_path / "home"
    _make_wal_db(home / "palace/logstream.sqlite3", 1)
    _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "logstream")


def test_logstream_rejects_missing_tables_and_columns(tmp_path):
    for statement in ("DROP TABLE artifacts", "DROP TABLE event_artifacts",
                      "ALTER TABLE events DROP COLUMN body"):
        home = tmp_path / str(len(list(tmp_path.iterdir())))
        with contextlib.closing(_make_logstream(home / "palace")) as con:
            con.execute(statement)
            con.commit()
            _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "logstream")


def test_existing_logstream_requires_valid_provenance(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    with contextlib.closing(_make_logstream(data)):
        replica = data / "replica.json"
        replica.unlink()
        _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "replica")
        for invalid in ("{", "[]", "{}", '{"replica_id": "invalid"}'):
            replica.write_text(invalid, encoding="utf-8")
            _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "replica")
        assert not replica.read_text(encoding="utf-8").startswith('{"replica_id": "rep_')


def test_low_level_sqlite_helpers_do_not_create_absent_databases(tmp_path):
    for operation in (pb.checkpoint_db, pb.integrity_check):
        missing = tmp_path / f"{operation.__name__}.sqlite3"
        _assert_raises(sqlite3.OperationalError, lambda: operation(missing), "open")
        assert not missing.exists()


def test_force_never_masks_missing_required_or_corrupt_task_storage(tmp_path):
    home = tmp_path / "home"
    data = home / "palace"
    data.mkdir(parents=True)
    for corrupt in (False, True):
        if corrupt:
            (data / "logstream.sqlite3").write_bytes(b"corrupt")
        args = pb.build_parser().parse_args(
            ["--palace", str(home), "backup", "--force", "--no-quiesce",
             "--require-logstream"])
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "run", side_effect=AssertionError("must not snapshot")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "logstream")


def test_task_backup_requires_offline_even_with_old_bypass_flags(tmp_path):
    home = tmp_path / "home"
    data = _make_task_palace(home, "custom")
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "--data-path", str(data),
         "backup", "--require-logstream", "--no-quiesce", "--force"])
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "run", side_effect=AssertionError("must not snapshot")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "offline")

def test_expected_authority_declares_required_task_storage_even_without_manifest(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    args = pb.build_parser().parse_args([
        "--palace", str(home), "backup", "--force", "--no-quiesce",
        "--expected-authority", "11111111-1111-1111-1111-111111111111"])
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "run", side_effect=AssertionError("declared task storage must not be omitted")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "logstream")


def test_force_remains_a_legacy_busy_checkpoint_override_only(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    _make_wal_db(home / "palace/chroma.sqlite3", 1)
    argv = ["--palace", str(home), "backup", "--no-quiesce"]
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "checkpoint_db", return_value=(1, 4, 3)), \
            patch.object(pb, "run", return_value=0) as run:
        _assert_raises(SystemExit, lambda: pb.cmd_backup(
            pb.build_parser().parse_args(argv)), "writer")
        assert run.call_count == 0
        assert pb.cmd_backup(pb.build_parser().parse_args(argv + ["--force"])) == 0
        assert run.call_args_list[0].args[0][:3] == ["restic", "backup", str(home)]


def test_daemon_stop_uses_data_path_but_mine_locks_stay_home_relative(tmp_path):
    home = tmp_path / "home"
    data = home / "custom"
    data.mkdir(parents=True)
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(data)}), encoding="utf-8")
    with patch.object(pb, "has_cmd", return_value=True), \
            patch.object(pb, "run", return_value=0) as run:
        pb.quiesce(home)
        assert run.call_args.args[0] == [
            "mempalace", "--palace", str(data), "daemon", "stop"]
        (home / "locks").mkdir()
        (home / "locks/mine_palace_active.lock").write_text("", encoding="utf-8")
        _assert_raises(SystemExit, lambda: pb.quiesce(home), "active mine")


def test_verify_and_restore_forward_data_path_not_home(tmp_path):
    home = tmp_path / "home"
    data = home / "custom"
    data.mkdir(parents=True)
    prefix = ["--palace", str(home), "--data-path", str(data), "--dry-run"]
    parser = pb.build_parser()
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "has_cmd", return_value=True), \
            patch.object(pb, "run", return_value=0) as run:
        assert pb.cmd_verify(parser.parse_args(prefix + ["verify"])) == 0
        assert ["mempalace", "--palace", str(data), "repair-status"] in [
            call.args[0] for call in run.call_args_list]
        run.reset_mock()
        args = parser.parse_args(prefix + [
            "restore", "latest", "--in-place", "--offline", "--target", str(tmp_path / "stage")])
        assert pb.cmd_restore(args) == 0
        commands = [call.args[0] for call in run.call_args_list]
        assert all(command[0] != "mempalace" for command in commands)


def test_restore_staging_checks_origin_under_custom_data_path(tmp_path):
    home = tmp_path / "home"
    data = home / "custom"
    data.mkdir(parents=True)
    stage = tmp_path / "stage"
    (stage / "custom/.mempalace").mkdir(parents=True)
    (stage / "custom/.mempalace/origin.json").write_text("{}", encoding="utf-8")
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "--data-path", str(data),
         "restore", "latest", "--target", str(stage), "--from-stage"])
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "has_cmd", return_value=False), \
            patch.object(pb, "run", return_value=0):
        assert pb.cmd_restore(args) == 0
    assert not (stage / "palace").exists()
    assert home.is_dir()


def test_restore_can_resolve_a_lost_home_without_creating_it(tmp_path):
    home = tmp_path / "lost-home"
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "--dry-run", "restore", "latest",
         "--target", str(tmp_path / "stage")])
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "has_cmd", return_value=True), \
            patch.object(pb, "run", return_value=0) as run:
        assert pb.cmd_restore(args) == 0
        assert run.call_args_list[0].args[0][:2] == ["restic", "restore"]
    assert not home.exists()


def test_main_reports_data_errors_without_traceback_or_new_files(tmp_path):
    home = tmp_path / "home"
    (home / "palace").mkdir(parents=True)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert pb.main(["--palace", str(home), "checkpoint", "--require-logstream"]) == 1
    assert "logstream" in stderr.getvalue().lower()
    assert "traceback" not in stderr.getvalue().lower()
    assert list((home / "palace").iterdir()) == []


def test_task_backup_sqlite_writers_are_excluded_through_restic_snapshot(tmp_path):
    from mempalace_tasks.snapshot import validate_task_snapshot
    home = tmp_path / "home"
    data = _make_task_palace(home, "custom")
    commands = []

    def restic(argv, *, dry_run=False):
        commands.append(argv)
        if argv[:2] == ["restic", "backup"]:
            assert argv[2] == str(home)
            with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3", timeout=0)) as writer:
                _assert_raises(sqlite3.OperationalError,
                               lambda: writer.execute("UPDATE events SET status='blocked'"), "locked")
            manifest = json.loads((home / pb.BACKUP_MANIFEST).read_text(encoding="utf-8"))
            assert manifest["data_relative"] == "custom"
            assert manifest["logstream_required"] is True
            assert manifest["authorities"] == ["11111111-1111-1111-1111-111111111111"]
            assert len(manifest["snapshot_sha256"]) == 64
            assert validate_task_snapshot(data)["authorities"][manifest["authorities"][0]]["task_count"] == 1
        return 0

    args = pb.build_parser().parse_args(["--palace", str(home), "backup", "--offline"])
    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", side_effect=restic):
        assert pb.cmd_backup(args) == 0
    assert commands[0][:2] == ["restic", "backup"]
    assert all(command[0] == "restic" for command in commands)

def test_wal_commit_between_checkpoint_and_guard_survives_one_root_restore(tmp_path):
    import test_snapshot as fixtures
    from mempalace_tasks import restore, snapshot
    from mempalace_tasks.codec import canonical_json
    from mempalace_tasks.protocol import make_proposal

    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    original_checkpoint = pb.checkpoint_all
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")

        def checkpoint_then_commit(*args, **kwargs):
            clean = original_checkpoint(*args, **kwargs)
            log = restore.read_task_logs(data)[fixtures.AUTHORITY]
            payload = make_proposal(log, fixtures.create(3), fixtures.NOW)
            raw = fixtures.raw_record(payload, 3)
            writer.execute("""INSERT INTO events VALUES (
                :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
                :branch, :base_commit, :status, :body, :created_at, :metadata_json,
                :origin_replica, :origin_seq, :hlc)""",
                           {**raw, "metadata_json": canonical_json(raw["metadata"])})
            writer.execute("INSERT INTO artifacts VALUES "
                           "('artifact-in-cut', 'note', ?, 5, 'hello', 'operator', ?, '{}', ?)",
                           ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                            fixtures.NOW, fixtures.REPLICA))
            writer.execute("INSERT INTO event_artifacts VALUES ('evt-000003', 'artifact-in-cut')")
            writer.commit()
            return clean

        def capture(argv, *, dry_run=False):
            if argv[:2] == ["restic", "backup"]:
                assert (data / "logstream.sqlite3-wal").stat().st_size > 0
                shutil.copytree(home, stage)
            return 0

        args = pb.build_parser().parse_args(["--palace", str(home), "backup", "--offline"])
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "checkpoint_all", side_effect=checkpoint_then_commit), \
                patch.object(pb, "run", side_effect=capture):
            assert pb.cmd_backup(args) == 0
    captured = snapshot.validate_task_snapshot(stage / "palace")
    assert captured["authorities"][fixtures.AUTHORITY]["task_count"] == 2
    assert captured["artifacts"]["artifact-in-cut"]["size_bytes"] == 5
    future = tmp_path / "outside-recovery"
    future.mkdir()
    (future / "pending.json").write_text("discarded future", encoding="utf-8")
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--from-stage", "--target", str(stage),
         "--in-place", "--offline"])
    with patch.object(pb, "run", side_effect=AssertionError("from-stage must not invoke restic/startup")):
        assert pb.cmd_restore(args) == 0
    restored = snapshot.validate_task_snapshot(data)
    assert restored["authorities"][fixtures.AUTHORITY]["tasks"] == captured["authorities"][fixtures.AUTHORITY]["tasks"]
    assert restored["artifacts"] == captured["artifacts"]
    assert (future / "pending.json").read_text(encoding="utf-8") == "discarded future"


def test_task_backup_busy_writer_is_not_bypassed_by_force(tmp_path):
    home = tmp_path / "home"
    data = _make_task_palace(home)
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "backup", "--offline", "--force", "--no-quiesce"])
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "run", side_effect=AssertionError("must not snapshot")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "writer")


def test_backup_rejects_active_or_malformed_hub_registry(tmp_path):
    from mempalace_tasks import restore
    home = tmp_path / "home"
    data = _make_task_palace(home)
    record = restore.serverinfo_path(data)
    record.parent.mkdir(parents=True)
    args = pb.build_parser().parse_args(["--palace", str(home), "backup", "--offline"])
    for value in (json.dumps({"pid": os.getpid(), "host": "localhost", "port": 1234,
                             "palace_path": str(data), "read_only": True, "scheme": "http"}), "{"):
        record.write_text(value, encoding="utf-8")
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "run", side_effect=AssertionError("must not run")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "writer")


def test_staging_only_uses_staged_config_and_never_reads_or_mutates_live_target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{broken live config", encoding="utf-8")
    stage = tmp_path / "stage"
    data = _make_task_palace(stage, "custom", configured_home=home)
    before = (home / "config.json").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--from-stage", "--target", str(stage)])
    with patch.object(pb, "run", side_effect=AssertionError("no live operations")):
        assert pb.cmd_restore(args) == 0
    assert (home / "config.json").read_bytes() == before
    assert data.is_dir()
    assert sorted(p.name for p in home.iterdir()) == ["config.json"]


def test_restore_rejects_stage_inside_target_or_target_inside_stage(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for stage in (home, home / "stage", tmp_path):
        args = pb.build_parser().parse_args(
            ["--palace", str(home), "restore", "latest", "--target", str(stage)])
        with patch.object(pb, "run", side_effect=AssertionError("unsafe path must fail first")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "disjoint")

def test_restore_rejects_symlink_hidden_before_parent_traversal(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "link").symlink_to(home, target_is_directory=True)
    target = tmp_path / "link/../stage"
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(target)])
    with patch.object(pb, "run", side_effect=AssertionError("must reject link before restic")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "symlink")

def test_data_cannot_overlap_preserved_server_control_directory(tmp_path):
    home = tmp_path / "home"
    data = home / "server/data"
    data.mkdir(parents=True)
    _assert_raises(pb.BackupError, lambda: pb.resolve_data_path(home, data), "control")


def test_staging_only_refuses_an_incomplete_activation_marker(tmp_path):
    from mempalace_tasks import restore
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(stage, configured_home=home)
    (data / restore.PREPARE_MARKER).write_text(
        '{"schema_version":1,"phase":"preparing"}', encoding="utf-8")
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--from-stage", "--target", str(stage)])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "incomplete")
    assert not home.exists()


def test_in_place_restore_requires_offline_and_keeps_stage_on_failure(tmp_path):
    home = tmp_path / "home"
    data = _make_task_palace(home)
    stage = tmp_path / "stage"
    _make_task_palace(stage, configured_home=home)
    before = (data / "logstream.sqlite3").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place"])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "offline")
    assert (data / "logstream.sqlite3").read_bytes() == before
    assert stage.is_dir()


def test_offline_restore_activates_before_publication_preserves_locks_and_never_starts(tmp_path):
    from mempalace_tasks import restore, snapshot
    home = Path.home() / ".mempalace"
    data = _make_task_palace(home, "custom")
    (home / "future.txt").write_text("keep in rollback", encoding="utf-8")
    lock = restore.writer_lock_path(data)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"\0permanent lock diagnostics")
    inode, content = lock.stat().st_ino, lock.read_bytes()
    stage = tmp_path / "stage"
    staged_data = _make_task_palace(stage, "custom", configured_home=home)
    (stage / "restored.txt").write_text("snapshot", encoding="utf-8")
    (stage / "locks").mkdir()
    (stage / "locks/stale.lock").write_text("never publish", encoding="utf-8")
    before = snapshot.validate_task_snapshot(staged_data)
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place", "--offline", "--require-logstream"])
    with patch.object(pb, "run", side_effect=AssertionError("no restic/live repair/startup")):
        assert pb.cmd_restore(args) == 0
    after = snapshot.validate_task_snapshot(data)
    authority = "11111111-1111-1111-1111-111111111111"
    assert after["authorities"][authority]["epoch_id"] is not None
    assert before["authorities"][authority]["tasks"] == after["authorities"][authority]["tasks"]
    assert (lock.stat().st_ino, lock.read_bytes()) == (inode, content)
    assert not (home / "locks/stale.lock").exists()
    assert not (home / "future.txt").exists()
    assert (home / "restored.txt").read_text(encoding="utf-8") == "snapshot"
    backups = list(home.parent.glob(home.name + ".bak-*"))
    assert len(backups) == 1 and (backups[0] / "future.txt").exists()


def test_missing_declared_snapshot_logstream_refuses_staging_and_publication(tmp_path):
    home = tmp_path / "home"
    _make_task_palace(home)
    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", return_value=0):
        assert pb.cmd_backup(pb.build_parser().parse_args(
            ["--palace", str(home), "backup", "--offline"])) == 0
    stage = tmp_path / "stage"
    shutil.copytree(home, stage)
    (stage / "palace/logstream.sqlite3").unlink()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage), "--from-stage"])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "logstream")
    assert (home / "palace/logstream.sqlite3").exists()

def test_live_manifest_remembers_required_tasks_when_the_live_database_is_lost(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", return_value=0):
        assert pb.cmd_backup(pb.build_parser().parse_args(
            ["--palace", str(home), "backup", "--offline"])) == 0
    (data / "logstream.sqlite3").unlink()
    (stage / "palace/.mempalace").mkdir(parents=True)
    (stage / "palace/.mempalace/origin.json").write_text("{}", encoding="utf-8")
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "logstream")
    assert (home / pb.BACKUP_MANIFEST).exists()
    assert (stage / "palace/.mempalace/origin.json").exists()


def test_earlier_snapshot_does_not_require_later_live_authorities(tmp_path):
    from mempalace_tasks.snapshot import validate_task_snapshot

    authority_a = "11111111-1111-1111-1111-111111111111"
    authority_b = "22222222-2222-2222-2222-222222222222"
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    backup = pb.build_parser().parse_args(
        ["--palace", str(home), "backup", "--offline"])
    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", return_value=0):
        assert pb.cmd_backup(backup) == 0
    shutil.copytree(home, stage)
    _add_task_authority(data, authority_b)
    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", return_value=0):
        assert pb.cmd_backup(backup) == 0
    assert set(pb.load_backup_manifest(home)["authorities"]) == {authority_a, authority_b}
    assert pb.load_backup_manifest(stage)["authorities"] == [authority_a]
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place", "--offline", "--expected-authority", authority_a])
    assert pb.cmd_restore(args) == 0
    restored = validate_task_snapshot(data)
    assert set(restored["authorities"]) == {authority_a}
    assert restored["authorities"][authority_a]["task_count"] == 1
    assert restored["authorities"][authority_a]["epoch_id"] is not None


def test_explicitly_required_later_authority_still_rejects_older_snapshot(tmp_path):
    authority_b = "22222222-2222-2222-2222-222222222222"
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    shutil.copytree(home, stage)
    _add_task_authority(data, authority_b)
    before = (data / "logstream.sqlite3").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place", "--offline", "--expected-authority", authority_b])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "authorit")
    assert (data / "logstream.sqlite3").read_bytes() == before
    assert (stage / "palace/logstream.sqlite3").exists()
    assert not (home / pb.RESTORE_MARKER).exists()


def test_restore_partial_append_failure_never_moves_live_target(tmp_path):
    from mempalace_tasks import restore
    home = tmp_path / "home"
    data = _make_task_palace(home)
    stage = tmp_path / "stage"
    _make_task_palace(stage, configured_home=home)
    before = (data / "logstream.sqlite3").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    with patch.object(restore, "append_epoch_cli",
                      side_effect=restore.RestoreError("append_failed", "synthetic failure")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "not publishable")
    assert (data / "logstream.sqlite3").read_bytes() == before
    assert (stage / "palace" / restore.PREPARE_MARKER).exists()
    assert not (home / pb.RESTORE_MARKER).exists()


def test_publication_failure_rolls_back_without_replacing_lock_directory(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    (home / "locks").mkdir(parents=True)
    stage.mkdir()
    (home / "old.txt").write_text("old", encoding="utf-8")
    (stage / "new.txt").write_text("new", encoding="utf-8")
    inode = (home / "locks").stat().st_ino
    rename = Path.rename

    def fail_new(path, target):
        if path == stage / "new.txt":
            raise OSError("synthetic publication failure")
        return rename(path, target)

    with patch.object(Path, "rename", fail_new):
        _assert_raises(pb.BackupError, lambda: pb.publish_stage(home, stage), "rolled back")
    assert (home / "old.txt").read_text(encoding="utf-8") == "old"
    assert (stage / "new.txt").read_text(encoding="utf-8") == "new"
    assert (home / "locks").stat().st_ino == inode
    assert not (home / pb.RESTORE_MARKER).exists()


def test_publication_rollback_failure_keeps_precise_recovery_marker(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    stage.mkdir()
    (home / "old.txt").write_text("old", encoding="utf-8")
    (stage / "new.txt").write_text("new", encoding="utf-8")
    rename = Path.rename

    def fail(path, target):
        if path == stage / "new.txt" or (path.name == "old.txt" and path.parent != home):
            raise OSError("synthetic move failure")
        return rename(path, target)

    with patch.object(Path, "rename", fail):
        _assert_raises(pb.BackupError, lambda: pb.publish_stage(home, stage), "rollback incomplete")
    marker = json.loads((home / pb.RESTORE_MARKER).read_text(encoding="utf-8"))
    assert marker["phase"] == "rollback_incomplete"
    assert Path(marker["backup"]) / "old.txt" in list(Path(marker["backup"]).iterdir())
    assert marker["stage"] == str(stage)

def test_rollback_never_overwrites_an_unexpected_new_target_entry(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    stage.mkdir()
    (home / "old.txt").write_text("original", encoding="utf-8")
    (stage / "new.txt").write_text("snapshot", encoding="utf-8")
    rename = Path.rename

    def interfere(path, target):
        if path == stage / "new.txt":
            (home / "old.txt").write_text("unexpected writer", encoding="utf-8")
            raise OSError("new entry appeared outside the supported offline boundary")
        return rename(path, target)

    with patch.object(Path, "rename", interfere):
        _assert_raises(pb.BackupError, lambda: pb.publish_stage(home, stage), "rollback incomplete")
    assert (home / "old.txt").read_text(encoding="utf-8") == "unexpected writer"
    marker = json.loads((home / pb.RESTORE_MARKER).read_text(encoding="utf-8"))
    assert (Path(marker["backup"]) / "old.txt").read_text(encoding="utf-8") == "original"

def test_publication_refuses_cross_filesystem_before_moving_contents(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    stage.mkdir()
    (home / "old").write_text("keep", encoding="utf-8")
    original_stat = Path.stat

    def stat_other(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == stage:
            fields = list(info)
            fields[2] += 1
            return os.stat_result(fields)
        return info

    with patch.object(Path, "stat", stat_other):
        _assert_raises(pb.BackupError, lambda: pb.publish_stage(home, stage), "same-filesystem")
    assert (home / "old").read_text(encoding="utf-8") == "keep"

def test_publication_syncs_recovery_directory_parent_before_moving_old_contents(tmp_path):
    from mempalace_tasks import restore
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    stage.mkdir()
    (home / "old").write_text("keep", encoding="utf-8")
    (stage / "new").write_text("snapshot", encoding="utf-8")
    sync = restore.sync_directory

    def fail_parent(path):
        if path == home.parent:
            raise OSError("parent directory cannot be synchronized")
        sync(path)

    with patch.object(restore, "sync_directory", side_effect=fail_parent):
        _assert_raises(OSError, lambda: pb.publish_stage(home, stage), "parent")
    assert (home / "old").read_text(encoding="utf-8") == "keep"
    assert (stage / "new").read_text(encoding="utf-8") == "snapshot"


def test_materialization_failure_leaves_live_target_and_partial_stage_visible(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    before = (data / "logstream.sqlite3").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "latest", "--target", str(stage),
         "--in-place", "--offline"])

    def fail(argv, *, dry_run=False):
        assert argv[:2] == ["restic", "restore"]
        (stage / "partial").write_text("incomplete", encoding="utf-8")
        return 1

    with patch.object(pb, "require_restic_env"), patch.object(pb, "run", side_effect=fail):
        assert pb.cmd_restore(args) == 1
    assert (data / "logstream.sqlite3").read_bytes() == before
    assert (stage / "partial").exists()

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
            with tempfile.TemporaryDirectory() as d:
                user_home = Path(d) / "user"
                user_home.mkdir()
                with patch.dict(os.environ, {"MEMPALACE_PALACE_PATH": "",
                                            "MEMPAL_PALACE_PATH": "",
                                            "MEMPALACE_DAEMON_STATE_ROOT": "",
                                            "HOME": str(user_home), "USERPROFILE": str(user_home)}):
                    if "tmp_path" in params:
                        fn(Path(d))
                    else:
                        fn()
            print(f"ok   {fn.__name__}")
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_without_pytest())
