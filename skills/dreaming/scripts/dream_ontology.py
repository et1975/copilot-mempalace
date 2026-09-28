"""Pure ontology rule-candidate helpers for dreaming.

This module intentionally has no palace coupling. It reads/writes only plain
``ontology.json`` documents and operates on in-memory predicate/triple dicts.
"""
import json
import os
from collections import defaultdict


ONTOLOGY_VERSION = 1

_TRANSITIVE_PREDICATES = {
    "depends_on",
    "part_of",
    "contains",
    "subclass_of",
    "subclass",
    "located_in",
    "ancestor_of",
    "precedes",
    "before",
    "after",
    "causes",
    "leads_to",
    "blocks",
    "requires",
    "includes",
    "parent_of",
}

_SYMMETRIC_PREDICATES = {
    "co_authored",
    "collaborates_with",
    "collaborates",
    "sibling_of",
    "related_to",
    "connected_to",
    "married_to",
    "adjacent_to",
    "near",
    "same_as",
    "synonym_of",
    "co_occurs_with",
}

_OF_INVERSE_PAIRS = (
    ("ancestor_of", "descendant_of"),
    ("parent_of", "child_of"),
)

_HEURISTIC_TRANSITIVE_RATIONALE = (
    "heuristic: predicate name suggests transitivity — REVIEW before enabling"
)
_HEURISTIC_SYMMETRIC_RATIONALE = (
    "heuristic: predicate name suggests symmetry — REVIEW before enabling"
)
_HEURISTIC_INVERSE_RATIONALE = (
    "heuristic: predicate names suggest inverse relationship — REVIEW before enabling"
)


def suggest_rules_from_predicates(predicates: list[str]) -> list[dict]:
    """Return disabled ontology rule candidates suggested by predicate names."""
    predicate_set = {p for p in predicates if isinstance(p, str) and p}
    rules_by_id = {}

    for predicate in predicate_set:
        if predicate in _TRANSITIVE_PREDICATES:
            _add_rule(
                rules_by_id,
                {
                    "id": f"transitive:{predicate}",
                    "family": "transitive",
                    "predicate": predicate,
                    "derived_predicate": f"{predicate}_closure",
                    "enabled": False,
                    "rationale": _HEURISTIC_TRANSITIVE_RATIONALE,
                },
            )
        if predicate in _SYMMETRIC_PREDICATES:
            _add_rule(
                rules_by_id,
                {
                    "id": f"symmetric:{predicate}",
                    "family": "symmetric",
                    "predicate": predicate,
                    "enabled": False,
                    "rationale": _HEURISTIC_SYMMETRIC_RATIONALE,
                },
            )

    for passive in predicate_set:
        if not passive.endswith("_by"):
            continue
        active = passive[: -len("_by")]
        if active in predicate_set:
            _add_inverse_rule(rules_by_id, active, passive, _HEURISTIC_INVERSE_RATIONALE)

    for predicate, inverse_predicate in _OF_INVERSE_PAIRS:
        if predicate in predicate_set and inverse_predicate in predicate_set:
            _add_inverse_rule(
                rules_by_id,
                predicate,
                inverse_predicate,
                _HEURISTIC_INVERSE_RATIONALE,
            )

    return _rules_sorted_by_id(rules_by_id)


