"""Read-only, bounded Copilot event evidence for offline activity review.

Descriptions and task prompts are agent statements, not user intent. Artifact
paths are observations only: this module never opens them or executes commands.
The envelope hash identifies the exact input snapshot for every source reference.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat


DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EVENTS = 100000
DEFAULT_MAX_CALLS = 20000
DEFAULT_MAX_TEXT_CHARS = 2000
DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_RETAINED_BYTES = 16 * 1024 * 1024

_MAX_DEPTH = 64
_MAX_NODES = 20000
_MAX_PATH_CHARS = 4096
_MAX_ID_CHARS = 1024
_MAX_ARTIFACTS = 256
_MAX_COMMAND_CHARS = 65536
_MAX_COMMAND_TOKENS = 4096
_PATH_KEYS = {
    "path", "paths", "file_path", "file_paths", "filePath", "filePaths",
    "filename", "filenames", "fileName", "fileNames", "target_file", "targetFile",
}
_BODY_KEYS = {"content", "transformedContent", "code", "command", "description", "prompt", "patch", "input"}
_FILE_RELATIONS = {
    "view": "read", "read": "read", "read_file": "read",
    "create": "created", "create_file": "created",
    "edit": "modified", "edit_file": "modified", "write_file": "modified",
}
_SHELL_TOOLS = {"bash", "shell", "powershell", "terminal", "run_in_terminal"}
_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_JSON_STRUCTURE = re.compile(r'["\\{}\[\]]')
_RETAIN_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class EvidenceError(ValueError):
    """Content-free input or limit diagnostic, safe to display."""


def _fail(code: str) -> None:
    raise EvidenceError(code) from None


def _text(value, *, optional: bool = False, cap: int = _MAX_ID_CHARS):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _fail("invalid_text")
    if _SURROGATE.search(value):
        _fail("invalid_unicode")
    if len(value) > cap:
        _fail("text_limit")
    return value


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _records(path: Path, max_bytes: int, max_line_bytes: int, digest):
    """Yield bounded binary records; only exhaustion establishes a stable scan."""
    fd = None
    try:
        before = os.stat(path)
        if not stat.S_ISREG(before.st_mode):
            _fail("source_not_regular")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            _fail("source_not_regular")
        if _identity(before) != _identity(opened):
            _fail("source_changed")
        if opened.st_size > max_bytes:
            _fail("source_byte_limit")
        record = bytearray()
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail("source_byte_limit")
            digest.update(chunk)
            offset = 0
            while offset < len(chunk):
                end = chunk.find(b"\n", offset)
                stop = len(chunk) if end < 0 else end + 1
                if len(record) + stop - offset > max_line_bytes:
                    _fail("record_byte_limit")
                record.extend(chunk[offset:stop])
                offset = stop
                if end >= 0:
                    raw = bytes(record)
                    record.clear()
                    yield raw
                    del raw
        if record:
            raw = bytes(record)
            record.clear()
            yield raw
            del raw
        after = os.fstat(fd)
        named_after = os.stat(path)
        if _identity(opened) != _identity(after) or _identity(after) != _identity(named_after):
            _fail("source_changed")
        if total != opened.st_size:
            _fail("source_changed")
    except OSError:
        _fail("source_io_error")
    finally:
        if fd is not None:
            os.close(fd)


def _pairs(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key")
        value[key] = item
    return value


def _json_line(line: str) -> dict:
    depth = 0
    quoted = False
    escaped_position = -1
    for match in _JSON_STRUCTURE.finditer(line):
        position = match.start()
        char = match.group()
        if quoted:
            if position == escaped_position:
                continue
            elif char == "\\":
                escaped_position = position + 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_DEPTH:
                _fail("payload_depth_limit")
        elif char in "]}":
            depth -= 1
    try:
        value = json.loads(line, object_pairs_hook=_pairs, parse_constant=lambda _: _fail("invalid_json"))
    except EvidenceError:
        raise
    except (ValueError, RecursionError):
        _fail("invalid_json")
    if not isinstance(value, dict):
        _fail("invalid_event")
    pending = [value]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > _MAX_NODES:
            _fail("payload_node_limit")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and _SURROGATE.search(item):
            _fail("invalid_unicode")
        elif isinstance(item, float) and not math.isfinite(item):
            _fail("invalid_json")
    return value


def _declared(event: dict, data: dict, key: str, *, required: bool = False):
    outer = _text(event.get(key), optional=True)
    inner = _text(data.get(key), optional=True)
    if outer is not None and inner is not None and outer != inner:
        _fail("conflicting_identity")
    value = inner if inner is not None else outer
    if required and value is None:
        _fail("missing_identity")
    return value


def _reference(event: dict, line: int, field: str = "data") -> dict:
    return {
        "line": line,
        "event_id": event.get("id"),
        "timestamp": event.get("timestamp"),
        "field": field,
    }


def _claim(event: dict, line: int, name: str, text: str, cap: int) -> dict:
    return {
        "kind": "description" if name == "description" else "delegation_prompt",
        "attribution": "agent",
        "text": text[:cap],
        "truncated": len(text) > cap,
        "source": _reference(event, line, f"data.arguments.{name}"),
    }


def _add_artifact(artifacts: list, path, relation: str, source: dict) -> None:
    path = _text(path, cap=_MAX_PATH_CHARS)
    _text(source["field"], cap=_MAX_PATH_CHARS)
    if any(ord(char) < 32 for char in path):
        _fail("invalid_path")
    if len(artifacts) >= _MAX_ARTIFACTS:
        _fail("artifact_limit")
    artifacts.append({"path": path, "relation": relation, "source": source})


def _file_paths(arguments: dict, relation: str, event: dict, line: int, artifacts: list) -> None:
    pending = [(arguments, "data.arguments")]
    while pending:
        item, field = pending.pop()
        if len(field) > _MAX_PATH_CHARS:
            _fail("field_limit")
        if isinstance(item, dict):
            children = []
            for key, value in item.items():
                suffix = f".{key}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f"[{json.dumps(key)}]"
                child_field = field + suffix
                if len(child_field) > _MAX_PATH_CHARS:
                    _fail("field_limit")
                if key in _PATH_KEYS:
                    if isinstance(value, list):
                        for index, path in enumerate(value):
                            _add_artifact(
                                artifacts, path, relation,
                                _reference(event, line, f"{child_field}[{index}]"),
                            )
                    else:
                        _add_artifact(artifacts, value, relation, _reference(event, line, child_field))
                elif key not in _BODY_KEYS and isinstance(value, (dict, list)):
                    children.append((value, child_field))
            pending.extend(reversed(children))
        elif isinstance(item, list):
            pending.extend((value, f"{field}[{index}]") for index, value in reversed(list(enumerate(item))))


def _patch_paths(patch: str, field: str, event: dict, line: int, artifacts: list) -> None:
    lines = patch.strip().splitlines()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return
    for text in lines[1:-1]:
        match = _PATCH_HEADER.fullmatch(text)
        if match:
            operation, path = match.groups()
            _add_artifact(
                artifacts, path, "created" if operation == "Add" else "modified",
                _reference(event, line, field),
            )


def _shell_paths(command: str, event: dict, line: int, artifacts: list) -> None:
    if len(command) > _MAX_COMMAND_CHARS:
        _fail("command_limit")
    if "\n" in command or "\r" in command or "<<" in command:
        return
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    skip_body = False
    try:
        for index, token in enumerate(lexer):
            if index >= _MAX_COMMAND_TOKENS:
                _fail("command_token_limit")
            if skip_body:
                skip_body = False
                continue
            if token.lower() in {"-c", "-e", "--eval", "--command", "-command", "-encodedcommand"}:
                skip_body = True
                continue
            if token.startswith("-") or "://" in token or not re.fullmatch(r"[A-Za-z0-9_./~:\\+-]+", token):
                continue
            if "/" not in token and not re.fullmatch(r"[^.\s]+\.[^.\s]+", token):
                continue
            _add_artifact(artifacts, token, "referenced", _reference(event, line, "data.arguments.command"))
    except EvidenceError:
        raise
    except ValueError:
        # A shell dialect we cannot tokenize supplies no token evidence.
        artifacts[:] = [a for a in artifacts if a["source"]["field"] != "data.arguments.command"]


def _start(event: dict, data: dict, line: int, text_cap: int) -> dict:
    call_id = _declared(event, data, "toolCallId", required=True)
    tool = _declared(event, data, "toolName", required=True)
    leaf = tool.rsplit(".", 1)[-1]
    arguments = data.get("arguments", {})
    if not isinstance(arguments, dict) and not (leaf == "apply_patch" and isinstance(arguments, str)):
        _fail("invalid_arguments")
    claims = []
    artifacts = []
    if isinstance(arguments, dict):
        for name in ("description", "prompt") if leaf == "task" else ("description",):
            if name in arguments:
                text = arguments[name]
                if not isinstance(text, str):
                    _fail("invalid_claim")
                if text.strip():
                    claims.append(_claim(event, line, name, text, text_cap))
        _file_paths(arguments, _FILE_RELATIONS.get(leaf, "referenced"), event, line, artifacts)
        if leaf == "apply_patch" and "patch" in arguments:
            if not isinstance(arguments["patch"], str):
                _fail("invalid_patch")
            _patch_paths(arguments["patch"], "data.arguments.patch", event, line, artifacts)
        if leaf in _SHELL_TOOLS and "command" in arguments:
            if not isinstance(arguments["command"], str):
                _fail("invalid_command")
            _shell_paths(arguments["command"], event, line, artifacts)
    else:
        _patch_paths(arguments, "data.arguments", event, line, artifacts)
    return {
        "call_id": call_id,
        "tool_name": tool,
        "agent_id": _declared(event, data, "agentId"),
        "parent_call_id": _declared(event, data, "parentToolCallId"),
        "source": _reference(event, line),
        "claims": claims,
        "artifacts": artifacts,
        "outcome": {
            "completion_observed": False, "tool_success": None, "exit_code": None, "source": None,
        },
    }


def _completion(event: dict, data: dict, line: int) -> dict:
    success = data.get("success")
    if success is not None and type(success) is not bool:
        _fail("invalid_success")
    shell = data.get("shellExecution")
    if shell is not None and not isinstance(shell, dict):
        _fail("invalid_shell_outcome")
    exit_code = shell.get("exitCode") if shell is not None else None
    if exit_code is not None and type(exit_code) is not int:
        _fail("invalid_exit_code")
    return {
        "call_id": _declared(event, data, "toolCallId", required=True),
        "tool_name": _declared(event, data, "toolName"),
        "agent_id": _declared(event, data, "agentId"),
        "parent_call_id": _declared(event, data, "parentToolCallId"),
        "outcome": {
            "completion_observed": True, "tool_success": success, "exit_code": exit_code,
            "source": _reference(event, line),
        },
    }


def _link(calls: dict, completions: dict, bindings: dict) -> list[str]:
    warnings = []
    for call_id, completed in completions.items():
        call = calls.get(call_id)
        if call is None:
            warnings.append(f"orphan_completion:line={completed['outcome']['source']['line']}")
            continue
        if completed["tool_name"] is not None and completed["tool_name"] != call["tool_name"]:
            _fail("conflicting_completion_identity")
        for field in ("agent_id", "parent_call_id"):
            value = completed[field]
            if value is not None:
                if call[field] is not None and call[field] != value:
                    _fail("conflicting_completion_identity")
                call[field] = value
        call["outcome"] = completed["outcome"]
    for call in calls.values():
        parent = bindings.get(call["agent_id"])
        if parent is not None:
            if call["parent_call_id"] is not None and call["parent_call_id"] != parent:
                _fail("conflicting_agent_parent")
            call["parent_call_id"] = parent
    checked = set()
    for call_id in calls:
        seen = set()
        current = call_id
        while current in calls and current not in checked:
            if current in seen:
                _fail("parent_cycle")
            seen.add(current)
            current = calls[current]["parent_call_id"]
        checked.update(seen)
    return warnings


def _project(raw: bytes, line: int, session_id: str, text_cap: int):
    """Validate one entire record and release its payload after projection."""
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        _fail("invalid_utf8")
    if not text or text.isspace():
        return None
    event = _json_line(text)
    kind = _text(event.get("type"))
    event_id = _text(event.get("id"), optional=True)
    _text(event.get("timestamp"), optional=True)
    entry = None
    if kind in {"tool.execution_start", "tool.execution_complete", "subagent.started", "session.start"}:
        data = event.get("data")
        if not isinstance(data, dict):
            _fail("invalid_event_data")
        declared_session = _declared(event, data, "sessionId", required=kind == "session.start")
        if declared_session is not None and declared_session != session_id:
            _fail("session_identity_mismatch")
        if kind == "tool.execution_start":
            entry = _start(event, data, line, text_cap)
        elif kind == "tool.execution_complete":
            entry = _completion(event, data, line)
        elif kind == "subagent.started":
            agent = _declared(event, data, "agentId", required=True)
            parent = _declared(event, data, "toolCallId", required=True)
            _declared(event, data, "parentToolCallId")
            entry = (agent, parent)
    return event_id, kind, entry


def _charge_retained(key: str, value, used: int, limit: int) -> int:
    for part in _RETAIN_ENCODER.iterencode({key: value}):
        used += len(part.encode("utf-8"))
        if used > limit:
            _fail("evidence_byte_limit")
    return used


def load_evidence(
    events_path: str,
    session_id: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_calls: int = DEFAULT_MAX_CALLS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES,
) -> dict:
    """Load an explicit event snapshot without accessing referenced artifacts.

    Limits are positive integers; overflow raises content-free ``EvidenceError``
    (a ``ValueError``), never a successful partial packet. Only quotes truncate.
    Scan bytes and retained evidence have independent limits. Each physical
    record, including its LF/CRLF terminator, must fit ``max_line_bytes``.
    Records are hashed, validated, projected and discarded in one pass.

    ``max_retained_bytes`` charges the compact UTF-8 JSON size of each normalized
    single-entry call/completion/binding mapping once, before storing it. Both
    start and completion entries count, even when later linked. This is an
    evidence-size budget, not an exact Python heap limit: container overhead,
    the final envelope/warnings, the current record, and the event-ID set are
    excluded. Event IDs are separately bounded by ``max_events`` and the
    identifier cap; raw argument/result bodies never count as retained evidence.

    Additional defensive caps: 64 JSON nesting levels, 20,000 nodes per event,
    1,024-character identifiers, 4,096-character paths/field references and 256
    artifact observations per call. Shell tokenization caps command text at
    65,536 characters and 4,096 tokens, omitting bodies and complex dialects.

    ``data.shellExecution.exitCode`` is the only process outcome source; result
    text cannot establish an exit code. ``parentId`` is never a call link.
    """
    for limit in (max_bytes, max_events, max_calls, max_text_chars, max_line_bytes, max_retained_bytes):
        if type(limit) is not int or limit <= 0:
            _fail("invalid_limit")
    _text(events_path, cap=32768)
    _text(session_id)
    path = Path(events_path).absolute()
    digest = hashlib.sha256()
    calls = {}
    completions = {}
    bindings = {}
    event_ids = set()
    count = byte_count = retained_bytes = 0
    with closing(_records(path, max_bytes, max_line_bytes, digest)) as records:
        for line, raw in enumerate(records, 1):
            byte_count += len(raw)
            projected = _project(raw, line, session_id, max_text_chars)
            del raw
            if projected is None:
                continue
            count += 1
            if count > max_events:
                _fail("event_limit")
            event_id, kind, entry = projected
            if event_id is not None:
                if event_id in event_ids:
                    _fail("duplicate_event_id")
                event_ids.add(event_id)
            if kind == "tool.execution_start":
                if len(calls) >= max_calls:
                    _fail("call_limit")
                call_id = entry["call_id"]
                if call_id in calls:
                    _fail("duplicate_call")
                retained_bytes = _charge_retained(call_id, entry, retained_bytes, max_retained_bytes)
                calls[call_id] = entry
            elif kind == "tool.execution_complete":
                call_id = entry["call_id"]
                if call_id in completions:
                    _fail("duplicate_completion")
                if len(completions) >= max_calls:
                    _fail("completion_limit")
                retained_bytes = _charge_retained(call_id, entry, retained_bytes, max_retained_bytes)
                completions[call_id] = entry
            elif kind == "subagent.started":
                agent, parent = entry
                if agent in bindings:
                    _fail("ambiguous_agent_binding")
                if len(bindings) >= max_calls:
                    _fail("binding_limit")
                retained_bytes = _charge_retained(agent, parent, retained_bytes, max_retained_bytes)
                bindings[agent] = parent
    warnings = _link(calls, completions, bindings)
    return {
        "schema_version": 1,
        "source": {
            "kind": "copilot_events",
            "path": str(path),
            "session_id": session_id,
            "sha256": digest.hexdigest(),
            "bytes": byte_count,
            "events": count,
        },
        "calls": list(calls.values()),
        "warnings": warnings,
    }
