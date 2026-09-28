"""Sanctioned-handler boundary, readback, evidence protection and POSIX races."""
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from datetime import timedelta
import errno
from functools import wraps
import json
import multiprocessing
from multiprocessing.managers import SyncManager
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import dream_adopt
import dream_harvest
import dream_palace
from dream_metadata import canonical_json, content_hash
from dream_procedural import parse_event, project_rules, Policy
from test_dream_procedural import NOW, event_data, evidence, resign

SESSION = "11111111-1111-4111-8111-111111111111"
SOURCE_TEXT = f"SESSION_ID: {SESSION}\nobserved result"


@contextmanager
def installed_palace(path):
    """Isolate installed MCP config, audit log and lease files, not storage I/O."""
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    cache = Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH).resolve()
    required = ("config.json", "model.onnx", "special_tokens_map.json",
                "tokenizer_config.json", "tokenizer.json", "vocab.txt")
    if not all((cache / "onnx" / name).is_file() for name in required):
        raise RuntimeError("integration requires the already installed MiniLM cache; no downloads")
    with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as home, \
         patch.dict(os.environ, {"HOME": home, "MEMPALACE_PALACE_PATH": path,
                    "MEMPALACE_BACKEND": "sqlite_exact", "MEMPALACE_BACKEND_EXPLICIT": "sqlite_exact",
                    "MEMPALACE_EMBEDDING_MODEL": "minilm", "MEMPALACE_MCP_READ_ONLY": "0",
                    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "ANONYMIZED_TELEMETRY": "False"}), \
         patch.object(sys, "argv", ["dreaming-integration"]), \
         patch.object(ONNXMiniLM_L6_V2, "_download", side_effect=AssertionError("model download")):
        child_cache = Path(home, ".cache/chroma/onnx_models/all-MiniLM-L6-v2")
        child_cache.parent.mkdir(parents=True)
        child_cache.symlink_to(cache, target_is_directory=True)
        stdout, fd = sys.stdout, os.dup(1)
        try:
            from mempalace import mcp_server, wal
        finally:
            os.dup2(fd, 1)
            os.close(fd)
            sys.stdout = stdout
        from mempalace.config import MempalaceConfig
        from mempalace.palace import get_backend_for_palace
        with patch.object(mcp_server, "_config", MempalaceConfig()), \
             patch.object(mcp_server, "_READ_ONLY", False), \
             patch.object(wal, "_WAL_FILE", Path(home, ".mempalace/wal/write_log.jsonl")):
            try:
                yield mcp_server
            finally:
                mcp_server._release_mcp_writer_lock()
                mcp_server._discard_mcp_storage_handles()
                get_backend_for_palace(path).close_palace(path)


def proposal(number=1):
    return parse_event(event_data("proposal", number, origin_drawer_ids=[],
                                 evidence=[evidence("source", SESSION, SOURCE_TEXT)]))


def matches(meta, where):
    if not where:
        return True
    if "$and" in where:
        return all(matches(meta, clause) for clause in where["$and"])
    if "$or" in where:
        return any(matches(meta, clause) for clause in where["$or"])
    return all(meta.get(key) == value for key, value in where.items())


class DrawerCollection:
    """A shared fake storage boundary with real query/chunk/delete semantics."""
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else {}

    def get(self, *, ids=None, where=None, include=None, limit=None, offset=0):
        rows = [deepcopy(row) for key, row in sorted(self.rows.items())
                if (ids is None or key in ids) and matches(row["metadata"], where)]
        rows = rows[offset:None if limit is None else offset + limit]
        return {"ids": [r["id"] for r in rows], "documents": [r["text"] for r in rows],
                "metadatas": [r["metadata"] for r in rows],
                "embeddings": [r.get("embedding", [1.0, 0.0]) for r in rows]}

    def add(self, wing, room, content, added_by="dreaming", metadata=None, chunk_size=100000):
        parent = "drawer-" + content_hash(content)
        parts = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
        ids = []
        for index, part in enumerate(parts):
            pid = parent if len(parts) == 1 else f"{parent}_chunk_{index:06}"
            ids.append(pid)
            meta = {"wing": wing, "room": room, "added_by": added_by, "chunk_index": index,
                    **(metadata or {})}
            if len(parts) > 1:
                meta["parent_drawer_id"] = parent
            self.rows[pid] = {"id": pid, "text": part, "metadata": meta}
        return {"success": True, "drawer_id": parent, "chunk_ids": ids}

    def delete(self, drawer_id):
        ids = [key for key, row in self.rows.items()
               if key == drawer_id or row["metadata"].get("parent_drawer_id") == drawer_id]
        if not ids:
            return {"success": False, "error": "missing"}
        for key in ids:
            del self.rows[key]
        return {"success": True, "deleted_ids": ids}


