"""Keep test-only assets outside deployable source trees."""

from collections import Counter
import importlib.util
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
SUPPORT_FILES = {
    "authority_fixture.py",
    "portable_lifecycle_fixture.py",
    "runtime_support.py",
    "runtime_worker.py",
    "service_fixture.py",
}


def test_deployable_trees_do_not_contain_test_assets():
    misplaced = []
    for directory in ("hooks", "skills", "sidecar/src", "sidecar/tests"):
        root = REPOSITORY / directory
        for path in root.rglob("*"):
            if path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            if (
                path.name in SUPPORT_FILES
                or (path.is_file() and path.match("test_*.py"))
                or (path.is_dir() and path.name == "tests")
            ):
                misplaced.append(str(path.relative_to(REPOSITORY)))
    assert not misplaced, f"Test assets in deployable trees: {misplaced}"


def test_test_module_basenames_are_unique():
    counts = Counter(path.name for path in (REPOSITORY / "tests").rglob("test_*.py"))
    assert {name: count for name, count in counts.items() if count > 1} == {}


def test_test_directories_are_not_packages():
    assert list((REPOSITORY / "tests").rglob("__init__.py")) == []


@pytest.mark.parametrize(
    ("module", "relative_path"),
    [
        ("copilot_transcript", "hooks/copilot_transcript.py"),
        ("mempalace_tasks", "sidecar/src/mempalace_tasks/__init__.py"),
        ("dream_procedure", "skills/dreaming/scripts/dream_procedure.py"),
        ("dream_procedural_sources", "skills/dreaming/scripts/dream_procedural_sources.py"),
        ("dream_procedural_drafts", "skills/dreaming/scripts/dream_procedural_drafts.py"),
        ("procedural_context", "hooks/procedural_context.py"),
        ("palace_backup", "skills/mempalace-backup/scripts/palace_backup.py"),
        ("palace_wing", "skills/mempalace-backup/scripts/palace_wing.py"),
        ("palace_wing_lib", "skills/mempalace-backup/scripts/palace_wing_lib.py"),
        ("authority_fixture", "tests/sidecar/authority_fixture.py"),
        ("test_snapshot", "tests/sidecar/test_snapshot.py"),
        ("test_dream_procedural_palace", "tests/dreaming/test_dream_procedural_palace.py"),
    ],
)
def test_imports_resolve_to_checkout(module, relative_path):
    specification = importlib.util.find_spec(module)
    assert specification is not None
    assert Path(specification.origin).resolve() == REPOSITORY / relative_path


def test_sidecar_workers_stay_beside_their_consumers():
    sidecar = REPOSITORY / "tests" / "sidecar"
    for name in SUPPORT_FILES | {"test_execution.py", "test_cli.py"}:
        assert (sidecar / name).is_file()
