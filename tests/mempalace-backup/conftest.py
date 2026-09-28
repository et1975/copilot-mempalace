"""Preserve the standalone backup runner's per-test palace isolation."""

import pytest


@pytest.fixture(autouse=True)
def isolated_backup_environment(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in (
        "MEMPALACE_PALACE_PATH",
        "MEMPAL_PALACE_PATH",
        "MEMPALACE_DAEMON_STATE_ROOT",
    ):
        monkeypatch.setenv(name, "")
