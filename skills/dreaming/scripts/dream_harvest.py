#!/usr/bin/env python3
"""dream_harvest — phase 1 of the dreaming pipeline (READ-ONLY).

Reads a mempalace palace, clusters near-duplicate drawers, and writes a
deterministic ``worklist.json`` of merge candidates. Writes nothing to the
palace. The agent (the dreaming skill) then fills each item's ``decision`` in
an ``adjudicate`` phase to produce ``decisions.json`` for ``dream_adopt.py``.

Usage:
    python3 dream_harvest.py --palace ~/.mempalace/palace --wing myproj \\
        --tau 0.9 --out worklist.json
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import re
import sys

import dream_palace
from dream_lib import (
    build_contradiction_worklist,
    build_pattern_worklist,
    build_prune_worklist,
    build_worklist,
    compute_redundancy,
    drawer_salience,
    group_observation_themes,
    select_prune_candidates,
)

_LESSON_TRAILER_RE = re.compile(
    r"<!--dreaming-meta:\s*\{[^}]*[\"']kind[\"']\s*:\s*[\"']lesson[\"'][^}]*\}\s*-->",
    re.IGNORECASE,
)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument("--task", choices=["merge", "contradiction", "pattern", "prune"], default="merge",
                    help="Dreaming task to harvest (default merge)")
    ap.add_argument("--wing", help="Scope merge harvest to this wing (ignored for contradiction)")
    ap.add_argument("--room", help="Scope merge harvest to this room (ignored for contradiction)")
    ap.add_argument("--tau", type=float,
                    help="Cosine-similarity threshold; defaults to 0.9 for merge and 0.75 for pattern")
    ap.add_argument("--min-support", type=int, default=3,
                    help="Minimum distinct sessions for pattern themes (default 3)")
    ap.add_argument("--v-min", type=float, default=0.35,
                    help="Maximum salience value for prune candidates (default 0.35)")
    ap.add_argument("--age-floor-days", type=int, default=30,
                    help="Minimum drawer age for prune candidates (default 30)")
    ap.add_argument("--rooms", default="diary",
                    help=(
                        "Comma-separated rooms for pattern observation harvest (default diary). "
                        "Put surfaced lessons in a non-mined room so future pattern harvests ignore them."
                    ))
    ap.add_argument("--instructions", help="Optional steering note recorded in the worklist")
    ap.add_argument("--out", default="worklist.json", help="Output worklist path (default worklist.json)")
    args = ap.parse_args(argv)

    path = dream_palace.bind_palace(args.palace)
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
        entries = [
            entry for entry in dream_palace.load_observation_entries(path, wing=args.wing, rooms=rooms)
            if not _is_surfaced_lesson(entry)
        ]
        themes = group_observation_themes(entries, tau=tau, min_support=args.min_support)
        worklist = build_pattern_worklist(
            themes,
            scope={"palace": path, "wing": args.wing, "rooms": list(rooms), "task": "pattern"},
            params={"tau": tau, "min_support": args.min_support},
            instructions=args.instructions,
        )
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, ensure_ascii=False)
        print(
            f"harvested {len(entries)} observation entries -> {len(worklist['items'])} "
            f"pattern theme(s) spanning >= {args.min_support} sessions -> {args.out}",
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
