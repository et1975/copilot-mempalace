#!/usr/bin/env python3
"""dream_adopt — phase 4 of the dreaming pipeline (the only live write).

Reads a ``decisions.json`` (a worklist whose merge items have an approved
``decision``) and applies it to the palace: each approved merge adds the
synthesised drawer, then deletes the superseded originals. Purely mechanical —
no cognition. ``--dry-run`` prints what would change without writing.

A decision is ``item["decision"]``:
    {"action": "merge", "wing": "...", "room": "...", "text": "...",
     "supersedes": ["<physical id>", ...]}      # or {"action": "skip"}

If a merge decision omits ``wing``/``room``/``supersedes``, they default to the
first member's wing/room and the item's ``supersedes`` list.

Usage:
    python3 dream_adopt.py --palace ~/.mempalace/palace --decisions decisions.json
    python3 dream_adopt.py --palace ~/.mempalace/palace --decisions decisions.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import dream_palace
from dream_lib import (
    apply_contradiction_decisions,
    apply_merge_decisions,
    apply_pattern_decisions,
    apply_prune_decisions,
)


def _resolve_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete merge/skip decisions from an adjudicated worklist."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision:
            resolved.append({"action": "skip"})
            continue
        if decision.get("action") != "merge":
            resolved.append({"action": "skip"})
            continue
        members = item.get("members", [])
        first = members[0] if members else {}
        resolved.append(
            {
                "action": "merge",
                "wing": decision.get("wing") or first.get("wing"),
                "room": decision.get("room") or first.get("room"),
                "text": decision["text"],
                "supersedes": decision.get("supersedes") or item.get("supersedes", []),
            }
        )
    return resolved


def _resolve_contradiction_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete KG invalidate/skip decisions from an adjudicated worklist."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "invalidate":
            resolved.append({"action": "skip"})
            continue
        invalidate = decision.get("invalidate")
        if invalidate is None:
            keep = decision.get("keep")
            invalidate = [
                c["object"] for c in item.get("candidates", [])
                if c.get("object") != keep
            ]
        resolved.append(
            {
                "action": "invalidate",
                "subject": item["subject"],
                "predicate": item["predicate"],
                "invalidate": invalidate,
            }
        )
    return resolved


def _first_scope_room(scope: dict[str, Any]) -> str | None:
    rooms = scope.get("rooms")
    if isinstance(rooms, str):
        rooms = [room.strip() for room in rooms.split(",") if room.strip()]
    if rooms:
        return rooms[0]
    return scope.get("room")


def _resolve_pattern_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete pattern surface/skip decisions from an adjudicated worklist."""
    resolved = []
    scope = worklist.get("scope", {})
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "surface":
            resolved.append({"action": "skip"})
            continue
        members = item.get("members", [])
        first = members[0] if members else {}
        evidence = item.get("evidence", {})
        resolved.append(
            {
                "action": "surface",
                "wing": decision.get("wing") or first.get("wing") or scope.get("wing"),
                "room": decision.get("room") or first.get("room") or _first_scope_room(scope),
                "text": decision["text"],
                "supported_by": decision.get("supported_by") or evidence.get("support_ids", []),
            }
        )
    return resolved


def _resolve_prune_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete prune/keep decisions from an adjudicated worklist."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "prune":
            resolved.append({"action": "keep"})
            continue
        resolved.append(
            {
                "action": "prune",
                "id": decision.get("id") or item["id"],
                "member_ids": decision.get("member_ids") or item.get("member_ids", [item["id"]]),
                "wing": decision.get("wing") or item.get("wing"),
                "room": decision.get("room") or item.get("room"),
                "text": decision.get("text") or item.get("text", ""),
                "salience": decision.get("salience") or item.get("salience", {}),
            }
        )
    return resolved


class _DryRunWriter:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def add_drawer(self, wing: str, room: str, content: str) -> Any:
        preview = content.replace("\n", " ")[:60]
        self.planned.append(f"ADD  {wing}/{room}: {preview}...")
        return {"drawer_id": "dry-run"}

    def delete_drawer(self, drawer_id: str) -> Any:
        self.planned.append(f"DEL  {drawer_id}")
        return {"success": True}


