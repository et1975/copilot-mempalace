#!/usr/bin/env python3
"""Tests for palace_backup.py.

Pure-function and SQLite tests plus installed-handler procedural integration.
MemPalace and its model cache must already be installed for the latter; restic
uses a physical-copy seam. All homes/storage are disposable, never a live palace.

From the repository root, with ``PYTHONDONTWRITEBYTECODE=1`` exported:
``$TEST_PY -m pytest tests/mempalace-backup/test_palace_backup.py -q``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid5

import palace_backup as pb


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


def _seed_task_authority(con, authority):
    import test_snapshot as fixtures
    from mempalace_tasks.protocol import LogState, fold_record, make_epoch, make_proposal
    from mempalace_tasks.codec import canonical_json

    log = LogState(authority)
    index = con.execute("SELECT coalesce(max(rowid), 0) + 1 FROM events").fetchone()[0]
    activation = make_epoch(
        log, str(uuid5(UUID(authority), "backup-test-epoch")),
        str(uuid5(UUID(authority), "backup-test-activation")), fixtures.NOW)
    for command in (None, fixtures.genesis(), fixtures.create(2)):
        payload = activation if command is None else make_proposal(log, command, fixtures.NOW)
        raw = fixtures.raw_record(payload, index)
        fold_record(log, raw)
        con.execute("""INSERT INTO events VALUES (
            :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
            :branch, :base_commit, :status, :body, :created_at, :metadata_json,
            :origin_replica, :origin_seq, :hlc)""",
                    {**raw, "metadata_json": canonical_json(raw["metadata"])})
        index += 1


def _make_task_palace(home, data_name="palace", *, configured_home=None):
    import test_snapshot as fixtures

    data = home / data_name
    (data / ".mempalace").mkdir(parents=True)
    (data / ".mempalace/origin.json").write_text("{}", encoding="utf-8")
    (home / "config.json").write_text(json.dumps({
        "palace_path": str((configured_home or home) / data_name)}), encoding="utf-8")
    (data / "replica.json").write_text(
        json.dumps({"replica_id": fixtures.REPLICA}), encoding="utf-8")
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
        con.executescript(fixtures.SCHEMA)
        _seed_task_authority(con, fixtures.AUTHORITY)
        con.commit()
    return data


def _add_task_authority(data, authority):
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
        _seed_task_authority(con, authority)
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


@contextlib.contextmanager
def _published_procedural_palace(tmp_path, *, retired=False, orphan=False, origin=False):
    """Real installed handlers; session input and all palace state are disposable."""
    # MemPalace strips inherited PYTHONPATH on first import; preserve this
    # runner's already selected fixture/pytest paths, not package resolution.
    with patch.object(sys, "path", list(sys.path)):
        import mempalace
    import dream_palace
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    from dream_procedural_palace import append_event
    from dream_procedural import event_to_data
    from test_dream_procedural import NOW
    from test_dream_procedural_palace import installed_palace
    from test_dream_procedural_validate import GroundedFixture

    fixture = GroundedFixture()
    fixture.setUp()
    try:
        fixture.storage_patch.stop()
        home = tmp_path / "original"
        data = home / "custom"
        data.mkdir(parents=True)
        (data / ".mempalace").mkdir()
        (data / ".mempalace/origin.json").write_text("{}", encoding="utf-8")
        fixture.path = str(data)
        (home / "config.json").write_text(json.dumps({
            "palace_path": str(data), "backend": "sqlite_exact"}), encoding="utf-8")
        for ref in fixture.refs[:2]:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        cache = os.environ["DREAMING_TEST_MODEL_CACHE"]
        with patch.object(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", cache), \
                installed_palace(str(data)):
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                stored = writer.add_drawer("w", "diary", fixture.refs[2]["quote"])
                fixture.refs[2]["source_id"] = stored["drawer_id"]
                events = [fixture.proposal(), fixture.review()]
                if origin:
                    from dream_procedural import parse_event
                    from test_dream_procedural import event_data
                    stored = writer.add_drawer("w", "diary", "Generated lesson: lineage, not evidence.")
                    fixture.origin_id = stored["drawer_id"]
                    events[0] = parse_event(event_data(
                        origin_drawer_ids=[fixture.origin_id], evidence=fixture.refs[:3]))
                fixture.refs[3].update(source_kind="session_turn",
                    source_id=fixture.refs[3]["session_id"], turn_index=0, field="user_message")
                if retired:
                    held = fixture.review(3, verdict="hold", parent_review_ids=[events[1].event_id],
                        dispositions=[*event_to_data(events[1])["payload"]["dispositions"],
                            {"evidence_id": fixture.refs[3]["source_id"], "disposition": "contradicts",
                             "reason": "Original adverse observation.", "evidence": [fixture.refs[3]]}])
                    events.extend([held, fixture.review(4, verdict="retire",
                                                       parent_review_ids=[held.event_id])])
                for event in events:
                    append_event(str(data), "w", event, writer=writer,
                                 session_store=fixture.store, clock=lambda: NOW)
                if orphan:
                    from dream_procedural import parse_event
                    from dream_procedural_sources import capture_source
                    from dream_procedural_validate import acquire_original
                    from test_dream_procedural import event_data
                    ref = parse_event(event_data(evidence=[fixture.refs[3]])).payload.evidence[0]
                    source = acquire_original(ref, palace=str(data), session_store=fixture.store)
                    capture_source(source, palace=str(data), wing="orphans", writer=writer,
                                   captured_at=NOW, captured_by="fixture")
        # The source store is not a recovery input after publication.
        Path(fixture.store).unlink()
        yield home, data, fixture, events
    finally:
        fixture.doCleanups()


@contextlib.contextmanager
def _palace_only_reads():
    import dream_palace
    with patch("dream_procedural_validate._session_repository",
               side_effect=AssertionError("host session repository")), \
            patch("dream_procedural_validate.acquire_original",
                  side_effect=AssertionError("original acquisition")), \
            patch.object(dream_palace, "MempalaceWriter",
                         side_effect=AssertionError("writer construction")), \
            patch.object(dream_palace, "ensure_firewall_schema",
                         side_effect=AssertionError("schema repair")):
        yield


def _procedural_projection(data):
    from dream_procedural import Policy, project_rules
    from dream_procedural_palace import nonmutating_read, read_events, revalidate_sources
    from dream_procedural_validate import EvidenceReader
    from test_dream_procedural import NOW

    with nonmutating_read(str(data)):
        events = read_events(str(data), "w")
        projection = project_rules(events, as_of=NOW, policy=Policy())
        projection, diagnostics = revalidate_sources(
            projection, evidence_reader=EvidenceReader(str(data), "w"), as_of=NOW)
    assert not diagnostics
    return events, projection


def test_procedural_physical_backup_stage_recovers_without_host_or_original_palace(tmp_path):
    from dream_procedural import event_evidence
    from dream_procedural_validate import inspect_published_sources

    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        # Empty task storage selects the existing guarded capture path without
        # creating task authorities or requiring a task activation subprocess.
        with contextlib.closing(_make_logstream(data)) as con:
            con.execute("DELETE FROM event_artifacts")
            con.execute("DELETE FROM artifacts")
            con.execute("DELETE FROM events")
            con.commit()
        with _palace_only_reads():
            before_events, before_projection = _procedural_projection(data)
        assert set(before_events) == set(events)
        assert before_projection.rules[0].review_heads == (events[1].event_id,)
        assert before_projection.rules[0].eligible
        snapshot, stage = tmp_path / "physical-snapshot", tmp_path / "restore-stage"

        def physical_copy(argv, *, dry_run=False):
            assert not dry_run
            if argv[:2] == ["restic", "backup"]:
                assert argv[2] == str(home)
                # Prove current SQLite exclusion is held at the capture seam.
                with contextlib.closing(sqlite3.connect(
                        data / "sqlite_exact.sqlite3", timeout=0)) as contender:
                    _assert_raises(sqlite3.OperationalError,
                                   lambda: contender.execute("BEGIN IMMEDIATE"), "locked")
                shutil.copytree(home, snapshot)
            elif argv[:2] == ["restic", "restore"]:
                assert argv[2] == "saved:" + str(home)
                assert argv[-1] == str(stage)
                shutil.copytree(snapshot, stage, dirs_exist_ok=True)
            else:
                assert argv[:2] in (["restic", "snapshots"], ["restic", "check"])
            return 0

        backup = pb.build_parser().parse_args([
            "--palace", str(home), "backup", "--offline", "--require-logstream"])
        restore = pb.build_parser().parse_args([
            "--palace", str(home), "restore", "saved", "--target", str(stage)])
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "run", side_effect=physical_copy), _palace_only_reads():
            assert pb.cmd_backup(backup) == 0
            shutil.rmtree(home)
            assert not Path(fixture.store).exists()
            # A poisoned ambient path must not redirect stage inspection.
            with patch.dict(os.environ, {"MEMPALACE_PALACE_PATH": str(home / "missing")}):
                with patch("dream_procedural_validate.inspect_published_sources",
                           wraps=inspect_published_sources) as inspect:
                    assert pb.cmd_restore(restore) == 0
                    assert inspect.call_count
                    assert all(call.args == (str(stage / "custom"), "w")
                               for call in inspect.call_args_list)
            after_events, after_projection = _procedural_projection(stage / "custom")
            assert set(after_events) == set(before_events)
            assert {ref.source_hash for event in after_events for ref in event_evidence(event)} == {
                ref.source_hash for event in before_events for ref in event_evidence(event)}
            assert after_projection == before_projection
            assert not home.exists()
            publish = pb.build_parser().parse_args([
                "--palace", str(home), "restore", "saved", "--target", str(stage),
                "--from-stage", "--in-place", "--offline"])
            with patch.object(pb, "run", side_effect=AssertionError("no startup/task replay")):
                assert pb.cmd_restore(publish) == 0
            assert _procedural_projection(home / "custom") == (after_events, before_projection)


def _assert_procedural_stage_damage_blocked(tmp_path, damage, expected, **options):
    with _published_procedural_palace(tmp_path, **options) as (home, data, fixture, events):
        stage = tmp_path / "stage"
        shutil.copytree(home, stage)
        staged_data = stage / "custom"
        with contextlib.closing(sqlite3.connect(staged_data / "sqlite_exact.sqlite3")) as con:
            damage(con, fixture)
            con.commit()
        before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()}
        args = pb.build_parser().parse_args([
            "--palace", str(home), "restore", "saved", "--target", str(stage),
            "--from-stage", "--in-place", "--offline"])
        with _palace_only_reads(), \
                patch.object(pb, "run", side_effect=AssertionError("restic/repair/startup")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), expected)
        assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
        assert not list(tmp_path.glob("original.bak-*"))
        assert not (home / pb.RESTORE_MARKER).exists()
        assert staged_data.is_dir()


def test_procedural_stage_missing_capture_blocks_before_publication(tmp_path):
    def damage(con, fixture):
        con.execute("DELETE FROM documents WHERE json_extract(metadata_json, '$.room') = "
                    "'procedural-sources'")
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "uncaptured")


def test_procedural_stage_corrupt_capture_blocks_before_publication(tmp_path):
    def damage(con, fixture):
        con.execute("UPDATE documents SET document = 'corrupt capture' WHERE "
                    "json_extract(metadata_json, '$.room') = 'procedural-sources'")
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "source record")


def test_procedural_stage_deleted_original_is_distinct_from_missing_capture(tmp_path):
    def damage(con, fixture):
        import dream_palace
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        from test_dream_procedural_palace import installed_palace
        data = str(Path(con.execute("PRAGMA database_list").fetchone()[2]).parent)
        with patch.object(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH",
                          os.environ["DREAMING_TEST_MODEL_CACHE"]), installed_palace(data) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                result = server.TOOLS["mempalace_delete_drawer"]["handler"](
                    drawer_id=fixture.refs[2]["source_id"])
                assert result["success"], result
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "original_drawer_missing")

def test_procedural_stage_missing_only_retired_origin_blocks_publication(tmp_path):
    def damage(con, fixture):
        import dream_palace
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        from dream_procedural_validate import EvidenceReader
        from test_dream_procedural_palace import installed_palace
        data = str(Path(con.execute("PRAGMA database_list").fetchone()[2]).parent)
        con.execute("DELETE FROM documents WHERE id = ? OR "
                    "json_extract(metadata_json, '$.parent_drawer_id') = ?",
                    (fixture.origin_id, fixture.origin_id))
        con.commit()
        with patch.object(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH",
                          os.environ["DREAMING_TEST_MODEL_CACHE"]), \
                installed_palace(data), _palace_only_reads():
            reader = EvidenceReader(data, "w")
            for ref in fixture.proposal().payload.evidence:
                reader.resolve(ref)
            assert reader.sources.record_count == 4
            assert dream_palace.load_source_drawer(data, fixture.origin_id) is None
    _assert_procedural_stage_damage_blocked(
        tmp_path, damage, "origin_drawer_missing", origin=True, retired=True)


def test_procedural_stage_retired_adverse_source_is_still_required(tmp_path):
    def damage(con, fixture):
        con.execute("DELETE FROM documents WHERE "
                    "coalesce(json_extract(metadata_json, '$.parent_drawer_id'), id) IN ("
                    "SELECT coalesce(json_extract(metadata_json, '$.parent_drawer_id'), id) "
                    "FROM documents WHERE json_extract(metadata_json, '$.room') = "
                    "'procedural-sources' AND document LIKE ?)",
                    ("%" + fixture.refs[3]["session_id"] + "%",))
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "uncaptured", retired=True)


def test_procedural_stage_checks_orphan_sources_in_other_wings(tmp_path):
    def damage(con, fixture):
        con.execute("UPDATE documents SET document = 'corrupt orphan' WHERE "
                    "json_extract(metadata_json, '$.wing') = 'orphans'")
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "source record", orphan=True)


def test_procedural_stage_moved_event_room_cannot_be_silently_omitted(tmp_path):
    def damage(con, fixture):
        con.execute("UPDATE documents SET metadata_json=json_set(metadata_json, '$.room', 'diary') "
                    "WHERE json_extract(metadata_json, '$.room') = 'procedural'")
    _assert_procedural_stage_damage_blocked(tmp_path, damage, "wing/room")


def test_procedural_stage_refuses_custom_collection_instead_of_validating_default(tmp_path):
    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        (home / "config.json").write_text(json.dumps({
            "palace_path": str(data), "backend": "sqlite_exact", "collection_name": "other"}),
            encoding="utf-8")
        with _palace_only_reads():
            _assert_raises(pb.BackupError, lambda: pb._validate_stage(home, data), "collection")


def test_procedural_stage_unavailable_inspector_or_runtime_never_skips_validation(tmp_path):
    import builtins
    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        before = (data / "sqlite_exact.sqlite3").read_bytes()
        with patch.dict(sys.modules, {"dream_procedural_validate": None}):
            _assert_raises(pb.BackupError, lambda: pb._validate_stage(home, data), "requires")
        real_import = builtins.__import__

        def without_mempalace(name, *args, **kwargs):
            if name == "mempalace":
                raise ImportError("runtime unavailable")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=without_mempalace):
            _assert_raises(pb.BackupError, lambda: pb._validate_stage(home, data), "requires")
        assert (data / "sqlite_exact.sqlite3").read_bytes() == before


def test_procedural_stage_refuses_configured_backend_mismatch_without_writes(tmp_path):
    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        (home / "config.json").write_text(json.dumps({
            "palace_path": str(data), "backend": "chroma"}), encoding="utf-8")
        before = {p.relative_to(home): p.read_bytes() for p in home.rglob("*")
                  if p.is_file() and not p.name.endswith("-shm")}
        files_before = {p.relative_to(home) for p in home.rglob("*") if p.is_file()}
        with _palace_only_reads(), patch.dict(os.environ, {"MEMPALACE_BACKEND_EXPLICIT": ""}):
            _assert_raises(pb.BackupError, lambda: pb._validate_stage(home, data), "backend")
        assert {p.relative_to(home): p.read_bytes() for p in home.rglob("*")
                if p.is_file() and not p.name.endswith("-shm")} == before
        assert {p.relative_to(home) for p in home.rglob("*") if p.is_file()} == files_before


def test_procedural_stage_read_is_closed_nonmutating_and_ignores_live_home_config(tmp_path):
    import dream_palace
    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        stage = tmp_path / "stage"
        shutil.copytree(home, stage)
        # Make this the live default HOME config: even reading it is forbidden.
        live_config = Path.home() / ".mempalace/config.json"
        live_config.parent.mkdir()
        live_config.write_text("{broken live config", encoding="utf-8")
        before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*")
                  if p.is_file() and not p.name.endswith("-shm")}
        files_before = {p.relative_to(stage) for p in stage.rglob("*") if p.is_file()}
        read_bytes = Path.read_bytes
        read_text = Path.read_text
        opener = dream_palace.procedural_collection
        opened = []

        def guarded_bytes(path, *args, **kwargs):
            assert path != live_config, "read live target configuration"
            return read_bytes(path, *args, **kwargs)

        def guarded_text(path, *args, **kwargs):
            assert path != live_config, "read live target configuration"
            return read_text(path, *args, **kwargs)

        def remember_handle(path):
            collection = opener(path)
            opened.append(collection)
            return collection

        args = pb.build_parser().parse_args([
            "--palace", str(home), "restore", "saved", "--target", str(stage), "--from-stage"])
        with _palace_only_reads(), \
                patch.object(Path, "read_bytes", guarded_bytes), \
                patch.object(Path, "read_text", guarded_text), \
                patch.object(dream_palace, "procedural_collection", side_effect=remember_handle), \
                patch.object(pb, "run", side_effect=AssertionError("no live operations")):
            assert pb.cmd_restore(args) == 0
        assert {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*")
                if p.is_file() and not p.name.endswith("-shm")} == before
        assert {p.relative_to(stage) for p in stage.rglob("*") if p.is_file()} == files_before
        assert opened
        _assert_raises(sqlite3.ProgrammingError,
                       lambda: opened[0]._handle.conn.execute("SELECT 1"), "closed")
        assert live_config.read_text(encoding="utf-8") == "{broken live config"


def test_legacy_stage_without_procedural_state_needs_no_mempalace_or_dreaming(tmp_path):
    import builtins
    home = tmp_path / "stage"
    data = _make_exact_palace(home)
    real_import = builtins.__import__

    def without_optional_runtime(name, *args, **kwargs):
        assert not name.startswith(("mempalace", "dream_")), name
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=without_optional_runtime):
        assert pb._validate_stage(home, data) is None


def test_procedural_fresh_cli_loads_sibling_tree_through_installed_script_symlink(tmp_path):
    with _published_procedural_palace(tmp_path) as (home, data, fixture, events):
        stage = tmp_path / "stage"
        shutil.copytree(home, stage)
        script = tmp_path / "installed-palace-backup.py"
        script.symlink_to(Path(pb.__file__).resolve())
        before = {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*")
                  if p.is_file() and not p.name.endswith("-shm")}
        # Deliberately omit dreaming scripts from PYTHONPATH and use another cwd.
        sidecar = Path(pb.__file__).resolve().parents[3] / "sidecar/src"
        env = {**os.environ, "PYTHONPATH": str(sidecar), "MEMPALACE_MCP_READ_ONLY": "1",
               "COPILOT_SESSION_STORE": str(tmp_path / "absent-host.db"),
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "ANONYMIZED_TELEMETRY": "False"}
        result = subprocess.run([
            sys.executable, str(script), "--palace", str(home), "restore", "saved",
            "--from-stage", "--target", str(stage)], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Validated private stage" in result.stderr
        assert {p.relative_to(stage): p.read_bytes() for p in stage.rglob("*")
                if p.is_file() and not p.name.endswith("-shm")} == before
        assert not (tmp_path / "absent-host.db").exists()


def _make_exact_palace(home, *, configured_home=None):
    data = home / "custom"
    (data / ".mempalace").mkdir(parents=True)
    (data / ".mempalace/origin.json").write_text("{}", encoding="utf-8")
    (home / "config.json").write_text(json.dumps({
        "palace_path": str((configured_home or home) / "custom")}), encoding="utf-8")
    _make_wal_db(data / "sqlite_exact.sqlite3", 1)
    return data


def test_sqlite_exact_only_inventory_and_checkpoint_include_committed_wal(tmp_path):
    home = tmp_path / "home"
    data = _make_exact_palace(home)
    db = data / "sqlite_exact.sqlite3"
    with contextlib.closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO t VALUES (99)")
        writer.commit()
        wal = data / "sqlite_exact.sqlite3-wal"
        assert wal.stat().st_size > 0
        assert set(pb.backup_inventory(home)) == {
            db, wal, data / "sqlite_exact.sqlite3-shm",
        }
        assert pb.checkpoint_all(home)
        assert wal.stat().st_size == 0
        with contextlib.closing(sqlite3.connect(db)) as reader:
            assert reader.execute("SELECT id FROM t ORDER BY id").fetchall() == [(0,), (99,)]
    assert not (data / "chroma.sqlite3").exists()
    assert not (data / "logstream.sqlite3").exists()
    assert not (data / "replica.json").exists()


def test_sqlite_exact_busy_writer_prevents_clean_checkpoint(tmp_path):
    home = tmp_path / "home"
    data = _make_exact_palace(home)
    with contextlib.closing(sqlite3.connect(data / "sqlite_exact.sqlite3")) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert pb.checkpoint_all(home) is False
        writer.rollback()
        assert pb.checkpoint_all(home) is True


def test_sqlite_exact_orphan_sidecars_refuse_before_checkpoint(tmp_path):
    for suffix in ("-wal", "-shm"):
        home = tmp_path / suffix
        data = home / "palace"
        data.mkdir(parents=True)
        sidecar = data / ("sqlite_exact.sqlite3" + suffix)
        sidecar.write_bytes(b"orphan")
        _assert_raises(pb.BackupError, lambda: pb.checkpoint_all(home), "missing")
        assert sidecar.read_bytes() == b"orphan"
        assert not (data / "sqlite_exact.sqlite3").exists()


def test_sqlite_exact_corruption_refuses_force_backup_before_checkpoint(tmp_path):
    home = tmp_path / "home"
    data = _make_exact_palace(home)
    db = data / "sqlite_exact.sqlite3"
    db.write_bytes(b"not a SQLite database")
    args = pb.build_parser().parse_args([
        "--palace", str(home), "backup", "--offline", "--force", "--no-quiesce"])
    with patch.object(pb, "require_restic_env"), \
            patch.object(pb, "checkpoint_db", side_effect=AssertionError("must validate first")), \
            patch.object(pb, "run", side_effect=AssertionError("must not snapshot")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_backup(args), "sqlite_exact")
    assert db.read_bytes() == b"not a SQLite database"


def test_sqlite_exact_only_physical_restore_preserves_committed_wal(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_exact_palace(home)
    db = data / "sqlite_exact.sqlite3"
    checkpoint = pb.checkpoint_all
    with contextlib.closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")

        def checkpoint_then_commit(*args, **kwargs):
            clean = checkpoint(*args, **kwargs)
            writer.execute("INSERT INTO t VALUES (99)")
            writer.commit()
            return clean

        def capture(argv, *, dry_run=False):
            if argv[:2] == ["restic", "backup"]:
                assert (data / "sqlite_exact.sqlite3-wal").stat().st_size > 0
                shutil.copytree(home, stage)
            return 0

        args = pb.build_parser().parse_args([
            "--palace", str(home), "backup", "--offline", "--no-quiesce"])
        with patch.object(pb, "require_restic_env"), \
                patch.object(pb, "checkpoint_all", side_effect=checkpoint_then_commit), \
                patch.object(pb, "run", side_effect=capture):
            assert pb.cmd_backup(args) == 0
        writer.execute("INSERT INTO t VALUES (100)")
        writer.commit()
    args = pb.build_parser().parse_args([
        "--palace", str(home), "restore", "latest", "--target", str(stage),
        "--from-stage", "--in-place", "--offline"])
    with patch.object(pb, "run", side_effect=AssertionError("no live operations")):
        assert pb.cmd_restore(args) == 0
    with contextlib.closing(sqlite3.connect(db)) as reader:
        assert reader.execute("SELECT id FROM t ORDER BY id").fetchall() == [(0,), (99,)]
    assert pb.integrity_check(db) == "ok"
    assert (data / ".mempalace/origin.json").read_text(encoding="utf-8") == "{}"
    assert not (data / "chroma.sqlite3").exists()
    assert not (data / "logstream.sqlite3").exists()


def _assert_exact_restore_refuses_writer(tmp_path, busy_stage):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_exact_palace(home)
    staged_data = _make_exact_palace(stage, configured_home=home)
    (home / "untouched").write_text("live", encoding="utf-8")
    locked_data = staged_data if busy_stage else data
    args = pb.build_parser().parse_args([
        "--palace", str(home), "restore", "latest", "--target", str(stage),
        "--from-stage", "--in-place", "--offline"])
    with contextlib.closing(sqlite3.connect(locked_data / "sqlite_exact.sqlite3")) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with patch.object(pb, "run", side_effect=AssertionError("no live operations")):
            _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "writer")
    assert (home / "untouched").read_text(encoding="utf-8") == "live"
    assert (staged_data / "sqlite_exact.sqlite3").exists()
    assert not list(home.parent.glob(home.name + ".bak-*"))
    assert not (home / pb.RESTORE_MARKER).exists()


def test_sqlite_exact_restore_guard_refuses_live_writer(tmp_path):
    _assert_exact_restore_refuses_writer(tmp_path, busy_stage=False)


def test_sqlite_exact_restore_guard_refuses_stage_writer(tmp_path):
    _assert_exact_restore_refuses_writer(tmp_path, busy_stage=True)


def test_sqlite_exact_corrupt_stage_refuses_before_publication(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_exact_palace(home)
    staged_data = _make_exact_palace(stage, configured_home=home)
    (staged_data / "sqlite_exact.sqlite3").write_bytes(b"corrupt stage")
    before = (data / "sqlite_exact.sqlite3").read_bytes()
    args = pb.build_parser().parse_args([
        "--palace", str(home), "restore", "latest", "--target", str(stage),
        "--from-stage", "--in-place", "--offline"])
    with patch.object(pb, "run", side_effect=AssertionError("no live operations")):
        _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "sqlite_exact")
    assert (data / "sqlite_exact.sqlite3").read_bytes() == before
    assert (staged_data / "sqlite_exact.sqlite3").read_bytes() == b"corrupt stage"


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
                 "knowledge_graph.sqlite3", "sqlite_exact.sqlite3",
                 "sqlite_exact.sqlite3-wal", "sqlite_exact.sqlite3-shm"):
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


def _restore_without_sidecar(home, stage, *flags):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-S", str(Path(pb.__file__).resolve()),
         "--palace", str(home), "restore", "fixture", "--target", str(stage),
         "--from-stage", *flags],
        cwd=stage.parent, env=environment, capture_output=True, text=True,
        timeout=10, check=False,
    )


def _memory_stage(stage):
    data = stage / "palace"
    (data / ".mempalace").mkdir(parents=True)
    (data / ".mempalace/origin.json").write_text("{}", encoding="utf-8")
    (data / "memory.txt").write_text("restored evidence", encoding="utf-8")
    return data


def test_memory_only_staging_does_not_require_sidecar_installation(tmp_path):
    home, stage = tmp_path / "missing-home", tmp_path / "stage"
    _memory_stage(stage)
    result = _restore_without_sidecar(home, stage)
    assert result.returncode == 0, result.stderr
    assert "Validated private stage" in result.stderr
    assert not home.exists()
    assert (stage / "palace/memory.txt").read_text() == "restored evidence"


def test_memory_only_publication_without_sidecar_preserves_control_inodes(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    (home / "locks").mkdir(parents=True)
    (home / "locks/owned-control").write_text("keep", encoding="utf-8")
    (home / "old-memory.txt").write_text("original evidence", encoding="utf-8")
    inode = (home / "locks").stat().st_ino
    _memory_stage(stage)
    result = _restore_without_sidecar(home, stage, "--in-place", "--offline")
    assert result.returncode == 0, result.stderr
    assert (home / "palace/memory.txt").read_text() == "restored evidence"
    assert (home / "locks").stat().st_ino == inode
    assert (home / "locks/owned-control").read_text() == "keep"
    backup, = home.parent.glob("home.bak-*")
    assert (backup / "old-memory.txt").read_text() == "original evidence"
    assert not (home / pb.RESTORE_MARKER).exists()


def test_task_restore_without_sidecar_still_fails_without_publication(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    (home / "unchanged").write_text("live", encoding="utf-8")
    _make_task_palace(stage, configured_home=home)
    result = _restore_without_sidecar(home, stage, "--in-place", "--offline")
    assert result.returncode == 1
    assert "preinstalled epoch-aware" in result.stderr
    assert (home / "unchanged").read_text() == "live"
    assert not list(home.parent.glob("home.bak-*"))


def test_memory_restore_without_sidecar_refuses_active_hub(tmp_path):
    from mempalace_tasks import restore

    home, stage = tmp_path / "home", tmp_path / "stage"
    (home / "palace").mkdir(parents=True)
    (home / "unchanged").write_text("live", encoding="utf-8")
    _memory_stage(stage)
    data = home / "palace"
    registry = restore.serverinfo_path(data)
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({
        "pid": os.getpid(), "host": "127.0.0.1", "port": 1234,
        "palace_path": str(data), "read_only": False, "scheme": "http",
    }), encoding="utf-8")
    result = _restore_without_sidecar(home, stage, "--in-place", "--offline")
    assert result.returncode == 1
    assert "active" in result.stderr.lower(), result.stderr
    assert (home / "unchanged").read_text() == "live"
    assert not list(home.parent.glob("home.bak-*"))


def test_memory_restore_without_sidecar_refuses_busy_sqlite_writer(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = home / "palace"
    data.mkdir(parents=True)
    _memory_stage(stage)
    with contextlib.closing(sqlite3.connect(data / "chroma.sqlite3")) as writer:
        writer.execute("CREATE TABLE memories (value TEXT)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        result = _restore_without_sidecar(home, stage, "--in-place", "--offline")
    assert result.returncode == 1
    assert "writer" in result.stderr.lower(), result.stderr
    assert not list(home.parent.glob("home.bak-*"))


def test_memory_restore_without_sidecar_obeys_the_same_writer_lease(tmp_path):
    from mempalace_tasks import restore

    home, stage = tmp_path / "home", tmp_path / "stage"
    data = home / "palace"
    data.mkdir(parents=True)
    _memory_stage(stage)
    with restore.writer_lease(data) as lock:
        inode, contents = lock.stat().st_ino, lock.read_bytes()
        result = _restore_without_sidecar(home, stage, "--in-place", "--offline")
        assert result.returncode == 1
        assert "writer lease" in result.stderr.lower(), result.stderr
        assert (lock.stat().st_ino, lock.read_bytes()) == (inode, contents)
    assert not list(home.parent.glob("home.bak-*"))
    assert (stage / "palace/memory.txt").exists()


def test_memory_restore_without_sidecar_rejects_linked_staging_files(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _memory_stage(stage)
    outside = tmp_path / "outside"
    outside.write_text("do not read or publish", encoding="utf-8")
    link = data / "linked"
    link.symlink_to(outside)
    result = _restore_without_sidecar(home, stage)
    assert result.returncode == 1
    assert "link" in result.stderr.lower(), result.stderr
    link.unlink()
    os.link(outside, link)
    result = _restore_without_sidecar(home, stage)
    assert result.returncode == 1
    assert "regular" in result.stderr.lower(), result.stderr
    assert outside.read_text() == "do not read or publish"
    assert not home.exists()


def test_memory_restore_without_sidecar_honors_explicit_task_requirement(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    _memory_stage(stage)
    for flags in (
        ("--require-logstream",),
        ("--expected-authority", "11111111-1111-1111-1111-111111111111"),
    ):
        result = _restore_without_sidecar(home, stage, *flags)
        assert result.returncode == 1
        assert "logstream" in result.stderr.lower(), result.stderr
    assert not home.exists()


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
    _make_wal_db(data / "sqlite_exact.sqlite3", 1)
    commands = []

    def restic(argv, *, dry_run=False):
        commands.append(argv)
        if argv[:2] == ["restic", "backup"]:
            assert argv[2] == str(home)
            with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3", timeout=0)) as writer:
                _assert_raises(sqlite3.OperationalError,
                               lambda: writer.execute("UPDATE events SET status='blocked'"), "locked")
            with contextlib.closing(sqlite3.connect(data / "sqlite_exact.sqlite3", timeout=0)) as writer:
                _assert_raises(sqlite3.OperationalError,
                               lambda: writer.execute("INSERT INTO t VALUES (99)"), "locked")
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
            raw = fixtures.raw_record(payload, 4)
            writer.execute("""INSERT INTO events VALUES (
                :id, :type, :stream, :room, :from_agent, :to_agent, :correlation_id,
                :branch, :base_commit, :status, :body, :created_at, :metadata_json,
                :origin_replica, :origin_seq, :hlc)""",
                           {**raw, "metadata_json": canonical_json(raw["metadata"])})
            writer.execute("INSERT INTO artifacts VALUES "
                           "('artifact-in-cut', 'note', ?, 5, 'hello', 'operator', ?, '{}', ?)",
                           ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                            fixtures.NOW, fixtures.REPLICA))
            writer.execute("INSERT INTO event_artifacts VALUES ('evt-000004', 'artifact-in-cut')")
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
    assert after["authorities"][authority]["epoch_id"] != before["authorities"][authority]["epoch_id"]
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


def test_valid_snapshot_replaces_logically_corrupt_live_task_history(tmp_path):
    from mempalace_tasks.snapshot import validate_task_snapshot

    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    shutil.copytree(home, stage)
    before = validate_task_snapshot(stage / "palace")
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as con:
        con.execute("UPDATE events SET body='{}' WHERE type='mptask.command'")
        con.commit()
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    damaged = (data / "logstream.sqlite3").read_bytes()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "fixture", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    assert pb.cmd_restore(args) == 0
    after = validate_task_snapshot(data)
    authority = "11111111-1111-1111-1111-111111111111"
    assert after["authorities"][authority]["tasks"] == before["authorities"][authority]["tasks"]
    assert after["authorities"][authority]["epoch_id"] != before["authorities"][authority]["epoch_id"]
    backup, = home.parent.glob("home.bak-*")
    assert (backup / "palace/logstream.sqlite3").read_bytes() == damaged


def test_corrupt_selected_task_snapshot_still_refuses_publication(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    shutil.copytree(home, stage)
    live = (data / "logstream.sqlite3").read_bytes()
    with contextlib.closing(sqlite3.connect(stage / "palace/logstream.sqlite3")) as con:
        con.execute("UPDATE events SET body='{}' WHERE type='mptask.command'")
        con.commit()
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "fixture", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "protocol")
    assert (data / "logstream.sqlite3").read_bytes() == live
    assert not list(home.parent.glob("home.bak-*"))


def test_corrupt_live_history_does_not_bypass_sqlite_writer_exclusion(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = _make_task_palace(home)
    shutil.copytree(home, stage)
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "fixture", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    with contextlib.closing(sqlite3.connect(data / "logstream.sqlite3")) as writer:
        writer.execute("UPDATE events SET body='{}' WHERE type='mptask.command'")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "writer")
    assert not list(home.parent.glob("home.bak-*"))
    assert (stage / "palace/logstream.sqlite3").is_file()


def test_orphan_live_logstream_sidecar_does_not_allow_task_free_restore(tmp_path):
    home, stage = tmp_path / "home", tmp_path / "stage"
    data = home / "palace"
    data.mkdir(parents=True)
    (data / "logstream.sqlite3-wal").write_bytes(b"unrecovered task storage")
    _memory_stage(stage)
    args = pb.build_parser().parse_args(
        ["--palace", str(home), "restore", "fixture", "--target", str(stage),
         "--from-stage", "--in-place", "--offline"])
    _assert_raises(pb.BackupError, lambda: pb.cmd_restore(args), "logstream")
    assert (data / "logstream.sqlite3-wal").read_bytes() == b"unrecovered task storage"
    assert not list(home.parent.glob("home.bak-*"))


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
    home, stage = tmp_path / "home", tmp_path / "stage"
    home.mkdir()
    stage.mkdir()
    (home / "old").write_text("keep", encoding="utf-8")
    (stage / "new").write_text("snapshot", encoding="utf-8")
    sync = pb.palace_restore_io.sync_directory

    def fail_parent(path):
        if path == home.parent:
            raise OSError("parent directory cannot be synchronized")
        sync(path)

    with patch.object(pb.palace_restore_io, "sync_directory", side_effect=fail_parent):
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
