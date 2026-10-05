"""Isolated adapter contracts; these fixtures do not prove native host delivery."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid


SCRIPT = Path(__file__).resolve().parents[2] / "hooks" / "procedural_context.py"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


def packet():
    return {
        "policy_version": "procedural-v1", "as_of": "2026-09-26T20:00:00Z",
        "repository": "owner/repo", "status": "ok", "item_count": 1,
        "omitted_count": 0, "rules": [{
            "rule_id": "proc:" + "a" * 64, "rule_type": "rule", "statement": "Keep original evidence.",
            "applies_when": "Changing memory adapters", "exceptions": ["Not generated lineage"],
            "maturity": "established", "effective_score": 1.0, "relevance": 0.8,
            "latest_validation": "2026-09-26T20:00:00Z",
            "evidence": [{"source_kind": "session_turn", "source_id": "original-one"}],
            "delivery": "guidance",
        }], "anti_patterns": [], "trials": [],
    }


class AdapterTests(unittest.TestCase):
    def setUp(self):
        # Fail closed rather than allowing tempfile's fallback to the user's HOME or /tmp.
        base = os.environ.get("DREAMING_TEST_TMPDIR")
        if not base or not Path(base).is_absolute():
            self.fail("Set DREAMING_TEST_TMPDIR to an external session files directory")
        self.root = Path(base) / str(uuid.uuid4())
        self.root.mkdir(mode=0o700, parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir(mode=0o700)
        (self.repo / ".git").mkdir(mode=0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.palace = self.root / "palace"
        self.palace.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.backend = self.root / "procedure.py"
        self.result = self.root / "backend-result.json"
        self.result.write_text(encode(packet()), encoding="utf-8")
        self.calls = self.root / "calls.jsonl"
        # The child double implements only the pinned read/draft seam, never core writes.
        self.backend.write_text(
            "import json, pathlib, sys\n"
            "base = pathlib.Path(__file__).parent\n"
            "args = sys.argv[1:]\n"
            "assert args[0] in ('guidance', 'draft'), args\n"
            "with (base/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')\n"
            "if args[0] == 'guidance':\n"
            "    sys.stdout.write((base/'backend-result.json').read_text())\n"
            "else:\n"
            "    receipt = json.loads(pathlib.Path(args[args.index('--input')+1]).read_text())\n"
            "    draft = {key: receipt[key] for key in "
            "('repository','session_id','task_digest','delivered_rule_ids')}\n"
            "    draft.update(schema_version=1, kind='procedural_draft', "
            "status='pending_original_evidence', original_references=[], "
            "lineage_references=[], missing_requirements=['original evidence'])\n"
            "    out = pathlib.Path(args[args.index('--out')+1])\n"
            "    with out.open('x') as f: f.write(json.dumps(draft)+'\\n')\n"
            "    print(json.dumps(draft))\n", encoding="utf-8")
        self.backend.chmod(0o600)
        core = SCRIPT.parent.parent / "skills" / "dreaming" / "scripts"
        for name in ("dream_metadata", "dream_procedural", "dream_procedural_guidance"):
            shutil.copyfile(core / f"{name}.py", self.backend.parent / f"{name}.py")
        self.config = self.root / "config.json"
        self.settings = {
            "schema_version": 1, "mode": "guidance_drafts", "python": sys.executable,
            "procedure_script": str(self.backend), "state_root": str(self.state),
            "repositories": [{"root": str(self.repo), "repository": "owner/repo",
                              "palace": str(self.palace), "wing": "wing_demo"}],
        }
        self.write_config()
        self.env = {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "XDG_CONFIG_HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.home), "COPILOT_HOME": str(self.home),
            "TMPDIR": str(self.root), "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
        }

    def write_config(self):
        self.config.write_text(encode(self.settings), encoding="utf-8")
        self.config.chmod(0o600)

    def event(self, mode, *, dialect="camel", **overrides):
        payload = {"sessionId": "session-one", "cwd": str(self.repo), "timestamp": 1790443200000}
        if mode == "prompt":
            payload["prompt"] = "Implement evidence adapter"
        if mode in ("before-tool", "after-tool"):
            payload.update(toolName="mempalace-mempalace_search", toolArgs={"query": "adapters"})
        if mode == "after-tool":
            payload["toolResult"] = {
                "resultType": "success",
                "textResultForLlm": encode({
                    "query": "adapters", "filters": {"wing": None, "room": None},
                    "total_before_filter": 0, "results": [],
                }),
            }
        payload.update(overrides)
        if dialect == "snake":
            rename = {"sessionId": "session_id", "toolName": "tool_name",
                      "toolArgs": "tool_input", "toolResult": "tool_result"}
            payload = {rename.get(k, k): v for k, v in payload.items()}
            if "tool_result" in payload:
                value = payload["tool_result"]
                payload["tool_result"] = {"result_type": value["resultType"],
                                          "text_result_for_llm": value["textResultForLlm"]}
            payload["hook_event_name"] = {
                "prompt": "UserPromptSubmit", "before-tool": "PreToolUse",
                "after-tool": "PostToolUse", "start": "SessionStart",
                "stop": "Stop", "compact": "PreCompact",
            }[mode]
        return payload

    def invoke(self, mode, payload=None, *, config=True, timeout=5):
        command = [sys.executable, str(SCRIPT), "--event", mode]
        if config:
            command += ["--config", str(self.config)]
        result = subprocess.run(command, input=encode(payload or self.event(mode)),
                                text=True, capture_output=True, cwd=self.repo,
                                env=self.env, timeout=timeout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 1024)
        if result.stdout:
            parsed = json.loads(result.stdout)
            self.assertNotIn("permissionDecision", result.stdout)
            self.assertNotIn("modifiedResult", parsed)
            self.assertNotIn("decision", parsed)
        return result

    def recorded_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def pointer(self, result):
        self.assertTrue(result.stdout, result.stderr)
        output = json.loads(result.stdout)
        context = output.get("additionalContext") or output["hookSpecificOutput"]["additionalContext"]
        return Path(context.split("packet: ", 1)[1])

    def deliver(self, *, dialect="camel"):
        self.invoke("prompt", self.event("prompt", dialect=dialect))
        self.invoke("before-tool", self.event("before-tool", dialect=dialect))
        return self.invoke("after-tool", self.event("after-tool", dialect=dialect))

    def test_standalone_hook_uses_separately_configured_checkout_for_delivery_and_receipt(self):
        standalone = self.root / "standalone"
        standalone.mkdir()
        hook = standalone / "procedural_context.py"
        shutil.copyfile(SCRIPT, hook)
        checkout = self.root / "configured-checkout"
        checkout.mkdir()
        for path in (self.backend, self.result, *[
                self.root / f"{name}.py" for name in
                ("dream_metadata", "dream_procedural", "dream_procedural_guidance")]):
            path.rename(checkout / path.name)
        self.backend = checkout / self.backend.name
        self.result = checkout / self.result.name
        self.calls = checkout / self.calls.name
        self.settings["procedure_script"] = str(self.backend)
        self.write_config()
        self.assertFalse((hook.parent.parent / "skills").exists())
        with patch(__name__ + ".SCRIPT", hook):
            result = self.deliver()
            self.assertEqual(json.loads(self.pointer(result).read_text()), packet())
            stopped = self.invoke("stop")
        self.assertIn("review-only draft", stopped.stderr)
        self.assertEqual([args[0] for args in self.recorded_calls()], ["guidance", "draft"])

    def test_configured_validator_absence_or_untrusted_path_never_falls_back_to_hook_checkout(self):
        validator = self.backend.parent / "dream_procedural_guidance.py"
        for defect in ("missing", "symlink", "writable"):
            with self.subTest(defect=defect):
                original = validator.read_bytes()
                if defect == "missing":
                    validator.unlink()
                elif defect == "symlink":
                    validator.unlink()
                    validator.symlink_to(SCRIPT.parent.parent / "skills" / "dreaming" / "scripts"
                                         / "dream_procedural_guidance.py")
                else:
                    validator.chmod(0o666)
                result = self.deliver()
                self.assertEqual(result.stdout, "")
                self.assertIn("unavailable", result.stderr)
                if validator.is_symlink():
                    validator.unlink()
                validator.write_bytes(original)
                validator.chmod(0o600)

    def test_foreign_import_cache_cannot_override_configured_validator_or_dependencies(self):
        from types import ModuleType
        module = self.load_adapter()
        cfg = module.load_config(str(self.config), str(self.repo))
        foreign = {}
        for name in ("dream_metadata", "dream_procedural", "dream_procedural_guidance"):
            foreign[name] = ModuleType(name)
            foreign[name].__file__ = str(self.root / "foreign" / f"{name}.py")
        foreign["dream_procedural_guidance"].validate_guidance = lambda *args, **kwargs: packet()
        prior_path = list(sys.path)
        with patch.dict(sys.modules, foreign):
            for mode in ("prompt", "before-tool"):
                module.handle(cfg, module.normalize(mode, self.event(mode)))
            malformed = dict(packet(), item_count=True)
            with self.assertRaisesRegex(module.Unavailable, "count"):
                module.handle(cfg, module.normalize("after-tool", self.event("after-tool")),
                              lambda *args: encode(malformed).encode())
            for name, cached in foreign.items():
                self.assertIs(sys.modules[name], cached)
            self.assertEqual(sys.path, prior_path)

    def test_missing_or_off_config_has_no_state_or_child_side_effects(self):
        self.invoke("prompt", config=False)
        self.settings["mode"] = "off"
        self.write_config()
        self.deliver()
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.recorded_calls(), [])

    def test_pointer_offers_reading_not_acknowledgment_or_current_use_permission(self):
        for dialect in ("camel", "snake"):
            with self.subTest(dialect=dialect):
                result = self.deliver(dialect=dialect)
                output = json.loads(result.stdout)
                text = (output.get("additionalContext")
                        or output["hookSpecificOutput"]["additionalContext"])
                self.assertIn("offered; not read, intended, applied or helpful", text)
                self.assertIn("Read fully; use task-guidance/use-check before advised action", text)
                self.assertEqual(json.loads(self.pointer(result).read_text()), packet())
        self.assertEqual([args[0] for args in self.recorded_calls()],
                         ["guidance", "guidance"])

    def test_invalid_config_is_visible_but_never_denies_before_tool(self):
        self.config.write_text("{bad")
        result = self.invoke("before-tool")
        self.assertIn("unavailable", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.state.iterdir()), [])

    def test_exact_root_required_no_cwd_or_remote_inference(self):
        child = self.repo / "child"
        child.mkdir()
        result = self.invoke("prompt", self.event("prompt", cwd=str(child)))
        self.assertIn("scope", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_core_wing_spellings_are_preserved_in_state_and_both_commands(self):
        for wing in ("copilot-mempalace", "Team_ALPHA-2", "wing_demo"):
            with self.subTest(wing=wing):
                self.settings["repositories"][0]["wing"] = wing
                self.write_config()
                result = self.deliver()
                state_path = self.pointer(result).parent / "state.json"
                self.assertEqual(json.loads(state_path.read_text())["identity"]["wing"], wing)
                self.assertEqual(self.recorded_calls()[-1][4], wing)
                self.invoke("stop")
                self.assertEqual(self.recorded_calls()[-1][0], "draft")
                self.assertEqual(self.recorded_calls()[-1][4], wing)

    def test_core_repository_names_are_canonical_in_state_commands_and_receipts(self):
        for configured, expected in (
            ("Owner/Repo", "owner/repo"),
            ("  Owner/Repo  ", "owner/repo"),
            ("owner/.github", "owner/.github"),
            ("Kowner/Repo", "kowner/repo"),
        ):
            with self.subTest(repository=configured):
                self.settings["repositories"][0]["repository"] = configured
                self.write_config()
                value = packet()
                value["repository"] = expected
                self.result.write_text(encode(value))
                result = self.deliver()
                state_path = self.pointer(result).parent / "state.json"
                state = json.loads(state_path.read_text())
                self.assertEqual(state["identity"]["repository"], expected)
                self.assertEqual(state["receipt"]["repository"], expected)
                self.assertEqual(self.recorded_calls()[-1][6], expected)
                self.invoke("stop")
                draft_args = self.recorded_calls()[-1]
                self.assertEqual(draft_args[0], "draft")
                self.assertEqual(draft_args[6], expected)
                receipt = json.loads(Path(draft_args[draft_args.index("--input") + 1]).read_text())
                self.assertEqual(receipt["repository"], expected)

    def test_repository_case_only_config_change_keeps_same_pairing_identity(self):
        self.settings["repositories"][0]["repository"] = "Owner/Repo"
        self.write_config()
        self.invoke("prompt")
        self.invoke("before-tool")
        self.settings["repositories"][0]["repository"] = "owner/repo"
        self.write_config()
        result = self.invoke("after-tool")
        self.assertEqual(json.loads(self.pointer(result).read_text())["repository"], "owner/repo")
        self.assertEqual(len(list(self.state.iterdir())), 1)

    def test_canonical_repository_still_rejects_different_backend_scope(self):
        self.settings["repositories"][0]["repository"] = "Owner/Repo"
        self.write_config()
        value = packet()
        value["repository"] = "other/repo"
        self.result.write_text(encode(value))
        self.assertIn("repository scope mismatch", self.deliver().stderr)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])

    def test_invalid_core_wing_values_never_create_state_or_invoke_backend(self):
        for wing in ("", " ", "wing/name", "../escape", "wing.name", "two words", "répo", None, 12):
            with self.subTest(wing=wing):
                self.settings["repositories"][0]["wing"] = wing
                self.write_config()
                result = self.invoke("before-tool")
                self.assertIn("unavailable", result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.recorded_calls(), [])

    def test_invalid_core_repository_values_never_create_state_or_invoke_backend(self):
        for repository in ("", " ", "owner", "/repo", "owner/", "owner//repo", "owner/repo/extra",
                           "two words/repo", "owner/repo?", "owner/répo", None, 12):
            with self.subTest(repository=repository):
                self.settings["repositories"][0]["repository"] = repository
                self.write_config()
                result = self.invoke("before-tool")
                self.assertIn("unavailable", result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.recorded_calls(), [])

    def test_successful_search_delivers_complete_private_packet_once(self):
        result = self.deliver()
        path = self.pointer(result)
        self.assertTrue(path.is_relative_to(self.state))
        self.assertEqual(path.read_bytes(), self.result.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Keep original evidence", result.stdout)
        self.assertEqual(self.recorded_calls(), [[
            "guidance", "--palace", str(self.palace), "--wing", "wing_demo",
            "--repository", "owner/repo", "--task", "Implement evidence adapter",
            "--max-items", "5", "--max-chars", "6000", "--max-bytes", "8192",
        ]])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_documented_snake_dialect_preserves_wrapper(self):
        result = self.deliver(dialect="snake")
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(json.loads(self.pointer(result).read_text()), packet())

    def test_transport_success_does_not_make_semantic_errors_successful(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        payload = self.event("after-tool")
        payload["toolResult"]["textResultForLlm"] = encode({"error": "source unavailable", "results": []})
        result = self.invoke("after-tool", payload)
        self.assertIn("unavailable", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_old_generation_completion_cannot_borrow_new_prompt(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        self.invoke("prompt", self.event("prompt", prompt="New task"))
        self.invoke("after-tool")
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(self.recorded_calls()[0][8], "New task")

    def test_identical_overlaps_stay_ambiguous_across_prompt_until_drained(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        self.invoke("prompt")
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.invoke("after-tool")
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_dispatch_latches_abstention_even_after_next_prompt_and_resume(self):
        for name in ("task", "Agent", "run_factory"):
            with self.subTest(name=name):
                sid = "dispatch-" + name
                self.invoke("prompt", self.event("prompt", sessionId=sid))
                self.invoke("before-tool", self.event("before-tool", sessionId=sid, toolName=name))
                self.invoke("start", self.event("start", sessionId=sid, source="resume"))
                self.invoke("prompt", self.event("prompt", sessionId=sid))
                self.invoke("before-tool", self.event("before-tool", sessionId=sid))
                result = self.invoke("after-tool", self.event("after-tool", sessionId=sid))
                self.assertIn("isolation", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_start_and_compact_invalidate_task_and_old_completions(self):
        for mode in ("start", "compact"):
            with self.subTest(mode=mode):
                self.invoke("prompt")
                self.invoke("before-tool")
                self.invoke(mode)
                self.invoke("after-tool")
        self.assertEqual(self.recorded_calls(), [])

    def test_rejects_symlink_config_and_public_state(self):
        original = self.root / "actual.json"
        self.config.rename(original)
        self.config.symlink_to(original)
        self.assertIn("unavailable", self.invoke("before-tool").stderr)
        self.config.unlink()
        original.rename(self.config)
        self.state.chmod(0o755)
        self.assertIn("private", self.invoke("prompt").stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_session_id_is_hashed_not_used_as_a_path(self):
        payload = self.event("prompt", sessionId="../../escape")
        self.invoke("prompt", payload)
        self.assertEqual(len(list(self.state.iterdir())), 1)
        self.assertFalse((self.root / "escape").exists())
        self.assertTrue(all(len(p.name) == 64 for p in self.state.iterdir()))

    def test_invalid_scope_trials_shape_and_unicode_budget_are_withheld_whole(self):
        variants = []
        wrong_scope = packet()
        wrong_scope["repository"] = "other/repo"
        variants.append(encode(wrong_scope))
        trial = packet()
        trial["trials"] = trial["rules"]
        variants.append(encode(trial))
        wrong_count = packet()
        wrong_count["item_count"] = 0
        variants.append(encode(wrong_count))
        huge_bytes = packet()
        huge_bytes["rules"][0]["statement"] = "🦄" * 2000
        self.assertLess(len(encode(huge_bytes)), 6000)
        self.assertGreater(len(encode(huge_bytes).encode("utf-8")), 8192)
        variants.append(encode(huge_bytes))
        huge_chars = packet()
        huge_chars["rules"][0]["statement"] = "a" * 6000
        variants.append(encode(huge_chars))
        variants.extend(["not json\n", encode({"status": "evidence_unavailable"})])
        for raw in variants:
            with self.subTest(raw=raw[:35]):
                self.result.write_text(raw, encoding="utf-8")
                result = self.deliver()
                self.assertIn("unavailable", result.stderr)
                self.assertNotIn("packet:", result.stdout)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])

    def test_stop_uses_genuine_receipt_and_only_draft_command(self):
        self.invoke("stop")
        self.assertEqual(self.recorded_calls(), [])
        self.deliver()
        result = self.invoke("stop")
        calls = self.recorded_calls()
        self.assertEqual([c[0] for c in calls], ["guidance", "draft"])
        draft_args = calls[1]
        self.assertEqual(draft_args[:7], [
            "draft", "--palace", str(self.palace), "--wing", "wing_demo", "--repository", "owner/repo",
        ])
        self.assertEqual(draft_args[7], "--input")
        receipt = json.loads(Path(draft_args[8]).read_text())
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["session_id"], "session-one")
        self.assertEqual(receipt["task_digest"], hashlib.sha256(b"Implement evidence adapter").hexdigest())
        self.assertEqual(receipt["delivered_rule_ids"], ["proc:" + "a" * 64])
        self.assertEqual(result.stdout, "")
        self.assertIn("draft", result.stderr)
        self.invoke("stop")
        self.assertEqual(len(self.recorded_calls()), 2)

    def load_adapter(self):
        spec = importlib.util.spec_from_file_location("procedural_context", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_injected_runner_has_exact_read_only_argv_and_no_pretool_backend(self):
        module = self.load_adapter()
        cfg = module.load_config(str(self.config), str(self.repo))
        calls = []

        def runner(argv, cwd, limit):
            calls.append((argv, cwd, limit))
            return self.result.read_bytes()

        output = io.BytesIO()
        wrapped = io.TextIOWrapper(output, encoding="utf-8")
        with patch.object(sys, "stdout", wrapped):
            module.handle(cfg, module.normalize("prompt", self.event("prompt")), runner)
            module.handle(cfg, module.normalize("before-tool", self.event("before-tool")), runner)
            self.assertEqual(calls, [])
            module.handle(cfg, module.normalize("after-tool", self.event("after-tool")), runner)
            wrapped.flush()
            raw = output.getvalue()
        self.assertIn(b"packet:", raw)
        self.assertEqual(calls, [([
            sys.executable, str(self.backend), "guidance", "--palace", str(self.palace),
            "--wing", "wing_demo", "--repository", "owner/repo",
            "--task", "Implement evidence adapter", "--max-items", "5",
            "--max-chars", "6000", "--max-bytes", "8192",
        ], self.repo, 8192)])

    def test_duplicate_and_unmatched_completions_never_start_guidance(self):
        self.invoke("prompt")
        self.invoke("after-tool")
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.invoke("after-tool")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_exact_native_tool_names_only_not_substring_lookalikes(self):
        self.invoke("prompt")
        for name in ("evil_mempalace_search", "bash", "capture-sources"):
            self.invoke("before-tool", self.event("before-tool", toolName=name))
            self.invoke("after-tool", self.event("after-tool", toolName=name))
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("before-tool", self.event("before-tool", toolName="mempalace_search"))
        self.invoke("after-tool", self.event("after-tool", toolName="mempalace_search"))
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_mixed_dialects_and_missing_identity_abstain_visibly(self):
        payloads = [
            self.event("prompt", session_id="different"),
            self.event("prompt", sessionId=""),
            self.event("prompt", tool_name="mempalace_search"),
        ]
        for payload in payloads:
            self.assertIn("unavailable", self.invoke("prompt", payload).stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_shared_identity_and_parallel_dispatch_never_create_parent_receipts(self):
        self.invoke("prompt")
        self.invoke("before-tool", self.event("before-tool", toolName="multi_tool_use.parallel", toolArgs={
            "tool_uses": [{"recipient_name": "functions.task", "parameters": {"prompt": "child"}}],
        }))
        self.invoke("prompt")
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.invoke("stop")
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("prompt", self.event("prompt", sessionId="child", agentId="child-id"))
        self.invoke("before-tool", self.event("before-tool", sessionId="child"))
        self.assertIn("isolation", self.invoke("after-tool", self.event("after-tool", sessionId="child")).stderr)

    def test_new_prompt_does_not_draft_previous_receipt_but_compact_can_draft_current(self):
        self.deliver()
        self.invoke("prompt", self.event("prompt", prompt="Other task"))
        self.invoke("stop")
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance"])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.invoke("compact")
        self.invoke("stop")
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance", "guidance", "draft"])

    def test_draft_passes_only_configured_original_store_not_host_transcript(self):
        source = self.root / "original.db"
        source.touch(mode=0o600)
        self.settings["repositories"][0]["session_store"] = str(source)
        self.write_config()
        self.deliver()
        self.invoke("stop", self.event("stop", transcriptPath="/not-trusted/history.jsonl"))
        args = self.recorded_calls()[1]
        self.assertEqual(args[7:9], ["--session-store", str(source)])
        self.assertNotIn("/not-trusted/history.jsonl", args)

    def test_session_store_disappearance_preserves_pairing_guidance_and_draft_path(self):
        host = self.root / "host"
        host.mkdir(mode=0o700)
        source = host / "sessions.db"
        source.touch(mode=0o600)
        self.settings["repositories"][0]["session_store"] = str(source)
        self.write_config()
        directory = self.pointer(self.deliver()).parent
        self.invoke("stop")
        self.invoke("prompt")
        self.invoke("before-tool")
        source.unlink()
        self.assertEqual(self.pointer(self.invoke("after-tool")).parent, directory)
        self.invoke("stop")
        self.invoke("prompt")
        self.invoke("before-tool")
        host.rmdir()
        self.assertEqual(self.pointer(self.invoke("after-tool")).parent, directory)
        self.invoke("compact")
        self.invoke("start")
        host.mkdir(mode=0o700)
        source.touch(mode=0o600)
        self.assertEqual(self.pointer(self.deliver()).parent, directory)
        self.invoke("stop")
        state = json.loads((directory / "state.json").read_text())
        self.assertEqual(state["identity"]["session_store"], str(source))
        self.assertEqual(list(self.state.iterdir()), [directory])
        calls = self.recorded_calls()
        self.assertEqual([c[0] for c in calls], ["guidance", "draft"] * 4)
        for args in calls[1::2]:
            self.assertEqual(args[7:9], ["--session-store", str(source)])

    def test_session_store_lexical_errors_rejected_without_state_or_child(self):
        for source in ("relative.db", str(self.root / "a") + "/../sessions.db",
                       str(self.root) + "/./sessions.db", str(self.root) + "//sessions.db",
                       str(self.root / "sessions.db") + "/", str(self.root) + "/bad\0.db"):
            with self.subTest(source=source):
                self.settings["repositories"][0]["session_store"] = source
                self.write_config()
                self.assertIn("unavailable", self.invoke("prompt").stderr)
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.recorded_calls(), [])

    def test_session_store_draft_access_still_rejects_unsafe_existing_paths(self):
        host = self.root / "host"
        host.mkdir(mode=0o700)
        source = host / "sessions.db"
        source.touch(mode=0o600)
        self.settings["repositories"][0]["session_store"] = str(source)
        self.write_config()
        for damage in ("public", "writable", "symlink", "dangling", "hardlink",
                       "directory", "fifo", "ancestor_symlink", "ancestor_writable"):
            with self.subTest(damage=damage):
                self.deliver()
                if damage in ("public", "writable"):
                    source.chmod(0o644 if damage == "public" else 0o622)
                elif damage == "hardlink":
                    os.link(source, self.root / "alias")
                elif damage == "directory":
                    source.unlink()
                    source.mkdir(mode=0o700)
                elif damage == "fifo":
                    source.unlink()
                    os.mkfifo(source, 0o600)
                elif damage in ("symlink", "dangling"):
                    source.unlink()
                    source.symlink_to(self.result if damage == "symlink" else self.root / "missing")
                elif damage == "ancestor_symlink":
                    host.rename(self.root / "moved-host")
                    host.symlink_to(self.root / "moved-host", target_is_directory=True)
                else:
                    host.chmod(0o722)
                before = len(self.recorded_calls())
                result = self.invoke("stop")
                self.assertIn("unavailable", result.stderr)
                self.assertEqual(len(self.recorded_calls()), before)
                self.assertEqual(list(self.state.rglob("draft-*.json")), [])
                if damage == "ancestor_symlink":
                    host.unlink()
                    (self.root / "moved-host").rename(host)
                elif damage == "hardlink":
                    (self.root / "alias").unlink()
                elif damage == "directory":
                    source.rmdir()
                    source.touch(mode=0o600)
                elif damage in ("symlink", "dangling", "fifo"):
                    source.unlink()
                    source.touch(mode=0o600)
                source.chmod(0o600)
                host.chmod(0o700)

    def test_private_packet_state_lock_symlinks_and_world_readable_config_are_rejected(self):
        self.config.chmod(0o644)
        self.assertIn("private", self.invoke("before-tool").stderr)
        self.config.chmod(0o600)
        self.invoke("prompt")
        directory = next(self.state.iterdir())
        victim = self.root / "victim"
        victim.write_text("untouched")
        for name in ("state.json", "lock"):
            target = directory / name
            saved = target.with_suffix(".original")
            target.rename(saved)
            target.symlink_to(victim)
            self.assertIn("unavailable", self.invoke("before-tool").stderr)
            self.assertEqual(victim.read_text(), "untouched")
            target.unlink()
            saved.rename(target)

    def test_before_tool_lock_contention_is_bounded_and_never_denies(self):
        self.invoke("prompt")
        directory = next(self.state.iterdir())
        with (directory / "lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            result = self.invoke("before-tool")
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertIn("lock busy", result.stderr)
        self.assertEqual(self.recorded_calls(), [])

    def test_lost_pretool_stamp_poisoning_is_persistent_not_assigned_to_old_call(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        directory = next(self.state.iterdir())
        with (directory / "lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn("lock busy", self.invoke("before-tool").stderr)
        self.invoke("after-tool")
        self.invoke("prompt")
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(self.recorded_calls(), [])

    def test_actual_hook_working_directory_must_match_declared_scope(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), "--event", "prompt"],
            input=encode(self.event("prompt")), text=True, capture_output=True,
            cwd=self.root, env=self.env, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scope", result.stderr)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_corrupt_receipt_cannot_trigger_a_draft(self):
        self.deliver()
        state_path = next(self.state.iterdir()) / "state.json"
        state = json.loads(state_path.read_text())
        state["receipt"]["task_digest"] = "0" * 64
        state_path.write_text(encode(state))
        result = self.invoke("stop")
        self.assertIn("unavailable", result.stderr)
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance"])

    def test_empty_guidance_is_a_complete_healthy_packet_not_error_fallback(self):
        data = packet()
        data.update(status="no_eligible_rules", item_count=0, rules=[])
        self.result.write_text(encode(data))
        result = self.deliver()
        self.assertEqual(json.loads(self.pointer(result).read_text()), data)
        self.assertIn("no_eligible_rules", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_transport_failure_drains_old_start_without_publishing(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        event = self.event("after-tool", error="native transport failed")
        event.pop("toolResult")
        self.invoke("after-tool", event)
        self.assertEqual(self.recorded_calls(), [])
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_failed_guidance_is_not_retried_in_same_generation(self):
        self.result.write_text('{"status":"evidence_unavailable"}\n')
        self.deliver()
        self.result.write_text(encode(packet()))
        self.invoke("before-tool")
        self.invoke("after-tool")
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])
        self.deliver()
        self.assertEqual(len(self.recorded_calls()), 2)

    def block_backend(self):
        original = self.backend.read_text()
        self.backend.write_text(
            "import pathlib, time\n"
            "base = pathlib.Path(__file__).parent\n"
            "(base/'ready').touch()\n"
            "deadline = time.monotonic() + 6\n"
            "while not (base/'release').exists():\n"
            "    if time.monotonic() > deadline: raise RuntimeError('test barrier expired')\n"
            "    time.sleep(.01)\n" + original)

    def start_after(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--config", str(self.config), "--event", "after-tool"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.repo, env=self.env)
        proc.stdin.write(encode(self.event("after-tool")))
        proc.stdin.close()
        proc.stdin = None
        self.addCleanup(self.finish_child, proc)
        return proc

    def finish_child(self, proc):
        (self.root / "release").touch()
        if proc.poll() is None:
            proc.communicate(timeout=8)

    def wait_ready(self):
        deadline = time.monotonic() + 3
        while not (self.root / "ready").exists():
            self.assertLess(time.monotonic(), deadline, "backend did not start")
            time.sleep(.01)

    def test_child_process_generation_cas_withholds_superseded_packet_without_holding_lock(self):
        self.block_backend()
        self.invoke("prompt")
        self.invoke("before-tool")
        process = self.start_after()
        self.wait_ready()
        result = self.invoke("prompt", self.event("prompt", prompt="Changed during guidance"))
        self.assertEqual(result.stderr, "")
        self.assertIsNone(process.poll(), "guidance must still be waiting when the prompt commits")
        (self.root / "release").touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("generation changed", stderr)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])
        self.invoke("stop")
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance"])

    def test_child_process_dispatch_cas_prevents_parent_receipt_reuse(self):
        self.block_backend()
        self.invoke("prompt")
        self.invoke("before-tool")
        process = self.start_after()
        self.wait_ready()
        self.invoke("before-tool", self.event("before-tool", toolName="run_factory"))
        (self.root / "release").touch()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(stdout, "")
        self.assertIn("generation changed", stderr)
        self.invoke("stop")
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance"])

    def test_two_actual_hook_processes_cannot_publish_one_completion_twice(self):
        self.block_backend()
        self.invoke("prompt")
        self.invoke("before-tool")
        first = self.start_after()
        self.wait_ready()
        second = self.start_after()
        second_stdout, second_stderr = second.communicate(timeout=3)
        self.assertEqual(second.returncode, 0, second_stderr)
        self.assertEqual(second_stdout, "")
        (self.root / "release").touch()
        first_stdout, first_stderr = first.communicate(timeout=3)
        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertIn("packet:", first_stdout)
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_child_nonzero_stderr_and_invalid_utf8_fail_visibly_without_packet(self):
        cases = [
            "import sys; sys.exit(7)",
            "import sys; sys.stderr.write('x'*20000)",
            "import sys; sys.stdout.buffer.write(b'\\xff\\n')",
        ]
        for code in cases:
            self.backend.write_text(code + "\n")
            result = self.deliver()
            self.assertIn("unavailable", result.stderr)
            self.assertNotIn("packet:", result.stdout)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])

    def test_timeout_kills_only_invocation_tree_including_term_ignoring_descendant(self):
        module = self.load_adapter()
        leaf = self.root / "leaf.py"
        leaf.write_text(
            "import os, pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "(pathlib.Path(__file__).parent/'leaf.pid').write_text(str(os.getpid()))\n"
            "time.sleep(60)\n")
        parent = self.root / "parent.py"
        parent.write_text(
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name('leaf.py'))])\n"
            "time.sleep(60)\n")
        sentinel = self.root / "sentinel.py"
        sentinel.write_text("import time\ntime.sleep(60)\n")
        other = subprocess.Popen([sys.executable, str(sentinel)], cwd=self.root, env=self.env)
        try:
            with patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(module.Unavailable, "timeout"):
                    module.run_child([sys.executable, str(parent)], self.root, 8192, timeout=5)
            self.assertIsNone(other.poll(), "timeout killed an unrelated process")
            pid = int((self.root / "leaf.pid").read_text())
            status = Path(f"/proc/{pid}/stat")
            if status.exists():
                self.assertEqual(status.read_text().split(") ", 1)[1][0], "Z")
        finally:
            other.terminate()
            other.wait(timeout=3)

    def test_capacity_overflow_latches_abstention_not_unpaired_new_search(self):
        self.invoke("prompt")
        directory = next(self.state.iterdir())
        state_path = directory / "state.json"
        state = json.loads(state_path.read_text())
        # Seed a valid full ledger, then exercise the real pre/post transition.
        module = self.load_adapter()
        key = module.normalize("before-tool", self.event("before-tool")).key
        state["starts"] = {key: {"count": 1024, "generation": state["generation"], "ambiguous": True}}
        state_path.write_text(encode(state))
        result = self.invoke("before-tool")
        self.assertIn("unavailable", result.stderr)
        self.invoke("prompt")
        state = json.loads(state_path.read_text())
        self.assertTrue(state["disabled"])

    def test_invalid_packet_cannot_smuggle_error_status_with_healthy_rules(self):
        value = packet()
        value["error"] = "evidence unavailable"
        self.result.write_text(encode(value))
        self.assertIn("unavailable", self.deliver().stderr)
        self.assertEqual(list(self.state.rglob("guidance-*.json")), [])

    def test_failed_bookkeeping_commit_never_publishes_packet(self):
        self.invoke("prompt")
        self.invoke("before-tool")
        module = self.load_adapter()
        cfg = module.load_config(str(self.config), str(self.repo))
        original = module.State.write
        commits = 0

        def failing_write(store, name, raw):
            nonlocal commits
            if name == "state.json":
                commits += 1
                if commits == 2:
                    raise OSError("injected final commit failure")
            return original(store, name, raw)

        output = io.BytesIO()
        wrapped = io.TextIOWrapper(output, encoding="utf-8")
        with patch.object(module.State, "write", failing_write), patch.object(sys, "stdout", wrapped):
            with self.assertRaises(OSError):
                module.handle(cfg, module.normalize("after-tool", self.event("after-tool")),
                              lambda argv, cwd, limit: self.result.read_bytes())
            wrapped.flush()
            self.assertEqual(output.getvalue(), b"")

    def test_partial_or_invalid_child_draft_is_not_a_completed_review_artifact(self):
        self.deliver()
        self.backend.write_text(
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "pathlib.Path(args[args.index('--out')+1]).write_text('{broken')\n"
            "sys.exit(7)\n")
        self.assertIn("unavailable", self.invoke("stop").stderr)
        self.assertEqual(list(self.state.rglob("draft-*.json")), [])
        self.assertEqual(list(self.state.rglob(".draft-stage-*")), [])

    def test_configuration_example_is_disabled_without_touching_placeholder_paths(self):
        example = SCRIPT.with_name("procedural-context.config.example.json")
        self.config.write_text(example.read_text())
        self.invoke("prompt")
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(self.recorded_calls(), [])

    def test_hook_example_exec_argv_drives_full_synthetic_lifecycle(self):
        example = json.loads(SCRIPT.with_name("procedural-context.json.example").read_text())
        self.assertEqual(example["version"], 1)
        modes = {
            "sessionStart": "start", "userPromptSubmitted": "prompt", "preToolUse": "before-tool",
            "postToolUse": "after-tool", "postToolUseFailure": "after-tool",
            "preCompact": "compact", "agentStop": "stop",
        }
        for host_name in ("sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse", "agentStop"):
            entries = example["hooks"][host_name]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            command = [entry["exec"], *entry["args"]]
            replacements = {
                "/absolute/path/to/python": sys.executable,
                "/absolute/path/to/hooks/procedural_context.py": str(SCRIPT),
                "/absolute/private/adapter-config.json": str(self.config),
            }
            command = [replacements.get(arg, arg) for arg in command]
            response = subprocess.run(command, input=encode(self.event(modes[host_name])),
                                      text=True, capture_output=True, cwd=self.repo,
                                      env=self.env, timeout=5)
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertNotIn("permissionDecision", response.stdout)
            if host_name == "postToolUse":
                self.assertIn("packet:", response.stdout)
        self.assertEqual([c[0] for c in self.recorded_calls()], ["guidance", "draft"])
        self.assertEqual(set(example["hooks"]), set(modes))
        self.assertLessEqual(example["hooks"]["preToolUse"][0]["timeoutSec"], 3)
        self.assertLessEqual(example["hooks"]["postToolUse"][0]["timeoutSec"], 30)


class CoreCliTests(unittest.TestCase):
    def check_store_lifetime(self, *, captured):
        from datetime import datetime, timedelta, timezone
        import sqlite3
        fixture_path = SCRIPT.parent.parent / "skills/dreaming/scripts"
        fixture_imports = patch.object(sys, "path", [str(fixture_path), *sys.path])
        fixture_imports.start()
        self.addCleanup(fixture_imports.stop)
        # Contain the installed runtime's first-import PYTHONPATH cleanup.
        with patch.object(sys, "path", list(sys.path)):
            import mempalace
        import dream_palace
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        from dream_metadata import content_hash
        from dream_procedural import event_to_data, parse_event
        from dream_procedural_palace import append_event
        from test_dream_procedural import event_data, sha, stamp
        from test_dream_procedural_palace import installed_palace
        from test_dream_procedural_validate import GroundedFixture

        adapter = AdapterTests()
        adapter.setUp()
        self.addCleanup(adapter.doCleanups)
        fixture = GroundedFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.storage_patch.stop()
        fixture.path = str(adapter.palace)
        host = adapter.root / "host"
        host.mkdir(mode=0o700)
        source = host / "sessions.db"
        Path(fixture.store).rename(source)
        source.chmod(0o600)
        fixture.store = str(source)
        task = fixture.proposal().payload.definition.statement
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        pending_session = str(uuid.UUID(int=105))
        response = "The regression was isolated by following the rule."
        with sqlite3.connect(source) as con:
            con.execute("UPDATE turns SET timestamp=?", (stamp(now),))
            con.execute("UPDATE turns SET user_message=? WHERE session_id=?",
                        (task, fixture.refs[0]["session_id"]))
            fixture.refs[0].update(quote=task, source_hash=content_hash(task))
            con.execute("INSERT INTO sessions VALUES (?,?)", (pending_session, "owner/repo"))
            con.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                        (pending_session, 0, task, response, stamp(now)))
        for ref in fixture.refs:
            ref.update(source_kind="session_turn", source_id=ref["session_id"],
                       turn_index=0, field="user_message")
        evidence = fixture.refs[:3]
        if captured:
            evidence = [*evidence, dict(fixture.refs[0], field="assistant_response",
                                       quote=response, source_hash=content_hash(response))]
        review = event_to_data(fixture.review())["payload"]
        review["validation_packet"]["validated_at"] = stamp(now)
        review["validation_digest"] = sha(review["validation_packet"])
        events = [parse_event(event_data(at=now, origin_drawer_ids=[], evidence=evidence)),
                  parse_event(event_data("review", 2, at=now, **review))]
        events += [parse_event(event_data("outcome", 10 + i, at=now,
            source_session_id=ref["session_id"], evidence=[ref])) for i, ref in enumerate(fixture.refs)]
        cache = os.environ["DREAMING_TEST_MODEL_CACHE"]
        with patch.object(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", cache), installed_palace(fixture.path):
            writer = dream_palace.MempalaceWriter()
            with writer.mutation():
                writer.add_drawer("w", "diary", "Disposable palace initialization.")
                for event in events:
                    append_event(fixture.path, "w", event, writer=writer,
                                 session_store=fixture.store, clock=lambda: now)
        before = {p.name: p.read_bytes() for p in adapter.palace.iterdir()
                  if p.is_file() and not p.name.endswith("-shm")}
        saved = adapter.root / "saved-sessions.db"
        shutil.copy2(source, saved)
        child_cache = adapter.home / ".cache/chroma/onnx_models/all-MiniLM-L6-v2"
        child_cache.parent.mkdir(parents=True)
        child_cache.symlink_to(cache, target_is_directory=True)
        adapter.settings["procedure_script"] = str(
            SCRIPT.parent.parent / "skills/dreaming/scripts/dream_procedure.py")
        adapter.settings["repositories"][0].update(wing="w", session_store=str(source))
        adapter.write_config()
        adapter.env.update(
            MEMPALACE_BACKEND="sqlite_exact", MEMPALACE_BACKEND_EXPLICIT="sqlite_exact",
            MEMPALACE_PALACE_PATH=str(adapter.palace), MEMPALACE_MCP_READ_ONLY="1",
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", ANONYMIZED_TELEMETRY="False")
        session = fixture.refs[0]["session_id"] if captured else pending_session

        def invoke(mode):
            return adapter.invoke(mode, adapter.event(mode, sessionId=session, prompt=task), timeout=35)

        directory = None
        for phase in ("existing", "missing_file", "missing_directory", "reappeared"):
            with self.subTest(captured=captured, phase=phase):
                if phase == "reappeared":
                    host.mkdir(mode=0o700)
                    shutil.copy2(saved, source)
                invoke("prompt")
                invoke("before-tool")
                if phase == "missing_file":
                    source.unlink()
                elif phase == "missing_directory":
                    host.rmdir()
                path = adapter.pointer(invoke("after-tool"))
                guidance = json.loads(path.read_text())
                self.assertEqual(guidance["item_count"], 1)
                self.assertEqual(guidance["rules"][0]["maturity"], "established")
                directory = directory or path.parent
                self.assertEqual(path.parent, directory)
                state = json.loads((directory / "state.json").read_text())
                self.assertEqual(state["identity"]["session_store"], str(source))
                result = invoke("stop")
                self.assertIn("review-only draft:", result.stderr)
                draft_path = Path(result.stderr.strip().split("review-only draft: ", 1)[1])
                draft = json.loads(draft_path.read_text())
                missing = phase.startswith("missing") and not captured
                self.assertEqual(draft["status"],
                                 "pending_original_evidence" if missing else "requires_review")
                self.assertEqual(len(draft["original_references"]), 0 if missing else 2)
                self.assertEqual(draft["independent_sessions"], 0 if missing else 1)
                if missing:
                    self.assertIn("session_store_unavailable", draft["missing_requirements"])
                    self.assertNotIn("session_store_not_supplied", draft["missing_requirements"])
                else:
                    self.assertEqual({ref["field"] for ref in draft["original_references"]},
                                     {"user_message", "assistant_response"})
                if phase.startswith("missing"):
                    self.assertFalse(source.exists())
        self.assertEqual(len(list(adapter.state.iterdir())), 1)
        self.assertEqual({p.name: p.read_bytes() for p in adapter.palace.iterdir()
                          if p.is_file() and not p.name.endswith("-shm")}, before)
        self.assertEqual(source.read_bytes(), saved.read_bytes())
        self.assertFalse(adapter.calls.exists(), "the child double must not serve the core integration")

    def test_core_cli_uses_captured_fallback_after_store_and_directory_loss(self):
        self.check_store_lifetime(captured=True)

    def test_core_cli_keeps_missing_store_pending_without_captured_task(self):
        self.check_store_lifetime(captured=False)


if __name__ == "__main__":
    unittest.main()
