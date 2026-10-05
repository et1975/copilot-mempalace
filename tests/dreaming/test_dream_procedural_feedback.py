"""Explicit feedback selection preserves originals without publishing outcomes."""
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
from dream_procedural import event_to_data, parse_event, to_data
from test_dream_procedural import NOW, event_data, stamp
from test_dream_procedural_drafts import DraftFixture


class FeedbackFixture(DraftFixture):
    def selection(self, evidence=None, **changes):
        return dict(schema_version=1, repository="owner/repo", rule_id=self.proposal().rule_id,
                    evidence=deepcopy(evidence if evidence is not None else self.refs[:1]), **changes)

    def prepare(self, selection=None, *, events=None, reader=None, at=NOW, **kwargs):
        from dream_procedural_feedback import prepare_feedback
        return prepare_feedback(selection if selection is not None else self.selection(),
            projection=self.projection(*(events if events is not None else [self.proposal()])),
            reader=reader or self.reader(), as_of=at, **kwargs)

    def artifact(self, name, value=None):
        path = Path(self.tmp.name, name)
        if value is not None:
            path.write_text(canonical_json(value), encoding="utf-8")
        return path

    def feedback_cli(self, name="feedback.json", *, selection=None, store=True, extra=()):
        from dream_procedure import main
        source = self.artifact("selection.json", selection if selection is not None else self.selection())
        target = self.artifact(name)
        args = ["feedback-prepare", "--palace", self.path, "--wing", "w",
                "--repository", "owner/repo", "--input", str(source), "--out", str(target)]
        if store:
            args += ["--session-store", self.store]
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error), \
             patch("dream_procedure.now_utc", return_value=NOW):
            code = main([*args, *extra])
        return code, json.loads(output.getvalue()), error.getvalue()

    def turn_ref(self, *, text=None, field="user_message", index=0, session=None):
        session = session or self.refs[0]["session_id"]
        text = text if text is not None else self.refs[0]["quote"]
        with sqlite3.connect(self.store) as con:
            if index:
                con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                            (session, index, text, text, stamp()))
            else:
                con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, session))
        return dict(source_kind="session_turn", source_id=session, session_id=session,
                    source_hash=content_hash(text), quote=text, turn_index=index, field=field)


