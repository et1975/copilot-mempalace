"""Receipts are immutable historical lineage, never eligibility or credit."""
import importlib
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import pytest

import dream_palace
from dream_metadata import canonical_json, is_generated_observation, is_procedural_record
from receipt_fixtures import receipts, resign
from test_dream_procedural import NOW, stamp
from test_dream_procedural_palace import DrawerCollection, sanctioned_writer
from test_dream_procedural_validate import GroundedFixture


def core():
    return importlib.import_module("dream_procedural_receipts")


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Copied report:\n{}\nEnd."])
@pytest.mark.parametrize("encoded", [False, True])
def test_receipt_full_source_transport_guard(index, wrapper, encoded):
    from dream_metadata import reject_generated_transport
    text = canonical_json(receipts()[index])
    text = wrapper.format(canonical_json(text) if encoded else text)
    with pytest.raises(ValueError, match="generated"):
        reject_generated_transport(text)
    assert is_generated_observation({"text": text})


@pytest.mark.parametrize("metadata", [
    {"kind": "procedural_receipt"}, {"room": "procedural-receipts"},
    {"added_by": "dream-procedure-receipt"},
])
def test_receipt_native_markers_are_generated_and_reserved(metadata):
    assert is_generated_observation({"text": "historical report", "metadata": metadata})
    assert is_procedural_record({"text": "historical report", "metadata": metadata})


def test_draft_echo_guard_shares_receipt_registry():
    from dream_procedural_drafts import is_procedural_echo
    for record in receipts()[:2]:
        assert is_procedural_echo("Copied report:\n" + canonical_json(canonical_json(record)))
    assert not is_procedural_echo("Later independent parser run failed on input X with error Y.")


def test_strict_roundtrip_parent_and_single_embedded_context():
    delivery, application, _ = receipts()
    assert core().validate_receipt(delivery, NOW) == delivery
    assert core().validate_receipt(application, NOW, parent=delivery) == application
    assert "context" not in application and "context" not in application["payload"]
    unsigned = deepcopy(delivery)
    del unsigned["digest"]
    assert core().prepare_receipt(unsigned, NOW) == delivery
    assert "digest" not in unsigned


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"kind": "outcome"}, {"authority": "verified"},
    {"digest": "0" * 64}, {"context_digest": "0" * 64}, {"outcome": "helpful"},
    {"recorded_at": "2026-09-24T00:00:00Z"}, {"receipt_id": "delivery:not-uuid"},
    {"recorded_at": "2026-09-22T00:00:00Z"}, {"receipt_permission_ref": ""},
])
def test_invalid_delivery_schema_and_digest(change):
    delivery, _, _ = receipts()
    delivery.update(change)
    if "digest" not in change:
        delivery = resign(delivery)
    with pytest.raises(ValueError):
        core().validate_receipt(delivery, NOW)


@pytest.mark.parametrize("change", [
    {"delivery_receipt_id": "delivery:" + str(UUID(int=222))},
    {"rule_id": "proc:" + "b" * 64}, {"disposition": "intended"},
    {"action": ""}, {"action": "x" * 1601}, {"action_reference": "x" * 513},
    {"score": 10}, {"reason": "extra field"},
])
def test_invalid_application_membership_and_schema(change):
    delivery, application, _ = receipts()
    application["payload"].update(change)
    with pytest.raises(ValueError):
        core().validate_receipt(resign(application), NOW, parent=delivery)


def test_application_needs_parent_context_and_time_but_not_current_eligibility():
    delivery, application, _ = receipts()
    with pytest.raises(ValueError, match="parent"):
        core().validate_receipt(application, NOW)
    application["context_digest"] = "0" * 64
    with pytest.raises(ValueError, match="context"):
        core().validate_receipt(resign(application), NOW, parent=delivery)
    application["context_digest"] = delivery["context_digest"]
    application["recorded_at"] = stamp(NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="before"):
        core().validate_receipt(resign(application), NOW, parent=delivery)
    application["recorded_at"] = stamp(NOW + timedelta(days=100))
    assert core().validate_receipt(resign(application), NOW + timedelta(days=100), parent=delivery)
    delivery["payload"]["packet"]["guidance"].update(rules=[], item_count=0, status="no_rules")
    from delivery_fixtures import digest
    delivery["payload"]["packet"]["guidance_digest"] = digest(delivery["payload"]["packet"]["guidance"])
    with pytest.raises(ValueError, match="member"):
        core().validate_receipt(resign(application), NOW + timedelta(days=100), parent=resign(delivery))


