#!/usr/bin/env python3
"""One-shot read-only survey across all dreaming tasks and wings.

Instead of shelling out to ``dream_harvest.py`` once per (task, wing) — which
generates dozens of invocations — this driver runs every read-only harvest
in a single process by calling :func:`dream_harvest.main` in-process, then
aggregates the worklists into one report.

READ-ONLY: it never adopts. ``induce-rules`` candidates are written to a
temporary ontology and reported, never to the live ``<palace>/ontology.json``.

Usage::

    MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//')
    "$MPY" dream_survey.py
    "$MPY" dream_survey.py --palace ~/.mempalace/palace
    "$MPY" dream_survey.py --palace <p> --tasks merge,prune --wings avs,icm_automation
    "$MPY" dream_survey.py --palace <p> --format json --out survey.json \
        --worklists-dir ./wl
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

import dream_harvest
import dream_ontology
import dream_palace

DEFAULT_TASKS = ["contradiction", "induce-rules", "pattern", "reflect", "merge", "prune"]
PALACE_WIDE = {"contradiction", "induce-rules"}
WING_SCOPED = {"merge", "pattern", "reflect", "prune"}


def _default_palace() -> str | None:
    config_path = os.environ.get("MEMPALACE_CONFIG") or os.path.expanduser("~/.mempalace/config.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, encoding="utf-8") as fh:
            palace_path = json.load(fh).get("palace_path")
    except (OSError, json.JSONDecodeError):
        return None
    return os.path.expanduser(palace_path) if palace_path else None


# --------------------------------------------------------------------------
# Pure aggregation (no palace access) — unit-tested in test_dream_survey.py
# --------------------------------------------------------------------------
def count(worklist: dict) -> int:
    return len((worklist or {}).get("items") or [])


def _snippet(text: str, width: int = 80) -> str:
    text = " ".join((text or "").split())
    return text[:width] + ("…" if len(text) > width else "")


def _example_for(kind: str, item: dict) -> dict:
    if kind == "merge":
        ev = item.get("evidence") or {}
        sims = ev.get("pair_sims") or []
        max_sim = max((p.get("sim", 0.0) for p in sims), default=None)
        members = item.get("members") or [{}]
        first = members[0] if members else {}
        return {
            "cluster_id": item.get("cluster_id"),
            "wing": first.get("wing"),
            "room": first.get("room"),
            "size": ev.get("size"),
            "max_sim": max_sim,
        }
    if kind == "contradiction":
        ev = item.get("evidence") or {}
        return {
            "subject": item.get("subject"),
            "predicate": item.get("predicate"),
            "objects": [c.get("object") for c in (item.get("candidates") or [])],
            "newest": ev.get("newest_object"),
        }
    if kind == "pattern":
        ev = item.get("evidence") or {}
        members = item.get("members") or [{}]
        return {
            "support": ev.get("support"),
            "support_ids": ev.get("support_ids"),
            "sample": _snippet((members[0] if members else {}).get("text", "")),
        }
    if kind == "reflect":
        members = item.get("members") or [{}]
        return {
            "reflect_kind": item.get("reflect_kind"),
            "coverage": item.get("coverage"),
            "score": item.get("score"),
            "sample": _snippet((members[0] if members else {}).get("text", "")),
        }
    if kind == "prune":
        sal = item.get("salience") or {}
        return {
            "id": item.get("id"),
            "wing": item.get("wing"),
            "room": item.get("room"),
            "topic": item.get("topic"),
            "v": sal.get("v"),
            "age_days": sal.get("age_days"),
            "kg_degree": sal.get("kg_degree"),
            "redundancy": sal.get("redundancy"),
        }
    return {"kind": item.get("kind")}


def examples(task: str, worklist: dict, n: int = 3) -> list:
    items = (worklist or {}).get("items") or []
    return [_example_for(task, item) for item in items[:n]]


def palace_task_summary(task: str, worklist: dict, n: int = 3) -> dict:
    return {"total": count(worklist), "examples": examples(task, worklist, n)}


def wing_task_summary(task: str, by_wing: dict, n: int = 3) -> dict:
    total = 0
    per_wing = {}
    collected = []
    for wing in sorted(by_wing):
        worklist = by_wing[wing]
        c = count(worklist)
        total += c
        if c:
            per_wing[wing] = c
            for ex in examples(task, worklist, n):
                ex = dict(ex)
                ex.setdefault("wing", wing)
                if not ex.get("wing"):
                    ex["wing"] = wing
                collected.append(ex)
    return {"total": total, "by_wing": per_wing, "examples": collected[:n]}


def rules_summary(rules: list) -> dict:
    rules = rules or []
    return {
        "total": len(rules),
        "rules": [
            {"id": r.get("id"), "family": r.get("family"), "enabled": r.get("enabled", False)}
            for r in rules
        ],
    }


def build_report(palace: str, wings: list, collected: dict, n: int = 3) -> dict:
    tasks = {}
    for task, data in collected.items():
        if task == "induce-rules":
            tasks[task] = rules_summary(data)
        elif task in PALACE_WIDE:
            tasks[task] = palace_task_summary(task, data, n)
        else:
            tasks[task] = wing_task_summary(task, data, n)
    return {"palace": palace, "wings": wings, "tasks": tasks}


def summarize_report(report: dict) -> str:
    lines = []
    lines.append(f"palace: {report.get('palace')}")
    lines.append(f"wings ({len(report.get('wings') or [])}): {', '.join(report.get('wings') or [])}")
    lines.append("")
    for task, summ in (report.get("tasks") or {}).items():
        total = summ.get("total", 0)
        header = f"[{task}] total={total}"
        if "by_wing" in summ and summ["by_wing"]:
            header += "  wings=" + ", ".join(f"{w}:{c}" for w, c in summ["by_wing"].items())
        lines.append(header)
        if task == "induce-rules":
            for r in summ.get("rules", []):
                lines.append(f"    - {r['family']:10} {r['id']}  enabled={r['enabled']}")
        else:
            for ex in summ.get("examples", []):
                lines.append("    - " + _format_example(task, ex))
    return "\n".join(lines)


def _format_example(task: str, ex: dict) -> str:
    if task == "merge":
        return f"{ex.get('wing')}/{ex.get('room')} size={ex.get('size')} max_sim={ex.get('max_sim')}"
    if task == "contradiction":
        return f"{ex.get('subject')} -{ex.get('predicate')}-> {ex.get('objects')} (newest={ex.get('newest')})"
    if task == "pattern":
        return f"support={ex.get('support')} :: {ex.get('sample')}"
    if task == "reflect":
        kind = ex.get("reflect_kind") or "cluster-seed"
        return (f"kind={kind} coverage={ex.get('coverage')} "
                f"score={ex.get('score')} :: {ex.get('sample')}")
    if task == "prune":
        return f"{ex.get('wing')}/{ex.get('room')} v={ex.get('v')} age={ex.get('age_days')}d :: {ex.get('id')}"
    return json.dumps(ex, ensure_ascii=False)


# --------------------------------------------------------------------------
# Orchestration (palace-facing) — validated by live smoke test
# --------------------------------------------------------------------------
def _run_main(argv: list) -> None:
    """Call dream_harvest.main in-process, suppressing its stderr chatter."""
    with contextlib.redirect_stderr(io.StringIO()):
        rc = dream_harvest.main(argv)
    if rc != 0:
        raise RuntimeError(f"dream_harvest.main {argv} returned {rc}")


def harvest(task: str, palace: str, wing: str | None = None, *, tau: float = 0.9,
            min_support: int = 2, v_min: float = 0.35, age_floor_days: int = 30) -> dict:
    """Run a single read-only harvest and return its worklist dict."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "wl.json")
        argv = ["--palace", palace, "--task", task, "--out", out]
        if task == "merge":
            argv += ["--tau", str(tau)]
            if wing:
                argv += ["--wing", wing]
        elif task == "pattern":
            argv += ["--min-support", str(min_support)]
            if wing:
                argv += ["--wing", wing]
        elif task == "reflect":
            # Constructive cluster path (no --source); recurrence/converge is
            # already covered by the pattern task. Bound clusters for a survey.
            argv += ["--min-support", str(min_support), "--max-candidates", "10"]
            if wing:
                argv += ["--wing", wing]
        elif task == "prune":
            argv += ["--v-min", str(v_min), "--age-floor-days", str(age_floor_days)]
            if wing:
                argv += ["--wing", wing]
        _run_main(argv)
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)