class FeedbackTests(FeedbackFixture):
    def test_packet_has_exact_review_only_contract_full_definition_and_history(self):
        proposal, review = self.proposal(), self.review()
        packet = self.prepare(events=[review, proposal])
        self.assertEqual(set(packet), {"schema_version", "kind", "status", "repository", "rule_id",
            "definition", "history_digest", "source_session_id", "observed_at", "evidence",
            "delivery_receipt", "draft_origin", "packet_id"})
        self.assertEqual((packet["schema_version"], packet["kind"], packet["status"]),
                         (1, "procedural_feedback", "requires_adjudication"))
        self.assertEqual(packet["definition"], to_data(proposal.payload.definition))
        self.assertEqual(packet["history_digest"], content_hash(canonical_json(sorted(
            [[proposal.event_id, proposal.digest], [review.event_id, review.digest]]))))
        self.assertEqual(packet["source_session_id"], self.refs[0]["session_id"])
        self.assertEqual(packet["observed_at"], stamp())
        self.assertEqual(packet["evidence"], self.refs[:1])
        self.assertIsNone(packet["delivery_receipt"])
        self.assertIsNone(packet["draft_origin"])
        self.assertEqual(packet["packet_id"], content_hash(canonical_json(
            {key: value for key, value in packet.items() if key != "packet_id"})))
        self.assertEqual(packet, self.prepare(events=[proposal, review]))
        with self.assertRaises(ValueError):
            parse_event(packet)

    def test_submitted_limits_exact_duplicates_and_distinct_quotes_preserved(self):
        from dream_procedural_feedback import feedback_references
        ref = self.refs[0]
        eight = [{**ref, "quote": ref["quote"][i:]} for i in range(8)]
        self.assertEqual(len(feedback_references(eight)), 8)
        packet = self.prepare(self.selection(list(reversed(eight))))
        self.assertEqual(packet["evidence"], sorted(eight, key=canonical_json))
        self.assertEqual(packet["source_session_id"], ref["session_id"])
        for refs, message in (([], "1..8"), ([ref] * 9, "1..8"), ([ref, ref], "duplicate")):
            with self.subTest(refs=len(refs)), self.assertRaisesRegex(ValueError, message), \
                 patch("dream_procedural_validate.AdmissionReader.resolve",
                       side_effect=AssertionError("limits must precede source reads")):
                self.prepare(self.selection(refs))
        for size in (800, 801):
            text = "e\u0301" * 400 + "x" * (size - 800)
            ref = self.turn_ref(text=text)
            if size == 800:
                self.assertEqual(self.prepare(self.selection([ref]))["evidence"][0]["quote"], text)
            else:
                with self.assertRaisesRegex(ValueError, "800"):
                    self.prepare(self.selection([ref]))

    def test_two_fields_and_later_original_are_one_observation_without_success_inference(self):
        user = self.turn_ref(text="  Unnormalized e\u0301 observed regression.  ", index=1)
        assistant = {**user, "field": "assistant_response"}
        result = self.prepare(self.selection([assistant, user]))
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual(result["source_session_id"], user["session_id"])
        self.assertNotIn("outcome", result)
        self.assertNotIn("event_id", result)

    def test_selection_exact_input_byte_limit_is_inclusive_not_a_canonical_newline(self):
        selection = self.selection()
        old_id = selection["evidence"][0]["source_id"]
        padding = 24576 - len(canonical_json(selection).encode("utf-8"))
        source_id = old_id + "x" * padding
        selection["evidence"][0]["source_id"] = source_id
        self.collection.rows[source_id] = {**self.collection.rows[old_id], "id": source_id}
        self.assertEqual(len(canonical_json(selection).encode("utf-8")), 24576)
        self.assertEqual(self.prepare(selection)["evidence"][0]["source_id"], source_id)
        selection["evidence"][0]["source_id"] += "x"
        with self.assertRaisesRegex(ValueError, "byte limit"):
            self.prepare(selection)

    def test_full_generated_field_is_not_rescued_by_original_looking_quote(self):
        for kind in ("procedural_feedback", "procedural_feedback_abstention"):
            packet = canonical_json({"kind": kind, "quote": "observed regression"})
            for text in (packet, "```json\n" + packet + "\n```",
                         "Copied:\n" + canonical_json(packet) + "\nEnd."):
                ref = self.turn_ref(text=text)
                ref["quote"] = "observed regression"
                with self.assertRaisesRegex(ValueError, "generated"):
                    self.prepare(self.selection([ref]))
        real = self.turn_ref(text="A later independent correction exposed the false assumption.", index=1)
        self.assertEqual(self.prepare(self.selection([real]))["evidence"], [real])

    def test_retained_old_generated_capture_cannot_poison_a_distinct_genuine_original(self):
        from dream_procedural import EvidenceReference
        from dream_procedural_sources import _HEADER, _metadata, source_key
        from test_dream_procedural_palace import sanctioned_writer
        self.publish(self.proposal())
        text = canonical_json({"kind": "procedural_feedback", "quote": "copied feedback"})
        raw = self.turn_ref(text=text)
        ref = EvidenceReference(**raw)
        key = source_key(ref)
        data = dict(schema_version=1, identity={k: v for k, v in raw.items() if k != "quote"},
                    repository="owner/repo", observed_at=stamp(), captured_at=stamp(),
                    captured_by="legacy", captured_text=text)
        body = canonical_json(data)
        digest = content_hash(body)
        sanctioned_writer(self.path, self.collection).add_drawer(
            "w", "procedural-sources", _HEADER.format(key=key, digest=digest) + body,
            added_by="dream-procedure-source", metadata=_metadata(key, digest))
        before = deepcopy(self.collection.rows)
        with self.assertRaisesRegex(ValueError, "generated"):
            self.prepare(self.selection([raw]), reader=self.published_reader())
        self.assertEqual(self.prepare(reader=self.published_reader())["evidence"], self.refs[:1])
        later = self.turn_ref(text="A later independent observation exposes a real exception.", index=1)
        self.assertEqual(self.prepare(self.selection([later]))["evidence"], [later])
        self.assertEqual(self.collection.rows, before)

    def test_strict_selection_and_evidence_shape_reject_before_source_read(self):
        variants = [
            {**self.selection(), "schema_version": True},
            {**self.selection(), "schema_version": 2},
            {**self.selection(), "repository": "Owner/Repo"},
            {**self.selection(), "repository": " owner/repo"},
            {**self.selection(), "rule_id": "proc:no"},
            {**self.selection(), "outcome": "helpful"},
            {key: value for key, value in self.selection().items() if key != "repository"},
        ]
        for change in ({"source_hash": "ABC"}, {"quote": " "}, {"source_id": ""},
                       {"session_id": None}, {"source_kind": "tool_result"},
                       {"unknown": True}, {"turn_index": 0}):
            variants.append(self.selection([{**self.refs[0], **change}]))
        for selection in variants:
            with self.subTest(selection=selection), self.assertRaises(ValueError), \
                 patch("dream_procedural_validate.AdmissionReader.resolve",
                       side_effect=AssertionError("invalid input read a source")):
                self.prepare(selection)

    def test_exact_hash_quote_session_repository_and_observation_time(self):
        for change in ({"source_hash": "0" * 64}, {"quote": "not in original"},
                       {"session_id": self.refs[1]["session_id"]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.prepare(self.selection([{**self.refs[0], **change}]))
        for repository in ("other/repo", None, ""):
            with sqlite3.connect(self.store) as con:
                con.execute("UPDATE sessions SET repository=? WHERE id=?",
                            (repository, self.refs[0]["session_id"]))
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                self.prepare()
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE sessions SET repository='owner/repo'")
        with self.assertRaisesRegex(ValueError, "split_observations"):
            self.prepare(self.selection(self.refs[:2]))
        ref = self.turn_ref(text="Independent later correction.")
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET timestamp=? WHERE session_id=?",
                        (stamp(NOW + timedelta(seconds=1)), ref["session_id"]))
        with self.assertRaisesRegex(ValueError, "future"):
            self.prepare(self.selection([ref]))
        with self.assertRaisesRegex(ValueError, "split_observations"):
            self.prepare(self.selection([self.refs[0], ref]), at=NOW + timedelta(days=1))
        del self.collection.rows["source-1"]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.prepare()

    def test_stampless_drawer_does_not_inherit_filing_time_or_receipt_context(self):
        text = f"SESSION_ID: {self.refs[0]['session_id']}\nObserved failure."
        self.collection.rows["source-1"].update(text=text)
        self.collection.rows["source-1"]["metadata"]["filed_at"] = stamp()
        ref = {**self.refs[0], "quote": "Observed failure.", "source_hash": content_hash(text)}
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.prepare(self.selection([ref]), receipt=self.receipt())

    def test_held_stale_retired_definitions_remain_reportable(self):
        for verdict in ("hold", "retire"):
            packet = self.prepare(events=[self.proposal(), self.review(verdict=verdict)],
                                  at=NOW + timedelta(days=100))
            self.assertEqual(packet["status"], "requires_adjudication")
            self.assertEqual(packet["definition"]["exceptions"],
                             to_data(self.proposal().payload.definition)["exceptions"])

    def test_optional_legacy_receipt_is_full_unverified_lineage_only(self):
        receipt = self.receipt()
        self.assertEqual(self.prepare(receipt=receipt)["delivery_receipt"], receipt)
        for changes in ({"repository": "foreign/repo"}, {"session_id": "other"},
                        {"delivered_rule_ids": []}, {"schema_version": True},
                        {"task_digest": "0" * 64}, {"kind": "procedural_receipt"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.prepare(receipt={**receipt, **changes})
        from receipt_fixtures import receipts
        with self.assertRaises(ValueError):
            self.prepare(receipt=receipts()[0])

    def test_draft_mapping_is_explicit_original_only_and_retains_full_origin_digest(self):
        from dream_procedural_drafts import build_draft
        from dream_procedural_feedback import selection_from_draft
        draft = build_draft(self.receipt(), repository="owner/repo", session_store=self.store, as_of=NOW)
        selection = selection_from_draft(draft, self.proposal().rule_id, [1, 0])
        self.assertEqual(selection["evidence"], [draft["original_references"][1],
                                               draft["original_references"][0]])
        packet = self.prepare(selection, draft_origin=draft)
        self.assertEqual(packet["draft_origin"], {
            "digest": content_hash(canonical_json(draft)), "status": "requires_review",
            "missing_requirements": draft["missing_requirements"],
            "lineage_references": draft["lineage_references"]})
        for indexes in ([], [0, 0], [True], [-1], [2], [0] * 9):
            with self.subTest(indexes=indexes), self.assertRaises(ValueError):
                selection_from_draft(draft, self.proposal().rule_id, indexes)
        with self.assertRaises(ValueError):
            selection_from_draft(draft, "proc:" + "0" * 64, [0])
        with self.assertRaises(ValueError):
            self.prepare(draft_origin=draft)  # Original drawer is not the draft's original turn.
        for changes in ({"status": "pending_original_evidence"}, {"omitted_count": 1},
                        {"schema_version": True}, {"unexpected": 1},
                        {"missing_requirements": draft["missing_requirements"][:-1]},
                        {"independent_sessions": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                selection_from_draft({**draft, **changes}, self.proposal().rule_id, [0])
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET user_message='drift'")
        with self.assertRaisesRegex(ValueError, "drift"):
            self.prepare(selection, draft_origin=draft)

    def test_raw_json_byte_limits_duplicates_and_nonfinite_are_checked_before_parsing(self):
        from dream_procedural_feedback import read_json
        path = self.artifact("bounded.json")
        base = canonical_json({"text": "é"}).encode("utf-8")
        for extra, allowed in ((24576 - len(base), True), (24577 - len(base), False)):
            path.write_bytes(base + b" " * extra)
            if allowed:
                self.assertEqual(read_json(str(path), 24576), {"text": "é"})
            else:
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    read_json(str(path), 24576)
        path.write_bytes(b"{" + b"x" * 24576)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            read_json(str(path), 24576)
        for body in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '{"a":Infinity}'):
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_json(str(path), 24576)
        path.write_bytes(b'{"a":"\xff"}')
        with self.assertRaises(ValueError):
            read_json(str(path), 24576)

    def test_packet_limit_counts_complete_encoded_output_without_truncation(self):
        base_definition = event_to_data(self.proposal())["payload"]["definition"]
        text = "漢" * 800 + "abcdefgh"
        ref = self.turn_ref(text=text)
        evidence = [{**ref, "quote": text[i:i + 800]} for i in range(8)]
        task = "r" * 16384

        def prepare_padding(padding):
            definition = {**base_definition, "exceptions": ["é" * 10000 + "x" * padding]}
            proposal = self.proposal(rule=definition)
            selection = {**self.selection(evidence), "rule_id": proposal.rule_id}
            receipt = self.receipt(task=task, task_digest=content_hash(task),
                                   delivered_rule_ids=[proposal.rule_id])
            return self.prepare(selection, events=[proposal], receipt=receipt)

        packet = prepare_padding(0)
        padding = 65536 - len((canonical_json(packet) + "\n").encode("utf-8"))
        packet = prepare_padding(padding)
        self.assertEqual(len((canonical_json(packet) + "\n").encode("utf-8")), 65536)
        with self.assertRaisesRegex(ValueError, "packet byte limit"):
            prepare_padding(padding + 1)


class FeedbackCommandTests(FeedbackFixture):
    def test_r1_identity_cannot_join_foreign_turn_to_whitespace_aliased_target_session(self):
        from dream_procedural_palace import record_data
        from dream_procedural_feedback import feedback_references
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        for number, (source, session) in enumerate((
                ("session-a", " session-a "), (" session-b ", "session-b"))):
            text = "Exact foreign observation, not target evidence."
            foreign_time = stamp(NOW - timedelta(days=2))
            with sqlite3.connect(self.store) as con:
                con.executemany("INSERT INTO sessions VALUES (?,?)",
                                [(source, "foreign/repo"), (session, "owner/repo")])
                con.executemany("INSERT INTO turns VALUES (?,?,?,?,?)", [
                    (source, 0, text, "Foreign response", foreign_time),
                    (session, 0, "Different target text", "Target response", stamp())])
            ref = dict(source_kind="session_turn", source_id=source, session_id=session,
                       turn_index=0, field="user_message", quote=text, source_hash=content_hash(text))
            before = deepcopy(self.collection.rows)
            store_before = Path(self.store).read_bytes()
            code, result, error = self.feedback_cli(f"identity-{number}.json",
                                                    selection=self.selection([ref]))
            self.assertNotEqual(code, 0, (result, error))
            self.assertIn("source_id", result["error"])
            self.assertFalse(self.artifact(f"identity-{number}.json").exists())
            self.assertEqual(self.collection.rows, before)
            self.assertEqual(Path(self.store).read_bytes(), store_before)
            with self.assertRaisesRegex(ValueError, "source_id"):
                feedback_references([ref])
            with patch("dream_procedural_validate.acquire_original",
                       side_effect=AssertionError("identity validation must precede source access")):
                code, result, _ = self.feedback_cli(f"identity-preflight-{number}.json",
                                                    selection=self.selection([ref]))
            self.assertEqual(code, 2)
            self.assertIn("source_id", result["error"])
            self.assertFalse(self.artifact(f"identity-preflight-{number}.json").exists())

    def test_r1_misrouted_captures_fail_before_host_fallback_with_or_without_valid_copy(self):
        from dream_procedural_sources import OriginalSource, source_record_data
        from dream_procedural import EvidenceReference
        from dream_procedural_palace import record_data
        from test_dream_procedural_palace import sanctioned_writer
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        baseline = deepcopy(self.collection.rows)
        ref = self.turn_ref(text="A real selected original observation.")
        original = OriginalSource(EvidenceReference(**ref), "owner/repo", NOW, ref["quote"])
        for native in (True, False):
            for alongside_valid in (False, True):
                self.collection.rows = deepcopy(baseline)
                writer = sanctioned_writer(self.path, self.collection, native=native, chunk_size=79)
                body, metadata = source_record_data(original, captured_at=NOW, captured_by="original")
                if alongside_valid:
                    writer.add_drawer("w", "procedural-sources", body,
                                      added_by="dream-procedure-source", metadata=metadata)
                wrong_key = "0" * 64
                body = body.replace("Source key: " + metadata["source_key"], "Source key: " + wrong_key)
                writer.add_drawer("w", "procedural-sources", body, added_by="dream-procedure-source",
                                  metadata={**metadata, "source_key": wrong_key})
                before = deepcopy(self.collection.rows)
                name = f"routing-{native}-{alongside_valid}.json"
                with patch("dream_procedural_validate.acquire_original",
                           side_effect=AssertionError("host fallback must not run")):
                    code, result, error = self.feedback_cli(name, selection=self.selection([ref]))
                self.assertNotEqual(code, 0, error)
                self.assertIn("identity/key mismatch", result["error"])
                self.assertFalse(self.artifact(name).exists())
                self.assertEqual(self.collection.rows, before)

    def test_explicit_empty_store_cannot_activate_default_host_fallback(self):
        from dream_procedural_palace import record_data
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        code, result, _ = self.feedback_cli(store=False, extra=("--session-store", ""))
        self.assertNotEqual(code, 0, result)
        self.assertFalse(self.artifact("feedback.json").exists())

    def test_cli_explicit_store_no_store_host_loss_and_no_mutation(self):
        self.publish(self.proposal(), self.review())
        before = deepcopy(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")), \
             patch("dream_sessions.default_store_path", side_effect=AssertionError("hidden host")):
            for name, store in (("explicit.json", True), ("published.json", False)):
                code, result, error = self.feedback_cli(name, store=store)
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(self.artifact(name).read_text()), result)
            Path(self.store).unlink()
            self.assertEqual(self.feedback_cli("host-lost.json", store=False)[0], 0)
        self.assertEqual(self.collection.rows, before)
        self.assertNotEqual(self.feedback_cli("published.json", store=False)[0], 0)
        self.assertEqual(self.collection.rows, before)

    def test_no_store_never_acquires_uncaptured_originals_or_repairs_corruption(self):
        from dream_procedural_palace import record_data
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        before = deepcopy(self.collection.rows)
        code, result, _ = self.feedback_cli(store=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(result["code"], "uncaptured")
        self.assertFalse(self.artifact("feedback.json").exists())
        self.assertEqual(self.collection.rows, before)
        self.assertEqual(self.feedback_cli("explicit.json")[0], 0)
        self.assertEqual(self.collection.rows, before)

    def test_cli_scope_mismatch_artifact_symlinks_and_inside_palace_are_refused(self):
        self.publish(self.proposal())
        selection = {**self.selection(), "repository": "other/repo"}
        self.assertNotEqual(self.feedback_cli(selection=selection)[0], 0)
        target = self.artifact("link.json")
        target.symlink_to(self.artifact("absent.json"))
        self.assertNotEqual(self.feedback_cli("link.json")[0], 0)
        self.assertFalse(self.artifact("absent.json").exists())
        self.assertNotEqual(self.feedback_cli("palace/inside.json")[0], 0)
        self.assertFalse(Path(self.path, "inside.json").exists())
        directory = Path(self.tmp.name, "linked-directory")
        directory.symlink_to(Path(self.tmp.name), target_is_directory=True)
        self.assertNotEqual(self.feedback_cli("linked-directory/parent-symlink.json")[0], 0)
        self.assertFalse(self.artifact("parent-symlink.json").exists())

    def test_live_uncaptured_store_loss_and_present_corruption_fail_without_artifacts(self):
        from dream_procedural_palace import record_data
        body, metadata = record_data(self.proposal())
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        before = deepcopy(self.collection.rows)
        Path(self.store).unlink()
        self.assertNotEqual(self.feedback_cli("missing.json")[0], 0)
        Path(self.store).write_bytes(b"not sqlite")
        self.assertNotEqual(self.feedback_cli("corrupt.json")[0], 0)
        self.assertFalse(self.artifact("missing.json").exists())
        self.assertFalse(self.artifact("corrupt.json").exists())
        self.assertEqual(self.collection.rows, before)

    def test_witness_loss_drift_and_capture_corruption_do_not_fall_back(self):
        self.publish(self.proposal())
        original = deepcopy(self.collection.rows["source-1"])
        self.collection.rows["source-1"]["text"] += " drift"
        self.assertNotEqual(self.feedback_cli("drift.json")[0], 0)
        del self.collection.rows["source-1"]
        self.assertNotEqual(self.feedback_cli("lost.json")[0], 0)
        self.collection.rows["source-1"] = original
        capture = next(row for row in self.collection.rows.values()
                       if row["metadata"]["room"] == "procedural-sources")
        capture["text"] = capture["text"].replace('"observed_at":', '"corrupt_at":')
        before = deepcopy(self.collection.rows)
        self.assertNotEqual(self.feedback_cli("corrupt-capture.json")[0], 0)
        self.assertEqual(self.collection.rows, before)
        self.assertFalse(self.artifact("corrupt-capture.json").exists())

    def test_strict_actual_cli_selection_receipt_and_draft_bytes(self):
        self.publish(self.proposal())
        for option, name, limit in (("--receipt", "oversize-receipt.json", 24576),
                                    ("--draft-origin", "oversize-draft.json", 65536)):
            path = self.artifact(name)
            path.write_bytes(b"{" + b"x" * limit)
            code, result, _ = self.feedback_cli(name + ".out", extra=(option, str(path)))
            self.assertNotEqual(code, 0)
            self.assertIn("byte limit", result["error"])
            self.assertFalse(self.artifact(name + ".out").exists())
        from dream_procedure import main
        source = self.artifact("raw-input.json")
        target = self.artifact("raw-output.json")
        base = canonical_json(self.selection()).encode("utf-8")
        for suffix, valid in ((b" " * (24576 - len(base)), True),
                              (b" " * (24577 - len(base)), False)):
            source.write_bytes(base + suffix)
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()), \
                 patch("dream_procedure.now_utc", return_value=NOW):
                code = main(["feedback-prepare", "--palace", self.path, "--wing", "w",
                    "--repository", "owner/repo", "--input", str(source), "--out", str(target)])
            self.assertEqual(code == 0, valid, output.getvalue())
            if valid:
                target.unlink()
        self.assertFalse(target.exists())

    def test_cli_full_draft_receipt_input_and_pending_rejection(self):
        from dream_procedural_drafts import build_draft
        from dream_procedural_feedback import selection_from_draft
        self.publish(self.proposal(), self.review())
        draft = build_draft(self.receipt(), repository="owner/repo", session_store=self.store, as_of=NOW)
        selection = selection_from_draft(draft, self.proposal().rule_id, [0])
        flags = ("--draft-origin", str(self.artifact("origin.json", draft)),
                 "--receipt", str(self.artifact("legacy.json", self.receipt())))
        code, result, error = self.feedback_cli(selection=selection, extra=flags)
        self.assertEqual(code, 0, error)
        self.assertIsNotNone(result["draft_origin"])
        pending = {**draft, "status": "pending_original_evidence", "omitted_count": 1}
        self.artifact("origin.json", pending)
        self.assertNotEqual(self.feedback_cli("pending.json", selection=selection, extra=flags)[0], 0)
        self.assertFalse(self.artifact("pending.json").exists())


class InstalledFeedbackTests(FeedbackFixture):
    def test_real_handlers_live_wal_host_loss_closed_reads_and_native_audit_are_nonmutating(self):
        from dream_procedural_palace import append_event
        from test_dream_procedural_palace import installed_palace
        from mempalace.palace import get_backend_for_palace
        self.storage_patch.stop()
        raw_ref = self.turn_ref(text="The focused test exposed a missed exception before the change.")
        self.refs[0] = raw_ref
        with installed_palace(self.path) as server:
            from mempalace import wal
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for ref in self.refs[1:]:
                    ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"])["drawer_id"]
                for event in (self.proposal(), self.review()):
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
                Path(self.store).unlink()
                collection = server._get_collection()
                collection._handle.conn.execute("PRAGMA wal_autocheckpoint=0")
                before_state = tuple(collection._handle.conn.iterdump())
                before_config = deepcopy(server._config._file_config)

                def snapshot():
                    files = {str(path): path.read_bytes() for path in Path(self.path).rglob("*")
                             if path.is_file() and not path.name.endswith("-shm")}
                    home = Path(os.environ["HOME"])
                    files.update({str(path): path.read_bytes() for path in home.rglob("*")
                                  if path.is_file() and not path.is_symlink()})
                    return files

                def run_reads(suffix):
                    before = snapshot()
                    with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
                         patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")), \
                         patch("dream_sessions.default_store_path", side_effect=AssertionError("host")), \
                         patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")), \
                         patch("dream_procedural_palace.local_embedder", side_effect=AssertionError("embedder")):
                        for label, ref in (("raw", raw_ref), ("witness", self.refs[1])):
                            code, result, error = self.feedback_cli(f"{label}-{suffix}.json",
                                selection=self.selection([ref]), store=False)
                            self.assertEqual(code, 0, error)
                            self.assertEqual(result["evidence"], [ref])
                    self.assertEqual(snapshot(), before)
                    self.assertEqual(server._config._file_config, before_config)

                self.assertTrue(Path(wal._WAL_FILE).exists())
                run_reads("live")
                self.assertEqual(tuple(collection._handle.conn.iterdump()), before_state)
            get_backend_for_palace(self.path).close_palace(self.path)
            run_reads("closed")
            # A fresh process owns its production UTC clock and works with no host,
            # while operator read-only mode remains set; no test clock CLI exists.
            script = Path(__file__).resolve().parents[2] / "skills/dreaming/scripts/dream_procedure.py"
            source = self.artifact("subprocess-selection.json", self.selection([raw_ref]))
            target = self.artifact("subprocess-feedback.json")
            before = snapshot()
            completed = subprocess.run([sys.executable, str(script), "feedback-prepare",
                "--palace", self.path, "--wing", "w", "--repository", "owner/repo",
                "--input", str(source), "--out", str(target)], capture_output=True, text=True,
                timeout=90, env={**os.environ, "MEMPALACE_MCP_READ_ONLY": "1"})
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["evidence"], [raw_ref])
            self.assertEqual(snapshot(), before)

    def test_real_generated_copies_held_rules_and_read_errors_preserve_all_storage(self):
        from dream_procedural_palace import append_event
        from test_dream_procedural_palace import installed_palace
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for ref in self.refs:
                    ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"])["drawer_id"]
                for event in (self.proposal(), self.review(verdict="hold")):
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
                code, packet, error = self.feedback_cli("held.json", store=False)
                self.assertEqual(code, 0, error)
                copies = [packet, {"schema_version": 1, "kind": "procedural_feedback_abstention",
                                   "status": "abstained", "decision": {"reason": "Cannot attribute"}}]
                refs = []
                for copied in copies:
                    text = (f"SESSION_ID: {self.refs[0]['session_id']}\nOBSERVED_AT: {stamp()}\n"
                            "Grounded-looking quote\n```json\n" + canonical_json(canonical_json(copied))
                            + "\n```")
                    source_id = writer.add_drawer("w", "diary", text)["drawer_id"]
                    refs.append({**self.refs[0], "source_id": source_id,
                                 "source_hash": content_hash(text), "quote": "Grounded-looking quote"})
                collection = server._get_collection()
                before = tuple(collection._handle.conn.iterdump())
                for number, ref in enumerate(refs):
                    code, result, _ = self.feedback_cli(f"generated-{number}.json",
                                                        selection=self.selection([ref]))
                    self.assertNotEqual(code, 0)
                    self.assertIn("generated", result["error"])
                    self.assertFalse(self.artifact(f"generated-{number}.json").exists())
                self.assertEqual(tuple(collection._handle.conn.iterdump()), before)
            # Present invalid storage is a nonzero read error, never empty feedback.
            with patch.object(dream_palace, "procedural_collection",
                              side_effect=sqlite3.DatabaseError("unreadable storage")):
                code, result, _ = self.feedback_cli("storage-error.json", store=False)
            self.assertEqual((code, result["kind"]), (1, "storage_integrity"))
            self.assertFalse(self.artifact("storage-error.json").exists())
