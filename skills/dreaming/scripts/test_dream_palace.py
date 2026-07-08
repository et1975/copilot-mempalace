"""Tests for mempalace-facing dreaming adapters that do not import mempalace."""
from __future__ import annotations

import os
import json
import sqlite3
import sys
import tempfile
import types
import unittest

import dream_palace


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-palace-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


class TestLoadActiveTriples(unittest.TestCase):
    def test_missing_kg_returns_empty_list(self):
        with _test_tmpdir() as palace:
            self.assertEqual(dream_palace.load_active_triples(palace), [])

    def test_loads_only_active_triples_as_plain_dicts(self):
        with _test_tmpdir() as palace:
            db_path = os.path.join(palace, "knowledge_graph.sqlite3")
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE triples (
                    id TEXT PRIMARY KEY,
                    subject TEXT,
                    predicate TEXT,
                    object TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    confidence REAL,
                    source_closet TEXT,
                    source_file TEXT,
                    source_drawer_id TEXT,
                    adapter_name TEXT,
                    extracted_at TEXT
                );
                INSERT INTO entities (id, name) VALUES
                    ('e1', 'Alice'), ('e2', 'Portland'), ('e3', 'Seattle');
                INSERT INTO triples (
                    id, subject, predicate, object, valid_from, valid_to,
                    confidence, source_closet, source_file, source_drawer_id,
                    adapter_name, extracted_at
                ) VALUES
                    ('t1', 'e1', 'lives_in', 'e2', '2024-01-01', NULL,
                     1.0, NULL, NULL, NULL, NULL, '2024-01-02'),
                    ('t2', 'e1', 'lives_in', 'e3', '2023-01-01', '2024-01-01',
                     1.0, NULL, NULL, NULL, NULL, '2023-01-02');
                """
            )
            con.commit()
            con.close()

            triples = dream_palace.load_active_triples(palace)

            self.assertEqual(triples, [
                {
                    "triple_id": "t1",
                    "subject": "Alice",
                    "subject_id": "e1",
                    "predicate": "lives_in",
                    "object": "Portland",
                    "object_id": "e2",
                    "valid_from": "2024-01-01",
                    "valid_to": None,
                    "extracted_at": "2024-01-02",
                    "source_drawer_id": None,
                    "confidence": 1.0,
                }
            ])

    def test_returns_entity_ids_to_distinguish_homonyms(self):
        with _test_tmpdir() as palace:
            db_path = os.path.join(palace, "knowledge_graph.sqlite3")
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE triples (
                    id TEXT PRIMARY KEY,
                    subject TEXT,
                    predicate TEXT,
                    object TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    confidence REAL,
                    source_drawer_id TEXT,
                    extracted_at TEXT
                );
                INSERT INTO entities (id, name) VALUES
                    ('person-1', 'Alex'), ('person-2', 'Alex'), ('city-1', 'Paris');
                INSERT INTO triples (
                    id, subject, predicate, object, valid_from, valid_to,
                    confidence, source_drawer_id, extracted_at
                ) VALUES
                    ('t-person-1', 'person-1', 'visited', 'city-1', '2025-01-01', NULL,
                     0.8, 'drawer-a', '2025-01-02'),
                    ('t-person-2', 'person-2', 'visited', 'city-1', '2025-02-01', NULL,
                     0.9, 'drawer-b', '2025-02-02');
                """
            )
            con.commit()
            con.close()

            triples = dream_palace.load_active_triples(palace)

            self.assertEqual([row["subject"] for row in triples], ["Alex", "Alex"])
            self.assertEqual([row["subject_id"] for row in triples], ["person-1", "person-2"])
            self.assertEqual([row["triple_id"] for row in triples], ["t-person-1", "t-person-2"])


