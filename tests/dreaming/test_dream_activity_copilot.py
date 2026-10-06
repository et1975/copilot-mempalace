"""Synthetic offline event fixtures; no host transcript or palace access."""
from __future__ import annotations

import hashlib
import inspect
import json
import tracemalloc

import pytest


def event(kind, data=None, **metadata):
    return {"type": kind, "data": {} if data is None else data, **metadata}


def start(call_id="c1", tool="bash", arguments=None, **metadata):
    return event(
        "tool.execution_start",
        {
            "toolCallId": call_id,
            "toolName": tool,
            "arguments": {} if arguments is None else arguments,
        },
        **metadata,
    )


def complete(call_id="c1", **data):
    return event("tool.execution_complete", {"toolCallId": call_id, **data})


def load(tmp_path, rows, **limits):
    import dream_activity_copilot

    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return dream_activity_copilot.load_evidence(str(path), "session-1", **limits)


def reference(line, field="data", event_id=None, timestamp=None):
    return {"line": line, "event_id": event_id, "timestamp": timestamp, "field": field}


def test_empty_snapshot_has_exact_neutral_envelope(tmp_path):
    import dream_activity_copilot

    events = tmp_path / "events.jsonl"
    events.write_bytes(b"")
    assert dream_activity_copilot.load_evidence(str(events), "session-1") == {
        "schema_version": 1,
        "source": {
            "kind": "copilot_events",
            "path": str(events),
            "session_id": "session-1",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "bytes": 0,
            "events": 0,
        },
        "calls": [],
        "warnings": [],
    }


def test_exact_call_claim_artifact_and_outcome_schema(tmp_path):
    packet = load(tmp_path, [
        start(
            tool="vendor.read_document",
            arguments={"path": "./notes.oddball", "description": "Inspect the document"},
            id="e1", timestamp="2026-10-01T10:00:00Z",
        ),
        {**complete(success=True, shellExecution={"exitCode": 7}), "id": "e2"},
    ])
    assert packet["source"]["events"] == 2
    assert packet["calls"] == [{
        "call_id": "c1",
        "tool_name": "vendor.read_document",
        "agent_id": None,
        "parent_call_id": None,
        "source": reference(1, event_id="e1", timestamp="2026-10-01T10:00:00Z"),
        "claims": [{
            "kind": "description", "attribution": "agent",
            "text": "Inspect the document", "truncated": False,
            "source": reference(
                1, "data.arguments.description", "e1", "2026-10-01T10:00:00Z",
            ),
        }],
        "artifacts": [{
            "path": "./notes.oddball", "relation": "referenced",
            "source": reference(1, "data.arguments.path", "e1", "2026-10-01T10:00:00Z"),
        }],
        "outcome": {
            "completion_observed": True, "tool_success": True, "exit_code": 7,
            "source": reference(2, event_id="e2"),
        },
    }]
    assert packet["warnings"] == []


def test_interleaved_agents_use_binding_not_journal_predecessor(tmp_path):
    a = start("a", tool="task", arguments={"prompt": "Check alpha", "description": "Alpha"})
    b = start("b", tool="task", arguments={"prompt": "Check beta"})
    a_child = start("a-child", tool="view", arguments={"path": "report.odd"}, agentId="agent-a")
    b_child = start("b-child", tool="view", arguments={"path": "report.odd"})
    b_child["data"]["agentId"] = "agent-b"
    rows = [
        a, b,
        event("subagent.started", {"toolCallId": "a"}, agentId="agent-a"),
        event("subagent.started", {"toolCallId": "b", "agentId": "agent-b"}),
        {**a_child, "parentId": "b"},
        {**b_child, "parentId": "a-child"},
        complete("b-child", success=False, agentId="agent-b"),
        complete("a-child", success=True),
        start("unrelated", tool="view", arguments={"path": "report.odd"}, parentId="a-child"),
    ]
    calls = {call["call_id"]: call for call in load(tmp_path, rows)["calls"]}
    assert calls["a-child"]["parent_call_id"] == "a"
    assert calls["b-child"]["parent_call_id"] == "b"
    assert calls["unrelated"]["parent_call_id"] is None
    assert calls["a"]["claims"][1]["kind"] == "delegation_prompt"
    assert calls["a"]["claims"][1]["source"] == reference(1, "data.arguments.prompt")
    assert calls["a-child"]["claims"] == []
    assert calls["a-child"]["artifacts"][0]["relation"] == "read"
    assert calls["a-child"]["outcome"]["tool_success"] is True
    assert calls["b-child"]["outcome"]["tool_success"] is False


