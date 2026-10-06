from copy import deepcopy
import json
import tracemalloc

import pytest

from dream_activity import build_activities


def source_ref(line=1, field="data", event_id=None):
    return {
        "line": line,
        "event_id": event_id or f"event-{line}",
        "timestamp": None,
        "field": field,
    }


def claim(text, kind="description", *, truncated=False, line=1):
    return {
        "kind": kind,
        "attribution": "agent",
        "text": text,
        "truncated": truncated,
        "source": source_ref(line, f"data.arguments.{kind}"),
    }


def call(call_id="call-1", *, line=1, parent=None, agent=None, claims=None):
    return {
        "call_id": call_id,
        "tool_name": "unrelated-service:inspect",
        "agent_id": agent,
        "parent_call_id": parent,
        "source": source_ref(line),
        "claims": claims or [],
        "artifacts": [],
        "outcome": {
            "completion_observed": False,
            "tool_success": None,
            "exit_code": None,
            "source": None,
        },
    }


def packet(*calls):
    return {
        "schema_version": 1,
        "source": {
            "kind": "unrelated_host",
            "path": "/never/open/observations",
            "session_id": "session-1",
            "sha256": "a" * 64,
            "bytes": 9000,
            "events": 100,
        },
        "calls": list(calls),
        "warnings": [],
    }


def test_direct_call_without_artifact_preserves_unknowns_and_input():
    evidence = packet(call())
    before = deepcopy(evidence)

    report = build_activities(evidence)

    assert report["kind"] == "activity_intents"
    assert report["schema_version"] == 1
    assert report["source"] == evidence["source"]
    assert report["warnings"] == []
    [activity] = report["activities"]
    assert activity["root_call_id"] == "call-1"
    assert activity["call_ids"] == ["call-1"]
    assert activity["visibility"] == "observed_calls_only"
    assert activity["intent"] == {
        "status": "unknown",
        "text": None,
        "evidence": [],
        "attribution": None,
    }
    assert activity["calls"][0]["execution_status"] == "incomplete"
    assert activity["calls"][0]["artifacts"] == []
    assert evidence == before
    report["source"]["path"] = "changed"
    activity["calls"][0]["source"]["field"] = "changed"
    assert evidence == before


def test_only_explicit_call_parents_group_interleaved_agents():
    evidence = packet(
        call("child-a", parent="root-a", agent="worker-a", line=2,
             claims=[claim("Child-specific purpose", line=2)]),
        call("root-b", agent="worker-b", line=3),
        call("root-a", line=1, claims=[claim("Parent purpose")]),
        call("child-b", parent="root-b", agent="worker-b", line=4),
        call("grandchild", parent="child-a", agent="worker-a", line=5),
        call("unrelated", agent="worker-a", line=6),
    )
    report = build_activities(evidence)
    activities = {item["root_call_id"]: item for item in report["activities"]}

    assert set(activities) == {"root-a", "root-b", "unrelated"}
    assert activities["root-a"]["call_ids"] == ["root-a", "child-a", "grandchild"]
    assert activities["root-b"]["call_ids"] == ["root-b", "child-b"]
    assert activities["root-a"]["intent"]["text"] == "Parent purpose"
    assert activities["root-a"]["calls"][1]["intent"]["text"] == "Child-specific purpose"
    assert activities["root-a"]["calls"][2]["intent"]["status"] == "unknown"


def test_missing_parent_is_a_visible_unlinked_root_with_its_children():
    evidence = packet(
        call("orphan", parent="missing", line=1),
        call("child", parent="orphan", line=2),
    )
    evidence["warnings"] = ["adapter visibility gap"]

    report = build_activities(evidence)

    [activity] = report["activities"]
    assert activity["root_call_id"] == "orphan"
    assert activity["call_ids"] == ["orphan", "child"]
    assert report["warnings"][0] == "adapter visibility gap"
    assert len(report["warnings"]) == 2
    assert activity["calls"][0]["parent_call_id"] == "missing"


