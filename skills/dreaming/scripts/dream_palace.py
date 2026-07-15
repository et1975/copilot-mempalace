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
import secrets
import sqlite3
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from dream_lib import TRUSTED_STATUSES, cosine_similarity, normalize_predicate

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
CONTROLLER_STATES = {
    "open",
    "deducing",
    "deduced",
    "gap_selected",
    "acquire_proposed",
    "acquire_approved",
    "acquiring",
    "acquired",
    "extracting",
    "claim_extracted",
    "assert_proposed",
    "assert_approved",
    "asserting",
    "asserted",
    "fixpoint",
    "budget_exhausted",
    "abandoned",
}
CONTROLLER_TERMINAL_STATES = {"fixpoint", "budget_exhausted", "abandoned"}
S1_CONTROLLER_TRANSITIONS = {
    ("open", "start_deduction"): "deducing",
    ("asserted", "start_deduction"): "deducing",
    ("deducing", "record_deduction"): "deduced",
    ("deduced", "select_gap"): "gap_selected",
    ("deduced", "finish_fixpoint"): "fixpoint",
    ("gap_selected", "finish_fixpoint"): "fixpoint",
    ("asserted", "finish_fixpoint"): "fixpoint",
}
S1_ACTIONS = {"start_deduction", "record_deduction", "select_gap", "finish_fixpoint", "abandon"}
BROKER_PROVISIONAL_WRITABLE_STATES = {"deduced", "gap_selected", "acquired", "claim_extracted"}


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
    """
    CREATE TABLE IF NOT EXISTS contemplate_runs (
      run_id TEXT PRIMARY KEY,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL,
      expired_at TEXT, metadata_json TEXT NOT NULL DEFAULT '{}');
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_expiry ON contemplate_runs(status, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS contemplate_run_events (
      event_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      version_before INTEGER NOT NULL,
      version_after INTEGER NOT NULL,
      from_state TEXT NOT NULL,
      to_state TEXT NOT NULL,
      action TEXT NOT NULL,
      actor TEXT NOT NULL,
      created_at TEXT NOT NULL,
      FOREIGN KEY(run_id) REFERENCES contemplate_runs(run_id));
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_events_run ON contemplate_run_events(run_id, version_after)",
    """
    CREATE TABLE IF NOT EXISTS contemplate_approvals (
      approval_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      run_version_issued INTEGER NOT NULL,
      approval_kind TEXT NOT NULL,
      tool_name TEXT NOT NULL,
      args_hash TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL DEFAULT 'issued',
      issued_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      consumed_at TEXT,
      FOREIGN KEY(run_id) REFERENCES contemplate_runs(run_id));
    """,
    "CREATE INDEX IF NOT EXISTS idx_approvals_run ON contemplate_approvals(run_id, status)",
    """
    CREATE TABLE IF NOT EXISTS contemplate_provisional_facts (
      provisional_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, fact_key TEXT NOT NULL,
      subject TEXT, predicate TEXT, object TEXT,
      subject_id TEXT, object_id TEXT,
      status TEXT NOT NULL DEFAULT 'abduced',
      confidence REAL, source_kind TEXT, source_ref TEXT,
      created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
      expires_at TEXT NOT NULL, expired_at TEXT, fact_status TEXT NOT NULL DEFAULT 'active',
      UNIQUE(run_id, fact_key),
      FOREIGN KEY(run_id) REFERENCES contemplate_runs(run_id));
    """,
    "CREATE INDEX IF NOT EXISTS idx_prov_active ON contemplate_provisional_facts(run_id, fact_status, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS kg_verification_events (
      event_id TEXT PRIMARY KEY,
      provisional_id TEXT,
      new_support_id TEXT NOT NULL,
      triple_id TEXT NOT NULL,
      verification_kind TEXT NOT NULL,
      claim_digest TEXT NOT NULL,
      run_id TEXT, evidence_ref TEXT, evidence_quote TEXT,
      created_at TEXT NOT NULL);
    """,
    "CREATE INDEX IF NOT EXISTS idx_verif_support ON kg_verification_events(new_support_id)",
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
        if "expires_at" not in _table_columns(con, "kg_triple_supports"):
            con.execute("ALTER TABLE kg_triple_supports ADD COLUMN expires_at TEXT")
        _ensure_provisional_entity_columns(con)
        _ensure_controlled_run_columns(con)
        con.commit()
    finally:
        con.close()