class TestKgSourceDegree(unittest.TestCase):
    def test_missing_kg_returns_empty_dict(self):
        with _test_tmpdir() as palace:
            self.assertEqual(dream_palace.kg_source_degree(palace), {})

    def test_counts_triples_by_source_drawer_id(self):
        with _test_tmpdir() as palace:
            db_path = os.path.join(palace, "knowledge_graph.sqlite3")
            con = sqlite3.connect(db_path)
            con.executescript(
                """
                CREATE TABLE triples (
                    id TEXT PRIMARY KEY,
                    subject TEXT,
                    predicate TEXT,
                    object TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    source_drawer_id TEXT
                );
                INSERT INTO triples (id, subject, predicate, object, source_drawer_id) VALUES
                    ('t1', 's', 'p', 'o1', 'drawer-1'),
                    ('t2', 's', 'p', 'o2', 'drawer-1'),
                    ('t3', 's', 'p', 'o3', 'chunk-2'),
                    ('t4', 's', 'p', 'o4', NULL);
                """
            )
            con.commit()
            con.close()

            self.assertEqual(
                dream_palace.kg_source_degree(palace),
                {"drawer-1": 2, "chunk-2": 1},
            )


class TestArchiver(unittest.TestCase):
    def test_archive_then_delete_appends_jsonl_before_deleting_members(self):
        with _test_tmpdir() as td:
            archive_path = os.path.join(td, "cold", "archive.jsonl")
            record = {
                "id": "logical-1",
                "member_ids": ["chunk-1", "chunk-2"],
                "wing": "wing",
                "room": "room",
                "text": "forgettable",
                "salience": {"v": 0.1, "kg_degree": 0},
                "reason": "prune",
            }

            class FakeCollection:
                def get(self, **kwargs):
                    self.last_get = kwargs
                    return {
                        "ids": ["chunk-1", "chunk-2"],
                        "documents": ["first chunk", "second chunk"],
                        "metadatas": [{"chunk_index": 0}, {"chunk_index": 1}],
                        "embeddings": [[1.0, 2.0], [3.0, 4.0]],
                    }

            class FakeWriter:
                def __init__(self):
                    self.deleted = []
                    self.archive_seen_at_delete = []

                def delete_drawer(self, drawer_id):
                    with open(archive_path, encoding="utf-8") as fh:
                        self.archive_seen_at_delete.append(fh.read())
                    self.deleted.append(drawer_id)
                    return {"deleted": drawer_id}

            collection = FakeCollection()
            writer = FakeWriter()
            result = dream_palace.Archiver(
                td, archive_path=archive_path, writer=writer, collection=collection
            ).archive_then_delete(record)

            with open(archive_path, encoding="utf-8") as fh:
                lines = fh.readlines()
            archived = json.loads(lines[0])
            self.assertEqual(archived["schema"], 1)
            self.assertEqual(archived["id"], "logical-1")
            self.assertEqual(archived["member_ids"], ["chunk-1", "chunk-2"])
            self.assertEqual(archived["wing"], "wing")
            self.assertEqual(archived["room"], "room")
            self.assertEqual(archived["salience"], {"v": 0.1, "kg_degree": 0})
            self.assertEqual(archived["reason"], "prune")
            self.assertIn("archived_at", archived)
            self.assertEqual(archived["rows"], [
                {
                    "id": "chunk-1",
                    "document": "first chunk",
                    "metadata": {"chunk_index": 0},
                    "embedding": [1.0, 2.0],
                },
                {
                    "id": "chunk-2",
                    "document": "second chunk",
                    "metadata": {"chunk_index": 1},
                    "embedding": [3.0, 4.0],
                },
            ])
            self.assertEqual(writer.deleted, ["chunk-1", "chunk-2"])
            self.assertEqual(json.loads(writer.archive_seen_at_delete[0])["rows"], archived["rows"])
            self.assertEqual(result, {"archived": "logical-1", "deleted": ["chunk-1", "chunk-2"]})

    def test_archive_failure_does_not_delete(self):
        with _test_tmpdir() as td:
            archive_path = os.path.join(td, "archive-dir")
            os.mkdir(archive_path)

            class FakeCollection:
                def get(self, **kwargs):
                    return {
                        "ids": ["d1"],
                        "documents": ["doc"],
                        "metadatas": [{}],
                        "embeddings": [[]],
                    }

            class FakeWriter:
                def __init__(self):
                    self.deleted = []

                def delete_drawer(self, drawer_id):
                    self.deleted.append(drawer_id)

            writer = FakeWriter()
            archiver = dream_palace.Archiver(td, archive_path=archive_path, writer=writer, collection=FakeCollection())

            with self.assertRaises(IsADirectoryError):
                archiver.archive_then_delete({"id": "d1", "member_ids": ["d1"]})
            self.assertEqual(writer.deleted, [])

    def test_archive_defaults_to_palace_local_path(self):
        with _test_tmpdir() as palace:
            class FakeCollection:
                def get(self, **kwargs):
                    return {
                        "ids": ["d1"],
                        "documents": ["doc"],
                        "metadatas": [{}],
                        "embeddings": [[]],
                    }

            class FakeWriter:
                def __init__(self):
                    self.deleted = []

                def delete_drawer(self, drawer_id):
                    self.deleted.append(drawer_id)

            writer = FakeWriter()
            dream_palace.Archiver(palace, writer=writer, collection=FakeCollection()).archive_then_delete(
                {"id": "d1", "member_ids": ["d1"]}
            )
            self.assertTrue(os.path.exists(os.path.join(palace, "dream-archive.jsonl")))

    def test_missing_member_preflight_raises_without_archiving_or_deleting(self):
        with _test_tmpdir() as palace:
            archive_path = os.path.join(palace, "archive.jsonl")

            class FakeCollection:
                def get(self, **kwargs):
                    return {
                        "ids": ["chunk-1"],
                        "documents": ["first chunk"],
                        "metadatas": [{}],
                        "embeddings": [[]],
                    }

            class FakeWriter:
                def __init__(self):
                    self.deleted = []

                def delete_drawer(self, drawer_id):
                    self.deleted.append(drawer_id)

            writer = FakeWriter()
            archiver = dream_palace.Archiver(
                palace, archive_path=archive_path, writer=writer, collection=FakeCollection()
            )

            with self.assertRaises(ValueError):
                archiver.archive_then_delete({"id": "logical-1", "member_ids": ["chunk-1", "chunk-2"]})
            self.assertEqual(writer.deleted, [])
            self.assertFalse(os.path.exists(archive_path))


