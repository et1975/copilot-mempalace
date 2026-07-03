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
from typing import Any

from dream_lib import group_logical_drawers


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
    return group_logical_drawers(rows)


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
