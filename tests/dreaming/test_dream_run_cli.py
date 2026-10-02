"""Native run IDs, optional exports, and explicit legacy CLI compatibility."""
import json
import pytest

import dream_decide
import dream_harvest
import dream_show
import dream_survey
import dream_adopt
import dream_incremental
from test_incremental import inputs, review, writer, proposal


def test_bare_harvest_needs_no_repository_wing_or_export(inputs, tmp_path, capsys):
    assert dream_harvest.main(["--palace", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    loaded = dream_incremental.load_run(str(tmp_path), report["run_id"])
    assert len(loaded["coverage"]) == 62
    assert loaded["incremental"]["scope"] == {
        "scope_schema": 1, "repository": None, "wings": None}
    assert not (tmp_path / "worklist.json").exists()


def test_save_show_and_adopt_run_id_without_export_files(inputs, tmp_path):
    worklist = dream_incremental.harvest(str(tmp_path))
    identity = worklist["incremental"]["run_id"]
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps(review(worklist)))
    assert dream_decide.main(["--palace", str(tmp_path), "--run-id", identity,
                              "--decisions", str(decisions)]) == 0
    decisions.unlink()
    output = tmp_path / "recovered.json"
    assert dream_show.main(["--palace", str(tmp_path), "--run-id", identity,
                            "--out", str(output)]) == 0
    assert json.loads(output.read_text())["completion"]["action"] == "complete"
    output.unlink()
    assert dream_adopt.main(["--palace", str(tmp_path), "--run-id", identity]) == 0
    inputs[0].unlink()
    assert dream_adopt.main(["--palace", str(tmp_path), "--run-id", identity]) == 0


def test_bare_survey_uses_one_joint_all_wing_manifest(inputs, tmp_path, capsys):
    assert dream_survey.main(["--palace", str(tmp_path), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    run = report["tasks"]["reflect"]["incremental"]
    assert run["scope"]["repository"] is None
    assert run["scope"]["wings"] is None
    assert len(dream_incremental.load_run(str(tmp_path), run["run_id"])["coverage"]) == 62


def test_ontology_suggestion_cli_defaults_to_native_control_state(inputs, tmp_path, monkeypatch):
    import dream_store
    import dream_palace
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", "suggest-rules"]) == 0
    events = dream_store.DreamStore(str(tmp_path)).events()
    assert any(event["record_type"] == "ontology_saved" for event in events)
    assert not (tmp_path / "ontology.json").exists()


def test_show_reports_outstanding_native_proposal_hold(inputs, tmp_path, writer, capsys):
    import pytest
    worklist = review(dream_incremental.harvest(str(tmp_path)))
    worklist["items"] = [proposal()]
    writer.fail_on = 1
    with pytest.raises(RuntimeError):
        dream_incremental.complete(str(tmp_path), worklist)
    assert dream_show.main(["--palace", str(tmp_path), "--run-id",
                            worklist["incremental"]["run_id"]]) == 0
    text = capsys.readouterr().out
    assert "blocked" in text
    assert "lesson-1" in text


def test_explicit_survey_induction_remains_a_nonpublishing_preview(inputs, tmp_path, monkeypatch):
    import dream_store
    import dream_palace
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    before = dream_store.DreamStore(str(tmp_path)).events()
    assert dream_survey.induce(str(tmp_path)) == []
    assert dream_store.DreamStore(str(tmp_path)).events() == before


def test_new_ontology_export_does_not_erase_enabled_native_rules(inputs, tmp_path, monkeypatch):
    import dream_store
    import dream_palace
    import dream_ontology
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    doc = {"version": 1, "rules": [{"id": "transitive:depends_on", "enabled": True,
                                  "family": "transitive", "predicate": "depends_on"}]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(tmp_path))
    output = tmp_path / "new-export.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", "suggest-rules",
                              "--ontology-out", str(output)]) == 0
    assert dream_ontology.read_ontology_doc(palace=str(tmp_path)) == doc
    assert json.loads(output.read_text()) == doc


def test_explicit_rules_import_preserves_enabled_flags_in_native_state(inputs, tmp_path, monkeypatch):
    import dream_store
    import dream_palace
    import dream_ontology
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    doc = {"version": 1, "rules": [{"id": "transitive:depends_on", "enabled": True,
                                  "family": "transitive", "predicate": "depends_on"}]}
    source = tmp_path / "explicit-rules.json"
    source.write_text(json.dumps(doc))
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", "suggest-rules",
                              "--rules", str(source)]) == 0
    assert dream_ontology.read_ontology_doc(palace=str(tmp_path)) == doc


