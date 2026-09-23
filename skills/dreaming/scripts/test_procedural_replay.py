"""Chronological synthetic evaluation, not a production accuracy benchmark."""
from datetime import timedelta
import unittest

from dream_procedural import Policy, event_to_data, parse_event, project_rules
from dream_procedural_palace import GuidanceLimits, get_task_guidance, revalidate_sources
from test_dream_procedural import NOW, definition, event_data, evidence, parsed, resign
from test_dream_procedural_validate import GroundedFixture


def deliver(events, at, *, repository="owner/repo", trials=False):
    projection = project_rules(events, as_of=at, policy=Policy())
    result = get_task_guidance(projection, task="Fix a reproducible defect.", repository=repository,
        embedder=lambda texts: [[1., 0.] for text in texts], limits=GuidanceLimits(),
        include_candidates=trials, as_of=at)
    ids = [item["rule_id"] for key in ("rules", "anti_patterns", "trials") for item in result.data[key]]
    return projection, result, ids


class ChronologicalReplayTests(unittest.TestCase):
    def test_useful_recurring_advice_coverage_without_lookahead_or_retrieval_feedback(self):
        events = [parsed(at=NOW - timedelta(days=1)), parsed("review", 2, at=NOW - timedelta(days=1))]
        rid = events[0].rule_id
        delivered, baseline, abstentions, violations = 0, 0, 0, 0
        for day in range(6):
            task_at = NOW + timedelta(days=day)
            before = list(events)
            projection, result, ids = deliver(events, task_at)
            self.assertEqual(ids, [rid] if day >= 4 else [])
            self.assertTrue(all(e.recorded_at < task_at for s in projection.rules for e in s.events))
            score = projection.rules[0].score
            self.assertEqual(score.helpful_sessions, day)
            expected = sum(2 ** (-(day - observed_day) / 90) for observed_day in range(day))
            self.assertAlmostEqual(score.helpful, expected, places=12)
            self.assertEqual(score.harmful, 0)
            self.assertEqual(projection.rules[0].maturity, "established" if day >= 4 else "candidate")
            # Evidence-only baseline surfaces an original reference, not normative advice.
            baseline += bool(events[0].payload.evidence)
            delivered += bool(ids)
            abstentions += not ids
            violations += len(result.serialized) > 6000 or result.data["item_count"] > 5
            self.assertEqual(deliver(events, task_at)[1], result)
            self.assertEqual(events, before)
            # This task's attribution arrives AFTER its retrieval; no lookahead.
            outcome = parsed("outcome", 10 + day, at=task_at)
            events.append(outcome)
            events.append(outcome)  # Lost-ack physical retry contributes no credit.
        final = project_rules(events, as_of=NOW + timedelta(days=6), policy=Policy())
        self.assertEqual(final.duplicate_count, 6)
        self.assertEqual(final.rules[0].score.helpful_sessions, 6)
        self.assertEqual((delivered, baseline, abstentions, violations), (2, 6, 4, 0))

    def test_harm_misleading_correlation_and_scope_drift_abstain(self):
        useful = definition()
        misleading = definition(statement="Always reuse the broad successful workaround.")
        scoped = definition(statement="Use the other repository's convention.",
                            scope={"kind": "repository", "key": "owner/other"})
        events = [parsed(rule=useful), parsed("review", 2, rule=useful),
                  parsed("proposal", 100, rule=misleading),
                  parsed("review", 101, rule=misleading, verdict="hold"),
                  parsed("proposal", 200, rule=scoped)]
        # A task-level success has no rule-level observation: neutral only.
        events += [parsed("outcome", 110 + i, rule=misleading, outcome="neutral") for i in range(12)]
        events += [parsed("outcome", 10 + i, rule=useful) for i in range(4)]
        before_harm = NOW + timedelta(seconds=1)
        projection, result, ids = deliver(events, before_harm)
        self.assertEqual(ids, [events[0].rule_id])
        correlation = next(s for s in projection.rules if s.rule_id == events[2].rule_id)
        self.assertEqual((correlation.score.helpful, correlation.score.harmful), (0, 0))
        self.assertIn("unapproved", correlation.suppression_reasons)
        self.assertEqual(deliver(events, before_harm, repository="owner/repo-extra")[2], [])
        events.append(parsed("outcome", 50, outcome="harmful", at=before_harm))
        for at in (before_harm + timedelta(seconds=1), NOW + timedelta(days=900)):
            projection, result, ids = deliver(events, at, trials=True)
            self.assertEqual(ids, [])
            harmed = next(s for s in projection.rules if s.rule_id == events[0].rule_id)
            self.assertIn("unresolved_harm", harmed.suppression_reasons)
            self.assertLessEqual(len(result.serialized), 6000)
        # Harm never manufactures its textual opposite.
        self.assertEqual(len(projection.rules), 3)
        self.assertEqual({s.definition.statement for s in projection.rules},
                         {useful["statement"], misleading["statement"], scoped["statement"]})

    def test_concurrent_reviewers_require_join_and_stale_evidence_stays_withheld(self):
        events = [parsed(), parsed("review", 2),
                  *[parsed("outcome", 10 + i) for i in range(4)]]
        self.assertEqual(deliver(events, NOW + timedelta(seconds=1))[2], [events[0].rule_id])
        # Simulate external concurrent appends; package serialized writers would
        # reject the second stale-head artifact instead.
        events.append(parsed("review", 3))
        projection, _, ids = deliver(events, NOW + timedelta(seconds=2))
        self.assertEqual(ids, [])
        self.assertEqual(projection.rules[0].review_heads, (events[1].event_id, events[-1].event_id))
        self.assertIn("conflicted", projection.rules[0].suppression_reasons)
        join = parsed("review", 4, at=NOW + timedelta(seconds=3),
                      parent_review_ids=[events[1].event_id, events[-1].event_id])
        events.append(join)
        self.assertEqual(deliver(events, NOW + timedelta(seconds=4))[2], [events[0].rule_id])
        projection, _, ids = deliver(events, NOW + timedelta(days=90, seconds=3), trials=True)
        self.assertEqual(ids, [])
        self.assertIn("stale_validation", projection.rules[0].suppression_reasons)

    def test_reviewed_invalid_attribution_preserves_lineage_and_original_decay_anchors(self):
        old = NOW - timedelta(days=90)
        first = parsed("outcome", 3, at=old, source_session_id="same",
                       evidence=[evidence(session_id="same")])
        later = parsed("outcome", 4, source_session_id="same", evidence=[evidence(session_id="same")])
        harm = parsed("outcome", 5, outcome="harmful", source_session_id="same",
                      evidence=[evidence(session_id="same")])
        review = parsed("review", 6, acknowledged_evidence_ids=[harm.event_id], dispositions=[
            {"evidence_id": harm.event_id, "disposition": "invalid",
             "reason": "Original evidence attributes harm to an unrelated action.", "evidence": [evidence()]}])
        events = [parsed(at=old), first, later, harm, review]
        projection, _, ids = deliver(events, NOW, trials=True)
        self.assertEqual(ids, [events[0].rule_id])
        self.assertEqual(projection.rules[0].score.helpful, .5)
        self.assertEqual(projection.rules[0].score.harmful, 0)
        self.assertEqual(projection.rules[0].score.terms[0].event_id, first.event_id)
        self.assertIn(harm, projection.rules[0].events)


