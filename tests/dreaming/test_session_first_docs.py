"""Guidance contracts and executable schema examples, not live-agent efficacy."""

import json
import re
import shlex
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DREAM = "skills/dreaming/SKILL.md"
PIPELINE = "skills/dreaming/references/pipeline.md"
RECALL_DOCS = ("skills/mempalace/SKILL.md", "copilot-instructions.md")


def document(path):
    return (ROOT / path).read_text(encoding="utf-8")


def section(path, title):
    text = document(path)
    heading = re.search(rf"^(#+) {re.escape(title)}\s*$", text, re.MULTILINE)
    assert heading, f"{path} is missing the {title!r} contract"
    remainder = text[heading.end():]
    headings_only = re.sub(
        r"```.*?```", lambda match: " " * len(match.group()), remainder,
        flags=re.DOTALL,
    )
    next_heading = re.search(
        rf"^#{{1,{len(heading.group(1))}}} ", headings_only, re.MULTILINE,
    )
    return remainder[:next_heading.start()] if next_heading else remainder


def shell_commands(path, script):
    commands = []
    text = re.sub(r"^> ?", "", document(path), flags=re.MULTILINE)
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if script in line and not line.lstrip().startswith("#"):
                commands.append(shlex.split(line, comments=True))
    return commands


@pytest.mark.parametrize("path", (DREAM, PIPELINE, "README.md"))
def test_first_survey_example_covers_exact_repository_and_memory_wing(path):
    commands = shell_commands(path, "dream_survey.py")
    assert commands, f"{path} needs a runnable default survey example"
    first = commands[0]
    assert first[:2] == ["$MPY", "$DREAM_SCRIPTS/dream_survey.py"]
    assert first[first.index("--repository") + 1] == "owner/repository"
    assert first[first.index("--wings") + 1] == "<project-wing>"
    assert first[first.index("--worklists-dir") + 1] == "<session-files>/dream-worklists"
    assert "--tasks" not in first, "the first example must exercise the safe default"
    assert not {
        "--source", "--since", "--limit-sessions", "--max-candidates", "--tau",
    }.intersection(first), "the completed window must not be a partial preview"


@pytest.mark.parametrize("path", (DREAM, PIPELINE, "README.md"))
def test_old_sweep_keeps_legacy_sources_and_diary_reflection_is_separate(path):
    commands = shell_commands(path, "dream_survey.py")
    migration = [
        args for args in commands
        if "--tasks" in args and args[args.index("--tasks") + 1]
        == "contradiction,induce-rules,pattern,reflect,merge,prune"
    ]
    assert migration, f"{path} must retain an explicit old-sweep migration"
    assert all("--source" not in args for args in migration), (
        "mixed maintenance must not force a source onto non-reflection tasks"
    )
    assert any(
        "--tasks" in args and args[args.index("--tasks") + 1] == "reflect"
        and "--source" in args and args[args.index("--source") + 1] == "diary"
        for args in commands
    ), "show diary-only reflection as a separate explicit request"


def test_harvest_examples_do_not_accidentally_reinterpret_legacy_merge():
    for args in shell_commands(DREAM, "dream_harvest.py"):
        assert "--task" in args or "--repository" in args
        if "--task" not in args:
            assert args[args.index("--wing") + 1] == "<project-wing>"
    phases = section(DREAM, "The 5-phase pipeline")
    assert "--task merge --wing" in phases


@pytest.mark.parametrize("path,title", (
    (DREAM, "The 5-phase pipeline"),
    (PIPELINE, "Default: session review, then relevant recall"),
))
def test_default_window_is_complete_instead_of_a_recent_count_sample(path, title):
    text = " ".join(section(path, title).split()).lower()
    for required in (
        "all eligible history", "[lower, upper)", "run start", "no input-count",
        "continuing sessions", "original memories", "all project rooms",
    ):
        assert required in text


@pytest.mark.parametrize("path,title", (
    (DREAM, "The 5-phase pipeline"),
    (PIPELINE, "Default: session review, then relevant recall"),
))
def test_successful_abstention_retains_auditable_worklist(path, title):
    contract = " ".join(section(path, title).split()).lower()
    assert "reflect.incremental.json" in contract
    assert "empty window" in contract
    assert "abstention" in contract


def test_lesson_proposals_have_actionable_prose_without_a_new_decision_schema():
    contract = section(DREAM, "Session lesson review")
    template = re.search(r"```text\n(.*?)```", contract, re.DOTALL)
    assert template, "lesson shape belongs inside existing decision text"
    for label in (
        "Situation / trigger:", "Action / avoidance:", "Scope / exceptions:",
        "Original source evidence:", "Expected difference:",
    ):
        assert label in template.group(1)
    contract = " ".join(contract.split())
    assert "existing reflect/converge" in contract
    assert "at most five" in contract.lower()
    assert "across all worklists" in contract
    assert "non-mined `lessons` room" in contract
    assert "dedup" in contract.lower()