def test_receipts_cannot_relabel_foreign_scope_or_add_transfer_authority():
    from delivery_fixtures import digest
    delivery, _, _ = receipts()
    delivery["payload"]["packet"]["guidance"]["repository"] = "foreign/repo"
    delivery["payload"]["packet"]["guidance_digest"] = digest(delivery["payload"]["packet"]["guidance"])
    with pytest.raises(ValueError, match="scope"):
        core().validate_receipt(resign(delivery), NOW)
    for field in ("scope", "transfer_approved", "helpfulness", "support", "kg"):
        delivery, application, _ = receipts()
        application["payload"][field] = "source authority"
        with pytest.raises(ValueError, match="fields"):
            core().validate_receipt(resign(application), NOW, parent=delivery)


@pytest.fixture
def store(tmp_path, monkeypatch):
    collection = DrawerCollection()
    monkeypatch.setattr(dream_palace, "procedural_collection", lambda path: collection)
    return str(tmp_path), collection


def append(path, collection, record, permission, **kwargs):
    native, chunk_size = kwargs.pop("native", False), kwargs.pop("chunk_size", 83)
    return core().append_receipt(path, "w", record, permissions=lambda: permission,
        writer_factory=lambda: sanctioned_writer(path, collection, native=native, chunk_size=chunk_size),
        clock=lambda: NOW, **kwargs)


@pytest.mark.parametrize("native", [False, True])
def test_exact_chunked_roundtrip_retry_and_conflict(store, native):
    path, collection = store
    delivery, application, permission = receipts()
    assert append(path, collection, delivery, permission, native=native)["status"] == "appended"
    assert append(path, collection, application, permission, native=native)["status"] == "appended"
    assert core().read_receipts(path, "w", now=NOW)[delivery["receipt_id"]] == delivery
    assert core().read_receipts(path, "w", now=NOW)[application["receipt_id"]] == application
    before = deepcopy(collection.rows)
    def forbidden():
        pytest.fail("identical replay must precede writer construction and permission")
    assert core().append_receipt(path, "w", delivery, permissions=forbidden,
        writer_factory=forbidden, clock=lambda: NOW)["status"] == "already_exists"
    changed = deepcopy(delivery)
    changed["receipt_permission_ref"] = "user:4"
    with pytest.raises(ValueError, match="conflict"):
        core().append_receipt(path, "w", resign(changed), permissions=forbidden,
            writer_factory=forbidden, clock=lambda: NOW)
    assert collection.rows == before


@pytest.mark.parametrize("change", [
    {"advice": "deny"}, {"receipts": "deny"}, {"receipts": "unknown"},
    {"receipts_ref": "user:4"}, {"checked_at": stamp(NOW - timedelta(seconds=61))},
    {"repository": "foreign/repo"}, {"actor_id": str(UUID(int=777))},
])
def test_fresh_append_refuses_revoked_missing_stale_or_foreign_permission(store, change):
    path, collection = store
    delivery, _, permission = receipts()
    permission.update(change)
    with pytest.raises(ValueError):
        append(path, collection, delivery, permission)
    assert not collection.rows


def test_new_append_checks_permission_after_second_race_lookup_under_both_locks(store, monkeypatch):
    path, collection = store
    delivery, _, permission = receipts()
    active = []
    acquired = []
    writer = sanctioned_writer(path, collection)
    @contextmanager
    def mutation(*args):
        active.append("writer")
        acquired.append("writer")
        try:
            yield
        finally:
            active.pop()
    writer.mutation = mutation
    palace_lock = dream_palace.palace_mutation_lock
    @contextmanager
    def ordered_lock(*args, **kwargs):
        assert "writer" in active
        acquired.append("palace")
        with palace_lock(*args, **kwargs):
            yield
    monkeypatch.setattr(dream_palace, "palace_mutation_lock", ordered_lock)
    def witness():
        assert "writer" in active
        import fcntl
        import os
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        return permission
    assert core().append_receipt(path, "w", delivery, permissions=witness,
        writer_factory=lambda: writer, clock=lambda: NOW)["status"] == "appended"
    assert acquired[:2] == ["writer", "palace"]


