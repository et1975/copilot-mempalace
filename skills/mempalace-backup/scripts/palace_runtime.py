"""Stdlib-only path compatibility for supported MemPalace runtimes."""

from __future__ import annotations

import importlib.metadata
import os
import re
import sqlite3
from pathlib import Path


class LayoutError(ValueError):
    """The runtime's effective storage path cannot be inferred safely."""


def installed_version() -> str | None:
    try:
        return importlib.metadata.version("mempalace")
    except importlib.metadata.PackageNotFoundError:
        return None


def runtime_family() -> str:
    version = installed_version()
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version or "")
    if match:
        major, minor = (int(value) for value in match.groups())
        if (major, minor) == (3, 12):
            return "modern"
        if (major, minor) == (3, 8):
            return "legacy"
    return "unknown"


def data_environment_path() -> str | None:
    primary = os.environ.get("MEMPALACE_PALACE_PATH")
    short = os.environ.get("MEMPALACE_PALACE")
    legacy = os.environ.get("MEMPAL_PALACE_PATH")
    if primary:
        return primary
    if short:
        family = runtime_family()
        if family == "modern":
            return short
        if family == "legacy":
            return legacy or None
        raise LayoutError(
            "MEMPALACE_PALACE is version-dependent; use --data-path or an "
            "interpreter from the running MemPalace installation."
        )
    return legacy or None


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def state_graph() -> Path:
    return Path.home() / ".mempalace" / "knowledge_graph.sqlite3"


def graph_present(path: Path) -> bool:
    for component in (path, *path.parents):
        if component.is_symlink() and not component.exists():
            raise OSError(f"Knowledge graph path has a dangling symlink: {component}")
    if path.exists() and not path.is_file():
        raise OSError(f"Knowledge graph is not a regular file: {path}")
    return path.is_file()


def graph_has_rows(path: Path) -> bool:
    """Match the runtime's conservative read-only graph presence check."""
    if not graph_present(path):
        return False
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return True
    try:
        for table in ("triples", "entities"):
            try:
                if con.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]:
                    return True
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    return True
        return False
    except sqlite3.Error:
        return True
    finally:
        con.close()