class TestLoadObservationEntries(unittest.TestCase):
    def _with_fake_collection(self, ids, documents, metadatas, embeddings):
        original_mempalace = sys.modules.get("mempalace")
        original_palace_module = sys.modules.get("mempalace.palace")
        calls = []

        class FakeCollection:
            def get(self, **kwargs):
                calls.append(kwargs)
                return {
                    "ids": ids,
                    "documents": documents,
                    "metadatas": metadatas,
                    "embeddings": embeddings,
                }

        palace_module = types.ModuleType("mempalace.palace")
        palace_module.get_collection = lambda palace_path: FakeCollection()
        sys.modules["mempalace"] = types.ModuleType("mempalace")
        sys.modules["mempalace.palace"] = palace_module
        return original_mempalace, original_palace_module, calls

    def _restore_fake_collection(self, original_mempalace, original_palace_module):
        if original_mempalace is None:
            sys.modules.pop("mempalace", None)
        else:
            sys.modules["mempalace"] = original_mempalace
        if original_palace_module is None:
            sys.modules.pop("mempalace.palace", None)
        else:
            sys.modules["mempalace.palace"] = original_palace_module

    def test_groups_diary_chunks_by_parent_entry_id(self):
        originals = self._with_fake_collection(
            ids=["chunk-2", "chunk-1"],
            documents=[
                "continued pattern",
                "SESSION_ID: 12345678-abcd-1234-abcd-123456789abc first pattern",
            ],
            metadatas=[
                {
                    "parent_entry_id": "entry-1",
                    "chunk_index": 1,
                    "wing": "wing_copilot-cli",
                    "room": "diary",
                    "agent": "Copilot CLI",
                    "date": "2026-07-03",
                    "topic": "dreaming",
                },
                {
                    "parent_entry_id": "entry-1",
                    "chunk_index": 0,
                    "wing": "wing_copilot-cli",
                    "room": "diary",
                    "agent": "Copilot CLI",
                    "date": "2026-07-03",
                    "topic": "dreaming",
                },
            ],
            embeddings=[[3.0, 5.0], [1.0, 3.0]],
        )
        try:
            entries = dream_palace.load_observation_entries("/palace", wing="wing_copilot-cli")
        finally:
            self._restore_fake_collection(originals[0], originals[1])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["id"], "entry-1")
        self.assertEqual(entries[0]["member_ids"], ["chunk-1", "chunk-2"])
        self.assertEqual(
            entries[0]["text"],
            "SESSION_ID: 12345678-abcd-1234-abcd-123456789abc first pattern\ncontinued pattern",
        )
        self.assertEqual(entries[0]["embedding"], [2.0, 4.0])
        self.assertEqual(entries[0]["session_id"], "12345678-abcd-1234-abcd-123456789abc")
        self.assertEqual(entries[0]["agent"], "Copilot CLI")
        self.assertEqual(entries[0]["date"], "2026-07-03")
        self.assertEqual(entries[0]["topic"], "dreaming")
        self.assertEqual(entries[0]["wing"], "wing_copilot-cli")
        self.assertEqual(entries[0]["room"], "diary")
        self.assertEqual(originals[2][0]["where"], {"$and": [{"wing": "wing_copilot-cli"}, {"room": "diary"}]})

    def test_single_chunk_row_passes_through(self):
        originals = self._with_fake_collection(
            ids=["drawer-1"],
            documents=["SESSION_ID: abcdef12-1111-2222-3333-abcdef123456 one chunk"],
            metadatas=[
                {
                    "wing": "wing_copilot-cli",
                    "room": "diary",
                    "agent": "Copilot CLI",
                    "date": "2026-07-03",
                    "topic": "single",
                }
            ],
            embeddings=[[0.5, 0.25]],
        )
        try:
            entries = dream_palace.load_observation_entries("/palace")
        finally:
            self._restore_fake_collection(originals[0], originals[1])

        self.assertEqual(entries, [
            {
                "id": "drawer-1",
                "member_ids": ["drawer-1"],
                "text": "SESSION_ID: abcdef12-1111-2222-3333-abcdef123456 one chunk",
                "embedding": [0.5, 0.25],
                "session_id": "abcdef12-1111-2222-3333-abcdef123456",
                "agent": "Copilot CLI",
                "date": "2026-07-03",
                "topic": "single",
                "wing": "wing_copilot-cli",
                "room": "diary",
            }
        ])

    def test_session_id_is_none_when_no_session_token(self):
        originals = self._with_fake_collection(
            ids=["legacy-1"],
            documents=["legacy diary entry without a session token"],
            metadatas=[{"wing": "wing_copilot-cli", "room": "diary"}],
            embeddings=[[1.0]],
        )
        try:
            entries = dream_palace.load_observation_entries("/palace")
        finally:
            self._restore_fake_collection(originals[0], originals[1])

        self.assertIsNone(entries[0]["session_id"])

    def test_ambiguous_when_logical_entry_contains_multiple_session_ids(self):
        originals = self._with_fake_collection(
            ids=["chunk-1", "chunk-2"],
            documents=[
                "SESSION_ID: 11111111-1111-1111-1111-111111111111 first",
                "SESSION_ID: 22222222-2222-2222-2222-222222222222 second",
            ],
            metadatas=[
                {"parent_entry_id": "entry-ambiguous", "chunk_index": 0, "wing": "wing_copilot-cli", "room": "diary"},
                {"parent_entry_id": "entry-ambiguous", "chunk_index": 1, "wing": "wing_copilot-cli", "room": "diary"},
            ],
            embeddings=[[1.0], [3.0]],
        )
        try:
            entries = dream_palace.load_observation_entries("/palace")
        finally:
            self._restore_fake_collection(originals[0], originals[1])

        self.assertIsNone(entries[0]["session_id"])
        self.assertTrue(entries[0]["ambiguous"])


