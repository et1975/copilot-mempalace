"""B3 S5 RESUMABLE ACQUIRE agent-in-the-loop contract tests.

Run after the acquire_start/acquire_resume implementation lands:
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//'); cd skills/dreaming/scripts && "$MPY" -m unittest test_b3_resume -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from typing import Any

import dream_acquire as acquire
import dream_lib
import dream_palace


try:
    from mempalace.knowledge_graph import KnowledgeGraph as _RealKG
    _HAS_MEMPALACE = True
except Exception:
    _HAS_MEMPALACE = False


SERVICE_A = "service_a"
SERVICE_B = "service_b"
SERVICE_C = "service_c"
SERVICE_D = "service_d"
PREDICATE = "depends_on"
SOURCE_CONTENT = "Design note: service_b depends on service_c for startup."
QUOTE_B_C = "service_b depends on service_c"
RETRIEVED_AT = "2026-01-01T12:00:00+00:00"
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

TRANSITIVE_RULES = [{
    "id": "transitive:depends_on",
    "family": "transitive",
    "predicate": PREDICATE,
    "derived_predicate": "depends_on_closure",
    "enabled": True,
    "max_depth": 8,
}]

QUERY_A_D = {
    "subject_id": SERVICE_A,
    "base_predicate": PREDICATE,
    "object_id": SERVICE_D,
}

PENDING_TARGET_B_C = {
    "subject_id": SERVICE_B,
    "predicate": PREDICATE,
    "object_id": SERVICE_C,
}


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="b3-resume-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3ResumeAcquireTests(unittest.TestCase):
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
                    adapter_name="test:b3-resume",
                )
            )
        finally:
            kg.close()

        dream_palace.ensure_firewall_schema(db_path)
        source_trust = "trusted_rule" if status == "deduced" else "trusted_legacy"
        support_id = f"support:{status}:{triple_id}"
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    support_id,
                    triple_id,
                    status,
                    source_trust,
                    status,
                    "[]",
                    "durable",
                    "test",
                    support_id,
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

    def _seed_reachability_palace(self, palace: str) -> None:
        self._add_triple(palace, SERVICE_A, PREDICATE, SERVICE_B)
        self._add_triple(palace, SERVICE_C, PREDICATE, SERVICE_D)

    def _source_b_c(self) -> dict[str, Any]:
        return {
            "source_type": "recall",
            "trust_domain": "untrusted",
            "locator": {"test": "b3-resume", "edge": "service_b-service_c"},
            "retrieved_at": RETRIEVED_AT,
            "content": SOURCE_CONTENT,
        }

    def _recall_b_c(self, query_text=None, gap=None, **_kwargs) -> list[dict[str, Any]]:
        if query_text is not None:
            self.assertIsInstance(query_text, str)
        if gap is not None:
            self._assert_gap_key_is_edge(acquire.gap_hypothesis_key(gap), SERVICE_B, PREDICATE, SERVICE_C)
        return [self._source_b_c()]

    def _good_verdict(self) -> dict[str, Any]:
        start = SOURCE_CONTENT.index(QUOTE_B_C)
        return {
            "verdict": "supports",
            "quote": QUOTE_B_C,
            "char_span": {"start": start, "end": start + len(QUOTE_B_C)},
            "speaker": "eugene",
            "modality": "factual",
        }

    def _fabricated_verdict(self) -> dict[str, Any]:
        return {
            "verdict": "supports",
            "quote": "THIS TEXT IS NOT IN THE SOURCE",
            "char_span": {"start": 0, "end": len("THIS TEXT IS NOT IN THE SOURCE")},
            "speaker": "eugene",
            "modality": "factual",
        }

    def _not_addressed_verdict(self) -> dict[str, Any]:
        return {"verdict": "not_addressed"}

    def _start(self, palace: str, *, run_id: str | None = None, budgets: dict[str, int] | None = None) -> dict[str, Any]:
        acquire_start = getattr(acquire, "acquire_start", None)
        if acquire_start is None:
            self.fail("dream_acquire.acquire_start is missing")
        return acquire_start(
            palace,
            query=QUERY_A_D,
            rules=TRANSITIVE_RULES,
            recall_fn=self._recall_b_c,
            budgets=budgets or {"max_iterations": 5, "max_acquisitions": 1, "max_tool_calls": 5},
            trusted_speakers={"eugene"},
            source_kind="recall",
            run_id=run_id,
            now=FIXED_NOW,
        )

    def _resume(self, palace: str, run_id: str, verdict: dict[str, Any]) -> dict[str, Any]:
        acquire_resume = getattr(acquire, "acquire_resume", None)
        if acquire_resume is None:
            self.fail("dream_acquire.acquire_resume is missing")
        return acquire_resume(
            palace,
            run_id,
            verdict=verdict,
            rules=TRANSITIVE_RULES,
            recall_fn=self._recall_b_c,
            now=FIXED_NOW,
        )

    def _assert_documented_step_keys(self, result: dict[str, Any]) -> None:
        self.assertTrue(
            {
                "run_id",
                "status",
                "pending",
                "answer",
                "confidence",
                "acquired",
                "unfilled_gaps",
                "budgets",
            }.issubset(result),
            result,
        )
        self.assertIn(result["status"], {"awaiting_extraction", "answered", "fixpoint", "budget_exhausted", "abandoned"})

    def _assert_pending_b_c(self, result: dict[str, Any]) -> None:
        self._assert_documented_step_keys(result)
        self.assertEqual(result["status"], "awaiting_extraction")
        pending = result["pending"]
        self.assertIsInstance(pending, dict)
        self.assertTrue(pending.get("request_id"))
        self.assertEqual(pending.get("target"), PENDING_TARGET_B_C)
        self.assertIn(SOURCE_CONTENT, pending.get("source", {}).get("content", ""))
        self.assertTrue(pending.get("instruction"))

    def _assert_answered_with_one_acquisition(self, result: dict[str, Any]) -> None:
        self._assert_documented_step_keys(result)
        self.assertEqual(result["status"], "answered")
        self.assertIsNone(result["pending"])
        self.assertIs(result["answer"]["value"], True)
        self.assertEqual(result["answer"]["epistemic_status"], "entailed_given")
        self.assertEqual(len(result["acquired"]), 1)
        acquired = result["acquired"][0]
        self.assertTrue(acquired.get("provisional_id"))
        self._assert_gap_key_is_edge(acquired.get("gap_key"), SERVICE_B, PREDICATE, SERVICE_C)

    def _assert_unsupported_terminal(self, result: dict[str, Any]) -> None:
        self._assert_documented_step_keys(result)
        self.assertIn(result["status"], {"fixpoint", "budget_exhausted"})
        self.assertIsNone(result["pending"])
        self.assertEqual(result["acquired"], [])
        self.assertIs(result["answer"]["value"], False)
        self.assertEqual(result["answer"]["epistemic_status"], "unsupported")
        self.assertTrue(result["unfilled_gaps"])
        self.assertTrue(
            any(
                self._gap_key_matches(gap.get("gap_key"), SERVICE_B, PREDICATE, SERVICE_C)
                for gap in result["unfilled_gaps"]
            ),
            result["unfilled_gaps"],
        )

    def _assert_gap_key_is_edge(self, gap_key, subject_id: str, predicate: str, object_id: str) -> None:
        self.assertTrue(self._gap_key_matches(gap_key, subject_id, predicate, object_id), gap_key)

    def _gap_key_matches(self, gap_key, subject_id: str, predicate: str, object_id: str) -> bool:
        expected = (subject_id, dream_lib.normalize_predicate(predicate), object_id)
        if isinstance(gap_key, (list, tuple)) and len(gap_key) >= 3:
            actual = (str(gap_key[0]), dream_lib.normalize_predicate(str(gap_key[1])), str(gap_key[2]))
            return actual == expected
        if isinstance(gap_key, dict):
            hypothesis = gap_key.get("hypothesis") or gap_key
            actual = (
                str(hypothesis.get("subject_id")),
                dream_lib.normalize_predicate(str(hypothesis.get("predicate"))),
                str(hypothesis.get("object_id")),
            )
            return actual == expected
        text = str(gap_key)
        return subject_id in text and dream_lib.normalize_predicate(predicate) in text and object_id in text

    def _durable_edge_keys(self, palace: str) -> set[tuple[str, str, str]]:
        return {
            (str(row["subject_id"]), dream_lib.normalize_predicate(row["predicate"]), str(row["object_id"]))
            for row in dream_palace.load_premises(palace, purpose="durable")
        }

    def _provisional_count_for_run(self, palace: str, run_id: str) -> int:
        rows = self._rows(
            palace,
            "SELECT COUNT(*) AS n FROM contemplate_provisional_facts WHERE run_id=?",
            (run_id,),
        )
        return int(rows[0]["n"])

    def _assert_owner_token_absent(self, result: dict[str, Any]) -> None:
        self.assertNotIn('"owner_token"', json.dumps(result, sort_keys=True, default=str))
        self.assertFalse(self._contains_key(result, "owner_token"), result)

    def _contains_key(self, value: Any, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(self._contains_key(item, key) for item in value.values())
        if isinstance(value, list):
            return any(self._contains_key(item, key) for item in value)
        return False

    def test_start_pauses_with_pending_extraction_for_missing_gap(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)

            result = self._start(palace)

        self._assert_pending_b_c(result)

    def test_resume_with_good_verdict_acquires_gap_and_flips_answer(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)
            started = self._start(palace)

            result = self._resume(palace, started["run_id"], self._good_verdict())

        self._assert_answered_with_one_acquisition(result)

    def test_fabricated_quote_is_rejected_and_leaves_gap_unfilled(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)
            started = self._start(palace)

            result = self._resume(palace, started["run_id"], self._fabricated_verdict())

        self._assert_unsupported_terminal(result)

    def test_not_addressed_verdict_leaves_query_unsupported(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)
            started = self._start(palace)

            result = self._resume(palace, started["run_id"], self._not_addressed_verdict())

        self._assert_unsupported_terminal(result)

    def test_acquired_gap_stays_provisional_and_out_of_durable_premises(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)
            started = self._start(palace)
            result = self._resume(palace, started["run_id"], self._good_verdict())
            durable_edges = self._durable_edge_keys(palace)

        self._assert_answered_with_one_acquisition(result)
        self.assertNotIn((SERVICE_B, PREDICATE, SERVICE_C), durable_edges)

    def test_resume_after_terminal_answer_is_idempotent_and_does_not_acquire_twice(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)
            started = self._start(palace)
            first = self._resume(palace, started["run_id"], self._good_verdict())
            count_after_first = self._provisional_count_for_run(palace, started["run_id"])

            second = self._resume(palace, started["run_id"], self._good_verdict())
            count_after_second = self._provisional_count_for_run(palace, started["run_id"])

        self._assert_answered_with_one_acquisition(first)
        self.assertEqual(second["status"], "answered")
        self.assertIs(second["answer"]["value"], True)
        self.assertEqual(count_after_first, 1)
        self.assertEqual(count_after_second, count_after_first)
        self.assertLessEqual(len(second["acquired"]), 1)

    def test_step_results_do_not_leak_owner_token(self):
        with _test_tmpdir() as palace:
            self._seed_reachability_palace(palace)

            started = self._start(palace)
            resumed = self._resume(palace, started["run_id"], self._good_verdict())

        self._assert_owner_token_absent(started)
        self._assert_owner_token_absent(resumed)


if __name__ == "__main__":
    unittest.main()