def test_documented_incremental_proposal_uses_existing_reflect_conclusion():
    contract = section(DREAM, "Session lesson review")
    examples = [
        json.loads(block) for block in re.findall(
            r"```json\n(.*?)```", contract, re.DOTALL,
        )
    ]
    surfaced = [
        example for example in examples
        if example.get("decision", {}).get("action") == "surface"
    ]
    assert len(surfaced) == 1, "show one complete accepted-lesson decision"
    item = surfaced[0]
    decision = item["decision"]

    from dream_incremental import _proposals
    from dream_metadata import content_hash

    coverage = [
        {
            "id": identity, "source_kind": "session", "session_id": session_id,
            "text": text, "content_hash": content_hash(text),
        }
        for identity, session_id, text in (
            ("session:<a>", "<a>", "First original session observation"),
            ("session:<b>", "<b>", "Independent original session observation"),
        )
    ]
    worklist = {
        "scope": {"wing": "<project-wing>"},
        "incremental": {"run_id": "example-run"},
        "items": [item],
    }
    resolved, = _proposals(worklist, coverage)
    assert resolved["reflect_kind"] == "converge"
    assert resolved["text"] == decision["conclusion"]["text"]
    assert resolved["text"]
    assert resolved["wing"] == "<project-wing>"
    assert resolved["room"] == "lessons"
    assert resolved["member_ids"] == ["session:<a>", "session:<b>"]
    assert resolved["evidence"]["support_ids"] == ["<a>", "<b>"]
    assert resolved["min_support"] == 2

    memories = [{**source, "source_kind": "memory"} for source in coverage]
    with pytest.raises(ValueError, match="original raw sessions"):
        _proposals(worklist, memories)


def test_documented_review_and_completion_edits_are_explicit():
    contract = section(DREAM, "Session lesson review")
    examples = [
        json.loads(block) for block in re.findall(
            r"```json\n(.*?)```", contract, re.DOTALL,
        )
    ]
    for action in ("reviewed", "complete"):
        edits = [example for example in examples if example.get("action") == action]
        assert len(edits) == 1, f"document the {action} edit, not just lesson items"
        assert edits[0]["reason"].strip()


@pytest.mark.parametrize("case,required", (
    ("checkpoint scope", ("dream-checkpoints.json", "repository", "wing", "session store")),
    ("partial review", ("every coverage record", "missing reviews", "do not advance")),
    ("frozen boundary", ("frozen upper", "source drift", "re-harvest")),
    ("failed adoption", ("failed writes", "receipts", "retry")),
    ("safe preview", ("dry-run", "proposal-only", "explicit", "do not advance")),
    ("overlapping run", ("stale overlapping", "checkpoint", "re-harvest")),
    ("late source versions", ("reviewed_versions", "late", "historical edits", "unchanged")),
    ("producer timestamps", ("local time", "utc", "filed_at")),
    ("checkpoint upgrade", ("version 2", "version 1", "reconciliation", "re-harvest")),
))
def test_incremental_completion_pressure_contract(case, required):
    contract = " ".join(section(DREAM, "Incremental completion and recovery").lower().split())
    assert all(term in contract for term in required), case


@pytest.mark.parametrize("case,required", (
    ("weak support", ("abstain", "distinct original sessions", "at least two")),
    ("missing sources", ("missing", "original", "abstain")),
    ("memory grounding", ("original memories", "exact quotes", "session support")),
    ("large first run", ("batches", "every coverage record", "five", "output")),
    ("synthetic recurrence", ("generated", "independent evidence")),
    ("proposal versus adoption", ("review", "adopt", "add-only")),
))
def test_session_review_pressure_contract(case, required):
    contract = " ".join(section(DREAM, "Session lesson review").lower().split())
    assert all(term.lower() in contract for term in required), case


@pytest.mark.parametrize("path", RECALL_DOCS)
@pytest.mark.parametrize("case,required", (
    ("bounded relevant advice", ("after ordinary recall", "at most three", "lessons")),
    ("matching task", ("trigger", "scope", "exceptions", "original evidence")),
    ("unrelated task", ("no applicable lesson", "no advice", "broad search")),
    ("failed search", ("search failure", "not empty recall")),
    ("procedural record", ("explicit opt-in", "guidance", "explain")),
    ("not authority", ("fallible context", "not instructions", "efficacy")),
))
def test_task_start_recall_pressure_contract(path, case, required):
    contract = " ".join(section(path, "Task-relevant lessons").lower().split())
    assert all(term.lower() in contract for term in required), case


def test_contemplation_is_not_required_to_use_an_ordinary_lesson():
    contract = section(
        "skills/contemplate/SKILL.md", "Ordinary lessons are not KG premises",
    ).lower()
    assert "mempalace_search" in contract
    assert "not" in contract and "ontology" in contract
    assert "independent evidence" in contract
