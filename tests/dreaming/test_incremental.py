"""No-loss incremental dreaming using real host SQLite and disposable drawer adapters."""
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import sys
import time

import pytest

import dream_adopt
import dream_harvest
import dream_palace
import dream_sessions
import dream_survey
from test_dream_procedural_palace import DrawerCollection


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    import dream_incremental
    from dream_store import DreamStore
    from mempalace.logstream import Logstream
    from test_dream_store import caller
    log = Logstream(str(tmp_path / "logstream.sqlite3"), replica_id="runtime-test")
    monkeypatch.setattr(dream_incremental, "_store",
                        lambda palace: DreamStore(palace, call_tool=caller(log)))
    store = tmp_path / "sessions.db"
    with sqlite3.connect(store) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT, repository TEXT, branch TEXT, summary TEXT,
                created_at TEXT, updated_at TEXT, cwd TEXT);
            CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                assistant_response TEXT, timestamp TEXT);
        """)
        for i in range(61):
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                        (f"s{i}", "owner/project", "main", "session", "2000-01-01",
                         "2000-01-01", "/project"))
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (f"s{i}", 0, f"original request {i}", "original response", "2000-01-01"))
    drawers = [
        {"id": "memory", "text": "An original memory without a session ID", "embedding": [1., 0.],
         "wing": "project", "room": "architecture",
         "metadata": {"wing": "project", "room": "architecture", "filed_at": "2000-01-01"}},
        {"id": "generated", "text": "A generated reflection", "embedding": [1., 0.],
         "wing": "project", "room": "reflections",
         "metadata": {"wing": "project", "room": "reflections", "kind": "reflect"}},
    ]
    monkeypatch.setitem(sys.modules, "dream_sessions", dream_sessions)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(store))
    monkeypatch.setattr(dream_palace, "bind_palace", lambda path: str(path))
    monkeypatch.setattr(dream_palace, "load_logical_drawers",
                        lambda path, wing=None, room=None: copy.deepcopy([
                            d for d in drawers if (wing is None or d["wing"] == wing)
                            and (room is None or d["room"] == room)]))
    monkeypatch.setattr(dream_adopt, "load_logical_drawers", dream_palace.load_logical_drawers)
    def collection(path, **kwargs):
        return DrawerCollection({
        d["id"]: {**copy.deepcopy(d), "metadata": {**d["metadata"], "wing": d["wing"], "room": d["room"]}}
        for d in drawers
        })
    monkeypatch.setattr(dream_palace, "procedural_collection", collection)
    import mempalace.palace
    monkeypatch.setattr(mempalace.palace, "get_collection", collection)
    monkeypatch.setattr(dream_palace, "_palace_embed",
                        lambda path, texts: [[0., 1.] for text in texts])
    monkeypatch.setattr(dream_adopt, "_palace_embed", dream_palace._palace_embed)
    yield store, drawers
    log.close()


def harvest(tmp_path, *options):
    out = tmp_path / "worklist.json"
    assert dream_harvest.main([
        "--palace", str(tmp_path), "--repository", "owner/project",
        "--wing", "project", "--out", str(out), *options]) == 0
    return json.loads(out.read_text())


def review(worklist):
    for source in worklist["coverage"]:
        source["review"] = {"action": "reviewed", "reason": "Read the original evidence; no further lesson warranted."}
    worklist["completion"] = {"action": "complete", "reason": "Reviewed every source and proposal."}
    return worklist


def adopt(tmp_path, worklist, *options):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(worklist))
    return dream_adopt.main(["--palace", str(tmp_path), "--decisions", str(path), *options])


def checkpoint(tmp_path):
    from dream_store import DreamStore
    store = DreamStore(str(tmp_path))
    events = store.events()
    scopes = {event["scope_id"]: store.checkpoint(event["scope_id"])
              for event in events if event["record_type"] == "completed"}
    return {"version": 3, "scopes": scopes} if scopes else None


def test_default_includes_all_older_unprocessed_sessions_and_original_memories(inputs, tmp_path):
    worklist = harvest(tmp_path)
    assert len(worklist["coverage"]) == 62
    assert {s["id"] for s in worklist["coverage"]} == {"memory"} | {f"session:s{i}" for i in range(61)}
    assert worklist["items"] == []
    assert worklist["incremental"]["lower"] is None
    assert checkpoint(tmp_path) is None


@pytest.mark.parametrize("option", [
    ["--limit-sessions", "50"], ["--max-candidates", "5"], ["--since", "2001-01-01"],
    ["--source", "sessions"], ["--rooms", "architecture"],
])
def test_partial_options_require_explicit_legacy_preview(inputs, tmp_path, option, capsys):
    with pytest.raises(SystemExit) as exc:
        harvest(tmp_path, *option)
    assert exc.value.code == 2
    assert "--task" in capsys.readouterr().err
    assert checkpoint(tmp_path) is None


def test_memory_wing_is_optional_and_not_inferred_from_repository(inputs, tmp_path):
    output = tmp_path / "all-wings.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--repository", "owner/project",
                               "--out", str(output)]) == 0
    assert json.loads(output.read_text())["incremental"]["scope"]["wings"] is None


@pytest.mark.parametrize("option", [["--tau", "0.9"], ["--min-support", "2"]])
def test_survey_does_not_silently_ignore_preview_candidate_options(inputs, tmp_path, option, capsys):
    with pytest.raises(SystemExit) as exc:
        dream_survey.main(["--palace", str(tmp_path), "--repository", "owner/project",
                          "--wings", "project", *option])
    assert exc.value.code == 2
    assert "--tasks" in capsys.readouterr().err
    assert checkpoint(tmp_path) is None


def test_complete_review_advances_and_second_run_is_empty(inputs, tmp_path):
    worklist = harvest(tmp_path)
    assert adopt(tmp_path, review(worklist)) == 0
    saved = checkpoint(tmp_path)
    assert len(saved["scopes"]) == 1
    assert next(iter(saved["scopes"].values()))["cutoff"] == worklist["incremental"]["upper"]
    again = harvest(tmp_path)
    assert again["coverage"] == []
    assert again["incremental"]["lower"] == worklist["incremental"]["upper"]


@pytest.mark.parametrize("change", ["missing-review", "removed-source", "missing-completion", "dry-run"])
def test_incomplete_or_dry_run_never_advances(inputs, tmp_path, change):
    worklist = review(harvest(tmp_path))
    if change == "missing-review":
        worklist["coverage"][0]["review"] = None
    elif change == "removed-source":
        worklist["coverage"].pop()
    elif change == "missing-completion":
        del worklist["completion"]
    rc = adopt(tmp_path, worklist, *(["--dry-run"] if change == "dry-run" else []))
    assert rc == (0 if change == "dry-run" else 1)
    assert checkpoint(tmp_path) is None


def test_stale_overlapping_run_cannot_advance_or_regress(inputs, tmp_path):
    older = review(harvest(tmp_path))
    newer = review(harvest(tmp_path))
    assert adopt(tmp_path, newer) == 0
    before = checkpoint(tmp_path)
    assert adopt(tmp_path, older) == 1
    assert checkpoint(tmp_path) == before


def test_full_session_review_contains_content_after_old_4000_char_cap(inputs, tmp_path):
    store, _ = inputs
    with sqlite3.connect(store) as con:
        con.execute("UPDATE turns SET user_message=? WHERE session_id='s0'",
                    ("a" * 5000 + " critical tail",))
    source = next(s for s in harvest(tmp_path)["coverage"] if s["id"] == "session:s0")
    assert source["turns"][0]["user_message"].endswith("critical tail")
    assert source["turns"][0]["assistant_response"] == "original response"


@pytest.mark.parametrize("broken", ["source-drift", "missing-store", "bad-session-date", "bad-memory-date"])
def test_invalid_source_prevents_successful_completion(inputs, tmp_path, broken):
    store, drawers = inputs
    worklist = review(harvest(tmp_path))
    if broken == "missing-store":
        store.unlink()
    elif broken == "bad-memory-date":
        drawers[0]["metadata"]["filed_at"] = ""
    else:
        with sqlite3.connect(store) as con:
            if broken == "source-drift":
                con.execute("UPDATE turns SET user_message='changed' WHERE session_id='s0'")
            else:
                con.execute("UPDATE sessions SET created_at='invalid' WHERE id='s0'")
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None


def test_survey_exports_the_same_uncapped_single_scope_manifest(inputs, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("incremental survey must not enumerate wings")
    monkeypatch.setattr(dream_palace, "list_wings", forbidden)
    out = tmp_path / "worklists"
    assert dream_survey.main([
        "--palace", str(tmp_path), "--repository", "owner/project", "--wings", "project",
        "--instructions", "Review deployment lessons", "--worklists-dir", str(out)]) == 0
    worklist = json.loads((out / "reflect.incremental.json").read_text())
    assert len(worklist["coverage"]) == 62
    assert "Review deployment lessons" in worklist["instructions"]
    assert checkpoint(tmp_path) is None


def test_frozen_boundary_and_continuing_turns_are_reviewed_next_run(inputs, tmp_path, monkeypatch):
    import dream_incremental
    store, drawers = inputs
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-09-20T00:00:00+00:00")
    first = review(harvest(tmp_path))
    with sqlite3.connect(store) as con:
        con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                    ("s0", 1, "new continuing request", "new response", "2026-09-20T02:00:00+0200"))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                    ("later", "owner/project", "main", "new", "20260920T000001", "", ""))
        con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                    ("later", 0, "later request", "later response", "2026-09-20T00:00:01Z"))
    drawers.append({"id": "later-memory", "text": "new original memory", "wing": "project",
                    "room": "decisions", "embedding": [1., 0.],
                    "metadata": {"filed_at": "2026-09-20T00:00:00Z"}})
    assert adopt(tmp_path, first) == 0
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-09-21T00:00:00+00:00")
    second = harvest(tmp_path)
    assert {s["id"] for s in second["coverage"]} == {"session:s0", "session:later", "later-memory"}
    session = next(s for s in second["coverage"] if s["id"] == "session:s0")
    assert len(session["turns"]) == 2
    assert adopt(tmp_path, review(second)) == 0


def test_scope_isolation_is_exact_for_repository_and_wing(inputs, tmp_path):
    store, drawers = inputs
    with sqlite3.connect(store) as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                    ("foreign", "owner/project-suffix", "main", "foreign", "2000-01-01", "", ""))
    drawers.append({"id": "foreign-memory", "text": "not in this project", "wing": "elsewhere",
                    "room": "general", "metadata": {"filed_at": "2000-01-01"}})
    worklist = review(harvest(tmp_path))
    assert len(worklist["coverage"]) == 62
    assert adopt(tmp_path, worklist) == 0
    output = tmp_path / "other.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--repository", "owner/project-suffix",
                              "--wing", "elsewhere", "--out", str(output)]) == 0
    other = json.loads(output.read_text())
    assert {s["id"] for s in other["coverage"]} == {"session:foreign", "foreign-memory"}
    assert other["incremental"]["lower"] is None
    assert adopt(tmp_path, review(other)) == 0
    assert len(checkpoint(tmp_path)["scopes"]) == 2


def proposal(identity="lesson-1", sources=None):
    return {"proposal_id": identity, "source_ids": sources or ["session:s0", "session:s1"],
            "decision": {"action": "surface", "conclusion": {
                "kind": "converge", "text": f"{identity}: use original evidence for review",
                "decision_or_prediction": "Review evidence before repeating a failed approach"},
                "premises": [], "wing": "project", "room": "lessons"}}


@pytest.fixture
def writer(inputs, monkeypatch):
    _, drawers = inputs

    class Writer:
        def __init__(self):
            self.calls = 0
            self.fail_on = None
            self.fail_after_on = None

        @contextmanager
        def mutation(self):
            yield

        def add_drawer(self, wing, room, content, metadata=None):
            self.calls += 1
            if self.calls == self.fail_on:
                raise RuntimeError("fixture write failure")
            drawer_id = f"saved-{self.calls}"
            drawers.append({"id": drawer_id, "text": content, "wing": wing, "room": room,
                            "metadata": {**metadata, "wing": wing, "room": room}, "embedding": [0., 1.]})
            if self.calls == self.fail_after_on:
                raise TimeoutError("fixture reply lost after durable write")
            return {"drawer_id": drawer_id}

    instance = Writer()
    monkeypatch.setattr(dream_palace, "MempalaceWriter", lambda: instance)
    monkeypatch.setattr(dream_palace, "MempalaceTunneler",
                        lambda: pytest.fail("incremental lesson adoption must not create tunnels"))
    return instance


def test_reflection_adoption_then_checkpoint_is_retry_safe(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal()]
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 1
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 1
    assert next(iter(checkpoint(tmp_path)["scopes"].values()))["run_id"] == worklist["incremental"]["run_id"]


def test_partial_write_failure_does_not_advance_and_retry_does_not_duplicate(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal(), proposal("lesson-2", ["session:s2", "session:s3"])]
    writer.fail_after_on = 2
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None
    assert writer.calls == 2
    writer.fail_after_on = None
    # Use a different novel vector for the still-unwritten second lesson.
    dream_adopt._palace_embed = lambda path, texts: [[-1., 0.] for _ in texts]
    try:
        assert adopt(tmp_path, worklist) == 0
    finally:
        dream_adopt._palace_embed = dream_palace._palace_embed
    assert writer.calls == 2
    assert len([d for d in inputs[1] if d["id"].startswith("saved-")]) == 2


@pytest.mark.parametrize("malformed", ["missing-decision", "forged-source", "single-session", "over-budget", "connect"])
def test_invalid_proposals_never_write_or_advance(inputs, tmp_path, writer, malformed):
    worklist = review(harvest(tmp_path))
    item = proposal()
    worklist["items"] = [item]
    if malformed == "missing-decision":
        del item["decision"]
    elif malformed == "forged-source":
        item["source_ids"] = ["session:forged", "session:s1"]
    elif malformed == "single-session":
        item["source_ids"] = ["session:s0"]
    elif malformed == "over-budget":
        worklist["items"] = [proposal(str(i)) for i in range(6)]
    else:
        item["decision"]["conclusion"]["kind"] = "connect"
    assert adopt(tmp_path, worklist) == 1
    assert writer.calls == 0
    assert checkpoint(tmp_path) is None


def test_empty_window_requires_explicit_completion_but_can_advance(inputs, tmp_path):
    store, drawers = inputs
    with sqlite3.connect(store) as con:
        con.execute("DELETE FROM turns")
        con.execute("DELETE FROM sessions")
    drawers.clear()
    worklist = harvest(tmp_path)
    assert worklist["coverage"] == []
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None
    assert adopt(tmp_path, review(worklist)) == 0


@pytest.mark.parametrize("state", ["missing", "corrupt", "schema"])
def test_broken_source_is_not_successful_empty_harvest(inputs, tmp_path, state):
    store, _ = inputs
    store.unlink()
    if state == "corrupt":
        store.write_text("not a database")
    elif state == "schema":
        with sqlite3.connect(store) as con:
            con.execute("CREATE TABLE unrelated(value TEXT)")
    out = tmp_path / "missing.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--repository", "owner/project",
                              "--wing", "project", "--out", str(out)]) == 2
    assert not out.exists()
    assert checkpoint(tmp_path) is None


def test_corrupt_source_in_survey_is_reported_not_empty(inputs, tmp_path, capsys):
    store, _ = inputs
    store.write_bytes(b"invalid sqlite")
    assert dream_survey.main(["--palace", str(tmp_path), "--repository", "owner/project",
                             "--wings", "project"]) == 2
    assert "error" in capsys.readouterr().err
    assert checkpoint(tmp_path) is None


@pytest.mark.parametrize("kind", ["lesson", "reflect", "procedural_event", "control", "task_event"])
def test_generated_and_control_records_are_not_independent_input(inputs, tmp_path, kind):
    _, drawers = inputs
    drawers.append({"id": "excluded", "text": "generated/control", "wing": "project",
                    "room": "general", "metadata": {"kind": kind}})
    assert "excluded" not in {s["id"] for s in harvest(tmp_path)["coverage"]}


def test_control_source_kind_is_excluded_without_fake_timestamp(inputs, tmp_path):
    inputs[1].append({"id": "control-source", "text": "run status", "wing": "project",
                      "room": "general", "metadata": {"source_kind": "control"}})
    assert "control-source" not in {s["id"] for s in harvest(tmp_path)["coverage"]}


def test_new_memory_without_sessions_remains_reviewable_and_quote_grounded(inputs, tmp_path, writer):
    store, drawers = inputs
    with sqlite3.connect(store) as con:
        con.execute("DELETE FROM turns")
        con.execute("DELETE FROM sessions")
    drawers.append({"id": "second", "text": "A second original constraint", "wing": "project",
                    "room": "design", "metadata": {"filed_at": "2000-01-02"}, "embedding": [1., 0.]})
    worklist = review(harvest(tmp_path))
    assert {s["id"] for s in worklist["coverage"]} == {"memory", "second"}
    item = proposal(sources=["memory", "second"])
    item["decision"]["conclusion"]["kind"] = "generalize"
    item["decision"]["premises"] = [
        {"drawer_id": "memory", "quote": "original memory"},
        {"drawer_id": "second", "quote": "original constraint"},
    ]
    worklist["items"] = [item]
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 1


def test_single_sparse_memory_requires_review_not_fabricated_recurrence(inputs, tmp_path, writer):
    store, _ = inputs
    with sqlite3.connect(store) as con:
        con.execute("DELETE FROM turns")
        con.execute("DELETE FROM sessions")
    worklist = harvest(tmp_path)
    assert [s["id"] for s in worklist["coverage"]] == ["memory"]
    assert adopt(tmp_path, worklist) == 1
    worklist = review(worklist)
    worklist["items"] = [proposal(sources=["memory"])]
    assert adopt(tmp_path, worklist) == 1
    assert writer.calls == 0
    worklist["items"] = []
    assert adopt(tmp_path, worklist) == 0


def test_long_raw_session_can_ground_a_proposal_without_clipped_hash(inputs, tmp_path, writer):
    with sqlite3.connect(inputs[0]) as con:
        con.execute("UPDATE turns SET user_message=? WHERE session_id='s0'", ("x" * 5001 + " tail",))
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal()]
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 1


def test_dry_run_with_proposal_does_not_write_or_advance(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal()]
    assert adopt(tmp_path, worklist, "--dry-run") == 0
    assert writer.calls == 0
    assert checkpoint(tmp_path) is None


def test_checkpoint_write_failure_retries_using_existing_lesson_receipt(inputs, tmp_path, writer, monkeypatch):
    import dream_incremental
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal()]
    publish = dream_incremental._publish

    def fail(store, kind, *args, **kwargs):
        if kind == "completed":
            raise OSError("fixture completion publication failure")
        return publish(store, kind, *args, **kwargs)

    monkeypatch.setattr(dream_incremental, "_publish", fail)
    assert adopt(tmp_path, worklist) == 1
    assert writer.calls == 1
    assert checkpoint(tmp_path) is None
    assert not list(tmp_path.glob(".dream-checkpoints-*"))
    monkeypatch.setattr(dream_incremental, "_publish", publish)
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 1


@pytest.mark.parametrize("field,value", [
    ("supported_by", []), ("premises", [{"drawer_id": "forged", "quote": "invented"}]),
    ("decision_or_prediction", "changed"),
])
def test_retry_rejects_generated_lesson_provenance_drift(inputs, tmp_path, writer, monkeypatch, field, value):
    import dream_incremental
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal()]
    publish = dream_incremental._publish

    def fail(store, kind, *args, **kwargs):
        if kind == "completed":
            raise OSError("fixture completion publication failure")
        return publish(store, kind, *args, **kwargs)

    monkeypatch.setattr(dream_incremental, "_publish", fail)
    assert adopt(tmp_path, worklist) == 1
    saved = next(d for d in inputs[1] if d["id"].startswith("saved-"))
    saved["metadata"][field] = value
    monkeypatch.setattr(dream_incremental, "_publish", publish)
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None
    assert writer.calls == 1


def test_removed_proposal_after_partial_adoption_cannot_certify_completion(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal(), proposal("lesson-2", ["session:s2", "session:s3"])]
    writer.fail_on = 2
    assert adopt(tmp_path, worklist) == 1
    worklist["items"] = []
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None


def test_source_drift_after_uncertain_write_allows_inspection_but_holds_competing_completion(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [proposal(), proposal("lesson-2", ["session:s2", "session:s3"])]
    writer.fail_on = 2
    assert adopt(tmp_path, worklist) == 1
    with sqlite3.connect(inputs[0]) as con:
        con.execute("UPDATE turns SET user_message='corrected original evidence' WHERE session_id='s0'")
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None
    fresh = harvest(tmp_path)
    assert len(fresh["coverage"]) == 62
    assert next(s for s in fresh["coverage"] if s["id"] == "session:s0")["text"] == "corrected original evidence"
    assert adopt(tmp_path, review(fresh)) == 1
    assert writer.calls == 2


def test_lesson_budget_does_not_limit_reviewed_abstentions(inputs, tmp_path, writer):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [
        {"proposal_id": f"considered-{i}",
         "decision": {"action": "skip", "reason": "Evidence reviewed; no new actionable lesson."}}
        for i in range(12)
    ]
    assert adopt(tmp_path, worklist) == 0
    assert writer.calls == 0


def test_abstention_completion_needs_no_memory_writer(inputs, tmp_path, monkeypatch):
    worklist = review(harvest(tmp_path))
    worklist["items"] = [{"proposal_id": "considered", "decision": {
        "action": "skip", "reason": "Already captured; no new lesson."}}]
    monkeypatch.setattr(dream_palace, "MempalaceWriter",
                        lambda: pytest.fail("abstention must not acquire a memory writer"))
    assert adopt(tmp_path, worklist) == 0


def test_forged_lower_bound_cannot_skip_unreviewed_history(inputs, tmp_path):
    import dream_incremental
    worklist = review(harvest(tmp_path))
    run = worklist["incremental"]
    run["lower"] = run["upper"]
    worklist["coverage"] = []
    run["source_hash"] = dream_incremental._digest([])
    run["run_id"] = dream_incremental._digest({k: v for k, v in run.items() if k != "run_id"})
    assert adopt(tmp_path, worklist) == 1
    assert checkpoint(tmp_path) is None


def test_explicit_legacy_preview_never_creates_checkpoint(inputs, tmp_path):
    output = tmp_path / "preview.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", "reflect", "--source", "sessions",
                              "--repository", "owner/project", "--limit-sessions", "2",
                              "--max-candidates", "1", "--out", str(output)]) == 0
    worklist = json.loads(output.read_text())
    assert "incremental" not in worklist
    assert checkpoint(tmp_path) is None


def test_missing_memory_store_cannot_be_initialized_as_empty_coverage(inputs, tmp_path, monkeypatch):
    import mempalace.palace
    def missing(path, **kwargs):
        raise FileNotFoundError("memory store does not exist")
    monkeypatch.setattr(dream_palace, "procedural_collection", missing)
    monkeypatch.setattr(mempalace.palace, "get_collection", missing)
    assert dream_harvest.main(["--palace", str(tmp_path), "--repository", "owner/project",
                              "--wing", "project", "--out", str(tmp_path / "failed.json")]) == 2
    assert checkpoint(tmp_path) is None


def test_default_memory_read_does_not_require_optional_procedural_backend(inputs, tmp_path, monkeypatch):
    def unsupported(path):
        raise RuntimeError("optional procedural storage rejects this otherwise supported backend")
    monkeypatch.setattr(dream_palace, "procedural_collection", unsupported)
    worklist = harvest(tmp_path)
    assert len(worklist["coverage"]) == 62
    assert next(s for s in worklist["coverage"] if s["id"] == "memory")["text"] == inputs[1][0]["text"]


def test_incremental_reads_existing_chroma_without_procedural_opt_in(tmp_path, monkeypatch):
    from mempalace.palace import get_backend_for_palace, get_collection
    palace = tmp_path / "chroma-palace"
    palace.mkdir()
    store = tmp_path / "host.sqlite3"
    with sqlite3.connect(store) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT, repository TEXT, created_at TEXT);
            CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                               assistant_response TEXT, timestamp TEXT);
        """)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(store))
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "chroma")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
    backend = get_backend_for_palace(str(palace))
    monkeypatch.setattr(backend, "_resolve_embedding_function", lambda: None)
    try:
        collection = get_collection(str(palace), create=True)
        collection.add(ids=["memory"], documents=["Original Chroma memory"],
                       metadatas=[{"wing": "project", "room": "architecture", "filed_at": "2000-01-01"}],
                       embeddings=[[1., 0.]])
        from dream_store import DreamStore
        DreamStore.initialize(str(palace))
        worklist = harvest(palace)
        assert [source["id"] for source in worklist["coverage"]] == ["memory"]
        assert worklist["coverage"][0]["text"] == "Original Chroma memory"
        assert adopt(palace, review(worklist)) == 0
        assert checkpoint(palace) is not None
    finally:
        backend.close_palace(str(palace))


