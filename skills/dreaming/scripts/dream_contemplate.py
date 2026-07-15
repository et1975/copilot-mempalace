#!/usr/bin/env python3
"""One-shot contemplate reconnaissance driver.

Runs the read-only derive/contemplate harvest in one process by calling the
shared dreaming functions directly. Optional ``--bootstrap`` writes only
disabled ontology rule candidates for later human review; it never materializes
KG facts and never enables rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import dream_acquire
import dream_ontology
import dream_palace
from dream_lib import (
    build_contemplate_worklist,
    deductive_closure,
    enabled_rules,
    filter_skipped,
    ontology_version,
)


def _default_palace() -> str | None:
    config_path = os.environ.get("MEMPALACE_CONFIG") or os.path.expanduser("~/.mempalace/config.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, encoding="utf-8") as fh:
            palace_path = json.load(fh).get("palace_path")
    except (OSError, json.JSONDecodeError):
        return None
    return os.path.expanduser(palace_path) if palace_path else None


# ---------------------------------------------------------------------------
# Pure reporting helpers (no palace access)
# ---------------------------------------------------------------------------
def _candidate_example(item: dict) -> str:
    conclusion = item.get("conclusion") or {}
    proof = item.get("proof") or {}
    subject = conclusion.get("subject") if conclusion.get("subject") is not None else conclusion.get("subject_id")
    predicate = conclusion.get("predicate")
    obj = conclusion.get("object") if conclusion.get("object") is not None else conclusion.get("object_id")
    return f"{subject} -{predicate}-> {obj} (depth {proof.get('depth')})"


def _bootstrap_rules_summary(bootstrap: dict | None) -> dict | None:
    if not bootstrap:
        return None
    proposed = []
    for rule in bootstrap.get("proposed_disabled_rules") or []:
        proposed.append({
            "id": rule.get("id"),
            "family": rule.get("family"),
            "enabled": bool(rule.get("enabled", False)),
            "rationale": rule.get("rationale"),
        })
    return {
        "target": bootstrap.get("target"),
        "stats": dict(bootstrap.get("stats") or {}),
        "proposed_disabled_rules": proposed,
    }


def build_report(
    *,
    palace: str,
    rules_path: str,
    enabled_rule_count: int,
    worklist: dict,
    ontology_rules: list[dict],
    kg_path: str | None = None,
    triple_count: int | None = None,
    bootstrap: dict | None = None,
    example_limit: int = 5,
) -> dict:
    items = list((worklist or {}).get("items") or [])
    examples = [_candidate_example(item) for item in items[:example_limit]]
    ontology_rule_count = len(ontology_rules or [])
    empty_ontology = ontology_rule_count == 0
    no_enabled_rules = enabled_rule_count == 0
    report = {
        "palace": palace,
        "kg_path": kg_path,
        "rules_path": rules_path,
        "ontology_rule_count": ontology_rule_count,
        "enabled_rule_count": enabled_rule_count,
        "triple_count": triple_count,
        "ontology_version": (worklist or {}).get("ontology_version"),
        "derive_candidate_count": len(items),
        "examples": examples,
        "truncated": len(items) > example_limit or any(bool(item.get("truncated")) for item in items),
        "empty_ontology": empty_ontology,
        "messages": [],
    }
    if no_enabled_rules:
        ontology_state = "ontology is empty" if empty_ontology else "ontology has no enabled rules"
        report["messages"].append(
            f"0 enabled rules; {ontology_state}, so 0 derivations. "
            "This is intentional: predicate names are not predicate semantics."
        )
        if bootstrap:
            report["messages"].append(
                "bootstrap proposed disabled rule candidates only; review them before enabling any rule"
            )
        else:
            report["messages"].append("0 enabled rules; run with --bootstrap to propose disabled candidates")
    bootstrap_summary = _bootstrap_rules_summary(bootstrap)
    if bootstrap_summary is not None:
        report["bootstrap"] = bootstrap_summary
    return report


def summarize_report(report: dict) -> str:
    lines = [
        f"palace: {report.get('palace')}",
        f"kg: {report.get('kg_path')}",
        f"rules: {report.get('enabled_rule_count')} enabled / {report.get('ontology_rule_count')} total ({report.get('rules_path')})",
        f"active triples: {report.get('triple_count')}",
        f"derive candidates: {report.get('derive_candidate_count')}",
        f"truncated: {bool(report.get('truncated'))}",
    ]
    for message in report.get("messages") or []:
        lines.append(f"note: {message}")
    examples = report.get("examples") or []
    if examples:
        lines.append("examples:")
        for example in examples:
            lines.append(f"  - {example}")
    bootstrap = report.get("bootstrap")
    if bootstrap:
        stats = bootstrap.get("stats") or {}
        lines.extend([
            f"bootstrap target: {bootstrap.get('target')}",
            "bootstrap proposed disabled rules: "
            f"{len(bootstrap.get('proposed_disabled_rules') or [])} "
            f"(added {stats.get('added', 0)}, skipped {stats.get('skipped_existing', 0)})",
        ])
        for rule in bootstrap.get("proposed_disabled_rules") or []:
            lines.append(f"  - {rule.get('id')} [{rule.get('family')}] {rule.get('rationale')}")
    return "\n".join(lines)


def _recall_snippet(text: str, limit: int = 100) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def build_recall_report(query: str, k: int, palace: str, hits: list[dict]) -> dict:
    sanitized_hits = []
    for hit in hits or []:
        sanitized_hits.append({
            "session_id": hit.get("session_id"),
            "similarity": hit.get("similarity"),
            "topic": hit.get("topic"),
            "date": hit.get("date"),
            "text": hit.get("text"),
        })
    return {
        "query": query,
        "k": k,
        "palace": palace,
        "count": len(sanitized_hits),
        "hits": sanitized_hits,
    }


def summarize_recall_report(report: dict) -> str:
    lines = [
        f"recall query: {report.get('query')}",
        f"k: {report.get('k')}",
        f"palace: {report.get('palace')}",
    ]
    hits = report.get("hits") or []
    if not hits:
        lines.append("no relevant sessions found")
        return "\n".join(lines)
    for hit in hits:
        session_id = str(hit.get("session_id") or "")
        label = hit.get("topic") or hit.get("date") or ""
        similarity = hit.get("similarity")
        try:
            similarity_text = f"{float(similarity):.4f}"
        except (TypeError, ValueError):
            similarity_text = "nan"
        lines.append(f"  - {similarity_text} [{session_id[:8]}] {label}: {_recall_snippet(hit.get('text'))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Palace-facing orchestration
# ---------------------------------------------------------------------------
def derive_worklist(
    palace: str,
    *,
    rules_path: str,
    skips_path: str,
    max_depth: int = 3,
    max_iterations: int = 10,
    max_candidates: int = 500,
) -> tuple[dict, list[dict], list[dict]]:
    rules = dream_palace.load_ontology_config(rules_path)
    onto_ver = ontology_version(rules)
    triples = dream_palace.load_premises(palace, purpose="durable")
    candidates = deductive_closure(
        triples,
        rules,
        max_depth=max_depth,
        max_iterations=max_iterations,
        max_candidates=max_candidates,
    )
    candidates = filter_skipped(candidates, dream_palace.load_skip_markers(skips_path), onto_ver)
    for candidate in candidates:
        candidate["ontology_version"] = onto_ver
    worklist = build_contemplate_worklist(
        candidates,
        scope={"palace": palace},
        rules=rules,
        onto_version=onto_ver,
        params={
            "max_depth": max_depth,
            "max_iterations": max_iterations,
            "max_candidates": max_candidates,
        },
    )
    return worklist, rules, triples


def bootstrap_ontology(
    *,
    triples: list[dict],
    ontology_path: str,
    min_support: int,
) -> dict:
    existing = dream_ontology.read_ontology_doc(ontology_path)
    existing_rules = existing.get("rules", [])
    predicates = sorted({
        triple.get("predicate") for triple in triples
        if isinstance(triple.get("predicate"), str) and triple.get("predicate")
    })
    candidates = []
    candidates.extend(dream_ontology.suggest_rules_from_predicates(predicates))
    base = dream_ontology.filter_base_triples(triples, existing_rules)
    candidates.extend(dream_ontology.induce_rules_from_triples(base, min_support=min_support))
    merged_candidates, _candidate_stats = dream_ontology.merge_ontology_candidates([], candidates)
    merged, stats = dream_ontology.merge_ontology_candidates(existing_rules, merged_candidates)
    doc = dream_ontology.build_ontology_doc(merged, existing.get("version", 1))
    dream_ontology.write_ontology_doc(ontology_path, doc)
    return {
        "target": ontology_path,
        "stats": stats,
        "proposed_disabled_rules": merged_candidates,
    }


def run(
    palace: str,
    *,
    rules_path: str | None = None,
    skips_path: str | None = None,
    min_support: int = 2,
    bootstrap: bool = False,
    ontology_out: str | None = None,
    max_depth: int = 3,
    max_iterations: int = 10,
    max_candidates: int = 500,
) -> dict:
    path = dream_palace.bind_palace(palace)
    kg_path = dream_palace._resolve_kg_path(path)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    effective_skips_path = skips_path or os.path.join(path, "dream-derive-skips.jsonl")
    worklist, rules, triples = derive_worklist(
        path,
        rules_path=effective_rules_path,
        skips_path=effective_skips_path,
        max_depth=max_depth,
        max_iterations=max_iterations,
        max_candidates=max_candidates,
    )
    bootstrap_report = None
    if bootstrap:
        bootstrap_target = os.path.abspath(os.path.expanduser(ontology_out or os.path.join(path, "ontology.json")))
        bootstrap_report = bootstrap_ontology(
            triples=triples,
            ontology_path=bootstrap_target,
            min_support=min_support,
        )
    return build_report(
        palace=path,
        kg_path=kg_path,
        rules_path=effective_rules_path,
        enabled_rule_count=len(enabled_rules(rules)),
        worklist=worklist,
        ontology_rules=rules,
        triple_count=len(triples),
        bootstrap=bootstrap_report,
    )


def run_recall(
    palace: str,
    query: str,
    *,
    k: int = 5,
    repository: str | None = None,
    since: str | None = None,
    limit_sessions: int | None = None,
    min_similarity: float = 0.0,
) -> dict:
    path = dream_palace.bind_palace(palace)
    hits = dream_palace.retrieve_relevant_session_observations(
        path,
        query,
        k=k,
        repository=repository,
        since=since,
        limit_sessions=limit_sessions,
        min_similarity=min_similarity,
    )
    return build_recall_report(query, k, path, hits)


def run_acquire(
    palace: str,
    *,
    query: dict[str, str],
    rules_path: str | None,
    run_id: str | None,
    budgets: dict | None,
    recall_file: str | None,
    extractor_mode: str,
    oracle_file: str | None,
    trusted_speakers: list[str] | set[str] | None,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    rules = dream_palace.load_ontology_config(effective_rules_path)

    recall_fn = None
    if recall_file:
        with open(recall_file, encoding="utf-8") as fh:
            recall_sources = json.load(fh)
        if not isinstance(recall_sources, list):
            raise ValueError("--recall-file must contain a JSON list")
        recall_sources = [dict(item) for item in recall_sources if isinstance(item, dict)]

        def recall_fn(_query_text, _gap):
            return [dict(item) for item in recall_sources]

    if extractor_mode == "heuristic":
        extractor_fn = dream_acquire.heuristic_support_extractor
    elif extractor_mode == "oracle":
        if not oracle_file:
            raise ValueError("--extractor oracle requires --oracle-file")
        extractor_fn = _oracle_extractor(oracle_file)
    else:
        raise ValueError(f"unsupported extractor mode: {extractor_mode}")

    result = dream_acquire.acquire_loop(
        palace_path=path,
        query=query,
        rules=rules,
        extractor_fn=extractor_fn,
        recall_fn=recall_fn,
        run_id=run_id or str(uuid.uuid4()),
        budgets=budgets,
        trusted_speakers=set(trusted_speakers or []),
        now=now,
    )
    report = dict(result)
    report.update(
        {
            "palace": path,
            "rules_path": effective_rules_path,
            "query": dict(query),
            "enabled_rule_count": len(enabled_rules(rules)),
        }
    )
    return report


def run_acquire_start(
    palace: str,
    *,
    query: dict[str, str],
    rules_path: str | None,
    run_id: str | None,
    budgets: dict | None,
    recall_file: str | None,
    trusted_speakers: list[str] | set[str] | None,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    rules = dream_palace.load_ontology_config(effective_rules_path)
    result = dream_acquire.acquire_start(
        palace_path=path,
        query=query,
        rules=rules,
        recall_fn=_recall_file_fn(recall_file),
        run_id=run_id or str(uuid.uuid4()),
        budgets=budgets,
        trusted_speakers=set(trusted_speakers or []),
        now=now,
    )
    return result


def run_acquire_resume(
    palace: str,
    *,
    run_id: str,
    verdict_file: str,
    rules_path: str | None,
    budgets: dict | None = None,
    recall_file: str | None = None,
    now=None,
) -> dict:
    del budgets
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    rules = dream_palace.load_ontology_config(effective_rules_path)
    with open(verdict_file, encoding="utf-8") as fh:
        verdict = json.load(fh)
    if not isinstance(verdict, dict):
        raise ValueError("--verdict-file must contain a JSON object")
    return dream_acquire.acquire_resume(
        palace_path=path,
        run_id=run_id,
        verdict=verdict,
        rules=rules,
        recall_fn=_recall_file_fn(recall_file),
        now=now,
    )


def _recall_file_fn(recall_file: str | None):
    if not recall_file:
        return None
    with open(recall_file, encoding="utf-8") as fh:
        recall_sources = json.load(fh)
    if not isinstance(recall_sources, list):
        raise ValueError("--recall-file must contain a JSON list")
    recall_sources = [dict(item) for item in recall_sources if isinstance(item, dict)]

    def recall_fn(_query_text, _gap):
        return [dict(item) for item in recall_sources]

    return recall_fn


def _oracle_extractor(oracle_file: str):
    with open(oracle_file, encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, list):
        responses = [dict(item) for item in payload if isinstance(item, dict)]
        index = {"value": 0}

        def extractor(_prompt_payload):
            if index["value"] >= len(responses):
                return {"verdict": "not_addressed"}
            response = dict(responses[index["value"]])
            index["value"] += 1
            return response

        return extractor
    if isinstance(payload, dict):
        response = dict(payload)
        return lambda _prompt_payload: dict(response)
    raise ValueError("--oracle-file must contain a JSON object or list")


def summarize_acquire_report(report: dict) -> str:
    answer = report.get("answer") or {}
    confidence = report.get("confidence") or {}
    budgets = report.get("budgets") or {}
    lines = [
        f"status: {report.get('status')}",
        "answer: "
        f"{answer.get('value')} ({answer.get('epistemic_status')})",
        f"confidence: {confidence.get('level')} - {confidence.get('rationale')}",
        f"acquired: {len(report.get('acquired') or [])}",
    ]
    for item in report.get("acquired") or []:
        lines.append(
            "  - "
            f"{item.get('gap_key')} -> {item.get('provisional_id')} "
            f"source_ref={item.get('source_ref')}"
        )
    unfilled = report.get("unfilled_gaps") or []
    lines.append(f"unfilled_gaps: {len(unfilled)}")
    for gap in unfilled:
        lines.append(f"  - {gap.get('gap_key')} reason={gap.get('reason')} duc={gap.get('duc')}")
    lines.append(
        "budgets: "
        f"iterations {budgets.get('iterations_used')}/{budgets.get('max_iterations')}, "
        f"acquisitions {budgets.get('acquisitions_used')}/{budgets.get('max_acquisitions')}, "
        f"tool_calls {budgets.get('tool_calls_used')}/{budgets.get('max_tool_calls')}"
    )
    return "\n".join(lines)


def summarize_step_result(result: dict) -> str:
    lines = [
        f"status: {result.get('status')}",
        f"run_id: {result.get('run_id')}",
    ]
    pending = result.get("pending")
    if pending:
        target = pending.get("target") or {}
        source = pending.get("source") or {}
        content = source.get("content") or ""
        lines.extend(
            [
                f"pending_request_id: {pending.get('request_id')}",
                f"pending_gap: {pending.get('gap_key')}",
                "target: "
                f"{target.get('subject_id')} -{target.get('predicate')}-> {target.get('object_id')}",
                f"source_type: {source.get('source_type')} trust_domain={source.get('trust_domain')}",
                f"source_locator: {json.dumps(source.get('locator') or {}, sort_keys=True)}",
                f"source_excerpt: {_recall_snippet(content, limit=160)}",
                f"instruction: {pending.get('instruction')}",
            ]
        )
    answer = result.get("answer") or {}
    if answer:
        confidence = result.get("confidence") or {}
        lines.extend(
            [
                "answer: "
                f"{answer.get('value')} ({answer.get('epistemic_status')})",
                f"confidence: {confidence.get('level')} - {confidence.get('rationale')}",
            ]
        )
    lines.append(f"acquired: {len(result.get('acquired') or [])}")
    for item in result.get("acquired") or []:
        lines.append(
            "  - "
            f"{item.get('gap_key')} -> {item.get('provisional_id')} "
            f"source_ref={item.get('source_ref')}"
        )
    unfilled = result.get("unfilled_gaps") or []
    lines.append(f"unfilled_gaps: {len(unfilled)}")
    for gap in unfilled:
        lines.append(f"  - {gap.get('gap_key')} reason={gap.get('reason')} duc={gap.get('duc')}")
    budgets = result.get("budgets") or {}
    lines.append(
        "budgets: "
        f"iterations {budgets.get('iterations_used')}/{budgets.get('max_iterations')}, "
        f"acquisitions {budgets.get('acquisitions_used')}/{budgets.get('max_acquisitions')}, "
        f"tool_calls {budgets.get('tool_calls_used')}/{budgets.get('max_tool_calls')}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", help="Path to the mempalace palace directory (default: mempalace config)")
    ap.add_argument("--rules", default=None, help="Path to ontology config (default: <palace>/ontology.json)")
    ap.add_argument("--min-support", type=int, default=2, help="Minimum support for induced bootstrap rules (default 2)")
    ap.add_argument("--bootstrap", action="store_true", help="Write disabled ontology candidates for review")
    ap.add_argument(
        "--out",
        default=None,
        help="Ontology output path for --bootstrap (default: <palace>/ontology.json)",
    )
    ap.add_argument("--format", choices=["summary", "json"], default="summary", help="Output format (default summary)")
    ap.add_argument("--recall", default=None, help="Reasoning query for relevance-ranked past session recall")
    ap.add_argument("--k", type=int, default=5, help="Maximum relevant sessions to return for --recall (default 5)")
    ap.add_argument("--repository", default=None, help="Repository filter passed through to --recall retrieval")
    ap.add_argument("--since", default=None, help="Lower date/time bound passed through to --recall retrieval")
    ap.add_argument("--limit-sessions", type=int, default=None, help="Maximum sessions to inspect for --recall")
    ap.add_argument("--min-similarity", type=float, default=0.0, help="Minimum similarity for --recall (default 0.0)")
    ap.add_argument("--acquire", action="store_true", help="Run the ACQUIRE loop instead of derive/recall")
    ap.add_argument("--acquire-start", action="store_true", help="Start a resumable ACQUIRE loop and pause for F8")
    ap.add_argument("--acquire-resume", action="store_true", help="Resume a paused ACQUIRE loop with --verdict-file")
    ap.add_argument("--subject", default=None, help="Reachability query subject entity id for ACQUIRE")
    ap.add_argument("--predicate", default=None, help="Reachability query base predicate for ACQUIRE")
    ap.add_argument("--object", dest="object_id", default=None, help="Reachability query object entity id for ACQUIRE")
    ap.add_argument("--run-id", default=None, help="Controlled run id for ACQUIRE (default: generated UUID)")
    ap.add_argument("--max-acquisitions", type=int, default=5, help="Maximum ACQUIRE provisional assertions (default 5)")
    ap.add_argument("--max-tool-calls", type=int, default=20, help="Maximum ACQUIRE recall calls (default 20)")
    ap.add_argument("--recall-file", default=None, help="JSON list of UntrustedSource dicts for deterministic --acquire recall")
    ap.add_argument("--extractor", choices=["heuristic", "oracle"], default="heuristic", help="F8 extractor for --acquire")
    ap.add_argument("--oracle-file", default=None, help="JSON oracle extractor response(s) for --extractor oracle")
    ap.add_argument("--verdict-file", default=None, help="JSON agent F8 verdict for --acquire-resume")
    ap.add_argument("--trusted-speaker", action="append", default=[], help="Trusted speaker name for F8 promotion")
    ap.add_argument("--skips", default=None, help="Path to skip-markers file (default: <palace>/dream-derive-skips.jsonl)")
    ap.add_argument("--max-depth", type=int, default=3, help="Maximum derivation depth (default 3)")
    ap.add_argument("--max-iterations", type=int, default=10, help="Maximum closure iterations (default 10)")
    ap.add_argument("--max-candidates", type=int, default=500, help="Maximum derive candidates (default 500)")
    args = ap.parse_args(argv)

    effective_palace = args.palace or _default_palace()
    if effective_palace is None:
        config_path = os.environ.get("MEMPALACE_CONFIG") or "~/.mempalace/config.json"
        print(f"error: no --palace given and {config_path} has no palace_path", file=sys.stderr)
        return 2

    if args.recall is not None:
        report = run_recall(
            effective_palace,
            args.recall,
            k=args.k,
            repository=args.repository,
            since=args.since,
            limit_sessions=args.limit_sessions,
            min_similarity=args.min_similarity,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_recall_report(report))
        return 0

    acquire_modes = [args.acquire, args.acquire_start, args.acquire_resume]
    if sum(1 for enabled in acquire_modes if enabled) > 1:
        print("error: choose only one of --acquire, --acquire-start, --acquire-resume", file=sys.stderr)
        return 2

    if args.acquire:
        missing = [
            name
            for name, value in (
                ("--subject", args.subject),
                ("--predicate", args.predicate),
                ("--object", args.object_id),
            )
            if not value
        ]
        if missing:
            print(f"error: --acquire requires {', '.join(missing)}", file=sys.stderr)
            return 2
        if args.extractor == "oracle" and not args.oracle_file:
            print("error: --extractor oracle requires --oracle-file", file=sys.stderr)
            return 2
        report = run_acquire(
            effective_palace,
            query={
                "subject_id": args.subject,
                "base_predicate": args.predicate,
                "object_id": args.object_id,
            },
            rules_path=args.rules,
            run_id=args.run_id,
            budgets={
                "max_iterations": args.max_iterations,
                "max_acquisitions": args.max_acquisitions,
                "max_tool_calls": args.max_tool_calls,
            },
            recall_file=args.recall_file,
            extractor_mode=args.extractor,
            oracle_file=args.oracle_file,
            trusted_speakers=args.trusted_speaker,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_acquire_report(report))
        return 0

    if args.acquire_start:
        missing = [
            name
            for name, value in (
                ("--subject", args.subject),
                ("--predicate", args.predicate),
                ("--object", args.object_id),
            )
            if not value
        ]
        if missing:
            print(f"error: --acquire-start requires {', '.join(missing)}", file=sys.stderr)
            return 2
        report = run_acquire_start(
            effective_palace,
            query={
                "subject_id": args.subject,
                "base_predicate": args.predicate,
                "object_id": args.object_id,
            },
            rules_path=args.rules,
            run_id=args.run_id,
            budgets={
                "max_iterations": args.max_iterations,
                "max_acquisitions": args.max_acquisitions,
                "max_tool_calls": args.max_tool_calls,
            },
            recall_file=args.recall_file,
            trusted_speakers=args.trusted_speaker,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_step_result(report))
        return 0

    if args.acquire_resume:
        missing = [
            name
            for name, value in (
                ("--run-id", args.run_id),
                ("--verdict-file", args.verdict_file),
            )
            if not value
        ]
        if missing:
            print(f"error: --acquire-resume requires {', '.join(missing)}", file=sys.stderr)
            return 2
        report = run_acquire_resume(
            effective_palace,
            run_id=args.run_id,
            verdict_file=args.verdict_file,
            rules_path=args.rules,
            budgets={
                "max_iterations": args.max_iterations,
                "max_acquisitions": args.max_acquisitions,
                "max_tool_calls": args.max_tool_calls,
            },
            recall_file=args.recall_file,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_step_result(report))
        return 0

    report = run(
        effective_palace,
        rules_path=args.rules,
        skips_path=args.skips,
        min_support=args.min_support,
        bootstrap=args.bootstrap,
        ontology_out=args.out,
        max_depth=args.max_depth,
        max_iterations=args.max_iterations,
        max_candidates=args.max_candidates,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(summarize_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
