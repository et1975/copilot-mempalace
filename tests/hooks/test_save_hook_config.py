"""Regression coverage for the shipped Copilot save-hook registration."""

import json
import os
from pathlib import Path
import subprocess

import pytest


CONFIG_PATH = Path(__file__).resolve().parents[2] / "hooks" / "mempalace-save.json"


@pytest.fixture
def save_hook_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_save_hook_uses_supported_config_version(save_hook_config):
    assert save_hook_config.get("version") == 1


@pytest.mark.parametrize("event", ["Stop", "PreCompact", "SessionEnd"])
def test_save_lifecycle_events_register_the_same_installed_adapter(
    save_hook_config, event
):
    assert save_hook_config["hooks"].get(event) == [
        {
            "type": "command",
            "command": 'python3 "${COPILOT_HOME:-$HOME/.copilot}/hooks/copilot_transcript.py"',
            "cwd": ".",
            "timeout": 30,
        }
    ]


@pytest.mark.parametrize(
    "copilot_home",
    [None, "", "custom home ' \" $(false)"],
    ids=["default-home", "empty-home-fallback", "quoted-custom-home"],
)
def test_registered_exit_command_resolves_installed_adapter(
    save_hook_config, tmp_path, copilot_home
):
    home = tmp_path / "user home"
    install_root = tmp_path / copilot_home if copilot_home else home / ".copilot"
    adapter = install_root / "hooks" / "copilot_transcript.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("print(__file__)\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home)}
    env.pop("COPILOT_HOME", None)
    if copilot_home is not None:
        env["COPILOT_HOME"] = str(install_root) if copilot_home else ""

    result = subprocess.run(
        ["/bin/sh", "-c", save_hook_config["hooks"]["SessionEnd"][0]["command"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(adapter)