def test_legacy_checkpoint_is_diagnostic_not_new_scope_authority(inputs, tmp_path):
    path = tmp_path / "dream-checkpoints.json"
    path.write_text('{"version":1,"scopes":{"old":{"cutoff":"2099-01-01"}}}')
    before = path.read_bytes()
    worklist = harvest(tmp_path)
    assert len(worklist["coverage"]) == 62
    assert worklist["incremental"]["lower"] is None
    assert adopt(tmp_path, review(worklist)) == 0
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".dream-checkpoints-*"))


@pytest.fixture
def new_york_timezone(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("native local-time producer regression requires tzset")
    try:
        with monkeypatch.context() as context:
            context.setenv("TZ", "America/New_York")
            time.tzset()
            yield
    finally:
        time.tzset()


@pytest.mark.parametrize("filed_at,expected", [
    ("2026-09-29T14:47:00", "2026-09-29T18:47:00+00:00"),
    ("2026-01-15T14:47:00", "2026-01-15T19:47:00+00:00"),
    ("2026-09-29T14:47:00-04:00", "2026-09-29T18:47:00+00:00"),
    ("2026-09-29T18:47:00Z", "2026-09-29T18:47:00+00:00"),
    ("2026-11-01T01:30:00-04:00", "2026-11-01T05:30:00+00:00"),
    ("2026-11-01T01:30:00-05:00", "2026-11-01T06:30:00+00:00"),
])
def test_memory_producer_local_time_is_not_host_session_utc(
        inputs, tmp_path, new_york_timezone, filed_at, expected, monkeypatch):
    import dream_incremental
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-12-01T00:00:00+00:00")
    inputs[1][0]["metadata"]["filed_at"] = filed_at
    source = next(s for s in harvest(tmp_path)["coverage"] if s["id"] == "memory")
    assert source["created_at"] == expected
    assert dream_incremental.timestamp("2026-09-29T14:47:00", "host turn").isoformat() == "2026-09-29T14:47:00+00:00"


def test_local_clock_rule_is_specific_to_native_filed_at(inputs, tmp_path, new_york_timezone, monkeypatch):
    import dream_incremental
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-10-01T00:00:00+00:00")
    metadata = inputs[1][0]["metadata"]
    del metadata["filed_at"]
    metadata["created_at"] = "2026-09-29T14:47:00"
    source = next(s for s in harvest(tmp_path)["coverage"] if s["id"] == "memory")
    assert source["created_at"] == "2026-09-29T14:47:00+00:00"


def test_fall_back_repeated_local_time_does_not_hide_new_source_version(
        inputs, tmp_path, new_york_timezone, monkeypatch):
    import dream_incremental
    inputs[1][0]["metadata"]["filed_at"] = "2026-11-01T01:30:00"
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-11-01T05:45:00+00:00")
    assert adopt(tmp_path, review(harvest(tmp_path))) == 0
    inputs[1].append({"id": "fall-back-memory", "text": "Another original memory after the clock repeated",
                      "wing": "project", "room": "architecture",
                      "metadata": {"filed_at": "2026-11-01T01:30:00"}, "embedding": [1., 0.]})
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: "2026-11-01T06:45:00+00:00")
    assert [s["id"] for s in harvest(tmp_path)["coverage"]] == ["fall-back-memory"]


