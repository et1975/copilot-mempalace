"""Real-filesystem and process tests for the local recovery journal."""

import copy
import errno
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import warnings

from mempalace_tasks.journal import (
    AuthorityLock,
    HeadStore,
    JournalError,
    JsonStore,
    PendingStore,
)


PROPOSAL = {
    "authority_id": "authority-1",
    "command_id": "command-1",
    "command": {"operation": "update", "patch": {"hold_reason": None}},
    "decision_time": "2026-09-23T20:00:00Z",
    "payload_hash": "proposal-hash",
    "response": {"title": "Exact \u2603 text", "items": [1, True, None]},
}
SETTLEMENT = {
    "kind": "settlement",
    "command_id": "command-1",
    "proposal_hash": "proposal-hash",
    "control_id": "control-1",
}
HEAD = {
    "raw_cursor": "event-2",
    "raw_hash": "raw-hash-2",
    "domain_head": "proposal-hash",
    "domain_ordinal": 1,
}


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

    def test_startup_resyncs_existing_ancestor_entries(self):
        self.state.mkdir()
        fsync = os.fsync
        parent_info = self.root.stat()

        def fail_parent_sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino):
                raise OSError(errno.EIO, "parent directory sync")
            fsync(fd)

        with mock.patch("mempalace_tasks.journal.os.fsync", side_effect=fail_parent_sync):
            self.assert_error(
                "io_error", AuthorityLock(self.state, "authority-1").acquire
            )
        self.assertFalse((self.state / "authority.lock").exists())

    def test_missing_read_uses_default_without_creating_state(self):
        store = JsonStore(self.state / "clock.json")
        self.assertIsNone(store.read())
        self.assertEqual({"clock": None}, store.read({"clock": None}))
        self.assertFalse(self.state.exists())

    def test_round_trips_generic_clock_and_process_handle_json(self):
        value = {
            "clock": {"wall": "2026-09-23T20:00:00Z", "monotonic": 12.125},
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


class PendingStoreTests(JournalTestCase):
    def test_exact_proposal_and_settlement_survive_reopen(self):
        store = PendingStore(self.state)
        proposal = copy.deepcopy(PROPOSAL)
        store.write(proposal)
        proposal["command"]["patch"]["hold_reason"] = "changed"
        self.assertEqual(
            {"proposal": PROPOSAL, "settlement": None}, PendingStore(self.state).read()
        )
        settlement = copy.deepcopy(SETTLEMENT)
        store.attach_settlement(settlement)
        settlement["control_id"] = "changed"
        self.assertEqual(
            {"proposal": PROPOSAL, "settlement": SETTLEMENT},
            PendingStore(self.state).read(),
        )

    def test_pending_proposal_cannot_be_replaced_or_implicitly_cleared(self):
        store = PendingStore(self.state)
        store.write(PROPOSAL)
        store.attach_settlement(SETTLEMENT)
        store.write(copy.deepcopy(PROPOSAL))
        self.assertEqual(SETTLEMENT, store.read()["settlement"])
        other = dict(PROPOSAL, command_id="different")
        self.assert_error("pending_conflict", lambda: store.write(other))
        self.assertEqual(PROPOSAL, store.read()["proposal"])

    def test_settlement_is_idempotent_but_cannot_change(self):
        store = PendingStore(self.state)
        self.assert_error("missing_pending", lambda: store.attach_settlement(SETTLEMENT))
        store.write(PROPOSAL)
        store.attach_settlement(SETTLEMENT)
        store.attach_settlement(copy.deepcopy(SETTLEMENT))
        self.assert_error(
            "settlement_conflict",
            lambda: store.attach_settlement(dict(SETTLEMENT, control_id="different")),
        )
        self.assertEqual(SETTLEMENT, store.read()["settlement"])

    def test_json_numeric_types_cannot_silently_change_an_exact_payload(self):
        store = PendingStore(self.state)
        store.write({"value": 1})
        self.assert_error("pending_conflict", lambda: store.write({"value": True}))
        self.assert_error("pending_conflict", lambda: store.write({"value": 1.0}))
        self.assertEqual({"value": 1}, store.read()["proposal"])

    def test_corrupt_pending_shape_is_not_missing_and_is_not_discarded(self):
        path = self.state / "pending.json"
        for value in (None, [], {}, {"proposal": {}, "settlement": "bad"}):
            with self.subTest(value=value):
                JsonStore(path).write(value)
                store = PendingStore(self.state)
                self.assert_error("corrupt_state", store.read)
                self.assert_error("corrupt_state", store.clear)
                self.assert_error("corrupt_state", lambda: store.archive("confirmed"))
                self.assertEqual(value, JsonStore(path).read())

    def test_write_and_settlement_io_failures_preserve_exact_proposal(self):
        for operation in ("write", "file_fsync", "replace", "directory_fsync"):
            with self.subTest(operation=operation):
                state = self.root / operation
                store = PendingStore(state)
                store.write(PROPOSAL)
                original = (state / "pending.json").read_bytes()
                fsync = os.fsync

                def failed_sync(fd):
                    is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if (
                        operation == "file_fsync" and not is_directory
                        or operation == "directory_fsync" and same_inode(fd, state)
                    ):
                        raise OSError(errno.EIO, operation)
                    fsync(fd)

                target = {
                    "write": "os.write",
                    "replace": "os.replace",
                    "file_fsync": "os.fsync",
                    "directory_fsync": "os.fsync",
                }[operation]
                effect = failed_sync if "fsync" in operation else OSError(errno.EIO, operation)
                with mock.patch("mempalace_tasks.journal." + target, side_effect=effect):
                    self.assert_error("io_error", lambda: store.attach_settlement(SETTLEMENT))
                recovered = PendingStore(state).read()
                self.assertEqual(PROPOSAL, recovered["proposal"])
                if operation != "directory_fsync":
                    self.assertEqual(original, (state / "pending.json").read_bytes())
                else:
                    self.assertEqual(SETTLEMENT, recovered["settlement"])
                self.assertEqual({"pending.json"}, {path.name for path in state.iterdir()})

    def test_failed_first_persistence_cannot_be_reported_as_success(self):
        self.state.mkdir()
        store = PendingStore(self.state)
        with mock.patch(
            "mempalace_tasks.journal.os.fsync",
            side_effect=OSError(errno.EIO, "storage unavailable"),
        ):
            self.assert_error("io_error", lambda: store.write(PROPOSAL))
        self.assertIsNone(store.read())

    def test_identical_retry_must_reestablish_durability_after_sync_failure(self):
        store = PendingStore(self.state)
        store.write(PROPOSAL)
        fsync = os.fsync

        def failed_directory_sync(fd):
            if same_inode(fd, self.state):
                raise OSError(errno.EIO, "directory sync")
            fsync(fd)

        with mock.patch("mempalace_tasks.journal.os.fsync", side_effect=failed_directory_sync):
            first = self.assert_error("io_error", lambda: store.attach_settlement(SETTLEMENT))
            second = self.assert_error("io_error", lambda: store.attach_settlement(SETTLEMENT))
            third = self.assert_error("io_error", lambda: store.write(PROPOSAL))
        self.assertTrue(first.ambiguous)
        self.assertTrue(second.ambiguous)
        self.assertTrue(third.ambiguous)
        self.assertEqual(PROPOSAL, store.read()["proposal"])
        self.assertEqual(SETTLEMENT, store.read()["settlement"])
        store.attach_settlement(SETTLEMENT)

    def test_process_crash_leaves_complete_pending_at_replace_boundaries(self):
        for boundary in ("before_replace", "after_replace"):
            with self.subTest(boundary=boundary):
                state = self.root / boundary
                store = PendingStore(state)
                store.write(PROPOSAL)
                result = subprocess.run(
                    [
                        sys.executable, str(Path(__file__).absolute()),
                        "--crash-child", str(state), boundary,
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(91, result.returncode, result.stderr)
                expected = None if boundary == "before_replace" else SETTLEMENT
                self.assertEqual(
                    {"proposal": PROPOSAL, "settlement": expected}, store.read()
                )

    def test_archive_keeps_exact_record_and_reason_before_removing_pending(self):
        store = PendingStore(self.state)
        store.write(PROPOSAL)
        store.attach_settlement(SETTLEMENT)
        neighbor = self.state / "unrelated"
        neighbor.write_text("keep", encoding="utf-8")
        archived = store.archive("terminal outcome verified")
        self.assertEqual(self.state, archived.parent)
        self.assertEqual(
            {
                "reason": "terminal outcome verified",
                "pending": {"proposal": PROPOSAL, "settlement": SETTLEMENT},
            },
            JsonStore(archived).read(),
        )
        self.assertIsNone(store.read())
        self.assertEqual(0o600, stat.S_IMODE(archived.stat().st_mode))
        self.assertEqual("keep", neighbor.read_text(encoding="utf-8"))
        self.assertIsNone(store.archive("nothing pending"))

    def test_failed_archive_does_not_clear_pending(self):
        store = PendingStore(self.state)
        store.write(PROPOSAL)
        with mock.patch(
            "mempalace_tasks.journal.os.replace", side_effect=OSError(errno.EIO, "replace")
        ):
            self.assert_error("io_error", lambda: store.archive("confirmed"))
        self.assertEqual(PROPOSAL, store.read()["proposal"])

    def test_archive_clear_failure_leaves_durable_audit_and_pending(self):
        store = PendingStore(self.state)
        store.write(PROPOSAL)
        unlink = os.unlink

        def fail_pending_unlink(path, *args, **kwargs):
            if Path(path).name == "pending.json":
                raise OSError(errno.EIO, "unlink")
            return unlink(path, *args, **kwargs)

        with mock.patch("mempalace_tasks.journal.os.unlink", side_effect=fail_pending_unlink):
            self.assert_error("io_error", lambda: store.archive("confirmed"))
        self.assertEqual(PROPOSAL, store.read()["proposal"])
        archives = [path for path in self.state.iterdir() if path.name != "pending.json"]
        self.assertEqual(1, len(archives))
        self.assertEqual(PROPOSAL, JsonStore(archives[0]).read()["pending"]["proposal"])


class HeadStoreTests(JournalTestCase):
    def test_both_heads_round_trip_and_raw_can_advance_without_domain(self):
        store = HeadStore(self.state)
        self.assertIsNone(store.read())
        store.write(HEAD)
        self.assertEqual(HEAD, HeadStore(self.state).read())
        advanced = dict(HEAD, raw_cursor="event-3", raw_hash="raw-hash-3")
        store.write(advanced)
        self.assertEqual(advanced, store.read())
        self.assertEqual("verified_head.json", store.path.name)

    def test_empty_heads_are_explicit_not_silent_defaults(self):
        empty = {"raw_cursor": None, "raw_hash": None, "domain_head": None, "domain_ordinal": 0}
        store = HeadStore(self.state)
        store.write(empty)
        self.assertEqual(empty, store.read())

    def test_malformed_or_partial_heads_are_rejected(self):
        store = HeadStore(self.state)
        for value in (
            {},
            dict(HEAD, raw_hash=None),
            dict(HEAD, domain_ordinal=True),
            dict(HEAD, domain_ordinal=-1),
            dict(HEAD, domain_head=None),
            dict(HEAD, domain_ordinal=0),
        ):
            with self.subTest(value=value):
                self.assert_error("invalid_state", lambda: store.write(value))
        JsonStore(store.path).write({"raw_cursor": "only-raw"})
        self.assert_error("corrupt_state", store.read)


class AuthorityLockTests(JournalTestCase):
    def test_thread_fork_waits_for_complete_lock_descriptor_lifecycle(self):
        for stage in ("acquire_fsync", "acquire_dup", "release_close"):
            with self.subTest(stage=stage):
                state = self.root / stage
                lock = AuthorityLock(state, "authority-1")
                reached, resume = threading.Event(), threading.Event()
                fork_started, fork_completed = threading.Event(), threading.Event()
                os.register_at_fork(
                    before=fork_started.set, after_in_parent=fork_completed.set
                )
                native_sync, native_dup, native_close = os.fsync, os.dup, os.close
                hold_reader, hold_writer = os.pipe()
                ready_reader, ready_writer = os.pipe()
                pipe_fds = {hold_reader, hold_writer, ready_reader, ready_writer}
                errors = []
                child = None
                worker = None
                releaser = None

                def pause(fd):
                    if threading.current_thread() is worker and same_inode(fd, lock.path):
                        reached.set()
                        if not resume.wait(5):
                            raise RuntimeError("lock lifecycle was not resumed")

                def sync(fd):
                    native_sync(fd)
                    if stage == "acquire_fsync" and lock.path.exists():
                        pause(fd)

                def duplicate(fd):
                    result = native_dup(fd)
                    if stage == "acquire_dup":
                        pause(fd)
                    return result

                def close(fd):
                    if stage == "release_close" and lock.path.exists():
                        pause(fd)
                    native_close(fd)

                def operation():
                    try:
                        lock.release() if stage == "release_close" else lock.acquire()
                    except (JournalError, OSError, RuntimeError) as error:
                        errors.append(error)

                def unblock_lifecycle():
                    if not fork_started.wait(5):
                        errors.append(RuntimeError("fork preparation never started"))
                    # Before the fix, resume only after the unguarded fork occurs.
                    # With the guard, the bounded wait releases its blocked lifecycle.
                    fork_completed.wait(1)
                    resume.set()

                try:
                    with (
                        mock.patch("mempalace_tasks.journal.os.fsync", side_effect=sync),
                        mock.patch("mempalace_tasks.journal.os.dup", side_effect=duplicate),
                        mock.patch("mempalace_tasks.journal.os.close", side_effect=close),
                    ):
                        if stage == "release_close":
                            lock.acquire()
                        worker = threading.Thread(target=operation, daemon=True)
                        worker.start()
                        self.assertTrue(reached.wait(5), f"missing {stage} boundary")
                        releaser = threading.Thread(target=unblock_lifecycle, daemon=True)
                        releaser.start()
                        with warnings.catch_warnings():
                            # This test deliberately exercises the guarded multithreaded fork.
                            warnings.filterwarnings(
                                "ignore", category=DeprecationWarning,
                                message=r"This process .* is multi-threaded, use of fork\(\)",
                            )
                            child = os.fork()
                        if child == 0:
                            native_close(hold_writer)
                            native_close(ready_reader)
                            os.write(ready_writer, b"ready")
                            os.read(hold_reader, 1)
                            os._exit(0)
                        worker.join(5)
                        releaser.join(5)
                        self.assertFalse(worker.is_alive())
                        self.assertFalse(releaser.is_alive())
                        self.assertEqual([], errors)
                        with selectors.DefaultSelector() as selector:
                            selector.register(ready_reader, selectors.EVENT_READ)
                            self.assertTrue(selector.select(5), "fork child failed to resume")
                        self.assertEqual(b"ready", os.read(ready_reader, 5))
                        lock.release()
                        with AuthorityLock(state, "authority-1"):
                            pass
                finally:
                    resume.set()
                    if worker is not None:
                        worker.join(5)
                    if releaser is not None:
                        releaser.join(5)
                    if child is not None and child != 0:
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(child, 0)
                    for fd in pipe_fds:
                        native_close(fd)
                    lock.release()

    def child(self, mode, state=None, authority="authority-1"):
        return [
            sys.executable,
            str(Path(__file__).absolute()),
            "--lock-child",
            str(state or self.state),
            authority,
            mode,
        ]

    def start_child(self, mode):
        process = subprocess.Popen(
            self.child(mode),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def read_line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(5), "child did not report its next boundary")
        return process.stdout.readline().strip()

    def holder(self, mode="wait"):
        process = self.start_child(mode)
        self.assertEqual("acquired", self.read_line(process))
        return process

    def test_initial_publication_cannot_be_stolen_before_creator_flock(self):
        creator = self.start_child("pause-publication")
        self.assertEqual("published", self.read_line(creator))
        inode = (self.state / "authority.lock").stat().st_ino
        contender = self.start_child("pause-after-flock")
        boundary = self.read_line(contender)
        self.assertIn(boundary, ("locked", "authority_locked"))
        creator_output, creator_error = creator.communicate(input="continue\n", timeout=5)
        contender_output, contender_error = contender.communicate(
            input="continue\n" if boundary == "locked" else None, timeout=5
        )
        self.assertEqual(
            0, creator.returncode,
            f"creator: {creator_output} {creator_error}; "
            f"contender: {contender.returncode} {contender_output} {contender_error}",
        )
        self.assertEqual(73, contender.returncode, contender_error)
        self.assertEqual("acquired", creator_output.strip())
        with AuthorityLock(self.state, "authority-1"):
            self.assertEqual(inode, (self.state / "authority.lock").stat().st_ino)

    def test_duplicate_owner_in_another_process_is_rejected(self):
        self.holder()
        result = subprocess.run(self.child("once"), capture_output=True, text=True, timeout=5)
        self.assertEqual(73, result.returncode, result.stderr)
        self.assertEqual("authority_locked", result.stdout.strip())

    def test_process_death_releases_lock_without_deleting_lock_file(self):
        process = self.holder()
        path = self.state / "authority.lock"
        inode = path.stat().st_ino
        process.kill()
        process.wait(timeout=5)
        with AuthorityLock(self.state, "authority-1"):
            self.assertEqual(inode, path.stat().st_ino)
        self.assertTrue(path.exists())

    def test_normal_process_exit_releases_lock(self):
        process = self.holder()
        process.communicate(input="exit\n", timeout=5)
        self.assertEqual(0, process.returncode)
        with AuthorityLock(self.state, "authority-1"):
            pass

    def test_forked_descendant_does_not_pin_lock_after_owner_exit(self):
        process = self.holder("fork")
        process.wait(timeout=5)
        self.assertEqual(0, process.returncode)
        descendant = process.stdout.readline().strip()
        self.assertTrue(descendant.startswith("descendant:"), descendant)
        os.kill(int(descendant.split(":")[1]), 0)
        with AuthorityLock(self.state, "authority-1"):
            pass
        process.communicate(input="exit\n", timeout=5)

    def test_unsupported_platform_fails_explicitly(self):
        with mock.patch("mempalace_tasks.journal.fcntl", None):
            self.assert_error(
                "unsupported_platform", AuthorityLock(self.state, "authority-1").acquire
            )
        self.assertFalse(self.state.exists())

    def test_corrupt_lock_metadata_does_not_silently_create_an_owner(self):
        self.state.mkdir()
        path = self.state / "authority.lock"
        for data in ("{", "", "null", '{"authority_id":null}'):
            with self.subTest(data=data):
                path.write_text(data, encoding="utf-8")
                self.assert_error(
                    "corrupt_state", AuthorityLock(self.state, "authority-1").acquire
                )
                self.assertEqual(data, path.read_text(encoding="utf-8"))

    def test_forked_child_cannot_unlock_live_parent(self):
        with AuthorityLock(self.state, "authority-1"):
            process = os.fork()
            if process == 0:
                try:
                    AuthorityLock(self.state, "authority-1").acquire()
                except JournalError as error:
                    os._exit(73 if error.code == "authority_locked" else 74)
                os._exit(0)
            _, status = os.waitpid(process, 0)
            self.assertEqual(73, os.waitstatus_to_exitcode(status))

    def test_same_process_and_canonical_path_alias_cannot_acquire_twice(self):
        with AuthorityLock(self.state, "authority-1"):
            relative = os.path.relpath(self.state)
            contender = AuthorityLock(relative, "authority-1")
            self.assert_error("authority_locked", contender.acquire)
        with AuthorityLock(self.state, "authority-1"):
            pass

    def test_lock_is_private_and_state_is_bound_to_one_authority(self):
        with AuthorityLock(self.state, "authority-1"):
            self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((self.state / "authority.lock").stat().st_mode))
        self.assert_error(
            "authority_mismatch", AuthorityLock(self.state, "authority-2").acquire
        )

    def test_context_exception_and_repeated_release_are_safe(self):
        lock = AuthorityLock(self.state, "authority-1")
        with self.assertRaisesRegex(RuntimeError, "body"):
            with lock:
                raise RuntimeError("body")
        lock.release()
        with AuthorityLock(self.state, "authority-1"):
            pass

    def test_symlink_lock_file_is_rejected(self):
        self.state.mkdir()
        target = self.root / "other-lock"
        target.write_text("untouched", encoding="utf-8")
        (self.state / "authority.lock").symlink_to(target)
        self.assert_error("unsafe_path", AuthorityLock(self.state, "authority-1").acquire)
        self.assertEqual("untouched", target.read_text(encoding="utf-8"))


def acquire_child_lock(state, authority, mode):
    lock = AuthorityLock(state, authority)
    if mode == "pause-publication":
        native_open = os.open

        def pause_publication(path, flags, *args, **kwargs):
            fd = native_open(path, flags, *args, **kwargs)
            if Path(path).name == "authority.lock" and flags & os.O_EXCL:
                print("published", flush=True)
                sys.stdin.readline()
            return fd

        with mock.patch("mempalace_tasks.journal.os.open", side_effect=pause_publication):
            lock.acquire()
    elif mode == "pause-after-flock":
        native_dup = os.dup

        def pause_locked(fd):
            print("locked", flush=True)
            sys.stdin.readline()
            return native_dup(fd)

        with mock.patch("mempalace_tasks.journal.os.dup", side_effect=pause_locked):
            lock.acquire()
    else:
        lock.acquire()
    return lock


def lock_child():
    _, _, state, authority, mode = sys.argv
    try:
        lock = acquire_child_lock(state, authority, mode)
    except JournalError as error:
        print(error.code, flush=True)
        return 73 if error.code == "authority_locked" else 74
    print("acquired", flush=True)
    if mode == "wait":
        sys.stdin.readline()
    elif mode == "fork":
        reader, writer = os.pipe()
        descendant = os.fork()
        if descendant == 0:
            os.close(reader)
            os.write(writer, b"ready")
            os.close(writer)
            sys.stdin.readline()
            os._exit(0)
        os.close(writer)
        os.read(reader, 5)
        os.close(reader)
        print(f"descendant:{descendant}", flush=True)
    # Deliberately leave release to the OS on process exit.
    return 0


def crash_child():
    _, _, state, boundary = sys.argv
    replace = os.replace

    def crash_at_replace(source, destination, *args, **kwargs):
        if boundary == "after_replace":
            replace(source, destination, *args, **kwargs)
        os._exit(91)

    with mock.patch("mempalace_tasks.journal.os.replace", side_effect=crash_at_replace):
        PendingStore(state).attach_settlement(SETTLEMENT)
    return 92


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--lock-child":
        raise SystemExit(lock_child())
    if len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
        raise SystemExit(crash_child())
    unittest.main()
