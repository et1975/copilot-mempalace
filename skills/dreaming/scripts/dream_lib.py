"""Pure, dependency-free core for the dreaming pipeline.

No mempalace / numpy imports so this module is unit-testable in isolation. All
palace I/O lives in ``dream_palace.py``; orchestration in ``dream_harvest.py``
and ``dream_adopt.py``.

The dreaming pipeline consolidates near-duplicate drawers. This module owns the
deterministic ("mechanical") half: similarity, clustering, worklist assembly,
and applying already-made decisions. The cognitive half (deciding what to merge
and synthesising the merged text) is performed by the agent and arrives here as
plain ``decisions``.
"""
from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Any

WORKLIST_VERSION = 1
DEG_CAP = 5

_EPHEMERAL_RE = re.compile(
    r"\b(?:for now|this session|temporarily|one-off|throwaway|scratch|just for this)\b",
    re.IGNORECASE,
)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is zero."""
    if len(a) != len(b):
        raise ValueError("vectors must have equal length")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mean_vectors(vectors: list[list[float]]) -> list[float]:
    vectors = [v for v in vectors if v]
    if not vectors:
        return []
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


def group_logical_drawers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse physical chunk rows into logical drawers.

    ``rows`` are physical collection records ``{id, text, embedding, metadata}``.
    Chunks of one logical drawer share ``metadata['parent_drawer_id']``;
    single-chunk drawers have none. Returns logical drawers
    ``{id, member_ids, text, embedding, wing, room}`` where ``embedding`` is the
    mean of chunk embeddings and ``member_ids`` are the physical ids to delete on
    adoption. Insertion order of first-seen groups is preserved.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        meta = r.get("metadata") or {}
        key = meta.get("parent_drawer_id") or r["id"]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)

    logical = []
    for key in order:
        members = sorted(
            groups[key], key=lambda x: (x.get("metadata") or {}).get("chunk_index", 0)
        )
        meta0 = members[0].get("metadata") or {}
        logical.append(
            {
                "id": key,
                "member_ids": [m["id"] for m in members],
                "text": "\n".join(m.get("text", "") for m in members),
                "embedding": _mean_vectors([m.get("embedding") or [] for m in members]),
                "wing": meta0.get("wing"),
                "room": meta0.get("room"),
            }
        )
    return logical


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_duplicates(drawers: list[dict[str, Any]], tau: float) -> list[dict[str, Any]]:
    """Cluster logical drawers whose pairwise cosine similarity >= ``tau``.

    Similarity is symmetric but not transitive, so clusters are the connected
    components of the ``>= tau`` graph (union-find). Only components of size >= 2
    are returned (singletons carry no merge work). Each cluster is
    ``{members: [drawer...], pair_sims: [{a, b, sim}...]}``.
    """
    n = len(drawers)
    uf = _UnionFind(n)
    sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = cosine_similarity(drawers[i]["embedding"], drawers[j]["embedding"])
            if s >= tau:
                uf.union(i, j)
                sims[(i, j)] = s

    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(uf.find(i), []).append(i)

    clusters = []
    for members in comps.values():
        if len(members) < 2:
            continue
        members.sort()
        pair_sims = [
            {"a": drawers[i]["id"], "b": drawers[j]["id"], "sim": round(sims[(i, j)], 4)}
            for i in members
            for j in members
            if i < j and (i, j) in sims
        ]
        clusters.append({"members": [drawers[k] for k in members], "pair_sims": pair_sims})
    return clusters


def compute_redundancy(drawers: list[dict[str, Any]]) -> dict[str, float]:
    """Return each drawer's maximum cosine similarity to any other drawer."""
    scores = {d["id"]: 0.0 for d in drawers}
    for i in range(len(drawers)):
        for j in range(i + 1, len(drawers)):
            s = cosine_similarity(drawers[i].get("embedding") or [], drawers[j].get("embedding") or [])
            s = max(0.0, s)
            scores[drawers[i]["id"]] = max(scores[drawers[i]["id"]], s)
            scores[drawers[j]["id"]] = max(scores[drawers[j]["id"]], s)
    return scores


