"""Isolated executable contracts for the optional F# setup entry point.

The same test file doubles as the fixture CLI, copied under pytest's external
temporary root. It never invokes an installed task service or user Copilot.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


def fixture_cli():
    root = Path(os.environ["SETUP_FIXTURE_ROOT"])
    selected = os.environ.get("SETUP_FIXTURE_EXECUTABLE_DIRECTORY")
    if selected:
        assert Path(sys.argv[0]).parent == Path(selected), "Unexpected PATH executable"
    arguments = sys.argv[1:]
    fault = os.environ.get("SETUP_FIXTURE_FAULT", "")
    command = arguments[0]
    with (root / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps([Path(sys.argv[0]).name, *arguments]) + "\n")

    def emit(value):
        print(json.dumps(value), flush=True)

    def error(code):
        print(code + ": hidden Bearer SECRET-DO-NOT-PRINT", file=sys.stderr)
        raise SystemExit(1)

    if Path(sys.argv[0]).name == "copilot":
        registrations = json.loads((root / "registrations.json").read_text())
        if arguments[:2] == ["mcp", "get"]:
            if fault == "registration_failure":
                error("unavailable")
            if "mempalace-tasks" not in registrations:
                print('Error: Server "mempalace-tasks" not found.', file=sys.stderr)
                raise SystemExit(1)
            emit({"mempalace-tasks": registrations["mempalace-tasks"]})
        elif arguments[:2] == ["mcp", "add"]:
            if fault == "add_failure":
                error("add_failed")
            command_args = arguments[arguments.index("--") + 1:]
            registrations["mempalace-tasks"] = {
                "type": "local", "command": command_args[0],
                "args": command_args[1:], "tools": ["*"], "enabled": True,
            }
            (root / "registrations.json").write_text(json.dumps(registrations))
            emit({"ok": True})
        else:
            error("bad_copilot_command")
        return
    if command == "--help":
        print("init start connect mcp" + ("" if fault == "old_cli" else " validate-config"))
        return
    if arguments == ["mcp", "--help"]:
        if fault == "old_frontend":
            print("mcp --config --timeout")
            return
        sys.path.insert(0, os.environ["SETUP_SIDECAR_SOURCE"])
        from mempalace_tasks.cli import main
        raise SystemExit(main(arguments))
    config_path = Path(arguments[arguments.index("--config") + 1])
    sys.path.insert(0, os.environ["SETUP_SIDECAR_SOURCE"])
    if command == "validate-config":
        if fault == "validator_garbage":
            print("Bearer SECRET-DO-NOT-PRINT")
            return
        from mempalace_tasks.cli import main
        raise SystemExit(main(arguments))
    from mempalace_tasks.config import ConfigError, create_service_token, load_config

    try:
        config = load_config(
            config_path,
            allow_missing_service_token=(
                "--allow-missing-service-token" in arguments or "--generate-token" in arguments
            ),
        )
    except ConfigError as problem:
        print(problem.code + ": " + str(problem), file=sys.stderr)
        raise SystemExit(2)
    identity = config.authority_id
    if command == "connect":
        if fault == "binding":
            error("binding_mismatch")
        if not (root / "owner").exists():
            error("registry_missing")
        emit({"authority_id": identity, "instance_id": str(uuid.UUID(int=1)), "ready": True})
        if fault == "owner_race":
            (root / "owner").unlink()
    elif command == "init":
        if fault == "hub_failure":
            error("palace_unavailable")
        (root / "genesis").write_text(identity)
        if config.service_token is None:
            create_service_token(config.service_token_file)
        emit({"ok": True, "authority_id": identity})
    elif command == "start":
        if not (root / "genesis").exists():
            error("not_initialized")
        (root / "owner").write_text(identity)
        emit({"authority_id": identity, "instance_id": str(uuid.UUID(int=1)), "ready": True})
    elif command == "mcp":
        original = json.loads((root / "config-path").read_text())
        assert config_path == Path(original), "Probe must use the original config path"
        assert "--no-autostart" in arguments
        assert config.lifecycle == "launcher"
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert config_path.parent.stat().st_mode & 0o777 == 0o700
        (root / "probe-config").write_text(str(config_path))
        if not (root / "owner").exists():
            error("registry_missing")
        print("Bearer SECRET-DO-NOT-PRINT", file=sys.stderr, flush=True)
        if fault == "timeout":
            (root / "child-pid").write_text(str(os.getpid()))
            descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
            (root / "descendant-pid").write_text(str(descendant.pid))
            time.sleep(90)
        if fault == "stdout_cap":
            print("x" * 1100000, flush=True)
            return
        if fault == "stderr_cap":
            print("SECRET-DO-NOT-PRINT" * 70000, file=sys.stderr, flush=True)
        for line in sys.stdin:
            message = json.loads(line)
            if "id" not in message:
                continue
            result = {}
            if fault == "malformed":
                print("Bearer SECRET-DO-NOT-PRINT", flush=True)
                return
            if fault == "rpc_error":
                emit({"jsonrpc": "2.0", "id": message["id"], "error": {"message": "SECRET"}})
                continue
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            elif message["method"] == "tools/list":
                names = ["create", "expand", "goal_close", "snapshot", "health"]
                if fault == "missing_tool":
                    names.remove("create")
                result = {"tools": [
                    {"name": "mptask_" + name, "description": name,
                     "inputSchema": {"type": "object"}} for name in names
                ]}
                if fault == "cursor_loop":
                    result["nextCursor"] = "repeat"
            elif message["method"] == "tools/call":
                assert message["params"]["name"] == "mptask_health"
                health = {
                    "authority_id": identity if fault != "wrong_authority" else str(uuid.uuid4()),
                    "epoch_id": str(uuid.UUID(int=1)),
                    "fresh": fault != "stale",
                    "maintenance": {"ok": fault != "unhealthy"},
                }
                result = {"structuredContent": health}
                if fault == "tool_error":
                    result["isError"] = True
                if fault == "text_health":
                    result = {"content": [{"type": "text", "text": json.dumps(health)}]}
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            if fault == "invalid_utf8" and message["method"] == "tools/list":
                encoded = json.dumps(response).encode().replace(
                    b'"description": "create"', b'"description": "bad\xff"',
                )
                sys.stdout.buffer.write(encoded + b"\n")
                sys.stdout.buffer.flush()
            else:
                emit(response)
            if fault == "trailing_stdout" and message["method"] == "tools/call":
                print("Bearer SECRET-DO-NOT-PRINT", flush=True)
        (root / "eof-seen").write_text("true")
    else:
        error("unsupported")


if __name__ == "__main__" and os.environ.get("SETUP_FIXTURE_ROOT"):
    fixture_cli()
    raise SystemExit(0)

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "setup-tasks.fsx"
pytestmark = pytest.mark.skipif(
    shutil.which("dotnet") is None or sys.platform != "linux",
    reason="optional Linux F# setup runtime unavailable; no installation attempted",
)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    binary = tmp_path / "bin"
    binary.mkdir(mode=0o700)
    for name in ("mempalace-tasks", "copilot"):
        executable = binary / name
        executable.write_text("#!" + sys.executable + "\n" + Path(__file__).read_text())
        executable.chmod(0o700)
    (tmp_path / "registrations.json").write_text(json.dumps({"unrelated": {"env": {"KEEP": "yes"}}}))
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SETUP_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setenv("SETUP_SIDECAR_SOURCE", str(REPOSITORY / "sidecar" / "src"))
    config = tmp_path / "private" / "config.json"
    (tmp_path / "config-path").write_text(json.dumps(str(config)))
    return tmp_path, config


def invoke(deployment, mode, *arguments, ok=True):
    root, config = deployment
    result = subprocess.run(
        ["dotnet", "fsi", "--exec", str(SCRIPT), mode, "--config", str(config), *arguments],
        capture_output=True, text=True, timeout=65,
    )
    assert "SECRET" not in result.stdout + result.stderr
    assert (result.returncode == 0) == ok, (result.stdout, result.stderr)
    summary = json.loads(result.stdout)
    assert summary["ok"] == ok
    return summary


def calls(deployment):
    path = deployment[0] / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def configure(deployment):
    return invoke(deployment, "configure", "--new-authority", "--hub-url", "http://127.0.0.1:8765/mcp")


def ready(deployment):
    configure(deployment)
    return invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot")


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        if path.name in {"calls.jsonl", "probe-config", "child-pid", "descendant-pid"}:
            continue
        if path.is_symlink():
            content = ("symlink", os.readlink(path))
        elif path.is_dir():
            content = "directory"
        elif path.is_file():
            content = path.read_bytes()
        else:
            content = "special"
        result[str(path.relative_to(root))] = content, path.lstat().st_mode & 0o777
    return result


def test_fresh_configure_is_private_valid_and_idempotent(deployment):
    configure(deployment)
    root, config = deployment
    document = json.loads(config.read_text())
    assert document["schema_version"] == 2
    assert document["lifecycle"] == "launcher"
    assert document["host"] == "127.0.0.1" and document["port"] == 0
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700
    assert Path(document["runtime_dir"]).stat().st_mode & 0o777 == 0o700
    assert not Path(document["service_token_file"]).exists()
    before = snapshot(root)
    configure(deployment)
    assert snapshot(root) == before
    assert all(call[1] not in ("init", "start", "connect") for call in calls(deployment))


@pytest.mark.parametrize("arguments", [
    (), ("--hub-url", "http://127.0.0.1:8765/mcp"), ("--new-authority",),
    ("--new-authority", "--hub-url", "https://example.org/mcp"),
    ("--new-authority", "--hub-url", "http://secret@127.0.0.1/mcp"),
    ("--initialize",), ("--unknown",),
])
def test_invalid_configure_has_no_mutations(deployment, arguments):
    before = snapshot(deployment[0])
    invoke(deployment, "configure", *arguments, ok=False)
    assert snapshot(deployment[0]) == before
    assert not deployment[1].parent.exists()


@pytest.mark.parametrize("document,code", [
    ('{"schema_version":1}', "unsupported_schema"),
    ("not json", "malformed_config"),
    ('{"schema_version":2,"unexpected":true}', "invalid_config"),
])
def test_existing_bad_schema_is_not_replaced(deployment, document, code):
    _, config = deployment
    config.parent.mkdir(mode=0o700)
    config.write_text(document)
    config.chmod(0o600)
    before = snapshot(deployment[0])
    assert invoke(deployment, "configure", ok=False)["code"] == code
    assert snapshot(deployment[0]) == before


def test_existing_config_rejects_different_inputs(deployment):
    configure(deployment)
    before = snapshot(deployment[0])
    invoke(deployment, "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:9999/mcp", ok=False)
    invoke(deployment, "configure", "--hub-token-file", str(deployment[0] / "other"), ok=False)
    assert snapshot(deployment[0]) == before


def test_enable_order_and_idempotence(deployment):
    ready(deployment)
    ordered = [call[1] for call in calls(deployment)
               if call[0] == "mempalace-tasks" and "--help" not in call]
    assert ordered.index("connect") < ordered.index("init") < ordered.index("start") < ordered.index("mcp")
    root, _ = deployment
    before = snapshot(root)
    offset = len(calls(deployment))
    invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot")
    assert snapshot(root) == before
    assert not any(call[1] in ("init", "start") for call in calls(deployment)[offset:])
    assert json.loads((root / "registrations.json").read_text())["unrelated"] == {"env": {"KEEP": "yes"}}


def test_enable_without_initialize_never_creates_genesis(deployment):
    configure(deployment)
    invoke(deployment, "enable", "--register-copilot", ok=False)
    assert not (deployment[0] / "genesis").exists()
    assert not any(call[1] == "init" for call in calls(deployment))
    invoke(deployment, "enable", "--generate-token", ok=False)


def test_check_is_readonly_and_absent_owner_never_spawns(deployment):
    ready(deployment)
    before = snapshot(deployment[0])
    offset = len(calls(deployment))
    invoke(deployment, "check")
    assert snapshot(deployment[0]) == before
    assert Path((deployment[0] / "probe-config").read_text()) == deployment[1]
    task_calls = [call for call in calls(deployment)[offset:] if call[0] == "mempalace-tasks"]
    assert next(index for index, call in enumerate(task_calls) if call[1] == "connect") < next(
        index for index, call in enumerate(task_calls) if call[1] == "mcp"
    )
    frontend = next(call for call in task_calls if call[1] == "mcp" and "--config" in call)
    assert "--no-autostart" in frontend
    (deployment[0] / "owner").unlink()
    offset = len(calls(deployment))
    before = snapshot(deployment[0])
    assert invoke(deployment, "check", ok=False)["code"] == "owner_not_running"
    assert snapshot(deployment[0]) == before
    assert not any(call[0] == "mempalace-tasks" and call[1] in ("init", "start", "mcp")
                   for call in calls(deployment)[offset:])


@pytest.mark.parametrize("change", [
    {"command": "/bin/false"}, {"args": ["mcp", "--config", "/elsewhere"]},
    {"tools": []}, {"tools": ["mptask_health"]}, {"enabled": False},
    {"env": {"CUSTOM": "SECRET-DO-NOT-PRINT"}}, {"headers": {"X": "SECRET-DO-NOT-PRINT"}},
])
def test_registration_conflicts_refuse_before_mutation(deployment, change):
    ready(deployment)
    root, _ = deployment
    registrations = json.loads((root / "registrations.json").read_text())
    registrations["mempalace-tasks"].update(change)
    (root / "registrations.json").write_text(json.dumps(registrations))
    (root / "owner").unlink()
    before = snapshot(root)
    offset = len(calls(deployment))
    invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot", ok=False)
    invoke(deployment, "check", ok=False)
    assert snapshot(root) == before
    assert not any(call[1] in ("init", "start") or call[1:3] == ["mcp", "add"]
                   for call in calls(deployment)[offset:])


@pytest.mark.parametrize("fault", [
    "missing_tool", "unhealthy", "stale", "wrong_authority", "rpc_error", "tool_error",
    "timeout", "malformed", "stdout_cap", "stderr_cap", "cursor_loop", "binding",
    "validator_garbage", "trailing_stdout", "invalid_utf8",
])
def test_protocol_failures_are_bounded_private_and_cleaned(deployment, monkeypatch, fault):
    ready(deployment)
    before = snapshot(deployment[0])
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", fault)
    invoke(deployment, "check", ok=False)
    assert snapshot(deployment[0]) == before
    assert Path((deployment[0] / "probe-config").read_text()) == deployment[1]
    pid_file = deployment[0] / "child-pid"
    if pid_file.exists():
        assert not Path("/proc", pid_file.read_text()).exists()
        status = Path("/proc", (deployment[0] / "descendant-pid").read_text(), "status")
        assert not status.exists() or "State:\tZ" in status.read_text()


def test_text_health_is_supported(deployment, monkeypatch):
    ready(deployment)
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "text_health")
    invoke(deployment, "check")


def test_init_and_registration_partial_failures_report_phase(deployment, monkeypatch):
    configure(deployment)
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "hub_failure")
    summary = invoke(deployment, "enable", "--initialize", "--generate-token",
                     "--register-copilot", ok=False)
    assert summary["phase"] == "initialize"
    assert not any(call[1] == "start" for call in calls(deployment))
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "add_failure")
    summary = invoke(deployment, "enable", "--initialize", "--generate-token",
                     "--register-copilot", ok=False)
    assert summary["phase"] == "registration"
    assert (deployment[0] / "genesis").exists() and (deployment[0] / "owner").exists()


def test_missing_prerequisites_and_registration(deployment, monkeypatch):
    assert invoke(deployment, "check", ok=False)["code"] == "missing_config"
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "old_cli")
    assert invoke(deployment, "configure", "--new-authority", "--hub-url",
                  "http://127.0.0.1:8765/mcp", ok=False)["code"] == "validator_unavailable"
    monkeypatch.delenv("SETUP_FIXTURE_FAULT")
    configure(deployment)
    assert invoke(deployment, "check", ok=False)["code"] == "missing_credential"
    assert invoke(deployment, "check", "--task-executable",
                  str(deployment[0] / "absent"), ok=False)["code"] == "missing_executable"
    ready(deployment)
    (deployment[0] / "registrations.json").write_text("{}")
    assert invoke(deployment, "check", ok=False)["code"] == "missing_registration"


@pytest.mark.parametrize("kind", ["parent_link", "config_link", "public_parent", "public_config"])
def test_unsafe_paths_refuse_without_repair(deployment, kind):
    root, config = deployment
    other = root / "other"
    other.mkdir(mode=0o700)
    if kind == "parent_link":
        config.parent.symlink_to(other, target_is_directory=True)
    else:
        config.parent.mkdir(mode=0o700)
        if kind == "public_parent":
            config.parent.chmod(0o755)
        elif kind == "config_link":
            source = other / "config"
            source.write_text('{"schema_version":1}')
            config.symlink_to(source)
        else:
            config.write_text('{"schema_version":1}')
            config.chmod(0o644)
    before = snapshot(root)
    invoke(deployment, "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:8765/mcp", ok=False)
    assert snapshot(root) == before


def test_fsharp_contracts(deployment):
    result = subprocess.run(
        ["dotnet", "fsi", "--exec", str(REPOSITORY / "tests/task_setup/contracts.fsx")],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_runtime_is_not_inferred_as_missing_history(deployment):
    ready(deployment)
    root, config = deployment
    document = json.loads(config.read_text())
    Path(document["runtime_dir"]).rmdir()
    (root / "genesis").unlink()  # No inspection of this fixture's local history marker.
    offset = len(calls(deployment))
    before = snapshot(root)
    invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot")
    assert snapshot(root) == before
    assert not Path(document["runtime_dir"]).exists()
    assert not any(call[1] in ("init", "start") for call in calls(deployment)[offset:])
    assert (root / "eof-seen").read_text() == "true"
    assert (root / "owner").exists()


def test_absent_owner_with_existing_token_requires_explicit_init(deployment):
    ready(deployment)
    root, _ = deployment
    (root / "owner").unlink()
    (root / "genesis").unlink()
    offset = len(calls(deployment))
    invoke(deployment, "enable", ok=False)
    assert not (root / "genesis").exists()
    assert not any(call[1] == "init" for call in calls(deployment)[offset:])
    invoke(deployment, "enable", "--initialize")
    assert (root / "genesis").exists()


def test_owner_shutdown_race_cannot_start_owner(deployment, monkeypatch):
    ready(deployment)
    offset = len(calls(deployment))
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "owner_race")
    invoke(deployment, "check", ok=False)
    assert not (deployment[0] / "owner").exists()
    assert not any(call[1] in ("init", "start") for call in calls(deployment)[offset:])
    assert Path((deployment[0] / "probe-config").read_text()) == deployment[1]


@pytest.mark.parametrize("kind", [
    "runtime_link", "runtime_public", "token_link", "token_public", "missing_hub_token",
])
def test_native_validation_and_private_runtime_refuse_before_enable(deployment, kind):
    configure(deployment)
    root, config = deployment
    document = json.loads(config.read_text())
    runtime = Path(document["runtime_dir"])
    token = Path(document["service_token_file"])
    if kind == "runtime_link":
        runtime.rmdir()
        runtime.symlink_to(root)
    elif kind == "runtime_public":
        runtime.chmod(0o755)
    elif kind == "token_link":
        other = root / "other-token"
        other.write_text("SECRET-DO-NOT-PRINT")
        other.chmod(0o600)
        token.symlink_to(other)
    elif kind == "token_public":
        token.write_text("SECRET-DO-NOT-PRINT")
        token.chmod(0o644)
    else:
        document["hub_token_file"] = str(root / "absent-hub-token")
        config.write_text(json.dumps(document))
    before = snapshot(root)
    offset = len(calls(deployment))
    invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot", ok=False)
    assert snapshot(root) == before
    assert not any(call[1] in ("init", "start") for call in calls(deployment)[offset:])


def test_missing_hub_token_prevents_config_publication(deployment):
    invoke(deployment, "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:8765/mcp", "--hub-token-file",
           str(deployment[0] / "absent-token"), ok=False)
    assert not deployment[1].parent.exists()


def test_registration_query_failure_never_means_missing(deployment, monkeypatch):
    ready(deployment)
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "registration_failure")
    offset = len(calls(deployment))
    invoke(deployment, "enable", "--initialize", "--register-copilot", ok=False)
    assert not any(call[1] in ("init", "start") or call[1:3] == ["mcp", "add"]
                   for call in calls(deployment)[offset:])


def test_configure_staging_cannot_be_placed_in_repository(deployment, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(REPOSITORY))
    assert invoke(deployment, "configure", "--new-authority", "--hub-url",
                  "http://127.0.0.1:8765/mcp", ok=False)["code"] == "temporary_root_inside_repository"
    assert not deployment[1].parent.exists()


def test_check_does_not_need_temporary_config(deployment, monkeypatch):
    ready(deployment)
    monkeypatch.setenv("TMPDIR", str(REPOSITORY))
    before = snapshot(deployment[0])
    invoke(deployment, "check")
    assert snapshot(deployment[0]) == before
    assert Path((deployment[0] / "probe-config").read_text()) == deployment[1]


@pytest.mark.parametrize("arguments", [
    ["mcp", "--config", "CONFIG", "--timeout", "10s"],
    ["mcp", "--timeout", "10s", "--config", "CONFIG"],
])
def test_default_timeout_registration_is_preserved(deployment, arguments):
    ready(deployment)
    root, config = deployment
    path = root / "registrations.json"
    registrations = json.loads(path.read_text())
    registrations["mempalace-tasks"]["args"] = [
        str(config) if argument == "CONFIG" else argument for argument in arguments
    ]
    path.write_text(json.dumps(registrations))
    before = snapshot(root)
    offset = len(calls(deployment))
    invoke(deployment, "check")
    invoke(deployment, "enable", "--initialize", "--generate-token", "--register-copilot")
    assert snapshot(root) == before
    assert not any(call[1] in ("init", "start") or call[1:3] == ["mcp", "add"]
                   for call in calls(deployment)[offset:])


def test_missing_no_autostart_support_refuses_before_enable(deployment, monkeypatch):
    configure(deployment)
    monkeypatch.setenv("SETUP_FIXTURE_FAULT", "old_frontend")
    before = snapshot(deployment[0])
    summary = invoke(deployment, "enable", "--initialize", "--generate-token",
                     "--register-copilot", ok=False)
    assert summary["code"] == "no_autostart_unavailable"
    assert snapshot(deployment[0]) == before
    assert not any(call[1] in ("init", "start") for call in calls(deployment))


def test_nondefault_timeout_registration_refuses_before_mutation(deployment):
    ready(deployment)
    root, config = deployment
    path = root / "registrations.json"
    registrations = json.loads(path.read_text())
    registrations["mempalace-tasks"]["args"] = [
        "mcp", "--config", str(config), "--timeout", "60s",
    ]
    path.write_text(json.dumps(registrations))
    (root / "owner").unlink()
    before = snapshot(root)
    offset = len(calls(deployment))
    summary = invoke(deployment, "enable", "--initialize", "--register-copilot", ok=False)
    assert summary["code"] == "registration_mismatch"
    assert snapshot(root) == before
    assert not any(call[1] in ("init", "start") or call[1:3] == ["mcp", "add"]
                   for call in calls(deployment)[offset:])


def test_missing_copilot_and_config_directory_do_not_mutate(deployment):
    ready(deployment)
    before = snapshot(deployment[0])
    assert invoke(deployment, "check", "--copilot-executable",
                  str(deployment[0] / "missing"), ok=False)["code"] == "missing_executable"
    assert snapshot(deployment[0]) == before
    deployment[1].unlink()
    deployment[1].mkdir(mode=0o700)
    before = snapshot(deployment[0])
    invoke(deployment, "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:8765/mcp", ok=False)
    assert snapshot(deployment[0]) == before


def test_config_fifo_refuses_without_blocking(deployment):
    _, config = deployment
    config.parent.mkdir(mode=0o700)
    os.mkfifo(config, mode=0o600)
    assert invoke(deployment, "configure", ok=False)["code"] == "malformed_config"


def test_directory_shaped_config_path_refuses_before_mutation(deployment):
    root, config = deployment
    before = snapshot(root)
    invoke((root, str(config) + "/"), "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:8765/mcp", ok=False)
    assert snapshot(root) == before
    assert not config.parent.exists()


def test_executable_shebang_and_invalid_invocation(deployment):
    result = subprocess.run(
        [str(SCRIPT), "check", "--initialize"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "ok": False, "code": "invalid_invocation", "phase": "arguments",
    }
    assert calls(deployment) == []


def test_common_executable_overrides_select_all_phases(deployment, monkeypatch):
    root, _ = deployment
    selected = root / "selected"
    selected.mkdir(mode=0o700)
    for name in ("mempalace-tasks", "copilot"):
        shutil.copy2(root / "bin" / name, selected / name)
    monkeypatch.setenv("SETUP_FIXTURE_EXECUTABLE_DIRECTORY", str(selected))
    arguments = (
        "--task-executable", str(selected / "mempalace-tasks"),
        "--copilot-executable", str(selected / "copilot"),
    )
    invoke(deployment, "configure", "--new-authority", "--hub-url",
           "http://127.0.0.1:8765/mcp", *arguments)
    assert all(call[0] != "copilot" for call in calls(deployment))
    invoke(deployment, "enable", "--initialize", "--generate-token",
           "--register-copilot", *arguments)
    registration = json.loads((root / "registrations.json").read_text())["mempalace-tasks"]
    assert registration["command"] == str(selected / "mempalace-tasks")
    before = snapshot(root)
    invoke(deployment, "check", *arguments)
    invoke(deployment, "enable", "--initialize", "--generate-token",
           "--register-copilot", *arguments)
    assert snapshot(root) == before


def test_native_no_autostart_preserves_original_binding_on_registry_loss(
        deployment, monkeypatch, capsys):
    """Exercise the real native CLI flag without launching an owner or network service."""
    from mempalace_tasks import cli, discovery, stdio_frontend
    from mempalace_tasks.config import create_service_token, load_config
    from mempalace_tasks.server_identity import InstanceIdentity

    configure(deployment)
    config = load_config(deployment[1], allow_missing_service_token=True)
    create_service_token(config.service_token_file)
    config = load_config(deployment[1])
    identity = InstanceIdentity(
        config.authority_id, str(uuid.uuid4()), "http://127.0.0.1:19000/mcp",
    )
    discovery.publish_registry(config, identity)
    events = []

    def forbidden_start(*args, **kwargs):
        events.append("start")
        raise AssertionError("No-autostart must never launch an owner")

    def authenticated(observed, observed_identity, deadline):
        assert observed == config and observed.lifecycle == "launcher"
        assert observed_identity == identity
        events.append("probe")
        return discovery.ConnectionInfo(
            identity.endpoint, identity.authority_id, identity.instance_id, "fixture-token", True,
        )

    async def frontend(connection, timeout, secrets):
        assert connection.authority_id == config.authority_id and timeout == 10.0
        events.append("frontend")
        return 0

    monkeypatch.setattr(stdio_frontend.launcher, "start", forbidden_start)
    monkeypatch.setattr(discovery, "_probe", authenticated)
    monkeypatch.setattr(stdio_frontend, "_run", frontend)
    arguments = ["mcp", "--config", str(deployment[1]), "--timeout", "10s", "--no-autostart"]
    before = snapshot(deployment[0])
    assert cli.main(arguments) == 0
    assert events == ["probe", "frontend"]
    assert snapshot(deployment[0]) == before
    assert capsys.readouterr().out == ""

    (config.runtime_dir / "serverinfo.json").unlink()
    before = snapshot(deployment[0])
    assert cli.main(arguments) == 1
    output = capsys.readouterr()
    assert output.out == "" and "registry_missing:" in output.err
    assert events == ["probe", "frontend"]
    assert snapshot(deployment[0]) == before