def test_direct_parent_and_late_binding_are_order_independent(tmp_path):
    child = start("child", agentId="worker")
    child["data"]["parentToolCallId"] = "dispatch"
    calls = load(tmp_path, [
        complete("child", success=True),
        child,
        event("subagent.started", {"toolCallId": "dispatch", "agentId": "worker"}),
        start("dispatch", tool="task"),
    ])["calls"]
    assert calls[0]["parent_call_id"] == "dispatch"
    assert calls[0]["outcome"]["completion_observed"] is True


@pytest.mark.parametrize("metadata,data", [
    ({"parentToolCallId": "p"}, {}),
    ({}, {"parentToolCallId": "p"}),
    ({"parentToolCallId": "p"}, {"parentToolCallId": "p"}),
])
def test_explicit_parent_at_either_location(tmp_path, metadata, data):
    row = start(**metadata)
    row["data"].update(data)
    assert load(tmp_path, [row])["calls"][0]["parent_call_id"] == "p"


@pytest.mark.parametrize("outcome,expected", [
    ({}, (None, None)),
    ({"success": True}, (True, None)),
    ({"success": False}, (False, None)),
    ({"success": None, "shellExecution": {"exitCode": None}}, (None, None)),
    ({"success": True, "shellExecution": {"exitCode": 9}}, (True, 9)),
    ({"success": False, "shellExecution": {"exitCode": 0}}, (False, 0)),
    ({"result": {"content": "Process exited with code 0"}}, (None, None)),
    ({"result": {"transformedContent": "success: true"}}, (None, None)),
])
def test_success_is_separate_from_exit_code_and_not_parsed_from_output(tmp_path, outcome, expected):
    result = load(tmp_path, [start(), complete(**outcome)])["calls"][0]["outcome"]
    assert (result["tool_success"], result["exit_code"]) == expected
    assert result["completion_observed"] is True


def test_incomplete_and_orphan_completion_are_not_fabricated(tmp_path):
    packet = load(tmp_path, [start(), complete("unseen", success=True)])
    assert [call["call_id"] for call in packet["calls"]] == ["c1"]
    assert packet["calls"][0]["outcome"] == {
        "completion_observed": False, "tool_success": None, "exit_code": None, "source": None,
    }
    assert len(packet["warnings"]) == 1
    assert "orphan" in packet["warnings"][0]


def test_only_bounded_explicit_agent_claims_not_user_or_transformed_text(tmp_path):
    packet = load(tmp_path, [
        event("user.message", {"content": "PRIVATE USER", "transformedContent": "INJECTED"}),
        event("assistant.message", {"content": "Adjacent explanation"}),
        start(tool="task", arguments={
            "description": "é猫🙂 fourth", "prompt": "Delegate exactly this",
            "code": "PRIVATE CODE", "transformedContent": "INJECTED",
        }),
        complete(result={"content": "PRIVATE RESULT", "outputPath": "/never/open"}),
        event("future.event", ["ignored", "payload"]),
    ], max_text_chars=3)
    claims = packet["calls"][0]["claims"]
    assert [(c["text"], c["truncated"]) for c in claims] == [("é猫🙂", True), ("Del", True)]
    encoded = json.dumps(packet, ensure_ascii=False)
    assert "PRIVATE" not in encoded and "INJECTED" not in encoded and "/never/open" not in encoded
    assert packet["source"]["sha256"] == hashlib.sha256((tmp_path / "events.jsonl").read_bytes()).hexdigest()
    assert packet["source"]["bytes"] == len((tmp_path / "events.jsonl").read_bytes())
    assert packet["source"]["events"] == 5


