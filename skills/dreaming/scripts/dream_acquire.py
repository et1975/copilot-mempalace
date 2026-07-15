"""Pure reasoning helpers for the Track B B3 ACQUIRE loop.

This module deliberately performs no I/O and owns no loop state.  The brokered
S4b loop supplies premises, deductive candidates, rules, and gap worklists.
"""

from __future__ import annotations

from typing import Any

from dream_lib import normalize_predicate, derived_predicate_for, enabled_rules


def closure_predicate_for(base_predicate: str, rules: list[dict[str, Any]]) -> str | None:
    """Return the derived closure predicate for an enabled transitive base rule."""
    base = normalize_predicate(base_predicate)
    for rule in enabled_rules(rules):
        if rule.get("family") != "transitive":
            continue
        if normalize_predicate(rule.get("predicate", "")) == base:
            return normalize_predicate(derived_predicate_for(rule))
    return None


def extract_boolean_reachability_answer(
    query: dict[str, str],
    premises: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Extract a deterministic boolean reachability answer from premises + closure.

    The only supported query IR is:
    ``{"subject_id": str, "base_predicate": str, "object_id": str}``.
    """
    subject_id = query["subject_id"]
    object_id = query["object_id"]
    base_predicate = normalize_predicate(query["base_predicate"])
    closure_predicate = closure_predicate_for(base_predicate, rules)
    answer_predicate = base_predicate

    premise_predicates = {base_predicate}
    if closure_predicate is not None:
        premise_predicates.add(closure_predicate)

    for premise in premises or []:
        if (
            premise.get("subject_id") == subject_id
            and premise.get("object_id") == object_id
            and normalize_predicate(premise.get("predicate", "")) in premise_predicates
        ):
            return _answer_frame(
                subject_id,
                answer_predicate,
                object_id,
                value=True,
                epistemic_status="deduced",
                support={
                    "kind": "premise",
                    "triple": _spo_projection(premise),
                },
                conditional_on=[],
            )

    if closure_predicate is not None:
        for candidate in candidates or []:
            conclusion = candidate.get("conclusion") or {}
            if (
                conclusion.get("subject_id") == subject_id
                and conclusion.get("object_id") == object_id
                and normalize_predicate(conclusion.get("predicate", "")) == closure_predicate
            ):
                epistemic_status = _candidate_epistemic_status(candidate)
                if epistemic_status in {"deduced", "entailed_given"}:
                    return _answer_frame(
                        subject_id,
                        answer_predicate,
                        object_id,
                        value=True,
                        epistemic_status=epistemic_status,
                        support={
                            "kind": "derived",
                            "candidate_id": candidate.get("candidate_id"),
                            "epistemic_status": epistemic_status,
                        },
                        conditional_on=_candidate_conditional_on(candidate),
                    )

    return _answer_frame(
        subject_id,
        answer_predicate,
        object_id,
        value=False,
        epistemic_status="unsupported",
        support=None,
        conditional_on=[],
    )


def answers_equivalent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return whether two answer frames are equivalent for S4 fixpoint purposes."""
    return (
        a.get("kind") == b.get("kind")
        and _answer_query_key(a) == _answer_query_key(b)
        and bool(a.get("value")) == bool(b.get("value"))
        and a.get("epistemic_status") == b.get("epistemic_status")
    )


def answer_changed(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return whether two answer frames differ."""
    return not answers_equivalent(a, b)


def gap_hypothesis_key(gap: dict[str, Any]) -> tuple[Any, str, Any]:
    """Canonical key for acquired/attempted deduplication by the S4 loop."""
    hypothesis = gap["hypothesis"]
    return (
        hypothesis["subject_id"],
        normalize_predicate(hypothesis["predicate"]),
        hypothesis["object_id"],
    )


def rank_gaps(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank gaps by DUC descending only; EAC is intentionally off in S4a."""
    return sorted(
        list(gaps or []),
        key=lambda gap: -_gap_duc(gap),
    )


def select_gap(
    gaps: list[dict[str, Any]],
    *,
    acquired_keys=frozenset(),
    attempted_keys=frozenset(),
    query: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Select the next eligible gap.

    If a query is supplied, prefer the first ranked gap that explicitly unblocks the
    query target, or whose hypothesised edge touches the query source/target.  This is
    a deterministic relevance hint only: if no gap matches it, the top DUC-ranked
    remaining gap is returned.
    """
    acquired = set(acquired_keys or ())
    attempted = set(attempted_keys or ())
    remaining = [
        gap
        for gap in rank_gaps(gaps)
        if gap_hypothesis_key(gap) not in acquired
        and gap_hypothesis_key(gap) not in attempted
    ]
    if not remaining:
        return None

    if query is not None:
        for gap in remaining:
            if _gap_prefers_query(gap, query):
                return gap

    return remaining[0]


def _answer_frame(
    subject_id: str,
    predicate: str,
    object_id: str,
    *,
    value: bool,
    epistemic_status: str,
    support: dict[str, Any] | None,
    conditional_on: list[Any],
) -> dict[str, Any]:
    return {
        "kind": "boolean",
        "query": {
            "subject_id": subject_id,
            "predicate": normalize_predicate(predicate),
            "object_id": object_id,
        },
        "value": value,
        "epistemic_status": epistemic_status,
        "support": support,
        "conditional_on": list(conditional_on or []),
    }


def _spo_projection(triple: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject_id": triple.get("subject_id"),
        "predicate": normalize_predicate(triple.get("predicate", "")),
        "object_id": triple.get("object_id"),
        "subject": triple.get("subject"),
        "object": triple.get("object"),
    }


def _candidate_epistemic_status(candidate: dict[str, Any]) -> str:
    evidence = candidate.get("evidence") or {}
    return candidate.get("epistemic_status") or evidence.get("epistemic_status") or "unsupported"


def _candidate_conditional_on(candidate: dict[str, Any]) -> list[Any]:
    proof = candidate.get("proof") or {}
    if "entailed_given" in proof:
        return list(proof.get("entailed_given") or [])
    return list(candidate.get("entailed_given") or [])


def _answer_query_key(answer: dict[str, Any]) -> tuple[Any, str, Any]:
    query = answer.get("query") or {}
    return (
        query.get("subject_id"),
        normalize_predicate(query.get("predicate", "")),
        query.get("object_id"),
    )


def _gap_duc(gap: dict[str, Any]) -> float:
    evidence = gap.get("evidence") or {}
    try:
        return float(evidence.get("duc", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _gap_prefers_query(gap: dict[str, Any], query: dict[str, str]) -> bool:
    target = (query["subject_id"], query["object_id"])
    evidence = gap.get("evidence") or {}
    for unblocked in evidence.get("unblocks") or []:
        if (unblocked.get("subject_id"), unblocked.get("object_id")) == target:
            return True

    hypothesis = gap.get("hypothesis") or {}
    return (
        hypothesis.get("subject_id") == query["subject_id"]
        or hypothesis.get("object_id") == query["object_id"]
    )
