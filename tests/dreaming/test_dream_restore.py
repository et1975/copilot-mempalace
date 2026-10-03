"""Tests for restoring drawers from the dreaming prune archive."""
from __future__ import annotations

import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock

import pytest

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


def _source_archive_record():
    from dream_metadata import canonical_json, content_hash
    from dream_procedural import EvidenceReference
    from dream_procedural_sources import OriginalSource, source_record_data

    session = "11111111-1111-4111-8111-111111111111"
    observed = datetime(2026, 9, 23, tzinfo=timezone.utc)
    text = "original observation"
    reference = EvidenceReference(
        "session_turn", session, session, text, content_hash(text), 0, "user_message")
    body, metadata = source_record_data(
        OriginalSource(reference, "owner/repo", observed, text),
        captured_at=observed, captured_by="archive-test")
    record = _record("captured-source", room="procedural-sources")
    record["member_ids"] = ["captured-source"]
    record["rows"] = [{
        "id": "captured-source",
        "document": body + "\n\n<!--dreaming-meta: " + canonical_json(metadata) + "-->",
        "metadata": {"room": "procedural-sources", "added_by": "dream-procedure-source"},
    }]
    return record


@pytest.mark.parametrize("dry_run", (False, True))
@pytest.mark.parametrize("mixed", (False, True), ids=("protected-only", "ordinary-first"))
def test_procedural_source_archive_blocks_complete_batch_before_writes(dry_run, mixed):
    protected = _source_archive_record()
    records = ([_record("ordinary")] if mixed else []) + [protected]
    before = deepcopy(records)
    writer, out = FakeWriter(), io.StringIO()

    report = dream_restore.restore(iter(records), writer, dry_run=dry_run, out=out)

    assert report["restored"] == 0
    assert writer.calls == []
    assert report["errors"] and report["errors"][0]["id"] == "captured-source"
    assert "procedural" in report["errors"][0]["error"].lower()
    assert "physical" in report["errors"][0]["error"].lower()
    assert "WOULD restore" not in out.getvalue()
    assert records == before


@pytest.mark.parametrize("location", ("archive-scope", "archive-metadata", "row-metadata",
                                     "omitted-row", "row-trailer", "wrapped-trailer", "split-trailer"))
@pytest.mark.parametrize("kind,room", (("procedural_source", "procedural-sources"),
                                    ("procedural_event", "procedural")))
def test_all_archive_provenance_channels_preflight_before_ordinary_replay(location, kind, room):
    record = _record("protected")
    if location == "archive-scope":
        record["room"] = room
    elif location == "archive-metadata":
        record["metadata"] = {"kind": kind}
    elif location in {"row-metadata", "omitted-row"}:
        record["rows"][0]["metadata"]["kind"] = kind
        if location == "omitted-row":
            record["member_ids"] = [record["rows"][1]["id"]]
    elif location in {"row-trailer", "wrapped-trailer"}:
        record["rows"][0]["document"] += '\n\n<!--dreaming-meta: {"kind":"' + kind + '"}-->'
        if location == "wrapped-trailer":
            record["rows"][0]["document"] += '\n\n<!--dreaming-meta: {"restored":true}-->'
    else:
        record["rows"][1]["document"] = "payload\n\n<!--dreaming-met"
        record["rows"][0]["document"] = 'a: {"kind":"' + kind + '"}-->'
    writer = FakeWriter()

    report = dream_restore.restore([_record("ordinary"), record], writer)

    assert report["restored"] == 0 and writer.calls == []
    assert report["errors"] and "procedural" in report["errors"][0]["error"].lower()


@pytest.mark.parametrize("metadata", ({"added_by": "dream-procedure-source"},
                                     {"kind": "procedural_source", "room": "ordinary"}))
def test_source_reservation_metadata_blocks_archive_replay(metadata):
    record = _record("protected")
    record["rows"][0]["metadata"].update(metadata)
    writer = FakeWriter()

    report = dream_restore.restore([_record("ordinary"), record], writer)

    assert writer.calls == [] and report["restored"] == 0
    assert report["errors"]