class GroundedReplayTests(GroundedFixture):
    def test_generated_mirrors_cannot_supply_support_and_drift_cannot_deliver(self):
        self.preflight(self.proposal())
        review = self.review()
        self.preflight(review, self.proposal())
        events = [self.proposal(), review]
        projection, diagnostics = revalidate_sources(self.projection(*events),
            evidence_reader=self.reader(), as_of=NOW)
        self.assertEqual(diagnostics, {})
        self.assertTrue(projection.rules[0].eligible)
        self.collection.rows["source-3"]["metadata"]["kind"] = "reflect"
        with self.assertRaisesRegex(ValueError, "generated"):
            self.preflight(self.proposal())
        projection, diagnostics = revalidate_sources(self.projection(*events),
            evidence_reader=self.reader(), as_of=NOW)
        self.assertIn("evidence_unavailable", projection.rules[0].suppression_reasons)
        with self.assertRaises(RuntimeError):
            get_task_guidance(projection, task="regression", repository="owner/repo",
                embedder=lambda texts: [[1., 0.] for t in texts], limits=GuidanceLimits(),
                as_of=NOW, include_candidates=True)

    def test_source_repository_drift_and_missing_lineage_fail_closed(self):
        import sqlite3
        events = [self.proposal(), self.review()]
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE sessions SET repository='owner/other'")
        projection, _ = revalidate_sources(self.projection(*events),
            evidence_reader=self.reader(), as_of=NOW)
        self.assertFalse(projection.rules[0].eligible)
        self.assertIn("evidence_unavailable", projection.rules[0].suppression_reasons)

    def test_helpful_causal_label_is_reviewed_not_proven_by_code(self):
        ref = self.refs[0]
        attribution = "Following the focused-test rule isolated this defect, according to the observer."
        outcome = parse_event(event_data("outcome", 3, source_session_id=ref["session_id"],
                                        evidence=[ref], attribution=attribution))
        self.preflight(outcome, self.proposal(), self.review())
        review = event_to_data(self.review(4, parent_review_ids=[self.review().event_id],
                                         acknowledged_evidence_ids=[outcome.event_id]))
        review["payload"]["dispositions"].append({"evidence_id": outcome.event_id,
            "disposition": "invalid", "reason": "On review this was correlation, not a rule-specific effect.",
            "evidence": [ref]})
        corrected = parse_event(resign(review))
        self.preflight(corrected, self.proposal(), self.review(), outcome)
        state = self.projection(self.proposal(), self.review(), outcome, corrected).rules[0]
        self.assertEqual(state.score.helpful, 0)
        self.assertIn(outcome, state.events)
        self.assertEqual(state.maturity, "candidate")
