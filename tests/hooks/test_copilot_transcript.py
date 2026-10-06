"""Tests for the Copilot -> Claude transcript adapter (copilot_transcript.py).

Run from the repository root with ``$TEST_PY -m pytest tests/hooks -q``
and ``PYTHONDONTWRITEBYTECODE=1`` exported. Pure translation/mapping tests
need only pytest. The integration tests
prove mempalace's own claude-code parsers accept our translated output; they
are skipped unless mempalace is importable; use a preprovisioned interpreter
with both pytest and MemPalace to include them.
"""
from __future__ import annotations

import json
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import copilot_transcript as ct
import pytest

try:  # mempalace is only importable under its own venv interpreter
    from mempalace import hooks_cli as _h  # type: ignore

    _HAS_MEMPALACE = True
except Exception:  # pragma: no cover - depends on interpreter
    _h = None
    _HAS_MEMPALACE = False


def _events(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


class TranslateEventsTests(unittest.TestCase):
    def test_user_message_becomes_claude_user_line(self):
        out = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "hello"}}),
            cwd="/repo",
        )
        self.assertEqual(len(out), 1)
        line = json.loads(out[0])
        self.assertEqual(line["type"], "user")
        self.assertEqual(line["message"], {"role": "user", "content": "hello"})

    def test_assistant_message_becomes_claude_assistant_line(self):
        out = ct.translate_events(
            _events({"type": "assistant.message", "data": {"content": "hi there"}}),
            cwd="/repo",
        )
        line = json.loads(out[0])
        self.assertEqual(line["type"], "assistant")
        self.assertEqual(line["message"], {"role": "assistant", "content": "hi there"})

    def test_non_message_events_are_skipped(self):
        out = ct.translate_events(
            _events(
                {"type": "session.start", "data": {"sessionId": "x"}},
                {"type": "assistant.turn_start", "data": {"turnId": "0"}},
                {"type": "hook.start", "data": {}},
            ),
            cwd="/repo",
        )
        self.assertEqual(out, [])

    def test_uses_clean_content_not_transformed(self):
        out = ct.translate_events(
            _events(
                {
                    "type": "user.message",
                    "data": {
                        "content": "the real question",
                        "transformedContent": "<system_reminder>noise</system_reminder>",
                    },
                }
            ),
            cwd="/repo",
        )
        line = json.loads(out[0])
        self.assertEqual(line["message"]["content"], "the real question")

    def test_each_line_carries_top_level_cwd(self):
        out = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": "a"}},
                {"type": "assistant.message", "data": {"content": "b"}},
            ),
            cwd="/home/e/proj",
        )
        for raw in out:
            self.assertEqual(json.loads(raw)["cwd"], "/home/e/proj")

    def test_empty_and_missing_content_skipped(self):
        out = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": ""}},
                {"type": "user.message", "data": {"content": "   "}},
                {"type": "user.message", "data": {}},
            ),
            cwd="/repo",
        )
        self.assertEqual(out, [])

    def test_malformed_lines_are_skipped(self):
        out = ct.translate_events(
            ["not json", "", "{bad", json.dumps({"type": "user.message", "data": {"content": "ok"}})],
            cwd="/repo",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0])["message"]["content"], "ok")


class MapHookTests(unittest.TestCase):
    def test_known_event_names_map_to_cli_flags(self):
        self.assertEqual(ct.map_hook("Stop"), "stop")
        self.assertEqual(ct.map_hook("PreCompact"), "precompact")
        self.assertEqual(ct.map_hook("SessionStart"), "session-start")
        self.assertEqual(ct.map_hook("SessionEnd"), "session-end")

    def test_event_names_are_case_insensitive(self):
        self.assertEqual(ct.map_hook("stop"), "stop")
        self.assertEqual(ct.map_hook("preCompact"), "precompact")

    def test_unknown_event_returns_none(self):
        self.assertIsNone(ct.map_hook("PreToolUse"))
        self.assertIsNone(ct.map_hook(""))


@pytest.fixture
def session(tmp_path, monkeypatch):
    home = tmp_path / "copilot"
    directory = home / "session-state" / "session-123"
    directory.mkdir(parents=True)
    monkeypatch.setenv("COPILOT_HOME", str(home))
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("MEMPALACE_HOOKS_AUTO_SAVE", raising=False)
    monkeypatch.setenv("MEMPALACE_PYTHON", sys.executable)
    return directory


def write_session(directory, messages, *, session_id="session-123", project_cwd="/repo"):
    data = {"sessionId": session_id}
    if project_cwd is not None:
        data["context"] = {"cwd": project_cwd}
    records = [{"type": "session.start", "data": data}]
    records.extend(
        {
            "id": f"event-{index}",
            "type": f"{role}.message",
            "timestamp": f"2026-10-02T10:{index // 60:02}:{index % 60:02}.000Z",
            "data": {"content": content},
        }
        for index, (role, content) in enumerate(messages)
    )
    path = directory / "events.jsonl"
    path.write_text("\n".join(map(json.dumps, records)) + "\n")
    return path


def invoke(payload, runner):
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(ct.sys, "stdin", io.StringIO(json.dumps(payload))), \
         patch.object(ct.sys, "stdout", stdout), \
         patch.object(ct.sys, "stderr", stderr), \
         patch.object(ct.subprocess, "run", runner):
        assert ct.main() == 0
    assert json.loads(stdout.getvalue()) == {}
    return json.loads(stderr.getvalue())


