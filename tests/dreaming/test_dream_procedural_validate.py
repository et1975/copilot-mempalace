"""Grounding uses real local session stores and original drawer text."""
from copy import deepcopy
from contextlib import redirect_stderr
from datetime import timedelta
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

import dream_palace
from dream_procedural import Policy, event_to_data, parse_event, project_rules
from test_dream_procedural import NOW, definition, event_data, evidence, sha, stamp
from test_dream_procedural_palace import DrawerCollection


class GroundedFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"])
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name, "palace"))
        Path(self.path).mkdir()
        self.store = str(Path(self.tmp.name, "sessions.db"))
        self.collection = DrawerCollection()
        self.refs = []
        with sqlite3.connect(self.store) as con:
            con.executescript("""
                CREATE TABLE sessions(id TEXT PRIMARY KEY, repository TEXT);
                CREATE TABLE turns(session_id TEXT, turn_index INTEGER, user_message TEXT,
                                   assistant_response TEXT, timestamp TEXT);
            """)
            for number in range(1, 5):
                session = str(UUID(int=100 + number))
                text = f"SESSION_ID: {session}\nOBSERVED_AT: {stamp()}\nFocused test exposed defect {number}."
                con.execute("INSERT INTO sessions VALUES (?,?)", (session, "owner/repo"))
                con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                            (session, 0, text, "The regression was isolated by following the rule.", stamp()))
                source = f"source-{number}"
                self.collection.rows[source] = {"id": source, "text": text,
                    "metadata": {"wing": "w", "room": "diary", "repository": "owner/repo"}}
                self.refs.append(evidence(source, session, text))
        self.storage_patch = patch.object(dream_palace, "procedural_collection", return_value=self.collection)
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        p = patch.dict(os.environ, {"COPILOT_SESSION_STORE": self.store})
        p.start()
        self.addCleanup(p.stop)

    def proposal(self, **kwargs):
        return parse_event(event_data(origin_drawer_ids=[], evidence=self.refs[:3], **kwargs))

    def projection(self, *events):
        return project_rules(events, as_of=NOW, policy=Policy())

    def reader(self):
        from dream_procedural_validate import AdmissionReader
        return AdmissionReader(self.path, "w", self.store)

    def published_reader(self):
        from dream_procedural_validate import EvidenceReader
        return EvidenceReader(self.path, "w")

    def publish(self, *events):
        from dream_procedural_palace import append_event
        from test_dream_procedural_palace import sanctioned_writer
        for event in events:
            append_event(self.path, "w", event, writer=sanctioned_writer(self.path, self.collection),
                         session_store=self.store, clock=lambda: NOW)

    def review(self, number=2, **changes):
        packet = {"rule_id": self.proposal().rule_id, "repository": "owner/repo",
                  "validated_at": stamp(), "queries": [definition()["statement"], "When does this fail?"],
                  "evidence": self.refs[:3]}
        dispositions = [{"evidence_id": r["source_id"], "disposition": "supports",
                         "reason": "Applicable original use.", "evidence": [r]} for r in self.refs[:3]]
        return parse_event(event_data("review", number, validation_packet=packet,
            validation_digest=sha(packet), **{"dispositions": dispositions, **changes}))

    def preflight(self, event, *prior, at=NOW):
        from dream_procedural_validate import preflight_event
        return preflight_event(event, projection=self.projection(*prior),
                               evidence_reader=self.reader(), as_of=at)


