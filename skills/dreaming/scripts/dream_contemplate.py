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

import dream_insight
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


def _ontology_rules_doc(doc: dict | list) -> tuple[list[dict], int]:
    if isinstance(doc, dict):
        return list(doc.get("rules") or []), int(doc.get("version", 1) or 1)
    if isinstance(doc, list):
        return list(doc), 1
    return [], 1


def _candidate_rules_from_triples(triples: list[dict], current_rules: list[dict], min_support: int) -> list[dict]:
    predicates = sorted({
        triple.get("predicate") for triple in triples
        if isinstance(triple.get("predicate"), str) and triple.get("predicate")
    })
    candidates = []
    candidates.extend(dream_ontology.suggest_rules_from_predicates(predicates))
    base = dream_ontology.filter_base_triples(triples, current_rules)
    candidates.extend(dream_ontology.induce_rules_from_triples(base, min_support=min_support))
    merged_candidates, _candidate_stats = dream_ontology.merge_ontology_candidates([], candidates)
    return merged_candidates


def _preview_candidate_derivations(triples, candidate_rule, *, limit=3) -> list[str]:
    """Return up to ``limit`` plain-language example conclusions this one candidate rule would derive."""
    try:
        max_depth = int(candidate_rule.get("max_depth", 8) or 8)
        candidates = deductive_closure(
            triples,
            [{**candidate_rule, "enabled": True}],
            max_depth=max_depth,
            max_iterations=50,
            max_candidates=max(limit, 10),
        )
        previews = []
        for candidate in candidates:
            conclusion = candidate.get("conclusion") or {}
            subject = conclusion.get("subject") or conclusion.get("subject_id")
            predicate = conclusion.get("predicate")
            obj = conclusion.get("object") or conclusion.get("object_id")
            if subject is None or predicate is None or obj is None:
                continue
            previews.append(f"{subject} {predicate} {obj}")
            if len(previews) >= limit:
                break
        return previews
    except Exception:
        return []


def _plain_evidence_text(evidence) -> str:
    if isinstance(evidence, (list, tuple)):
        text = ", ".join(str(item) for item in evidence)
    else:
        text = str(evidence or "")
    replacements = {
        "transitivity": "chain behavior",
        "transitive": "chain",
        "symmetry": "two-way behavior",
        "symmetric": "two-way",
        "inverse": "paired",
        "family": "kind",
    }
    for old, new in replacements.items():
        text = text.replace(old, new).replace(old.capitalize(), new.capitalize())
    return text


def propose_rules(palace, *, rules_path=None, min_support=2) -> dict:
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    triples = dream_palace.load_premises(path, purpose="durable")
    current_rules = dream_palace.load_ontology_config(effective_rules_path)
    already_enabled = sorted(
        str(rule.get("id"))
        for rule in current_rules
        if rule.get("id") and bool(rule.get("enabled", False))
    )
    enabled_ids = set(already_enabled)

    candidates = _candidate_rules_from_triples(triples, current_rules, min_support)
    proposals = []
    for candidate in candidates:
        rule_id = candidate.get("id")
        if rule_id in enabled_ids:
            continue
        described = dream_ontology.describe_rule_candidate(candidate)
        preview = _preview_candidate_derivations(triples, candidate)
        proposals.append({
            **described,
            "would_derive_now": len(preview) > 0,
            "example_derivations": preview,
            "accept_command": f"--enable-rule {rule_id}",
        })

    return {
        "palace": path,
        "rules_path": effective_rules_path,
        "triple_count": len(triples),
        "proposals": proposals,
        "already_enabled": already_enabled,
    }


def summarize_proposals(report) -> str:
    lines = [
        f"palace: {report.get('palace')}",
        f"rules: {report.get('rules_path')}",
        f"active triples: {report.get('triple_count')}",
    ]
    already_enabled = report.get("already_enabled") or []
    if already_enabled:
        lines.append(f"already enabled: {len(already_enabled)}")

    proposals = report.get("proposals") or []
    if not proposals:
        lines.append("No rule proposals right now.")
        return "\n".join(lines)

    lines.append("rule proposals:")
    for index, proposal in enumerate(proposals, start=1):
        lines.append(f"{index}. {proposal.get('plain_question')}")
        evidence = _plain_evidence_text(proposal.get("evidence"))
        if evidence:
            lines.append(f"   why: {evidence}")
        else:
            lines.append("   why: no evidence text available")
        lines.append(f"   effect: {proposal.get('effect') or ''}")
        lines.append(f"   would help right now: {'yes' if proposal.get('would_derive_now') else 'no'}")
        examples = proposal.get("example_derivations") or []
        if examples:
            lines.append("   for example, enabling this would let me conclude:")
            for example in examples:
                lines.append(f"     - {example}")
        else:
            lines.append("   (no new conclusions from your current notes yet)")
    lines.append("")
    lines.append("To turn any of these on, just say the number (e.g. \"enable 1\") and I'll do it.")
    return "\n".join(lines)