def induce_rules_from_triples(triples: list[dict], min_support: int = 2) -> list[dict]:
    """Return disabled ontology rule candidates induced from observed triples."""
    min_support = max(1, int(min_support))
    triple_set = _triple_set(triples)
    edges_by_predicate = _edges_by_predicate(triple_set)
    rules_by_id = {}

    for predicate in sorted(edges_by_predicate):
        support, example = _symmetric_support(edges_by_predicate[predicate])
        if support >= min_support:
            _add_rule(
                rules_by_id,
                {
                    "id": f"symmetric:{predicate}",
                    "family": "symmetric",
                    "predicate": predicate,
                    "enabled": False,
                    "rationale": (
                        f"induced: {support} symmetric pair(s) observed, "
                        f"e.g. {example} — REVIEW"
                    ),
                },
            )

    predicates = sorted(edges_by_predicate)
    for i, predicate in enumerate(predicates):
        for inverse_predicate in predicates[i + 1 :]:
            support, example = _inverse_support(
                edges_by_predicate[predicate],
                predicate,
                edges_by_predicate[inverse_predicate],
                inverse_predicate,
            )
            if support >= min_support:
                _add_rule(
                    rules_by_id,
                    {
                        "id": f"inverse:{predicate}:{inverse_predicate}",
                        "family": "inverse",
                        "predicate": predicate,
                        "inverse_predicate": inverse_predicate,
                        "enabled": False,
                        "rationale": (
                            f"induced: {support} inverse co-occurrence(s), "
                            f"e.g. {example} — REVIEW"
                        ),
                    },
                )

    for predicate in sorted(edges_by_predicate):
        support, example = _transitive_support(edges_by_predicate[predicate])
        if support >= min_support:
            _add_rule(
                rules_by_id,
                {
                    "id": f"transitive:{predicate}",
                    "family": "transitive",
                    "predicate": predicate,
                    "derived_predicate": f"{predicate}_closure",
                    "enabled": False,
                    "rationale": (
                        f"induced: {support} chain(s) observed; "
                        "transitivity CANNOT be confirmed from data — "
                        f"REVIEW carefully (low confidence), e.g. {example}"
                    ),
                },
            )

    return _rules_sorted_by_id(rules_by_id)


def filter_base_triples(triples: list[dict], rules: list[dict] | None = None) -> list[dict]:
    """Drop derived triples so induction sees only observed facts."""
    derived_predicates = set()
    for rule in rules or []:
        for key in ("derived_predicate", "inverse_predicate"):
            value = rule.get(key)
            if isinstance(value, str) and value:
                derived_predicates.add(value)

    filtered = []
    for triple in triples:
        predicate = triple.get("predicate")
        if not isinstance(predicate, str):
            continue
        if predicate.endswith("_closure") or predicate in derived_predicates:
            continue
        # kg_derivations lineage-based exclusion is out of scope for this pure helper.
        filtered.append(triple)
    return filtered


def merge_ontology_candidates(existing: list[dict], new: list[dict]) -> tuple[list[dict], dict]:
    """Union rules by id, preserving existing rules verbatim."""
    merged = list(existing)
    seen = {rule.get("id") for rule in existing}
    to_add = []
    skipped_existing = 0

    for rule in sorted(new, key=lambda r: str(r.get("id", ""))):
        rule_id = rule.get("id")
        if rule_id in seen:
            skipped_existing += 1
            continue
        seen.add(rule_id)
        to_add.append(rule)

    merged.extend(to_add)
    return merged, {"added": len(to_add), "skipped_existing": skipped_existing}


def describe_rule_candidate(rule: dict) -> dict:
    """Render an ontology rule candidate as a plain-language approval prompt."""
    rule_id = rule.get("id", "")
    family = rule.get("family")
    predicate = rule.get("predicate", "")
    inverse_predicate = rule.get("inverse_predicate", "")

    if family == "transitive":
        plain_question = (
            f"When '{predicate}' forms a chain — A {predicate} B, and B {predicate} C — "
            f"should I conclude that A {predicate} C too?"
        )
        effect = f"Lets me follow chains of '{predicate}' to connect things you didn't state directly."
    elif family == "inverse":
        plain_question = (
            f"Are '{predicate}' and '{inverse_predicate}' just two ways of saying the same link "
            f"(X {predicate} Y means Y {inverse_predicate} X)?"
        )
        effect = f"Lets me use a '{predicate}' fact and its '{inverse_predicate}' restatement interchangeably."
    elif family == "symmetric":
        plain_question = (
            f"'{predicate}' looks like a two-way link between equals — if A and B are "
            f"connected by it, does it hold in both directions (B to A as well as A to B)?"
        )
        effect = f"Lets me treat '{predicate}' as mutual, so it counts in both directions."
    else:
        plain_question = f"Enable rule {rule_id}?"
        effect = rule.get("rationale") or ""

    return {
        "id": rule_id,
        "family": family,
        "plain_question": plain_question,
        "effect": effect,
        "evidence": rule.get("rationale") or "",
        "enabled": bool(rule.get("enabled", False)),
    }


