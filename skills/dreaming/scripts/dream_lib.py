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
_SESSION_ID_RE = re.compile(
    r"SESSION_ID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
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
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _normalise_pair_for_compare(a: datetime, b: datetime) -> tuple[datetime, datetime]:
    if a.tzinfo is not None and b.tzinfo is None:
        a = a.replace(tzinfo=None)
    elif a.tzinfo is None and b.tzinfo is not None:
        b = b.replace(tzinfo=None)
    return a, b


def _coerce_now(now: str | datetime | None) -> datetime:
    if now is None:
        return datetime.now()
    parsed = _parse_iso(now)
    return parsed if parsed is not None else datetime.now()


def _is_future_iso(value: Any, now: datetime) -> bool:
    parsed = _parse_iso(value)
    if parsed is None:
        return False
    parsed, comparison_now = _normalise_pair_for_compare(parsed, now)
    return parsed > comparison_now


def _age_days(filed_at: Any, now: datetime) -> int:
    filed = _parse_iso(filed_at)
    if filed is None:
        return 0
    filed, comparison_now = _normalise_pair_for_compare(filed, now)
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
    topic_counts: dict[Any, int] = {}
    for d in scored:
        topic = _drawer_topic_key(d)
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

    candidates = []
    for d in scored:
        salience = d.get("salience", d)
        pinned = bool(d.get("pinned", False) or (d.get("metadata") or {}).get("pinned", False))
        topic = _drawer_topic_key(d)
        if (
            salience.get("v", 1.0) < v_min
            and salience.get("age_days", 0) >= age_floor_days
            and salience.get("kg_degree", 0) == 0
            and not pinned
            and topic_counts.get(topic, 0) > 1
        ):
            candidates.append(d)
    return candidates


def _drawer_topic_key(drawer: dict[str, Any]) -> Any:
    return (drawer.get("metadata") or {}).get("topic") or drawer.get("topic") or drawer.get("room")


def extract_session_id(text: str) -> str | None:
    """Extract the first canonical ``SESSION_ID:<uuid>`` token from diary text."""
    ids = extract_all_session_ids(text)
    return ids[0] if ids else None


def extract_all_session_ids(text: str) -> list[str]:
    """Extract all canonical session UUIDs, preserving first-seen order."""
    ids: list[str] = []
    seen: set[str] = set()
    for match in _SESSION_ID_RE.finditer(text):
        session_id = match.group(1)
        if session_id not in seen:
            ids.append(session_id)
            seen.add(session_id)
    return ids


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
    cluster_id = 0
    for c in clusters:
        partitions: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
        for m in c["members"]:
            partitions.setdefault((m.get("wing"), m.get("room")), []).append(m)
        mixed_room_split = len(partitions) > 1
        for members in partitions.values():
            if len(members) < 2:
                continue
            member_ids = {m["id"] for m in members}
            pair_sims = [
                p for p in c["pair_sims"]
                if p["a"] in member_ids and p["b"] in member_ids
            ]
            item = {
                "kind": "merge",
                "cluster_id": cluster_id,
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
                "evidence": {"pair_sims": pair_sims, "size": len(members)},
                "decision": None,
            }
            if mixed_room_split:
                item["mixed_room_split"] = True
            items.append(item)
            cluster_id += 1
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
        metadata = c.get("metadata") or {}
        items.append(
            {
                "kind": "prune",
                "id": c["id"],
                "member_ids": c.get("member_ids", [c["id"]]),
                "text": c.get("text", ""),
                "wing": c.get("wing"),
                "room": c.get("room"),
                "topic": metadata.get("topic") or c.get("topic") or c.get("room"),
                "pinned": bool(metadata.get("pinned", c.get("pinned", False))),
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


def group_contradictions(
    triples: list[dict[str, Any]],
    now: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Group active KG triples that disagree on object for a subject/predicate.

    A group is only a structural candidate: predicates may be legitimately
    multi-valued, so the agent decides whether to invalidate anything.
    """
    now_dt = _coerce_now(now)
    grouped: dict[tuple[Any, str], dict[str, Any]] = {}
    for t in triples:
        if _is_future_iso(t.get("valid_from"), now_dt):
            continue
        subject_id = t.get("subject_id") or t.get("subject")
        key = (subject_id, t["predicate"])
        object_name = t["object"]
        object_id = t.get("object_id")
        object_key = object_id or object_name
        triple_id = t.get("triple_id")
        candidate = {
            "object": object_name,
            "object_id": object_id,
            "triple_id": triple_id,
            "triple_ids": [triple_id] if triple_id is not None else [],
            "valid_from": t.get("valid_from"),
            "extracted_at": t.get("extracted_at"),
        }
        group = grouped.setdefault(
            key,
            {
                "subject": t.get("subject"),
                "subject_id": subject_id,
                "predicate": t["predicate"],
                "objects": {},
            },
        )
        objects = group["objects"]
        current = objects.get(object_key)
        if current is not None and triple_id is not None and triple_id not in current["triple_ids"]:
            current["triple_ids"].append(triple_id)
        if current is None or _contradiction_recency_key(candidate) > _contradiction_recency_key(current):
            if current is not None:
                candidate["triple_ids"] = current["triple_ids"]
            objects[object_key] = candidate

    clusters = []
    for group in grouped.values():
        objects = group["objects"]
        if len(objects) < 2:
            continue
        candidates = sorted(objects.values(), key=lambda c: c["object"])
        candidates.sort(key=_contradiction_recency_key, reverse=True)
        clusters.append(
            {
                "subject": group["subject"],
                "subject_id": group["subject_id"],
                "predicate": group["predicate"],
                "candidates": candidates,
                "newest_object": candidates[0]["object"],
            }
        )
    return sorted(clusters, key=lambda c: (c["subject"] or "", c["predicate"], c["subject_id"] or ""))


def _contradiction_recency_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (candidate.get("valid_from") or "", candidate.get("extracted_at") or "")


def build_contradiction_worklist(
    triples: list[dict[str, Any]],
    scope: dict[str, Any],
    instructions: str | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    """Produce the deterministic worklist of KG contradiction candidates."""
    clusters = group_contradictions(triples, now=now)
    items = []
    for idx, c in enumerate(clusters):
        items.append(
            {
                "kind": "contradiction",
                "cluster_id": idx,
                "subject": c["subject"],
                "subject_id": c.get("subject_id"),
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


def apply_merge_decisions(decisions: list[dict[str, Any]], writer: Any, archiver: Any) -> dict[str, Any]:
    """Execute approved merge decisions against ``writer`` and ``archiver``.

    ``writer`` exposes ``add_drawer(wing, room, content, metadata=None)``.
    ``archiver`` exposes ``archive_then_delete(record)`` and is called only after
    the merged drawer is durably added.
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
        text = d.get("text") or ""
        supersedes = list(d.get("supersedes") or [])
        if not text.strip() or not supersedes:
            report["errors"].append({"stage": "soundness", "error": "merge requires text and supersedes", "decision": d})
            continue
        try:
            res = writer.add_drawer(
                d["wing"],
                d["room"],
                text,
                metadata={"supersedes": supersedes, "kind": "merged"},
            )
            report["added"].append(res)
        except Exception as exc:  # noqa: BLE001 - record and continue, stay non-destructive
            report["errors"].append({"stage": "add", "error": str(exc), "decision": d})
            continue
        record = {
            "id": _added_drawer_id(res, supersedes),
            "member_ids": supersedes,
            "wing": d["wing"],
            "room": d["room"],
            "reason": "merge",
        }
        try:
            archive_result = archiver.archive_then_delete(record)
            report["deleted"].extend(_archive_deleted_ids(archive_result, supersedes))
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"stage": "archive", "error": str(exc), "decision": d})
            continue
        report["merged"] += 1
    return report


def _added_drawer_id(add_result: Any, supersedes: list[str]) -> str:
    if isinstance(add_result, dict):
        drawer_id = add_result.get("drawer_id") or add_result.get("id")
        if drawer_id:
            return drawer_id
    return supersedes[0]


def _archive_deleted_ids(archive_result: Any, fallback: list[str]) -> list[str]:
    if isinstance(archive_result, dict):
        deleted = archive_result.get("deleted") or archive_result.get("deleted_ids")
        if isinstance(deleted, list):
            return list(deleted)
    return list(fallback)


def apply_pattern_decisions(decisions: list[dict[str, Any]], writer: Any, min_support: int) -> dict[str, Any]:
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
        supported_by = list(d.get("supported_by") or [])
        support_set = set(supported_by)
        allowed_support = d.get("_support_pool", d.get("allowed_support", supported_by))
        allowed_set = set(allowed_support or [])
        error = None
        if not supported_by:
            error = "unsupported rule"
        elif len(support_set) != len(supported_by):
            error = "support ids must be distinct"
        elif len(support_set) < min_support:
            error = "insufficient support"
        elif not support_set.issubset(allowed_set):
            error = "support ids outside evidence"
        if error is not None:
            report["errors"].append({"stage": "groundedness", "error": error, "decision": d})
            continue
        try:
            res = writer.add_drawer(
                d["wing"],
                d["room"],
                d["text"],
                metadata={"supported_by": supported_by, "kind": "lesson"},
            )
            report["added"].append(res)
            report["surfaced"] += 1
        except Exception as exc:  # noqa: BLE001 - record and continue, stay add-only
            report["errors"].append({"stage": "add", "error": str(exc), "decision": d})
            continue
    return report


def apply_contradiction_decisions(decisions: list[dict[str, Any]], writer: Any) -> dict[str, Any]:
    """Execute approved KG triple invalidation decisions against ``writer``."""
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
        triple_ids = list(d.get("invalidate") or [])
        if not triple_ids:
            report["errors"].append({"stage": "groundedness", "error": "no triples selected", "decision": d})
            continue
        try:
            writer.invalidate_triples(triple_ids)
            report["invalidated"] += len(triple_ids)
            report["invalidated_facts"].extend({"triple_id": tid} for tid in triple_ids)
        except Exception as exc:  # noqa: BLE001 - record and continue, stay soft
            report["errors"].append(
                {
                    "stage": "invalidate",
                    "error": str(exc),
                    "triple_ids": triple_ids,
                    "decision": d,
                }
            )
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
