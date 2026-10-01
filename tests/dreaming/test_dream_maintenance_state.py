"""Native maintenance state must survive without Dreaming sidecar files."""
import json
import multiprocessing
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

import dream_ontology
import dream_palace
import dream_restore
from test_dream_procedural_palace import DrawerCollection, installed_palace


def initialize_logstream(palace):
    """Seed an existing native logstream independently of fake vector fixtures."""
    from mempalace.logstream import Logstream
    from pathlib import Path
    Logstream(str(Path(palace) / "logstream.sqlite3")).close()


@pytest.fixture
def native_palace(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    with installed_palace(str(palace)):
        from mempalace.logstream import Logstream

        log = Logstream(str(palace / "logstream.sqlite3"))
        yield palace
        log.close()


def originals():
    return DrawerCollection({
        "part-1": {"id": "part-1", "text": "first", "metadata": {"wing": "alpha", "room": "notes"},
                   "embedding": [1.0, 0.0]},
        "part-2": {"id": "part-2", "text": "second", "metadata": {"wing": "alpha", "room": "notes"},
                   "embedding": [0.0, 1.0]},
    })


RECORD = {"id": "logical", "member_ids": ["part-2", "part-1"],
          "wing": "alpha", "room": "notes", "reason": "prune"}


class DeleteWriter:
    def __init__(self, collection, *, fail=None):
        self.collection = collection
        self.fail = fail
        self.calls = []

    def delete_drawer(self, drawer_id):
        self.calls.append(drawer_id)
        if drawer_id == self.fail:
            from dream_transport import NotDispatchedError
            raise NotDispatchedError("interrupted delete before dispatch")
        return self.collection.delete(drawer_id)


def test_default_archive_is_native_and_retains_complete_ordered_originals(native_palace):
    collection = originals()
    writer = DeleteWriter(collection)
    dream_palace.Archiver(str(native_palace), writer=writer, collection=collection).archive_then_delete(RECORD)

    assert not (native_palace / "dream-archive.jsonl").exists()
    records = dream_restore.load_native_archive_records(str(native_palace))
    assert len(records) == 1
    assert records[0]["member_ids"] == ["part-2", "part-1"]
    assert records[0]["reason"] == "prune"
    assert [row["document"] for row in records[0]["rows"]] == ["second", "first"]
    assert [row["embedding"] for row in records[0]["rows"]] == [[0.0, 1.0], [1.0, 0.0]]
    assert not collection.rows


def test_native_archive_publication_failure_leaves_all_originals(native_palace, monkeypatch):
    from dream_store import DreamStore

    collection = originals()
    writer = DeleteWriter(collection)
    monkeypatch.setattr(DreamStore, "publish", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("native publication failed")))
    with pytest.raises(RuntimeError, match="native publication failed"):
        dream_palace.Archiver(str(native_palace), writer=writer, collection=collection).archive_then_delete(RECORD)
    assert set(collection.rows) == {"part-1", "part-2"}
    assert writer.calls == []


def test_interrupted_delete_reuses_archive_and_settles_each_member(native_palace):
    collection = originals()
    writer = DeleteWriter(collection, fail="part-1")
    archiver = dream_palace.Archiver(str(native_palace), writer=writer, collection=collection)
    with pytest.raises(RuntimeError, match="interrupted delete"):
        archiver.archive_then_delete(RECORD)
    writer.fail = None
    result = archiver.archive_then_delete(RECORD)
    assert result["deleted"] == ["part-2", "part-1"]
    assert writer.calls == ["part-2", "part-1", "part-1"]
    assert len(dream_restore.load_native_archive_records(str(native_palace))) == 1
    from dream_store import DreamStore
    settled = [r for r in DreamStore(str(native_palace)).events()
               if r["record_type"] == "archive_delete_settled"]
    assert {r["member_id"] for r in settled} == {"part-1", "part-2"}
    assert [r["outcome"] for r in settled] == ["deleted", "not_dispatched", "deleted"]


