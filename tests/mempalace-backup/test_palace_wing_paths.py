"""Disposable SQLite integration tests for wing DATA and KG resolution."""

import contextlib
import json
import os
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

import palace_wing as pw
import palace_wing_lib as lib
import palace_runtime
import pytest
from test_palace_wing import patched


@pytest.fixture(autouse=True)
def modern_runtime(monkeypatch):
    monkeypatch.setattr(palace_runtime, "installed_version", lambda: "3.12.0")


def _create_graph(path, *, subject=None, entity=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.executescript("""
            CREATE TABLE triples (
                id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                valid_from TEXT, valid_to TEXT, confidence REAL, source_drawer_id TEXT
            );
            CREATE TABLE entities (id TEXT);
        """)
        if subject is not None:
            con.execute(
                "INSERT INTO triples (subject, predicate, object, confidence, source_drawer_id) "
                "VALUES (?, 'rel', 'object', 1.0, 'd1')", (subject,))
        if entity is not None:
            con.execute("INSERT INTO entities VALUES (?)", (entity,))
        con.commit()


def _graph_subjects(path):
    with contextlib.closing(sqlite3.connect(path)) as con:
        return [row[0] for row in con.execute("SELECT subject FROM triples ORDER BY id")]


class SqliteGraph:
    def __init__(self, db_path):
        self.con = sqlite3.connect(db_path)

    def add_triple(self, subject, predicate, obj, confidence=None,
                   valid_from=None, valid_to=None):
        self.con.execute(
            "INSERT INTO triples (subject, predicate, object, confidence) VALUES (?, ?, ?, ?)",
            (subject, predicate, obj, confidence))
        self.con.commit()

    def close(self):
        self.con.close()


def _create_chroma(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as con:
        con.executescript("""
            CREATE TABLE embeddings (id INTEGER PRIMARY KEY, embedding_id TEXT);
            CREATE TABLE embedding_metadata (
                id INTEGER, key TEXT, string_value TEXT,
                int_value INTEGER, float_value REAL, bool_value INTEGER
            );
            INSERT INTO embeddings VALUES (7, 'd1');
            INSERT INTO embedding_metadata (id, key, string_value) VALUES
                (7, 'wing', 'avs'),
                (7, 'room', 'general'),
                (7, 'chroma:document', 'the drawer');
        """)
        con.commit()


def test_wing_data_path_accepts_bom_in_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    custom = home / "custom"
    config = home / "config.json"
    original = ("\ufeff" + json.dumps({"palace_path": str(custom)})).encode("utf-8")
    config.write_bytes(original)
    assert pw.resolve_palace_layout(home) == (custom, custom / "knowledge_graph.sqlite3")
    assert config.read_bytes() == original


def test_export_uses_populated_data_graph_instead_of_legacy(tmp_path):
    home = tmp_path / "home"
    legacy = home / "knowledge_graph.sqlite3"
    active = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(legacy, subject="legacy")
    _create_graph(active, subject="active")
    out = tmp_path / "wing.jsonl"
    rows = [{"id": "d1", "text": "content",
             "metadata": {"wing": "avs", "room": "general"}}]
    with patched(read_wing_drawer_rows=lambda palace, wing: rows,
                 mempalace_version=lambda: "test"):
        assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["active"]
    assert _graph_subjects(legacy) == ["legacy"]


def test_wing_reads_populated_legacy_when_default_data_graph_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".mempalace"
    legacy = home / "knowledge_graph.sqlite3"
    active = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(legacy, subject="legacy")
    _create_graph(active)
    assert [row["subject"] for row in pw.read_wing_triples(str(home))] == ["legacy"]


def test_data_graph_with_entities_does_not_fall_back_to_legacy(tmp_path):
    home = tmp_path / "home"
    _create_graph(home / "knowledge_graph.sqlite3", subject="legacy")
    _create_graph(home / "palace" / "knowledge_graph.sqlite3", entity="present")
    assert pw.read_wing_triples(str(home)) == []


def test_unreadable_data_graph_is_not_silently_replaced_by_legacy(tmp_path):
    home = tmp_path / "home"
    _create_graph(home / "knowledge_graph.sqlite3", subject="legacy")
    local = home / "palace" / "knowledge_graph.sqlite3"
    local.parent.mkdir()
    local.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        pw.read_wing_triples(str(home))


def test_import_writes_selected_data_graph_not_legacy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy = home / "knowledge_graph.sqlite3"
    active = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(legacy, subject="legacy")
    _create_graph(active, subject="active")
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text(lib.dump_jsonl([
        lib.build_manifest("avs", "3.12.0", {"kg_triples": 1}, "n", "t"),
        lib.kg_triple_record("new", "rel", "object", 1.0, None, None, "d1"),
    ]), encoding="utf-8")

    mempalace = types.ModuleType("mempalace")
    mempalace.__path__ = []
    graph_module = types.ModuleType("mempalace.knowledge_graph")
    graph_module.KnowledgeGraph = SqliteGraph
    monkeypatch.setitem(sys.modules, "mempalace", mempalace)
    monkeypatch.setitem(sys.modules, "mempalace.knowledge_graph", graph_module)
    with patched(require_mempalace=lambda: None,
                 preflight_import_target=lambda *args, **kwargs: None):
        assert pw.main(["import", str(bundle), "--palace", str(home)]) == 0
    assert _graph_subjects(active) == ["active", "new"]
    assert _graph_subjects(legacy) == ["legacy"]


@pytest.mark.parametrize("env_name", [
    "MEMPALACE_PALACE_PATH", "MEMPALACE_PALACE", "MEMPAL_PALACE_PATH",
])
def test_wing_data_environment_override_wins_config(tmp_path, monkeypatch, env_name):
    home = tmp_path / "home"
    home.mkdir()
    configured = home / "configured"
    selected = home / "selected"
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(configured)}), encoding="utf-8")
    monkeypatch.setenv(env_name, str(selected))
    data, kg = pw.resolve_palace_layout(home)
    assert data == selected
    assert kg == selected / "knowledge_graph.sqlite3"


