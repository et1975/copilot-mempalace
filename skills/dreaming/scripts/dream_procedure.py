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
    EvidenceReader, EvidenceUnavailable, ValidationLimits, build_validation_packet, preflight_event,
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
    for name in ("propose", "review", "outcome", "validate", "guidance", "explain"):
        sub = commands.add_parser(name)
        sub.add_argument("--palace", required=True)
        sub.add_argument("--wing", required=True)
        sub.add_argument("--session-store", help="Original Copilot SQLite session store; opened read-only")
        if name in {"propose", "review", "outcome"}:
            sub.add_argument("--input", required=True)
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true")
            mode.add_argument("--prepare", action="store_true", help="Fill missing digests; no palace writes")
            sub.add_argument("--out", help="Prepared artifact destination (required with --prepare)")
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


def _execute(args):
    palace = os.path.realpath(os.path.expanduser(args.palace))
    from dream_procedural_palace import _wing
    _wing(args.wing)
    as_of = now_utc()
    # Offline before any installed-library lazy import (including storage).
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    reader = EvidenceReader(palace, args.session_store)
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
        preflight_event(event, projection=projection, evidence_reader=reader, as_of=as_of)
        if args.prepare:
            _write_artifact(_artifact_path(args.out, palace), event_to_data(event))
            return {"status": "prepared", "event_id": event.event_id, "out": args.out}
        if args.dry_run:
            return {"status": "dry_run", "event_id": event.event_id, "projection": to_data(projection)}
        dream_palace.bind_palace(palace)
        from dream_procedural_palace import local_embedder
        local_embedder()
        result = append_event(palace, args.wing, event, writer=dream_palace.MempalaceWriter(),
            clock=now_utc,
            preflight=lambda ev, live: preflight_event(
                ev, projection=live, evidence_reader=reader, as_of=now_utc()))
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
        limits=GuidanceLimits(args.max_items, args.max_chars), as_of=as_of,
        include_candidates=args.include_candidates)
    return result.data


def main(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
        from dream_procedural_palace import nonmutating_read
        read_only = args.command in {"validate", "guidance", "explain"} or \
            getattr(args, "dry_run", False) or getattr(args, "prepare", False)
        # Imported handlers/models may print; keep the command's stdout strictly JSON.
        with redirect_stdout(sys.stderr), (nonmutating_read(args.palace) if read_only else nullcontext()):
            result = _execute(args)
        print(canonical_json(result))
        return 1 if result.get("status") == "evidence_unavailable" else 0
    except (ValueError, TypeError) as exc:
        print(canonical_json({"status": "error", "kind": "invalid_request", "error": str(exc)}))
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        kind = "evidence_unavailable" if isinstance(exc, EvidenceUnavailable) else "storage_integrity"
        print(canonical_json({"status": "error", "kind": kind, "error": str(exc)}))
        print(f"{kind}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
