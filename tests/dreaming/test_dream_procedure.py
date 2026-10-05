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
from test_dream_procedural_palace import installed_palace, sanctioned_writer
from test_dream_procedural_validate import GroundedFixture
from test_dream_procedural import definition, parsed
from dream_procedural import Policy, project_rules
from datetime import timedelta
import math
import unittest
import os
import sys
import sqlite3
import subprocess
from types import SimpleNamespace


class CommandTests(GroundedFixture):
    def test_direct_commands_reject_copied_complete_delivery_without_new_captures(self):
        from delivery_fixtures import wrapped_packet
        from dream_metadata import canonical_json, content_hash
        from receipt_fixtures import receipts
        from itertools import product
        self.invoke("propose", self.proposal())
        self.invoke("review", self.review())
        transports = [wrapped_packet("Original commentary:\n{}\nEnd."), *[
            "Original commentary:\n" + canonical_json(canonical_json(r)) for r in receipts()[:2]], *[
            "Original commentary:\n" + canonical_json(canonical_json({"kind": kind, "quote": "parser"}))
            for kind in ("procedural_feedback", "procedural_feedback_abstention")]]
        for field, text in product(("user_message", "assistant_response", "drawer"), transports):
            session = self.refs[3]["session_id"]
            if field == "drawer":
                text = f"SESSION_ID: {session}\nOBSERVED_AT: {stamp()}\n" + text
                self.collection.rows["source-4"]["text"] = text
                ref = dict(self.refs[3], source_hash=content_hash(text), quote="parser")
            else:
                with sqlite3.connect(self.store) as con:
                    con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, session))
                ref = dict(source_kind="session_turn", source_id=session, session_id=session,
                           source_hash=content_hash(text), quote="parser",
                           turn_index=0, field=field)
            proposal = event_data("proposal", 80, origin_drawer_ids=[], evidence=[ref, *self.refs[:2]])
            review = event_to_data(self.review(81, parent_review_ids=[self.review().event_id]))
            packet = review["payload"]["validation_packet"]
            packet["evidence"][0] = ref
            review["payload"]["validation_digest"] = sha(packet)
            review["payload"]["dispositions"][0] = dict(evidence_id=ref["source_id"],
                disposition="supports", reason="Original observed support.", evidence=[ref])
            review = resign(review)
            outcome = event_data("outcome", 82, source_session_id=session, evidence=[ref])
            for command, event in (("propose", proposal), ("review", review), ("outcome", outcome)):
                for mode in ("append", "dry-run", "prepare"):
                    before = deepcopy(self.collection.rows)
                    target = Path(self.tmp.name, f"{field}-{command}-{mode}.json")
                    flags = (() if mode == "append" else ("--dry-run",) if mode == "dry-run"
                             else ("--prepare", "--out", str(target)))
                    code, result, err = self.invoke(command, event, *flags)
                    self.assertNotEqual(code, 0, (field, command, mode, err))
                    self.assertIn("generated", result["error"])
                    self.assertEqual(self.collection.rows, before)
                    self.assertFalse(target.exists())

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
        self.assertEqual(len(calls), 4)  # support/contrast, each with an original and capture channel
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

    def test_read_commands_recheck_sources_never_construct_writer_and_explain_retains_lineage(self):
        from dream_procedure import main
        self.invoke("propose", self.proposal())
        self.invoke("review", self.review())
        self.collection.embedding_function = lambda texts: [[1., 0.] for t in texts]
        before = deepcopy(self.collection.rows)
        out = StringIO()
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")), \
             patch("dream_procedure.now_utc", return_value=NOW), redirect_stdout(out):
            code = main(["guidance", "--palace", self.path, "--wing", "w",
                         "--repository", "owner/repo", "--task", "regression", "--include-candidates"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["trials"][0]["rule_id"], self.proposal().rule_id)
        self.assertEqual(self.collection.rows, before)
        code, result, err = self.invoke("explain", None, "--rule-id", self.proposal().rule_id)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(result["rule"]["events"]), 2)
        self.assertEqual(result["rule"]["score"]["helpful"], 0)
        self.collection.rows["source-1"]["text"] = "drifted"
        code, result, err = self.invoke("guidance", None, "--repository", "owner/repo",
                                       "--task", "regression", "--include-candidates")
        self.assertEqual((code, result["kind"]), (1, "evidence_unavailable"), err)
        code, result, err = self.invoke("explain", None, "--rule-id", self.proposal().rule_id)
        self.assertEqual(code, 1, err)
        self.assertIn("evidence_unavailable", result["rule"]["suppression_reasons"])

    def test_strict_read_refuses_incomplete_wal_sidecars_before_any_backend_open(self):
        wal = Path(self.path, "sqlite_exact.sqlite3-wal")
        wal.write_bytes(b"uncheckpointed state")
        before = wal.read_bytes()
        code, result, err = self.invoke("guidance", None, "--task", "test", "--repository", "owner/repo")
        self.assertEqual(code, 1, err)
        self.assertIn("WAL", result["error"])
        self.assertEqual(wal.read_bytes(), before)

    def test_read_does_not_trust_externally_filed_review_without_dispositions(self):
        from dream_procedural_palace import _record_body
        self.invoke("propose", self.proposal())
        review = self.review(dispositions=[])
        self.collection.add("w", "procedural", _record_body(review), "dream-procedure",
            {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(review)})
        self.collection.embedding_function = lambda texts: [[1., 0.] for t in texts]
        code, result, err = self.invoke("guidance", None, "--repository", "owner/repo",
                                       "--task", "regression", "--include-candidates")
        self.assertEqual((code, result["kind"]), (1, "evidence_unavailable"), err)

    def test_missing_local_model_fails_without_bootstrap_or_remote_embedder(self):
        from dream_procedural_palace import local_embedder
        with patch("mempalace.embedding.current_model_name", return_value="openai-compat"), \
             patch("mempalace.embedding.get_embedding_function", side_effect=AssertionError("remote")):
            with self.assertRaisesRegex(RuntimeError, "installed minilm"):
                local_embedder()
        with patch("mempalace.embedding.current_model_name", return_value="minilm"), \
             patch("chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2.ONNXMiniLM_L6_V2.DOWNLOAD_PATH",
                   Path(self.tmp.name, "absent-cache")), \
             patch("mempalace.embedding.get_embedding_function", side_effect=AssertionError("bootstrap")):
            with self.assertRaisesRegex(RuntimeError, "never download"):
                local_embedder()

    def test_unknown_palace_embedding_identity_cannot_be_invented(self):
        from mempalace.backends.embedding_wrapper import EmbeddingCollection
        from dream_procedural_palace import embed_texts
        inner = SimpleNamespace(get_stored_embedder_identity=lambda: None)
        with self.assertRaisesRegex(RuntimeError, "identity"):
            embed_texts(EmbeddingCollection(inner), ["some task"])

    def test_dry_run_rejects_oversized_record_body_like_real_append(self):
        self.invoke("propose", self.proposal())
        oversized = self.review(reason="r" * 16000)
        code, result, err = self.invoke("review", oversized, "--dry-run")
        self.assertEqual(code, 2, err)
        self.assertIn("32 KiB", result["error"])
        self.assertEqual(self.invoke("review", oversized)[0], 2)

    def test_duplicate_json_keys_and_backend_exceptions_have_explicit_failure_results(self):
        from dream_procedure import main
        data = json.dumps(event_to_data(self.proposal()))
        path = Path(self.tmp.name, "ambiguous.json")
        path.write_text(data.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1'))
        args = ["propose", "--palace", self.path, "--wing", "w", "--input", str(path), "--dry-run"]
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("dream_procedure.now_utc", return_value=NOW):
            self.assertEqual(main(args), 2)
        self.assertIn("duplicate", json.loads(out.getvalue())["error"])
        path.write_text(data)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("dream_procedure.read_events", side_effect=sqlite3.DatabaseError("damaged backing store")):
            self.assertEqual(main(args), 1)
        self.assertEqual(json.loads(out.getvalue())["kind"], "storage_integrity")


def guidance_events(rule=None, base=1, count=3):
    rule = rule or definition()
    review = event_data("review", base + 1, rule=rule)
    review["payload"]["validation_packet"]["repository"] = rule["scope"]["key"]
    review["payload"]["validation_digest"] = sha(review["payload"]["validation_packet"])
    return [parsed("proposal", base, rule=rule), parse_event(resign(review)),
            *[parsed("outcome", base + i + 2, rule=rule, repository=rule["scope"]["key"])
              for i in range(count)]]


class GuidanceTests(unittest.TestCase):
    def test_utf8_budget_drops_whole_ranked_items_and_preserves_default(self):
        from dream_procedural_palace import GuidanceLimits
        events = guidance_events(definition(statement="漢字" * 180, exceptions=["Never omit this exception."]))
        events += guidance_events(definition(statement="Second instruction.", rule_type="anti_pattern"), 50)
        embedder = lambda texts: [[1., 0.] if text != "Second instruction." else [.8, .6] for text in texts]
        default = self.guidance(events, embedder=embedder)
        wire = len(default.serialized.encode("utf-8"))
        self.assertGreater(wire, len(default.serialized))
        exact = self.guidance(events, embedder=embedder, limits=GuidanceLimits(max_bytes=wire))
        self.assertEqual(exact, default)
        smaller = self.guidance(events, embedder=embedder, limits=GuidanceLimits(max_bytes=wire - 1))
        self.assertEqual(smaller.data["item_count"], 1)
        self.assertEqual(smaller.data["rules"][0]["exceptions"], ["Never omit this exception."])
        self.assertLessEqual(len(smaller.serialized.encode("utf-8")), wire - 1)
        self.assertLessEqual(len(smaller.serialized), 6000)

    def guidance(self, events, *, embedder=None, at=NOW, **kwargs):
        from dream_procedural_palace import get_task_guidance, GuidanceLimits
        limits = kwargs.pop("limits", GuidanceLimits())
        return get_task_guidance(project_rules(events, as_of=at, policy=Policy()),
            task="Fix regression", repository="owner/repo",
            embedder=embedder or (lambda texts: [[1., 0.] for t in texts]),
            limits=limits, as_of=at, **kwargs)

    def test_exact_scope_and_maturity_filter_before_embedding(self):
        good = guidance_events()
        wrong = guidance_events(definition(scope={"kind": "repository", "key": "owner/repo2"}), 10)
        candidate = guidance_events(definition(statement="Candidate advice."), 20, 0)
        seen = []
        def embed(texts):
            seen.extend(texts)
            return [[1., 0.] for t in texts]
        result = self.guidance(good + wrong + candidate, embedder=embed)
        self.assertEqual([r["rule_id"] for r in result.data["rules"]], [good[0].rule_id])
        self.assertEqual(len(seen), 2)
        self.assertEqual(result.data["trials"], [])
        trials = self.guidance(good + wrong + candidate, include_candidates=True)
        self.assertEqual([r["rule_id"] for r in trials.data["trials"]], [candidate[0].rule_id])
        self.assertEqual(trials.data["trials"][0]["delivery"], "approved_candidate_trial")

    def test_stale_harm_conflict_retirement_replacement_are_never_delivered(self):
        base = guidance_events()
        rid = base[0].rule_id
        variants = [
            (base, NOW + timedelta(days=90)),
            (base + [parsed("outcome", 50, outcome="harmful")], NOW),
            (base + [parsed("review", 50)], NOW),
            (base + [parsed("review", 50, verdict="retire", parent_review_ids=[base[1].event_id])], NOW),
        ]
        other = definition(statement="Alternative reviewed statement.")
        variants.append((base + [parsed("proposal", 80, rule=other),
                         parsed("review", 50, verdict="replace", parent_review_ids=[base[1].event_id],
                                replacement_rule_id=parsed(rule=other).rule_id)], NOW))
        for events, at in variants:
            with self.subTest(at=at, events=len(events)):
                result = self.guidance(events, at=at, include_candidates=True)
                self.assertEqual(result.data["item_count"], 0)

    def test_cosine_boundary_stable_ties_and_invalid_vectors(self):
        base = guidance_events()
        self.assertEqual(self.guidance(base,
            embedder=lambda texts: [[1., 0.], [1., math.sqrt(15)]]).data["item_count"], 1)
        self.assertEqual(self.guidance(base,
            embedder=lambda texts: [[1., 0.], [.249, math.sqrt(1 - .249 ** 2)]]).data["item_count"], 0)
        for vectors in ([], [[0., 0.], [1., 0.]], [[1., 0.], [float("nan"), 0.]],
                        [[1., 0.], [1.]], [[1., 0.], [float("inf"), 1.]]):
            with self.subTest(vectors=vectors), self.assertRaises(RuntimeError):
                self.guidance(base, embedder=lambda texts: vectors)
        other = guidance_events(definition(statement="Another rule."), 20)
        result = self.guidance(list(reversed(base + other)))
        self.assertEqual([r["rule_id"] for r in result.data["rules"]], sorted([base[0].rule_id, other[0].rule_id]))

    def test_combined_five_items_exact_serialized_budget_preserves_exceptions(self):
        from dream_procedural_palace import GuidanceLimits
        events = []
        exceptions = [('quote " slash \\ newline\n café 漢字 ' * 10).strip()]
        for i in range(8):
            rule = definition(statement=f"Rule {i}.",
                              rule_type="rule" if i % 2 else "anti_pattern", exceptions=exceptions)
            events.extend(guidance_events(rule, 1 + 20 * i, 0 if i == 7 else 3))
        result = self.guidance(events, include_candidates=True)
        self.assertLessEqual(result.data["item_count"], 5)
        self.assertLessEqual(len(result.serialized), 6000)
        self.assertEqual(result.serialized, json.dumps(result.data, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False) + "\n")
        for item in result.data["rules"] + result.data["anti_patterns"] + result.data["trials"]:
            self.assertEqual(item["exceptions"], exceptions)
            self.assertEqual(item["applies_when"], "Fixing a reproducible defect.")
        small = self.guidance(events, include_candidates=True, limits=GuidanceLimits(max_chars=512))
        self.assertEqual(small.data["item_count"], 0)
        self.assertLessEqual(len(small.serialized), 512)
        with self.assertRaises(ValueError):
            GuidanceLimits(max_chars=10)
        with self.assertRaises(ValueError):
            GuidanceLimits(max_items=6)

    def test_no_rules_no_eligible_and_invalid_projection_are_distinct(self):
        self.assertEqual(self.guidance([]).data["status"], "no_rules")
        self.assertEqual(self.guidance([parsed()]).data["status"], "no_eligible_rules")
        with self.assertRaises(RuntimeError):
            self.guidance([parsed()] * 5001)
        with self.assertRaises(RuntimeError):
            self.guidance([parsed("proposal", i+1, rule=definition(statement=f"Rule {i}.")) for i in range(101)])
        with self.assertRaises(RuntimeError):
            self.guidance([parsed("review", 2)])


class SelfContainedCommandTests(GroundedFixture):
    invoke = CommandTests.invoke

    def raw_references(self):
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")

    def legacy(self, *events):
        from dream_procedural_palace import record_data
        for event in events:
            body, metadata = record_data(event)
            self.collection.add("w", "procedural", body, "dream-procedure", metadata)

    def test_publication_captures_raw_full_fields_and_drawer_witnesses_before_host_loss(self):
        from dream_procedural_sources import read_source_records
        from dream_procedural_validate import EvidenceReader
        for ref in self.refs[:2]:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message", quote="Focused test")
        proposal = self.proposal()
        self.assertEqual(self.invoke("propose", proposal)[0], 0)
        self.assertEqual(read_source_records(self.path, "w").record_count, 3)
        bodies = [r["text"] for r in self.collection.rows.values()
                  if r["metadata"]["room"] == "procedural-sources"]
        self.assertEqual(sum('"captured_text":' in text for text in bodies), 2)
        os.unlink(self.store)
        with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")):
            self.assertEqual(self.invoke("review", self.review())[0], 0)
            outcome = parse_event(event_data("outcome", 3, evidence=[self.refs[0]],
                                            source_session_id=self.refs[0]["session_id"]))
            self.assertEqual(self.invoke("outcome", outcome)[0], 0)
            reader = EvidenceReader(self.path, "w")
            self.assertEqual(reader.source_text(self.refs[0]),
                             self.collection.rows["source-1"]["text"])
            code, result, err = self.invoke("explain", None, "--rule-id", proposal.rule_id)
            self.assertEqual((code, result["status"]), (0, "ok"), err)
            self.collection.embedding_function = lambda texts: [[1., 0.] for _ in texts]
            code, result, err = self.invoke("guidance", None, "--repository", "owner/repo",
                "--task", "regression", "--include-candidates")
            self.assertEqual((code, result.get("item_count")), (0, 1), err)

    def test_bare_append_captures_and_enforces_admission_without_callback(self):
        from dream_procedural_palace import append_event, read_events, verify_event_sources
        from dream_procedural_sources import read_source_records
        writer = sanctioned_writer(self.path, self.collection)
        invalid = parse_event(event_data(origin_drawer_ids=[], evidence=self.refs[:2]))
        with self.assertRaisesRegex(ValueError, "three"):
            append_event(self.path, "w", invalid, writer=writer, clock=lambda: NOW)
        self.assertEqual(read_source_records(self.path, "w").record_count, 0)
        self.raw_references()
        proposal = self.proposal()
        with patch("dream_sessions.load_session_turns", side_effect=AssertionError("hidden host fallback")):
            with self.assertRaisesRegex(ValueError, "wing-scoped EvidenceReader"):
                verify_event_sources(self.path, proposal)
        result = append_event(self.path, "w", proposal, writer=writer, clock=lambda: NOW)
        self.assertEqual(result.status, "appended")
        self.assertEqual(read_source_records(self.path, "w").record_count, 3)
        os.unlink(self.store)
        with patch("dream_procedural_validate.acquire_original", side_effect=AssertionError("acquire")), \
             patch.object(writer, "add_drawer", side_effect=AssertionError("write")):
            retry = append_event(self.path, "w", proposal, writer=writer,
                                 clock=lambda: NOW - timedelta(days=1))
        self.assertEqual(retry.status, "already_exists")
        self.assertEqual(read_events(self.path, "w"), [proposal])

    def test_explicit_legacy_capture_is_read_only_when_dry_and_does_not_rewrite_events(self):
        from dream_procedural_palace import read_events
        self.raw_references()
        proposal, review = self.proposal(), self.review(verdict="retire")
        self.legacy(proposal, review)
        code, result, _ = self.invoke("explain", None, "--rule-id", proposal.rule_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["source_diagnostics"][0]["code"], "uncaptured")
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store, "--dry-run")
        self.assertEqual((code, result.get("pending")), (0, 3), err)
        self.assertEqual(self.collection.rows, before)
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result.get("captured")), (0, 3), err)
        self.assertEqual(read_events(self.path, "w"), [proposal, review])
        os.unlink(self.store)
        code, result, err = self.invoke("explain", None, "--rule-id", proposal.rule_id)
        self.assertEqual((code, result["status"]), (0, "ok"), err)
        self.assertIn("retired", result["rule"]["suppression_reasons"])
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result.get("already_captured")), (0, 3), err)

    def test_null_session_legacy_is_blocked_not_repaired_and_capture_reports_failures(self):
        from test_dream_procedural import resign
        data = event_to_data(self.proposal())
        data["payload"]["evidence"][0]["session_id"] = None
        legacy = parse_event(resign(data))
        self.legacy(legacy)
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result.get("status")), (1, "blocked"), err)
        self.assertEqual(result["failures"][0]["code"], "missing_session")
        self.assertEqual(self.collection.rows, before)

    def test_partial_capture_retry_uses_captured_originals_after_host_turn_is_removed(self):
        from dream_procedural_palace import append_event, read_events
        from dream_procedural_sources import read_source_records
        self.raw_references()
        proposal = self.proposal()
        writer = sanctioned_writer(self.path, self.collection)
        handler = writer._tools["mempalace_add_drawer"]["handler"]
        calls = []
        from functools import wraps
        @wraps(handler)
        def interrupted(**kwargs):
            calls.append(kwargs["room"])
            if len(calls) == 2:
                raise RuntimeError("capture interruption")
            return handler(**kwargs)
        writer._tools["mempalace_add_drawer"]["handler"] = interrupted
        with self.assertRaisesRegex(RuntimeError, "capture interruption"):
            append_event(self.path, "w", proposal, writer=writer, clock=lambda: NOW)
        self.assertEqual(read_events(self.path, "w"), [])
        self.assertEqual(read_source_records(self.path, "w").record_count, 1)
        with sqlite3.connect(self.store) as con:
            con.execute("DELETE FROM turns WHERE session_id=?", (self.refs[0]["session_id"],))
            con.execute("DELETE FROM sessions WHERE id=?", (self.refs[0]["session_id"],))
        result = append_event(self.path, "w", proposal,
            writer=sanctioned_writer(self.path, self.collection), clock=lambda: NOW)
        self.assertEqual(result.status, "appended")
        self.assertEqual(read_source_records(self.path, "w").record_count, 3)

    def test_prepare_requires_hash_for_ambiguous_captured_versions(self):
        from dream_metadata import content_hash
        from dream_procedural_sources import capture_source
        from dream_procedural_validate import acquire_original, EvidenceReader
        self.raw_references()
        original = self.proposal().payload.evidence[0]
        writer = sanctioned_writer(self.path, self.collection)
        for text in (self.refs[0]["quote"], self.refs[0]["quote"] + "\nAnother version"):
            with sqlite3.connect(self.store) as con:
                con.execute("UPDATE turns SET user_message=? WHERE session_id=?", (text, original.session_id))
            from dataclasses import replace
            ref = replace(original, source_hash=content_hash(text))
            capture_source(acquire_original(ref, palace=self.path, session_store=self.store),
                palace=self.path, wing="w", captured_at=NOW, captured_by="test", writer=writer)
        os.unlink(self.store)
        reader = EvidenceReader(self.path, "w")
        partial = {k: v for k, v in self.refs[0].items() if k != "source_hash"}
        with self.assertRaisesRegex(ValueError, "explicit source_hash"):
            reader.source_text(partial)
        self.assertEqual(reader.source_text(self.refs[0]), self.refs[0]["quote"])

    def test_migration_reports_missing_drawer_and_projected_headroom_without_writes(self):
        self.legacy(self.proposal())
        del self.collection.rows["source-1"]
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result.get("status")), (1, "blocked"), err)
        self.assertEqual(result["failures"][0]["code"], "original_drawer_missing")
        self.assertEqual((result["source_count"], result["already_captured"],
                          result["pending"], result["captured"], result["record_count"]), (3, 0, 2, 0, 0))
        self.assertEqual(self.collection.rows, before)
        self.collection.rows["source-1"] = {"id": "source-1", "text": self.refs[0]["quote"],
                                          "metadata": {"wing": "w", "room": "diary"}}
        with patch("dream_procedural_sources.RECORD_WARNING_THRESHOLD", 3):
            code, result, err = self.invoke("capture-sources", None, "--session-store", self.store, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(result["capacity"]["projected_record_count"], 3)
        self.assertEqual(result["capacity"]["records_until_warning"], 0)
        self.assertTrue(result["warnings"])

    def test_migration_missing_host_does_not_count_failed_acquisitions_as_captures(self):
        self.legacy(self.proposal())
        Path(self.store).unlink()
        before = deepcopy(self.collection.rows)
        for options in (("--dry-run",), ()):
            with self.subTest(options=options):
                code, result, err = self.invoke("capture-sources", None,
                    "--session-store", self.store, *options)
                self.assertEqual((code, result["status"]), (1, "blocked"), err)
                self.assertEqual((result["source_count"], result["already_captured"],
                                  result["pending"], result["captured"], result["failed"],
                                  result["record_count"]), (3, 0, 0, 0, 3, 0))
                self.assertEqual({failure["code"] for failure in result["failures"]},
                                 {"session_store_unavailable"})
                self.assertEqual(self.collection.rows, before)

    def test_migration_counts_only_distinct_verified_captures_among_acquired_and_failed_sources(self):
        from dream_procedural_sources import capture_source
        from dream_procedural_validate import acquire_original
        proposal = self.proposal()
        ref = proposal.payload.evidence[0]
        capture_source(acquire_original(ref, palace=self.path, session_store=self.store),
            palace=self.path, wing="w", captured_at=NOW, captured_by="test",
            writer=sanctioned_writer(self.path, self.collection))
        self.refs[0]["quote"] = "Focused test"
        self.legacy(proposal, self.review())
        del self.collection.rows["source-3"]
        before = deepcopy(self.collection.rows)
        for options in (("--dry-run",), ()):
            with self.subTest(options=options):
                code, result, err = self.invoke("capture-sources", None,
                    "--session-store", self.store, *options)
                self.assertEqual((code, result["status"]), (1, "blocked"), err)
                self.assertEqual((result["reference_count"], result["source_count"],
                                  result["already_captured"], result["pending"],
                                  result["captured"], result["failed"], result["record_count"]),
                                 (4, 3, 1, 1, 0, 1, 1))
                self.assertEqual(result["failures"][0]["code"], "original_drawer_missing")
                self.assertEqual(self.collection.rows, before)
        self.collection.rows["source-1"]["text"] = "drifted original"
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result["status"]), (1, "blocked"), err)
        self.assertEqual((result["already_captured"], result["pending"], result["record_count"]), (0, 1, 1))

    def test_postcapture_readback_rechecks_drawer_instead_of_using_prewrite_resolution(self):
        from dream_procedural_palace import append_event, read_events
        from functools import wraps
        writer = sanctioned_writer(self.path, self.collection)
        handler = writer._tools["mempalace_add_drawer"]["handler"]
        @wraps(handler)
        def corrupt_original(**kwargs):
            result = handler(**kwargs)
            if kwargs["room"] == "procedural-sources":
                self.collection.rows["source-1"]["text"] = "changed during capture"
            return result
        writer._tools["mempalace_add_drawer"]["handler"] = corrupt_original
        with self.assertRaisesRegex(ValueError, "drift"):
            append_event(self.path, "w", self.proposal(), writer=writer, clock=lambda: NOW)
        self.assertEqual(read_events(self.path, "w"), [])

    def test_failed_source_and_event_ack_readback_boundaries_are_retryable(self):
        from dream_procedural_palace import append_event, read_events
        from dream_procedural_sources import read_source_records
        from functools import wraps
        self.raw_references()
        proposal = self.proposal()
        for boundary in ("source_before", "source_after", "event_before", "event_after"):
            with self.subTest(boundary=boundary):
                self.collection.rows = {k: v for k, v in self.collection.rows.items()
                                        if v["metadata"]["room"] == "diary"}
                writer = sanctioned_writer(self.path, self.collection)
                handler = writer._tools["mempalace_add_drawer"]["handler"]
                count = 0
                @wraps(handler)
                def interrupt(**kwargs):
                    nonlocal count
                    is_source = kwargs["room"] == "procedural-sources"
                    count += is_source
                    selected = (is_source and count == 3) if boundary.startswith("source") else not is_source
                    if selected and boundary.endswith("before"):
                        return {"success": True}
                    result = handler(**kwargs)
                    if selected:
                        raise RuntimeError("lost acknowledgment")
                    return result
                writer._tools["mempalace_add_drawer"]["handler"] = interrupt
                with self.assertRaisesRegex(RuntimeError, "readback|acknowledgment"):
                    append_event(self.path, "w", proposal, writer=writer, clock=lambda: NOW)
                self.assertEqual(len(read_events(self.path, "w")), int(boundary == "event_after"))
                self.assertEqual(read_source_records(self.path, "w").record_count,
                                 2 if boundary == "source_before" else 3)
                # Captured originals need not survive. Keep only the one never written.
                if boundary == "source_before":
                    result = append_event(self.path, "w", proposal,
                        writer=sanctioned_writer(self.path, self.collection), clock=lambda: NOW)
                else:
                    with patch("dream_procedural_validate.acquire_original", side_effect=AssertionError("host")):
                        result = append_event(self.path, "w", proposal,
                            writer=sanctioned_writer(self.path, self.collection), clock=lambda: NOW)
                self.assertEqual(result.status, "already_exists" if boundary == "event_after" else "appended")
                self.assertEqual(read_events(self.path, "w"), [proposal])

    def test_source_size_rejected_before_any_publication_or_migration_write(self):
        from dream_metadata import content_hash
        from dream_procedural_sources import MAX_RECORD_BYTES
        self.raw_references()
        text = "oversized " + "x" * MAX_RECORD_BYTES
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET user_message=? WHERE session_id=?", (text, self.refs[0]["session_id"]))
        self.refs[0].update(quote="oversized", source_hash=content_hash(text))
        proposal = self.proposal()
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("propose", proposal, "--dry-run")
        self.assertEqual(code, 2, err)
        self.assertIn("4 MiB", result["error"])
        code, result, err = self.invoke("propose", proposal)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.collection.rows, before)
        self.legacy(proposal)
        before = deepcopy(self.collection.rows)
        code, result, err = self.invoke("capture-sources", None, "--session-store", self.store)
        self.assertEqual((code, result.get("status")), (1, "blocked"), err)
        self.assertEqual(result["failures"][0]["code"], "source_size")
        self.assertEqual(self.collection.rows, before)

    def test_diagnostics_distinguish_missing_drawer_and_corrupt_capture_without_host_fallback(self):
        from dream_procedural_validate import inspect_published_sources
        self.publish(self.proposal(), self.review())
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate.acquire_original", side_effect=AssertionError("host")), \
             patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")):
            report = inspect_published_sources(self.path, "w")
            self.assertEqual((report["status"], report["source_count"]), ("ok", 3))
            self.assertEqual(self.collection.rows, before)
        del self.collection.rows["source-1"]
        report = inspect_published_sources(self.path, "w")
        self.assertEqual(report["failures"][0]["code"], "original_drawer_missing")
        self.collection.rows = before
        source = next(row for row in self.collection.rows.values()
                      if row["metadata"]["room"] == "procedural-sources")
        source["text"] = source["text"].replace("owner/repo", "other/repo")
        code, result, err = self.invoke("explain", None, "--rule-id", self.proposal().rule_id)
        self.assertEqual(code, 1, err)
        self.assertEqual(result["source_diagnostics"][0]["code"], "corrupt_capture")

    def test_inspector_requires_retained_origins_separately_from_captured_evidence(self):
        from dream_procedural_validate import inspect_published_sources
        proposals = [parse_event(event_data(number=n, origin_drawer_ids=["source-4"],
                                           evidence=self.refs[:3])) for n in (1, 5)]
        review = self.review()
        self.publish(*proposals, review,
                     self.review(3, verdict="retire", parent_review_ids=[review.event_id]))
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate.acquire_original", side_effect=AssertionError("host")), \
             patch("dream_procedural_validate.project_rules", side_effect=AssertionError("eligibility")), \
             patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")):
            report = inspect_published_sources(self.path, "w")
            self.assertEqual((report["status"], report["reference_count"], report["source_count"],
                              report["record_count"]), ("ok", 3, 3, 3))
            self.assertEqual(self.collection.rows, before)
            del self.collection.rows["source-4"]
            damaged = deepcopy(self.collection.rows)
            report = inspect_published_sources(self.path, "w")
            self.assertEqual(report["status"], "blocked")
            self.assertEqual((report["event_count"], report["origin_count"], report["failed"]), (4, 1, 1))
            self.assertEqual(report["failures"][0]["code"], "origin_drawer_missing")
            self.assertEqual(report["failures"][0]["source_id"], "source-4")
            self.assertEqual(report["failures"][0]["reference_kind"], "origin")
            self.assertEqual((report["reference_count"], report["source_count"],
                              report["record_count"]), (3, 3, 3))
            self.assertEqual(self.collection.rows, damaged)

    def test_inspector_distinguishes_invalid_origin_chunks_from_missing_origin(self):
        from dream_procedural_validate import inspect_published_sources
        self.publish(parse_event(event_data(origin_drawer_ids=["source-4"], evidence=self.refs[:3])))
        del self.collection.rows["source-4"]
        for index in (0, 2):
            name = f"source-4_chunk_{index:06d}"
            self.collection.rows[name] = {"id": name, "text": "lineage", "metadata": {
                "wing": "w", "room": "diary", "parent_drawer_id": "source-4",
                "chunk_index": index, "id_recipe": "v3", "added_by": "original-agent"}}
        before = deepcopy(self.collection.rows)
        report = inspect_published_sources(self.path, "w")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual((report["origin_count"], report["failed"]), (1, 1))
        self.assertEqual(report["failures"][0]["code"], "origin_drawer_invalid")
        self.assertIn("incomplete source drawer chunks", report["failures"][0]["error"])
        self.assertEqual(self.collection.rows, before)

    def test_preparation_and_validation_classify_corrupt_capture_as_integrity_not_request(self):
        self.publish(self.proposal())
        source = next(row for row in self.collection.rows.values()
                      if row["metadata"]["room"] == "procedural-sources")
        source["text"] = source["text"].replace("owner/repo", "other/repo")
        draft = event_to_data(self.proposal(number=8))
        del draft["digest"]
        del draft["payload"]["evidence"][0]["source_hash"]
        code, result, err = self.invoke("propose", draft, "--prepare", "--out",
                                        str(Path(self.tmp.name, "prepared-corrupt.json")))
        self.assertEqual((code, result.get("code")), (1, "corrupt_capture"), err)
        self.collection.query = lambda **kwargs: {
            "ids": [["source-1"]], "documents": [[self.refs[0]["quote"]]],
            "metadatas": [[self.collection.rows["source-1"]["metadata"]]]}
        self.collection.embedding_function = lambda texts: [[1., 0.] for _ in texts]
        code, result, err = self.invoke("validate", None, "--rule-id", self.proposal().rule_id,
            "--contrast-query", "When does this fail?", "--out", str(Path(self.tmp.name, "corrupt-packet.json")))
        self.assertEqual((code, result.get("code")), (1, "corrupt_capture"), err)


