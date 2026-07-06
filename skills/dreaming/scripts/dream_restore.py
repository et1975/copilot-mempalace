#!/usr/bin/env python3
"""dream_restore — restore drawers from the append-only prune archive.

Restores archived logical drawers by adding new drawers with the original
wing/room and reconstructed content. The original physical ids and chunking are
not resurrected: mempalace mints new drawer ids and recomputes embeddings.

Usage:
    python3 dream_restore.py --palace ~/.mempalace/palace
    python3 dream_restore.py --palace ~/.mempalace/palace --dry-run
    python3 dream_restore.py --palace ~/.mempalace/palace --id logical-id
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from typing import Any, TextIO

import dream_palace


def iter_archive_records(
    path: str,
    *,
    id_filter: str | None = None,
    reason_filter: str | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """Return archive records from ``path``, optionally filtered by id/reason."""
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as ex:
                if strict:
                    raise ValueError(f"malformed JSON on line {line_no} of {path}: {ex.msg}") from ex
                continue
            if not isinstance(record, dict):
                if strict:
                    raise ValueError(f"archive line {line_no} is not a JSON object")
                continue
            if id_filter is not None and record.get("id") != id_filter:
                continue
            if reason_filter is not None and record.get("reason") != reason_filter:
                continue
            records.append(record)
    return records


def record_to_content(record: dict[str, Any]) -> str:
    """Reconstruct a logical drawer by concatenating row documents in member order."""
    rows = record.get("rows") or []
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    member_ids = list(record.get("member_ids") or [])

    ordered_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member_id in member_ids:
        row = by_id.get(member_id)
        if row is not None:
            ordered_rows.append(row)
            seen.add(member_id)
    if not member_ids:
        ordered_rows.extend(row for row in rows if isinstance(row, dict))

    return "\n".join(str(row.get("document") or "") for row in ordered_rows if isinstance(row, dict))


def _metadata_for_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "restored_from_archive": record.get("archived_at"),
        "original_id": record.get("id"),
        "reason": record.get("reason"),
    }
    original_metadata = {
        row["id"]: row.get("metadata") or {}
        for row in record.get("rows") or []
        if isinstance(row, dict) and row.get("id") and isinstance(row.get("metadata") or {}, dict)
    }
    if original_metadata:
        metadata["original_metadata_by_member_id"] = original_metadata
    return metadata


def _preview(content: str, limit: int = 80) -> str:
    compact = " ".join(content.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def restore(
    records: Iterable[dict[str, Any]],
    writer: Any,
    *,
    dry_run: bool = False,
    out: TextIO | None = None,
) -> dict[str, Any]:
    """Restore records through ``writer.add_drawer``; keep going after failures."""
    output = out if out is not None else sys.stdout
    report: dict[str, Any] = {"restored": 0, "skipped": 0, "errors": [], "new_drawers": []}
    for record in records:
        content = record_to_content(record)
        wing = record.get("wing")
        room = record.get("room")
        logical_id = record.get("id")
        if dry_run:
            print(
                f"WOULD restore {logical_id} to {wing}/{room}: "
                f"chars={len(content)} preview={_preview(content)}",
                file=output,
            )
            report["skipped"] += 1
            continue
        try:
            result = writer.add_drawer(
                wing,
                room,
                content,
                added_by="dreaming",
                metadata=_metadata_for_record(record),
            )
        except Exception as ex:
            report["errors"].append({"id": logical_id, "error": str(ex)})
            continue
        report["restored"] += 1
        report["new_drawers"].append({"original_id": logical_id, "result": result})
    return report


def _selected(record: dict[str, Any], id_filter: str | None, reason_filter: str | None) -> bool:
    if id_filter is not None and record.get("id") != id_filter:
        return False
    if reason_filter is not None and record.get("reason") != reason_filter:
        return False
    return True


def main(argv: list[str] | None = None, *, writer_factory: Callable[[], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument(
        "--archive-file",
        help="Archive JSONL path (default: <palace>/dream-archive.jsonl)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview restore actions; write nothing")
    ap.add_argument("--id", dest="id_filter", help="Restore only one archived logical id")
    ap.add_argument("--reason", dest="reason_filter", help="Restore only archive records with this reason")
    ap.add_argument("--strict", action="store_true", help="Fail on malformed archive lines")
    args = ap.parse_args(argv)

    palace_path = dream_palace.bind_palace(args.palace)
    archive_path = args.archive_file or os.path.join(palace_path, "dream-archive.jsonl")

    try:
        all_records = iter_archive_records(archive_path, strict=args.strict)
    except (OSError, ValueError) as ex:
        print(f"ERROR reading archive {archive_path}: {ex}", file=sys.stderr)
        return 1

    records = [
        record for record in all_records
        if _selected(record, args.id_filter, args.reason_filter)
    ]
    filtered = len(all_records) - len(records)
    writer = writer_factory() if writer_factory is not None else (object() if args.dry_run else dream_palace.MempalaceWriter())

    report = restore(records, writer, dry_run=args.dry_run)
    skipped = filtered + report["skipped"]
    action = "would restore" if args.dry_run else "restored"
    print(
        f"{action} {report['restored']} drawer(s) from {archive_path} (skipped {skipped}); "
        "new drawer ids are minted and embeddings are recomputed on add",
    )
    for err in report["errors"]:
        print(f"  ERROR {err['id']}: {err['error']}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
