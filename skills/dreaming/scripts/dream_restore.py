#!/usr/bin/env python3
"""dream_restore — restore drawers from native MemPalace archive artifacts.

Restores archived logical drawers by adding new drawers with the original
wing/room and reconstructed content. The original physical ids and chunking are
not resurrected: mempalace mints new drawer ids and recomputes embeddings.
Procedural events/source records/receipts are refused before native import,
export or replay of the selected archives, including during dry-run. Use
coherent physical recovery for identity-bound procedural state.

Usage:
    "$MPY" "$DREAM_SCRIPTS/dream_restore.py" --palace ~/.mempalace/palace
    "$MPY" "$DREAM_SCRIPTS/dream_restore.py" --palace ~/.mempalace/palace --dry-run
    "$MPY" "$DREAM_SCRIPTS/dream_restore.py" --palace ~/.mempalace/palace --id logical-id

Select absolute MPY (the provisioned MemPalace interpreter) and DREAM_SCRIPTS
paths; do not assume the current directory or system Python owns MemPalace.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from typing import Any, TextIO

import dream_palace
from dream_metadata import (
    decode_dream_metadata, is_procedural_record, is_source_record_metadata,
    split_dream_metadata, strict_json,
)

MAX_ARCHIVE_TRAILERS = 32


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
                record = strict_json(raw)
            except json.JSONDecodeError as ex:
                if strict:
                    raise ValueError(f"malformed JSON on line {line_no} of {path}: {ex.msg}") from ex
                continue
            except ValueError as ex:
                raise ValueError(f"ambiguous archive JSON on line {line_no} of {path}: {ex}") from ex
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


def load_native_archive_records(palace: str) -> list[dict[str, Any]]:
    """Read complete verified archives without initializing or migrating storage."""
    from dream_store import DreamStore

    store = DreamStore(palace)
    records = [store.get_document(event["document"]) for event in store.events()
               if event["record_type"] == "archive"]
    for record in records:
        dream_palace._validate_archive_record(record)
    return records


def _ordered_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = record.get("rows") or []
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    member_ids = list(record.get("member_ids") or [])

    ordered_rows: list[dict[str, Any]] = []
    for member_id in member_ids:
        row = by_id.get(member_id)
        if row is not None:
            ordered_rows.append(row)
    if not member_ids:
        ordered_rows.extend(row for row in rows if isinstance(row, dict))
    return ordered_rows


def record_to_content(record: dict[str, Any]) -> str:
    """Reconstruct canonical content in member order, preserving exact sources."""
    ordered_rows = _ordered_rows(record)
    logical = {
        "id": record["id"],
        "member_ids": [row["id"] for row in ordered_rows],
        "metadata": {},
        "text": "\n".join(str(row.get("document") or "") for row in ordered_rows),
    }
    physical = {
        row["id"]: {"id": row["id"], "text": str(row.get("document") or ""),
                    "metadata": row.get("metadata") or {}}
        for row in ordered_rows
    }
    return dream_palace._canonical_drawer(logical, physical)["text"]


def _archive_metadata_claims(drawer: dict[str, Any]) -> Iterable[dict[str, Any]]:
    text = drawer["text"]
    for peeled in range(MAX_ARCHIVE_TRAILERS + 1):
        body, metadata = split_dream_metadata({"text": text, "metadata": drawer["metadata"]})
        if body == text:
            return
        if peeled == MAX_ARCHIVE_TRAILERS:
            raise ValueError(f"archive metadata trailer limit ({MAX_ARCHIVE_TRAILERS}) exceeded")
        yield metadata
        text = body


def _reject_procedural_metadata(metadata: dict[str, Any]) -> None:
    if is_procedural_record({"metadata": metadata}) or is_source_record_metadata(metadata):
        raise ValueError(
            "Cannot replay procedural events, source records or receipts with new drawer IDs; "
            "use a coherent whole-palace physical backup/restore instead")


def _preflight_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    errors = []
    for record in records:
        try:
            native = [
                record, record.get("metadata"),
                *(row.get("metadata")
                  for row in record.get("rows") or [] if isinstance(row, dict)),
            ]
            writers = set()
            for claim in native:
                metadata = decode_dream_metadata({"metadata": claim})
                _reject_procedural_metadata(metadata)
                if "added_by" in metadata:
                    writers.add(metadata["added_by"])
            if len(writers) > 1:
                raise ValueError("conflicting archive writer context")
            writer_context = {"added_by": next(iter(writers))} if writers else {}
            # Inspect complete original and replay views, never isolated chunk
            # text: chunks can split fence delimiters or metadata tokens.
            original = "".join(str(row.get("document") or "") for row in _ordered_rows(record))
            for text in dict.fromkeys((original, record_to_content(record))):
                for metadata in _archive_metadata_claims({"text": text, "metadata": writer_context}):
                    _reject_procedural_metadata(metadata)
        except (TypeError, ValueError) as ex:
            errors.append({"id": record.get("id"), "error": f"archive preflight: {ex}"})
    return errors


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
    """Preflight the complete selection, then keep going after ordinary write failures."""
    output = out if out is not None else sys.stdout
    records = list(records)
    report: dict[str, Any] = {
        "restored": 0, "skipped": 0, "errors": _preflight_records(records), "new_drawers": []}
    if report["errors"]:
        return report
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
        help="Explicit legacy JSONL import; retained natively before non-dry restoration",
    )
    ap.add_argument("--export-file", help="Explicit JSONL export of the selected native archives")
    ap.add_argument("--dry-run", action="store_true", help="Preview restore actions; write nothing")
    ap.add_argument("--id", dest="id_filter", help="Restore only one archived logical id")
    ap.add_argument("--reason", dest="reason_filter", help="Restore only archive records with this reason")
    ap.add_argument("--strict", action="store_true", help="Fail on malformed archive lines")
    args = ap.parse_args(argv)

    palace_path = dream_palace.bind_palace(args.palace)
    archive_path = args.archive_file or "native MemPalace archives"

    try:
        all_records = (iter_archive_records(args.archive_file, strict=args.strict)
                       if args.archive_file else load_native_archive_records(palace_path))
    except (OSError, ValueError, RuntimeError) as ex:
        print(f"ERROR reading archive {archive_path}: {ex}", file=sys.stderr)
        return 1

    records = [
        record for record in all_records
        if _selected(record, args.id_filter, args.reason_filter)
    ]
    filtered = len(all_records) - len(records)
    errors = _preflight_records(records)
    if errors:
        for error in errors:
            print(f"  ERROR {error['id']}: {error['error']}", file=sys.stderr)
        return 1
    if args.archive_file and not args.dry_run:
        try:
            dream_palace.retain_archive_records(palace_path, all_records)
        except (OSError, ValueError, RuntimeError) as ex:
            print(f"ERROR retaining archive {archive_path}: {ex}", file=sys.stderr)
            return 1
    if args.export_file and not args.dry_run:
        dream_palace._append_jsonl_export(args.export_file, records)
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
