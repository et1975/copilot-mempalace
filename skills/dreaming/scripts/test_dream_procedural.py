"""Reusable synthetic events for the pure core and follow-on adapter/CLI tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import unittest
from uuid import UUID

NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def stamp(value=NOW):
    return value.isoformat().replace("+00:00", "Z")


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def definition(**changes):
    return {"rule_type": "rule", "statement": "Use a focused regression test.",
            "scope": {"kind": "repository", "key": "owner/repo"}, "category": "testing",
            "applies_when": "Fixing a reproducible defect.", "exceptions": [], **changes}


def evidence(source_id="source", session_id="session-1", text="observed result"):
    return {"source_kind": "drawer", "source_id": source_id, "session_id": session_id,
            "quote": text, "source_hash": hashlib.sha256(text.encode()).hexdigest()}


def event_data(kind="proposal", number=1, *, rule=None, at=NOW, **payload):
    rule = rule or definition()
    rule_id = "proc:" + sha(rule)
    if kind == "proposal":
        body = {"definition": rule, "origin_drawer_ids": ["reflection"],
                "evidence": [evidence()]}
    elif kind == "review":
        packet = {"rule_id": rule_id, "repository": "owner/repo", "validated_at": stamp(at),
                  "queries": ["regression test", "regression test counterexample"],
                  "evidence": [evidence()]}
        body = {"verdict": "approve", "parent_review_ids": [], "validation_packet": packet,
                "validation_digest": sha(packet), "dispositions": [],
                "acknowledged_evidence_ids": [], "reason": "Reviewed original evidence.",
                "replacement_rule_id": None}
    else:
        body = {"outcome": "helpful", "observation_id": f"observation-{number}",
                "source_session_id": f"session-{number}", "repository": "owner/repo",
                "observed_at": stamp(at), "evidence": [evidence(session_id=f"session-{number}")],
                "attribution": "Following this rule caught the failing edge case."}
    body.update(payload)
    result = {"schema_version": 1, "event_id": str(UUID(int=number)), "rule_id": rule_id,
              "event_kind": kind, "recorded_at": stamp(at), "actor_kind": "agent",
              "session_id": "reviewer-session", "payload": body}
    result["digest"] = sha(result)
    return result


def parsed(*args, **kwargs):
    from dream_procedural import parse_event
    return parse_event(event_data(*args, **kwargs))


def resign(data):
    data["digest"] = sha({k: v for k, v in data.items() if k != "digest"})
    return data


class ContractTests(unittest.TestCase):
    def test_identity_normalizes_only_nfc_outer_whitespace_repository_and_exception_order(self):
        from dream_procedural import canonical_rule_id
        a = definition(statement="  Caf\u0065\u0301 ", exceptions=[" Z ", "A"],
                       scope={"kind": "repository", "key": "Owner/Repo"})
        b = definition(statement="Caf\u00e9", exceptions=["A", "Z"])
        self.assertEqual(canonical_rule_id(a), "proc:" + sha(b))
        self.assertNotEqual(canonical_rule_id(b),
                            canonical_rule_id(dict(b, statement="caf\u00e9")))
        self.assertNotEqual(canonical_rule_id(b),
                            canonical_rule_id(dict(b, applies_when="Always.")))

    def test_strict_envelope_and_payload(self):
        from dream_procedural import parse_event, event_to_data
        base = event_data()
        self.assertEqual(event_to_data(parse_event(base)), base)
        mutations = [
            lambda d: d.update(schema_version=2),
            lambda d: d.update(event_kind="retrieval"),
            lambda d: d.update(recorded_at="2026-02-30T00:00:00Z"),
            lambda d: d.update(recorded_at="2026-09-23"),
            lambda d: d.update(recorded_at="2026-09-23T00:00:00+01:00"),
            lambda d: d.update(actor_kind="automatic"),
            lambda d: d.update(session_id=" "),
            lambda d: d.update(event_id="not-a-uuid"),
            lambda d: d.update(extra="unknown"),
            lambda d: d["payload"]["evidence"][0].update(quote=""),
            lambda d: d["payload"]["definition"].update(statement="x" * 801),
            lambda d: d["payload"].update(unknown=True),
            lambda d: d.update(rule_id="proc:" + "f" * 64),
        ]
        for mutate in mutations:
            data = deepcopy(base)
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_event(resign(data))
        base["payload"]["origin_drawer_ids"] = ["tampered"]
        with self.assertRaisesRegex(ValueError, "digest"):
            parse_event(base)

    def test_outcomes_require_explicit_attribution_references_and_original_time(self):
        from dream_procedural import parse_event
        for fields in ({"attribution": ""}, {"outcome": "success"},
                       {"evidence": []}, {"observed_at": stamp(NOW + timedelta(seconds=1))}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_event(event_data("outcome", **fields))

    def test_packet_digest_and_evidence_backed_disposition(self):
        from dream_procedural import parse_event
        data = event_data("review", validation_digest="0" * 64)
        with self.assertRaisesRegex(ValueError, "digest"):
            parse_event(data)
        data = event_data("review", dispositions=[{
            "evidence_id": str(UUID(int=3)), "disposition": "invalid",
            "reason": "The attributed behavior did not occur.", "evidence": []}])
        with self.assertRaises(ValueError):
            parse_event(data)

    def test_encoded_size_and_source_turn_shape(self):
        from dream_procedural import parse_event
        with self.assertRaises(ValueError):
            parse_event(event_data("outcome", attribution="x" * 33000))
        ref = {**evidence(), "source_kind": "session_turn"}
        with self.assertRaises(ValueError):
            parse_event(event_data(evidence=[ref]))
        ref.update(turn_index=0, field="assistant_response", source_id="session-1")
        self.assertEqual(parse_event(event_data(evidence=[ref])).payload.evidence[0].turn_index, 0)


class ScoreTests(unittest.TestCase):
    def score(self, events, as_of=NOW):
        from dream_procedural import score_outcomes, Policy
        return score_outcomes(events, as_of=as_of, policy=Policy())

    def test_hand_calculated_weights_and_harm_multiplier(self):
        helpful = parsed("outcome", 1)
        self.assertEqual(self.score([helpful]).helpful, 1)
        self.assertEqual(self.score([helpful], NOW + timedelta(days=90)).helpful, .5)
        score = self.score([helpful, parsed("outcome", 2, outcome="harmful")])
        self.assertEqual((score.helpful, score.harmful, score.effective_score), (1, 1, -3))
        self.assertEqual(self.score([parsed("outcome", 3, outcome="neutral")]).effective_score, 0)

    def test_session_harm_wins_and_earliest_original_time_anchors_decay(self):
        events = [parsed("outcome", 1, at=NOW - timedelta(days=90), source_session_id="s",
                         evidence=[evidence(session_id="s")]),
                  parsed("outcome", 2, source_session_id="s", evidence=[evidence(session_id="s")])]
        self.assertEqual(self.score(events).helpful, .5)
        events.extend([parsed("outcome", 3, source_session_id="s", outcome="harmful",
                              evidence=[evidence(session_id="s")], at=NOW - timedelta(days=90)),
                       parsed("outcome", 4, source_session_id="s", outcome="harmful",
                              evidence=[evidence(session_id="s")])])
        score = self.score(events)
        self.assertEqual((score.helpful, score.harmful, score.effective_score), (0, .5, -2))
        self.assertEqual(score.terms[0].event_id, str(UUID(int=3)))

    def test_future_time_and_invalid_policy_rejected_not_clamped(self):
        from dream_procedural import Policy, score_outcomes
        with self.assertRaises(ValueError):
            self.score([parsed("outcome", at=NOW + timedelta(seconds=1))])
        for half_life in (0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                score_outcomes([], as_of=NOW, policy=Policy(half_life_days=half_life))


class ProjectionTests(unittest.TestCase):
    def project(self, events, at=NOW):
        from dream_procedural import project_rules, Policy
        return project_rules(events, as_of=at, policy=Policy())

    def state(self, *events, at=NOW):
        return self.project([parsed(), *events], at).rules[0]

    def test_empty_projection_is_not_an_error(self):
        projection = self.project([])
        self.assertEqual((projection.rules, projection.errors), ((), ()))

    def test_maturity_boundaries_do_not_grant_approval(self):
        for count, label in ((0, "candidate"), (2, "candidate"), (3, "established"),
                             (9, "established"), (10, "proven")):
            events = [parsed("outcome", i + 10) for i in range(count)]
            state = self.state(*events)
            self.assertEqual(state.maturity, label)
            self.assertFalse(state.eligible)
            approved = self.state(*events, parsed("review", 2))
            self.assertTrue(approved.eligible)
            if count in (3, 10):
                older = self.state(*events, parsed("review", 2), at=NOW + timedelta(seconds=1))
                self.assertNotEqual(older.maturity, label)

    def test_duplicate_proposals_and_outcomes_add_no_credit(self):
        outcome = parsed("outcome", 3)
        result = self.project([parsed(), parsed("proposal", 2), outcome, outcome])
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.rules[0].score.helpful, 1)
        duplicate = parsed("outcome", 4, source_session_id="session-3",
                           evidence=[evidence(session_id="session-3")])
        self.assertEqual(self.state(outcome, duplicate).score.helpful, 1)

    def test_conflicting_event_ids_invalidate_all_affected_rules(self):
        data = event_data("proposal", 1, rule=definition(statement="Different guidance."))
        result = self.project([parsed(), __import__("dream_procedural").parse_event(data)])
        self.assertTrue(result.errors)
        self.assertEqual(len(result.rules), 2)
        self.assertTrue(all(not r.eligible and "event_id_conflict" in r.suppression_reasons
                            for r in result.rules))

    def test_review_heads_join_missing_parents_and_terminal_retirement(self):
        a, b = parsed("review", 2), parsed("review", 3)
        conflicted = self.state(a, b)
        self.assertFalse(conflicted.eligible)
        self.assertIn("conflicted", conflicted.suppression_reasons)
        join = parsed("review", 4, parent_review_ids=[a.event_id, b.event_id])
        self.assertTrue(self.state(a, b, join).eligible)
        self.assertFalse(self.state(join).eligible)
        retired = parsed("review", 5, verdict="retire", parent_review_ids=[join.event_id])
        resurrection = parsed("review", 6, parent_review_ids=[retired.event_id])
        state = self.state(a, b, join, retired, resurrection)
        self.assertIn("retired", state.suppression_reasons)
        self.assertFalse(state.eligible)
        self.assertFalse(self.state(a, parsed("review", 7, verdict="retire")).eligible)

    def test_review_cycle_and_replacement_cycle_suppressed(self):
        a = parsed("review", 2, parent_review_ids=[str(UUID(int=3))])
        b = parsed("review", 3, parent_review_ids=[a.event_id])
        self.assertIn("review_cycle", self.state(a, b).suppression_reasons)
        other = definition(statement="Use another approach.")
        from dream_procedural import canonical_rule_id
        events = [parsed(), parsed("proposal", 10, rule=other),
                  parsed("review", 2, verdict="replace", replacement_rule_id=canonical_rule_id(other)),
                  parsed("review", 11, rule=other, verdict="replace",
                         replacement_rule_id=canonical_rule_id(definition()))]
        self.assertTrue(all("replacement_cycle" in r.suppression_reasons
                            for r in self.project(events).rules))

    def test_harm_does_not_disappear_with_decay_and_review_stales_at_90_days(self):
        approval = parsed("review", 2)
        harmful = parsed("outcome", 3, outcome="harmful")
        self.assertIn("unresolved_harm", self.state(approval, harmful).suppression_reasons)
        self.assertIn("unresolved_harm", self.state(approval, harmful,
                      at=NOW + timedelta(days=90000)).suppression_reasons)
        self.assertTrue(self.state(approval, at=NOW + timedelta(days=90, microseconds=-1)).eligible)
        self.assertIn("stale_validation",
                      self.state(approval, at=NOW + timedelta(days=90)).suppression_reasons)

    def test_reviewed_disposition_recomputes_from_retained_original_anchors(self):
        helpful = parsed("outcome", 3, at=NOW - timedelta(days=90))
        harmful = parsed("outcome", 4, outcome="harmful", source_session_id="session-3",
                         evidence=[evidence(session_id="session-3")])
        review = parsed("review", 5, acknowledged_evidence_ids=[harmful.event_id],
                        dispositions=[{"evidence_id": harmful.event_id, "disposition": "invalid",
                                       "reason": "Not caused by following this rule.",
                                       "evidence": [evidence()]}])
        state = self.state(helpful, harmful, review)
        self.assertTrue(state.eligible)
        self.assertEqual(state.score.helpful, .5)
        self.assertEqual(state.score.harmful, 0)
        self.assertEqual(state.dispositions, review.payload.dispositions)
        new_harm = parsed("outcome", 6, outcome="harmful")
        self.assertFalse(self.state(helpful, harmful, review, new_harm).eligible)

    def test_unavailable_declared_evidence_wrong_scope_and_future_events(self):
        review = parsed("review", 2, acknowledged_evidence_ids=[str(UUID(int=999))])
        self.assertIn("missing_declared_evidence", self.state(review).suppression_reasons)
        wrong = parsed("outcome", 3, repository="owner/other")
        self.assertIn("scope_mismatch", self.state(parsed("review", 2), wrong).suppression_reasons)
        future = parsed("review", 4, at=NOW + timedelta(seconds=1))
        self.assertTrue(self.project([parsed(), future]).errors)

    def test_shuffled_replay_is_identical(self):
        events = [parsed(), parsed("review", 2)] + [parsed("outcome", i) for i in range(3, 15)]
        expected = self.project(events)
        for seed in range(12):
            shuffled = list(events)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(self.project(shuffled), expected)

    def test_bounds_fail_with_errors_not_partial_history(self):
        result = self.project([parsed()] * 5001)
        self.assertEqual(result.rules, ())
        self.assertEqual(result.errors[0].code, "event_limit")
        result = self.project([parsed("proposal", i + 1,
                                     rule=definition(statement=f"Rule {i}."))
                               for i in range(101)])
        self.assertEqual(result.rules, ())
        self.assertEqual(result.errors[0].code, "rule_limit")

    def test_declared_contradiction_requires_explicit_acknowledgment(self):
        review = parsed("review", 2, dispositions=[{
            "evidence_id": "source", "disposition": "contradicts",
            "reason": "This source describes a counterexample.", "evidence": [evidence()]}])
        self.assertFalse(self.state(review).eligible)
        self.assertIn("unacknowledged_conflict", self.state(review).suppression_reasons)

    def test_acknowledging_valid_harm_is_not_a_license_for_unsafe_trials(self):
        harm = parsed("outcome", 3, outcome="harmful")
        review = parsed("review", 4, acknowledged_evidence_ids=[harm.event_id],
                        dispositions=[{"evidence_id": harm.event_id, "disposition": "contradicts",
                                       "reason": "The harm is real.", "evidence": [evidence()]}])
        self.assertFalse(self.state(harm, review).eligible)
        self.assertIn("unresolved_harm", self.state(harm, review).suppression_reasons)

    def test_missing_evidence_in_ancestor_cannot_be_hidden_by_new_head(self):
        bad = parsed("review", 2, acknowledged_evidence_ids=["missing"])
        join = parsed("review", 3, parent_review_ids=[bad.event_id])
        self.assertIn("missing_declared_evidence", self.state(bad, join).suppression_reasons)

    def test_duplicate_evidence_session_attribution_cannot_be_contradictory(self):
        from dream_procedural import parse_event
        ref = evidence()
        forged = dict(ref, session_id="different")
        with self.assertRaises(ValueError):
            parse_event(event_data(evidence=[ref, forged]))

    def test_review_disposition_survives_descendants_but_concurrent_dispositions_need_resolution(self):
        harm = parsed("outcome", 3, outcome="harmful")
        disposition = {"evidence_id": harm.event_id, "disposition": "invalid",
                       "reason": "Misattributed action.", "evidence": [evidence()]}
        first = parsed("review", 4, acknowledged_evidence_ids=[harm.event_id],
                       dispositions=[disposition])
        next_review = parsed("review", 5, parent_review_ids=[first.event_id],
                             acknowledged_evidence_ids=[harm.event_id])
        state = self.state(harm, first, next_review)
        self.assertEqual(state.score.harmful, 0)
        self.assertEqual(state.dispositions, first.payload.dispositions)
        alternative = parsed("review", 6, acknowledged_evidence_ids=[harm.event_id],
                             dispositions=[dict(disposition, disposition="contradicts")])
        joined = parsed("review", 7, parent_review_ids=[first.event_id, alternative.event_id],
                        acknowledged_evidence_ids=[harm.event_id])
        state = self.state(harm, first, alternative, joined)
        self.assertFalse(state.eligible)
        self.assertIn("disposition_conflict", state.suppression_reasons)