def test_mutating_derive_imports_explicit_rules_and_skips_but_dry_run_does_not(inputs, tmp_path, monkeypatch):
    import dream_store
    import dream_palace
    import dream_ontology
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)

    class EmptyWriter:
        def close(self):
            pass

    monkeypatch.setattr(dream_palace, "KgDeriveWriter", lambda palace: EmptyWriter())
    doc = {"version": 1, "rules": [{"id": "transitive:depends_on", "enabled": True,
                                  "family": "transitive", "predicate": "depends_on"}]}
    marker = {"candidate_id": "previously-rejected", "ontology_version": "reviewed-version"}
    rules, skips, decisions = tmp_path / "rules.json", tmp_path / "skips.jsonl", tmp_path / "derive.json"
    rules.write_text(json.dumps(doc))
    skips.write_text(json.dumps(marker) + "\n")
    decisions.write_text(json.dumps({"task": "contemplate", "items": []}))
    args = ["--palace", str(tmp_path), "--decisions", str(decisions), "--rules", str(rules),
            "--skips", str(skips)]
    before = dream_store.DreamStore(str(tmp_path)).events()
    assert dream_adopt.main([*args, "--dry-run"]) == 0
    assert dream_store.DreamStore(str(tmp_path)).events() == before
    assert dream_adopt.main(args) == 0
    rules.unlink()
    skips.unlink()
    assert dream_ontology.read_ontology_doc(palace=str(tmp_path)) == doc
    assert dream_palace.load_skip_markers(palace=str(tmp_path)) == [marker]


def test_prune_scoring_excludes_control_drawers_from_the_population():
    original = {"id": "original", "text": "original source", "wing": "A", "room": "notes",
                "embedding": [1., 0.], "metadata": {"filed_at": "2000-01-01"}}
    control = {**original, "id": "control", "metadata": {"kind": "dream_control"}}
    scored = dream_harvest.score_prune_drawers([original, control], {}, {})
    assert [drawer["id"] for drawer in scored] == ["original"]


def test_explicit_recurrence_preview_cannot_use_control_as_third_source(inputs, tmp_path, monkeypatch):
    import dream_palace
    entries = [{"id": f"entry-{i}", "text": f"Original observation {i}", "session_id": f"session-{i}",
                "embedding": [1., 0.], "metadata": {"kind": "dream_control"} if i == 2 else {}}
               for i in range(3)]
    monkeypatch.setattr(dream_palace, "load_observation_entries", lambda *args, **kwargs: entries)
    output = tmp_path / "explicit-preview.json"
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", "reflect", "--source", "diary",
                              "--min-support", "3", "--out", str(output)]) == 0
    assert json.loads(output.read_text())["items"] == []


@pytest.mark.parametrize("task", ["suggest-rules", "induce-rules"])
def test_existing_ontology_output_cannot_enable_native_rules(inputs, tmp_path, monkeypatch, task):
    import dream_store
    import dream_palace
    import dream_ontology
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    rule = {"id": "transitive:depends_on", "enabled": False,
            "family": "transitive", "predicate": "depends_on"}
    doc = {"version": 1, "rules": [rule]}
    dream_ontology.write_ontology_doc(None, doc, palace=str(tmp_path))
    output = tmp_path / "stale-export.json"
    output.write_text(json.dumps({"version": 1, "rules": [{**rule, "enabled": True}]}))
    assert dream_harvest.main(["--palace", str(tmp_path), "--task", task,
                              "--ontology-out", str(output)]) == 0
    assert dream_ontology.read_ontology_doc(palace=str(tmp_path)) == doc
    assert json.loads(output.read_text()) == doc


@pytest.mark.parametrize("task", ["suggest-rules", "induce-rules"])
def test_ontology_harvest_cannot_overwrite_concurrent_approved_disable(inputs, tmp_path, monkeypatch, task):
    import threading
    import dream_store
    import dream_palace
    import dream_ontology
    monkeypatch.setattr(dream_store, "native_call_tool", dream_incremental._store(str(tmp_path))._caller)
    monkeypatch.setattr(dream_palace, "load_premises", lambda *args, **kwargs: [])
    rule = {"id": "transitive:depends_on", "enabled": True,
            "family": "transitive", "predicate": "depends_on"}
    enabled = {"version": 1, "rules": [rule]}
    disabled = {"version": 1, "rules": [{**rule, "enabled": False}]}
    dream_ontology.write_ontology_doc(None, enabled, palace=str(tmp_path))
    read = dream_ontology.read_ontology_doc
    observed, resume = threading.Event(), threading.Event()
    failures = []

    def pause_after_read(*args, **kwargs):
        result = read(*args, **kwargs)
        if threading.current_thread().name == "harvest-config" and not observed.is_set():
            observed.set()
            assert resume.wait(timeout=10)
        return result

    def harvest_config():
        try:
            assert dream_harvest.main(["--palace", str(tmp_path), "--task", task]) == 0
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(dream_ontology, "read_ontology_doc", pause_after_read)
    harvester = threading.Thread(target=harvest_config, name="harvest-config")
    harvester.start()
    disabled_before_publication = False
    try:
        assert observed.wait(timeout=10)
        try:
            with dream_palace.palace_mutation_lock(str(tmp_path), timeout_seconds=0):
                dream_ontology.write_ontology_doc(None, disabled, palace=str(tmp_path))
                disabled_before_publication = True
        except TimeoutError:
            pass
    finally:
        resume.set()
        harvester.join(timeout=10)
    assert not harvester.is_alive()
    assert not failures
    if not disabled_before_publication:
        dream_ontology.write_ontology_doc(None, disabled, palace=str(tmp_path))
    assert read(palace=str(tmp_path)) == disabled
