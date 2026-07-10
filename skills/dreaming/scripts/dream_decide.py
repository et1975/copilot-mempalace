#!/usr/bin/env python3
"""dream_decide — mechanically stamp simple dreaming decisions."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


_ALLOWED_ACTIONS: dict[str, set[str]] = {
    "derive": {"materialize", "skip"},
    "prune": {"prune", "keep"},
    "merge": {"skip"},
    "contradiction": {"skip"},
    "pattern": {"skip"},
}

_MANUAL_ACTION_HINTS: dict[tuple[str, str], str] = {
    ("merge", "merge"): "merge requires manual text",
    ("merge", "materialize"): "merge requires manual text",
    ("pattern", "surface"): "pattern surface requires manual text",
    ("contradiction", "invalidate"): "contradiction invalidation requires a keep/invalidate selection",
}


def _csv_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def _task_from_worklist(worklist: dict[str, Any]) -> str:
    task = worklist.get("task")
    if task in _ALLOWED_ACTIONS:
        return task

    items = worklist.get("items") or []
    if items:
        kind = items[0].get("kind")
        if kind in _ALLOWED_ACTIONS:
            return kind

    if task == "contemplate":
        return "derive"
    return task or "merge"


def _item_id(task: str, item: dict[str, Any]) -> str:
    if task == "derive":
        return str(item["candidate_id"])
    if task == "prune":
        return str(item["id"])
    if "id" in item:
        return str(item["id"])
    if "candidate_id" in item:
        return str(item["candidate_id"])
    if "cluster_id" in item:
        return str(item["cluster_id"])
    raise KeyError(f"cannot determine id for {task} item")


def _validate_action(task: str, action: str) -> str | None:
    allowed = _ALLOWED_ACTIONS.get(task)
    if allowed is None:
        return f"unsupported dreaming task: {task}"
    if action in allowed:
        return None

    hint = _MANUAL_ACTION_HINTS.get((task, action))
    allowed_text = ", ".join(sorted(allowed))
    if hint:
        return f"{task} blanket decisions only support: {allowed_text}; requested: {action}. {hint}"
    return f"{task} blanket decisions only support: {allowed_text}; requested: {action}"


def _selected(item_id: str, only: set[str], except_ids: set[str]) -> bool:
    if only and item_id not in only:
        return False
    return item_id not in except_ids


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worklist", required=True, help="Input worklist JSON path")
    ap.add_argument("--out", required=True, help="Output decisions JSON path")
    ap.add_argument("--all", dest="all_action", metavar="ACTION", help="Apply ACTION to every item")
    ap.add_argument("--only", help="Comma-separated item ids to receive the decision")
    ap.add_argument("--except", dest="except_ids", help="Comma-separated item ids to exclude")
    ap.add_argument("--action", help="Action to apply to the --only subset")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.all_action and args.action:
        print("decide: use either --all ACTION or --action ACTION, not both", file=sys.stderr)
        return 2
    if args.action and not args.only:
        print("decide: --action requires --only", file=sys.stderr)
        return 2
    action = args.all_action or args.action
    if not action:
        print("decide: specify --all ACTION or --only IDS --action ACTION", file=sys.stderr)
        return 2

    with open(args.worklist, encoding="utf-8") as fh:
        worklist = json.load(fh)

    task = _task_from_worklist(worklist)
    error = _validate_action(task, action)
    if error:
        print(f"decide: {error}", file=sys.stderr)
        return 2

    only = _csv_ids(args.only)
    except_ids = _csv_ids(args.except_ids)
    items = worklist.get("items") or []
    applied = 0
    for item in items:
        item_id = _item_id(task, item)
        if _selected(item_id, only, except_ids):
            item["decision"] = {"action": action}
            applied += 1
        else:
            item.pop("decision", None)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(worklist, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"decide: task={task} action={action} applied={applied}/{len(items)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
