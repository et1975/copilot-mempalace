"""Whole-corpus scope and palace-owned incremental lifecycle regressions."""
import copy
import json
import sqlite3

import pytest

import dream_incremental as runtime
from test_incremental import inputs, review, writer, proposal


def test_unfiltered_sources_include_other_repositories_repositoryless_and_wings(inputs, tmp_path):
    session_store, drawers = inputs
    with sqlite3.connect(session_store) as con:
        for identity, repository in (("other", "owner/other"), ("unscoped", None)):
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                        (identity, repository, "main", "", "2000-01-01", "", ""))
    drawers.append({"id": "other-memory", "text": "Original from another wing", "wing": "other",
                    "room": "design", "metadata": {"filed_at": "2000-01-01"}})
    scope = {"scope_schema": 1, "repository": None, "wings": None,
             "session_store": str(session_store)}
    sources = runtime.collect_sources(str(tmp_path), scope, None, runtime.utc_now())
    assert {"session:other", "session:unscoped", "other-memory"} <= {s["id"] for s in sources}
    assert next(s for s in sources if s["id"] == "other-memory")["wing"] == "other"


def test_repository_and_wing_filters_are_independent(inputs, tmp_path):
    session_store, drawers = inputs
    drawers.append({"id": "other-memory", "text": "Original other wing", "wing": "other",
                    "room": "design", "metadata": {"filed_at": "2000-01-01"}})
    scope = {"scope_schema": 1, "repository": "owner/project", "wings": None,
             "session_store": str(session_store)}
    sources = runtime.collect_sources(str(tmp_path), scope, None, runtime.utc_now())
    assert "other-memory" in {s["id"] for s in sources}
    scope.update(repository=None, wings=["other"])
    sources = runtime.collect_sources(str(tmp_path), scope, None, runtime.utc_now())
    assert {s["id"] for s in sources if s["source_kind"] == "memory"} == {"other-memory"}
    assert len([s for s in sources if s["source_kind"] == "session"]) == 61


def test_session_diary_mirror_cannot_double_count_original_evidence(inputs, tmp_path):
    session_store, drawers = inputs
    drawers.append({"id": "mirror", "text": "SESSION_ID: s0\nA summary of the original conversation.",
                    "wing": "journal", "room": "diary",
                    "metadata": {"filed_at": "2000-01-01", "room": "diary"}})
    scope = {"scope_schema": 1, "repository": None, "wings": None,
             "session_store": str(session_store)}
    sources = runtime.collect_sources(str(tmp_path), scope, None, runtime.utc_now())
    assert "mirror" not in {s["id"] for s in sources}


def test_scope_identity_is_normalized_and_path_free():
    assert runtime.normalize_scope(None, wings=["B", "A", "B"]) == {
        "scope_schema": 1, "repository": None, "wings": ["A", "B"]}
    assert runtime.normalize_scope("owner/repo", "A") == runtime.normalize_scope(
        "owner/repo", wings=["A"])
    for args in ({"repository": ""}, {"wing": " "}, {"wings": []}, {"wings": ["A", ""]},
                 {"wing": "A", "wings": ["A"]}):
        with pytest.raises(ValueError):
            runtime.normalize_scope(**args)


def test_cross_wing_sources_can_target_an_independent_lesson_destination():
    coverage = [
        {"id": "one", "source_kind": "memory", "wing": "A", "text": "first source",
         "content_hash": runtime.content_hash("first source")},
        {"id": "two", "source_kind": "memory", "wing": "B", "text": "second source",
         "content_hash": runtime.content_hash("second source")},
    ]
    item = proposal(sources=["one", "two"])
    item["decision"].update(wing="destination", room="lessons", premises=[
        {"drawer_id": "one", "quote": "first source"},
        {"drawer_id": "two", "quote": "second source"}])
    item["decision"]["conclusion"]["kind"] = "generalize"
    worklist = {"scope": {"wings": None}, "incremental": {"run_id": "run"}, "items": [item]}
    assert runtime._proposals(worklist, coverage)[0]["wing"] == "destination"
    item["decision"]["room"] = "diary"
    with pytest.raises(ValueError, match="lessons"):
        runtime._proposals(worklist, coverage)