def exit_payload(**changes):
    return {
        "hook_event_name": "SessionEnd", "session_id": "session-123",
        "timestamp": "2026-10-02T11:00:00.000Z", "cwd": "/repo",
        "reason": "user_exit", **changes,
    }


class SuccessfulSweep:
    def __init__(self):
        self.records = []
        self.calls = 0

    def __call__(self, args, **kwargs):
        assert args[0] == os.environ["MEMPALACE_PYTHON"]
        assert Path(args[1]).name == "copilot_capture.py"
        assert len(args) == 2
        assert kwargs["timeout"] <= 25
        request = json.loads(kwargs["input"])
        records = [json.loads(line) for line in Path(request["snapshot"]).read_text().splitlines()]
        self.records.extend(records)
        self.calls += 1
        return subprocess.CompletedProcess(
            args, 0,
            json.dumps({
                "protocol": 1, "outcome": "saved", "code": "confirmed",
                "messages": len(records), "sha256": request["sha256"],
                "parts": len(records), "wing": "wing_repo", "room": "diary", "route": "mcp",
                "drawer_ids": [f"drawer_{record['uuid']}" for record in records],
            }),
            "",
        )


def status(directory):
    return json.loads((directory / "mempalace-save" / "status.json").read_text())


def test_documented_exit_payload_flushes_full_short_session(session):
    write_session(session, [("user", "q" * 500), ("assistant", "a" * 900)])
    save = SuccessfulSweep()
    result = invoke(exit_payload(), save)
    assert result["outcome"] == "saved"
    assert [r["message"]["content"] for r in save.records] == ["q" * 500, "a" * 900]
    assert save.records[1]["sessionId"] == "session-123"
    assert save.records[1]["uuid"] == "event-1"
    assert save.records[1]["timestamp"] == "2026-10-02T10:00:01.000Z"
    saved = status(session)
    assert saved["cursor"]["messages"] == 2
    assert saved["termination_reason"] == "user_exit"
    assert saved["timestamp"] == "2026-10-02T11:00:00.000Z"
    assert "q" * 500 not in json.dumps(saved)


def test_exit_retry_is_noop_but_assistant_only_tail_is_flushed(session):
    write_session(session, [("user", "q"), ("assistant", "a")])
    save = SuccessfulSweep()
    invoke(exit_payload(), save)
    assert invoke(exit_payload(), save)["outcome"] == "skipped"
    write_session(session, [("user", "q"), ("assistant", "a"), ("assistant", "tail")])
    assert invoke(exit_payload(reason="complete"), save)["outcome"] == "saved"
    assert [r["message"]["content"] for r in save.records] == ["q", "a", "tail"]
    assert save.calls == 2


def test_stop_interval_precompact_and_exit_share_confirmed_cursor(session):
    save = SuccessfulSweep()
    messages = [("user", f"q{i}") for i in range(14)]
    write_session(session, messages)
    assert invoke(exit_payload(hook_event_name="Stop"), save)["outcome"] == "skipped"
    assert save.calls == 0
    messages.append(("user", "q14"))
    write_session(session, messages)
    assert invoke(exit_payload(hook_event_name="Stop"), save)["outcome"] == "saved"
    messages.append(("assistant", "precompact tail"))
    write_session(session, messages)
    assert invoke(exit_payload(hook_event_name="PreCompact"), save)["outcome"] == "saved"
    assert invoke(exit_payload(), save)["outcome"] == "skipped"
    assert len(save.records) == 16


@pytest.mark.parametrize("reason", ["complete", "error", "abort", "timeout", "user_exit"])
def test_exit_reasons_are_observations_not_success_feedback(session, reason):
    write_session(session, [("assistant", "unfinished")])
    result = invoke(exit_payload(reason=reason), SuccessfulSweep())
    assert result["outcome"] == "saved"
    saved = status(session)
    assert saved["termination_reason"] == reason
    assert "task_success" not in saved and "helpful" not in saved


def test_empty_transcript_is_explicit_skip(session):
    write_session(session, [])
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["code"] == "empty"
    assert save.calls == 0


@pytest.mark.parametrize("session_id", ["../other", "a/b", "a\\b", ".", "", "a" * 200])
def test_unsafe_session_id_never_reads_or_saves_other_sessions(session, session_id):
    write_session(session, [("user", "private")])
    save = SuccessfulSweep()
    assert invoke(exit_payload(session_id=session_id), save)["outcome"] == "failed"
    assert save.calls == 0


def test_explicit_legacy_path_requires_matching_session_identity(session, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    path = write_session(legacy, [("assistant", "legacy tail")])
    save = SuccessfulSweep()
    assert invoke(exit_payload(transcript_path=str(path)), save)["outcome"] == "saved"
    write_session(legacy, [("user", "other session")], session_id="another-session")
    assert invoke(exit_payload(transcript_path=str(path)), save)["outcome"] == "failed"
    assert save.calls == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "parent-symlink", "fifo"])
