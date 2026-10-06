"""Task-bound advice is read-only, freshly checked and never host authority."""
from copy import deepcopy
from datetime import timedelta
import importlib
import json
from uuid import UUID

import pytest

from delivery_fixtures import RID, applicability, case, digest
from dream_metadata import canonical_json
from test_dream_procedural import NOW, stamp
from test_dream_procedural_validate import GroundedFixture


def delivery():
    return importlib.import_module("dream_procedural_delivery")


def use(packet, current, permission, guidance, **kwargs):
    return delivery().preflight_use(
        packet, current, permission, [RID], lambda context, now: guidance, NOW,
        applicability=applicability(current["context"], guidance["rules"]), **kwargs)


def test_complete_packet_roundtrip_and_fresh_text():
    packet, current, permission, guidance = case()
    assert delivery().build_packet(current, permission, packet["request_id"],
                                   lambda context, now: guidance, NOW) == packet
    assert use(packet, current, permission, guidance) == guidance["rules"]


@pytest.mark.parametrize("change", [
    {"revision": 1}, {"actor_id": None}, {"actor_id": str(UUID(int=1001))},
    {"constraints": ["Change spans"]}, {"task_id": str(UUID(int=1002))},
    {"session_id": str(UUID(int=1003))}, {"wing": "other"}, {"repository": "owner/other"},
    {"task": "An unrelated action"}, {"mode": "approved_candidate_trial"},
])
def test_current_context_is_independent_not_packet_self_consistency(change):
    packet, current, permission, guidance = case()
    current["context"].update(change)
    with pytest.raises(ValueError):
        use(packet, current, permission, guidance)


def test_constraint_reordering_invalidates_exact_context_identity():
    packet, current, permission, guidance = case()
    packet["context"]["constraints"] = ["Preserve spans", "Keep the public API"]
    packet["context_digest"] = digest(packet["context"])
    current["context"]["constraints"] = list(reversed(packet["context"]["constraints"]))
    with pytest.raises(ValueError, match="context changed"):
        use(packet, current, permission, guidance)


@pytest.mark.parametrize("reason", ["harm", "retired", "review_conflict", "stale"])
def test_unchanged_context_always_refreshes_policy(reason):
    packet, current, permission, guidance = case()
    called = []

    def refresh(context, now):
        called.append(reason)
        return dict(guidance, status="no_eligible_rules", item_count=0, rules=[])

    with pytest.raises(delivery().AdviceWithheld, match="currently"):
        delivery().preflight_use(packet, current, permission, [RID], refresh, NOW,
            applicability=applicability(current["context"], guidance["rules"]))
    assert called == [reason]


def test_source_and_model_failures_remain_errors_not_empty_advice():
    packet, current, permission, guidance = case()
    for reason in ("source drift", "model unavailable"):
        def refresh(context, now):
            raise RuntimeError(reason)
        with pytest.raises(RuntimeError, match=reason):
            delivery().preflight_use(packet, current, permission, [RID], refresh, NOW)


@pytest.mark.parametrize("field", ["current", "permission", "applicability"])
@pytest.mark.parametrize("seconds", [-1, 61])
def test_witnesses_cannot_be_future_or_expired(field, seconds):
    packet, current, permission, guidance = case()
    fit = applicability(current["context"], guidance["rules"])
    {"current": current, "permission": permission, "applicability": fit}[field][
        "checked_at"] = stamp(NOW - timedelta(seconds=seconds))
    with pytest.raises(ValueError):
        delivery().preflight_use(packet, current, permission, [RID],
                                lambda context, now: guidance, NOW, applicability=fit)


def test_freshness_is_rechecked_after_slow_refresh():
    packet, current, permission, guidance = case()
    with pytest.raises(ValueError, match="expired"):
        delivery().preflight_use(packet, current, permission, [RID],
            lambda context, now: guidance, NOW,
            applicability=applicability(current["context"], guidance["rules"]),
            clock=lambda: NOW + timedelta(seconds=61))
    with pytest.raises(ValueError, match="expired"):
        delivery().build_packet(current, permission, packet["request_id"],
            lambda context, now: guidance, NOW, clock=lambda: NOW + timedelta(seconds=61))


