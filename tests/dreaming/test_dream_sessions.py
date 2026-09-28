from __future__ import annotations

import ast
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import dream_sessions


class DreamSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_root = Path(os.path.expanduser("~/.copilot/session-state/dream-sessions-tests"))
        self._temp_root.mkdir(parents=True, exist_ok=True)
        self._tempdir = tempfile.mkdtemp(prefix="fixture-", dir=str(self._temp_root))
        self.db_path = os.path.join(self._tempdir, "session-store.db")

        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(
                """
                CREATE TABLE sessions(
                    id TEXT,
                    cwd TEXT,
                    repository TEXT,
                    branch TEXT,
                    summary TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    host_type TEXT
                );
                CREATE TABLE turns(
                    id INTEGER,
                    session_id TEXT,
                    turn_index INTEGER,
                    user_message TEXT,
                    assistant_response TEXT,
                    timestamp TEXT
                );
                """
            )
            con.executemany(
                """
                INSERT INTO sessions(
                    id, cwd, repository, branch, summary, created_at, updated_at, host_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "s1",
                        "/work/foo",
                        "foo/bar",
                        "main",
                        "First foo session",
                        "2026-07-01T10:00:00+00:00",
                        "2026-07-01T10:30:00+00:00",
                        "copilot-cli",
                    ),
                    (
                        "s2",
                        "/work/baz",
                        "baz/qux",
                        "feature/a",
                        "Baz session",
                        "2026-07-02T10:00:00+00:00",
                        "2026-07-02T10:30:00+00:00",
                        "copilot-cli",
                    ),
                    (
                        "s3",
                        "/work/foo",
                        "foo/bar",
                        "feature/b",
                        "Second foo session",
                        "2026-07-03T10:00:00+00:00",
                        "2026-07-03T10:30:00+00:00",
                        "copilot-cli",
                    ),
                ],
            )
            con.executemany(
                """
                INSERT INTO turns(
                    id, session_id, turn_index, user_message, assistant_response, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "s1", 2, "Second user intent", "reply 2", "2026-07-01T10:02:00+00:00"),
                    (2, "s1", 1, "First user intent", "reply 1", "2026-07-01T10:01:00+00:00"),
                    (3, "s1", 3, "", "empty ignored", "2026-07-01T10:03:00+00:00"),
                    (4, "s2", 1, "Baz asks for help", "reply", "2026-07-02T10:01:00+00:00"),
                    (5, "s3", 1, None, "none ignored", "2026-07-03T10:01:00+00:00"),
                    (6, "s3", 2, "Third session preference", "reply", "2026-07-03T10:02:00+00:00"),
                ],
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self) -> None:
        shutil.rmtree(self._tempdir)
        try:
            self._temp_root.rmdir()
        except OSError:
            pass

    def _row_counts(self) -> tuple[int, int]:
        con = sqlite3.connect(self.db_path)
        try:
            sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            turns = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            return sessions, turns
        finally:
            con.close()

    def test_load_sessions_returns_ordered_session_dicts(self) -> None:
        before = self._row_counts()

        sessions = dream_sessions.load_sessions(self.db_path)

        self.assertEqual(["s1", "s2", "s3"], [session["session_id"] for session in sessions])
        self.assertEqual(
            {
                "session_id",
                "repository",
                "branch",
                "summary",
                "created_at",
                "updated_at",
                "cwd",
            },
            set(sessions[0]),
        )
        self.assertEqual("foo/bar", sessions[0]["repository"])
        self.assertEqual(before, self._row_counts())

    def test_load_sessions_filters_by_repository_since_and_limit(self) -> None:
        self.assertEqual(
            ["s1", "s3"],
            [session["session_id"] for session in dream_sessions.load_sessions(self.db_path, repository="foo")],
        )
        self.assertEqual(
            ["s2", "s3"],
            [
                session["session_id"]
                for session in dream_sessions.load_sessions(
                    self.db_path, since="2026-07-02T00:00:00+00:00"
                )
            ],
        )
        self.assertEqual(["s1"], [session["session_id"] for session in dream_sessions.load_sessions(self.db_path, limit=1)])

    def test_load_sessions_missing_db_returns_empty_list(self) -> None:
        missing = os.path.join(self._tempdir, "missing.db")

        self.assertEqual([], dream_sessions.load_sessions(missing))

    def test_load_session_turns_returns_turns_ordered_by_turn_index(self) -> None:
        before = self._row_counts()

        turns = dream_sessions.load_session_turns("s1", self.db_path)

        self.assertEqual([1, 2, 3], [turn["turn_index"] for turn in turns])
        self.assertEqual(["First user intent", "Second user intent", ""], [turn["user_message"] for turn in turns])
        self.assertEqual(before, self._row_counts())

    def test_load_session_turns_unknown_session_returns_empty_list(self) -> None:
        self.assertEqual([], dream_sessions.load_session_turns("missing", self.db_path))

    def test_load_session_observations_preserve_session_identity_and_turn_text(self) -> None:
        before = self._row_counts()

        observations = dream_sessions.load_session_observations(self.db_path)

        by_id = {observation["session_id"]: observation for observation in observations}
        self.assertEqual(["s1", "s2", "s3"], [observation["session_id"] for observation in observations])
        self.assertEqual("First user intent\nSecond user intent", by_id["s1"]["text"])
        self.assertEqual(3, by_id["s1"]["turn_count"])
        self.assertEqual("foo/bar", by_id["s1"]["repository"])
        self.assertEqual("2026-07-01T10:00:00+00:00", by_id["s1"]["created_at"])
        self.assertEqual("First foo session", by_id["s1"]["summary"])
        self.assertEqual("Third session preference", by_id["s3"]["text"])
        self.assertEqual(before, self._row_counts())

    def test_load_session_observations_honor_repository_since_and_limit_filters(self) -> None:
        self.assertEqual(
            ["s1", "s3"],
            [
                observation["session_id"]
                for observation in dream_sessions.load_session_observations(self.db_path, repository="foo")
            ],
        )
        self.assertEqual(
            ["s2", "s3"],
            [
                observation["session_id"]
                for observation in dream_sessions.load_session_observations(
                    self.db_path, since="2026-07-02T00:00:00+00:00"
                )
            ],
        )
        self.assertEqual(
            ["s1"],
            [observation["session_id"] for observation in dream_sessions.load_session_observations(self.db_path, limit_sessions=1)],
        )

    def test_load_session_observations_truncates_oversized_text(self) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                """
                INSERT INTO turns(
                    id, session_id, turn_index, user_message, assistant_response, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (7, "s2", 2, "x" * 5000, "reply", "2026-07-02T10:02:00+00:00"),
            )
            con.commit()
        finally:
            con.close()

        observations = dream_sessions.load_session_observations(self.db_path, repository="baz")

        self.assertEqual(1, len(observations))
        self.assertLessEqual(len(observations[0]["text"]), 4000)
        self.assertTrue(observations[0]["text"].endswith("…"))

    def test_module_imports_no_mempalace_dependency(self) -> None:
        source = Path(dream_sessions.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        self.assertFalse([name for name in imported_modules if name == "mempalace" or name.startswith("mempalace.")])


if __name__ == "__main__":
    unittest.main()