def test_native_manifest_and_saved_review_recover_without_external_worklists(inputs, tmp_path):
    worklist = runtime.harvest(str(tmp_path))
    run_id = worklist["incremental"]["run_id"]
    assert runtime.load_run(str(tmp_path), run_id) == worklist
    review(worklist)
    runtime.save_review(str(tmp_path), run_id, worklist)
    assert runtime.load_run(str(tmp_path), run_id) == worklist
    assert runtime.complete(str(tmp_path), worklist)["completed"]
    inputs[0].unlink()
    assert runtime.load_run(str(tmp_path), run_id) == worklist
    assert runtime.complete(str(tmp_path), worklist)["replayed"]
    assert not (tmp_path / "dream-checkpoints.json").exists()


def test_missing_originals_blocks_pending_adoption_but_not_saved_review(inputs, tmp_path):
    worklist = review(runtime.harvest(str(tmp_path)))
    run_id = worklist["incremental"]["run_id"]
    inputs[0].unlink()
    runtime.save_review(str(tmp_path), run_id, worklist)
    with pytest.raises((FileNotFoundError, ValueError), match="missing|source"):
        runtime.complete(str(tmp_path), runtime.load_run(str(tmp_path), run_id))


def test_global_and_filtered_checkpoints_never_inherit(inputs, tmp_path):
    filtered = review(runtime.harvest(str(tmp_path), "owner/project", "project"))
    runtime.complete(str(tmp_path), filtered)
    global_run = runtime.harvest(str(tmp_path))
    assert global_run["incremental"]["lower"] is None
    assert len(global_run["coverage"]) == 62
    runtime.complete(str(tmp_path), review(global_run))
    inputs[1].append({"id": "new-wing", "text": "Original backdated memory", "wing": "new",
                      "room": "design", "metadata": {"filed_at": "2000-01-01"}})
    again = runtime.harvest(str(tmp_path))
    assert [source["id"] for source in again["coverage"]] == ["new-wing"]
    assert again["incremental"]["scope"] == global_run["incremental"]["scope"]
    assert "new" in again["incremental"]["inventory"]["wings"]
    assert runtime.harvest(str(tmp_path), "owner/project", "project")["coverage"] == []


def test_partial_review_saved_but_not_adopted_and_manifest_edit_rejected(inputs, tmp_path):
    worklist = runtime.harvest(str(tmp_path))
    identity = worklist["incremental"]["run_id"]
    worklist["coverage"][0]["review"] = {"action": "reviewed", "reason": "Read this source only"}
    result = runtime.save_review(str(tmp_path), identity, worklist)
    assert result["review_hash"] == runtime._digest(worklist)
    with pytest.raises(ValueError, match="every source"):
        runtime.complete(str(tmp_path), worklist)
    changed = copy.deepcopy(worklist)
    changed["coverage"].pop()
    with pytest.raises(ValueError, match="coverage|manifest"):
        runtime.save_review(str(tmp_path), identity, changed)
    with pytest.raises(ValueError, match="review"):
        runtime.save_review(str(tmp_path), identity, review(worklist), expected_review_hash="wrong")


def test_uncertain_proposal_pins_scope_review_and_blocks_resubmission(inputs, tmp_path, writer):
    worklist = review(runtime.harvest(str(tmp_path)))
    competitor = review(runtime.harvest(str(tmp_path)))
    worklist["items"] = [proposal()]
    writer.fail_on = 1
    with pytest.raises(RuntimeError, match="adoption"):
        runtime.complete(str(tmp_path), worklist)
    writer.fail_on = None
    with pytest.raises(ValueError, match="unsettled|unresolved"):
        runtime.complete(str(tmp_path), worklist)
    assert writer.calls == 1
    with pytest.raises(ValueError, match="intent|adoption"):
        runtime.complete(str(tmp_path), competitor)
    changed = copy.deepcopy(worklist)
    changed["items"] = []
    with pytest.raises(ValueError, match="pinned|intent|adoption"):
        runtime.save_review(str(tmp_path), worklist["incremental"]["run_id"], changed)


