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


class InstalledCommandTests(GroundedFixture):
    def run_cli(self, command, event, *, env=None):
        artifact = Path(self.tmp.name, f"{command}.json")
        artifact.write_text(json.dumps(event_to_data(event)), encoding="utf-8")
        return subprocess.run([sys.executable, str(Path(__file__).with_name("dream_procedure.py")),
            command, "--palace", self.path, "--wing", "w", "--session-store", self.store,
            "--input", str(artifact)], capture_output=True, text=True, timeout=90, env=env)

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
            self.assertEqual(set(read_events(self.path, "w")), set(events))
            state = project_rules(read_events(self.path, "w"), as_of=NOW, policy=Policy()).rules[0]
            self.assertEqual(state.review_heads, (events[1].event_id,))
            self.assertEqual(state.score.helpful, 1)

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
