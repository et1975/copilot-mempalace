"""Fail-closed gates for the dreaming `reflect` task (constructive lobe).

Reflect materializes NEW drawer-facts latent in the drawer/session corpus. Gates
reuse the insight grounding validator and add a structural admission gate
(coverage + novelty margin + top-K cap). No external fetch, no KG writes.
"""
from __future__ import annotations

from typing import Any

from dream_insight import validate_insight
from dream_lib import cosine_similarity

REFLECT_KINDS = {"distill", "generalize", "name_gap", "connect",
                 "converge", "tension", "shared_constraint"}
CLUSTER_KINDS = {"distill", "generalize", "connect", "tension", "shared_constraint"}
RECURRENCE_KINDS = {"converge"}


def validate_reflect(candidate: Any, members_by_id: Any) -> dict:
    """Kind-aware, uniform >=2-drawer grounding. Reuses validate_insight, then
    widens the accepted kind set to REFLECT_KINDS."""
    base = validate_insight(candidate, members_by_id)
    rejects = [r for r in base.get("rejects", []) if r != "bad_kind"]
    try:
        kind = ((candidate or {}).get("conclusion") or {}).get("kind")
    except Exception:
        kind = None
    if kind not in REFLECT_KINDS:
        rejects.append("bad_kind")
    return {"ok": not rejects, "rejects": rejects}


def nearest_drawer_distance(cand_vec, existing_vecs) -> float:
    """Minimum cosine DISTANCE (1 - cosine) from cand_vec to any existing vector.
    Returns 1.0 when there are no existing vectors."""
    best = 1.0
    for vec in existing_vecs or []:
        if not vec:
            continue
        dist = 1.0 - cosine_similarity(cand_vec, vec)
        if dist < best:
            best = dist
    return best


def is_novel(cand_vec, existing_vecs, *, margin: float = 0.15) -> bool:
    return nearest_drawer_distance(cand_vec, existing_vecs) >= float(margin)


def admit_structural(candidates, *, min_coverage: int = 2, top_k: int = 10) -> list[dict]:
    """Keep candidates whose coverage >= min_coverage, sort by score desc
    (stable, ties broken by id), cap to top_k."""
    kept = [c for c in (candidates or []) if int(c.get("coverage", 0)) >= int(min_coverage)]
    kept.sort(key=lambda c: (-float(c.get("score", 0.0)), str(c.get("id", ""))))
    return kept[: max(0, int(top_k))]
