"""Profile/artifact contracts and lazy, optional managed-process construction.

Local-only effects exclude daemonized/escaped processes and remote jobs. Git
worktrees use an explicit immutable base and are preserved for review; nothing
here applies a patch to a shared checkout. Configure a dedicated root (normally
below ~/s), never an existing user worktree.

Importing these contracts never loads the Linux supervisor implementation.
OwnedProcess construction requires that backend; there is no native Copilot or
fleet adapter, nor a generic process-killing fallback on unsupported hosts.
"""
from __future__ import annotations

import copy
import math
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .journal import JsonStore


class ExecutionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class DeadlineClock:
    """Shared seconds basis with serialized samples and a latched safety fault.

    Host and watchdog must share this guard: a regression/failure observed by
    either invalidates further authorization even if the provider later recovers.
    """
    def __init__(self, source: Callable[[], float]):
        if not callable(source):
            raise ValueError("Execution elapsed_time must be callable")
        self._source = source
        self._lock = threading.Lock()
        self._last = None
        self.error = None

    def __call__(self) -> float:
        with self._lock:
            if self.error is not None:
                raise self.error
            try:
                value = self._source()
                if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
                    raise ExecutionError("invalid_clock_sample", "Invalid execution deadline clock sample")
                if self._last is not None and value < self._last:
                    raise ExecutionError("continuous_discontinuity", "Execution deadline clock moved backwards")
            except ExecutionError as error:
                self.error = error
                raise
            except Exception as error:
                self.error = ExecutionError(getattr(error, "code", "clock_unavailable"),
                                            "Cannot sample execution deadline clock")
                raise self.error from error
            self._last = value
            return value


class ReconciliationAdapter(Protocol):
    """Trusted target-specific evidence, not an inference from process exit."""
    def reconcile(self, task: dict, workspace: "Workspace", *, process_stopped: bool,
                  recovering: bool, required_fences: dict[str, int]) -> dict: ...


class FenceTarget(Protocol):
    """Install resource-global high-water tokens; commits must reject old tokens."""
    def install(self, fences: dict[str, int]) -> list[str]: ...


@dataclass(frozen=True)
class Workspace:
    root: Path
    working_directory: Path
    input_path: Path
    result_path: Path
    checkpoint_path: Path
    evidence: dict


def _directory(path: Path, *, create=True):
    if not path.is_absolute() or ".." in path.parts:
        raise ExecutionError("unsafe_workspace", "Use an absolute non-aliased workspace path")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise ExecutionError("unsafe_workspace", "Configured source directory does not exist") from error
        if not stat.S_ISDIR(info.st_mode):
            raise ExecutionError("unsafe_workspace", "Workspace ancestors must be real directories")


def _references(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 20
            or any(not isinstance(ref, str) or not ref.strip()
                   or len(ref.encode("utf-8")) > 2048 for ref in value)
            or len(set(value)) != len(value)):
        raise ExecutionError("invalid_artifact", "Distinct nonempty evidence references required")
    return value