@pytest.mark.parametrize(
    ("claims", "status", "text"),
    [
        ([claim("Count failures")], "stated", "Count failures"),
        ([claim("Count failures"), claim("Count failures", line=2)], "stated", "Count failures"),
        ([claim("Count failures"), claim("Restart service", line=2)], "ambiguous", None),
        ([claim("Summary"), claim("Full delegation", "delegation_prompt", line=2)],
         "stated", "Full delegation"),
        ([claim("Full delegation", "delegation_prompt"),
          claim("Different delegation", "delegation_prompt", line=2)], "ambiguous", None),
        ([claim("Only a fragment", truncated=True)], "unknown", None),
        ([claim("Complete description"),
          claim("Partial delegation", "delegation_prompt", truncated=True, line=2)],
         "stated", "Complete description"),
        ([claim("Complete description"), claim("Incomplete peer", truncated=True, line=2)],
         "stated", "Complete description"),
        ([claim("Complete delegation", "delegation_prompt"),
          claim("Partial description", truncated=True, line=2)], "stated", "Complete delegation"),
    ],
)
def test_intent_claim_ranking_is_attributed_and_truncation_safe(claims, status, text):
    evidence = packet(call(claims=claims))

    [activity] = build_activities(evidence)["activities"]

    assert activity["intent"]["status"] == status
    assert activity["intent"]["text"] == text
    assert activity["calls"][0]["claims"] == claims
    assert activity["calls"][0]["intent"] == activity["intent"]
    assert activity["intent"]["attribution"] == "agent"
    assert activity["intent"]["evidence"]
    assert all(ref in [item["source"] for item in claims]
               for ref in activity["intent"]["evidence"])


def test_conflicting_claims_retain_both_exact_support_references():
    first = claim("Count failures")
    second = claim("Restart service", line=2)

    intent = build_activities(packet(call(claims=[first, second])))["activities"][0]["intent"]

    assert intent["evidence"] == [first["source"], second["source"]]


@pytest.mark.parametrize(
    ("tool_success", "exit_code", "expected"),
    [(None, None, "unknown"), (True, None, "succeeded"), (False, None, "failed"),
     (None, 0, "succeeded"), (None, 8, "failed"), (True, 1, "failed"),
     (False, 0, "failed"), (True, 0, "succeeded"), (None, -9, "failed")],
)
def test_execution_status_never_erases_tool_failure_or_nonzero_exit(
    tool_success, exit_code, expected
):
    observed = call()
    observed["outcome"] = {
        "completion_observed": True,
        "tool_success": tool_success,
        "exit_code": exit_code,
        "source": source_ref(2),
    }

    [activity] = build_activities(packet(observed))["activities"]

    assert activity["calls"][0]["execution_status"] == expected
    assert activity["calls"][0]["outcome"] == observed["outcome"]
    assert activity["intent"]["status"] == "unknown"


def test_activity_identity_is_stable_source_qualified_and_unambiguous():
    base = packet(call())
    activity_id = build_activities(base)["activities"][0]["activity_id"]
    appended = deepcopy(base)
    appended["source"]["sha256"] = "b" * 64
    appended["source"]["events"] += 1
    appended["calls"].append(call("other", line=3))
    assert build_activities(appended)["activities"][0]["activity_id"] == activity_id
    for key, value in [("session_id", "session-2"), ("kind", "another_host")]:
        changed = deepcopy(base)
        changed["source"][key] = value
        assert build_activities(changed)["activities"][0]["activity_id"] != activity_id
    left, right = packet(call("b:c")), packet(call("c"))
    left["source"]["session_id"], right["source"]["session_id"] = "a", "a:b"
    assert (build_activities(left)["activities"][0]["activity_id"] !=
            build_activities(right)["activities"][0]["activity_id"])


