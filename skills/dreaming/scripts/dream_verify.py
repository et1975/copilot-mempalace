#!/usr/bin/env python3
"""Report-only exhaustive merge verification using the harvest candidate pipeline.

Print JSON to stdout. With --strict, residual candidates return status 1.
Backend, capability or incomplete-scan failures always return status 2.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

import dream_harvest
import dream_palace


def verify_merge(path: str, *, wing: str | None = None,
                 room: str | None = None, tau: float = 0.9) -> dict:
    worklist = dream_harvest.harvest_merge_worklist(path, wing=wing, room=room, tau=tau)
    items = worklist["items"]
    return {
        "task": "merge", "scope": {"palace": path, "wing": wing, "room": room},
        "params": {"tau": tau}, "complete": True, "converged": not items,
        "residual": len(items), "items": items, "errors": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--palace", help="Palace path (default: mempalace config)")
    parser.add_argument("--wing", help="Restrict the scan to this wing")
    parser.add_argument("--room", help="Restrict the scan to this room")
    parser.add_argument("--tau", type=float, default=0.9, help="Merge cosine threshold (default 0.9)")
    parser.add_argument("--strict", action="store_true", help="Return status 1 for residual candidates")
    args = parser.parse_args(argv)
    path = args.palace or dream_harvest._default_palace()
    try:
        if not path:
            raise ValueError("no --palace given and no configured palace_path")
        path = dream_palace.bind_palace(path)
        report = verify_merge(path, wing=args.wing, room=args.room, tau=args.tau)
    except Exception as exc:  # Report errors explicitly even for non-strict callers.
        report = {
            "task": "merge", "scope": {"palace": path, "wing": args.wing, "room": args.room},
            "params": {"tau": args.tau if math.isfinite(args.tau) else None},
            "complete": False, "converged": False,
            "residual": None, "items": [], "errors": [str(exc)],
        }
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        print(f"error: verification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    return 1 if args.strict and report["residual"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