def test_transcript_must_be_a_real_regular_file(session, kind):
    path = session / "events.jsonl"
    if kind == "directory":
        path.mkdir()
    elif kind in ("symlink", "parent-symlink"):
        actual = write_session(session, [("user", "private")])
        other = session / "other.jsonl"
        actual.rename(other)
        path.symlink_to(other)
        if kind == "parent-symlink":
            path.unlink()
            other.rename(path)
            alias = session.parent / "alias"
            alias.symlink_to(session, target_is_directory=True)
            path = alias / "events.jsonl"
    elif kind == "fifo":
        os.mkfifo(path)
    save = SuccessfulSweep()
    assert invoke(exit_payload(transcript_path=str(path)), save)["outcome"] == "failed"
    assert save.calls == 0


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "empty-receipt", "incomplete-receipt"])
def test_unknown_save_outcomes_do_not_advance_or_blindly_retry(session, failure):
    write_session(session, [("user", "private content")])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 25, stderr="private content")
        return subprocess.CompletedProcess(
            args, 1 if failure == "nonzero" else 0,
            json.dumps({"protocol": 1, "outcome": "saved", "messages": 0})
            if failure == "incomplete-receipt" else "",
            "private content" if failure == "nonzero" else "",
        )

    assert invoke(exit_payload(), run)["outcome"] == "unknown"
    saved = status(session)
    assert saved["cursor"]["messages"] == 0
    assert saved["pending"] is not None
    result = invoke(exit_payload(), run)
    assert result["code"] == "unsettled_previous_attempt"
    assert len(calls) == 1
    assert "private content" not in json.dumps(saved) + json.dumps(result)


def test_spawn_failure_is_explicit_and_retryable(session):
    write_session(session, [("assistant", "tail")])

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing launcher")

    assert invoke(exit_payload(), missing)["outcome"] == "failed"
    assert status(session)["cursor"]["messages"] == 0
    assert invoke(exit_payload(), SuccessfulSweep())["outcome"] == "saved"


def test_save_overlap_does_not_launch_second_writer(session):
    write_session(session, [("user", "q")])
    save = SuccessfulSweep()
    nested = []

    def overlap(args, **kwargs):
        nested.append(invoke(exit_payload(hook_event_name="Stop"), save))
        return save(args, **kwargs)

    assert invoke(exit_payload(), overlap)["outcome"] == "saved"
    assert nested[0]["code"] == "save_in_progress"
    assert save.calls == 1


def test_changed_acknowledged_prefix_is_not_silently_lost(session):
    write_session(session, [("user", "original")])
    save = SuccessfulSweep()
    invoke(exit_payload(), save)
    write_session(session, [("user", "changed"), ("assistant", "tail")])
    assert invoke(exit_payload(), save)["code"] == "transcript_rewritten"
    assert save.calls == 1


def test_malformed_message_identity_and_timestamp_are_not_silently_skipped(session):
    path = write_session(session, [("user", "q")])
    path.write_text(path.read_text() + '{"type":"assistant.message","data":{"content":"tail"}}\n')
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "failed"
    assert save.calls == 0


def test_disabled_auto_save_is_respected(session, monkeypatch, installed_storage):
    write_session(session, [("user", "q")])
    monkeypatch.setenv("MEMPALACE_HOOKS_AUTO_SAVE", "false")
    assert invoke(exit_payload(), run_capture_worker)["code"] == "auto_save_disabled"
    assert installed_storage[0].count() == 0


def test_config_opt_out_is_respected_without_environment_override(session, monkeypatch, installed_storage):
    write_session(session, [("user", "q")])
    config = Path(os.environ["MEMPALACE_CONFIG_DIR"])
    config.mkdir(exist_ok=True)
    (config / "config.json").write_text('{"hooks":{"auto_save":false}}')
    assert invoke(exit_payload(), run_capture_worker)["code"] == "auto_save_disabled"
    assert installed_storage[0].count() == 0


def test_missing_transcript_failure_is_persisted_without_claiming_saved(session):
    result = invoke(exit_payload(), SuccessfulSweep())
    assert result["outcome"] == "failed"
    assert status(session)["outcome"] == "failed"
    assert status(session)["cursor"]["messages"] == 0


@pytest.mark.parametrize("reason", [None, [], {}, "success"])
def test_invalid_exit_reason_is_a_nonblocking_diagnostic(session, reason):
    write_session(session, [("user", "q")])
    save = SuccessfulSweep()
    assert invoke(exit_payload(reason=reason), save)["outcome"] == "failed"
    assert save.calls == 0


def test_default_copilot_home_resolves_only_named_session(session, monkeypatch):
    write_session(session, [("assistant", "default home")])
    monkeypatch.delenv("COPILOT_HOME")
    monkeypatch.setattr(ct.Path, "home", lambda: session.parents[2])
    default_home = session.parents[2] / ".copilot"
    session.parents[1].rename(default_home)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "saved"
    assert save.records[0]["message"]["content"] == "default home"


def test_empty_copilot_home_uses_default_session_tree(session, monkeypatch, tmp_path):
    write_session(session, [("assistant", "empty home tail")])
    monkeypatch.setenv("COPILOT_HOME", "")
    monkeypatch.setattr(ct.Path, "home", lambda: session.parents[2])
    session.parents[1].rename(session.parents[2] / ".copilot")
    monkeypatch.chdir(tmp_path)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "saved"
    assert save.records[0]["message"]["content"] == "empty home tail"
    assert not (tmp_path / "session-state").exists()


def test_zero_exit_with_empty_json_is_not_a_save_receipt(session):
    write_session(session, [("assistant", "tail")])
    result = invoke(
        exit_payload(), lambda args, **kw: subprocess.CompletedProcess(args, 0, "{}", ""),
    )
    assert result["outcome"] == "unknown"
    assert status(session)["cursor"]["messages"] == 0