@pytest.mark.parametrize(
    "calls",
    [
        [call(), call()],
        [call("a", parent="a")],
        [call("a", parent="b"), call("b", parent="a", line=2)],
        [call("a", parent="b"), call("b", parent="c", line=2),
         call("c", parent="a", line=3)],
    ],
)
def test_duplicate_call_ids_and_parent_cycles_fail(calls):
    with pytest.raises(ValueError):
        build_activities(packet(*calls))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True), (("schema_version",), 2),
        (("source",), None), (("source", "kind"), ""),
        (("source", "session_id"), []), (("source", "path"), None),
        (("source", "sha256"), "ABC"), (("source", "bytes"), -1),
        (("source", "bytes"), True), (("source", "events"), 1.5),
        (("calls",), {}), (("warnings",), "warning"), (("warnings",), [{}]),
        (("calls", 0, "call_id"), ""), (("calls", 0, "tool_name"), None),
        (("calls", 0, "agent_id"), 2), (("calls", 0, "parent_call_id"), ["a", "b"]),
        (("calls", 0, "claims"), None), (("calls", 0, "artifacts"), {}),
        (("calls", 0, "source", "line"), 0),
        (("calls", 0, "source", "line"), True),
        (("calls", 0, "source", "event_id"), []),
        (("calls", 0, "source", "timestamp"), 1),
        (("calls", 0, "source", "field"), ""),
        (("calls", 0, "outcome"), []),
        (("calls", 0, "outcome", "completion_observed"), "false"),
        (("calls", 0, "outcome", "tool_success"), 1),
        (("calls", 0, "outcome", "exit_code"), False),
        (("calls", 0, "outcome", "source"), source_ref(2)),
        (("calls", 0, "outcome", "completion_observed"), True),
        (("calls", 0, "outcome", "tool_success"), True),
        (("calls", 0, "outcome", "exit_code"), 0),
    ],
)
def test_malformed_evidence_fails_explicitly_without_mutation(path, value):
    evidence = packet(call())
    target = evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    before = deepcopy(evidence)

    with pytest.raises(ValueError):
        build_activities(evidence)

    assert evidence == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "user_request"), ("attribution", "user"), ("text", ""),
     ("text", 42), ("truncated", 1), ("source", None)],
)
def test_invalid_claims_are_not_accepted_as_stated_intent(field, value):
    observed = claim("Valid")
    observed[field] = value
    with pytest.raises(ValueError):
        build_activities(packet(call(claims=[observed])))


def test_missing_required_nullable_fields_and_unknown_schema_fields_fail():
    evidence = packet(call())
    del evidence["calls"][0]["agent_id"]
    with pytest.raises(ValueError):
        build_activities(evidence)
    evidence = packet(call())
    evidence["calls"][0]["parentId"] = "not-a-call-parent"
    with pytest.raises(ValueError):
        build_activities(evidence)


def test_empty_source_is_valid_but_observations_need_events():
    evidence = packet()
    evidence["source"]["events"] = 0
    evidence["source"]["bytes"] = 0
    assert build_activities(evidence)["activities"] == []
    evidence["calls"].append(call())
    with pytest.raises(ValueError):
        build_activities(evidence)


def test_deep_explicit_parent_chain_does_not_use_python_recursion():
    evidence = packet(*[
        call(str(index), line=index + 1, parent=str(index - 1) if index else None)
        for index in range(2000)
    ])
    evidence["source"]["events"] = 2000

    [activity] = build_activities(evidence)["activities"]

    assert activity["root_call_id"] == "0"
    assert len(activity["call_ids"]) == 2000


def completed(observed, *, line=2, success=None, exit_code=None):
    observed["outcome"] = {
        "completion_observed": True,
        "tool_success": success,
        "exit_code": exit_code,
        "source": source_ref(line),
    }
    return observed


def artifact(path, relation="referenced", *, line=1):
    return {
        "path": path,
        "relation": relation,
        "source": source_ref(line, "data.arguments.path"),
    }


def review_for(activity, *, evidence=None, source_sha256="a" * 64):
    return {
        "activity_id": activity["activity_id"],
        "source_sha256": source_sha256,
        "status": "inferred",
        "text": "The reviewer interprets this as an error-count comparison",
        "evidence": evidence or [activity["calls"][0]["source"]],
        "rationale": "This interpretation rests on the explicitly selected observation",
    }


def test_one_completion_cannot_be_attached_to_two_distinct_calls():
    first = completed(call("a"), line=3)
    second = completed(call("b", line=2), line=3)
    with pytest.raises(ValueError):
        build_activities(packet(first, second))


@pytest.mark.parametrize(
    ("field", "value"),
    [("path", ""), ("path", []), ("relation", "executed"),
     ("source", None), ("source", {"line": 1})],
)
def test_malformed_artifacts_are_rejected_without_opening_paths(field, value):
    observed = call()
    item = artifact("/never/open/query.anything")
    item[field] = value
    observed["artifacts"] = [item]
    with pytest.raises(ValueError):
        build_activities(packet(observed))


