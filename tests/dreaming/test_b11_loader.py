"""Independent B1.1 regression tests for the fail-closed premise loader."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

import dream_palace

try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="b11-loader-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B11PremiseLoaderRegressionTests(unittest.TestCase):
    def _kg_path(self, palace):
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _make_palace(self, td):
        palace = os.path.join(td, "palace")
        os.makedirs(palace)
        return palace

    def _seed_triples(self, palace, count=3):
        triples = [
            ("Alpha", "relates_to", "Beta", "2026-01-01"),
            ("Beta", "relates_to", "Gamma", "2026-01-02"),
            ("Gamma", "relates_to", "Delta", "2026-01-03"),
            ("Delta", "relates_to", "Epsilon", "2026-01-04"),
            ("Epsilon", "relates_to", "Zeta", "2026-01-05"),
        ][:count]
        kg = _RealKG(db_path=self._kg_path(palace))
        try:
            return [
                str(kg.add_triple(subject, predicate, obj, valid_from=valid_from))
                for subject, predicate, obj, valid_from in triples
            ]
        finally:
            kg.close()

    def _load_premises(self, palace, **kwargs):
        return getattr(dream_palace, "load_premises")(palace, **kwargs)

    def _reconcile(self, palace):
        return getattr(dream_palace, "reconcile_firewall_provenance")(palace)

    def _identity_set(self, rows):
        return {
            (
                row["triple_id"],
                row["subject"],
                row["predicate"],
                row["object"],
            )
            for row in rows
        }

    def _triple_ids(self, rows):
        return {row["triple_id"] for row in rows}

    def _set_support(self, palace, triple_id, *, status, source_trust, inherited_status=None, conditional_on="[]"):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            con.execute(
                """
                UPDATE kg_triple_supports
                SET status = ?,
                    source_trust = ?,
                    inherited_status = ?,
                    conditional_on_triple_ids = ?
                WHERE triple_id = ?
                """,
                (status, source_trust, inherited_status or status, conditional_on, triple_id),
            )
            self.assertEqual(con.total_changes, 1)
            con.commit()
        finally:
            con.close()

    def _epoch_value(self, palace):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            row = con.execute(
                "SELECT value FROM kg_firewall_meta WHERE key='epoch_committed_at'"
            ).fetchone()
            return None if row is None else row[0]
        finally:
            con.close()

    def _table_exists(self, palace, table):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            return con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone() is not None
        finally:
            con.close()

    def _support_count(self, palace):
        con = sqlite3.connect(self._kg_path(palace))
        try:
            return con.execute("SELECT COUNT(*) FROM kg_triple_supports").fetchone()[0]
        finally:
            con.close()

    def _insert_raw_triple_without_support(self, palace, triple_id):
        db_path = self._kg_path(palace)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            entity_ids = [
                row["id"]
                for row in con.execute("SELECT id FROM entities ORDER BY id LIMIT 2").fetchall()
            ]
            self.assertGreaterEqual(len(entity_ids), 2)
            columns = [dict(row) for row in con.execute("PRAGMA table_info(triples)").fetchall()]
            values = {
                "id": triple_id,
                "subject": entity_ids[0],
                "predicate": "post_epoch_relates_to",
                "object": entity_ids[1],
                "valid_from": "2026-02-01",
                "valid_to": None,
                "confidence": 1.0,
                "source_closet": None,
                "source_file": None,
                "source_drawer_id": "drawer:post-epoch",
                "adapter_name": "test:b11-loader",
                "extracted_at": "2026-02-02",
            }
            insert_columns = []
            insert_values = []
            for column in columns:
                name = column["name"]
                if name in values:
                    insert_columns.append(name)
                    insert_values.append(values[name])
                elif column["notnull"] and column["dflt_value"] is None and not column["pk"]:
                    insert_columns.append(name)
                    insert_values.append("")
            placeholders = ",".join("?" for _ in insert_columns)
            con.execute(
                f"INSERT INTO triples({','.join(insert_columns)}) VALUES ({placeholders})",
                insert_values,
            )
            con.commit()
        finally:
            con.close()

    def test_legacy_durable_and_audit_have_identical_current_premise_set(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            self._seed_triples(palace, count=4)

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertEqual(self._identity_set(durable), self._identity_set(audit))
        self.assertEqual(
            [{key: row[key] for key in ("triple_id", "subject", "predicate", "object")} for row in durable],
            [{key: row[key] for key in ("triple_id", "subject", "predicate", "object")} for row in audit],
        )

    def test_durable_auto_reconciles_fresh_palace_and_commits_epoch(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            ids = self._seed_triples(palace, count=3)
            self.assertFalse(self._table_exists(palace, "kg_triple_supports"))
            self.assertFalse(self._table_exists(palace, "kg_firewall_meta"))

            durable = self._load_premises(palace, purpose="durable")

            self.assertEqual(self._triple_ids(durable), set(ids))
            self.assertIsNotNone(self._epoch_value(palace))
            self.assertEqual(self._support_count(palace), len(ids))

    def test_tainted_only_support_is_excluded_from_durable_but_present_in_audit(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            triple_id = self._seed_triples(palace, count=1)[0]
            self._reconcile(palace)
            self._set_support(
                palace,
                triple_id,
                status="abduced",
                source_trust="hypothesis",
                inherited_status="abduced",
            )

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertNotIn(triple_id, self._triple_ids(durable))
        self.assertIn(triple_id, self._triple_ids(audit))

    def test_disallowed_status_source_trust_pairs_are_excluded_from_durable(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            deduced_untrusted_id, asserted_unknown_id = self._seed_triples(palace, count=2)
            self._reconcile(palace)
            self._set_support(
                palace,
                deduced_untrusted_id,
                status="deduced",
                source_trust="untrusted_source",
            )
            self._set_support(
                palace,
                asserted_unknown_id,
                status="asserted",
                source_trust="unknown",
            )

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertFalse({deduced_untrusted_id, asserted_unknown_id} & self._triple_ids(durable))
        self.assertTrue({deduced_untrusted_id, asserted_unknown_id}.issubset(self._triple_ids(audit)))

    def test_conditional_support_is_excluded_from_durable(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            triple_id = self._seed_triples(palace, count=1)[0]
            self._reconcile(palace)
            self._set_support(
                palace,
                triple_id,
                status="asserted",
                source_trust="trusted_legacy",
                conditional_on='["t_x"]',
            )

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertNotIn(triple_id, self._triple_ids(durable))
        self.assertIn(triple_id, self._triple_ids(audit))

    def test_post_epoch_triple_without_support_is_denied_by_durable_but_visible_to_audit(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            original_id = self._seed_triples(palace, count=1)[0]
            self._load_premises(palace, purpose="durable")
            self.assertIsNotNone(self._epoch_value(palace))
            unsupported_id = "post-epoch-unsupported"
            self._insert_raw_triple_without_support(palace, unsupported_id)

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertIn(original_id, self._triple_ids(durable))
        self.assertNotIn(unsupported_id, self._triple_ids(durable))
        self.assertIn(unsupported_id, self._triple_ids(audit))

    def test_simulation_requires_run_id_and_with_run_id_matches_durable(self):
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            self._seed_triples(palace, count=2)
            durable = self._load_premises(palace, purpose="durable")

            with self.assertRaises(ValueError):
                self._load_premises(palace, purpose="simulation")
            simulation = self._load_premises(palace, purpose="simulation", run_id="r1")

        self.assertEqual(simulation, durable)


    def test_ended_support_is_excluded_from_durable_but_triple_still_active(self):
        """A support ended by (future) M1 invalidation must not keep a triple premise-eligible."""
        with _test_tmpdir() as td:
            palace = self._make_palace(td)
            ids = self._seed_triples(palace, count=2)
            self._reconcile(palace)
            target = ids[0]
            # Simulate B1.3 invalidation ending the sole support (triple row stays valid_to IS NULL).
            con = sqlite3.connect(self._kg_path(palace))
            try:
                con.execute(
                    "UPDATE kg_triple_supports SET ended_at=?, valid_to=? WHERE triple_id=?",
                    ("2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00", target),
                )
                self.assertEqual(con.total_changes, 1)
                con.commit()
            finally:
                con.close()

            durable = self._load_premises(palace, purpose="durable")
            audit = self._load_premises(palace, purpose="audit")

        self.assertNotIn(target, self._triple_ids(durable))
        self.assertIn(target, self._triple_ids(audit))
        self.assertIn(ids[1], self._triple_ids(durable))


if __name__ == "__main__":
    unittest.main()