def test_corrupt_state_is_not_reset_or_overwritten(session):
    write_session(session, [("assistant", "tail")])
    directory = session / "mempalace-save"
    directory.mkdir()
    path = directory / "status.json"
    path.write_text("broken")
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "failed"
    assert path.read_text() == "broken"
    assert save.calls == 0


def test_out_of_order_timestamps_are_preserved_without_a_time_cursor(session):
    path = write_session(session, [("user", "q"), ("assistant", "a")])
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[-1]["timestamp"] = "2026-09-01T00:00:00.000Z"
    path.write_text("\n".join(map(json.dumps, records)) + "\n")
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "saved"
    assert save.records[-1]["timestamp"] == "2026-09-01T00:00:00.000Z"


def test_more_than_upstream_summary_window_is_captured_without_truncation(session):
    messages = [("user" if i % 2 == 0 else "assistant", f"{i}:" + "x" * 1000) for i in range(80)]
    write_session(session, messages)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["captured_messages"] == 80
    assert [r["message"]["content"] for r in save.records] == [content for _, content in messages]


def test_stdout_json_and_stderr_diagnostic_for_malformed_payload():
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(ct.sys, "stdin", io.StringIO("not json")), \
         patch.object(ct.sys, "stdout", stdout), patch.object(ct.sys, "stderr", stderr):
        assert ct.main() == 0
    assert json.loads(stdout.getvalue()) == {}
    assert json.loads(stderr.getvalue())["outcome"] == "failed"


def test_explicit_missing_launcher_does_not_fall_back_to_another_install(monkeypatch):
    monkeypatch.setenv("MEMPALACE_PYTHON", "/missing/python")
    assert ct._mempalace_python() is None


def test_missing_explicit_interpreter_is_a_retryable_failure(session, monkeypatch):
    write_session(session, [("assistant", "tail")])
    monkeypatch.delenv("MEMPALACE_PYTHON")
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["code"] == "mempalace_python_unavailable"
    assert save.calls == 0
    assert status(session)["pending"] is None


@pytest.mark.parametrize("name", ["save.lock", "status.json", "status.new", "pending.jsonl"])
def test_state_symlinks_cannot_overwrite_unrelated_files(session, name):
    write_session(session, [("user", "q")])
    directory = session / "mempalace-save"
    directory.mkdir()
    target = session / "unrelated"
    target.write_text("unchanged")
    (directory / name).symlink_to(target)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "failed"
    assert target.read_text() == "unchanged"
    assert save.calls == 0


def test_nonzero_save_exposes_returncode_without_private_stderr(session):
    write_session(session, [("user", "private")])
    result = invoke(
        exit_payload(),
        lambda args, **kwargs: subprocess.CompletedProcess(args, 7, "", "private failure detail"),
    )
    assert result["outcome"] == "unknown"
    assert result["returncode"] == 7
    assert result["stderr_present"] is True
    assert "private" not in json.dumps(result)


def test_nul_transcript_path_is_a_nonblocking_failure(session):
    assert invoke(exit_payload(transcript_path="bad\0path"), SuccessfulSweep())["outcome"] == "failed"


def test_unrecognized_non_string_event_type_is_ignored(session):
    path = write_session(session, [("assistant", "tail")])
    path.write_text(path.read_text() + '{"type":[],"data":{}}\n')
    assert invoke(exit_payload(), SuccessfulSweep())["outcome"] == "saved"


def test_exit_waits_for_overlapping_stop_then_captures_new_tail(session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    import threading

    messages = [("user", f"q{i}") for i in range(15)]
    write_session(session, messages)
    started, finish_stop = threading.Event(), threading.Event()
    save = SuccessfulSweep()

    def slow_stop(args, **kwargs):
        if not started.is_set():
            started.set()
            assert finish_stop.wait(3)
        return save(args, **kwargs)

    monkeypatch.setattr(ct.subprocess, "run", slow_stop)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stop = pool.submit(ct.capture, exit_payload(hook_event_name="Stop"))
        assert started.wait(3)
        write_session(session, messages + [("assistant", "last tail")])
        final = pool.submit(ct.capture, exit_payload())
        try:
            # The exit must not return a false no-op while the older snapshot saves.
            with pytest.raises(FutureTimeout):
                final.result(timeout=0.1)
        finally:
            finish_stop.set()
        assert stop.result(timeout=3)["outcome"] == "saved"
        assert final.result(timeout=3)["outcome"] == "saved"
    assert len(save.records) == 16
    assert save.records[-1]["message"]["content"] == "last tail"


def test_lock_timeout_is_explicit_and_does_not_launch_another_writer(session, monkeypatch):
    import fcntl

    write_session(session, [("assistant", "tail")])
    directory = session / "mempalace-save"
    directory.mkdir()
    save = SuccessfulSweep()
    monkeypatch.setattr(ct, "HOOK_BUDGET", 0.01)
    with (directory / "save.lock").open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        result = invoke(exit_payload(), save)
    assert result["outcome"] == "failed"
    assert result["code"] == "save_lock_timeout"
    assert save.calls == 0


@pytest.fixture
def installed_storage(session, monkeypatch):
    """Real stdio admission and opener; substitute only the embedding provider."""
    if not _HAS_MEMPALACE:
        pytest.skip("requires provisioned MemPalace")
    import sys
    from mempalace.backends import embedding_wrapper

    palace = session / "isolated-palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    for name in (
        "MEMPALACE_HOOK_WRITE_ROUTING", "MEMPALACE_CLI_WRITE_ROUTING",
        "MEMPALACE_WRITE_ROUTING", "MEMPALACE_HOOKS_DAEMON",
    ):
        monkeypatch.delenv(name, raising=False)
    saved_stdout, saved_fd = sys.stdout, os.dup(1)
    try:
        from mempalace import mcp_server
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        sys.stdout = saved_stdout
    mcp_server._release_mcp_writer_lock()
    mcp_server._apply_server_flags(palace=str(palace), backend="sqlite_exact")
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])

    class StorageReads:
        def count(self):
            collection = mcp_server._get_collection()
            return collection.count() if collection is not None else 0

        def query(self, **kwargs):
            collection = mcp_server._get_collection()
            assert collection is not None
            return collection.query(**kwargs)

    yield StorageReads(), mcp_server
    mcp_server._release_mcp_writer_lock()
    mcp_server._discard_mcp_storage_handles()