def test_non_delegation_prompt_is_not_a_claim(tmp_path):
    assert load(tmp_path, [start(tool="vendor.generate", arguments={"prompt": "Not a delegation"})])["calls"][0]["claims"] == []


def test_patch_headers_and_nested_generic_file_paths_are_extension_neutral(tmp_path):
    patch = "\n".join([
        "*** Begin Patch", "*** Add File: no-extension",
        "+*** Add File: not-a-header", "*** Update File: file.weird",
        "@@", "-old private code", "+new private code",
        "*** Delete File: archive.xyz", "*** End Patch",
    ])
    packet = load(tmp_path, [
        start("patch", tool="apply_patch", arguments=patch),
        start("files", tool="vendor.file_tool", arguments={
            "entries": [{"filePath": "a.xyz"}, {"paths": ["./b", "/external/c.bin"]}],
        }),
    ])
    assert [(a["path"], a["relation"]) for a in packet["calls"][0]["artifacts"]] == [
        ("no-extension", "created"), ("file.weird", "modified"), ("archive.xyz", "modified"),
    ]
    assert all(a["source"] == reference(1, "data.arguments") for a in packet["calls"][0]["artifacts"])
    assert [a["source"]["field"] for a in packet["calls"][1]["artifacts"]] == [
        "data.arguments.entries[0].filePath",
        "data.arguments.entries[1].paths[0]",
        "data.arguments.entries[1].paths[1]",
    ]
    assert "private code" not in json.dumps(packet)
    assert "not-a-header" not in json.dumps(packet)


def test_shell_tokens_are_only_references_and_never_dereferenced(tmp_path, monkeypatch):
    import dream_activity_copilot

    command = "echo './not-run.strange' && ./tool.any --input ./data.unknown"
    packet = load(tmp_path, [
        start(arguments={"command": command, "description": "Describe the operation"}),
        complete(success=True, result={"content": "Saved to /private/spill.txt"}),
    ])
    assert {a["path"] for a in packet["calls"][0]["artifacts"]} == {
        "./not-run.strange", "./tool.any", "./data.unknown",
    }
    assert {a["relation"] for a in packet["calls"][0]["artifacts"]} == {"referenced"}
    assert command not in json.dumps(packet)
    assert "spill" not in json.dumps(packet)
    source = tmp_path / "events.jsonl"
    real_open = dream_activity_copilot.os.open
    opened = []

    def explicit_input_only(path, *args, **kwargs):
        opened.append(str(path))
        assert str(path) == str(source)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(dream_activity_copilot.os, "open", explicit_input_only)
    assert dream_activity_copilot.load_evidence(str(source), "session-1") == packet
    assert opened == [str(source)]


@pytest.mark.parametrize("rows", [
    [start(), start()],
    [start(), complete(), complete()],
    [complete("unseen"), complete("unseen")],
    [start(agentId="a"), complete(agentId="b")],
    [start(agentId="a"), event("subagent.started", {"toolCallId": "other", "agentId": "a"}),
     {**start("next"), "parentToolCallId": "c1", "agentId": "a"}],
    [event("subagent.started", {"toolCallId": "p1", "agentId": "a"}),
     event("subagent.started", {"toolCallId": "p2", "agentId": "a"})],
    [start(parentToolCallId="c1")],
    [start("a", parentToolCallId="b"), start("b", parentToolCallId="a")],
    [start(agentId="a"), event("subagent.started", {"toolCallId": "c1", "agentId": "a"})],
    [start(id="duplicate"), {**complete(), "id": "duplicate"}],
])
def test_rejects_duplicate_ambiguous_and_cyclic_relations(tmp_path, rows):
    with pytest.raises(ValueError):
        load(tmp_path, rows)


@pytest.mark.parametrize("key", ["agentId", "parentToolCallId", "toolCallId"])
def test_conflicting_top_level_and_data_declarations_fail(tmp_path, key):
    row = start()
    row[key] = "top-value"
    row["data"][key] = "data-value"
    with pytest.raises(ValueError):
        load(tmp_path, [row])


