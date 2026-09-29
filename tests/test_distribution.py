"""Build real sidecar artifacts offline and verify their deployment boundary."""

from configparser import ConfigParser
from email.parser import Parser
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
TEST_ASSETS = {
    "authority_fixture.py",
    "portable_lifecycle_fixture.py",
    "runtime_support.py",
    "runtime_worker.py",
    "service_fixture.py",
    "conftest.py",
    "pytest.ini",
    "requirements-test.txt",
}


def build(source, output, *, wheel_only=False):
    command = [
        "uv", "build", "--offline", "--no-build-isolation",
        "--python", sys.executable, "--out-dir", str(output),
    ]
    if wheel_only:
        command.append("--wheel")
    command.append(str(source))
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        command, cwd=output.parent, env=environment,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    root = tmp_path_factory.mktemp("sidecar-distributions")
    source = root / "sidecar"
    shutil.copytree(
        REPOSITORY / "sidecar", source,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.egg-info", "build", "dist"),
    )
    output = root / "dist"
    build(source, output)
    wheels = list(output.glob("*.whl"))
    sources = list(output.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, list(output.iterdir())
    rebuilt = root / "rebuilt"
    build(sources[0], rebuilt, wheel_only=True)
    rebuilt_wheels = list(rebuilt.glob("*.whl"))
    assert len(rebuilt_wheels) == 1, list(rebuilt.iterdir())
    return wheels[0], sources[0], rebuilt_wheels[0]


def assert_no_test_assets(names):
    unexpected = []
    for name in names:
        path = PurePosixPath(name)
        if (
            "tests" in path.parts
            or ".pytest_cache" in path.parts
            or path.name in TEST_ASSETS
            or (path.suffix == ".py" and (
                path.name.startswith("test_") or path.name.endswith("_test.py")
            ))
        ):
            unexpected.append(name)
    assert not unexpected, unexpected


def assert_runtime_wheel(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert_no_test_assets(names)
        assert {
            "mempalace_tasks/__init__.py",
            "mempalace_tasks/__main__.py",
            "mempalace_tasks/cli.py",
            "mempalace_tasks/setup.py",
            "mempalace_tasks/setup_runtime.py",
        } <= set(names)
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points) == 1, entry_points
        parser = ConfigParser()
        parser.read_string(archive.read(entry_points[0]).decode())
        assert parser["console_scripts"]["mempalace-tasks"] == "mempalace_tasks.cli:main"
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_files) == 1, metadata_files
        metadata = Parser().parsestr(archive.read(metadata_files[0]).decode())
        assert not any(
            requirement.lower().startswith("pytest")
            for requirement in metadata.get_all("Requires-Dist", [])
        )


def test_wheel_and_sdist_rebuilt_wheel_are_runtime_only(distributions):
    wheel, _, rebuilt_wheel = distributions
    assert_runtime_wheel(wheel)
    assert_runtime_wheel(rebuilt_wheel)


def test_source_distribution_is_test_free_and_buildable(distributions):
    _, source, _ = distributions
    with tarfile.open(source, "r:gz") as archive:
        names = archive.getnames()
    assert_no_test_assets(names)
    relative = {str(PurePosixPath(*PurePosixPath(name).parts[1:])) for name in names}
    assert {
        "README.md",
        "setup.md",
        "requirements.lock",
        "pyproject.toml",
        "src/mempalace_tasks/__init__.py",
        "src/mempalace_tasks/cli.py",
        "src/mempalace_tasks/setup.py",
        "src/mempalace_tasks/setup_runtime.py",
    } <= relative