def _utc_now_iso(now: datetime | str | None = None) -> str:
    if now is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        dt = now
    elif isinstance(now, str):
        text = now[:-1] + "+00:00" if now.endswith("Z") else now
        dt = datetime.fromisoformat(text)
    else:
        raise TypeError("now must be None, datetime, or ISO timestamp string")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _add_hours_iso(now_iso: str, ttl_hours: float) -> str:
    dt = datetime.fromisoformat(now_iso)
    return (dt + timedelta(hours=ttl_hours)).isoformat()


def _add_seconds_iso(now_iso: str, secs: int) -> str:
    dt = datetime.fromisoformat(now_iso)
    return (dt + timedelta(seconds=secs)).isoformat()


def _new_owner_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_owner_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _provisional_fact_key(
    subject: Any,
    predicate: Any,
    object: Any,
    source_kind: Any,
    source_ref: Any,
) -> str:
    parts = [subject, predicate, object, source_kind, source_ref]
    text = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_args_hash(tool_name: str, args: dict) -> str:
    canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((tool_name + "\n" + canonical_args).encode()).hexdigest()


def _provisional_approval_args(
    subject: Any,
    predicate: Any,
    object: Any,
    status: str,
    source_kind: Any,
    source_ref: Any,
) -> dict:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "status": status,
        "source_kind": source_kind,
        "source_ref": source_ref,
    }


def _entity_id(name: Any) -> str:
    return str(name).lower().replace(" ", "_").replace("'", "")


def _entity_exists(con: sqlite3.Connection, entity_id: str) -> bool:
    if not _has_table(con, "entities"):
        return False
    return con.execute("SELECT 1 FROM entities WHERE id=? LIMIT 1", (entity_id,)).fetchone() is not None


def _ensure_provisional_entity_columns(con: sqlite3.Connection) -> None:
    columns = _table_columns(con, "contemplate_provisional_facts")
    if "subject_id" not in columns:
        con.execute("ALTER TABLE contemplate_provisional_facts ADD COLUMN subject_id TEXT")
    if "object_id" not in columns:
        con.execute("ALTER TABLE contemplate_provisional_facts ADD COLUMN object_id TEXT")


