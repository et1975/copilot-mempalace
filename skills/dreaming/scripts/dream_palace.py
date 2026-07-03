"""mempalace integration adapter for the dreaming pipeline.

All mempalace imports are isolated here so ``dream_lib`` (and its tests) stay
dependency-free. Reads go through ``mempalace.palace.get_collection``; writes go
through the sanctioned MCP tool handlers in ``mempalace.mcp_server.TOOLS`` — the
same code path the MCP server uses.

The target palace is selected via the ``MEMPALACE_PALACE_PATH`` environment
variable, which mempalace's config layer reads. Call ``bind_palace(path)``
*before* importing anything from mempalace (the CLIs do this at startup).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from dream_lib import extract_session_id, group_logical_drawers


def bind_palace(palace_path: str) -> str:
    """Point mempalace at ``palace_path`` for this process. Call before imports."""
    abspath = os.path.abspath(os.path.expanduser(palace_path))
    os.environ["MEMPALACE_PALACE_PATH"] = abspath
    return abspath


def _where(wing: str | None, room: str | None) -> dict[str, Any] | None:
    clauses = []
    if wing:
        clauses.append({"wing": wing})
    if room:
        clauses.append({"room": room})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _field(res: Any, key: str) -> Any:
    if isinstance(res, dict):
        return res.get(key)
    return getattr(res, key, None)


def _rows_from_collection_result(res: Any) -> list[dict[str, Any]]:
    ids = _field(res, "ids") or []
    docs = _field(res, "documents")
    metas = _field(res, "metadatas")
    embs = _field(res, "embeddings")

    rows = []
    for i, _id in enumerate(ids):
        emb = list(embs[i]) if embs is not None and i < len(embs) and embs[i] is not None else []
        rows.append(
            {
                "id": _id,
                "text": docs[i] if docs is not None and i < len(docs) else "",
                "metadata": metas[i] if metas is not None and i < len(metas) else {},
                "embedding": emb,
            }
        )
    return rows


def _mean_vectors(vectors: list[list[float]]) -> list[float]:
    vectors = [v for v in vectors if v]
    if not vectors:
        return []
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(dim)]


def _group_by_parent(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        meta = row.get("metadata") or {}
        key = next((meta[field] for field in key_fields if meta.get(field)), row["id"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    logical = []
    for key in order:
        members = sorted(
            groups[key],
            key=lambda row: (row.get("metadata") or {}).get("chunk_index", 0),
        )
        meta0 = members[0].get("metadata") or {}
        logical.append(
            {
                "id": key,
                "member_ids": [member["id"] for member in members],
                "text": "\n".join(member.get("text", "") for member in members),
                "embedding": _mean_vectors([member.get("embedding") or [] for member in members]),
                "metadata": meta0,
                "wing": meta0.get("wing"),
                "room": meta0.get("room"),
            }
        )
    return logical


def load_logical_drawers(
    palace_path: str, wing: str | None = None, room: str | None = None
) -> list[dict[str, Any]]:
    """Read drawers (optionally scoped) and return logical drawers with mean embeddings."""
    from mempalace.palace import get_collection  # lazy: heavy import

    col = get_collection(palace_path)
    kwargs: dict[str, Any] = {"include": ["documents", "metadatas", "embeddings"]}
    where = _where(wing, room)
    if where:
        kwargs["where"] = where
    res = col.get(**kwargs)
    return group_logical_drawers(_rows_from_collection_result(res))


def load_observation_entries(
    palace_path: str,
    wing: str | None = None,
    rooms: tuple[str, ...] = ("diary",),
) -> list[dict[str, Any]]:
    """Read diary/observation drawers as logical entries for pattern induction."""
    from mempalace.palace import get_collection  # lazy: heavy import

    col = get_collection(palace_path)
    rows = []
    room_names = tuple(room.strip() for room in rooms if room and room.strip())
    for room in room_names:
        kwargs: dict[str, Any] = {"include": ["documents", "metadatas", "embeddings"]}
        where = _where(wing, room)
        if where:
            kwargs["where"] = where
        rows.extend(_rows_from_collection_result(col.get(**kwargs)))

    entries = []
    for logical in _group_by_parent(rows, ("parent_entry_id", "parent_drawer_id")):
        meta = logical.get("metadata") or {}
        text = logical["text"]
        entries.append(
            {
                "id": logical["id"],
                "member_ids": logical["member_ids"],
                "text": text,
                "embedding": logical["embedding"],
                "session_id": extract_session_id(text),
                "agent": meta.get("agent"),
                "date": meta.get("date"),
                "topic": meta.get("topic"),
                "wing": meta.get("wing"),
                "room": meta.get("room"),
            }
        )
    return entries


def load_active_triples(palace_path: str) -> list[dict[str, Any]]:
    """Read currently-active KG triples from the palace-local SQLite store."""
    db_path = os.path.join(palace_path, "knowledge_graph.sqlite3")
    if not os.path.exists(db_path):
        return []

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)

    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT
                s.name AS subject,
                t.predicate AS predicate,
                o.name AS object,
                t.valid_from AS valid_from,
                t.extracted_at AS extracted_at
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            WHERE t.valid_to IS NULL
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


class MempalaceWriter:
    """Writes through the sanctioned MCP tool handlers against the bound palace."""

    def __init__(self) -> None:
        from mempalace.mcp_server import TOOLS  # lazy

        self._tools = TOOLS

    def add_drawer(self, wing: str, room: str, content: str) -> Any:
        return self._tools["mempalace_add_drawer"]["handler"](
            wing=wing, room=room, content=content, added_by="dreaming"
        )

    def delete_drawer(self, drawer_id: str) -> Any:
        return self._tools["mempalace_delete_drawer"]["handler"](drawer_id=drawer_id)


class KgWriter:
    """Writes KG invalidations to the palace-local KG, bypassing MCP handlers.

    The MCP ``mempalace_kg_invalidate`` handler resolves the KG path through a
    CLI-only ``_palace_flag_given`` gate; library imports without ``--palace``
    would target the default user KG. Direct ``KnowledgeGraph`` use keeps
    contradiction adoption scoped to the requested palace path.
    """

    def __init__(self, palace_path: str) -> None:
        from mempalace.knowledge_graph import KnowledgeGraph  # lazy

        self._kg = KnowledgeGraph(db_path=os.path.join(palace_path, "knowledge_graph.sqlite3"))

    def invalidate(self, subject: str, predicate: str, object: str, ended: str | None = None) -> Any:
        return self._kg.invalidate(subject, predicate, object, ended=ended)

    def close(self) -> None:
        self._kg.close()


if __name__ == "__main__":  # tiny manual smoke: print drawer count for a scope
    import argparse

    ap = argparse.ArgumentParser(description="Smoke-read logical drawers from a palace.")
    ap.add_argument("--palace", required=True)
    ap.add_argument("--wing")
    ap.add_argument("--room")
    args = ap.parse_args()
    path = bind_palace(args.palace)
    drawers = load_logical_drawers(path, args.wing, args.room)
    print(f"{len(drawers)} logical drawers in {path} (wing={args.wing} room={args.room})")
