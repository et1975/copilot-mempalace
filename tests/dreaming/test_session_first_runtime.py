"""Explicit session-preview compatibility against a real read-only host store."""
import hashlib
import json
import sqlite3
import sys

import pytest

import dream_harvest as dh
import dream_palace
import dream_sessions
import dream_survey as ds


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "dream_sessions", dream_sessions)
    store = tmp_path / "sessions.db"
    with sqlite3.connect(store) as con:
        con.executescript("""
            CREATE TABLE sessions(
                id TEXT, repository TEXT, branch TEXT, summary TEXT,
                created_at TEXT, updated_at TEXT, cwd TEXT);
            CREATE TABLE turns(
                session_id TEXT, turn_index INTEGER, user_message TEXT,
                assistant_response TEXT, timestamp TEXT);
        """)
        for i in range(60):
            day = f"2026-09-{i // 3 + 1:02d}T{i % 3:02d}:00:00Z"
            con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (f"s{i:02d}", "owner/project", "main", "topic", day, day, "/project"))
            con.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?)",
                        (f"s{i:02d}", 0, f"topic{i // 3 % 7} request {i}", "response", day))
        con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("other", "owner/unrelated", "main", "other", "2026-09-30", "", "/other"))
        con.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?)",
                    ("other", 0, "topic0 unrelated", "", "2026-09-30"))
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(store))
    monkeypatch.setattr(dream_palace, "bind_palace", lambda path: str(path))
    monkeypatch.setattr(
        dream_palace, "_palace_embed",
        lambda palace, texts: [[float(text.startswith(f"topic{j}")) for j in range(7)]
                               for text in texts],
    )

    def unexpected(*args, **kwargs):
        pytest.fail("session-first workflow must not read palace drawers, wings or KG")

    for name in ("list_wings", "load_observation_entries", "load_logical_drawers",
                 "load_premises"):
        monkeypatch.setattr(dream_palace, name, unexpected)
    return store


def run_harvest(tmp_path, *options):
    if "--task" not in options:
        options = ("--task", "reflect", *options)
        if "--source" not in options:
            options = (*options, "--source", "sessions")
    output = tmp_path / "worklist.json"
    rc = dh.main(["--palace", str(tmp_path), "--out", str(output), *options])
    return rc, json.loads(output.read_text()) if output.exists() else None


def test_explicit_preview_can_bound_repository_sessions_and_seeds(session_store, tmp_path):
    rc, worklist = run_harvest(tmp_path, "--repository", "owner/project",
                               "--limit-sessions", "50", "--max-candidates", "5")
    assert rc == 0
    assert worklist["task"] == "reflect"
    assert len(worklist["items"]) == 5
    assert worklist["scope"]["source"] == "sessions"
    assert worklist["scope"]["repository"] == "owner/project"
    assert worklist["scope"]["limit_sessions"] == 50
    assert worklist["params"]["top_k"] == 5
    for item in worklist["items"]:
        assert item["reflect_kind"] == "converge"
        assert item["decision"] is None
        assert len(item["evidence"]["support_ids"]) >= 2
        for member in item["members"]:
            assert member["session_id"] != "other"
            assert int(member["session_id"][1:]) < 50
            assert member["content_hash"] == hashlib.sha256(member["text"].encode()).hexdigest()


def test_survey_scans_once_without_enumerating_destination_wings(session_store, tmp_path, monkeypatch):
    calls = []
    load = dream_sessions.load_sessions

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return load(*args, **kwargs)

    monkeypatch.setattr(dream_sessions, "load_sessions", observe)
    out = tmp_path / "report.json"
    worklists = tmp_path / "worklists"
    assert ds.main(["--palace", str(tmp_path), "--tasks", "reflect", "--source", "sessions",
                    "--repository", "owner/project", "--limit-sessions", "50", "--max-candidates", "5",
                    "--out", str(out), "--worklists-dir", str(worklists)]) == 0
    report = json.loads(out.read_text())
    assert set(report["tasks"]) == {"reflect"}
    assert report["tasks"]["reflect"]["total"] == 5
    assert "by_wing" not in report["tasks"]["reflect"]
    assert len(calls) == 1
    assert json.loads((worklists / "reflect.sessions.json").read_text())["scope"]["source"] == "sessions"