def sanctioned_writer(path, collection, *, native=False, chunk_size=100000):
    writer = dream_palace.MempalaceWriter.__new__(dream_palace.MempalaceWriter)
    writer.palace_path = path
    writer.mutation = lambda *args: nullcontext()
    if native:
        def add(wing, room, content, added_by, metadata):
            return collection.add(wing, room, content, added_by, metadata, chunk_size)
    else:
        def add(wing, room, content, added_by):
            return collection.add(wing, room, content, added_by, chunk_size=chunk_size)
    writer._tools = {"mempalace_add_drawer": {"handler": add},
                     "mempalace_delete_drawer": {"handler": collection.delete}}
    return writer


def seed_source(collection):
    collection.rows["source"] = {"id": "source", "text": SOURCE_TEXT,
                                 "metadata": {"wing": "w", "room": "diary"}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="procedural-", dir=os.environ["DREAMING_TEST_TMPDIR"])
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name
        self.collection = DrawerCollection()
        seed_source(self.collection)
        self.reader_patch = patch.object(dream_palace, "procedural_collection",
                                         return_value=self.collection, create=True)
        self.reader_patch.start()
        self.addCleanup(self.reader_patch.stop)
        legacy = patch.object(dream_palace, "protection_collection", return_value=self.collection)
        legacy.start()
        self.addCleanup(legacy.stop)

    def append(self, event=None, **kwargs):
        from dream_procedural_palace import append_event
        return append_event(self.path, "w", event or proposal(),
                            writer=sanctioned_writer(self.path, self.collection, **kwargs))

    def test_native_trailer_and_arbitrary_chunks_roundtrip(self):
        from dream_procedural_palace import read_events
        for native in (True, False):
            with self.subTest(native=native):
                self.collection.rows.clear()
                seed_source(self.collection)
                result = self.append(native=native, chunk_size=173)
                self.assertEqual(result.status, "appended")
                self.assertGreater(len(result.drawer_ids), 1)
                self.assertEqual(read_events(self.path, "w"), [proposal()])
                rows = [row for row in self.collection.rows.values()
                        if row["metadata"]["room"] == "procedural"]
                self.assertTrue(all(r["metadata"]["added_by"] == "dream-procedure" for r in rows))
                text = "".join(r["text"] for r in sorted(rows, key=lambda r: r["metadata"]["chunk_index"]))
                self.assertTrue(text.startswith("Procedural memory record; not an active instruction."))
                self.assertNotIn("proven", text)

    def test_legacy_chroma_safety_lookup_does_not_require_readonly_backend(self):
        from dream_procedural_palace import live_protected_drawer_ids
        with patch.object(dream_palace, "procedural_collection",
                          side_effect=RuntimeError("Chroma cannot be opened strictly read-only")), \
             patch.object(dream_palace, "protection_collection",
                          wraps=lambda path: __import__("mempalace.palace", fromlist=["get_collection"]).get_collection(path)), \
             patch("mempalace.palace.get_collection", return_value=self.collection):
            self.assertEqual(live_protected_drawer_ids(self.path), set())
            kept, errors = dream_adopt._preflight_merge_decisions(self.path, [{"action": "skip"}])
            self.assertEqual(errors, [])
            writer = sanctioned_writer(self.path, self.collection)
            self.assertTrue(writer.delete_drawer("source")["success"])

    def test_retry_before_changed_sources_freshness_and_current_heads(self):
        from dream_procedural_palace import append_event, read_events
        self.append()
        # Acknowledgment can be lost after commit; a retry must not consult new preflight.
        del self.collection.rows["source"]
        writer = sanctioned_writer(self.path, self.collection)
        with patch.object(writer, "add_drawer", side_effect=AssertionError("duplicate write")):
            result = append_event(self.path, "w", proposal(), writer=writer,
                                  preflight=lambda *a: self.fail("preflight ran on committed retry"))
        self.assertEqual(result.status, "already_exists")
        self.assertEqual(len(read_events(self.path, "w")), 1)

    def test_committed_review_retry_precedes_current_head_check(self):
        from dream_procedural_palace import append_event
        self.append()
        packet = event_data("review", 2)["payload"]["validation_packet"]
        packet["evidence"] = [evidence("source", SESSION, SOURCE_TEXT)]
        review = parse_event(event_data("review", 2, validation_packet=packet,
                                        validation_digest=content_hash(canonical_json(packet))))
        self.append(review)
        result = append_event(self.path, "w", review,
                              writer=sanctioned_writer(self.path, self.collection),
                              preflight=lambda *a: self.fail("stale/current-head preflight"))
        self.assertEqual(result.status, "already_exists")
        self.assertEqual(result.projection.rules[0].review_heads, (review.event_id,))

    def test_success_false_and_lost_readback_are_errors_and_same_id_retry_recovers(self):
        from dream_procedural_palace import append_event, read_events
        writer = sanctioned_writer(self.path, self.collection)
        writer._tools["mempalace_add_drawer"]["handler"] = lambda **kw: {
            "success": False, "error": "backend unavailable"}
        with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
            append_event(self.path, "w", proposal(), writer=writer)
        self.assertEqual(read_events(self.path, "w"), [])
        writer = sanctioned_writer(self.path, self.collection)
        handler = writer._tools["mempalace_add_drawer"]["handler"]
        @wraps(handler)
        def interrupted(**kwargs):
            handler(**kwargs)
            raise RuntimeError("acknowledgment lost")
        writer._tools["mempalace_add_drawer"]["handler"] = interrupted
        with self.assertRaisesRegex(RuntimeError, "acknowledgment lost"):
            append_event(self.path, "w", proposal(), writer=writer)
        self.assertEqual(self.append().status, "already_exists")
        self.assertEqual(len(read_events(self.path, "w")), 1)

    def test_success_without_commit_is_not_success(self):
        from dream_procedural_palace import append_event
        writer = sanctioned_writer(self.path, self.collection)
        writer._tools["mempalace_add_drawer"]["handler"] = lambda **kw: {"success": True}
        with self.assertRaisesRegex(RuntimeError, "readback"):
            append_event(self.path, "w", proposal(), writer=writer)

    def test_drift_generated_source_or_missing_source_rejected_before_append(self):
        for changed in (None, {**self.collection.rows["source"], "text": "drift"},
                        {**self.collection.rows["source"], "metadata": {"kind": "reflect"}}):
            with self.subTest(changed=changed):
                if changed is None:
                    self.collection.rows.pop("source", None)
                else:
                    self.collection.rows["source"] = changed
                with self.assertRaises(ValueError):
                    self.append()
                self.assertFalse(any(r["metadata"].get("room") == "procedural"
                                     for r in self.collection.rows.values()))

    def test_incomplete_chunks_conflicting_encoding_and_unknown_version_fail_closed(self):
        from dream_procedural_palace import read_events
        for mutation in ("gap", "version", "conflict"):
            self.collection.rows.clear()
            seed_source(self.collection)
            self.append(chunk_size=173)
            ids = [key for key in self.collection.rows if key != "source"]
            if mutation == "gap":
                del self.collection.rows[ids[1]]
            else:
                row = self.collection.rows[ids[0]]
                row["metadata"][{"version": "schema_version", "conflict": "kind"}[mutation]] = (
                    2 if mutation == "version" else "reflect")
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                read_events(self.path, "w")

    def test_bounded_read_no_writers_or_kg_and_unknown_room_content_rejected(self):
        from dream_procedural_palace import read_events
        self.append()
        self.append(proposal(2))
        with self.assertRaises(ValueError):
            read_events(self.path, "w", max_events=1)
        before = deepcopy(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")):
            self.assertEqual(len(read_events(self.path, "w")), 2)
        self.assertEqual(before, self.collection.rows)
        self.collection.add("w", "procedural", "unrecognized legacy record")
        with self.assertRaises(ValueError):
            read_events(self.path, "w")

    def test_protection_includes_historic_events_origin_sources_and_physical_chunks(self):
        from dream_procedural_palace import live_protected_drawer_ids, protected_drawer_ids
        self.append(chunk_size=173)
        packet = event_data("review", 2)["payload"]["validation_packet"]
        packet["evidence"] = [evidence("source", SESSION, SOURCE_TEXT)]
        retired = parse_event(event_data("review", 2, verdict="retire", validation_packet=packet,
                                        validation_digest=content_hash(canonical_json(packet))))
        self.append(retired)
        self.assertIn("source", protected_drawer_ids([proposal(), retired]))
        protected = live_protected_drawer_ids(self.path)
        self.assertTrue(set(self.collection.rows) <= protected)
        with self.assertRaisesRegex(ValueError, "protected"):
            dream_palace.Archiver(self.path, writer=sanctioned_writer(self.path, self.collection),
                                  collection=self.collection).archive_then_delete(
                                      {"id": "source", "member_ids": ["source"]})
        self.assertFalse(Path(self.path, "dream-archive.jsonl").exists())

    def test_old_merge_and_prune_worklists_cannot_bypass_live_protection(self):
        self.append()
        merge = {"action": "merge", "wing": "w", "room": "diary", "text": "replacement",
                 "supersedes": ["source"], "sources": []}
        kept, errors = dream_adopt._preflight_merge_decisions(self.path, [merge])
        self.assertTrue(errors)
        self.assertEqual(kept, [{"action": "skip"}])
        with patch.object(dream_palace, "kg_protection_degree", return_value={}):
            kept, errors = dream_adopt._preflight_prune_decisions(self.path, [
                {"action": "prune", "id": "source", "member_ids": ["source"]}])
        self.assertTrue(errors)
        self.assertEqual(kept, [{"action": "keep"}])

    def test_harvest_excludes_protected_sources_and_event_drawers(self):
        from dream_procedural_palace import exclude_protected_drawers
        self.append()
        drawers = list(self.collection.rows.values()) + [
            {"id": "ordinary", "text": "other evidence", "metadata": {}}]
        self.assertEqual([d["id"] for d in exclude_protected_drawers(self.path, drawers)], ["ordinary"])

    def test_physical_reference_protects_whole_logical_source(self):
        from dream_procedural_palace import live_protected_drawer_ids
        self.collection.rows["source"]["metadata"].update(parent_drawer_id="logical", chunk_index=0)
        self.collection.rows["second"] = {"id": "second", "text": "more",
                                          "metadata": {"parent_drawer_id": "logical", "chunk_index": 1}}
        ref = evidence("source", SESSION, SOURCE_TEXT + "\nmore")
        self.append(parse_event(event_data(origin_drawer_ids=[], evidence=[ref])))
        self.assertTrue({"logical", "source", "second"} <= live_protected_drawer_ids(self.path))

    def test_unsupported_lock_prevents_mutation(self):
        import fcntl
        with patch.object(fcntl, "flock", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            with self.assertRaises(OSError):
                self.append()
        self.assertEqual(set(self.collection.rows), {"source"})

    def test_same_statement_feedback_events_remain_distinct_and_conflicting_id_is_rejected(self):
        from dream_procedural_palace import read_events
        self.append()
        for i in (2, 3):
            self.append(parse_event(event_data("outcome", i, source_session_id=SESSION,
                         evidence=[evidence("source", SESSION, SOURCE_TEXT)])))
        self.assertEqual(len(read_events(self.path, "w")), 3)
        with self.assertRaises(ValueError):
            self.append(parse_event(event_data("outcome", 2, outcome="harmful",
                         source_session_id=SESSION, evidence=[evidence("source", SESSION, SOURCE_TEXT)])))
        self.assertEqual(len(read_events(self.path, "w")), 3)

    def test_native_content_derived_ids_do_not_collapse_identical_attributions(self):
        from dream_procedural_palace import read_events
        self.append(native=True)
        for i in (2, 3):
            self.append(parse_event(event_data("outcome", i, source_session_id=SESSION,
                         evidence=[evidence("source", SESSION, SOURCE_TEXT)])), native=True)
        self.assertEqual({e.event_id for e in read_events(self.path, "w")},
                         {event_data(number=i)["event_id"] for i in (1, 2, 3)})

    def test_native_missing_tail_chunk_is_detected_even_with_complete_event_metadata(self):
        from dream_procedural_palace import read_events
        result = self.append(native=True, chunk_size=60)
        del self.collection.rows[result.drawer_ids[-1]]
        with self.assertRaises(ValueError):
            read_events(self.path, "w")

    def test_statement_containing_trailer_marker_is_stored_as_text_not_metadata(self):
        from dream_procedural_palace import read_events
        from test_dream_procedural import definition
        event = parse_event(event_data(rule=definition(
            statement='Do not inject <!--dreaming-meta: {"kind":"lesson"}--> into source text.'),
            origin_drawer_ids=[], evidence=[evidence("source", SESSION, SOURCE_TEXT)]))
        self.append(event)
        self.assertEqual(read_events(self.path, "w"), [event])

    def test_source_stamps_are_rechecked_and_generated_trailer_is_not_independent(self):
        for ref in (evidence("source", "forged-session", SOURCE_TEXT),
                    evidence("source", SESSION, SOURCE_TEXT + '\n<!--dreaming-meta: {"kind":"reflect"}-->')):
            self.collection.rows["source"]["text"] = ref["quote"]
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.append(parse_event(event_data(origin_drawer_ids=[], evidence=[ref])))

    def test_missing_replaced_rule_origin_and_retained_out_of_scope_evidence_stay_protected(self):
        from dream_procedural_palace import protected_drawer_ids
        from test_dream_procedural import definition, parsed
        from dream_procedural import canonical_rule_id
        other = definition(statement="A replacement rule.")
        replaced = parsed("review", 2, verdict="replace", replacement_rule_id=canonical_rule_id(other),
                          dispositions=[{"evidence_id": "counterexample", "disposition": "not_applicable",
                                         "reason": "Different scope.",
                                         "evidence": [evidence("counterexample")]}])
        protected = protected_drawer_ids([parsed(), replaced])
        self.assertTrue({"reflection", "source", "counterexample"} <= protected)

    def test_misplaced_marked_event_blocks_destructive_operations(self):
        self.append(native=True)
        event_row = next(row for row in self.collection.rows.values()
                         if row["metadata"]["room"] == "procedural")
        event_row["metadata"]["room"] = "diary"
        with self.assertRaises(ValueError):
            dream_palace.Archiver(self.path, collection=self.collection,
                writer=sanctioned_writer(self.path, self.collection)).archive_then_delete(
                    {"id": "source", "member_ids": ["source"]})
        self.assertIn("source", self.collection.rows)

    def test_real_harvest_and_old_adopt_worklist_protect_sources_before_any_write(self):
        self.append()
        source = {**self.collection.rows["source"], "wing": "w", "room": "diary",
                  "member_ids": ["source"], "embedding": [1., 0.]}
        other = {**source, "id": "other", "member_ids": ["other"], "text": "unrelated"}
        worklist = Path(self.path, "worklist.json")
        with patch.object(dream_palace, "load_logical_drawers", return_value=[source, other]):
            self.assertEqual(dream_harvest.main(["--palace", self.path, "--wing", "w",
                                               "--task", "merge", "--out", str(worklist)]), 0)
        self.assertEqual(json.loads(worklist.read_text())["items"], [])
        old = {"task": "merge", "items": [{"members": [source, other],
                "supersedes": ["source", "other"], "decision": {"action": "merge", "text": "replacement"}}]}
        worklist.write_text(json.dumps(old))
        before = deepcopy(self.collection.rows)
        writer = sanctioned_writer(self.path, self.collection)
        with patch.object(dream_palace, "MempalaceWriter", return_value=writer):
            self.assertEqual(dream_adopt.main(["--palace", self.path,
                                               "--decisions", str(worklist)]), 1)
        self.assertEqual(self.collection.rows, before)

    def test_delete_handler_failure_and_false_acknowledgment_are_not_success(self):
        for response in ({"success": False, "error": "delete refused"}, {"success": True}):
            writer = sanctioned_writer(self.path, self.collection)
            writer._tools["mempalace_delete_drawer"]["handler"] = lambda **kw: response
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                dream_palace.Archiver(self.path, collection=self.collection, writer=writer).archive_then_delete(
                    {"id": "source", "member_ids": ["source"]})
            self.assertIn("source", self.collection.rows)

    def test_sanctioned_writer_delete_also_guards_reflect_rollback_and_direct_callers(self):
        self.append()
        writer = sanctioned_writer(self.path, self.collection)
        with self.assertRaisesRegex(ValueError, "protected"):
            writer.delete_drawer("source")
        self.assertIn("source", self.collection.rows)

    def test_append_readback_detects_backend_replacing_prior_event(self):
        from dream_procedural_palace import append_event
        self.append()
        writer = sanctioned_writer(self.path, self.collection)
        original = writer._tools["mempalace_add_drawer"]["handler"]
        @wraps(original)
        def replace_prior(**kwargs):
            for key, row in list(self.collection.rows.items()):
                if row["metadata"]["room"] == "procedural":
                    del self.collection.rows[key]
            return original(**kwargs)
        writer._tools["mempalace_add_drawer"]["handler"] = replace_prior
        with self.assertRaisesRegex(RuntimeError, "readback"):
            append_event(self.path, "w", proposal(2), writer=writer)

    def test_terminal_heads_can_be_joined_without_resurrecting_rule(self):
        from dream_procedural_palace import _record_body
        from dream_procedural import event_to_data
        self.append()
        packet = event_data("review", 2)["payload"]["validation_packet"]
        packet["evidence"] = [evidence("source", SESSION, SOURCE_TEXT)]
        reviews = [parse_event(event_data("review", i, verdict=verdict, validation_packet=packet,
                              validation_digest=content_hash(canonical_json(packet))))
                   for i, verdict in ((2, "approve"), (3, "retire"))]
        for review in reviews:
            self.collection.add("w", "procedural", _record_body(review), "dream-procedure",
                {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(review)})
        joined = parse_event(event_data("review", 4, verdict="retire",
            parent_review_ids=[r.event_id for r in reviews], validation_packet=packet,
            validation_digest=content_hash(canonical_json(packet))))
        result = self.append(joined)
        self.assertEqual(result.projection.rules[0].review_heads, (joined.event_id,))
        self.assertFalse(result.projection.rules[0].eligible)
        self.assertIn("retired", result.projection.rules[0].suppression_reasons)


class InstalledPalaceTests(unittest.TestCase):
    def test_read_scope_sees_live_uncheckpointed_wal_without_application_writes(self):
        from dream_procedural_palace import nonmutating_read
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            ok, reason = server._acquire_mcp_writer_lock()
            self.assertTrue(ok, reason)
            writer = server._get_collection(create=True)
            writer._handle.conn.execute("PRAGMA wal_autocheckpoint=0")
            result = server.tool_add_drawer("w", "diary", SOURCE_TEXT)
            self.assertTrue(result["success"], result)
            db = Path(path, "sqlite_exact.sqlite3")
            wal = Path(str(db) + "-wal")
            self.assertGreater(wal.stat().st_size, 32)
            before = (db.read_bytes(), wal.read_bytes(), tuple(writer._handle.conn.iterdump()))
            owner = server._MCP_WRITER_LOCK_CM
            with nonmutating_read(path):
                reader = dream_palace.procedural_collection(path)
                self.assertFalse(reader._handle.immutable)
                self.assertEqual(dream_palace.load_source_drawer(
                    path, result["drawer_id"])["text"], SOURCE_TEXT)
                for sql in ("DELETE FROM documents", "CREATE TABLE forbidden(x)",
                            "CREATE TEMP TABLE forbidden_temp(x)"):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError):
                        reader._handle.conn.execute(sql)
            self.assertEqual((db.read_bytes(), wal.read_bytes(),
                              tuple(writer._handle.conn.iterdump())), before)
            self.assertIs(server._MCP_WRITER_LOCK_CM, owner)
            self.assertFalse(writer._handle.closed)
            latest = server.tool_add_drawer("w", "diary", "committed after the first read")
            self.assertTrue(latest["success"], latest)
            with nonmutating_read(path):
                self.assertEqual(dream_palace.load_source_drawer(path, latest["drawer_id"])["text"],
                                 "committed after the first read")

    def test_read_scope_rejects_incomplete_wal_pair_even_after_clean_reader_cached(self):
        from dream_procedural_palace import nonmutating_read
        from mempalace.palace import get_collection, get_backend_for_palace
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path):
            get_collection(path, backend="sqlite_exact", create=True)
            get_backend_for_palace(path).close_palace(path)
            dream_palace.procedural_collection(path)
            wal = Path(path, "sqlite_exact.sqlite3-wal")
            wal.write_bytes(b"incomplete WAL fixture")
            with self.assertRaisesRegex(RuntimeError, "incomplete WAL"):
                with nonmutating_read(path):
                    dream_palace.procedural_collection(path)
            with self.assertRaisesRegex(RuntimeError, "incomplete WAL"):
                dream_palace.procedural_collection(path)
            self.assertEqual(wal.read_bytes(), b"incomplete WAL fixture")

    def test_writer_promotes_cached_reader_and_releases_ownership_after_each_call(self):
        from mempalace.palace import get_collection, get_backend_for_palace
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            get_collection(path, backend="sqlite_exact", create=True)
            get_backend_for_palace(path).close_palace(path)
            self.assertIsNotNone(server._get_collection())
            writer = dream_palace.MempalaceWriter()
            result = writer.add_drawer("w", "diary", SOURCE_TEXT)
            self.assertEqual(dream_palace.load_source_drawer(path, result["drawer_id"])["text"], SOURCE_TEXT)
            self.assertIsNone(server._MCP_WRITER_LOCK_CM)
            writer.delete_drawer(result["drawer_id"])
            self.assertIsNone(dream_palace.load_source_drawer(path, result["drawer_id"]))
            self.assertIsNone(server._MCP_WRITER_LOCK_CM)

    def test_writer_failure_releases_new_lease_but_preserves_borrowed_owner(self):
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            writer = dream_palace.MempalaceWriter()
            with self.assertRaisesRegex(RuntimeError, "add_drawer failed"):
                writer.add_drawer("", "diary", SOURCE_TEXT)
            self.assertIsNone(server._MCP_WRITER_LOCK_CM)
            with writer.mutation():
                owner = server._MCP_WRITER_LOCK_CM
                with self.assertRaisesRegex(RuntimeError, "add_drawer failed"):
                    writer.add_drawer("", "diary", SOURCE_TEXT)
                self.assertIs(server._MCP_WRITER_LOCK_CM, owner)
            self.assertIsNone(server._MCP_WRITER_LOCK_CM)

    def test_archiver_readback_survives_writer_handle_retirement_between_calls(self):
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            writer = dream_palace.MempalaceWriter()
            ids = [writer.add_drawer("w", "diary", text)["drawer_id"]
                   for text in ("first original observation", "second original observation")]
            archiver = dream_palace.Archiver(path, writer=writer)
            for source_id in ids:
                self.assertEqual(archiver.archive_then_delete({"id": source_id})["deleted"], [source_id])
                self.assertIsNone(server._MCP_WRITER_LOCK_CM)
                self.assertIsNone(dream_palace.load_source_drawer(path, source_id))
            archive = [json.loads(line) for line in Path(path, "dream-archive.jsonl").read_text().splitlines()]
            self.assertEqual([item["id"] for item in archive], ids)

    def test_throwaway_palace_roundtrip_reopen_without_application_schema_growth(self):
        from dream_procedural_palace import append_event, read_events
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path):
            from mempalace.palace import get_collection, get_backend_for_palace
            get_collection(path, backend="sqlite_exact", create=True)
            def schema():
                col = dream_palace.procedural_collection(path)
                return col._handle.conn.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            before_schema = schema()
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                source = writer.add_drawer("w", "diary", SOURCE_TEXT)["drawer_id"]
                event = parse_event(event_data(origin_drawer_ids=[],
                                    evidence=[evidence(source, SESSION, SOURCE_TEXT)]))
                append_event(path, "w", event, writer=writer)
                for i in (2, 3):
                    append_event(path, "w", parse_event(event_data("outcome", i, source_session_id=SESSION,
                        evidence=[evidence(source, SESSION, SOURCE_TEXT)])), writer=writer)
                self.assertEqual(append_event(path, "w", event, writer=writer).status, "already_exists")
            events = read_events(path, "w")
            self.assertEqual(len(events), 3)
            self.assertEqual(schema(), before_schema)
            backend = get_backend_for_palace(path)
            backend.close_palace(path)
            before_files = {p.name: p.read_bytes() for p in Path(path).iterdir()
                            if p.is_file() and not p.name.endswith("-shm")}
            reopened = read_events(path, "w")
            self.assertEqual(project_rules(events, as_of=NOW, policy=Policy()),
                             project_rules(reopened, as_of=NOW, policy=Policy()))
            after_files = {p.name: p.read_bytes() for p in Path(path).iterdir()
                           if p.is_file() and not p.name.endswith("-shm")}
            self.assertEqual(after_files, before_files)
            backend.close_palace(path)


