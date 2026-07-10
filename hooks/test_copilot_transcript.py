"""Tests for the Copilot -> Claude transcript adapter (copilot_transcript.py).

Pure translation/mapping tests run under any python3. The integration tests
prove mempalace's own claude-code parsers accept our translated output; they
are skipped unless mempalace is importable (run under the mempalace interpreter,
e.g. MPY=$(head -1 "$(command -v mempalace)" | sed 's/^#!//'); "$MPY" -m unittest).
"""
from __future__ import annotations

import json
import unittest

import copilot_transcript as ct

try:  # mempalace is only importable under its own venv interpreter
    from mempalace import hooks_cli as _h  # type: ignore

    _HAS_MEMPALACE = True
except Exception:  # pragma: no cover - depends on interpreter
    _h = None
    _HAS_MEMPALACE = False


def _events(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


class TranslateEventsTests(unittest.TestCase):
    def test_user_message_becomes_claude_user_line(self):
        out = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "hello"}}),
            cwd="/repo",
        )
        self.assertEqual(len(out), 1)
        line = json.loads(out[0])
        self.assertEqual(line["type"], "user")
        self.assertEqual(line["message"], {"role": "user", "content": "hello"})

    def test_assistant_message_becomes_claude_assistant_line(self):
        out = ct.translate_events(
            _events({"type": "assistant.message", "data": {"content": "hi there"}}),
            cwd="/repo",
        )
        line = json.loads(out[0])
        self.assertEqual(line["type"], "assistant")
        self.assertEqual(line["message"], {"role": "assistant", "content": "hi there"})

    def test_non_message_events_are_skipped(self):
        out = ct.translate_events(
            _events(
                {"type": "session.start", "data": {"sessionId": "x"}},
                {"type": "assistant.turn_start", "data": {"turnId": "0"}},
                {"type": "hook.start", "data": {}},
            ),
            cwd="/repo",
        )
        self.assertEqual(out, [])

    def test_uses_clean_content_not_transformed(self):
        out = ct.translate_events(
            _events(
                {
                    "type": "user.message",
                    "data": {
                        "content": "the real question",
                        "transformedContent": "<system_reminder>noise</system_reminder>",
                    },
                }
            ),
            cwd="/repo",
        )
        line = json.loads(out[0])
        self.assertEqual(line["message"]["content"], "the real question")

    def test_each_line_carries_top_level_cwd(self):
        out = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": "a"}},
                {"type": "assistant.message", "data": {"content": "b"}},
            ),
            cwd="/home/e/proj",
        )
        for raw in out:
            self.assertEqual(json.loads(raw)["cwd"], "/home/e/proj")

    def test_empty_and_missing_content_skipped(self):
        out = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": ""}},
                {"type": "user.message", "data": {"content": "   "}},
                {"type": "user.message", "data": {}},
            ),
            cwd="/repo",
        )
        self.assertEqual(out, [])

    def test_malformed_lines_are_skipped(self):
        out = ct.translate_events(
            ["not json", "", "{bad", json.dumps({"type": "user.message", "data": {"content": "ok"}})],
            cwd="/repo",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(out[0])["message"]["content"], "ok")


class MapHookTests(unittest.TestCase):
    def test_known_event_names_map_to_cli_flags(self):
        self.assertEqual(ct.map_hook("Stop"), "stop")
        self.assertEqual(ct.map_hook("PreCompact"), "precompact")
        self.assertEqual(ct.map_hook("SessionStart"), "session-start")
        self.assertEqual(ct.map_hook("SessionEnd"), "session-end")

    def test_event_names_are_case_insensitive(self):
        self.assertEqual(ct.map_hook("stop"), "stop")
        self.assertEqual(ct.map_hook("preCompact"), "precompact")

    def test_unknown_event_returns_none(self):
        self.assertIsNone(ct.map_hook("PreToolUse"))
        self.assertIsNone(ct.map_hook(""))


@unittest.skipUnless(_HAS_MEMPALACE, "requires the mempalace interpreter")
class MempalaceParserIntegrationTests(unittest.TestCase):
    """The translated output must be parseable by mempalace's claude-code path."""

    def _write(self, lines: list[str]) -> str:
        import tempfile

        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write("\n".join(lines) + "\n")
        f.close()
        return f.name

    def test_mempalace_counts_translated_human_messages(self):
        lines = ct.translate_events(
            _events(
                {"type": "user.message", "data": {"content": "q1"}},
                {"type": "assistant.message", "data": {"content": "a1"}},
                {"type": "user.message", "data": {"content": "q2"}},
            ),
            cwd="/repo",
        )
        path = self._write(lines)
        self.assertEqual(_h._count_human_messages(path), 2)

    def test_mempalace_extracts_translated_messages(self):
        lines = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "remember this"}}),
            cwd="/repo",
        )
        path = self._write(lines)
        self.assertEqual(_h._extract_recent_messages(path), ["remember this"])

    def test_mempalace_derives_wing_from_translated_cwd(self):
        lines = ct.translate_events(
            _events({"type": "user.message", "data": {"content": "x"}}),
            cwd="/home/e/copilot-mempalace",
        )
        path = self._write(lines)
        # mempalace slugifies the cwd leaf, replacing '-' with '_'.
        self.assertEqual(_h._wing_from_jsonl_cwd(path), "wing_copilot_mempalace")


if __name__ == "__main__":
    unittest.main()
