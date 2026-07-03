"""Tests for mempalace-facing dreaming adapters that do not import mempalace."""
from __future__ import annotations

import os
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
                    "subject": "Alice",
                    "predicate": "lives_in",
                    "object": "Portland",
                    "valid_from": "2024-01-01",
                    "extracted_at": "2024-01-02",
                }
            ])


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
            documents=["continued pattern", "SESSION_ID: 12345678-abcd first pattern"],
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
            "SESSION_ID: 12345678-abcd first pattern\ncontinued pattern",
        )
        self.assertEqual(entries[0]["embedding"], [2.0, 4.0])
        self.assertEqual(entries[0]["session_id"], "12345678-abcd")
        self.assertEqual(entries[0]["agent"], "Copilot CLI")
        self.assertEqual(entries[0]["date"], "2026-07-03")
        self.assertEqual(entries[0]["topic"], "dreaming")
        self.assertEqual(entries[0]["wing"], "wing_copilot-cli")
        self.assertEqual(entries[0]["room"], "diary")
        self.assertEqual(originals[2][0]["where"], {"$and": [{"wing": "wing_copilot-cli"}, {"room": "diary"}]})

    def test_single_chunk_row_passes_through(self):
        originals = self._with_fake_collection(
            ids=["drawer-1"],
            documents=["SESSION_ID: abcdef12 one chunk"],
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
                "text": "SESSION_ID: abcdef12 one chunk",
                "embedding": [0.5, 0.25],
                "session_id": "abcdef12",
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


if __name__ == "__main__":
    unittest.main()