def worker_request(session, messages):
    path = write_session(session, messages)
    records = [json.loads(line) for line in ct.translate_events(path.read_text().splitlines(), "/repo")]
    for record in records:
        record["sessionId"] = "session-123"
    snapshot = session / "worker-input.jsonl"
    snapshot.write_text("\n".join(map(json.dumps, records)) + "\n")
    return {
        "protocol": 1, "session_id": "session-123", "snapshot": str(snapshot),
        "source_file": str(path), "sha256": ct._digest(records), "timeout": 20,
        "project_cwd": "/repo",
    }


def run_capture_worker(args, **kwargs):
    import copilot_capture as worker

    assert args[0] == os.environ["MEMPALACE_PYTHON"]
    assert Path(args[1]).name == "copilot_capture.py"
    result = worker.capture(json.loads(kwargs["input"]))
    return subprocess.CompletedProcess(args, 0, json.dumps(result), "")


def wing_query(collection, wing="wing_repo"):
    return collection.query(
        query_texts=["original observation"], n_results=1000,
        where={"$and": [{"wing": wing}, {"room": "diary"}]},
        include=["documents", "metadatas"],
    )


def test_supported_capture_preserves_actual_project_wing_retrieval(session, installed_storage):
    import copilot_capture as worker

    collection, _ = installed_storage
    request = worker_request(session, [("user", "original question"), ("assistant", "original answer")])
    result = worker.capture(request)
    assert result["outcome"] == "saved"
    assert result["wing"] == "wing_repo" and result["room"] == "diary"
    originals = [json.loads(document) for document in wing_query(collection).documents[0]]
    assert {item["content"] for item in originals} == {"original question", "original answer"}
    assert {item["event_id"] for item in originals} == {"event-0", "event-1"}
    assert {item["session_id"] for item in originals} == {"session-123"}
    assert originals[0]["timestamp"].startswith("2026-10-02T10:00:")
    assert wing_query(collection, "unrelated_project").documents == [[]]
    again = worker.capture(request)
    assert again["drawer_ids"] == result["drawer_ids"]
    assert collection.count() == 2


@pytest.mark.parametrize("hooks,cli,expected", [
    ("require", "direct", "failed"), ("require", None, "failed"),
    ("direct", "prefer", "saved"), ("prefer", "require", "saved"),
])
def test_actual_hook_routing_ignores_cli_policy_and_never_starts_daemon(
    session, installed_storage, monkeypatch, hooks, cli, expected,
):
    import copilot_capture as worker
    from mempalace import daemon

    collection, _ = installed_storage
    config = Path(os.environ["MEMPALACE_CONFIG_DIR"])
    config.mkdir(exist_ok=True)
    routing = {"hooks": hooks}
    if cli:
        routing["cli"] = cli
    (config / "config.json").write_text(json.dumps({"write_routing": routing}))
    monkeypatch.setattr(daemon, "get_client_if_running", lambda *args, **kwargs: None)

    def forbidden_submit(*args, **kwargs):
        pytest.fail("absent daemon must not be submitted to or auto-started")

    monkeypatch.setattr(daemon, "submit_job", forbidden_submit)
    result = worker.capture(worker_request(session, [("assistant", "retained tail")]))
    assert result["outcome"] == expected
    if expected == "failed":
        assert result["code"] == "daemon_required_unavailable"
        assert result["write_started"] is False
        assert collection.count() == 0
    else:
        assert json.loads(wing_query(collection).documents[0][0])["content"] == "retained tail"


def test_selected_daemon_is_refused_without_unsafe_raw_dispatch(
    session, installed_storage, monkeypatch,
):
    import copilot_capture as worker
    from mempalace import daemon

    collection, _ = installed_storage
    monkeypatch.setenv("MEMPALACE_HOOK_WRITE_ROUTING", "require")
    monkeypatch.setenv("MEMPALACE_CLI_WRITE_ROUTING", "direct")
    monkeypatch.setattr(daemon, "get_client_if_running", lambda *args, **kwargs: object())
    submitted = []

    def submit(kind, payload, **kwargs):
        pytest.fail("installed raw daemon service must not be used")

    monkeypatch.setattr(daemon, "submit_job", submit)
    result = worker.capture(worker_request(session, [("assistant", "daemon original")]))
    assert result["outcome"] == "failed"
    assert result["code"] == "daemon_capture_unavailable"
    assert result["write_started"] is False
    assert len(submitted) == 0 and collection.count() == 0