class TestKgWriter(unittest.TestCase):
    def test_uses_knowledge_graph_with_palace_relative_db_path(self):
        original_mempalace = sys.modules.get("mempalace")
        original_kg_module = sys.modules.get("mempalace.knowledge_graph")
        calls = []

        class FakeKnowledgeGraph:
            def __init__(self, db_path):
                calls.append(("init", db_path))

            def invalidate(self, subject, predicate, object, ended=None):
                calls.append(("invalidate", subject, predicate, object, ended))
                return {"ok": True}

            def close(self):
                calls.append(("close",))

        mempalace_module = types.ModuleType("mempalace")
        kg_module = types.ModuleType("mempalace.knowledge_graph")
        kg_module.KnowledgeGraph = FakeKnowledgeGraph
        sys.modules["mempalace"] = mempalace_module
        sys.modules["mempalace.knowledge_graph"] = kg_module
        try:
            with _test_tmpdir() as palace:
                writer = dream_palace.KgWriter(palace)
                result = writer.invalidate("Alice", "lives_in", "Portland")
                writer.close()

                self.assertEqual(result, {"ok": True})
                self.assertEqual(calls, [
                    ("init", os.path.join(palace, "knowledge_graph.sqlite3")),
                    ("invalidate", "Alice", "lives_in", "Portland", None),
                    ("close",),
                ])
        finally:
            if original_mempalace is None:
                sys.modules.pop("mempalace", None)
            else:
                sys.modules["mempalace"] = original_mempalace
            if original_kg_module is None:
                sys.modules.pop("mempalace.knowledge_graph", None)
            else:
                sys.modules["mempalace.knowledge_graph"] = original_kg_module

    def test_invalidate_triples_updates_only_requested_ids(self):
        original_mempalace = sys.modules.get("mempalace")
        original_kg_module = sys.modules.get("mempalace.knowledge_graph")

        class FakeKnowledgeGraph:
            def __init__(self, db_path):
                self.db_path = db_path

            def close(self):
                pass

        mempalace_module = types.ModuleType("mempalace")
        kg_module = types.ModuleType("mempalace.knowledge_graph")
        kg_module.KnowledgeGraph = FakeKnowledgeGraph
        sys.modules["mempalace"] = mempalace_module
        sys.modules["mempalace.knowledge_graph"] = kg_module
        try:
            with _test_tmpdir() as palace:
                db_path = os.path.join(palace, "knowledge_graph.sqlite3")
                con = sqlite3.connect(db_path)
                con.executescript(
                    """
                    CREATE TABLE triples (
                        id TEXT PRIMARY KEY,
                        subject TEXT,
                        predicate TEXT,
                        object TEXT,
                        valid_to TEXT
                    );
                    INSERT INTO triples (id, subject, predicate, object, valid_to) VALUES
                        ('t1', 'e1', 'same', 'e2', NULL),
                        ('t2', 'e1', 'same', 'e2', NULL),
                        ('t3', 'e1', 'same', 'e2', 'already-ended');
                    """
                )
                con.commit()
                con.close()

                writer = dream_palace.KgWriter(palace)
                count = writer.invalidate_triples(["t1"], ended="2026-07-06T15:00:00+00:00")
                writer.close()

                con = sqlite3.connect(db_path)
                rows = dict(con.execute("SELECT id, valid_to FROM triples").fetchall())
                con.close()
                self.assertEqual(count, 1)
                self.assertEqual(rows["t1"], "2026-07-06T15:00:00+00:00")
                self.assertIsNone(rows["t2"])
                self.assertEqual(rows["t3"], "already-ended")
        finally:
            if original_mempalace is None:
                sys.modules.pop("mempalace", None)
            else:
                sys.modules["mempalace"] = original_mempalace
            if original_kg_module is None:
                sys.modules.pop("mempalace.knowledge_graph", None)
            else:
                sys.modules["mempalace.knowledge_graph"] = original_kg_module


