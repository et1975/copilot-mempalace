#!/usr/bin/env python3
"""Wing-scoped logical export / import for a local MemPalace palace.

Companion to the restic-based ``palace_backup.py``: that helper is *physical*
and *whole-palace only* (wings share ChromaDB collections, the HNSW index, and
the SQLite databases, so a file snapshot cannot isolate one wing). This script
exports **one wing's logical contents** — drawers, best-effort KG triples, and
touching tunnels — to a portable JSONL bundle, and replays that bundle back into
a palace. It complements, and does not replace, the restic path.

Design notes
------------
* **Logical, not byte-exact.** Import re-adds drawers (new IDs, re-embedded) and
  re-inserts triples. ``orig_*`` fields are provenance only.
* **Pure transforms live in ``palace_wing_lib``.** This module owns the
  mempalace + palace-SQLite I/O behind lazy imports and thin module-level seams
  so ``palace_wing_lib`` and both test files import cleanly under a plain
  ``python3`` with no ``mempalace`` installed.
* **Palace binding first.** ``bind_palace`` sets ``MEMPALACE_PALACE_PATH`` before
  any mempalace import (mirrors the dreaming CLIs).
* **KG writes go direct.** Triples are written via ``KnowledgeGraph.add_triple``
  (not the MCP handler, which has a CLI-only palace gate), matching dreaming.

Interpreter: this script imports ``mempalace`` at run time, which system
``python3`` cannot do. Run it under the interpreter where mempalace is
installed (e.g. the uv-tool venv) or via ``python -m``; if the import fails it
exits with a clear message rather than a traceback. The shebang stays
``python3`` so the pure library and tests remain runnable anywhere.

Usage::

    ./palace_wing.py export <wing> [--out FILE] [--palace PATH]
    ./palace_wing.py import <bundle> [--into-wing NAME] [--palace PATH]
                             [--dry-run] [--force-add] [--dup-threshold 0.9]
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_wing_lib as lib  # noqa: E402

DEFAULT_PALACE = Path("~/.mempalace").expanduser()

# A representative interpreter that can import mempalace, shown in the guard
# message. Not used as a shebang and not assumed to exist — purely a hint.
_VENV_HINT = "~/.local/share/uv/tools/mempalace/bin/python"


# --------------------------------------------------------------------------- #
# Palace layout resolution.
#
# ``--palace`` always means the mempalace HOME dir (default ``~/.mempalace``).
# Under HOME live two separate stores whose paths resolve by DIFFERENT rules —
# the running MCP server (launched without ``--palace``) reads:
#   * Chroma vectors at ``<palace_path>/chroma.sqlite3`` where ``palace_path`` is
#     ``config.json``'s ``palace_path`` (default ``<home>/palace``), and
#   * the KnowledgeGraph at the HOME-level ``<home>/knowledge_graph.sqlite3``.
# Conflating the two (e.g. pointing ``MEMPALACE_PALACE_PATH`` at HOME) silently
# creates a stray second Chroma DB at ``<home>/chroma.sqlite3`` that the server
# never reads. ``resolve_palace_layout`` centralizes both rules so export and
# import agree with the server.
# --------------------------------------------------------------------------- #
def resolve_palace_layout(home: str | Path) -> tuple[Path, Path]:
    """Map a mempalace HOME dir to ``(chroma_palace_dir, kg_db_path)``.

    ``chroma_palace_dir`` is ``config.json``'s ``palace_path`` when present, else
    ``<home>/palace``. ``kg_db_path`` is the server's HOME-level default
    ``<home>/knowledge_graph.sqlite3``. Mirrors the mempalace MCP server's own
    resolution so drawers and KG triples land where the server reads them.
    """
    home = Path(home).expanduser()
    palace_dir = home / "palace"
    cfg = home / "config.json"
    if cfg.exists():
        try:
            configured = json.loads(cfg.read_text(encoding="utf-8")).get("palace_path")
        except (OSError, ValueError):
            configured = None
        if configured:
            palace_dir = Path(configured).expanduser()
    return palace_dir, home / "knowledge_graph.sqlite3"


# --------------------------------------------------------------------------- #
# mempalace / palace I/O seams.
#
# Every mempalace or palace-SQLite touch point is a module-level function with a
# lazy import inside its body. Importing this module therefore never imports
# mempalace, and tests replace these seams with in-memory fakes.
# --------------------------------------------------------------------------- #
def bind_palace(home: str) -> str:
    """Point mempalace at the palace under mempalace HOME for this process.

    ``home`` is the mempalace HOME dir (default ``~/.mempalace``). Sets
    ``MEMPALACE_PALACE_PATH`` to the RESOLVED Chroma dir (``<home>/palace`` or
    ``config.json``'s ``palace_path``) — never HOME itself — so writes land in
    the same Chroma DB the running server reads, not a stray ``<home>/chroma``.
    Returns the HOME abspath (readers below join their own subpaths from it).
    Call before importing mempalace.
    """
    home_abs = os.path.abspath(os.path.expanduser(home))
    palace_dir, _ = resolve_palace_layout(home_abs)
    os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(str(palace_dir))
    return home_abs


def require_mempalace() -> None:
    """Exit with a human-readable message if ``mempalace`` is not importable."""
    try:
        import mempalace  # noqa: F401
    except ImportError:
        sys.exit(
            "mempalace is not importable under this interpreter "
            f"({sys.executable}).\n"
            "Run this script under the interpreter where mempalace is installed, "
            f"e.g.\n    {_VENV_HINT} "
            f"{os.path.basename(__file__)} ...\n"
            "or invoke it as a module under that venv (python -m)."
        )


def mempalace_version() -> str:
    """Return the installed mempalace version string (best effort).

    Export does not require mempalace to be importable, so this degrades to
    ``"unknown"`` rather than failing when the package is absent.
    """
    try:
        import mempalace  # lazy
    except ImportError:
        return "unknown"
    return getattr(mempalace, "__version__", "unknown")


def read_wing_drawer_rows(palace: str, wing: str) -> list[dict[str, Any]]:
    """Read a wing's drawer chunk rows directly from ``palace/chroma.sqlite3``.

    ``palace`` is the mempalace HOME dir. The Chroma dir is resolved via
    ``resolve_palace_layout`` so a non-default ``config.json`` ``palace_path`` is
    honored. ``get_collection(...).get(...)`` returns nothing in a standalone
    process (the ChromaDB API view is only populated inside the running server),
    so export reads the persisted ``embedding_metadata`` EAV table directly. Each
    chunk ``id`` gathers its keys into a metadata dict; the drawer text is the
    value of the ``chroma:document`` key. The returned rows share the shape
    (``{"id", "text", "metadata"}``) consumed by ``_group_logical_drawers``.
    """
    palace_dir, _ = resolve_palace_layout(palace)
    db_path = os.path.join(str(palace_dir), "chroma.sqlite3")
    if not os.path.exists(db_path):
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)
    try:
        chunk_ids = [
            row[0]
            for row in con.execute(
                "SELECT id FROM embedding_metadata "
                "WHERE key = 'wing' AND string_value = ?",
                (wing,),
            ).fetchall()
        ]
        rows = []
        for chunk_id in chunk_ids:
            meta: dict[str, Any] = {}
            for key, sval, ival, fval, bval in con.execute(
                "SELECT key, string_value, int_value, float_value, bool_value "
                "FROM embedding_metadata WHERE id = ?",
                (chunk_id,),
            ).fetchall():
                value = sval if sval is not None else (
                    ival if ival is not None else (
                        fval if fval is not None else bval
                    )
                )
                meta[key] = value
            text = meta.pop("chroma:document", "") or ""
            rows.append({"id": chunk_id, "text": text, "metadata": meta})
        return rows
    finally:
        con.close()


def palace_drawer_count(palace: str, wing: str | None = None) -> int | None:
    """Count distinct logical drawers in the resolved Chroma DB (read-only).

    Returns the drawer count (optionally filtered to ``wing``), or ``None`` when
    the Chroma DB does not exist yet — the signal used by the import preflight to
    refuse writing into a would-be-new/stray palace. Counts distinct
    ``parent_drawer_id`` (falling back to chunk ``id`` for single-chunk drawers).
    """
    palace_dir, _ = resolve_palace_layout(palace)
    db_path = os.path.join(str(palace_dir), "chroma.sqlite3")
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(db_path)
    try:
        if wing is not None:
            ids = [
                row[0]
                for row in con.execute(
                    "SELECT id FROM embedding_metadata "
                    "WHERE key = 'wing' AND string_value = ?",
                    (wing,),
                ).fetchall()
            ]
        else:
            ids = [
                row[0]
                for row in con.execute(
                    "SELECT DISTINCT id FROM embedding_metadata"
                ).fetchall()
            ]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        parents: set[str] = set()
        rows = con.execute(
            "SELECT id, string_value FROM embedding_metadata "
            f"WHERE key = 'parent_drawer_id' AND id IN ({placeholders})",
            ids,
        ).fetchall()
        have_parent = {row[0] for row in rows}
        parents.update(row[1] for row in rows if row[1])
        parents.update(cid for cid in ids if cid not in have_parent)
        return len(parents)
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def add_drawer(
    wing: str, room: str, content: str, source_file: str | None, added_by: str | None
) -> Any:
    """Add a drawer via the sanctioned MCP handler."""
    from mempalace.mcp_server import TOOLS  # lazy

    handler = TOOLS["mempalace_add_drawer"]["handler"]
    kwargs: dict[str, Any] = {"wing": wing, "room": room, "content": content}
    if source_file is not None:
        kwargs["source_file"] = source_file
    if added_by is not None:
        kwargs["added_by"] = added_by
    return handler(**kwargs)


def check_duplicate(content: str, threshold: float) -> Any:
    """Palace-wide, content-based near-duplicate check via the MCP handler."""
    from mempalace.mcp_server import TOOLS  # lazy

    return TOOLS["mempalace_check_duplicate"]["handler"](
        content=content, threshold=threshold
    )


def create_tunnel(
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str,
) -> Any:
    """Create a tunnel via the MCP handler (which validates room existence)."""
    from mempalace.mcp_server import TOOLS  # lazy

    return TOOLS["mempalace_create_tunnel"]["handler"](
        source_wing=source_wing,
        source_room=source_room,
        target_wing=target_wing,
        target_room=target_room,
        label=label,
    )


def open_kg(palace: str) -> Any:
    """Open the KnowledgeGraph the running server reads for direct triple writes.

    ``palace`` is the mempalace HOME dir; the KG resolves to the HOME-level
    ``<home>/knowledge_graph.sqlite3`` via ``resolve_palace_layout`` — matching
    the server's default — so triples are visible after reconnect.
    """
    from mempalace.knowledge_graph import KnowledgeGraph  # lazy

    _, kg_path = resolve_palace_layout(palace)
    return KnowledgeGraph(db_path=str(kg_path))


def add_triple(
    kg: Any,
    subject: str,
    predicate: str,
    object: str,
    confidence: float | None,
    valid_from: str | None,
    valid_to: str | None,
) -> Any:
    """Insert one triple, passing only kwargs the KG's ``add_triple`` accepts.

    ``source_drawer_id`` is intentionally dropped: the exported ids no longer
    exist after a replay, so re-attaching them would be misleading.
    """
    try:
        params = inspect.signature(kg.add_triple).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, Any] = {}
    for name, value in (
        ("valid_from", valid_from),
        ("valid_to", valid_to),
        ("confidence", confidence),
    ):
        if value is not None and (not params or name in params):
            kwargs[name] = value
    return kg.add_triple(subject, predicate, object, **kwargs)


def read_wing_triples(palace: str) -> list[dict[str, Any]]:
    """Read all active triples' provenance-bearing columns (read-only)."""
    db_path = os.path.join(palace, "knowledge_graph.sqlite3")
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
            SELECT id, subject, predicate, object, valid_from, valid_to,
                   confidence, source_drawer_id
            FROM triples
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def read_tunnels(palace: str) -> list[dict[str, Any]]:
    """Read ``tunnels.json`` (a JSON array) from the palace root."""
    path = os.path.join(palace, "tunnels.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("tunnels", [])
    return []


# --------------------------------------------------------------------------- #
# Drawer grouping (pure over the fetched rows).
# --------------------------------------------------------------------------- #
def _group_logical_drawers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble multi-chunk drawers grouped by ``parent_drawer_id``.

    Chunks are ordered by ``chunk_index`` and their text concatenated. A
    single-chunk drawer (no ``parent_drawer_id``) passes through unchanged. The
    first chunk's metadata represents the logical drawer.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        meta = row.get("metadata") or {}
        key = meta.get("parent_drawer_id") or row["id"]
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
                "text": "\n".join(member.get("text", "") for member in members),
                "metadata": meta0,
            }
        )
    return logical


