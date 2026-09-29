#!/usr/bin/env python3
"""dream_harvest — phase 1 of the dreaming pipeline (READ-ONLY).

By default, exports all unreviewed sessions and original wing memories in a
frozen incremental window to ``worklist.json``. Explicit tasks retain their legacy
meaning, including drawer-cluster reflection without --source. Writes nothing
to the palace for reflection. The agent then fills each item's ``decision`` in
an ``adjudicate`` phase to produce ``decisions.json`` for ``dream_adopt.py``.

Usage:
    "$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --palace ~/.mempalace/palace \\
        --repository owner/myproj --wing myproj --out worklist.json
    "$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --task merge --wing myproj --tau 0.9 --out worklist.json

Select absolute MPY (the provisioned MemPalace interpreter) and DREAM_SCRIPTS
paths. Run from an external session workspace for relative artifact paths.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
import sqlite3
import sys

import dream_ontology
import dream_palace
from dream_metadata import is_generated_observation
from dream_procedural_palace import exclude_protected_drawers, live_protected_drawer_ids
from dream_lib import (
    WORKLIST_VERSION,
    build_contradiction_worklist,
    build_gap_worklist,
    build_pattern_worklist,
    build_prune_worklist,
    build_reflect_worklist,
    compute_redundancy,
    deductive_closure,
    drawer_salience,
    filter_skipped,
    find_transitive_gaps,
    build_contemplate_worklist,
    group_observation_themes,
    ontology_version,
    select_prune_candidates,
)

DEFAULT_V_MIN = 0.35
DEFAULT_AGE_FLOOR_DAYS = 30

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _is_surfaced_lesson(entry: dict) -> bool:
    return is_generated_observation(entry)


def _stamp_merge_hashes(worklist: dict) -> None:
    for item in worklist.get("items", []):
        hashes = {}
        for member in item.get("members", []):
            digest = _content_hash(member.get("text", ""))
            member["content_hash"] = digest
            hashes[member["id"]] = digest
        item["content_hashes"] = hashes


def _stamp_prune_hashes(worklist: dict) -> None:
    for item in worklist.get("items", []):
        item["content_hash"] = _content_hash(item.get("text", ""))


def _degree_for(drawer: dict, degrees: dict[str, int]) -> int:
    ids = {drawer["id"], *drawer.get("member_ids", [])}
    return sum(degrees.get(drawer_id, 0) for drawer_id in ids)


def score_prune_drawers(drawers: list[dict], degrees: dict[str, int],
                        usage: dict[str, dict]) -> list[dict]:
    """Score the current scoped population, including topic-retention context."""
    redundancy = compute_redundancy(drawers)
    now = datetime.now()
    scored = []
    for drawer in drawers:
        metadata = drawer.get("metadata") or {}
        scoring_input = {**drawer, "filed_at": metadata.get("filed_at", drawer.get("filed_at"))}
        scored.append({
            **drawer,
            "salience": drawer_salience(
                scoring_input, redundancy[drawer["id"]], _degree_for(drawer, degrees),
                now=now, usage=usage.get(drawer["id"])),
            "pinned": metadata.get("pinned", False),
        })
    return scored


def harvest_merge_worklist(path: str, *, wing: str | None = None,
                           room: str | None = None, tau: float = 0.9,
                           instructions: str | None = None) -> dict:
    """The exhaustive actionable merge pipeline shared by all CLI entry points."""
    if not math.isfinite(tau) or not 0 <= tau <= 1:
        raise ValueError("merge tau must be finite and between zero and one")
    clusters = dream_palace.find_duplicate_clusters(
        path, wing=wing, room=room, threshold=1 - tau,
        exclude_ids=live_protected_drawer_ids(path))
    items = []
    for cluster in clusters:
        members = cluster["members"]
        items.append({
            "kind": "merge",
            "cluster_id": len(items),
            "members": [{key: value for key, value in member.items() if key != "embedding"}
                        for member in members],
            "supersedes": [pid for member in members for pid in member["member_ids"]],
            "evidence": {"pair_sims": cluster["pair_sims"], "size": len(members)},
            "decision": None,
        })
    worklist = {
        "version": WORKLIST_VERSION, "task": "merge",
        "scope": {"palace": path, "wing": wing, "room": room},
        "params": {"tau": tau}, "instructions": instructions, "items": items,
    }
    _stamp_merge_hashes(worklist)
    return worklist


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", help="Path to the mempalace palace directory (default: mempalace config)")
    ap.add_argument("--task", choices=[
        "merge", "contradiction", "pattern", "prune", "derive", "gaps", "suggest-rules", "induce-rules", "reflect"
    ], default=None,
                    help="Explicit preview/maintenance task (default: incremental sessions and original memories)")
    ap.add_argument("--wing", help="Required memory wing for incremental review; drawer scope for explicit tasks")
    ap.add_argument("--room", help="Drawer room scope for explicit tasks, not incremental review")
    ap.add_argument("--tau", type=float,
                    help="Cosine-similarity threshold; defaults to 0.9 for merge and 0.75 for pattern")
    ap.add_argument("--min-support", type=positive_int, default=None,
                    help="Minimum support for pattern themes (default 3) or induced ontology rules (default 2)")
    ap.add_argument("--v-min", type=float, default=DEFAULT_V_MIN,
                    help="Maximum salience value for prune candidates (default 0.35)")
    ap.add_argument("--age-floor-days", type=int, default=DEFAULT_AGE_FLOOR_DAYS,
                    help="Minimum drawer age for prune candidates (default 30)")
    ap.add_argument("--rooms", default=None,
                    help=(
                        "Comma-separated rooms for pattern observation harvest (default diary). "
                        "Put surfaced lessons in a non-mined room so future pattern harvests ignore them."
                    ))
    ap.add_argument("--source", choices=["diary", "sessions", "both"], default=None,
                    help=(
                        "Explicit reflection preview source: diary, sessions, or both. "
                        "Requires --task; implicit review always includes sessions and original memories."
                    ))
    ap.add_argument("--repository",
                    help="Exact repository for incremental review; substring filter for explicit session previews")
    ap.add_argument("--since",
                    help="Explicit preview lower bound; implicit review uses the completed-dream checkpoint")
    ap.add_argument("--limit-sessions", type=positive_int, default=None,
                    help="Explicit preview session cap; no default cap and not allowed for incremental review")
    ap.add_argument("--instructions", help="Optional steering note recorded in the worklist")
    ap.add_argument("--rules", default=None,
                    help="Path to ontology config (default: <palace>/ontology.json)")
    ap.add_argument("--ontology-out", default=None,
                    help="Path to ontology output for rule suggestion/induction (default: <palace>/ontology.json)")
    ap.add_argument("--skips", default=None,
                    help="Path to skip-markers file (default: <palace>/dream-derive-skips.jsonl)")
    ap.add_argument("--max-depth", type=int, default=3,
                    help="Maximum derivation depth for derive (default 3)")
    ap.add_argument("--max-iterations", type=int, default=10,
                    help="Maximum closure iterations for derive (default 10)")
    ap.add_argument("--max-candidates", type=positive_int, default=None,
                    help="Explicit task candidate cap (default 500); not an incremental input limit")
    ap.add_argument("--target-subject", default=None,
                    help="Restrict gaps (--task gaps) to conclusions about this subject (entity id or display name)")
    ap.add_argument("--out", default="worklist.json", help="Output worklist path (default worklist.json)")
    args = ap.parse_args(argv)

    effective_palace = args.palace or _default_palace()
    if effective_palace is None:
        config_path = os.environ.get("MEMPALACE_CONFIG") or "~/.mempalace/config.json"
        print(f"error: no --palace given and {config_path} has no palace_path", file=sys.stderr)
        return 2

    implicit = args.task is None
    if implicit:
        if not (args.repository or "").strip() or not (args.wing or "").strip():
            ap.error("incremental dreaming requires explicit nonblank --repository and --wing")
        if any(value is not None for value in (
                args.source, args.since, args.limit_sessions, args.max_candidates, args.room,
                args.rooms, args.tau, args.min_support)):
            ap.error("partial source/since/limit/room options require an explicit --task reflect preview")
        import dream_incremental
        try:
            path = dream_palace.bind_palace(effective_palace)
            worklist = dream_incremental.harvest(path, args.repository, args.wing, args.instructions)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"error: incremental harvest failed: {exc}", file=sys.stderr)
            return 2
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        return 0
    raw_sessions = args.source in ("sessions", "both")
    if args.source and args.task not in ("reflect", "pattern"):
        ap.error("--source is only supported for reflect or pattern")
    if any(value is not None for value in (args.repository, args.since, args.limit_sessions)) and not raw_sessions:
        ap.error("--repository, --since and --limit-sessions require --source sessions or both")
    if raw_sessions and args.room:
        ap.error("--room does not scope raw sessions; use --repository")
    if args.since is not None:
        try:
            datetime.fromisoformat(args.since)
        except ValueError:
            ap.error("--since must be an ISO date or timestamp")
    if args.max_candidates is None:
        args.max_candidates = 500

    path = dream_palace.bind_palace(effective_palace)
    if args.task == "contradiction":
        triples = dream_palace.load_premises(path, purpose="audit")
        worklist = build_contradiction_worklist(
            triples,
            scope={"palace": path, "task": "contradiction"},
            instructions=args.instructions,
        )
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(
            f"harvested {len(triples)} active triples -> {len(worklist['items'])} "
            f"contradiction candidate group(s) -> {args.out}",
            file=sys.stderr,
        )
        return 0

    if args.task == "prune":
        drawers = dream_palace.load_logical_drawers(path, wing=args.wing, room=args.room)
        drawers = exclude_protected_drawers(path, drawers)
        degrees = dream_palace.kg_protection_degree(path)
        usage = dream_palace.load_drawer_usage(path, wing=args.wing, room=args.room)
        scored = score_prune_drawers(drawers, degrees, usage)
        candidates = select_prune_candidates(
            scored,
            v_min=args.v_min,
            age_floor_days=args.age_floor_days,
        )
        worklist = build_prune_worklist(
            candidates,
            scope={"palace": path, "wing": args.wing, "room": args.room, "task": "prune"},
            params={"v_min": args.v_min, "age_floor_days": args.age_floor_days},
            instructions=args.instructions,
        )
        _stamp_prune_hashes(worklist)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(
            f"harvested {len(drawers)} drawers -> {len(worklist['items'])} prune candidate(s) "
            f"(v<v_min, age>=floor, kg_degree=0) -> {args.out}",
            file=sys.stderr,
        )
        return 0

    if args.task == "suggest-rules":
        ontology_out = args.ontology_out or os.path.join(path, "ontology.json")
        triples = dream_palace.load_premises(path, purpose="audit")
        predicates = sorted({
            triple.get("predicate") for triple in triples
            if isinstance(triple.get("predicate"), str) and triple.get("predicate")
        })
        cands = dream_ontology.suggest_rules_from_predicates(predicates)
        existing = dream_ontology.read_ontology_doc(ontology_out)
        merged, stats = dream_ontology.merge_ontology_candidates(existing.get("rules", []), cands)
        doc = dream_ontology.build_ontology_doc(merged, existing.get("version", 1))
        dream_ontology.write_ontology_doc(ontology_out, doc)
        print(
            f"suggest-rules: proposed {len(cands)} candidate(s), added {stats['added']} "
            f"(skipped {stats['skipped_existing']} existing) -> {ontology_out}",
            file=sys.stderr,
        )
        print(
            "all candidates written DISABLED — review and set enabled:true on approved rules before contemplate can use them",
            file=sys.stderr,
        )
        return 0

    if args.task == "induce-rules":
        ontology_out = args.ontology_out or os.path.join(path, "ontology.json")
        triples = dream_palace.load_premises(path, purpose="audit")
        existing = dream_ontology.read_ontology_doc(ontology_out)
        base = dream_ontology.filter_base_triples(triples, existing.get("rules", []))
        min_support = args.min_support if args.min_support is not None else 2
        cands = dream_ontology.induce_rules_from_triples(base, min_support=min_support)
        merged, stats = dream_ontology.merge_ontology_candidates(existing.get("rules", []), cands)
        doc = dream_ontology.build_ontology_doc(merged, existing.get("version", 1))
        dream_ontology.write_ontology_doc(ontology_out, doc)
        print(
            f"induce-rules: min_support={min_support} proposed {len(cands)} candidate(s), "
            f"added {stats['added']} (skipped {stats['skipped_existing']} existing) -> {ontology_out}",
            file=sys.stderr,
        )
        print(
            "all candidates written DISABLED — review and set enabled:true on approved rules before contemplate can use them",
            file=sys.stderr,
        )
        return 0

    if args.task == "derive":
        rules_path = args.rules or os.path.join(path, "ontology.json")
        skips_path = args.skips or os.path.join(path, "dream-derive-skips.jsonl")
        rules = dream_palace.load_ontology_config(rules_path)
        onto_ver = ontology_version(rules)
        triples = dream_palace.load_premises(path, purpose="durable")
        candidates = deductive_closure(
            triples, rules, max_depth=args.max_depth,
            max_iterations=args.max_iterations, max_candidates=args.max_candidates)
        skips = dream_palace.load_skip_markers(skips_path)
        candidates = filter_skipped(candidates, skips, onto_ver)
        for c in candidates:
            c["ontology_version"] = onto_ver
        worklist = build_contemplate_worklist(
            candidates, scope={"palace": path}, rules=rules, onto_version=onto_ver,
            params={"max_depth": args.max_depth, "max_iterations": args.max_iterations,
                    "max_candidates": args.max_candidates})
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(f"derive: {len(candidates)} candidate(s) ({onto_ver}) -> {args.out}", file=sys.stderr)
        return 0

    if args.task == "gaps":
        rules_path = args.rules or os.path.join(path, "ontology.json")
        rules = dream_palace.load_ontology_config(rules_path)
        onto_ver = ontology_version(rules)
        triples = dream_palace.load_premises(path, purpose="durable")
        gaps = find_transitive_gaps(
            triples, rules, target_subject=args.target_subject,
            max_candidates=args.max_candidates)
        for g in gaps:
            g["ontology_version"] = onto_ver
        worklist = build_gap_worklist(
            gaps,
            scope={"palace": path, "task": "gaps", "target_subject": args.target_subject},
            params={"max_candidates": args.max_candidates},
            rules=rules, onto_version=onto_ver,
            instructions=args.instructions)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(f"gaps: {len(gaps)} gap(s) ({onto_ver}) -> {args.out}", file=sys.stderr)
        return 0

    if args.task == "pattern":            # legacy alias: pattern == reflect/converge
        args.task = "reflect"
        args.source = args.source or "diary"

    if args.task == "reflect":
        import dream_reflect
        if args.source:  # recurrence / converge path (also the pattern alias)
            tau = args.tau if args.tau is not None else 0.75
            room_names = args.rooms if args.rooms is not None else "diary"
            rooms = tuple(room.strip() for room in room_names.split(",") if room.strip())
            entries = []
            if args.source in ("diary", "both"):
                entries.extend(
                    e for e in dream_palace.load_observation_entries(path, wing=args.wing, rooms=rooms)
                    if not _is_surfaced_lesson(e)
                )
            if args.source in ("sessions", "both"):
                try:
                    entries.extend(
                        dream_palace.load_session_observation_entries(
                            path, repository=args.repository, since=args.since,
                            limit_sessions=args.limit_sessions)
                    )
                except (OSError, sqlite3.Error) as exc:
                    print(f"error: cannot read session source: {exc}", file=sys.stderr)
                    return 2
            min_support = args.min_support if args.min_support is not None else 3
            seeds = dream_reflect.converge_seeds_from_recurrence(entries, tau=tau, min_support=min_support)
            params = {"tau": tau, "min_support": min_support, "top_k": args.max_candidates, "min_coverage": 2}
        else:            # cluster path
            seeds = dream_reflect.gather_reflect_seeds(
                path, wing=args.wing, room=args.room, k=args.min_support or 5,
                top_n=args.max_candidates)
            params = {"top_k": args.max_candidates, "min_coverage": 2}
        admitted = dream_reflect.admit_structural(
            seeds, min_coverage=2, top_k=args.max_candidates)
        items = [{
            "kind": "reflect", "seed_id": s["anchor_id"], "member_ids": s["member_ids"],
            "members": s.get("members"), "snippets": s.get("snippets"),
            "coverage": s["coverage"], "score": s["score"],
            "evidence": s.get("evidence"), "reflect_kind": s.get("reflect_kind"),
            "decision": None,
        } for s in admitted]
        scope = {"wing": args.wing, "room": args.room, "source": args.source}
        if raw_sessions:
            scope.update(repository=args.repository, since=args.since, limit_sessions=args.limit_sessions)
        worklist = build_reflect_worklist(items, scope=scope, params=params)
        if args.instructions:
            worklist["instructions"] += f"\nSteering note (not source evidence): {args.instructions}"
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        return 0

    tau = args.tau if args.tau is not None else 0.9
    worklist = harvest_merge_worklist(
        path, wing=args.wing, room=args.room, tau=tau, instructions=args.instructions)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(worklist, fh, indent=2, ensure_ascii=False)

    n_items = len(worklist["items"])
    n_drawers = sum(item["evidence"]["size"] for item in worklist["items"])
    print(
        f"harvested {n_items} merge cluster(s) "
        f"covering {n_drawers} drawers -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