def test_survey_forwards_scope_bounds_and_steering(session_store, tmp_path):
    worklists = tmp_path / "worklists"
    assert ds.main([
        "--palace", str(tmp_path), "--tasks", "reflect", "--wings", "destination", "--source", "sessions",
        "--repository", "owner/project", "--since", "2026-09-10",
        "--limit-sessions", "12", "--max-candidates", "2",
        "--instructions", "Prefer build validation lessons", "--worklists-dir", str(worklists),
    ]) == 0
    worklist = json.loads((worklists / "reflect.sessions.json").read_text())
    assert len(worklist["items"]) == 2
    assert worklist["scope"] == {
        "wing": "destination", "room": None, "source": "sessions",
        "repository": "owner/project", "since": "2026-09-10", "limit_sessions": 12,
    }
    assert worklist["params"]["top_k"] == 2
    assert "Prefer build validation lessons" in worklist["instructions"]
    assert all(27 <= int(m["session_id"][1:]) < 39
               for item in worklist["items"] for m in item["members"])


@pytest.mark.parametrize("main", [dh.main, ds.main])
@pytest.mark.parametrize("repository", [["--repository", ""], ["--repository", "  "]])
def test_supplied_repository_filter_must_be_nonblank(main, repository, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--palace", str(tmp_path), "--out", str(tmp_path / "unexpected.json"), *repository])
    assert error.value.code == 2
    assert "repository" in capsys.readouterr().err


@pytest.mark.parametrize("main", [dh.main, ds.main])
@pytest.mark.parametrize("flag", ["--limit-sessions", "--max-candidates"])
@pytest.mark.parametrize("value", ["0", "-1", "nope"])
def test_invalid_bounds_fail_before_reading_sources(main, flag, value, tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--palace", str(tmp_path), "--out", str(tmp_path / "unexpected.json"),
              "--repository", "owner/project", flag, value])
    assert error.value.code == 2


@pytest.mark.parametrize("options", [
    ["--wings", "first,"],
    ["--tasks", "reflect,pattern", "--source", "sessions"],
    ["--tasks", "merge", "--source", "sessions"],
    ["--source", "diary", "--repository", "owner/project"],
    ["--tasks", "unknown"],
    ["--tasks", ""],
])
def test_survey_rejects_ambiguous_or_ignored_scope(options, tmp_path):
    with pytest.raises(SystemExit) as error:
        ds.main(["--palace", str(tmp_path), "--repository", "owner/project", *options])
    assert error.value.code == 2


@pytest.mark.parametrize("state", ["missing", "corrupt", "bad-schema", "missing-turns"])
@pytest.mark.parametrize("main", [dh.main, ds.main])
def test_missing_or_broken_session_source_is_not_successful_empty_scan(
        state, main, session_store, tmp_path, capsys):
    session_store.unlink()
    if state == "corrupt":
        session_store.write_text("not sqlite")
    elif state == "bad-schema":
        with sqlite3.connect(session_store) as con:
            con.execute("CREATE TABLE unexpected(value TEXT)")
    elif state == "missing-turns":
        with sqlite3.connect(session_store) as con:
            con.execute("CREATE TABLE sessions(id TEXT)")
    output = tmp_path / "failed.json"
    rc = main(["--palace", str(tmp_path), "--task" if main is dh.main else "--tasks",
               "reflect", "--source", "sessions", "--repository", "owner/project", "--out", str(output)])
    assert rc != 0
    assert not output.exists()
    assert "session" in capsys.readouterr().err.lower()
    assert session_store.exists() == (state != "missing")


