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

import json
import os
import hashlib
import inspect
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

SESSION_ID_RE = re.compile(
    r"SESSION_ID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)


def bind_palace(palace_path: str) -> str:
    """Point mempalace at ``palace_path`` for this process. Call before imports."""
    abspath = os.path.abspath(os.path.expanduser(palace_path))
    os.environ["MEMPALACE_PALACE_PATH"] = abspath
    return abspath


def _resolve_kg_path(palace_path: str) -> str | None:
    """Resolve the KG SQLite path for palace-local and home-level layouts."""
    palace_dir = os.path.abspath(os.path.expanduser(palace_path))
    palace_local = os.path.join(palace_dir, "knowledge_graph.sqlite3")
    home_level = os.path.abspath(os.path.join(palace_dir, os.pardir, "knowledge_graph.sqlite3"))
    for db_path in (palace_local, home_level):
        if os.path.exists(db_path):
            print(f"dream_palace: KG resolved to {db_path}", file=sys.stderr)
            return db_path
    return palace_local


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


def _session_id_state(text: str) -> tuple[str | None, bool]:
    ids = list(dict.fromkeys(match.lower() for match in SESSION_ID_RE.findall(text)))
    if len(ids) == 1:
        return ids[0], False
    if len(ids) > 1:
        return None, True
    return None, False


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
    return _group_by_parent(_rows_from_collection_result(res), ("parent_drawer_id",))


def load_drawer_by_id(palace_path: str, drawer_id: str) -> dict[str, Any] | None:
    """Read a current logical drawer by id, reassembling chunks and hashing text."""
    from mempalace.palace import get_collection  # lazy: heavy import

    col = get_collection(palace_path)
    include = ["documents", "metadatas", "embeddings"]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for kwargs in (
        {"where": {"parent_drawer_id": drawer_id}, "include": include},
        {"ids": [drawer_id], "include": include},
    ):
        for row in _rows_from_collection_result(col.get(**kwargs)):
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])

    if not rows:
        return None

    logicals = _group_by_parent(rows, ("parent_drawer_id",))
    logical = next((item for item in logicals if item["id"] == drawer_id), None)
    if logical is None:
        if len(logicals) != 1:
            return None
        logical = logicals[0]

    text = logical["text"]
    return {
        "id": logical["id"],
        "text": text,
        "metadata": logical["metadata"],
        "embedding": logical["embedding"],
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


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
        session_id, ambiguous = _session_id_state(text)
        entry = {
            "id": logical["id"],
            "member_ids": logical["member_ids"],
            "text": text,
            "embedding": logical["embedding"],
            "session_id": session_id,
            "agent": meta.get("agent"),
            "date": meta.get("date"),
            "topic": meta.get("topic"),
            "wing": meta.get("wing"),
            "room": meta.get("room"),
        }
        if ambiguous:
            entry["ambiguous"] = True
        entries.append(entry)
    return entries


def load_active_triples(palace_path: str) -> list[dict[str, Any]]:
    """Read currently-active KG triples from the resolved KG SQLite store."""
    db_path = _resolve_kg_path(palace_path)
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
                t.id AS triple_id,
                s.name AS subject,
                t.subject AS subject_id,
                t.predicate AS predicate,
                o.name AS object,
                t.object AS object_id,
                t.valid_from AS valid_from,
                t.valid_to AS valid_to,
                t.extracted_at AS extracted_at,
                t.source_drawer_id AS source_drawer_id,
                t.confidence AS confidence
            FROM triples t
            JOIN entities s ON t.subject = s.id
            JOIN entities o ON t.object = o.id
            WHERE t.valid_to IS NULL
            ORDER BY t.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def kg_source_degree(palace_path: str) -> dict[str, int]:
    """Return per-drawer counts of KG triples sourced from each drawer id."""
    db_path = _resolve_kg_path(palace_path)
    if not os.path.exists(db_path):
        return {}

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)

    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT source_drawer_id, COUNT(*) AS n
            FROM triples
            WHERE source_drawer_id IS NOT NULL
            GROUP BY source_drawer_id
            """
        ).fetchall()
        return {row["source_drawer_id"]: int(row["n"]) for row in rows}
    finally:
        con.close()


class MempalaceWriter:
    """Writes through the sanctioned MCP tool handlers against the bound palace."""

    def __init__(self) -> None:
        from mempalace.mcp_server import TOOLS  # lazy

        self._tools = TOOLS

    def add_drawer(
        self,
        wing: str,
        room: str,
        content: str,
        added_by: str = "dreaming",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        handler = self._tools["mempalace_add_drawer"]["handler"]
        kwargs: dict[str, Any] = {"wing": wing, "room": room, "content": content, "added_by": added_by}
        if metadata:
            try:
                sig = inspect.signature(handler)
                accepts_metadata = "metadata" in sig.parameters or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()
                )
            except (TypeError, ValueError):
                accepts_metadata = False
            if accepts_metadata:
                kwargs["metadata"] = metadata
            else:
                # The shipped MCP add_drawer schema has no metadata field, so keep
                # provenance reversible by appending a machine-readable trailer.
                meta_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                kwargs["content"] = f"{content}\n\n<!--dreaming-meta: {meta_json}-->"
        return self._tools["mempalace_add_drawer"]["handler"](
            **kwargs
        )

    def delete_drawer(self, drawer_id: str) -> Any:
        return self._tools["mempalace_delete_drawer"]["handler"](drawer_id=drawer_id)


class Archiver:
    """Archive-before-delete guarantees reversibility.

    Deletes go through the sanctioned handler so the closet/AAAK index is purged.
    """

    def __init__(
        self,
        palace_path: str,
        archive_path: str | None = None,
        writer: Any | None = None,
        collection: Any | None = None,
    ) -> None:
        self.palace_path = os.path.abspath(os.path.expanduser(palace_path))
        if archive_path is None:
            archive_path = os.path.join(self.palace_path, "dream-archive.jsonl")
        self.archive_path = os.path.abspath(os.path.expanduser(archive_path))
        self._writer = writer if writer is not None else MempalaceWriter()
        self._collection = collection

    def _get_collection(self) -> Any:
        if self._collection is None:
            from mempalace.palace import get_collection  # lazy: heavy import

            self._collection = get_collection(self.palace_path)
        return self._collection

    def _reload_rows(self, member_ids: list[str]) -> list[dict[str, Any]]:
        res = self._get_collection().get(
            ids=member_ids,
            include=["documents", "metadatas", "embeddings"],
        )
        rows = _rows_from_collection_result(res)
        by_id = {row["id"]: row for row in rows}
        missing = [drawer_id for drawer_id in member_ids if drawer_id not in by_id]
        if missing:
            raise ValueError(f"archive preflight failed; missing drawer ids: {missing}")
        return [by_id[drawer_id] for drawer_id in member_ids]

    def archive_then_delete(self, record: dict[str, Any]) -> dict[str, Any]:
        member_ids = list(record.get("member_ids") or [record["id"]])
        rows = self._reload_rows(member_ids)
        archive_record = {
            "schema": 1,
            "id": record["id"],
            "member_ids": member_ids,
            "wing": record.get("wing"),
            "room": record.get("room"),
            "salience": record.get("salience"),
            "reason": record.get("reason", "prune"),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "rows": [
                {
                    "id": row["id"],
                    "document": row.get("text", ""),
                    "metadata": row.get("metadata") or {},
                    "embedding": row.get("embedding") or [],
                }
                for row in rows
            ],
        }

        parent = os.path.dirname(self.archive_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(self.archive_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(archive_record, ensure_ascii=False))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        deleted = []
        for drawer_id in member_ids:
            try:
                self._writer.delete_drawer(drawer_id)
            except Exception as ex:
                ex.args = (*ex.args, {"deleted": deleted.copy(), "failed": drawer_id})
                raise
            else:
                deleted.append(drawer_id)
        return {"archived": record["id"], "deleted": deleted}


class KgWriter:
    """Writes KG invalidations to the resolved KG, bypassing MCP handlers.

    The MCP ``mempalace_kg_invalidate`` handler resolves the KG path through a
    CLI-only ``_palace_flag_given`` gate; library imports without ``--palace``
    would target the default user KG. Direct ``KnowledgeGraph`` use keeps
    contradiction adoption scoped to the requested palace path.
    """

    def __init__(self, palace_path: str) -> None:
        from mempalace.knowledge_graph import KnowledgeGraph  # lazy

        self._db_path = _resolve_kg_path(palace_path)
        self._kg = KnowledgeGraph(db_path=self._db_path)

    def invalidate(self, subject: str, predicate: str, object: str, ended: str | None = None) -> Any:
        return self._kg.invalidate(subject, predicate, object, ended=ended)

    def invalidate_triples(self, triple_ids: list[str], ended: str | None = None) -> int:
        if not os.path.exists(self._db_path):
            return 0
        ended_at = ended or datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(self._db_path)
        try:
            count = 0
            for triple_id in triple_ids:
                cur = con.execute(
                    "UPDATE triples SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (ended_at, triple_id),
                )
                count += cur.rowcount
            con.commit()
            return count
        finally:
            con.close()

    def close(self) -> None:
        self._kg.close()


load_active_triples_with_ids = load_active_triples  # alias: proofs need t.id (already returned)

def load_ontology_config(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return list(data.get("rules") or [])
    if isinstance(data, list):
        return list(data)
    return []

def load_skip_markers(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out

def append_skip_markers(path, markers):
    if not markers:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for m in markers:
            f.write(json.dumps(m, separators=(",", ":")) + "\n")


def _normalize_dt_for_kg(value):
    """Normalize a date/datetime string to what mempalace add_triple accepts.

    premise_interval() returns isoformat() which for midnight datetimes gives
    'YYYY-MM-DDTHH:MM:SS' (no Z).  add_triple expects either 'YYYY-MM-DD' or
    'YYYY-MM-DDTHH:MM:SSZ'.  Convert accordingly.
    """
    if not value or not isinstance(value, str) or "T" not in value:
        return value
    if value.endswith("Z"):
        return value
    # naive datetime string: if midnight, return date-only; otherwise append Z (assume UTC)
    if value.endswith("T00:00:00"):
        return value[:10]
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value + "Z"


class KgDeriveWriter:
    """Writes derived triples + a kg_derivations lineage row to the resolved KG.

    Direct KnowledgeGraph use (not the mempalace_kg_add MCP handler) for the same
    _palace_flag_given reason the contradiction KgWriter documents. add_triple RETURNS the
    triple id (existing on dedupe, new otherwise) so no post-write re-query is needed.
    """

    _DDL = (
        "CREATE TABLE IF NOT EXISTS kg_derivations("
        " id INTEGER PRIMARY KEY,"
        " candidate_id TEXT UNIQUE,"
        " conclusion_triple_id TEXT,"
        " rule_id TEXT,"
        " ontology_version TEXT,"
        " premise_triple_ids TEXT,"
        " premise_drawer_ids TEXT,"
        " confidence REAL,"
        " created_at TEXT)"
    )

    def __init__(self, palace_path):
        from mempalace.knowledge_graph import KnowledgeGraph  # lazy
        self._db_path = _resolve_kg_path(palace_path)
        self._kg = KnowledgeGraph(db_path=self._db_path)
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(self._DDL)
            con.commit()
        finally:
            con.close()

    def _resolve_name(self, con, entity_id):
        row = con.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"derive references unknown entity id {entity_id!r}; "
                             "premises must come from the live KG loader")
        return row[0]

    def add_derived(self, conclusion, rule_id, premise_ids, premise_drawer_ids,
                    ontology_version, confidence, valid_from, valid_to):
        from dream_lib import derive_candidate_id, normalize_predicate
        candidate_id = derive_candidate_id(conclusion, rule_id, premise_ids, ontology_version)
        con = sqlite3.connect(self._db_path)
        try:
            if con.execute("SELECT 1 FROM kg_derivations WHERE candidate_id=?",
                           (candidate_id,)).fetchone():
                return {"ok": True, "idempotent": True}
            subj = self._resolve_name(con, conclusion["subject_id"])
            obj = self._resolve_name(con, conclusion["object_id"])
        finally:
            con.close()
        pred = normalize_predicate(conclusion["predicate"])
        # add_triple resolves entities by NAME (mempalace is name-keyed) and RETURNS the id.
        triple_id = self._kg.add_triple(
            subj, pred, obj,
            valid_from=_normalize_dt_for_kg(valid_from), valid_to=_normalize_dt_for_kg(valid_to),
            confidence=confidence if confidence is not None else 1.0,
            source_drawer_id="derive:" + rule_id,
            adapter_name="contemplate:derive")
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                "INSERT OR IGNORE INTO kg_derivations(candidate_id, conclusion_triple_id,"
                " rule_id, ontology_version, premise_triple_ids, premise_drawer_ids,"
                " confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (candidate_id, str(triple_id), rule_id, ontology_version,
                 json.dumps(premise_ids), json.dumps(premise_drawer_ids),
                 confidence, datetime.now(timezone.utc).isoformat()))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "triple_id": triple_id}

    def close(self):
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
