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
    def test_quoted_or_inline_markers_are_not_provenance(self):
        from dream_metadata import decode_dream_metadata, is_generated_observation
        examples = (
            "Example: <!--dreaming-meta: ...-->",
            'A source string contains <!--dreaming-meta: {"kind":"reflect"}-->.',
            "```markdown\n<!--dreaming-meta: ...-->\n```",
            '```markdown\n<!--dreaming-meta: {"kind":"reflect"}-->',
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(decode_dream_metadata({"text": text}), {})
                self.assertFalse(is_generated_observation({"text": text}))
        text = examples[0] + '\n\n<!--dreaming-meta: {"kind":"reflect"}-->'
        self.assertEqual(decode_dream_metadata({"text": text}), {"kind": "reflect"})

    def test_writer_trailer_survives_code_fences_and_unicode_separators(self):
        from dream_metadata import decode_dream_metadata
        meta = {"kind": "reflect", "quote": "alpha\u2028beta"}
        text = "```text\nan unfinished example\n\n<!--dreaming-meta: " + json.dumps(
            meta, ensure_ascii=False) + "-->"
        result = decode_dream_metadata({"text": text, "metadata": {"added_by": "dreaming"}})
        self.assertEqual(result["kind"], "reflect")
        self.assertEqual(result["quote"], meta["quote"])

    def test_split_reports_consumed_empty_trailer_without_changing_decoder_result(self):
        from dream_metadata import decode_dream_metadata, split_dream_metadata
        prefix = "original \u2028 content\n\n"
        native = {"added_by": "dreaming"}
        drawer = {"text": prefix + "<!--dreaming-meta: {}--> \n", "metadata": native}
        body, metadata = split_dream_metadata(drawer)
        self.assertEqual(body, prefix)
        self.assertEqual(metadata, native)
        self.assertEqual(metadata, decode_dream_metadata(drawer))

    def test_split_preserves_unconsumed_fences_and_retains_writer_context(self):
        from dream_metadata import split_dream_metadata
        prefix = "```html\nunfinished example\n"
        text = prefix + '<!--dreaming-meta: {"kind":"procedural_source"}-->'
        self.assertEqual(split_dream_metadata({"text": text}), (text, {}))
        body, metadata = split_dream_metadata(
            {"text": text, "metadata": {"added_by": "dreaming"}})
        self.assertEqual(body, prefix)
        self.assertEqual(metadata["kind"], "procedural_source")
        closed = text + "\n```"
        self.assertEqual(
            split_dream_metadata({"text": closed, "metadata": {"added_by": "dreaming"}}),
            (closed, {"added_by": "dreaming"}))

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
        for kind in ("lesson", "reflect", "procedural_event", "procedural_source"):
            for drawer in ({"metadata": {"kind": kind}},
                           {"text": '<!--dreaming-meta: {"kind":"' + kind + '"}-->'},
                           {"metadata": {"kind": "summary", "source_kind": kind}}):
                self.assertTrue(is_generated_observation(drawer))
        self.assertTrue(is_procedural_record({"metadata": {"kind": "procedural_event"}}))
        self.assertTrue(is_procedural_record({"metadata": {"room": "procedural-sources"}}))
        self.assertTrue(is_generated_observation({"metadata": {"added_by": "dream-procedure-source"}}))
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
        for kind in ("lesson", "reflect", "procedural_event", "procedural_source"):
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
def test_packet_transport_detection_handles_complete_wrapped_escaped_envelopes():
    from delivery_fixtures import wrapped_packet
    from dream_metadata import reject_generated_transport, is_generated_observation
    import pytest
    for wrapper in ("{}", "```json\n{}\n```", "Copied report:\n{}\nEnd."):
        for encoded in (False, True):
            text = wrapped_packet(wrapper, encoded)
            with pytest.raises(ValueError, match="generated"):
                reject_generated_transport(text)
            assert is_generated_observation({"text": text, "metadata": {}})
    reject_generated_transport("The parser failed later on input X; the observed error was Y.")


def test_transport_inspection_depth_is_explicit_not_independent_evidence():
    from dream_metadata import canonical_json, reject_generated_transport
    import pytest
    text = '{"ki\\u006ed":"procedural_delivery_packet"}'
    with pytest.raises(ValueError, match="generated"):
        reject_generated_transport(text)
    for _ in range(6):
        text = canonical_json(text)
    with pytest.raises(ValueError, match="inconclusive"):
        reject_generated_transport(text)


def test_strict_json_rejects_numeric_overflow_not_just_nonstandard_constants():
    from dream_metadata import canonical_json, strict_json, decode_dream_metadata
    import pytest
    for value in ("1e999", "-1e999", "NaN", "Infinity"):
        with pytest.raises(ValueError, match="finite"):
            strict_json('{"score":' + value + "}")
        with pytest.raises(ValueError, match="finite"):
            decode_dream_metadata({"metadata": {}, "text": '\n<!--dreaming-meta: {"score":' + value + "}-->"})
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            canonical_json({"score": value})


def test_transient_applicability_and_use_results_are_generated_not_new_support():
    from delivery_fixtures import applicability, case
    from dream_metadata import canonical_json, reject_generated_transport
    import pytest
    packet, current, _, guidance = case()
    artifacts = [
        applicability(current["context"], guidance["rules"]),
        dict(kind="procedural_use_check", authority="agent_reported", status="usable",
             items=guidance["rules"], checked_at=guidance["as_of"],
             notice="Cooperative current check, not semantic proof or a durable authorization token; recheck before action."),
    ]
    for artifact in artifacts:
        for wrapped in (canonical_json(artifact), canonical_json(canonical_json(artifact))):
            with pytest.raises(ValueError, match="generated"):
                reject_generated_transport("Copied result:\n" + wrapped + "\nEnd.")


def test_feedback_and_abstention_are_generated_in_raw_wrapped_escaped_and_metadata_forms():
    from dream_metadata import canonical_json, reject_generated_transport, is_generated_observation
    import pytest
    for kind in ("procedural_feedback", "procedural_feedback_abstention"):
        body = canonical_json({"kind": kind, "quote": "independent-looking observation"})
        for value in (body, canonical_json(body), body.replace('"kind"', '"ki\\u006ed"')):
            for wrapper in ("{}", "```json\n{}\n```", "Copied:\n{}\nEnd."):
                with pytest.raises(ValueError, match="generated"):
                    reject_generated_transport(wrapper.format(value))
        for key in ("kind", "source_kind", "generated_from"):
            assert is_generated_observation({"text": "observation", "metadata": {key: kind}})
            assert is_generated_observation({"text": "observation\n<!--dreaming-meta: "
                + canonical_json({key: kind}) + "-->", "metadata": {}})