def _detect_ephemeral(text: str) -> bool:
    return _EPHEMERAL_RE.search(text) is not None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _age_days(filed_at: Any, now: datetime) -> int:
    filed = _parse_iso(filed_at)
    if filed is None:
        return 0
    comparison_now = now
    if filed.tzinfo is not None and comparison_now.tzinfo is None:
        filed = filed.replace(tzinfo=None)
    elif filed.tzinfo is None and comparison_now.tzinfo is not None:
        comparison_now = comparison_now.replace(tzinfo=None)
    return max(0, (comparison_now - filed).days)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def drawer_salience(
    drawer: dict[str, Any],
    redundancy: float,
    kg_degree: int,
    now: datetime,
    half_life_days: float = 180.0,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Score one drawer for pruning; lower ``v`` means weaker / more prunable."""
    age_days = _age_days(drawer.get("filed_at"), now)
    negatives = _detect_ephemeral(drawer.get("text", ""))
    recency = math.exp(-age_days / half_life_days) if half_life_days > 0.0 else 0.0
    deg = min(max(kg_degree, 0), DEG_CAP) / DEG_CAP

    w = {
        "recency": 0.4,
        "kg_degree": 0.4,
        "redundancy": 0.3,
        "negatives": 0.5,
    }
    if weights is not None:
        w.update(weights)

    v = _clamp01(
        w["recency"] * recency
        + w["kg_degree"] * deg
        - w["redundancy"] * _clamp01(redundancy)
        - (w["negatives"] if negatives else 0.0)
    )
    return {
        "id": drawer["id"],
        "age_days": age_days,
        "kg_degree": kg_degree,
        "redundancy": round(redundancy, 4),
        "negatives": negatives,
        "v": round(v, 4),
    }


def select_prune_candidates(
    scored: list[dict[str, Any]],
    v_min: float,
    age_floor_days: int,
) -> list[dict[str, Any]]:
    """Select drawers using the multi-gate AND.

    Expected shape: each element is the original logical drawer dict augmented
    with a ``salience`` sub-dict from ``drawer_salience`` and optional ``pinned``.
    """
    candidates = []
    for d in scored:
        salience = d.get("salience", d)
        if (
            salience.get("v", 1.0) < v_min
            and salience.get("age_days", 0) >= age_floor_days
            and salience.get("kg_degree", 0) == 0
            and not d.get("pinned", False)
        ):
            candidates.append(d)
    return candidates


def extract_session_id(text: str) -> str | None:
    """Extract the first ``SESSION_ID:<uuid-like>`` token from diary text."""
    match = re.search(r"SESSION_ID:\s*([0-9a-fA-F-]{8,})", text, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1)


def group_observation_themes(
    entries: list[dict[str, Any]],
    tau: float,
    min_support: int,
    support_key: str = "session_id",
) -> list[dict[str, Any]]:
    """Cluster observations into cross-session topical themes.

    Themes are connected components of the ``>= tau`` similarity graph. A theme is
    retained only when it has at least ``min_support`` distinct non-None
    provenance values from ``support_key``.
    """
    n = len(entries)
    uf = _UnionFind(n)
    sims: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            s = cosine_similarity(entries[i]["embedding"], entries[j]["embedding"])
            if s >= tau:
                uf.union(i, j)
                sims[(i, j)] = s

    comps: dict[int, list[int]] = {}
    for i in range(n):
        comps.setdefault(uf.find(i), []).append(i)

    themes = []
    for members in comps.values():
        members.sort()
        support_ids = sorted(
            {entries[i].get(support_key) for i in members if entries[i].get(support_key) is not None}
        )
        support = len(support_ids)
        if support < min_support:
            continue
        pair_sims = [
            {"a": entries[i]["id"], "b": entries[j]["id"], "sim": round(sims[(i, j)], 4)}
            for i in members
            for j in members
            if i < j and (i, j) in sims
        ]
        themes.append(
            {
                "members": [entries[k] for k in members],
                "support": support,
                "support_ids": support_ids,
                "pair_sims": pair_sims,
            }
        )
    return sorted(
        themes,
        key=lambda t: (-t["support"], min(m["id"] for m in t["members"])),
    )


def build_worklist(
    drawers: list[dict[str, Any]],
    tau: float,
    scope: dict[str, Any],
    instructions: str | None = None,
) -> dict[str, Any]:
    """Produce the deterministic worklist of merge candidates.

    Each item's ``decision`` starts ``None``; the agent fills it during the
    adjudicate phase (``{"action": "merge", "text", "wing", "room", "supersedes"}``
    or ``{"action": "skip"}``).
    """
    clusters = cluster_duplicates(drawers, tau)
    items = []
    for idx, c in enumerate(clusters):
        members = c["members"]
        items.append(
            {
                "kind": "merge",
                "cluster_id": idx,
                "members": [
                    {
                        "id": m["id"],
                        "member_ids": m["member_ids"],
                        "text": m["text"],
                        "wing": m["wing"],
                        "room": m["room"],
                    }
                    for m in members
                ],
                "supersedes": [pid for m in members for pid in m["member_ids"]],
                "evidence": {"pair_sims": c["pair_sims"], "size": len(members)},
                "decision": None,
            }
        )
    return {
        "version": WORKLIST_VERSION,
        "task": "merge",
        "scope": scope,
        "params": {"tau": tau},
        "instructions": instructions,
        "items": items,
    }


def build_pattern_worklist(
    themes: list[dict[str, Any]],
    scope: dict[str, Any],
    params: dict[str, Any],
    instructions: str | None = None,
) -> dict[str, Any]:
    """Produce the deterministic worklist of cross-session pattern candidates."""
    items = []
    for idx, t in enumerate(themes):
        members = t["members"]
        items.append(
            {
                "kind": "pattern",
                "cluster_id": idx,
                "members": [
                    {
                        "id": m["id"],
                        "text": m["text"],
                        "session_id": m.get("session_id"),
                        "agent": m.get("agent"),
                        "date": m.get("date"),
                        "topic": m.get("topic"),
                    }
                    for m in members
                ],
                "evidence": {
                    "size": len(members),
                    "support": t["support"],
                    "support_ids": t["support_ids"],
                    "pair_sims": t["pair_sims"],
                },
                "decision": None,
            }
        )
    return {
        "version": WORKLIST_VERSION,
        "task": "pattern",
        "scope": scope,
        "params": params,
        "instructions": instructions,
        "items": items,
    }


def build_prune_worklist(
    candidates: list[dict[str, Any]],
    scope: dict[str, Any],
    params: dict[str, Any],
    instructions: str | None = None,
) -> dict[str, Any]:
    """Produce the deterministic worklist of prune candidates."""
    items = []
    for c in candidates:
        items.append(
            {
                "kind": "prune",
                "id": c["id"],
                "member_ids": c.get("member_ids", [c["id"]]),
                "text": c.get("text", ""),
                "wing": c.get("wing"),
                "room": c.get("room"),
                "salience": c["salience"],
                "decision": None,
            }
        )
    return {
        "version": WORKLIST_VERSION,
        "task": "prune",
        "scope": scope,
        "params": params,
        "instructions": instructions,
        "items": items,
    }


def group_contradictions(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group active KG triples that disagree on object for a subject/predicate.

    A group is only a structural candidate: predicates may be legitimately
    multi-valued, so the agent decides whether to invalidate anything.
    """
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for t in triples:
        key = (t["subject"], t["predicate"])
        object_name = t["object"]
        candidate = {
            "object": object_name,
            "valid_from": t.get("valid_from"),
            "extracted_at": t.get("extracted_at"),
        }
        objects = grouped.setdefault(key, {})
        current = objects.get(object_name)
        if current is None or _contradiction_recency_key(candidate) > _contradiction_recency_key(current):
            objects[object_name] = candidate

    clusters = []
    for (subject, predicate), objects in grouped.items():
        if len(objects) < 2:
            continue
        candidates = sorted(objects.values(), key=lambda c: c["object"])
        candidates.sort(key=_contradiction_recency_key, reverse=True)
        clusters.append(
            {
                "subject": subject,
                "predicate": predicate,
                "candidates": candidates,
                "newest_object": candidates[0]["object"],
            }
        )
    return sorted(clusters, key=lambda c: (c["subject"], c["predicate"]))


def _contradiction_recency_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (candidate.get("valid_from") or "", candidate.get("extracted_at") or "")


def build_contradiction_worklist(
    triples: list[dict[str, Any]],
    scope: dict[str, Any],
    instructions: str | None = None,
) -> dict[str, Any]:
    """Produce the deterministic worklist of KG contradiction candidates."""
    clusters = group_contradictions(triples)
    items = []
    for idx, c in enumerate(clusters):
        items.append(
            {
                "kind": "contradiction",
                "cluster_id": idx,
                "subject": c["subject"],
                "predicate": c["predicate"],
                "candidates": c["candidates"],
                "evidence": {
                    "size": len(c["candidates"]),
                    "newest_object": c["newest_object"],
                },
                "decision": None,
            }
        )
    return {
        "version": WORKLIST_VERSION,
        "task": "contradiction",
        "scope": scope,
        "params": {},
        "instructions": instructions,
        "items": items,
    }


def apply_merge_decisions(decisions: list[dict[str, Any]], writer: Any) -> dict[str, Any]:
    """Execute approved merge decisions against ``writer`` (no cognition here).

    ``writer`` exposes ``add_drawer(wing, room, content)`` and
    ``delete_drawer(drawer_id)``. For each ``{"action": "merge"}`` decision the
    merged drawer is added first; only on a successful add are the ``supersedes``
    originals deleted (so a failed add is non-destructive). ``{"action": "skip"}``
    items are ignored.
    """
    report: dict[str, Any] = {
        "merged": 0,
        "skipped": 0,
        "added": [],
        "deleted": [],
        "errors": [],
    }
    for d in decisions:
        if d.get("action") != "merge":
            report["skipped"] += 1
            continue
        try:
            res = writer.add_drawer(d["wing"], d["room"], d["text"])
            report["added"].append(res)
        except Exception as exc:  # noqa: BLE001 - record and continue, stay non-destructive
            report["errors"].append({"stage": "add", "error": str(exc), "decision": d})
            continue
        for pid in d.get("supersedes", []):
            try:
                writer.delete_drawer(pid)
                report["deleted"].append(pid)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"stage": "delete", "id": pid, "error": str(exc)})
        report["merged"] += 1
    return report