def test_adapter_worker_and_supported_writer_end_to_end(session, installed_storage):
    collection, server = installed_storage
    write_session(session, [("user", "q" * 700), ("assistant", "a" * 900)])
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "saved"
    query = wing_query(collection)
    logical_ids = {
        metadata.get("parent_drawer_id", identifier)
        for identifier, metadata in zip(query.ids[0], query.metadatas[0])
    }
    originals = [json.loads(server.tool_get_drawer(identifier)["content"]) for identifier in logical_ids]
    assert {item["content"] for item in originals} == {"q" * 700, "a" * 900}
    assert {item["event_id"] for item in originals} == {"event-0", "event-1"}
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "skipped"
    write_session(session, [("user", "q" * 700), ("assistant", "a" * 900), ("assistant", "tail")])
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "saved"
    later = wing_query(collection)
    assert len({
        metadata.get("parent_drawer_id", identifier)
        for identifier, metadata in zip(later.ids[0], later.metadatas[0])
    }) == 3


def test_required_daemon_failure_is_retryable_only_when_no_write_started(
    session, installed_storage, monkeypatch,
):
    from mempalace import daemon

    collection, _ = installed_storage
    write_session(session, [("assistant", "tail")])
    monkeypatch.setenv("MEMPALACE_HOOK_WRITE_ROUTING", "require")
    monkeypatch.setattr(daemon, "get_client_if_running", lambda *args, **kwargs: None)
    result = invoke(exit_payload(), run_capture_worker)
    assert result["code"] == "daemon_required_unavailable"
    assert result["outcome"] == "failed"
    assert status(session)["pending"] is None
    assert collection.count() == 0
    monkeypatch.setenv("MEMPALACE_HOOK_WRITE_ROUTING", "direct")
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "saved"
    assert collection.count() == 1


def test_unavailable_daemon_capture_never_falls_back_to_direct(
    session, installed_storage, monkeypatch,
):
    from mempalace import daemon

    collection, _ = installed_storage
    write_session(session, [("assistant", "tail")])
    monkeypatch.setenv("MEMPALACE_HOOK_WRITE_ROUTING", "require")
    monkeypatch.setattr(daemon, "get_client_if_running", lambda *args, **kwargs: object())
    calls = []

    def queued(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["auto_start"] is False
        return {"state": "queued", "id": "job-held"}

    monkeypatch.setattr(daemon, "submit_job", queued)
    assert invoke(exit_payload(), run_capture_worker)["code"] == "daemon_capture_unavailable"
    assert status(session)["pending"] is None
    assert len(calls) == 0 and collection.count() == 0


def test_partial_supported_api_write_keeps_whole_batch_unacknowledged(
    session, installed_storage, monkeypatch,
):
    collection, server = installed_storage
    write_session(session, [("user", "q"), ("assistant", "a")])
    original = server.tool_add_drawer
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs)
        return original(**kwargs) if len(calls) == 1 else {"success": False, "error": "private"}

    monkeypatch.setitem(server.TOOLS, "mempalace_add_drawer", {
        **server.TOOLS["mempalace_add_drawer"], "handler": fail_second,
    })
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "unknown"
    assert collection.count() == 1
    assert status(session)["cursor"]["messages"] == 0
    assert invoke(exit_payload(), run_capture_worker)["code"] == "unsettled_previous_attempt"
    assert len(calls) == 2


def test_worker_protocol_survives_supported_api_stdout_redirection(
    session, installed_storage, monkeypatch, capfd,
):
    import copilot_capture as worker

    _, server = installed_storage
    request = worker_request(session, [("assistant", "original")])
    original = server.tool_add_drawer
    saved_stdout, saved_fd = sys.stdout, os.dup(1)

    def redirecting_api(**kwargs):
        sys.stdout = sys.stderr
        os.dup2(2, 1)
        return original(**kwargs)

    monkeypatch.setitem(server.TOOLS, "mempalace_add_drawer", {
        **server.TOOLS["mempalace_add_drawer"], "handler": redirecting_api,
    })
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(request)))
    try:
        assert worker.main() == 0
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        sys.stdout = saved_stdout
    captured = capfd.readouterr()
    assert json.loads(captured.out)["outcome"] == "saved"


def test_missing_companion_helper_is_explicit_and_retryable(session, monkeypatch):
    write_session(session, [("assistant", "tail")])
    monkeypatch.setattr(ct, "__file__", str(session / "copilot_transcript.py"))
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["code"] == "capture_worker_unavailable"
    assert status(session)["pending"] is None
    assert save.calls == 0


@pytest.mark.parametrize("callback_cwd", [None, ""])
def test_session_start_context_supplies_missing_callback_project(session, callback_cwd):
    write_session(session, [("assistant", "tail")], project_cwd="/authoritative-project")
    save = SuccessfulSweep()
    assert invoke(exit_payload(cwd=callback_cwd), save)["outcome"] == "saved"
    assert save.records[0]["cwd"] == "/authoritative-project"
    assert status(session)["project_cwd"] == "/authoritative-project"


def test_callback_project_conflict_is_refused_before_capture(session):
    write_session(session, [("assistant", "tail")], project_cwd="/authoritative-project")
    save = SuccessfulSweep()
    assert invoke(exit_payload(cwd="/wrong-project"), save)["code"] == "project_context_conflict"
    assert save.calls == 0


