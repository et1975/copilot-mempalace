#!/usr/bin/env python3
"""Tests for palace_wing.py (CLI + mempalace adapter, all I/O mocked).

No live palace and no importable ``mempalace`` required: every mempalace /
palace-SQLite seam on the module is replaced with an in-memory fake.

From the repository root, with ``PYTHONDONTWRITEBYTECODE=1`` exported:
``$TEST_PY -m pytest tests/mempalace-backup/test_palace_wing.py -q``.
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import uuid
from pathlib import Path

import palace_wing as pw
import palace_wing_lib as lib


# --------------------------------------------------------------------------- #
# Fakes and helpers.
# --------------------------------------------------------------------------- #
class FakeKG:
    def __init__(self):
        self.triples = []
        self.closed = False

    def add_triple(self, subject, predicate, obj, valid_from=None, valid_to=None,
                   confidence=1.0):
        self.triples.append({
            "subject": subject, "predicate": predicate, "obj": obj,
            "valid_from": valid_from, "valid_to": valid_to, "confidence": confidence,
        })

    def close(self):
        self.closed = True


@contextlib.contextmanager
def patched(**overrides):
    saved = {k: getattr(pw, k) for k in overrides}
    for k, v in overrides.items():
        setattr(pw, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(pw, k, v)


def _bundle_path() -> Path:
    return Path(tempfile.gettempdir()) / f".test-bundle-{uuid.uuid4().hex}.jsonl"


def _out_path() -> Path:
    return Path(tempfile.gettempdir()) / f".test-out-{uuid.uuid4().hex}.jsonl"


def _write_bundle(records) -> Path:
    path = _bundle_path()
    path.write_text(lib.dump_jsonl(records), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Export.
# --------------------------------------------------------------------------- #
def test_export_produces_manifest_and_records_with_counts():
    # Two chunks of one parent drawer + one single-chunk drawer, in the row
    # shape read_wing_drawer_rows yields (text extracted, metadata dict).
    rows = [
        {"id": "d1_c0", "text": "part one",
         "metadata": {"wing": "avs", "room": "scripting", "parent_drawer_id": "d1",
                      "chunk_index": 0, "topic": "t", "source_file": "a.md",
                      "added_by": "copilot-cli"}},
        {"id": "d1_c1", "text": "part two",
         "metadata": {"wing": "avs", "room": "scripting", "parent_drawer_id": "d1",
                      "chunk_index": 1}},
        {"id": "d2", "text": "solo",
         "metadata": {"wing": "avs", "room": "general"}},
    ]
    triples = [
        {"subject": "A", "predicate": "r", "object": "B", "confidence": 1.0,
         "valid_from": None, "valid_to": None, "source_drawer_id": "d1"},   # keep
        {"subject": "X", "predicate": "r", "object": "Y", "confidence": 1.0,
         "valid_from": None, "valid_to": None, "source_drawer_id": None},   # skip
    ]
    tunnels = [
        {"source": {"wing": "avs", "room": "scripting"},
         "target": {"wing": "conveyor", "room": "general"}, "label": "why"},
        {"source": {"wing": "other", "room": "x"},
         "target": {"wing": "nope", "room": "y"}, "label": "nope"},   # untouched
    ]
    out = _out_path()
    try:
        with patched(
            mempalace_version=lambda: "3.5.0",
            read_wing_drawer_rows=lambda palace, wing: rows,
            read_wing_triples=lambda palace: triples,
            read_tunnels=lambda palace: tunnels,
        ):
            rc = pw.main(["export", "avs", "--out", str(out), "--palace", "/nope"])
        assert rc == 0
        records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
        manifest = records[0]
        assert manifest["type"] == "manifest"
        assert manifest["wing"] == "avs"
        assert manifest["counts"] == {"drawers": 2, "kg_triples": 1, "tunnels": 1}

        drawers = [r for r in records if r.get("type") == "drawer"]
        triple_recs = [r for r in records if r.get("type") == "kg_triple"]
        tunnel_recs = [r for r in records if r.get("type") == "tunnel"]
        assert len(drawers) == 2
        assert len(triple_recs) == 1
        assert len(tunnel_recs) == 1

        # Multi-chunk reassembly, ordered by chunk_index.
        parent = next(d for d in drawers if d["orig_drawer_id"] == "d1")
        assert parent["content"] == "part one\npart two"
        assert parent["extra"] == {"topic": "t"}
        assert parent["room"] == "scripting"

        assert triple_recs[0]["subject"] == "A"
        assert tunnel_recs[0]["target"]["wing"] == "conveyor"
    finally:
        out.unlink(missing_ok=True)


def test_export_preserves_procedural_payloads_without_replaying_them(tmp_path):
    bodies = [
        'event\n\n<!--dreaming-meta: {"kind":"procedural_event","digest":"original"}-->',
        'source\n\n<!--dreaming-meta: {"kind":"procedural_source","source_hash":"original"}-->',
    ]
    rows = [
        {"id": "event-id", "text": bodies[0], "metadata": {
            "wing": "avs", "room": "procedural", "added_by": "dream-procedure"}},
        {"id": "source-id", "text": bodies[1], "metadata": {
            "wing": "avs", "room": "procedural-sources", "added_by": "dream-procedure-source"}},
    ]
    for format in ("jsonl", "md"):
        out = tmp_path / format
        with patched(
            mempalace_version=lambda: "v",
            read_wing_drawer_rows=lambda palace, wing: rows,
            read_wing_triples=lambda palace: [],
            read_tunnels=lambda palace: [],
        ):
            assert pw.main(["export", "avs", "--format", format, "--out", str(out),
                            "--palace", str(tmp_path / "palace")]) == 0
        records = (pw.read_md_dir(str(out / "avs")) if format == "md"
                   else lib.parse_jsonl(out.read_text(encoding="utf-8")))
        assert [r["content"] for r in records[1:]] == bodies
        assert [r["orig_drawer_id"] for r in records[1:]] == ["event-id", "source-id"]
        assert [r["room"] for r in records[1:]] == ["procedural", "procedural-sources"]


# --------------------------------------------------------------------------- #
# Markdown directory: write -> read round-trip and import.
# --------------------------------------------------------------------------- #
def _sample_records():
    return [
        lib.build_manifest("avs", "3.5.0",
                           {"drawers": 2, "kg_triples": 1, "tunnels": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "body one\nwith --> arrow", "a.md",
                          "copilot-cli", "drawer_avs_scripting_1", {"topic": "t"}),
        lib.drawer_record("avs", "general", "solo body", None, None, None, {}),
        lib.kg_triple_record("A", "rel", "B", 1.0, None, None, "drawer_avs_scripting_1"),
        lib.tunnel_record("avs", "scripting", "conveyor", "general", "why"),
    ]


def test_md_dir_write_read_round_trip(tmp_path):
    records = _sample_records()
    wing_dir = pw.write_md_dir(records, str(tmp_path))
    assert (Path(wing_dir) / "manifest.json").exists()
    assert (Path(wing_dir) / "drawer_avs_scripting_1.md").exists()
    assert (Path(wing_dir) / "general__0001.md").exists()
    assert (Path(wing_dir) / "kg.jsonl").exists()
    assert (Path(wing_dir) / "tunnels.jsonl").exists()

    back = pw.read_md_dir(wing_dir)
    assert back[0]["type"] == "manifest" and back[0]["wing"] == "avs"
    drawers = [r for r in back if r.get("type") == "drawer"]
    triples = [r for r in back if r.get("type") == "kg_triple"]
    tunnels = [r for r in back if r.get("type") == "tunnel"]
    assert len(drawers) == 2 and len(triples) == 1 and len(tunnels) == 1
    d0 = next(d for d in drawers if d["orig_drawer_id"] == "drawer_avs_scripting_1")
    assert d0["content"] == "body one\nwith --> arrow"   # verbatim, arrow survives
    assert d0["room"] == "scripting" and d0["extra"] == {"topic": "t"}
    assert triples[0]["subject"] == "A"
    assert tunnels[0]["target"]["wing"] == "conveyor"


def test_import_from_md_dir_invokes_writes(tmp_path):
    wing_dir = pw.write_md_dir(_sample_records(), str(tmp_path))
    added, kg = [], FakeKG()
    tunnels = []
    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            add_drawer=lambda **kw: added.append(kw),
            open_kg=lambda palace: kg,
            create_tunnel=lambda **kw: tunnels.append(kw) or {"tunnel_id": "x"},
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 2,
        ):
            rc = pw.main(["import", wing_dir, "--palace", str(tmp_path)])
        assert rc == 0
        assert len(added) == 2
        assert {a["room"] for a in added} == {"scripting", "general"}
        assert len(kg.triples) == 1
        assert len(tunnels) == 1
    finally:
        pass


def test_import_from_manifest_json_path(tmp_path):
    wing_dir = pw.write_md_dir(_sample_records(), str(tmp_path))
    added = []
    with patched(
        require_mempalace=lambda: None,
        check_duplicate=lambda content, threshold: {"is_duplicate": False},
        add_drawer=lambda **kw: added.append(kw),
        open_kg=lambda palace: FakeKG(),
        create_tunnel=lambda **kw: {"tunnel_id": "x"},
        preflight_import_target=lambda *a, **k: None,
        palace_drawer_count=lambda *a, **k: 2,
    ):
        rc = pw.main(["import", str(Path(wing_dir) / "manifest.json"),
                      "--palace", str(tmp_path)])
    assert rc == 0
    assert len(added) == 2


def test_read_legacy_md_dir_splits_and_parses_kg(tmp_path):
    # Legacy: one file per room; 'insights.md' holds two drawers; prose KG.
    wing_dir = tmp_path / "agentic"
    wing_dir.mkdir()
    (wing_dir / "insights.md").write_text(
        "<!--\nMemPalace wing export\nwing: agentic\n-->\n"
        "# Insights\n\n## INSIGHT I1\nfirst\n\n## INSIGHT I2\nsecond\n",
        encoding="utf-8")
    (wing_dir / "researchers.md").write_text(
        "<!--\nMemPalace wing export\n-->\n"
        "# Researchers\n\n## Knowledge-Graph Triples\n\n"
        "- Ida \u2192 works_on \u2192 memory\n- Ken \u2192 authored \u2192 SORT\n",
        encoding="utf-8")
    manifest = {
        "type": "manifest", "bundle_version": 1, "wing": "agentic",
        "counts": {"drawers": 3}, "exported_by": "copilot-cli-fleet",
        "drawers": [
            {"drawer_id": "d1", "room": "insights", "added_by": "mcp",
             "source_file": "", "content_file": "insights.md"},
            {"drawer_id": "d2", "room": "insights", "added_by": "mcp",
             "source_file": "", "content_file": "insights.md"},
            {"drawer_id": "d3", "room": "researchers", "added_by": "mcp",
             "source_file": "", "content_file": "researchers.md"},
        ],
    }
    (wing_dir / "manifest.json").write_text(
        __import__("json").dumps(manifest), encoding="utf-8")

    records = pw.read_md_dir(str(wing_dir))
    drawers = [r for r in records if r.get("type") == "drawer"]
    triples = [r for r in records if r.get("type") == "kg_triple"]
    assert len(drawers) == 3
    ins = [d for d in drawers if d["room"] == "insights"]
    assert ins[0]["content"].startswith("# Insights")     # preamble folded into 1st
    assert "## INSIGHT I1" in ins[0]["content"]
    assert ins[1]["content"].startswith("## INSIGHT I2")
    assert len(triples) == 2
    assert (triples[0]["subject"], triples[0]["predicate"]) == ("Ida", "works_on")


# --------------------------------------------------------------------------- #
# Import: dedup merge.
# --------------------------------------------------------------------------- #
def test_import_binds_palace_before_requiring_mempalace():
    # Regression: MEMPALACE_PALACE_PATH must be set before mempalace is imported,
    # so bind_palace must run before require_mempalace.
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 0}, "n", "t"),
    ])
    order = []
    real_bind = pw.bind_palace
    try:
        with patched(
            bind_palace=lambda p: (order.append("bind"), real_bind(p))[1],
            require_mempalace=lambda: order.append("require"),
            preflight_import_target=lambda *a, **k: None,
        ):
            rc = pw.main(["import", str(bundle), "--palace", "/nope"])
        assert rc == 0
        assert order == ["bind", "require"], order
    finally:
        bundle.unlink(missing_ok=True)


def test_import_skips_near_duplicate():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello", "a.md", "copilot-cli",
                          "d1", {"topic": "x"}),
    ])
    added = []
    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": True},
            add_drawer=lambda **kw: added.append(kw),
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--palace", "/nope"])
        assert rc == 0
        assert added == []  # duplicate skipped, no write
    finally:
        bundle.unlink(missing_ok=True)


def test_import_adds_with_target_wing_and_trailer_content():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello body", "a.md", "copilot-cli",
                          "d1", {"topic": "x", "hall": "ops"}),
    ])
    added = []
    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            add_drawer=lambda **kw: added.append(kw),
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--palace", "/nope"])
        assert rc == 0
        assert len(added) == 1
        call = added[0]
        assert call["wing"] == "avs"
        assert call["room"] == "scripting"
        assert call["content"].startswith("hello body")
        assert lib.TRAILER_MARKER in call["content"]  # extra preserved as trailer
    finally:
        bundle.unlink(missing_ok=True)


def test_import_into_wing_bypasses_dedup():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello", None, None, "d1", {}),
    ])
    added = []
    dup_calls = []

    def _dup(content, threshold):
        dup_calls.append(content)
        return {"is_duplicate": True}

    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=_dup,
            add_drawer=lambda **kw: added.append(kw),
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--into-wing", "avs_clone",
                          "--palace", "/nope"])
        assert rc == 0
        assert dup_calls == []             # dedup bypassed for clone
        assert len(added) == 1
        assert added[0]["wing"] == "avs_clone"
    finally:
        bundle.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Import: dry-run performs no writes.
# --------------------------------------------------------------------------- #
def test_import_dry_run_invokes_no_write_handlers():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0",
                           {"drawers": 1, "kg_triples": 1, "tunnels": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello", None, None, "d1", {}),
        lib.kg_triple_record("A", "r", "B", 1.0, None, None, "d1"),
        lib.tunnel_record("avs", "scripting", "conveyor", "general", "l"),
    ])
    add_calls, tunnel_calls, kg_opens = [], [], []

    def _open_kg(palace):
        kg_opens.append(palace)
        return FakeKG()

    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            add_drawer=lambda **kw: add_calls.append(kw),
            open_kg=_open_kg,
            create_tunnel=lambda **kw: tunnel_calls.append(kw),
        ):
            rc = pw.main(["import", str(bundle), "--dry-run", "--palace", "/nope"])
        assert rc == 0
        assert add_calls == []      # no add_drawer
        assert kg_opens == []       # KG never opened
        assert tunnel_calls == []   # no create_tunnel
    finally:
        bundle.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Import: KG uses KnowledgeGraph.add_triple (not an MCP handler).
# --------------------------------------------------------------------------- #
def test_import_kg_uses_knowledge_graph_add_triple():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"kg_triples": 1}, "n", "t"),
        lib.kg_triple_record("A", "rel", "B", 0.8, "2026-06-02", None, "d1"),
    ])
    kg = FakeKG()
    try:
        with patched(
            require_mempalace=lambda: None,
            open_kg=lambda palace: kg,
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--palace", "/nope"])
        assert rc == 0
        assert len(kg.triples) == 1
        t = kg.triples[0]
        assert (t["subject"], t["predicate"], t["obj"]) == ("A", "rel", "B")
        assert t["confidence"] == 0.8
        assert t["valid_from"] == "2026-06-02"
        assert kg.closed is True
    finally:
        bundle.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Import: tunnels — error skipped+counted, success counted, remap on --into-wing.
# --------------------------------------------------------------------------- #
def test_import_tunnel_error_is_skipped_success_counted():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"tunnels": 2}, "n", "t"),
        lib.tunnel_record("avs", "scripting", "conveyor", "general", "ok"),
        lib.tunnel_record("avs", "missing", "conveyor", "general", "bad"),
    ])
    calls = []

    def _create_tunnel(**kw):
        calls.append(kw)
        if kw["source_room"] == "missing":
            return {"error": "room 'missing' not found in wing 'avs'"}
        return {"tunnel_id": "abc"}

    try:
        with patched(
            require_mempalace=lambda: None,
            create_tunnel=_create_tunnel,
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--palace", "/nope"])
        assert rc == 0            # tunnel error is a skip, not a hard failure
        assert len(calls) == 2    # both attempted
    finally:
        bundle.unlink(missing_ok=True)


def test_import_tunnel_endpoint_remapped_under_into_wing():
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"tunnels": 1}, "n", "t"),
        lib.tunnel_record("avs", "scripting", "conveyor", "general", "l"),
    ])
    calls = []
    try:
        with patched(
            require_mempalace=lambda: None,
            create_tunnel=lambda **kw: calls.append(kw) or {"tunnel_id": "x"},
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            rc = pw.main(["import", str(bundle), "--into-wing", "avs_clone",
                          "--palace", "/nope"])
        assert rc == 0
        assert len(calls) == 1
        # source endpoint (manifest wing) remapped; target endpoint untouched.
        assert calls[0]["source_wing"] == "avs_clone"
        assert calls[0]["target_wing"] == "conveyor"
    finally:
        bundle.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Import: manifest validation.
# --------------------------------------------------------------------------- #
def test_import_rejects_unknown_bundle_version():
    manifest = lib.build_manifest("avs", "3.5.0", {}, "n", "t")
    manifest["bundle_version"] = 999
    bundle = _write_bundle([manifest])
    try:
        with patched(require_mempalace=lambda: None):
            try:
                pw.main(["import", str(bundle), "--palace", "/nope"])
                raised = False
            except SystemExit:
                raised = True
        assert raised
    finally:
        bundle.unlink(missing_ok=True)


def _assert_import_refused(bundle, palace, flags=()):
    added, tunnels, initialized = [], [], []
    kg = FakeKG()
    files = list(bundle.rglob("*")) if bundle.is_dir() else [bundle]
    before = {path: path.read_bytes() for path in files if path.is_file()}
    error = None
    with patched(
        require_mempalace=lambda: initialized.append(True),
        check_duplicate=lambda content, threshold: {"is_duplicate": False},
        add_drawer=lambda **kw: added.append(kw),
        open_kg=lambda palace: kg,
        create_tunnel=lambda **kw: tunnels.append(kw) or {"tunnel_id": "x"},
        preflight_import_target=lambda *a, **k: None,
        palace_drawer_count=lambda *a, **k: 2,
    ):
        args = pw.build_parser().parse_args(
            ["import", str(bundle), "--palace", str(palace), *flags])
        try:
            pw.cmd_import(args)
        except (SystemExit, ValueError) as exc:
            error = str(exc)
    assert added == [] and kg.triples == [] and tunnels == []
    assert initialized == [], "refusal must precede backend imports/initialization"
    assert error is not None
    assert any(word in error.lower() for word in ("procedural", "metadata", "record"))
    assert {path: path.read_bytes() for path in before} == before


def _marked_records(marker):
    records = _sample_records()
    records[0]["counts"]["drawers"] = 3
    record = lib.drawer_record("avs", "general", "incomplete payload", None, None, "unsafe", {})
    record.update(marker)
    return [*records, record]


def test_direct_jsonl_import_refuses_procedural_bundle_before_ordinary_writes(tmp_path):
    for index, marker in enumerate((
        {"room": "procedural"}, {"room": "procedural-sources"},
        {"kind": "procedural_event"}, {"kind": "procedural_source"},
        {"added_by": "dream-procedure"}, {"added_by": "dream-procedure-source"},
        {"extra": {"kind": "procedural_event"}},
        {"metadata": {"kind": "procedural_source"}},
        {"content": 'payload\n\n<!--dreaming-meta: {"kind":"procedural_event"}-->'},
        {"content": 'payload\n\n<!--dreaming-meta: {"kind":"procedural_source"}-->'},
    )):
        bundle = tmp_path / f"bundle-{index}.jsonl"
        bundle.write_text(lib.dump_jsonl(_marked_records(marker)), encoding="utf-8")
        _assert_import_refused(bundle, tmp_path / "target")


def test_direct_markdown_import_refuses_procedural_bundle_before_ordinary_writes(tmp_path):
    for index, marker in enumerate((
        {"room": "procedural"}, {"room": "procedural-sources"},
        {"added_by": "dream-procedure"}, {"added_by": "dream-procedure-source"},
        {"extra": {"kind": "procedural_event"}},
        {"extra": {"kind": "procedural_source"}},
        {"content": 'payload\n\n<!--dreaming-meta: {"kind":"procedural_source"}-->'},
    )):
        bundle = Path(pw.write_md_dir(_marked_records(marker), str(tmp_path / str(index))))
        _assert_import_refused(bundle, tmp_path / "target")


def test_import_flags_cannot_bypass_procedural_refusal(tmp_path):
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text(lib.dump_jsonl(_marked_records({"room": "procedural"})), encoding="utf-8")
    for flags in (("--force-add",), ("--into-wing", "clone"),
                  ("--create-new-palace",), ("--dry-run",)):
        _assert_import_refused(bundle, tmp_path / "target", flags)


def test_direct_import_refuses_malformed_procedural_trailers_before_writes(tmp_path):
    for index, body in enumerate((
        '<!--dreaming-meta: {"kind":"procedural_event",}-->',
        '<!--dreaming-meta: {"kind":"procedural_source"}',
        '<!--wing-meta: {"kind":"procedural_source" BROKEN}-->',
        '<!--dreaming-meta {"kind":"procedural_source"}-->',
    )):
        records = _marked_records({"content": body})
        bundle = tmp_path / f"bundle-{index}.jsonl"
        bundle.write_text(lib.dump_jsonl(records), encoding="utf-8")
        _assert_import_refused(bundle, tmp_path / "target")
        directory = Path(pw.write_md_dir(records, str(tmp_path / str(index))))
        _assert_import_refused(directory, tmp_path / "target")


def test_markdown_preflight_does_not_erase_malformed_or_hidden_metadata(tmp_path):
    for index, extra in enumerate((
        'extra: {"kind":"procedural_source",}',
        'extra: ["procedural_event"]',
        "extra: {}\nkind: procedural_source",
        "extra: {}\nagent: dream-procedure",
        "extra: {}\nkind: procedural_source\nkind: source",
    )):
        directory = Path(pw.write_md_dir(_marked_records({}), str(tmp_path / str(index))))
        drawer = directory / "unsafe.md"
        drawer.write_text(drawer.read_text(encoding="utf-8").replace("extra: {}", extra),
                          encoding="utf-8")
        _assert_import_refused(directory, tmp_path / "target")


def test_markdown_preflight_refuses_unrecognized_and_malformed_header_lines(tmp_path):
    for index, line in enumerate((
        "kind procedural_source", 'extra {"kind":"procedural_source"}',
        "unknown: procedural_source", ": procedural_source",
    )):
        directory = Path(pw.write_md_dir(_marked_records({}), str(tmp_path / str(index))))
        drawer = directory / "unsafe.md"
        drawer.write_text(drawer.read_text(encoding="utf-8").replace(
            "extra: {}", f"extra: {{}}\n{line}"), encoding="utf-8")
        _assert_import_refused(directory, tmp_path / "target")


def test_markdown_preflight_checks_manifest_entries_not_only_decoded_drawers(tmp_path):
    directory = Path(pw.write_md_dir(_sample_records(), str(tmp_path)))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["drawers"].append({
        "room": "procedural-sources", "content_file": "general__0001.md"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _assert_import_refused(directory, tmp_path / "target")


def test_legacy_markdown_preflight_checks_comments_before_stripping_them(tmp_path):
    directory = tmp_path / "legacy"
    directory.mkdir()
    manifest = lib.build_manifest("avs", "legacy", {"drawers": 2}, "n", "t")
    manifest["drawers"] = [
        {"room": "general", "content_file": "ordinary.md"},
        {"room": "general", "content_file": "hidden.md"},
    ]
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "ordinary.md").write_text("# ordinary", encoding="utf-8")
    (directory / "hidden.md").write_text(
        '<!--dreaming-meta: {"kind":"procedural_source"}-->\nsource payload',
        encoding="utf-8")
    _assert_import_refused(directory, tmp_path / "target")


def test_jsonl_preflight_rejects_duplicate_keys_hiding_procedural_metadata(tmp_path):
    bundle = tmp_path / "duplicate.jsonl"
    bundle.write_text(lib.dump_jsonl(_sample_records()) +
                      '{"type":"drawer","room":"procedural","room":"general",'
                      '"content":"hidden event"}\n', encoding="utf-8")
    _assert_import_refused(bundle, tmp_path / "target")


def test_markdown_manifest_preflight_rejects_duplicate_procedural_keys(tmp_path):
    directory = Path(pw.write_md_dir(_sample_records(), str(tmp_path)))
    manifest = directory / "manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        '"room": "scripting"', '"room": "procedural", "room": "scripting"'),
        encoding="utf-8")
    _assert_import_refused(directory, tmp_path / "target")


def test_preflight_rejects_duplicate_procedural_trailer_and_extra_keys(tmp_path):
    records = _marked_records({
        "content": '<!--dreaming-meta: {"kind":"procedural_event","kind":"source"}-->'})
    bundle = tmp_path / "duplicate-trailer.jsonl"
    bundle.write_text(lib.dump_jsonl(records), encoding="utf-8")
    _assert_import_refused(bundle, tmp_path / "target")
    directory = Path(pw.write_md_dir(_marked_records({}), str(tmp_path / "md")))
    drawer = directory / "unsafe.md"
    drawer.write_text(drawer.read_text(encoding="utf-8").replace(
        "extra: {}", 'extra: {"kind":"procedural_source","kind":"source"}'), encoding="utf-8")
    _assert_import_refused(directory, tmp_path / "target")


def test_direct_import_keeps_ordinary_source_only_archives_supported(tmp_path):
    content = "Archived observation mentioning procedural_event and procedural_source as prose."
    records = [
        lib.build_manifest("avs", "v", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "sources", content, "session.jsonl", "archive-agent",
                          "source-original", {"kind": "source", "source_hash": "a" * 64}),
    ]
    jsonl = tmp_path / "archive.jsonl"
    jsonl.write_text(lib.dump_jsonl(records), encoding="utf-8")
    directory = Path(pw.write_md_dir(records, str(tmp_path / "md")))
    for bundle in (jsonl, directory):
        added = []
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            add_drawer=lambda **kw: added.append(kw),
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            args = pw.build_parser().parse_args(
                ["import", str(bundle), "--palace", str(tmp_path / "target")])
            assert pw.cmd_import(args) == 0
        assert len(added) == 1
        assert added[0]["room"] == "sources"
        assert added[0]["added_by"] == "archive-agent"
        assert lib.decode_trailer(added[0]["content"]) == (
            content, {"kind": "source", "source_hash": "a" * 64})


def test_ordinary_metadata_with_comment_delimiters_survives_export_import(tmp_path):
    topic = 'A --> B; quoted <!--wing-meta: {"kind":"procedural_source"}--> example'
    content = lib.encode_trailer("ordinary observation", {"topic": topic})
    rows = [{"id": "ordinary", "text": content,
             "metadata": {"wing": "avs", "room": "sources"}}]
    for format in ("jsonl", "md"):
        out = tmp_path / format
        added = []
        with patched(
            mempalace_version=lambda: "v",
            read_wing_drawer_rows=lambda palace, wing: rows,
            read_wing_triples=lambda palace: [],
            read_tunnels=lambda palace: [],
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            add_drawer=lambda **kw: added.append(kw),
            preflight_import_target=lambda *a, **k: None,
            palace_drawer_count=lambda *a, **k: 1,
        ):
            assert pw.main(["export", "avs", "--format", format, "--out", str(out),
                            "--palace", str(tmp_path / "source")]) == 0
            bundle = out / "avs" if format == "md" else out
            assert pw.cmd_import(pw.build_parser().parse_args(
                ["import", str(bundle), "--palace", str(tmp_path / "target")])) == 0
        assert len(added) == 1
        assert added[0]["content"] == content
        assert lib.decode_trailer(added[0]["content"]) == ("ordinary observation", {"topic": topic})


# --------------------------------------------------------------------------- #
# Palace layout resolution + stray-palace guard.
# --------------------------------------------------------------------------- #
def test_resolve_palace_layout_default(tmp_path):
    home = tmp_path / ".mempalace"
    home.mkdir()
    palace_dir, kg = pw.resolve_palace_layout(home)
    assert palace_dir == home / "palace"
    assert kg == home / "knowledge_graph.sqlite3"


def test_resolve_palace_layout_honors_config_palace_path(tmp_path):
    home = tmp_path / ".mempalace"
    home.mkdir()
    custom = tmp_path / "elsewhere" / "db"
    (home / "config.json").write_text(
        f'{{"palace_path": "{custom.as_posix()}"}}', encoding="utf-8")
    palace_dir, kg = pw.resolve_palace_layout(home)
    assert palace_dir == custom                       # chroma follows config
    assert kg == home / "knowledge_graph.sqlite3"     # KG stays home-level


def test_bind_palace_sets_resolved_chroma_dir_not_home(tmp_path):
    home = tmp_path / ".mempalace"
    home.mkdir()
    returned = pw.bind_palace(str(home))
    import os
    # Returns HOME (readers join their own subpaths), but the env points at the
    # nested palace/ dir so writes never create a stray <home>/chroma.sqlite3.
    assert returned == str(home)
    assert os.environ["MEMPALACE_PALACE_PATH"] == str(home / "palace")


def test_import_aborts_on_missing_palace(tmp_path):
    # Real preflight + palace_drawer_count returning None (no chroma) must abort.
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello", None, None, "d1", {}),
    ])
    added = []
    try:
        with patched(
            require_mempalace=lambda: None,
            palace_drawer_count=lambda *a, **k: None,   # chroma missing
            add_drawer=lambda **kw: added.append(kw),
        ):
            try:
                pw.main(["import", str(bundle), "--palace", str(tmp_path)])
                raised = False
            except SystemExit:
                raised = True
        assert raised            # guard fired
        assert added == []       # no writes attempted
    finally:
        bundle.unlink(missing_ok=True)


def test_import_create_new_palace_allows_missing(tmp_path):
    bundle = _write_bundle([
        lib.build_manifest("avs", "3.5.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "scripting", "hello", None, None, "d1", {}),
    ])
    added = []
    try:
        with patched(
            require_mempalace=lambda: None,
            check_duplicate=lambda content, threshold: {"is_duplicate": False},
            palace_drawer_count=lambda *a, **k: None,   # chroma missing
            add_drawer=lambda **kw: added.append(kw),
        ):
            rc = pw.main(["import", str(bundle), "--palace", str(tmp_path),
                          "--create-new-palace"])
        assert rc == 0
        assert len(added) == 1   # write proceeds when explicitly opted in
    finally:
        bundle.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Argument parsing.
# --------------------------------------------------------------------------- #
def test_parser_export_defaults():
    args = pw.build_parser().parse_args(["export", "avs"])
    assert args.command == "export"
    assert args.wing == "avs"
    assert args.palace == pw.DEFAULT_PALACE
    assert args.func is pw.cmd_export


def test_parser_import_defaults_and_required():
    args = pw.build_parser().parse_args(["import", "bundle.jsonl"])
    assert args.command == "import"
    assert args.bundle == "bundle.jsonl"
    assert args.into_wing is None
    assert args.dry_run is False
    assert args.force_add is False
    assert args.dup_threshold == 0.9
    assert args.create_new_palace is False
    assert args.func is pw.cmd_import


def test_parser_import_flags():
    args = pw.build_parser().parse_args(
        ["import", "b.jsonl", "--into-wing", "clone", "--dry-run",
         "--force-add", "--dup-threshold", "0.8"])
    assert args.into_wing == "clone"
    assert args.dry_run is True
    assert args.force_add is True
    assert args.dup_threshold == 0.8


def test_parser_requires_subcommand():
    try:
        pw.build_parser().parse_args([])
        raised = False
    except SystemExit:
        raised = True
    assert raised


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