def _race_worker(path, rows, action, ready, release, queue, first):
    """Forked test actor; real package lock/append/archive, shared fake drawer I/O."""
    from dream_procedural_palace import append_event
    collection = DrawerCollection(rows)
    writer = sanctioned_writer(path, collection)
    try:
        with patch.object(dream_palace, "procedural_collection", return_value=collection), \
             patch.object(dream_palace, "protection_collection", return_value=collection):
            if first:
                with dream_palace.palace_mutation_lock(path):
                    ready.set()
                    if not release.wait(4):
                        raise RuntimeError("test synchronization timeout")
                    if action == "append":
                        append_event(path, "w", proposal(), writer=writer)
                    else:
                        dream_palace.Archiver(path, writer=writer, collection=collection).archive_then_delete(
                            {"id": "source", "member_ids": ["source"]})
            elif action == "append":
                append_event(path, "w", proposal(), writer=writer)
            else:
                dream_palace.Archiver(path, writer=writer, collection=collection).archive_then_delete(
                    {"id": "source", "member_ids": ["source"]})
        queue.put((action, "committed"))
    except (ValueError, RuntimeError, OSError) as exc:
        queue.put((action, str(exc)))


def _hold_lock(path, ready, release):
    with dream_palace.palace_mutation_lock(path):
        ready.set()
        if not release.wait(10):
            raise RuntimeError("test lock release timeout")


