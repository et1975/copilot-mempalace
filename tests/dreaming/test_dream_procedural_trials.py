"""Existing trial gates, not evidence of empirical usefulness or agent compliance."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch
from uuid import UUID

import pytest

import dream_palace
from delivery_fixtures import applicability, case
from dream_metadata import canonical_json
from dream_procedural import Policy, event_to_data, parse_event, project_rules
from dream_procedural_palace import (
    GuidanceLimits, append_event, get_task_guidance, read_events, record_data,
)
from test_dream_procedural import (
    NOW, definition, event_data, evidence, parsed, sha, stamp,
)
from test_dream_procedural_palace import installed_mutation, installed_palace
from test_dream_procedural_validate import GroundedFixture


def ranked(events, *, at=NOW, repository="owner/repo", include_candidates=True):
    return get_task_guidance(
        project_rules(events, as_of=at, policy=Policy()),
        task="Fix a reproducible defect with a focused regression test.",
        repository=repository, embedder=lambda texts: [[1.0, 0.0] for _ in texts],
        limits=GuidanceLimits(max_items=1), as_of=at,
        include_candidates=include_candidates,
    ).data


@pytest.mark.parametrize("state", [
    "unapproved", "stale", "conflicted", "retired", "harmful", "acknowledged_harm",
    "contradicted",
])
def test_candidate_switch_never_overrides_policy_suppression(state):
    events = [parsed()]
    at = NOW
    if state != "unapproved":
        events.append(parsed("review", 2))
    if state == "stale":
        at += timedelta(days=90)
    elif state == "conflicted":
        events.append(parsed("review", 3))
    elif state == "retired":
        events.append(parsed("review", 3, verdict="retire",
                             parent_review_ids=[events[-1].event_id]))
    elif state in {"harmful", "acknowledged_harm"}:
        harm = parsed("outcome", 3, outcome="harmful")
        events.append(harm)
        if state == "acknowledged_harm":
            events.append(parsed("review", 4, parent_review_ids=[events[1].event_id],
                acknowledged_evidence_ids=[harm.event_id],
                dispositions=[dict(evidence_id=harm.event_id, disposition="contradicts",
                    reason="The observed harm is valid.", evidence=[evidence()])]))
    elif state == "contradicted":
        events.append(parsed("review", 3, parent_review_ids=[events[-1].event_id],
            dispositions=[dict(evidence_id="source", disposition="contradicts",
                reason="The trigger has an applicable counterexample.", evidence=[evidence()])]))
    result = ranked(events, at=at)
    assert result["status"] == "no_eligible_rules"
    assert result["item_count"] == 0
    assert result["trials"] == []


def test_single_item_candidate_search_can_select_established_advice_instead():
    established = definition(statement="Preserve the focused regression test.")
    events = [
        parsed(), parsed("review", 2),
        parsed("proposal", 10, rule=established),
        parsed("review", 11, rule=established),
        *[parsed("outcome", number, rule=established) for number in range(12, 15)],
    ]
    result = ranked(events)
    assert result["item_count"] == 1
    assert result["rules"][0]["rule_id"] == events[2].rule_id
    assert result["rules"][0]["maturity"] == "established"
    assert result["trials"] == []


def test_foreign_proven_support_never_becomes_target_trial_or_target_credit():
    events = [parsed(), parsed("review", 2),
              *[parsed("outcome", number) for number in range(10, 20)]]
    assert ranked(events)["rules"][0]["maturity"] == "proven"
    result = ranked(events, repository="owner/target")
    assert result["status"] == "no_rules"
    assert result["item_count"] == 0
    assert result["trials"] == []


class InstalledTrialTests(GroundedFixture):
    """Real CLI, source admission, SQLite storage, handlers and offline MiniLM."""

    def setUp(self):
        super().setUp()
        self.storage_patch.stop()
        self.server = self.enterContext(installed_palace(self.path))
        self.writer = dream_palace.MempalaceWriter()
        self.enterContext(self.writer.mutation())
        for ref in self.refs:
            ref["source_id"] = self.writer.add_drawer(
                "w", "diary", ref["quote"], added_by="original-agent",
            )["drawer_id"]

    def append(self, event):
        return append_event(self.path, "w", event, writer=self.writer,
                            session_store=self.store, clock=lambda: NOW)

    def approve(self):
        self.append(self.proposal())
        self.append(self.review())

    def cli(self, command, *args, at=NOW):
        from dream_procedure import main
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("dream_procedure.now_utc", return_value=at):
            code = main([command, "--palace", self.path, "--wing", "w", *args])
        return code, json.loads(out.getvalue()), err.getvalue()

    def guidance(self, *, candidates=False, at=NOW):
        args = ["--repository", "owner/repo", "--task", definition()["statement"],
                "--max-items", "1"]
        if candidates:
            args.append("--include-candidates")
        return self.cli("guidance", *args, at=at)

    def original_turn(self, number, text):
        session = str(UUID(int=1000 + number))
        with sqlite3.connect(self.store) as con:
            con.execute("INSERT INTO sessions VALUES (?,?)", (session, "owner/repo"))
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (session, 0, text, "", stamp()))
        return dict(evidence(session, session, text), source_kind="session_turn",
                    turn_index=0, field="user_message")

    def offer(self):
        packet, current, permissions, _ = case(task=definition()["statement"])
        current["context"]["mode"] = "approved_candidate_trial"
        permissions["trials"] = "allow"
        root = Path(self.tmp.name)
        (root / "current.json").write_text(canonical_json(current), encoding="utf-8")
        (root / "permissions.json").write_text(canonical_json(permissions), encoding="utf-8")
        args = ["--current", str(root / "current.json"),
                "--permissions", str(root / "permissions.json")]
        code, offered, err = self.cli("task-guidance", *args,
            "--request-id", packet["request_id"], "--out", str(root / "packet.json"))
        self.assertEqual((code, offered["status"]), (0, "bound_guidance"), err)
        items = offered["guidance"]["trials"]
        self.assertEqual(len(items), 1)
        (root / "fit.json").write_text(
            canonical_json(applicability(offered["context"], items)), encoding="utf-8")
        return root, [*args, "--packet", str(root / "packet.json"),
                      "--rule-id", items[0]["rule_id"],
                      "--applicability", str(root / "fit.json")]

    def test_default_excludes_candidate_and_deliberate_local_reuse_adds_no_credit(self):
        self.approve()
        before_events = read_events(self.path, "w")
        before_state = tuple(self.server._get_collection()._handle.conn.iterdump())
        code, result, err = self.guidance()
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)
        self.assertEqual(result["trials"], [])
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["item_count"]), (0, 1), err)
        trial = result["trials"][0]
        self.assertEqual((trial["delivery"], trial["effective_score"]),
                         ("approved_candidate_trial", 0))
        _, args = self.offer()
        for _ in range(2):
            code, result, err = self.cli("use-check", *args)
            self.assertEqual((code, result["status"]), (0, "usable"), err)
            self.assertEqual(result["items"][0]["rule_id"], trial["rule_id"])
        self.assertEqual(read_events(self.path, "w"), before_events)
        self.assertEqual(tuple(self.server._get_collection()._handle.conn.iterdump()), before_state)
        state = project_rules(before_events, as_of=NOW, policy=Policy()).rules[0]
        self.assertEqual((state.maturity, state.score.helpful, state.score.harmful),
                         ("candidate", 0, 0))

    def test_three_target_original_sessions_required_before_enrollment(self):
        raw = dict(self.refs[0], source_kind="session_turn",
                   source_id=self.refs[0]["session_id"], turn_index=0, field="user_message")
        for refs in (self.refs[:2], [self.refs[0], raw, self.refs[1]]):
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, "three"):
                self.append(parse_event(event_data(origin_drawer_ids=[], evidence=refs)))
            self.assertEqual(read_events(self.path, "w"), [])
        self.approve()
        self.assertEqual(len(read_events(self.path, "w")), 2)

    def test_foreign_or_repositoryless_originals_cannot_enroll_target_rule(self):
        for repository in ("owner/foreign", None):
            with self.subTest(repository=repository):
                with sqlite3.connect(self.store) as con:
                    con.execute("UPDATE sessions SET repository=? WHERE id=?",
                                (repository, self.refs[2]["session_id"]))
                with self.assertRaisesRegex(ValueError, "repository"):
                    self.append(self.proposal())
                self.assertEqual(read_events(self.path, "w"), [])

    def test_review_needs_three_supporting_target_sessions_not_only_a_proposal(self):
        self.append(self.proposal())
        dispositions = event_to_data(self.review())["payload"]["dispositions"]
        dispositions[2].update(disposition="not_applicable",
                               reason="This original does not support the proposed condition.")
        with self.assertRaisesRegex(ValueError, "three"):
            self.append(self.review(dispositions=dispositions))
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)

    def test_cli_candidate_switch_withholds_unapproved_and_retired_rules(self):
        self.append(self.proposal())
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)
        approval = self.review()
        self.append(approval)
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["item_count"]), (0, 1), err)
        self.append(self.review(3, verdict="retire", parent_review_ids=[approval.event_id]))
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)
        self.assertEqual(result["trials"], [])

    def test_cli_rechecks_retained_concurrent_approvals_before_trial_use(self):
        self.approve()
        _, args = self.offer()
        # Seed retained concurrent history; ordinary append rejects a stale parent list.
        body, metadata = record_data(self.review(3))
        self.writer.add_drawer("w", "procedural", body,
                               added_by="dream-procedure", metadata=metadata)
        code, result, err = self.guidance(candidates=True)
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)
        code, result, err = self.cli("use-check", *args)
        self.assertEqual((code, result["status"]), (0, "withheld"), err)
        self.assertEqual(result["items"], [])

    def test_foreign_approval_and_current_target_permission_do_not_supply_target_trial(self):
        self.approve()
        root, _ = self.offer()
        current = json.loads((root / "current.json").read_text())
        permission = json.loads((root / "permissions.json").read_text())
        current["context"]["repository"] = permission["repository"] = "owner/target"
        (root / "current.json").write_text(canonical_json(current), encoding="utf-8")
        (root / "permissions.json").write_text(canonical_json(permission), encoding="utf-8")
        code, result, err = self.cli("task-guidance",
            "--current", str(root / "current.json"),
            "--permissions", str(root / "permissions.json"),
            "--request-id", str(UUID(int=990)), "--out", str(root / "target-packet.json"))
        self.assertEqual((code, result["guidance"]["status"]), (0, "no_rules"), err)
        self.assertEqual(result["guidance"]["trials"], [])
        self.assertEqual(len(read_events(self.path, "w")), 2)

    def test_frozen_trial_packet_cannot_bypass_current_fit_or_trial_permission(self):
        self.approve()
        root, args = self.offer()
        fit_path = root / "fit.json"
        original_fit = json.loads(fit_path.read_text())
        for verdict in ("incompatible", "unknown"):
            fit = deepcopy(original_fit)
            fit["assessments"][0]["supported_context"].update(
                verdict=verdict, reason="The current framework differs from the reviewed originals.")
            fit_path.write_text(canonical_json(fit), encoding="utf-8")
            code, result, err = self.cli("use-check", *args)
            self.assertEqual((code, result["status"]), (0, "withheld"), err)
            self.assertEqual(result["items"], [])
        fit_path.write_text(canonical_json(original_fit), encoding="utf-8")
        permission_path = root / "permissions.json"
        permissions = json.loads(permission_path.read_text())
        permissions["trials"] = "deny"
        permission_path.write_text(canonical_json(permissions), encoding="utf-8")
        code, result, err = self.cli("use-check", *args)
        self.assertNotEqual(code, 0, err)
        self.assertNotEqual(result.get("status"), "usable")

    def test_cli_revalidates_stale_review_and_drifted_or_missing_original(self):
        self.approve()
        _, args = self.offer()
        code, result, err = self.guidance(candidates=True, at=NOW + timedelta(days=90))
        self.assertEqual((code, result["status"]), (0, "no_eligible_rules"), err)
        for damage in ("drifted", "missing"):
            with self.subTest(damage=damage):
                with installed_mutation(self.server) as collection:
                    if damage == "drifted":
                        collection.update(ids=[self.refs[0]["source_id"]],
                                          documents=["Changed original observation."])
                    else:
                        collection.delete(ids=[self.refs[0]["source_id"]])
                for command, extra in (("guidance", ["--repository", "owner/repo",
                        "--task", definition()["statement"], "--include-candidates", "--max-items", "1"]),
                        ("use-check", args)):
                    code, result, err = self.cli(command, *extra)
                    self.assertNotEqual(code, 0, err)
                    self.assertEqual(result["kind"], "evidence_unavailable")
                    self.assertNotEqual(result.get("status"), "usable")

    def test_harm_stops_trial_preserves_original_and_cannot_be_removed_by_decay(self):
        self.approve()
        _, args = self.offer()
        ref = self.original_turn(5, "Following the focused-test rule delayed the permitted mitigation.")
        harm = parse_event(event_data("outcome", 20, outcome="harmful",
            source_session_id=ref["session_id"], evidence=[ref],
            attribution="The focused-test step delayed the mitigation under this trigger."))
        self.append(harm)
        code, result, err = self.cli("use-check", *args)
        self.assertEqual((code, result["status"]), (0, "withheld"), err)
        self.assertEqual(result["items"], [])
        events = read_events(self.path, "w")
        self.assertIn(harm, events)
        for at in (NOW, NOW + timedelta(days=900)):
            state = project_rules(events, as_of=at, policy=Policy()).rules[0]
            self.assertIn("unresolved_harm", state.suppression_reasons)
            self.assertFalse(state.eligible)

    def test_manual_neutral_needs_explicit_original_attribution_and_adds_no_helpfulness(self):
        self.approve()
        ref = self.original_turn(6, "The focused regression test was performed; it did not change "
                                 "the already isolated diagnosis or the chosen fix.")
        event = event_data("outcome", 30, outcome="neutral",
            source_session_id=ref["session_id"], evidence=[ref],
            attribution="This focused test changed neither the existing diagnosis nor the fix.")
        before = read_events(self.path, "w")
        for changes in ({"attribution": ""}, {"evidence": []}, {"outcome": "task_success"}):
            invalid = deepcopy(event)
            invalid["payload"].update(changes)
            invalid["digest"] = sha({key: value for key, value in invalid.items() if key != "digest"})
            with self.assertRaises(ValueError):
                parse_event(invalid)
        self.assertEqual(read_events(self.path, "w"), before)
        artifact = Path(self.tmp.name, "manual-neutral.json")
        artifact.write_text(canonical_json(event), encoding="utf-8")
        code, result, err = self.cli("outcome", "--session-store", self.store,
                                     "--input", str(artifact))
        self.assertEqual((code, result["status"]), (0, "appended"), err)
        events = read_events(self.path, "w")
        self.assertIn(parse_event(event), events)
        state = project_rules(events, as_of=NOW, policy=Policy()).rules[0]
        self.assertEqual((state.score.helpful, state.score.harmful, state.score.effective_score),
                         (0, 0, 0))
        self.assertEqual(state.maturity, "candidate")