def induce(palace: str, min_support: int = 2) -> list:
    """Run induce-rules to a throwaway ontology and return the candidate rules."""
    with tempfile.TemporaryDirectory() as td:
        onto = os.path.join(td, "ontology.json")
        _run_main(["--palace", palace, "--task", "induce-rules",
                   "--min-support", str(min_support), "--ontology-out", onto])
        return dream_ontology.read_ontology_doc(onto).get("rules", [])


def survey(palace: str, wings: list | None = None, tasks: list | None = None, *,
           tau: float = 0.9, min_support: int = 2, v_min: float = 0.35,
           age_floor_days: int = 30, n: int = 3, worklists_dir: str | None = None) -> dict:
    palace = os.path.abspath(os.path.expanduser(palace))
    dream_palace.bind_palace(palace)
    tasks = tasks or list(DEFAULT_TASKS)
    if wings is None:
        wings = dream_palace.list_wings(palace)

    if worklists_dir:
        os.makedirs(worklists_dir, exist_ok=True)

    collected = {}
    for task in tasks:
        if task == "induce-rules":
            collected[task] = induce(palace, min_support=min_support)
            if worklists_dir:
                _dump(worklists_dir, "induce-rules.json", {"rules": collected[task]})
        elif task in PALACE_WIDE:
            wl = harvest(task, palace, tau=tau, min_support=min_support,
                         v_min=v_min, age_floor_days=age_floor_days)
            collected[task] = wl
            if worklists_dir:
                _dump(worklists_dir, f"{task}.json", wl)
        else:
            by_wing = {}
            for wing in wings:
                wl = harvest(task, palace, wing=wing, tau=tau, min_support=min_support,
                             v_min=v_min, age_floor_days=age_floor_days)
                by_wing[wing] = wl
                if worklists_dir and count(wl):
                    _dump(worklists_dir, f"{task}.{wing}.json", wl)
            collected[task] = by_wing

    return build_report(palace, wings, collected, n=n)


