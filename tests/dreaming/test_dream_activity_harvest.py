"""Native dreaming harvest batches existing histories without a wrapper."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


HARVEST = Path(__file__).resolve().parents[2] / "skills/dreaming/scripts/dream_harvest.py"
SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def source(root, session=SESSION_A, *, description="Inspect deployment errors"):
    folder = root / session
    folder.mkdir(parents=True)
    path = folder / "events.jsonl"
    records = [
        {"id": "start", "type": "tool.execution_start", "data": {
            "toolCallId": "call", "toolName": "create_file",
            "arguments": {"path": "query.sql", "description": description, "content": "private"},
        }},
        {"id": "end", "type": "tool.execution_complete", "data": {
            "toolCallId": "call", "success": True,
        }},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def run(root, output, *extra):
    return subprocess.run(
        [sys.executable, str(HARVEST), "--task", "activity",
         "--session-root", str(root), "--out", str(output), *extra],
        text=True, capture_output=True, timeout=30, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_native_harvest_runs_two_views_and_returns_only_compact_summary(tmp_path):
    root = tmp_path / "sessions"
    original = source(root)
    source(root, SESSION_B)
    before = original.read_bytes()
    output = tmp_path / "summary.json"
    result = run(root, output, "--session-id", SESSION_A, "--session-id", SESSION_B,
                 "--activity-view", "both")
    assert result.returncode == 0, result.stderr
    assert output.read_text() == result.stdout
    summary = json.loads(result.stdout)
    assert summary["task"] == "activity"
    assert summary["status"] == "complete"
    assert summary["summary"]["attempted"] == 2
    assert summary["summary"]["succeeded"] == 2
    assert summary["summary"]["failed"] == 0
    assert summary["summary"]["activities"] == 2
    assert "Inspect deployment errors" not in result.stdout
    assert "private" not in result.stdout
    for session in summary["sessions"]:
        activities = json.loads(Path(session["activity_report"]).read_text())
        artifacts = json.loads(Path(session["artifact_report"]).read_text())
        assert activities["kind"] == "activity_intents"
        assert artifacts["kind"] == "artifact_reuse_index"
        assert artifacts["activity_report"] == session["activity_report"]
        assert activities["source"]["sha256"] == hashlib.sha256(before).hexdigest()
        assert len(artifacts["candidates"]) == 1
        assert artifacts["candidates"][0]["assessment"] == "needs_review"
        assert "claims" not in artifacts["candidates"][0]["observations"][0]
    assert original.read_bytes() == before


def test_explicit_activity_keeps_default_output_without_native_palace(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    result = subprocess.run(
        [sys.executable, str(HARVEST), "--task", "activity",
         "--session-root", str(root), "--session-id", SESSION_A],
        cwd=tmp_path, text=True, capture_output=True, timeout=30, check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "MEMPALACE_CONFIG": str(tmp_path / "missing-config.json")},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "worklist.json").read_text() == result.stdout
    summary = json.loads(result.stdout)
    assert summary["status"] == "complete"
    assert summary["summary"]["succeeded"] == 1
    assert not (tmp_path / "logstream.sqlite3").exists()


def test_batch_loads_each_source_once_even_when_emitting_both_views(tmp_path, monkeypatch):
    import dream_activity_harvest as batch
    root = tmp_path / "sessions"
    source(root)
    original = batch.load_evidence
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(batch, "load_evidence", counted)
    report = batch.harvest_sessions(str(root), [SESSION_A], str(tmp_path / "summary.json"),
                                    view="both")
    assert len(calls) == 1
    assert report["summary"]["succeeded"] == 1


def test_source_failure_is_recorded_and_does_not_hide_successful_sessions(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "summary.json"
    result = run(root, output, "--session-id", SESSION_A, "--session-id", SESSION_B)
    assert result.returncode == 1
    summary = json.loads(output.read_text())
    assert summary["status"] == "partial"
    assert summary["summary"]["succeeded"] == 1
    assert summary["summary"]["failed"] == 1
    failed = next(item for item in summary["sessions"] if item["session_id"] == SESSION_B)
    assert failed["status"] == "failed"
    assert failed["error"]
    assert failed["activity_report"] is None


@pytest.mark.parametrize("session", ["../escape", "/absolute", "not-a-uuid"])
def test_invalid_selection_cannot_escape_session_root(tmp_path, session):
    root = tmp_path / "sessions"
    root.mkdir()
    output = tmp_path / "unused.json"
    result = run(root, output, "--session-id", session)
    assert result.returncode != 0
    assert not output.exists()
    assert not output.with_name(output.name + ".sessions").exists()


def test_duplicate_ids_are_rejected_before_publishing(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "unused.json"
    result = run(root, output, "--session-id", SESSION_A, "--session-id", SESSION_A)
    assert result.returncode != 0
    assert not output.exists()


def test_existing_output_is_preserved(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "summary.json"
    output.write_text("existing")
    result = run(root, output, "--session-id", SESSION_A)
    assert result.returncode != 0
    assert output.read_text() == "existing"


def test_missing_session_store_is_not_valid_empty_selection(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    output = tmp_path / "unused.json"
    result = run(root, output, "--session-store", str(tmp_path / "missing.db"))
    assert result.returncode != 0
    assert not output.exists()


def test_index_selection_reuses_read_only_store_filters(tmp_path):
    import sqlite3
    root = tmp_path / "sessions"
    source(root)
    source(root, SESSION_B)
    store = tmp_path / "store.db"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE sessions(id TEXT,repository TEXT,branch TEXT,summary TEXT,"
            "created_at TEXT,updated_at TEXT,cwd TEXT)"
        )
        connection.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", [
            (SESSION_A, "owner/wanted", "main", "x", "2026-10-01", "2026-10-01", "/repo"),
            (SESSION_B, "owner/other", "main", "y", "2026-10-02", "2026-10-02", "/repo"),
        ])
    before = store.read_bytes()
    output = tmp_path / "summary.json"
    result = run(root, output, "--session-store", str(store), "--repository", "owner/wanted",
                 "--since", "2026-10-01", "--limit-sessions", "8")
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert [item["session_id"] for item in summary["sessions"]] == [SESSION_A]
    assert store.read_bytes() == before


def test_no_matching_index_rows_is_explicit_valid_empty_result(tmp_path):
    import sqlite3
    root = tmp_path / "sessions"
    root.mkdir()
    store = tmp_path / "store.db"
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE sessions(id TEXT,repository TEXT,branch TEXT,summary TEXT,"
            "created_at TEXT,updated_at TEXT,cwd TEXT)"
        )
    result = run(root, tmp_path / "summary.json", "--session-store", str(store))
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "complete"
    assert summary["summary"]["attempted"] == 0


def test_source_session_symlink_outside_root_is_refused(tmp_path):
    root = tmp_path / "sessions"
    outside = tmp_path / "elsewhere"
    source(outside)
    root.mkdir()
    (root / SESSION_A).symlink_to(outside / SESSION_A, target_is_directory=True)
    result = run(root, tmp_path / "summary.json", "--session-id", SESSION_A)
    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["summary"]["failed"] == 1
    assert summary["sessions"][0]["error"] == "session_path_outside_root"


def test_reports_and_summary_are_private(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "summary.json"
    result = run(root, output, "--session-id", SESSION_A)
    assert result.returncode == 0, result.stderr
    item = json.loads(result.stdout)["sessions"][0]
    assert output.stat().st_mode & 0o777 == 0o600
    assert Path(item["activity_report"]).stat().st_mode & 0o777 == 0o600
    assert output.with_name(output.name + ".sessions").stat().st_mode & 0o777 == 0o700


def test_activity_route_never_binds_a_palace(tmp_path, monkeypatch):
    import dream_harvest
    root = tmp_path / "sessions"
    source(root)

    def forbidden(*args, **kwargs):
        raise AssertionError("activity extraction must not open a palace")

    monkeypatch.setattr(dream_harvest.dream_palace, "bind_palace", forbidden)
    assert dream_harvest.main([
        "--task", "activity", "--session-root", str(root),
        "--session-id", SESSION_A, "--out", str(tmp_path / "summary.json"),
    ]) == 0


def test_explicit_ids_and_index_filters_cannot_silently_disagree(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "unused.json"
    result = run(root, output, "--session-id", SESSION_A, "--repository", "other")
    assert result.returncode != 0
    assert not output.exists()


def test_per_session_budget_failure_is_explicit_and_report_is_not_written(tmp_path):
    root = tmp_path / "sessions"
    source(root)
    output = tmp_path / "summary.json"
    result = run(root, output, "--session-id", SESSION_A, "--max-retained-bytes", "1")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "failed"
    assert report["sessions"][0]["activity_report"] is None
    assert report["sessions"][0]["error"]


@pytest.mark.parametrize("legacy_exception", [False, True])
def test_symlink_loop_does_not_abort_other_sessions(tmp_path, monkeypatch, legacy_exception):
    import dream_activity_harvest as batch
    root = tmp_path / "sessions"
    loop = source(root)
    loop.unlink()
    loop.symlink_to("events.jsonl")
    source(root, SESSION_B)
    if legacy_exception:
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == loop:
                raise RuntimeError("Symlink loop from supported pre-3.13 pathlib")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    output = tmp_path / "summary.json"
    report = batch.harvest_sessions(str(root), [SESSION_A, SESSION_B], str(output))
    assert report["status"] == "partial"
    assert report["summary"]["failed"] == 1
    assert report["summary"]["succeeded"] == 1
    assert report["sessions"][0]["error"]
    assert report["sessions"][1]["status"] == "complete"
    assert json.loads(output.read_text()) == report