class TestMempalaceWriter(unittest.TestCase):
    def _with_fake_tools(self, handler):
        original_mempalace = sys.modules.get("mempalace")
        original_mcp_module = sys.modules.get("mempalace.mcp_server")
        mempalace_module = types.ModuleType("mempalace")
        mcp_module = types.ModuleType("mempalace.mcp_server")
        mcp_module.TOOLS = {
            "mempalace_add_drawer": {"handler": handler},
            "mempalace_delete_drawer": {"handler": lambda drawer_id: {"deleted": drawer_id}},
        }
        sys.modules["mempalace"] = mempalace_module
        sys.modules["mempalace.mcp_server"] = mcp_module
        return original_mempalace, original_mcp_module

    def _restore_fake_tools(self, original_mempalace, original_mcp_module):
        if original_mempalace is None:
            sys.modules.pop("mempalace", None)
        else:
            sys.modules["mempalace"] = original_mempalace
        if original_mcp_module is None:
            sys.modules.pop("mempalace.mcp_server", None)
        else:
            sys.modules["mempalace.mcp_server"] = original_mcp_module

    def test_add_drawer_forwards_metadata_when_handler_accepts_it(self):
        calls = []

        def handler(wing, room, content, added_by="dreaming", metadata=None):
            calls.append((wing, room, content, added_by, metadata))
            return {"id": "new-drawer"}

        originals = self._with_fake_tools(handler)
        try:
            result = dream_palace.MempalaceWriter().add_drawer(
                "wing", "room", "content", metadata={"kind": "pattern"}
            )
        finally:
            self._restore_fake_tools(originals[0], originals[1])

        self.assertEqual(result, {"id": "new-drawer"})
        self.assertEqual(calls, [("wing", "room", "content", "dreaming", {"kind": "pattern"})])

    def test_add_drawer_embeds_metadata_trailer_when_handler_does_not_accept_it(self):
        calls = []

        def handler(wing, room, content, added_by="dreaming"):
            calls.append((wing, room, content, added_by))
            return {"id": "new-drawer"}

        originals = self._with_fake_tools(handler)
        try:
            dream_palace.MempalaceWriter().add_drawer(
                "wing", "room", "content", metadata={"supersedes": ["old"], "kind": "merge"}
            )
        finally:
            self._restore_fake_tools(originals[0], originals[1])

        self.assertEqual(calls[0][0:2], ("wing", "room"))
        self.assertTrue(calls[0][2].startswith("content\n\n<!--dreaming-meta: "))
        self.assertIn('"supersedes":["old"]', calls[0][2])