def build_ontology_doc(rules: list[dict], version: int = 1) -> dict:
    """Build an ontology.json document."""
    return {"version": version, "rules": rules}


def read_ontology_doc(path: str) -> dict:
    """Read an ontology.json document, defaulting missing/empty files to v1 empty."""
    if not os.path.exists(path):
        return _empty_doc()
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        return _empty_doc()
    return json.loads(text)


def write_ontology_doc(path: str, doc: dict) -> None:
    """Write an ontology.json document as pretty UTF-8 JSON."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _add_inverse_rule(rules_by_id: dict[str, dict], predicate: str, inverse_predicate: str, rationale: str) -> None:
    _add_rule(
        rules_by_id,
        {
            "id": f"inverse:{predicate}:{inverse_predicate}",
            "family": "inverse",
            "predicate": predicate,
            "inverse_predicate": inverse_predicate,
            "enabled": False,
            "rationale": rationale,
        },
    )


def _add_rule(rules_by_id: dict[str, dict], rule: dict) -> None:
    rules_by_id.setdefault(rule["id"], rule)


def _rules_sorted_by_id(rules_by_id: dict[str, dict]) -> list[dict]:
    return [rules_by_id[rule_id] for rule_id in sorted(rules_by_id)]


def _empty_doc() -> dict:
    return {"version": ONTOLOGY_VERSION, "rules": []}


def _triple_set(triples: list[dict]) -> set[tuple[str, str, str]]:
    triple_set = set()
    for triple in triples:
        subject = _triple_endpoint(triple, "subject", "subject_id")
        predicate = triple.get("predicate")
        obj = _triple_endpoint(triple, "object", "object_id")
        if subject is None or obj is None or not isinstance(predicate, str) or not predicate:
            continue
        triple_set.add((subject, predicate, obj))
    return triple_set


def _triple_endpoint(triple: dict, name_key: str, id_key: str) -> str | None:
    if name_key in triple and triple.get(name_key) is not None:
        return str(triple[name_key])
    if id_key in triple and triple.get(id_key) is not None:
        return str(triple[id_key])
    return None


def _edges_by_predicate(triple_set: set[tuple[str, str, str]]) -> dict[str, set[tuple[str, str]]]:
    edges = defaultdict(set)
    for subject, predicate, obj in triple_set:
        edges[predicate].add((subject, obj))
    return dict(edges)


def _symmetric_support(edges: set[tuple[str, str]]) -> tuple[int, str]:
    pairs = set()
    for subject, obj in edges:
        if subject == obj or (obj, subject) not in edges:
            continue
        pairs.add(tuple(sorted((subject, obj))))
    if not pairs:
        return 0, ""
    example = _format_symmetric_example(min(pairs))
    return len(pairs), example


def _inverse_support(
    edges: set[tuple[str, str]],
    predicate: str,
    inverse_edges: set[tuple[str, str]],
    inverse_predicate: str,
) -> tuple[int, str]:
    evidence = []
    for subject, obj in sorted(edges):
        if subject == obj:
            continue
        if (obj, subject) in inverse_edges:
            evidence.append((subject, predicate, obj, inverse_predicate))
    if not evidence:
        return 0, ""
    return len(evidence), _format_inverse_example(evidence[0])


def _transitive_support(edges: set[tuple[str, str]]) -> tuple[int, str]:
    adjacency = defaultdict(set)
    for subject, obj in edges:
        adjacency[subject].add(obj)

    chains = []
    for subject in sorted(adjacency):
        for middle in sorted(adjacency[subject]):
            for obj in sorted(adjacency.get(middle, ())):
                if len({subject, middle, obj}) == 3:
                    chains.append((subject, middle, obj))
    if not chains:
        return 0, ""
    return len(chains), _format_chain_example(chains[0])


def _format_symmetric_example(pair: tuple[str, str]) -> str:
    return f"{pair[0]}<->{pair[1]}"


def _format_inverse_example(evidence: tuple[str, str, str, str]) -> str:
    subject, predicate, obj, inverse_predicate = evidence
    return f"{subject} {predicate} {obj} <-> {obj} {inverse_predicate} {subject}"


def _format_chain_example(chain: tuple[str, str, str]) -> str:
    return f"{chain[0]}->{chain[1]}->{chain[2]}"