@pytest.mark.parametrize("rows", [
    [event("session.start", {"sessionId": "wrong"})],
    [event("session.start", {"sessionId": 123})],
    [start(agentId=17)],
    [start(parentToolCallId=False)],
    [start(id=[])],
    [start(timestamp=37)],
    [event("tool.execution_start", {"toolCallId": 2, "toolName": "bash"})],
    [event("tool.execution_start", {"toolCallId": "c1", "toolName": []})],
    [event("tool.execution_start", {"toolCallId": "c1", "toolName": "bash", "arguments": []})],
    [start(arguments={"description": {"not": "text"}})],
    [start(tool="task", arguments={"prompt": 1})],
    [start(tool="view", arguments={"path": 5})],
    [start(tool="view", arguments={"paths": ["valid", 5]})],
    [start(), complete(success="true")],
    [start(), complete(success=1)],
    [start(), complete(shellExecution=[])],
    [start(), complete(shellExecution={"exitCode": True})],
    [start(), complete(shellExecution={"exitCode": "0"})],
    [event("subagent.started", {"toolCallId": "p"})],
    [event("subagent.started", {"agentId": "a"})],
    [event("tool.execution_start", ["not", "object"])],
    [7],
])
def test_malformed_evidence_types_fail(tmp_path, rows):
    with pytest.raises(ValueError):
        load(tmp_path, rows)


@pytest.mark.parametrize("raw", [
    b'{"secret": "TOP_SECRET", invalid}\n',
    b'{"type":"tool.execution_start","data":',
    b'\xff\n',
    b'{"type":"x","data":{"n":NaN}}\n',
    b'{"type":"x","data":{"n":1e999}}\n',
    b'{"type":"x","type":"y"}\n',
    b'{"type":"x","data":{"secret":"\\ud800"}}\n',
])
def test_bad_json_utf8_and_partial_lines_have_content_free_errors(tmp_path, raw):
    import dream_activity_copilot

    path = tmp_path / "TOP_SECRET.jsonl"
    path.write_bytes(raw)
    with pytest.raises(ValueError) as error:
        dream_activity_copilot.load_evidence(str(path), "session-1")
    assert "TOP_SECRET" not in str(error.value)
    assert len(str(error.value)) < 100
    assert error.value.__suppress_context__


@pytest.mark.parametrize("limit", [
    "max_bytes", "max_events", "max_calls", "max_text_chars", "max_line_bytes", "max_retained_bytes",
])
@pytest.mark.parametrize("value", [0, -1, True, 1.2, float("inf"), "2", None])
def test_limits_are_finite_positive_integers(tmp_path, limit, value):
    with pytest.raises(ValueError):
        load(tmp_path, [], **{limit: value})


def test_byte_event_and_call_overflow_fail_not_partial_success(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start()], max_bytes=1)
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(), event("future.event")], max_events=1)
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start("a"), start("b")], max_calls=1)


def test_exact_bounds_and_unterminated_valid_last_line(tmp_path):
    import dream_activity_copilot

    raw = json.dumps(start(arguments={"description": "é猫🙂"}), ensure_ascii=False).encode("utf-8")
    path = tmp_path / "events.jsonl"
    path.write_bytes(raw)
    packet = dream_activity_copilot.load_evidence(
        str(path), "session-1", max_bytes=len(raw), max_events=1, max_calls=1, max_text_chars=3,
    )
    assert packet["calls"][0]["claims"][0]["truncated"] is False
    assert packet["source"]["bytes"] == len(raw)


