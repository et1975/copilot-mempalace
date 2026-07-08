#!/usr/bin/env python3
"""Tests for palace_wing_lib.py (pure core, no mempalace).

Run: ``python3 -m pytest test_palace_wing_lib.py`` or ``python3 test_palace_wing_lib.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_wing_lib as lib  # noqa: E402


# --------------------------------------------------------------------------- #
# Manifest build / validate.
# --------------------------------------------------------------------------- #
def test_build_manifest_shape_and_counts():
    m = lib.build_manifest(
        wing="avs",
        mempalace_version="3.5.0",
        counts={"drawers": 42, "kg_triples": 7, "tunnels": 2},
        kg_note="best-effort: 3 skipped",
        exported_at="2026-07-08T14:00:00Z",
    )
    assert m["type"] == "manifest"
    assert m["bundle_version"] == lib.BUNDLE_VERSION
    assert m["tool_version"] == lib.TOOL_VERSION
    assert m["wing"] == "avs"
    assert m["counts"] == {"drawers": 42, "kg_triples": 7, "tunnels": 2}
    assert m["kg_note"] == "best-effort: 3 skipped"


def test_validate_manifest_round_trip_ok():
    m = lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "note", "2026-07-08T00:00:00Z")
    lib.validate_manifest(m)  # should not raise


def test_validate_manifest_rejects_unknown_bundle_version():
    m = lib.build_manifest("avs", "3.5.0", {}, "n", "t")
    m["bundle_version"] = 999
    try:
        lib.validate_manifest(m)
        raised = False
    except ValueError as exc:
        raised = True
        assert "bundle_version" in str(exc)
    assert raised


def test_validate_manifest_rejects_non_manifest():
    try:
        lib.validate_manifest({"type": "drawer"})
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_validate_manifest_rejects_missing_wing():
    m = {"type": "manifest", "bundle_version": lib.BUNDLE_VERSION, "counts": {}}
    try:
        lib.validate_manifest(m)
        raised = False
    except ValueError as exc:
        raised = True
        assert "wing" in str(exc)
    assert raised


# --------------------------------------------------------------------------- #
# JSONL serialize -> parse round-trip per record type.
# --------------------------------------------------------------------------- #
def test_jsonl_round_trip_all_record_types():
    manifest = lib.build_manifest("avs", "3.5.0", {"drawers": 1, "kg_triples": 1,
                                                   "tunnels": 1}, "note", "t")
    drawer = lib.drawer_record("avs", "scripting", "hello", "src.md", "copilot-cli",
                               "drawer_1", {"topic": "x"})
    triple = lib.kg_triple_record("A", "rel", "B", 1.0, "2026-06-02", None, "drawer_1")
    tunnel = lib.tunnel_record("avs", "scripting", "conveyor", "general", "why")
    records = [manifest, drawer, triple, tunnel]

    text = lib.dump_jsonl(records)
    parsed = lib.parse_jsonl(text)
    assert parsed == records


def test_dump_jsonl_one_object_per_line():
    records = [lib.drawer_record("w", "r", "c", None, None, None, None),
               lib.tunnel_record("w", "r", "w2", "r2", "l")]
    text = lib.dump_jsonl(records)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2


def test_parse_jsonl_skips_blank_lines():
    text = '{"type":"drawer"}\n\n   \n{"type":"tunnel"}\n'
    parsed = lib.parse_jsonl(text)
    assert [r["type"] for r in parsed] == ["drawer", "tunnel"]


# --------------------------------------------------------------------------- #
# filter_wing_triples.
# --------------------------------------------------------------------------- #
def test_filter_wing_triples_keeps_in_set_drops_null_and_foreign():
    triples = [
        {"subject": "A", "source_drawer_id": "d1"},   # in set -> keep
        {"subject": "B", "source_drawer_id": None},   # null -> skip
        {"subject": "C", "source_drawer_id": "d9"},   # foreign -> skip
        {"subject": "D", "source_drawer_id": "d2"},   # in set -> keep
        {"subject": "E"},                             # missing -> skip
    ]
    kept, skipped = lib.filter_wing_triples(triples, {"d1", "d2"})
    assert [t["subject"] for t in kept] == ["A", "D"]
    assert skipped == 3


def test_filter_wing_triples_empty():
    kept, skipped = lib.filter_wing_triples([], {"d1"})
    assert kept == []
    assert skipped == 0


# --------------------------------------------------------------------------- #
# tunnel_touches_wing.
# --------------------------------------------------------------------------- #
def test_tunnel_touches_wing_source_match():
    t = {"source": {"wing": "avs"}, "target": {"wing": "conveyor"}}
    assert lib.tunnel_touches_wing(t, "avs") is True


def test_tunnel_touches_wing_target_match():
    t = {"source": {"wing": "avs"}, "target": {"wing": "conveyor"}}
    assert lib.tunnel_touches_wing(t, "conveyor") is True


def test_tunnel_touches_wing_no_match():
    t = {"source": {"wing": "avs"}, "target": {"wing": "conveyor"}}
    assert lib.tunnel_touches_wing(t, "other") is False


# --------------------------------------------------------------------------- #
# remap_into_wing.
# --------------------------------------------------------------------------- #
def test_remap_into_wing_drawer_wing():
    rec = lib.drawer_record("avs", "r", "c", None, None, None, None)
    out = lib.remap_into_wing(rec, "avs", "avs_clone")
    assert out["wing"] == "avs_clone"
    assert rec["wing"] == "avs"  # original untouched


def test_remap_into_wing_drawer_non_matching_wing_untouched():
    rec = lib.drawer_record("other", "r", "c", None, None, None, None)
    out = lib.remap_into_wing(rec, "avs", "avs_clone")
    assert out["wing"] == "other"


def test_remap_into_wing_tunnel_only_matching_endpoint():
    rec = lib.tunnel_record("avs", "scripting", "conveyor", "general", "l")
    out = lib.remap_into_wing(rec, "avs", "avs_clone")
    assert out["source"]["wing"] == "avs_clone"
    assert out["target"]["wing"] == "conveyor"  # untouched
    # original untouched
    assert rec["source"]["wing"] == "avs"


def test_remap_into_wing_tunnel_target_endpoint():
    rec = lib.tunnel_record("conveyor", "general", "avs", "scripting", "l")
    out = lib.remap_into_wing(rec, "avs", "avs_clone")
    assert out["target"]["wing"] == "avs_clone"
    assert out["source"]["wing"] == "conveyor"


# --------------------------------------------------------------------------- #
# Provenance trailer encode / decode.
# --------------------------------------------------------------------------- #
def test_trailer_round_trip():
    extra = {"topic": "scripting", "hall": "ops", "type": "howto", "date": "2026-06-02"}
    encoded = lib.encode_trailer("body text", extra)
    assert lib.TRAILER_MARKER in encoded
    content, decoded = lib.decode_trailer(encoded)
    assert content == "body text"
    assert decoded == extra


def test_trailer_absent_is_tolerated():
    content, decoded = lib.decode_trailer("just body, no trailer")
    assert content == "just body, no trailer"
    assert decoded == {}


def test_trailer_empty_extra_leaves_content_unchanged():
    assert lib.encode_trailer("body", {}) == "body"
    assert lib.encode_trailer("body", None) == "body"


def test_trailer_malformed_returns_empty_extra():
    broken = "body\n\n<!--wing-meta: {not json}-->"
    content, decoded = lib.decode_trailer(broken)
    assert decoded == {}


def test_trailer_round_trip_when_content_contains_trailer_like_text():
    # Content that itself ends with a trailer-like block must still round-trip:
    # encode appends exactly one trailer, decode removes exactly the last one.
    inner = "body with an embedded block\n\n<!--wing-meta: {\"x\":1}-->"
    extra = {"topic": "t", "date": "2026-01-01"}
    encoded = lib.encode_trailer(inner, extra)
    content, decoded = lib.decode_trailer(encoded)
    assert content == inner
    assert decoded == extra


def test_filter_wing_triples_keeps_falsy_zero_id():
    triples = [{"source_drawer_id": 0}, {"source_drawer_id": None},
               {"source_drawer_id": 7}]
    kept, skipped = lib.filter_wing_triples(triples, {0, 7})
    assert len(kept) == 2  # id 0 kept, None skipped
    assert skipped == 1


def test_validate_manifest_rejects_non_string_wing():
    bad = {"type": "manifest", "bundle_version": lib.BUNDLE_VERSION,
           "wing": 123, "counts": {}}
    try:
        lib.validate_manifest(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_manifest_rejects_negative_counts():
    bad = {"type": "manifest", "bundle_version": lib.BUNDLE_VERSION,
           "wing": "avs", "counts": {"drawers": -1}}
    try:
        lib.validate_manifest(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# Minimal runner when pytest is unavailable.
# --------------------------------------------------------------------------- #
def _run_without_pytest() -> int:
    import inspect
    import tempfile

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        params = inspect.signature(fn).parameters
        try:
            if "tmp_path" in params:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_without_pytest())
