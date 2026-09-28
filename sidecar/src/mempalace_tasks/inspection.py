"""Bounded, read-only human diagnostics; never an execution authorization API."""

from datetime import datetime
import json
import math
import re
import time
import unicodedata

from .client import TaskClientError


_FILTERS = ("project", "goal_id", "status", "assignee", "kind", "needs_attention")
_STATUSES = ("open", "in_progress", "recovering", "quarantined", "closed", "cancelled")
_COMMANDS = {"status", "list", "show", "history", "watch"}
_OPTIONS = set(_FILTERS) | {
    "task_id", "limit", "cursor", "after_record_seq", "json",
    "refresh_seconds", "duration_seconds",
}
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|credential|credentials|api[_-]?key|private[_-]?key)"
    r"(?:$|[_-])|^(?:authorization|proxy-authorization|cookie|set-cookie)$", re.I,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"\b((?:access[_-]?token|api[_-]?key|password|client[_-]?secret)\s*[=:]\s*)"
    r"([^\s,;]+)", re.I,
)


def _redact(value, *, field=None):
    if isinstance(value, dict):
        fences = field in {"resource_fences", "installed_fences", "required_fences"} and all(
            type(key) is str and type(counter) is int and counter > 0
            for key, counter in value.items()
        )
        return {_redact(key): "[redacted]" if not fences and _SECRET_KEY.search(
                    re.sub(r"([a-z])([A-Z])", r"\1_\2", key))
                else _redact(child, field=key)
                for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", _BEARER.sub("Bearer [redacted]", value))
    return value


def escape_text(value):
    """Escape data, not layout; neutralize terminal, bidi, and line controls."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    result = []
    for char in text:
        number = ord(char)
        if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            result.append(f"\\x{number:02x}" if number < 256 else f"\\u{number:04x}")
        else:
            result.append(char)
    return "".join(result)


def _instant(value):
    if not isinstance(value, str) or not value.endswith(("Z", "+00:00")):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _seconds(later, earlier):
    end, start = _instant(later), _instant(earlier)
    return (end - start).total_seconds() if end is not None and start is not None else None


def _count(value):
    return type(value) is int and value >= 0


def _summary_valid(summary):
    return (
        isinstance(summary, dict)
        and all(_count(summary.get(key)) for key in ("total", "ready", "needs_attention"))
        and isinstance(summary.get("statuses"), dict)
        and all(key in _STATUSES and _count(value) for key, value in summary["statuses"].items())
        and sum(summary["statuses"].values()) == summary["total"]
        and summary["ready"] <= summary["total"]
        and summary["needs_attention"] <= summary["total"]
    )


def _eligibility_valid(value):
    return (
        isinstance(value, dict) and type(value.get("ready")) is bool
        and isinstance(value.get("reasons"), list)
        and all(isinstance(reason, dict) and isinstance(reason.get("code"), str)
                for reason in value["reasons"])
    )


def _task_valid(task, *, detail=False):
    return (
        isinstance(task, dict)
        and all(isinstance(task.get(key), str) and bool(task[key]) for key in ("id", "title"))
        and task.get("status") in _STATUSES
        and type(task.get("priority")) is int and 0 <= task["priority"] <= 4
        and (
            (task.get("attempt") is None or isinstance(task["attempt"], dict)) if detail
            else (_eligibility_valid(task) and type(task.get("needs_attention")) is bool
                  and isinstance(task.get("blockers"), list))
        )
    )


def _history_row_valid(row):
    return (
        isinstance(row, dict) and _count(row.get("record_seq"))
        and all(isinstance(row.get(key), str) and bool(row[key]) for key in (
            "event_id", "record_type", "command_id", "disposition", "outcome"))
        and isinstance(row.get("task_ids"), list) and isinstance(row.get("payload"), dict)
    )


def _task_display(task, as_of, *, detail=False):
    attempt = (task.get("attempt") or {}) if detail else {}
    checkpoint = attempt.get("checkpoint") if detail else task.get("checkpoint")
    checkpoint_at = checkpoint.get("at") if isinstance(checkpoint, dict) else None
    deadlines = {
        "lease_remaining_seconds": _seconds(task.get("lease_expires_at"), as_of),
        "progress_remaining_seconds": _seconds(
            attempt.get("progress_deadline") if detail else task.get("progress_deadline"), as_of),
        "hard_remaining_seconds": _seconds(
            attempt.get("hard_deadline") if detail else task.get("hard_deadline"), as_of),
    }
    age = _seconds(as_of, checkpoint_at)
    return {
        "task_id": task.get("id"),
        "attempt_id": attempt.get("id") if detail else task.get("attempt_id"),
        "attempt_status": attempt.get("status") if detail else task.get("attempt_status"),
        **deadlines,
        "progress_age_seconds": age if age is not None and age >= 0 else None,
        "pending_expiry": task.get("status") == "in_progress" and any(
            value is not None and value <= 0 for value in deadlines.values()),
    }


def build_frame(command, payload, *, scope=None, error=None, observation_age_seconds=None, warnings=()):
    """Pure detached/redacted v1 frame. All deadline arithmetic uses payload as_of.

    ``data`` retains the server shape; ``display`` cannot grant authorization.
    ``error`` is a safe client-error dictionary, not an arbitrary exception.
    """
    data = _redact(payload) if isinstance(payload, dict) else None
    error = _redact(error) if error is not None else None
    warnings = list(warnings)
    view = data or {}
    as_of = view.get("as_of")
    verified_at = view.get("last_verified_at")
    age = _seconds(as_of, verified_at)
    reason = view.get("reason")
    valid_metadata = (
        type(view.get("schema_version")) is int and view["schema_version"] == 1
        and isinstance(view.get("authority_id"), str) and bool(view["authority_id"])
        and type(view.get("fresh")) is bool and _instant(as_of) is not None
        and (verified_at is None or _instant(verified_at) is not None)
        and (age is None or age >= 0)
        and (reason is None or isinstance(reason, str))
        and (view.get("fresh") is not True or (verified_at is not None and reason is None))
    )
    if data is not None and not valid_metadata:
        warnings.append("Authority freshness metadata unknown or malformed.")
    rows = view.get("rows")
    rows_valid = isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
    count_valid = _summary_valid(view.get("summary")) if command in {"status", "list", "watch"} else None
    body_valid = True
    if command in {"status", "list", "watch", "history"}:
        page_valid = (
            isinstance(view.get("snapshot_id"), str) and bool(view["snapshot_id"])
            and "next_cursor" in view and (
                view["next_cursor"] is None or
                isinstance(view["next_cursor"], str) and bool(view["next_cursor"]))
        )
        body_valid = rows_valid and page_valid
        if not rows_valid:
            warnings.append("Page rows unknown or malformed.")
        if not page_valid:
            warnings.append("Page identity or continuation unknown or malformed.")
        if rows_valid:
            shape_valid = all(
                _history_row_valid(row) if command == "history" else _task_valid(row) for row in rows)
            if not shape_valid:
                body_valid = False
                warnings.append("Task or history row fields unknown or malformed.")
        if command != "history" and count_valid and rows_valid:
            total = view["summary"]["total"]
            count_valid = total >= len(rows) and (
                reason == "pinned_snapshot" or view.get("next_cursor") is not None or total == len(rows))
        if command != "history" and not count_valid:
            body_valid = False
            warnings.append("Full-scope counts unknown or inconsistent; page length is not the total.")
        if command == "history" and not (
            _count(view.get("after_record_seq")) and _count(view.get("upper_record_seq"))
        ):
            body_valid = False
            warnings.append("History sequence bounds unknown.")
    elif command == "show":
        body_valid = _task_valid(view.get("task"), detail=True) and _eligibility_valid(view.get("eligibility"))
        if not body_valid:
            warnings.append("Task detail or eligibility unknown.")
    if error is not None:
        state = "OUTCOME UNKNOWN" if (
            data is None or error.get("ambiguous") is True or error.get("code") == "outcome_unknown"
        ) else "STALE"
        reason = error.get("code", "unknown_error")
    elif not valid_metadata or not body_valid:
        state = "OUTCOME UNKNOWN"
    elif reason == "pinned_snapshot":
        state = "HISTORICAL SNAPSHOT"
    elif view.get("fresh") is True:
        state = "CURRENT"
    elif reason in {"pending_command", "outcome_unknown", "uninitialized", "unverified", "not_started"}:
        state = "OUTCOME UNKNOWN"
    else:
        state = "STALE"
    current = state == "CURRENT"
    if not current:
        warnings.append("Captured as_of values are not live countdowns or execution authorization.")
    next_cursor = view.get("next_cursor")
    if next_cursor is not None:
        warnings.append("More rows exist; this page is not the entire scope. Continue with next_cursor.")
    task_views = []
    if command == "show" and isinstance(view.get("task"), dict):
        task_views = [_task_display(view["task"], as_of, detail=True)]
    elif command in {"status", "list", "watch"} and rows_valid:
        task_views = [_task_display(task, as_of) for task in rows]
    return {
        "inspection_schema_version": 1, "command": command, "data": data,
        "display": {
            "state": state, "current": current, "reason": reason,
            "data_valid": valid_metadata and body_valid,
            "scope": _redact(scope or {}), "as_of": as_of, "last_verified_at": verified_at,
            "verification_age_seconds": age if age is not None and age >= 0 else None,
            "observation_age_seconds": observation_age_seconds,
            "counts_scope": "full_filtered_scope" if count_valid else "unknown" if count_valid is False else None,
            "page": {
                "snapshot_id": view.get("snapshot_id"),
                "row_count": len(rows) if rows_valid else None,
                "next_cursor": next_cursor,
            } if command != "show" else None,
            "tasks": task_views, "warnings": _redact(warnings),
        },
        "error": error,
    }


def render_json(frame):
    """One canonical JSON object, without a trailing newline or terminal codes."""
    return json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _duration(seconds):
    return "unknown" if seconds is None else f"{seconds:g}s"


def _task_lines(task, display, eligibility=None):
    ready = eligibility.get("ready") if eligibility is not None else task.get("ready")
    reasons = eligibility.get("reasons") if eligibility is not None else task.get("reasons")
    remaining = display["lease_remaining_seconds"]
    lease = "unknown" if remaining is None else (
        f"EXPIRED ({_duration(-remaining)} ago)" if remaining <= 0 else _duration(remaining))
    lines = [
        f"id={escape_text(task.get('id'))} title={escape_text(task.get('title'))} "
        f"priority={escape_text(task.get('priority'))} status={escape_text(task.get('status'))} "
        f"owner={escape_text(task.get('assignee'))}",
        f"  attempt={escape_text(display['attempt_id'])} "
        f"attempt_status={escape_text(display['attempt_status'])} "
        f"generation={escape_text(task.get('claim_generation'))} "
        f"lease_revision={escape_text(task.get('lease_revision'))} lease_remaining={lease} "
        f"progress_age={_duration(display['progress_age_seconds'])} (checkpoint, at server as_of)",
        f"  ready={escape_text(ready)} needs_attention={escape_text(task.get('needs_attention', 'unknown'))} "
        f"blockers={escape_text(task.get('blockers', 'see server reasons/relations'))} "
        f"reasons={escape_text(reasons)}",
        f"  resources={escape_text(task.get('resource_keys'))} recovery={escape_text(task.get('recovery'))}",
    ]
    if display["pending_expiry"]:
        lines.append("  EXPIRED deadline at as_of; stored in_progress, authorization expired / pending expiry.")
    lines.append(f"  progress_remaining={_duration(display['progress_remaining_seconds'])} "
                 f"hard_remaining={_duration(display['hard_remaining_seconds'])} (at server as_of)")
    return lines


def render_text(frame):
    """Plain append-only text; every server/user string is escaped as data."""
    data, display = frame["data"] or {}, frame["display"]
    scope = display["scope"]
    scope_text = "captured cursor scope" if scope.get("captured_cursor") else (
        escape_text(scope) if scope else "all tasks")
    lines = [
        f"{display['state']} authority={escape_text(data.get('authority_id', 'unknown'))} "
        f"scope={scope_text} as_of={escape_text(display['as_of'])}",
        f"last_verified_at={escape_text(display['last_verified_at'])} "
        f"verification_age={_duration(display['verification_age_seconds'])} "
        f"reason={escape_text(display['reason'])}",
    ]
    if display["observation_age_seconds"] is not None:
        lines.append(f"Last successful observation age={_duration(display['observation_age_seconds'])}; "
                     f"observation as_of={escape_text(display['as_of'])}.")
    if frame["error"] is not None:
        lines.append("error=" + escape_text(frame["error"]))
    if frame["data"] is None:
        lines.append("No verified task state; service unavailable or response unknown.")
    if frame["command"] in {"status", "list", "watch"}:
        if display["counts_scope"] == "full_filtered_scope":
            summary = data["summary"]
            lines.append(f"Counts (full filtered scope): total={summary['total']} ready={summary['ready']} "
                         f"needs_attention={summary['needs_attention']} "
                         f"statuses={escape_text(summary['statuses'])}")
            if display["current"] and summary["total"] == 0:
                lines.append("No tasks in verified scope.")
        else:
            lines.append("counts=unknown (full filtered scope)")
        for task, task_view in zip(data.get("rows") or [], display["tasks"]):
            lines.extend(_task_lines(task, task_view))
    elif frame["command"] == "show" and display["tasks"]:
        task = data["task"]
        lines.extend(_task_lines(task, display["tasks"][0], data.get("eligibility")))
        for key in (
            "description", "acceptance", "goal_id", "execution_class", "execution_profile",
            "admitted", "hold_reason", "deferred_until", "policy", "automatic_retries_used",
            "retry_budget_granted", "retry_not_before", "escalation", "completion",
            "cancellation_reason", "attempt", "goal_policy", "graph_revision",
        ):
            lines.append(f"  {key}={escape_text(task.get(key))}")
        lines.append("  relations=" + escape_text(data.get("relations")))
    elif frame["command"] == "history":
        lines.append(f"task_id={escape_text(data.get('task_id'))} "
                     f"after_record_seq={escape_text(data.get('after_record_seq'))} (exclusive) "
                     f"upper_record_seq={escape_text(data.get('upper_record_seq'))} (captured)")
        for record in data.get("rows") or []:
            if isinstance(record, dict):
                lines.append(f"record_seq={escape_text(record.get('record_seq'))} " + escape_text(record))
    if display["page"] is not None:
        page = display["page"]
        lines.append(f"page rows={escape_text(page['row_count'])} "
                     f"snapshot_id={escape_text(page['snapshot_id'])} "
                     f"next_cursor={escape_text(page['next_cursor'])}")
    lines.extend("WARNING: " + escape_text(warning) for warning in display["warnings"])
    return "\n".join(lines)


def _validate(command, options):
    def require(condition):
        if not condition:
            raise TaskClientError("invalid_options", "Invalid inspection command or options.")

    require(type(command) is str and command in _COMMANDS)
    require(type(options) is dict and set(options) <= _OPTIONS)
    result = {
        "limit": 100, "after_record_seq": 0, "json": False,
        "refresh_seconds": 5.0, "duration_seconds": 600.0, **options,
    }
    for key in (*_FILTERS, "task_id", "cursor"):
        value = result.get(key)
        if value is None:
            continue
        if key == "needs_attention":
            require(type(value) is bool)
        else:
            require(type(value) is str and bool(value.strip()))
    require(result.get("status") is None or result["status"] in _STATUSES)
    require(result.get("kind") is None or result["kind"] in {"task", "epic"})
    require(type(result["limit"]) is int and 1 <= result["limit"] <= 500)
    require(type(result["after_record_seq"]) is int and 0 <= result["after_record_seq"] <= 2**63 - 1)
    require(type(result["json"]) is bool)
    for key in ("refresh_seconds", "duration_seconds"):
        value = result[key]
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        require(valid)
    require(command not in {"show", "history"} or result.get("task_id") is not None)
    return result


def _query(command, options):
    if command == "show":
        return "mptask_get", {"task_id": options["task_id"]}
    arguments = {"limit": options["limit"]}
    if options.get("cursor") is not None:
        arguments["cursor"] = options["cursor"]
    if command == "history":
        return "mptask_history", {**arguments, "task_id": options["task_id"],
                                  "after_record_seq": options["after_record_seq"]}
    filters = {key: options[key] for key in _FILTERS if options.get(key) is not None}
    if filters:
        arguments["filters"] = filters
    return "mptask_snapshot", arguments


def _scope(command, options):
    if command in {"show", "history"}:
        return {"task_id": options.get("task_id")}
    filters = {key: options[key] for key in _FILTERS if options.get(key) is not None}
    return filters or ({"captured_cursor": True} if options.get("cursor") else {})


def _error(error):
    return {"code": error.code, "message": error.message,
            "ambiguous": error.ambiguous, "details": error.details}


def _emit(frame, json_mode, stdout):
    stdout.write((render_json(frame) if json_mode else render_text(frame)) + "\n")
    stdout.flush()
    return 0 if frame["display"]["current"] else 1


def _read_frame(client, command, options, *, cached=None, cached_scope=None, observation_age_seconds=None):
    scope = _scope(command, options)
    try:
        tool, arguments = _query(command, options)
        return build_frame(command, client.call_tool(tool, arguments), scope=scope)
    except TaskClientError as error:
        return build_frame(command, cached, scope=cached_scope if cached is not None else scope, error=_error(error),
                           observation_age_seconds=observation_age_seconds)
    except Exception:
        return build_frame(command, cached, scope=cached_scope if cached is not None else scope,
                           observation_age_seconds=observation_age_seconds,
                           error=_error(TaskClientError("inspection_error", "Unexpected inspection failure.")))


def _watch(client, options, stdout, stderr, monotonic, sleep):
    start = monotonic()
    interval, duration = options["refresh_seconds"], options["duration_seconds"]
    timeout = getattr(client, "timeout", None)
    try:
        valid = (
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
            and math.isfinite(start + duration) and math.isfinite(duration / interval)
            and start + interval > start and start + duration > start
        )
    except OverflowError:
        valid = False
    if not valid:
        frame = build_frame("watch", None, error=_error(TaskClientError(
            "invalid_options", "Watch needs a finite positive client.timeout and representable time bounds.")))
        _emit(frame, options["json"], stdout)
        return 2
    deadline = start + duration
    cached, cached_scope, observed_at, last_exit = None, None, None, None
    try:
        while monotonic() < deadline:
            now = monotonic()
            if deadline - now < timeout:
                if last_exit is None:
                    return _emit(build_frame("watch", None, scope=_scope("watch", options),
                                            error=_error(TaskClientError(
                                                "watch_budget_exhausted",
                                                "Remaining watch duration is below client.timeout; no read started."))),
                                 options["json"], stdout)
                stderr.write("Watch stopped before next read: remaining duration is below client.timeout; "
                             "the final frame is the last completed observation.\n")
                return last_exit
            frame = _read_frame(client, "watch", options, cached=cached, cached_scope=cached_scope,
                                observation_age_seconds=None if observed_at is None else now - observed_at)
            completed = monotonic()
            if frame["error"] is not None:
                if observed_at is not None:
                    frame["display"]["observation_age_seconds"] = completed - observed_at
                if frame["error"]["code"] == "snapshot_expired":
                    cached, cached_scope, observed_at = None, None, None
                    frame = build_frame("watch", None, scope=_scope("watch", options),
                                        error=frame["error"],
                                        warnings=("Expired cursor discarded; next refresh starts a new capture.",))
            elif frame["display"]["data_valid"]:
                cached, observed_at = frame["data"], completed
                cached_scope = frame["display"]["scope"]
            if completed > deadline:
                frame = build_frame("watch", frame["data"], scope=_scope("watch", options),
                                    error=_error(TaskClientError(
                                        "watch_deadline_exceeded", "Client read exceeded the watch duration.")))
            last_exit = _emit(frame, options["json"], stdout)
            if frame["error"] is not None and frame["error"]["code"] in {
                "invalid_cursor", "unknown_tool", "invalid_configuration",
                "invalid_arguments", "validation_error", "not_found",
            }:
                return last_exit
            options.pop("cursor", None)
            # Anchor to the initial cadence; never enqueue ticks missed during a read.
            next_tick = start + (math.floor((completed - start) / interval) + 1) * interval
            remaining = min(next_tick, deadline) - monotonic()
            if remaining > 0:
                sleep(remaining)
        return last_exit if last_exit is not None else 1
    except KeyboardInterrupt:
        frame = build_frame("watch", cached, scope=cached_scope or _scope("watch", options),
                            error=_error(TaskClientError("interrupted", "Inspection interrupted.")),
                            observation_age_seconds=None if observed_at is None else monotonic() - observed_at)
        _emit(frame, options["json"], stdout)
        return 130


def run_inspection(client, command: str, options: dict, *, stdout, stderr,
                   monotonic=time.monotonic, sleep=time.sleep) -> int:
    """Inspect via call_tool; exits 0=current, 1=noncurrent, 2=local error, 130=Ctrl-C.

    Watch never changes client.timeout. It stops before a read cannot fit that
    configured bound; in this case its final frame remains the last completed
    observation, not a newly verified view of the stop time.
    """
    try:
        options = _validate(command, options)
    except TaskClientError as error:
        safe_command = command if isinstance(command, str) and command in _COMMANDS else "unknown"
        frame = build_frame(safe_command, None, error=_error(error))
        json_mode = isinstance(options, dict) and options.get("json") is True
        _emit(frame, json_mode, stdout)
        if not json_mode:
            stderr.write("invalid_options: Invalid inspection command or options.\n")
        return 2
    if command == "watch":
        return _watch(client, options, stdout, stderr, monotonic, sleep)
    try:
        frame = _read_frame(client, command, options)
    except KeyboardInterrupt:
        _emit(build_frame(command, None, scope=_scope(command, options),
                          error=_error(TaskClientError("interrupted", "Inspection interrupted."))),
              options["json"], stdout)
        return 130
    return _emit(frame, options["json"], stdout)