def _summarize_enable_result(report: dict) -> str:
    return "\n".join([
        f"palace: {report.get('palace')}",
        f"rules: {report.get('rules_path')}",
        "enabled: " + (", ".join(report.get("enabled") or []) or "none"),
        "unknown: " + (", ".join(report.get("unknown") or []) or "none"),
        f"now enabled: {report.get('now_enabled_count')}",
    ])


def _summarize_disable_result(report: dict) -> str:
    return "\n".join([
        f"palace: {report.get('palace')}",
        f"rules: {report.get('rules_path')}",
        "disabled: " + (", ".join(report.get("disabled") or []) or "none"),
        "unknown: " + (", ".join(report.get("unknown") or []) or "none"),
        f"now enabled: {report.get('now_enabled_count')}",
    ])


def enable_rules(palace, rule_ids: list[str], *, rules_path=None, min_support=2) -> dict:
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    doc = dream_ontology.read_ontology_doc(effective_rules_path)
    current_rules, version = _ontology_rules_doc(doc)
    triples = dream_palace.load_premises(path, purpose="durable")
    candidates = {
        rule.get("id"): rule
        for rule in _candidate_rules_from_triples(triples, current_rules, min_support)
        if rule.get("id")
    }

    existing_by_id = {rule.get("id"): rule for rule in current_rules if rule.get("id")}
    enabled = []
    unknown = []
    seen_requested = set()
    for rule_id in rule_ids:
        if rule_id in seen_requested:
            continue
        seen_requested.add(rule_id)
        existing = existing_by_id.get(rule_id)
        if existing is not None:
            existing["enabled"] = True
            enabled.append(rule_id)
            continue
        candidate = candidates.get(rule_id)
        if candidate is None:
            unknown.append(rule_id)
            continue
        new_rule = dict(candidate)
        new_rule["enabled"] = True
        current_rules.append(new_rule)
        existing_by_id[rule_id] = new_rule
        enabled.append(rule_id)

    updated_doc = dream_ontology.build_ontology_doc(current_rules, version)
    dream_ontology.write_ontology_doc(effective_rules_path, updated_doc)
    return {
        "palace": path,
        "rules_path": effective_rules_path,
        "enabled": enabled,
        "unknown": unknown,
        "now_enabled_count": sum(1 for rule in current_rules if bool(rule.get("enabled", False))),
    }