class _DryRunKgWriter:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def invalidate(self, subject: str, predicate: str, object: str, ended: str | None = None) -> Any:
        self.planned.append(f"INVALIDATE {subject} {predicate}={object}")
        return {"success": True}


class _DryRunArchiver:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def archive_then_delete(self, record: dict[str, Any]) -> dict[str, Any]:
        self.planned.append(f"PRUNE {record['id']} (archive+delete)")
        return {"archived": record["id"], "deleted": record.get("member_ids", [record["id"]])}


def _worklist_task(worklist: dict[str, Any]) -> str:
    task = worklist.get("task")
    if task:
        return task
    items = worklist.get("items", [])
    if items:
        return items[0].get("kind", "merge")
    return "merge"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", required=True, help="Path to the mempalace palace directory")
    ap.add_argument("--decisions", required=True, help="Path to the adjudicated decisions.json")
    ap.add_argument("--archive-file", default="archive.jsonl", help="Append-only prune archive JSONL path")
    ap.add_argument("--dry-run", action="store_true", help="Print planned changes; write nothing")
    args = ap.parse_args(argv)

    path = dream_palace.bind_palace(args.palace)
    with open(args.decisions, encoding="utf-8") as fh:
        worklist = json.load(fh)
    task = _worklist_task(worklist)

    if args.dry_run:
        if task == "contradiction":
            decisions = _resolve_contradiction_decisions(worklist)
            kg_writer = _DryRunKgWriter()
            report = apply_contradiction_decisions(decisions, kg_writer)
            for line in kg_writer.planned:
                print(line)
            print(
                f"[dry-run] would invalidate {report['invalidated']}, skip {report['skipped']}",
                file=sys.stderr,
            )
            return 0

        if task == "pattern":
            decisions = _resolve_pattern_decisions(worklist)
            writer = _DryRunWriter()
            report = apply_pattern_decisions(decisions, writer)
            for line in writer.planned:
                print(line)
            print(
                f"[dry-run] would surface {report['surfaced']}, skip {report['skipped']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        if task == "merge":
            decisions = _resolve_decisions(worklist)
            writer: Any = _DryRunWriter()
            report = apply_merge_decisions(decisions, writer)
            for line in writer.planned:
                print(line)
            print(
                f"[dry-run] would merge {report['merged']}, skip {report['skipped']}",
                file=sys.stderr,
            )
            return 0

        if task == "prune":
            decisions = _resolve_prune_decisions(worklist)
            archiver = _DryRunArchiver()
            report = apply_prune_decisions(decisions, archiver)
            for line in archiver.planned:
                print(line)
            print(
                f"[dry-run] would prune {report['pruned']}, keep {report['kept']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        print(f"unknown dreaming task: {task}", file=sys.stderr)
        return 2

    if task == "contradiction":
        decisions = _resolve_contradiction_decisions(worklist)
        kg_writer = dream_palace.KgWriter(path)
        try:
            report = apply_contradiction_decisions(decisions, kg_writer)
        finally:
            kg_writer.close()
        print(
            f"adopted: invalidated {report['invalidated']}, skipped {report['skipped']}, "
            f"facts {len(report['invalidated_facts'])}, errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "pattern":
        decisions = _resolve_pattern_decisions(worklist)
        report = apply_pattern_decisions(decisions, dream_palace.MempalaceWriter())
        print(
            f"adopted (pattern): surfaced {report['surfaced']}, skipped {report['skipped']}, "
            f"errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "merge":
        decisions = _resolve_decisions(worklist)
        report = apply_merge_decisions(decisions, dream_palace.MempalaceWriter())
        print(
            f"adopted: merged {report['merged']}, skipped {report['skipped']}, "
            f"deleted {len(report['deleted'])}, errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "prune":
        decisions = _resolve_prune_decisions(worklist)
        report = apply_prune_decisions(decisions, dream_palace.Archiver(args.archive_file))
        print(
            f"adopted (prune): pruned {report['pruned']}, kept {report['kept']}, "
            f"errors {len(report['errors'])}",
            file=sys.stderr,
        )
    else:
        print(f"unknown dreaming task: {task}", file=sys.stderr)
        return 2

    for err in report["errors"]:
        print(f"  ERROR {err}", file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