def test_restore_default_uses_native_archive_without_jsonl(native_palace):
    collection = originals()
    dream_palace.Archiver(str(native_palace), writer=DeleteWriter(collection),
                         collection=collection).archive_then_delete(RECORD)
    class Writer:
        def add_drawer(self, wing, room, content, **kwargs):
            return collection.add(wing, room, content, **kwargs)

    assert dream_restore.main(["--palace", str(native_palace)], writer_factory=Writer) == 0
    assert [r["text"] for r in collection.rows.values()] == ["second\nfirst"]


def test_ontology_and_skips_native_roundtrip_preserve_disabled_rules(native_palace):
    doc = {"version": 1, "rules": [{"id": "transitive:depends_on", "enabled": False,
                                  "family": "transitive", "predicate": "depends_on"}]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(native_palace))
    dream_palace.append_skip_markers(None, [{"candidate_id": "candidate", "reason": "reviewed"}],
                                    palace=str(native_palace))
    assert dream_ontology.read_ontology_doc(palace=str(native_palace)) == doc
    assert dream_palace.load_ontology_config(palace=str(native_palace)) == doc["rules"]
    assert dream_palace.load_skip_markers(palace=str(native_palace)) == [
        {"candidate_id": "candidate", "reason": "reviewed"}]
    assert not (native_palace / "ontology.json").exists()
    assert not (native_palace / "dream-derive-skips.jsonl").exists()


def test_native_ontology_is_not_silently_imported_from_old_default_file(native_palace):
    old = native_palace / "ontology.json"
    old.write_text(json.dumps({"version": 1, "rules": [{"id": "old", "enabled": True}]}))
    assert dream_ontology.read_ontology_doc(palace=str(native_palace)) == {"version": 1, "rules": []}
    assert json.loads(old.read_text())["rules"][0]["enabled"] is True


def test_explicit_ontology_export_and_import_keeps_native_copy(native_palace, tmp_path):
    export = tmp_path / "explicit-ontology.json"
    doc = {"version": 1, "rules": [{"id": "reviewed", "enabled": True}]}
    dream_ontology.write_ontology_doc(str(export), doc, palace=str(native_palace))
    assert json.loads(export.read_text()) == doc
    export.unlink()
    assert dream_ontology.read_ontology_doc(palace=str(native_palace)) == doc


def test_missing_native_storage_does_not_fall_back_or_initialize(tmp_path):
    palace = tmp_path / "missing"
    palace.mkdir()
    (palace / "ontology.json").write_text('{"version":1,"rules":[]}')
    before = set(palace.iterdir())
    with pytest.raises((RuntimeError, ValueError, FileNotFoundError)):
        dream_ontology.read_ontology_doc(palace=str(palace))
    assert set(palace.iterdir()) == before


def test_writer_uses_sanctioned_transport_and_verifies_exact_receipt(native_palace):
    calls = []
    stored = {}
    def transport(palace, name, arguments, *, vector):
        assert palace == str(native_palace)
        assert vector is True
        calls.append(name)
        if name == "mempalace_add_drawer":
            stored.update(arguments)
            return {"success": True, "drawer_id": "receipt"}
        return {"drawer_id": "receipt", "content": stored["content"],
                "wing": stored["wing"], "room": stored["room"]}
    writer = dream_palace.MempalaceWriter(str(native_palace), call_tool=transport)
    result = writer.add_drawer("alpha", "lessons", "exact lesson", metadata={"run_id": "run"})
    assert result["drawer_id"] == "receipt"
    assert calls == ["mempalace_add_drawer", "mempalace_get_drawer"]
    assert stored["content"] == 'exact lesson\n\n<!--dreaming-meta: {"run_id":"run"}-->'