def test_explicit_wing_data_path_wins_environment_and_is_restored(tmp_path, monkeypatch):
    home = tmp_path / "home"
    selected = home / "selected"
    _create_graph(selected / "knowledge_graph.sqlite3", subject="selected")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(home / "environment"))
    out = tmp_path / "wing.jsonl"
    rows = [{"id": "d1", "text": "content",
             "metadata": {"wing": "avs", "room": "general"}}]
    with patched(read_wing_drawer_rows=lambda palace, wing: rows,
                 mempalace_version=lambda: "test"):
        assert pw.main(["export", "avs", "--palace", str(home),
                        "--data-path", str(selected), "--out", str(out)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["selected"]
    assert os.environ["MEMPALACE_PALACE_PATH"] == str(home / "environment")


def test_custom_data_with_only_legacy_graph_requires_explicit_kg_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".mempalace"
    custom = home / "custom"
    custom.mkdir(parents=True)
    legacy = home / "knowledge_graph.sqlite3"
    _create_graph(legacy, subject="legacy")
    (home / "config.json").write_text(
        json.dumps({"palace_path": str(custom)}), encoding="utf-8")
    out = tmp_path / "wing.jsonl"
    rows = [{"id": "d1", "text": "content",
             "metadata": {"wing": "avs", "room": "general"}}]
    with patched(read_wing_drawer_rows=lambda palace, wing: rows,
                 mempalace_version=lambda: "test"):
        with pytest.raises(SystemExit, match="--kg-path"):
            pw.main(["export", "avs", "--palace", str(home), "--out", str(out)])
        assert not out.exists()
        assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out),
                        "--kg-path", str(legacy)]) == 0
        records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
        assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["legacy"]
        local = custom / "knowledge_graph.sqlite3"
        assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out),
                        "--kg-path", str(local)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert not [r for r in records if r.get("type") == "kg_triple"]
    assert not local.exists()


def test_invalid_kg_override_fails_before_export_and_restores_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    original = home / "configured"
    selected = home / "selected"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(original))
    out = tmp_path / "wing.jsonl"
    with pytest.raises(SystemExit, match="--kg-path"):
        pw.main(["export", "avs", "--palace", str(home), "--data-path", str(selected),
                 "--kg-path", str(tmp_path / "unrelated.sqlite3"), "--out", str(out)])
    assert not out.exists()
    assert os.environ["MEMPALACE_PALACE_PATH"] == str(original)


def test_data_kg_path_with_uri_special_characters(tmp_path):
    home = tmp_path / "home ?# space"
    active = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(active, subject="active")
    assert [row["subject"] for row in pw.read_wing_triples(str(home))] == ["active"]


def test_chroma_with_uri_special_characters_is_read_without_creating_a_stray_file(tmp_path):
    home = tmp_path / "home ?# space"
    _create_chroma(home / "palace" / "chroma.sqlite3")
    assert len(pw.read_wing_drawer_rows(str(home), "avs")) == 1
    assert pw.palace_drawer_count(str(home), "avs") == 1
    assert not (tmp_path / "home ").exists()


def test_valid_empty_chroma_reports_zero_drawers(tmp_path):
    home = tmp_path / "home"
    db = home / "palace" / "chroma.sqlite3"
    db.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT)")
        con.commit()
    assert pw.palace_drawer_count(str(home), "avs") == 0


def test_invalid_chroma_schema_blocks_import_before_writes(tmp_path):
    home = tmp_path / "home"
    db = home / "palace" / "chroma.sqlite3"
    db.parent.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(db)) as con:
        con.execute("CREATE TABLE unrelated (id INTEGER)")
        con.commit()
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text(lib.dump_jsonl([
        lib.build_manifest("avs", "3.12.0", {"drawers": 1}, "n", "t"),
        lib.drawer_record("avs", "general", "content", None, None, "d1", {}),
    ]), encoding="utf-8")
    added = []
    with patched(require_mempalace=lambda: None,
                 check_duplicate=lambda content, threshold: {"is_duplicate": False},
                 add_drawer=lambda **kwargs: added.append(kwargs)):
        with pytest.raises(sqlite3.OperationalError, match="embedding_metadata"):
            pw.main(["import", str(bundle), "--palace", str(home)])
    assert added == []