def apply_pattern_decisions(decisions: list[dict[str, Any]], writer: Any) -> dict[str, Any]:
    """Execute approved pattern-surfacing decisions against ``writer``.

    Pattern induction is add-only: approved ``{"action": "surface"}`` decisions
    write a new drawer, but never delete or invalidate source evidence.
    """
    report: dict[str, Any] = {
        "surfaced": 0,
        "skipped": 0,
        "added": [],
        "errors": [],
    }
    for d in decisions:
        if d.get("action") != "surface":
            report["skipped"] += 1
            continue
        if not d.get("supported_by"):
            report["errors"].append({"stage": "groundedness", "error": "unsupported rule", "decision": d})
            continue
        try:
            res = writer.add_drawer(d["wing"], d["room"], d["text"])
            report["added"].append(res)
            report["surfaced"] += 1
        except Exception as exc:  # noqa: BLE001 - record and continue, stay add-only
            report["errors"].append({"stage": "add", "error": str(exc), "decision": d})
            continue
    return report


def apply_contradiction_decisions(decisions: list[dict[str, Any]], writer: Any) -> dict[str, Any]:
    """Execute approved KG invalidation decisions against ``writer``."""
    report: dict[str, Any] = {
        "invalidated": 0,
        "skipped": 0,
        "invalidated_facts": [],
        "errors": [],
    }
    for d in decisions:
        if d.get("action") != "invalidate":
            report["skipped"] += 1
            continue
        subject = d["subject"]
        predicate = d["predicate"]
        for obj in d.get("invalidate", []):
            try:
                writer.invalidate(subject, predicate, obj)
                report["invalidated_facts"].append(
                    {"subject": subject, "predicate": predicate, "object": obj}
                )
            except Exception as exc:  # noqa: BLE001 - record and continue, stay soft
                report["errors"].append(
                    {
                        "error": str(exc),
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                    }
                )
        report["invalidated"] += 1
    return report


def apply_prune_decisions(decisions: list[dict[str, Any]], archiver: Any) -> dict[str, Any]:
    """Execute approved prune decisions through ``archiver.archive_then_delete``."""
    report: dict[str, Any] = {
        "pruned": 0,
        "kept": 0,
        "archived": [],
        "errors": [],
    }
    for d in decisions:
        if d.get("action") != "prune":
            report["kept"] += 1
            continue
        salience = d.get("salience", {})
        if salience.get("kg_degree", 0) > 0 or d.get("pinned", False):
            report["errors"].append({"stage": "protected", "error": "protected drawer", "decision": d})
            continue
        record = {
            "id": d["id"],
            "member_ids": d.get("member_ids", [d["id"]]),
            "wing": d.get("wing"),
            "room": d.get("room"),
            "text": d.get("text", ""),
            "salience": salience,
            "pruned_at": datetime.now().isoformat(),
        }
        try:
            archiver.archive_then_delete(record)
            report["archived"].append(record)
            report["pruned"] += 1
        except Exception as exc:  # noqa: BLE001 - record and continue; archive failure deletes nothing
            report["errors"].append({"stage": "archive", "error": str(exc), "decision": d})
            continue
    return report