def test_destination_cannot_change_after_resume(session):
    messages = [("user", "original")]
    write_session(session, messages)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "saved"
    write_session(session, messages + [("assistant", "tail")], project_cwd="/different-project")
    assert invoke(exit_payload(cwd="/different-project"), save)["code"] == "project_context_changed"
    assert save.calls == 1


def test_true_legacy_context_requires_explicit_absolute_callback_and_binds_it(session):
    write_session(session, [("assistant", "legacy")], project_cwd=None)
    save = SuccessfulSweep()
    assert invoke(exit_payload(cwd=""), save)["code"] == "missing_project_context"
    assert save.calls == 0
    assert invoke(exit_payload(cwd="/legacy-project"), save)["outcome"] == "saved"
    assert status(session)["project_cwd"] == "/legacy-project"


@pytest.mark.parametrize("project_cwd", ["", "/", "relative/project"])
def test_invalid_authoritative_project_never_falls_back_to_callback(session, project_cwd):
    write_session(session, [("assistant", "tail")], project_cwd=project_cwd)
    save = SuccessfulSweep()
    assert invoke(exit_payload(), save)["outcome"] == "failed"
    assert save.calls == 0


def test_real_worker_process_resolves_actual_opt_out_without_writing(session, monkeypatch):
    request = worker_request(session, [("assistant", "original")])
    monkeypatch.setenv("MEMPALACE_HOOKS_AUTO_SAVE", "false")
    result = subprocess.run(
        [sys.executable, str(Path(ct.__file__).with_name("copilot_capture.py"))],
        input=json.dumps(request), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert receipt["outcome"] == "skipped"
    assert receipt["code"] == "auto_save_disabled" and receipt["write_started"] is False


def test_unprovisioned_worker_fails_before_any_write(session, monkeypatch):
    import builtins
    import copilot_capture as worker

    original = builtins.__import__

    def missing_api(name, *args, **kwargs):
        if name == "mempalace.config":
            raise ImportError("not provisioned")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_api)
    result = worker.capture(worker_request(session, [("assistant", "tail")]))
    assert result["outcome"] == "failed"
    assert result["code"] == "mempalace_api_unavailable"
    assert result["write_started"] is False


def test_invalid_hook_policy_does_not_fall_back_to_direct(session, installed_storage):
    import copilot_capture as worker

    config = Path(os.environ["MEMPALACE_CONFIG_DIR"])
    config.mkdir(exist_ok=True)
    (config / "config.json").write_text('{"write_routing":{"hooks":"invalid","cli":"direct"}}')
    result = worker.capture(worker_request(session, [("assistant", "tail")]))
    assert result["outcome"] == "failed"
    assert result["code"] == "hooks_routing_invalid" and result["write_started"] is False
    assert installed_storage[0].count() == 0


@pytest.mark.parametrize(
    "original", ["x" * 120001, "漢字" * 20000, "\x00\n\\" * 18000],
    ids=["ascii-long", "cjk-long", "escaped-long"],
)
def test_real_writer_reconstructs_lossless_bounded_observation_parts(
    session, installed_storage, original,
):
    import hashlib
    import copilot_capture as worker

    collection, server = installed_storage
    request = worker_request(session, [("assistant", original)])
    result = worker.capture(request)
    assert result["outcome"] == "saved"
    query = wing_query(collection)
    logical_ids = {
        meta.get("parent_drawer_id", identifier)
        for identifier, meta in zip(query.ids[0], query.metadatas[0])
    }
    envelopes = [server.tool_get_drawer(identifier)["content"] for identifier in logical_ids]
    assert all(len(envelope) <= 100000 for envelope in envelopes)
    parts = sorted((json.loads(text) for text in envelopes), key=lambda part: part["part_index"])
    assert len(parts) > 1
    assert {part["part_count"] for part in parts} == {len(parts)}
    assert {part["event_id"] for part in parts} == {"event-0"}
    assert {part["content_sha256"] for part in parts} == {hashlib.sha256(original.encode()).hexdigest()}
    assert "".join(part["content"] for part in parts) == original
    count = collection.count()
    assert worker.capture(request)["drawer_ids"] == result["drawer_ids"]
    assert collection.count() == count


def test_all_parts_are_preflighted_before_any_writer_call(session, installed_storage):
    import copilot_capture as worker

    collection, _ = installed_storage
    request = worker_request(session, [("user", "valid first observation"), ("assistant", "later")])
    snapshot = Path(request["snapshot"])
    records = [json.loads(line) for line in snapshot.read_text().splitlines()]
    records[-1]["timestamp"] = "2026-10-02T10:00:00." + "1" * 110000 + "Z"
    snapshot.write_text("\n".join(map(json.dumps, records)) + "\n")
    request["sha256"] = ct._digest(records)
    result = worker.capture(request)
    assert result["outcome"] == "failed"
    assert result["code"] == "observation_preflight_failed"
    assert result["write_started"] is False
    assert collection.count() == 0
    assert not (session / "isolated-palace" / "sqlite_exact.sqlite3").exists()
    source = Path(request["source_file"])
    events = [json.loads(line) for line in source.read_text().splitlines()]
    events[-1]["timestamp"] = records[-1]["timestamp"]
    source.write_text("\n".join(map(json.dumps, events)) + "\n")
    result = invoke(exit_payload(), run_capture_worker)
    assert result["code"] == "observation_preflight_failed"
    assert status(session)["pending"] is None
    assert status(session)["cursor"]["messages"] == 0