def disable_rules(palace, rule_ids: list[str], *, rules_path=None) -> dict:
    """Set enabled=false for the named rules in the palace ontology."""
    path = dream_palace.bind_palace(palace)
    effective_rules_path = rules_path or os.path.join(path, "ontology.json")
    doc = dream_ontology.read_ontology_doc(effective_rules_path)
    current_rules, version = _ontology_rules_doc(doc)

    existing_by_id = {rule.get("id"): rule for rule in current_rules if rule.get("id")}
    disabled = []
    unknown = []
    seen_requested = set()
    for rule_id in rule_ids:
        if rule_id in seen_requested:
            continue
        seen_requested.add(rule_id)
        existing = existing_by_id.get(rule_id)
        if existing is None:
            unknown.append(rule_id)
            continue
        existing["enabled"] = False
        disabled.append(rule_id)

    updated_doc = dream_ontology.build_ontology_doc(current_rules, version)
    dream_ontology.write_ontology_doc(effective_rules_path, updated_doc)
    return {
        "palace": path,
        "rules_path": effective_rules_path,
        "disabled": disabled,
        "unknown": unknown,
        "now_enabled_count": sum(1 for rule in current_rules if bool(rule.get("enabled", False))),
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


def run_insight_start(
    palace: str,
    *,
    anchor_drawer_id: str | None,
    seed_query: str | None,
    wing: str | None,
    room: str | None,
    k: int,
    run_id: str | None,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    return dream_insight.insight_start(
        path,
        anchor_drawer_id=anchor_drawer_id,
        seed_query=seed_query,
        wing=wing,
        room=room,
        k=k,
        run_id=run_id or str(uuid.uuid4()),
        now=now,
    )


def run_insight_survey(
    palace: str,
    *,
    wing: str | None,
    room: str | None,
    k: int,
    top_n: int,
) -> dict:
    path = dream_palace.bind_palace(palace)
    return dream_insight.survey_insight_clusters(path, wing=wing, room=room, k=k, top_n=top_n)


def run_insight_resume(
    palace: str,
    *,
    run_id: str,
    candidate_file: str,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    with open(candidate_file, encoding="utf-8") as fh:
        candidate = json.load(fh)
    if not isinstance(candidate, dict):
        raise ValueError("--candidate-file must contain a JSON object")
    return dream_insight.insight_resume(path, run_id, candidate=candidate, now=now)


def run_insight_critique(
    palace: str,
    *,
    run_id: str,
    verdict: str,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    return dream_insight.insight_critique(path, run_id, verdict=verdict, now=now)


def run_insight_accept(
    palace: str,
    *,
    run_id: str,
    wing: str | None,
    room: str | None,
    now=None,
) -> dict:
    path = dream_palace.bind_palace(palace)
    return dream_insight.insight_accept(
        path,
        run_id,
        wing=wing or "copilot-mempalace",
        room=room or "insights",
        now=now,
    )


def summarize_insight_result(result: dict) -> str:
    lines = [
        f"status: {result.get('status')}",
        f"run_id: {result.get('run_id')}",
    ]
    if result.get("reason"):
        lines.append(f"reason: {result.get('reason')}")
    if result.get("rejects"):
        lines.append("rejects: " + ", ".join(result.get("rejects") or []))
    if result.get("anchor"):
        lines.append(f"anchor: {result['anchor'].get('id')}")
    neighbors = result.get("neighbors") or []
    if neighbors:
        lines.append(f"neighbors: {len(neighbors)}")
    if result.get("candidate"):
        conclusion = (result["candidate"].get("conclusion") or {}).get("text")
        if conclusion:
            lines.append(f"candidate: {_recall_snippet(conclusion, limit=160)}")
    nearest = result.get("nearest_existing")
    if nearest:
        try:
            sim = f"{float(nearest.get('sim')):.4f}"
        except (TypeError, ValueError):
            sim = "nan"
        lines.append(f"nearest_existing: {sim} {nearest.get('id')}")
    if result.get("insight_drawer_id"):
        lines.append(f"insight_drawer_id: {result.get('insight_drawer_id')}")
    if result.get("instruction"):
        lines.append(f"instruction: {result.get('instruction')}")
    if result.get("critic_instruction"):
        lines.append(f"critic_instruction: {result.get('critic_instruction')}")
    return "\n".join(lines)


def summarize_insight_survey(report: dict) -> str:
    clusters = report.get("clusters") or []
    lines = [
        f"insight survey: total drawers {report.get('total_drawers')}; clusters found {len(clusters)}"
    ]
    if not clusters:
        lines.append("no candidate insight clusters found")
        return "\n".join(lines)
    palace = report.get("palace")
    for index, cluster in enumerate(clusters, start=1):
        wings = cluster.get("wings") or []
        wing_text = ", ".join(str(wing) for wing in wings) if wings else "(none)"
        cross = " (cross-wing)" if cluster.get("cross_wing") else ""
        anchor_id = cluster.get("anchor_id")
        lines.extend(
            [
                f"{index}. {cluster.get('anchor_snippet')}",
                f"   spans wings: {wing_text}{cross}",
                f"   neighbors: {cluster.get('neighbor_count')}",
                f"   to pursue: contemplate --palace {palace} --insight-start --anchor-drawer {anchor_id}",
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", help="Path to the mempalace palace directory (default: mempalace config)")
    ap.add_argument("--rules", default=None, help="Path to ontology config (default: <palace>/ontology.json)")
    ap.add_argument("--min-support", type=int, default=2, help="Minimum support for induced bootstrap rules (default 2)")
    ap.add_argument("--bootstrap", action="store_true", help="Write disabled ontology candidates for review")
    ap.add_argument("--propose", action="store_true", help="Show plain-language ontology rule proposals")
    ap.add_argument("--enable-rule", action="append", default=[], metavar="ID", help="Enable one proposed ontology rule by id")
    ap.add_argument("--disable-rule", action="append", default=[], metavar="ID", help="Disable one ontology rule by id")
    ap.add_argument(
        "--out",
        default=None,
        help="Ontology output path for --bootstrap (default: <palace>/ontology.json)",
    )
    ap.add_argument("--format", choices=["summary", "json"], default="summary", help="Output format (default summary)")
    ap.add_argument("--recall", default=None, help="Reasoning query for relevance-ranked past session recall")
    ap.add_argument("--k", type=int, default=5, help="Maximum relevant sessions/neighbors to return (default 5)")
    ap.add_argument("--top-n", type=int, default=10, help="Maximum --insight-survey clusters to return (default 10)")
    ap.add_argument("--repository", default=None, help="Repository filter passed through to --recall retrieval")
    ap.add_argument("--since", default=None, help="Lower date/time bound passed through to --recall retrieval")
    ap.add_argument("--limit-sessions", type=int, default=None, help="Maximum sessions to inspect for --recall")
    ap.add_argument("--min-similarity", type=float, default=0.0, help="Minimum similarity for --recall (default 0.0)")
    ap.add_argument("--insight-start", action="store_true", help="Start drawer-only insight synthesis")
    ap.add_argument("--insight-survey", action="store_true", help="Read-only survey of candidate insight seed clusters")
    ap.add_argument("--insight-resume", action="store_true", help="Resume insight synthesis with --candidate-file")
    ap.add_argument("--insight-critique", action="store_true", help="Resume insight synthesis with --verdict")
    ap.add_argument("--insight-accept", action="store_true", help="Accept and materialize a supported insight drawer")
    ap.add_argument("--anchor-drawer", default=None, help="Anchor drawer id for --insight-start")
    ap.add_argument("--insight-query", default=None, help="Seed query for --insight-start")
    ap.add_argument("--candidate-file", default=None, help="JSON Candidate object for --insight-resume")
    ap.add_argument("--verdict", choices=["supported", "insufficient", "contradicted"], default=None, help="Critic verdict for --insight-critique")
    ap.add_argument("--wing", default=None, help="Wing scope for insight modes or target wing for --insight-accept")
    ap.add_argument("--room", default=None, help="Room scope for insight modes or target room for --insight-accept")
    ap.add_argument("--run-id", default=None, help="Controlled run id for resumable insight modes (default: generated UUID)")
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

    contemplate_modes = [
        args.insight_start,
        args.insight_survey,
        args.insight_resume,
        args.insight_critique,
        args.insight_accept,
    ]
    if sum(1 for enabled in contemplate_modes if enabled) > 1:
        print("error: choose only one contemplate mode", file=sys.stderr)
        return 2

    if args.insight_start:
        if bool(args.anchor_drawer) == bool(args.insight_query):
            print("error: --insight-start requires exactly one of --anchor-drawer or --insight-query", file=sys.stderr)
            return 2
        report = run_insight_start(
            effective_palace,
            anchor_drawer_id=args.anchor_drawer,
            seed_query=args.insight_query,
            wing=args.wing,
            room=args.room,
            k=args.k,
            run_id=args.run_id,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_insight_result(report))
        return 0

    if args.insight_survey:
        report = run_insight_survey(
            effective_palace,
            wing=args.wing,
            room=args.room,
            k=args.k,
            top_n=args.top_n,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_insight_survey(report))
        return 0

    if args.insight_resume:
        missing = [
            name
            for name, value in (
                ("--run-id", args.run_id),
                ("--candidate-file", args.candidate_file),
            )
            if not value
        ]
        if missing:
            print(f"error: --insight-resume requires {', '.join(missing)}", file=sys.stderr)
            return 2
        report = run_insight_resume(effective_palace, run_id=args.run_id, candidate_file=args.candidate_file)
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_insight_result(report))
        return 0

    if args.insight_critique:
        missing = [
            name
            for name, value in (("--run-id", args.run_id), ("--verdict", args.verdict))
            if not value
        ]
        if missing:
            print(f"error: --insight-critique requires {', '.join(missing)}", file=sys.stderr)
            return 2
        report = run_insight_critique(effective_palace, run_id=args.run_id, verdict=args.verdict)
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_insight_result(report))
        return 0

    if args.insight_accept:
        if not args.run_id:
            print("error: --insight-accept requires --run-id", file=sys.stderr)
            return 2
        report = run_insight_accept(effective_palace, run_id=args.run_id, wing=args.wing, room=args.room)
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_insight_result(report))
        return 0

    if args.propose:
        report = propose_rules(
            effective_palace,
            rules_path=args.rules,
            min_support=args.min_support,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(summarize_proposals(report))
        return 0

    if args.enable_rule or args.disable_rule:
        enable_report = None
        if args.enable_rule:
            enable_report = enable_rules(
                effective_palace,
                args.enable_rule,
                rules_path=args.rules,
                min_support=args.min_support,
            )
        disable_report = None
        if args.disable_rule:
            disable_report = disable_rules(
                effective_palace,
                args.disable_rule,
                rules_path=args.rules,
            )
        if args.format == "json":
            report = (
                {"enable": enable_report, "disable": disable_report}
                if enable_report is not None and disable_report is not None
                else enable_report or disable_report
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            sections = []
            if enable_report is not None:
                sections.append(_summarize_enable_result(enable_report))
            if disable_report is not None:
                sections.append(_summarize_disable_result(disable_report))
            print("\n\n".join(sections))
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