def _ensure_controlled_run_columns(con: sqlite3.Connection) -> None:
    columns = _table_columns(con, "contemplate_runs")
    if "state" not in columns:
        con.execute("ALTER TABLE contemplate_runs ADD COLUMN state TEXT NOT NULL DEFAULT 'open'")
    if "version" not in columns:
        con.execute("ALTER TABLE contemplate_runs ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
    if "owner_token_hash" not in columns:
        con.execute("ALTER TABLE contemplate_runs ADD COLUMN owner_token_hash TEXT")
    if "lease_expires_at" not in columns:
        con.execute("ALTER TABLE contemplate_runs ADD COLUMN lease_expires_at TEXT")
    if "lease_ttl_seconds" not in columns:
        con.execute(
            "ALTER TABLE contemplate_runs ADD COLUMN lease_ttl_seconds INTEGER NOT NULL DEFAULT 300"
        )


def _check_provisional_endpoints(con: sqlite3.Connection, subject: Any, object: Any) -> tuple[str, str]:
    subject_id = _entity_id(subject)
    object_id = _entity_id(object)
    subject_exists = _entity_exists(con, subject_id)
    object_exists = _entity_exists(con, object_id)
    if not subject_exists:
        raise ValueError(f"provisional subject does not exist in KG entities: {subject!r}")
    if not object_exists:
        raise ValueError(f"provisional object does not exist in KG entities: {object!r}")
    return subject_id, object_id


def _write_provisional_row(
    con: sqlite3.Connection,
    run_id: str,
    subject: Any,
    predicate: Any,
    object: Any,
    *,
    status: str,
    confidence: float | None,
    source_kind: str | None,
    source_ref: str | None,
    now_iso: str,
    run_expires_at: str,
) -> str:
    subject_id, object_id = _check_provisional_endpoints(con, subject, object)
    fact_key = _provisional_fact_key(subject, predicate, object, source_kind, source_ref)
    provisional_id = str(uuid4())
    con.execute(
        """
        INSERT INTO contemplate_provisional_facts(
            provisional_id, run_id, fact_key,
            subject, predicate, object, subject_id, object_id,
            status, confidence, source_kind, source_ref,
            created_at, last_seen_at, expires_at, expired_at, fact_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'active')
        ON CONFLICT(run_id, fact_key) DO UPDATE SET
            subject=excluded.subject,
            predicate=excluded.predicate,
            object=excluded.object,
            subject_id=excluded.subject_id,
            object_id=excluded.object_id,
            status=excluded.status,
            confidence=excluded.confidence,
            source_kind=excluded.source_kind,
            source_ref=excluded.source_ref,
            last_seen_at=excluded.last_seen_at,
            expires_at=excluded.expires_at,
            expired_at=NULL,
            fact_status='active'
        """,
        (
            provisional_id,
            run_id,
            fact_key,
            subject,
            predicate,
            object,
            subject_id,
            object_id,
            status,
            confidence,
            source_kind,
            source_ref,
            now_iso,
            now_iso,
            run_expires_at,
        ),
    )
    row = con.execute(
        """
        SELECT provisional_id
        FROM contemplate_provisional_facts
        WHERE run_id=? AND fact_key=?
        """,
        (run_id, fact_key),
    ).fetchone()
    return str(row["provisional_id"])


def create_or_resume_run(
    palace_path,
    run_id: str | None = None,
    *,
    ttl_hours: float = 24,
    now: datetime | str | None = None,
) -> str:
    """Create a contemplate run or refresh last_seen_at for an active unexpired run."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    expires_at = _add_hours_iso(now_iso, ttl_hours)
    chosen_run_id = run_id or str(uuid4())

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT status, expires_at FROM contemplate_runs WHERE run_id=?",
            (chosen_run_id,),
        ).fetchone()
        if row is not None:
            if row["status"] != "active" or row["expires_at"] <= now_iso:
                raise ValueError(f"contemplate run is not active: {chosen_run_id}")
            con.execute(
                "UPDATE contemplate_runs SET last_seen_at=? WHERE run_id=?",
                (now_iso, chosen_run_id),
            )
        else:
            con.execute(
                """
                INSERT INTO contemplate_runs(
                    run_id, status, created_at, last_seen_at, expires_at, expired_at, metadata_json
                ) VALUES (?, 'active', ?, ?, ?, NULL, '{}')
                """,
                (chosen_run_id, now_iso, now_iso, expires_at),
            )
        con.commit()
        return chosen_run_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _controller_refusal(
    run_id: str,
    state: str | None,
    version: int | None,
    code: str,
    message: str,
) -> dict:
    return {
        "ok": False,
        "run_id": run_id,
        "state": state,
        "version": version,
        "refusal": {"code": code, "message": message},
    }


def _insert_run_event(
    con: sqlite3.Connection,
    *,
    run_id: str,
    version_before: int,
    version_after: int,
    from_state: str,
    to_state: str,
    action: str,
    actor: str,
    created_at: str,
) -> None:
    con.execute(
        """
        INSERT INTO contemplate_run_events(
            event_id, run_id, version_before, version_after,
            from_state, to_state, action, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            run_id,
            version_before,
            version_after,
            from_state,
            to_state,
            action,
            actor,
            created_at,
        ),
    )


