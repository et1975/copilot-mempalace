#!/usr/bin/env python3
"""Provisioned-MemPalace worker for the stdlib Copilot hook adapter.

Run only with explicitly configured MEMPALACE_PYTHON. All palace writes use
the installed stdio dispatcher: existing hub forwarding or guarded local writer
admission, never a raw handler. A hooks policy selecting the installed daemon
is refused because its raw mcp_tool path lacks that admission. No CLI policy,
embedding provider, collection writer or daemon launcher is substituted.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import time

import copilot_transcript as adapter

PART_CHARACTERS = 10000


def capture(request: dict) -> dict:
    """Return an exact batch receipt; a partial/uncertain write is never saved."""
    with adapter._open_regular(Path(request["snapshot"])) as source:
        records = [json.loads(line) for line in source if line.strip()]
    digest = adapter._digest(records)
    if request.get("protocol") != 1 or request.get("sha256") != digest:
        raise adapter.CaptureError("invalid_capture_request")
    if any(record.get("sessionId") != request.get("session_id") for record in records):
        raise adapter.CaptureError("session_identity_mismatch")

    def receipt(outcome, code, **fields):
        return {
            "protocol": 1, "outcome": outcome, "code": code,
            "sha256": digest, "messages": len(records), **fields,
        }

    try:
        from mempalace.config import MempalaceConfig, sanitize_content, sanitize_name
        from mempalace import daemon
        from mempalace.write_routing import choose_write_route, WriteRoutingPolicy
        from mempalace.hooks_cli import _wing_from_transcript_path
    except ImportError:
        return receipt("failed", "mempalace_api_unavailable", write_started=False)

    config = MempalaceConfig()
    if not config.hooks_auto_save:
        return receipt("skipped", "auto_save_disabled", write_started=False)
    try:
        resolved = config.resolve_write_routing("hooks")
    except ValueError:
        return receipt("failed", "hooks_routing_invalid", write_started=False)
    available = False
    if resolved.policy is not WriteRoutingPolicy.DIRECT:
        available = daemon.get_client_if_running(
            config.palace_path, health_timeout=daemon.HOOK_PROBE_TIMEOUT,
        ) is not None
    routing = choose_write_route(
        resolved.policy, daemon_available=available, daemon_can_start=False,
    )
    if routing.blocked:
        return receipt("failed", "daemon_required_unavailable", write_started=False)
    if not Path(config.palace_path).is_dir():
        return receipt("failed", "palace_unavailable", write_started=False)
    try:
        project = adapter._project_path(request.get("project_cwd"))
        if any(record.get("cwd") != project for record in records):
            return receipt("failed", "project_context_conflict", write_started=False)
        wing = sanitize_name(_wing_from_transcript_path(request["snapshot"]), "wing")
        if config.chunk_size <= 0:
            raise ValueError("invalid chunk configuration")
        planned = []
        for record in records:
            original = record["message"]["content"]
            content_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
            part_count = max(1, (len(original) + PART_CHARACTERS - 1) // PART_CHARACTERS)
            for index in range(part_count):
                content = json.dumps(
                    {
                        "schema": "copilot-observation-v2",
                        "session_id": record["sessionId"], "event_id": record["uuid"],
                        "timestamp": record["timestamp"], "role": record["type"],
                        "part_index": index, "part_count": part_count,
                        "content_sha256": content_digest,
                        "content": original[index * PART_CHARACTERS:(index + 1) * PART_CHARACTERS],
                    },
                    sort_keys=True, ensure_ascii=False,
                )
                # Validate the complete batch before admission. The API's
                # 100000-character limit applies before its internal chunker.
                if sanitize_content(content) != content:
                    raise ValueError("content would be changed")
                planned.append({
                    "wing": wing, "room": "diary", "content": content,
                    "source_file": request["source_file"],
                    "added_by": "copilot-cli-session-hook",
                })
    except (adapter.CaptureError, KeyError, TypeError, ValueError):
        return receipt("failed", "observation_preflight_failed", write_started=False)
    # The installed daemon's mcp_tool service invokes raw handlers without
    # stdio writer admission. Do not offer that unsafe path or silently fall
    # back after hooks policy selected the daemon.
    if routing.use_daemon:
        return receipt("failed", "daemon_capture_unavailable", write_started=False)
    route = "mcp"
    try:
        from mempalace.mcp_server import _dispatch_stdio_request
    except ImportError:
        return receipt("failed", "mempalace_api_unavailable", write_started=False)

    deadline = time.monotonic() + min(float(request["timeout"]), adapter.SAVE_TIMEOUT)
    drawer_ids = []
    for arguments in planned:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return receipt("unknown", "capture_timeout", write_started=bool(drawer_ids))
        try:
            response = _dispatch_stdio_request({
                "jsonrpc": "2.0", "id": len(drawer_ids) + 1, "method": "tools/call",
                "params": {"name": "mempalace_add_drawer", "arguments": arguments},
            })
            if response.get("error"):
                if response["error"].get("code") == -32001 and not drawer_ids:
                    return receipt("failed", "writer_unavailable", write_started=False)
                if response["error"].get("code") == -32003 and not drawer_ids:
                    return receipt("failed", "capture_read_only", write_started=False)
                return receipt("unknown", "capture_write_unsettled", write_started=True)
            result = json.loads(response["result"]["content"][0]["text"])
        except (OSError, RuntimeError, ValueError):
            return receipt("unknown", "capture_write_unsettled", write_started=True)
        if not result.get("success") or not isinstance(result.get("drawer_id"), str):
            return receipt("unknown", "capture_write_unconfirmed", write_started=True)
        drawer_ids.append(result["drawer_id"])
    return receipt(
        "saved", "confirmed", wing=wing, room="diary", route=route,
        drawer_ids=drawer_ids, parts=len(planned), write_started=bool(drawer_ids),
    )


def main() -> int:
    # mcp_server deliberately redirects stdout at import, including fd 1.
    # Preserve a private protocol handle before any MemPalace imports.
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as protocol:
        try:
            result = capture(json.load(sys.stdin))
        except (ImportError, adapter.CaptureError, KeyError, TypeError, ValueError, OSError):
            # No save assertion or retry permission for an unexpected worker
            # failure. The parent's persisted intent remains unsettled.
            result = {"protocol": 1, "outcome": "unknown", "code": "capture_worker_error"}
        json.dump(result, protocol)
        protocol.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