class ProcessLockTests(unittest.TestCase):
    def test_canonical_directory_lock_times_out_at_five_seconds_and_creates_no_file(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path:
            alias = path + "-alias"
            os.symlink(path, alias)
            self.addCleanup(lambda: os.unlink(alias))
            ready, release = ctx.Event(), ctx.Event()
            process = ctx.Process(target=_hold_lock, args=(path, ready, release))
            process.start()
            try:
                self.assertTrue(ready.wait(3))
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    with dream_palace.palace_mutation_lock(alias):
                        self.fail("unlocked")
                self.assertGreaterEqual(time.monotonic() - started, 4.9)
                self.assertLess(time.monotonic() - started, 6)
                self.assertEqual(os.listdir(path), [])
            finally:
                release.set()
                process.join(3)
                if process.is_alive():
                    process.terminate()
                    process.join()
            self.assertEqual(process.exitcode, 0)

    def test_both_proposal_prune_interleavings_preserve_referential_integrity(self):
        from dream_procedural_palace import read_events
        ctx = multiprocessing.get_context("spawn")
        with SyncManager(address=("127.0.0.1", 0), ctx=ctx) as manager:
            for first in ("append", "prune"):
                with self.subTest(first=first), tempfile.TemporaryDirectory(
                        dir=os.environ["DREAMING_TEST_TMPDIR"]) as path:
                    rows = manager.dict()
                    collection = DrawerCollection(rows)
                    seed_source(collection)
                    ready, release, queue = ctx.Event(), ctx.Event(), ctx.Queue()
                    other = "prune" if first == "append" else "append"
                    a = ctx.Process(target=_race_worker, args=(path, rows, first, ready, release, queue, True))
                    b = ctx.Process(target=_race_worker, args=(path, rows, other, ready, release, queue, False))
                    a.start()
                    try:
                        self.assertTrue(ready.wait(3))
                        b.start()
                        release.set()
                        a.join(5)
                        b.join(5)
                        self.assertEqual((a.exitcode, b.exitcode), (0, 0))
                        results = dict([queue.get(timeout=2), queue.get(timeout=2)])
                        self.assertEqual(results[first], "committed")
                        self.assertNotEqual(results[other], "committed")
                        with patch.object(dream_palace, "procedural_collection", return_value=collection):
                            events = read_events(path, "w")
                        self.assertEqual(len(events), 1 if first == "append" else 0)
                        self.assertEqual("source" in rows, first == "append")
                    finally:
                        release.set()
                        for process in (a, b):
                            if process.pid is not None and process.is_alive():
                                process.terminate()
                                process.join()
                        queue.close()
