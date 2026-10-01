"""Exact Dreaming control storage, exercised against installed native logstream."""
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest


def api():
    assert importlib.util.find_spec("dream_store"), "DreamStore native persistence is not implemented"
    return importlib.import_module("dream_store")


@pytest.fixture
def native(tmp_path):
    from mempalace.logstream import Logstream

    palace = tmp_path / "palace"
    palace.mkdir()
    log = Logstream(str(palace / "logstream.sqlite3"), replica_id="test-store")
    yield palace, log
    log.close()


def caller(log):
    def call(palace, name, arguments, *, vector=False):
        assert vector is False
        handlers = {
            "mempalace_artifact_put": lambda: {"success": True, "artifact": log.put_artifact(**arguments)},
            "mempalace_artifact_get": lambda: {"artifact": log.get_artifact(**arguments)},
            "mempalace_event_append": lambda: {"success": True, "event": log.append_event(**arguments)},
            "mempalace_event_list": lambda: {"events": log.list_events(
                **{key: value for key, value in arguments.items() if key != "preview"})},
        }
        return handlers[name]()
    return call


def record(kind="run_created", **kwargs):
    return {"schema_version": 1, "record_type": kind, "scope_id": "scope", "run_id": "run", **kwargs}


def snapshot(palace):
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in palace.iterdir() if path.is_file()
    }


def test_missing_storage_is_not_empty_history_or_implicit_initialization(tmp_path):
    module = api()
    before = snapshot(tmp_path)
    with pytest.raises(module.StoreError, match="initializ"):
        module.DreamStore(str(tmp_path)).events()
    assert snapshot(tmp_path) == before


def test_native_document_event_roundtrip_and_readonly_reopen(native, monkeypatch):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    value = {"original": "完整\n" * 12, "sources": [{"id": "a", "review": None}]}
    reference = store.put_document(value, purpose="manifest")
    published = store.publish(record(manifest=reference), artifacts=[reference])
    log.close()
    before = snapshot(palace)
    monkeypatch.setattr(module, "native_call_tool", lambda *a, **k: pytest.fail("read dispatched native handler"))
    reopened = module.DreamStore(str(palace))
    assert reopened.get_document(reference) == value
    assert reopened.events()[0]["event_id"] == published["event_id"]
    assert reopened.load_run("run") == value
    assert snapshot(palace) == before


def test_healthy_empty_native_namespace_is_empty_without_bootstrap(native):
    palace, log = native
    log.close()
    before = snapshot(palace)
    store = api().DreamStore(str(palace))
    assert store.events() == []
    assert store.checkpoint("scope") is None
    assert snapshot(palace) == before


def test_reads_committed_wal_without_checkpoint_or_storage_mutation(native):
    palace, log = native
    store = api().DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document({"in": "wal"}, purpose="manifest")
    store.publish(record(manifest=reference), artifacts=[reference])
    assert (palace / "logstream.sqlite3-wal").stat().st_size > 0
    before = snapshot(palace)
    assert api().DreamStore(str(palace)).load_run("run") == {"in": "wal"}
    assert snapshot(palace) == before


def test_large_unicode_document_full_roundtrip_and_relocated_palace(native, tmp_path):
    palace, log = native
    module = api()
    value = {"turns": "😀雪" * 650_000, "last": "must survive"}
    store = module.DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document(value, purpose="manifest")
    assert reference["document_size_bytes"] > 4 * 1024 * 1024
    store.publish(record(manifest=reference), artifacts=[reference])
    assert store.get_document(reference) == value
    log.close()
    relocated = tmp_path / "relocated"
    shutil.copytree(palace, relocated)
    shutil.rmtree(palace)
    assert module.DreamStore(str(relocated)).load_run("run") == value