def test_lost_acknowledgment_is_unknown_and_identical_retry_has_no_new_effect(store):
    path, collection = store
    delivery, _, permission = receipts()
    writer = sanctioned_writer(path, collection)
    real = writer.add_drawer
    def lost(*args, **kwargs):
        real(*args, **kwargs)
        raise TimeoutError("lost acknowledgment")
    writer.add_drawer = lost
    with pytest.raises(core().ReceiptOutcomeUnknown, match="identical"):
        core().append_receipt(path, "w", delivery, permissions=lambda: permission,
            writer_factory=lambda: writer, clock=lambda: NOW)
    before = deepcopy(collection.rows)
    assert append(path, collection, delivery, None)["status"] == "already_exists"
    assert collection.rows == before


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit"])
def test_append_exception_retry_distinguishes_attempts_from_durable_records(store, failure_point):
    path, collection = store
    delivery, _, permission = receipts()
    writer = sanctioned_writer(path, collection, native=True, chunk_size=100000)
    real_append = writer.add_drawer
    attempts = 0
    def unreliable(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure_point == "before_commit":
            raise OSError("transport failed before durable write")
        result = real_append(*args, **kwargs)
        if attempts == 1:
            raise OSError("transport failed after durable commit")
        return result
    writer.add_drawer = unreliable
    with pytest.raises(core().ReceiptOutcomeUnknown, match="identical"):
        core().append_receipt(path, "w", delivery, permissions=lambda: permission,
                             writer_factory=lambda: writer, clock=lambda: NOW)
    present = core().read_receipts(path, "w", now=NOW)
    assert (delivery["receipt_id"] in present) == (failure_point == "after_commit")
    result = core().append_receipt(path, "w", delivery, permissions=lambda: permission,
                                  writer_factory=lambda: writer, clock=lambda: NOW)
    assert result["status"] == ("appended" if failure_point == "before_commit" else "already_exists")
    assert attempts == (2 if failure_point == "before_commit" else 1)
    assert len(collection.rows) == 1
    assert core().read_receipts(path, "w", now=NOW) == {delivery["receipt_id"]: delivery}


def test_rotated_grant_acknowledges_committed_but_refuses_absent_stale_reference(store):
    path, collection = store
    delivery, _, permission = receipts()
    append(path, collection, delivery, permission)
    rotated = dict(permission, receipts_ref="user:rotated-grant")
    def forbidden():
        pytest.fail("committed acknowledgment must not construct writer or read permission")
    assert core().append_receipt(path, "w", delivery, permissions=forbidden,
        writer_factory=forbidden, clock=lambda: NOW)["status"] == "already_exists"
    conflicting = deepcopy(delivery)
    conflicting["receipt_permission_ref"] = rotated["receipts_ref"]
    with pytest.raises(ValueError, match="conflict"):
        core().append_receipt(path, "w", resign(conflicting), permissions=forbidden,
            writer_factory=forbidden, clock=lambda: NOW)
    absent = deepcopy(delivery)
    absent["payload"]["packet"]["request_id"] = str(UUID(int=999))
    absent["receipt_id"] = "delivery:" + str(UUID(int=999))
    before = deepcopy(collection.rows)
    with pytest.raises(ValueError, match="reference changed"):
        append(path, collection, resign(absent), rotated)
    assert absent["receipt_id"] not in core().read_receipts(path, "w", now=NOW)
    assert collection.rows == before


def test_unknown_absent_write_is_not_settled_or_reidentified_by_rotated_grant(store):
    path, collection = store
    delivery, _, permission = receipts()
    writer = sanctioned_writer(path, collection)
    def unavailable(*args, **kwargs):
        raise TimeoutError("append acknowledgment unavailable before durable effect")
    writer.add_drawer = unavailable
    with pytest.raises(core().ReceiptOutcomeUnknown):
        core().append_receipt(path, "w", delivery, permissions=lambda: permission,
                             writer_factory=lambda: writer, clock=lambda: NOW)
    assert core().read_receipts(path, "w", now=NOW) == {}
    with pytest.raises(ValueError, match="reference changed"):
        append(path, collection, delivery, dict(permission, receipts_ref="user:new-grant"))
    assert core().read_receipts(path, "w", now=NOW) == {}
    assert delivery["receipt_id"] == receipts()[0]["receipt_id"]


def test_conflicting_durable_readback_is_unknown_then_retry_is_readonly_conflict(store):
    path, collection = store
    delivery, _, permission = receipts()
    different = deepcopy(delivery)
    different["receipt_permission_ref"] = "user:another-historical-grant"
    different = resign(different)
    writer = sanctioned_writer(path, collection, native=True, chunk_size=100000)
    def conflicting(*args, **kwargs):
        body, metadata = core().record_data(different, "w")
        return collection.add("w", core().ROOM, body, core().AUTHOR, metadata)
    writer.add_drawer = conflicting
    with pytest.raises(core().ReceiptOutcomeUnknown, match="readback mismatch"):
        core().append_receipt(path, "w", delivery, permissions=lambda: permission,
                             writer_factory=lambda: writer, clock=lambda: NOW)
    assert len(collection.rows) == 1
    assert core().read_receipts(path, "w", now=NOW) == {different["receipt_id"]: different}
    def forbidden():
        pytest.fail("conflicting committed retry must not reach writer/permission")
    with pytest.raises(ValueError, match="conflict"):
        core().append_receipt(path, "w", delivery, permissions=forbidden,
                             writer_factory=forbidden, clock=lambda: NOW)


def test_normal_chunk_page_splits_assemble_across_discovery_and_body_pages(store, monkeypatch):
    path, collection = store
    delivery, application, permission = receipts()
    append(path, collection, delivery, permission, chunk_size=5)
    append(path, collection, application, permission, chunk_size=5)
    calls = []
    original = collection.get
    def observed(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(collection, "get", observed)
    assert core().read_receipts(path, "w", now=NOW) == {
        delivery["receipt_id"]: delivery, application["receipt_id"]: application}
    assert any(call.get("offset", 0) >= 256 for call in calls)
    assert any(len(call.get("ids", [])) == 256 for call in calls)


def test_missing_chunks_corruption_and_overflow_are_not_empty_history(store):
    path, collection = store
    delivery, _, permission = receipts()
    append(path, collection, delivery, permission, chunk_size=40)
    before = deepcopy(collection.rows)
    del collection.rows[sorted(collection.rows)[1]]
    with pytest.raises(ValueError):
        core().read_receipts(path, "w", now=NOW)
    collection.rows = before
    key = next(iter(collection.rows))
    collection.rows[key]["text"] = "corrupt"
    with pytest.raises(ValueError):
        core().read_receipts(path, "w", now=NOW)


def cli(path, *args):
    from contextlib import redirect_stdout, redirect_stderr
    import io
    import json
    import dream_procedure
    output, error = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(error):
        code = dream_procedure.main([args[0], "--palace", path, "--wing", "w", *args[1:]])
    return code, json.loads(output.getvalue()), error.getvalue()


def test_cli_prepare_dry_run_and_history_never_construct_writer(store, tmp_path, monkeypatch):
    import dream_procedure
    path, collection = store
    monkeypatch.setattr(dream_procedure, "now_utc", lambda: NOW)
    monkeypatch.setattr(dream_palace, "MempalaceWriter", lambda: pytest.fail("writer on read-only path"))
    delivery, _, permission = receipts()
    unsigned = deepcopy(delivery)
    del unsigned["digest"]
    input_file, permission_file, prepared = tmp_path / "input", tmp_path / "permission", tmp_path.parent / (
        tmp_path.name + "-prepared.json")
    input_file.write_text(canonical_json(unsigned))
    permission_file.write_text(canonical_json(permission))
    code, report, _ = cli(path, "receipt", "--input", str(input_file), "--permissions",
                          str(permission_file), "--prepare", "--out", str(prepared))
    assert code == 0 and report["status"] == "prepared"
    assert prepared.read_text() == canonical_json(delivery) + "\n"
    input_file.write_text(canonical_json(delivery))
    code, report, _ = cli(path, "receipt", "--input", str(input_file), "--permissions",
                          str(permission_file), "--dry-run")
    assert code == 0 and report["status"] == "dry_run"
    assert not collection.rows
    permission_file.write_text(canonical_json(dict(permission, receipts="deny")))
    assert cli(path, "receipt", "--input", str(prepared), "--permissions",
               str(permission_file), "--dry-run")[0] != 0
    context_file = tmp_path / "context"
    context_file.write_text(canonical_json(delivery["payload"]["packet"]["context"]))
    code, report, _ = cli(path, "delivery-status", "--context", str(context_file))
    assert code == 0 and report["matching_delivery_ids"] == [] and report["reports"] == []
    assert report["status"] == "ok" and "history" in report["notice"]


def test_real_installed_cli_append_history_after_host_loss_and_revoked_retry(tmp_path, monkeypatch):
    import dream_procedure
    import json
    from test_dream_procedural_palace import installed_palace
    path = str(tmp_path / "palace")
    (tmp_path / "palace").mkdir()
    monkeypatch.setattr(dream_procedure, "now_utc", lambda: NOW)
    delivery, application, permission = receipts()
    files = {}
    for name, value in (("delivery", delivery), ("application", application), ("permission", permission)):
        files[name] = tmp_path / (name + ".json")
        files[name].write_text(canonical_json(value))
    with installed_palace(path) as server:
        dream_palace.bind_palace(path)
        writer = dream_palace.MempalaceWriter()
        writer.add_drawer("w", "notes", "Ordinary independent observation.")
        for name in ("delivery", "application"):
            code, report, error = cli(path, "receipt", "--input", str(files[name]),
                                      "--permissions", str(files["permission"]))
            assert code == 0, (report, error)
            assert report["status"] == "appended"
        from dream_procedural_palace import read_events
        assert read_events(path, "w") == []
        before = core().read_receipts(path, "w", now=NOW)
        pending = deepcopy(application)
        pending["receipt_id"] = "application:" + str(UUID(int=908))
        files["application"].write_text(canonical_json(resign(pending)))
        prepared = tmp_path / "prepared-pending.json"
        code, report, _ = cli(path, "receipt", "--input", str(files["application"]),
            "--permissions", str(files["permission"]), "--prepare", "--out", str(prepared))
        assert code == 0 and report["status"] == "prepared"
        files["permission"].write_text(canonical_json(dict(permission, receipts="deny")))
        code, report, _ = cli(path, "receipt", "--input", str(prepared),
                              "--permissions", str(files["permission"]))
        assert code != 0 and "permission" in report["error"]
        assert core().read_receipts(path, "w", now=NOW) == before
        files["permission"].unlink()
        files["application"].unlink()
        monkeypatch.setenv("COPILOT_SESSION_STORE", str(tmp_path / "missing-host"))
        server._release_mcp_writer_lock()
        monkeypatch.setattr(server, "_READ_ONLY", True)
        connection = server._get_collection()._handle.conn
        connection.execute("PRAGMA wal_autocheckpoint=0")
        snapshot = tuple(connection.iterdump())
        with monkeypatch.context() as readonly:
            readonly.setattr(dream_palace, "MempalaceWriter", lambda: pytest.fail("writer after revocation"))
            code, report, error = cli(path, "receipt", "--input", str(files["delivery"]))
            assert code == 0 and report["status"] == "already_exists", (report, error)
            code, report, error = cli(path, "receipt-get", "--receipt-id", application["receipt_id"])
            assert code == 0 and report["receipt"] == application, (report, error)
        assert core().read_receipts(path, "w", now=NOW) == before
        assert tuple(connection.iterdump()) == snapshot
        assert not (tmp_path / "missing-host").exists()
        new = deepcopy(delivery)
        new["payload"]["packet"]["request_id"] = str(UUID(int=907))
        new["receipt_id"] = "delivery:" + str(UUID(int=907))
        files["delivery"].write_text(canonical_json(resign(new)))
        files["permission"].write_text(canonical_json(permission))
        code, report, _ = cli(path, "receipt", "--input", str(files["delivery"]),
                              "--permissions", str(files["permission"]))
        assert code != 0 and "refused" in report["error"]
        assert core().read_receipts(path, "w", now=NOW) == before


@pytest.mark.parametrize("task", ["a" * 16384, "界" * (16384 // 3)])
def test_maximum_context_and_full_guidance_fit_one_encoded_receipt(task):
    from delivery_fixtures import case, digest
    packet, _, _, guidance = case(task)
    packet["context"]["constraints"] = ["x" * 512] * 7 + [""]
    remaining = 20480 - len(canonical_json(packet["context"]).encode())
    packet["context"]["constraints"][-1] = "x" * remaining
    assert 0 < remaining <= 512
    packet["context_digest"] = digest(packet["context"])
    guidance["rules"] = [dict(guidance["rules"][0], rule_id="proc:" + f"{i:064x}",
                              statement="s" * 500, applies_when="a" * 120, exceptions=["e" * 50])
                         for i in range(5)]
    guidance["item_count"] = 5
    packet["guidance_digest"] = digest(guidance)
    record, _, _ = receipts()
    record["payload"]["packet"] = packet
    record["context_digest"] = packet["context_digest"]
    record = resign(record)
    core().validate_receipt(record, NOW)
    body, metadata = core().record_data(record, "w")
    import json
    decoded = json.loads(body[len(core().HEADER):])
    assert decoded == record
    assert decoded["payload"].keys() == {"packet", "acknowledgment"}
    assert decoded["payload"]["packet"] == packet
    assert "context" not in decoded and "context" not in decoded["payload"]
    assert "packet" not in metadata and "context" not in metadata
    assert len((body + canonical_json(metadata)).encode()) < 65536


def test_serialized_wrapper_bounds_count_escaped_controls_not_characters():
    delivery, application, _ = receipts()
    application["payload"]["action"] = "\x00" * 1600
    with pytest.raises(ValueError, match="wrapper"):
        core().validate_receipt(resign(application), NOW, parent=delivery)
    application["payload"]["action"] = "reason"
    application["payload"]["disposition"] = "not_applied"
    application["payload"]["action_reference"] = None
    assert core().validate_receipt(resign(application), NOW, parent=delivery)


def test_second_lookup_observes_race_before_permission(store):
    path, collection = store
    delivery, _, permission = receipts()
    def factory():
        append(path, collection, delivery, permission)
        return sanctioned_writer(path, collection)
    result = core().append_receipt(path, "w", delivery,
        permissions=lambda: pytest.fail("raced identical commit does not need new permission"),
        writer_factory=factory, clock=lambda: NOW)
    assert result["status"] == "already_exists"


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_same_id_writers_serialize_second_lookup(store, conflict):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path, collection = store
    delivery, _, permission = receipts()
    other = deepcopy(delivery)
    if conflict:
        other["recorded_at"] = stamp(NOW + timedelta(seconds=1))
        other = resign(other)
    ready = Barrier(2)
    def write(record):
        def factory():
            ready.wait(timeout=10)
            return sanctioned_writer(path, collection, native=True, chunk_size=100000)
        try:
            return core().append_receipt(path, "w", record, permissions=lambda: permission,
                writer_factory=factory, clock=lambda: NOW + timedelta(seconds=1))["status"]
        except ValueError as exc:
            assert "conflict" in str(exc)
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(write, (delivery, other)))
    assert results == (["appended", "conflict"] if conflict else ["already_exists", "appended"])
    assert len(collection.rows) == 1


def test_history_reports_prior_revisions_and_absence_as_unknown(store):
    path, collection = store
    delivery, application, permission = receipts()
    append(path, collection, delivery, permission)
    context = deepcopy(delivery["payload"]["packet"]["context"])
    report = core().delivery_status(path, "w", context, now=NOW)
    assert report["unknown_applications"] == [{
        "delivery_receipt_id": delivery["receipt_id"], "rule_id": application["payload"]["rule_id"]}]
    append(path, collection, application, permission)
    context["revision"] += 1
    report = core().delivery_status(path, "w", context, now=NOW)
    assert report["matching_delivery_ids"] == []
    assert report["prior_revision_delivery_ids"] == [delivery["receipt_id"]]
    assert report["unknown_applications"] == [] and report["reports"] == [application]


def test_reserved_receipt_retention_is_body_independent_not_rule_projection(store, monkeypatch):
    from dream_procedural_palace import live_protected_drawer_ids, read_events
    path, collection = store
    delivery, application, permission = receipts()
    append(path, collection, delivery, permission)
    append(path, collection, application, permission)
    expected = set(collection.rows)
    expected.update(row["metadata"].get("parent_drawer_id") for row in collection.rows.values())
    expected.discard(None)
    for row in collection.rows.values():
        row["text"] = "corrupt"
    monkeypatch.setattr(dream_palace, "protection_collection", lambda path: collection)
    assert expected <= live_protected_drawer_ids(path)
    assert read_events(path, "w") == []
    with pytest.raises(ValueError):
        core().read_receipts(path, "w", now=NOW)
    import mempalace.palace
    monkeypatch.setattr(mempalace.palace, "get_collection", lambda path: collection)
    assert dream_palace.load_logical_drawers(path) == []
    assert dream_palace.load_observation_entries(path, rooms=("procedural-receipts",)) == []


def test_receipt_page_duplicate_out_of_scope_and_capacity_fail_closed(store, monkeypatch):
    path, collection = store
    delivery, _, permission = receipts()
    append(path, collection, delivery, permission)
    second = deepcopy(delivery)
    second["payload"]["packet"]["request_id"] = str(UUID(int=906))
    second["receipt_id"] = "delivery:" + str(UUID(int=906))
    monkeypatch.setattr(core(), "MAX_RECEIPTS", 1)
    before = deepcopy(collection.rows)
    with pytest.raises(ValueError, match="limit"):
        append(path, collection, resign(second), permission)
    assert collection.rows == before
    monkeypatch.setattr(core(), "MAX_RECEIPTS", 5000)
    append(path, collection, resign(second), permission)
    monkeypatch.setattr(core(), "MAX_RECEIPTS", 1)
    with pytest.raises(ValueError, match="limit"):
        core().read_receipts(path, "w", now=NOW)


def test_actual_5000_limit_pages_strictly_and_errors_on_5001_without_body_reads(store, monkeypatch):
    path, collection = store
    collection.rows = {f"physical-{i:04}": dict(id=f"physical-{i:04}", text="unneeded",
        metadata={"wing": "w", "room": "procedural-receipts", "added_by": "dream-procedure-receipt"})
        for i in range(5000)}
    calls = []
    real = collection.get
    def page(**kwargs):
        calls.append(kwargs)
        assert kwargs["include"] == ["metadatas"] and kwargs["limit"] == 256
        return real(**kwargs)
    monkeypatch.setattr(collection, "get", page)
    assert len(core()._groups(collection, "w")) == 5000
    assert [c["offset"] for c in calls] == list(range(0, 5120, 256)) + [5000]
    collection.rows["overflow"] = dict(id="overflow", text="unneeded",
        metadata={"wing": "w", "room": "procedural-receipts", "added_by": "dream-procedure-receipt"})
    with pytest.raises(ValueError, match="limit"):
        core().read_receipts(path, "w", now=NOW)


@pytest.mark.parametrize("failure", ["duplicate", "outside", "missing_metadata", "oversized"])
def test_bad_metadata_pages_do_not_become_empty_history(store, monkeypatch, failure):
    path, collection = store
    meta = {"wing": "w", "room": "procedural-receipts", "added_by": "dream-procedure-receipt"}
    if failure == "outside":
        meta["wing"] = "other"
    rows = {"ids": ["physical"], "metadatas": [meta]}
    if failure == "missing_metadata":
        rows["metadatas"] = []
    if failure == "oversized":
        rows = {"ids": [str(i) for i in range(257)], "metadatas": [meta] * 257}
    monkeypatch.setattr(collection, "get", lambda **kwargs: rows)
    with pytest.raises(ValueError):
        core().read_receipts(path, "w", now=NOW)


def test_capacity_dry_run_is_not_successful_when_new_append_cannot_fit(store, tmp_path, monkeypatch):
    import dream_procedure
    path, collection = store
    delivery, _, permission = receipts()
    append(path, collection, delivery, permission)
    monkeypatch.setattr(core(), "MAX_RECEIPTS", 1)
    monkeypatch.setattr(dream_procedure, "now_utc", lambda: NOW)
    new = deepcopy(delivery)
    new["payload"]["packet"]["request_id"] = str(UUID(int=906))
    new["receipt_id"] = "delivery:" + str(UUID(int=906))
    input_file, permissions_file = tmp_path / "record", tmp_path / "permission"
    input_file.write_text(canonical_json(resign(new)))
    permissions_file.write_text(canonical_json(permission))
    code, report, _ = cli(path, "receipt", "--input", str(input_file),
                          "--permissions", str(permissions_file), "--dry-run")
    assert code != 0 and "limit" in report["error"]


def test_physical_duplicate_history_cannot_bypass_new_append_capacity(store, monkeypatch):
    path, collection = store
    delivery, _, permission = receipts()
    append(path, collection, delivery, permission, native=True, chunk_size=100000)
    row = deepcopy(next(iter(collection.rows.values())))
    row["id"] = "same-receipt-another-physical-drawer"
    collection.rows[row["id"]] = row
    monkeypatch.setattr(core(), "MAX_RECEIPTS", 2)
    before = deepcopy(collection.rows)
    new = deepcopy(delivery)
    new["payload"]["packet"]["request_id"] = str(UUID(int=906))
    new["receipt_id"] = "delivery:" + str(UUID(int=906))
    with pytest.raises(ValueError, match="limit"):
        append(path, collection, resign(new), permission)
    assert collection.rows == before


def test_shared_encoder_exact_limits_and_limit_plus_one(monkeypatch):
    delivery, _, _ = receipts()
    body, metadata = core().record_data(delivery, "w")
    native = dict(metadata, wing="w", room=core().ROOM, added_by=core().AUTHOR)
    trailer = "\n\n<!--dreaming-meta: " + canonical_json(metadata) + "-->"
    limits = {
        "MAX_RECORD_BYTES": len((body + trailer + canonical_json(native)).encode()),
        "MAX_WRAPPER_BYTES": (len(canonical_json(delivery).encode())
                              - len(canonical_json(delivery["payload"]["packet"]).encode())),
        "MAX_METADATA_BYTES": len((core().HEADER + trailer + canonical_json(native) + "\n").encode()),
    }
    for name, exact in limits.items():
        with monkeypatch.context() as bounded:
            bounded.setattr(core(), name, exact)
            assert core().record_data(delivery, "w") == (body, metadata)
            bounded.setattr(core(), name, exact - 1)
            with pytest.raises(ValueError, match="exceeds"):
                core().record_data(delivery, "w")


def test_readback_failure_is_unknown_then_retry_resolves_actual_committed_record(store, monkeypatch):
    path, collection = store
    delivery, _, permission = receipts()
    real_read = core()._read_receipts
    count = 0
    def lost_readback(*args):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("readback unavailable")
        return real_read(*args)
    monkeypatch.setattr(core(), "_read_receipts", lost_readback)
    with pytest.raises(core().ReceiptOutcomeUnknown, match="identical"):
        append(path, collection, delivery, permission)
    before = deepcopy(collection.rows)
    assert append(path, collection, delivery, None)["status"] == "already_exists"
    assert collection.rows == before


class InstalledReceiptLifecycleTests(GroundedFixture):
    def test_truthful_retired_rule_history_preserves_projection_and_original_parents(self):
        from pathlib import Path
        from unittest.mock import patch
        from delivery_fixtures import case
        from dream_procedural_delivery import build_packet, refresh_guidance
        from dream_procedural_palace import append_event, read_events, live_protected_drawer_ids
        from dream_procedural import parse_event
        from test_dream_procedural import event_data
        from test_dream_procedural_palace import installed_palace
        self.storage_patch.stop()
        with installed_palace(self.path) as server:
            writer = dream_palace.MempalaceWriter()
            for ref in self.refs[:3]:
                ref["source_id"] = writer.add_drawer("w", "diary", ref["quote"])["drawer_id"]
            events = [self.proposal(), self.review(), *[
                parse_event(event_data("outcome", number, source_session_id=ref["session_id"],
                                       evidence=[ref]))
                for number, ref in enumerate(self.refs[:3], 10)]]
            for event in events:
                with writer.mutation():
                    append_event(self.path, "w", event, writer=writer,
                                 session_store=self.store, clock=lambda: NOW)
            _, current, permission, _ = case()
            current["context"]["task"] = "Fix a reproducible defect using a focused regression test."
            packet = build_packet(current, permission, str(UUID(int=904)),
                                  lambda context, now: refresh_guidance(self.path, context, now), NOW)
            self.assertEqual(packet["guidance"]["item_count"], 1)
            delivery, application, permission = receipts()
            delivery["payload"]["packet"] = packet
            delivery["context_digest"] = packet["context_digest"]
            application["context_digest"] = packet["context_digest"]
            application["payload"]["rule_id"] = events[0].rule_id
            retired = self.review(31, verdict="retire", parent_review_ids=[events[1].event_id])
            with writer.mutation():
                append_event(self.path, "w", retired, writer=writer,
                             session_store=self.store, clock=lambda: NOW)
            before = self.projection(*read_events(self.path, "w"))
            self.assertIn("retired", before.rules[0].suppression_reasons)
            Path(self.store).unlink()
            with patch("dream_procedural_delivery.preflight_use", side_effect=AssertionError("historical use-check")), \
                 patch("dream_procedural_delivery.refresh_guidance", side_effect=AssertionError("historical refresh")):
                for record in (resign(delivery), resign(application)):
                    result = core().append_receipt(self.path, "w", record,
                        permissions=lambda: permission, writer_factory=lambda: writer, clock=lambda: NOW)
                    self.assertEqual(result["status"], "appended")
            self.assertEqual(self.projection(*read_events(self.path, "w")), before)
            self.assertEqual(core().read_receipts(self.path, "w", now=NOW)[application["receipt_id"]],
                             resign(application))
            protected = live_protected_drawer_ids(self.path)
            self.assertTrue({r["source_id"] for r in self.refs[:3]} <= protected)
            for source in self.refs[:3]:
                with self.assertRaisesRegex(ValueError, "protected"):
                    writer.delete_drawer(source["source_id"])
            col = server._get_collection()
            reserved = col.get(where={"room": "procedural-receipts"}, include=["metadatas"])
            for pid in reserved["ids"]:
                with self.assertRaisesRegex(ValueError, "protected"):
                    writer.delete_drawer(pid)
            with writer.mutation():
                server._get_collection().update(ids=[reserved["ids"][0]], documents=["corrupt receipt"])
            self.assertEqual(self.projection(*read_events(self.path, "w")), before)
            from dream_procedural_palace import procedural_status
            self.assertEqual(procedural_status(self.path, "w", "owner/repo", as_of=NOW)["status"], "ok")
            self.assertTrue(set(reserved["ids"]) <= live_protected_drawer_ids(self.path))
            with self.assertRaises(ValueError):
                core().read_receipts(self.path, "w", now=NOW)
