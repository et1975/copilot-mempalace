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


class TestLoadActiveTriplesKgPathResolution(unittest.TestCase):
    def _make_palace(self, root):
        palace = os.path.join(root, "palace")
        os.mkdir(palace)
        return palace

    def _write_kg(self, db_path, triple_id, object_name):
        con = sqlite3.connect(db_path)
        con.executescript(
            f"""
            CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject INTEGER,
                predicate TEXT,
                object INTEGER,
                valid_from TEXT,
                valid_to TEXT,
                extracted_at TEXT,
                source_drawer_id TEXT,
                confidence REAL
            );
            INSERT INTO entities (id, name) VALUES (1, 'Alice'), (2, '{object_name}');
            INSERT INTO triples (
                id, subject, predicate, object, valid_from, valid_to,
                extracted_at, source_drawer_id, confidence
            ) VALUES (
                '{triple_id}', 1, 'lives_in', 2, '2026-01-01', NULL,
                '2026-01-02', 'drawer-{triple_id}', 0.75
            );
            """
        )
        con.commit()
        con.close()

    def _assert_single_triple(self, triples, triple_id, object_name):
        self.assertEqual(triples, [
            {
                "triple_id": triple_id,
                "subject": "Alice",
                "subject_id": 1,
                "predicate": "lives_in",
                "object": object_name,
                "object_id": 2,
                "valid_from": "2026-01-01",
                "valid_to": None,
                "extracted_at": "2026-01-02",
                "source_drawer_id": f"drawer-{triple_id}",
                "confidence": 0.75,
            }
        ])

    def test_palace_local_kg_is_loaded(self):
        with _test_tmpdir() as root:
            palace = self._make_palace(root)
            self._write_kg(os.path.join(palace, "knowledge_graph.sqlite3"), "local", "Portland")

            triples = dream_palace.load_active_triples(palace)

            self._assert_single_triple(triples, "local", "Portland")

    def test_home_level_kg_is_loaded_when_palace_local_is_absent(self):
        with _test_tmpdir() as root:
            palace = self._make_palace(root)
            self._write_kg(os.path.join(root, "knowledge_graph.sqlite3"), "home", "Seattle")

            triples = dream_palace.load_active_triples(palace)

            self._assert_single_triple(triples, "home", "Seattle")

    def test_palace_local_kg_wins_when_both_exist(self):
        with _test_tmpdir() as root:
            palace = self._make_palace(root)
            self._write_kg(os.path.join(root, "knowledge_graph.sqlite3"), "home", "Seattle")
            self._write_kg(os.path.join(palace, "knowledge_graph.sqlite3"), "local", "Portland")

            triples = dream_palace.load_active_triples(palace)

            self._assert_single_triple(triples, "local", "Portland")

    def test_missing_kg_returns_empty_list(self):
        with _test_tmpdir() as root:
            palace = self._make_palace(root)

            self.assertEqual(dream_palace.load_active_triples(palace), [])


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

    def test_uses_home_level_db_path_when_palace_local_db_is_absent(self):
        original_mempalace = sys.modules.get("mempalace")
        original_kg_module = sys.modules.get("mempalace.knowledge_graph")
        calls = []

        class FakeKnowledgeGraph:
            def __init__(self, db_path):
                calls.append(("init", db_path))

            def close(self):
                calls.append(("close",))

        mempalace_module = types.ModuleType("mempalace")
        kg_module = types.ModuleType("mempalace.knowledge_graph")
        kg_module.KnowledgeGraph = FakeKnowledgeGraph
        sys.modules["mempalace"] = mempalace_module
        sys.modules["mempalace.knowledge_graph"] = kg_module
        try:
            with _test_tmpdir() as root:
                palace = os.path.join(root, "palace")
                os.mkdir(palace)
                home_kg = os.path.join(root, "knowledge_graph.sqlite3")
                sqlite3.connect(home_kg).close()

                writer = dream_palace.KgWriter(palace)
                writer.close()

                self.assertEqual(calls, [("init", home_kg), ("close",)])
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