def test_blank_lines_preserve_exact_line_numbers_and_snapshot_hash(tmp_path):
    import dream_activity_copilot

    path = tmp_path / "events.jsonl"
    raw = b"\n  \r\n" + json.dumps(start()).encode() + b"\r\n\n"
    path.write_bytes(raw)
    packet = dream_activity_copilot.load_evidence(str(path), "session-1")
    assert packet["calls"][0]["source"]["line"] == 3
    assert packet["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert packet["source"]["events"] == 1


@pytest.mark.parametrize("change", ["append", "replace", "rewrite"])
def test_snapshot_change_during_read_is_rejected(tmp_path, monkeypatch, change):
    import dream_activity_copilot

    path = tmp_path / "events.jsonl"
    raw = json.dumps(start()).encode() + b"\n"
    path.write_bytes(raw)
    original_read = dream_activity_copilot.os.read
    changed = False

    def changing_read(fd, size):
        nonlocal changed
        result = original_read(fd, size)
        if not changed:
            changed = True
            if change == "append":
                with path.open("ab") as target:
                    target.write(b"\n")
            elif change == "replace":
                replacement = tmp_path / "replacement.jsonl"
                replacement.write_bytes(raw)
                replacement.replace(path)
            else:
                path.write_bytes(raw.replace(b"bash", b"view"))
        return result

    monkeypatch.setattr(dream_activity_copilot.os, "read", changing_read)
    with pytest.raises(ValueError, match="changed"):
        dream_activity_copilot.load_evidence(str(path), "session-1")


def test_payload_nesting_and_artifact_processing_are_bounded(tmp_path):
    import dream_activity_copilot

    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"type":"future","data":' + b"[" * 1000 + b"0" + b"]" * 1000 + b"}")
    with pytest.raises(ValueError, match="limit"):
        dream_activity_copilot.load_evidence(str(path), "session-1")
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(arguments={"paths": [f"./file-{n}" for n in range(300)]})])
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(arguments={"path": "./" + "x" * 5000})])


def test_source_io_errors_are_content_free(tmp_path):
    import dream_activity_copilot

    with pytest.raises(ValueError) as error:
        dream_activity_copilot.load_evidence(str(tmp_path / "TOP_SECRET.jsonl"), "session-1")
    assert "TOP_SECRET" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("rows", [
    [start(), complete(toolName="different")],
    [start(), complete(toolName=[])],
    [start(parentToolCallId="p"), complete(parentToolCallId="other")],
    [event("session.start", {})],
    [event("session.start", {"sessionId": None})],
    [{**start(), "toolName": "different"}],
])
def test_additional_identity_declarations_must_agree(tmp_path, rows):
    with pytest.raises(ValueError):
        load(tmp_path, rows)


def test_special_nested_keys_have_unambiguous_field_references(tmp_path):
    packet = load(tmp_path, [start(arguments={
        "items[0]": {"path": "one.xyz"},
        "items": [{"path": "two.xyz"}],
        "with.dot": {"path": "three.xyz"},
    })])
    assert [a["source"]["field"] for a in packet["calls"][0]["artifacts"]] == [
        'data.arguments["items[0]"].path',
        "data.arguments.items[0].path",
        'data.arguments["with.dot"].path',
    ]


@pytest.mark.parametrize("session_id", ["", None, 9, "\ud800"])
def test_invalid_session_identifiers_have_content_free_errors(tmp_path, session_id):
    import dream_activity_copilot

    path = tmp_path / "events.jsonl"
    path.write_bytes(b"")
    with pytest.raises(ValueError):
        dream_activity_copilot.load_evidence(str(path), session_id)


def test_nul_and_surrogate_source_paths_have_content_free_errors():
    import dream_activity_copilot

    for path in ("TOP_SECRET\x00", "TOP_SECRET\ud800"):
        with pytest.raises(ValueError) as error:
            dream_activity_copilot.load_evidence(path, "session-1")
        assert "TOP_SECRET" not in str(error.value)


def test_snapshot_must_be_regular_file(tmp_path):
    import dream_activity_copilot

    with pytest.raises(ValueError, match="regular"):
        dream_activity_copilot.load_evidence(str(tmp_path), "session-1")


def test_late_completion_identity_and_missing_parent_are_preserved(tmp_path):
    packet = load(tmp_path, [
        start(),
        complete(agentId="observed-worker", parentToolCallId="not-observed"),
    ])
    assert packet["calls"][0]["agent_id"] == "observed-worker"
    assert packet["calls"][0]["parent_call_id"] == "not-observed"


