"""CLI-level tests for Track B B3 acquire wiring.

Run once dream_contemplate --acquire lands:
MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//'); cd skills/dreaming/scripts && "$MPY" -m unittest test_b3_acquire_cli -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest

import dream_contemplate
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
QUOTE_B_C = "service_b depends on service_c."
RETRIEVED_AT = "2026-01-01T12:00:00+00:00"


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="b3-acquire-cli-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


@unittest.skipUnless(_HAS_MEMPALACE, "requires mempalace interpreter")
class B3AcquireCliTests(unittest.TestCase):
    def _db_path(self, palace: str) -> str:
        return os.path.join(palace, "knowledge_graph.sqlite3")

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
                    adapter_name="test:b3-acquire-cli",
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

    def _seed_reachability_palace(self, palace: str) -> str:
        self._add_triple(palace, SERVICE_A, PREDICATE, SERVICE_B)
        self._add_triple(palace, SERVICE_C, PREDICATE, SERVICE_D)
        ontology_path = os.path.join(palace, "ontology.json")
        self._write_json(
            ontology_path,
            {
                "version": 1,
                "rules": [
                    {
                        "id": "transitive:depends_on",
                        "family": "transitive",
                        "predicate": PREDICATE,
                        "derived_predicate": "depends_on_closure",
                        "enabled": True,
                        "max_depth": 8,
                    }
                ],
            },
        )
        return ontology_path

    def _write_json(self, path: str, value: object) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
        return path

    def _write_recall_file(self, palace: str, content: str) -> str:
        return self._write_json(
            os.path.join(palace, "recall.json"),
            [
                {
                    "source_type": "session_recall",
                    "trust_domain": "session_store",
                    "locator": {"test": "b3-acquire-cli", "edge": "service_b-service_c"},
                    "retrieved_at": RETRIEVED_AT,
                    "content": content,
                }
            ],
        )

    def _write_oracle_file(self, palace: str, content: str, verdict: str = "supports") -> str:
        if verdict == "supports":
            start = content.index(QUOTE_B_C)
            canned = {
                "verdict": "supports",
                "quote": QUOTE_B_C,
                "char_span": {"start": start, "end": start + len(QUOTE_B_C)},
                "speaker": "eugene",
                "modality": "factual",
            }
        else:
            canned = {
                "verdict": "not_addressed",
                "quote": None,
                "char_span": None,
                "speaker": None,
                "modality": "factual",
            }
        return self._write_json(os.path.join(palace, "oracle.json"), canned)

    def _base_argv(self, palace: str, ontology_path: str, recall_path: str) -> list[str]:
        return [
            "--palace",
            palace,
            "--rules",
            ontology_path,
            "--acquire",
            "--subject",
            SERVICE_A,
            "--predicate",
            PREDICATE,
            "--object",
            SERVICE_D,
            "--run-id",
            "test-b3-acquire-cli",
            "--max-iterations",
            "5",
            "--max-acquisitions",
            "2",
            "--max-tool-calls",
            "5",
            "--recall-file",
            recall_path,
            "--trusted-speaker",
            "eugene",
            "--format",
            "json",
        ]

    def _invoke_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = dream_contemplate.main(argv)
            except SystemExit as ex:
                code = ex.code
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def _run_json(self, argv: list[str]) -> dict:
        code, stdout, stderr = self._invoke_main(argv)
        self.assertEqual(code, 0, stderr)
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as ex:
            self.fail(f"expected JSON stdout, got {stdout!r}: {ex}")

    def _field(self, report: dict, key: str):
        if key in report:
            return report[key]
        result = report.get("result") or {}
        return result.get(key)

    def _answer(self, report: dict) -> dict:
        return self._field(report, "answer") or {}

    def _acquired(self, report: dict) -> list[dict]:
        return list(self._field(report, "acquired") or [])

    def _assert_report_metadata(self, report: dict, palace: str, ontology_path: str) -> None:
        self.assertEqual(self._field(report, "palace"), dream_palace.bind_palace(palace))
        self.assertEqual(self._field(report, "rules_path"), ontology_path)
        self.assertEqual(self._field(report, "enabled_rule_count"), 1)
        query = self._field(report, "query")
        self.assertEqual(query["subject_id"], SERVICE_A)
        self.assertEqual(query["base_predicate"], PREDICATE)
        self.assertEqual(query["object_id"], SERVICE_D)
        self.assertIn("budgets", (report.get("result") or report))

    def _assert_answered_with_one_acquisition(self, report: dict) -> None:
        self.assertEqual(self._field(report, "status"), "answered")
        answer = self._answer(report)
        self.assertIs(answer.get("value"), True)
        self.assertEqual(answer.get("epistemic_status"), "entailed_given")
        self.assertEqual(len(self._acquired(report)), 1)

    def _durable_edge_keys(self, palace: str) -> set[tuple[str, str, str]]:
        return {
            (str(row["subject_id"]), dream_lib.normalize_predicate(row["predicate"]), str(row["object_id"]))
            for row in dream_palace.load_premises(palace, purpose="durable")
        }

    def test_acquire_cli_oracle_answers_and_keeps_acquired_gap_provisional(self):
        with _test_tmpdir() as palace:
            ontology_path = self._seed_reachability_palace(palace)
            recall_path = self._write_recall_file(palace, QUOTE_B_C)
            oracle_path = self._write_oracle_file(palace, QUOTE_B_C)

            report = self._run_json(
                self._base_argv(palace, ontology_path, recall_path)
                + ["--extractor", "oracle", "--oracle-file", oracle_path]
            )

            self._assert_report_metadata(report, palace, ontology_path)
            self._assert_answered_with_one_acquisition(report)
            self.assertNotIn(
                (SERVICE_B, PREDICATE, SERVICE_C),
                self._durable_edge_keys(palace),
            )

    def test_acquire_cli_heuristic_answers_when_recall_sentence_co_mentions_gap_endpoints(self):
        with _test_tmpdir() as palace:
            ontology_path = self._seed_reachability_palace(palace)
            recall_path = self._write_recall_file(palace, QUOTE_B_C)

            report = self._run_json(
                self._base_argv(palace, ontology_path, recall_path)
                + ["--extractor", "heuristic"]
            )

            self._assert_report_metadata(report, palace, ontology_path)
            self._assert_answered_with_one_acquisition(report)

    def test_acquire_cli_not_addressed_leaves_query_unsupported(self):
        with _test_tmpdir() as palace:
            ontology_path = self._seed_reachability_palace(palace)
            recall_path = self._write_recall_file(palace, "service_b was mentioned without the other endpoint.")

            report = self._run_json(
                self._base_argv(palace, ontology_path, recall_path)
                + ["--extractor", "heuristic"]
            )

            self._assert_report_metadata(report, palace, ontology_path)
            self.assertIn(self._field(report, "status"), {"fixpoint", "budget_exhausted"})
            self.assertEqual(self._acquired(report), [])
            answer = self._answer(report)
            self.assertFalse(answer.get("value"))
            self.assertEqual(answer.get("epistemic_status"), "unsupported")

    def test_acquire_cli_missing_subject_predicate_object_returns_2(self):
        with _test_tmpdir() as palace:
            code, _stdout, _stderr = self._invoke_main(["--palace", palace, "--acquire"])

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