class TestKgDeriveWriterPathResolution(unittest.TestCase):
    def test_uses_home_level_db_path_when_palace_local_db_is_absent(self):
        original_mempalace = sys.modules.get("mempalace")
        original_kg_module = sys.modules.get("mempalace.knowledge_graph")
        calls = []

        class FakeKnowledgeGraph:
            def __init__(self, db_path):
                calls.append(("init", db_path))

            def close(self):
                calls.append(("close",))

        mempalace_module = types.ModuleType("mempalace")
        kg_module = types.ModuleType("mempalace.knowledge_graph")
        kg_module.KnowledgeGraph = FakeKnowledgeGraph
        sys.modules["mempalace"] = mempalace_module
        sys.modules["mempalace.knowledge_graph"] = kg_module
        try:
            with _test_tmpdir() as root:
                palace = os.path.join(root, "palace")
                os.mkdir(palace)
                home_kg = os.path.join(root, "knowledge_graph.sqlite3")
                sqlite3.connect(home_kg).close()

                writer = dream_palace.KgDeriveWriter(palace)
                writer.close()

                self.assertEqual(calls, [("init", home_kg), ("close",)])
                self.assertTrue(os.path.exists(home_kg))
                self.assertFalse(os.path.exists(os.path.join(palace, "knowledge_graph.sqlite3")))
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


class TestStripContextBoilerplate(unittest.TestCase):
    def test_removes_paired_skill_context_block(self):
        text = 'review and implement <skill-context name="using-superpowers">Base directory: /home/x</skill-context> now'
        self.assertEqual(dream_palace._strip_context_boilerplate(text), "review and implement now")

    def test_removes_unclosed_trailing_skill_context(self):
        text = 'what does this repo lack <skill-context name="using-superpowers"> Base directory for this skill: /home'
        self.assertEqual(dream_palace._strip_context_boilerplate(text), "what does this repo lack")

    def test_removes_system_reminder_and_hook_lines(self):
        text = (
            "fix the bug\n"
            "<system_reminder>sql tables: todos</system_reminder>\n"
            "[palace-reflex] About to call 'Grep' without a recent mempalace_search"
        )
        self.assertEqual(dream_palace._strip_context_boilerplate(text), "fix the bug")

    def test_empty_and_none_safe(self):
        self.assertEqual(dream_palace._strip_context_boilerplate(""), "")
        self.assertEqual(dream_palace._strip_context_boilerplate(None), "")


class TestResolveEmbedFn(unittest.TestCase):
    def test_prefers_public_then_falls_back_to_inner_private(self):
        pub = types.SimpleNamespace(embedding_function=lambda xs: [[1.0] for _ in xs])
        self.assertTrue(callable(dream_palace._resolve_embed_fn(pub)))

        inner = types.SimpleNamespace(_embedding_function=lambda xs: [[2.0] for _ in xs])
        wrapper = types.SimpleNamespace(_collection=inner)
        self.assertIs(dream_palace._resolve_embed_fn(wrapper), inner._embedding_function)

    def test_raises_when_unresolvable(self):
        with self.assertRaises(RuntimeError):
            dream_palace._resolve_embed_fn(types.SimpleNamespace())