class InstalledCommandTests(GroundedFixture):
    def run_cli(self, command, event, *, env=None):
        artifact = Path(self.tmp.name, f"{command}.json")
        artifact.write_text(json.dumps(event_to_data(event)), encoding="utf-8")
        script = Path(__file__).resolve().parents[2] / "skills" / "dreaming" / "scripts" / "dream_procedure.py"
        return subprocess.run([sys.executable, str(script),
            command, "--palace", self.path, "--wing", "w", "--session-store", self.store,
            "--input", str(artifact)], capture_output=True, text=True, timeout=90, env=env)

    def test_real_handlers_mixed_sources_survive_entire_host_directory_loss_and_closed_reads(self):
        from dream_metadata import content_hash
        from dream_procedural_sources import read_source_records, source_key
        from dream_procedural_validate import EvidenceReader, inspect_published_sources
        from dream_procedural_palace import append_event, read_events
        from dream_procedure import main
        from mempalace.palace import get_backend_for_palace
        import shutil
        self.storage_patch.stop()
        host = Path(self.tmp.name, "host-session-directory")
        host.mkdir()
        moved = host / "session-store.db"
        Path(self.store).rename(moved)
        self.store = str(moved)
        texts = [" \tFull raw user field\n```fsharp\nlet x = \"漢字\"\n```\r\n\u2028tail \n",
                 "\n Full raw assistant field, not just a quotation. \u2029 \t"]
        with sqlite3.connect(self.store) as con:
            for ref, field, text in zip(self.refs[:2], ("user_message", "assistant_response"), texts):
                con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, ref["session_id"]))
                ref.update(source_kind="session_turn", source_id=ref["session_id"], field=field,
                           turn_index=0, source_hash=content_hash(text), quote="raw")
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                with patch.dict(server._config._file_config, {"chunk_size": 73}):
                    stored = writer.add_drawer("w", "diary", self.refs[2]["quote"])
                physical = stored["drawer_id"] + "_chunk_000001"
                self.refs[2]["source_id"] = physical
                events = [self.proposal(), self.review()]
                for event in events:
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
                shutil.rmtree(host)
                col = server._get_collection()
                col._handle.conn.execute("PRAGMA wal_autocheckpoint=0")
                before_state = tuple(col._handle.conn.iterdump())
                before = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                          if p.is_file() and not p.name.endswith("-shm")}

                def readonly_commands(suffix):
                    with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")), \
                         patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
                         patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")):
                        reader = EvidenceReader(self.path, "w")
                        for ref, text in zip(self.refs[:2], texts):
                            self.assertEqual(reader.source_text(ref), text)
                        self.assertEqual(reader.resolve(events[0].payload.evidence[2]).reference.source_id, physical)
                        index = read_source_records(self.path, "w")
                        self.assertIn(source_key(events[0].payload.evidence[2]), index.locators)
                        self.assertEqual(inspect_published_sources(self.path, "w")["status"], "ok")
                        for command, extra in (
                            ("guidance", ["--task", events[0].payload.definition.statement,
                                          "--repository", "owner/repo", "--include-candidates"]),
                            ("explain", ["--rule-id", events[0].rule_id]),
                            ("validate", ["--rule-id", events[0].rule_id, "--contrast-query", "regression failed",
                                          "--out", str(Path(self.tmp.name, f"packet-{suffix}.json"))])):
                            out, err = StringIO(), StringIO()
                            with redirect_stdout(out), redirect_stderr(err), \
                                 patch("dream_procedure.now_utc", return_value=NOW):
                                code = main([command, "--palace", self.path, "--wing", "w",
                                             "--session-store", self.store, *extra])
                            self.assertEqual(code, 0, command + ": " + out.getvalue() + err.getvalue())
                readonly_commands("live")
                self.assertEqual(tuple(col._handle.conn.iterdump()), before_state)
                self.assertEqual({p.name: p.read_bytes() for p in Path(self.path).iterdir()
                                  if p.is_file() and not p.name.endswith("-shm")}, before)
            get_backend_for_palace(self.path).close_palace(self.path)
            before = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                      if p.is_file() and not p.name.endswith("-shm")}
            readonly_commands("closed")
            self.assertEqual({p.name: p.read_bytes() for p in Path(self.path).iterdir()
                              if p.is_file() and not p.name.endswith("-shm")}, before)
            self.assertEqual(set(read_events(self.path, "w")), set(events))

    def test_fresh_cli_subprocess_propose_review_outcome_persist_without_opener_patch(self):
        from dream_procedural_palace import read_events
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            ok, reason = server._acquire_mcp_writer_lock()
            self.assertTrue(ok, reason)
            for ref in self.refs:
                source = server.TOOLS["mempalace_add_drawer"]["handler"](
                    wing="w", room="diary", content=ref["quote"])
                self.assertTrue(source["success"], source)
                ref["source_id"] = source["drawer_id"]
            server._release_mcp_writer_lock()
            events = [self.proposal(), self.review(), parse_event(event_data(
                "outcome", 3, source_session_id=self.refs[0]["session_id"], evidence=[self.refs[0]]))]
            for command, event in zip(("propose", "review", "outcome"), events):
                completed = self.run_cli(command, event)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["status"], "appended")
                self.assertEqual(result["event_id"], event.event_id)
                if command == "propose":
                    Path(self.store).unlink()
            self.assertEqual(set(read_events(self.path, "w")), set(events))
            state = project_rules(read_events(self.path, "w"), as_of=NOW, policy=Policy()).rules[0]
            self.assertEqual(state.review_heads, (events[1].event_id,))
            self.assertEqual(state.score.helpful, 1)

    def test_real_cli_legacy_capture_includes_adverse_retired_raw_history(self):
        from dream_procedural_palace import read_events, record_data
        from dream_procedural_sources import read_source_records
        self.storage_patch.stop()
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        proposal = self.proposal()
        held = self.review(verdict="hold", dispositions=[
            *event_to_data(self.review())["payload"]["dispositions"],
            {"evidence_id": self.refs[3]["source_id"], "disposition": "contradicts",
             "reason": "Original adverse observation.", "evidence": [self.refs[3]]}])
        retired = self.review(4, verdict="retire", parent_review_ids=[held.event_id])
        events = [proposal, held, retired]
        with installed_palace(self.path):
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for event in events:
                    body, metadata = record_data(event)
                    writer.add_drawer("w", "procedural", body, added_by="dream-procedure", metadata=metadata)
            def run(command, *extra):
                script = Path(__file__).resolve().parents[2] / "skills" / "dreaming" / "scripts" / "dream_procedure.py"
                completed = subprocess.run([sys.executable, str(script),
                    command, "--palace", self.path, "--wing", "w", "--session-store", self.store, *extra],
                    capture_output=True, text=True, timeout=90)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                return json.loads(completed.stdout)
            before = set(read_events(self.path, "w"))
            self.assertEqual(run("capture-sources", "--dry-run")["pending"], 4)
            self.assertEqual(read_source_records(self.path, "w").record_count, 0)
            result = run("capture-sources")
            self.assertEqual((result["captured"], result["failed"]), (4, 0))
            Path(self.store).unlink()
            self.assertEqual(run("capture-sources")["already_captured"], 4)
            explanation = run("explain", "--rule-id", proposal.rule_id)
            self.assertEqual(explanation["status"], "ok")
            self.assertIn("retired", explanation["rule"]["suppression_reasons"])
            self.assertEqual(set(read_events(self.path, "w")), before)

    def test_cli_refuses_other_writer_and_operator_readonly_without_appending(self):
        from dream_procedural_palace import read_events
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            ok, reason = server._acquire_mcp_writer_lock()
            self.assertTrue(ok, reason)
            for ref in self.refs:
                result = server.tool_add_drawer("w", "diary", ref["quote"])
                self.assertTrue(result["success"], result)
                ref["source_id"] = result["drawer_id"]
            owner = server._MCP_WRITER_LOCK_CM
            before = tuple(server._get_collection()._handle.conn.iterdump())
            refused = self.run_cli("propose", self.proposal())
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("writer", json.loads(refused.stdout)["error"])
            self.assertEqual(read_events(self.path, "w"), [])
            self.assertEqual(tuple(server._get_collection()._handle.conn.iterdump()), before)
            self.assertIs(server._MCP_WRITER_LOCK_CM, owner)
            server._release_mcp_writer_lock()
            refused = self.run_cli("propose", self.proposal(),
                                   env={**os.environ, "MEMPALACE_MCP_READ_ONLY": "1"})
            self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
            self.assertIn("read-only", json.loads(refused.stdout)["error"])
            self.assertEqual(read_events(self.path, "w"), [])

    def test_real_sqlite_commands_keep_live_writer_and_read_application_state_unchanged(self):
        from dream_procedure import main
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for ref in self.refs:
                    ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"])["drawer_id"]

                def run(command, event=None, *extra):
                    args = [command, "--palace", self.path, "--wing", "w", "--session-store", self.store]
                    if event is not None:
                        path = Path(self.tmp.name, "event.json")
                        path.write_text(json.dumps(event_to_data(event)))
                        args += ["--input", str(path)]
                    out, err = StringIO(), StringIO()
                    with redirect_stdout(out), redirect_stderr(err), patch("dream_procedure.now_utc", return_value=NOW):
                        code = main([*args, *extra])
                    self.assertEqual(code, 0, err.getvalue())
                    return json.loads(out.getvalue()), out.getvalue()

                run("propose", self.proposal())
                packet_path = Path(self.tmp.name, "packet.json")
                packet, _ = run("validate", None, "--rule-id", self.proposal().rule_id,
                                 "--contrast-query", "focused regression test harmful misleading",
                                 "--out", str(packet_path))
                self.assertGreaterEqual(len(packet["validation_packet"]["evidence"]), 3)
                review = event_to_data(self.review())
                review["payload"]["validation_packet"] = packet["validation_packet"]
                review["payload"]["validation_digest"] = packet["validation_digest"]
                review["payload"]["dispositions"] = [
                    {"evidence_id": r["source_id"], "disposition": "supports",
                     "reason": "Reviewed original regression observation.", "evidence": [r]}
                    for r in packet["validation_packet"]["evidence"]]
                review = parse_event(resign(review))
                run("review", review)
                self.assertEqual(run("review", review)[0]["status"], "already_exists")
                for i, ref in enumerate(self.refs[:3]):
                    run("outcome", parse_event(event_data("outcome", 10 + i,
                        source_session_id=ref["session_id"], evidence=[ref])))
                col = server._get_collection()
                owner = server._MCP_WRITER_LOCK_CM
                before_state = tuple(col._handle.conn.iterdump())
                before = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                          if p.is_file() and not p.name.endswith("-shm")}
                source_before = Path(self.store).read_bytes()
                with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
                     patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")):
                    guidance, text = run("guidance", None, "--task", self.proposal().payload.definition.statement,
                                         "--repository", "owner/repo")
                    self.assertEqual(guidance["item_count"], 1)
                    self.assertEqual(guidance["rules"][0]["maturity"], "established")
                    self.assertLessEqual(len(text), 6000)
                    explained, _ = run("explain", None, "--rule-id", self.proposal().rule_id)
                    self.assertEqual(explained["rule"]["score"]["helpful"], 3)
                    self.assertEqual(len(explained["rule"]["events"]), 5)
                after = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                         if p.is_file() and not p.name.endswith("-shm")}
                self.assertEqual(after, before)
                self.assertEqual(tuple(col._handle.conn.iterdump()), before_state)
                self.assertIs(server._MCP_WRITER_LOCK_CM, owner)
                self.assertFalse(col._handle.closed)
                self.assertEqual(source_before, Path(self.store).read_bytes())