def test_successful_empty_scan_is_valid(session_store, tmp_path):
    rc, worklist = run_harvest(tmp_path, "--repository", "owner/no-match")
    assert rc == 0
    assert worklist["items"] == []


def test_explicit_legacy_reflect_still_uses_drawer_clusters(tmp_path, monkeypatch):
    import dream_reflect
    monkeypatch.setattr(dream_palace, "bind_palace", str)
    seen = []

    def gather(palace, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(dream_reflect, "gather_reflect_seeds", gather)
    rc, worklist = run_harvest(tmp_path, "--task", "reflect", "--wing", "existing")
    assert rc == 0
    assert seen == [{"wing": "existing", "room": None, "k": 5, "top_n": 500}]
    assert worklist["scope"]["source"] is None


def test_explicit_merge_still_works_without_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(dream_palace, "bind_palace", str)
    monkeypatch.setattr(dream_palace, "find_duplicate_clusters", lambda *args, **kwargs: [])
    monkeypatch.setattr(dh, "live_protected_drawer_ids", lambda palace: set())
    rc, worklist = run_harvest(tmp_path, "--task", "merge")
    assert rc == 0
    assert worklist["task"] == "merge"


def test_explicit_sessions_keep_unscoped_and_oldest_first_compatibility(session_store, tmp_path):
    rc, worklist = run_harvest(tmp_path, "--task", "reflect", "--source", "sessions",
                                "--limit-sessions", "3")
    assert rc == 0
    assert worklist["items"][0]["evidence"]["support_ids"] == ["s00", "s01", "s02"]


def test_recent_session_adapter_selects_newest_slice_and_handles_mixed_timestamps(session_store):
    with sqlite3.connect(session_store) as con:
        con.execute("UPDATE sessions SET created_at = ? WHERE id = 's59'",
                    ("2026-09-20 23:00:00",))
    observations = dream_sessions.load_session_observations(
        str(session_store), repository="owner/project", limit_sessions=2, recent_first=True)
    assert [obs["session_id"] for obs in observations] == ["s59", "s58"]


def test_session_observations_fail_for_missing_store(tmp_path):
    with pytest.raises(FileNotFoundError):
        dream_sessions.load_session_observations(str(tmp_path / "missing.db"))


@pytest.mark.parametrize("main", [dh.main, ds.main])
def test_invalid_since_reports_actionable_error(main, session_store, tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--palace", str(tmp_path), "--task" if main is dh.main else "--tasks",
              "reflect", "--source", "sessions", "--repository", "owner/project", "--since", "nonsense"])
    assert error.value.code == 2
    assert "--since" in capsys.readouterr().err


_EQUIVALENT_SINCE_BOUNDS = [
    "20260920",
    "2026-09-20T00:00:00+0000",
    "20260920T000000+0000",
    "2026-09-20T01:00:30+01:00:30",
    "2026-09-20T02:00:00+02:00",
    "2026-09-19T20:00:00-0400",
    "2026-09-20T00:00:00Z",
    "2026-09-20",
]


@pytest.mark.parametrize("since", _EQUIVALENT_SINCE_BOUNDS)
def test_session_helper_normalizes_equivalent_iso_bounds(session_store, since):
    before = session_store.read_bytes()
    sessions = dream_sessions.load_sessions(
        str(session_store), repository="owner/project", since=since)
    assert [session["session_id"] for session in sessions] == ["s57", "s58", "s59"]
    assert session_store.read_bytes() == before


@pytest.mark.parametrize("main", [dh.main, ds.main])
@pytest.mark.parametrize("since", _EQUIVALENT_SINCE_BOUNDS)
def test_cli_accepted_iso_bounds_retain_matching_session_evidence(main, session_store, tmp_path, since):
    out = tmp_path / "result.json"
    options = ["--palace", str(tmp_path), "--task" if main is dh.main else "--tasks",
               "reflect", "--source", "sessions", "--repository", "owner/project",
               "--since", since, "--out", str(out)]
    if main is ds.main:
        worklists = tmp_path / "worklists"
        options += ["--worklists-dir", str(worklists)]
        worklist_path = worklists / "reflect.sessions.json"
    else:
        worklist_path = out
    assert main(options) == 0
    worklist = json.loads(worklist_path.read_text())
    assert worklist["scope"]["since"] == since
    assert len(worklist["items"]) == 1
    assert worklist["items"][0]["evidence"]["support_ids"] == ["s57", "s58", "s59"]