def test_explicit_review_is_not_automatic_and_preserves_original_observations():
    from dream_activity import apply_reviews

    report = build_activities(packet(call(claims=[claim("Count failures")])))
    before = deepcopy(report)
    [activity] = report["activities"]
    review = review_for(activity, evidence=[activity["calls"][0]["claims"][0]["source"]])
    review_before = deepcopy(review)

    [reviewed] = apply_reviews(report, [review])["activities"]

    assert report == before
    assert review == review_before
    assert reviewed["observed_intent"] == activity["intent"]
    assert reviewed["calls"] == activity["calls"]
    assert reviewed["intent"] == {
        "status": "inferred",
        "text": review["text"],
        "evidence": review["evidence"],
        "attribution": "reviewer",
    }
    assert reviewed["review"] == {**review, "attribution": "reviewer"}
    assert "review" not in activity
    assert apply_reviews(report, []) == report


def test_review_can_cite_child_call_artifact_and_outcome_evidence_in_its_activity():
    from dream_activity import apply_reviews

    child = completed(call("child", parent="root", line=2), line=3, success=False)
    child["artifacts"] = [artifact("./query.sql", line=2)]
    report = build_activities(packet(call("root"), child))
    [activity] = report["activities"]
    references = [child["source"], child["artifacts"][0]["source"], child["outcome"]["source"]]

    [reviewed] = apply_reviews(report, [review_for(activity, evidence=references)])["activities"]

    assert reviewed["intent"]["evidence"] == references
    assert reviewed["observed_intent"]["status"] == "unknown"
    assert reviewed["calls"][1]["execution_status"] == "failed"


def test_review_cannot_borrow_source_from_an_unrelated_activity():
    from dream_activity import apply_reviews

    report = build_activities(packet(call("a"), call("b", line=2)))
    first, second = report["activities"]
    review = review_for(first, evidence=[second["calls"][0]["source"]])
    before = deepcopy(report)

    with pytest.raises(ValueError):
        apply_reviews(report, [review])
    assert report == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("activity_id", "unknown"), ("activity_id", []), ("status", "stated"),
     ("text", ""), ("text", None), ("rationale", " "), ("rationale", 2),
     ("evidence", []), ("evidence", None), ("evidence", [source_ref(90)]),
     ("evidence", [{"line": 1}]), ("evidence", [source_ref(), source_ref()])],
)
def test_malformed_and_unattributed_reviews_are_rejected(field, value):
    from dream_activity import apply_reviews

    report = build_activities(packet(call()))
    review = review_for(report["activities"][0])
    review[field] = value
    with pytest.raises(ValueError):
        apply_reviews(report, [review])


def test_duplicate_reviews_are_rejected_within_batch_and_across_calls():
    from dream_activity import apply_reviews

    report = build_activities(packet(call()))
    review = review_for(report["activities"][0])
    with pytest.raises(ValueError):
        apply_reviews(report, [review, review])
    reviewed = apply_reviews(report, [review])
    with pytest.raises(ValueError):
        apply_reviews(reviewed, [review])
    assert apply_reviews(reviewed, []) == reviewed


@pytest.mark.parametrize("malformed", [None, {}, [None], [{"arbitrary": "value"}]])
def test_review_list_shape_is_validated(malformed):
    from dream_activity import apply_reviews

    with pytest.raises(ValueError):
        apply_reviews(build_activities(packet(call())), malformed)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("kind",), "artifact_reuse_review"),
        (("activities",), None),
        (("activities", 0, "activity_id"), "forged"),
        (("activities", 0, "root_call_id"), "forged"),
        (("activities", 0, "call_ids"), ["forged"]),
        (("activities", 0, "intent", "status"), "inferred"),
        (("activities", 0, "calls", 0, "intent", "text"), "forged"),
        (("activities", 0, "calls", 0, "execution_status"), "succeeded"),
    ],
)
def test_projection_and_reviews_reject_malformed_or_forged_derived_reports(path, value):
    from dream_activity import apply_reviews, artifact_review

    report = build_activities(packet(call()))
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValueError):
        apply_reviews(report, [])
    with pytest.raises(ValueError):
        artifact_review(report)