def test_writer_refuses_mismatched_receipt_without_retry(native_palace):
    calls = []
    def transport(palace, name, arguments, *, vector):
        calls.append(name)
        if name == "mempalace_add_drawer":
            return {"success": True, "drawer_id": "receipt"}
        return {"drawer_id": "receipt", "content": "different", "wing": "alpha", "room": "lessons"}
    writer = dream_palace.MempalaceWriter(str(native_palace), call_tool=transport)
    with pytest.raises(RuntimeError, match="readback"):
        writer.add_drawer("alpha", "lessons", "exact lesson")
    assert calls == ["mempalace_add_drawer", "mempalace_get_drawer"]


def test_writer_propagates_uncertainty_without_alternate_vector_writer(native_palace):
    class Uncertain(RuntimeError):
        uncertain = True
    error = Uncertain("hub request may have reached handler")
    calls = []
    def transport(palace, name, arguments, *, vector):
        calls.append(name)
        raise error
    writer = dream_palace.MempalaceWriter(str(native_palace), call_tool=transport)
    with pytest.raises(Uncertain) as caught:
        writer.add_drawer("alpha", "lessons", "exact lesson")
    assert caught.value is error
    assert calls == ["mempalace_add_drawer"]


def test_archive_publish_without_durable_readback_never_deletes(native_palace, monkeypatch):
    from dream_store import DreamStore
    collection = originals()
    writer = DeleteWriter(collection)
    monkeypatch.setattr(DreamStore, "publish", lambda self, record, **kw: record)
    with pytest.raises(RuntimeError, match="readback"):
        dream_palace.Archiver(str(native_palace), writer=writer, collection=collection).archive_then_delete(RECORD)
    assert writer.calls == []
    assert len(collection.rows) == 2


def test_uncertain_delete_is_not_reissued_while_original_still_present(native_palace):
    class Uncertain(RuntimeError):
        uncertain = True
    collection = originals()
    class Writer:
        calls = 0
        def delete_drawer(self, drawer_id):
            self.calls += 1
            raise Uncertain("request outstanding")
    writer = Writer()
    archiver = dream_palace.Archiver(str(native_palace), writer=writer, collection=collection)
    with pytest.raises(Uncertain):
        archiver.archive_then_delete(RECORD)
    with pytest.raises(RuntimeError, match="unsettled"):
        archiver.archive_then_delete(RECORD)
    assert writer.calls == 1
    assert len(dream_restore.load_native_archive_records(str(native_palace))) == 1


@pytest.mark.parametrize("failure_kind", ["runtime", "transport", "negative_result"])
def test_generic_delete_failure_is_not_positive_no_dispatch_proof(native_palace, failure_kind):
    from dream_store import DreamStore
    from dream_transport import TransportError

    collection = originals()
    class Writer:
        calls = 0
        def delete_drawer(self, drawer_id):
            self.calls += 1
            if failure_kind == "negative_result":
                return {"success": False, "error": "handler returned failure after dispatch"}
            error_type = TransportError if failure_kind == "transport" else RuntimeError
            raise error_type("handler may have received the delete")
    writer = Writer()
    archiver = dream_palace.Archiver(str(native_palace), writer=writer, collection=collection)
    with pytest.raises(RuntimeError):
        archiver.archive_then_delete(RECORD)
    events = DreamStore(str(native_palace)).events()
    assert not any(event["record_type"] == "archive_delete_settled" for event in events)
    with pytest.raises(RuntimeError, match="unsettled"):
        archiver.archive_then_delete(RECORD)
    assert writer.calls == 1


def test_unsettled_delete_reconciles_exact_absence_without_reissuing(native_palace):
    from dream_transport import TransportError
    collection = originals()
    class Writer:
        calls = []
        def delete_drawer(self, drawer_id):
            self.calls.append(drawer_id)
            if drawer_id == "part-2":
                raise TransportError("request outcome unknown")
            return collection.delete(drawer_id)
    writer = Writer()
    archiver = dream_palace.Archiver(str(native_palace), writer=writer, collection=collection)
    with pytest.raises(TransportError):
        archiver.archive_then_delete(RECORD)
    collection.delete("part-2")
    result = archiver.archive_then_delete(RECORD)
    assert result["deleted"] == ["part-2", "part-1"]
    assert writer.calls == ["part-2", "part-1"]


