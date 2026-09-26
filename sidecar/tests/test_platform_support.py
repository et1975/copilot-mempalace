"""Native filesystem/lock probes use only caller-provided disposable directories."""

import errno
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mempalace_tasks import platform_support as platform


class PlatformFilesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-platform-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.addCleanup(self.directory.cleanup)
        # The caller's test root can itself be a native /var -> /private/var alias.
        self.root = Path(self.directory.name).resolve()
        if os.name == "nt":
            # A TemporaryDirectory inherits its DACL; secure only an intended child.
            self.root = platform.ensure_private_directory(self.root / "private")
        self.path = self.root / "credential"

    def test_private_creation_roundtrips_and_never_overwrites(self):
        platform.create_private(self.path, b"secret\n")
        self.assertEqual(platform.read_regular(self.path, private=True), b"secret\n")
        with self.assertRaises(platform.PlatformError):
            platform.create_private(self.path, b"replacement")
        self.assertEqual(self.path.read_bytes(), b"secret\n")

    def test_read_does_not_create_missing_parent(self):
        missing = self.root / "missing"
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(missing / "config")
        self.assertFalse(missing.exists())

    def test_creation_requires_existing_parent(self):
        missing = self.root / "missing"
        with self.assertRaises(platform.PlatformError):
            platform.create_private(missing / "secret", b"secret")
        self.assertFalse(missing.exists())

    def test_directory_creation_only_creates_requested_leaf(self):
        with self.assertRaises(platform.PlatformError):
            platform.ensure_private_directory(self.root / "missing" / "child")
        self.assertFalse((self.root / "missing").exists())
        path = platform.ensure_private_directory(self.root / "private")
        self.assertEqual(path, self.root / "private")
        self.assertEqual(platform.ensure_private_directory(path), path)

    def test_canonical_path_accepts_missing_leaf_not_relative_paths(self):
        self.assertEqual(platform.canonical_path(self.path), self.path)
        for value in ("relative", self.root / ".." / "escape", "", b"bad"):
            with self.subTest(value=value), self.assertRaises(platform.PlatformError):
                platform.canonical_path(value)

    def test_read_bounds_and_file_type_are_enforced(self):
        platform.create_private(self.path, b"abc")
        self.assertEqual(platform.read_regular(self.path, maximum=3), b"abc")
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path, maximum=2)
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.root)
        for maximum in (-1, True, 1.2):
            with self.subTest(maximum=maximum), self.assertRaises(platform.PlatformError):
                platform.read_regular(self.path, maximum=maximum)

    def test_empty_file_and_zero_bound(self):
        platform.create_private(self.path, b"")
        self.assertEqual(platform.read_regular(self.path, maximum=0), b"")

    def test_hardlinks_rejected_even_for_public_reads(self):
        platform.create_private(self.path, b"secret")
        os.link(self.path, self.root / "alias")
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path)
        with self.assertRaises(platform.PlatformError):
            platform.atomic_write_cache(self.path, b"replacement")
        with self.assertRaises(platform.PlatformError):
            platform.LifetimeLock(self.path).acquire()
        self.assertEqual(self.path.read_bytes(), b"secret")

    def test_cache_replace_is_atomic_and_private(self):
        platform.atomic_write_cache(self.path, b"old")
        platform.atomic_write_cache(self.path, b"new")
        self.assertEqual(platform.read_regular(self.path, private=True), b"new")
        self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_lock_contention_release_and_stable_file(self):
        first = platform.LifetimeLock(self.path)
        with first:
            before = self.path.stat()
            with self.assertRaises(platform.PlatformError) as caught:
                platform.LifetimeLock(self.path).acquire()
            self.assertEqual(caught.exception.code, "lock_busy")
        first.release()
        with platform.LifetimeLock(self.path):
            after = self.path.stat()
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_continuous_clock_real_native_provider(self):
        first = platform.continuous_time_ns()
        time.sleep(0.002)
        second = platform.continuous_time_ns()
        self.assertIs(type(first), int)
        self.assertGreater(second, first)

    def test_non_bytes_and_invalid_private_flags_fail_before_creation(self):
        for value in ("secret", bytearray(b"secret"), None):
            with self.subTest(value=value), self.assertRaises(platform.PlatformError):
                platform.create_private(self.path, value)
        self.assertFalse(self.path.exists())
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path, private=1)

    def test_global_continuous_provider_rejects_invalid_and_regressing_samples(self):
        # Exercise the safety wrapper, not another OS's unrun implementation.
        backend = platform._backend()
        with patch.object(platform, "_last_continuous", None):
            with patch.object(backend, "continuous_time_ns", side_effect=[100, 99, 101]):
                self.assertEqual(platform.continuous_time_ns(), 100)
                with self.assertRaises(platform.PlatformError) as caught:
                    platform.continuous_time_ns()
                self.assertEqual(caught.exception.code, "continuous_discontinuity")
                self.assertEqual(platform.continuous_time_ns(), 101)
        for value in (-1, True, 2**64):
            with patch.object(backend, "continuous_time_ns", return_value=value):
                with self.subTest(value=value), self.assertRaises(platform.PlatformError):
                    platform.continuous_time_ns()