@dataclass(frozen=True)
class LocalProfile:
    name: str
    argv: tuple[str, ...]
    workspace_root: Path
    execution_class: str = "isolated"
    repository: Path | None = None
    base_commit: str | None = None
    local_only: bool = False
    reconciler: ReconciliationAdapter | None = None
    fence_target: FenceTarget | None = None

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name
                or not isinstance(self.argv, tuple) or not self.argv
                or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv)
                or not Path(self.argv[0]).is_absolute()):
            raise ValueError("Profile requires a name and a preconfigured absolute executable argv")
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        if self.execution_class not in {"isolated", "shared_unfenced", "resource_fenced"}:
            raise ValueError("Unimplemented execution class")
        if type(self.local_only) is not bool:
            raise ValueError("local_only must be explicit boolean")
        if self.reconciler is not None and not callable(getattr(self.reconciler, "reconcile", None)):
            raise ValueError("Reconciliation adapter is not implemented")
        if self.fence_target is not None and not callable(getattr(self.fence_target, "install", None)):
            raise ValueError("Resource fence target is not implemented")
        if self.execution_class == "shared_unfenced" and not (self.local_only or self.reconciler):
            raise ValueError("Shared external effects require a registered reconciliation adapter")
        if self.execution_class == "resource_fenced" and not (self.fence_target and self.reconciler):
            raise ValueError("Fenced execution requires an implemented target and reconciler")
        if (self.repository is None) != (self.base_commit is None):
            raise ValueError("Repository work requires an explicit immutable base commit")
        if self.repository is not None:
            object.__setattr__(self, "repository", Path(self.repository))
            if not isinstance(self.base_commit, str) or not re.fullmatch(
                    r"[0-9a-f]{40}|[0-9a-f]{64}", self.base_commit):
                raise ValueError("base_commit must be an immutable full object ID, not a ref")

    def prepare(self, task: dict) -> Workspace:
        task_id, attempt_id = task["id"], task["attempt"]["id"]
        if not re.fullmatch(r"tsk_[A-Za-z0-9_-]+", task_id) or not re.fullmatch(
                r"att_[A-Za-z0-9_-]+", attempt_id):
            raise ExecutionError("unsafe_workspace", "Invalid task/attempt path component")
        _directory(self.workspace_root)
        task_root = self.workspace_root / task_id
        _directory(task_root)
        root = task_root / attempt_id
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ExecutionError("workspace_exists", "Never reuse an attempt workspace") from error
        work = root / "work"
        if self.repository is None:
            work.mkdir(mode=0o700)
        else:
            _directory(self.repository, create=False)
            args = ["git", "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
                    "-C", str(self.repository)]
            try:
                kind = subprocess.check_output(args + ["cat-file", "-t", self.base_commit],
                                               stderr=subprocess.PIPE, timeout=15, text=True).strip()
                if kind != "commit":
                    raise ExecutionError("invalid_base", "base_commit must name a commit object")
                subprocess.run(args + ["worktree", "add", "--detach", str(work), self.base_commit],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise ExecutionError("worktree_failed", "Local immutable worktree preparation failed") from error
        metadata = root / "runtime"
        input_path = metadata / "input.json"
        JsonStore(input_path).write(copy.deepcopy(task))
        preparation = metadata / "prepared.json"
        evidence = {"references": [preparation.as_uri()], "prepared": True}
        if self.execution_class == "resource_fenced":
            fences = copy.deepcopy(task["attempt"]["resource_fences"])
            references = _references(self.fence_target.install(fences))
            evidence["references"] = _references(evidence["references"] + references)
            evidence["resource_fences"] = fences
        JsonStore(preparation).write({"task_id": task_id, "attempt_id": attempt_id,
                                     "claim_generation": task["claim_generation"],
                                     "working_directory": str(work), "base_commit": self.base_commit,
                                     "evidence": evidence})
        return Workspace(root, work, input_path, metadata / "result.json",
                         metadata / "checkpoint.json", evidence)

    def _proof(self, task, workspace, *, process_stopped, recovering, required_fences):
        if self.execution_class == "isolated":
            proof = {"references": workspace.evidence["references"]}
            if recovering:
                if task["recovery"] is None or task["attempt"]["status"] != "revoked":
                    raise ExecutionError("reconciliation_required", "Authority revocation must precede recovery")
                proof["publication_revoked"] = True
        elif self.reconciler is not None:
            proof = self.reconciler.reconcile(
                copy.deepcopy(task), workspace, process_stopped=process_stopped,
                recovering=recovering, required_fences=copy.deepcopy(required_fences))
        elif self.local_only and process_stopped:
            proof = {"references": workspace.evidence["references"],
                     "process_stopped": True, "effects_reconciled": True}
        else:
            raise ExecutionError("reconciliation_required", "Owned process quiescence not established")
        if not isinstance(proof, dict):
            raise ExecutionError("reconciliation_required", "Adapter must provide explicit evidence")
        _references(proof.get("references"))
        if self.execution_class != "isolated" and proof.get("effects_reconciled") is not True:
            raise ExecutionError("reconciliation_required", "External effects remain unresolved")
        if self.execution_class == "shared_unfenced" and not (
                process_stopped and proof.get("process_stopped") is True):
            raise ExecutionError("reconciliation_required", "Shared execution must be stopped")
        if (self.execution_class == "resource_fenced" and recovering
                and proof.get("installed_fences") != required_fences):
            raise ExecutionError("reconciliation_required", "Exact resource-global recovery fences required")
        return copy.deepcopy(proof)

    def settlement(self, task, workspace, *, process_stopped):
        return self._proof(task, workspace, process_stopped=process_stopped,
                           recovering=False, required_fences={})

    def recovery(self, task, workspace, *, process_stopped, required_fences):
        return self._proof(task, workspace, process_stopped=process_stopped,
                           recovering=True, required_fences=required_fences)


def _artifact(path, task, fields):
    try:
        if path.lstat().st_size > 65536:
            raise ExecutionError("invalid_artifact", "Worker artifact exceeds 64 KiB")
    except FileNotFoundError:
        return None
    value = JsonStore(path).read()
    identity = {"schema_version": 1, "task_id": task["id"],
                "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"]}
    if (not isinstance(value, dict) or not set(identity) | fields >= value.keys()
            or not set(identity) <= value.keys()
            or any(type(value.get(k)) is not type(v) or value.get(k) != v for k, v in identity.items())):
        raise ExecutionError("invalid_artifact", "Artifact schema or attempt identity is stale/invalid")
    return value