def test_rebinding_copied_session_store_preserves_scope_but_different_corpus_fails(inputs, tmp_path, monkeypatch):
    import shutil
    worklist = review(runtime.harvest(str(tmp_path)))
    moved = tmp_path / "moved-sessions.db"
    shutil.copyfile(inputs[0], moved)
    inputs[0].unlink()
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(moved))
    assert runtime.complete(str(tmp_path), worklist)["completed"]
    assert runtime.harvest(str(tmp_path))["coverage"] == []
    alien = tmp_path / "alien.db"
    shutil.copyfile(moved, alien)
    with sqlite3.connect(alien) as con:
        con.execute("DELETE FROM turns")
        con.execute("DELETE FROM sessions")
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(alien))
    with pytest.raises(ValueError, match="corpus"):
        runtime.harvest(str(tmp_path))


def test_existing_accepted_lesson_in_other_wing_still_blocks_novelty(inputs, tmp_path, writer):
    inputs[1].append({"id": "existing-lesson", "text": "lesson-1: use original evidence for review",
                      "wing": "elsewhere", "room": "lessons", "embedding": [0., 1.],
                      "metadata": {"kind": "lesson", "wing": "elsewhere", "room": "lessons"}})
    worklist = review(runtime.harvest(str(tmp_path), wings=["project"]))
    assert "existing-lesson" not in {s["id"] for s in worklist["coverage"]}
    worklist["items"] = [proposal()]
    with pytest.raises(ValueError, match="not_novel"):
        runtime.complete(str(tmp_path), worklist)
    assert writer.calls == 0


def test_internal_control_drawer_is_not_novelty_evidence(inputs, tmp_path, writer):
    inputs[1].append({"id": "control-drawer", "text": "internal run intent", "wing": "dreaming",
                      "room": "control", "embedding": [0., 1.],
                      "metadata": {"kind": "dream_control", "room": "control"}})
    worklist = review(runtime.harvest(str(tmp_path)))
    worklist["items"] = [proposal()]
    assert runtime.complete(str(tmp_path), worklist)["surfaced"] == 1


def test_cross_destination_partial_reply_loss_recovers_without_duplicate_writes(inputs, tmp_path, writer):
    worklist = review(runtime.harvest(str(tmp_path)))
    worklist["items"] = [proposal("one"), proposal("two")]
    worklist["items"][0]["decision"]["wing"] = "destination-a"
    worklist["items"][1]["decision"]["wing"] = "destination-b"
    writer.fail_after_on = 2
    with pytest.raises(RuntimeError, match="adoption"):
        runtime.complete(str(tmp_path), worklist)
    assert writer.calls == 2
    writer.fail_after_on = None
    assert runtime.complete(str(tmp_path), worklist)["completed"]
    assert writer.calls == 2
    lessons = [d for d in inputs[1] if d["id"].startswith("saved-")]
    assert {d["wing"] for d in lessons} == {"destination-a", "destination-b"}


def test_lost_completion_reply_reconciles_one_logical_completion(inputs, tmp_path, monkeypatch):
    from dream_store import DreamStore
    worklist = review(runtime.harvest(str(tmp_path)))
    publish = DreamStore.publish

    def lose_reply(self, record, *, artifacts):
        result = publish(self, record, artifacts=artifacts)
        if record["record_type"] == "completed":
            raise TimeoutError("completion committed but reply lost")
        return result

    monkeypatch.setattr(DreamStore, "publish", lose_reply)
    assert runtime.complete(str(tmp_path), worklist)["completed"]
    assert runtime.complete(str(tmp_path), worklist)["replayed"]
    completed = [event for event in runtime._store(str(tmp_path)).events()
                 if event["record_type"] == "completed"]
    assert len(completed) == 1