def create_or_resume_controlled_run(
    palace_path,
    *,
    run_id: str | None = None,
    ttl_hours: float = 24.0,
    lease_ttl_seconds: int = 300,
    now: datetime | str | None = None,
) -> dict:
    """Create or take over a leased contemplate controller run."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    expires_at = _add_hours_iso(now_iso, ttl_hours)
    lease_expires_at = _add_seconds_iso(now_iso, lease_ttl_seconds)
    chosen_run_id = run_id or str(uuid4())
    owner_token = _new_owner_token()
    owner_hash = _hash_owner_token(owner_token)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT status, state, version, lease_expires_at, expires_at
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (chosen_run_id,),
        ).fetchone()
        if row is None:
            con.execute(
                """
                INSERT INTO contemplate_runs(
                    run_id, status, state, version, owner_token_hash,
                    lease_expires_at, lease_ttl_seconds,
                    created_at, last_seen_at, expires_at, expired_at, metadata_json
                ) VALUES (?, 'active', 'open', 0, ?, ?, ?, ?, ?, ?, NULL, '{}')
                """,
                (
                    chosen_run_id,
                    owner_hash,
                    lease_expires_at,
                    lease_ttl_seconds,
                    now_iso,
                    now_iso,
                    expires_at,
                ),
            )
            con.commit()
            return {
                "run_id": chosen_run_id,
                "owner_token": owner_token,
                "state": "open",
                "version": 0,
                "lease_expires_at": lease_expires_at,
                "resumed": False,
                "took_over": False,
            }

        state = str(row["state"])
        version_before = int(row["version"])
        current_lease = row["lease_expires_at"]
        if row["status"] != "active" or row["expires_at"] <= now_iso:
            raise ValueError(f"contemplate run is not active: {chosen_run_id}")
        if state in CONTROLLER_TERMINAL_STATES:
            raise ValueError(f"contemplate run is terminal: {chosen_run_id}")
        if current_lease is not None and current_lease > now_iso:
            raise ValueError(f"contemplate run is leased by another driver: {chosen_run_id}")

        version_after = version_before + 1
        con.execute(
            """
            UPDATE contemplate_runs
            SET owner_token_hash=?,
                version=?,
                lease_expires_at=?,
                last_seen_at=?
            WHERE run_id=?
            """,
            (owner_hash, version_after, lease_expires_at, now_iso, chosen_run_id),
        )
        _insert_run_event(
            con,
            run_id=chosen_run_id,
            version_before=version_before,
            version_after=version_after,
            from_state=state,
            to_state=state,
            action="takeover",
            actor="controller",
            created_at=now_iso,
        )
        con.commit()
        return {
            "run_id": chosen_run_id,
            "owner_token": owner_token,
            "state": state,
            "version": version_after,
            "lease_expires_at": lease_expires_at,
            "resumed": False,
            "took_over": True,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def renew_lease(
    palace_path,
    run_id,
    *,
    owner_token,
    expected_version,
    now: datetime | str | None = None,
) -> dict:
    """Renew a controller lease without changing state or version."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    owner_hash = _hash_owner_token(owner_token)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT status, state, version, owner_token_hash, lease_expires_at, lease_ttl_seconds
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            con.rollback()
            return _controller_refusal(run_id, None, None, "unknown_run", f"unknown run: {run_id}")

        lease_expires_at = _add_seconds_iso(now_iso, int(row["lease_ttl_seconds"]))
        cur = con.execute(
            """
            UPDATE contemplate_runs
            SET lease_expires_at=?,
                last_seen_at=?
            WHERE run_id=?
              AND version=?
              AND owner_token_hash=?
              AND status='active'
            """,
            (lease_expires_at, now_iso, run_id, expected_version, owner_hash),
        )
        if cur.rowcount == 1:
            con.commit()
            return {
                "ok": True,
                "run_id": run_id,
                "version": expected_version,
                "lease_expires_at": lease_expires_at,
            }

        state = row["state"]
        version = int(row["version"])
        if version != expected_version:
            code = "cas_conflict"
            message = f"expected version {expected_version}, found {version}"
        elif row["owner_token_hash"] != owner_hash:
            code = "lease_lost"
            message = f"lease lost for run: {run_id}"
        else:
            code = "bad_state"
            message = f"run cannot renew lease in current state: {run_id}"
        con.rollback()
        return _controller_refusal(run_id, state, version, code, message)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def controller_step(
    palace_path,
    run_id,
    action,
    payload=None,
    *,
    owner_token,
    expected_version,
    now: datetime | str | None = None,
) -> dict:
    """Accept or refuse one deterministic S1 controller transition."""
    del payload
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    owner_hash = _hash_owner_token(owner_token)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT status, state, version, owner_token_hash, lease_expires_at, lease_ttl_seconds
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            con.rollback()
            return _controller_refusal(run_id, None, None, "unknown_run", f"unknown run: {run_id}")

        state = str(row["state"])
        version = int(row["version"])
        if action == "abandon" and state not in CONTROLLER_TERMINAL_STATES:
            to_state = "abandoned"
        elif (state, action) in S1_CONTROLLER_TRANSITIONS:
            to_state = S1_CONTROLLER_TRANSITIONS[(state, action)]
        elif action not in S1_ACTIONS:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "unsupported_action",
                f"unsupported controller action: {action}",
            )
        elif state in CONTROLLER_TERMINAL_STATES:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "terminal",
                f"contemplate run is terminal: {run_id}",
            )
        else:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "bad_transition",
                f"cannot {action} from state {state}",
            )

        new_lease = _add_seconds_iso(now_iso, int(row["lease_ttl_seconds"]))
        cur = con.execute(
            """
            UPDATE contemplate_runs
            SET state=?,
                version=version+1,
                last_seen_at=?,
                lease_expires_at=?
            WHERE run_id=?
              AND version=?
              AND owner_token_hash=?
              AND state=?
              AND lease_expires_at > ?
              AND status='active'
            """,
            (to_state, now_iso, new_lease, run_id, expected_version, owner_hash, state, now_iso),
        )
        if cur.rowcount == 1:
            _insert_run_event(
                con,
                run_id=run_id,
                version_before=expected_version,
                version_after=expected_version + 1,
                from_state=state,
                to_state=to_state,
                action=action,
                actor="agent",
                created_at=now_iso,
            )
            con.commit()
            return {"ok": True, "run_id": run_id, "state": to_state, "version": expected_version + 1}

        current_lease = row["lease_expires_at"]
        if version != expected_version:
            code = "cas_conflict"
            message = f"expected version {expected_version}, found {version}"
        elif row["owner_token_hash"] != owner_hash or current_lease is None or current_lease <= now_iso:
            code = "lease_lost"
            message = f"lease lost for run: {run_id}"
        else:
            code = "bad_state"
            message = f"run state changed before transition: {run_id}"
        con.rollback()
        return _controller_refusal(run_id, state, version, code, message)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def assert_provisional(
    palace_path,
    run_id,
    subject,
    predicate,
    object,
    *,
    status: str = "abduced",
    confidence: float | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    now: datetime | str | None = None,
) -> str:
    """Record a tainted, run-scoped provisional fact without touching the durable KG."""
    if status in TRUSTED_STATUSES:
        raise ValueError(f"provisional facts cannot use trusted status: {status!r}")

    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        _ensure_provisional_entity_columns(con)
        con.execute("BEGIN IMMEDIATE")
        run = con.execute(
            """
            SELECT expires_at, owner_token_hash
            FROM contemplate_runs
            WHERE run_id=?
              AND status='active'
              AND expires_at > ?
            """,
            (run_id, now_iso),
        ).fetchone()
        if run is None:
            raise ValueError(f"contemplate run is not active: {run_id}")
        if run["owner_token_hash"] is not None:
            raise ValueError(f"controlled run requires broker; use broker_assert_provisional: {run_id}")

        provisional_id = _write_provisional_row(
            con,
            run_id,
            subject,
            predicate,
            object,
            status=status,
            confidence=confidence,
            source_kind=source_kind,
            source_ref=source_ref,
            now_iso=now_iso,
            run_expires_at=run["expires_at"],
        )
        con.commit()
        return provisional_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def issue_approval(
    palace_path,
    run_id,
    *,
    owner_token,
    expected_version,
    approval_kind,
    tool_name,
    canonical_args,
    ttl_seconds=300,
    now: datetime | str | None = None,
) -> dict:
    """Mint a one-use controller approval token for a controlled run."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    owner_hash = _hash_owner_token(owner_token)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT status, state, version, owner_token_hash, lease_expires_at
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            con.rollback()
            return _controller_refusal(run_id, None, None, "unknown_run", f"unknown run: {run_id}")

        state = str(row["state"])
        version = int(row["version"])
        current_lease = row["lease_expires_at"]
        if version != expected_version:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "cas_conflict",
                f"expected version {expected_version}, found {version}",
            )
        if row["owner_token_hash"] != owner_hash or current_lease is None or current_lease <= now_iso:
            con.rollback()
            return _controller_refusal(run_id, state, version, "lease_lost", f"lease lost for run: {run_id}")
        if state in CONTROLLER_TERMINAL_STATES:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "terminal",
                f"contemplate run is terminal: {run_id}",
            )
        if row["status"] != "active":
            con.rollback()
            return _controller_refusal(run_id, state, version, "inactive_run", f"run is not active: {run_id}")

        approval_token = _new_owner_token()
        token_hash = _hash_owner_token(approval_token)
        args_hash = _canonical_args_hash(tool_name, canonical_args)
        approval_id = str(uuid4())
        expires_at = _add_seconds_iso(now_iso, ttl_seconds)
        con.execute(
            """
            INSERT INTO contemplate_approvals(
                approval_id, run_id, run_version_issued, approval_kind,
                tool_name, args_hash, token_hash, status, issued_at, expires_at, consumed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'issued', ?, ?, NULL)
            """,
            (
                approval_id,
                run_id,
                expected_version,
                approval_kind,
                tool_name,
                args_hash,
                token_hash,
                now_iso,
                expires_at,
            ),
        )
        con.commit()
        return {
            "ok": True,
            "approval_id": approval_id,
            "approval_token": approval_token,
            "expires_at": expires_at,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def broker_assert_provisional(
    palace_path,
    run_id,
    *,
    owner_token,
    expected_version,
    approval_token,
    subject,
    predicate,
    object,
    status: str = "acquired",
    confidence: float | None = None,
    source_kind: str | None = None,
    source_ref: str | None = None,
    now: datetime | str | None = None,
) -> dict:
    """Consume a one-use controller approval and write a controlled provisional fact."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    owner_hash = _hash_owner_token(owner_token)
    approval_token_hash = _hash_owner_token(approval_token)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        _ensure_provisional_entity_columns(con)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            SELECT status, state, version, owner_token_hash, lease_expires_at,
                   lease_ttl_seconds, expires_at
            FROM contemplate_runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            con.rollback()
            return _controller_refusal(run_id, None, None, "unknown_run", f"unknown run: {run_id}")

        state = str(row["state"])
        version = int(row["version"])
        current_lease = row["lease_expires_at"]
        if status in TRUSTED_STATUSES:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "bad_status",
                f"provisional facts cannot use trusted status: {status!r}",
            )
        if version != expected_version:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "cas_conflict",
                f"expected version {expected_version}, found {version}",
            )
        if row["owner_token_hash"] is None or row["owner_token_hash"] != owner_hash or current_lease is None or current_lease <= now_iso:
            con.rollback()
            return _controller_refusal(run_id, state, version, "lease_lost", f"lease lost for run: {run_id}")
        if state in CONTROLLER_TERMINAL_STATES:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "terminal",
                f"contemplate run is terminal: {run_id}",
            )
        if row["status"] != "active" or row["expires_at"] <= now_iso:
            con.rollback()
            return _controller_refusal(run_id, state, version, "inactive_run", f"run is not active: {run_id}")
        approval = con.execute(
            """
            SELECT *
            FROM contemplate_approvals
            WHERE token_hash=?
            """,
            (approval_token_hash,),
        ).fetchone()
        if approval is None:
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_invalid", "approval token is invalid")
        if approval["run_id"] != run_id:
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_invalid", "approval token is invalid")
        if approval["approval_kind"] != "assert_provisional" or approval["tool_name"] != "assert_provisional":
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_invalid", "approval token is invalid")
        if approval["status"] == "consumed":
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_consumed", "approval token already consumed")
        if approval["status"] == "expired" or approval["expires_at"] <= now_iso:
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_expired", "approval token expired")
        if int(approval["run_version_issued"]) != expected_version:
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_stale", "approval token was issued for a different run version")

        expected_args_hash = _canonical_args_hash(
            "assert_provisional",
            _provisional_approval_args(subject, predicate, object, status, source_kind, source_ref),
        )
        if approval["args_hash"] != expected_args_hash:
            con.rollback()
            return _controller_refusal(run_id, state, version, "args_mismatch", "approval token arguments do not match")
        if state not in BROKER_PROVISIONAL_WRITABLE_STATES:
            con.rollback()
            return _controller_refusal(
                run_id,
                state,
                version,
                "bad_transition",
                f"cannot broker assert provisional from state {state}",
            )

        provisional_id = _write_provisional_row(
            con,
            run_id,
            subject,
            predicate,
            object,
            status=status,
            confidence=confidence,
            source_kind=source_kind,
            source_ref=source_ref,
            now_iso=now_iso,
            run_expires_at=row["expires_at"],
        )
        cur = con.execute(
            """
            UPDATE contemplate_approvals
            SET status='consumed',
                consumed_at=?
            WHERE token_hash=?
              AND status='issued'
            """,
            (now_iso, approval_token_hash),
        )
        if cur.rowcount != 1:
            con.rollback()
            return _controller_refusal(run_id, state, version, "approval_consumed", "approval token already consumed")

        new_lease = _add_seconds_iso(now_iso, int(row["lease_ttl_seconds"]))
        cur = con.execute(
            """
            UPDATE contemplate_runs
            SET state='asserted',
                version=version+1,
                last_seen_at=?,
                lease_expires_at=?
            WHERE run_id=?
              AND version=?
              AND owner_token_hash=?
              AND state=?
              AND lease_expires_at > ?
              AND status='active'
            """,
            (now_iso, new_lease, run_id, expected_version, owner_hash, state, now_iso),
        )
        if cur.rowcount != 1:
            con.rollback()
            return _controller_refusal(run_id, state, version, "bad_state", f"run state changed before broker assert: {run_id}")

        _insert_run_event(
            con,
            run_id=run_id,
            version_before=expected_version,
            version_after=expected_version + 1,
            from_state=state,
            to_state="asserted",
            action="broker_assert_provisional",
            actor="controller",
            created_at=now_iso,
        )
        con.commit()
        return {
            "ok": True,
            "run_id": run_id,
            "provisional_id": provisional_id,
            "state": "asserted",
            "version": expected_version + 1,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def assert_user_fact_from_provisional(
    palace_path,
    provisional_id,
    *,
    confirmation_token,
    run_id,
    evidence_ref=None,
    evidence_quote=None,
    now: datetime | str | None = None,
) -> dict:
    """Promote a human-confirmed provisional fact into a durable trusted-user support."""
    if run_id is None:
        raise ValueError("run_id is required to promote a provisional fact")

    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT f.provisional_id, f.run_id, f.subject, f.predicate, f.object,
                   f.subject_id, f.object_id, f.confidence, f.created_at, f.expires_at
            FROM contemplate_provisional_facts f
            JOIN contemplate_runs r ON r.run_id = f.run_id
            WHERE f.provisional_id=?
              AND f.run_id=?
              AND f.fact_status='active'
              AND r.status='active'
              AND f.expires_at > ?
              AND r.expires_at > ?
            """,
            (provisional_id, run_id, now_iso, now_iso),
        ).fetchone()
    finally:
        con.close()

    if row is None:
        raise ValueError(f"active provisional fact not found: {provisional_id}")

    subject_id = row["subject_id"] or _entity_id(row["subject"])
    object_id = row["object_id"] or _entity_id(row["object"])
    con = sqlite3.connect(db_path)
    try:
        if not _entity_exists(con, subject_id):
            raise ValueError(f"provisional subject entity no longer exists: {subject_id!r}")
        if not _entity_exists(con, object_id):
            raise ValueError(f"provisional object entity no longer exists: {object_id!r}")
    finally:
        con.close()

    predicate = normalize_predicate(row["predicate"])
    claim_digest = hashlib.sha256(
        f"{subject_id}|{predicate}|{object_id}".encode("utf-8")
    ).hexdigest()
    expected_token = hashlib.sha256(
        f"{provisional_id}|{claim_digest}|{run_id}".encode("utf-8")
    ).hexdigest()
    if confirmation_token != expected_token:
        raise ValueError("confirmation token does not match provisional claim")

    from mempalace.knowledge_graph import KnowledgeGraph  # lazy

    valid_from = _normalize_dt_for_kg(row["created_at"] or now_iso)
    valid_to = None
    kg = KnowledgeGraph(db_path=db_path)
    try:
        triple_id = str(
            kg.add_triple(
                row["subject"],
                predicate,
                row["object"],
                valid_from=valid_from,
                valid_to=valid_to,
                confidence=row["confidence"] if row["confidence"] is not None else 1.0,
                adapter_name="contemplate:user_assert",
                source_drawer_id="promote:" + str(provisional_id),
            )
        )
    finally:
        kg.close()

    support_id = "sup:user:" + hashlib.sha256(
        f"{triple_id}|{provisional_id}".encode("utf-8")
    ).hexdigest()[:32]
    event_id = str(uuid4())

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """
            INSERT OR IGNORE INTO kg_triple_supports(
                support_id, triple_id, status, source_trust, inherited_status,
                conditional_on_triple_ids, scope, source_kind, source_ref,
                valid_from, valid_to, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                support_id,
                triple_id,
                "asserted",
                "trusted_user",
                "asserted",
                "[]",
                "durable",
                "contemplate:user_assert",
                "promote:" + str(provisional_id),
                valid_from,
                valid_to,
                now_iso,
            ),
        )
        con.execute(
            """
            INSERT INTO kg_verification_events(
                event_id, provisional_id, new_support_id, triple_id,
                verification_kind, claim_digest, run_id, evidence_ref,
                evidence_quote, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                provisional_id,
                support_id,
                triple_id,
                "user_confirmed",
                claim_digest,
                run_id,
                evidence_ref,
                evidence_quote,
                now_iso,
            ),
        )
        cur = con.execute(
            """
            UPDATE contemplate_provisional_facts
            SET fact_status='promoted',
                expired_at=?
            WHERE provisional_id=?
              AND fact_status='active'
            """,
            (now_iso, provisional_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"provisional fact is no longer active: {provisional_id}")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    return {
        "triple_id": triple_id,
        "support_id": support_id,
        "event_id": event_id,
        "promoted": True,
    }


def startup_cleanup(palace_path, *, now: datetime | str | None = None) -> dict:
    """Expire stale contemplate state and expired materialized-abduced supports."""
    db_path = _resolve_kg_path(palace_path)
    ensure_firewall_schema(db_path)
    now_iso = _utc_now_iso(now)
    counts = {
        "runs_expired": 0,
        "provisional_expired": 0,
        "abduced_supports_expired": 0,
        "abduced_triples_cascaded": 0,
    }
    cascade_roots: list[str] = []

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            """
            UPDATE contemplate_runs
            SET status='expired',
                expired_at=COALESCE(expired_at, ?)
            WHERE status='active'
              AND expires_at <= ?
            """,
            (now_iso, now_iso),
        )
        counts["runs_expired"] = cur.rowcount

        cur = con.execute(
            """
            UPDATE contemplate_provisional_facts
            SET fact_status='expired',
                expired_at=COALESCE(expired_at, ?)
            WHERE fact_status='active'
              AND expires_at <= ?
            """,
            (now_iso, now_iso),
        )
        counts["provisional_expired"] = cur.rowcount

        expired_supports = con.execute(
            """
            SELECT support_id, triple_id
            FROM kg_triple_supports
            WHERE status='materialized_abduced'
              AND expires_at IS NOT NULL
              AND expires_at <= ?
              AND ended_at IS NULL
            ORDER BY support_id
            """,
            (now_iso,),
        ).fetchall()
        ended_triple_ids: list[str] = []
        for support in expired_supports:
            cur = con.execute(
                """
                UPDATE kg_triple_supports
                SET ended_at=?
                WHERE support_id=?
                  AND ended_at IS NULL
                """,
                (now_iso, support["support_id"]),
            )
            if cur.rowcount:
                counts["abduced_supports_expired"] += cur.rowcount
                ended_triple_ids.append(str(support["triple_id"]))

        for triple_id in sorted(set(ended_triple_ids)):
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
                cascade_roots.append(triple_id)

        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    for triple_id in cascade_roots:
        invalidate_triples_cascade(palace_path, [triple_id], now_iso)
        counts["abduced_triples_cascaded"] += 1

    return counts


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
        durable = load_premises(palace_path, purpose="durable", strict_schema=strict_schema)
        db_path = _resolve_kg_path(palace_path)
        if not os.path.exists(db_path):
            return durable
        ensure_firewall_schema(db_path)
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            con = sqlite3.connect(db_path)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT f.provisional_id, f.subject, f.predicate, f.object,
                       f.subject_id, f.object_id, f.status
                FROM contemplate_provisional_facts f
                JOIN contemplate_runs r ON r.run_id = f.run_id
                WHERE f.run_id=?
                  AND r.status='active'
                  AND f.fact_status='active'
                  AND r.expires_at > r.last_seen_at
                  AND f.expires_at > r.last_seen_at
                ORDER BY f.provisional_id
                """,
                (run_id,),
            ).fetchall()
        finally:
            con.close()
        durable.extend(
            {
                "triple_id": "prov:" + str(row["provisional_id"]),
                "subject": row["subject"],
                "object": row["object"],
                "subject_id": row["subject_id"] or row["subject"],
                "object_id": row["object_id"] or row["object"],
                "predicate": row["predicate"],
                "epistemic_status": row["status"],
                "inherited_status": row["status"],
                "conditional_on": "[]",
                "source_trust": "hypothesis",
                "tainted": True,
            }
            for row in rows
        )
        return durable
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


