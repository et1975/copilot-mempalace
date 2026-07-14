#!/usr/bin/env python3
"""dream_harvest — phase 1 of the dreaming pipeline (READ-ONLY).

Reads a mempalace palace, clusters near-duplicate drawers, and writes a
deterministic ``worklist.json`` of merge candidates. Writes nothing to the
palace. The agent (the dreaming skill) then fills each item's ``decision`` in
an ``adjudicate`` phase to produce ``decisions.json`` for ``dream_adopt.py``.

Usage:
    python3 dream_harvest.py --palace ~/.mempalace/palace --wing myproj \\
        --tau 0.9 --out worklist.json
    python3 dream_harvest.py --wing myproj --tau 0.9 --out worklist.json
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import re
import sys

import dream_ontology
import dream_palace
from dream_lib import (
    build_contradiction_worklist,
    build_gap_worklist,
    build_pattern_worklist,
    build_prune_worklist,
    build_worklist,
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

_LESSON_TRAILER_RE = re.compile(
    r"<!--dreaming-meta:\s*\{[^}]*[\"']kind[\"']\s*:\s*[\"']lesson[\"'][^}]*\}\s*-->",
    re.IGNORECASE,
)


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
    metadata = entry.get("metadata") or {}
    return metadata.get("kind") == "lesson" or _LESSON_TRAILER_RE.search(entry.get("text", "")) is not None


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", help="Path to the mempalace palace directory (default: mempalace config)")
    ap.add_argument("--task", choices=[
        "merge", "contradiction", "pattern", "prune", "derive", "gaps", "suggest-rules", "induce-rules"
    ], default="merge",
                    help="Dreaming task to harvest (default merge)")
    ap.add_argument("--wing", help="Scope merge harvest to this wing (ignored for contradiction)")
    ap.add_argument("--room", help="Scope merge harvest to this room (ignored for contradiction)")
    ap.add_argument("--tau", type=float,
                    help="Cosine-similarity threshold; defaults to 0.9 for merge and 0.75 for pattern")
    ap.add_argument("--min-support", type=int, default=None,
                    help="Minimum support for pattern themes (default 3) or induced ontology rules (default 2)")
    ap.add_argument("--v-min", type=float, default=0.35,
                    help="Maximum salience value for prune candidates (default 0.35)")
    ap.add_argument("--age-floor-days", type=int, default=30,
                    help="Minimum drawer age for prune candidates (default 30)")
    ap.add_argument("--rooms", default="diary",
                    help=(
                        "Comma-separated rooms for pattern observation harvest (default diary). "
                        "Put surfaced lessons in a non-mined room so future pattern harvests ignore them."
                    ))
    ap.add_argument("--source", choices=["diary", "sessions", "both"], default="diary",
                    help=(
                        "Observation source for the pattern task: diary rooms (default), raw Copilot "
                        "host sessions, or both unioned. 'sessions'/'both' mine raw session turns."
                    ))
    ap.add_argument("--repository",
                    help="Filter host sessions by repository substring (pattern --source sessions/both)")
    ap.add_argument("--since",
                    help="Only host sessions created at/after this ISO timestamp (pattern --source sessions/both)")
    ap.add_argument("--limit-sessions", type=int, default=None,
                    help="Cap the number of host sessions read (pattern --source sessions/both)")
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
    ap.add_argument("--max-candidates", type=int, default=500,
                    help="Maximum candidates for derive (default 500)")
    ap.add_argument("--target-subject", default=None,
                    help="Restrict gaps (--task gaps) to conclusions about this subject (entity id or display name)")
    ap.add_argument("--out", default="worklist.json", help="Output worklist path (default worklist.json)")
    args = ap.parse_args(argv)

    effective_palace = args.palace or _default_palace()
    if effective_palace is None:
        config_path = os.environ.get("MEMPALACE_CONFIG") or "~/.mempalace/config.json"
        print(f"error: no --palace given and {config_path} has no palace_path", file=sys.stderr)
        return 2

    path = dream_palace.bind_palace(effective_palace)
    if args.task == "contradiction":
        triples = dream_palace.load_active_triples(path)
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

    if args.task == "pattern":
        tau = args.tau if args.tau is not None else 0.75
        rooms = tuple(room.strip() for room in args.rooms.split(",") if room.strip())
        entries = []
        if args.source in ("diary", "both"):
            entries.extend(
                entry for entry in dream_palace.load_observation_entries(path, wing=args.wing, rooms=rooms)
                if not _is_surfaced_lesson(entry)
            )
        if args.source in ("sessions", "both"):
            entries.extend(
                dream_palace.load_session_observation_entries(
                    path,
                    repository=args.repository,
                    since=args.since,
                    limit_sessions=args.limit_sessions,
                )
            )
        min_support = args.min_support if args.min_support is not None else 3
        themes = group_observation_themes(entries, tau=tau, min_support=min_support)
        worklist = build_pattern_worklist(
            themes,
            scope={
                "palace": path,
                "wing": args.wing,
                "rooms": list(rooms),
                "source": args.source,
                "task": "pattern",
            },
            params={"tau": tau, "min_support": min_support},
            instructions=args.instructions,
        )
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(
            f"harvested {len(entries)} observation entries ({args.source}) -> {len(worklist['items'])} "
            f"pattern theme(s) spanning >= {min_support} sessions -> {args.out}",
            file=sys.stderr,
        )
        return 0

    if args.task == "prune":
        drawers = dream_palace.load_logical_drawers(path, wing=args.wing, room=args.room)
        degrees = dream_palace.kg_source_degree(path)
        redundancy = compute_redundancy(drawers)
        now = datetime.now()
        scored = []
        for drawer in drawers:
            metadata = drawer.get("metadata") or {}
            drawer_for_salience = {**drawer, "filed_at": metadata.get("filed_at", drawer.get("filed_at"))}
            scored.append(
                {
                    **drawer,
                    "salience": drawer_salience(
                        drawer_for_salience,
                        redundancy[drawer["id"]],
                        _degree_for(drawer, degrees),
                        now=now,
                    ),
                    "pinned": metadata.get("pinned", False),
                }
            )
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
        triples = dream_palace.load_active_triples_with_ids(path)
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
        triples = dream_palace.load_active_triples_with_ids(path)
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
        triples = dream_palace.load_active_triples_with_ids(path)
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
        triples = dream_palace.load_active_triples_with_ids(path)
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

    tau = args.tau if args.tau is not None else 0.9
    drawers = dream_palace.load_logical_drawers(path, args.wing, args.room)
    worklist = build_worklist(
        drawers,
        tau=tau,
        scope={"palace": path, "wing": args.wing, "room": args.room},
        instructions=args.instructions,
    )
    _stamp_merge_hashes(worklist)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(worklist, fh, indent=2, ensure_ascii=False)

    n_items = len(worklist["items"])
    n_drawers = sum(item["evidence"]["size"] for item in worklist["items"])
    print(
        f"harvested {len(drawers)} logical drawers -> {n_items} merge cluster(s) "
        f"covering {n_drawers} drawers -> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
