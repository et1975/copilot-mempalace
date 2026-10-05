"""Exact captured-source storage and metadata-only retention in disposable palaces."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dream_adopt
import dream_harvest
import dream_palace
from dream_metadata import canonical_json, content_hash
from dream_procedural import EvidenceReference, parse_event
from dream_procedural_validate import EvidenceUnavailable
from test_dream_procedural import NOW, event_data, evidence
from test_dream_procedural_palace import (
    DrawerCollection, SESSION, installed_palace, sanctioned_writer,
)

RAW = '  exact café\u2028text\n```json\n{"nested":"value"}\n\n'
DRAWER = f"SESSION_ID: {SESSION}\nOBSERVED_AT: 2026-09-23T00:00:00Z\noriginal observation"


def reference(text=RAW, **changes):
    return replace(EvidenceReference("session_turn", SESSION, SESSION, "exact",
                                    content_hash(text), 7, "user_message"), **changes)


def original(text=RAW, **changes):
    from dream_procedural_sources import OriginalSource
    return OriginalSource(reference(text, **changes), "owner/repo", NOW, text)


class SourceTests(unittest.TestCase):
    def test_new_packet_capture_is_rejected_before_any_writer_call(self):
        from delivery_fixtures import wrapped_packet
        text = wrapped_packet()
        before = deepcopy(self.collection.rows)
        with self.assertRaisesRegex(ValueError, "generated"):
            self.capture(original(text, quote="Add a parser regression"))
        self.assertEqual(self.collection.rows, before)

    def test_pre_upgrade_captured_transport_rejected_without_host_or_rewrite(self):
        from delivery_fixtures import applicability, case, wrapped_packet
        from dream_procedural_sources import _HEADER, _metadata, source_key
        from dream_procedural import to_data
        from dream_procedural_validate import EvidenceReader
        from receipt_fixtures import receipts
        _, current, _, guidance = case()
        texts = [
            *[canonical_json(r) for r in receipts()[:2]],
            *[wrapper.format(body) for kind in ("procedural_feedback", "procedural_feedback_abstention")
              for value in [canonical_json({"kind": kind, "quote": "parser"})]
              for body in (value, canonical_json(value))
              for wrapper in ("{}", "```json\n{}\n```", "Copied:\n{}\nEnd.")],
            wrapped_packet("Copied report:\n{}\nEnd.", encoded=True),
            canonical_json(applicability(current["context"], guidance["rules"])),
            canonical_json(dict(kind="procedural_use_check", authority="agent_reported",
                status="usable", items=guidance["rules"], checked_at=guidance["as_of"],
                notice="Cooperative current check, not semantic proof or a durable authorization token; recheck before action.")),
        ]
        for text in texts:
            ref = reference(text, quote="parser")
            key = source_key(ref)
            # Deliberately seed old bytes without the now-guarded capture constructor.
            data = dict(schema_version=1, identity={k: v for k, v in to_data(ref).items() if k != "quote"},
                        repository="owner/repo", observed_at="2026-09-23T00:00:00Z",
                        captured_at="2026-09-23T00:00:00Z", captured_by="legacy", captured_text=text)
            payload = canonical_json(data)
            digest = content_hash(payload)
            sanctioned_writer(self.path, self.collection).add_drawer(
                "w", "procedural-sources", _HEADER.format(key=key, digest=digest) + payload,
                added_by="dream-procedure-source", metadata=_metadata(key, digest))
            before = deepcopy(self.collection.rows)
            with self.assertRaisesRegex(ValueError, "generated"):
                EvidenceReader(self.path, "w").resolve(ref)
            self.assertEqual(self.collection.rows, before)

    def test_feedback_drawer_witness_cannot_rescue_copied_transport(self):
        from dream_procedural_sources import OriginalSource
        for kind in ("procedural_feedback", "procedural_feedback_abstention"):
            text = DRAWER + "\nCopied:\n" + canonical_json(canonical_json(
                {"kind": kind, "quote": "original observation"}))
            ref = EvidenceReference("drawer", "original", SESSION, "original observation",
                                    content_hash(text))
            self.collection.rows["original"] = {"id": "original", "text": text,
                "metadata": {"wing": "w", "room": "diary", "repository": "owner/repo"}}
            self.capture(OriginalSource(ref, "owner/repo", NOW))
            before = deepcopy(self.collection.rows)
            with self.assertRaisesRegex(ValueError, "generated"):
                self.resolve(ref)
            self.assertEqual(self.collection.rows, before)

    def test_pre_upgrade_drawer_witness_does_not_rescue_transport_body(self):
        from delivery_fixtures import wrapped_packet
        from dream_procedural_sources import OriginalSource
        text = DRAWER + "\n" + wrapped_packet()
        ref = EvidenceReference("drawer", "original", SESSION, "Add a parser regression",
                                content_hash(text))
        self.collection.rows["original"] = {"id": "original", "text": text,
            "metadata": {"wing": "w", "room": "diary", "repository": "owner/repo"}}
        self.capture(OriginalSource(ref, "owner/repo", NOW))
        before = deepcopy(self.collection.rows)
        with self.assertRaisesRegex(ValueError, "generated"):
            self.resolve(ref)
        self.assertEqual(self.collection.rows, before)

    def test_receipt_drawer_witness_revalidates_full_body_and_preserves_history(self):
        from receipt_fixtures import receipts
        from dream_procedural_sources import OriginalSource
        for record in receipts()[:2]:
            text = DRAWER + "\nCopied report:\n" + canonical_json(canonical_json(record))
            ref = EvidenceReference("drawer", "original", SESSION, "parser", content_hash(text))
            self.collection.rows["original"] = {"id": "original", "text": text,
                "metadata": {"wing": "w", "room": "diary", "repository": "owner/repo"}}
            self.capture(OriginalSource(ref, "owner/repo", NOW))
            before = deepcopy(self.collection.rows)
            with self.assertRaisesRegex(ValueError, "generated"):
                self.resolve(ref)
            self.assertEqual(self.collection.rows, before)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sources-", dir=os.environ["DREAMING_TEST_TMPDIR"])
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name
        self.collection = DrawerCollection()
        for name in ("procedural_collection", "protection_collection"):
            patched = patch.object(dream_palace, name, return_value=self.collection)
            patched.start()
            self.addCleanup(patched.stop)

    def capture(self, source=None, *, native=False, chunk_size=83, **kwargs):
        from dream_procedural_sources import capture_source
        return capture_source(source or original(), palace=self.path, wing="w",
                              captured_at=NOW + timedelta(days=1), captured_by="test-agent",
                              writer=sanctioned_writer(self.path, self.collection,
                                                       native=native, chunk_size=chunk_size), **kwargs)

    def resolve(self, ref=None):
        from dream_procedural_sources import read_source_records, resolve_captured
        return resolve_captured(ref or reference(), palace=self.path, wing="w",
                                sources=read_source_records(self.path, "w"))

    def test_exact_keys_exclude_quote_but_not_as_written_identity_fields(self):
        from dream_procedural_sources import source_key
        ref = reference()
        identity = {"source_kind": "session_turn", "source_id": SESSION, "session_id": SESSION,
                    "source_hash": content_hash(RAW), "turn_index": 7, "field": "user_message"}
        self.assertEqual(source_key(ref), content_hash(canonical_json(identity)))
        self.assertEqual(source_key(ref), source_key(replace(ref, quote="café")))
        for variant in (replace(ref, turn_index=8), replace(ref, field="assistant_response"),
                        replace(ref, source_hash="0" * 64)):
            self.assertNotEqual(source_key(ref), source_key(variant))
        drawer = EvidenceReference("drawer", "logical", SESSION, "original", content_hash(DRAWER))
        for variant in (replace(drawer, source_id="logical_chunk_000000"),
                        replace(drawer, source_id=" logical"),
                        replace(drawer, session_id="another-session")):
            self.assertNotEqual(source_key(drawer), source_key(variant))
        for variant in (replace(drawer, session_id=None), replace(ref, turn_index=True),
                        replace(ref, source_id="other"), replace(ref, source_hash="bad")):
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                source_key(variant)

    def test_exact_raw_record_native_trailer_and_header_split_roundtrip(self):
        from dream_procedural_sources import read_source_records, resolve_captured, source_key
        for native in (False, True):
            with self.subTest(native=native):
                self.collection.rows.clear()
                result = self.capture(native=native)
                self.assertEqual(result.status, "appended")
                index = read_source_records(self.path, "w")
                self.assertEqual(index.record_count, 1)
                self.assertEqual(set(index.locators), {source_key(reference())})
                self.assertGreater(len(result.drawer_ids), 2)
                rows = sorted(self.collection.rows.values(), key=lambda r: r["metadata"]["chunk_index"])
                text = "".join(r["text"] for r in rows)
                self.assertEqual(index.encoded_bytes, len(text.encode("utf-8")))
                body = text.split("\n\n", 1)[1].split("\n\n<!--dreaming-meta:", 1)[0]
                data = json.loads(body)
                self.assertEqual(data, {
                    "schema_version": 1,
                    "identity": {"source_kind": "session_turn", "source_id": SESSION,
                                 "session_id": SESSION, "source_hash": content_hash(RAW),
                                 "turn_index": 7, "field": "user_message"},
                    "repository": "owner/repo", "observed_at": "2026-09-23T00:00:00Z",
                    "captured_at": "2026-09-24T00:00:00Z", "captured_by": "test-agent",
                    "captured_text": RAW})
                self.assertIn("\nDigest: " + content_hash(canonical_json(data)) + "\n\n", text)
                self.assertTrue(all(r["metadata"]["added_by"] == "dream-procedure-source" for r in rows))
                self.assertNotIn(RAW, repr(index))
                self.assertNotIn("captured_text", repr(index))
                resolved = resolve_captured(reference(), palace=self.path, wing="w", sources=index)
                self.assertEqual((resolved.reference, resolved.session_id, resolved.repository,
                                  resolved.observed_at), (reference(), SESSION, "owner/repo", NOW))
                with patch.object(self.collection, "get", side_effect=AssertionError("memoized read")):
                    self.assertEqual(resolve_captured(reference(), palace=self.path, wing="w",
                                                       sources=index), resolved)

    def test_new_quotes_rechecked_against_full_raw_field(self):
        self.capture()
        self.assertEqual(self.resolve(replace(reference(), quote='{"nested":"value"}')).session_id, SESSION)
        with self.assertRaisesRegex(ValueError, "hash/quote"):
            self.resolve(replace(reference(), quote="not in original"))

    def test_drawer_witness_never_copies_or_replaces_original_body(self):
        from dream_procedural_sources import OriginalSource
        ref = EvidenceReference("drawer", "original", SESSION, "original", content_hash(DRAWER))
        self.collection.rows["original"] = {"id": "original", "text": DRAWER, "metadata": {
            "wing": "w", "room": "diary", "repository": "owner/repo"}}
        before = deepcopy(self.collection.rows["original"])
        self.capture(OriginalSource(ref, "owner/repo", NOW))
        self.assertEqual(self.collection.rows["original"], before)
        stored = "".join(r["text"] for r in self.collection.rows.values() if r["id"] != "original")
        self.assertNotIn("captured_text", stored)
        self.assertNotIn("original observation", stored)
        self.assertEqual(self.resolve(ref).observed_at, NOW)
        for change in ({"text": DRAWER + " drift"},
                       {"metadata": {**before["metadata"], "repository": "elsewhere/repo"}},
                       {"metadata": {**before["metadata"], "observed_at": "2026-01-02T00:00:00Z"}},
                       {"metadata": {**before["metadata"], "kind": "reflect"}}):
            self.collection.rows["original"] = {**before, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.resolve(ref)
        del self.collection.rows["original"]
        with self.assertRaises(EvidenceUnavailable):
            self.resolve(ref)

    def test_missing_is_distinct_from_malformed_and_every_same_key_candidate_is_verified(self):
        from dream_procedural_sources import source_record_data
        with self.assertRaises(EvidenceUnavailable):
            self.resolve()
        self.capture()
        body, meta = source_record_data(original(), captured_at=NOW + timedelta(days=2),
                                        captured_by="other-agent")
        sanctioned_writer(self.path, self.collection).add_drawer(
            "w", "procedural-sources", body, added_by="dream-procedure-source", metadata=meta)
        self.assertEqual(self.resolve().repository, "owner/repo")
        body, meta = source_record_data(replace(original(), repository="elsewhere/repo"),
                                        captured_at=NOW, captured_by="test")
        sanctioned_writer(self.path, self.collection).add_drawer(
            "w", "procedural-sources", body, added_by="dream-procedure-source", metadata=meta)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.resolve()

    def test_retry_compares_substantive_record_not_capture_audit_and_rejects_conflict(self):
        from dream_procedural_sources import capture_source
        self.capture()
        before = deepcopy(self.collection.rows)
        result = capture_source(original(), palace=self.path, wing="w",
            captured_at=NOW + timedelta(days=2), captured_by="another-agent",
            writer=sanctioned_writer(self.path, self.collection))
        self.assertEqual(result.status, "already_exists")
        self.assertEqual(self.collection.rows, before)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.capture(replace(original(), repository="elsewhere/repo"))

    def test_full_record_tampering_duplicate_json_and_native_conflicts_rejected(self):
        for mutation in ("body", "digest", "duplicate", "noncanonical", "native"):
            self.collection.rows.clear()
            self.capture(chunk_size=100000)
            row = next(iter(self.collection.rows.values()))
            if mutation == "body":
                row["text"] = row["text"].replace("owner/repo", "other/repo")
            elif mutation == "digest":
                row["text"] = row["text"].replace("Digest: ", "Digest: 0", 1)
            elif mutation == "duplicate":
                row["text"] = row["text"].replace('{"captured_at":', '{"schema_version":1,"captured_at":')
            elif mutation == "noncanonical":
                row["text"] = row["text"].replace('"repository":', '"repository": ')
            else:
                row["metadata"]["source_digest"] = "0" * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.resolve()

    def _assert_discovery_rejects_header_substitution(self, header, metadata_key):
        from dream_procedural_sources import read_source_records, source_record_data
        for encoding in ("native", "trailer", "both"):
            for alongside_valid in (False, True):
                with self.subTest(encoding=encoding, alongside_valid=alongside_valid):
                    self.collection.rows.clear()
                    writer = sanctioned_writer(self.path, self.collection,
                                               native=encoding != "trailer", chunk_size=79)
                    if alongside_valid:
                        body, meta = source_record_data(original(), captured_at=NOW, captured_by="valid")
                        writer.add_drawer("w", "procedural-sources", body,
                                          added_by="dream-procedure-source", metadata=meta)
                    body, meta = source_record_data(original(), captured_at=NOW, captured_by="corrupt")
                    body = body.replace(f"{header}: {meta[metadata_key]}",
                                        f"{header}: {'0' * 64}", 1)
                    if encoding == "both":
                        body += "\n\n<!--dreaming-meta: " + canonical_json(meta) + "-->"
                    writer.add_drawer("w", "procedural-sources", body,
                                      added_by="dream-procedure-source", metadata=meta)
                    with self.assertRaisesRegex(ValueError, "conflict|mismatch"):
                        read_source_records(self.path, "w")

    def test_discovery_rejects_valid_format_header_key_substitution_before_routing(self):
        self._assert_discovery_rejects_header_substitution("Source key", "source_key")

    def test_discovery_rejects_valid_format_header_digest_substitution_before_routing(self):
        self._assert_discovery_rejects_header_substitution("Digest", "source_digest")

    def test_discovery_reconciles_structural_body_before_routing(self):
        from dream_procedural_sources import read_source_records, source_record_data
        text = RAW * 100
        source = original(text)
        body, meta = source_record_data(source, captured_at=NOW, captured_by="test")
        body = body.replace("owner/repo", "other/repo")
        sanctioned_writer(self.path, self.collection, chunk_size=79).add_drawer(
            "w", "procedural-sources", body, added_by="dream-procedure-source", metadata=meta)
        with self.assertRaisesRegex(ValueError, "body/digest mismatch"):
            read_source_records(self.path, "w")

    def test_chunks_missing_duplicate_indices_conflicting_metadata_and_bad_identity_fail_closed(self):
        from dream_procedural_sources import read_source_records
        for mutation in ("gap", "tail", "duplicate_index", "metadata", "identity"):
            self.collection.rows.clear()
            self.capture()
            ids = list(self.collection.rows)
            if mutation in {"gap", "tail"}:
                del self.collection.rows[ids[1 if mutation == "gap" else -1]]
            elif mutation == "duplicate_index":
                self.collection.rows[ids[1]]["metadata"]["chunk_index"] = 0
            elif mutation == "metadata":
                self.collection.rows[ids[-1]]["metadata"]["added_by"] = "dream-procedure"
            else:
                self.collection.rows[ids[1]]["metadata"]["parent_drawer_id"] = "wrong"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                index = read_source_records(self.path, "w")
                from dream_procedural_sources import resolve_captured
                resolve_captured(reference(), palace=self.path, wing="w", sources=index)

    def test_record_bound_is_actual_utf8_encoding_including_trailer(self):
        from dream_procedural_sources import source_record_data
        text = "é" * 100
        src = original(text, quote="é")
        body, meta = source_record_data(src, captured_at=NOW, captured_by="test")
        overhead = len((body + "\n\n<!--dreaming-meta: " + canonical_json(meta) + "-->").encode()) - 200
        text = "é" * ((4 * 1024 * 1024 - overhead) // 2)
        if (4 * 1024 * 1024 - overhead) % 2:
            text += "x"
        body, meta = source_record_data(original(text, quote="é"), captured_at=NOW, captured_by="test")
        self.assertEqual(len((body + "\n\n<!--dreaming-meta: " + canonical_json(meta) + "-->").encode()),
                         4 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            source_record_data(original(text + "x", quote="é"), captured_at=NOW, captured_by="test")

    def test_thresholds_warn_without_blocking_capture(self):
        from dream_procedural_sources import read_source_records
        # Smaller diagnostic thresholds exercise real publication/indexing, not a cap.
        with patch("dream_procedural_sources.RECORD_WARNING_THRESHOLD", 5), \
             patch("dream_procedural_sources.BYTES_WARNING_THRESHOLD", 3000):
            for i in range(6):
                self.capture(original(turn_index=i))
                index = read_source_records(self.path, "w")
                if i < 3:
                    self.assertFalse(any("records" in w for w in index.warnings))
                else:
                    self.assertTrue(any("records" in w for w in index.warnings))
            self.assertEqual(index.record_count, 6)
            self.assertGreater(index.encoded_bytes, 3000)
            self.assertTrue(any("bytes" in w for w in index.warnings))

    def test_readers_never_write_and_index_does_not_cross_palace_or_wing(self):
        from dream_procedural_sources import read_source_records, resolve_captured
        self.capture()
        before = deepcopy(self.collection.rows)
        with patch.object(dream_palace, "MempalaceWriter", side_effect=AssertionError("writer")), \
             patch.object(dream_palace, "ensure_firewall_schema", side_effect=AssertionError("schema")):
            index = read_source_records(self.path, "w")
            self.resolve()
            with self.assertRaises(ValueError):
                resolve_captured(reference(), palace=self.path, wing="other", sources=index)
            with self.assertRaises(ValueError):
                resolve_captured(reference(), palace=self.path + "/elsewhere", wing="w", sources=index)
        self.assertEqual(self.collection.rows, before)

    def test_failed_ack_or_readback_never_reports_capture_success(self):
        from dream_procedural_sources import capture_source
        for result in ({"success": False, "error": "refused"}, {"success": True}):
            writer = sanctioned_writer(self.path, self.collection)
            writer._tools["mempalace_add_drawer"]["handler"] = lambda **kw: result
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                capture_source(original(), palace=self.path, wing="w",
                    captured_at=NOW, captured_by="test", writer=writer)

    def test_wrong_writer_or_invalid_original_cannot_publish(self):
        from dream_procedural_sources import OriginalSource, capture_source
        sources = (replace(original(), captured_text="drift"),
                   replace(original(), reference=replace(reference(), session_id=None)),
                   OriginalSource(EvidenceReference("drawer", "d", SESSION, "original",
                       content_hash(DRAWER)), "owner/repo", NOW, DRAWER))
        for source in sources:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.capture(source)
        writer = sanctioned_writer(self.path + "/other", self.collection)
        with self.assertRaisesRegex(ValueError, "different palace"):
            capture_source(original(), palace=self.path, wing="w", captured_at=NOW,
                           captured_by="test", writer=writer)
        self.assertEqual(self.collection.rows, {})

    def test_discovery_pages_metadata_and_never_batches_raw_bodies(self):
        from dream_procedural_sources import read_source_records
        self.capture(chunk_size=1)
        get = self.collection.get
        def bounded(**kw):
            if "documents" in kw["include"]:
                self.assertEqual(len(kw["ids"]), 1)
            else:
                self.assertLessEqual(kw["limit"], 256)
            return get(**kw)
        with patch.object(self.collection, "get", side_effect=bounded):
            index = read_source_records(self.path, "w")
        self.assertEqual(index.record_count, 1)
        self.assertGreater(sum(len(loc.member_ids) for locs in index.locators.values() for loc in locs), 256)
        self.assertEqual(self.resolve().repository, "owner/repo")

    def test_oversized_or_missing_header_never_becomes_an_empty_index(self):
        from dream_procedural_sources import read_source_records
        self.capture(chunk_size=100000)
        row = next(iter(self.collection.rows.values()))
        original_text = row["text"]
        for text in (" " * 1024 + original_text, original_text + "x" * (4 * 1024 * 1024)):
            row["text"] = text
            with self.subTest(size=len(text)), self.assertRaises(ValueError):
                read_source_records(self.path, "w")

    def test_orphans_and_malformed_reserved_records_protected_using_only_metadata(self):
        from dream_procedural_palace import live_protected_drawer_ids, read_events
        result = self.capture()
        self.collection.rows["malformed"] = {"id": "malformed", "text": "broken record",
            "metadata": {"wing": "w", "room": "procedural-sources"}}
        self.collection.rows["ordinary"] = {"id": "ordinary", "text": "unrelated", "metadata": {}}
        get = self.collection.get
        def metadata_only(**kw):
            self.assertNotIn("documents", kw.get("include", []))
            return get(**kw)
        # Event scan has no matches; no captured record should enter that selector.
        self.assertEqual(read_events(self.path, "w"), [])
        with patch.object(self.collection, "get", side_effect=lambda **kw: (
                get(**kw) if kw.get("where", {}).get("$or", [{}])[0] == {"room": "procedural"}
                else metadata_only(**kw))):
            protected = live_protected_drawer_ids(self.path)
        self.assertTrue(set(result.drawer_ids) | {"malformed"} <= protected)
        self.assertNotIn("ordinary", protected)
        writer = sanctioned_writer(self.path, self.collection)
        with self.assertRaisesRegex(ValueError, "protected"):
            writer.delete_drawer(result.drawer_ids[0])

    def test_bad_reserved_chunk_metadata_blocks_even_unrelated_delete(self):
        self.capture()
        self.collection.rows["ordinary"] = {"id": "ordinary", "text": "other", "metadata": {}}
        source = next(row for row in self.collection.rows.values() if row["id"] != "ordinary")
        source["metadata"]["total_chunks"] = 0
        writer = sanctioned_writer(self.path, self.collection)
        with self.assertRaises(ValueError):
            writer.delete_drawer("ordinary")
        self.assertIn("ordinary", self.collection.rows)

    def test_old_merge_prune_worklists_and_actual_harvest_exclude_source_records(self):
        result = self.capture()
        target = result.drawer_ids[0]
        with patch("mempalace.palace.get_collection", return_value=self.collection):
            self.assertEqual(dream_palace.load_logical_drawers(self.path, "w"), [])
            self.assertEqual(dream_palace.load_observation_entries(
                self.path, "w", rooms=("procedural-sources",)), [])
            out = Path(self.path, "worklist.json")
            self.assertEqual(dream_harvest.main(["--palace", self.path, "--wing", "w",
                "--task", "merge", "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["items"], [])
            with patch.object(dream_palace, "kg_protection_degree", return_value={}):
                self.assertEqual(dream_harvest.main(["--palace", self.path, "--wing", "w",
                    "--task", "prune", "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["items"], [])
        merge = {"action": "merge", "wing": "w", "room": "diary", "text": "replacement",
                 "supersedes": [target], "sources": []}
        kept, errors = dream_adopt._preflight_merge_decisions(self.path, [merge])
        self.assertTrue(errors)
        self.assertEqual(kept, [{"action": "skip"}])
        with patch.object(dream_palace, "kg_protection_degree", return_value={}):
            kept, errors = dream_adopt._preflight_prune_decisions(self.path, [
                {"action": "prune", "id": target, "member_ids": [target]}])
        self.assertTrue(errors)
        self.assertEqual(kept, [{"action": "keep"}])

    def test_only_committed_event_reference_protects_original_drawer(self):
        from dream_procedural_sources import OriginalSource
        from dream_procedural_palace import record_data, live_protected_drawer_ids
        self.collection.rows["original"] = {"id": "original", "text": DRAWER, "metadata": {
            "wing": "w", "room": "diary", "repository": "owner/repo"}}
        ref = EvidenceReference("drawer", "original", SESSION, "original", content_hash(DRAWER))
        self.capture(OriginalSource(ref, "owner/repo", NOW))
        self.assertNotIn("original", live_protected_drawer_ids(self.path))
        body, metadata = record_data(parse_event(event_data(origin_drawer_ids=[],
            evidence=[evidence("original", SESSION, DRAWER)])))
        self.collection.add("w", "procedural", body, "dream-procedure", metadata)
        self.assertIn("original", live_protected_drawer_ids(self.path))

    def test_drawer_witness_rejects_conflicting_later_chunk_provenance(self):
        from dream_procedural_sources import OriginalSource
        parent = "original"
        for i, text in enumerate((DRAWER[:20], DRAWER[20:])):
            pid = f"{parent}_chunk_{i:06}"
            self.collection.rows[pid] = {"id": pid, "text": text, "metadata": {
                "wing": "w", "room": "diary", "parent_drawer_id": parent,
                "chunk_index": i, "id_recipe": "v3", "added_by": "original-agent",
                "repository": "owner/repo"}}
        ref = EvidenceReference("drawer", "original_chunk_000000", SESSION, "original", content_hash(DRAWER))
        self.capture(OriginalSource(ref, "owner/repo", NOW))
        self.assertEqual(self.resolve(ref).repository, "owner/repo")
        self.collection.rows["original_chunk_000001"]["metadata"]["repository"] = "elsewhere/repo"
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.resolve(ref)

    def test_search_excludes_reserved_room_before_limiting_or_loading_source_bodies(self):
        from dream_procedural_validate import AdmissionReader, ResolvedEvidence
        generated = {"id": "captured", "text": "invalid source chunks", "metadata": {
            "wing": "w", "room": "procedural-sources", "added_by": "dream-procedure-source"}}
        ordinary = {"id": "original", "text": DRAWER, "metadata": {"wing": "w", "room": "diary"}}
        def matches(meta, where):
            if "$and" in where:
                return all(matches(meta, part) for part in where["$and"])
            return all(meta.get(k) != v["$ne"] if isinstance(v, dict) else meta.get(k) == v
                       for k, v in where.items())
        def query(**kw):
            rows = [r for r in (generated, ordinary) if matches(r["metadata"], kw["where"])]
            rows = rows[:kw["n_results"]]
            return {"ids": [[r["id"] for r in rows]], "documents": [[r["text"] for r in rows]],
                    "metadatas": [[r["metadata"] for r in rows]]}
        def load(path, source_id):
            if source_id == "captured":
                self.fail("search loaded captured source text")
            return ordinary
        reader = AdmissionReader(self.path, "w", str(Path(self.path, "nonexistent-host-store")))
        from dream_procedural_sources import SourceIndex
        reader._sources = SourceIndex(self.path, "w", {}, 0, 0, ())
        with patch.object(dream_palace, "procedural_collection", return_value=SimpleNamespace(query=query)), \
             patch("dream_procedural_palace.embed_texts", return_value=[[1., 0.]]), \
             patch.object(dream_palace, "load_source_drawer", side_effect=load), \
             patch.object(reader, "resolve", side_effect=lambda ref: ResolvedEvidence(
                 ref, SESSION, "owner/repo", NOW)):
            found = reader.search("w", "original", 1, as_of=NOW)
        self.assertEqual([r.source_id for r in found], ["original"])

    def test_publication_requires_the_exact_record_not_just_same_substantive_witness(self):
        from dream_procedural_sources import capture_source, source_record_data
        writer = sanctioned_writer(self.path, self.collection)
        def substitute(**kw):
            body, meta = source_record_data(original(), captured_at=NOW, captured_by="substituted")
            return self.collection.add("w", "procedural-sources",
                body + "\n\n<!--dreaming-meta: " + canonical_json(meta) + "-->",
                added_by="dream-procedure-source")
        writer._tools["mempalace_add_drawer"]["handler"] = substitute
        with self.assertRaisesRegex(RuntimeError, "readback"):
            capture_source(original(), palace=self.path, wing="w", captured_at=NOW,
                           captured_by="requested", writer=writer)

    def test_real_default_count_and_byte_thresholds_do_not_cap_publication(self):
        from dream_procedural_sources import source_record_data, read_source_records
        get = self.collection.get
        def fast_exact(**kw):
            if kw.get("ids") is not None:
                rows = {i: self.collection.rows[i] for i in kw["ids"] if i in self.collection.rows}
                return DrawerCollection(rows).get(**kw)
            return get(**kw)
        # All byte-count fixtures share one immutable string, not 68 MiB of copies.
        body, meta = source_record_data(original("x" * (4 * 1024 * 1024 - 1024), quote="x"),
                                        captured_at=NOW, captured_by="test")
        for i in range(17):
            pid = f"large-{i}"
            self.collection.rows[pid] = {"id": pid, "text": body, "metadata": {
                **meta, "wing": "w", "room": "procedural-sources", "added_by": "dream-procedure-source"}}
        with patch.object(self.collection, "get", side_effect=fast_exact):
            result = self.capture(chunk_size=100000)
            self.assertEqual(result.status, "appended")
            index = read_source_records(self.path, "w")
            self.assertGreater(index.encoded_bytes, 64 * 1024 * 1024)
            self.assertTrue(any("bytes" in message for message in index.warnings))
            self.collection.rows.clear()
            body, meta = source_record_data(original(), captured_at=NOW, captured_by="test")
            for i in range(3999):
                pid = f"small-{i}"
                self.collection.rows[pid] = {"id": pid, "text": body, "metadata": {
                    **meta, "wing": "w", "room": "procedural-sources", "added_by": "dream-procedure-source"}}
            self.assertFalse(read_source_records(self.path, "w").warnings)
            for i in range(3999, 5000):
                pid = f"small-{i}"
                self.collection.rows[pid] = {"id": pid, "text": body, "metadata": {
                    **meta, "wing": "w", "room": "procedural-sources", "added_by": "dream-procedure-source"}}
                if i == 3999:
                    self.assertTrue(any("records" in message
                                        for message in read_source_records(self.path, "w").warnings))
            result = self.capture(original(turn_index=8), chunk_size=100000)
            self.assertEqual(result.status, "appended")
            self.assertEqual(read_source_records(self.path, "w").record_count, 5001)


class InstalledSourceTests(unittest.TestCase):
    def test_real_chunked_legacy_packet_capture_is_retained_but_rejected(self):
        from delivery_fixtures import wrapped_packet
        from dream_procedural import to_data
        from dream_procedural_sources import _HEADER, _metadata, source_key
        from dream_procedural_validate import EvidenceReader
        from mempalace.palace import get_collection
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            get_collection(path, backend="sqlite_exact", create=True)
            text = wrapped_packet("Copied field:\n{}\nIndependent-looking commentary.", encoded=True)
            ref = reference(text, quote="Add a parser regression")
            data = dict(schema_version=1, identity={k: v for k, v in to_data(ref).items() if k != "quote"},
                        repository="owner/repo", observed_at="2026-09-23T00:00:00Z",
                        captured_at="2026-09-23T00:00:00Z", captured_by="legacy", captured_text=text)
            payload, key = canonical_json(data), source_key(ref)
            digest = content_hash(payload)
            writer = dream_palace.MempalaceWriter()
            with patch.dict(server._config._file_config, {"chunk_size": 79}):
                stored = writer.add_drawer(
                    "w", "procedural-sources", _HEADER.format(key=key, digest=digest) + payload,
                    added_by="dream-procedure-source", metadata=_metadata(key, digest))
            col = server._get_collection()
            before = tuple(col._handle.conn.iterdump())
            with self.assertRaisesRegex(ValueError, "generated"):
                EvidenceReader(path, "w").resolve(ref)
            self.assertEqual(tuple(col._handle.conn.iterdump()), before)
            self.assertIsNotNone(dream_palace.load_source_drawer(path, stored["drawer_id"]))

    def test_real_multichunk_drawer_witness_resolves_physical_identity_without_copying_body(self):
        from dream_procedural_sources import OriginalSource, capture_source, read_source_records, resolve_captured
        from mempalace.palace import get_collection
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            get_collection(path, backend="sqlite_exact", create=True)
            with patch.dict(server._config._file_config, {"chunk_size": 43}):
                writer = dream_palace.MempalaceWriter()
                result = writer.add_drawer("w", "diary", DRAWER, added_by="original-agent")
                parent = result["drawer_id"]
                ref = EvidenceReference("drawer", parent + "_chunk_000000", SESSION,
                                        "original observation", content_hash(DRAWER))
                before = dream_palace.load_source_drawer(path, parent)["text"]
                captured = capture_source(OriginalSource(ref, "owner/repo", NOW),
                    palace=path, wing="w", captured_at=NOW, captured_by="integration", writer=writer)
            index = read_source_records(path, "w")
            self.assertEqual(resolve_captured(ref, palace=path, wing="w", sources=index).reference, ref)
            self.assertEqual(dream_palace.load_source_drawer(path, parent)["text"], before)
            col = dream_palace.procedural_collection(path)
            documents = col.get(ids=list(captured.drawer_ids), include=["documents"])["documents"]
            joined = "".join(documents)
            self.assertNotIn("captured_text", joined)
            self.assertNotIn("original observation", joined)

    def test_real_handler_exact_multichunk_roundtrip_harvest_retention_and_clean_reopen(self):
        from dream_procedural_sources import capture_source, read_source_records, resolve_captured
        from dream_procedural_palace import live_protected_drawer_ids, nonmutating_read
        from mempalace.palace import get_collection, get_backend_for_palace
        with tempfile.TemporaryDirectory(dir=os.environ["DREAMING_TEST_TMPDIR"]) as path, \
             installed_palace(path) as server:
            get_collection(path, backend="sqlite_exact", create=True)
            text = RAW * 40
            src = original(text)
            with patch.dict(server._config._file_config, {"chunk_size": 79}):
                result = capture_source(src, palace=path, wing="w", captured_at=NOW,
                    captured_by="integration", writer=dream_palace.MempalaceWriter())
            self.assertGreater(len(result.drawer_ids), 3)
            col = dream_palace.procedural_collection(path)
            rows = dream_palace._rows_from_collection_result(col.get(
                ids=list(result.drawer_ids), include=["documents", "metadatas"]))
            full = "".join(r["text"] for r in sorted(rows, key=lambda r: r["metadata"]["chunk_index"]))
            data = {"schema_version": 1, "identity": {
                "source_kind": "session_turn", "source_id": SESSION, "session_id": SESSION,
                "source_hash": content_hash(text), "turn_index": 7, "field": "user_message"},
                "repository": "owner/repo", "observed_at": "2026-09-23T00:00:00Z",
                "captured_at": "2026-09-23T00:00:00Z", "captured_by": "integration", "captured_text": text}
            key, digest = content_hash(canonical_json(data["identity"])), content_hash(canonical_json(data))
            meta = {"kind": "procedural_source", "schema_version": 1,
                    "source_key": key, "source_digest": digest}
            expected = (f"Procedural source record; not independent evidence.\nSource key: {key}\n"
                        f"Digest: {digest}\n\n{canonical_json(data)}\n\n"
                        f"<!--dreaming-meta: {canonical_json(meta)}-->")
            self.assertEqual(full, expected)
            self.assertEqual(dream_palace.load_logical_drawers(path, "w"), [])
            worklist = Path(path, "merge.json")
            self.assertEqual(dream_harvest.main(["--palace", path, "--wing", "w",
                "--task", "merge", "--out", str(worklist)]), 0)
            self.assertEqual(json.loads(worklist.read_text())["items"], [])
            with patch.object(col, "get", wraps=col.get) as get:
                self.assertTrue(set(result.drawer_ids) <= live_protected_drawer_ids(path, collection=col))
                source_calls = [c for c in get.call_args_list
                                if c.kwargs.get("where", {}).get("$or", [{}])[0] != {"room": "procedural"}]
                self.assertTrue(source_calls)
                self.assertTrue(all("documents" not in c.kwargs.get("include", []) for c in source_calls))
            backend = get_backend_for_palace(path)
            backend.close_palace(path)
            before = {p.name: p.read_bytes() for p in Path(path).iterdir()
                      if p.is_file() and not p.name.endswith("-shm")}
            with nonmutating_read(path):
                index = read_source_records(path, "w")
                self.assertEqual(resolve_captured(src.reference, palace=path, wing="w",
                                                   sources=index).repository, "owner/repo")
            after = {p.name: p.read_bytes() for p in Path(path).iterdir()
                     if p.is_file() and not p.name.endswith("-shm")}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