class TestLoadSessionObservationEntries(unittest.TestCase):
    def _install_fake_sessions(self, observations):
        fake = types.ModuleType("dream_sessions")
        fake.load_session_observations = lambda **kwargs: observations
        fake._calls = []
        original_load = fake.load_session_observations

        def _record(**kwargs):
            fake._calls.append(kwargs)
            return original_load(**kwargs)

        fake.load_session_observations = _record
        sys.modules["dream_sessions"] = fake
        self.addCleanup(lambda: sys.modules.pop("dream_sessions", None))
        return fake

    def test_builds_embedded_entries_and_strips_boilerplate(self):
        observations = [
            {
                "session_id": "sid-1",
                "repository": "copilot-mempalace",
                "created_at": "2026-07-01",
                "summary": "packaging",
                "text": 'make this a marketplace skillset <skill-context name="x">noise</skill-context>',
                "turn_count": 3,
            },
        ]
        self._install_fake_sessions(observations)
        captured = {}

        def fake_embed(palace, texts):
            captured["texts"] = list(texts)
            return [[0.5] * 4 for _ in texts]

        original_embed = dream_palace._palace_embed
        dream_palace._palace_embed = fake_embed
        self.addCleanup(lambda: setattr(dream_palace, "_palace_embed", original_embed))

        entries = dream_palace.load_session_observation_entries(
            "/bound", repository="copilot-mempalace", since="2026-07-01", limit_sessions=10
        )

        self.assertEqual(captured["texts"], ["make this a marketplace skillset"])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "session:sid-1")
        self.assertEqual(entry["member_ids"], ["session:sid-1"])
        self.assertEqual(entry["session_id"], "sid-1")
        self.assertEqual(entry["room"], "__session__")
        self.assertEqual(entry["topic"], "packaging")
        self.assertEqual(entry["date"], "2026-07-01")
        self.assertEqual(entry["embedding"], [0.5, 0.5, 0.5, 0.5])
        self.assertNotIn("skill-context", entry["text"])
        self.assertEqual(sys.modules["dream_sessions"]._calls, [
            {"repository": "copilot-mempalace", "since": "2026-07-01", "limit_sessions": 10}
        ])

    def test_drops_sessions_that_are_pure_boilerplate(self):
        observations = [
            {"session_id": "keep", "created_at": "d", "summary": "s", "text": "real user intent"},
            {"session_id": "drop", "created_at": "d", "summary": "s",
             "text": '<skill-context name="x">only framework noise</skill-context>'},
        ]
        self._install_fake_sessions(observations)
        original_embed = dream_palace._palace_embed
        dream_palace._palace_embed = lambda palace, texts: [[1.0] for _ in texts]
        self.addCleanup(lambda: setattr(dream_palace, "_palace_embed", original_embed))

        entries = dream_palace.load_session_observation_entries("/bound")
        self.assertEqual([e["session_id"] for e in entries], ["keep"])

    def test_no_observations_returns_empty(self):
        self._install_fake_sessions([])
        called = {"embed": False}

        def fake_embed(palace, texts):
            called["embed"] = True
            return []

        original_embed = dream_palace._palace_embed
        dream_palace._palace_embed = fake_embed
        self.addCleanup(lambda: setattr(dream_palace, "_palace_embed", original_embed))

        self.assertEqual(dream_palace.load_session_observation_entries("/bound"), [])
        self.assertFalse(called["embed"])


