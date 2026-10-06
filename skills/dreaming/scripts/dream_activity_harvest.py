"""Offline activity harvesting for the existing dreaming entry point.

Selection is explicit or uses the existing read-only host index. Each source is
loaded once, with detailed reports on disk and a compact batch summary in context.
"""

from collections import Counter
from pathlib import Path
import sqlite3
import sys
import time
from uuid import UUID

from dream_activity import artifact_review, build_activities, encode_json
from dream_activity_copilot import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_EVENTS,
    DEFAULT_MAX_LINE_BYTES,
    DEFAULT_MAX_RETAINED_BYTES,
    DEFAULT_MAX_TEXT_CHARS,
    load_evidence,
)
from dream_activity_io import publish_report
from dream_sessions import default_store_path, load_sessions


_MAX_SESSIONS = 100
_DEFAULT_OUTPUT_BYTES = 4 * 1024 * 1024
_LIMITS = {
    "max_bytes": DEFAULT_MAX_BYTES,
    "max_events": DEFAULT_MAX_EVENTS,
    "max_calls": DEFAULT_MAX_CALLS,
    "max_line_bytes": DEFAULT_MAX_LINE_BYTES,
    "max_retained_bytes": DEFAULT_MAX_RETAINED_BYTES,
    "max_text_chars": DEFAULT_MAX_TEXT_CHARS,
}


def _positive(value: str) -> int:
    import argparse
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def add_arguments(parser) -> None:
    parser.add_argument("--session-id", action="append", dest="activity_session_ids",
                        help="Explicit session UUID; repeat for an activity batch")
    parser.add_argument("--session-root",
                        help="Host session-state directory (--task activity)")
    parser.add_argument("--session-store",
                        help="Existing read-only host session index (--task activity)")
    parser.add_argument("--activity-view", choices=("activities", "both"),
                        default="activities", help="Detailed activity reports to persist")
    for name, default in _LIMITS.items():
        parser.add_argument("--" + name.replace("_", "-"), type=_positive, default=default,
                            help=f"Activity evidence bound (default {default})")
    parser.add_argument("--max-output-bytes", type=_positive, default=_DEFAULT_OUTPUT_BYTES,
                        help="Per-report serialized output budget")


def _validated_ids(session_ids) -> list[str]:
    if not isinstance(session_ids, list) or len(session_ids) > _MAX_SESSIONS:
        raise ValueError("activity batch requires at most 100 session IDs")
    result = []
    for identifier in session_ids:
        if not isinstance(identifier, str):
            raise ValueError("invalid session ID")
        try:
            canonical = str(UUID(identifier))
        except ValueError:
            raise ValueError("invalid session ID") from None
        if canonical != identifier:
            raise ValueError("session IDs must use canonical UUID spelling")
        if identifier in result:
            raise ValueError("duplicate session ID")
        result.append(identifier)
    return result


def _metrics(report: dict) -> dict:
    activities = report["activities"]
    calls = [call for activity in activities for call in activity["calls"]]
    return {
        "activities": len(activities),
        "calls": len(calls),
        "multi_call_activities": sum(len(activity["calls"]) > 1 for activity in activities),
        "intent_states": dict(Counter(activity["intent"]["status"] for activity in activities)),
        "execution_states": dict(Counter(call["execution_status"] for call in calls)),
        "referenced_paths": len({item["path"] for call in calls for item in call["artifacts"]}),
        "warnings": len(report["warnings"]),
    }


def _resolve(path: Path, *, strict: bool = False) -> Path:
    try:
        return path.resolve(strict=strict)
    except RuntimeError:
        # Python 3.11/3.12 report symlink loops as RuntimeError rather than OSError.
        raise ValueError("path_resolution_error") from None


