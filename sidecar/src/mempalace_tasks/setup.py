"""Explicit configuration, enablement and read-only task-MCP readiness."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from . import platform_support as platform
from .config import ConfigError, load_config
from .setup_runtime import (
    Child, SetupError, create_parents, document, executable, private_directory,
    private_parent, require, task_command,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupError("invalid_invocation")


def parser():
    result = _Parser(prog="mempalace-tasks setup", allow_abbrev=False,
                     description="Explicit setup; never installs packages or resets task history.")
    result.add_argument("mode", choices=("configure", "enable", "check"))
    result.add_argument("--config", help="Absolute schema-2 configuration (required)")
    result.add_argument("--task-executable", help="Absolute native executable or Python .py entry")
    result.add_argument("--copilot-executable", help="Absolute native Copilot executable or .py entry")
    result.add_argument("--hub-url", help="Numeric loopback HTTP(S) hub endpoint (configure only)")
    result.add_argument("--hub-token-file", help="Existing private hub credential (configure only)")
    result.add_argument("--new-authority", action="store_true")
    result.add_argument("--initialize", action="store_true")
    result.add_argument("--generate-token", action="store_true")
    result.add_argument("--register-copilot", action="store_true")
    return result


def parse_arguments(argv):
    names = [value.split("=", 1)[0] for value in argv if value.startswith("--")]
    require(len(names) == len(set(names)), "duplicate_option")
    args = parser().parse_args(argv)
    require(args.mode == "configure" or not (
        args.new_authority or args.hub_url or args.hub_token_file), "invalid_invocation")
    require(args.mode == "enable" or not (
        args.initialize or args.generate_token or args.register_copilot), "invalid_invocation")
    require(bool(args.hub_url) == args.new_authority, "new_authority_pair_required")
    require(not args.generate_token or args.initialize, "initialize_required")
    require(args.config is not None, "config_required")
    for name in ("config", "task_executable", "copilot_executable", "hub_token_file"):
        value = getattr(args, name)
        if value is not None:
            require(Path(value).is_absolute() and str(Path(value)) == value
                    and ".." not in Path(value).parts
                    and not any(ord(character) < 32 for character in value),
                    "absolute_path_required")
    return args


def _identity(value, code="invalid_identity"):
    try:
        identity = UUID(value)
        require(identity.int != 0 and str(identity) == value, code)
        return value
    except (ValueError, TypeError, AttributeError):
        raise SetupError(code) from None


def _read(path):
    private_parent(path)
    try:
        data = platform.read_regular(path, private=True)
    except platform.PlatformError as error:
        code = {"not_found": "missing_config", "unsafe_file": "malformed_config",
                "file_too_large": "config_too_large"}.get(error.code, error.code)
        raise SetupError(code) from None
    result = document(data, "malformed_config")
    require(type(result.get("schema_version")) is int and result["schema_version"] == 2,
            "unsupported_schema")
    return result


def _validate(command, path, allow_missing):
    # Always use the package's native validator, and validate the selected CLI
    # too: an explicit override must not silently point at an incompatible build.
    try:
        config = load_config(path, allow_missing_service_token=allow_missing)
    except ConfigError as error:
        raise SetupError("missing_credential" if error.code == "not_found" else "invalid_config") from None
    _identity(config.authority_id)
    private_parent(config.runtime_dir / ".setup-readonly")
    help_result = command.run("--help")
    require(help_result.returncode == 0 and "validate-config" in help_result.stdout,
            "validator_unavailable")
    result = command.run("validate-config", "--config", str(path),
                         *(("--allow-missing-service-token",) if allow_missing else ()))
    require(result.returncode == 0, "invalid_config")
    validated = document(result.stdout)
    require(validated.get("ok") is True and validated.get("schema_version") == 2
            and validated.get("authority_id") == config.authority_id, "invalid_config")
    return config


def _template(args):
    try:
        hub = urlsplit(args.hub_url)
        require(hub.scheme in {"http", "https"} and ipaddress.ip_address(hub.hostname).is_loopback
                and hub.username is None and hub.password is None and not hub.query
                and not hub.fragment and hub.port != 0 and "\\" not in args.hub_url
                and not any(c.isspace() for c in args.hub_url), "loopback_hub_required")
    except (ValueError, TypeError):
        raise SetupError("loopback_hub_required") from None
    identity = str(uuid4())
    directory = Path(args.config).parent
    result = {
        "schema_version": 2, "authority_id": identity, "hub_url": args.hub_url,
        "service_token_file": str(directory / f"task-token-{identity}"),
        "runtime_dir": str(directory / f"task-runtime-{identity}"),
        "lifecycle": "launcher", "host": "127.0.0.1", "port": 0,
        "maintenance_actor": "system", "recovery_actor": "operator",
        "genesis": {
            "actor": "operator",
            "actors": {actor: actor for actor in
                       ("operator", "coordinator", "worker", "supervisor", "system")},
            "execution_profiles": {"local": {"execution_class": "isolated"}},
            "supervisors": {"supervisor": {"profiles": ["local"], "workers": ["worker"]}},
        },
    }
    if args.hub_token_file:
        result["hub_token_file"] = args.hub_token_file
    return result


def configure(command, args):
    path = private_parent(args.config)
    if path.exists():
        value = _read(path)
        for key in ("hub_url", "hub_token_file"):
            supplied = getattr(args, key)
            require(supplied is None or value.get(key) == supplied, "config_input_mismatch")
        return _validate(command, path, True)
    require(args.new_authority, "new_authority_pair_required")
    value = _template(args)
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    # OS-selected roots may contain system aliases (macOS /var -> /private/var).
    # Only this internal root is resolved; user configuration paths stay strict.
    root = platform.canonical_path(Path(tempfile.gettempdir()).resolve(strict=True))
    # A checkout-based invocation must not stage generated files in its source.
    package_root = Path(__file__).resolve().parents[3]
    if (package_root / "sidecar" / "pyproject.toml").is_file():
        require(not root.is_relative_to(package_root), "temporary_root_inside_repository")
    staging = root / ("mempalace-task-setup-" + uuid4().hex)
    platform.ensure_private_directory(staging)
    candidate = staging / "config.json"
    try:
        platform.create_private(candidate, data)
        config = _validate(command, candidate, True)
        create_parents(path.parent)
        private_directory(path.parent)
        platform.ensure_private_directory(config.runtime_dir)
        platform.create_private(path, data)
        return config
    finally:
        if candidate.exists():
            candidate.unlink()
        staging.rmdir()


def registration(task, copilot, args):
    result = copilot.run("mcp", "get", "mempalace-tasks", "--json")
    if result.returncode:
        require(result.returncode == 1 and result.stderr.startswith(
            'Error: Server "mempalace-tasks" not found.'), "registration_query_failed")
        return False
    value = document(result.stdout).get("mempalace-tasks")
    require(type(value) is dict, "registration_query_failed")
    supported = {"type", "command", "args", "tools", "enabled", "env", "headers",
                 "source", "timeout", "description"}
    require(not value.keys() - supported and value.get("env") in (None, {})
            and value.get("headers") in (None, {}), "custom_registration")
    prefix = list(task.argv[1:])
    variants = [
        ["mcp", "--config", args.config],
        ["mcp", "--config", args.config, "--timeout", "10s"],
        ["mcp", "--timeout", "10s", "--config", args.config],
    ]
    require(value.get("type") == "local" and value.get("command") == task.argv[0]
            and value.get("args") in [prefix + variant for variant in variants],
            "registration_mismatch")
    require(value.get("enabled", True) is True and value.get("tools") == ["*"],
            "restricted_registration")
    return True


def _capabilities(task):
    result = task.run("mcp", "--help")
    require(result.returncode == 0 and "--no-autostart" in result.stdout,
            "no_autostart_unavailable")


def _connected(result, identity):
    if result.returncode == 0:
        value = document(result.stdout)
        require(value.get("ready") is True, "owner_unhealthy")
        require(value.get("authority_id") == identity, "authority_mismatch")
        return True
    if result.stderr.startswith("registry_missing:"):
        return False
    raise SetupError("binding_mismatch" if result.stderr.startswith("binding_mismatch:")
                     else "owner_unreachable")


def probe(task, args, identity):
    with Child((*task.argv, "mcp", "--config", args.config, "--timeout", "10s",
                "--no-autostart")) as child:
        value = child.call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "mempalace-task-setup", "version": "1"},
        })
        require(value.get("protocolVersion") in {
            "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}, "unsupported_protocol")
        require(type(value.get("capabilities")) is dict
                and type(value["capabilities"].get("tools")) is dict, "missing_tools_capability")
        child.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        names, cursors, params = set(), set(), {}
        for _ in range(10):
            catalog = child.call("tools/list", params)
            require(type(catalog.get("tools")) is list, "invalid_tool_catalog")
            for tool in catalog["tools"]:
                require(type(tool) is dict, "invalid_tool_descriptor")
                name = tool.get("name")
                require(type(name) is str and re.fullmatch(r"mptask_[A-Za-z0-9_]+", name)
                        and name not in names, "invalid_tool_catalog")
                require(type(tool.get("description")) is str and tool["description"].strip()
                        and type(tool.get("inputSchema")) is dict
                        and tool["inputSchema"].get("type") == "object", "invalid_tool_descriptor")
                if "outputSchema" in tool:
                    require(type(tool["outputSchema"]) is dict
                            and tool["outputSchema"].get("type") == "object", "invalid_tool_descriptor")
                names.add(name)
            cursor = catalog.get("nextCursor")
            if cursor in (None, ""):
                break
            require(type(cursor) is str and cursor not in cursors, "repeated_cursor")
            cursors.add(cursor)
            params = {"cursor": cursor}
        else:
            raise SetupError("page_limit")
        require({f"mptask_{name}" for name in ("create", "expand", "goal_close", "snapshot", "health")}
                <= names, "missing_required_tool")
        value = child.call("tools/call", {"name": "mptask_health", "arguments": {}})
        health = value.get("structuredContent")
        if health is None:
            content = value.get("content")
            require(type(content) is list, "malformed_response")
            text = [item["text"] for item in content if type(item) is dict
                    and item.get("type") == "text" and type(item.get("text")) is str]
            health = document("\n".join(text))
        require(type(health) is dict, "malformed_response")
        require(health.get("authority_id") == identity, "authority_mismatch")
        _identity(health.get("epoch_id"), "invalid_epoch")
        require(health.get("fresh") is True, "health_not_fresh")
        require(type(health.get("maintenance")) is dict
                and health["maintenance"].get("ok") is True, "maintenance_unhealthy")
        child.finish_protocol()
        return len(names)


def _readiness(task, args, progress):
    _read(args.config)
    config = _validate(task, args.config, args.initialize and args.generate_token)
    if args.mode == "enable":
        _capabilities(task)
    copilot = executable("copilot", args.copilot_executable)
    registered = registration(task, copilot, args)
    require(args.mode != "check" or registered, "missing_registration")
    completed = progress["completed"]
    completed.append("preflight")
    progress["phase"] = "connect"
    result = task.run("connect", "--config", args.config, "--timeout", "10s")
    missing_token = (config.service_token is None and args.initialize and args.generate_token
                     and result.returncode != 0 and result.stderr.startswith("not_found:"))
    live = False if missing_token else _connected(result, config.authority_id)
    if args.mode == "check":
        require(live, "owner_not_running")
    elif not live:
        if args.initialize:
            progress["phase"] = "initialize"
            result = task.run("init", "--config", args.config,
                              *(("--generate-token",) if args.generate_token else ()))
            require(result.returncode == 0 and document(result.stdout).get("ok") is True,
                    "initialization_failed")
            completed.append("initialize")
        progress["phase"] = "start"
        result = task.run("start", "--config", args.config, "--timeout", "10s")
        require(result.returncode == 0, "owner_start_failed")
        require(_connected(result, config.authority_id), "owner_not_running")
        completed.append("start")
    completed.append("connected")
    progress["phase"] = "registration"
    if args.mode == "enable" and args.register_copilot and not registered:
        if not registration(task, copilot, args):
            result = copilot.run("mcp", "add", "mempalace-tasks", "--",
                                 *task.argv, "mcp", "--config", args.config)
            require(result.returncode == 0, "registration_add_failed")
        registered = registration(task, copilot, args)
        require(registered, "registration_add_failed")
        completed.append("registration")
    progress["phase"] = "stdio"
    if args.mode == "check":
        _capabilities(task)
    count = probe(task, args, config.authority_id)
    completed.append("stdio")
    return {
        "ok": True, "mode": args.mode, "phase": "ready", "completed": completed,
        "authority_id": config.authority_id, "service_ready": True, "tool_count": count,
        "registration_verified": registered,
        "current_session": "reload_required_if_tools_not_visible",
        "native_supervision": "unavailable_not_assessed",
    }


def _code(error):
    if isinstance(error, (SetupError, platform.PlatformError)):
        return error.code
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return "deadline_exceeded"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError):
        return "filesystem_or_pipe_error"
    return "invalid_response"


def _emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


def _remedy(code):
    return {
        "unsupported_schema": "Schema 1 is not migrated. Preserve history/backups; select a new independent config.",
        "malformed_config": "Repair the selected config with the native validator; never reset history.",
        "invalid_config": "Repair the selected config with the native validator; never reset history.",
        "validator_unavailable": "Select a prepared task CLI with validate-config support; setup never installs.",
        "no_autostart_unavailable": "Select a prepared task CLI with mcp --no-autostart support; setup never installs.",
        "missing_executable": "Select an installed native executable or explicit Python entry; setup never installs.",
        "unsafe_executable": "Batch files are not executed. Select a native executable (.exe on Windows) or Python .py entry.",
        "unsupported_child_control": "Native child control is required; Windows needs the packaged pywin32 dependency.",
        "registration_mismatch": "Review/remove only mempalace-tasks explicitly, then rerun with --register-copilot.",
        "restricted_registration": "Review/remove only mempalace-tasks explicitly, then rerun with --register-copilot.",
        "custom_registration": "Review/remove only mempalace-tasks explicitly, then rerun with --register-copilot.",
        "missing_credential": "Check credential paths; --initialize --generate-token creates missing service tokens.",
        "owner_not_running": "Use enable deliberately; only --initialize authorizes creating/verifying genesis.",
        "binding_mismatch": "Resolve the selected binding with native tooling; do not reset registry/history.",
        "initialization_failed": "Inspect native authority/hub diagnostics. Accepted genesis is never undone by setup.",
        "registration_add_failed": "Owner/genesis may be ready. Repair registration and rerun; no rollback occurred.",
    }.get(code, "Resolve the reported phase and rerun. No history or unrelated owner is undone; setup never installs.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_arguments(argv)
    except SystemExit as exit:
        return int(exit.code)
    except SetupError as error:
        print(error.code + ": See sidecar/setup.md for supported command forms.", file=sys.stderr)
        _emit({"ok": False, "phase": "arguments", "code": error.code})
        return 2
    progress = {"phase": "preflight", "completed": []}
    try:
        task = task_command(args.task_executable)
        if args.mode == "configure":
            progress["phase"] = "configure"
            config = configure(task, args)
            result = {
                "ok": True, "mode": args.mode, "phase": "configure",
                "authority_id": config.authority_id, "service_ready": False,
                "registration_verified": False, "native_supervision": "unavailable_not_assessed",
            }
        else:
            result = _readiness(task, args, progress)
        _emit(result)
        return 0
    except Exception as error:
        code = _code(error)
        repair = _remedy(code)
        print(code + ": " + repair, file=sys.stderr)
        _emit({"ok": False, "mode": args.mode, **progress, "code": code, "repair": repair})
        return 1
