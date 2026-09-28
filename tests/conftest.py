"""Shared import and temporary-storage policy for repository tests."""

import os
from pathlib import Path
import tempfile

import pytest


pytest_plugins = ["pytester"]

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT_VARIABLES = ("TMPDIR", "TEMP", "TMP", "DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR")


def _validate_root(name, value, *, may_create=False):
    try:
        path = Path(value).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise pytest.UsageError(f"{name} is not a valid temporary directory: {value!r}") from error
    if path.is_relative_to(REPOSITORY):
        raise pytest.UsageError(f"{name} must be outside the repository: {value!r}")
    if may_create and REPOSITORY.is_relative_to(path):
        raise pytest.UsageError(f"{name} must not contain the repository: {value!r}")
    if not path.is_dir() and not (may_create and not path.exists()):
        raise pytest.UsageError(f"{name} must be an existing directory: {value!r}")


def pytest_configure(config):
    # getbasetemp() deletes an explicit basetemp; validate before any fixture calls it.
    if config.option.basetemp is not None:
        _validate_root("--basetemp", config.option.basetemp, may_create=True)
    for name in ROOT_VARIABLES:
        if name in os.environ:
            _validate_root(name, os.environ[name])


@pytest.fixture(scope="session", autouse=True)
def external_test_environment(tmp_path_factory):
    root = str(tmp_path_factory.getbasetemp())
    _validate_root("pytest temporary root", root)
    with pytest.MonkeyPatch.context() as environment:
        for name in ("DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR"):
            if name not in os.environ:
                environment.setenv(name, root)
        for name in ("TMPDIR", "TEMP", "TMP"):
            environment.setenv(name, root)
        environment.setattr(tempfile, "tempdir", root)
        environment.setenv("PYTHONDONTWRITEBYTECODE", "1")
        yield


@pytest.fixture(autouse=True)
def sidecar_subprocess_imports(request, monkeypatch):
    if request.path.resolve().is_relative_to(REPOSITORY / "tests" / "sidecar"):
        # pytest's pythonpath does not reach -m children. Keep this sidecar-only:
        # MemPalace imports deliberately strip inherited PYTHONPATH from sys.path.
        paths = (REPOSITORY / "sidecar" / "src", REPOSITORY / "tests" / "sidecar")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, paths)))