def test_checkpoint_base_contains_no_physical_control_artifact_ids(inputs, tmp_path):
    runtime.complete(str(tmp_path), review(runtime.harvest(str(tmp_path))))
    next_run = runtime.harvest(str(tmp_path))
    assert "artifact_id" not in json.dumps(next_run["incremental"]["base"])


def test_wrongly_typed_partial_review_is_rejected_before_native_save(inputs, tmp_path):
    worklist = runtime.harvest(str(tmp_path))
    identity = worklist["incremental"]["run_id"]
    worklist["coverage"][0]["review"] = "looks reviewed"
    with pytest.raises(ValueError, match="review"):
        runtime.save_review(str(tmp_path), identity, worklist)


def test_source_rows_with_missing_session_identity_fail_closed(inputs, tmp_path):
    with sqlite3.connect(inputs[0]) as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                    (None, "owner/project", "", "", "2000-01-01", "", ""))
    with pytest.raises(ValueError, match="session identity"):
        runtime.harvest(str(tmp_path))


@pytest.mark.parametrize("peers", [
    '{"peers":[{"name":"other-host","url":"https://peer.invalid"}]}',
    '{"peers":',
])
def test_pending_completion_rejects_mesh_or_malformed_membership_without_effects(inputs, tmp_path, peers):
    worklist = review(runtime.harvest(str(tmp_path)))
    (tmp_path / "peers.json").write_text(peers)
    before = runtime._store(str(tmp_path)).events()
    with pytest.raises(ValueError, match="mesh|malformed"):
        runtime.complete(str(tmp_path), worklist)
    assert runtime._store(str(tmp_path)).events() == before
    assert runtime.load_run(str(tmp_path), worklist["incremental"]["run_id"])["incremental"] == worklist["incremental"]


def test_reserved_mesh_transport_cannot_complete_local_checkpoint(inputs, tmp_path, monkeypatch):
    worklist = review(runtime.harvest(str(tmp_path)))
    monkeypatch.setenv("MEMPALACE_TRANSPORT", "meshguard")
    with pytest.raises((ValueError, NotImplementedError), match="topology|transport"):
        runtime.complete(str(tmp_path), worklist)


def _finish_process(palace, worklist, barrier, results):
    import dream_palace
    dream_palace.bind_palace(palace)
    barrier.wait(timeout=30)
    try:
        results.put(("ok", runtime.complete(palace, worklist)))
    except (ValueError, RuntimeError, OSError) as exc:
        results.put(("error", str(exc)))


