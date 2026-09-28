"""Exercise the repository harness in disposable, external pytest projects."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


REPOSITORY = Path(__file__).resolve().parents[1]
ROOT_VARIABLES = ("TMPDIR", "TEMP", "TMP", "DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR")
PALACE_VARIABLES = (
    "MEMPALACE_PALACE_PATH",
    "MEMPAL_PALACE_PATH",
    "MEMPALACE_DAEMON_STATE_ROOT",
)


@pytest.fixture
def project(pytester, monkeypatch):
    external = pytester.path.parent / f"{pytester.path.name}-external"
    external.mkdir()
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for name in ROOT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(external))
    tests = pytester.path / "tests"
    tests.mkdir()
    for relative in ("pytest.ini", "tests/conftest.py", "tests/mempalace-backup/conftest.py"):
        source = REPOSITORY / relative
        if source.exists():
            destination = pytester.path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return pytester, external


def write_test(project, relative, source):
    pytester, _ = project
    path = pytester.path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def run_project(project, *arguments):
    pytester, external = project
    return pytester.runpytest_subprocess(
        "--basetemp", str(external / "run"), "-q", *arguments, timeout=60,
    )


def test_default_discovery_runs_functions_and_unittest_only_under_tests(project):
    write_test(project, "tests/test_mixed.py", """
        import unittest

        def test_function():
            assert True

        class Example(unittest.TestCase):
            def test_case(self):
                self.assertTrue(True)
    """)
    write_test(project, "production/test_not_deployed.py", """
        def test_unexpected():
            raise AssertionError("production tests must not be collected")
    """)
    run_project(project).assert_outcomes(passed=2)


def test_defaults_reach_functions_unittest_and_child_processes(project, monkeypatch):
    monkeypatch.setenv("EXPECTED_HOME", os.environ.get("HOME", ""))
    write_test(project, "tests/test_roots.py", """
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        import unittest

        def check_roots():
            repository = Path(__file__).resolve().parents[1]
            roots = [Path(os.environ[name]).resolve() for name in
                     ("TMPDIR", "TEMP", "TMP", "DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR")]
            assert all(root.is_dir() and not root.is_relative_to(repository) for root in roots)
            assert len(set(roots)) == 1
            assert Path(tempfile.gettempdir()).resolve() == roots[0]
            assert os.environ.get("HOME", "") == os.environ["EXPECTED_HOME"]
            assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
            result = subprocess.run(
                [sys.executable, "-c",
                 "import os,tempfile; print(tempfile.gettempdir()); "
                 "print(os.environ['PYTHONDONTWRITEBYTECODE'])"],
                text=True, capture_output=True, check=True, timeout=10,
            )
            assert result.stdout.splitlines() == [str(roots[0]), "1"]

        def test_function():
            check_roots()

        class Example(unittest.TestCase):
            def test_case(self):
                check_roots()
    """)
    run_project(project).assert_outcomes(passed=2)


def test_explicit_external_roots_are_preserved(project, monkeypatch):
    _, external = project
    for name in ("DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR"):
        root = external / name.lower()
        root.mkdir()
        monkeypatch.setenv(name, str(root))
        monkeypatch.setenv(f"EXPECTED_{name}", str(root))
    write_test(project, "tests/test_overrides.py", """
        import os

        def test_overrides():
            for name in ("DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR"):
                assert os.environ[name] == os.environ["EXPECTED_" + name]
    """)
    run_project(project).assert_outcomes(passed=1)


def test_sidecar_import_paths_reach_child_processes_without_leaking(project, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    write_test(project, "sidecar/src/checkout_probe.py", "VALUE = 'checkout source'\n")
    write_test(project, "tests/sidecar/worker_probe.py", """
        import checkout_probe

        print(checkout_probe.VALUE)
    """)
    write_test(project, "tests/sidecar/test_child_import.py", """
        import subprocess
        import sys
        import checkout_probe

        def test_child_import():
            assert checkout_probe.VALUE == "checkout source"
            child = subprocess.run(
                [sys.executable, "-m", "worker_probe"],
                text=True, capture_output=True, timeout=10, check=True,
            )
            assert child.stdout.strip() == "checkout source"
    """)
    write_test(project, "tests/test_after_sidecar.py", """
        import os
        from pathlib import Path

        def test_pythonpath_does_not_leak_to_other_suites():
            # pytester supplies the fake project root to its subprocess.
            assert os.environ["PYTHONPATH"] == str(Path(__file__).resolve().parents[1])
    """)
    run_project(project).assert_outcomes(passed=2)


def test_session_teardown_restores_environment_and_tempfile_cache(project):
    pytester, _ = project
    write_test(project, "tests/test_active.py", """
        import os
        import tempfile

        def test_active():
            assert os.environ["DREAMING_TEST_TMPDIR"] == tempfile.gettempdir()
            assert os.environ["MPTASK_TEST_TMPDIR"] == tempfile.gettempdir()
    """)
    hooks = textwrap.dedent("""
        import os
        import tempfile

        _variables = ("TMPDIR", "TEMP", "TMP", "DREAMING_TEST_TMPDIR", "MPTASK_TEST_TMPDIR",
                      "PYTHONPATH", "PYTHONDONTWRITEBYTECODE")
        _before = None
        _cached = None

        def pytest_sessionstart(session):
            global _before, _cached
            _before = {name: os.environ.get(name) for name in _variables}
            tempfile.tempdir = os.environ["TMPDIR"]
            _cached = tempfile.tempdir

        def pytest_sessionfinish(session, exitstatus):
            assert {name: os.environ.get(name) for name in _variables} == _before
            assert tempfile.tempdir == _cached
            print("session environment restored")
    """)
    conftest = pytester.path / "tests" / "conftest.py"
    with conftest.open("a", encoding="utf-8") as stream:
        stream.write("\n" + hooks)
    result = run_project(project)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*session environment restored*"])


@pytest.mark.parametrize("name", ("--basetemp", *ROOT_VARIABLES))
@pytest.mark.parametrize("alias", (False, True), ids=("direct", "symlink"))
def test_repository_roots_are_refused_before_basetemp_is_cleared(project, monkeypatch, name, alias):
    pytester, external = project
    unsafe = pytester.path / "precious"
    unsafe.mkdir()
    sentinel = unsafe / "keep.txt"
    sentinel.write_text("not disposable", encoding="utf-8")
    configured = unsafe
    if alias:
        configured = external / "alias"
        configured.symlink_to(unsafe, target_is_directory=True)
    disposable = external / "run"
    disposable.mkdir()
    untouched = disposable / "before-validation.txt"
    untouched.write_text("untouched", encoding="utf-8")
    write_test(project, "tests/test_never.py", """
        def test_never(tmp_path):
            raise AssertionError("invalid roots must fail before tests execute")
    """)
    if name == "--basetemp":
        result = run_project(project, "--basetemp", str(configured))
    else:
        monkeypatch.setenv(name, str(configured))
        result = run_project(project)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert name in result.stderr.str()
    assert "outside the repository" in result.stderr.str()
    assert sentinel.read_text(encoding="utf-8") == "not disposable"
    assert untouched.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("name", ROOT_VARIABLES)
@pytest.mark.parametrize("kind", ("missing", "file"))
def test_invalid_explicit_roots_are_refused(project, monkeypatch, name, kind):
    _, external = project
    invalid = external / "invalid"
    if kind == "file":
        invalid.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(name, str(invalid))
    write_test(project, "tests/test_never.py", """
        def test_never(tmp_path):
            raise AssertionError("invalid roots must fail before tests execute")
    """)
    result = run_project(project)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert name in result.stderr.str()


def test_basetemp_may_be_a_missing_external_child_but_not_a_file(project):
    _, external = project
    write_test(project, "tests/test_temp.py", """
        def test_temp(tmp_path):
            assert tmp_path.is_dir()
    """)
    run_project(project, "--basetemp", str(external / "new-child")).assert_outcomes(passed=1)
    invalid = external / "not-a-directory"
    invalid.write_text("preserve me", encoding="utf-8")
    result = run_project(project, "--basetemp", str(invalid))
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "--basetemp" in result.stderr.str()
    assert invalid.read_text(encoding="utf-8") == "preserve me"


def test_backup_isolation_is_per_test_and_does_not_change_other_suites(project, monkeypatch):
    _, external = project
    live = external / "live"
    live.mkdir()
    sentinel = live / "keep.txt"
    sentinel.write_text("live palace", encoding="utf-8")
    monkeypatch.setenv("HOME", str(live))
    monkeypatch.setenv("USERPROFILE", str(live))
    monkeypatch.setenv("EXPECTED_LIVE", str(live))
    for name in PALACE_VARIABLES:
        monkeypatch.setenv(name, str(live))
    write_test(project, "tests/mempalace-backup/test_backup_environment.py", """
        import os
        from pathlib import Path

        def check_isolation():
            home = Path(os.environ["HOME"])
            assert home == Path(os.environ["USERPROFILE"])
            assert home.is_dir()
            assert home != Path(os.environ["EXPECTED_LIVE"])
            for name in ("MEMPALACE_PALACE_PATH", "MEMPAL_PALACE_PATH",
                         "MEMPALACE_DAEMON_STATE_ROOT"):
                assert os.environ[name] == ""

        def test_first():
            check_isolation()
            os.environ["MEMPALACE_PALACE_PATH"] = "leaked by a wing test"
            (Path(os.environ["HOME"]) / "only-first").write_text("first")

        def test_second():
            check_isolation()
            assert not (Path(os.environ["HOME"]) / "only-first").exists()
    """)
    write_test(project, "tests/test_after_backup.py", """
        import os

        def test_outside_backup():
            for name in ("HOME", "USERPROFILE", "MEMPALACE_PALACE_PATH",
                         "MEMPAL_PALACE_PATH", "MEMPALACE_DAEMON_STATE_ROOT"):
                assert os.environ[name] == os.environ["EXPECTED_LIVE"]
    """)
    run_project(project).assert_outcomes(passed=3)
    assert list(live.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "live palace"


def test_native_fork_checks_are_isolated_from_other_suites_threads(tmp_path):
    module = tmp_path / "fork_probe.py"
    module.write_text(textwrap.dedent("""
        import os
        import threading
        import unittest
        from runtime_support import isolated_fork_test

        class ForkProbe(unittest.TestCase):
            @isolated_fork_test
            def test_runs_without_parent_threads(self):
                self.assertEqual(threading.active_count(), 1)

        if __name__ == "__main__":
            release = threading.Event()
            thread = threading.Thread(target=release.wait)
            thread.start()
            try:
                suite = unittest.defaultTestLoader.loadTestsFromName(
                    "fork_probe.ForkProbe.test_runs_without_parent_threads")
                result = unittest.TextTestRunner().run(suite)
            finally:
                release.set()
                thread.join()
            raise SystemExit(not result.wasSuccessful())
    """), encoding="utf-8")
    paths = (tmp_path, REPOSITORY / "tests" / "sidecar", REPOSITORY / "sidecar" / "src")
    result = subprocess.run(
        [sys.executable, str(module)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, paths))},
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
