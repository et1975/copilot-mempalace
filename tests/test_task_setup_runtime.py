"""Portable Python setup contracts; Windows API mocks are not certification."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mempalace_tasks import cli, platform_support, platform_windows, setup, setup_runtime as runtime


REPOSITORY = Path(__file__).resolve().parents[1]


def test_setup_has_no_fsharp_entry_or_dependency():
    for relative in ("scripts/setup-tasks.fsx", "scripts/task-setup/Runtime.fsx",
                     "tests/task_setup/contracts.fsx"):
        assert not (REPOSITORY / relative).exists()
    for module in (setup, runtime):
        source = Path(module.__file__).read_text()
        assert "dotnet" not in source
        assert ".fsx" not in source


def test_repository_has_no_obsolete_setup_script_references():
    result = subprocess.run(
        ["git", "grep", "--untracked", "-n", "-I", "-F",
         "-e", "setup-tasks.fsx", "-e", "task-setup/Runtime.fsx",
         "-e", "task_setup/contracts.fsx", "--", ".",
         ":!tests/test_task_setup_runtime.py"],
        cwd=REPOSITORY, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode in (0, 1), result.stderr
    assert result.returncode == 1, "Obsolete setup references:\n" + result.stdout


@pytest.mark.parametrize("mode", ["configure", "enable", "check"])
def test_common_paths_and_consent_defaults(mode, tmp_path):
    args = setup.parse_arguments([
        mode, "--config", str(tmp_path / "config.json"),
        "--task-executable", str(tmp_path / "task.exe"),
        "--copilot-executable", str(tmp_path / "copilot.exe"),
    ])
    assert not args.initialize and not args.generate_token and not args.register_copilot
    assert args.task_executable == str(tmp_path / "task.exe")
    assert args.copilot_executable == str(tmp_path / "copilot.exe")


@pytest.mark.parametrize("arguments", [
    ["setup", "check", "--new-authority"],
    ["setup", "enable", "--generate-token"],
    ["setup", "check", "--config", "relative"],
    ["setup", "configure", "--config", "SECRET", "--unexpected"],
    ["setup", "check", "--config", str(REPOSITORY / "config\ninjected")],
])
def test_cli_invocation_failure_is_json_sanitized_and_side_effect_free(arguments, monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Invocation errors must not inspect paths or start children")

    monkeypatch.setattr(runtime, "task_command", forbidden)
    monkeypatch.setattr(setup, "task_command", forbidden)
    monkeypatch.setattr(platform_support, "canonical_path", forbidden)
    assert cli.main(arguments) == 2
    out, err = capsys.readouterr()
    assert json.loads(out)["phase"] == "arguments"
    assert "SECRET" not in out + err


def test_global_config_and_python_cli_help(monkeypatch, capsys, tmp_path):
    observed = []
    monkeypatch.setattr(setup, "main", lambda argv: observed.append(argv) or 0)
    assert cli.main(["--config", str(tmp_path / "config"), "setup", "check"]) == 0
    assert observed == [["check", "--config", str(tmp_path / "config")]]
    assert cli.main(["--help"]) == 0
    assert "setup" in capsys.readouterr().out


@pytest.mark.parametrize("suffix", [".cmd", ".CMD", ".bat", ".BAT"])
def test_batch_executables_fail_closed_without_shell(tmp_path, suffix):
    selected = tmp_path / ("with & shell metacharacters" + suffix)
    selected.write_text("not executed")
    with pytest.raises(runtime.SetupError, match="unsafe_executable"):
        runtime.executable("copilot", selected)


def test_windows_executable_paths_are_single_arguments(tmp_path, monkeypatch):
    path = tmp_path / "native with & spaces.exe"
    path.write_bytes(b"not executed")
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", path=os.path))
    assert runtime.executable("copilot", path).argv == (str(path),)
    script = tmp_path / "entry with & spaces.py"
    script.write_text("not executed")
    assert runtime.executable("copilot", script).argv == (sys.executable, str(script))


def test_windows_extensionless_path_is_not_assumed_executable(tmp_path, monkeypatch):
    path = tmp_path / "copilot"
    path.write_text("not executable on Windows")
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", path=os.path))
    with pytest.raises(runtime.SetupError, match="missing_executable"):
        runtime.executable("copilot", path)


def test_default_task_module_is_isolated_from_repository_imports(monkeypatch, tmp_path):
    python = str(tmp_path / "python")
    monkeypatch.setattr(runtime.sys, "executable", python)
    observations = []

    def command_run(command, *args):
        observations.append((command.argv, args))
        return subprocess.CompletedProcess(command.argv, 0, "validate-config", "")

    monkeypatch.setattr(runtime.Command, "run", command_run)
    command = runtime.task_command()
    assert command.argv == (python, "-I", "-m", "mempalace_tasks")
    assert observations == [(command.argv, ("--help",))]


def test_uninstalled_module_falls_back_only_to_native_path_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(runtime.Command, "run",
                        lambda *args: subprocess.CompletedProcess([], 1, "", "SECRET"))
    monkeypatch.setenv("PATH", "")
    with pytest.raises(runtime.SetupError, match="missing_executable"):
        runtime.task_command()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bit fixture")
def test_default_executable_discovery_ignores_relative_path_entries(tmp_path, monkeypatch):
    command = tmp_path / "copilot"
    command.write_text("not executed")
    command.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".")
    with pytest.raises(runtime.SetupError, match="missing_executable"):
        runtime.executable("copilot")


def test_windows_discovery_selects_only_absolute_native_executable(tmp_path, monkeypatch):
    native = tmp_path / "copilot.exe"
    native.write_bytes(b"not executed")
    (tmp_path / "copilot.cmd").write_text("not executed")
    environment = {"PATH": os.pathsep.join((".", str(tmp_path)))}
    monkeypatch.setattr(runtime, "os", SimpleNamespace(
        name="nt", path=os.path, pathsep=os.pathsep, environ=environment))
    assert runtime.executable("copilot").argv == (str(native),)


def test_private_directory_is_readonly_and_validates_existing_native_handle(tmp_path, monkeypatch):
    path = platform_support.ensure_private_directory(tmp_path / "private")
    before = path.stat()
    assert runtime.private_directory(path) == path
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises((runtime.SetupError, platform_support.PlatformError)):
        runtime.private_directory(path / "missing")
    assert not (path / "missing").exists()


def test_windows_private_directory_uses_backend_acl_not_chmod(monkeypatch, tmp_path):
    handle = object()
    calls = []

    @contextmanager
    def directory(path):
        calls.append(("open", path))
        yield handle
        calls.append(("close", handle))

    backend = SimpleNamespace(_directory=directory, _private=lambda item: calls.append(("acl", item)))
    monkeypatch.setattr(runtime.platform, "_backend", lambda: backend)
    monkeypatch.setattr(runtime.platform, "canonical_path", lambda value: value)
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    assert runtime.private_directory(tmp_path) == tmp_path
    assert calls == [("open", tmp_path), ("acl", handle), ("close", handle)]


def test_native_windows_path_contract_rejects_shell_aliases_before_io(monkeypatch):
    monkeypatch.setattr(platform_windows, "_require", lambda: None)
    for path in ("relative", "C:relative", r"\\server\share\config", r"C:\NUL",
                 "C:\\private\\config:stream", "C:\\private\\config."):
        with pytest.raises(platform_support.PlatformError):
            platform_windows._path(path)


def test_windows_job_is_kill_on_close_and_breakaway_is_explicit(monkeypatch):
    calls = []
    api = SimpleNamespace(CloseHandle=lambda handle: calls.append(("close", handle)))
    job = SimpleNamespace(
        CreateJobObject=lambda *_: "owned-job",
        QueryInformationJobObject=lambda *_: {"BasicLimitInformation": {}},
        SetInformationJobObject=lambda *args: calls.append(("limits", args[2])),
        AssignProcessToJobObject=lambda *args: calls.append(("assign", args)),
        JobObjectExtendedLimitInformation=9,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x2000,
        JOB_OBJECT_LIMIT_BREAKAWAY_OK=0x800,
    )
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32job", job)
    for allow_service in (False, True):
        owned = runtime.WindowsJob(allow_service)
        owned.assign(SimpleNamespace(_handle="owned-child"))
        owned.close()
        owned.close()
        assert calls[-3:] == [
            ("limits", {"BasicLimitInformation": {"LimitFlags": 0x2800 if allow_service else 0x2000}}),
            ("assign", ("owned-job", "owned-child")),
            ("close", "owned-job"),
        ]


def test_windows_gate_assigns_job_before_selected_child_runs(tmp_path, monkeypatch):
    target = tmp_path / "started"
    calls = []

    class Job:
        def __init__(self, allow_service):
            assert not allow_service

        def assign(self, process):
            assert not target.exists()
            calls.append("assigned")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(runtime, "WindowsJob", Job)
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    argv = (sys.executable, "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started'); print('ok')",
            str(target))
    with runtime.Child(argv) as child:
        result = child.complete()
    assert target.read_text() == "started"
    assert result.returncode == 0 and result.stdout == "ok\n"
    assert calls == ["assigned", "closed"]


def test_child_excessive_requests_are_bounded_before_pipe_write():
    child = object.__new__(runtime.Child)
    child.deadline = float("inf")
    child.error = None
    child.sent = 4090
    child.process = SimpleNamespace(stdin=None)
    with pytest.raises(runtime.SetupError, match="request_limit"):
        child.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_malformed_protocol_catalog_and_health_are_rejected(monkeypatch, tmp_path):
    class ProtocolChild:
        def __init__(self, *_):
            self.health = {"authority_id": "00000000-0000-0000-0000-000000000001",
                           "epoch_id": "00000000-0000-0000-0000-000000000002",
                           "fresh": True, "maintenance": {"ok": True}}
            self.tool = {"name": "mptask_create", "description": "create",
                         "inputSchema": {"type": "object"}}
            self.initialized = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}

        def call(self, method, params):
            if method == "initialize":
                return self.initialized
            if method == "tools/list":
                return {"tools": [self.tool, *[
                    {**self.tool, "name": f"mptask_{name}"}
                    for name in ("expand", "goal_close", "snapshot", "health")]]}
            assert params == {"name": "mptask_health", "arguments": {}}
            return {"structuredContent": self.health}

        def notify(self, value):
            assert value == {"jsonrpc": "2.0", "method": "notifications/initialized"}

        def finish_protocol(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    args = setup.parse_arguments(["check", "--config", str(tmp_path / "config")])
    command = runtime.Command(("unused",))
    child = ProtocolChild()
    monkeypatch.setattr(setup, "Child", lambda *_: child)
    assert setup.probe(command, args, child.health["authority_id"]) == 5
    for target, key, value, code in [
        (child.initialized, "protocolVersion", "unknown", "unsupported_protocol"),
        (child.initialized, "capabilities", {}, "missing_tools_capability"),
        (child.tool, "name", "not_task", "invalid_tool_catalog"),
        (child.tool, "description", "", "invalid_tool_descriptor"),
        (child.tool, "inputSchema", {"type": "string"}, "invalid_tool_descriptor"),
        (child.health, "epoch_id", "00000000-0000-0000-0000-000000000000", "invalid_epoch"),
        (child.health, "fresh", 1, "health_not_fresh"),
        (child.health, "maintenance", {"ok": "true"}, "maintenance_unhealthy"),
    ]:
        original = target[key]
        target[key] = value
        with pytest.raises(runtime.SetupError, match=code):
            setup.probe(command, args, child.health["authority_id"])
        target[key] = original


def test_missing_windows_job_api_is_explicit(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32job", None)
    with pytest.raises(runtime.SetupError, match="unsupported_child_control"):
        runtime.WindowsJob(False)


@pytest.mark.parametrize("name,backend", [("posix", "platform_posix"), ("nt", "platform_windows")])
def test_platform_dispatch_uses_native_backend(name, backend, monkeypatch):
    monkeypatch.setattr(platform_support, "os", SimpleNamespace(name=name))
    assert platform_support._backend().__name__.endswith(backend)


@pytest.mark.parametrize("owner,protected,ace_sid,ace_flags,accepted", [
    ("self", True, "self", 0, True),
    ("other", True, "self", 0, False),
    ("self", False, "self", 0, False),
    ("self", True, "other", 0, False),
    ("self", True, "self", 0x10, False),
])
def test_windows_acl_requires_owned_protected_owner_only_access(
        owner, protected, ace_sid, ace_flags, accepted, monkeypatch):
    acl = SimpleNamespace(GetAceCount=lambda: 1, GetAce=lambda _: ((0, ace_flags), 0xFFFF, ace_sid))
    descriptor = SimpleNamespace(
        GetSecurityDescriptorOwner=lambda: owner,
        GetSecurityDescriptorControl=lambda: (0x1000 if protected else 0, 1),
        GetSecurityDescriptorDacl=lambda: acl,
    )
    security = SimpleNamespace(
        GetSecurityInfo=lambda *_: descriptor,
        SE_FILE_OBJECT=1, OWNER_SECURITY_INFORMATION=1, DACL_SECURITY_INFORMATION=4,
        SE_DACL_PROTECTED=0x1000, ACCESS_ALLOWED_ACE_TYPE=0, INHERITED_ACE=0x10,
        INHERIT_ONLY_ACE=8,
    )
    monkeypatch.setattr(platform_windows, "_require", lambda: (None, None, None, None, security))
    monkeypatch.setattr(platform_windows, "_current_sid", lambda: "self")
    if accepted:
        platform_windows._private(object())
    else:
        with pytest.raises(platform_support.PlatformError, match="Object requires|DACL permits"):
            platform_windows._private(object())