def _host_store(path):
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE sessions(id TEXT, repository TEXT, created_at TEXT);
            CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                               assistant_response TEXT, timestamp TEXT);
        """)
        for identity, repository in (("a", "owner/a"), ("b", "owner/b"), ("none", None)):
            con.execute("INSERT INTO sessions VALUES (?,?,?)", (identity, repository, "2000-01-01"))
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (identity, 0, f"Full original {identity}", "Complete response", "2000-01-01"))


def test_two_real_local_processes_cannot_complete_same_checkpoint(tmp_path, monkeypatch):
    import multiprocessing
    import dream_palace
    from dream_store import DreamStore
    from test_dream_procedural_palace import installed_palace
    palace = tmp_path / "palace"
    palace.mkdir()
    source = tmp_path / "host.db"
    _host_store(source)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(source))
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        dream_palace.MempalaceWriter().add_drawer("A", "design", "Original memory A", added_by="user")
        DreamStore.initialize(str(palace))
        first = review(runtime.harvest(str(palace)))
        second = review(runtime.harvest(str(palace)))
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        processes = [context.Process(target=_finish_process,
                                     args=(str(palace), worklist, barrier, results))
                     for worklist in (first, second)]
        try:
            for process in processes:
                process.start()
            outcomes = [results.get(timeout=45), results.get(timeout=45)]
            for process in processes:
                process.join(timeout=30)
                assert process.exitcode == 0
            assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
            assert "stale" in next(value for kind, value in outcomes if kind == "error")
            assert len([event for event in DreamStore(str(palace)).events()
                        if event["record_type"] == "completed"]) == 1
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)


def test_palace_only_restore_recovers_complete_native_run_shape_and_original_inventory(tmp_path, monkeypatch):
    import shutil
    import dream_palace
    from dream_store import DreamStore
    from test_dream_procedural_palace import installed_palace
    palace = tmp_path / "palace"
    palace.mkdir()
    source = tmp_path / "host.db"
    _host_store(source)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(source))
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        writer = dream_palace.MempalaceWriter()
        writer.add_drawer("A", "architecture", "Original wing A source", added_by="user")
        writer.add_drawer("B", "design", "Original wing B source", added_by="user")
        DreamStore.initialize(str(palace))
        worklist = review(runtime.harvest(str(palace)))
        identity = worklist["incremental"]["run_id"]
        assert {s["repository"] for s in worklist["coverage"] if s["source_kind"] == "session"} == {
            "owner/a", "owner/b", None}
        assert {s["wing"] for s in worklist["coverage"] if s["source_kind"] == "memory"} == {"A", "B"}
        assert all(s["turns"][0]["assistant_response"] == "Complete response"
                   for s in worklist["coverage"] if s["source_kind"] == "session")
        runtime.save_review(str(palace), identity, worklist)
        runtime.complete(str(palace), worklist)
        # Preserve a saved but uncompleted review in the same full-palace backup.
        pending = review(runtime.harvest(str(palace), "owner/a"))
        runtime.save_review(str(palace), pending["incremental"]["run_id"], pending)
    restored = tmp_path / "restored"
    shutil.copytree(palace, restored)
    shutil.rmtree(palace)
    source.unlink()
    with installed_palace(str(restored)):
        dream_palace.bind_palace(str(restored))
        native = DreamStore(str(restored))
        assert runtime.load_run(str(restored), identity) == worklist
        assert runtime.complete(str(restored), worklist)["replayed"]
        kinds = {event["record_type"] for event in native.events(run_id=identity)}
        assert {"run_created", "review_saved", "adoption_started", "completed"} <= kinds
        checkpoint = native.checkpoint(worklist["incremental"]["scope_id"])
        assert len(checkpoint["reviewed_versions"]) == 5
        loaded_pending = runtime.load_run(str(restored), pending["incremental"]["run_id"])
        assert loaded_pending == pending
        with pytest.raises(FileNotFoundError, match="session store is missing"):
            runtime.complete(str(restored), loaded_pending)


def _adopt_over_http_process(palace, worklist, info, token, results):
    from unittest.mock import patch
    from mempalace import server_registry
    import dream_palace
    dream_palace.bind_palace(palace)
    with patch.object(server_registry, "read_live_serverinfo", return_value=info), \
            patch.object(server_registry, "load_server_token", return_value=token):
        try:
            results.put(("ok", runtime.complete(palace, worklist)))
        except Exception as exc:
            results.put(("error", str(exc)))


def test_dead_client_cannot_release_durable_intent_while_native_http_handler_is_pending(tmp_path, monkeypatch):
    import multiprocessing
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import dream_palace
    import dream_transport
    from dream_store import DreamStore
    from test_dream_procedural_palace import installed_palace

    palace = tmp_path / "palace"
    palace.mkdir()
    source = tmp_path / "host.db"
    _host_store(source)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(source))
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        dream_palace.MempalaceWriter().add_drawer(
            "garden", "notes", "Orchids prefer humid shade and gentle indirect sunlight.", added_by="user")
        DreamStore.initialize(str(palace))
        worklist = review(runtime.harvest(str(palace)))
        worklist["items"] = [proposal(sources=["session:a", "session:b"])]
        worklist["items"][0]["decision"]["conclusion"]["text"] = (
            "Before deploying a database schema migration, verify the rollback against an isolated copy.")
        competitor = review(runtime.harvest(str(palace)))
        received, release, settled = threading.Event(), threading.Event(), threading.Event()
        handler_errors = []
        token = secrets.token_urlsafe(24)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.headers.get("Authorization") == f"Bearer {token}"
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    params = request["params"]
                    assert params["name"] == "mempalace_add_drawer"
                    received.set()
                    assert release.wait(timeout=45), "test did not release pending native handler"
                    result = dream_transport._embedded_call(str(palace), params["name"], params["arguments"])
                    payload = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "content": [{"type": "text", "text": json.dumps(result)}]}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    try:
                        self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                except Exception as exc:
                    handler_errors.append(exc)
                finally:
                    settled.set()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        context = multiprocessing.get_context("spawn")
        results = context.Queue()
        info = {"host": "127.0.0.1", "port": server.server_port, "palace_path": str(palace)}
        client = context.Process(target=_adopt_over_http_process,
                                 args=(str(palace), worklist, info, token, results))
        opponent = None
        try:
            client.start()
            if not received.wait(timeout=30):
                assert not handler_errors, handler_errors
                assert client.is_alive(), results.get(timeout=5)
                pytest.fail("client never reached native HTTP handler")
            events = DreamStore(str(palace)).events(run_id=worklist["incremental"]["run_id"])
            assert any(event["record_type"] == "proposal_started" for event in events)
            assert not any(event["record_type"] == "proposal_settled" for event in events)
            client.terminate()
            client.join(timeout=10)
            assert not client.is_alive()
            opponent_ready = context.Barrier(1)
            opponent = context.Process(target=_finish_process,
                                       args=(str(palace), competitor, opponent_ready, results))
            opponent.start()
            kind, message = results.get(timeout=30)
            opponent.join(timeout=10)
            assert opponent.exitcode == 0
            assert kind == "error" and "adoption intent" in message
            changed = copy.deepcopy(worklist)
            changed["items"] = []
            with pytest.raises(ValueError, match="pins|pinned"):
                runtime.save_review(str(palace), worklist["incremental"]["run_id"], changed)
            with pytest.raises(ValueError, match="unresolved"):
                runtime.complete(str(palace), worklist)
            release.set()
            assert settled.wait(timeout=30), "native handler did not settle"
            assert not handler_errors
            assert runtime.complete(str(palace), worklist)["completed"]
            assert len([drawer for drawer in runtime._drawers(str(palace))
                        if runtime.decode_dream_metadata(drawer).get("incremental_run_id") ==
                        worklist["incremental"]["run_id"]]) == 1
        finally:
            release.set()
            for process in (client, opponent):
                if process is not None and process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
            server.shutdown()
            server.server_close()
            serving.join(timeout=10)


@pytest.mark.parametrize("owner_timing", ["before-validation", "before-dispatch"])
def test_real_predispatch_foreign_owner_refusal_can_retry_after_owner_exits(tmp_path, monkeypatch, owner_timing):
    import multiprocessing
    import dream_palace
    import dream_adopt
    from dream_store import DreamStore
    from test_dream_procedural_palace import installed_palace
    from test_dream_maintenance_state import _hold_native_stdio_owner

    palace = tmp_path / "palace"
    palace.mkdir()
    source = tmp_path / "host.db"
    _host_store(source)
    monkeypatch.setenv("COPILOT_SESSION_STORE", str(source))
    with installed_palace(str(palace)):
        dream_palace.bind_palace(str(palace))
        dream_palace.MempalaceWriter().add_drawer(
            "garden", "notes", "Orchids prefer humid shade and gentle indirect sunlight.", added_by="user")
        DreamStore.initialize(str(palace))
        worklist = review(runtime.harvest(str(palace)))
        worklist["items"] = [proposal(sources=["session:a", "session:b"])]
        worklist["items"][0]["decision"]["conclusion"]["text"] = (
            "Before deploying a database schema migration, verify the rollback against an isolated copy.")
        context = multiprocessing.get_context("spawn")
        ready, release = context.Queue(), context.Event()
        owner = context.Process(target=_hold_native_stdio_owner, args=(str(palace), ready, release))
        preflight = dream_adopt._preflight_reflect_decisions

        def owner_arrives_after_readonly_validation(*args, **kwargs):
            result = preflight(*args, **kwargs)
            if owner.pid is None:
                owner.start()
                acquired, reason = ready.get(timeout=20)
                assert acquired, reason
            return result

        monkeypatch.setattr(dream_adopt, "_preflight_reflect_decisions", owner_arrives_after_readonly_validation)
        try:
            if owner_timing == "before-validation":
                owner.start()
                acquired, reason = ready.get(timeout=20)
                assert acquired, reason
            with pytest.raises(RuntimeError, match="adoption"):
                runtime.complete(str(palace), worklist)
            assert len(runtime._drawers(str(palace))) == 1
        finally:
            release.set()
            if owner.pid is not None:
                owner.join(timeout=10)
                if owner.is_alive():
                    owner.terminate()
                    owner.join(timeout=5)
            ready.close()
            ready.join_thread()
        assert owner.exitcode == 0
        assert runtime.complete(str(palace), worklist)["completed"]
        events = DreamStore(str(palace)).events(run_id=worklist["incremental"]["run_id"])
        starts = [event for event in events if event["record_type"] == "proposal_started"]
        settlements = [event for event in events if event["record_type"] == "proposal_settled"]
        assert [event["attempt"] for event in starts] == [1, 2]
        assert [event["outcome"] for event in settlements] == ["not_dispatched", "adopted"]
        assert [event["attempt_id"] for event in settlements] == [event["operation_id"] for event in starts]
        assert len(runtime._drawers(str(palace))) == 2


@pytest.mark.parametrize("failure", ["generic-transport", "uncertain", "false-result"])
def test_unproven_failure_never_settles_as_not_dispatched(inputs, tmp_path, writer, monkeypatch, failure):
    import dream_transport
    worklist = review(runtime.harvest(str(tmp_path)))
    worklist["items"] = [proposal()]
    calls = []

    def fail(*args, **kwargs):
        calls.append(failure)
        if failure == "generic-transport":
            raise dream_transport.TransportError("tool replied with an error after dispatch")
        if failure == "uncertain":
            raise dream_transport.UncertainWriteError("reply unavailable")
        return {"success": False, "error": "post-dispatch failure"}

    monkeypatch.setattr(writer, "add_drawer", fail)
    with pytest.raises(RuntimeError):
        runtime.complete(str(tmp_path), worklist)
    with pytest.raises(ValueError, match="unresolved"):
        runtime.complete(str(tmp_path), worklist)
    assert calls == [failure]
    assert not any(event["record_type"] == "proposal_settled"
                   for event in runtime._store(str(tmp_path)).events())


def test_uncertain_second_attempt_remains_blocked_after_safe_first_refusal(inputs, tmp_path, writer, monkeypatch):
    from dream_transport import NotDispatchedError, TransportError
    worklist = review(runtime.harvest(str(tmp_path)))
    worklist["items"] = [proposal()]
    calls = []

    def fail(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise NotDispatchedError("native preflight refused before dispatch")
        raise TransportError("handler error does not prove no write")

    monkeypatch.setattr(writer, "add_drawer", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="adoption"):
            runtime.complete(str(tmp_path), worklist)
    with pytest.raises(ValueError, match="unresolved"):
        runtime.complete(str(tmp_path), worklist)
    assert len(calls) == 2
    status = runtime.run_status(str(tmp_path), worklist["incremental"]["run_id"])
    assert status["status"] == "blocked"
    assert status["unresolved_proposals"] == ["lesson-1"]