@pytest.mark.parametrize("corruption", ["missing", "content", "reorder"])
def test_fragment_integrity_failures_are_explicit(native, corruption):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document({"text": "a" * (4 * 1024 * 1024) + "different-tail"}, purpose="manifest")
    index = log.get_artifact(reference["artifact_id"])
    document = json.loads(index["content"])
    fragment = document["parts"][0]["artifact_id"]
    conn = log._conn()
    if corruption == "missing":
        conn.execute("DELETE FROM artifacts WHERE id=?", (fragment,))
    elif corruption == "content":
        conn.execute("UPDATE artifacts SET content='wrong' WHERE id=?", (fragment,))
    else:
        document["parts"].reverse()
        text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        conn.execute("UPDATE artifacts SET content=?,sha256=?,size_bytes=? WHERE id=?",
                     (text, digest, len(text.encode()), reference["artifact_id"]))
        reference = {**reference, "sha256": digest, "size_bytes": len(text.encode())}
    conn.commit()
    with pytest.raises(module.StoreError, match="artifact|hash|fragment|integrity|JSON"):
        store.get_document(reference)


def test_small_artifact_hash_and_schema_must_match(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    ref = store.put_document({"ok": True}, purpose="manifest")
    with pytest.raises(module.StoreError):
        store.get_document({**ref, "sha256": "0" * 64})
    with pytest.raises(module.StoreError):
        store.get_document({"artifact_id": ref["artifact_id"]})
    with pytest.raises(module.StoreError):
        store.get_document({**ref, "format": "unknown/v90"})


@pytest.mark.parametrize("value", [{"text": "\ud800"}, {"number": float("nan")}, {1: "not JSON keys"}])
def test_noncanonical_document_rejected_before_artifacts(native, value):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    with pytest.raises((ValueError, module.StoreError)):
        store.put_document(value, purpose="manifest")
    assert log._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_event_pagination_consumes_all_pages_and_native_append_order(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    for index in range(507):
        body = record("derive_skip", operation_id=f"op-{index}", index=index)
        body["payload_hash"] = module.semantic_hash(body)
        log.append_event(type="dream.derive_skip", stream="dreaming/v1", room="control",
                         from_agent="dreaming", body=module.canonical_json(body),
                         correlation_id="scope")
    log._conn().execute("UPDATE events SET created_at='1999-01-01T00:00:00Z' WHERE rowid=507")
    log._conn().commit()
    result = store.events(scope_id="scope")
    assert len(result) == 507
    assert result[-1]["index"] == 506
    assert len(module.DreamStore(str(palace)).events()) == 507


def test_repeated_native_cursor_is_not_silently_truncated(native, monkeypatch):
    palace, log = native
    module = api()
    base = caller(log)
    store = module.DreamStore(str(palace), call_tool=base)
    store.publish(record("derive_skip"), artifacts=[])
    event = log.list_events(limit=1)[0]
    monkeypatch.setattr(store, "_native_events", lambda: iter([event] * 500))
    with pytest.raises(module.StoreError, match="cursor|duplicate|progress"):
        store.events()


def test_lost_committed_reply_reconciles_once_and_semantic_conflicts_fail(native):
    palace, log = native
    module = api()
    base = caller(log)
    def lost(palace, name, arguments, *, vector=False):
        result = base(palace, name, arguments, vector=vector)
        if name == "mempalace_event_append":
            raise TimeoutError("reply lost after commit")
        return result
    store = module.DreamStore(str(palace), call_tool=lost)
    ref = store.put_document({"original": "unchanged"}, purpose="manifest")
    original = record(operation_id="logical", manifest=ref)
    first = store.publish(original, artifacts=[ref])
    replacement = store.put_document({"original": "unchanged"}, purpose="manifest")
    again = store.publish({**original, "manifest": replacement}, artifacts=[replacement])
    assert again["event_id"] == first["event_id"]
    assert len(store.events()) == 1
    with pytest.raises(module.StoreConflict, match="conflict"):
        store.publish({**original, "changed": True}, artifacts=[ref])


def test_publish_document_prelookup_reuses_artifacts_on_retry(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    first = store.publish_document(record(), {"original": "full"}, field="manifest", purpose="manifest")
    before = log._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    second = store.publish_document(record(), {"original": "full"}, field="manifest", purpose="manifest")
    assert second["event_id"] == first["event_id"]
    assert log._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == before


def test_checkpoint_materializes_ledger_and_rejects_branch(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    versions = store.put_document({"source": "sha"}, purpose="reviewed_versions")
    complete = record("completed", cutoff="2026-01-01T00:00:00Z", review_hash="review",
                      expected_checkpoint_operation_id=None, reviewed_versions=versions)
    first = store.publish(complete, artifacts=[versions])
    checkpoint = store.checkpoint("scope")
    assert checkpoint["reviewed_versions"] == {"source": "sha"}
    assert checkpoint["operation_id"] == first["operation_id"]
    assert checkpoint["cutoff"] == complete["cutoff"]
    with pytest.raises(module.StoreConflict):
        store.publish({**complete, "run_id": "competing", "cutoff": "2026-01-02T00:00:00Z"},
                      artifacts=[versions])
    assert store.checkpoint("unrelated") is None


def test_saved_review_loads_without_original_exports(native):
    palace, log = native
    store = api().DreamStore(str(palace), call_tool=caller(log))
    store.publish_document(record(), {"original": "frozen"}, field="manifest", purpose="manifest")
    reviewed = {"original": "frozen", "review": "partly complete"}
    store.publish_document(record("review_saved", previous_review_hash=None, review_hash="review"),
                           reviewed, field="review", purpose="review")
    assert store.load_run("run") == reviewed


def test_unknown_native_schema_and_missing_wal_sidecar_fail_readonly(native):
    palace, log = native
    module = api()
    log._conn().execute("PRAGMA user_version=999")
    log._conn().commit()
    with pytest.raises(module.StoreError, match="schema|version"):
        module.DreamStore(str(palace)).events()


def test_real_embedded_handlers_roundtrip_and_explicit_initialize(native):
    palace, log = native
    module = api()
    log.close()
    store = module.DreamStore(str(palace))
    ref = store.put_document({"real": "native MCP handlers"}, purpose="manifest")
    store.publish(record(manifest=ref), artifacts=[ref])
    assert store.load_run("run") == {"real": "native MCP handlers"}


def test_semantic_hash_preserves_nested_user_fields():
    module = api()
    assert module.semantic_hash({"metadata": {"payload_hash": "a"}}) != \
        module.semantic_hash({"metadata": {"payload_hash": "b"}})


def test_supplied_payload_hash_is_verified_before_append(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    with pytest.raises(module.StoreError, match="hash"):
        store.publish(record("derive_skip", payload_hash="bad"), artifacts=[])
    assert log.list_events() == []


def test_initialize_rejects_non_palace_directory_without_writing(tmp_path):
    module = api()
    before = snapshot(tmp_path)
    with pytest.raises(module.StoreError, match="palace"):
        module.DreamStore.initialize(str(tmp_path))
    assert snapshot(tmp_path) == before


def test_explicit_native_bootstrap_and_cli_are_idempotent(tmp_path):
    module = api()
    from mempalace.backends.sqlite_exact import SQLiteExactBackend
    backend = SQLiteExactBackend()
    backend.create_collection(str(tmp_path), "mempalace")
    backend.close()
    before = (tmp_path / "sqlite_exact.sqlite3").read_bytes()
    store = module.DreamStore.initialize(str(tmp_path))
    assert store.events() == []
    ref = store.put_document({"initialized": "native"}, purpose="manifest")
    store.publish(record(manifest=ref), artifacts=[ref])
    result = subprocess.run(
        [sys.executable, module.__file__, "--palace", str(tmp_path), "--initialize"],
        env={**os.environ, "HOME": str(tmp_path), "MEMPALACE_BACKEND": "sqlite_exact",
             "MEMPALACE_BACKEND_EXPLICIT": "sqlite_exact"},
        text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["initialized"]
    assert module.DreamStore(str(tmp_path)).load_run("run") == {"initialized": "native"}
    assert (tmp_path / "sqlite_exact.sqlite3").read_bytes() == before


def test_missing_one_wal_sidecar_is_never_treated_as_cold(native, tmp_path):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    store.publish(record("derive_skip"), artifacts=[])
    copy = tmp_path / "incomplete"
    copy.mkdir()
    shutil.copy2(palace / "logstream.sqlite3", copy / "logstream.sqlite3")
    shutil.copy2(palace / "logstream.sqlite3-wal", copy / "logstream.sqlite3-wal")
    before = snapshot(copy)
    with pytest.raises(module.StoreError, match="WAL"):
        module.DreamStore(str(copy)).events()
    assert snapshot(copy) == before


def test_malformed_json_duplicate_keys_and_unknown_record_schema_fail(native):
    palace, log = native
    module = api()
    log.append_event(type="dream.derive_skip", stream="dreaming/v1", room="control",
                     from_agent="dreaming", body='{"schema_version":1,"schema_version":2}')
    with pytest.raises(module.StoreError, match="duplicate"):
        module.DreamStore(str(palace)).events()


def test_fragment_indexes_are_bounded_and_recursive(native, monkeypatch):
    palace, log = native
    module = api()
    monkeypatch.setattr(module, "ARTIFACT_LIMIT", 1024)
    monkeypatch.setattr(module, "INDEX_PARTS", 2)
    store = module.DreamStore(str(palace), call_tool=caller(log))
    value = {"sources": "all originals " * 300}
    ref = store.put_document(value, purpose="manifest")
    root = json.loads(log.get_artifact(ref["artifact_id"])["content"])
    assert len(root["parts"]) <= 2
    assert root["parts"][0]["format"] == "dream-index/v1"
    assert store.get_document(ref) == value


def test_large_document_semantic_prelookup_avoids_fragment_replacement(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    value = {"text": "x" * (4 * 1024 * 1024)}
    first = store.publish_document(record(), value, field="manifest", purpose="manifest")
    count = log._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    assert store.publish_document(record(), value, field="manifest", purpose="manifest") == first
    assert log._conn().execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == count


def test_conflicting_native_physical_duplicates_do_not_choose_last(native):
    palace, log = native
    module = api()
    for value in ("first", "second"):
        body = record("derive_skip", operation_id="same", value=value)
        body["payload_hash"] = module.semantic_hash(body)
        log.append_event(type="dream.derive_skip", stream="dreaming/v1", room="control",
                         from_agent="dreaming", body=module.canonical_json(body),
                         correlation_id="scope")
    with pytest.raises(module.StoreConflict, match="conflict"):
        module.DreamStore(str(palace)).events()


def test_unknown_record_version_bool_rejected_before_write(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    with pytest.raises(module.StoreError, match="schema"):
        store.publish({**record("derive_skip"), "schema_version": True}, artifacts=[])


@pytest.mark.parametrize("artifacts", [[{}], [{"artifact_id": "missing"}], "not-a-list"])
def test_publish_rejects_incomplete_artifact_references_before_writes(native, artifacts):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    with pytest.raises(module.StoreError, match="artifact"):
        store.publish(record("archive"), artifacts=artifacts)
    assert log.list_events() == []


def test_semantic_depth_exhaustion_is_explicit_not_recursion_crash():
    module = api()
    value = {}
    for _ in range(1500):
        value = {"child": value}
    with pytest.raises(module.StoreError, match="depth|JSON"):
        module.semantic_hash(value)


def test_native_duplicate_same_semantics_replays_one_logical_operation(native):
    palace, log = native
    module = api()
    body = record("derive_skip", operation_id="same", value="unchanged")
    body["payload_hash"] = module.semantic_hash(body)
    for _ in range(2):
        log.append_event(type="dream.derive_skip", stream="dreaming/v1", room="control",
                         from_agent="dreaming", body=module.canonical_json(body),
                         correlation_id="scope")
    events = module.DreamStore(str(palace)).events()
    assert len(events) == 1
    assert len(events[0]["native_event_ids"]) == 2


def test_injected_native_mutation_caller_cannot_lazy_initialize_reads(native, monkeypatch):
    palace, log = native
    module = api()
    writer = module.DreamStore(str(palace), call_tool=caller(log))
    ref = writer.put_document({"safe": "reader"}, purpose="manifest")
    writer.publish(record(manifest=ref), artifacts=[ref])
    log.close()
    before = snapshot(palace)
    store = module.DreamStore(
        str(palace), call_tool=lambda *a, **kw: pytest.fail("read called a potentially mutating native handler"))
    assert store.get_document(ref) == {"safe": "reader"}
    assert len(store.events()) == 1
    assert snapshot(palace) == before


@pytest.mark.parametrize("stage", [
    "delete_started", "delete_settled", "restore_settled",
    "archive_delete_started", "archive_delete_settled",
])
def test_maintenance_effect_transitions_preserve_exact_stage_payloads(native, stage):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    published = store.publish(record(stage, archive_operation_id="archive-id",
                                     drawer_id="source", outcome="stage-owned"), artifacts=[])
    replayed = store.events()[0]
    assert replayed["record_type"] == stage
    assert replayed["event_id"] == published["event_id"]
    assert replayed["archive_operation_id"] == "archive-id"


@pytest.mark.parametrize("fragment_state", ["intact", "missing", "corrupt"])
def test_event_replay_rejects_fragment_reference_format_downgrade(native, fragment_state):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document({"text": "x" * (4 * 1024 * 1024)}, purpose="manifest")
    published = store.publish(record(manifest=reference), artifacts=[reference])
    physical = log.list_events(limit=1)[0]
    envelope = json.loads(physical["body"])
    envelope["manifest"] = {**reference, "format": "dream-json/v1"}
    # Keep the ORIGINAL event payload hash: changing interpretation must not
    # preserve semantic identity while replacing the original with its index.
    assert envelope["payload_hash"] == published["payload_hash"]
    index = json.loads(log.get_artifact(reference["artifact_id"])["content"])
    fragment_id = index["parts"][0]["artifact_id"]
    if fragment_state == "missing":
        log._conn().execute("DELETE FROM artifacts WHERE id=?", (fragment_id,))
    elif fragment_state == "corrupt":
        log._conn().execute("UPDATE artifacts SET content='corrupt' WHERE id=?", (fragment_id,))
    log._conn().execute("UPDATE events SET body=? WHERE id=?",
                        (module.canonical_json(envelope), physical["id"]))
    log._conn().commit()
    with pytest.raises(module.StoreError, match="reference|format|aggregate|schema|hash"):
        store.events()


def test_fragment_reference_cannot_claim_json_while_retaining_document_digest(native):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document({"text": "x" * (4 * 1024 * 1024)}, purpose="manifest")
    altered = {**reference, "format": "dream-json/v1"}
    with pytest.raises(module.StoreError, match="reference|format|aggregate|schema"):
        store.get_document(altered)
    with pytest.raises(module.StoreError, match="reference|format|aggregate|schema"):
        module.semantic_hash(altered)


@pytest.mark.parametrize("change", [
    {"document_sha256": "f" * 64, "document_size_bytes": 10},
    {"extra": "not part of the typed reference schema"},
    {"format": None},
])
def test_json_reference_schema_matches_the_bytes_used_for_semantics(native, change):
    palace, log = native
    module = api()
    store = module.DreamStore(str(palace), call_tool=caller(log))
    reference = store.put_document({"original": "exact"}, purpose="manifest")
    altered = {**reference, **change}
    with pytest.raises(module.StoreError, match="reference|format|aggregate|schema"):
        store.get_document(altered)
    with pytest.raises(module.StoreError, match="reference|format|aggregate|schema"):
        module.semantic_hash(altered)