def test_adapter_acknowledges_original_messages_not_part_count(session, installed_storage):
    write_session(session, [("assistant", "漢字" * 20000)])
    result = invoke(exit_payload(), run_capture_worker)
    assert result["outcome"] == "saved" and result["captured_messages"] == 1
    assert status(session)["receipt"]["parts"] > 1
    assert status(session)["cursor"]["messages"] == 1
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "skipped"


def _capture_in_fresh_process(request, connection):
    from mempalace.backends import embedding_wrapper
    import copilot_capture as worker

    embedding_wrapper._embed_texts = lambda texts: [[1.0, 0.0] for _ in texts]
    protocol_path = Path(request["snapshot"]).with_suffix(".receipt.json")
    sys.stdin = io.StringIO(json.dumps(request))
    with protocol_path.open("w") as output:
        os.dup2(output.fileno(), 1)
        assert worker.main() == 0
    result = json.loads(protocol_path.read_text())
    from mempalace import mcp_server

    collection = mcp_server._get_collection()
    query = collection.query(
        query_texts=["fresh original"], n_results=10,
        where={"$and": [{"wing": "wing_repo"}, {"room": "diary"}]},
        include=["documents", "metadatas"],
    )
    connection.send({
        "receipt": result, "documents": query.documents[0],
        "writer_admitted": mcp_server._MCP_WRITER_LOCK_CM is not None,
    })
    connection.close()


def test_fresh_stdio_process_uses_real_writer_admission_and_opener(session, installed_storage):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    request = worker_request(session, [("assistant", "fresh original")])
    process = context.Process(target=_capture_in_fresh_process, args=(request, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "fresh worker produced no receipt"
        evidence = parent.recv()
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert evidence["receipt"]["outcome"] == "saved"
    assert evidence["writer_admitted"] is True
    assert json.loads(evidence["documents"][0])["content"] == "fresh original"
    assert (session / "isolated-palace" / "sqlite_exact.sqlite3").is_file()


def _hold_real_palace_lock(palace, ready, release):
    from mempalace.palace import mine_palace_lock

    with mine_palace_lock(palace):
        ready.set()
        assert release.wait(10)


def test_real_peer_writer_is_not_bypassed_and_refusal_is_retryable(
    session, installed_storage,
):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_real_palace_lock,
        args=(str(session / "isolated-palace"), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5)
        write_session(session, [("assistant", "tail")])
        result = invoke(exit_payload(), run_capture_worker)
        assert result["outcome"] == "failed"
        assert result["code"] == "writer_unavailable"
        assert status(session)["pending"] is None
        assert not (session / "isolated-palace" / "sqlite_exact.sqlite3").exists()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert invoke(exit_payload(), run_capture_worker)["outcome"] == "saved"
    assert installed_storage[0].count() == 1


def test_real_project_query_uses_header_when_callback_context_is_absent(
    session, installed_storage,
):
    collection, _ = installed_storage
    write_session(session, [("assistant", "actual project original")], project_cwd="/my-actual-repo")
    assert invoke(exit_payload(cwd=None), run_capture_worker)["outcome"] == "saved"
    query = wing_query(collection, "wing_my_actual_repo")
    assert json.loads(query.documents[0][0])["content"] == "actual project original"
    assert wing_query(collection, "wing_repo").documents == [[]]


def test_real_read_only_runtime_refusal_does_not_leave_unknown_intent(session, installed_storage):
    _, server = installed_storage
    server._apply_server_flags(
        palace=str(session / "isolated-palace"), backend="sqlite_exact", read_only=True,
    )
    write_session(session, [("assistant", "must not write")])
    result = invoke(exit_payload(), run_capture_worker)
    assert result["outcome"] == "failed"
    assert result["code"] == "capture_read_only"
    assert status(session)["pending"] is None
    assert not (session / "isolated-palace" / "sqlite_exact.sqlite3").exists()


@unittest.skipUnless(_HAS_MEMPALACE, "requires the mempalace interpreter")
class MempalaceParserIntegrationTests(unittest.TestCase):
    """The translated output must be parseable by mempalace's claude-code path."""

    def _write(self, lines: list[str]) -> str:
        import tempfile

        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write("\n".join(lines) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_mempalace_counts_translated_human_messages(self):
        lines = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": "q1"}},
                {"type": "assistant.message", "data": {"content": "a1"}},
                {"type": "user.message", "data": {"content": "q2"}},
            ),
            cwd="/repo",
        )
        path = self._write(lines)
        self.assertEqual(_h._count_human_messages(path), 2)

    def test_mempalace_extracts_translated_messages(self):
        lines = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "remember this"}}),
            cwd="/repo",
        )
        path = self._write(lines)
        self.assertEqual(_h._extract_recent_messages(path), ["remember this"])

    def test_mempalace_derives_wing_from_translated_cwd(self):
        lines = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "x"}}),
            cwd="/home/e/copilot-mempalace",
        )
        path = self._write(lines)
        # mempalace slugifies the cwd leaf, replacing '-' with '_'.
        self.assertEqual(_h._wing_from_jsonl_cwd(path), "wing_copilot_mempalace")


if __name__ == "__main__":
    unittest.main()
