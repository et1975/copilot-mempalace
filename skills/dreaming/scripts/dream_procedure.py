#!/usr/bin/env python3
"""Opt-in drawer-backed procedural commands; JSON stdout, diagnostics stderr."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import dream_palace
from dream_metadata import canonical_json, content_hash, strict_json
from dream_procedural import (
    Policy, ProposalPayload, canonical_rule_id, event_to_data, parse_definition, parse_event,
    project_rules, to_data,
)
from dream_procedural_palace import _scope_check, append_event, read_events
from dream_procedural_validate import (
    AdmissionReader, EvidenceReader, EvidenceUnavailable, SourceInvalid, ValidationLimits,
    build_validation_packet, capture_retained_sources, preflight_event,
)


def now_utc():
    return datetime.now(timezone.utc)


class RequestError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RequestError(message)


def _parser():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("propose", "review", "outcome", "validate", "guidance", "explain",
                 "capture-sources", "status", "draft", "task-guidance", "use-check",
                 "receipt", "receipt-get", "delivery-status"):
        sub = commands.add_parser(name)
        sub.add_argument("--palace", required=True)
        sub.add_argument("--wing", required=True)
        sub.add_argument("--session-store", required=name == "capture-sources",
                         help="Original Copilot SQLite session store; opened read-only for acquisition")
        if name == "receipt":
            sub.add_argument("--input", required=True)
            sub.add_argument("--permissions", help="Fresh permission witness; required for new effects")
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--prepare", action="store_true", help="Fill only missing digest")
            mode.add_argument("--dry-run", action="store_true")
            sub.add_argument("--out", help="Exclusive prepared artifact destination")
        elif name == "receipt-get":
            sub.add_argument("--receipt-id", required=True)
        elif name == "delivery-status":
            sub.add_argument("--context", required=True)
        elif name in {"task-guidance", "use-check"}:
            sub.add_argument("--current", required=True,
                             help="Fresh independent {context, checked_at} observation")
            sub.add_argument("--permissions", required=True, help="Fresh separate permission witness")
            if name == "task-guidance":
                sub.add_argument("--request-id", required=True)
                sub.add_argument("--out", required=True)
            else:
                sub.add_argument("--packet", required=True)
                sub.add_argument("--rule-id", action="append", required=True)
                sub.add_argument("--applicability",
                    help="Fresh explicit condition/exception/constraint assessment; absent means withheld")
        elif name in {"propose", "review", "outcome"}:
            sub.add_argument("--input", required=True)
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true")
            mode.add_argument("--prepare", action="store_true", help="Fill missing digests; no palace writes")
            sub.add_argument("--out", help="Prepared artifact destination (required with --prepare)")
        elif name == "capture-sources":
            sub.add_argument("--dry-run", action="store_true")
        elif name in {"status", "draft"}:
            sub.add_argument("--repository", required=True)
            if name == "draft":
                sub.add_argument("--input", required=True)
                sub.add_argument("--out", required=True)
        elif name == "validate":
            sub.add_argument("--rule-id", required=True)
            sub.add_argument("--contrast-query", required=True)
            sub.add_argument("--out", required=True)
        elif name == "guidance":
            sub.add_argument("--task", required=True)
            sub.add_argument("--repository", required=True)
            sub.add_argument("--include-candidates", action="store_true")
            sub.add_argument("--max-items", type=int, default=5)
            sub.add_argument("--max-chars", type=int, default=6000)
            sub.add_argument("--max-bytes", type=int)
        else:
            sub.add_argument("--rule-id", required=True)
    return parser


def _artifact_path(path, palace):
    target = Path(path).expanduser().resolve()
    if target == Path(palace) or Path(palace) in target.parents:
        raise RequestError("artifacts must be outside the palace")
    return target


def _prepare(data, reader):
    if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
        raise RequestError("expected event envelope and payload")
    payload = data["payload"]
    if data.get("event_kind") == "proposal":
        payload["definition"] = to_data(parse_definition(payload["definition"]))
        data.setdefault("rule_id", canonical_rule_id(payload["definition"]))
    refs = list(payload.get("evidence", []))
    packet = payload.get("validation_packet")
    if packet is not None:
        refs += packet.get("evidence", [])
        refs += [ref for d in payload.get("dispositions", []) for ref in d.get("evidence", [])]
    for ref in refs:
        if "source_hash" not in ref:
            ref["source_hash"] = content_hash(reader.source_text(ref))
    if packet is not None:
        payload.setdefault("validation_digest", content_hash(canonical_json(packet)))
    data.setdefault("digest", content_hash(canonical_json({k: v for k, v in data.items() if k != "digest"})))
    return data


def _write_artifact(path, value):
    # Exclusive creation prevents accidentally replacing the immutable retry artifact.
    with path.open("x", encoding="utf-8") as fh:
        fh.write(canonical_json(value) + "\n")


def _delivery(args, palace, as_of):
    import dream_procedural_delivery as delivery
    current = delivery.read_json(args.current, delivery.MAX_CURRENT_BYTES)
    permission = delivery.read_json(args.permissions, delivery.MAX_PERMISSION_BYTES)
    context = delivery.validate_current(current, as_of)
    if context["wing"] != args.wing:
        raise RequestError("current context wing mismatch")
    applicability = (delivery.read_json(args.applicability, delivery.MAX_APPLICABILITY_BYTES)
                     if getattr(args, "applicability", None) else None)

    def refresh(context, now):
        return delivery.refresh_guidance(palace, context, now)

    def final_check():
        latest_current = delivery.read_json(args.current, delivery.MAX_CURRENT_BYTES)
        latest_permission = delivery.read_json(args.permissions, delivery.MAX_PERMISSION_BYTES)
        latest_applicability = (delivery.read_json(args.applicability, delivery.MAX_APPLICABILITY_BYTES)
                                if applicability is not None else None)
        now = now_utc()
        delivery.validate_current(latest_current, now)
        delivery.consent(context, latest_permission, now)
        if latest_current != current or latest_permission != permission:
            raise RequestError("current context or permission changed during delivery")
        if latest_applicability != applicability:
            raise RequestError("applicability changed during delivery")
        return now

    if args.command == "task-guidance":
        if Path(args.out).expanduser().is_symlink():
            raise RequestError("packet output must be a new regular file, not a symlink")
        target = _artifact_path(args.out, palace)
        result = delivery.build_packet(current, permission, args.request_id, refresh, as_of,
                                       clock=final_check)
        _write_artifact(target, result)
        return result
    packet = delivery.read_json(args.packet, delivery.MAX_PACKET_BYTES)
    try:
        items = delivery.preflight_use(packet, current, permission, args.rule_id, refresh, as_of,
                                       applicability=applicability, clock=final_check)
    except delivery.AdviceWithheld as exc:
        return {"kind": "procedural_use_check", "authority": "agent_reported",
                "status": "withheld", "items": [], "checked_at": to_data(now_utc()), "reason": str(exc)}
    return {"kind": "procedural_use_check", "authority": "agent_reported",
            "status": "usable", "items": items, "checked_at": to_data(now_utc()),
            "notice": "Cooperative current check, not semantic proof or a durable authorization token; recheck before action."}


def _execute(args):
    palace = os.path.realpath(os.path.expanduser(args.palace))
    from dream_procedural_palace import _wing
    _wing(args.wing)
    as_of = now_utc()
    # Offline before any installed-library lazy import (including storage).
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if args.command in {"task-guidance", "use-check"}:
        return _delivery(args, palace, as_of)
    if args.command in {"receipt", "receipt-get", "delivery-status"}:
        return _receipts(args, palace, as_of)
    if args.command == "status":
        from dream_procedural_palace import procedural_status
        return procedural_status(palace, args.wing, args.repository, as_of=as_of)
    if args.command == "draft":
        from dream_procedural_drafts import build_draft, read_receipt
        if Path(args.out).expanduser().is_symlink():
            raise RequestError("draft output must be a new regular file, not a symlink")
        target = _artifact_path(args.out, palace)
        receipt = read_receipt(args.input, args.repository)
        # No host default, source capture, publication or lifecycle mutation.
        dream_palace.procedural_collection(palace)
        result = build_draft(receipt, repository=args.repository,
                             session_store=args.session_store, as_of=as_of,
                             evidence_reader=EvidenceReader(palace, args.wing))
        _write_artifact(target, result)
        return result
    reader = (EvidenceReader(palace, args.wing) if args.command in {"guidance", "explain"}
              else AdmissionReader(palace, args.wing, args.session_store))
    if args.command == "capture-sources":
        report = capture_retained_sources(palace, args.wing, session_store=args.session_store, as_of=as_of)
        if args.dry_run or report["failed"]:
            return report
        if report["pending"] == 0:
            return {**report, "status": "complete"}
        dream_palace.bind_palace(palace)
        from dream_procedural_palace import local_embedder
        local_embedder()
        writer = dream_palace.MempalaceWriter()
        with writer.mutation():
            return capture_retained_sources(palace, args.wing, session_store=args.session_store,
                                             as_of=now_utc(), writer=writer)
    event = None
    if args.command in {"propose", "review", "outcome"}:
        if bool(args.prepare) != bool(args.out):
            raise RequestError("--prepare requires --out; --out is only for preparation")
        try:
            data = strict_json(Path(args.input).read_text(encoding="utf-8"))
            event = parse_event(_prepare(data, reader) if args.prepare else data)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RequestError(f"invalid event artifact: {exc}") from exc
        expected = "proposal" if args.command == "propose" else args.command
        if event.event_kind != expected:
            raise RequestError("artifact event_kind does not match command")
    try:
        events = read_events(palace, args.wing)
        projection = project_rules(events, as_of=as_of, policy=Policy())
    except ValueError as exc:
        raise RuntimeError(f"procedural storage integrity: {exc}") from exc
    if projection.errors:
        raise RuntimeError(f"procedural projection integrity: {to_data(projection.errors)}")
    if event is not None:
        _scope_check(event, events)
        prior = [e for e in events if e.event_id == event.event_id]
        if prior:
            if any(e.digest != event.digest for e in prior):
                raise RuntimeError("event ID already has a different digest")
            return {"status": "already_exists", "event_id": event.event_id,
                    "projection": to_data(projection)}
        if args.prepare or args.dry_run:
            preflight_event(event, projection=projection, evidence_reader=reader, as_of=as_of)
            reader.preflight_captures(at=as_of, actor=event.session_id)
        if args.prepare:
            _write_artifact(_artifact_path(args.out, palace), event_to_data(event))
            return {"status": "prepared", "event_id": event.event_id, "out": args.out}
        if args.dry_run:
            return {"status": "dry_run", "event_id": event.event_id, "projection": to_data(projection)}
        dream_palace.bind_palace(palace)
        from dream_procedural_palace import local_embedder
        local_embedder()
        writer = dream_palace.MempalaceWriter()
        with writer.mutation():
            result = append_event(palace, args.wing, event, writer=writer,
                                  clock=now_utc, session_store=args.session_store)
        return to_data(result)
    state = next((s for s in projection.rules if s.rule_id == getattr(args, "rule_id", None)), None)
    if args.command == "validate":
        if state is None or state.definition is None:
            raise RequestError("unknown rule definition")
        packet = build_validation_packet(state.definition,
            queries=[state.definition.statement, args.contrast_query],
            source_reader=lambda query, limit: reader.search(args.wing, query, limit, as_of=as_of),
            limits=ValidationLimits(), as_of=as_of)
        data = to_data(packet)
        result = {"validation_packet": data, "validation_digest": content_hash(canonical_json(data)),
                  "notice": "No counterexample found means none within this bounded search, not none exist."}
        _write_artifact(_artifact_path(args.out, palace), result)
        return result
    from dream_procedural import repository_key
    from dream_procedural_palace import (
        GuidanceLimits, embed_texts, explain_rule, get_task_guidance, revalidate_sources,
    )
    repository = repository_key(args.repository) if args.command == "guidance" else None
    projection, diagnostics = revalidate_sources(projection, evidence_reader=reader,
                                                 as_of=as_of, repository=repository)
    if args.command == "explain":
        return explain_rule(projection, args.rule_id, as_of=as_of, source_diagnostics=diagnostics)
    result = get_task_guidance(projection, task=args.task, repository=repository,
        embedder=lambda texts: embed_texts(dream_palace.procedural_collection(palace), texts),
        limits=GuidanceLimits(args.max_items, args.max_chars, max_bytes=args.max_bytes), as_of=as_of,
        include_candidates=args.include_candidates)
    return result.data


def _receipts(args, palace, as_of):
    import dream_procedural_receipts as receipts
    from dream_procedural_delivery import MAX_CONTEXT_BYTES, MAX_PERMISSION_BYTES, read_json
    if args.command == "delivery-status":
        return receipts.delivery_status(palace, args.wing,
            read_json(args.context, MAX_CONTEXT_BYTES + 1), now=as_of)
    if args.command == "receipt-get":
        receipts.receipt_id(args.receipt_id)
        records = receipts.read_receipts(palace, args.wing, now=as_of)
        if args.receipt_id not in records:
            raise RequestError("receipt not found")
        return {"status": "ok", "receipt": records[args.receipt_id], "notice": receipts.NOTICE}
    if bool(args.prepare) != bool(args.out):
        raise RequestError("--prepare requires --out; --out is only for preparation")
    record = read_json(args.input, receipts.MAX_RECORD_BYTES)

    def permissions():
        if not args.permissions:
            raise RequestError("new receipt requires current --permissions")
        return read_json(args.permissions, MAX_PERMISSION_BYTES)

    if not args.prepare and not args.dry_run:
        return receipts.append_receipt(palace, args.wing, record, permissions=permissions, clock=now_utc)
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        raise RequestError("expected receipt envelope and payload")
    records, count = receipts._read_receipts(palace, args.wing, as_of, True)
    parent = records.get(record["payload"].get("delivery_receipt_id"))
    if args.prepare:
        record = receipts.prepare_receipt(record, as_of, parent=parent, wing=args.wing)
    else:
        receipts.validate_receipt(record, as_of, parent=parent, wing=args.wing)
    if receipts._prior(records, record):
        return receipts._result("already_exists", record)
    if count >= receipts.MAX_RECEIPTS:
        raise RequestError("receipt limit exceeded")
    receipts.receipt_preflight(record, records, args.wing, permissions(), now_utc())
    if args.prepare:
        if Path(args.out).expanduser().is_symlink():
            raise RequestError("receipt output must be a new regular file, not a symlink")
        _write_artifact(_artifact_path(args.out, palace), record)
    return receipts._result("prepared" if args.prepare else "dry_run", record)


def main(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
        from dream_procedural_palace import nonmutating_read
        read_only = args.command in {"validate", "guidance", "explain", "status", "draft",
                                     "task-guidance", "use-check", "receipt-get", "delivery-status"} or \
            getattr(args, "dry_run", False) or getattr(args, "prepare", False)
        # Imported handlers/models may print; keep the command's stdout strictly JSON.
        with redirect_stdout(sys.stderr), (nonmutating_read(args.palace) if read_only else nullcontext()):
            result = _execute(args)
        for warning in result.get("warnings", ()):
            print(warning, file=sys.stderr)
        print(canonical_json(result))
        return 1 if result.get("status") in {"evidence_unavailable", "blocked"} else 0
    except SourceInvalid as exc:
        print(canonical_json({"status": "error", "kind": "evidence_integrity",
                              "code": exc.code, "error": str(exc)}))
        print(f"evidence_integrity: {exc}", file=sys.stderr)
        return 1
    except (ValueError, TypeError) as exc:
        print(canonical_json({"status": "error", "kind": "invalid_request",
                              "code": getattr(exc, "code", "invalid_request"), "error": str(exc)}))
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        kind = "evidence_unavailable" if isinstance(exc, EvidenceUnavailable) else "storage_integrity"
        print(canonical_json({"status": "error", "kind": kind,
                              "code": getattr(exc, "code", kind), "error": str(exc)}))
        print(f"{kind}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
