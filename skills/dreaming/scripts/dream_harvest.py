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
import json
import sys

import dream_palace
from dream_lib import build_contradiction_worklist, build_worklist


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument("--task", choices=["merge", "contradiction"], default="merge",
                    help="Dreaming task to harvest (default merge)")
    ap.add_argument("--wing", help="Scope merge harvest to this wing (ignored for contradiction)")
    ap.add_argument("--room", help="Scope merge harvest to this room (ignored for contradiction)")
    ap.add_argument("--tau", type=float, default=0.9,
                    help="Cosine-similarity threshold for merge near-duplicates; ignored for contradiction")
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

    drawers = dream_palace.load_logical_drawers(path, args.wing, args.room)
    worklist = build_worklist(
        drawers,
        tau=args.tau,
        scope={"palace": path, "wing": args.wing, "room": args.room},
        instructions=args.instructions,
    )

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
