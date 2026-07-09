#!/usr/bin/env python3
"""Tests for palace_wing.py (CLI + mempalace adapter, all I/O mocked).

No live palace and no importable ``mempalace`` required: every mempalace /
palace-SQLite seam on the module is replaced with an in-memory fake.

Run: ``python3 -m pytest test_palace_wing.py`` or ``python3 test_palace_wing.py``.
"""
from __future__ import annotations

import contextlib
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import palace_wing as pw  # noqa: E402
import palace_wing_lib as lib  # noqa: E402

_SCRATCH = Path(__file__).resolve().parent


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
    return _SCRATCH / f".test-bundle-{uuid.uuid4().hex}.jsonl"


def _out_path() -> Path:
    return _SCRATCH / f".test-out-{uuid.uuid4().hex}.jsonl"


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