def test_legacy_ambiguous_failed_event_does_not_authorize_another_delete(native_palace):
    from dream_store import DreamStore

    collection = originals()
    class Writer:
        calls = 0
        def delete_drawer(self, drawer_id):
            self.calls += 1
            raise RuntimeError("outcome unknown")
    writer = Writer()
    archiver = dream_palace.Archiver(str(native_palace), writer=writer, collection=collection)
    with pytest.raises(RuntimeError, match="unknown"):
        archiver.archive_then_delete(RECORD)
    store = DreamStore(str(native_palace))
    start = next(e for e in store.events() if e["record_type"] == "archive_delete_started")
    with dream_palace.palace_mutation_lock(str(native_palace)):
        store.publish({
            "schema_version": 1, "record_type": "archive_delete_settled",
            "archive_id": start["archive_id"], "member_id": start["member_id"],
            "attempt_id": start["operation_id"], "outcome": "failed",
        }, artifacts=[])
    with pytest.raises(RuntimeError, match="unsettled"):
        archiver.archive_then_delete(RECORD)
    assert writer.calls == 1


def test_native_maintenance_survives_palace_only_copy(native_palace, tmp_path):
    collection = originals()
    dream_palace.Archiver(str(native_palace), writer=DeleteWriter(collection),
                         collection=collection).archive_then_delete(RECORD)
    doc = {"version": 1, "rules": [{"id": "disabled", "enabled": False}]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(native_palace))
    dream_palace.append_skip_markers(None, [{"candidate_id": "skipped"}], palace=str(native_palace))
    moved = tmp_path / "restored-palace"
    moved.mkdir()
    with sqlite3.connect(native_palace / "logstream.sqlite3") as source, \
            sqlite3.connect(moved / "logstream.sqlite3") as target:
        source.backup(target)
    assert dream_restore.load_native_archive_records(str(moved))[0]["rows"][0]["document"] == "second"
    assert dream_ontology.read_ontology_doc(palace=str(moved)) == doc
    assert dream_palace.load_skip_markers(palace=str(moved)) == [{"candidate_id": "skipped"}]


