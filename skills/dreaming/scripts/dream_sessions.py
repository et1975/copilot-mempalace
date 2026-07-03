"""Read-only Copilot host session-store adapter for the dreaming pipeline.

This module is intentionally host-coupled: it knows about Copilot's local
``session-store.db`` schema and isolates that coupling away from the portable
mempalace adapter in ``dream_palace.py``. It never imports mempalace and should
only issue SELECT queries against the host-owned SQLite store.
"""
from __future__ import annotations

import argparse
import os
import sqlite3


_TEXT_CAP = 4000
_ELLIPSIS = "…"


def default_store_path() -> str:
    """Return the Copilot host session store path, honoring an env override."""
    override = os.environ.get("COPILOT_SESSION_STORE")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.copilot/session-store.db")


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open ``db_path`` for read-only queries, falling back for test fixtures."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def load_sessions(
    db_path: str | None = None,
    repository: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Load session metadata ordered by creation time."""
    store_path = db_path or default_store_path()
    if not os.path.exists(store_path):
        return []

    clauses = []
    params: list[object] = []
    if repository:
        clauses.append("repository LIKE ?")
        params.append(f"%{repository}%")
    if since:
        clauses.append("created_at >= ?")
        params.append(since)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = "LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)

    con = _connect_ro(store_path)
    try:
        rows = con.execute(
            f"""
            SELECT
                id AS session_id,
                repository,
                branch,
                summary,
                created_at,
                updated_at,
                cwd
            FROM sessions
            {where_sql}
            ORDER BY created_at ASC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def load_session_turns(session_id: str, db_path: str | None = None) -> list[dict]:
    """Load turns for one host session ordered by turn index."""
    store_path = db_path or default_store_path()
    if not os.path.exists(store_path):
        return []

    con = _connect_ro(store_path)
    try:
        rows = con.execute(
            """
            SELECT
                turn_index,
                user_message,
                assistant_response,
                timestamp
            FROM turns
            WHERE session_id = ?
            ORDER BY turn_index ASC
            """,
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def _bounded_text(parts: list[str], max_chars: int = _TEXT_CAP) -> str:
    text = "\n".join(parts)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(_ELLIPSIS)] + _ELLIPSIS


def load_session_observations(
    db_path: str | None = None,
    repository: str | None = None,
    since: str | None = None,
    limit_sessions: int | None = None,
) -> list[dict]:
    """Load session-attributed user-message observations for pattern mining."""
    store_path = db_path or default_store_path()
    sessions = load_sessions(store_path, repository=repository, since=since, limit=limit_sessions)

    observations: list[dict] = []
    for session in sessions:
        turns = load_session_turns(str(session["session_id"]), store_path)
        user_messages = [
            turn["user_message"]
            for turn in turns
            if isinstance(turn.get("user_message"), str) and turn["user_message"]
        ]
        observations.append(
            {
                "session_id": session["session_id"],
                "repository": session["repository"],
                "created_at": session["created_at"],
                "summary": session["summary"],
                "text": _bounded_text(user_messages),
                "turn_count": len(turns),
            }
        )
    return observations


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-read Copilot host sessions.")
    parser.add_argument("--repository")
    parser.add_argument("--db")
    args = parser.parse_args()

    sessions = load_sessions(db_path=args.db, repository=args.repository)
    print(f"{len(sessions)} sessions")