def _one_session(root: Path, identifier: str, folder: Path, view: str,
                 limits: dict, output_limit: int) -> dict:
    started = time.monotonic()
    item = {
        "session_id": identifier, "status": "failed", "error": None,
        "source": None, "metrics": None, "activity_report": None, "artifact_report": None,
        "activity_bytes": 0, "artifact_bytes": 0,
    }
    try:
        source = root / identifier / "events.jsonl"
        if not _resolve(source).is_relative_to(root):
            raise ValueError("session_path_outside_root")
        packet = load_evidence(str(source), identifier, **limits)
        report = build_activities(packet)
        item["source"] = report["source"]
        item["metrics"] = _metrics(report)
        path = folder / f"{identifier}.activities.json"
        encoded = encode_json(report, max_output_bytes=output_limit)
        publish_report(path, encoded)
        item["activity_report"] = str(path)
        item["activity_bytes"] = len(encoded)
        if view == "both":
            projection = artifact_review(report, max_output_bytes=output_limit, compact=True)
            projection["activity_report"] = item["activity_report"]
            path = folder / f"{identifier}.artifacts.json"
            encoded = encode_json(projection, max_output_bytes=output_limit)
            publish_report(path, encoded)
            item["artifact_report"] = str(path)
            item["artifact_bytes"] = len(encoded)
        item["status"] = "complete"
    except OSError as exc:
        item["error"] = f"io_error:{exc.errno}"
    except ValueError as exc:
        item["error"] = str(exc)[:300]
    item["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return item


def harvest_sessions(session_root: str, session_ids: list[str], out_path: str, *,
                     view: str = "activities", limits: dict | None = None,
                     max_output_bytes: int = _DEFAULT_OUTPUT_BYTES) -> dict:
    """Persist independent reports and an explicit complete/partial/failed summary."""
    identifiers = _validated_ids(session_ids)
    if view not in ("activities", "both"):
        raise ValueError("invalid activity view")
    settings = dict(_LIMITS)
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) - settings.keys():
            raise ValueError("invalid activity limits")
        settings.update(limits)
    if any(type(value) is not int or value <= 0
           for value in [*settings.values(), max_output_bytes]):
        raise ValueError("activity limits must be positive integers")
    root = _resolve(Path(session_root).expanduser(), strict=True)
    if not root.is_dir():
        raise ValueError("session root must be a directory")
    output = Path(out_path).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("output already exists")
    folder = output.with_name(output.name + ".sessions")
    folder.mkdir(mode=0o700)
    sessions = [
        _one_session(root, identifier, folder, view, settings, max_output_bytes)
        for identifier in identifiers
    ]
    successful = [item for item in sessions if item["status"] == "complete"]
    failed = len(sessions) - len(successful)
    states = Counter()
    for item in successful:
        states.update(item["metrics"]["intent_states"])
    summary = {
        "schema_version": 1,
        "task": "activity",
        "status": "complete" if not failed else ("partial" if successful else "failed"),
        "summary": {
            "attempted": len(sessions), "succeeded": len(successful), "failed": failed,
            "activities": sum(item["metrics"]["activities"] for item in successful),
            "calls": sum(item["metrics"]["calls"] for item in successful),
            "intent_states": dict(states),
        },
        "sessions": sessions,
    }
    publish_report(output, encode_json(summary, max_output_bytes=max_output_bytes))
    return summary


def run_from_arguments(args) -> int:
    try:
        limit = args.limit_sessions if args.limit_sessions is not None else 8
        if type(limit) is not int or not 1 <= limit <= _MAX_SESSIONS:
            raise ValueError("limit-sessions must be between 1 and 100")
        store = args.session_store or default_store_path()
        if args.activity_session_ids is not None:
            if args.repository or args.since:
                raise ValueError("explicit session IDs cannot be combined with index filters")
            identifiers = args.activity_session_ids
            if len(identifiers) > limit:
                raise ValueError("explicit selection exceeds limit-sessions")
        else:
            if not Path(store).is_file():
                raise ValueError("session store is unavailable")
            identifiers = [
                item["session_id"] for item in load_sessions(
                    store, repository=args.repository, since=args.since, limit=limit,
                )
            ]
        root = args.session_root or str(Path(store).expanduser().absolute().parent / "session-state")
        settings = {name: getattr(args, name) for name in _LIMITS}
        summary = harvest_sessions(root, identifiers, args.out, view=args.activity_view,
                                   limits=settings, max_output_bytes=args.max_output_bytes)
    except (OSError, sqlite3.Error, ValueError) as exc:
        detail = "IO error" if isinstance(exc, OSError) else (
            "session index error" if isinstance(exc, sqlite3.Error) else str(exc)[:300]
        )
        print(f"activity harvest: {detail}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(encode_json(summary, max_output_bytes=args.max_output_bytes))
    return int(summary["summary"]["failed"] > 0)