# Metadata keys carried on a drawer that ``mempalace_add_drawer`` cannot set;
# preserved through the export/import round-trip via the content trailer.
_EXTRA_META_KEYS = ("topic", "hall", "type", "date")


def _extract_extra(meta: dict[str, Any]) -> dict[str, Any]:
    extra = {}
    for key in _EXTRA_META_KEYS:
        value = meta.get(key)
        if value is not None:
            extra[key] = value
    return extra


# --------------------------------------------------------------------------- #
# Subcommands.
# --------------------------------------------------------------------------- #
def cmd_export(args: argparse.Namespace) -> int:
    palace = bind_palace(str(args.palace))
    wing = args.wing

    print(f"Exporting wing {wing!r} from {palace}", file=sys.stderr)

    rows = read_wing_drawer_rows(palace, wing)
    logical = _group_logical_drawers(rows)
    drawer_ids = {item["id"] for item in logical}
    print(f"  drawers: {len(logical)} logical", file=sys.stderr)

    records: list[dict[str, Any]] = []
    for item in logical:
        meta = item.get("metadata") or {}
        records.append(
            lib.drawer_record(
                wing=wing,
                room=meta.get("room") or "general",
                content=item["text"],
                source_file=meta.get("source_file"),
                added_by=meta.get("added_by") or meta.get("agent"),
                orig_drawer_id=item["id"],
                extra=_extract_extra(meta),
            )
        )

    all_triples = read_wing_triples(palace)
    kept_triples, skipped = lib.filter_wing_triples(all_triples, drawer_ids)
    for triple in kept_triples:
        records.append(
            lib.kg_triple_record(
                subject=triple.get("subject"),
                predicate=triple.get("predicate"),
                object=triple.get("object"),
                confidence=triple.get("confidence"),
                valid_from=triple.get("valid_from"),
                valid_to=triple.get("valid_to"),
                orig_source_drawer_id=triple.get("source_drawer_id"),
            )
        )
    print(
        f"  kg triples: {len(kept_triples)} kept, {skipped} skipped",
        file=sys.stderr,
    )

    tunnels = [
        tunnel
        for tunnel in read_tunnels(palace)
        if lib.tunnel_touches_wing(tunnel, wing)
    ]
    for tunnel in tunnels:
        source = tunnel.get("source") or {}
        target = tunnel.get("target") or {}
        records.append(
            lib.tunnel_record(
                source_wing=source.get("wing"),
                source_room=source.get("room"),
                target_wing=target.get("wing"),
                target_room=target.get("room"),
                label=tunnel.get("label", ""),
            )
        )
    print(f"  tunnels: {len(tunnels)} touching", file=sys.stderr)

    kg_note = (
        f"best-effort: {skipped} triple(s) had no resolvable source_drawer_id "
        "and were skipped"
    )
    manifest = lib.build_manifest(
        wing=wing,
        mempalace_version=mempalace_version(),
        counts={
            "drawers": len(logical),
            "kg_triples": len(kept_triples),
            "tunnels": len(tunnels),
        },
        kg_note=kg_note,
        exported_at=datetime.now(timezone.utc).isoformat(),
    )

    all_records = [manifest, *records]
    if getattr(args, "format", "jsonl") == "md":
        out_dir = args.out or f"wing-{wing}-{datetime.now():%Y%m%d-%H%M%S}"
        wing_dir = write_md_dir(all_records, out_dir)
        print(wing_dir)
        return 0

    out_path = args.out or f"wing-{wing}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(lib.dump_jsonl(all_records))
    print(out_path)
    return 0


