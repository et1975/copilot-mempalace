"""Tests for restoring drawers from the dreaming prune archive."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

import dream_restore


def _test_tmpdir():
    return tempfile.TemporaryDirectory(
        prefix="dream-restore-",
        dir=os.environ.get("DREAMING_TEST_TMPDIR", os.getcwd()),
    )


def _record(
    logical_id: str,
    *,
    reason: str = "prune",
    wing: str = "wing",
    room: str = "room",
    archived_at: str = "2026-07-06T15:00:00+00:00",
) -> dict:
    return {
        "schema": 1,
        "id": logical_id,
        "member_ids": [f"{logical_id}-chunk-2", f"{logical_id}-chunk-1"],
        "wing": wing,
        "room": room,
        "salience": {"v": 0.1},
        "reason": reason,
        "archived_at": archived_at,
        "rows": [
            {
                "id": f"{logical_id}-chunk-1",
                "document": f"{logical_id} first",
                "metadata": {"chunk_index": 1, "source": "first"},
                "embedding": [1.0],
            },
            {
                "id": f"{logical_id}-chunk-2",
                "document": f"{logical_id} second",
                "metadata": {"chunk_index": 0, "source": "second"},
                "embedding": [2.0],
            },
        ],
    }


def _write_archive(path: str, entries: list[object]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            if entry == "":
                fh.write("\n")
            elif isinstance(entry, str):
                fh.write(entry + "\n")
            else:
                fh.write(json.dumps(entry) + "\n")


class FakeWriter:
    def __init__(self, fail_ids: set[str] | None = None):
        self.calls: list[dict] = []
        self.fail_ids = fail_ids or set()

    def add_drawer(self, wing, room, content, added_by="dreaming", metadata=None):
        original_id = metadata.get("original_id") if metadata else None
        if original_id in self.fail_ids:
            raise RuntimeError(f"cannot restore {original_id}")
        self.calls.append(
            {
                "wing": wing,
                "room": room,
                "content": content,
                "added_by": added_by,
                "metadata": metadata,
            }
        )
        return {"drawer_id": f"new-{original_id}"}


class TestIterArchiveRecords(unittest.TestCase):
    def test_parses_valid_lines_skips_blanks_and_filters_by_id_and_reason(self):
        with _test_tmpdir() as td:
            archive = os.path.join(td, "archive.jsonl")
            first = _record("logical-1", reason="prune")
            second = _record("logical-2", reason="merge")
            third = _record("logical-3", reason="prune")
            _write_archive(archive, [first, "", second, third])

            self.assertEqual(
                [record["id"] for record in dream_restore.iter_archive_records(archive)],
                ["logical-1", "logical-2", "logical-3"],
            )
            self.assertEqual(
                [record["id"] for record in dream_restore.iter_archive_records(archive, id_filter="logical-2")],
                ["logical-2"],
            )
            self.assertEqual(
                [record["id"] for record in dream_restore.iter_archive_records(archive, reason_filter="prune")],
                ["logical-1", "logical-3"],
            )

    def test_malformed_line_is_skipped_unless_strict(self):
        with _test_tmpdir() as td:
            archive = os.path.join(td, "archive.jsonl")
            good = _record("logical-1")
            _write_archive(archive, ["{not json", good])

            self.assertEqual(
                [record["id"] for record in dream_restore.iter_archive_records(archive)],
                ["logical-1"],
            )
            with self.assertRaises(ValueError):
                list(dream_restore.iter_archive_records(archive, strict=True))


class TestRecordToContent(unittest.TestCase):
    def test_concatenates_row_documents_in_member_id_order(self):
        record = _record("logical-1")

        self.assertEqual(
            dream_restore.record_to_content(record),
            "logical-1 second\nlogical-1 first",
        )


class TestRestore(unittest.TestCase):
    def test_restores_each_record_with_original_location_content_and_metadata(self):
        records = [
            _record("logical-1", wing="w1", room="r1", reason="prune"),
            _record("logical-2", wing="w2", room="r2", reason="merge"),
        ]
        writer = FakeWriter()

        report = dream_restore.restore(records, writer)

        self.assertEqual(report["restored"], 2)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["errors"], [])
        self.assertEqual([call["wing"] for call in writer.calls], ["w1", "w2"])
        self.assertEqual([call["room"] for call in writer.calls], ["r1", "r2"])
        self.assertEqual(writer.calls[0]["content"], "logical-1 second\nlogical-1 first")
        self.assertEqual(writer.calls[0]["added_by"], "dreaming")
        self.assertEqual(
            writer.calls[0]["metadata"],
            {
                "restored_from_archive": "2026-07-06T15:00:00+00:00",
                "original_id": "logical-1",
                "reason": "prune",
                "original_metadata_by_member_id": {
                    "logical-1-chunk-1": {"chunk_index": 1, "source": "first"},
                    "logical-1-chunk-2": {"chunk_index": 0, "source": "second"},
                },
            },
        )

    def test_dry_run_reports_preview_and_does_not_call_writer(self):
        out = io.StringIO()
        writer = FakeWriter()

        report = dream_restore.restore([_record("logical-1")], writer, dry_run=True, out=out)

        self.assertEqual(report["restored"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(writer.calls, [])
        self.assertIn("WOULD restore logical-1 to wing/room", out.getvalue())
        self.assertIn("chars=32", out.getvalue())

    def test_main_id_filter_restores_only_matching_record_with_fake_writer(self):
        with _test_tmpdir() as td:
            archive = os.path.join(td, "archive.jsonl")
            _write_archive(archive, [_record("logical-1"), _record("logical-2")])
            writer = FakeWriter()

            rc = dream_restore.main(
                ["--palace", td, "--archive-file", archive, "--id", "logical-2"],
                writer_factory=lambda: writer,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(writer.calls[0]["metadata"]["original_id"], "logical-2")

    def test_record_failure_is_recorded_and_does_not_stop_later_records(self):
        records = [_record("bad"), _record("good")]
        writer = FakeWriter(fail_ids={"bad"})

        report = dream_restore.restore(records, writer)

        self.assertEqual(report["restored"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["id"], "bad")
        self.assertEqual(writer.calls[0]["metadata"]["original_id"], "good")


if __name__ == "__main__":
    unittest.main()