def test_real_active_native_http_owner_accepts_writer_and_exact_readback(native_palace, monkeypatch):
    from mempalace import server_registry
    import dream_transport

    token = secrets.token_urlsafe(32)
    token_path = server_registry.server_token_path(str(native_palace))
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    token_path.chmod(0o600)
    process = subprocess.Popen(
        [sys.executable, "-m", "mempalace.mcp_server", "--palace", str(native_palace),
         "--backend", "sqlite_exact", "--transport", "http", "--host", "127.0.0.1", "--port", "0"],
        env={**os.environ, "MEMPALACE_EAGER_WARMUP": "0", "MEMPALACE_MCP_HTTP_TOKEN": token},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready = threading.Event()
        deadline = time.monotonic() + 30
        info = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _, error = process.communicate(timeout=5)
                pytest.fail(f"native HTTP owner exited before readiness: {error[-2000:]}")
            info = server_registry.read_live_serverinfo(str(native_palace))
            if info is not None:
                break
            ready.wait(.05)
        assert info is not None, "native HTTP owner did not advertise readiness in 30 seconds"
        request = urllib.request.Request(server_registry.client_base_url(info) + "/healthz")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        monkeypatch.setattr(dream_transport, "_embedded_call",
                            lambda *a: pytest.fail("competing embedded vector writer"))
        result = dream_palace.MempalaceWriter(str(native_palace)).add_drawer(
            "alpha", "lessons", "Actual active HTTP owner receipt.", metadata={"run_id": "active-hub"})
        assert result["success"] is True
        receipt = dream_transport.call_tool(str(native_palace), "mempalace_get_drawer",
                                            {"drawer_id": result["drawer_id"]}, vector=True)
        assert receipt["content"] == (
            'Actual active HTTP owner receipt.\n\n<!--dreaming-meta: {"run_id":"active-hub"}-->')
    finally:
        process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_real_writer_delete_readback_uses_live_storage_after_lease_release(native_palace):
    writer = dream_palace.MempalaceWriter(str(native_palace))
    result = writer.add_drawer("alpha", "notes", "Delete this disposable original.")
    writer.delete_drawer(result["drawer_id"])
    assert dream_palace.load_source_drawer(str(native_palace), result["drawer_id"]) is None


def test_real_archive_and_restore_roundtrip_without_external_archive(native_palace):
    writer = dream_palace.MempalaceWriter(str(native_palace))
    result = writer.add_drawer("alpha", "notes", "Restore this exact original.")
    original = dream_palace.load_source_drawer(str(native_palace), result["drawer_id"],
                                               include_embeddings=True)
    dream_palace.Archiver(str(native_palace), writer=writer).archive_then_delete({
        "id": original["id"], "member_ids": original["member_ids"],
        "wing": "alpha", "room": "notes", "reason": "prune"})
    assert dream_palace.load_source_drawer(str(native_palace), result["drawer_id"]) is None
    assert dream_restore.main(["--palace", str(native_palace)]) == 0
    restored = dream_palace.load_logical_drawers(str(native_palace))
    assert len(restored) == 1
    assert restored[0]["text"].startswith("Restore this exact original.")
    archived = dream_restore.load_native_archive_records(str(native_palace))[0]
    assert archived["rows"][0]["document"] == "Restore this exact original."
    assert len(archived["rows"][0]["embedding"]) == 384
    assert not (native_palace / "dream-archive.jsonl").exists()


def _hold_native_stdio_owner(palace, ready, release):
    server = dream_palace._embedded_mcp_server(palace)
    acquired, reason = server._acquire_mcp_writer_lock()
    ready.put((acquired, reason))
    try:
        if acquired and not release.wait(30):
            raise RuntimeError("test native owner release timeout")
    finally:
        server._release_mcp_writer_lock()


def test_foreign_native_stdio_owner_blocks_vectors_but_allows_control_state(native_palace):
    import dream_transport
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_hold_native_stdio_owner,
                          args=(str(native_palace), ready, release))
    process.start()
    try:
        acquired, reason = ready.get(timeout=20)
        assert acquired, reason
        with pytest.raises(dream_transport.TransportError, match="refused"):
            dream_palace.MempalaceWriter(str(native_palace)).add_drawer(
                "alpha", "lessons", "Do not bypass the foreign native owner.")
        dream_palace.append_skip_markers(None, [{"candidate_id": "owner-active"}],
                                        palace=str(native_palace))
        assert dream_palace.load_skip_markers(palace=str(native_palace)) == [
            {"candidate_id": "owner-active"}]
        assert process.is_alive()
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        ready.close()
        ready.join_thread()
    assert process.exitcode == 0


def test_logical_drawer_novelty_reads_do_not_compete_with_real_native_owner(native_palace):
    text = "Read this original without becoming a competing vector writer."
    dream_palace.MempalaceWriter(str(native_palace)).add_drawer("alpha", "notes", text)
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(target=_hold_native_stdio_owner,
                          args=(str(native_palace), ready, release))
    process.start()
    try:
        acquired, reason = ready.get(timeout=20)
        assert acquired, reason
        database = native_palace / "sqlite_exact.sqlite3"
        before = database.read_bytes()
        drawers = dream_palace.load_logical_drawers(str(native_palace), wing="alpha")
        assert [drawer["text"] for drawer in drawers] == [text]
        assert database.read_bytes() == before
        assert process.is_alive()
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        ready.close()
        ready.join_thread()
    assert process.exitcode == 0


