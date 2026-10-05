"""Immutable, agent-reported history in sanctioned MemPalace drawers.

Receipts neither refresh rule eligibility nor grant consent, support or credit.
No host store, outcome ledger or secondary journal participates in this history.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import os
import re

import dream_palace
from dream_metadata import (
    assemble_exact_chunks, canonical_json, decode_dream_metadata, exact_chunk_groups, strict_json,
)
from dream_procedural import utc_datetime
from dream_procedural_delivery import (
    _object, _require, _text, _uuid, consent, hash_data, validate_context, validate_packet,
)
from dream_procedural_palace import _wing, nonmutating_read
from dream_procedural_sources import _check_chunk_identity

ROOM = "procedural-receipts"
KIND = "procedural_receipt"
AUTHOR = "dream-procedure-receipt"
HEADER = "Procedural receipt; agent-reported history, not current eligibility or helpfulness.\n"
NOTICE = "Agent-reported history; not current eligibility or helpfulness."
MAX_RECEIPTS = 5000
MAX_RECORD_BYTES = 64 * 1024
MAX_WRAPPER_BYTES = 4 * 1024
MAX_METADATA_BYTES = 2 * 1024
FIELDS = {"schema_version", "kind", "receipt_id", "record_type", "recorded_at", "authority",
          "context_digest", "receipt_permission_ref", "payload", "digest"}


class ReceiptOutcomeUnknown(RuntimeError):
    code = "receipt_outcome_unknown"


def now_utc():
    return datetime.now(timezone.utc)


def receipt_id(value, record_type=None):
    _text(value, "receipt_id")
    prefix, separator, occurrence = value.partition(":")
    _require(separator and prefix in ("delivery", "application")
             and (record_type is None or prefix == record_type), "invalid receipt_id type")
    _uuid(occurrence, "receipt occurrence")
    return value


def _shape(record, now):
    _object(record, FIELDS, "receipt")
    _require(type(record["schema_version"]) is int and record["schema_version"] == 1,
             "invalid receipt schema_version")
    _require(record["kind"] == KIND and record["authority"] == "agent_reported",
             "invalid receipt kind/authority")
    _require(record["record_type"] in ("delivery", "application"), "invalid receipt record_type")
    receipt_id(record["receipt_id"], record["record_type"])
    _text(record["receipt_permission_ref"], "receipt_permission_ref")
    _require(isinstance(record["context_digest"], str)
             and re.fullmatch(r"[0-9a-f]{64}", record["context_digest"]), "invalid context digest")
    _require(isinstance(record["recorded_at"], str), "recorded_at must be UTC")
    recorded = utc_datetime(record["recorded_at"])
    _require(recorded <= utc_datetime(now), "future receipt recorded_at")
    payload = record["payload"]
    if record["record_type"] == "delivery":
        _object(payload, {"packet", "acknowledgment"}, "delivery payload")
        _require(payload["acknowledgment"] == "read_full_packet", "invalid acknowledgment")
        packet = validate_packet(payload["packet"], now)
        _require(record["receipt_id"] == "delivery:" + packet["request_id"],
                 "delivery ID does not match packet request")
        _require(record["context_digest"] == packet["context_digest"], "receipt context digest mismatch")
        _require(recorded >= utc_datetime(packet["guidance"]["as_of"]),
                 "receipt recorded before packet")
    else:
        _object(payload, {"delivery_receipt_id", "rule_id", "disposition", "action", "action_reference"},
                "application payload")
        receipt_id(payload["delivery_receipt_id"], "delivery")
        _require(isinstance(payload["rule_id"], str)
                 and re.fullmatch(r"proc:[0-9a-f]{64}", payload["rule_id"]), "invalid application rule_id")
        _require(payload["disposition"] in ("applied", "not_applied"), "invalid disposition")
        # For not_applied, action is the explicit reason; no second scope/reason ledger.
        _text(payload["action"], "performed action or nonapplication reason", chars=1600)
        if payload["action_reference"] is not None:
            _text(payload["action_reference"], "action_reference")
    _require(record["digest"] == hash_data({k: v for k, v in record.items() if k != "digest"}),
             "receipt digest mismatch")


def record_data(record, wing, *, native_metadata=None):
    """One UTF-8 encoder/budget for preparation, append, reads and restore.

    Count native metadata AND the fallback trailer, even if only one is stored.
    Native storage/chunk bookkeeping is also bounded when reading actual bytes.
    """
    _wing(wing)
    serialized = canonical_json(record)
    packet = (canonical_json(record["payload"]["packet"])
              if record["record_type"] == "delivery" else "")
    _require(len(serialized.encode("utf-8")) - len(packet.encode("utf-8")) <= MAX_WRAPPER_BYTES,
             "receipt wrapper exceeds 4 KiB")
    metadata = dict(kind=KIND, schema_version=1, receipt_id=record["receipt_id"],
                    digest=record["digest"], generated_summary=True)
    native = (dict(metadata, wing=wing, room=ROOM, added_by=AUTHOR)
              if native_metadata is None else native_metadata)
    trailer = "\n\n<!--dreaming-meta: " + canonical_json(metadata) + "-->"
    overhead = HEADER + trailer + canonical_json(native) + "\n"
    _require(len(overhead.encode("utf-8")) <= MAX_METADATA_BYTES, "receipt metadata/header exceeds 2 KiB")
    body = HEADER + serialized + "\n"
    _require(len((body + trailer + canonical_json(native)).encode("utf-8")) <= MAX_RECORD_BYTES,
             "encoded receipt exceeds 64 KiB")
    return body, metadata


def validate_receipt(record, now, *, parent=None, wing=None):
    _shape(record, now)
    if record["record_type"] == "delivery":
        context = record["payload"]["packet"]["context"]
    else:
        _require(parent is not None and parent.get("record_type") == "delivery",
                 "application delivery parent is missing")
        validate_receipt(parent, now)
        payload = record["payload"]
        _require(parent["receipt_id"] == payload["delivery_receipt_id"], "application parent mismatch")
        _require(parent["context_digest"] == record["context_digest"], "application context mismatch")
        _require(utc_datetime(record["recorded_at"]) >= utc_datetime(parent["recorded_at"]),
                 "application recorded before parent")
        packet = parent["payload"]["packet"]
        ids = {item["rule_id"] for section in ("rules", "anti_patterns", "trials")
               for item in packet["guidance"][section]}
        _require(payload["rule_id"] in ids, "application rule is not a parent packet member")
        context = packet["context"]
    _require(wing is None or context["wing"] == wing, "receipt wing mismatch")
    record_data(record, context["wing"])
    return record


def prepare_receipt(record, now, *, parent=None, wing=None):
    result = deepcopy(record)
    _require(isinstance(result, dict), "receipt must be an object")
    result.setdefault("digest", hash_data({k: v for k, v in result.items() if k != "digest"}))
    return validate_receipt(result, now, parent=parent, wing=wing)


def _groups(collection, wing=None):
    markers = {"$or": [{"room": ROOM}, {"kind": KIND}, {"added_by": AUTHOR}]}
    where = {"$and": [{"wing": _wing(wing)}, markers]} if wing is not None else markers
    rows, seen, by_wing, offset = [], set(), {}, 0
    while True:
        page = collection.get(where=where, include=["metadatas"], limit=256, offset=offset)
        ids, metadata = page.get("ids"), page.get("metadatas")
        _require(isinstance(ids, list) and isinstance(metadata, list)
                 and len(ids) == len(metadata) <= 256, "invalid receipt discovery page")
        if not ids:
            break
        for pid, meta in zip(ids, metadata):
            _require(isinstance(pid, str) and pid not in seen, "duplicate receipt physical row")
            seen.add(pid)
            _require(isinstance(meta, dict) and meta.get("room") == ROOM
                     and (wing is None or meta.get("wing") == wing), "invalid receipt storage scope")
            locus = _wing(meta.get("wing"))
            parent = meta.get("parent_drawer_id") or pid
            _require(isinstance(parent, str), "invalid receipt chunk parent")
            parents = by_wing.setdefault(locus, set())
            parents.add(parent)
            _require(len(parents) <= MAX_RECEIPTS, "receipt limit exceeded; refusing partial history")
            rows.append({"id": pid, "metadata": meta})
        offset += len(ids)
    groups = exact_chunk_groups(rows)
    for parent, members, _ in groups:
        _check_chunk_identity(parent, members)
    return groups


def receipt_record_ids(collection):
    """Metadata-only retention: corrupt receipt bodies never become deletable."""
    return {value for parent, members, _ in _groups(collection)
            for value in (parent, *(row["id"] for row in members))}


def _read_receipts(palace, wing, now, with_count=False):
    col = dream_palace.procedural_collection(palace)
    records = {}
    groups = _groups(col, wing)
    for _, members, _ in groups:
        rows, size = [], 0
        for start in range(0, len(members), 256):
            selected = members[start:start + 256]
            page = col.get(ids=[m["id"] for m in selected], include=["documents", "metadatas"])
            ids, texts, metas = page.get("ids"), page.get("documents"), page.get("metadatas")
            _require(isinstance(ids, list) and isinstance(texts, list) and isinstance(metas, list)
                     and len(ids) == len(texts) == len(metas) == len(selected),
                     "missing receipt chunks")
            expected = {m["id"]: m["metadata"] for m in selected}
            _require(len(set(ids)) == len(ids) and set(ids) == set(expected), "receipt chunk IDs changed")
            for pid, text, meta in zip(ids, texts, metas):
                _require(isinstance(text, str) and meta == expected[pid], "receipt chunks changed")
                size += len(text.encode("utf-8"))
                _require(size <= MAX_RECORD_BYTES, "encoded receipt exceeds 64 KiB")
                rows.append({"id": pid, "text": text, "metadata": meta})
        drawer, = assemble_exact_chunks(rows)
        metadata = decode_dream_metadata(drawer)
        text = drawer["text"]
        _require(text.startswith(HEADER), "invalid receipt header")
        payload = text[len(HEADER):]
        trailer = "\n\n<!--dreaming-meta: "
        if trailer in payload:
            payload = payload[:payload.rfind(trailer)]
        record = strict_json(payload)
        _shape(record, now)
        body, expected = record_data(record, wing, native_metadata=metadata)
        _require(all(type(metadata.get(k)) is type(v) and metadata[k] == v
                     for k, v in expected.items()) and metadata.get("added_by") == AUTHOR,
                 "receipt metadata/body mismatch")
        _require(text in (body, body + trailer + canonical_json(expected) + "-->"),
                 "receipt canonical body/chunk reconstruction mismatch")
        prior = records.get(record["receipt_id"])
        _require(prior is None or prior == record, "receipt ID conflict")
        records[record["receipt_id"]] = record
    for record in records.values():
        parent = records.get(record["payload"].get("delivery_receipt_id"))
        validate_receipt(record, now, parent=parent, wing=wing)
    return (records, len(groups)) if with_count else records


def read_receipts(palace, wing, *, now=None):
    with nonmutating_read(palace):
        return _read_receipts(palace, _wing(wing), now or now_utc())


def _prior(records, record):
    prior = records.get(record["receipt_id"])
    _require(prior is None or prior == record, "receipt ID conflict")
    return prior is not None


def _result(status, record):
    return {"status": status, "receipt_id": record["receipt_id"], "digest": record["digest"],
            "notice": NOTICE}


def receipt_preflight(record, records, wing, permissions, now):
    parent = records.get(record["payload"].get("delivery_receipt_id"))
    validate_receipt(record, now, parent=parent, wing=wing)
    context = (record if record["record_type"] == "delivery" else parent)["payload"]["packet"]["context"]
    consent(context, permissions, now, receipt=True)
    _require(record["receipt_permission_ref"] == permissions["receipts_ref"],
             "receipt permission reference changed")


def append_receipt(palace, wing, record, *, permissions, writer_factory=None, clock=now_utc):
    """Read-only historical retry first; only absent records acquire a writer.

    The permission callable rereads current input under BOTH mutation scopes.
    Post-effect failures are unknown, never permission to invent a replacement ID.
    """
    palace, wing, record = os.path.realpath(os.path.expanduser(palace)), _wing(wing), deepcopy(record)
    _shape(record, clock())
    records = read_receipts(palace, wing, now=clock())
    if _prior(records, record):
        return _result("already_exists", record)
    if writer_factory is None:
        def writer_factory():
            dream_palace.bind_palace(palace)
            from dream_procedural_palace import local_embedder
            local_embedder()
            return dream_palace.MempalaceWriter()
    writer = writer_factory()
    _require(os.path.realpath(writer.palace_path) == palace, "writer is bound to a different palace")
    with writer.mutation(), dream_palace.palace_mutation_lock(palace):
        records, count = _read_receipts(palace, wing, clock(), True)
        if _prior(records, record):
            return _result("already_exists", record)
        _require(count < MAX_RECEIPTS, "receipt limit exceeded")
        body, metadata = record_data(record, wing)
        witness = permissions()
        receipt_preflight(record, records, wing, witness, clock())
        try:
            result = writer.add_drawer(wing, ROOM, body, added_by=AUTHOR, metadata=metadata)
            if not isinstance(result, dict) or result.get("success") is False:
                raise RuntimeError("receipt append not acknowledged")
            readback = _read_receipts(palace, wing, clock())
            if readback.get(record["receipt_id"]) != record or any(
                    readback.get(key) != value for key, value in records.items()):
                raise RuntimeError("receipt exact readback mismatch")
        except Exception as exc:
            raise ReceiptOutcomeUnknown(
                "receipt outcome unknown; retry the identical artifact: " + str(exc)) from exc
    return _result("appended", record)


def delivery_status(palace, wing, context, *, now=None):
    validate_context(context)
    _require(context["wing"] == wing, "context wing mismatch")
    records = read_receipts(palace, wing, now=now)
    matching, prior = [], []
    for record in records.values():
        if record["record_type"] != "delivery":
            continue
        old = record["payload"]["packet"]["context"]
        if old == context:
            matching.append(record)
        elif all(old[k] == context[k] for k in ("repository", "wing", "session_id", "actor_id", "task_id")) \
                and old["revision"] < context["revision"]:
            prior.append(record)
    selected = {r["receipt_id"] for r in matching + prior}
    reports = sorted((r for r in records.values() if r["record_type"] == "application"
                      and r["payload"]["delivery_receipt_id"] in selected),
                     key=lambda r: (r["recorded_at"], r["receipt_id"]))
    reported = {(r["payload"]["delivery_receipt_id"], r["payload"]["rule_id"]) for r in reports}
    unknown = [{"delivery_receipt_id": r["receipt_id"], "rule_id": item["rule_id"]}
               for r in sorted(matching + prior, key=lambda r: r["receipt_id"])
               for section in ("rules", "anti_patterns", "trials")
               for item in r["payload"]["packet"]["guidance"][section]
               if (r["receipt_id"], item["rule_id"]) not in reported]
    return {"status": "ok", "notice": NOTICE,
            "matching_delivery_ids": sorted(r["receipt_id"] for r in matching),
            "prior_revision_delivery_ids": sorted(r["receipt_id"] for r in prior),
            "reports": reports, "unknown_applications": unknown}