def kg_protection_degree(palace_path: str) -> dict[str, int]:
    """Return per-drawer counts that should block pruning KG-dependent drawers."""
    degrees = dict(kg_source_degree(palace_path))
    db_path = _resolve_kg_path(palace_path)
    if not os.path.exists(db_path):
        return degrees

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)

    try:
        con.row_factory = sqlite3.Row
        if not (_has_table(con, "triples") and _has_table(con, "kg_derivations")):
            return degrees
        triple_columns = _table_columns(con, "triples")
        derivation_columns = _table_columns(con, "kg_derivations")
        if "valid_to" not in triple_columns or not {
            "conclusion_triple_id",
            "premise_drawer_ids",
        }.issubset(derivation_columns):
            return degrees

        rows = con.execute(
            """
            SELECT d.id, d.premise_drawer_ids
            FROM kg_derivations d
            JOIN triples t ON t.id = d.conclusion_triple_id
            WHERE t.valid_to IS NULL
            ORDER BY d.id
            """
        ).fetchall()
        for row in rows:
            try:
                premise_drawer_ids = json.loads(row["premise_drawer_ids"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(premise_drawer_ids, list):
                continue
            for drawer_id in {str(value) for value in premise_drawer_ids if value}:
                degrees[drawer_id] = degrees.get(drawer_id, 0) + 1
        return degrees
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
    value = re.sub(r"\.\d+", "", value)  # add_triple wants second precision; drop fractional seconds
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
        "orphans_quarantined": 0,
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
        epoch_present = con.execute(
            "SELECT 1 FROM kg_firewall_meta WHERE key='epoch_committed_at' LIMIT 1"
        ).fetchone() is not None
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
                quarantined = False
                if epoch_present:
                    status = "unknown"
                    source_trust = "unknown"
                    source_kind = adapter_name or "unknown"
                    quarantined = True
                else:
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
                if quarantined:
                    counts["orphans_quarantined"] += cur.rowcount

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