class TestRetrieveRelevantSessionObservations(unittest.TestCase):
    def _install_fake_retrieval_inputs(self, entries, query_embedding):
        original_load = dream_palace.load_session_observation_entries
        original_embed = dream_palace._palace_embed
        calls = {"load": [], "embed": []}

        def fake_load(palace, **kwargs):
            calls["load"].append((palace, kwargs))
            return entries

        def fake_embed(palace, texts):
            calls["embed"].append((palace, list(texts)))
            return [query_embedding]

        dream_palace.load_session_observation_entries = fake_load
        dream_palace._palace_embed = fake_embed
        self.addCleanup(lambda: setattr(dream_palace, "load_session_observation_entries", original_load))
        self.addCleanup(lambda: setattr(dream_palace, "_palace_embed", original_embed))
        return calls

    def test_returns_relevance_ranked_entries_with_k_cap(self):
        entries = [
            {"id": "middle", "member_ids": ["middle"], "text": "b", "embedding": [0.0, 1.0],
             "session_id": "sid-b", "agent": None, "date": "2026-07-02", "topic": "b", "wing": None, "room": "__session__"},
            {"id": "best", "member_ids": ["best"], "text": "a", "embedding": [1.0, 0.0],
             "session_id": "sid-a", "agent": None, "date": "2026-07-01", "topic": "a", "wing": None, "room": "__session__"},
            {"id": "worst", "member_ids": ["worst"], "text": "c", "embedding": [-1.0, 0.0],
             "session_id": "sid-c", "agent": None, "date": "2026-07-03", "topic": "c", "wing": None, "room": "__session__"},
        ]
        calls = self._install_fake_retrieval_inputs(entries, [1.0, 0.0])

        results = dream_palace.retrieve_relevant_session_observations(
            "/bound",
            "query text",
            k=2,
            repository="copilot-mempalace",
            since="2026-07-01",
            limit_sessions=20,
        )

        self.assertEqual([entry["id"] for entry in results], ["best", "middle"])
        self.assertEqual(calls["embed"], [("/bound", ["query text"])])
        self.assertEqual(calls["load"], [
            ("/bound", {"repository": "copilot-mempalace", "since": "2026-07-01", "limit_sessions": 20})
        ])

    def test_min_similarity_drops_low_matches(self):
        entries = [
            {"id": "keep", "member_ids": ["keep"], "text": "a", "embedding": [1.0, 0.0],
             "session_id": "sid-a", "agent": None, "date": None, "topic": None, "wing": None, "room": "__session__"},
            {"id": "drop", "member_ids": ["drop"], "text": "b", "embedding": [0.0, 1.0],
             "session_id": "sid-b", "agent": None, "date": None, "topic": None, "wing": None, "room": "__session__"},
        ]
        self._install_fake_retrieval_inputs(entries, [1.0, 0.0])

        results = dream_palace.retrieve_relevant_session_observations("/bound", "", min_similarity=0.5)

        self.assertEqual([entry["id"] for entry in results], ["keep"])
        self.assertAlmostEqual(results[0]["similarity"], 1.0)

    def test_single_session_match_does_not_require_recurrence_support(self):
        entries = [
            {"id": "only", "member_ids": ["only"], "text": "single support", "embedding": [1.0, 0.0],
             "session_id": "sid-only", "agent": None, "date": None, "topic": None, "wing": None, "room": "__session__"},
        ]
        self._install_fake_retrieval_inputs(entries, [1.0, 0.0])

        results = dream_palace.retrieve_relevant_session_observations("/bound", "single", k=1)

        self.assertEqual([entry["id"] for entry in results], ["only"])

    def test_empty_entries_and_non_positive_k_return_empty(self):
        calls = self._install_fake_retrieval_inputs([], [1.0, 0.0])

        self.assertEqual(dream_palace.retrieve_relevant_session_observations("/bound", "anything"), [])
        self.assertEqual(dream_palace.retrieve_relevant_session_observations("/bound", "anything", k=0), [])
        self.assertEqual(calls["embed"], [])

    def test_adds_numeric_similarity_without_mutating_source_entries(self):
        entries = [
            {"id": "zero", "member_ids": ["zero"], "text": "zero", "embedding": [],
             "session_id": "sid-zero", "agent": None, "date": None, "topic": None, "wing": None, "room": "__session__"},
            {"id": "match", "member_ids": ["match"], "text": "match", "embedding": [1.0, 0.0],
             "session_id": "sid-match", "agent": None, "date": None, "topic": None, "wing": None, "room": "__session__"},
        ]
        self._install_fake_retrieval_inputs(entries, [1.0, 0.0])

        results = dream_palace.retrieve_relevant_session_observations("/bound", "match", k=2)

        self.assertEqual([entry["id"] for entry in results], ["match", "zero"])
        self.assertTrue(all(isinstance(entry["similarity"], float) for entry in results))
        self.assertEqual(results[1]["similarity"], 0.0)
        self.assertNotIn("similarity", entries[0])
        self.assertNotIn("similarity", entries[1])
        self.assertIsNot(results[0], entries[1])


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class EpistemicFirewallB10AcceptanceTests(unittest.TestCase):
    def _kg_path(self, palace):
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _load_json(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _dump_json(self, path, value):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh)

    def _write_transitive_ontology(self, palace):
        with open(os.path.join(palace, "ontology.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "version": 1,
                "rules": [{
                    "id": "transitive:depends_on",
                    "family": "transitive",
                    "predicate": "depends_on",
                    "enabled": True,
                    "max_depth": 3,
                }],
            }, fh)

    def _chain_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        kg = _RealKG(db_path=self._kg_path(palace))
        try:
            premise_ab = str(kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01"))
            premise_bc = str(kg.add_triple("B", "depends_on", "C", valid_from="2026-01-01"))
        finally:
            kg.close()
        self._write_transitive_ontology(palace)
        return palace, [premise_ab, premise_bc]

    def _gaps_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        kg = _RealKG(db_path=self._kg_path(palace))
        try:
            kg.add_triple("A", "depends_on", "B", valid_from="2026-01-01")
            kg.add_triple("C", "depends_on", "D", valid_from="2026-01-01")
        finally:
            kg.close()
        self._write_transitive_ontology(palace)
        return palace

    def _harvest(self, task, palace, out):
        import dream_harvest

        rc = dream_harvest.main(["--task", task, "--palace", palace, "--out", out])
        self.assertEqual(rc, 0)
        return self._load_json(out)

    def _adopt_derive(self, palace, decisions_path):
        import dream_adopt

        rc = dream_adopt.main([
            "--task", "derive",
            "--palace", palace,
            "--decisions", decisions_path,
        ])
        self.assertEqual(rc, 0)

    def _materialize_first_derive_candidate(self, td, palace):
        out = os.path.join(td, "derive-worklist.json")
        worklist = self._harvest("derive", palace, out)
        self.assertEqual(len(worklist["items"]), 1)
        worklist["items"][0]["action"] = "materialize"
        decisions_path = os.path.join(td, "derive-decisions.json")
        self._dump_json(decisions_path, worklist)
        self._adopt_derive(palace, decisions_path)
        return worklist, decisions_path

    def _conclusion_triple_id(self, palace):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            rows = con.execute(
                """
                SELECT t.id
                FROM triples t
                JOIN entities s ON t.subject = s.id
                JOIN entities o ON t.object = o.id
                WHERE s.name = 'A'
                  AND t.predicate = 'depends_on_closure'
                  AND o.name = 'C'
                  AND t.valid_to IS NULL
                """
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        return str(rows[0][0])

    def _support_rows(self, palace, triple_id):
        con = sqlite3.connect(self._kg_path(palace))
        con.row_factory = sqlite3.Row
        try:
            return [
                dict(row)
                for row in con.execute(
                    """
                    SELECT status, source_trust, inherited_status,
                           conditional_on_triple_ids, scope, source_kind, source_ref
                    FROM kg_triple_supports
                    WHERE triple_id = ?
                    ORDER BY support_id
                    """,
                    (triple_id,),
                ).fetchall()
            ]
        finally:
            con.close()

    def _derivation_premise_ids(self, palace, conclusion_triple_id):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            rows = con.execute(
                """
                SELECT p.premise_triple_id
                FROM kg_derivation_premises p
                JOIN kg_derivations d ON p.derivation_id = d.id
                WHERE d.conclusion_triple_id = ?
                ORDER BY p.premise_triple_id
                """,
                (conclusion_triple_id,),
            ).fetchall()
        finally:
            con.close()
        return [str(row[0]) for row in rows]

    def _sidecar_counts(self, palace):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            return {
                "supports": con.execute("SELECT COUNT(*) FROM kg_triple_supports").fetchone()[0],
                "premises": con.execute("SELECT COUNT(*) FROM kg_derivation_premises").fetchone()[0],
            }
        finally:
            con.close()

    def test_schema_creation_is_idempotent_and_creates_firewall_sidecars(self):
        with _test_tmpdir() as td:
            palace = os.path.join(td, "palace")
            os.makedirs(palace)
            db_path = self._kg_path(palace)
            kg = _RealKG(db_path=db_path)
            kg.close()

            ensure_schema = getattr(dream_palace, "ensure_firewall_schema", None)
            if ensure_schema is not None:
                ensure_schema(db_path)
                ensure_schema(db_path)
            else:
                first = dream_palace.KgDeriveWriter(palace)
                first.close()
                second = dream_palace.KgDeriveWriter(palace)
                second.close()

            con = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                indexes = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                support_cols = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(kg_triple_supports)").fetchall()
                }
            finally:
                con.close()

        self.assertTrue({
            "kg_triple_supports",
            "kg_derivation_premises",
            "kg_firewall_meta",
        }.issubset(tables))
        self.assertTrue({
            "support_id",
            "triple_id",
            "status",
            "source_trust",
            "inherited_status",
            "conditional_on_triple_ids",
            "scope",
            "source_kind",
            "source_ref",
            "valid_from",
            "valid_to",
            "created_at",
            "ended_at",
        }.issubset(support_cols))
        self.assertIn("idx_supports_triple", indexes)
        self.assertIn("idx_derivprem_premise", indexes)

    def test_derive_materialize_records_deduced_support_and_premise_reverse_index(self):
        with _test_tmpdir() as td:
            palace, base_premises = self._chain_palace(td)
            worklist, _decisions_path = self._materialize_first_derive_candidate(td, palace)
            conclusion_id = self._conclusion_triple_id(palace)

            support_rows = self._support_rows(palace, conclusion_id)
            premise_rows = self._derivation_premise_ids(palace, conclusion_id)

        proof_premises = [str(p) for p in worklist["items"][0]["proof"]["premise_ids"]]
        self.assertEqual(set(proof_premises), set(base_premises))
        self.assertEqual(support_rows, [{
            "status": "deduced",
            "source_trust": "trusted_rule",
            "inherited_status": "deduced",
            "conditional_on_triple_ids": "[]",
            "scope": "durable",
            "source_kind": "contemplate:derive",
            "source_ref": "derive:transitive:depends_on",
        }])
        self.assertEqual(set(premise_rows), set(base_premises))
        self.assertEqual(len(premise_rows), len(base_premises))

    def test_repeated_derive_adopt_does_not_duplicate_supports_or_premises(self):
        with _test_tmpdir() as td:
            palace, _base_premises = self._chain_palace(td)
            _worklist, decisions_path = self._materialize_first_derive_candidate(td, palace)
            first_counts = self._sidecar_counts(palace)

            self._adopt_derive(palace, decisions_path)
            second_counts = self._sidecar_counts(palace)

        self.assertEqual(second_counts, first_counts)

    def test_deduped_existing_conclusion_still_gets_deduced_support(self):
        with _test_tmpdir() as td:
            palace, premise_ids = self._chain_palace(td)
            kg = _RealKG(db_path=self._kg_path(palace))
            try:
                existing_closure_id = str(
                    kg.add_triple("A", "depends_on_closure", "C", valid_from="2026-01-01")
                )
            finally:
                kg.close()

            con = sqlite3.connect(self._kg_path(palace))
            try:
                entity_ids = {
                    name: entity_id
                    for entity_id, name in con.execute("SELECT id, name FROM entities").fetchall()
                }
            finally:
                con.close()

            writer = dream_palace.KgDeriveWriter(palace)
            try:
                result = writer.add_derived(
                    {
                        "subject_id": entity_ids["A"],
                        "predicate": "depends_on_closure",
                        "object_id": entity_ids["C"],
                    },
                    "transitive:depends_on",
                    premise_ids,
                    [],
                    "ontology:v1",
                    1.0,
                    "2026-01-01",
                    None,
                )
            finally:
                writer.close()

            support_rows = self._support_rows(palace, existing_closure_id)

        self.assertEqual(str(result["triple_id"]), existing_closure_id)
        self.assertEqual(len(support_rows), 1)
        self.assertEqual(support_rows[0]["status"], "deduced")
        self.assertEqual(support_rows[0]["source_trust"], "trusted_rule")

    def test_reconcile_classifies_legacy_and_derive_and_backfills_premises_idempotently(self):
        with _test_tmpdir() as td:
            palace = os.path.join(td, "palace")
            os.makedirs(palace)
            kg = _RealKG(db_path=self._kg_path(palace))
            try:
                legacy_id = str(kg.add_triple("Legacy", "states", "Fact", valid_from="2026-01-01"))
                premise_id = str(kg.add_triple("Premise", "supports", "Fact", valid_from="2026-01-01"))
                derive_id = str(kg.add_triple(
                    "Derived", "depends_on_closure", "Result",
                    valid_from="2026-01-01",
                    source_drawer_id="derive:manual",
                    adapter_name="contemplate:derive",
                ))
                malformed_id = str(kg.add_triple(
                    "Malformed", "depends_on_closure", "Result",
                    valid_from="2026-01-01",
                    adapter_name="contemplate:derive",
                ))
            finally:
                kg.close()

            con = sqlite3.connect(self._kg_path(palace))
            try:
                con.executescript(
                    """
                    CREATE TABLE kg_derivations(
                        id INTEGER PRIMARY KEY,
                        candidate_id TEXT UNIQUE,
                        conclusion_triple_id TEXT,
                        rule_id TEXT,
                        ontology_version TEXT,
                        premise_triple_ids TEXT,
                        premise_drawer_ids TEXT,
                        confidence REAL,
                        created_at TEXT
                    );
                    """
                )
                con.execute(
                    """
                    INSERT INTO kg_derivations(
                        candidate_id, conclusion_triple_id, rule_id, ontology_version,
                        premise_triple_ids, premise_drawer_ids, confidence, created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        "valid-derivation",
                        derive_id,
                        "transitive:depends_on",
                        "ontology:v1",
                        json.dumps([legacy_id, premise_id]),
                        "[]",
                        1.0,
                        "2026-07-14T00:00:00+00:00",
                    ),
                )
                con.execute(
                    """
                    INSERT INTO kg_derivations(
                        candidate_id, conclusion_triple_id, rule_id, ontology_version,
                        premise_triple_ids, premise_drawer_ids, confidence, created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        "malformed-derivation",
                        malformed_id,
                        "transitive:depends_on",
                        "ontology:v1",
                        "not-json",
                        "[]",
                        1.0,
                        "2026-07-14T00:00:00+00:00",
                    ),
                )
                con.commit()
            finally:
                con.close()

            reconcile = getattr(dream_palace, "reconcile_firewall_provenance")
            first = reconcile(palace)
            support_count = self._sidecar_counts(palace)["supports"]
            premise_count = self._sidecar_counts(palace)["premises"]
            second = reconcile(palace)
            support_count_after_second = self._sidecar_counts(palace)["supports"]
            premise_count_after_second = self._sidecar_counts(palace)["premises"]

            con = sqlite3.connect(self._kg_path(palace))
            con.row_factory = sqlite3.Row
            try:
                support_by_triple = {
                    row["triple_id"]: (row["status"], row["source_trust"])
                    for row in con.execute(
                        "SELECT triple_id, status, source_trust FROM kg_triple_supports"
                    ).fetchall()
                }
                valid_derivation_id = con.execute(
                    "SELECT id FROM kg_derivations WHERE candidate_id='valid-derivation'"
                ).fetchone()[0]
                malformed_derivation_id = con.execute(
                    "SELECT id FROM kg_derivations WHERE candidate_id='malformed-derivation'"
                ).fetchone()[0]
                backfilled = [
                    str(row[0])
                    for row in con.execute(
                        """
                        SELECT premise_triple_id
                        FROM kg_derivation_premises
                        WHERE derivation_id = ?
                        ORDER BY premise_triple_id
                        """,
                        (valid_derivation_id,),
                    ).fetchall()
                ]
                malformed_backfill_count = con.execute(
                    "SELECT COUNT(*) FROM kg_derivation_premises WHERE derivation_id = ?",
                    (malformed_derivation_id,),
                ).fetchone()[0]
            finally:
                con.close()

        self.assertEqual(support_by_triple[legacy_id], ("asserted", "trusted_legacy"))
        self.assertEqual(support_by_triple[premise_id], ("asserted", "trusted_legacy"))
        self.assertEqual(support_by_triple[derive_id], ("deduced", "trusted_rule"))
        self.assertEqual(support_by_triple[malformed_id], ("deduced", "trusted_rule"))
        self.assertEqual(set(backfilled), {legacy_id, premise_id})
        self.assertEqual(malformed_backfill_count, 0)
        self.assertGreaterEqual(first["supports_inserted"], 4)
        self.assertEqual(first["malformed_derivations"], 1)
        self.assertEqual(second["supports_inserted"], 0)
        self.assertEqual(second["derivation_premises_inserted"], 0)
        self.assertEqual(support_count_after_second, support_count)
        self.assertEqual(premise_count_after_second, premise_count)

    def test_derive_and_gaps_harvest_items_are_read_neutral_after_sidecars(self):
        with _test_tmpdir() as td:
            palace, _premise_ids = self._chain_palace(td)
            before = self._harvest("derive", palace, os.path.join(td, "derive-before.json"))

            getattr(dream_palace, "reconcile_firewall_provenance")(palace)
            after = self._harvest("derive", palace, os.path.join(td, "derive-after.json"))

            self.assertEqual(before["items"], after["items"])
            self.assertEqual(len(after["items"]), 1)
            self.assertEqual(after["items"][0]["conclusion"]["predicate"], "depends_on_closure")

        with _test_tmpdir() as td:
            palace = self._gaps_palace(td)
            before = self._harvest("gaps", palace, os.path.join(td, "gaps-before.json"))

            getattr(dream_palace, "reconcile_firewall_provenance")(palace)
            after = self._harvest("gaps", palace, os.path.join(td, "gaps-after.json"))

            self.assertEqual(before["items"], after["items"])
            edges = {
                (item["hypothesis"]["subject"], item["hypothesis"]["object"])
                for item in after["items"]
            }
            self.assertIn(("B", "C"), edges)


if __name__ == "__main__":
    unittest.main()