def test_malformed_selected_trailer_cannot_hide_protected_metadata():
    record = _record("broken")
    record["rows"][0]["document"] = '<!--dreaming-meta: {"kind":"procedural_source"'
    writer = FakeWriter()

    report = dream_restore.restore([_record("ordinary"), record], writer)

    assert writer.calls == [] and report["restored"] == 0
    assert report["errors"] and report["errors"][0]["id"] == "broken"


@pytest.mark.parametrize("dry_run", (False, True))
def test_cli_rejects_selected_procedural_archive_before_writer_construction(tmp_path, capsys, dry_run):
    archive = tmp_path / "archive.jsonl"
    _write_archive(str(archive), [_record("ordinary"), _source_archive_record()])
    before = archive.read_bytes()
    factory = Mock(return_value=FakeWriter())
    args = ["--palace", str(tmp_path), "--archive-file", str(archive)]
    if dry_run:
        args.append("--dry-run")

    result = dream_restore.main(args, writer_factory=factory)

    assert result == 1
    factory.assert_not_called()
    output = capsys.readouterr()
    assert "procedural" in output.err.lower() and "physical" in output.err.lower()
    assert "WOULD restore" not in output.out
    assert archive.read_bytes() == before


def test_cli_preflight_is_scoped_to_selected_records_and_preserves_ordinary_restore(tmp_path):
    archive = tmp_path / "archive.jsonl"
    _write_archive(str(archive), [_source_archive_record(), _record("ordinary")])
    writer = FakeWriter()

    result = dream_restore.main(
        ["--palace", str(tmp_path), "--archive-file", str(archive), "--id", "ordinary"],
        writer_factory=lambda: writer)

    assert result == 0
    assert [call["metadata"]["original_id"] for call in writer.calls] == ["ordinary"]


def test_fenced_metadata_examples_are_not_procedural_archives():
    record = _record("ordinary")
    record["rows"][0]["document"] = (
        'Metadata example:\n```html\n<!--dreaming-meta: {"kind":"procedural_source"}-->\n```')
    record["rows"][0]["metadata"]["added_by"] = "dreaming"
    writer = FakeWriter()

    report = dream_restore.restore([record], writer)

    assert report["restored"] == 1 and not report["errors"]
    assert writer.calls[0]["content"] == dream_restore.record_to_content(record)


@pytest.mark.parametrize("location", ("scope", "row-metadata"))
@pytest.mark.parametrize("strict", (False, True))
def test_cli_refuses_duplicate_keys_that_hide_procedural_archive_provenance(tmp_path, location, strict):
    record = _record("protected")
    if location == "scope":
        raw = json.dumps(record).replace(
            '"room": "room"', '"room": "procedural-sources", "room": "room"')
    else:
        record["rows"][0]["metadata"]["kind"] = "ordinary"
        raw = json.dumps(record).replace(
            '"kind": "ordinary"', '"kind": "procedural_source", "kind": "ordinary"')
    archive = tmp_path / "ambiguous.jsonl"
    _write_archive(str(archive), [_record("ordinary"), raw])
    before = archive.read_bytes()
    factory = Mock(return_value=FakeWriter())
    args = ["--palace", str(tmp_path), "--archive-file", str(archive)]
    if strict:
        args.append("--strict")

    result = dream_restore.main(args, writer_factory=factory)

    assert result == 1
    factory.assert_not_called()
    assert archive.read_bytes() == before


def _logical_archive(text, split_at=None, *, added_by="dreaming"):
    pieces = [text] if split_at is None else [text[:split_at], text[split_at:]]
    rows = [
        {"id": f"logical-{index}", "document": piece,
         "metadata": {"added_by": added_by, "chunk_index": index}}
        for index, piece in enumerate(pieces)
    ]
    record = _record("logical")
    record["member_ids"] = [row["id"] for row in rows]
    record["rows"] = list(reversed(rows))
    return record


