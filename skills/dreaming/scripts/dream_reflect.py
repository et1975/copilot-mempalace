"""Fail-closed gates for the dreaming `reflect` task (constructive lobe).

Reflect materializes NEW drawer-facts latent in the drawer/session corpus. Gates
reuse the insight grounding validator and add a structural admission gate
(coverage + novelty margin + top-K cap). No external fetch, no KG writes.
"""
from __future__ import annotations

from typing import Any

from dream_insight import validate_insight, rank_survey_clusters
from dream_lib import cosine_similarity, group_observation_themes
from dream_palace import load_logical_drawers

REFLECT_KINDS = {"distill", "generalize", "name_gap", "connect",
                 "converge", "tension", "shared_constraint"}


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
    Returns 1.0 when there are no comparable existing vectors; returns 0.0 for an
    un-embeddable (falsy) cand_vec (fail-closed: treated as NOT novel)."""
    if not cand_vec:
        return 0.0
    best = 1.0
    for vec in existing_vecs or []:
        if not vec:
            continue
        try:
            sim = cosine_similarity(cand_vec, vec)
        except ValueError:
            continue  # skip existing vectors of a different embedding dimension
        dist = 1.0 - sim
        if dist < best:
            best = dist
    return best


def is_novel(cand_vec, existing_vecs, *, margin: float = 0.15) -> bool:
    if not cand_vec:
        return False  # fail-closed: a candidate we cannot embed is NOT admitted
    return nearest_drawer_distance(cand_vec, existing_vecs) >= float(margin)


def admit_structural(candidates, *, min_coverage: int = 2, top_k: int = 10) -> list[dict]:
    """Keep candidates whose coverage >= min_coverage, sort by score desc
    (stable, ties broken by id), cap to top_k."""
    kept = [c for c in (candidates or []) if int(c.get("coverage", 0)) >= int(min_coverage)]
    kept.sort(key=lambda c: (-float(c.get("score", 0.0)), str(c.get("id", ""))))
    return kept[: max(0, int(top_k))]


def gather_reflect_seeds(palace_path, *, wing=None, room=None, k=5, top_n=10) -> list[dict]:
    """Return ranked >=2-drawer seed clusters (anchor + neighbors) for the
    cluster reflect kinds, reusing the insight survey ranker. Each seed carries
    full member text so the agent can quote-ground during adjudication."""
    drawers = load_logical_drawers(palace_path, wing=wing, room=room)
    by_id = {str(d.get("id")): d for d in drawers}
    clusters = rank_survey_clusters(drawers, k=k, top_n=top_n)
    seeds = []
    for cluster in clusters:
        member_ids = [cluster["anchor_id"]] + list(cluster.get("neighbor_ids") or [])
        if len(member_ids) < 2:
            continue
        members = [{"id": str(mid), "text": (by_id.get(str(mid)) or {}).get("text", "")}
                   for mid in member_ids]
        seeds.append({
            "anchor_id": cluster["anchor_id"],
            "member_ids": member_ids,
            "members": members,
            "snippets": [{"id": cluster["anchor_id"], "snippet": cluster.get("anchor_snippet")}]
                        + list(cluster.get("neighbor_snippets") or []),
            "wings": cluster.get("wings"),
            "coverage": len(member_ids),
            "score": float(cluster.get("score", 0.0)),
        })
    return seeds


def converge_seeds_from_recurrence(entries, *, tau, min_support) -> list[dict]:
    themes = group_observation_themes(entries, tau, min_support, support_key="session_id")
    seeds = []
    for theme in themes:
        members = theme.get("members", [])
        member_ids = [m["id"] for m in members]
        support = int(theme.get("support", 0))
        seeds.append({
            "anchor_id": member_ids[0] if member_ids else None,
            "member_ids": member_ids,
            "members": [{"id": m["id"], "text": m.get("text", ""),
                         "session_id": m.get("session_id"), "date": m.get("date"),
                         "topic": m.get("topic")} for m in members],
            "reflect_kind": "converge",
            "evidence": {"support": support,
                         "support_ids": theme.get("support_ids", []),
                         "pair_sims": theme.get("pair_sims", [])},
            "coverage": max(support, len(set(member_ids))),
            "score": float(support),
        })
    return seeds