class TestLoadDrawerById(unittest.TestCase):
    def test_returns_reassembled_drawer_with_content_hash(self):
        original_mempalace = sys.modules.get("mempalace")
        original_palace_module = sys.modules.get("mempalace.palace")

        class FakeCollection:
            def get(self, **kwargs):
                if kwargs.get("ids") == ["logical-1"]:
                    return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}
                if kwargs.get("where") == {"parent_drawer_id": "logical-1"}:
                    return {
                        "ids": ["chunk-2", "chunk-1"],
                        "documents": ["second", "first"],
                        "metadatas": [
                            {"parent_drawer_id": "logical-1", "chunk_index": 1, "wing": "wing", "room": "room"},
                            {"parent_drawer_id": "logical-1", "chunk_index": 0, "wing": "wing", "room": "room"},
                        ],
                        "embeddings": [[3.0, 5.0], [1.0, 3.0]],
                    }
                return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

        palace_module = types.ModuleType("mempalace.palace")
        palace_module.get_collection = lambda palace_path: FakeCollection()
        sys.modules["mempalace"] = types.ModuleType("mempalace")
        sys.modules["mempalace.palace"] = palace_module
        try:
            drawer = dream_palace.load_drawer_by_id("/palace", "logical-1")
        finally:
            if original_mempalace is None:
                sys.modules.pop("mempalace", None)
            else:
                sys.modules["mempalace"] = original_mempalace
            if original_palace_module is None:
                sys.modules.pop("mempalace.palace", None)
            else:
                sys.modules["mempalace.palace"] = original_palace_module

        self.assertEqual(drawer["id"], "logical-1")
        self.assertEqual(drawer["text"], "first\nsecond")
        self.assertEqual(drawer["metadata"], {"parent_drawer_id": "logical-1", "chunk_index": 0, "wing": "wing", "room": "room"})
        self.assertEqual(drawer["embedding"], [2.0, 4.0])
        self.assertEqual(
            drawer["content_hash"],
            "4252f8d56b4bb236d0b1bc95a1202e392ca84ce0644bf628398fbb9517287da8",
        )

    def test_returns_none_for_unknown_drawer(self):
        original_mempalace = sys.modules.get("mempalace")
        original_palace_module = sys.modules.get("mempalace.palace")

        class FakeCollection:
            def get(self, **kwargs):
                return {"ids": [], "documents": [], "metadatas": [], "embeddings": []}

        palace_module = types.ModuleType("mempalace.palace")
        palace_module.get_collection = lambda palace_path: FakeCollection()
        sys.modules["mempalace"] = types.ModuleType("mempalace")
        sys.modules["mempalace.palace"] = palace_module
        try:
            self.assertIsNone(dream_palace.load_drawer_by_id("/palace", "missing"))
        finally:
            if original_mempalace is None:
                sys.modules.pop("mempalace", None)
            else:
                sys.modules["mempalace"] = original_mempalace
            if original_palace_module is None:
                sys.modules.pop("mempalace.palace", None)
            else:
                sys.modules["mempalace.palace"] = original_palace_module


