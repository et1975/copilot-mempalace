"""Real-filesystem tests for optional Linux worker artifact JSON."""

import copy
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from mempalace_tasks.journal import JournalError, JsonStore


def same_inode(fd, path):
    actual, expected = os.fstat(fd), path.stat()
    return (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        # The controller supplies a session-workspace path for fixture files.
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-journal-", dir=os.environ.get("MPTASK_TEST_TMPDIR", ".")
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).absolute()
        self.state = self.root / "state"

    def assert_error(self, code, action):
        with self.assertRaises(JournalError) as raised:
            action()
        self.assertEqual(code, raised.exception.code)
        return raised.exception


class JsonStoreTests(JournalTestCase):
    def test_retry_resyncs_existing_directory_entry_after_mkdir_sync_failure(self):
        fsync = os.fsync
        parent_info = self.root.stat()

        def fail_parent_sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino):
                raise OSError(errno.EIO, "parent directory sync")
            fsync(fd)

        with mock.patch("mempalace_tasks.journal.os.fsync", side_effect=fail_parent_sync):
            self.assert_error(
                "io_error", lambda: JsonStore(self.state / "value.json").write({"v": 1})
            )
            self.assertTrue(self.state.is_dir())
            self.assert_error(
                "io_error", lambda: JsonStore(self.state / "value.json").write({"v": 1})
            )
        self.assertFalse((self.state / "value.json").exists())
        JsonStore(self.state / "value.json").write({"v": 1})
        self.assertEqual({"v": 1}, JsonStore(self.state / "value.json").read())

    def test_missing_read_uses_default_without_creating_state(self):
        store = JsonStore(self.state / "checkpoint.json")
        self.assertIsNone(store.read())
        self.assertEqual({"checkpoint": None}, store.read({"checkpoint": None}))
        self.assertFalse(self.state.exists())

    def test_round_trips_worker_observation_and_process_handle_json(self):
        value = {
            "observation": {"at": "2026-09-23T20:00:00Z", "elapsed": 12.125},
            "handles": [{"pid": 100, "start": "opaque", "stopped": False}],
        }
        store = JsonStore(self.state / "runtime.json")
        store.write(value)
        read = store.read()
        self.assertEqual(value, read)
        read["handles"].clear()
        self.assertEqual(value, store.read())
        self.assertEqual(value, JsonStore(store.path).read())

    def test_json_scalars_and_null_remain_distinct_from_missing_default(self):
        store = JsonStore(self.state / "value.json")
        for value in (None, False, 0, 1.5, "text", [], {}):
            with self.subTest(value=value):
                store.write(value)
                self.assertEqual(value, store.read(default="missing"))

    def test_directory_and_files_are_private_even_under_permissive_umask(self):
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        store = JsonStore(self.state / "nested" / "value.json")
        store.write({"private": True})
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(store.path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(store.path.stat().st_mode))

    def test_existing_state_permissions_are_restricted(self):
        self.state.mkdir(mode=0o755)
        path = self.state / "value.json"
        path.write_text('{"private":true}', encoding="utf-8")
        path.chmod(0o644)
        self.assertEqual({"private": True}, JsonStore(path).read())
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_malformed_duplicate_key_and_nonfinite_json_fail_closed(self):
        self.state.mkdir()
        path = self.state / "value.json"
        for data in (
            b"", b"{", b'{"x":1,"x":2}', b"NaN", b"1e999", b"\xff",
            b'"\\ud800"', b'{"\\ud800":true}',
        ):
            with self.subTest(data=data):
                path.write_bytes(data)
                store = JsonStore(path)
                self.assert_error("corrupt_state", lambda: store.read({"default": True}))
                self.assert_error("corrupt_state", lambda: store.write({"new": True}))
                self.assert_error("corrupt_state", store.clear)
                self.assertEqual(data, path.read_bytes())

    def test_non_json_inputs_do_not_replace_durable_value(self):
        store = JsonStore(self.state / "value.json")
        store.write({"old": True})
        for value in ({1: "non-string"}, (1, 2), float("nan"), float("inf"), object()):
            with self.subTest(value=repr(value)):
                self.assert_error("invalid_state", lambda: store.write(value))
                self.assertEqual({"old": True}, store.read())

    def test_target_symlinks_are_rejected_without_touching_the_target(self):
        self.state.mkdir()
        target = self.root / "outside.json"
        target.write_text('{"untouched":true}', encoding="utf-8")
        path = self.state / "value.json"
        path.symlink_to(target)
        store = JsonStore(path)
        for action in (store.read, lambda: store.write({}), store.clear):
            self.assert_error("unsafe_path", action)
        self.assertEqual('{"untouched":true}', target.read_text(encoding="utf-8"))
        self.assertTrue(path.is_symlink())

    def test_dangling_symlink_and_symlinked_parent_are_rejected(self):
        self.state.symlink_to(self.root / "missing", target_is_directory=True)
        store = JsonStore(self.state / "value.json")
        self.assert_error("unsafe_path", store.read)
        self.assert_error("unsafe_path", lambda: store.write({}))
        self.assertFalse((self.root / "missing").exists())

    def test_existing_parent_symlink_is_not_resolved_before_validation(self):
        real = self.root / "real"
        real.mkdir()
        self.state.symlink_to(real, target_is_directory=True)
        store = JsonStore(self.state / "value.json")
        self.assert_error("unsafe_path", lambda: store.write({}))
        self.assertEqual([], list(real.iterdir()))

    def test_nonregular_state_and_hardlinks_are_rejected(self):
        self.state.mkdir()
        path = self.state / "value.json"
        path.mkdir()
        self.assert_error("unsafe_path", JsonStore(path).read)
        path.rmdir()
        target = self.root / "outside.json"
        target.write_text("{}", encoding="utf-8")
        os.link(target, path)
        self.assert_error("unsafe_path", JsonStore(path).read)
        self.assert_error("unsafe_path", lambda: JsonStore(path).write({}))
        self.assertEqual("{}", target.read_text(encoding="utf-8"))

    def test_permission_failure_is_explicit_not_treated_as_missing(self):
        store = JsonStore(self.state / "value.json")
        with mock.patch(
            "mempalace_tasks.journal.os.open",
            side_effect=PermissionError(errno.EACCES, "denied"),
        ):
            self.assert_error("io_error", store.read)

    def test_filesystem_root_is_not_a_private_state_directory(self):
        aliases = (
            Path("/") / "journal-test-must-not-create.json",
            Path.cwd() / Path(*([".."] * (len(Path.cwd().parts) - 1)))
            / "journal-test-must-not-create.json",
        )
        with mock.patch(
            "mempalace_tasks.journal.os.open",
            side_effect=AssertionError("must reject root before I/O"),
        ):
            for alias in aliases:
                with self.subTest(path=str(alias)):
                    self.assert_error("unsafe_path", JsonStore(alias).read)

    def test_write_orders_file_sync_replace_and_directory_sync(self):
        self.state.mkdir()
        store = JsonStore(self.state / "value.json")
        operations = []
        fsync = os.fsync
        replace = os.replace

        def traced_sync(fd):
            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            if kind == "file" or same_inode(fd, self.state):
                operations.append(kind)
            fsync(fd)

        def traced_replace(source, destination, *args, **kwargs):
            operations.append("replace")
            replace(source, destination, *args, **kwargs)

        with mock.patch("mempalace_tasks.journal.os.fsync", side_effect=traced_sync):
            with mock.patch("mempalace_tasks.journal.os.replace", side_effect=traced_replace):
                store.write({"new": True})
        self.assertEqual(["file", "replace", "directory"], operations)
        self.assertEqual({"new": True}, store.read())
        self.assertEqual({"value.json"}, {path.name for path in self.state.iterdir()})

    def test_partial_writes_are_completed_before_replacement(self):
        store = JsonStore(self.state / "value.json")
        write = os.write

        def short_write(fd, data):
            return write(fd, data[:3])

        with mock.patch("mempalace_tasks.journal.os.write", side_effect=short_write):
            store.write({"long_value": "must survive partial system calls"})
        self.assertEqual(
            {"long_value": "must survive partial system calls"}, store.read()
        )

    def test_zero_progress_write_fails_without_discarding_previous_value(self):
        store = JsonStore(self.state / "value.json")
        store.write({"old": True})
        with mock.patch("mempalace_tasks.journal.os.write", return_value=0):
            self.assert_error("io_error", lambda: store.write({"new": True}))
        self.assertEqual({"old": True}, store.read())

    def test_symlink_before_parent_traversal_is_not_lexically_normalized_away(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        store = JsonStore(link / ".." / "value.json")
        self.assert_error("unsafe_path", lambda: store.write({}))
        self.assertFalse((self.root / "value.json").exists())

    def test_clear_syncs_directory_after_unlink_and_reports_ambiguity(self):
        store = JsonStore(self.state / "value.json")
        store.write({"old": True})
        fsync = os.fsync

        def failed_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                self.assertFalse(store.path.exists())
                raise OSError(errno.EIO, "directory sync")
            fsync(fd)

        with mock.patch("mempalace_tasks.journal.os.fsync", side_effect=failed_directory_sync):
            error = self.assert_error("io_error", store.clear)
        self.assertTrue(error.ambiguous)

    def test_clear_removes_only_the_named_valid_file(self):
        store = JsonStore(self.state / "value.json")
        store.write({"old": True})
        neighbor = self.state / "value.json.do-not-clean"
        neighbor.write_text("untouched", encoding="utf-8")
        store.clear()
        self.assertIsNone(store.read())
        self.assertEqual("untouched", neighbor.read_text(encoding="utf-8"))
        store.clear()