def test_oversized_payload_and_shell_processing_fail_explicitly(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [event("future.event", {"values": [0] * 20001})])
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(arguments={"command": "echo " + "x" * 65536})])
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(arguments={"command": "x " * 4097})])


def test_unclosed_shell_quote_and_invalid_patch_do_not_create_artifacts(tmp_path):
    packet = load(tmp_path, [
        start("shell", arguments={"command": "./some-reference 'unclosed"}),
        start("patch", tool="apply_patch", arguments="*** Add File: unrelated.xyz"),
    ])
    assert all(call["artifacts"] == [] for call in packet["calls"])


def test_patch_object_field_preserves_provenance(tmp_path):
    packet = load(tmp_path, [
        start(tool="functions.apply_patch", arguments={
            "patch": "*** Begin Patch\n*** Add File: document.custom\n+body\n*** End Patch",
        }),
    ])
    assert packet["calls"][0]["artifacts"] == [{
        "path": "document.custom", "relation": "created",
        "source": reference(1, "data.arguments.patch"),
    }]


def test_shell_embedded_code_is_not_mislabeled_as_path_evidence(tmp_path):
    packet = load(tmp_path, [start(arguments={
        "command": """python -c "print('/PRIVATE/code.py')" && echo "PRIVATE body ./some.file" ./real.xyz""",
    })])
    artifacts = packet["calls"][0]["artifacts"]
    assert [a["path"] for a in artifacts] == ["./real.xyz"]
    assert "PRIVATE" not in json.dumps(packet)


@pytest.mark.parametrize("command", [
    "bash -c ./body.code",
    "cat <<EOF\n./body.code\nEOF",
    "printf example\n./body.code",
    "pwsh -Command ./body.code",
])
def test_shell_script_bodies_do_not_supply_path_tokens(tmp_path, command):
    packet = load(tmp_path, [start(arguments={"command": command})])
    assert packet["calls"][0]["artifacts"] == []