class OntologyLoaderTests(unittest.TestCase):
    def test_missing_config_returns_empty(self):
        with _test_tmpdir() as d:
            self.assertEqual(dream_palace.load_ontology_config(os.path.join(d, "none.json")), [])

    def test_loads_wrapped_rules(self):
        with _test_tmpdir() as d:
            p = os.path.join(d, "ontology.json")
            with open(p, "w") as f:
                json.dump({"version": 1, "rules": [{"id": "a", "family": "transitive",
                           "predicate": "depends_on", "enabled": True}]}, f)
            self.assertEqual(dream_palace.load_ontology_config(p)[0]["id"], "a")

    def test_loads_bare_array(self):
        with _test_tmpdir() as d:
            p = os.path.join(d, "ontology.json")
            with open(p, "w") as f:
                json.dump([{"id": "b", "family": "symmetric", "predicate": "x", "enabled": True}], f)
            self.assertEqual(dream_palace.load_ontology_config(p)[0]["id"], "b")

class SkipMarkerIOTests(unittest.TestCase):
    def test_append_then_load_roundtrip(self):
        with _test_tmpdir() as d:
            path = os.path.join(d, "skips.jsonl")
            dream_palace.append_skip_markers(path, [{"candidate_id": "derive:a", "ontology_version": "v"}])
            dream_palace.append_skip_markers(path, [{"candidate_id": "derive:b", "ontology_version": "v"}])
            got = dream_palace.load_skip_markers(path)
            self.assertEqual([m["candidate_id"] for m in got], ["derive:a", "derive:b"])

    def test_load_missing_returns_empty(self):
        with _test_tmpdir() as d:
            self.assertEqual(dream_palace.load_skip_markers(os.path.join(d, "no.jsonl")), [])