def test_export_matches_single_chunk_graph_sources_to_chroma_embedding_ids(tmp_path):
    home = tmp_path / "home"
    _create_chroma(home / "palace" / "chroma.sqlite3")
    _create_graph(home / "palace" / "knowledge_graph.sqlite3", subject="active")
    out = tmp_path / "wing.jsonl"
    assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["active"]


def test_dangling_data_graph_cannot_fall_back_to_legacy(tmp_path):
    home = tmp_path / "home"
    _create_graph(home / "knowledge_graph.sqlite3", subject="legacy")
    local = home / "palace" / "knowledge_graph.sqlite3"
    local.parent.mkdir()
    local.symlink_to(tmp_path / "missing-graph")
    with pytest.raises((SystemExit, OSError), match="graph|KG|knowledge_graph"):
        pw.read_wing_triples(str(home))
    with pytest.raises((SystemExit, OSError), match="graph|KG|knowledge_graph"):
        pw.resolve_palace_layout(home, local)
    assert not (tmp_path / "missing-graph").exists()


def test_custom_home_requires_explicit_selection_of_external_state_graph(tmp_path):
    home = tmp_path / "alternate-home"
    home.mkdir()
    state_graph = Path.home() / ".mempalace" / "knowledge_graph.sqlite3"
    _create_graph(state_graph, subject="external")
    out = tmp_path / "wing.jsonl"
    rows = [{"id": "d1", "text": "content",
             "metadata": {"wing": "avs", "room": "general"}}]
    with patched(read_wing_drawer_rows=lambda palace, wing: rows,
                 mempalace_version=lambda: "test"):
        with pytest.raises(SystemExit, match="--kg-path"):
            pw.main(["export", "avs", "--palace", str(home), "--out", str(out)])
        assert not out.exists()
        assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out),
                        "--kg-path", str(state_graph)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["external"]


def test_import_can_explicitly_select_external_state_graph(tmp_path):
    home = tmp_path / "alternate-home"
    home.mkdir()
    state_graph = Path.home() / ".mempalace" / "knowledge_graph.sqlite3"
    _create_graph(state_graph, subject="external")
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text(lib.dump_jsonl([
        lib.build_manifest("avs", "3.12.0", {"kg_triples": 1}, "n", "t"),
        lib.kg_triple_record("new", "rel", "object", 1.0, None, None, "d1"),
    ]), encoding="utf-8")
    with patched(require_mempalace=lambda: None,
                 preflight_import_target=lambda *args, **kwargs: None,
                 open_kg=lambda palace, kg_path: SqliteGraph(kg_path)):
        assert pw.main(["import", str(bundle), "--palace", str(home),
                        "--kg-path", str(state_graph)]) == 0
    assert _graph_subjects(state_graph) == ["external", "new"]
    assert not (home / "palace" / "knowledge_graph.sqlite3").exists()


def test_mem_palace_38_requires_graph_selection_even_when_data_is_populated(tmp_path):
    home = tmp_path / "home"
    local = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(local, subject="active")
    with patch.object(palace_runtime, "installed_version", return_value="3.8.0"):
        with pytest.raises(SystemExit, match="--kg-path"):
            pw.resolve_palace_layout(home)
        assert pw.resolve_palace_layout(home, local) == (home / "palace", local)


def test_unknown_runtime_requires_explicit_graph_when_facts_exist(tmp_path):
    home = tmp_path / "home"
    local = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(local, subject="active")
    with patch.object(palace_runtime, "installed_version", return_value=None):
        with pytest.raises(SystemExit, match="--kg-path"):
            pw.resolve_palace_layout(home)
        assert pw.resolve_palace_layout(home, local) == (home / "palace", local)


def test_wing_export_without_mempalace_uses_explicit_graph(tmp_path):
    home = tmp_path / "home"
    local = home / "palace" / "knowledge_graph.sqlite3"
    _create_graph(local, subject="active")
    out = tmp_path / "wing.jsonl"
    rows = [{"id": "d1", "text": "content",
             "metadata": {"wing": "avs", "room": "general"}}]
    with patch.object(palace_runtime, "installed_version", return_value=None), \
            patch.dict(sys.modules, {"mempalace": None}), \
            patched(read_wing_drawer_rows=lambda palace, wing: rows):
        assert pw.main(["export", "avs", "--palace", str(home), "--out", str(out),
                        "--kg-path", str(local)]) == 0
    records = lib.parse_jsonl(out.read_text(encoding="utf-8"))
    assert [r["subject"] for r in records if r.get("type") == "kg_triple"] == ["active"]


def test_direct_import_restores_data_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", "")
    home = tmp_path / "home"
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text(lib.dump_jsonl([
        lib.build_manifest("avs", "3.12.0", {}, "n", "t"),
    ]), encoding="utf-8")
    args = pw.build_parser().parse_args(["import", str(bundle), "--palace", str(home), "--dry-run"])
    with patched(require_mempalace=lambda: None):
        assert pw.cmd_import(args) == 0
    assert os.environ["MEMPALACE_PALACE_PATH"] == ""
