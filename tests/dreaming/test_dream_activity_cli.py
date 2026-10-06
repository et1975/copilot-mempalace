"""Exercise the offline activity workflow through its deployed entry point."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


CLI = (
    Path(__file__).resolve().parents[2]
    / "skills/dreaming/scripts/dream_activity_cli.py"
)


def event(kind, event_id, **data):
    return {
        "id": event_id,
        "type": kind,
        "timestamp": "2026-10-06T12:00:00Z",
        "data": data,
    }


def history(path, *, description="Compare deployment errors", artifact="probe.sql"):
    records = [
        event(
            "tool.execution_start",
            "start",
            toolCallId="call-1",
            toolName="create_file",
            arguments={"path": artifact, "description": description, "content": "private code"},
        ),
        event(
            "tool.execution_complete",
            "complete",
            toolCallId="call-1",
            success=True,
            result={"content": "private execution output"},
        ),
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def invoke(source, output, *extra):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [
            sys.executable,
            "-S",
            str(CLI),
            "--events",
            str(source),
            "--session-id",
            "session-test",
            "--out",
            str(output),
            *extra,
        ],
        text=True,
        capture_output=True,
        env=environment,
        timeout=15,
        check=False,
    )


def test_cli_preserves_provenance_and_omits_raw_payloads(tmp_path):
    source = history(tmp_path / "events.jsonl")
    before = source.read_bytes()
    output = tmp_path / "activities.json"
    result = invoke(source, output)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert output.read_text() == result.stdout
    report = json.loads(result.stdout)
    assert report["kind"] == "activity_intents"
    assert report["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    activity = report["activities"][0]
    assert activity["intent"]["status"] == "stated"
    assert activity["intent"]["text"] == "Compare deployment errors"
    assert activity["intent"]["attribution"] == "agent"
    assert activity["visibility"] == "observed_calls_only"
    assert activity["calls"][0]["outcome"]["tool_success"] is True
    assert source.read_bytes() == before
    assert "private code" not in result.stdout
    assert "private execution output" not in result.stdout


@pytest.mark.parametrize("artifact", ["probe.sql", "probe.fsx", "probe.py", "extensionless"])
def test_artifact_view_is_language_neutral_and_not_promotion(tmp_path, artifact):
    source = history(tmp_path / "events.jsonl", artifact=artifact)
    result = invoke(source, tmp_path / "review.json", "--view", "artifacts")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["kind"] == "artifact_reuse_review"
    assert len(report["candidates"]) == 1
    candidate = report["candidates"][0]
    assert candidate["assessment"] == "needs_review"
    assert artifact in json.dumps(candidate)
    assert "promoted" not in json.dumps(candidate)


def test_direct_call_without_artifact_is_an_activity(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text(
        json.dumps(
            event(
                "tool.execution_start",
                "start",
                toolCallId="call-1",
                toolName="mcp_metrics_query",
                arguments={"description": "Check error rate"},
            )
        ) + "\n"
    )
    result = invoke(source, tmp_path / "activities.json")
    assert result.returncode == 0, result.stderr
    call = json.loads(result.stdout)["activities"][0]["calls"][0]
    assert call["execution_status"] == "incomplete"
    assert call["artifacts"] == []


def test_empty_source_is_valid_empty_observation_not_an_error(tmp_path):
    source = tmp_path / "empty.jsonl"
    source.write_text("")
    result = invoke(source, tmp_path / "activities.json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["activities"] == []


@pytest.mark.parametrize("flag", ["--max-bytes", "--max-events", "--max-calls", "--max-output-bytes"])
def test_exceeded_limits_leave_no_output(tmp_path, flag):
    source = history(tmp_path / "events.jsonl")
    if flag == "--max-calls":
        with source.open("a") as stream:
            stream.write(
                json.dumps(
                    event(
                        "tool.execution_start",
                        "second",
                        toolCallId="call-2",
                        toolName="query",
                        arguments={},
                    )
                ) + "\n"
            )
    output = tmp_path / "not-created.json"
    result = invoke(source, output, flag, "1")
    assert result.returncode != 0
    assert result.stderr
    assert result.stdout == ""
    assert not output.exists()


@pytest.mark.parametrize("view", ["activities", "artifacts"])
def test_output_budget_counts_utf8_bytes(tmp_path, view):
    source = history(tmp_path / "events.jsonl", description="\u8abf\u67fb" * 20)
    initial = invoke(source, tmp_path / "initial.json", "--view", view)
    assert initial.returncode == 0, initial.stderr
    actual_bytes = len(initial.stdout.encode("utf-8"))
    failed_output = tmp_path / "too-small.json"
    failed = invoke(
        source, failed_output, "--view", view, "--max-output-bytes", str(actual_bytes - 1)
    )
    assert failed.returncode != 0
    assert not failed_output.exists()
    succeeded = invoke(
        source, tmp_path / "exact.json", "--view", view, "--max-output-bytes", str(actual_bytes)
    )
    assert succeeded.returncode == 0, succeeded.stderr
    assert (tmp_path / "exact.json").read_bytes() == succeeded.stdout.encode("utf-8")


@pytest.mark.parametrize(
    "flag",
    ["--max-bytes", "--max-events", "--max-calls", "--max-text-chars", "--max-output-bytes",
     "--max-line-bytes", "--max-retained-bytes"],
)
def test_nonpositive_limits_are_argument_errors(tmp_path, flag):
    source = history(tmp_path / "events.jsonl")
    output = tmp_path / "unused.json"
    result = invoke(source, output, flag, "0")
    assert result.returncode == 2
    assert not output.exists()


def test_existing_output_and_input_aliases_are_not_overwritten(tmp_path):
    source = history(tmp_path / "events.jsonl")
    before = source.read_bytes()
    output = tmp_path / "existing.json"
    output.write_text("retain me")
    assert invoke(source, output).returncode != 0
    assert output.read_text() == "retain me"
    assert invoke(source, source).returncode != 0
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)
    assert invoke(source, alias).returncode != 0
    hardlink = tmp_path / "hardlink.json"
    os.link(source, hardlink)
    assert invoke(source, hardlink).returncode != 0
    assert source.read_bytes() == before


@pytest.mark.parametrize("content", ['{"token": "DO_NOT_ECHO"', "[]\n", "{}\n"])
def test_invalid_source_has_content_free_error_and_no_output(tmp_path, content):
    source = tmp_path / "invalid.jsonl"
    source.write_text(content)
    output = tmp_path / "unused.json"
    result = invoke(source, output)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "DO_NOT_ECHO" not in result.stderr
    assert not output.exists()


def test_missing_source_does_not_become_empty_success(tmp_path):
    output = tmp_path / "unused.json"
    result = invoke(tmp_path / "missing.jsonl", output)
    assert result.returncode != 0
    assert not output.exists()


def test_explicit_review_preserves_observed_intent_and_provenance(tmp_path):
    source = history(tmp_path / "events.jsonl")
    original = invoke(source, tmp_path / "original.json")
    assert original.returncode == 0, original.stderr
    original_report = json.loads(original.stdout)
    activity = original_report["activities"][0]
    reviews = tmp_path / "reviews.json"
    reviews.write_text(
        json.dumps(
            [
                {
                    "activity_id": activity["activity_id"],
                    "source_sha256": original_report["source"]["sha256"],
                    "status": "inferred",
                    "text": "Assess the deployment's effect on reliability",
                    "evidence": activity["intent"]["evidence"],
                    "rationale": "The recorded description requests an error comparison.",
                }
            ]
        )
    )
    result = invoke(source, tmp_path / "reviewed.json", "--reviews", str(reviews))
    assert result.returncode == 0, result.stderr
    reviewed = json.loads(result.stdout)["activities"][0]
    assert reviewed["intent"]["status"] == "inferred"
    assert reviewed["observed_intent"] == activity["intent"]
    assert reviewed["review"]["rationale"]


def test_invalid_review_does_not_write_a_report(tmp_path):
    source = history(tmp_path / "events.jsonl")
    reviews = tmp_path / "reviews.json"
    reviews.write_text('[{"activity_id":"fabricated","status":"inferred"}]')
    output = tmp_path / "unused.json"
    result = invoke(source, output, "--reviews", str(reviews))
    assert result.returncode != 0
    assert not output.exists()


def test_stale_review_rejects_changed_snapshot_with_same_event_ids(tmp_path):
    source = history(tmp_path / "events.jsonl")
    original = invoke(source, tmp_path / "original.json")
    assert original.returncode == 0, original.stderr
    report = json.loads(original.stdout)
    activity = report["activities"][0]
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([{
        "activity_id": activity["activity_id"],
        "source_sha256": report["source"]["sha256"],
        "status": "inferred",
        "text": "Old inference",
        "evidence": activity["intent"]["evidence"],
        "rationale": "Evidence from the previous snapshot.",
    }]))
    history(source, description="A different activity under the same event IDs")
    output = tmp_path / "unused.json"
    result = invoke(source, output, "--reviews", str(reviews))
    assert result.returncode != 0
    assert not output.exists()


def test_explicit_delegation_groups_interleaved_calls_not_adjacent_calls(tmp_path):
    source = tmp_path / "events.jsonl"
    delegated = event(
        "tool.execution_start",
        "root",
        toolCallId="dispatch-1",
        toolName="task",
        arguments={"prompt": "Investigate deployment failures", "description": "Delegate audit"},
    )
    child = event(
        "tool.execution_start",
        "child",
        toolCallId="child-1",
        toolName="mcp_query",
        parentToolCallId="dispatch-1",
        arguments={"description": "Read deployment error counts"},
    )
    child["agentId"] = "worker-1"
    unrelated = event(
        "tool.execution_start",
        "unrelated",
        toolCallId="unrelated-1",
        toolName="mcp_query",
        arguments={"description": "Read unrelated inventory"},
    )
    source.write_text(
        "\n".join(json.dumps(record) for record in [delegated, unrelated, child]) + "\n"
    )
    result = invoke(source, tmp_path / "activities.json")
    assert result.returncode == 0, result.stderr
    activities = json.loads(result.stdout)["activities"]
    groups = {activity["root_call_id"]: activity for activity in activities}
    assert len(groups) == 2
    assert set(groups["dispatch-1"]["call_ids"]) == {"dispatch-1", "child-1"}
    assert groups["dispatch-1"]["intent"]["text"] == "Investigate deployment failures"
    child_call = next(call for call in groups["dispatch-1"]["calls"] if call["call_id"] == "child-1")
    assert child_call["intent"]["text"] == "Read deployment error counts"
    assert groups["unrelated-1"]["call_ids"] == ["unrelated-1"]


def test_command_and_spill_paths_are_not_executed_or_followed(tmp_path):
    marker = tmp_path / "must-not-exist"
    source = tmp_path / "events.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                event(
                    "tool.execution_start",
                    "start",
                    toolCallId="call-1",
                    toolName="bash",
                    arguments={"command": f"touch {marker}", "description": "Probe"},
                ),
                event(
                    "tool.execution_complete",
                    "complete",
                    toolCallId="call-1",
                    success=True,
                    result={"content": f"Saved to: {marker}"},
                ),
            ]
        ) + "\n"
    )
    result = invoke(source, tmp_path / "activities.json")
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_large_artifact_expansion_stops_in_projection_before_output_serialization(tmp_path):
    source = tmp_path / "events.jsonl"
    records = []
    for index in range(10):
        patch = "*** Begin Patch\n" + "".join(
            f"*** Add File: ./p-{index}-{number}.sql\n+x\n" for number in range(256)
        ) + "*** End Patch"
        records.append(event(
            "tool.execution_start", f"event-{index}", toolCallId=f"call-{index}",
            toolName="apply_patch", arguments={"patch": patch, "description": "x" * 2000},
        ))
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    assert source.stat().st_size < 150_000
    output = tmp_path / "must-not-exist.json"

    result = invoke(source, output, "--view", "artifacts")

    assert result.returncode == 1
    assert result.stdout == ""
    assert "artifact review" in result.stderr
    assert "output" in result.stderr and "limit" in result.stderr
    assert not output.exists()


def test_cli_forwards_explicit_budget_to_artifact_projection(tmp_path):
    source = history(tmp_path / "events.jsonl", description="x" * 2000)
    output = tmp_path / "must-not-exist.json"

    result = invoke(source, output, "--view", "artifacts", "--max-output-bytes", "1000")

    assert result.returncode == 1
    assert "artifact review" in result.stderr
    assert result.stdout == ""
    assert not output.exists()


def test_complete_description_survives_a_truncated_delegation_prompt(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps(event(
        "tool.execution_start", "start", toolCallId="call-1", toolName="task",
        arguments={"description": "Count deployment errors", "prompt": "Broader audit " * 100},
    )) + "\n")

    result = invoke(source, tmp_path / "activities.json", "--max-text-chars", "32")

    assert result.returncode == 0, result.stderr
    [activity] = json.loads(result.stdout)["activities"]
    assert activity["intent"]["status"] == "stated"
    assert activity["intent"]["text"] == "Count deployment errors"
    assert activity["intent"]["evidence"][0]["field"] == "data.arguments.description"
    assert any(claim["kind"] == "delegation_prompt" and claim["truncated"]
               for claim in activity["calls"][0]["claims"])
