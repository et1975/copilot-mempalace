"""Read-only health and grounded, nonpublishing receipt drafts."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import dream_palace
from dream_metadata import canonical_json, content_hash
from dream_procedural import event_to_data, parse_event
from test_dream_procedural import NOW, event_data, stamp
from test_dream_procedural_validate import GroundedFixture


class DraftFixture(GroundedFixture):
    def receipt(self, **changes):
        return dict({
            "schema_version": 1, "repository": "owner/repo",
            "session_id": self.refs[0]["session_id"], "generation": 1,
            "task": self.refs[0]["quote"],
            "task_digest": content_hash(self.refs[0]["quote"]),
            "delivered_rule_ids": [self.proposal().rule_id], "guidance_as_of": stamp(),
        }, **changes)

    def run_command(self, command, *extra, receipt=None, at=NOW):
        from dream_procedure import main
        args = [command, "--palace", self.path, "--wing", "w", "--repository", "owner/repo"]
        if receipt is not None:
            input_path = Path(self.tmp.name, "receipt.json")
            input_path.write_text(canonical_json(receipt), encoding="utf-8")
            args += ["--input", str(input_path)]
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error), \
             patch("dream_procedure.now_utc", return_value=at):
            code = main([*args, *extra])
        return code, json.loads(output.getvalue()), error.getvalue()

    def draft(self, name="draft.json", *, receipt=None, store=True):
        args = ["--out", str(Path(self.tmp.name, name))]
        if store:
            args += ["--session-store", self.store]
        return self.run_command("draft", *args, receipt=receipt or self.receipt())


def test_shared_transport_guard_rejects_packet_echo_and_accepts_independent_later_observation():
    from delivery_fixtures import applicability, case, wrapped_packet
    from dream_procedural_drafts import is_procedural_echo
    for wrapper in ("{}", "```json\n{}\n```", "Copied:\n{}\nLater commentary"):
        assert is_procedural_echo(wrapped_packet(wrapper, encoded=True))
    _, current, _, guidance = case()
    assert is_procedural_echo(canonical_json(applicability(current["context"], guidance["rules"])))
    assert not is_procedural_echo("A later run independently failed on input X with error Y.")


def test_feedback_copy_is_draft_lineage_not_original_even_when_escaped():
    from dream_procedural_drafts import is_procedural_echo
    for kind in ("procedural_feedback", "procedural_feedback_abstention"):
        body = canonical_json({"kind": kind, "quote": "observed defect"})
        for wrapper in ("{}", "```json\n{}\n```", "Copied:\n{}\nEnd."):
            for value in (body, canonical_json(body)):
                assert is_procedural_echo(wrapper.format(value))


class StatusTests(DraftFixture):
    def test_empty_health_needs_no_host_writer_or_embedder(self):
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")), \
             patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch("dream_procedural_palace.local_embedder", side_effect=AssertionError("embedder")):
            code, result, error = self.run_command("status")
        self.assertEqual(code, 0, error)
        self.assertEqual((result["schema_version"], result["readiness"]), (1, "no_rules"))
        self.assertEqual(result["capture_coverage"]["reference_count"], 0)
        self.assertEqual(result["evidence_errors"], [])
        self.assertEqual(self.collection.rows, before)

    def test_health_distinguishes_uncaptured_missing_drift_and_stale_conflicts(self):
        from dream_procedural_palace import record_data
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        code, health, _ = self.run_command("status")
        self.assertEqual(code, 1)
        self.assertIn("uncaptured", {e["code"] for e in health["evidence_errors"]})
        # Restore a clean fixture, then use real admission/capture.
        self.collection.rows = {k: v for k, v in self.collection.rows.items()
                                if v["metadata"]["room"] != "procedural"}
        self.publish(self.proposal(), self.review())
        code, health, _ = self.run_command("status", at=NOW + timedelta(days=91))
        self.assertEqual(code, 0)
        self.assertEqual(health["suppression_counts"]["stale_validation"], 1)
        body, metadata = record_data(self.review(3))
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        self.assertEqual(self.run_command("status")[1]["suppression_counts"]["conflicted"], 1)
        original = self.collection.rows.pop("source-1")
        code, health, _ = self.run_command("status")
        self.assertEqual(code, 1)
        self.assertIn("original_drawer_missing", {e["code"] for e in health["evidence_errors"]})
        self.collection.rows["source-1"] = dict(original, text="drift")
        self.assertIn("original_drawer_drift",
                      {e["code"] for e in self.run_command("status")[1]["evidence_errors"]})

    def test_capacity_and_corrupt_storage_are_not_healthy_empty(self):
        self.publish(self.proposal())
        with patch("dream_procedural_sources.RECORD_WARNING_THRESHOLD", 3):
            code, result, error = self.run_command("status")
        self.assertEqual(code, 0, error)
        self.assertTrue(result["warnings"])
        self.assertEqual(result["record_count"], 3)
        self.assertGreater(result["encoded_bytes"], 0)
        record = next(v for v in self.collection.rows.values()
                      if v["metadata"]["room"] == "procedural-sources")
        record["text"] = record["text"].replace('"observed_at":', '"corrupted_at":')
        code, result, _ = self.run_command("status")
        self.assertEqual((code, result["status"]), (1, "error"))
        self.assertNotIn("readiness", result)

    def test_malformed_events_and_missing_definitions_are_not_no_rules(self):
        from dream_procedural_palace import record_data
        body, metadata = record_data(self.review())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        code, result, _ = self.run_command("status")
        self.assertEqual(code, 1)
        self.assertNotEqual(result.get("readiness"), "no_rules")
        self.collection.add("w", "procedural", "malformed event", "dream-procedure", {})
        code, result, _ = self.run_command("status")
        self.assertEqual((code, result["kind"]), (1, "storage_integrity"))

    def test_unavailable_storage_never_returns_empty_health(self):
        with patch.object(dream_palace, "procedural_collection", side_effect=sqlite3.DatabaseError("unavailable")):
            code, result, _ = self.run_command("status")
        self.assertEqual((code, result["kind"]), (1, "storage_integrity"))
        self.assertNotIn("readiness", result)


class DraftTests(DraftFixture):
    def test_invalid_existing_session_stores_never_fall_back_to_complete_captures(self):
        from dream_procedural_validate import EvidenceReader
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        text = "The regression was isolated by following the rule."
        assistant = dict(self.refs[0], field="assistant_response", quote=text, source_hash=content_hash(text))
        self.publish(parse_event(event_data(origin_drawer_ids=[], evidence=[*self.refs[:3], assistant])))
        self.assertEqual(self.draft("captured-baseline.json", store=False)[1]["status"], "requires_review")
        plain = Path(self.tmp.name, "not-sqlite.db")
        plain.write_bytes(b"not a SQLite database")
        corrupt = Path(self.tmp.name, "truncated-sqlite.db")
        corrupt.write_bytes(Path(self.store).read_bytes()[:100])
        missing_schema = Path(self.tmp.name, "missing-schema.db")
        with sqlite3.connect(missing_schema) as con:
            con.execute("CREATE TABLE unrelated(value TEXT)")
        missing_turns = Path(self.tmp.name, "missing-turns.db")
        with sqlite3.connect(missing_turns) as con:
            con.execute("CREATE TABLE sessions(id TEXT, repository TEXT)")
        missing_column = Path(self.tmp.name, "missing-column.db")
        with sqlite3.connect(missing_column) as con:
            con.executescript("""
                CREATE TABLE sessions(id TEXT, repository TEXT);
                CREATE TABLE turns(session_id TEXT, turn_index INTEGER,
                                   timestamp TEXT, user_message TEXT);
            """)
        not_file = Path(self.tmp.name, "store-directory")
        not_file.mkdir()
        before = deepcopy(self.collection.rows)
        fallback = EvidenceReader.captured_turn_fields
        for store in (plain, corrupt, missing_schema, missing_turns, missing_column, not_file):
            with self.subTest(store=store.name):
                calls = []
                def observed_fallback(reader, session_id):
                    calls.append(session_id)
                    return fallback(reader, session_id)
                output = Path(self.tmp.name, store.name + "-draft.json")
                with patch.object(EvidenceReader, "captured_turn_fields", new=observed_fallback):
                    code, result, error = self.run_command("draft", "--session-store", str(store),
                        "--out", str(output), receipt=self.receipt())
                self.assertEqual(code, 1, error)
                self.assertEqual((result["status"], result["kind"], result["code"]),
                                 ("error", "storage_integrity", "session_store_invalid"))
                self.assertFalse(output.exists())
                self.assertEqual(calls, [])
                self.assertEqual(self.collection.rows, before)

    def test_public_event_envelopes_raw_and_fenced_are_lineage_not_originals(self):
        self.publish(self.proposal(), self.review())
        events = [self.proposal(), self.review(), parse_event(event_data(
            "outcome", 3, source_session_id=self.refs[0]["session_id"], evidence=[self.refs[0]]))]
        before = deepcopy(self.collection.rows)
        for event in events:
            envelope = canonical_json(event_to_data(event))
            for wrapped in (False, True):
                with self.subTest(event_kind=event.event_kind, fenced=wrapped):
                    text = f"Saved generated event:\n```json\n{envelope}\n```" if wrapped else envelope
                    with sqlite3.connect(self.store) as con:
                        con.execute("UPDATE turns SET assistant_response=? WHERE session_id=?",
                                    (text, self.refs[0]["session_id"]))
                    code, packet, error = self.draft(f"event-{event.event_kind}-{wrapped}.json")
                    self.assertEqual(code, 0, error)
                    self.assertEqual(packet["status"], "pending_original_evidence")
                    self.assertEqual([ref["field"] for ref in packet["original_references"]], ["user_message"])
                    self.assertIn("original_response_is_generated_echo", packet["missing_requirements"])
                    echo = packet["lineage_references"][-1]
                    self.assertEqual((echo["field"], echo["source_hash"], echo["reason"]),
                                     ("assistant_response", content_hash(text), "generated_transport_echo"))
                    self.assertEqual(self.collection.rows, before)

    def test_captured_task_fields_survive_host_loss_without_counting_source_copies(self):
        from dream_procedural_sources import AUTHOR
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        text = "The regression was isolated by following the rule."
        assistant = dict(self.refs[0], field="assistant_response", quote=text, source_hash=content_hash(text))
        self.publish(parse_event(event_data(origin_drawer_ids=[], evidence=[*self.refs[:3], assistant])))
        record = next(v for v in self.collection.rows.values()
                      if v["metadata"]["room"] == "procedural-sources")
        self.collection.add("w", "procedural-sources", record["text"], AUTHOR, record["metadata"])
        Path(self.store).unlink()
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")):
            code, packet, error = self.draft(store=False)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "requires_review")
        self.assertEqual(packet["independent_sessions"], 1)
        self.assertEqual(len(packet["original_references"]), 2)
        self.assertEqual({r["source_kind"] for r in packet["original_references"]}, {"session_turn"})
        self.assertEqual(self.collection.rows, before)

    def test_captured_older_task_is_not_the_current_original(self):
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        assistant = "The regression was isolated by following the rule."
        assistant_ref = dict(self.refs[0], field="assistant_response", quote=assistant,
                             source_hash=content_hash(assistant))
        later_ref = dict(self.refs[0], turn_index=1, quote="Later task", source_hash=content_hash("Later task"))
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (self.refs[0]["session_id"], 1, "Later task", "success", stamp()))
        self.publish(parse_event(event_data(origin_drawer_ids=[],
                                           evidence=[*self.refs[:3], assistant_ref, later_ref])))
        Path(self.store).unlink()
        code, packet, error = self.draft(store=False)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")
        self.assertEqual(packet["original_references"], [])
        self.assertIn("original_task_turn_not_current", packet["missing_requirements"])

    def test_grounded_task_success_is_review_only_and_never_published(self):
        self.publish(self.proposal(), self.review())
        before = deepcopy(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")):
            code, packet, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual((packet["kind"], packet["status"]), ("procedural_draft", "requires_review"))
        self.assertEqual(len(packet["original_references"]), 2)
        self.assertEqual({ref["session_id"] for ref in packet["original_references"]},
                         {self.refs[0]["session_id"]})
        self.assertIn("rule_specific_causal_attribution", packet["missing_requirements"])
        self.assertEqual(json.loads(Path(self.tmp.name, "draft.json").read_text()), packet)
        for key in ("event_kind", "digest", "outcome", "polarity", "statement", "disposition"):
            self.assertNotIn(key, packet)
        with self.assertRaises(ValueError):
            parse_event(packet)
        self.assertEqual(self.collection.rows, before)
        self.assertNotEqual(self.draft()[0], 0)  # exclusive creation
        self.assertEqual(self.collection.rows, before)

    def test_missing_current_turn_is_pending_never_an_older_success(self):
        task = "The current task has not reached SQLite yet."
        receipt = self.receipt(task=task, task_digest=content_hash(task))
        code, packet, error = self.draft(receipt=receipt)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")
        self.assertEqual(packet["original_references"], [])
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (receipt["session_id"], 1, task, "All tests passed.", stamp()))
        code, packet, error = self.draft("delayed.json", receipt=receipt)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "requires_review")
        self.assertEqual({r["turn_index"] for r in packet["original_references"]}, {1})
        code, packet, error = self.draft("no-store.json", receipt=receipt, store=False)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")

    def test_receipt_scope_digest_ids_and_session_are_checked(self):
        changes = [
            {"repository": "owner/other"}, {"task_digest": "0" * 64},
            {"generation": True}, {"delivered_rule_ids": [7]},
            {"delivered_rule_ids": ["../bad"]}, {"session_id": ""},
            {"schema_version": True}, {"guidance_as_of": "yesterday"},
        ]
        for number, change in enumerate(changes):
            with self.subTest(change=change):
                code, result, _ = self.draft(f"bad-{number}.json", receipt=self.receipt(**change))
                self.assertEqual(code, 2)
                self.assertEqual(result["kind"], "invalid_request")
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE sessions SET repository='other/repo'")
        self.assertNotEqual(self.draft("wrong-store.json")[0], 0)

    def test_receipt_bounds_missing_response_and_corrupt_capture(self):
        input_path = Path(self.tmp.name, "huge-receipt.json")
        input_path.write_text(" " * 24577)
        code, result, _ = self.run_command("draft", "--input", str(input_path),
                                          "--out", str(Path(self.tmp.name, "no.json")))
        self.assertEqual((code, result["kind"]), (2, "invalid_request"))
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET assistant_response=NULL WHERE session_id=?",
                        (self.refs[0]["session_id"],))
        code, result, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "pending_original_evidence")
        self.assertIn("original_response_missing", result["missing_requirements"])
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        self.publish(self.proposal())
        record = next(v for v in self.collection.rows.values()
                      if v["metadata"]["room"] == "procedural-sources")
        record["text"] = record["text"].replace('"observed_at":', '"corrupted_at":')
        code, result, _ = self.draft("corrupt.json", store=False)
        self.assertEqual((code, result["kind"]), (1, "evidence_integrity"))
        self.assertFalse(Path(self.tmp.name, "corrupt.json").exists())

    def test_draft_source_scan_bound_returns_explicit_pending_omission(self):
        from dream_procedural_drafts import build_draft
        self.publish(self.proposal())
        reader = self.published_reader()
        lookup = reader.captured_turn_fields
        with patch.object(reader, "captured_turn_fields",
                          side_effect=lambda session: lookup(session, max_records=1)):
            result = build_draft(self.receipt(), repository="owner/repo", session_store=None,
                                 evidence_reader=reader, as_of=NOW)
        self.assertEqual(result["status"], "pending_original_evidence")
        self.assertIn("captured_lookup_limit", result["missing_requirements"])
        self.assertGreater(result["omitted_count"], 0)

    def test_raw_turn_duplicate_index_with_different_task_is_ambiguous(self):
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (self.refs[0]["session_id"], 0, "another task", "success", stamp()))
        code, result, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "pending_original_evidence")
        self.assertIn("ambiguous_original_task_turn", result["missing_requirements"])

    def test_explicit_store_draft_still_refuses_unavailable_palace(self):
        with patch.object(dream_palace, "procedural_collection", side_effect=RuntimeError("unavailable")):
            code, result, _ = self.draft()
        self.assertEqual((code, result["kind"]), (1, "storage_integrity"))
        self.assertFalse(Path(self.tmp.name, "draft.json").exists())

    def test_draft_newfile_cannot_be_a_dangling_symlink(self):
        target = Path(self.tmp.name, "unexpected.json")
        Path(self.tmp.name, "draft.json").symlink_to(target)
        code, _, _ = self.draft()
        self.assertNotEqual(code, 0)
        self.assertFalse(target.exists())

    def test_old_matching_task_cannot_replace_the_current_unpersisted_task(self):
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (self.refs[0]["session_id"], 1, "Later task", "successful", stamp()))
        code, result, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "pending_original_evidence")
        self.assertEqual(result["original_references"], [])
        self.assertIn("original_task_turn_not_current", result["missing_requirements"])

    def test_generated_lesson_echo_remains_lineage_not_original_response(self):
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET assistant_response=? WHERE session_id=?",
                        ('Saved lesson: {"kind":"lesson","body":"Repeat retrieved advice."}',
                         self.refs[0]["session_id"]))
        code, packet, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")
        self.assertEqual([r["field"] for r in packet["original_references"]], ["user_message"])

    def test_echo_and_duplicate_task_turns_never_manufacture_originals(self):
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET assistant_response=? WHERE session_id=?",
                        ('[procedural-context] copied generated guidance', self.refs[0]["session_id"]))
        code, packet, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")
        self.assertEqual([r["field"] for r in packet["original_references"]], ["user_message"])
        self.assertTrue(packet["lineage_references"])
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO turns SELECT session_id,1,user_message,assistant_response,timestamp "
                        "FROM turns WHERE session_id=?", (self.refs[0]["session_id"],))
        code, packet, error = self.draft("ambiguous.json")
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["original_references"], [])
        self.assertIn("ambiguous_original_task_turn", packet["missing_requirements"])

    def test_draft_cannot_write_in_palace_or_read_oversized_original(self):
        code, _, _ = self.run_command("draft", "--session-store", self.store, "--out",
            str(Path(self.path, "forbidden.json")), receipt=self.receipt())
        self.assertEqual(code, 2)
        self.assertFalse(Path(self.path, "forbidden.json").exists())
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET assistant_response=? WHERE session_id=?",
                        ("漢" * 400000, self.refs[0]["session_id"]))
        code, packet, error = self.draft()
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["status"], "pending_original_evidence")
        self.assertIn("original_field_byte_limit", packet["missing_requirements"])
        self.assertLessEqual(len(canonical_json(packet).encode("utf-8")) + 1, 65536)


class InstalledDraftTests(DraftFixture):
    def test_real_cli_and_unmodified_hook_consumer_preserve_palace_and_scores(self):
        from dream_procedural_palace import append_event, read_events
        from test_dream_procedural_palace import installed_palace
        self.storage_patch.stop()
        Path(self.store).chmod(0o600)
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        repository = Path(__file__).resolve().parents[2]
        procedure = repository / "skills" / "dreaming" / "scripts" / "dream_procedure.py"
        hook = repository / "hooks" / "procedural_context.py"
        repo = Path(self.tmp.name, "worktree")
        repo.mkdir()
        (repo / ".git").mkdir()
        state = Path(self.tmp.name, "state")
        state.mkdir(mode=0o700)
        config = Path(self.tmp.name, "config.json")
        config.write_text(canonical_json({
            "schema_version": 1, "mode": "guidance_drafts", "python": sys.executable,
            "procedure_script": str(procedure), "state_root": str(state),
            "repositories": [{"root": str(repo), "repository": "owner/repo", "wing": "w",
                              "palace": self.path, "session_store": self.store}],
        }), encoding="utf-8")
        config.chmod(0o600)
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            events = [self.proposal(), self.review(), *[
                parse_event(event_data("outcome", 10 + i, source_session_id=ref["session_id"],
                                       evidence=[ref])) for i, ref in enumerate(self.refs)]]
            with writer.mutation():
                writer.add_drawer("w", "diary", "Disposable integration fixture, not evidence.")
                for event in events:
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
            col = server._get_collection()
            before_sql = tuple(col._handle.conn.iterdump())
            before_files = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                            if p.is_file() and not p.name.endswith("-shm")}
            task = self.refs[0]["quote"]
            def adapter(mode):
                payload = {"sessionId": self.refs[0]["session_id"], "cwd": str(repo),
                           "timestamp": 1790443200000}
                if mode == "prompt":
                    payload["prompt"] = task
                if mode in {"before-tool", "after-tool"}:
                    payload.update(toolName="mempalace-mempalace_search", toolArgs={"query": task})
                if mode == "after-tool":
                    payload["toolResult"] = {"resultType": "success",
                        "textResultForLlm": canonical_json({"query": task, "results": []})}
                result = subprocess.run([sys.executable, str(hook), "--event", mode,
                    "--config", str(config)], cwd=repo, input=canonical_json(payload),
                    text=True, capture_output=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("unavailable", result.stderr, result.stderr)
                return result
            adapter("prompt")
            adapter("before-tool")
            delivered = adapter("after-tool")
            pointer = json.loads(delivered.stdout)["additionalContext"].split("packet: ", 1)[1]
            raw = Path(pointer).read_bytes()
            packet = json.loads(raw)
            self.assertEqual(packet["status"], "ok")
            self.assertEqual(packet["rules"][0]["rule_id"], events[0].rule_id)
            self.assertLessEqual(len(raw), 8192)
            self.assertLessEqual(len(raw.decode("utf-8")), 6000)
            adapter("stop")
            drafts = list(state.glob("**/draft-*.json"))
            self.assertEqual(len(drafts), 1)
            draft = json.loads(drafts[0].read_text())
            self.assertEqual(draft["status"], "requires_review")
            self.assertEqual(draft["delivered_rule_ids"], [events[0].rule_id])
            self.assertEqual(draft["independent_sessions"], 1)
            # Actual publishing commands must reject the non-event draft.
            rejected = subprocess.run([sys.executable, str(procedure), "outcome",
                "--palace", self.path, "--wing", "w", "--input", str(drafts[0]), "--dry-run"],
                text=True, capture_output=True, timeout=90)
            self.assertEqual(rejected.returncode, 2, rejected.stdout + rejected.stderr)
            Path(self.store).unlink()
            health = subprocess.run([sys.executable, str(procedure), "status",
                "--palace", self.path, "--wing", "w", "--repository", "owner/repo"],
                text=True, capture_output=True, timeout=90)
            self.assertEqual(health.returncode, 0, health.stdout + health.stderr)
            self.assertEqual(json.loads(health.stdout)["readiness"], "ready")
            self.assertEqual(set(read_events(self.path, "w")), set(events))
            self.assertEqual(tuple(col._handle.conn.iterdump()), before_sql)
            self.assertEqual({p.name: p.read_bytes() for p in Path(self.path).iterdir()
                              if p.is_file() and not p.name.endswith("-shm")}, before_files)