@pytest.mark.parametrize("kind", ("procedural_source", "procedural_event"))
@pytest.mark.parametrize("split", (False, True), ids=("whole", "split-marker"))
@pytest.mark.parametrize("dry_run", (False, True))
def test_unfinished_fence_writer_trailer_and_wrapper_block_whole_batch(kind, split, dry_run):
    text = (
        '```text\nunfinished original field\n\n'
        '<!--dreaming-meta: {"kind":"' + kind + '"}-->\n\n'
        '<!--dreaming-meta: {}-->')
    split_at = text.index("<!--dreaming-meta:") + len("<!--dreaming-") if split else None
    protected = _logical_archive(text, split_at)
    records = [_record("ordinary"), protected]
    before = deepcopy(records)
    writer, output = FakeWriter(), io.StringIO()

    report = dream_restore.restore(iter(records), writer, dry_run=dry_run, out=output)

    assert report["restored"] == 0 and writer.calls == []
    assert report["errors"] and report["errors"][0]["id"] == "logical"
    assert "procedural" in report["errors"][0]["error"].lower()
    assert "WOULD restore" not in output.getvalue()
    assert records == before


@pytest.mark.parametrize("split", ("whole", "before-marker", "inside-marker",
                                 "inside-opening-fence", "inside-closing-fence"))
@pytest.mark.parametrize("dry_run", (False, True))
def test_closed_fenced_metadata_example_has_whole_and_chunked_equivalence(split, dry_run):
    text = (
        'Ordinary documentation\n```html\n'
        '<!--dreaming-meta: {"kind":"procedural_source"}-->\n'
        '```\n\n<!--dreaming-meta: {"topic":"documentation"}-->')
    cuts = {
        "whole": None,
        "before-marker": text.index("<!--dreaming-meta:"),
        "inside-marker": text.index("<!--dreaming-meta:") + len("<!--dreaming-"),
        "inside-opening-fence": text.index("```html") + 1,
        "inside-closing-fence": text.index("\n```\n") + 2,
    }
    ordinary = _logical_archive(text, cuts[split])
    records = [_record("first-ordinary"), ordinary]
    writer, output = FakeWriter(), io.StringIO()

    report = dream_restore.restore(records, writer, dry_run=dry_run, out=output)

    assert report["errors"] == []
    if dry_run:
        assert report["skipped"] == 2 and writer.calls == []
        assert "WOULD restore logical" in output.getvalue()
    else:
        assert report["restored"] == 2
        assert writer.calls[1]["content"] == dream_restore.record_to_content(ordinary)


@pytest.mark.parametrize("dry_run", (False, True))
def test_cli_combined_fence_split_and_wrapper_refuses_before_writer(tmp_path, capsys, dry_run):
    text = (
        '```text\nunfinished\n<!--dreaming-meta: {"kind":"procedural_source"}-->\n'
        '<!--dreaming-meta: {"restored":true}-->')
    record = _logical_archive(text, text.index("<!--dreaming-meta:") + 8)
    archive = tmp_path / "combined.jsonl"
    _write_archive(str(archive), [_record("ordinary"), record])
    factory = Mock(return_value=FakeWriter())
    args = ["--palace", str(tmp_path), "--archive-file", str(archive)]
    if dry_run:
        args.append("--dry-run")

    assert dream_restore.main(args, writer_factory=factory) == 1

    factory.assert_not_called()
    output = capsys.readouterr()
    assert "procedural" in output.err.lower()
    assert "WOULD restore" not in output.out


@pytest.mark.parametrize("trailers", (32, 33))
def test_terminal_trailer_peeling_has_explicit_exact_limit(trailers):
    text = "ordinary" + '\n\n<!--dreaming-meta: {}-->' * trailers
    writer = FakeWriter()

    report = dream_restore.restore([_logical_archive(text)], writer)

    if trailers == 32:
        assert report["restored"] == 1 and not report["errors"]
    else:
        assert writer.calls == [] and report["restored"] == 0
        assert "limit" in report["errors"][0]["error"].lower()


def test_conflicting_logical_writer_context_is_not_silently_discarded():
    record = _logical_archive("ordinary content", 8)
    record["rows"][0]["metadata"]["added_by"] = "another-writer"
    writer = FakeWriter()

    report = dream_restore.restore([_record("ordinary-first"), record], writer)

    assert writer.calls == [] and report["restored"] == 0
    assert "writer" in report["errors"][0]["error"].lower()


if __name__ == "__main__":
    unittest.main()