def _dump(directory: str, name: str, obj) -> None:
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", help="Path to the mempalace palace directory (default: mempalace config)")
    ap.add_argument("--tasks", default=",".join(DEFAULT_TASKS),
                    help=f"Comma-separated tasks (default: {','.join(DEFAULT_TASKS)})")
    ap.add_argument("--wings", default=None,
                    help="Comma-separated wings for wing-scoped tasks (default: all wings)")
    ap.add_argument("--tau", type=float, default=0.9, help="Merge cosine threshold (default 0.9)")
    ap.add_argument("--min-support", type=int, default=2,
                    help="Support for pattern themes / induced rules (default 2)")
    ap.add_argument("--v-min", type=float, default=0.35, help="Prune salience ceiling (default 0.35)")
    ap.add_argument("--age-floor-days", type=int, default=30, help="Prune age floor (default 30)")
    ap.add_argument("--examples", type=int, default=3, help="Max examples per task (default 3)")
    ap.add_argument("--worklists-dir", default=None,
                    help="Optional dir to dump non-empty worklists for later adjudication")
    ap.add_argument("--out", default=None, help="Write the full report JSON to this path")
    ap.add_argument("--format", choices=["summary", "json", "both"], default="summary",
                    help="Output format (default summary)")
    args = ap.parse_args(argv)

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    wings = [w.strip() for w in args.wings.split(",") if w.strip()] if args.wings else None
    effective_palace = args.palace or _default_palace()
    if effective_palace is None:
        config_path = os.environ.get("MEMPALACE_CONFIG") or "~/.mempalace/config.json"
        print(f"error: no --palace given and {config_path} has no palace_path", file=sys.stderr)
        return 2

    report = survey(
        effective_palace, wings=wings, tasks=tasks, tau=args.tau,
        min_support=args.min_support, v_min=args.v_min,
        age_floor_days=args.age_floor_days, n=args.examples,
        worklists_dir=args.worklists_dir,
    )

    if args.out:
        _dump(os.path.dirname(os.path.abspath(args.out)) or ".", os.path.basename(args.out), report)

    if args.format in ("summary", "both"):
        print(summarize_report(report))
    if args.format in ("json", "both"):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