def test_archive_rejects_internal_control_without_publishing_evidence(native_palace):
    from dream_store import DreamStore
    collection = originals()
    collection.rows["part-1"]["metadata"]["kind"] = "dream_control"
    writer = DeleteWriter(collection)
    with pytest.raises(ValueError, match="control"):
        dream_palace.Archiver(str(native_palace), writer=writer, collection=collection).archive_then_delete(RECORD)
    assert writer.calls == []
    assert DreamStore(str(native_palace)).events() == []


def test_contemplate_enable_disable_uses_native_ontology_without_sidecars(native_palace):
    import dream_contemplate
    doc = {"version": 1, "rules": [{"id": "reviewed", "family": "symmetric",
                                  "predicate": "related_to", "enabled": False}]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(native_palace))
    assert dream_contemplate.enable_rules(str(native_palace), ["reviewed"])["enabled"] == ["reviewed"]
    assert dream_palace.load_ontology_config(palace=str(native_palace))[0]["enabled"] is True
    assert dream_contemplate.disable_rules(str(native_palace), ["reviewed"])["disabled"] == ["reviewed"]
    assert dream_palace.load_ontology_config(palace=str(native_palace))[0]["enabled"] is False
    assert not (native_palace / "ontology.json").exists()


def test_contemplate_bootstrap_retains_native_enabled_rules_and_adds_disabled_candidates(
        native_palace, monkeypatch):
    import dream_contemplate
    doc = {"version": 1, "rules": [{"id": "reviewed", "family": "symmetric",
                                  "predicate": "related_to", "enabled": True}]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(native_palace))
    monkeypatch.setattr(dream_palace, "load_premises", lambda *a, **kw: [
        {"triple_id": "t1", "subject": "A", "subject_id": "a", "predicate": "depends_on",
         "object": "B", "object_id": "b", "confidence": 1.0, "valid_from": "2026-01-01",
         "valid_to": None, "source_drawer_id": None}])
    report = dream_contemplate.run(str(native_palace), bootstrap=True)
    assert report["enabled_rule_count"] == 1
    rules = {r["id"]: r for r in dream_palace.load_ontology_config(palace=str(native_palace))}
    assert rules["reviewed"]["enabled"] is True
    assert rules["transitive:depends_on"]["enabled"] is False
    assert not (native_palace / "ontology.json").exists()


def _fresh_readonly_embedding(palace, text, result_queue):
    from unittest.mock import patch
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    with patch.object(ONNXMiniLM_L6_V2, "_download", side_effect=AssertionError("model download")):
        dream_palace.procedural_collection(palace).get(include=["metadatas"])
        result_queue.put(dream_palace._palace_embed(palace, [text]))


def test_sqlite_exact_embedding_after_readonly_lookup_uses_native_vector_space(native_palace):
    text = "Match the configured local palace embedding space."
    result = dream_palace.MempalaceWriter(str(native_palace)).add_drawer("alpha", "notes", text)
    reader = dream_palace.procedural_collection(str(native_palace))
    stored = reader.get(ids=[result["drawer_id"]], include=["embeddings"])["embeddings"][0]
    database = native_palace / "sqlite_exact.sqlite3"
    before = database.read_bytes()
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    process = ctx.Process(target=_fresh_readonly_embedding,
                          args=(str(native_palace), text, result_queue))
    process.start()
    try:
        process.join(timeout=20)
        assert not process.is_alive(), "fresh offline embedding client did not finish"
        assert process.exitcode == 0
        vectors = result_queue.get(timeout=2)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()
    assert len(vectors) == 1
    assert len(vectors[0]) == 384
    assert vectors[0] == pytest.approx(list(stored), abs=1e-7)
    assert database.read_bytes() == before