def test_artifact_review_groups_exact_paths_and_keeps_raw_provenance_and_state():
    from dream_activity import apply_reviews, artifact_review

    first = completed(call("a", claims=[claim("Count deployment errors")]),
                      line=2, success=True, exit_code=7)
    first["artifacts"] = [artifact("./query.sql", "invoked")]
    second = call("b", line=3)
    second["artifacts"] = [artifact("./query.sql", "modified", line=3)]
    report = build_activities(packet(first, second))
    report = apply_reviews(report, [review_for(report["activities"][0])])
    before = deepcopy(report)

    view = artifact_review(report)

    assert view["kind"] == "artifact_reuse_review"
    assert view["schema_version"] == 1
    assert view["source"] == report["source"]
    [candidate] = view["candidates"]
    assert candidate["path"] == "./query.sql"
    assert candidate["assessment"] == "needs_review"
    assert candidate["activity_ids"] == [item["activity_id"] for item in report["activities"]]
    one, two = candidate["observations"]
    assert one["relation"] == "invoked"
    assert one["source"] == first["artifacts"][0]["source"]
    assert one["call_source"] == first["source"]
    assert one["outcome"] == first["outcome"]
    assert one["execution_status"] == "failed"
    assert one["intent"]["status"] == "inferred"
    assert one["observed_intent"]["status"] == "stated"
    assert one["call_intent"]["text"] == "Count deployment errors"
    assert one["review"] == report["activities"][0]["review"]
    assert two["relation"] == "modified"
    assert two["execution_status"] == "incomplete"
    assert two["intent"]["status"] == "unknown"
    assert two["review"] is None
    assert report == before
    one["outcome"]["tool_success"] = False
    view["source"]["path"] = "changed"
    assert report == before


def test_artifact_paths_are_extension_neutral_and_never_normalized_or_resolved():
    from dream_activity import artifact_review

    paths = ["./query.sql", "./program.py", "./automation.fsx", "./no-extension",
             "./config.unusual", "./a/../query.sql", "/never/open/query.sql"]
    observed = call()
    observed["artifacts"] = [artifact(path) for path in paths]

    candidates = artifact_review(build_activities(packet(observed)))["candidates"]

    assert [item["path"] for item in candidates] == paths
    assert all(item["assessment"] == "needs_review" for item in candidates)
    assert len({item["path"] for item in candidates}) == len(paths)


def test_artifactless_activity_is_not_fabricated_into_a_reuse_candidate():
    from dream_activity import artifact_review

    assert artifact_review(build_activities(packet(call())))["candidates"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("event_id", "different-id"), ("timestamp", "2026-01-01T00:00:00Z")],
)
def test_references_for_one_source_line_cannot_disagree_on_event_identity(field, value):
    observed = call(claims=[claim("Inspect")])
    observed["claims"][0]["source"][field] = value
    with pytest.raises(ValueError):
        build_activities(packet(observed))


def test_one_event_id_cannot_identify_different_source_lines():
    first, second = call("a"), call("b", line=2)
    second["source"]["event_id"] = first["source"]["event_id"]
    with pytest.raises(ValueError):
        build_activities(packet(first, second))


def test_nullable_source_identity_and_lines_after_blank_events_remain_valid():
    observed = call(line=200)
    observed["source"]["event_id"] = None
    evidence = packet(observed)
    evidence["source"]["events"] = 1
    assert build_activities(evidence)["activities"][0]["calls"][0]["source"] == observed["source"]


def test_repeated_exact_claim_and_reference_collapse_only_derived_support():
    observed = claim("Inspect")
    [activity] = build_activities(packet(call(claims=[observed, deepcopy(observed)])))["activities"]
    assert activity["intent"]["status"] == "stated"
    assert activity["intent"]["evidence"] == [observed["source"]]
    assert activity["calls"][0]["claims"] == [observed, observed]


def test_artifact_view_preserves_truncated_and_conflicting_raw_claims():
    from dream_activity import artifact_review

    observed = call(claims=[
        claim("First fragment", truncated=True),
        claim("Conflicting fragment", truncated=True, line=2),
    ])
    observed["artifacts"] = [artifact("./analysis.extension-neutral")]

    view = artifact_review(build_activities(packet(observed)))

    [observation] = view["candidates"][0]["observations"]
    assert observation["claims"] == observed["claims"]
    assert observation["intent"]["status"] == "unknown"
    assert observation["observed_intent"]["text"] is None


