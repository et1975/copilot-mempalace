#!/usr/bin/env python3
"""Render compact, human-readable dreaming/contemplate worklist digests."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


DEFAULT_TEXT_LIMIT = 120
DEFAULT_ID_LIMIT = 18


def _text(value: Any) -> str:
    if value is None:
        return "?"
    return str(value).replace("\n", " ").strip()


def _truncate(value: Any, limit: int = DEFAULT_TEXT_LIMIT, *, full: bool = False) -> str:
    text = _text(value)
    if full or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _short_id(value: Any, *, full: bool = False) -> str:
    return _truncate(value, DEFAULT_ID_LIMIT, full=full)


def _task_from_worklist(worklist: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override
    task = worklist.get("task")
    if task in {"merge", "contradiction", "pattern", "prune"}:
        return task
    items = worklist.get("items") or []
    if items:
        return items[0].get("kind") or task or "unknown"
    if task == "contemplate":
        return "derive"
    return task or "unknown"


def _worklist_ontology(worklist: dict[str, Any], *, full: bool) -> str | None:
    onto = worklist.get("ontology_version")
    if onto is None:
        for item in worklist.get("items") or []:
            onto = item.get("ontology_version")
            if onto is not None:
                break
    return _truncate(onto, 48, full=full) if onto is not None else None


def _item_label(item: dict[str, Any], fallback: int, *, full: bool) -> str:
    value = item.get("candidate_id") or item.get("id") or item.get("cluster_id")
    if value is None:
        value = fallback
    return _short_id(value, full=full)


def _render_merge(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    members = item.get("members") or []
    first = members[0] if members else {}
    wing = item.get("wing") or first.get("wing") or "?"
    room = item.get("room") or first.get("room") or "?"
    pair_sims = (item.get("evidence") or {}).get("pair_sims") or []
    sims = [p.get("sim") for p in pair_sims if isinstance(p, dict) and p.get("sim") is not None]
    line = (
        f"[{index}] {_item_label(item, index, full=full)}  {wing}/{room}  "
        f"members={len(members)}  sims={sims}"
    )
    lines = [line]
    for member in members:
        lines.append(f"    - {_truncate(member.get('text'), full=full)}")
    return lines


def _render_contradiction(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    candidates = item.get("candidates") or []
    objects = [_truncate(c.get("object"), 80, full=full) for c in candidates]
    newest = _truncate((item.get("evidence") or {}).get("newest_object"), 80, full=full)
    return [
        f"[{index}] {_text(item.get('subject'))} -{_text(item.get('predicate'))}-> "
        f"[{', '.join(objects)}]  newest={newest}"
    ]


def _pattern_theme(item: dict[str, Any]) -> Any:
    if item.get("theme") is not None:
        return item["theme"]
    if item.get("label") is not None:
        return item["label"]
    if item.get("topic") is not None:
        return item["topic"]
    return f"cluster:{item.get('cluster_id', '?')}"


def _render_pattern(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    members = item.get("members") or []
    evidence = item.get("evidence") or {}
    support_ids = evidence.get("support_ids") or []
    support = len(set(support_ids)) if support_ids else evidence.get("support", 0)
    snippets = "; ".join(_truncate(m.get("text"), 80, full=full) for m in members[:3])
    return [
        f"[{index}] theme={_truncate(_pattern_theme(item), 80, full=full)}  "
        f"support={support} distinct sessions  :: {snippets}"
    ]


def _render_prune(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    salience = item.get("salience") or {}
    drawer_id = item.get("physical_id") or item.get("id")
    return [
        f"[{index}] {_text(item.get('room'))}/{_short_id(drawer_id, full=full)} "
        f"v={salience.get('v')} age={salience.get('age_days')}d "
        f"kg_degree={salience.get('kg_degree')} redundancy={salience.get('redundancy')} "
        f"neg={salience.get('negatives')} :: {_truncate(item.get('text'), full=full)}"
    ]


def _render_derive(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    conclusion = item.get("conclusion") or {}
    proof = item.get("proof") or {}
    rule = item.get("rule") or {}
    return [
        f"[{index}] cid={_short_id(item.get('candidate_id'), full=full)} "
        f"depth={proof.get('depth')}  "
        f"{_text(conclusion.get('subject') or conclusion.get('subject_id'))} "
        f"-{_text(conclusion.get('predicate'))}-> "
        f"{_text(conclusion.get('object') or conclusion.get('object_id'))}  "
        f"(rule {_text(rule.get('id'))})"
    ]


def _render_unknown(index: int, item: dict[str, Any], *, full: bool) -> list[str]:
    kind = item.get("kind", "unknown")
    ident = _item_label(item, index, full=full)
    return [f"[{index}] kind={kind} id={ident}"]


_RENDERERS = {
    "merge": _render_merge,
    "contradiction": _render_contradiction,
    "pattern": _render_pattern,
    "prune": _render_prune,
    "derive": _render_derive,
}


def render_worklist(worklist: dict[str, Any], *, task: str | None = None, full: bool = False) -> str:
    resolved_task = _task_from_worklist(worklist, task)
    items = list(worklist.get("items") or [])
    header = f"task={resolved_task} items={len(items)}"
    onto = _worklist_ontology(worklist, full=full)
    if onto:
        header += f" ontology_version={onto}"
    lines = [header]
    for index, item in enumerate(items):
        kind = task or item.get("kind") or resolved_task
        renderer = _RENDERERS.get(kind, _render_unknown)
        lines.extend(renderer(index, item, full=full))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worklist", required=True, help="Path to worklist JSON")
    ap.add_argument("--task", choices=sorted(_RENDERERS), help="Override worklist task/kind")
    ap.add_argument("--full", action="store_true", help="Show untruncated ids and text")
    args = ap.parse_args(argv)

    with open(args.worklist, encoding="utf-8") as fh:
        worklist = json.load(fh)
    print(render_worklist(worklist, task=args.task, full=args.full), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