def test_native_writer_new_memory_after_dream_is_included_in_new_york(
        tmp_path, monkeypatch, new_york_timezone):
    from test_dream_procedural_palace import installed_palace
    import dream_incremental
    palace = tmp_path / "palace"
    palace.mkdir()
    store = tmp_path / "host.sqlite3"
    with sqlite3.connect(store) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT, repository TEXT, created_at TEXT);
            CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                               assistant_response TEXT, timestamp TEXT);
        """)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(store))
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        writer = dream_palace.MempalaceWriter()
        writer.add_drawer("project", "architecture", "Initial original memory", added_by="user")
        from dream_store import DreamStore
        DreamStore.initialize(str(palace))
        first = review(harvest(palace))
        assert adopt(palace, first) == 0
        writer.add_drawer("project", "architecture", "New original memory after dreaming", added_by="user")
        second = harvest(palace)
        assert len(second["coverage"]) == 1
        source = second["coverage"][0]
        assert source["text"] == "New original memory after dreaming"
        assert dream_incremental.timestamp(source["created_at"], "memory") > dream_incremental.timestamp(
            first["incremental"]["upper"], "completed cutoff")
        assert adopt(palace, review(second)) == 0
        assert harvest(palace)["coverage"] == []


@pytest.mark.parametrize("arrival", ["continuing-turn", "session", "memory"])
def test_source_persisted_after_final_validation_is_recovered_despite_older_timestamp(
        inputs, tmp_path, monkeypatch, arrival):
    import dream_incremental
    store, drawers = inputs
    clock = {"now": datetime(2026, 9, 29, 18, 41, 27, 614616, tzinfo=timezone.utc)}
    monkeypatch.setattr(dream_incremental, "utc_now", lambda: clock["now"].isoformat())
    first = review(harvest(tmp_path))
    publish = dream_incremental._publish
    old_event = "2026-09-29T18:41:27.612Z"

    def insert_after_validation(native_store, kind, *args, **kwargs):
        if kind != "completed":
            return publish(native_store, kind, *args, **kwargs)
        if arrival == "memory":
            drawers.append({"id": "late-memory", "text": "Late visible original memory",
                            "wing": "project", "room": "architecture",
                            "metadata": {"filed_at": "2000-01-01"}, "embedding": [1., 0.]})
        else:
            with sqlite3.connect(store) as con:
                identity = "s0" if arrival == "continuing-turn" else "late-session"
                if arrival == "session":
                    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                                (identity, "owner/project", "main", "late session", old_event, old_event, "/project"))
                con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                            (identity, 1, "Late persisted original request", "Late response", old_event))
        return publish(native_store, kind, *args, **kwargs)

    monkeypatch.setattr(dream_incremental, "_publish", insert_after_validation)
    assert adopt(tmp_path, first) == 0
    monkeypatch.setattr(dream_incremental, "_publish", publish)
    clock["now"] += timedelta(seconds=1)
    second = harvest(tmp_path)
    expected = {"continuing-turn": "session:s0", "session": "session:late-session", "memory": "late-memory"}[arrival]
    assert [s["id"] for s in second["coverage"]] == [expected]
    assert adopt(tmp_path, review(second)) == 0
    count = 62 if arrival == "continuing-turn" else 63
    assert len(next(iter(checkpoint(tmp_path)["scopes"].values()))["reviewed_versions"]) == count
    for _ in range(2):
        clock["now"] += timedelta(seconds=1)
        unchanged = harvest(tmp_path)
        assert unchanged["coverage"] == []
        assert adopt(tmp_path, review(unchanged)) == 0
        assert len(next(iter(checkpoint(tmp_path)["scopes"].values()))["reviewed_versions"]) == count


@pytest.mark.parametrize("edited", ["memory", "memory-metadata", "user-turn", "assistant-turn"])
def test_historical_source_edit_without_new_timestamp_requires_review(inputs, tmp_path, edited):
    store, drawers = inputs
    assert adopt(tmp_path, review(harvest(tmp_path))) == 0
    if edited == "memory":
        drawers[0]["text"] = "Corrected historical memory"
        expected = "memory"
    elif edited == "memory-metadata":
        drawers[0]["metadata"]["source_file"] = "corrected-original-source"
        expected = "memory"
    else:
        column = "user_message" if edited == "user-turn" else "assistant_response"
        with sqlite3.connect(store) as con:
            con.execute(f"UPDATE turns SET {column}='corrected original turn' WHERE session_id='s0'")
        expected = "session:s0"
    changed = harvest(tmp_path)
    assert [source["id"] for source in changed["coverage"]] == [expected]
    before = checkpoint(tmp_path)
    assert adopt(tmp_path, review(changed), "--dry-run") == 0
    assert checkpoint(tmp_path) == before
    assert [s["id"] for s in harvest(tmp_path)["coverage"]] == [expected]
    assert adopt(tmp_path, changed) == 0
    assert harvest(tmp_path)["coverage"] == []


def test_v1_checkpoint_requires_full_reconciliation_without_writing_during_harvest(inputs, tmp_path):
    old = {"version": 1, "scopes": {"old": {"cutoff": "2026-01-01"}}}
    path = tmp_path / "dream-checkpoints.json"
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    reconciled = harvest(tmp_path)
    assert len(reconciled["coverage"]) == 62
    assert path.read_bytes() == before
    assert reconciled["incremental"]["version"] == 3
    assert adopt(tmp_path, review(reconciled)) == 0
    assert checkpoint(tmp_path)["version"] == 3
    assert path.read_bytes() == before
    assert len(next(iter(checkpoint(tmp_path)["scopes"].values()))["reviewed_versions"]) == 62
    assert harvest(tmp_path)["coverage"] == []


def test_old_pending_worklist_must_be_reharvested(inputs, tmp_path, capsys):
    import dream_incremental
    old = review(harvest(tmp_path))
    old["incremental"]["version"] = 1
    old["incremental"]["run_id"] = dream_incremental._digest(
        {k: v for k, v in old["incremental"].items() if k != "run_id"})
    assert adopt(tmp_path, old) == 1
    assert "re-harvest" in capsys.readouterr().err
    assert checkpoint(tmp_path) is None


@pytest.mark.parametrize("invalid", [None, [], {"session:s0": "not-a-digest"}])
def test_malformed_native_reviewed_versions_is_not_empty_history(inputs, tmp_path, invalid, monkeypatch):
    import dream_incremental
    assert adopt(tmp_path, review(harvest(tmp_path))) == 0
    broken = checkpoint(tmp_path)
    scope = next(iter(broken["scopes"].values()))
    scope["reviewed_versions"] = invalid
    native = dream_incremental._store(str(tmp_path))
    monkeypatch.setattr(native, "checkpoint", lambda scope_id: scope)
    monkeypatch.setattr(dream_incremental, "_store", lambda palace: native)
    assert dream_harvest.main(["--palace", str(tmp_path), "--repository", "owner/project",
                              "--wing", "project", "--out", str(tmp_path / "invalid.json")]) == 2


def test_installed_sqlite_palace_preserves_full_memory_and_retries_generated_receipts(tmp_path, monkeypatch):
    from test_dream_procedural_palace import installed_palace
    palace = tmp_path / "palace"
    palace.mkdir()
    store = tmp_path / "host.sqlite3"
    with sqlite3.connect(store) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT, repository TEXT, created_at TEXT);
            CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                               assistant_response TEXT, timestamp TEXT);
        """)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(store))
    text = "An original architecture decision, without a host session ID. " * 100
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        dream_palace.MempalaceWriter().add_drawer("project", "architecture", text, added_by="user")
        from dream_store import DreamStore
        DreamStore.initialize(str(palace))
        before = (palace / "sqlite_exact.sqlite3").read_bytes()
        worklist = harvest(palace)
        assert len(worklist["coverage"]) == 1
        assert worklist["coverage"][0]["text"] == text
        assert (palace / "sqlite_exact.sqlite3").read_bytes() == before
        assert adopt(palace, review(worklist)) == 0
        assert checkpoint(palace) is not None
        assert (palace / "sqlite_exact.sqlite3").read_bytes() == before
        created = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(store) as con:
            for identity in ("real-a", "real-b"):
                con.execute("INSERT INTO sessions VALUES (?,?,?)", (identity, "owner/project", created))
                con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                            (identity, 0, "Review original sources before trusting summaries", "ack", created))
        monkeypatch.setattr(dream_adopt, "_palace_embed",
                            lambda path, texts: [[1.] + [0.] * 383 for _ in texts])
        second = review(harvest(palace))
        assert len(second["coverage"]) == 2
        second["items"] = [proposal(sources=["session:real-a", "session:real-b"])]
        assert adopt(palace, second) == 0
        assert adopt(palace, second) == 0
        from dream_incremental import _drawers
        receipts = [d for d in _drawers(str(palace), "project")
                    if "incremental_run_id" in d["text"]]
        assert len(receipts) == 1
        assert harvest(palace)["coverage"] == []