def test_reviewing_a_second_activity_preserves_the_first_existing_review():
    from dream_activity import apply_reviews, artifact_review

    report = build_activities(packet(call("a"), call("b", line=2)))
    first, second = report["activities"]
    once = apply_reviews(report, [review_for(first)])
    twice = apply_reviews(once, [review_for(second)])
    assert twice["activities"][0] == once["activities"][0]
    assert twice["activities"][1]["intent"]["status"] == "inferred"
    assert artifact_review(twice)["candidates"] == []


@pytest.mark.parametrize("field", ["observed_intent", "review"])
def test_review_provenance_cannot_be_deleted_and_still_accepted(field):
    from dream_activity import apply_reviews, artifact_review

    report = build_activities(packet(call()))
    reviewed = apply_reviews(report, [review_for(report["activities"][0])])
    del reviewed["activities"][0][field]
    with pytest.raises(ValueError):
        artifact_review(reviewed)


def test_forged_review_state_is_rejected_even_if_observed_calls_are_intact():
    from dream_activity import apply_reviews, artifact_review

    report = build_activities(packet(call()))
    reviewed = apply_reviews(report, [review_for(report["activities"][0])])
    reviewed["activities"][0]["intent"]["text"] = "Not the actual review"
    with pytest.raises(ValueError):
        artifact_review(reviewed)


def test_review_evidence_matches_all_source_fields_not_merely_line_or_event():
    from dream_activity import apply_reviews

    report = build_activities(packet(call()))
    reference = deepcopy(report["activities"][0]["calls"][0]["source"])
    reference["field"] = "data.unobserved"
    with pytest.raises(ValueError):
        apply_reviews(report, [review_for(report["activities"][0], evidence=[reference])])


def test_review_requires_snapshot_hash_even_when_all_references_match():
    from dream_activity import apply_reviews

    report = build_activities(packet(call()))
    review = review_for(report["activities"][0])
    del review["source_sha256"]
    with pytest.raises(ValueError):
        apply_reviews(report, [review])


@pytest.mark.parametrize("digest", ["b" * 64, "A" * 64, "", None, 1, ["a" * 64]])
def test_review_rejects_mismatched_or_invalid_snapshot_hash(digest):
    from dream_activity import apply_reviews

    report = build_activities(packet(call()))
    review = review_for(report["activities"][0], source_sha256=digest)
    with pytest.raises(ValueError):
        apply_reviews(report, [review])


def test_snapshot_bound_review_retains_full_original_source_and_exact_claim_reference():
    from dream_activity import apply_reviews

    evidence = packet(call(claims=[claim("Inspect the first version")]))
    evidence["source"]["sha256"] = "c" * 64
    report = build_activities(evidence)
    [activity] = report["activities"]
    reference = activity["calls"][0]["claims"][0]["source"]
    review = review_for(activity, evidence=[reference], source_sha256="c" * 64)

    reviewed = apply_reviews(report, [review])

    assert reviewed["source"] == evidence["source"]
    assert reviewed["activities"][0]["review"]["source_sha256"] == "c" * 64
    assert reviewed["activities"][0]["intent"]["evidence"] == [reference]
    fabricated = deepcopy(review)
    fabricated["evidence"][0]["field"] = "data.arguments.unobserved_prompt"
    with pytest.raises(ValueError):
        apply_reviews(report, [fabricated])


def test_review_cannot_be_replayed_on_changed_snapshot_with_identical_ids_and_references():
    from dream_activity import apply_reviews

    evidence = packet(call(claims=[claim("Inspect the first version")]))
    old = build_activities(evidence)
    review = review_for(old["activities"][0])
    evidence["calls"][0]["claims"][0]["text"] = "Delete the second version"
    evidence["source"]["sha256"] = "b" * 64
    new = build_activities(evidence)
    assert old["activities"][0]["activity_id"] == new["activities"][0]["activity_id"]
    assert old["activities"][0]["calls"][0]["source"] == new["activities"][0]["calls"][0]["source"]

    with pytest.raises(ValueError):
        apply_reviews(new, [review])


def test_already_reviewed_report_fails_when_snapshot_binding_is_changed():
    from dream_activity import apply_reviews, artifact_review

    report = build_activities(packet(call()))
    reviewed = apply_reviews(report, [review_for(report["activities"][0])])
    reviewed["source"]["sha256"] = "b" * 64
    with pytest.raises(ValueError):
        apply_reviews(reviewed, [])
    with pytest.raises(ValueError):
        artifact_review(reviewed)


