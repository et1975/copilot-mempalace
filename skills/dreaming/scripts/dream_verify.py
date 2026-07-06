#!/usr/bin/env python3
"""Report-only convergence verification for dreaming merge decisions."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import dream_palace


def _max_pair_sim(pair_sims: list[dict[str, Any]]) -> float | None:
    sims = [pair.get("sim") for pair in pair_sims if pair.get("sim") is not None]
    if not sims:
        return None
    return max(float(sim) for sim in sims)


def build_convergence_report(
    clusters: list[dict[str, Any]],
    scope: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    residual_clusters = len(clusters)
    closure = "bounded_partial" if params.get("max_clusters") is not None and residual_clusters == params["max_clusters"] else "true"
    residuals = [
        {
            "drawer_ids": [member["id"] for member in cluster.get("members", [])],
            "size": cluster.get("size", len(cluster.get("members", []))),
            "max_sim": _max_pair_sim(cluster.get("pair_sims", [])),
        }
        for cluster in clusters
    ]
    return {
        "schema": 1,
        "task": "merge",
        "scope": scope,
        "params": params,
        "closure": closure,
        "converged": residual_clusters == 0,
        "residual_clusters": residual_clusters,
        "residuals": residuals,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument("--wing", help="Scope verification to this wing")
    ap.add_argument("--room", help="Scope verification to this room")
    ap.add_argument("--tau", type=float, default=0.9, help="Cosine-similarity threshold (default 0.9)")
    ap.add_argument("--max-clusters", type=int, help="Maximum duplicate clusters to re-harvest")
    ap.add_argument("--out", help="Optional JSON report path; stdout is used when omitted")
    ap.add_argument("--strict", action="store_true", help="Exit non-zero when residual clusters remain")
    args = ap.parse_args(argv)

    palace_path = dream_palace.bind_palace(args.palace)
    scope = {"palace": palace_path, "wing": args.wing, "room": args.room}
    params = {"tau": args.tau, "max_clusters": args.max_clusters}
    clusters = dream_palace.find_duplicate_clusters(
        palace_path,
        wing=args.wing,
        room=args.room,
        tau=args.tau,
        max_clusters=args.max_clusters,
    )
    report = build_convergence_report(clusters, scope, params)

    status = "true" if report["converged"] else "false"
    print(
        f"merge convergence: {status} — {report['residual_clusters']} residual cluster(s) "
        f"[closure={report['closure']}]",
        file=sys.stderr,
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    else:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        print()

    return 1 if args.strict and report["residual_clusters"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