def test_array_index_is_included_in_source_field_length_bound(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        load(tmp_path, [start(arguments={"x" * 4075: {"paths": ["./file.xyz"]}})])


def test_streaming_defaults_are_shared_with_callers():
    import dream_activity_copilot as adapter

    defaults = {
        "max_bytes": 512 * 1024 * 1024,
        "max_events": 100000,
        "max_calls": 20000,
        "max_text_chars": 2000,
        "max_line_bytes": 16 * 1024 * 1024,
        "max_retained_bytes": 16 * 1024 * 1024,
    }
    signature = inspect.signature(adapter.load_evidence)
    for name, value in defaults.items():
        assert getattr(adapter, f"DEFAULT_{name.upper()}") == value
        assert signature.parameters[name].default == value


def test_records_are_projected_before_reading_the_whole_source(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    row = json.dumps(event("future.event", {"content": "x" * 4000})).encode() + b"\n"
    with path.open("wb") as output:
        for _ in range(128):
            output.write(row)
    original_read = adapter.os.read
    original_parse = adapter._json_line
    bytes_read = parses = 0

    def bounded_read(fd, size):
        nonlocal bytes_read
        assert 0 < size <= 65536
        if bytes_read >= 65536:
            assert parses > 0, "source was buffered instead of projecting records while scanning"
        chunk = original_read(fd, size)
        bytes_read += len(chunk)
        return chunk

    def observed_parse(raw):
        nonlocal parses
        parses += 1
        return original_parse(raw)

    monkeypatch.setattr(adapter.os, "read", bounded_read)
    monkeypatch.setattr(adapter, "_json_line", observed_parse)
    packet = adapter.load_evidence(str(path), "session-1")
    assert packet["source"]["events"] == parses == 128
    assert packet["source"]["bytes"] == bytes_read == len(row) * 128


def test_large_source_streams_with_small_useful_evidence_and_bounded_peak(tmp_path, record_property):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    filler = json.dumps(event("future.event", {"content": "x" * (256 * 1024)})).encode() + b"\r\n"
    first = json.dumps(start(arguments={"description": "Small retained statement"})).encode() + b"\n"
    last = json.dumps(complete(result={"content": "x" * (256 * 1024)}, success=True)).encode()
    digest = hashlib.sha256()
    with path.open("wb") as output:
        output.write(first)
        digest.update(first)
        for _ in range(80):
            output.write(filler)
            digest.update(filler)
        output.write(last)
        digest.update(last)
    del filler, first, last
    assert path.stat().st_size > 16 * 1024 * 1024
    tracemalloc.start()
    try:
        packet = adapter.load_evidence(str(path), "session-1")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    record_property("stream_source_bytes", path.stat().st_size)
    record_property("stream_peak_traced_bytes", peak)
    assert peak < 8 * 1024 * 1024, f"scan retained source-sized memory: {peak}"
    assert packet["source"]["sha256"] == digest.hexdigest()
    assert packet["source"]["bytes"] == path.stat().st_size
    assert packet["source"]["events"] == 82
    assert len(packet["calls"]) == 1
    assert packet["calls"][0]["outcome"]["source"]["line"] == 82


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_record_byte_limit_includes_multibyte_text_and_terminator(tmp_path, ending):
    import dream_activity_copilot as adapter

    raw = json.dumps(start(arguments={"description": "é猫🙂"}), ensure_ascii=False).encode() + ending
    path = tmp_path / "events.jsonl"
    path.write_bytes(raw)
    packet = adapter.load_evidence(str(path), "session-1", max_line_bytes=len(raw))
    assert packet["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="record_byte_limit"):
        adapter.load_evidence(str(path), "session-1", max_line_bytes=len(raw) - 1)


def test_blank_record_is_not_exempt_from_record_limit(tmp_path):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    path.write_bytes(b" " * 50 + b"\n")
    with pytest.raises(ValueError, match="record_byte_limit"):
        adapter.load_evidence(str(path), "session-1", max_line_bytes=50)


def test_mixed_line_endings_and_chunk_split_unicode_have_exact_provenance(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    raw = b"\r\n \t\n" + json.dumps(
        start(arguments={"description": "é猫🙂"}), ensure_ascii=False,
    ).encode() + b"\r\n\n" + json.dumps(complete()).encode()
    path.write_bytes(raw)
    original_read = adapter.os.read
    monkeypatch.setattr(adapter.os, "read", lambda fd, size: original_read(fd, min(size, 7)))
    packet = adapter.load_evidence(str(path), "session-1")
    assert packet["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert packet["source"]["bytes"] == len(raw)
    assert packet["source"]["events"] == 2
    assert packet["calls"][0]["source"]["line"] == 3
    assert packet["calls"][0]["outcome"]["source"]["line"] == 5
    assert packet["calls"][0]["claims"][0]["text"] == "é猫🙂"


def compact_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def test_retention_budget_charges_normalized_call_not_discarded_body(tmp_path):
    rows = [start(arguments={"description": "é猫🙂", "content": "x" * (1024 * 1024)})]
    call = load(tmp_path, rows)["calls"][0]
    exact = compact_bytes({"c1": call})
    assert load(tmp_path, rows, max_retained_bytes=exact)["calls"] == [call]
    with pytest.raises(ValueError, match="evidence_byte_limit"):
        load(tmp_path, rows, max_retained_bytes=exact - 1)


def test_retention_budget_charges_completion_entry_and_agent_binding(tmp_path):
    call = load(tmp_path, [start()])["calls"][0]
    completion = {
        "call_id": "c1", "tool_name": None, "agent_id": None, "parent_call_id": None,
        "outcome": {"completion_observed": True, "tool_success": None, "exit_code": None,
                    "source": reference(2)},
    }
    exact = compact_bytes({"c1": call}) + compact_bytes({"c1": completion})
    rows = [start(), complete()]
    assert load(tmp_path, rows, max_retained_bytes=exact)["calls"][0]["outcome"]["completion_observed"]
    with pytest.raises(ValueError, match="evidence_byte_limit"):
        load(tmp_path, rows, max_retained_bytes=exact - 1)
    rows.append(event("subagent.started", {"toolCallId": "c1", "agentId": "worker"}))
    binding_bytes = compact_bytes({"worker": "c1"})
    assert load(tmp_path, rows, max_retained_bytes=exact + binding_bytes)["calls"]
    with pytest.raises(ValueError, match="evidence_byte_limit"):
        load(tmp_path, rows, max_retained_bytes=exact + binding_bytes - 1)


def test_retention_budget_accumulates_and_stops_before_later_reads(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    first = json.dumps(start("a")).encode() + b"\n"
    second = json.dumps(start("b")).encode() + b"\n"
    normalized = load(tmp_path, [start("a")])["calls"][0]
    cap = compact_bytes({"a": normalized})
    path.write_bytes(first + second + b" " * 100000)
    original_read = adapter.os.read
    reads = 0

    def one_read_only(fd, size):
        nonlocal reads
        reads += 1
        assert reads == 1, "retention overflow must fail without scanning later records"
        return original_read(fd, size)

    monkeypatch.setattr(adapter.os, "read", one_read_only)
    with pytest.raises(ValueError, match="evidence_byte_limit"):
        adapter.load_evidence(str(path), "session-1", max_retained_bytes=cap)


@pytest.mark.parametrize("suffix,diagnostic", [
    (b',"malformed":}\n', "invalid_json"),
    (b',"nested":' + b"[" * 70 + b"0" + b"]" * 70 + b"}\n", "payload_depth_limit"),
    (b',"bad":"\xff"}\n', "invalid_utf8"),
    (b',"bad":"\\ud800"}\n', "invalid_unicode"),
])
def test_large_ignored_records_still_validate_entire_payload(tmp_path, suffix, diagnostic):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"type":"future.event","content":"' + b"x" * (1024 * 1024) + b'"' + suffix)
    with pytest.raises(ValueError, match=diagnostic):
        adapter.load_evidence(str(path), "session-1")


def test_event_id_duplicates_after_many_ignored_records_are_not_forgotten(tmp_path):
    rows = [event("future.event", id="same")]
    rows.extend(event("future.event", id=f"e-{n}") for n in range(100))
    rows.append(event("future.event", id="same"))
    with pytest.raises(ValueError, match="duplicate_event_id"):
        load(tmp_path, rows)


def test_record_limit_across_read_chunks_fails_before_parsing(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"type":"future.event","content":"' + b"x" * 131072 + b'"}\n')

    def must_not_parse(raw):
        pytest.fail("over-budget record reached the JSON parser")

    monkeypatch.setattr(adapter, "_json_line", must_not_parse)
    with pytest.raises(ValueError, match="record_byte_limit"):
        adapter.load_evidence(str(path), "session-1", max_line_bytes=100000)


def test_source_change_during_final_record_projection_fails(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    path.write_bytes(json.dumps(start()).encode())
    original_parse = adapter._json_line

    def parse_then_change(raw):
        result = original_parse(raw)
        with path.open("ab") as output:
            output.write(b"\n")
        return result

    monkeypatch.setattr(adapter, "_json_line", parse_then_change)
    with pytest.raises(ValueError, match="source_changed"):
        adapter.load_evidence(str(path), "session-1")


def test_retention_failure_closes_the_stream_descriptor(tmp_path, monkeypatch):
    import dream_activity_copilot as adapter

    path = tmp_path / "events.jsonl"
    path.write_bytes(json.dumps(start()).encode() + b"\n" + b" " * 100000)
    original_open = adapter.os.open
    descriptors = []

    def observed_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(adapter.os, "open", observed_open)
    with pytest.raises(ValueError, match="evidence_byte_limit"):
        adapter.load_evidence(str(path), "session-1", max_retained_bytes=1)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        adapter.os.fstat(descriptors[0])


def test_structural_validation_ignores_escaped_json_string_punctuation(tmp_path):
    text = 'escaped quote " slash \\ and brackets [[[{{{}}}]]]' * 100
    packet = load(tmp_path, [
        start(arguments={"description": text}),
        event("future.event", {"content": text}),
        complete(result={"content": text}),
    ])
    claim = packet["calls"][0]["claims"][0]
    assert claim["text"] == text[:2000]
    assert claim["truncated"]
