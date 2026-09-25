"""Native Windows security/identity probes; skipped rather than emulated elsewhere."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mempalace_tasks import platform_support as platform
from mempalace_tasks import platform_windows


@unittest.skipUnless(os.name == "nt", "requires native Windows handles, ACLs, and clock")
class WindowsPlatformTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="mptask-windows-", dir=os.environ["MPTASK_TEST_TMPDIR"])
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name).resolve()
        self.root = platform.ensure_private_directory(self.parent / "private")
        self.path = self.root / "credential"

    def test_creation_uses_protected_user_dacl_not_inherited_permissions(self):
        import win32security
        platform.create_private(self.path, b"secret")
        descriptor = win32security.GetNamedSecurityInfo(
            str(self.path), win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        self.assertTrue(descriptor.GetSecurityDescriptorControl()[0]
                        & win32security.SE_DACL_PROTECTED)
        self.assertEqual(platform.read_regular(self.path, private=True), b"secret")

    def test_permissive_acl_is_rejected_not_repaired(self):
        import win32con
        import win32security
        platform.create_private(self.path, b"secret")
        descriptor = win32security.GetNamedSecurityInfo(
            str(self.path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        acl = descriptor.GetSecurityDescriptorDacl()
        everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, win32con.GENERIC_READ, everyone)
        win32security.SetNamedSecurityInfo(
            str(self.path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        with self.assertRaises(platform.PlatformError):
            platform.read_regular(self.path, private=True)
        self.assertEqual(platform.read_regular(self.path), b"secret")
        after = win32security.GetNamedSecurityInfo(
            str(self.path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
        self.assertEqual(after.GetSecurityDescriptorDacl().GetAceCount(), acl.GetAceCount())

    def test_parent_acl_is_preserved(self):
        import win32security
        def security():
            descriptor = win32security.GetNamedSecurityInfo(
                str(self.parent), win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
            return bytes(descriptor)
        before = security()
        platform.create_private(self.parent / "token", b"token")
        platform.ensure_private_directory(self.parent / "another")
        self.assertEqual(security(), before)

    def test_windows_aliases_ads_devices_and_unc_are_not_local_canonical_paths(self):
        for value in (r"C:relative", r"\\.\C:\secret", r"\\?\C:\secret",
                      r"\\host\share\secret", str(self.path) + ":stream",
                      str(self.path) + ".", str(self.path) + " ",
                      str(self.root / "NUL"), str(self.root / "COM1.txt"),
                      str(self.root) + r"\..\escape"):
            with self.subTest(value=value), self.assertRaises(platform.PlatformError):
                platform.canonical_path(value)

    def test_case_variant_refers_to_same_lock_object(self):
        with platform.LifetimeLock(self.path):
            with self.assertRaises(platform.PlatformError) as caught:
                platform.LifetimeLock(str(self.path).swapcase()).acquire()
            self.assertEqual(caught.exception.code, "lock_busy")

    def test_fixed_byte_lock_blocks_other_process_and_releases(self):
        lock = platform.LifetimeLock(self.path).acquire()
        try:
            child = subprocess.run(
                [sys.executable, "-c",
                 "import sys; from mempalace_tasks.platform_support import LifetimeLock, PlatformError\n"
                 "try: LifetimeLock(sys.argv[1]).acquire()\n"
                 "except PlatformError as error: sys.exit(23 if error.code == 'lock_busy' else 24)\n"
                 "sys.exit(0)", str(self.path)],
                capture_output=True, timeout=15, check=False)
            self.assertEqual(child.returncode, 23, child.stderr)
        finally:
            lock.release()
        with platform.LifetimeLock(self.path):
            pass

    def test_open_lock_cannot_be_replaced_or_deleted(self):
        with platform.LifetimeLock(self.path):
            with self.assertRaises(OSError):
                self.path.unlink()
        self.assertTrue(self.path.exists())

    def test_lock_handle_is_not_inheritable(self):
        import win32api
        import win32con
        with platform.LifetimeLock(self.path) as lock:
            flags = win32api.GetHandleInformation(lock._lock._handle)
            self.assertFalse(flags & win32con.HANDLE_FLAG_INHERIT)

    def test_directory_handles_exclude_reparse_mutation_write_access(self):
        import pywintypes
        import win32con
        import win32file
        original = platform_windows._create
        def create(path, data):
            with self.assertRaises(pywintypes.error) as caught:
                handle = win32file.CreateFile(
                    str(self.root), win32con.GENERIC_WRITE,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                    None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
                handle.Close()
            self.assertEqual(caught.exception.winerror, 32)
            return original(path, data)
        with patch.object(platform_windows, "_create", side_effect=create):
            platform.create_private(self.path, b"secret")
        self.assertEqual(platform.read_regular(self.path, private=True), b"secret")

    def test_failed_cache_write_cleans_candidate_without_replacing_old_value(self):
        import pywintypes
        import win32file
        platform.atomic_write_cache(self.path, b"old")
        with patch.object(win32file, "WriteFile",
                          side_effect=pywintypes.error(112, "WriteFile", "Disk full")):
            with self.assertRaises(platform.PlatformError):
                platform.atomic_write_cache(self.path, b"new")
        self.assertEqual(platform.read_regular(self.path, private=True), b"old")
        self.assertEqual(set(self.root.iterdir()), {self.path})

    def test_reparse_point_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        junction = self.root / "junction"
        # Directory junction creation does not require symlink/developer privilege.
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(platform.PlatformError):
                platform.canonical_path(junction / "secret")
            with self.assertRaises(platform.PlatformError):
                platform.create_private(junction / "secret", b"secret")
        finally:
            junction.rmdir()

    def test_biased_continuous_clock_real_api(self):
        first = platform_windows.continuous_time_ns()
        second = platform_windows.continuous_time_ns()
        self.assertGreaterEqual(second, first)
        self.assertGreater(first, 0)


class WindowsImportGuardsTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "non-Windows import guard")
    def test_windows_module_imports_without_pywin32_on_posix(self):
        with self.assertRaises(platform.PlatformError) as caught:
            platform_windows.canonical_path(r"C:\secret")
        self.assertEqual(caught.exception.code, "unsupported_platform")


if __name__ == "__main__":
    unittest.main()
