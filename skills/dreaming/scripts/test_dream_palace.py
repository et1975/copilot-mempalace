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
