"""CLI behavior through real parsing, projection and sanctioned storage boundary."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

import dream_palace
from dream_procedural import event_to_data, parse_event
from test_dream_procedural import NOW, event_data, resign, sha, stamp
from test_dream_procedural_palace import sanctioned_writer
from test_dream_procedural_validate import GroundedFixture


class CommandTests(GroundedFixture):
    def invoke(self, command, event=None, *extra):
        from dream_procedure import main
        args = [command, "--palace", self.path, "--wing", "w"]
        if event is not None:
            path = Path(self.path, "input.json")
            path.write_text(json.dumps(event_to_data(event) if not isinstance(event, dict) else event))
            args += ["--input", str(path)]
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("dream_procedure.now_utc", return_value=NOW), \
             patch.object(dream_palace, "MempalaceWriter",
                          return_value=sanctioned_writer(self.path, self.collection)):
            code = main([*args, *extra])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def test_propose_review_outcome_and_lost_ack_retry(self):
        self.assertEqual(self.invoke("propose", self.proposal())[0], 0)
        review = self.review()
        self.assertEqual(self.invoke("review", review)[0], 0)
        del self.collection.rows["source-1"]
        code, result, err = self.invoke("review", review)
        self.assertEqual((code, result["status"]), (0, "already_exists"), err)
        self.assertEqual(len(result["projection"]["rules"][0]["events"]), 2)

    def test_dry_run_same_preflight_no_writer_construction(self):
        from dream_procedure import main
        path = Path(self.path, "dry.json")
        path.write_text(json.dumps(event_to_data(self.proposal())))
        before = deepcopy(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch("dream_procedure.now_utc", return_value=NOW), redirect_stdout(StringIO()):
            code = main(["propose", "--input", str(path), "--palace", self.path,
                         "--wing", "w", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(self.collection.rows, before)
        invalid = event_data(origin_drawer_ids=[], evidence=self.refs[:2])
        self.assertEqual(self.invoke("propose", invalid, "--dry-run")[0], 2)

    def test_invalid_requests_and_integrity_are_not_success_shaped_empty(self):
        invalid = event_to_data(self.proposal())
        invalid["payload"]["evidence"][0]["quote"] = "fabricated"
        self.assertEqual(self.invoke("propose", resign(invalid))[0], 2)
        self.assertEqual(self.invoke("propose", event_data(origin_drawer_ids=[]))[0], 1)
        self.collection.add("w", "procedural", "not an event")
        code, result, err = self.invoke("propose", self.proposal())
        self.assertEqual(code, 1, err)
        self.assertEqual(result["status"], "error")

    def test_prepare_fills_missing_digests_without_writing_palace(self):
        draft = event_to_data(self.proposal())
        del draft["digest"]
        del draft["rule_id"]
        del draft["payload"]["evidence"][0]["source_hash"]
        path = Path(self.tmp.name, "prepared.json")
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("propose", draft, "--prepare", "--out", str(path))
        self.assertEqual((code, result["status"]), (0, "prepared"), err)
        self.assertEqual(parse_event(json.loads(path.read_text())), self.proposal())
        self.assertEqual(self.collection.rows, before)
        self.assertEqual(self.invoke("propose", json.loads(path.read_text()))[0], 0)

    def test_explicit_polarities_count_once_per_source_session(self):
        self.invoke("propose", self.proposal())
        self.invoke("review", self.review())
        for number, outcome in ((3, "helpful"), (4, "helpful"), (5, "neutral"), (6, "harmful")):
            ref = self.refs[0]
            event = parse_event(event_data("outcome", number, outcome=outcome,
                                           source_session_id=ref["session_id"], evidence=[ref]))
            code, result, err = self.invoke("outcome", event)
            self.assertEqual(code, 0, err)
        state = result["projection"]["rules"][0]
        self.assertEqual((state["score"]["helpful"], state["score"]["harmful"]), (0, 1))
        self.assertIn("unresolved_harm", state["suppression_reasons"])

    def test_validate_searches_both_queries_and_emits_reusable_packet(self):
        self.invoke("propose", self.proposal())
        calls = []
        def query(**kwargs):
            calls.append(kwargs)
            ids = list(self.collection.rows)[:3]
            return {"ids": [ids], "documents": [[self.collection.rows[i]["text"] for i in ids]],
                    "metadatas": [[self.collection.rows[i]["metadata"] for i in ids]]}
        self.collection.query = query
        self.collection.embedding_function = lambda texts: [[1., 0.] for t in texts]
        out = Path(self.tmp.name, "packet.json")
        code, result, err = self.invoke("validate", None, "--rule-id", self.proposal().rule_id,
                                      "--contrast-query", "When does this fail?", "--out", str(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["n_results"] == 10 for c in calls))
        packet = json.loads(out.read_text())
        self.assertEqual(len(packet["validation_packet"]["evidence"]), 3)
        self.assertEqual(packet["validation_digest"], sha(packet["validation_packet"]))

    def test_cli_review_must_explicitly_resolve_adverse_events(self):
        self.invoke("propose", self.proposal())
        self.invoke("review", self.review())
        harm = parse_event(event_data("outcome", 3, outcome="harmful",
                            source_session_id=self.refs[0]["session_id"], evidence=[self.refs[0]]))
        self.invoke("outcome", harm)
        review = event_to_data(self.review(4, parent_review_ids=[self.review().event_id],
                                         acknowledged_evidence_ids=[harm.event_id]))
        self.assertEqual(self.invoke("review", resign(review))[0], 2)
        review["payload"]["dispositions"].append({"evidence_id": harm.event_id,
            "disposition": "not_applicable", "reason": "The blamed behavior was outside the condition.",
            "evidence": [self.refs[0]]})
        code, result, err = self.invoke("review", resign(review))
        self.assertEqual(code, 0, err)
        self.assertEqual(result["projection"]["rules"][0]["score"]["harmful"], 0)