# --------------------------------------------------------------------------- #
# Markdown-directory (de)serialization — file I/O over lib's pure transforms.
# --------------------------------------------------------------------------- #
def write_md_dir(records: list[dict[str, Any]], out_dir: str) -> str:
    """Write a bundle's records as a human-readable markdown directory.

    Layout: ``<out_dir>/<wing>/`` with one ``.md`` per drawer, ``kg.jsonl`` and
    ``tunnels.jsonl`` (structured), and ``manifest.json`` (index + counts).
    Returns the wing directory path.
    """
    manifest = records[0]
    wing = manifest["wing"]
    drawers = [r for r in records[1:] if r.get("type") == "drawer"]
    triples = [r for r in records[1:] if r.get("type") == "kg_triple"]
    tunnels = [r for r in records[1:] if r.get("type") == "tunnel"]

    wing_dir = os.path.join(os.path.expanduser(out_dir), wing)
    os.makedirs(wing_dir, exist_ok=True)

    index = []
    for i, drawer in enumerate(drawers):
        fname = lib.md_drawer_filename(drawer, i)
        with open(os.path.join(wing_dir, fname), "w", encoding="utf-8") as fh:
            fh.write(lib.encode_drawer_md(drawer))
        index.append({
            "drawer_id": drawer.get("orig_drawer_id"),
            "room": drawer.get("room"),
            "added_by": drawer.get("added_by"),
            "source_file": drawer.get("source_file"),
            "extra": drawer.get("extra") or {},
            "content_file": fname,
        })

    kg_file = "kg.jsonl" if triples else None
    if triples:
        with open(os.path.join(wing_dir, kg_file), "w", encoding="utf-8") as fh:
            fh.write(lib.dump_jsonl(triples))
    tunnels_file = "tunnels.jsonl" if tunnels else None
    if tunnels:
        with open(os.path.join(wing_dir, tunnels_file), "w", encoding="utf-8") as fh:
            fh.write(lib.dump_jsonl(tunnels))

    out_manifest = dict(manifest)
    out_manifest["format"] = "md-dir"
    out_manifest["drawers"] = index
    out_manifest["kg_file"] = kg_file
    out_manifest["tunnels_file"] = tunnels_file
    with open(os.path.join(wing_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(out_manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return wing_dir


def read_md_dir(path: str) -> list[dict[str, Any]]:
    """Read a markdown directory back into bundle records (manifest first).

    Accepts the wing directory (containing ``manifest.json``) or the
    ``manifest.json`` path itself. Auto-detects and delegates to the legacy
    one-file-per-room reader when the drawer files are not the new
    per-drawer format.
    """
    p = Path(os.path.expanduser(path))
    wing_dir = p.parent if p.name == "manifest.json" else p
    manifest_path = wing_dir / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"No manifest.json under {wing_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    entries = manifest.get("drawers") or []
    # Detect legacy (one file per room, merged drawers) vs new (per-drawer).
    if entries:
        first = wing_dir / (entries[0].get("content_file") or "")
        if first.exists():
            head = first.read_text(encoding="utf-8")
            if not head.startswith(lib._MD_OPEN):
                return read_legacy_md_dir(wing_dir, manifest)

    records: list[dict[str, Any]] = [manifest]
    seen_files: set[str] = set()
    for entry in entries:
        cf = entry.get("content_file")
        if not cf or cf in seen_files:
            continue
        seen_files.add(cf)
        text = (wing_dir / cf).read_text(encoding="utf-8")
        records.append(lib.decode_drawer_md(text))

    kg_file = manifest.get("kg_file")
    if kg_file and (wing_dir / kg_file).exists():
        records.extend(lib.parse_jsonl((wing_dir / kg_file).read_text(encoding="utf-8")))
    tunnels_file = manifest.get("tunnels_file")
    if tunnels_file and (wing_dir / tunnels_file).exists():
        records.extend(
            lib.parse_jsonl((wing_dir / tunnels_file).read_text(encoding="utf-8")))
    return records


def read_legacy_md_dir(
    wing_dir: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Best-effort reader for the legacy one-file-per-room markdown export.

    The legacy "copilot-cli-fleet" format wrote one markdown file per ROOM (its
    ``content_file`` shared by several drawers) under a ``<!-- MemPalace wing
    export ... -->`` header, with KG triples as prose under a
    ``## Knowledge-Graph Triples`` heading. Drawers are recovered by splitting a
    shared file at level-2 (``## ``) headers; a lone drawer takes the whole body.
    Marked best-effort: boundaries are heuristic, not authoritative.
    """
    print(
        "  NOTE: legacy one-file-per-room export detected; reconstructing drawers "
        "best-effort by splitting on '## ' headers.",
        file=sys.stderr,
    )
    wing = manifest["wing"]
    entries = manifest.get("drawers") or []
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for entry in entries:
        cf = entry.get("content_file")
        if cf not in groups:
            groups[cf] = []
            order.append(cf)
        groups[cf].append(entry)

    body: list[dict[str, Any]] = []
    bodies: dict[str, str] = {}
    for cf in order:
        group = groups[cf]
        text = _strip_legacy_comment((wing_dir / cf).read_text(encoding="utf-8"))
        bodies[cf] = text
        contents = [text] if len(group) == 1 else _split_h2_slices(text, len(group))
        for entry, content in zip(group, contents):
            body.append(
                lib.drawer_record(
                    wing=wing,
                    room=entry.get("room") or "general",
                    content=content,
                    source_file=(entry.get("source_file") or None),
                    added_by=entry.get("added_by"),
                    orig_drawer_id=entry.get("drawer_id"),
                    extra=entry.get("extra") or {},
                )
            )
    n_drawers = len(body)
    for text in bodies.values():
        for subject, predicate, obj in _parse_legacy_kg_triples(text):
            body.append(
                lib.kg_triple_record(subject, predicate, obj, None, None, None, None))
    n_triples = len(body) - n_drawers

    bundle_manifest = lib.build_manifest(
        wing=wing,
        mempalace_version="legacy",
        counts={"drawers": n_drawers, "kg_triples": n_triples, "tunnels": 0},
        kg_note="legacy import: KG triples parsed best-effort from prose",
        exported_at=str(manifest.get("exported_at") or ""),
    )
    return [bundle_manifest, *body]


_LEGACY_ARROW = "\u2192"


def _strip_legacy_comment(text: str) -> str:
    s = text.lstrip("\ufeff")
    if s.lstrip().startswith("<!--"):
        end = s.find("-->")
        if end != -1:
            s = s[end + 3:]
    return s.lstrip("\n")


def _split_h2_slices(body: str, n: int) -> list[str]:
    starts = [m.start() for m in re.finditer(r"^## ", body, re.MULTILINE)]
    if len(starts) < n:
        raise ValueError(
            f"legacy split needs >= {n} '## ' headers, found {len(starts)}")
    cuts = [0] + starts[1:n] + [len(body)]
    return [body[cuts[i]:cuts[i + 1]] for i in range(n)]


def _parse_legacy_kg_triples(body: str) -> list[tuple[str, str, str]]:
    m = re.search(r"^##\s+Knowledge-Graph Triples\s*$", body, re.MULTILINE)
    if not m:
        return []
    section = body[m.end():]
    nxt = re.search(r"^## ", section, re.MULTILINE)
    if nxt:
        section = section[:nxt.start()]
    out: list[tuple[str, str, str]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("- ") or _LEGACY_ARROW not in line:
            continue
        parts = [p.strip() for p in line[2:].split(_LEGACY_ARROW)]
        if len(parts) == 3 and all(parts):
            out.append((parts[0], parts[1], parts[2]))
    return out


def preflight_import_target(
    home: str, target_wing: str, create_new_palace: bool
) -> None:
    """Stray-palace guard: report resolved paths/counts and refuse a bad target.

    Prints the resolved Chroma dir, KG path, and existing drawer counts, then
    aborts when the Chroma DB does not exist yet (unless ``create_new_palace``) —
    the classic ``--palace ~/.mempalace/palace`` (DB dir) vs ``~/.mempalace``
    (HOME) mistake that silently created a second palace.
    """
    palace_dir, kg_path = resolve_palace_layout(home)
    chroma = os.path.join(str(palace_dir), "chroma.sqlite3")
    total = palace_drawer_count(home)
    existing = palace_drawer_count(home, target_wing)
    print(
        f"  target palace  : {palace_dir}\n"
        f"  chroma db      : {chroma} "
        f"({'exists' if total is not None else 'MISSING'})\n"
        f"  knowledge graph: {kg_path}\n"
        f"  drawers        : total={total if total is not None else 0}, "
        f"wing {target_wing!r}={existing if existing is not None else 0}",
        file=sys.stderr,
    )
    if total is None and not create_new_palace:
        sys.exit(
            f"Refusing to import: no Chroma DB at {chroma}.\n"
            "--palace must be the mempalace HOME dir (e.g. ~/.mempalace), NOT the "
            "nested ~/.mempalace/palace DB dir. If you really mean to initialize a "
            "brand-new palace here, pass --create-new-palace."
        )


def cmd_import(args: argparse.Namespace) -> int:
    # Bind the palace BEFORE importing mempalace: the config layer reads
    # MEMPALACE_PALACE_PATH at import time, so binding after would let --palace
    # be ignored and target the wrong palace.
    palace = bind_palace(str(args.palace))
    require_mempalace()

    bundle_path = Path(os.path.expanduser(args.bundle))
    if bundle_path.is_dir() or bundle_path.name == "manifest.json":
        records = read_md_dir(str(bundle_path))
    else:
        with open(args.bundle, encoding="utf-8") as fh:
            records = lib.parse_jsonl(fh.read())
    if not records:
        sys.exit(f"Bundle is empty: {args.bundle}")
    manifest = records[0]
    try:
        lib.validate_manifest(manifest)
    except ValueError as exc:
        sys.exit(f"Invalid bundle manifest: {exc}")

    source_wing = manifest["wing"]
    target_wing = args.into_wing or source_wing
    is_clone = bool(args.into_wing) and args.into_wing != source_wing
    bypass_dedup = args.force_add or is_clone
    if bypass_dedup:
        reason = "--force-add" if args.force_add else f"clone into {target_wing!r}"
        print(
            f"WARNING: bypassing near-duplicate detection ({reason}); "
            "palace-wide dedup would otherwise skip clones.",
            file=sys.stderr,
        )

    dry = args.dry_run
    prefix = "[dry-run] " if dry else ""
    print(
        f"{prefix}Importing bundle {args.bundle} -> wing {target_wing!r} in {palace}",
        file=sys.stderr,
    )
    if not dry:
        preflight_import_target(palace, target_wing, args.create_new_palace)

    drawers_added = 0
    drawers_skipped = 0
    triples_added = 0
    tunnels_created = 0
    tunnels_skipped: list[str] = []
    failed = 0

    tunnel_records: list[dict[str, Any]] = []
    triple_records: list[dict[str, Any]] = []

    for record in records[1:]:
        kind = record.get("type")
        if kind == "tunnel":
            tunnel_records.append(record)
            continue
        if kind == "kg_triple":
            triple_records.append(record)
            continue
        if kind != "drawer":
            continue

        content = record.get("content", "")
        extra = record.get("extra") or {}
        full_content = lib.encode_trailer(content, extra)

        if not bypass_dedup:
            try:
                dup = check_duplicate(full_content, args.dup_threshold)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ERROR check_duplicate: {exc}", file=sys.stderr)
                continue
            if isinstance(dup, dict) and dup.get("is_duplicate"):
                drawers_skipped += 1
                continue

        if dry:
            drawers_added += 1
            continue
        try:
            add_drawer(
                wing=target_wing,
                room=record.get("room") or "general",
                content=full_content,
                source_file=record.get("source_file"),
                added_by=record.get("added_by"),
            )
            drawers_added += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR add_drawer: {exc}", file=sys.stderr)

    # KG triples: direct KnowledgeGraph.add_triple; source_drawer_id dropped.
    if triple_records and not dry:
        try:
            kg = open_kg(palace)
        except Exception as exc:  # noqa: BLE001
            failed += len(triple_records)
            print(f"  ERROR open KG (all {len(triple_records)} triple(s) failed): "
                  f"{exc}", file=sys.stderr)
            kg = None
        if kg is not None:
            try:
                for record in triple_records:
                    try:
                        add_triple(
                            kg,
                            subject=record.get("subject"),
                            predicate=record.get("predicate"),
                            object=record.get("object"),
                            confidence=record.get("confidence"),
                            valid_from=record.get("valid_from"),
                            valid_to=record.get("valid_to"),
                        )
                        triples_added += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        print(f"  ERROR add_triple: {exc}", file=sys.stderr)
            finally:
                close = getattr(kg, "close", None)
                if callable(close):
                    close()
    elif triple_records and dry:
        triples_added = len(triple_records)

    # Tunnels last, so both endpoints may already exist. Remap the endpoint in
    # the source wing to the target when cloning under a new name.
    for record in tunnel_records:
        remapped = lib.remap_into_wing(record, source_wing, target_wing)
        source = remapped.get("source") or {}
        target = remapped.get("target") or {}
        if dry:
            tunnels_created += 1
            continue
        try:
            res = create_tunnel(
                source_wing=source.get("wing"),
                source_room=source.get("room"),
                target_wing=target.get("wing"),
                target_room=target.get("room"),
                label=remapped.get("label", ""),
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR create_tunnel: {exc}", file=sys.stderr)
            continue
        if isinstance(res, dict) and res.get("error"):
            tunnels_skipped.append(str(res.get("error")))
            print(f"  skip tunnel: {res.get('error')}", file=sys.stderr)
        else:
            tunnels_created += 1

    print(
        f"{prefix}Summary: drawers added={drawers_added} skipped={drawers_skipped}, "
        f"kg triples added={triples_added}, "
        f"tunnels created={tunnels_created} skipped={len(tunnels_skipped)}",
        file=sys.stderr,
    )
    for reason in tunnels_skipped:
        print(f"  tunnel skip reason: {reason}", file=sys.stderr)
    if failed:
        print(f"{prefix}{failed} record(s) failed.", file=sys.stderr)
    if not dry and (drawers_added or triples_added or tunnels_created):
        after = palace_drawer_count(palace, target_wing)
        if after is None:
            palace_dir, _ = resolve_palace_layout(palace)
            print(
                "  WARNING: no Chroma DB at "
                f"{os.path.join(str(palace_dir), 'chroma.sqlite3')} after import. "
                "mempalace creates a brand-new palace at the HOME level, not "
                "HOME/palace, so a freshly-created palace may be misplaced. Import "
                "into a palace initialized through mempalace, or verify the "
                "resulting location before relying on it.",
                file=sys.stderr,
            )
        else:
            print(
                f"  verify: wing {target_wing!r} now holds {after} drawer(s). Run "
                "MCP mempalace_reconnect (or restart the server) to refresh live "
                "search.",
                file=sys.stderr,
            )
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="palace_wing.py",
        description="Wing-scoped logical export/import for a local MemPalace palace.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    se = sub.add_parser("export", help="Export one wing to a JSONL bundle or md dir.")
    se.add_argument("wing", help="Wing name to export.")
    se.add_argument("--out", help="Output path: bundle file (jsonl) or parent dir "
                                  "(md). Default: wing-<wing>-<ts>[.jsonl].")
    se.add_argument("--format", choices=("jsonl", "md"), default="jsonl",
                    help="jsonl bundle (default) or human-readable markdown dir "
                         "(one file per drawer + kg.jsonl/tunnels.jsonl).")
    se.add_argument(
        "--palace",
        type=lambda s: Path(s).expanduser(),
        default=DEFAULT_PALACE,
        help="mempalace HOME dir (default: ~/.mempalace), NOT the nested "
             "palace/ DB dir.",
    )
    se.set_defaults(func=cmd_export)

    si = sub.add_parser("import", help="Replay a wing bundle (jsonl) or md dir "
                                       "into a palace.")
    si.add_argument("bundle", help="Bundle .jsonl path, or a markdown export dir "
                                   "(or its manifest.json) to import.")
    si.add_argument("--into-wing", help="Import into this wing (clone) instead of the "
                                        "bundle's wing; implies --force-add.")
    si.add_argument(
        "--palace",
        type=lambda s: Path(s).expanduser(),
        default=DEFAULT_PALACE,
        help="mempalace HOME dir (default: ~/.mempalace), NOT the nested "
             "palace/ DB dir.",
    )
    si.add_argument("--dry-run", action="store_true",
                    help="Report actions without performing any writes.")
    si.add_argument("--force-add", action="store_true",
                    help="Bypass near-duplicate detection and add every drawer.")
    si.add_argument("--dup-threshold", type=float, default=0.9,
                    help="Similarity threshold for near-duplicate skip (default: 0.9).")
    si.add_argument("--create-new-palace", action="store_true",
                    help="Allow importing into a HOME with no existing Chroma DB "
                         "(initialize a brand-new palace); otherwise the import "
                         "aborts to avoid creating a stray second palace.")
    si.set_defaults(func=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