class LoaderAliasTests(unittest.TestCase):
    def test_with_ids_alias_is_the_loader(self):
        self.assertIs(dream_palace.load_active_triples_with_ids, dream_palace.load_active_triples)


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False

@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class KgDeriveWriterTests(unittest.TestCase):
    def _seed(self, palace):
        # create entities A, B, C via real add_triple; return their ids
        kg = _RealKG(db_path=os.path.join(palace, "knowledge_graph.sqlite3"))
        kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
        kg.add_triple("B", "depends_on", "C", valid_from="2026-01-01")
        kg.close()
        con = sqlite3.connect(os.path.join(palace, "knowledge_graph.sqlite3"))
        ids = {r[1]: r[0] for r in con.execute("SELECT id,name FROM entities").fetchall()}
        con.close()
        return ids

    def test_add_derived_creates_derivation_row_and_triple(self):
        with _test_tmpdir() as palace:
            ids = self._seed(palace)
            w = dream_palace.KgDeriveWriter(palace)
            try:
                res = w.add_derived(
                    {"subject_id": ids["A"], "predicate": "depends_on_closure", "object_id": ids["C"]},
                    "transitive:depends_on", ["t1", "t2"], ["d1", "d2"], "onto:v", 0.7,
                    "2026-01-01", None)
            finally:
                w.close()
            self.assertIsNotNone(res["triple_id"])
            con = sqlite3.connect(os.path.join(palace, "knowledge_graph.sqlite3"))
            nd = con.execute("SELECT COUNT(*) FROM kg_derivations").fetchone()[0]
            nt = con.execute(
                "SELECT COUNT(*) FROM triples WHERE predicate='depends_on_closure' AND valid_to IS NULL"
            ).fetchone()[0]
            link = con.execute(
                "SELECT conclusion_triple_id FROM kg_derivations").fetchone()[0]
            con.close()
            self.assertEqual((nd, nt), (1, 1))
            self.assertEqual(link, res["triple_id"])  # lineage points at the written triple

    def test_add_derived_is_idempotent_on_same_candidate(self):
        with _test_tmpdir() as palace:
            ids = self._seed(palace)
            w = dream_palace.KgDeriveWriter(palace)
            args = ({"subject_id": ids["A"], "predicate": "depends_on_closure", "object_id": ids["C"]},
                    "r", ["t1", "t2"], ["d1", "d2"], "onto:v", 0.7, "2026-01-01", None)
            try:
                w.add_derived(*args)
                second = w.add_derived(*args)
            finally:
                w.close()
            self.assertTrue(second.get("idempotent"))
            con = sqlite3.connect(os.path.join(palace, "knowledge_graph.sqlite3"))
            nt = con.execute(
                "SELECT COUNT(*) FROM triples WHERE predicate='depends_on_closure'").fetchone()[0]
            nd = con.execute("SELECT COUNT(*) FROM kg_derivations").fetchone()[0]
            con.close()
            self.assertEqual((nt, nd), (1, 1))

    def test_valid_to_is_persisted(self):
        with _test_tmpdir() as palace:
            ids = self._seed(palace)
            w = dream_palace.KgDeriveWriter(palace)
            try:
                w.add_derived(
                    {"subject_id": ids["A"], "predicate": "depends_on_closure", "object_id": ids["C"]},
                    "r", ["t1"], ["d1"], "onto:v", 1.0, "2026-01-01", "2026-05-01")
            finally:
                w.close()
            con = sqlite3.connect(os.path.join(palace, "knowledge_graph.sqlite3"))
            vt = con.execute(
                "SELECT valid_to FROM triples WHERE predicate='depends_on_closure'").fetchone()[0]
            con.close()
            self.assertEqual(vt, "2026-05-01")


if __name__ == "__main__":
    unittest.main()
