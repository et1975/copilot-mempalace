"""Windows local-drive handles, protected ACLs, fixed-byte locks and biased time.

pywin32 is loaded only when a native Windows operation is requested. Missing
APIs/dependencies fail closed; POSIX imports never need Windows packages. UNC,
device namespaces, alternate streams, 8.3 aliases and reparse traversal are
deliberately unsupported. No chmod approximation or PID ownership is used.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os
from pathlib import Path, PureWindowsPath
import threading
from uuid import uuid4

from .platform_support import PlatformError


_api = None
_native_error = None


def _require():
    global _api, _native_error
    if os.name != "nt":
        raise PlatformError("unsupported_platform", "Native Windows handles are unavailable")
    if _api is None:
        try:
            import pywintypes
            import win32api
            import win32con
            import win32file
            import win32security
        except ImportError as error:
            raise PlatformError("unsupported_platform", "Windows requires installed pywin32") from error
        _api = pywintypes, win32api, win32con, win32file, win32security
        _native_error = pywintypes.error
    return _api


def _io(error) -> PlatformError:
    number = getattr(error, "winerror", None)
    if number in (2, 3):
        return PlatformError("not_found", "Required path does not exist")
    if number in (80, 183):
        return PlatformError("already_exists", "Exclusive creation requires a new file")
    return PlatformError("filesystem_error", "Native Windows filesystem operation failed")


def _path(value) -> Path:
    _require()
    try:
        text = os.fspath(value)
        path = PureWindowsPath(text)
    except (TypeError, ValueError) as error:
        raise PlatformError("unsafe_path", "Expected an absolute native Windows path") from error
    if (type(text) is not str or not path.is_absolute() or str(path) != text
            or len(path.drive) != 2 or path.drive[1] != ":"
            or not path.drive[0].isascii() or not path.drive[0].isalpha()
            or len(text) > 32760 or any(ord(character) < 32 for character in text)):
        raise PlatformError("unsafe_path", "Expected an unaliased absolute local-drive path")
    for part in path.parts[1:]:
        stem = part.split(".")[0].upper()
        device = (stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
                  or (len(stem) == 4 and stem[:3] in {"COM", "LPT"}
                      and stem[3] in "123456789¹²³"))
        if (part in {".", ".."} or part.endswith((".", " ")) or device
                or any(character in '<>:"/\\|?*' for character in part)):
            raise PlatformError("unsafe_path", "Windows path contains an alias or device name")
    return Path(path)


def _extended(path: Path) -> str:
    return "\\\\?\\" + str(path)


def _volume(path: Path) -> None:
    _, api, con, file, _ = _require()
    if file.GetDriveType(path.anchor) != con.DRIVE_FIXED:
        raise PlatformError("unsupported_filesystem", "Only local fixed drives are supported")
    # A security descriptor is silently ignored by filesystems without ACLs.
    if not api.GetVolumeInformation(path.anchor)[3] & 0x00000008:  # FILE_PERSISTENT_ACLS
        raise PlatformError("unsupported_filesystem", "Filesystem lacks persistent access controls")


def _current_sid():
    _, api, con, _, security = _require()
    token = security.OpenProcessToken(api.GetCurrentProcess(), con.TOKEN_QUERY)
    try:
        return security.GetTokenInformation(token, security.TokenUser)[0]
    finally:
        token.Close()


def _security_attributes():
    types, _, con, _, security = _require()
    sid = _current_sid()
    acl = security.ACL()
    acl.AddAccessAllowedAceEx(security.ACL_REVISION, 0, con.FILE_ALL_ACCESS, sid)
    descriptor = security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED)
    attributes = types.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def _private(handle) -> None:
    _, _, _, _, security = _require()
    descriptor = security.GetSecurityInfo(
        handle, security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
    sid = _current_sid()
    acl = descriptor.GetSecurityDescriptorDacl()
    if (descriptor.GetSecurityDescriptorOwner() != sid
            or not descriptor.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED
            or acl is None or not acl.GetAceCount()):
        raise PlatformError("unsafe_permissions", "Object requires an owned protected private DACL")
    # Strict allow-list, not a heuristic about an ACL's textual SID names. Reject
    # unsupported/object/callback/deny ACEs instead of guessing effective access.
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if (len(ace) != 3 or ace[0][0] != security.ACCESS_ALLOWED_ACE_TYPE
                or ace[0][1] & (security.INHERITED_ACE | security.INHERIT_ONLY_ACE)
                or ace[2] != sid):
            raise PlatformError("unsafe_permissions", "DACL permits unsupported or non-owner access")


def _information(handle, *, directory: bool, private: bool = False):
    _, _, con, file, _ = _require()
    info = file.GetFileInformationByHandle(handle)
    if (file.GetFileType(handle) != file.FILE_TYPE_DISK
            or info[0] & con.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(info[0] & con.FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and info[7] != 1)):
        raise PlatformError("unsafe_file", "Expected a non-reparse, singly linked native object")
    if private:
        _private(handle)
    return info


def _final_path(handle, requested: Path) -> Path:
    _, _, _, file, _ = _require()
    text = file.GetFinalPathNameByHandle(handle, 0)
    if not text.startswith("\\\\?\\") or text.startswith("\\\\?\\UNC\\"):
        raise PlatformError("unsafe_path", "Object is not on a local DOS drive")
    resolved = Path(text[4:])
    if str(resolved).casefold() != str(requested).casefold():
        raise PlatformError("unsafe_path", "Windows path resolves through an alias")
    return resolved


def _open(path: Path, *, directory: bool, access=None, sharing=None,
          disposition=None, attributes=None):
    _, _, con, file, _ = _require()
    if access is None:
        access = con.READ_CONTROL | con.FILE_READ_ATTRIBUTES
    if sharing is None:
        # Deny deletion AND write access on every ancestor: pinning its name
        # alone does not exclude in-place reparse conversion. OPEN_REPARSE_POINT
        # alone only protects the last component of each CreateFile call.
        sharing = con.FILE_SHARE_READ
    if disposition is None:
        disposition = con.OPEN_EXISTING
    handle = file.CreateFile(
        _extended(path), access, sharing, attributes, disposition,
        con.FILE_FLAG_OPEN_REPARSE_POINT | con.FILE_FLAG_BACKUP_SEMANTICS, None)
    try:
        _information(handle, directory=directory)
        _final_path(handle, path)
        return handle
    except BaseException:
        handle.Close()
        raise


@contextmanager
def _directory(path: Path):
    handles = []
    try:
        current = Path(path.anchor)
        handles.append(_open(current, directory=True))
        for part in path.parts[1:]:
            current /= part
            handles.append(_open(current, directory=True))
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            handle.Close()


def canonical_path(value) -> Path:
    path = _path(value)
    handles = []
    try:
        _volume(path)
        current = Path(path.anchor)
        handles.append(_open(current, directory=True))
        current = _final_path(handles[-1], current)
        for index, part in enumerate(path.parts[1:]):
            candidate = current / part
            try:
                if index == len(path.parts) - 2:
                    # Open without assuming the leaf's type; validate the handle.
                    _, _, con, file, _ = _require()
                    handle = file.CreateFile(
                        _extended(candidate), con.FILE_READ_ATTRIBUTES | con.READ_CONTROL,
                        con.FILE_SHARE_READ, None, con.OPEN_EXISTING,
                        con.FILE_FLAG_OPEN_REPARSE_POINT | con.FILE_FLAG_BACKUP_SEMANTICS, None)
                    handles.append(handle)
                    info = file.GetFileInformationByHandle(handle)
                    _information(handle, directory=bool(info[0] & con.FILE_ATTRIBUTE_DIRECTORY))
                else:
                    handles.append(_open(candidate, directory=True))
            except _native_error as error:
                if error.winerror in (2, 3):
                    return current.joinpath(*path.parts[index + 1:])
                raise
            current = _final_path(handles[-1], candidate)
        return current
    except _native_error as error:
        raise _io(error) from error
    finally:
        for handle in reversed(handles):
            handle.Close()


def read_regular(value, *, private: bool = False, maximum: int = 1_048_576) -> bytes:
    path = _path(value)
    _, _, con, file, _ = _require()
    try:
        _volume(path)
        with _directory(path.parent):
            handle = _open(path, directory=False, access=con.GENERIC_READ | con.READ_CONTROL)
            try:
                before = _information(handle, directory=False, private=private)
                if (before[5] << 32 | before[6]) > maximum:
                    raise PlatformError("file_too_large", "File exceeds the read limit")
                remaining = maximum + 1
                chunks = []
                while remaining:
                    try:
                        _, data = file.ReadFile(handle, min(remaining, 65536))
                    except _native_error as error:
                        if error.winerror == 38:  # ERROR_HANDLE_EOF
                            break
                        raise
                    if not data:
                        break
                    chunks.append(data)
                    remaining -= len(data)
                after = _information(handle, directory=False, private=private)
                if before[3:] != after[3:]:
                    raise PlatformError("file_changed", "File changed while it was read")
                result = b"".join(chunks)
                if len(result) > maximum:
                    raise PlatformError("file_too_large", "File exceeds the read limit")
                return result
            finally:
                handle.Close()
    except _native_error as error:
        raise _io(error) from error


def _create(path: Path, data: bytes):
    _, _, con, file, _ = _require()
    handle = _open(path, directory=False,
                   access=con.GENERIC_READ | con.GENERIC_WRITE | con.READ_CONTROL | con.DELETE,
                   disposition=con.CREATE_NEW, attributes=_security_attributes())
    try:
        _private(handle)
        remaining = memoryview(data)
        while remaining:
            _, written = file.WriteFile(handle, remaining[:65536].tobytes())
            if written <= 0:
                raise PlatformError("short_write", "File write made no progress")
            remaining = remaining[written:]
        _information(handle, directory=False, private=True)
        return handle
    except BaseException:
        try:
            # Delete by handle: never accidentally delete a replacement by name.
            file.SetFileInformationByHandle(handle, file.FileDispositionInfo, True)
        finally:
            handle.Close()
        raise


def create_private(value, data: bytes) -> None:
    path = _path(value)
    try:
        _volume(path)
        with _directory(path.parent):
            handle = _create(path, data)
            handle.Close()
    except _native_error as error:
        raise _io(error) from error


def ensure_private_directory(value) -> Path:
    path = _path(value)
    _, _, _, file, _ = _require()
    if path == Path(path.anchor):
        raise PlatformError("unsafe_path", "A drive root is not a private directory")
    try:
        _volume(path)
        with _directory(path.parent):
            try:
                file.CreateDirectory(_extended(path), _security_attributes())
            except _native_error as error:
                if error.winerror != 183:
                    raise
            handle = _open(path, directory=True)
            try:
                _private(handle)
                return _final_path(handle, path)
            finally:
                handle.Close()
    except _native_error as error:
        raise _io(error) from error


def atomic_write_cache(value, data: bytes) -> None:
    path = _path(value)
    _, _, con, file, _ = _require()
    candidate = path.with_name(f".{path.name}.{uuid4().hex}.cache")
    try:
        _volume(path)
        with _directory(path.parent) as parent:
            _private(parent)
            target = None
            try:
                target = _open(path, directory=False, sharing=con.FILE_SHARE_READ | con.FILE_SHARE_DELETE)
                _private(target)
            except _native_error as error:
                if error.winerror not in (2, 3):
                    raise
            finally:
                if target is not None:
                    target.Close()
            handle = _create(candidate, data)
            published = False
            try:
                # Handle-relative rename keeps the validated source object. It
                # does not claim Windows namespace/power-loss durability.
                file.SetFileInformationByHandle(
                    handle, file.FileRenameInfo,
                    {"ReplaceIfExists": True, "RootDirectory": parent, "FileName": path.name})
                published = True
            finally:
                try:
                    if not published:
                        file.SetFileInformationByHandle(handle, file.FileDispositionInfo, True)
                finally:
                    handle.Close()
    except _native_error as error:
        raise _io(error) from error


class LifetimeLock:
    def __init__(self, path):
        self.path = _path(path)
        self._handle = None
        self._guard = threading.Lock()

    def acquire(self):
        types, _, con, file, _ = _require()
        with self._guard:
            if self._handle is not None:
                raise PlatformError("lock_busy", "This lock is already acquired")
            handle = None
            try:
                _volume(self.path)
                with _directory(self.path.parent) as parent:
                    _private(parent)
                    handle = _open(
                        self.path, directory=False,
                        access=con.GENERIC_READ | con.GENERIC_WRITE | con.READ_CONTROL,
                        sharing=con.FILE_SHARE_READ | con.FILE_SHARE_WRITE,
                        disposition=con.OPEN_ALWAYS, attributes=_security_attributes())
                    _private(handle)
                    overlap = types.OVERLAPPED()
                    overlap.Offset = 0
                    overlap.OffsetHigh = 0
                    try:
                        file.LockFileEx(handle, con.LOCKFILE_EXCLUSIVE_LOCK
                                        | con.LOCKFILE_FAIL_IMMEDIATELY, 1, 0, overlap)
                    except _native_error as error:
                        if error.winerror in (32, 33):
                            raise PlatformError("lock_busy", "Another process owns the lock") from error
                        raise
                    self._handle = handle
                    _active_locks.add(self)
                    handle = None
                    return self
            except _native_error as error:
                raise _io(error) from error
            finally:
                if handle is not None:
                    handle.Close()

    def release(self) -> None:
        with self._guard:
            if self._handle is not None:
                handle, self._handle = self._handle, None
                _active_locks.discard(self)
                try:
                    handle.Close()
                except _native_error as error:
                    raise _io(error) from error


# Strong references prevent an abandoned Python wrapper from releasing ownership
# early. Native process teardown still closes every handle.
_active_locks: set[LifetimeLock] = set()
_interrupt_time = None


def continuous_time_ns() -> int:
    global _interrupt_time
    if os.name != "nt":
        raise PlatformError("unsupported_platform", "Native Windows clock is unavailable")
    if _interrupt_time is None:
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel.QueryInterruptTimePrecise
            function.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
            function.restype = None
        except (AttributeError, OSError) as error:
            raise PlatformError("clock_unavailable", "Biased QueryInterruptTimePrecise is unavailable") from error
        _interrupt_time = function
    value = ctypes.c_ulonglong()
    _interrupt_time(ctypes.byref(value))
    return value.value * 100
