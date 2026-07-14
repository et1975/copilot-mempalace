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
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from dream_lib import cosine_similarity

SESSION_ID_RE = re.compile(
    r"SESSION_ID:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)

ALLOWED_PREMISE_PAIRS = {
    ("asserted", "trusted_legacy"),
    ("asserted", "trusted_user"),
    ("asserted", "verified_source"),
    ("deduced", "trusted_rule"),
}
SUPPORT_ACTIVE_NOW_SQL = "s.ended_at IS NULL AND s.valid_to IS NULL"


def _support_active_now(row) -> bool:
    return row["ended_at"] is None and row["valid_to"] is None

KG_DERIVATIONS_DDL = (
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

FIREWALL_SCHEMA_DDL = (
    KG_DERIVATIONS_DDL,
    """
    CREATE TABLE IF NOT EXISTS kg_triple_supports (
      support_id TEXT PRIMARY KEY,
      triple_id  TEXT NOT NULL,
      status TEXT NOT NULL,
      source_trust TEXT NOT NULL,
      inherited_status TEXT NOT NULL,
      conditional_on_triple_ids TEXT NOT NULL DEFAULT '[]',
      scope TEXT NOT NULL DEFAULT 'durable',
      source_kind TEXT, source_ref TEXT,
      valid_from TEXT, valid_to TEXT,
      created_at TEXT NOT NULL, ended_at TEXT);
    """,
    "CREATE INDEX IF NOT EXISTS idx_supports_triple ON kg_triple_supports(triple_id)",
    "CREATE INDEX IF NOT EXISTS idx_supports_status ON kg_triple_supports(status)",
    """
    CREATE TABLE IF NOT EXISTS kg_derivation_premises (
      derivation_id INTEGER NOT NULL,
      premise_triple_id TEXT NOT NULL,
      PRIMARY KEY (derivation_id, premise_triple_id));
    """,
    "CREATE INDEX IF NOT EXISTS idx_derivprem_premise ON kg_derivation_premises(premise_triple_id)",
    "CREATE INDEX IF NOT EXISTS idx_derivations_conclusion ON kg_derivations(conclusion_triple_id)",
    """
    CREATE TABLE IF NOT EXISTS kg_firewall_meta (
      key TEXT PRIMARY KEY, value TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    """,
)


def bind_palace(palace_path: str) -> str:
    """Point mempalace at ``palace_path`` for this process. Call before imports."""
    abspath = os.path.abspath(os.path.expanduser(palace_path))
    os.environ["MEMPALACE_PALACE_PATH"] = abspath
    return abspath


def _resolve_kg_path(palace_path: str) -> str | None:
    """Resolve the KG SQLite path for palace-local and home-level layouts."""
    palace_dir = os.path.abspath(os.path.expanduser(palace_path))
    if palace_dir.endswith(".sqlite3"):
        return palace_dir
    palace_local = os.path.join(palace_dir, "knowledge_graph.sqlite3")
    home_level = os.path.abspath(os.path.join(palace_dir, os.pardir, "knowledge_graph.sqlite3"))
    for db_path in (palace_local, home_level):
        if os.path.exists(db_path):
            print(f"dream_palace: KG resolved to {db_path}", file=sys.stderr)
            return db_path
    return palace_local


def ensure_firewall_schema(db_path: str) -> None:
    """Create B1.0 epistemic-firewall sidecars in the KG SQLite database."""
    con = sqlite3.connect(db_path)
    try:
        for ddl in FIREWALL_SCHEMA_DDL:
            con.execute(ddl)
        con.commit()
    finally:
        con.close()


def _derived_support_id(triple_id: str, rule_id: str, candidate_id: str) -> str:
    key = f"{triple_id}|{rule_id}|{candidate_id}".encode("utf-8")
    return "sup:" + hashlib.sha256(key).hexdigest()[:32]


def _legacy_support_id(triple_id: str) -> str:
    return "sup:legacy:" + triple_id


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


# Injected framework context that pollutes raw host-session user messages. These
# blocks describe the harness (skills, reminders, environment), not the user's
# intent, so they are stripped before embedding session observations.
_CONTEXT_TAGS = (
    "skill-context",
    "system_reminder",
    "system-reminder",
    "system_notification",
    "available_skills",
    "current_datetime",
    "session_context",
    "environment_context",
    "custom_instruction",
    "functions",
)
_CONTEXT_BLOCK_RES = [
    re.compile(rf"<{tag}\b[^>]*>.*?</{tag}>", re.DOTALL | re.IGNORECASE)
    for tag in _CONTEXT_TAGS
]
_UNCLOSED_CONTEXT_RE = re.compile(
    r"<(?:skill-context|system_reminder|system-reminder)\b.*",
    re.DOTALL | re.IGNORECASE,
)
_HOOK_LINE_RE = re.compile(
    r"^\s*(?:Additional context from preToolUse hook:.*|\[palace-reflex\].*|\[fsx-[a-z]+\].*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_context_boilerplate(text: str) -> str:
    """Remove injected skill/hook/system context from a raw session message."""
    if not text:
        return ""
    cleaned = text
    for pattern in _CONTEXT_BLOCK_RES:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _UNCLOSED_CONTEXT_RE.sub(" ", cleaned)
    cleaned = _HOOK_LINE_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _resolve_embed_fn(collection: Any):
    """Resolve the palace collection's embedding function.

    Prefers any public wrapper attribute; falls back to the underlying Chroma
    collection's private embedding function. Isolated here so a future public
    mempalace embed API is a one-line swap.
    """
    for attr in ("embedding_function", "_embedding_function"):
        fn = getattr(collection, attr, None)
        if callable(fn):
            return fn
    inner = getattr(collection, "_collection", None)
    if inner is not None:
        fn = getattr(inner, "_embedding_function", None)
        if callable(fn):
            return fn
    raise RuntimeError("could not resolve palace embedding function for session observations")


def _palace_embed(palace_path: str, texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` in the palace's own embedding space (same as drawers)."""
    if not texts:
        return []
    from mempalace.palace import get_collection  # lazy: heavy import

    embed_fn = _resolve_embed_fn(get_collection(palace_path))
    return [[float(value) for value in vector] for vector in embed_fn(list(texts))]


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


def list_wings(palace_path: str) -> list[str]:
    """Return the sorted distinct wing names present in the palace collection."""
    from mempalace.palace import get_collection  # lazy: heavy import

    col = get_collection(palace_path)
    res = col.get(include=["metadatas"])
    metas = _field(res, "metadatas") or []
    wings = {meta.get("wing") for meta in metas if meta and meta.get("wing")}
    return sorted(wings)


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


def load_session_observation_entries(
    palace_path: str,
    repository: str | None = None,
    since: str | None = None,
    limit_sessions: int | None = None,
) -> list[dict[str, Any]]:
    """Read raw Copilot host sessions as pattern-mining observation entries.

    Bridges the host-only ``dream_sessions`` adapter into the palace embedding
    space: strips injected framework boilerplate, embeds each session's user
    text with the palace's own embedder, and returns entries shaped exactly like
    :func:`load_observation_entries` so both pools cluster together. Each entry's
    support key is the real host-minted ``session_id``.
    """
    import dream_sessions  # host-only adapter; never imports mempalace

    observations = dream_sessions.load_session_observations(
        repository=repository,
        since=since,
        limit_sessions=limit_sessions,
    )

    cleaned: list[tuple[dict[str, Any], str]] = []
    for obs in observations:
        text = _strip_context_boilerplate(obs.get("text") or "")
        if text:
            cleaned.append((obs, text))
    if not cleaned:
        return []

    vectors = _palace_embed(palace_path, [text for _obs, text in cleaned])

    entries = []
    for (obs, text), embedding in zip(cleaned, vectors):
        session_id = obs.get("session_id")
        entry_id = f"session:{session_id}"
        entries.append(
            {
                "id": entry_id,
                "member_ids": [entry_id],
                "text": text,
                "embedding": embedding,
                "session_id": str(session_id) if session_id is not None else None,
                "agent": None,
                "date": obs.get("created_at"),
                "topic": obs.get("summary"),
                "wing": None,
                "room": "__session__",
            }
        )
    return entries


def retrieve_relevant_session_observations(
    palace_path: str,
    query: str,
    *,
    k: int = 5,
    repository: str | None = None,
    since: str | None = None,
    limit_sessions: int | None = None,
    min_similarity: float = 0.0,
) -> list[dict]:
    """Return the top-k host-session observations most relevant to ``query``."""
    if k <= 0:
        return []

    entries = load_session_observation_entries(
        palace_path,
        repository=repository,
        since=since,
        limit_sessions=limit_sessions,
    )
    if not entries:
        return []

    query_vec = _palace_embed(palace_path, [query])[0]
    ranked = []
    for entry in entries:
        embedding = entry.get("embedding") or []
        similarity = 0.0 if not query_vec or not embedding else float(cosine_similarity(query_vec, embedding))
        if similarity < min_similarity:
            continue
        result = dict(entry)
        result["similarity"] = similarity
        ranked.append(result)

    ranked.sort(key=lambda entry: -entry["similarity"])
    return ranked[:k]


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


def load_premises(
    palace_path: str,
    *,
    purpose: str = "durable",
    run_id: str | None = None,
    strict_schema: bool = True,
) -> list[dict]:
    """Load KG triples through the B1.1 epistemic firewall premise contract."""
    if purpose == "audit":
        return load_active_triples(palace_path)
    if purpose == "simulation":
        if run_id is None:
            raise ValueError("load_premises(purpose='simulation') requires run_id")
        # B1.5 will add the run's provisional overlay; B1.1 returns durable premises only.
        return load_premises(palace_path, purpose="durable", strict_schema=strict_schema)
    if purpose != "durable":
        raise ValueError(f"unknown load_premises purpose: {purpose!r}")

    db_path = _resolve_kg_path(palace_path)
    if not os.path.exists(db_path):
        return []

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT value FROM kg_firewall_meta WHERE key='epoch_committed_at'"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()

    if row is None:
        reconcile_firewall_provenance(palace_path)

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)

    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT
                t.id AS triple_id,
                subj.name AS subject,
                t.subject AS subject_id,
                t.predicate AS predicate,
                obj.name AS object,
                t.object AS object_id,
                t.valid_from AS valid_from,
                t.valid_to AS valid_to,
                t.extracted_at AS extracted_at,
                t.source_drawer_id AS source_drawer_id,
                t.confidence AS confidence,
                s.status AS epistemic_status,
                s.source_trust AS source_trust,
                s.inherited_status AS inherited_status,
                s.conditional_on_triple_ids AS conditional_on
            FROM triples t
            JOIN kg_triple_supports s ON s.triple_id = t.id
            JOIN entities subj ON t.subject = subj.id
            JOIN entities obj ON t.object = obj.id
            WHERE t.valid_to IS NULL
              AND {SUPPORT_ACTIVE_NOW_SQL}
              AND s.status IN ('asserted', 'deduced')
              AND s.inherited_status IN ('asserted', 'deduced')
              AND s.conditional_on_triple_ids = '[]'
              AND (
                (s.status = 'asserted' AND s.source_trust IN ('trusted_legacy', 'trusted_user', 'verified_source'))
                OR (s.status = 'deduced' AND s.source_trust = 'trusted_rule')
              )
            GROUP BY t.id
            ORDER BY t.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        if strict_schema:
            raise
        return []
    finally:
        con.close()


def _eligible_triple_ids(palace_path: str) -> set[str]:
    """Return durable-premise-eligible active triple ids via the authoritative loader."""
    return {
        str(triple["triple_id"])
        for triple in load_premises(palace_path, purpose="durable")
    }


def _revalidate_premise_ids(palace_path: str, premise_ids: list[Any]) -> None:
    """B1.2/3 C4: independently reject any premise that is provisional or not
    durable-premise-eligible. Never mutates the ids; fails closed."""
    eligible = _eligible_triple_ids(palace_path)
    for premise_id in premise_ids:
        pid = str(premise_id)
        if pid.startswith("prov:") or pid not in eligible:
            raise ValueError(f"premise not grounded/eligible: {pid}")


def _support_ids(con: sqlite3.Connection) -> set[str]:
    if not _has_table(con, "kg_triple_supports"):
        return set()
    return {
        str(row[0])
        for row in con.execute("SELECT support_id FROM kg_triple_supports").fetchall()
    }


def _parse_derivation_premise_ids(raw: Any, derivation_id: Any) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError) as ex:
        raise ValueError(f"malformed premise_triple_ids for derivation {derivation_id}") from ex
    if not isinstance(parsed, list):
        raise ValueError(f"malformed premise_triple_ids for derivation {derivation_id}")
    return [str(premise_id) for premise_id in parsed if premise_id]


def _active_support_clause() -> str:
    return "ended_at IS NULL AND valid_to IS NULL"


def _is_independently_asserted(con: sqlite3.Connection, triple_id: str) -> bool:
    row = con.execute(
        f"""
        SELECT 1
        FROM kg_triple_supports s
        WHERE s.triple_id = ?
          AND s.status = 'asserted'
          AND {_active_support_clause()}
        LIMIT 1
        """,
        (triple_id,),
    ).fetchone()
    return row is not None


def _load_derivation_graph(con: sqlite3.Connection) -> tuple[dict[int, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    derivations_by_id: dict[int, dict[str, Any]] = {}
    derivations_by_conclusion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    derivations_by_premise: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_by_premise: dict[str, set[int]] = defaultdict(set)

    rows = con.execute(
        """
        SELECT id, candidate_id, conclusion_triple_id, rule_id, premise_triple_ids
        FROM kg_derivations
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        derivation_id = int(row["id"])
        premise_ids = _parse_derivation_premise_ids(row["premise_triple_ids"], derivation_id)
        derivation = {
            "id": derivation_id,
            "candidate_id": row["candidate_id"],
            "conclusion_triple_id": str(row["conclusion_triple_id"]),
            "rule_id": row["rule_id"],
            "premise_ids": premise_ids,
        }
        derivations_by_id[derivation_id] = derivation
        derivations_by_conclusion[derivation["conclusion_triple_id"]].append(derivation)
        for premise_id in premise_ids:
            derivations_by_premise[premise_id].append(derivation)
            seen_by_premise[premise_id].add(derivation_id)

    sidecar_rows = con.execute(
        """
        SELECT derivation_id, premise_triple_id
        FROM kg_derivation_premises
        ORDER BY derivation_id, premise_triple_id
        """
    ).fetchall()
    for row in sidecar_rows:
        derivation_id = int(row["derivation_id"])
        derivation = derivations_by_id.get(derivation_id)
        if derivation is None:
            raise ValueError(f"kg_derivation_premises references missing derivation {derivation_id}")
        premise_id = str(row["premise_triple_id"])
        if derivation_id not in seen_by_premise[premise_id]:
            derivations_by_premise[premise_id].append(derivation)
            seen_by_premise[premise_id].add(derivation_id)

    return derivations_by_id, derivations_by_conclusion, derivations_by_premise


def _active_triple_intervals(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    columns = _table_columns(con, "triples")
    valid_from_expr = "valid_from" if "valid_from" in columns else "NULL AS valid_from"
    valid_to_expr = "valid_to" if "valid_to" in columns else "NULL AS valid_to"
    rows = con.execute(
        f"""
        SELECT id, {valid_from_expr}, {valid_to_expr}
        FROM triples
        WHERE valid_to IS NULL
        """
    ).fetchall()
    return {
        str(row["id"]): {"valid_from": row["valid_from"], "valid_to": row["valid_to"]}
        for row in rows
    }


def _premise_interval_nonempty(triple_intervals: dict[str, dict[str, Any]], premise_ids: list[str]) -> bool:
    if not premise_ids:
        return False
    premises = []
    for premise_id in premise_ids:
        row = triple_intervals.get(str(premise_id))
        if row is None:
            return False
        premises.append(row)
    from dream_lib import premise_interval

    return premise_interval(premises) is not None


def invalidate_triples_cascade(palace_path: str, root_triple_ids: list[str], ended_at: str) -> dict:
    """Force-end roots and atomically invalidate derived dependents without an active proof."""
    db_path = _resolve_kg_path(palace_path)
    if not db_path or not os.path.exists(db_path):
        return {
            "roots_ended": [],
            "cascade_invalidated": [],
            "survived_by_alternate_proof": [],
        }

    ensure_firewall_schema(db_path)
    roots = [str(root_id) for root_id in root_triple_ids]
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")

        root_active_before = {
            str(row["id"])
            for row in con.execute(
                "SELECT id FROM triples WHERE valid_to IS NULL AND id IN (%s)"
                % ",".join("?" for _ in roots),
                roots,
            ).fetchall()
        } if roots else set()
        root_support_active_before = {
            str(row["triple_id"])
            for row in con.execute(
                "SELECT DISTINCT triple_id FROM kg_triple_supports s"
                " WHERE s.triple_id IN (%s) AND %s"
                % (",".join("?" for _ in roots), SUPPORT_ACTIVE_NOW_SQL),
                roots,
            ).fetchall()
        } if roots else set()

        for root_id in roots:
            con.execute(
                "UPDATE triples SET valid_to=? WHERE id=? AND valid_to IS NULL",
                (ended_at, root_id),
            )
            con.execute(
                f"""
                UPDATE kg_triple_supports
                SET ended_at=?
                WHERE triple_id=?
                  AND {_active_support_clause()}
                """,
                (ended_at, root_id),
            )

        _, derivations_by_conclusion, derivations_by_premise = _load_derivation_graph(con)

        affected: set[str] = set()
        queue = deque(roots)
        while queue:
            premise_id = queue.popleft()
            for derivation in derivations_by_premise.get(str(premise_id), []):
                conclusion_id = str(derivation["conclusion_triple_id"])
                if conclusion_id not in affected:
                    affected.add(conclusion_id)
                    queue.append(conclusion_id)

        triple_intervals = _active_triple_intervals(con)
        active_triples = set(triple_intervals)
        independently_asserted = {
            triple_id
            for triple_id in affected
            if triple_id in active_triples and _is_independently_asserted(con, triple_id)
        }
        candidates = {
            triple_id
            for triple_id in affected
            if triple_id in active_triples and triple_id not in independently_asserted
        }

        grounded = set(active_triples - candidates)
        grounded.update(independently_asserted)

        changed = True
        while changed:
            changed = False
            for triple_id in sorted(candidates - grounded):
                for derivation in derivations_by_conclusion.get(triple_id, []):
                    premise_ids = list(derivation["premise_ids"])
                    if not _premise_interval_nonempty(triple_intervals, premise_ids):
                        continue
                    if all(premise_id in active_triples and premise_id in grounded for premise_id in premise_ids):
                        grounded.add(triple_id)
                        changed = True
                        break

        to_end = sorted(candidates - grounded)
        for triple_id in to_end:
            con.execute(
                f"""
                UPDATE kg_triple_supports
                SET ended_at=?
                WHERE triple_id=?
                  AND status='deduced'
                  AND {_active_support_clause()}
                """,
                (ended_at, triple_id),
            )
            has_active_support = con.execute(
                f"""
                SELECT 1
                FROM kg_triple_supports
                WHERE triple_id=?
                  AND {_active_support_clause()}
                LIMIT 1
                """,
                (triple_id,),
            ).fetchone()
            if has_active_support is None:
                con.execute(
                    "UPDATE triples SET valid_to=? WHERE id=? AND valid_to IS NULL",
                    (ended_at, triple_id),
                )

        survived = sorted(candidates & grounded)
        roots_ended = sorted(root_active_before | root_support_active_before)
        con.commit()
        return {
            "roots_ended": roots_ended,
            "cascade_invalidated": to_end,
            "survived_by_alternate_proof": survived,
        }
    except Exception:
        con.rollback()
        raise
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


def _table_columns(con: sqlite3.Connection, table_name: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _has_table(con: sqlite3.Connection, table_name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def reconcile_firewall_provenance(palace_path: str) -> dict[str, int]:
    """Backfill B1.0 support and reverse-index sidecars for an existing KG."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    counts = {
        "triples_scanned": 0,
        "supports_inserted": 0,
        "derivations_scanned": 0,
        "derivation_premises_inserted": 0,
        "malformed_derivations": 0,
        "meta_written": 0,
    }

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        if _has_table(con, "triples"):
            columns = _table_columns(con, "triples")
            valid_from_expr = "t.valid_from" if "valid_from" in columns else "NULL"
            valid_to_expr = "t.valid_to" if "valid_to" in columns else "NULL"
            adapter_expr = "t.adapter_name" if "adapter_name" in columns else "NULL"
            source_expr = "t.source_drawer_id" if "source_drawer_id" in columns else "NULL"
            rows = con.execute(
                f"""
                SELECT
                    t.id AS triple_id,
                    {valid_from_expr} AS valid_from,
                    {valid_to_expr} AS valid_to,
                    {adapter_expr} AS adapter_name,
                    {source_expr} AS source_drawer_id,
                    EXISTS(
                        SELECT 1 FROM kg_derivations d
                        WHERE d.conclusion_triple_id = t.id
                    ) AS has_derivation
                FROM triples t
                WHERE NOT EXISTS (
                    SELECT 1 FROM kg_triple_supports s WHERE s.triple_id = t.id
                )
                ORDER BY t.id
                """
            ).fetchall()
            counts["triples_scanned"] = len(rows)
            for row in rows:
                triple_id = str(row["triple_id"])
                adapter_name = row["adapter_name"]
                source_drawer_id = row["source_drawer_id"]
                is_derive = (
                    bool(row["has_derivation"])
                    or adapter_name == "contemplate:derive"
                    or (isinstance(source_drawer_id, str) and source_drawer_id.startswith("derive:"))
                )
                if is_derive:
                    status = "deduced"
                    source_trust = "trusted_rule"
                    source_kind = adapter_name or "contemplate:derive"
                else:
                    status = "asserted"
                    source_trust = "trusted_legacy"
                    source_kind = adapter_name or "legacy"
                cur = con.execute(
                    "INSERT OR IGNORE INTO kg_triple_supports(support_id, triple_id, status,"
                    " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                    " source_kind, source_ref, valid_from, valid_to, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _legacy_support_id(triple_id),
                        triple_id,
                        status,
                        source_trust,
                        status,
                        "[]",
                        "durable",
                        source_kind,
                        source_drawer_id,
                        row["valid_from"],
                        row["valid_to"],
                        now,
                    ),
                )
                counts["supports_inserted"] += cur.rowcount

        derivation_rows = con.execute("SELECT id, premise_triple_ids FROM kg_derivations ORDER BY id").fetchall()
        counts["derivations_scanned"] = len(derivation_rows)
        for row in derivation_rows:
            try:
                premise_ids = json.loads(row["premise_triple_ids"] or "[]")
            except (TypeError, json.JSONDecodeError):
                counts["malformed_derivations"] += 1
                continue
            if not isinstance(premise_ids, list):
                counts["malformed_derivations"] += 1
                continue
            for premise_id in premise_ids:
                if premise_id:
                    cur = con.execute(
                        "INSERT OR IGNORE INTO kg_derivation_premises(derivation_id, premise_triple_id)"
                        " VALUES (?,?)",
                        (row["id"], str(premise_id)),
                    )
                    counts["derivation_premises_inserted"] += cur.rowcount

        con.execute(
            """
            INSERT OR IGNORE INTO kg_firewall_meta(key, value, created_at)
            VALUES ('epoch_committed_at', ?, ?)
            """,
            (now, now),
        )
        cur = con.execute(
            """
            INSERT INTO kg_firewall_meta(key, value, created_at)
            VALUES ('reconciled_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (now, now),
        )
        counts["meta_written"] = cur.rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return counts


class KgDeriveWriter:
    """Writes derived triples + a kg_derivations lineage row to the resolved KG.

    Direct KnowledgeGraph use (not the mempalace_kg_add MCP handler) for the same
    _palace_flag_given reason the contradiction KgWriter documents. add_triple RETURNS the
    triple id (existing on dedupe, new otherwise) so no post-write re-query is needed.
    """

    _DDL = KG_DERIVATIONS_DDL

    def __init__(self, palace_path):
        from mempalace.knowledge_graph import KnowledgeGraph  # lazy
        self._palace_or_db = palace_path
        self._db_path = _resolve_kg_path(palace_path)
        self._kg = KnowledgeGraph(db_path=self._db_path)
        ensure_firewall_schema(self._db_path)

    def _resolve_name(self, con, entity_id):
        row = con.execute("SELECT name FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"derive references unknown entity id {entity_id!r}; "
                             "premises must come from the live KG loader")
        return row[0]

    def add_derived(self, conclusion, rule_id, premise_ids, premise_drawer_ids,
                    ontology_version, confidence, valid_from, valid_to):
        from dream_lib import derive_candidate_id, normalize_predicate
        preexisting_support_ids: set[str] = set()
        if os.path.exists(self._db_path):
            pre_con = sqlite3.connect(self._db_path)
            try:
                preexisting_support_ids = _support_ids(pre_con)
            finally:
                pre_con.close()
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
        # B1.2/3 C4: independently re-validate premises before any durable write.
        # Never trust the caller; reject provisional or now-ineligible premises.
        _revalidate_premise_ids(self._palace_or_db, premise_ids)
        pred = normalize_predicate(conclusion["predicate"])
        normalized_valid_from = _normalize_dt_for_kg(valid_from)
        normalized_valid_to = _normalize_dt_for_kg(valid_to)
        # add_triple resolves entities by NAME (mempalace is name-keyed) and RETURNS the id.
        triple_id = self._kg.add_triple(
            subj, pred, obj,
            valid_from=normalized_valid_from, valid_to=normalized_valid_to,
            confidence=confidence if confidence is not None else 1.0,
            source_drawer_id="derive:" + rule_id,
            adapter_name="contemplate:derive")
        con = sqlite3.connect(self._db_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR IGNORE INTO kg_derivations(candidate_id, conclusion_triple_id,"
                " rule_id, ontology_version, premise_triple_ids, premise_drawer_ids,"
                " confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (candidate_id, str(triple_id), rule_id, ontology_version,
                 json.dumps(premise_ids), json.dumps(premise_drawer_ids),
                 confidence, now))
            row = con.execute("SELECT id FROM kg_derivations WHERE candidate_id=?", (candidate_id,)).fetchone()
            derivation_id = row[0]
            con.execute(
                "INSERT OR IGNORE INTO kg_triple_supports(support_id, triple_id, status,"
                " source_trust, inherited_status, conditional_on_triple_ids, scope,"
                " source_kind, source_ref, valid_from, valid_to, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _derived_support_id(str(triple_id), rule_id, candidate_id),
                    str(triple_id),
                    "deduced",
                    "trusted_rule",
                    "deduced",
                    "[]",
                    "durable",
                    "contemplate:derive",
                    "derive:" + rule_id,
                    normalized_valid_from,
                    normalized_valid_to,
                    now,
                ),
            )
            legacy_support_id = _legacy_support_id(str(triple_id))
            if legacy_support_id not in preexisting_support_ids:
                con.execute(
                    """
                    DELETE FROM kg_triple_supports
                    WHERE support_id=?
                      AND triple_id=?
                      AND status='asserted'
                      AND source_trust='trusted_legacy'
                    """,
                    (legacy_support_id, str(triple_id)),
                )
            for premise_id in premise_ids:
                if premise_id:
                    con.execute(
                        "INSERT OR IGNORE INTO kg_derivation_premises(derivation_id, premise_triple_id)"
                        " VALUES (?,?)",
                        (derivation_id, str(premise_id)),
                    )
            con.commit()
        except Exception:
            con.rollback()
            raise
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
