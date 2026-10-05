"""Explicit original review produces artifacts, never automatic causal credit."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
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
from uuid import UUID

import dream_palace
from dream_metadata import canonical_json, content_hash
from dream_procedural import Policy, canonical_rule_id, event_to_data, parse_event, project_rules, to_data
from test_dream_procedural import NOW, stamp
from test_dream_procedural_feedback import FeedbackFixture


class AdjudicationFixture(FeedbackFixture):
    def prepare(self, selection=None, *, events=None, reader=None, at=NOW, **kwargs):
        from dream_procedural_feedback import prepare_feedback
        projection = project_rules(events if events is not None else [self.proposal()], as_of=at, policy=Policy())
        return prepare_feedback(selection if selection is not None else self.selection(),
                                projection=projection, reader=reader or self.reader(), as_of=at, **kwargs)

    def decision(self, packet, *, mode="publish", **changes):
        result = {
            "schema_version": 1, "packet_id": packet["packet_id"], "decision": mode,
            "actor_kind": "human", "session_id": "reviewer-session", "recorded_at": stamp(),
        }
        if mode == "publish":
            result.update(event_id=str(UUID(int=30)), observation_id="focused regression",
                outcome="helpful", rationale={
                    "applicability": "Target parser defect was reproducible; tooling is unchanged.",
                    "exceptions": "No stated exception applied to this target input.",
                    "behavior": "Wrote the focused failing regression before changing the parser.",
                    "effect": "The original target run isolated the wrong branch, not merely a pass.",
                    "alternatives": "Broad testing hid this input; foreign success alone is not support.",
                })
        else:
            result["reason"] = "The original reports success but no rule-specific causal effect."
        return {**result, **changes}

    def adjudicate(self, packet, decision=None, *, events=None, at=NOW, reader=None, **kwargs):
        from dream_procedural_adjudication import reviewed_outcome
        projection = project_rules(events if events is not None else [self.proposal()], as_of=at, policy=Policy())
        return reviewed_outcome(packet, decision if decision is not None else self.decision(packet),
                                projection=projection, reader=reader or self.reader(), as_of=at, **kwargs)

    def cli(self, command, *extra, write=False, at=NOW):
        from dream_procedure import main
        from test_dream_procedural_palace import sanctioned_writer
        args = [command, "--palace", self.path, "--wing", "w"]
        if command in {"draft", "feedback-prepare", "feedback-adjudicate"}:
            args += ["--repository", "owner/repo"]
        output, error = StringIO(), StringIO()
        writer = (nullcontext() if write is None else
                  patch.object(dream_palace, "MempalaceWriter",
                      return_value=sanctioned_writer(self.path, self.collection)) if write else
                  patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")))
        with writer, patch("dream_procedure.now_utc", return_value=at), \
             redirect_stdout(output), redirect_stderr(error):
            code = main([*args, *map(str, extra)])
        return code, json.loads(output.getvalue()), error.getvalue()

    def adjudicate_cli(self, packet, decision=None, *, name="reviewed.json", store=True, extra=(), at=NOW):
        packet_path = self.artifact("feedback.json", packet)
        decision_path = self.artifact("decision.json", decision if decision is not None else self.decision(packet))
        flags = ["--session-store", self.store] if store else []
        return self.cli("feedback-adjudicate", "--input", packet_path, "--decision", decision_path,
                        "--out", self.artifact(name), *flags, *extra, at=at)


class AdjudicationTests(AdjudicationFixture):
    def test_explicit_human_and_agent_polarities_create_ordinary_immutable_events(self):
        packet = self.prepare()
        before = deepcopy(self.collection.rows)
        for actor in ("human", "agent"):
            for outcome in ("helpful", "harmful", "neutral"):
                decision = self.decision(packet, actor_kind=actor, outcome=outcome)
                result = self.adjudicate(packet, decision)
                self.assertEqual(event_to_data(parse_event(result)), result)
                self.assertEqual(result["event_id"], decision["event_id"])
                self.assertEqual(result["actor_kind"], actor)
                self.assertEqual(result["session_id"], "reviewer-session")
                self.assertEqual(result["payload"]["outcome"], outcome)
                self.assertEqual(result["payload"]["observed_at"], packet["observed_at"])
                self.assertEqual(result["payload"]["source_session_id"], packet["source_session_id"])
                self.assertEqual(result["payload"]["evidence"], packet["evidence"])
                self.assertEqual(result["payload"]["attribution"], canonical_json({
                    "packet_id": packet["packet_id"],
                    "decision_digest": content_hash(canonical_json(decision)),
                    "reviewed_rationale": decision["rationale"],
                }))
                self.assertEqual(self.adjudicate(packet, decision), result)
        self.assertEqual(self.collection.rows, before)

    def test_abstention_is_not_an_event_or_neutral_credit(self):
        packet = self.prepare()
        decision = self.decision(packet, mode="abstain")
        before = deepcopy(self.collection.rows)
        result = self.adjudicate(packet, decision)
        self.assertEqual(result, {
            "schema_version": 1, "kind": "procedural_feedback_abstention",
            "status": "abstained", "decision": decision,
        })
        with self.assertRaises(ValueError):
            parse_event(result)
        self.assertEqual(self.collection.rows, before)
        self.assertNotIn("event_id", canonical_json(result))
        self.assertNotIn("neutral", canonical_json(result))

    def test_complete_packet_comparison_rejects_tampering_even_with_rehashed_packet(self):
        packet = self.prepare()
        variants = [
            {"kind": "other"}, {"status": "approved"}, {"schema_version": True},
            {"repository": "Owner/Repo"}, {"source_session_id": "another-session"},
            {"observed_at": stamp(NOW - timedelta(days=1))},
            {"history_digest": "0" * 64}, {"definition": {**packet["definition"], "exceptions": ["new"]}},
            {"extra": "not an accepted T1 field"}, {"packet_id": "0" * 64},
        ]
        for changes in variants:
            altered = {**deepcopy(packet), **changes}
            if "packet_id" not in changes:
                altered["packet_id"] = content_hash(canonical_json({
                    k: v for k, v in altered.items() if k != "packet_id"}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.adjudicate(altered)
        missing = {k: v for k, v in packet.items() if k != "draft_origin"}
        with self.assertRaises(ValueError):
            self.adjudicate(missing)

    def test_decision_exact_shape_types_canonical_actor_and_existing_session_contract(self):
        packet = self.prepare()
        for mode in ("publish", "abstain"):
            decision = self.decision(packet, mode=mode)
            variants = [
                {"schema_version": True}, {"schema_version": "1"}, {"packet_id": "0" * 64},
                {"decision": "auto"}, {"actor_kind": "Human"}, {"actor_kind": "system"},
                {"session_id": " "}, {"session_id": 3}, {"session_id": " reviewer-session"},
                {"session_id": "Cafe\u0301"}, {"recorded_at": "2026-09-23"},
                {"recorded_at": "2026-09-23T00:00:00+01:00"}, {"recorded_at": True},
                {"recorded_at": "2026-09-23T00:00:00+00:00"}, {"unknown": "extra"},
            ]
            if mode == "publish":
                variants += [
                    {"event_id": "not-a-uuid"}, {"event_id": UUID(int=30)},
                    {"observation_id": " "}, {"observation_id": " label"},
                    {"outcome": "task_success"}, {"outcome": None}, {"reason": "wrong branch"},
                    {"rationale": None}, {"rationale": {**decision["rationale"], "target_fit": "extra"}},
                ]
                variants += [{"rationale": {**decision["rationale"], field: value}}
                             for field in decision["rationale"] for value in (" ", None, 1)]
            else:
                variants += [{"reason": " "}, {"reason": None}, {"outcome": "neutral"}]
            for changes in variants:
                with self.subTest(mode=mode, changes=changes), self.assertRaises((ValueError, TypeError)):
                    self.adjudicate(packet, {**decision, **changes})
            for field in decision:
                with self.subTest(mode=mode, missing=field), self.assertRaises(ValueError):
                    self.adjudicate(packet, {k: v for k, v in decision.items() if k != field})
            # Historical core session labels are canonical nonblank text, not G1 UUIDs.
            result = self.adjudicate(packet, {**decision, "session_id": "legacy reviewer"})
            self.assertEqual((result["decision"] if mode == "abstain" else result)["session_id"],
                             "legacy reviewer")

    def test_original_recording_and_command_clocks_are_ordered_in_both_decisions(self):
        packet = self.prepare()
        for mode in ("publish", "abstain"):
            for recorded in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)):
                with self.subTest(mode=mode, recorded=recorded), self.assertRaises(ValueError):
                    self.adjudicate(packet, self.decision(packet, mode=mode, recorded_at=stamp(recorded)))
            later = NOW + timedelta(days=10)
            result = self.adjudicate(packet, self.decision(packet, mode=mode, recorded_at=stamp(later)), at=later)
            if mode == "publish":
                self.assertEqual(result["payload"]["observed_at"], stamp())
                self.assertEqual(result["recorded_at"], stamp(later))

    def test_history_drift_requires_new_packet_and_review_including_abstention(self):
        packet = self.prepare()
        for mode in ("publish", "abstain"):
            with self.assertRaisesRegex(ValueError, "changed|drift|mismatch"):
                self.adjudicate(packet, self.decision(packet, mode=mode),
                                events=[self.proposal(), self.review()])
        fresh = self.prepare(events=[self.proposal(), self.review()])
        with self.assertRaisesRegex(ValueError, "packet"):
            self.adjudicate(fresh, self.decision(packet), events=[self.proposal(), self.review()])

    def test_held_stale_retired_rules_can_report_harm_without_becoming_eligible(self):
        for verdict in ("hold", "retire"):
            events = [self.proposal(), self.review(verdict=verdict)]
            packet = self.prepare(events=events)
            later = NOW + timedelta(days=100)
            result = self.adjudicate(packet,
                self.decision(packet, outcome="harmful", recorded_at=stamp(later)), events=events, at=later)
            projected = project_rules([*events, parse_event(result)], as_of=later, policy=Policy())
            self.assertFalse(projected.errors)
            self.assertFalse(projected.rules[0].eligible)
            self.assertAlmostEqual(projected.rules[0].score.harmful, 2 ** (-100 / Policy().half_life_days))
            self.assertEqual(projected.rules[0].score.terms[0].observed_at, NOW)
            self.assertEqual(result["payload"]["outcome"], "harmful")
            self.assertEqual(result["payload"]["observed_at"], stamp())

    def test_exact_draft_origin_and_all_four_obligations_are_required_again(self):
        from dream_procedural_drafts import build_draft
        from dream_procedural_feedback import selection_from_draft
        draft = build_draft(self.receipt(), repository="owner/repo", session_store=self.store, as_of=NOW)
        selection = selection_from_draft(draft, self.proposal().rule_id, [0])
        packet = self.prepare(selection, draft_origin=draft, receipt=self.receipt())
        event = self.adjudicate(packet, draft_origin=draft)
        self.assertEqual(event["payload"]["evidence"], packet["evidence"])
        for mode in ("publish", "abstain"):
            decision = self.decision(packet, mode=mode)
            variants = [
                None, {**draft, "status": "pending_original_evidence"},
                {**draft, "missing_requirements": draft["missing_requirements"][:-1]},
                {**draft, "original_references": list(reversed(draft["original_references"]))},
                {**draft, "lineage_references": [
                    {**draft["lineage_references"][0], "generation": 2}, *draft["lineage_references"][1:]]},
                {**draft, "original_references": [{**draft["original_references"][0], "quote": "wrong"}]},
            ]
            for altered in variants:
                with self.subTest(mode=mode, draft=altered), self.assertRaises(ValueError):
                    self.adjudicate(packet, decision, draft_origin=altered)
        plain = self.prepare(selection)
        with self.assertRaises(ValueError):
            self.adjudicate(plain, draft_origin=draft)
        original = deepcopy(packet)
        self.adjudicate(packet, draft_origin=draft)
        self.assertEqual(packet, original)

    def test_fresh_source_reads_reject_original_drift_and_cross_repository_substitution(self):
        ref = self.turn_ref(text="The later original observed a target-specific regression.", index=1)
        packet = self.prepare(self.selection([ref]))
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET user_message='Changed original' WHERE turn_index=1")
        for mode in ("publish", "abstain"):
            with self.assertRaises(ValueError):
                self.adjudicate(packet, self.decision(packet, mode=mode))
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET user_message=? WHERE turn_index=1", (ref["quote"],))
            con.execute("UPDATE sessions SET repository='foreign/fork' WHERE id=?", (ref["session_id"],))
        with self.assertRaisesRegex(ValueError, "repository"):
            self.adjudicate(packet)

    def test_foreign_counterexample_is_context_not_target_score_and_narrowing_is_new_identity(self):
        target = self.proposal().payload.definition
        narrowed = {**to_data(target), "applies_when": "Only the target parser with the original toolchain."}
        self.assertNotEqual(canonical_rule_id(narrowed), self.proposal().rule_id)
        packet = self.prepare()
        decision = self.decision(packet, outcome="harmful")
        decision["rationale"].update(
            applicability="Target toolchain drift triggered this rule's formerly absent assumption.",
            effect="The target original records the regression; foreign runs are not target outcomes.",
            alternatives="A foreign/fork failure is a relevant counterexample; assess the shared precondition.")
        event = self.adjudicate(packet, decision)
        state = self.projection(self.proposal(), parse_event(event)).rules[0]
        self.assertEqual((state.score.helpful, state.score.harmful), (0, 1))
        self.assertEqual(state.definition, target)
        self.assertIn(parse_event(event), state.events)
        self.assertEqual(event["payload"]["evidence"], self.refs[:1])
        self.assertEqual(event["payload"]["repository"], "owner/repo")

    def test_conflicting_event_id_is_not_repaired_and_observation_label_is_not_dedup_gate(self):
        packet = self.prepare()
        first = parse_event(self.adjudicate(packet))
        fresh = self.prepare(events=[self.proposal(), first])
        with self.assertRaises(ValueError):
            self.adjudicate(fresh, self.decision(fresh, outcome="harmful"),
                            events=[self.proposal(), first])
        next_event = parse_event(self.adjudicate(fresh,
            self.decision(fresh, event_id=str(UUID(int=31)), outcome="harmful"),
            events=[self.proposal(), first]))
        state = self.projection(self.proposal(), first, next_event).rules[0]
        self.assertEqual(next_event.payload.observation_id, first.payload.observation_id)
        self.assertEqual((state.score.helpful, state.score.harmful), (0, 1))
        self.assertEqual(len(state.events), 3)


class FeedbackFlowTests(AdjudicationFixture):
    def test_actual_draft_explicit_selection_abstention_outcome_publication_and_exact_retry(self):
        from dream_procedural_feedback import selection_from_draft
        self.publish(self.proposal(), self.review())
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET assistant_response=? WHERE session_id=?",
                        ("All tests passed.", self.refs[0]["session_id"]))
        before = deepcopy(self.collection.rows)
        draft_path = self.artifact("draft.json")
        code, draft, error = self.cli("draft", "--input", self.artifact("receipt.json", self.receipt()),
                                    "--out", draft_path, "--session-store", self.store)
        self.assertEqual(code, 0, error)
        self.assertEqual(draft["status"], "requires_review")
        self.assertNotIn("outcome", draft)
        selection = selection_from_draft(draft, self.proposal().rule_id, [0])
        packet_path = self.artifact("flow-feedback.json")
        code, packet, error = self.cli("feedback-prepare", "--input", self.artifact("selection.json", selection),
            "--out", packet_path, "--session-store", self.store, "--draft-origin", draft_path)
        self.assertEqual(code, 0, error)
        self.assertEqual(packet["draft_origin"]["missing_requirements"], draft["missing_requirements"])
        self.assertEqual(packet["draft_origin"]["lineage_references"], draft["lineage_references"])
        self.assertNotIn("outcome", packet)
        code, abstained, error = self.adjudicate_cli(packet, self.decision(packet, mode="abstain"),
            name="abstained.json", extra=("--draft-origin", draft_path))
        self.assertEqual(code, 0, error)
        self.assertEqual(abstained["status"], "abstained")
        self.assertEqual(self.collection.rows, before)
        code, _, _ = self.cli("outcome", "--input", self.artifact("abstained.json"), "--dry-run")
        self.assertNotEqual(code, 0)
        decision = self.decision(packet)
        code, reviewed, error = self.adjudicate_cli(packet, decision, extra=("--draft-origin", draft_path))
        self.assertEqual(code, 0, error)
        event_path = self.artifact("reviewed.json")
        event_bytes = event_path.read_bytes()
        self.assertEqual(self.collection.rows, before)
        code, repeated, error = self.adjudicate_cli(packet, decision, name="same-review.json",
                                                  extra=("--draft-origin", draft_path))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.artifact("same-review.json").read_bytes(), event_bytes)
        self.assertEqual(repeated, reviewed)
        code, dry, error = self.cli("outcome", "--input", event_path, "--dry-run",
                                   "--session-store", self.store)
        self.assertEqual((code, dry["status"]), (0, "dry_run"), error)
        self.assertEqual(self.collection.rows, before)
        code, published, error = self.cli("outcome", "--input", event_path,
                                         "--session-store", self.store, write=True)
        self.assertEqual(code, 0, error)
        after = deepcopy(self.collection.rows)
        code, retried, error = self.cli("outcome", "--input", event_path, write=True)
        self.assertEqual((code, retried["status"]), (0, "already_exists"), error)
        self.assertEqual(published["projection"], retried["projection"])
        self.assertEqual(retried["projection"]["rules"][0]["score"]["helpful"], 1)
        self.assertEqual(retried["projection"]["rules"][0]["maturity"], "candidate")
        self.assertEqual(self.collection.rows, after)
        self.assertEqual(event_path.read_bytes(), event_bytes)
        code, _, _ = self.adjudicate_cli(packet, decision, name="stale-review.json",
                                       extra=("--draft-origin", draft_path))
        self.assertNotEqual(code, 0)
        self.assertFalse(self.artifact("stale-review.json").exists())

    def test_later_correction_saved_original_and_same_session_harm_precedence(self):
        self.publish(self.proposal(), self.review())
        original = self.prepare(events=[self.proposal(), self.review()])
        code, helpful, error = self.adjudicate_cli(original)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.cli("outcome", "--input", self.artifact("reviewed.json"), write=True)[0], 0)
        later = NOW + timedelta(days=1)
        ref = self.turn_ref(text="Correction: the focused test hid the target integration failure.", index=1)
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET timestamp=? WHERE turn_index=1", (stamp(later),))
        events = [self.proposal(), self.review(), parse_event(helpful)]
        packet = self.prepare(self.selection([ref]), events=events, at=later)
        decision = self.decision(packet, event_id=str(UUID(int=31)), outcome="harmful",
                                 recorded_at=stamp(later))
        code, result, error = self.adjudicate_cli(packet, decision, name="correction.json", at=later)
        self.assertEqual(code, 0, error)
        code, published, error = self.cli("outcome", "--input", self.artifact("correction.json"),
                                         "--session-store", self.store, write=True, at=later)
        self.assertEqual(code, 0, error)
        score = published["projection"]["rules"][0]["score"]
        self.assertEqual((score["helpful"], score["harmful"]), (0, 1))
        self.assertIn("unresolved_harm", published["projection"]["rules"][0]["suppression_reasons"])
        self.assertEqual(result["payload"]["observed_at"], stamp(later))
        # The original saved drawer is independently resolvable, not synthesized
        # from the latest-task draft or the later feedback artifact.
        events.append(parse_event(result))
        saved_packet = self.prepare(events=events, at=later)
        code, saved, error = self.adjudicate_cli(saved_packet,
            self.decision(saved_packet, event_id=str(UUID(int=32)), outcome="neutral", recorded_at=stamp(later)),
            name="saved-original.json", at=later)
        self.assertEqual(code, 0, error)
        self.assertEqual(saved["payload"]["observed_at"], stamp())
        self.assertEqual(saved["payload"]["evidence"], self.refs[:1])

    def test_artifact_paths_are_exclusive_outside_palace_and_never_follow_symlinks(self):
        self.publish(self.proposal())
        packet = self.prepare()
        packet_path = self.artifact("packet.json", packet)
        decision_path = self.artifact("decision.json", self.decision(packet))
        sentinel = self.artifact("sentinel.json", {"keep": "exact bytes"})
        link = self.artifact("link.json")
        link.symlink_to(sentinel)
        directory_link = self.artifact("link-directory")
        directory_link.symlink_to(Path(self.tmp.name), target_is_directory=True)
        paths = [sentinel, link, directory_link / "new.json", Path(self.path, "forbidden.json")]
        before = deepcopy(self.collection.rows)
        sentinel_bytes = sentinel.read_bytes()
        for target in paths:
            code, _, _ = self.cli("feedback-adjudicate", "--input", packet_path,
                                 "--decision", decision_path, "--out", target)
            self.assertNotEqual(code, 0)
        self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
        self.assertFalse(self.artifact("new.json").exists())
        self.assertFalse(Path(self.path, "forbidden.json").exists())
        self.assertEqual(self.collection.rows, before)

    def test_raw_json_byte_bounds_duplicates_nonfinite_and_types_fail_without_output(self):
        self.publish(self.proposal())
        packet = self.prepare()
        decision = self.decision(packet, mode="abstain", reason="Original lacks attribution é.")
        paths = {"packet": self.artifact("raw-packet.json"), "decision": self.artifact("raw-decision.json")}
        target = self.artifact("strict-output.json")
        before = deepcopy(self.collection.rows)

        def invoke():
            return self.cli("feedback-adjudicate", "--input", paths["packet"],
                            "--decision", paths["decision"], "--out", target)

        for name, value, maximum in (("packet", packet, 65536), ("decision", decision, 24576)):
            base = canonical_json(value).encode("utf-8")
            for other, data in (("packet", packet), ("decision", decision)):
                paths[other].write_bytes(canonical_json(data).encode("utf-8"))
            paths[name].write_bytes(base + b" " * (maximum - len(base)))
            code, _, error = invoke()
            self.assertEqual(code, 0, error)
            target.unlink()
            invalid = [
                base + b" " * (maximum + 1 - len(base)), b"\xff",
                base[:-1] + b',"schema_version":1}', base.replace(b'"schema_version":1', b'"schema_version":true'),
                base.replace(b'"schema_version":1', b'"schema_version":1.0'),
                base.replace(b'"schema_version":1', b'"schema_version":NaN'),
                base.replace(b'"schema_version":1', b'"schema_version":Infinity'),
                base.replace(b'"schema_version":1', b'"schema_version":1e400'), b"[]", b"null",
            ]
            if name == "decision":
                invalid += [canonical_json(self.decision(packet)).replace(
                    '"behavior":', '"behavior":"duplicate","behavior":').encode("utf-8")]
            for raw in invalid:
                paths[name].write_bytes(raw)
                with self.subTest(name=name, raw=raw[:70]):
                    code, _, _ = invoke()
                    self.assertNotEqual(code, 0)
                    self.assertFalse(target.exists())
        self.assertEqual(self.collection.rows, before)

    def test_output_event_size_and_source_capture_preflight_keep_existing_limits(self):
        self.publish(self.proposal())
        long_id = "drawer-" + "x" * 18000
        self.collection.rows[long_id] = {**deepcopy(self.collection.rows["source-1"]), "id": long_id}
        ref = {**self.refs[0], "source_id": long_id}
        packet = self.prepare(self.selection([ref]))
        decision = self.decision(packet)
        decision["rationale"]["behavior"] = "x" * 20000
        code, result, _ = self.adjudicate_cli(packet, decision, name="oversize-event.json")
        self.assertNotEqual(code, 0)
        self.assertIn("32 KiB", result["error"])
        self.assertFalse(self.artifact("oversize-event.json").exists())
        ref = self.turn_ref(text="Observed result " + "x" * (4 * 1024 * 1024), index=1)
        ref["quote"] = "Observed result"
        packet = self.prepare(self.selection([ref]))
        before = deepcopy(self.collection.rows)
        code, result, _ = self.adjudicate_cli(packet, name="oversize-capture.json")
        self.assertNotEqual(code, 0)
        self.assertIn("4 MiB", result["error"])
        self.assertFalse(self.artifact("oversize-capture.json").exists())
        self.assertEqual(self.collection.rows, before)

    def test_review_failures_leave_no_output_and_preflight_never_persists(self):
        self.publish(self.proposal())
        packet = self.prepare()
        variants = [
            {"packet_id": "0" * 64}, {"outcome": "task_success"}, {"decision": "automatic"},
            {"rationale": {}}, {"recorded_at": stamp(NOW + timedelta(days=1))},
        ]
        before = deepcopy(self.collection.rows)
        for number, changes in enumerate(variants):
            name = f"invalid-{number}.json"
            code, _, _ = self.adjudicate_cli(packet, {**self.decision(packet), **changes}, name=name)
            self.assertNotEqual(code, 0)
            self.assertFalse(self.artifact(name).exists())
        with patch("dream_procedural_validate.AdmissionReader.persist",
                   side_effect=AssertionError("capture persistence")), \
             patch("dream_palace.ensure_firewall_schema", side_effect=AssertionError("schema")), \
             patch("dream_procedural_palace.local_embedder", side_effect=AssertionError("embedding")):
            self.assertEqual(self.adjudicate_cli(packet)[0], 0)
        self.assertEqual(self.collection.rows, before)

    def test_metrics_verdicts_compaction_and_success_never_choose_polarity_or_add_credit(self):
        self.publish(self.proposal(), self.review())
        before = deepcopy(self.collection.rows)
        reports = [
            "Retrieval benchmark recall rose from 0.4 to 0.9.",
            "Commit-scoped verification verdict: the implementation matches its tests.",
            "Compaction summary: all tasks completed and the rule was useful.",
            "All tests passed after retrieving the rule.",
        ]
        for number, text in enumerate(reports):
            ref = self.turn_ref(text=text, index=number + 1)
            code, packet, error = self.cli("feedback-prepare",
                "--input", self.artifact("selected.json", self.selection([ref])),
                "--session-store", self.store, "--out", self.artifact(f"report-packet-{number}.json"))
            self.assertEqual(code, 0, error)
            self.assertNotIn("outcome", packet)
            unsupported = self.decision(packet, mode="abstain",
                reason="This report establishes neither target rule application nor attributable effect.")
            code, abstained, error = self.adjudicate_cli(packet, unsupported,
                                                        name=f"report-abstention-{number}.json")
            self.assertEqual(code, 0, error)
            self.assertEqual(abstained["status"], "abstained")
            code, _, _ = self.adjudicate_cli(packet, self.decision(packet, outcome="task_success"),
                                            name=f"automatic-credit-{number}.json")
            self.assertNotEqual(code, 0)
            self.assertFalse(self.artifact(f"automatic-credit-{number}.json").exists())
        self.assertEqual(self.collection.rows, before)
        state = self.projection(self.proposal(), self.review()).rules[0]
        self.assertEqual((state.score.helpful, state.score.harmful), (0, 0))
        self.assertEqual(state.maturity, "candidate")
        # These assertions do not claim that arbitrary unmarked prose can be
        # classified, or that a dishonest nonblank rationale can be disproved.

    def test_generated_metadata_and_recognizable_transport_fail_but_foreign_scope_is_independent(self):
        from delivery_fixtures import wrapped_packet
        self.publish(self.proposal())
        before = deepcopy(self.collection.rows)
        packet = self.prepare()
        abstention = self.adjudicate(packet, self.decision(packet, mode="abstain"))
        for number, value in enumerate((packet, abstention)):
            ref = self.turn_ref(text="Observed target behavior\n" + canonical_json(canonical_json(value)), index=1)
            ref["quote"] = "Observed target behavior"
            code, _, _ = self.cli("feedback-prepare", "--input", self.artifact("generated.json", self.selection([ref])),
                "--out", self.artifact(f"generated-{number}.json"), "--session-store", self.store)
            self.assertNotEqual(code, 0)
            self.assertFalse(self.artifact(f"generated-{number}.json").exists())
        ref = self.turn_ref(text="Observed target behavior\n" + wrapped_packet("{}"), index=2)
        ref["quote"] = "Observed target behavior"
        with self.assertRaisesRegex(ValueError, "generated"):
            self.prepare(self.selection([ref]))
        # The report is explicitly marked generated using existing metadata,
        # not a keyword oracle for benchmark/verdict/compaction terminology.
        for form in ("native", "trailer"):
            source = deepcopy(self.collection.rows["source-1"])
            if form == "native":
                source["metadata"]["generated_summary"] = True
            else:
                source["text"] += '\n\n<!--dreaming-meta: {"generated_summary":true}-->'
            self.collection.rows["report"] = {**source, "id": "report"}
            report_ref = {**self.refs[0], "source_id": "report", "source_hash": content_hash(source["text"])}
            with self.assertRaisesRegex(ValueError, "generated"):
                self.prepare(self.selection([report_ref]))
        del self.collection.rows["report"]
        foreign = self.turn_ref(text="This rule succeeded in a fork with shared Git history.", index=3)
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE sessions SET repository='foreign/fork' WHERE id=?", (foreign["session_id"],))
        with self.assertRaisesRegex(ValueError, "repository"):
            self.prepare(self.selection([foreign]))
        self.assertEqual(self.collection.rows, before)

    def test_missing_drawer_witness_and_corrupt_capture_fail_without_host_fallback(self):
        self.publish(self.proposal())
        packet = self.prepare()
        original = deepcopy(self.collection.rows["source-1"])
        del self.collection.rows["source-1"]
        before = deepcopy(self.collection.rows)
        code, _, _ = self.adjudicate_cli(packet, name="lost-witness.json")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.artifact("lost-witness.json").exists())
        self.assertEqual(self.collection.rows, before)
        self.collection.rows["source-1"] = original
        capture = next(row for row in self.collection.rows.values()
                       if row["metadata"]["room"] == "procedural-sources")
        capture["text"] = capture["text"].replace('"observed_at":', '"corrupt_at":')
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate.acquire_original", side_effect=AssertionError("fallback")):
            code, _, _ = self.adjudicate_cli(packet, name="corrupt-capture.json")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.artifact("corrupt-capture.json").exists())
        self.assertEqual(self.collection.rows, before)

    def test_packet_source_and_draft_drift_fail_cli_before_creating_artifact(self):
        from dream_procedural_drafts import build_draft
        from dream_procedural_feedback import selection_from_draft
        self.publish(self.proposal())
        draft = build_draft(self.receipt(), repository="owner/repo", session_store=self.store, as_of=NOW)
        selection = selection_from_draft(draft, self.proposal().rule_id, [0])
        packet = self.prepare(selection, draft_origin=draft)
        draft_path = self.artifact("original-draft.json", draft)
        altered = deepcopy(draft)
        altered["lineage_references"][0]["generation"] += 1
        self.artifact("original-draft.json", altered)
        before = deepcopy(self.collection.rows)
        code, _, _ = self.adjudicate_cli(packet, name="draft-drift.json", extra=("--draft-origin", draft_path))
        self.assertNotEqual(code, 0)
        self.assertFalse(self.artifact("draft-drift.json").exists())
        self.artifact("original-draft.json", draft)
        for number, changes in enumerate((
                {"definition": {**packet["definition"], "statement": "Mutated rule"}},
                {"history_digest": "0" * 64}, {"source_session_id": "another-original"},
                {"delivery_receipt": {"kind": "procedural_receipt"}}, {"repository": "foreign/fork"})):
            tampered = {**deepcopy(packet), **changes}
            tampered["packet_id"] = content_hash(canonical_json({k: v for k, v in tampered.items()
                                                                 if k != "packet_id"}))
            name = f"packet-drift-{number}.json"
            code, _, _ = self.adjudicate_cli(tampered, name=name, extra=("--draft-origin", draft_path))
            self.assertNotEqual(code, 0)
            self.assertFalse(self.artifact(name).exists())
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET user_message='A changed original' WHERE session_id=?",
                        (self.refs[0]["session_id"],))
        code, _, _ = self.adjudicate_cli(packet, name="source-drift.json", extra=("--draft-origin", draft_path))
        self.assertNotEqual(code, 0)
        self.assertFalse(self.artifact("source-drift.json").exists())
        self.assertEqual(self.collection.rows, before)

    def test_partial_capture_then_unknown_event_write_retries_the_identical_artifact(self):
        from test_dream_procedural_palace import sanctioned_writer
        self.publish(self.proposal(), self.review())
        ref = self.turn_ref(text="The targeted run exposed the rule's adverse effect.", index=1)
        packet = self.prepare(self.selection([ref]), events=[self.proposal(), self.review()])
        code, event, error = self.adjudicate_cli(packet, self.decision(packet, outcome="harmful"))
        self.assertEqual(code, 0, error)
        target = self.artifact("reviewed.json")
        event_bytes = target.read_bytes()
        writer = sanctioned_writer(self.path, self.collection)
        original_add = writer._tools["mempalace_add_drawer"]["handler"]

        def fail_after_capture(wing, room, content, added_by):
            if room == "procedural":
                raise RuntimeError("write interrupted after source capture")
            return original_add(wing, room, content, added_by)

        writer._tools["mempalace_add_drawer"]["handler"] = fail_after_capture
        before_count = len(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", return_value=writer):
            code, _, _ = self.cli("outcome", "--input", target, "--session-store", self.store, write=None)
        self.assertNotEqual(code, 0)
        self.assertGreater(len(self.collection.rows), before_count)
        self.assertEqual(target.read_bytes(), event_bytes)

        def unknown_after_write(wing, room, content, added_by):
            result = original_add(wing, room, content, added_by)
            if room == "procedural":
                raise RuntimeError("acknowledgment lost after committed event")
            return result

        Path(self.store).unlink()
        writer._tools["mempalace_add_drawer"]["handler"] = unknown_after_write
        with patch.object(dream_palace, "MempalaceWriter", return_value=writer), \
             patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")):
            code, _, _ = self.cli("outcome", "--input", target, write=None)
        self.assertNotEqual(code, 0)
        after = deepcopy(self.collection.rows)
        code, repeated, error = self.cli("outcome", "--input", target)
        self.assertEqual((code, repeated["status"]), (0, "already_exists"), error)
        self.assertEqual(repeated["projection"]["rules"][0]["score"]["harmful"], 1)
        code, again, error = self.cli("outcome", "--input", target)
        self.assertEqual(code, 0, error)
        self.assertEqual(again["projection"], repeated["projection"])
        self.assertEqual(self.collection.rows, after)
        self.assertEqual(target.read_bytes(), event_bytes)
        changed = deepcopy(event)
        changed["payload"]["outcome"] = "helpful"
        changed["digest"] = content_hash(canonical_json({k: v for k, v in changed.items() if k != "digest"}))
        code, _, _ = self.cli("outcome", "--input", self.artifact("conflicting.json", changed))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.collection.rows, after)


class InstalledAdjudicationTests(AdjudicationFixture):
    def test_real_handlers_raw_capture_drawer_witness_live_wal_closed_read_and_subprocess(self):
        from dream_procedural_palace import append_event
        from test_dream_procedural_palace import installed_palace
        from mempalace.palace import get_backend_for_palace
        self.storage_patch.stop()
        raw_ref = self.turn_ref(text="The focused regression isolated the target parser branch.")
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
                    files.update({str(path): path.read_bytes() for path in Path(os.environ["HOME"]).rglob("*")
                                  if path.is_file() and not path.is_symlink()})
                    return files

                def run_reads(suffix):
                    before = snapshot()
                    with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
                         patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")), \
                         patch("dream_sessions.default_store_path", side_effect=AssertionError("hidden host")), \
                         patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")), \
                         patch("dream_procedural_palace.local_embedder", side_effect=AssertionError("embedding")):
                        for label, ref in (("raw", raw_ref), ("witness", self.refs[1])):
                            code, packet, error = self.cli("feedback-prepare",
                                "--input", self.artifact("selected.json", self.selection([ref])),
                                "--out", self.artifact(f"packet-{label}-{suffix}.json"))
                            self.assertEqual(code, 0, error)
                            for mode in ("publish", "abstain"):
                                code, result, error = self.adjudicate_cli(packet, self.decision(packet, mode=mode),
                                    name=f"{mode}-{label}-{suffix}.json", store=False)
                                self.assertEqual(code, 0, error)
                                if mode == "publish":
                                    self.assertEqual(result["payload"]["evidence"], [ref])
                    self.assertEqual(snapshot(), before)
                    self.assertEqual(server._config._file_config, before_config)

                self.assertTrue(Path(wal._WAL_FILE).exists())
                run_reads("live")
                self.assertEqual(tuple(collection._handle.conn.iterdump()), before_state)
            get_backend_for_palace(self.path).close_palace(self.path)
            run_reads("closed")
            packet_path = self.artifact("packet-raw-closed.json")
            packet = json.loads(packet_path.read_text())
            decision_path = self.artifact("process-decision.json", self.decision(packet))
            target = self.artifact("process-reviewed.json")
            script = Path(__file__).resolve().parents[2] / "skills/dreaming/scripts/dream_procedure.py"
            before = snapshot()
            completed = subprocess.run([sys.executable, str(script), "feedback-adjudicate",
                "--palace", self.path, "--wing", "w", "--repository", "owner/repo",
                "--input", str(packet_path), "--decision", str(decision_path), "--out", str(target)],
                capture_output=True, text=True, timeout=90,
                env={**os.environ, "MEMPALACE_MCP_READ_ONLY": "1"})
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["payload"]["evidence"], [raw_ref])
            self.assertEqual(json.loads(target.read_text()), result)
            self.assertEqual(snapshot(), before)

    def test_real_draft_reviewed_outcome_publish_and_exact_retry_after_host_loss(self):
        from dream_procedural_feedback import selection_from_draft
        from dream_procedural_palace import append_event
        from test_dream_procedural_palace import installed_palace
        self.storage_patch.stop()
        raw_ref = self.turn_ref(text="The observed target regression revealed the rule's harmful exception.")
        self.refs[0] = raw_ref
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for ref in self.refs[1:]:
                    ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"])["drawer_id"]
                for event in (self.proposal(), self.review(verdict="retire")):
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
                before = tuple(server._get_collection()._handle.conn.iterdump())
                draft_path = self.artifact("installed-draft.json")
                code, draft, error = self.cli("draft", "--input", self.artifact("legacy.json", self.receipt()),
                                            "--out", draft_path, "--session-store", self.store)
                self.assertEqual(code, 0, error)
                selection = selection_from_draft(draft, self.proposal().rule_id, [0])
                code, packet, error = self.cli("feedback-prepare",
                    "--input", self.artifact("selected.json", selection),
                    "--draft-origin", draft_path, "--out", self.artifact("installed-packet.json"))
                self.assertEqual(code, 0, error)
                code, event, error = self.adjudicate_cli(packet, self.decision(packet, outcome="harmful"),
                    store=False, extra=("--draft-origin", draft_path))
                self.assertEqual(code, 0, error)
                target = self.artifact("reviewed.json")
                event_bytes = target.read_bytes()
                Path(self.store).unlink()
                code, result, error = self.cli("outcome", "--input", target, "--dry-run")
                self.assertEqual((code, result["status"]), (0, "dry_run"), error)
                self.assertEqual(tuple(server._get_collection()._handle.conn.iterdump()), before)
                code, published, error = self.cli("outcome", "--input", target, write=None)
                self.assertEqual((code, published["status"]), (0, "appended"), error)
                after = tuple(server._get_collection()._handle.conn.iterdump())
                code, retried, error = self.cli("outcome", "--input", target)
                self.assertEqual((code, retried["status"]), (0, "already_exists"), error)
                self.assertEqual(retried["projection"], published["projection"])
                state = retried["projection"]["rules"][0]
                self.assertEqual(state["score"]["harmful"], 1)
                self.assertIn("retired", state["suppression_reasons"])
                self.assertFalse(state["eligible"])
                self.assertEqual(event["payload"]["observed_at"], stamp())
                self.assertEqual(tuple(server._get_collection()._handle.conn.iterdump()), after)
                self.assertEqual(target.read_bytes(), event_bytes)