def test_separate_advice_receipt_trial_consent():
    packet, current, permission, guidance = case()
    assert use(packet, current, permission, guidance)
    with pytest.raises(ValueError, match="permission"):
        delivery().consent(current["context"], permission, NOW, receipt=True)
    allowed = dict(permission, receipts="allow", receipts_ref="user:3")
    delivery().consent(current["context"], allowed, NOW, receipt=True)
    for field in ("advice", "receipts"):
        with pytest.raises(ValueError, match="permission"):
            delivery().consent(current["context"], dict(allowed, **{field: "deny"}), NOW, receipt=True)
    current["context"]["mode"] = "approved_candidate_trial"
    with pytest.raises(ValueError, match="trial"):
        delivery().consent(current["context"], permission, NOW)


@pytest.mark.parametrize("field,value", [
    ("revision", True), ("revision", -1), ("task_id", "tsk_durable"),
    ("actor_id", ""), ("session_id", "unknown"), ("repository", "Owner/Repo"),
    ("wing", " w "), ("task", ""), ("task", "界" * 5462),
    ("constraints", ["a"] * 9), ("constraints", ["a" * 513]),
    ("mode", "universal"), ("extra", "ignored"),
])
def test_context_schema_is_exact_without_silent_normalization(field, value):
    packet, current, permission, guidance = case()
    current["context"][field] = value
    with pytest.raises(ValueError):
        delivery().validate_current(current, NOW)


@pytest.mark.parametrize("field,value", [
    ("advice", True), ("advice", "yes"), ("advice_ref", None),
    ("receipts", "allow"), ("checked_at", "2026-09-23"),
    ("checked_at", "2026-09-23T01:00:00+01:00"), ("extra", None),
])
def test_permission_schema_and_utc_are_strict(field, value):
    _, current, permission, _ = case()
    permission[field] = value
    with pytest.raises(ValueError):
        delivery().consent(current["context"], permission, NOW)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema_version=True), lambda p: p.update(extra=1),
    lambda p: p.update(context_digest="0" * 64), lambda p: p.update(request_id="not-uuid"),
    lambda p: p["guidance"].update(extra="authority"),
    lambda p: p["guidance"]["rules"][0].update(effective_score=float("nan")),
    lambda p: p["guidance"]["rules"][0].update(relevance=True),
    lambda p: p["guidance"]["rules"][0].update(rule_id="bad"),
    lambda p: p["guidance"]["rules"][0].update(evidence=[{"source_kind": "generated", "source_id": "x"}]),
    lambda p: p["guidance"]["rules"][0].update(latest_validation="2026-09-24T00:00:00Z"),
    lambda p: p["guidance"].update(as_of="2026-09-24T00:00:00Z"),
])
def test_malformed_packets_never_authorize(mutate):
    packet, _, _, _ = case()
    mutate(packet)
    with pytest.raises(ValueError):
        delivery().validate_packet(packet, NOW)


def test_no_keyword_or_embedding_oracle_for_scope_fit():
    packet, current, permission, guidance = case()
    fit = applicability(current["context"], guidance["rules"])
    for key, verdict in (("condition", "unknown"), ("supported_context", "incompatible")):
        bad = deepcopy(fit)
        bad["assessments"][0][key]["verdict"] = verdict
        with pytest.raises(delivery().AdviceWithheld, match="applicability"):
            delivery().preflight_use(packet, current, permission, [RID],
                                    lambda context, now: guidance, NOW, applicability=bad)
    for key, verdict in (("exceptions", "present"), ("constraints", "unknown")):
        bad = deepcopy(fit)
        bad["assessments"][0][key][0]["verdict"] = verdict
        with pytest.raises(delivery().AdviceWithheld, match="applicability"):
            delivery().preflight_use(packet, current, permission, [RID],
                                    lambda context, now: guidance, NOW, applicability=bad)
    with pytest.raises(delivery().AdviceWithheld, match="applicability"):
        delivery().preflight_use(packet, current, permission, [RID],
                                lambda context, now: guidance, NOW)