def test_complete_description_survives_truncated_delegation_with_only_its_own_support():
    declared = claim("Count deployment errors")
    truncated = claim("Audit deployment errors and investigate every affected",
                      "delegation_prompt", truncated=True, line=2)
    [activity] = build_activities(packet(call(claims=[declared, truncated])))["activities"]

    assert activity["intent"] == {
        "status": "stated",
        "text": "Count deployment errors",
        "evidence": [declared["source"]],
        "attribution": "agent",
    }
    assert activity["calls"][0]["claims"] == [declared, truncated]


def test_artifact_expansion_is_rejected_during_projection_not_after_full_materialization():
    from dream_activity import artifact_review

    calls = []
    for index in range(10):
        observed = call(str(index), line=index + 1,
                        claims=[claim("x" * 2000, line=index + 1)])
        observed["artifacts"] = [
            artifact(f"./probe-{index}-{number}.sql", line=index + 1)
            for number in range(256)
        ]
        calls.append(observed)
    report = build_activities(packet(*calls))
    before = deepcopy(report)

    with pytest.raises(ValueError, match="output.*limit"):
        artifact_review(report)

    assert report == before


def test_projection_guard_fires_before_allocating_repeated_observations(monkeypatch):
    import dream_activity

    observed = call(claims=[claim("x" * 2000)])
    observed["artifacts"] = [artifact(f"./query-{index}.sql") for index in range(256)]
    report = build_activities(packet(observed))
    copied_observations = 0
    original_copy = deepcopy

    def count_observation_copies(value, *args, **kwargs):
        nonlocal copied_observations
        if isinstance(value, dict) and "call_intent" in value:
            copied_observations += 1
        return original_copy(value, *args, **kwargs)

    monkeypatch.setattr(dream_activity, "deepcopy", count_observation_copies)
    with pytest.raises(ValueError):
        dream_activity.artifact_review(report, max_output_bytes=20_000)
    assert copied_observations < 3


def test_artifact_budget_counts_exact_utf8_envelope_and_final_newline():
    from dream_activity import artifact_review, encode_json

    observed = call(claims=[claim("調査" * 10)])
    observed["artifacts"] = [artifact("./分析.sql"), artifact("./分析.sql", "modified"),
                             artifact("./different.py")]
    report = build_activities(packet(observed))
    projected = artifact_review(report)
    exact = len(encode_json(projected))

    assert artifact_review(report, max_output_bytes=exact) == projected
    with pytest.raises(ValueError):
        artifact_review(report, max_output_bytes=exact - 1)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, None])
def test_output_budget_requires_positive_integer(limit):
    from dream_activity import artifact_review, encode_json

    with pytest.raises(ValueError):
        artifact_review(build_activities(packet()), max_output_bytes=limit)
    with pytest.raises(ValueError):
        encode_json({}, max_output_bytes=limit)


def test_bounded_json_encoder_matches_exact_utf8_and_includes_newline():
    from dream_activity import encode_json

    value = {"quote": '"\n\\', "日本語": ["調査", None, False, 3]}
    expected = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    assert encode_json(value, max_output_bytes=len(expected)) == expected
    with pytest.raises(ValueError):
        encode_json(value, max_output_bytes=len(expected) - 1)


def test_bounded_json_encoder_stops_before_materializing_expanded_string():
    from dream_activity import encode_json

    value = ["x" * 2000] * 10_000
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="output.*limit"):
            encode_json(value, max_output_bytes=16_384)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak_bytes < 1_000_000


