"""B3 S4b ACQUIRE loop contract tests.

Run after the acquire_loop implementation lands:
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//'); cd skills/dreaming/scripts && "$MPY" -m unittest test_b3_loop -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

import dream_acquire as acquire
import dream_lib
import dream_palace


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = "2026-01-01T12:00:00+00:00"
QUOTE_B_C = "Service B depends on Service C."

TRANSITIVE_RULES = [{
    "id": "transitive:depends_on",
    "family": "transitive",
    "predicate": "depends_on",
    "inverse_predicate": None,
    "enabled": True,
    "max_depth": 8,
}]

QUERY_A_D = {
    "subject_id": "a",
    "base_predicate": "depends_on",
    "object_id": "d",
}

LOOP_RESULT_KEYS = {
    "status",
    "run_id",
    "answer",
    "confidence",
    "acquired",
    "unfilled_gaps",
    "budgets",
    "refusal",
}

ANSWER_KEYS = {
    "kind",
    "query",
    "value",
    "epistemic_status",
    "support",
    "conditional_on",
}

ACQUIRED_KEYS = {
    "gap_key",
    "provisional_id",
    "source_kind",
    "source_ref",
    "epistemic_status",
}


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="b3-loop-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3AcquireLoopTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

    def _connect(self, palace: str) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path(palace))
        con.row_factory = sqlite3.Row
        return con

    def _rows(self, palace: str, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        con = self._connect(palace)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def _add_triple(
        self,
        palace: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        status: str = "asserted",
        valid_from: str = "2026-01-01",
    ) -> str:
        db_path = self._db_path(palace)
        kg = _RealKG(db_path=db_path)
        try:
            triple_id = str(
                kg.add_triple(
                    subject,
                    predicate,
                    object_,
                    valid_from=valid_from,
                    confidence=1.0,
                    source_drawer_id=f"drawer:{subject}:{predicate}:{object_}",
                    adapter_name="test:b3-loop",
                )
            )
        finally:
            kg.close()

        dream_palace.ensure_firewall_schema(db_path)
        source_trust = "trusted_rule" if status == "deduced" else "trusted_legacy"
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"support:{status}:{triple_id}",
                    triple_id,
                    status,
                    source_trust,
                    status,
                    "[]",
                    "durable",
                    "test",
                    f"support:{status}:{triple_id}",
                    valid_from,
                    None,
                    RETRIEVED_AT,
                    None,
                ),
            )
            con.commit()
        finally:
            con.close()
        return triple_id

    def _seed_edges(self, palace: str, edges: list[tuple[str, str]]) -> None:
        for subject, object_ in edges:
            self._add_triple(palace, subject, "depends_on", object_)

    def _source_b_c(self) -> dict:
        return {
            "source_type": "recall",
            "trust_domain": "untrusted",
            "locator": {"test": "b3-loop", "edge": "b-c"},
            "retrieved_at": RETRIEVED_AT,
            "content": QUOTE_B_C,
        }

    def _recall_b_c(self, query_text, gap) -> list[dict]:
        self.assertIsInstance(query_text, str)
        self._assert_gap_key_is_edge(acquire.gap_hypothesis_key(gap), "b", "depends_on", "c")
        return [self._source_b_c()]

    def _supports_b_c(self, prompt_payload: dict) -> dict:
        self.assertEqual(prompt_payload["target"]["subject_id"], "b")
        self.assertEqual(prompt_payload["target"]["predicate"], "depends_on")
        self.assertEqual(prompt_payload["target"]["object_id"], "c")
        content = prompt_payload["source"]["content"]
        start = content.index(QUOTE_B_C)
        return {
            "verdict": "supports",
            "quote": QUOTE_B_C,
            "char_span": {"start": start, "end": start + len(QUOTE_B_C)},
            "speaker": "eugene",
            "modality": "factual",
        }

    def _not_addressed(self, prompt_payload: dict) -> dict:
        return {
            "verdict": "not_addressed",
            "quote": None,
            "char_span": None,
            "speaker": None,
            "modality": "factual",
        }

    def _fail_if_called(self, *args, **kwargs):
        self.fail("recall/extractor should not be called for an already-deduced answer")

    def _acquire_loop(self, palace: str, **kwargs) -> dict:
        acquire_loop = getattr(acquire, "acquire_loop", None)
        if acquire_loop is None:
            self.fail("dream_acquire.acquire_loop is missing")
        return acquire_loop(
            palace,
            query=QUERY_A_D,
            rules=TRANSITIVE_RULES,
            now=FIXED_NOW,
            trusted_speakers={"eugene"},
            **kwargs,
        )

    def _assert_result_shape(self, result: dict) -> None:
        self.assertEqual(set(result), LOOP_RESULT_KEYS)
        self.assertEqual(set(result["answer"]), ANSWER_KEYS)
        self.assertIn("level", result["confidence"])
        self.assertIn("rationale", result["confidence"])
        self.assertIn("iterations_used", result["budgets"])
        self.assertIn("acquisitions_used", result["budgets"])
        self.assertIn("tool_calls_used", result["budgets"])
        self.assertIn("max_iterations", result["budgets"])
        self.assertIn("max_acquisitions", result["budgets"])
        self.assertIn("max_tool_calls", result["budgets"])

    def _assert_gap_key_is_edge(self, gap_key, subject_id: str, predicate: str, object_id: str) -> None:
        expected = (subject_id, dream_lib.normalize_predicate(predicate), object_id)
        if isinstance(gap_key, (list, tuple)) and len(gap_key) >= 3:
            actual = (str(gap_key[0]), dream_lib.normalize_predicate(str(gap_key[1])), str(gap_key[2]))
            self.assertEqual(actual, expected)
            return
        if isinstance(gap_key, dict):
            hypothesis = gap_key.get("hypothesis") or gap_key
            actual = (
                str(hypothesis.get("subject_id")),
                dream_lib.normalize_predicate(str(hypothesis.get("predicate"))),
                str(hypothesis.get("object_id")),
            )
            self.assertEqual(actual, expected)
            return
        text = str(gap_key)
        self.assertIn(subject_id, text)
        self.assertIn(dream_lib.normalize_predicate(predicate), text)
        self.assertIn(object_id, text)

    def _assert_happy_flip(self, result: dict) -> None:
        self._assert_result_shape(result)
        self.assertEqual(result["status"], "answered")
        self.assertIs(result["answer"]["value"], True)
        self.assertEqual(result["answer"]["epistemic_status"], "entailed_given")
        self.assertEqual(result["confidence"]["level"], "medium")
        self.assertEqual(len(result["acquired"]), 1)
        acquired = result["acquired"][0]
        self.assertEqual(set(acquired), ACQUIRED_KEYS)
        self.assertTrue(acquired["provisional_id"])
        self.assertEqual(acquired["source_kind"], "recall")
        self.assertEqual(acquired["epistemic_status"], "acquired")
        self._assert_gap_key_is_edge(acquired["gap_key"], "b", "depends_on", "c")

    def _durable_edge_keys(self, palace: str) -> set[tuple[str, str, str]]:
        return {
            (str(row["subject_id"]), dream_lib.normalize_predicate(row["predicate"]), str(row["object_id"]))
            for row in dream_palace.load_premises(palace, purpose="durable")
        }

    def _durable_answer(self, palace: str) -> dict:
        durable = dream_palace.load_premises(palace, purpose="durable")
        candidates = dream_lib.deductive_closure(
            durable,
            TRANSITIVE_RULES,
            max_depth=8,
            max_iterations=50,
            max_candidates=500,
        )
        return acquire.extract_boolean_reachability_answer(
            QUERY_A_D,
            durable,
            candidates,
            TRANSITIVE_RULES,
        )

    def test_happy_flip_acquires_gap_and_answers_entailed_given(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._recall_b_c,
                extractor_fn=self._supports_b_c,
            )

        self._assert_happy_flip(result)

    def test_already_deduced_does_not_need_acquisition(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("b", "c"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._fail_if_called,
                extractor_fn=self._fail_if_called,
            )

        self._assert_result_shape(result)
        self.assertEqual(result["status"], "answered")
        self.assertIs(result["answer"]["value"], True)
        self.assertEqual(result["answer"]["epistemic_status"], "deduced")
        self.assertEqual(result["confidence"]["level"], "high")
        self.assertEqual(result["acquired"], [])

    def test_not_addressed_sources_leave_gap_unfilled(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._recall_b_c,
                extractor_fn=self._not_addressed,
            )

        self._assert_result_shape(result)
        self.assertIn(result["status"], {"fixpoint", "budget_exhausted"})
        self.assertIs(result["answer"]["value"], False)
        self.assertEqual(result["answer"]["epistemic_status"], "unsupported")
        self.assertEqual(result["confidence"]["level"], "low")
        self.assertEqual(result["acquired"], [])
        self.assertGreaterEqual(len(result["unfilled_gaps"]), 1)
        self._assert_gap_key_is_edge(result["unfilled_gaps"][0]["gap_key"], "b", "depends_on", "c")

    def test_max_acquisitions_zero_budget_exhausts_without_write(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._recall_b_c,
                extractor_fn=self._supports_b_c,
                budgets={"max_acquisitions": 0},
            )

        self._assert_result_shape(result)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertLessEqual(result["budgets"]["acquisitions_used"], 0)
        self.assertEqual(result["acquired"], [])

    def test_max_iterations_one_does_not_exceed_cap(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._recall_b_c,
                extractor_fn=self._supports_b_c,
                budgets={"max_iterations": 1},
            )

        self._assert_result_shape(result)
        self.assertLessEqual(result["budgets"]["iterations_used"], 1)

    def test_acquired_provisional_does_not_pollute_durable_premises(self):
        with _test_tmpdir() as palace:
            self._seed_edges(palace, [("a", "b"), ("c", "d")])

            result = self._acquire_loop(
                palace,
                recall_fn=self._recall_b_c,
                extractor_fn=self._supports_b_c,
            )
            durable_edges = self._durable_edge_keys(palace)
            durable_answer = self._durable_answer(palace)

        self._assert_happy_flip(result)
        self.assertNotIn(("b", "depends_on", "c"), durable_edges)
        self.assertIs(durable_answer["value"], False)
        self.assertEqual(durable_answer["epistemic_status"], "unsupported")

    def test_happy_flip_is_deterministic_for_same_inputs(self):
        results = []
        for _ in range(2):
            with _test_tmpdir() as palace:
                self._seed_edges(palace, [("a", "b"), ("c", "d")])
                results.append(
                    self._acquire_loop(
                        palace,
                        recall_fn=self._recall_b_c,
                        extractor_fn=self._supports_b_c,
                    )
                )

        self.assertEqual([r["status"] for r in results], ["answered", "answered"])
        self.assertEqual(
            [r["answer"]["epistemic_status"] for r in results],
            ["entailed_given", "entailed_given"],
        )
        self.assertEqual(
            [r["confidence"]["level"] for r in results],
            ["medium", "medium"],
        )


if __name__ == "__main__":
    unittest.main()