@pytest.mark.parametrize("since", ["", "not-a-timestamp"])
def test_session_helper_rejects_invalid_since_instead_of_empty_or_unbounded_scan(session_store, since):
    with pytest.raises(ValueError):
        dream_sessions.load_sessions(str(session_store), since=since)


def test_explicit_preview_reads_only_requested_session_bodies(session_store, tmp_path, monkeypatch):
    session_ids = []
    load_turns = dream_sessions.load_session_turns

    def observe(session_id, db_path):
        session_ids.append(session_id)
        return load_turns(session_id, db_path)

    monkeypatch.setattr(dream_sessions, "load_session_turns", observe)
    rc, _ = run_harvest(tmp_path, "--repository", "owner/project", "--limit-sessions", "50")
    assert rc == 0
    assert session_ids == [f"s{i:02d}" for i in range(50)]


def test_successful_empty_scan_still_requires_valid_turn_schema(session_store, tmp_path):
    with sqlite3.connect(session_store) as con:
        con.execute("DROP TABLE turns")
    rc, worklist = run_harvest(tmp_path, "--repository", "owner/no-match")
    assert rc == 2
    assert worklist is None


def test_diary_union_does_not_inflate_session_recurrence(session_store, tmp_path, monkeypatch):
    monkeypatch.setattr(dream_palace, "load_observation_entries", lambda *args, **kwargs: [
        {"id": "diary-s00", "session_id": "s00", "text": "topic0 diary copy",
         "embedding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        {"id": "generated", "session_id": "fictional", "text": "topic0 generated",
         "embedding": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "metadata": {"kind": "lesson"}},
    ])
    rc, worklist = run_harvest(
        tmp_path, "--task", "reflect", "--source", "both",
        "--repository", "owner/project", "--limit-sessions", "3", "--min-support", "2")
    assert rc == 0
    assert worklist["items"][0]["evidence"]["support"] == 3
    assert worklist["items"][0]["evidence"]["support_ids"] == ["s00", "s01", "s02"]


def test_explicit_legacy_survey_reflect_preserves_wing_clusters(tmp_path, monkeypatch):
    import dream_reflect
    monkeypatch.setattr(dream_palace, "bind_palace", str)
    monkeypatch.setattr(dream_palace, "list_wings", lambda path: ["one", "two"])
    calls = []

    def gather(palace, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(dream_reflect, "gather_reflect_seeds", gather)
    report = ds.survey(str(tmp_path), tasks=["reflect"])
    assert report["wings"] == ["one", "two"]
    assert [call["wing"] for call in calls] == ["one", "two"]
    assert all(call["top_n"] == 10 for call in calls)


def test_source_diary_is_explicit_opt_in_and_records_steering(tmp_path, monkeypatch):
    monkeypatch.setattr(dream_palace, "bind_palace", str)
    monkeypatch.setattr(dream_palace, "load_observation_entries", lambda *args, **kwargs: [])
    rc, worklist = run_harvest(tmp_path, "--source", "diary", "--max-candidates", "5",
                               "--instructions", "Check validation")
    assert rc == 0
    assert worklist["scope"]["source"] == "diary"
    assert worklist["params"]["top_k"] == 5
    assert "Check validation" in worklist["instructions"]


def test_source_files_unchanged_after_session_first_scan(session_store, tmp_path):
    before = session_store.read_bytes()
    rc, _ = run_harvest(tmp_path, "--repository", "owner/project")
    assert rc == 0
    assert session_store.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["sessions.db", "worklist.json"]