def test_compact_artifact_index_resolves_every_observation_against_canonical_report():
    from dream_activity import apply_reviews, artifact_review

    paths = ["./query.sql", "./program.py", "./automation.fsx", "./extensionless",
             "./config.unusual", "./a/../query.sql"]
    root = completed(call("root", claims=[claim("Inspect deployment failures")]),
                     line=2, success=False)
    root["artifacts"] = [artifact(path) for path in paths]
    child = call("child", parent="root", line=3)
    child["artifacts"] = [artifact("./query.sql", "read", line=3)]
    separate = call("separate", line=4)
    separate["artifacts"] = [artifact("./query.sql", "modified", line=4)]
    report = build_activities(packet(root, child, separate))
    report = apply_reviews(report, [review_for(report["activities"][0])])
    before = deepcopy(report)

    index = artifact_review(report, compact=True)

    assert set(index) == {"schema_version", "kind", "source", "candidates"}
    assert index["kind"] == "artifact_reuse_index"
    assert index["schema_version"] == 1
    assert index["source"] == report["source"]
    assert [candidate["path"] for candidate in index["candidates"]] == paths
    activities = {item["activity_id"]: item for item in report["activities"]}
    observations = []
    for candidate in index["candidates"]:
        assert set(candidate) == {"path", "assessment", "activity_ids", "observations"}
        assert candidate["assessment"] == "needs_review"
        assert candidate["activity_ids"] == list(dict.fromkeys(
            observation["activity_id"] for observation in candidate["observations"]
        ))
        for observation in candidate["observations"]:
            assert set(observation) == {"activity_id", "call_id", "relation", "source"}
            activity = activities[observation["activity_id"]]
            resolved = next(item for item in activity["calls"]
                            if item["call_id"] == observation["call_id"])
            assert {
                "path": candidate["path"],
                "relation": observation["relation"],
                "source": observation["source"],
            } in resolved["artifacts"]
            observations.append(observation)
    assert len(observations) == len(paths) + 2
    assert index["candidates"][0]["activity_ids"] == list(activities)
    assert report == before
    index["source"]["path"] = "changed"
    observations[0]["source"]["field"] = "changed"
    assert report == before


def test_compact_index_fits_default_bound_when_full_projection_does_not():
    from dream_activity import artifact_review, encode_json

    calls = []
    for index in range(2):
        observed = call(str(index), line=index + 1,
                        claims=[claim("x" * 2000, line=index + 1)])
        observed["artifacts"] = [
            artifact(f"./probe-{index}-{number}.sql", line=index + 1)
            for number in range(256)
        ]
        calls.append(observed)
    report = build_activities(packet(*calls))
    with pytest.raises(ValueError, match="output.*limit"):
        artifact_review(report)

    index = artifact_review(report, compact=True)
    compact_bytes = len(encode_json(index))
    # The larger bound measures the legacy shape, not a new production default.
    full = artifact_review(report, max_output_bytes=8 * 1024 * 1024)
    full_bytes = len(encode_json(full, max_output_bytes=8 * 1024 * 1024))
    canonical_bytes = len(encode_json(report))

    assert compact_bytes < 4 * 1024 * 1024 < full_bytes
    assert compact_bytes * 10 < full_bytes
    assert [candidate["path"] for candidate in index["candidates"]] == [
        candidate["path"] for candidate in full["candidates"]
    ]
    print(f"Synthetic bytes: canonical={canonical_bytes}, full={full_bytes}, compact={compact_bytes}")


def test_compact_index_retains_exact_utf8_envelope_and_newline_budget():
    from dream_activity import artifact_review, encode_json

    first = call("first")
    first["artifacts"] = [artifact("./分析.sql"), artifact("./分析.sql", "read")]
    second = call("second", line=2)
    second["artifacts"] = [artifact("./分析.sql", "modified", line=2)]
    report = build_activities(packet(first, second))
    index = artifact_review(report, compact=True)
    exact = len(encode_json(index))

    assert artifact_review(report, compact=True, max_output_bytes=exact) == index
    with pytest.raises(ValueError, match="output.*limit"):
        artifact_review(report, compact=True, max_output_bytes=exact - 1)


@pytest.mark.parametrize("compact", [None, 0, 1, "true", []])
def test_compact_mode_requires_boolean(compact):
    from dream_activity import artifact_review

    with pytest.raises(ValueError):
        artifact_review(build_activities(packet()), compact=compact)


def test_compact_projection_keeps_validation_and_full_default_shape():
    from dream_activity import artifact_review

    observed = call()
    observed["artifacts"] = [artifact("./test.sql")]
    report = build_activities(packet(observed))
    full = artifact_review(report)
    assert artifact_review(report, compact=False) == full
    assert full["kind"] == "artifact_reuse_review"
    assert "claims" in full["candidates"][0]["observations"][0]
    assert artifact_review(build_activities(packet(call())), compact=True)["candidates"] == []
    report["activities"][0]["calls"][0]["execution_status"] = "succeeded"
    with pytest.raises(ValueError):
        artifact_review(report, compact=True)
