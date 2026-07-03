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
from dream_lib import build_worklist


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument("--wing", help="Scope harvest to this wing (project)")
    ap.add_argument("--room", help="Scope harvest to this room (aspect)")
    ap.add_argument("--tau", type=float, default=0.9,
                    help="Cosine-similarity threshold for near-duplicates (default 0.9)")
    ap.add_argument("--instructions", help="Optional steering note recorded in the worklist")
    ap.add_argument("--out", default="worklist.json", help="Output worklist path (default worklist.json)")
    args = ap.parse_args(argv)

    path = dream_palace.bind_palace(args.palace)
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
