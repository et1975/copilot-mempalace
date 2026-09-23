"""Provenance contracts shared by recurrence and procedural storage."""
import hashlib
import json
import unittest
from unittest.mock import patch

import dream_adopt
import dream_reflect
import dream_palace


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MetadataTests(unittest.TestCase):
    def test_native_and_trailer_agree_and_preserve_source_metadata(self):
        from dream_metadata import decode_dream_metadata
        meta = {"kind": "reflect", "supported_by": ["s1"]}
        drawer = {"metadata": {**meta, "wing": "w"},
                  "text": "body\n<!--dreaming-meta: " + json.dumps(meta) + "-->"}
        self.assertEqual(decode_dream_metadata(drawer), {**meta, "wing": "w"})
        drawer["metadata"]["kind"] = "lesson"
        with self.assertRaises(ValueError):
            decode_dream_metadata(drawer)

    def test_malformed_metadata_fails_closed(self):
        from dream_metadata import decode_dream_metadata
        for text in ("<!--dreaming-meta: {broken}-->", "<!--dreaming-meta: {}",
                     '<!--dreaming-meta: {"kind":"lesson","kind":"reflect"}-->'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_dream_metadata({"text": text})
        for metadata in ([], "", {"kind": []}, {"source_kind": {}}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                decode_dream_metadata({"metadata": metadata})

    def test_generated_classification_native_trailer_and_summary(self):
        from dream_metadata import is_generated_observation, is_procedural_record
        for kind in ("lesson", "reflect", "procedural_event"):
            for drawer in ({"metadata": {"kind": kind}},
                           {"text": '<!--dreaming-meta: {"kind":"' + kind + '"}-->'},
                           {"metadata": {"kind": "summary", "source_kind": kind}}):
                self.assertTrue(is_generated_observation(drawer))
        self.assertTrue(is_procedural_record({"metadata": {"kind": "procedural_event"}}))
        self.assertFalse(is_generated_observation({"text": "ordinary evidence"}))

    def test_exact_chunks_split_inside_json_and_digest(self):
        from dream_metadata import decode_procedural_chunks
        event = {"event_id": "fixture", "payload": {"text": "alpha beta"}}
        event["digest"] = digest(json.dumps(event, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")))
        meta = {"kind": "procedural_event", "schema_version": 1, "event": event}
        text = "record\n<!--dreaming-meta: " + json.dumps(meta) + "-->"
        split = text.index("alpha") + 2
        rows = [{"id": f"c{i}", "text": part, "metadata": {
            "parent_drawer_id": "d", "chunk_index": i, "total_chunks": 2,
            "wing": "w", "room": "procedural"}}
                for i, part in enumerate((text[:split], text[split:]))]
        logical = decode_procedural_chunks(list(reversed(rows)))[0]
        self.assertEqual(logical["text"], text)
        self.assertEqual(logical["metadata"]["event"], event)
        self.assertEqual(logical["member_ids"], ["c0", "c1"])
        with self.assertRaises(ValueError):
            decode_procedural_chunks(rows[1:])
        rows[0]["text"] = rows[0]["text"].replace("al", "zz")
        with self.assertRaises(ValueError):
            decode_procedural_chunks(rows)

    def test_observation_loader_keeps_metadata_and_hash(self):
        import sys
        import types
        text = "SESSION_ID: 11111111-1111-4111-8111-111111111111\nobservation"
        meta = {"wing": "w", "room": "diary", "kind": "reflect"}
        collection = types.SimpleNamespace(get=lambda **kw: {
            "ids": ["d"], "documents": [text], "metadatas": [meta],
            "embeddings": [[1.0, 0.0]]})
        module = types.SimpleNamespace(get_collection=lambda p: collection)
        with patch.dict(sys.modules, {"mempalace.palace": module}):
            entry = dream_palace.load_observation_entries("P")[0]
        self.assertEqual(entry["metadata"], meta)
        self.assertEqual(entry["content_hash"], digest(text))


class AdmissionTests(unittest.TestCase):
    def entries(self):
        return [{"id": f"d{i}", "text": f"SESSION_ID: {i:08}-1111-4111-8111-111111111111\nuse tests {i}",
                 "session_id": f"{i:08}-1111-4111-8111-111111111111",
                 "embedding": [1.0, 0.0], "metadata": {}} for i in range(1, 4)]

    def decision(self, entries, minimum=3):
        seeds = dream_reflect.converge_seeds_from_recurrence(entries, tau=.9, min_support=2)
        seed = seeds[0]
        return dream_adopt._resolve_reflect_decisions({
            "params": {"min_support": minimum}, "items": [{
                **seed, "decision": {"action": "surface", "reflect_kind": "converge",
                "conclusion": {"kind": "converge", "text": "prefer grounded tests"}}}]})

    def preflight(self, entries, decisions):
        with patch.object(dream_adopt, "load_logical_drawers", return_value=entries), \
             patch.object(dream_adopt, "_palace_embed", return_value=[[0.0, 1.0]]):
            return dream_adopt._preflight_reflect_decisions("P", decisions)

    def test_converge_uses_live_sessions_hashes_and_declared_threshold(self):
        entries = self.entries()
        decisions = self.decision(entries)
        self.assertEqual(self.preflight(entries, decisions)[1], [])
        for live in (entries[:2], [dict(e, text=e["text"] + " drift") for e in entries],
                     [dict(e, text=entries[0]["text"]) for e in entries]):
            with self.subTest(live=live):
                kept, errors = self.preflight(live, decisions)
                self.assertTrue(errors)
                self.assertEqual(kept, [{"action": "skip"}])
        decisions[0]["evidence"]["support_ids"] = ["forged1", "forged2", "forged3"]
        self.assertTrue(self.preflight(entries, decisions)[1])

    def test_generated_records_cannot_supply_recurrence(self):
        entries = self.entries()
        for kind in ("lesson", "reflect", "procedural_event"):
            entries[2]["metadata"] = {"kind": kind}
            self.assertEqual(dream_reflect.converge_seeds_from_recurrence(
                entries, tau=.9, min_support=3), [])

    def test_diary_raw_same_session_counts_once(self):
        entries = self.entries()[:2]
        entries.append(dict(entries[0], id="session:" + entries[0]["session_id"]))
        self.assertEqual(dream_reflect.converge_seeds_from_recurrence(
            entries, tau=.9, min_support=3), [])

    def test_procedural_record_cannot_be_constructive_seed(self):
        entries = self.entries()
        entries[1]["embedding"] = [.6, .4]
        entries[2]["embedding"] = [.5, .5]
        entries[2]["metadata"] = {"kind": "procedural_event"}
        with patch.object(dream_reflect, "load_logical_drawers", return_value=entries):
            seeds = dream_reflect.gather_reflect_seeds("P")
        self.assertTrue(seeds)
        self.assertTrue(all("d3" not in s["member_ids"] for s in seeds))

    def test_empty_quote_is_not_grounded(self):
        result = dream_reflect.validate_reflect({
            "conclusion": {"kind": "generalize", "text": "new guidance",
                           "decision_or_prediction": "use it"},
            "premises": [{"drawer_id": "a", "quote": ""},
                         {"drawer_id": "b", "quote": "beta"}]},
            {"a": "alpha", "b": "beta"})
        self.assertFalse(result["ok"])
        self.assertIn("ungrounded", result["rejects"])
