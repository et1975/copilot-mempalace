"""Reasoning helpers and S4b ACQUIRE-loop driver for Track B B3."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import partial
from typing import Any

from dream_f8 import f8_assess
from dream_lib import (
    deductive_closure,
    find_transitive_gaps,
    normalize_predicate,
    derived_predicate_for,
    enabled_rules,
)
from dream_palace import (
    broker_assert_provisional,
    controller_step,
    create_or_resume_controlled_run,
    issue_approval,
    load_premises,
    retrieve_relevant_session_observations,
)


DEFAULT_ACQUIRE_BUDGETS = {"max_iterations": 5, "max_acquisitions": 5, "max_tool_calls": 20}


def heuristic_support_extractor(prompt_payload: dict) -> dict:
    """Deterministic, LLM-free F8 extractor stand-in.

    Returns verdict 'supports'/factual with an EXACT-substring sentence quote when
    the source content contains a single sentence co-mentioning BOTH the target
    subject and object (by name tokens derived from their entity ids); otherwise
    'not_addressed'. This is a heuristic for demos/tests, NOT semantic judgement;
    real deployments inject an LLM extractor. The F8 boundary re-validates the
    quote/span regardless.
    """
    try:
        target = prompt_payload.get("target") or {}
        source = prompt_payload.get("source") or {}
        content = source.get("content")
        if not isinstance(content, str):
            content = "" if content is None else str(content)

        subject_names = _entity_name_candidates(target.get("subject_id"))
        object_names = _entity_name_candidates(target.get("object_id"))
        if not subject_names or not object_names:
            return {"verdict": "not_addressed"}

        for match in re.finditer(r"[^.!?]*[.!?]", content):
            sentence = match.group(0)
            offset = len(sentence) - len(sentence.lstrip())
            start = match.start() + offset
            end = match.end()
            quote = content[start:end]
            lowered = quote.lower()
            if (
                any(name in lowered for name in subject_names)
                and any(name in lowered for name in object_names)
                and content[start:end] == quote
            ):
                return {
                    "verdict": "supports",
                    "quote": quote,
                    "char_span": {"start": start, "end": end},
                    "speaker": None,
                    "modality": "factual",
                }
    except Exception:
        return {"verdict": "not_addressed"}
    return {"verdict": "not_addressed"}


def _entity_name_candidates(entity_id: Any) -> list[str]:
    if entity_id is None:
        return []
    value = str(entity_id).strip()
    if not value:
        return []
    names = []
    for candidate in (value, value.replace("_", " ")):
        lowered = candidate.lower()
        if lowered and lowered not in names:
            names.append(lowered)
    return names


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


def default_recall(
    palace_path,
    query_text,
    gap,
    *,
    k=5,
    now=None,
) -> list[dict]:
    """Recall host-session observations and wrap them as F8 UntrustedSource dicts."""
    del gap
    retrieved_at = _iso_now(now)
    sources = []
    for obs in retrieve_relevant_session_observations(palace_path, query_text, k=k):
        locator = {
            key: obs.get(key)
            for key in ("id", "member_ids", "session_id", "agent", "date", "topic", "wing", "room", "similarity")
            if key in obs and obs.get(key) is not None
        }
        sources.append(
            {
                "source_type": "session_recall",
                "trust_domain": "session_store",
                "locator": locator,
                "retrieved_at": retrieved_at,
                "content": obs.get("text") or "",
            }
        )
    return sources


def acquire_loop(
    palace_path,
    *,
    query,
    rules,
    extractor_fn,
    recall_fn=None,
    run_id=None,
    budgets=None,
    trusted_speakers=None,
    source_kind="recall",
    now=None,
) -> dict:
    """Run the deterministic S4b deduce→gap→recall→F8→broker loop."""
    if extractor_fn is None:
        raise ValueError("acquire_loop requires extractor_fn")

    limits = dict(DEFAULT_ACQUIRE_BUDGETS)
    limits.update(budgets or {})
    recall = recall_fn if recall_fn is not None else partial(default_recall, palace_path)

    run = create_or_resume_controlled_run(palace_path, run_id=run_id, now=now)
    run_id = run["run_id"]
    owner_token = run["owner_token"]
    state = run["state"]
    version = int(run["version"])

    acquired: list[dict[str, Any]] = []
    attempted_keys: set[tuple[Any, str, Any]] = set()
    acquired_keys: set[tuple[Any, str, Any]] = set()
    iterations_used = 0
    acquisitions_used = 0
    tool_calls_used = 0
    answer: dict[str, Any] | None = None
    refusal = None
    status = "budget_exhausted"
    unfilled_reasons: dict[tuple[Any, str, Any], str] = {}
    last_gaps: list[dict[str, Any]] = []
    should_finalize = True

    def controller(action: str) -> bool:
        nonlocal state, version, refusal
        result = controller_step(
            palace_path,
            run_id,
            action,
            owner_token=owner_token,
            expected_version=version,
            now=now,
        )
        if not result.get("ok"):
            refusal = result.get("refusal", result)
            return False
        state = result["state"]
        version = int(result["version"])
        return True

    def terminate(next_status: str, next_answer: dict[str, Any], *, next_refusal=None) -> dict:
        nonlocal status, answer, refusal
        status = next_status
        answer = next_answer
        if next_refusal is not None:
            refusal = next_refusal
        return _acquire_result(
            status=status,
            run_id=run_id,
            answer=answer,
            acquired=acquired,
            unfilled_gaps=_unfilled_gaps(last_gaps, acquired_keys, unfilled_reasons),
            iterations_used=iterations_used,
            acquisitions_used=acquisitions_used,
            tool_calls_used=tool_calls_used,
            limits=limits,
            refusal=refusal,
        )

    try:
        for _ in range(int(limits["max_iterations"])):
            iterations_used += 1
            if state in {"open", "asserted"}:
                if not controller("start_deduction"):
                    return terminate("abandoned", answer or _unsupported_answer_for(query), next_refusal=refusal)
                if not controller("record_deduction"):
                    return terminate("abandoned", answer or _unsupported_answer_for(query), next_refusal=refusal)
            elif state == "deducing":
                if not controller("record_deduction"):
                    return terminate("abandoned", answer or _unsupported_answer_for(query), next_refusal=refusal)
            elif state != "deduced":
                refusal = {
                    "code": "bad_state",
                    "message": f"acquire_loop cannot deduce from state {state}",
                }
                return terminate("abandoned", answer or _unsupported_answer_for(query), next_refusal=refusal)

            prem = load_premises(palace_path, purpose="simulation", run_id=run_id)
            cands = deductive_closure(prem, rules, max_depth=8, max_iterations=50, max_candidates=500)
            answer = extract_boolean_reachability_answer(query, prem, cands, rules)
            if answer["value"] is True:
                return terminate("answered", answer)

            last_gaps = find_transitive_gaps(prem, rules)
            gap = select_gap(last_gaps, acquired_keys=acquired_keys, attempted_keys=attempted_keys, query=query)
            if gap is None:
                return terminate("fixpoint", answer)

            if acquisitions_used >= int(limits["max_acquisitions"]) or tool_calls_used >= int(limits["max_tool_calls"]):
                return terminate("budget_exhausted", answer)

            gap_key = gap_hypothesis_key(gap)
            hyp = gap["hypothesis"]
            sources = recall(_query_text_for(query, hyp), gap)
            tool_calls_used += 1
            supported = False
            for source in sources or []:
                target = {
                    "subject_id": hyp["subject_id"],
                    "predicate": hyp["predicate"],
                    "object_id": hyp["object_id"],
                }
                assessment = f8_assess(
                    source,
                    target,
                    extractor=extractor_fn,
                    trusted_speakers=trusted_speakers or set(),
                    now=now,
                )
                if assessment.get("valid") and assessment.get("supports"):
                    source_ref = _source_ref(source, gap, assessment)
                    canonical_args = {
                        "subject": hyp["subject_id"],
                        "predicate": hyp["predicate"],
                        "object": hyp["object_id"],
                        "status": "acquired",
                        "source_kind": source_kind,
                        "source_ref": source_ref,
                    }
                    approval = issue_approval(
                        palace_path,
                        run_id,
                        owner_token=owner_token,
                        expected_version=version,
                        approval_kind="assert_provisional",
                        tool_name="assert_provisional",
                        canonical_args=canonical_args,
                        now=now,
                    )
                    if not approval.get("ok"):
                        return terminate("abandoned", answer, next_refusal=approval.get("refusal", approval))
                    brokered = broker_assert_provisional(
                        palace_path,
                        run_id,
                        owner_token=owner_token,
                        expected_version=version,
                        approval_token=approval["approval_token"],
                        subject=hyp["subject_id"],
                        predicate=hyp["predicate"],
                        object=hyp["object_id"],
                        status="acquired",
                        source_kind=source_kind,
                        source_ref=source_ref,
                        now=now,
                    )
                    if not brokered.get("ok"):
                        return terminate("abandoned", answer, next_refusal=brokered.get("refusal", brokered))
                    state = brokered["state"]
                    version = int(brokered["version"])
                    acquired.append(
                        {
                            "gap_key": gap_key,
                            "provisional_id": brokered["provisional_id"],
                            "source_kind": source_kind,
                            "source_ref": source_ref,
                            "epistemic_status": "acquired",
                        }
                    )
                    acquired_keys.add(gap_key)
                    acquisitions_used += 1
                    supported = True
                    break

            if not supported:
                attempted_keys.add(gap_key)
                unfilled_reasons[gap_key] = "recall_no_support"
                if _gap_explicitly_unblocks_query(gap, query):
                    return terminate("fixpoint", answer)

        return terminate("budget_exhausted", answer or _unsupported_answer_for(query))
    finally:
        if should_finalize:
            if status == "abandoned":
                _best_effort_controller_step(palace_path, run_id, "abandon", owner_token, version, now)
            elif status in {"answered", "fixpoint"} and state in {"deduced", "gap_selected", "asserted"}:
                _best_effort_controller_step(palace_path, run_id, "finish_fixpoint", owner_token, version, now)


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
    if _gap_explicitly_unblocks_query(gap, query):
        return True

    hypothesis = gap.get("hypothesis") or {}
    return (
        hypothesis.get("subject_id") == query["subject_id"]
        or hypothesis.get("object_id") == query["object_id"]
    )


def _gap_explicitly_unblocks_query(gap: dict[str, Any], query: dict[str, str]) -> bool:
    target = (query["subject_id"], query["object_id"])
    evidence = gap.get("evidence") or {}
    for unblocked in evidence.get("unblocks") or []:
        if (unblocked.get("subject_id"), unblocked.get("object_id")) == target:
            return True
    return False


def _iso_now(now=None) -> str:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if isinstance(now, datetime):
        value = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(now)


def _query_text_for(query: dict[str, Any], hypothesis: dict[str, Any]) -> str:
    parts = [
        query.get("subject") or query.get("subject_id"),
        query.get("base_predicate"),
        query.get("object") or query.get("object_id"),
        hypothesis.get("subject") or hypothesis.get("subject_id"),
        hypothesis.get("predicate"),
        hypothesis.get("object") or hypothesis.get("object_id"),
    ]
    return " ".join(str(part) for part in parts if part is not None)


def _source_ref(source: dict[str, Any], gap: dict[str, Any], assessment: dict[str, Any]) -> str:
    if assessment.get("evidence_id"):
        return str(assessment["evidence_id"])
    if assessment.get("source_id"):
        return str(assessment["source_id"])
    locator = source.get("locator") or {}
    for key in ("id", "session_id", "source_ref"):
        if locator.get(key) is not None:
            return str(locator[key])
    if gap.get("gap_id") is not None:
        return str(gap["gap_id"])
    return ":".join(str(part) for part in gap_hypothesis_key(gap))


def _confidence(answer: dict[str, Any]) -> dict[str, str]:
    status = answer.get("epistemic_status")
    if status == "deduced":
        return {"level": "high", "rationale": "grounded on durable trusted premises"}
    if status == "entailed_given":
        return {"level": "medium", "rationale": "entailed by acquired provisional(s)"}
    return {"level": "low", "rationale": "no supporting acquisition found"}


def _unfilled_gaps(
    gaps: list[dict[str, Any]],
    acquired_keys: set[tuple[Any, str, Any]],
    reasons: dict[tuple[Any, str, Any], str],
) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for gap in gaps or []:
        key = gap_hypothesis_key(gap)
        if key in acquired_keys or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "gap_key": key,
                "duc": _gap_duc(gap),
                "reason": reasons.get(key, "not_selected"),
            }
        )
    for key, reason in reasons.items():
        if key not in seen and key not in acquired_keys:
            out.append({"gap_key": key, "duc": 0.0, "reason": reason})
    return out


def _acquire_result(
    *,
    status: str,
    run_id: str,
    answer: dict[str, Any],
    acquired: list[dict[str, Any]],
    unfilled_gaps: list[dict[str, Any]],
    iterations_used: int,
    acquisitions_used: int,
    tool_calls_used: int,
    limits: dict[str, Any],
    refusal: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "run_id": run_id,
        "answer": answer,
        "confidence": _confidence(answer),
        "acquired": list(acquired),
        "unfilled_gaps": unfilled_gaps,
        "budgets": {
            "iterations_used": iterations_used,
            "acquisitions_used": acquisitions_used,
            "tool_calls_used": tool_calls_used,
            "max_iterations": int(limits["max_iterations"]),
            "max_acquisitions": int(limits["max_acquisitions"]),
            "max_tool_calls": int(limits["max_tool_calls"]),
        },
        "refusal": refusal if status == "abandoned" else None,
    }


def _unsupported_answer_for(query: dict[str, Any]) -> dict[str, Any]:
    return _answer_frame(
        query["subject_id"],
        query["base_predicate"],
        query["object_id"],
        value=False,
        epistemic_status="unsupported",
        support=None,
        conditional_on=[],
    )


def _best_effort_controller_step(
    palace_path,
    run_id,
    action,
    owner_token,
    version,
    now,
) -> None:
    try:
        controller_step(
            palace_path,
            run_id,
            action,
            owner_token=owner_token,
            expected_version=version,
            now=now,
        )
    except Exception:
        pass