@unittest.skipUnless(os.name == "posix", "requires native POSIX descriptors and fork")
class PosixPlatformTests(PlatformFilesTests):
    def test_shared_parents_are_never_chmodded(self):
        self.root.chmod(0o755)
        self.path.write_bytes(b"public")
        self.path.chmod(0o644)
        self.assertEqual(platform.read_regular(self.path), b"public")
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path, private=True)
        platform.create_private(self.root / "private-file", b"secret")
        platform.ensure_private_directory(self.root / "private-dir")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_existing_directory_is_validated_never_repaired(self):
        self.root.chmod(0o755)
        with self.assertRaises(platform.PlatformError):
            platform.ensure_private_directory(self.root)
        with self.assertRaises(platform.PlatformError):
            platform.atomic_write_cache(self.path, b"cache")
        with self.assertRaises(platform.PlatformError):
            platform.LifetimeLock(self.path).acquire()
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)

    def test_new_private_objects_have_no_exposure_window(self):
        previous = os.umask(0)
        try:
            platform.create_private(self.path, b"secret")
            directory = platform.ensure_private_directory(self.root / "private-dir")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_symlink_leaf_ancestor_and_lexical_aliases_rejected(self):
        target = self.root / "target"
        target.mkdir()
        (target / "secret").write_bytes(b"original")
        self.path.symlink_to(target / "secret")
        ancestor = self.root / "link"
        ancestor.symlink_to(target, target_is_directory=True)
        for path in (self.path, ancestor / "secret"):
            with self.subTest(path=path):
                for operation in (platform.canonical_path, platform.read_regular):
                    with self.assertRaises(platform.PlatformError):
                        operation(path)
                with self.assertRaises(platform.PlatformError):
                    platform.create_private(path, b"new")
        for value in (str(self.root) + "/./a", str(self.root) + "//a",
                      str(self.root) + "/a/", "//" + str(self.root).lstrip("/") + "/a"):
            with self.subTest(value=value), self.assertRaises(platform.PlatformError):
                platform.canonical_path(value)
        self.assertEqual((target / "secret").read_bytes(), b"original")

    def test_fifo_read_does_not_block(self):
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path)

    def test_partial_writes_complete_and_failed_creation_removes_only_new_file(self):
        from mempalace_tasks import platform_posix
        original_write = os.write
        def short_write(fd, data):
            return original_write(fd, data[:2])
        with patch.object(platform_posix.os, "write", side_effect=short_write):
            platform.create_private(self.path, b"secret")
        self.assertEqual(self.path.read_bytes(), b"secret")
        failed = self.root / "failed"
        with patch.object(platform_posix.os, "write", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(platform.PlatformError):
                platform.create_private(failed, b"secret")
        self.assertFalse(failed.exists())

    def test_failed_cache_publication_preserves_old_value_and_cleans_candidate(self):
        from mempalace_tasks import platform_posix
        platform.atomic_write_cache(self.path, b"old")
        with patch.object(platform_posix.os, "replace", side_effect=OSError(errno.EIO, "I/O")):
            with self.assertRaises(platform.PlatformError):
                platform.atomic_write_cache(self.path, b"new")
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_failed_creation_stat_does_not_leak_open_descriptor(self):
        from mempalace_tasks import platform_posix
        opened = []
        original_open = os.open
        def capture(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor
        with patch.object(platform_posix.os, "open", side_effect=capture):
            with patch.object(platform_posix.os, "fstat", side_effect=OSError(errno.EIO, "stat")):
                with self.assertRaises(platform.PlatformError):
                    platform.create_private(self.path, b"secret")
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_zero_progress_write_fails_without_retaining_secret_prefix(self):
        from mempalace_tasks import platform_posix
        with patch.object(platform_posix.os, "write", return_value=0):
            with self.assertRaises(platform.PlatformError) as caught:
                platform.create_private(self.path, b"secret")
        self.assertEqual(caught.exception.code, "short_write")
        self.assertFalse(self.path.exists())

    def test_foreign_owner_private_read_is_not_repaired(self):
        from mempalace_tasks import platform_posix
        platform.create_private(self.path, b"secret")
        with patch.object(platform_posix.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(platform.PlatformError):
                platform.read_regular(self.path, private=True)
        self.assertEqual(platform.read_regular(self.path, private=True), b"secret")

    def test_concurrent_starters_contend_on_one_empty_stable_file(self):
        ready = threading.Barrier(3)
        release = threading.Event()
        results = []
        def contender():
            lock = platform.LifetimeLock(self.path)
            ready.wait(timeout=10)
            owned = False
            try:
                lock.acquire()
            except platform.PlatformError as error:
                results.append(error.code)
            else:
                owned = True
                results.append("owned")
            ready.wait(timeout=10)
            if owned:
                release.wait(timeout=10)
                lock.release()
        threads = [threading.Thread(target=contender) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            ready.wait(timeout=10)
            ready.wait(timeout=10)
            self.assertCountEqual(results, ["owned", "lock_busy"])
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.path.read_bytes(), b"")

    def test_new_lock_remains_reusable_with_restrictive_umask(self):
        old_umask = os.umask(0o777)
        try:
            with platform.LifetimeLock(self.path):
                pass
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with platform.LifetimeLock(self.path):
            pass

    def test_failed_creation_does_not_unlink_a_replacement(self):
        from mempalace_tasks import platform_posix
        def replaced(fd, data):
            self.path.unlink()
            self.path.write_bytes(b"someone-else")
            raise OSError(errno.EIO, "write failed")
        with patch.object(platform_posix.os, "write", side_effect=replaced):
            with self.assertRaises(platform.PlatformError):
                platform.create_private(self.path, b"secret")
        self.assertEqual(self.path.read_bytes(), b"someone-else")

    def test_mutation_during_read_is_rejected(self):
        from mempalace_tasks import platform_posix
        platform.create_private(self.path, b"original")
        original_read = os.read
        def changed(fd, size):
            result = original_read(fd, size)
            self.path.write_bytes(b"replaced")
            return result
        with patch.object(platform_posix.os, "read", side_effect=changed):
            with self.assertRaises(platform.PlatformError):
                platform.read_regular(self.path)

    def test_inherited_release_does_not_unlock_parent(self):
        lock = platform.LifetimeLock(self.path).acquire()
        self.addCleanup(lock.release)
        child = os.fork()
        if child == 0:
            try:
                lock.release()
            finally:
                os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertEqual(status, 0)
        with self.assertRaises(platform.PlatformError) as caught:
            platform.LifetimeLock(self.path).acquire()
        self.assertEqual(caught.exception.code, "lock_busy")

    def test_child_does_not_keep_lock_alive_after_parent_release(self):
        lock = platform.LifetimeLock(self.path).acquire()
        self.addCleanup(lock.release)
        read_end, write_end = os.pipe()
        ready_read, ready_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_end)
            os.close(ready_read)
            try:
                os.write(ready_write, b"r")
                os.close(ready_write)
                os.read(read_end, 1)
            finally:
                os._exit(0)
        os.close(read_end)
        os.close(ready_write)
        try:
            ready, _, _ = select.select([ready_read], [], [], 10)
            self.assertTrue(ready, "child failed to complete at-fork handlers")
            self.assertEqual(os.read(ready_read, 1), b"r")
            lock.release()
            with platform.LifetimeLock(self.path):
                pass
        finally:
            os.close(ready_read)
            os.write(write_end, b"x")
            os.close(write_end)
            os.waitpid(child, 0)

    def test_exec_does_not_inherit_lock_descriptor(self):
        lock = platform.LifetimeLock(self.path).acquire()
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read(1)"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=False)
            try:
                ready, _, _ = select.select([process.stdout], [], [], 10)
                self.assertTrue(ready, "child failed to start")
                self.assertEqual(process.stdout.readline(), b"ready\n")
                lock.release()
                with platform.LifetimeLock(self.path):
                    pass
            finally:
                process.communicate(b"x", timeout=10)
        finally:
            lock.release()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native provider")
    def test_linux_clock_has_no_monotonic_or_wall_fallback(self):
        from mempalace_tasks import platform_posix
        with patch.object(platform_posix.time, "clock_gettime_ns",
                          side_effect=OSError("unavailable")):
            with self.assertRaises(platform.PlatformError):
                platform.continuous_time_ns()


if __name__ == "__main__":
    unittest.main()