def test_complete_conditions_and_constraints_are_required_not_compact_projections():
    packet, current, permission, guidance = case()
    fit = applicability(current["context"], guidance["rules"])
    for key in ("exceptions", "constraints"):
        bad = deepcopy(fit)
        bad["assessments"][0][key] = []
        with pytest.raises(ValueError):
            delivery().preflight_use(packet, current, permission, [RID],
                                    lambda context, now: guidance, NOW, applicability=bad)
    newer = deepcopy(guidance)
    newer["rules"][0]["exceptions"].append("Architecture has changed")
    with pytest.raises(ValueError, match="applicability"):
        delivery().preflight_use(packet, current, permission, [RID],
                                lambda context, now: newer, NOW, applicability=fit)


def test_fresh_scores_not_cached_items_and_sixty_second_boundary():
    packet, current, permission, guidance = case()
    newer = deepcopy(guidance)
    newer["rules"][0]["effective_score"] = 3.9
    assert use(packet, current, permission, newer) == newer["rules"]
    delivery().validate_current(current, NOW + timedelta(seconds=60))


@pytest.mark.parametrize("task", ["a" * 16384, "界" * (16384 // 3), "\x01" * 2600])
def test_actual_encoded_context_and_packet_bounds(task):
    packet, current, permission, guidance = case(task)
    context = current["context"]
    context["constraints"] = ["x" * 512] * 7 + [""]
    remaining = 20480 - len(canonical_json(context).encode("utf-8"))
    if 0 <= remaining <= 512:
        context["constraints"][-1] = "x" * remaining
        assert len(canonical_json(context).encode("utf-8")) == 20480
    result = delivery().build_packet(current, permission, packet["request_id"],
                                    lambda context, now: guidance, NOW)
    assert len((canonical_json(result) + "\n").encode("utf-8")) <= 32768
    assert result["context"]["task"] == task
    if 0 <= remaining < 512:
        context["constraints"][-1] += "x"
        with pytest.raises(ValueError, match="context"):
            delivery().validate_current(current, NOW)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_input_json_rejects_duplicate_and_nonfinite(tmp_path, raw):
    path = tmp_path / "input.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        delivery().read_json(path, 32768)


@pytest.mark.parametrize("bad", [
    {"item_count": True}, {"omitted_count": False}, {"policy_version": "future-policy"},
    {"rules": []}, {"trials": "not-list"},
])
def test_strict_guidance_independently_checks_shape_count_and_policy(bad):
    from dream_procedural_guidance import validate_guidance
    _, _, _, guidance = case()
    guidance.update(bad)
    with pytest.raises(ValueError):
        validate_guidance((canonical_json(guidance) + "\n").encode(), "owner/repo", as_of=NOW)


@pytest.mark.parametrize("section", ["rules", "anti_patterns", "trials"])
def test_rule_ids_cannot_repeat_within_or_across_guidance_sections(section):
    from dream_procedural_guidance import validate_guidance
    _, _, _, guidance = case()
    duplicate = deepcopy(guidance["rules"][0])
    if section == "anti_patterns":
        duplicate["rule_type"] = "anti_pattern"
    elif section == "trials":
        duplicate.update(maturity="candidate", delivery="approved_candidate_trial")
    guidance[section].append(duplicate)
    guidance["item_count"] = 2
    with pytest.raises(ValueError, match="duplicate"):
        validate_guidance((canonical_json(guidance) + "\n").encode(), "owner/repo",
                          mode="approved_candidate_trial", as_of=NOW)


@pytest.mark.parametrize("location", ["packet", "fresh"])
def test_established_mode_rejects_trials_in_both_old_and_fresh_guidance(location):
    packet, current, permission, guidance = case()
    trial_guidance = deepcopy(guidance)
    trial_guidance["trials"] = trial_guidance.pop("rules")
    trial_guidance["rules"] = []
    trial_guidance["trials"][0].update(maturity="candidate", delivery="approved_candidate_trial")
    if location == "packet":
        packet["guidance"] = trial_guidance
        packet["guidance_digest"] = digest(trial_guidance)
    else:
        guidance = trial_guidance
    with pytest.raises(ValueError, match="candidate trials"):
        delivery().preflight_use(packet, current, permission, [RID],
                                lambda context, now: guidance, NOW)


def test_anti_pattern_keeps_polarity_and_full_exceptions_after_refresh():
    packet, current, permission, guidance = case()
    item = guidance["rules"].pop()
    item.update(rule_type="anti_pattern", statement="Do not replace parser spans")
    guidance["anti_patterns"] = [item]
    packet["guidance_digest"] = digest(guidance)
    result = delivery().preflight_use(packet, current, permission, [RID],
        lambda context, now: guidance, NOW,
        applicability=applicability(current["context"], [item]))
    assert result == [item]
    assert result[0]["rule_type"] == "anti_pattern"
    assert result[0]["exceptions"] == ["No local reproduction available"]


@pytest.mark.parametrize("location", ["packet", "current", "permission", "guidance"])
def test_nonschema_containers_rejected_before_item_indexing(location):
    packet, current, permission, guidance = case()
    if location == "packet":
        packet = []
    elif location == "current":
        current = {"checked_at": stamp()}
    elif location == "permission":
        permission = []
    else:
        guidance = []
    with pytest.raises(ValueError, match="fields"):
        delivery().preflight_use(packet, current, permission, [RID],
                                lambda context, now: guidance, NOW)


@pytest.mark.parametrize("task", ["a" * 16384, "界" * (16384 // 3)])
def test_maximal_context_full_five_item_packet_is_lossless(task):
    packet, current, permission, guidance = case(task)
    context = current["context"]
    context["constraints"] = ["x" * 512] * 7 + [""]
    remaining = 20480 - len(canonical_json(context).encode("utf-8"))
    assert 0 < remaining < 512
    context["constraints"][-1] = "x" * remaining
    exemplar = guidance["rules"][0]
    guidance["rules"] = [
        dict(exemplar, rule_id="proc:" + f"{index:064x}", statement="s" * 500,
             applies_when="a" * 120, exceptions=["e" * 50]) for index in range(5)]
    guidance["item_count"] = 5
    result = delivery().build_packet(current, permission, packet["request_id"],
                                    lambda context, now: guidance, NOW)
    assert len(canonical_json(context).encode("utf-8")) == 20480
    assert len((canonical_json(guidance) + "\n").encode()) <= 8192
    assert len(canonical_json(guidance) + "\n") <= 6000
    assert result["guidance"]["rules"] == guidance["rules"]
    assert result["context"] == context


class DeliveryCommandTests(GroundedFixture):
    def invoke_delivery(self, command, *extra, clock=NOW):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        from dream_procedure import main
        import dream_palace
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
             patch("dream_procedure.now_utc", side_effect=clock if callable(clock) else lambda: clock), \
             patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("read-only writer")):
            code = main([command, "--palace", self.path, "--wing", "w", *extra])
        return code, json.loads(out.getvalue()), err.getvalue()

    def inputs(self, mode="established"):
        from pathlib import Path
        packet, current, permission, _ = case()
        current["context"]["mode"] = mode
        if mode == "approved_candidate_trial":
            permission["trials"] = "allow"
        root = Path(self.tmp.name)
        (root / "current.json").write_text(canonical_json(current))
        (root / "permissions.json").write_text(canonical_json(permission))
        return root, ["--current", str(root / "current.json"),
                      "--permissions", str(root / "permissions.json")], packet["request_id"]

    def establish(self):
        from dream_procedural import parse_event
        from test_dream_procedural import event_data
        self.publish(self.proposal(), self.review())
        for number, ref in enumerate(self.refs[:3], 10):
            self.publish(parse_event(event_data("outcome", number,
                source_session_id=ref["session_id"], evidence=[ref])))
        self.collection.embedding_function = lambda texts: [[1., 0.] for _ in texts]

    def offer(self, mode="established"):
        root, args, request = self.inputs(mode)
        code, packet, err = self.invoke_delivery("task-guidance", *args,
            "--request-id", request, "--out", str(root / "packet.json"))
        self.assertEqual((code, packet["status"]), (0, "bound_guidance"), err)
        self.assertEqual(json.loads((root / "packet.json").read_text()), packet)
        return root, args, packet

    def test_cli_generation_use_is_nonmutating_and_legacy_guidance_unchanged(self):
        self.establish()
        before = deepcopy(self.collection.rows)
        root, args, packet = self.offer()
        items = packet["guidance"]["rules"]
        fit = applicability(packet["context"], items)
        (root / "fit.json").write_text(canonical_json(fit))
        code, result, err = self.invoke_delivery("use-check", *args,
            "--packet", str(root / "packet.json"), "--rule-id", items[0]["rule_id"],
            "--applicability", str(root / "fit.json"))
        self.assertEqual((code, result["status"]), (0, "usable"), err)
        self.assertEqual(result["items"], items)
        self.assertEqual((result["kind"], result["authority"]),
                         ("procedural_use_check", "agent_reported"))
        from dream_metadata import reject_generated_transport
        with self.assertRaisesRegex(ValueError, "generated"):
            reject_generated_transport(canonical_json(result))
        self.assertEqual(self.collection.rows, before)
        code, legacy, err = self.invoke_delivery("guidance", "--repository", "owner/repo",
            "--task", canonical_json({"task": packet["context"]["task"],
                                     "constraints": packet["context"]["constraints"]}),
            "--max-bytes", "8192")
        self.assertEqual(code, 0, err)
        self.assertEqual(legacy, packet["guidance"])

    def test_cli_missing_fit_is_healthy_withheld_not_authorization(self):
        self.establish()
        root, args, packet = self.offer()
        code, result, err = self.invoke_delivery("use-check", *args,
            "--packet", str(root / "packet.json"), "--rule-id", packet["guidance"]["rules"][0]["rule_id"])
        self.assertEqual((code, result["status"], result["items"]), (0, "withheld", []), err)

    def test_actual_harm_retirement_conflict_and_staleness_invalidate_unchanged_task(self):
        from dream_procedural import parse_event
        from dream_procedural_palace import _record_body
        from dream_procedural import event_to_data
        from test_dream_procedural import event_data
        self.establish()
        root, args, packet = self.offer()
        baseline = deepcopy(self.collection.rows)
        rule_id = packet["guidance"]["rules"][0]["rule_id"]
        (root / "fit.json").write_text(canonical_json(applicability(packet["context"], packet["guidance"]["rules"])))
        mutations = [
            parse_event(event_data("outcome", 30, outcome="harmful",
                source_session_id=self.refs[3]["session_id"], evidence=[self.refs[3]])),
            self.review(31, verdict="retire", parent_review_ids=[self.review().event_id]),
            self.review(32, verdict="hold"),
        ]
        for event in mutations:
            self.collection.rows = deepcopy(baseline)
            if event.event_kind == "outcome" or event.payload.verdict == "retire":
                self.publish(event)
            else:
                # Conflicting review heads cannot enter through current append admission.
                self.collection.add("w", "procedural", _record_body(event), "dream-procedure",
                    {"kind": "procedural_event", "schema_version": 1, "event": event_to_data(event)})
            code, result, err = self.invoke_delivery("use-check", *args,
                "--packet", str(root / "packet.json"), "--rule-id", rule_id,
                "--applicability", str(root / "fit.json"))
            self.assertEqual((code, result["status"]), (0, "withheld"), err)
        self.collection.rows = deepcopy(baseline)
        later = NOW + timedelta(days=100)
        current = json.loads((root / "current.json").read_text())
        permission = json.loads((root / "permissions.json").read_text())
        current["checked_at"] = permission["checked_at"] = stamp(later)
        (root / "current.json").write_text(canonical_json(current))
        (root / "permissions.json").write_text(canonical_json(permission))
        code, result, err = self.invoke_delivery("use-check", *args, "--packet", str(root / "packet.json"),
                                                "--rule-id", rule_id, clock=later)
        self.assertEqual((code, result["status"]), (0, "withheld"), err)

    def test_actual_source_drift_is_nonzero_and_new_reader_does_not_reuse_capture_cache(self):
        self.establish()
        root, args, packet = self.offer()
        self.collection.rows["source-1"]["text"] += " external drift"
        code, result, err = self.invoke_delivery("use-check", *args, "--packet", str(root / "packet.json"),
                                                "--rule-id", packet["guidance"]["rules"][0]["rule_id"])
        self.assertEqual((code, result["kind"]), (1, "evidence_unavailable"), err)

    def test_candidates_require_both_explicit_mode_and_trial_consent(self):
        self.publish(self.proposal(), self.review())
        self.collection.embedding_function = lambda texts: [[1., 0.] for _ in texts]
        root, args, packet = self.offer()
        self.assertEqual(packet["guidance"]["item_count"], 0)
        (root / "packet.json").unlink()
        root, args, packet = self.offer("approved_candidate_trial")
        self.assertEqual(len(packet["guidance"]["trials"]), 1)
        (root / "packet.json").unlink()
        permission = json.loads((root / "permissions.json").read_text())
        permission["trials"] = "deny"
        (root / "permissions.json").write_text(canonical_json(permission))
        code, result, err = self.invoke_delivery("task-guidance", *args,
            "--request-id", packet["request_id"], "--out", str(root / "packet.json"))
        self.assertNotEqual(code, 0, err)
        self.assertFalse((root / "packet.json").exists())


class InstalledDeliveryTests(GroundedFixture):
    invoke_delivery = DeliveryCommandTests.invoke_delivery
    inputs = DeliveryCommandTests.inputs

    def test_real_handlers_offline_minilm_live_wal_host_loss_and_nonmutation(self):
        from pathlib import Path
        from unittest.mock import patch
        import os
        import dream_palace
        from dream_procedural import parse_event
        from dream_procedural_palace import append_event
        from test_dream_procedural import event_data
        from test_dream_procedural_palace import installed_palace
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                for ref in self.refs[:3]:
                    ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"],
                                                        added_by="original-agent")["drawer_id"]
                events = [self.proposal(), self.review(), *[
                    parse_event(event_data("outcome", number,
                        source_session_id=ref["session_id"], evidence=[ref]))
                    for number, ref in enumerate(self.refs[:3], 10)]]
                for event in events:
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
                os.unlink(self.store)
                col = server._get_collection()
                col._handle.conn.execute("PRAGMA wal_autocheckpoint=0")
                before_state = tuple(col._handle.conn.iterdump())
                before_files = {p.name: p.read_bytes() for p in Path(self.path).iterdir()
                                if p.is_file() and not p.name.endswith("-shm")}
                root, args, request = self.inputs()
                current = json.loads((root / "current.json").read_text())
                current["context"]["task"] = "Fix a reproducible defect using a focused regression test."
                (root / "current.json").write_text(canonical_json(current))
                with patch("dream_procedural_validate._session_repository", side_effect=AssertionError("host")), \
                     patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")):
                    code, packet, err = self.invoke_delivery("task-guidance", *args,
                        "--request-id", request, "--out", str(root / "packet.json"))
                    self.assertEqual((code, packet["status"]), (0, "bound_guidance"), err)
                    self.assertEqual(packet["guidance"]["item_count"], 1)
                    items = packet["guidance"]["rules"]
                    (root / "fit.json").write_text(canonical_json(applicability(packet["context"], items)))
                    code, result, err = self.invoke_delivery("use-check", *args,
                        "--packet", str(root / "packet.json"), "--rule-id", items[0]["rule_id"],
                        "--applicability", str(root / "fit.json"))
                    self.assertEqual((code, result["status"]), (0, "usable"), err)
                    self.assertEqual(result["items"], items)
                self.assertEqual(tuple(col._handle.conn.iterdump()), before_state)
                self.assertEqual({p.name: p.read_bytes() for p in Path(self.path).iterdir()
                                  if p.is_file() and not p.name.endswith("-shm")}, before_files)

class AdditionalDeliveryCommandTests(GroundedFixture):
    invoke_delivery = DeliveryCommandTests.invoke_delivery
    inputs = DeliveryCommandTests.inputs
    establish = DeliveryCommandTests.establish
    offer = DeliveryCommandTests.offer

    def test_permission_reread_latency_cannot_extend_witness_freshness(self):
        from unittest.mock import patch
        self.establish()
        root, args, request = self.inputs()
        clock = [NOW]
        reader = delivery().read_json
        permission_reads = []

        def delayed_read(path, limit):
            value = reader(path, limit)
            if str(path) == str(root / "permissions.json"):
                permission_reads.append(path)
                if len(permission_reads) == 2:
                    clock[0] += timedelta(seconds=61)
            return value

        with patch("dream_procedural_delivery.read_json", side_effect=delayed_read):
            code, result, err = self.invoke_delivery("task-guidance", *args,
                "--request-id", request, "--out", str(root / "packet.json"), clock=lambda: clock[0])
        self.assertNotEqual(code, 0, err)
        self.assertIn("expired", result["error"])
        self.assertFalse((root / "packet.json").exists())

    def test_foreign_proven_rule_cannot_be_relabelled_into_target_guidance(self):
        import sqlite3
        from dream_procedural import parse_event
        from test_dream_procedural import event_data, evidence
        self.establish()
        for number in range(4, 11):
            session = str(UUID(int=2000 + number))
            text = f"Independent original regression caught defect {number}."
            with sqlite3.connect(self.store) as con:
                con.execute("INSERT INTO sessions VALUES (?,?)", (session, "owner/repo"))
                con.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (session, 0, text, "", stamp()))
            ref = dict(evidence(session, session, text), source_kind="session_turn",
                       turn_index=0, field="user_message")
            self.publish(parse_event(event_data("outcome", 100 + number,
                source_session_id=session, evidence=[ref])))
        root, args, packet = self.offer()
        self.assertEqual(packet["guidance"]["rules"][0]["maturity"], "proven")
        current = json.loads((root / "current.json").read_text())
        permission = json.loads((root / "permissions.json").read_text())
        current["context"]["repository"] = permission["repository"] = "owner/target"
        (root / "current.json").write_text(canonical_json(current))
        (root / "permissions.json").write_text(canonical_json(permission))
        (root / "packet.json").unlink()
        code, other, err = self.invoke_delivery("task-guidance", *args,
            "--request-id", packet["request_id"], "--out", str(root / "packet.json"))
        self.assertEqual(code, 0, err)
        self.assertEqual(other["guidance"]["item_count"], 0)
        packet["context"] = current["context"]
        packet["context_digest"] = digest(packet["context"])
        packet["guidance"]["repository"] = "owner/target"
        packet["guidance_digest"] = digest(packet["guidance"])
        (root / "packet.json").write_text(canonical_json(packet))
        code, result, err = self.invoke_delivery("use-check", *args,
            "--packet", str(root / "packet.json"), "--rule-id", packet["guidance"]["rules"][0]["rule_id"])
        self.assertEqual((code, result["status"]), (0, "withheld"), err)

    def test_output_is_exclusive_outside_palace_and_permission_refresh_precedes_emission(self):
        from pathlib import Path
        from unittest.mock import patch
        self.establish()
        root, args, request = self.inputs()
        for path in (Path(self.path, "packet.json"), root / "current.json"):
            code, result, err = self.invoke_delivery("task-guidance", *args,
                "--request-id", request, "--out", str(path))
            self.assertNotEqual(code, 0, err)
        refresh = delivery().refresh_guidance

        def revoke(palace, context, now):
            result = refresh(palace, context, now)
            permission = json.loads((root / "permissions.json").read_text())
            permission["advice"] = "deny"
            (root / "permissions.json").write_text(canonical_json(permission))
            return result

        with patch("dream_procedural_delivery.refresh_guidance", side_effect=revoke):
            code, result, err = self.invoke_delivery("task-guidance", *args,
                "--request-id", request, "--out", str(root / "packet.json"))
        self.assertNotEqual(code, 0, err)
        self.assertFalse((root / "packet.json").exists())