class GroundingTests(GroundedFixture):
    def test_transient_applicability_and_use_result_are_not_original_turn_or_drawer_support(self):
        from delivery_fixtures import applicability, case
        from dream_metadata import canonical_json, content_hash
        from dream_procedural import EvidenceReference
        from dream_procedural_validate import acquire_original
        _, current, _, guidance = case()
        session = self.refs[0]["session_id"]
        artifacts = [
            applicability(current["context"], guidance["rules"]),
            dict(kind="procedural_use_check", authority="agent_reported", status="usable",
                 items=guidance["rules"], checked_at=guidance["as_of"],
                 notice="Cooperative current check, not semantic proof or a durable authorization token; recheck before action."),
        ]
        for artifact in artifacts:
            copied = canonical_json(artifact)
            for field in ("user_message", "assistant_response", "drawer"):
                text = f"SESSION_ID: {session}\nOBSERVED_AT: {stamp()}\n```json\n{copied}\n```"
                if field == "drawer":
                    self.collection.rows["source-1"]["text"] = text
                    ref = EvidenceReference("drawer", "source-1", session, "parser", content_hash(text))
                else:
                    with sqlite3.connect(self.store) as con:
                        con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, session))
                    ref = EvidenceReference("session_turn", session, session, "parser",
                                            content_hash(text), 0, field)
                with self.assertRaisesRegex(ValueError, "generated"):
                    acquire_original(ref, palace=self.path, session_store=self.store)

    def test_complete_transport_rejected_in_both_original_turn_fields_and_drawer(self):
        from delivery_fixtures import wrapped_packet
        from dream_metadata import content_hash
        from dream_procedural import EvidenceReference
        from dream_procedural_validate import acquire_original
        session = self.refs[0]["session_id"]
        for wrapper in ("{}", "```json\n{}\n```", "Copied report:\n{}\nEnd."):
            for field in ("user_message", "assistant_response", "drawer"):
                text = f"SESSION_ID: {session}\nOBSERVED_AT: {stamp()}\n" + wrapped_packet(wrapper)
                if field == "drawer":
                    self.collection.rows["source-1"]["text"] = text
                    ref = EvidenceReference("drawer", "source-1", session,
                                            "Add a parser regression", content_hash(text))
                else:
                    with sqlite3.connect(self.store) as con:
                        con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, session))
                    ref = EvidenceReference("session_turn", session, session,
                        "Add a parser regression", content_hash(text), 0, field)
                with self.subTest(wrapper=wrapper, field=field), self.assertRaisesRegex(ValueError, "generated"):
                    acquire_original(ref, palace=self.path, session_store=self.store)

    def test_receipts_cannot_supply_direct_support_even_through_narrow_quotes(self):
        from dream_metadata import canonical_json, content_hash
        from dream_procedural import EvidenceReference
        from dream_procedural_validate import acquire_original
        from receipt_fixtures import receipts
        session = self.refs[0]["session_id"]
        for record in receipts()[:2]:
            for wrapper in ("{}", "```json\n{}\n```", "Copied report:\n{}\nEnd."):
                for encoded in (False, True):
                    body = canonical_json(record)
                    body = wrapper.format(canonical_json(body) if encoded else body)
                    text = f"SESSION_ID: {session}\nOBSERVED_AT: {stamp()}\n" + body
                    for field in ("user_message", "assistant_response", "drawer"):
                        if field == "drawer":
                            self.collection.rows["source-1"]["text"] = text
                            ref = EvidenceReference("drawer", "source-1", session, "parser",
                                                    content_hash(text))
                        else:
                            with sqlite3.connect(self.store) as con:
                                con.execute(f"UPDATE turns SET {field}=? WHERE session_id=?", (text, session))
                            ref = EvidenceReference("session_turn", session, session, "parser",
                                                    content_hash(text), 0, field)
                        with self.assertRaisesRegex(ValueError, "generated"):
                            acquire_original(ref, palace=self.path, session_store=self.store)

    def test_validation_finds_captured_raw_fields_once_without_host_or_generated_copies(self):
        from dream_procedural_validate import build_validation_packet, ValidationLimits
        from dream_procedural_sources import AUTHOR
        for ref in self.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        self.publish(self.proposal())
        record = next(v for v in self.collection.rows.values()
                      if v["metadata"]["room"] == "procedural-sources")
        self.collection.add("w", "procedural-sources", record["text"], AUTHOR, record["metadata"])
        self.collection.rows["reflection"] = {"id": "reflection", "text": self.refs[0]["quote"],
            "metadata": {"wing": "w", "room": "diary", "kind": "reflect"}}
        ids = [k for k, v in self.collection.rows.items()
               if v["metadata"]["room"] == "procedural-sources"] + ["reflection"]
        self.collection.query = lambda **kwargs: {
            "ids": [ids], "documents": [[self.collection.rows[i]["text"] for i in ids]],
            "metadatas": [[self.collection.rows[i]["metadata"] for i in ids]]}
        self.collection.embedding_function = lambda texts: [[1., 0.] for _ in texts]
        os.unlink(self.store)
        before = deepcopy(self.collection.rows)
        with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")):
            reader = self.reader()
            packet = build_validation_packet(self.proposal().payload.definition,
                queries=[definition()["statement"], "When does it fail?"],
                source_reader=lambda q, n: reader.search("w", q, n, as_of=NOW),
                limits=ValidationLimits(), as_of=NOW)
        self.assertEqual(len(packet.evidence), 3)
        self.assertEqual({ref.source_kind for ref in packet.evidence}, {"session_turn"})
        self.assertEqual({ref.source_id for ref in packet.evidence},
                         {ref["session_id"] for ref in self.refs[:3]})
        self.assertEqual(self.collection.rows, before)

    def test_packet_keeps_distinct_raw_fields_but_not_snapshot_copies(self):
        from dream_procedural import EvidenceReference
        from dream_procedural_validate import build_validation_packet, ValidationLimits
        refs = [
            EvidenceReference("session_turn", self.refs[0]["session_id"], self.refs[0]["session_id"],
                              "original", "a" * 64, index, field)
            for index, field in ((0, "user_message"), (0, "assistant_response"), (1, "user_message"))]
        packet = build_validation_packet(self.proposal().payload.definition,
            queries=[definition()["statement"], "When does it fail?"],
            source_reader=lambda q, n: refs + refs, limits=ValidationLimits(), as_of=NOW)
        self.assertEqual(len(packet.evidence), 3)

    def test_original_acquisition_rejects_cross_session_turn_identity(self):
        from dataclasses import replace
        from dream_procedural_validate import acquire_original
        ref = replace(self.proposal().payload.evidence[0], source_kind="session_turn",
                      source_id=self.refs[1]["session_id"], turn_index=0, field="user_message",
                      source_hash=self.refs[1]["source_hash"], quote=self.refs[1]["quote"])
        with self.assertRaisesRegex(ValueError, "session"):
            acquire_original(ref, palace=self.path, session_store=self.store)

    def test_three_original_sessions_required_not_three_references(self):
        self.assertEqual(self.preflight(self.proposal()).independent_sessions, 3)
        for refs in (self.refs[:2], [self.refs[0]] * 3):
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, "three"):
                self.preflight(parse_event(event_data(origin_drawer_ids=[], evidence=refs)))

    def test_source_hash_quote_session_scope_and_timestamp_are_authoritative(self):
        for change in ({"source_hash": "0" * 64}, {"quote": "fabricated"},
                       {"session_id": str(UUID(int=555))}, {"source_id": "missing"}):
            refs = deepcopy(self.refs[:3])
            refs[0].update(change)
            with self.subTest(change=change), self.assertRaises((ValueError, RuntimeError)):
                self.preflight(parse_event(event_data(origin_drawer_ids=[], evidence=refs)))
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE sessions SET repository='owner/repo-other' WHERE id=?",
                        (self.refs[0]["session_id"],))
        with self.assertRaisesRegex(ValueError, "repository"):
            self.preflight(self.proposal())

    def test_generated_diary_cannot_be_replication_and_missing_store_is_failure(self):
        self.collection.rows["source-1"]["metadata"]["kind"] = "reflect"
        with self.assertRaisesRegex(ValueError, "generated"):
            self.preflight(self.proposal())
        os.unlink(self.store)
        with self.assertRaises(RuntimeError):
            self.reader().resolve(self.proposal().payload.evidence[1])
        self.assertFalse(Path(self.store).exists())

    def test_raw_turn_and_diary_same_session_do_not_double_count(self):
        ref = dict(self.refs[0], source_kind="session_turn", source_id=self.refs[0]["session_id"],
                   turn_index=0, field="user_message")
        with self.assertRaisesRegex(ValueError, "three"):
            self.preflight(parse_event(event_data(origin_drawer_ids=[],
                                                  evidence=[self.refs[0], ref, self.refs[1]])))

    def test_review_requires_all_packet_dispositions_three_supports_and_fresh_packet(self):
        self.assertEqual(self.preflight(self.review(), self.proposal()).independent_sessions, 3)
        for changes in ({"dispositions": []},
                        {"dispositions": event_to_data(self.review())["payload"]["dispositions"][:2]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "disposition"):
                self.preflight(self.review(**changes), self.proposal())
        with self.assertRaisesRegex(ValueError, "stale"):
            self.preflight(self.review(), self.proposal(), at=NOW + timedelta(days=90))

    def test_outcome_requires_original_timestamp_scope_and_rule_specific_attribution(self):
        ref = self.refs[0]
        event = parse_event(event_data("outcome", 3, source_session_id=ref["session_id"], evidence=[ref]))
        self.preflight(event, self.proposal(), self.review())
        for fields in ({"observed_at": stamp(NOW - timedelta(seconds=1))},
                       {"repository": "owner/other"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.preflight(parse_event(event_data("outcome", 3, source_session_id=ref["session_id"],
                                                      evidence=[ref], **fields)), self.proposal(), self.review())
        # No semantic keywords are interpreted: an unsupported outcome enum is invalid.
        with self.assertRaises(ValueError):
            parse_event(event_data("outcome", 3, outcome="task_success"))

    def test_review_rechecks_prior_proposal_and_current_heads(self):
        review = self.review()
        with self.assertRaisesRegex(ValueError, "head"):
            self.preflight(self.review(3), self.proposal(), review)
        self.collection.rows["source-1"]["text"] = "changed"
        with self.assertRaises((ValueError, RuntimeError)):
            self.preflight(review, self.proposal())

    def test_approval_must_resolve_counterevidence_from_prior_hold(self):
        adverse_ref = self.refs[3]
        held = self.review(verdict="hold", dispositions=[
            *event_to_data(self.review())["payload"]["dispositions"],
            {"evidence_id": adverse_ref["source_id"], "disposition": "contradicts",
             "reason": "A contrary outcome in this context.", "evidence": [adverse_ref]}])
        self.preflight(held, self.proposal())
        approval = self.review(3, parent_review_ids=[held.event_id])
        with self.assertRaisesRegex(ValueError, "adverse"):
            self.preflight(approval, self.proposal(), held)
        dispositions = event_to_data(approval)["payload"]["dispositions"] + [{
            "evidence_id": adverse_ref["source_id"], "disposition": "not_applicable",
            "reason": "Original counterexample falls under the documented exception.",
            "evidence": [adverse_ref]}]
        resolved = self.review(4, parent_review_ids=[held.event_id],
            dispositions=dispositions, acknowledged_evidence_ids=[adverse_ref["source_id"]])
        self.preflight(resolved, self.proposal(), held)

    def test_validation_packet_fits_a_storable_review_with_dispositions(self):
        from dream_procedural import EvidenceReference, to_data
        from dream_procedural_palace import record_data
        from dream_procedural_validate import build_validation_packet, ValidationLimits
        for character in ("x", "\U0001f50e"):
            refs = [EvidenceReference("drawer", f"large-source-{i}", f"session-{i}",
                                      character * 800, "a" * 64) for i in range(20)]
            queries = [definition()["statement"], "When does this fail?"]
            errors = io.StringIO()
            with redirect_stderr(errors):
                packet = build_validation_packet(self.proposal().payload.definition,
                    queries=queries, source_reader=lambda q, n: refs[:10] if q == queries[0] else refs[10:],
                    limits=ValidationLimits(), as_of=NOW)
            packet_data = to_data(packet)
            dispositions = [{"evidence_id": ref["source_id"], "disposition": "supports",
                             "reason": "Reviewed applicability.", "evidence": [ref]}
                            for ref in packet_data["evidence"]]
            review = parse_event(event_data("review", 2, validation_packet=packet_data,
                validation_digest=sha(packet_data), dispositions=dispositions))
            record_data(review)
            self.assertTrue(packet.evidence)
            self.assertTrue(any(ref.source_id == "large-source-10" for ref in packet.evidence))
            self.assertTrue(all(ref.quote and ref.quote in character * 800 for ref in packet.evidence))
            self.assertRegex(errors.getvalue(), "budget|shorten")

    def test_support_and_contrast_execute_top_ten_and_deduplicate_sources(self):
        from dream_procedural_validate import build_validation_packet, ValidationLimits
        calls = []
        def search(query, limit):
            calls.append((query, limit))
            return list(self.proposal().payload.evidence)
        packet = build_validation_packet(self.proposal().payload.definition,
            queries=[definition()["statement"], "When does this fail?"], source_reader=search,
            limits=ValidationLimits(), as_of=NOW)
        self.assertEqual(calls, [(definition()["statement"], 10), ("When does this fail?", 10)])
        self.assertEqual(len(packet.evidence), 3)
        with self.assertRaisesRegex(RuntimeError, "evidence"):
            build_validation_packet(self.proposal().payload.definition,
                queries=packet.queries, source_reader=lambda *a: [], limits=ValidationLimits(), as_of=NOW)
        with self.assertRaises(ValueError):
            build_validation_packet(self.proposal().payload.definition, queries=["only one"],
                source_reader=search, limits=ValidationLimits(), as_of=NOW)

    def test_source_turn_timestamp_is_not_filing_time(self):
        ref = dict(self.refs[0], source_kind="session_turn", source_id=self.refs[0]["session_id"],
                   turn_index=0, field="user_message")
        outcome = parse_event(event_data("outcome", 3, evidence=[ref],
                                        source_session_id=ref["session_id"]))
        self.preflight(outcome, self.proposal(), self.review())
        with sqlite3.connect(self.store) as con:
            con.execute("UPDATE turns SET timestamp=? WHERE session_id=?",
                        (stamp(NOW - timedelta(days=1)), ref["session_id"]))
        with self.assertRaisesRegex(ValueError, "timestamp"):
            self.preflight(outcome, self.proposal(), self.review())