def _checkpoint(value):
    if (not isinstance(value, dict) or set(value) != {"sequence", "reference"}
            or type(value["sequence"]) is not int or value["sequence"] < 1):
        raise ExecutionError("invalid_artifact", "Checkpoint requires a positive monotonic sequence")
    _references([value["reference"]])


def read_result(workspace: Workspace, task: dict) -> dict | None:
    value = _artifact(workspace.result_path, task, {"summary", "evidence", "checkpoint"})
    if value is None:
        return None
    if (not isinstance(value.get("summary"), str) or not value["summary"].strip()
            or len(value["summary"].encode("utf-8")) > 8192):
        raise ExecutionError("invalid_artifact", "Explicit nonempty completion summary required")
    _references(value.get("evidence"))
    if "checkpoint" in value:
        _checkpoint(value["checkpoint"])
    return value


def read_checkpoint(workspace: Workspace, task: dict) -> dict | None:
    value = _artifact(workspace.checkpoint_path, task, {"sequence", "reference"})
    if value is not None:
        _checkpoint({k: v for k, v in value.items()
                     if k not in {"schema_version", "task_id", "attempt_id", "claim_generation"}})
    return value


def _linux_execution():
    if sys.platform != "linux":
        raise ExecutionError("unsupported_platform", "Owned process supervision requires Linux waitid")
    from . import execution_linux

    return execution_linux


def require_process_supervision():
    """Check optional local supervision before claims, never during import."""
    _linux_execution().require_process_supervision()


class OwnedProcess:
    """Lazy public constructor for the optional owned-process implementation.

    The Linux implementation subclasses this public type, retaining instance
    checks and the existing constructor without loading containment at import.
    Platform checks belong here, not in harmless profile/artifact operations.
    Direct callers retain monotonic deadlines unless elapsed_time is supplied.
    """
    def __new__(cls, profile: LocalProfile, workspace: Workspace, *, deadline: float,
                elapsed_time: Callable[[], float] | None = None):
        if cls is OwnedProcess:
            return object.__new__(_linux_execution().OwnedProcess)
        return super().__new__(cls)
