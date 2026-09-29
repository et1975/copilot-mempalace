"""Bounded setup children, shell-free commands and native private path checks.

The Windows stdin gate permits Job assignment before the selected program can
spawn descendants. It is internal process plumbing, not a task owner or store.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def _child_gate():
    if os.read(0, 1) != b"\x01":
        return 1
    return subprocess.call(sys.argv[2:], shell=False, close_fds=True)


if __name__ == "__main__":
    if sys.argv[1:2] != ["--child-gate"]:
        raise SystemExit(2)
    raise SystemExit(_child_gate())


from . import platform_support as platform


class SetupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SetupError(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "malformed_response")
        result[key] = value
    return result


def _constant(_):
    raise SetupError("malformed_response")


def document(data, code="malformed_response"):
    try:
        value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
        require(type(value) is dict, code)
        return value
    except (ValueError, UnicodeError, RecursionError, SetupError):
        raise SetupError(code) from None


def private_directory(path):
    """Read-only counterpart to ensure_private_directory; reuse native handles."""
    path = platform.canonical_path(path)
    backend = platform._backend()
    try:
        with backend._directory(path) as handle:
            backend._private(os.fstat(handle) if os.name == "posix" else handle)
    except FileNotFoundError:
        raise SetupError("missing_directory") from None
    return path


def private_parent(path):
    path = platform.canonical_path(path)
    require(path.name and not path.is_dir(), "regular_file_required")
    require(path.parent not in {Path(path.anchor), Path.home(), Path("/home"), Path("/tmp")},
            "broad_root_refused")
    if os.name == "posix":
        for parent in path.parents:
            if parent.exists():
                mode = parent.stat().st_mode
                require(not mode & 0o022 or
                        (parent == Path("/tmp") and mode & 0o1000), "unsafe_parent")
    if path.parent.exists():
        private_directory(path.parent)
    return path


def create_parents(path):
    path = platform.canonical_path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        platform.ensure_private_directory(directory)


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]

    def run(self, *args):
        with Child((*self.argv, *args), allow_service=args[:1] == ("start",)) as child:
            return child.complete()


def executable(name, supplied=None):
    """Never pass batch files to CreateProcess (which can implicitly use cmd.exe)."""
    if supplied is not None:
        path = str(supplied)
    else:
        filename = name + ".exe" if os.name == "nt" else name
        candidates = (Path(directory) / filename for directory in
                      os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(directory))
        path = next((str(candidate) for candidate in candidates if candidate.is_file()
                     and (os.name == "nt" or os.access(candidate, os.X_OK))), None)
        require(path is not None, "missing_executable")
    path = os.path.abspath(path)
    require(Path(path).suffix.lower() not in {".cmd", ".bat"}, "unsafe_executable")
    require(Path(path).is_file(), "missing_executable")
    if Path(path).suffix.lower() == ".py":
        return Command((sys.executable, path))
    require(os.name == "nt" and Path(path).suffix.lower() == ".exe"
            or os.name == "posix" and os.access(path, os.X_OK), "missing_executable")
    return Command((path,))


def task_command(supplied=None):
    if supplied is not None:
        return executable("mempalace-tasks", supplied)
    adjacent = Path(sys.executable).parent / ("mempalace-tasks.exe" if os.name == "nt"
                                              else "mempalace-tasks")
    if adjacent.is_file():
        return executable("mempalace-tasks", adjacent)
    # Isolated Python excludes cwd and PYTHONPATH, so a repository import alone
    # can never become a seemingly installed, but unusable, MCP registration.
    module = Command((sys.executable, "-I", "-m", "mempalace_tasks"))
    result = module.run("--help")
    if result.returncode == 0 and "validate-config" in result.stdout:
        return module
    return executable("mempalace-tasks")


class WindowsJob:
    def __init__(self, allow_service):
        try:
            import win32api
            import win32job
        except ImportError:
            raise SetupError("unsupported_child_control") from None
        self.api, self.job_api = win32api, win32job
        self.handle = win32job.CreateJobObject(None, None)
        try:
            limits = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation)
            flags = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if allow_service:
                # Only explicit native start may detach its authorized owner.
                flags |= win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
            limits["BasicLimitInformation"]["LimitFlags"] = flags
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, limits)
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        self.job_api.AssignProcessToJobObject(self.handle, process._handle)

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Child:
    """One owned process group/Job, 20 seconds and 1 MiB per output pipe."""

    def __init__(self, argv, *, allow_service=False, timeout=20.0):
        self.deadline = time.monotonic() + timeout
        self.condition = threading.Condition()
        self.buffers = [bytearray(), bytearray()]
        self.totals = [0, 0]
        self.ended = [False, False]
        self.error = None
        self.request_id = 0
        self.sent = 0
        self.job = None
        self.process = None
        self.threads = []
        options = {}
        if os.name == "nt":
            self.job = WindowsJob(allow_service)
            argv = (sys.executable, "-I", str(Path(__file__).resolve()), "--child-gate", *argv)
        elif os.name == "posix":
            options["start_new_session"] = True
        else:
            raise SetupError("unsupported_child_control")
        try:
            self.process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, close_fds=True, bufsize=0, **options)
            if self.job:
                self.job.assign(self.process)
                self.process.stdin.write(b"\x01")
            for index, stream in enumerate((self.process.stdout, self.process.stderr)):
                thread = threading.Thread(target=self._drain, args=(index, stream), daemon=True)
                self.threads.append(thread)
                thread.start()
        except BaseException:
            self.close()
            raise

    def _drain(self, index, stream):
        try:
            while True:
                data = stream.read(4096)
                with self.condition:
                    self.totals[index] += len(data)
                    if self.totals[index] > 1_048_576:
                        self.error = "output_limit"
                        break
                    self.buffers[index].extend(data)
                    self.condition.notify_all()
                    if not data:
                        break
        except (OSError, ValueError):
            with self.condition:
                self.error = "filesystem_or_pipe_error"
        finally:
            with self.condition:
                self.ended[index] = True
                self.condition.notify_all()

    def _remaining(self):
        require(self.error is None, self.error)
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "deadline_exceeded")
        return remaining

    def _line(self):
        with self.condition:
            while True:
                remaining = self._remaining()
                newline = self.buffers[0].find(b"\n")
                require(newline < 65536 and (newline >= 0 or len(self.buffers[0]) < 65536),
                        "output_limit")
                if newline >= 0:
                    line = bytes(self.buffers[0][:newline])
                    del self.buffers[0][:newline + 1]
                    return line.decode("utf-8", errors="strict")
                require(not self.ended[0], "unexpected_eof")
                self.condition.wait(remaining)

    def notify(self, value):
        data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        self.sent += len(data)
        require(self.sent <= 4096, "request_limit")
        self._remaining()
        # A whole-session budget fits one native pipe buffer, even if the peer
        # stops reading. Pagination cannot turn stdin into an unbounded write.
        self.process.stdin.write(data)

    def call(self, method, params):
        self.request_id += 1
        self.notify({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params})
        for _ in range(100):
            message = document(self._line())
            require(message.get("jsonrpc") == "2.0", "invalid_jsonrpc")
            require("error" not in message, "mcp_error")
            if type(message.get("id")) is int and message["id"] == self.request_id:
                result = message.get("result")
                require(type(result) is dict, "malformed_response")
                require(result.get("isError", False) is False, "tool_error")
                return result
        raise SetupError("message_limit")

    def complete(self):
        self.process.stdin.close()
        with self.condition:
            while not all(self.ended):
                self.condition.wait(self._remaining())
            self._remaining()
        # Keep the POSIX leader unreaped until owned-group cleanup. This avoids
        # sending a signal to a recycled PID after a normal exit.
        if os.name == "posix" and hasattr(os, "WNOWAIT"):
            while os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                time.sleep(min(0.01, self._remaining()))
            self._kill_group()
        returncode = self.process.wait(timeout=self._remaining())
        return subprocess.CompletedProcess(
            self.process.args, returncode,
            self.buffers[0].decode("utf-8", errors="strict"),
            self.buffers[1].decode("utf-8", errors="strict"))

    def finish_protocol(self):
        self.deadline = min(self.deadline, time.monotonic() + 3.0)
        result = self.complete()
        require(not result.stdout.strip(), "unexpected_stdout")
        require(result.returncode == 0, "frontend_shutdown_failed")

    def _kill_group(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        try:
            if self.job:
                self.job.close()
            if self.process is not None and self.process.returncode is None:
                if os.name == "posix":
                    self._kill_group()
                else:
                    self.process.kill()
                self.process.wait(timeout=3)
        finally:
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
            for thread in self.threads:
                thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
