"""Strict, storage-independent guidance transport validation for all consumers."""
from __future__ import annotations

from datetime import datetime
import math
import re

from dream_metadata import strict_json
from dream_procedural import Policy, repository_key, utc_datetime


def validate_guidance(raw: bytes, repository: str, *, mode="established",
                      as_of: datetime | None = None) -> dict:
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    def natural(value):
        return type(value) is int and value >= 0

    require(isinstance(raw, bytes) and len(raw) <= 8192, "guidance exceeds byte budget")
    serialized = raw.decode("utf-8")
    require(serialized.endswith("\n") and len(serialized) <= 6000,
            "guidance exceeds character budget or lacks serialized newline")
    data = strict_json(serialized)
    fields = {"policy_version", "as_of", "repository", "status", "item_count", "omitted_count",
              "rules", "anti_patterns", "trials"}
    require(isinstance(data, dict) and data.keys() == fields, "invalid guidance fields")
    require(repository_key(repository) == repository and data["repository"] == repository,
            "guidance repository scope mismatch")
    require(mode in {"established", "approved_candidate_trial"}, "invalid guidance mode")
    require(data["policy_version"] == Policy().version, "unsupported guidance policy")
    require(isinstance(data["as_of"], str), "invalid guidance time")
    generated_at = utc_datetime(data["as_of"])
    if as_of is not None:
        require(generated_at <= utc_datetime(as_of), "future guidance")
    require(data["status"] in ("ok", "no_rules", "no_eligible_rules"), "guidance status unavailable")
    require(all(isinstance(data[key], list) for key in ("rules", "anti_patterns", "trials")),
            "invalid guidance sections")
    require(mode != "established" or not data["trials"], "automatic candidate trials forbidden")
    items = data["rules"] + data["anti_patterns"] + data["trials"]
    require(natural(data["item_count"]) and data["item_count"] == len(items) <= 5
            and natural(data["omitted_count"]), "invalid combined guidance count")
    require((data["status"] == "ok") == bool(items), "inconsistent guidance status")
    ids = set()
    item_fields = {"rule_id", "rule_type", "statement", "applies_when", "exceptions", "maturity",
                   "effective_score", "relevance", "latest_validation", "evidence", "delivery"}
    for section in ("rules", "anti_patterns", "trials"):
        for item in data[section]:
            require(isinstance(item, dict) and item.keys() == item_fields, "invalid guidance item fields")
            require(text(item["rule_id"]) and re.fullmatch(r"proc:[0-9a-f]{64}", item["rule_id"])
                    and item["rule_id"] not in ids, "invalid or duplicate guidance rule ID")
            ids.add(item["rule_id"])
            require(item["rule_type"] in {"rule", "anti_pattern"}
                    and (section == "trials" or
                         item["rule_type"] == ("rule" if section == "rules" else "anti_pattern")),
                    "guidance rule section mismatch")
            require(all(text(item[k]) for k in ("statement", "applies_when")),
                    "incomplete guidance rule")
            trial = section == "trials"
            require(item["maturity"] in ({"candidate"} if trial else {"established", "proven"})
                    and item["delivery"] == ("approved_candidate_trial" if trial else "guidance"),
                    "ineligible guidance mode")
            require(isinstance(item["exceptions"], list)
                    and all(isinstance(value, str) for value in item["exceptions"]),
                    "invalid rule exceptions")
            require(all(type(item[k]) in (int, float) and math.isfinite(item[k])
                        for k in ("effective_score", "relevance")), "invalid guidance scores")
            require(isinstance(item["latest_validation"], str)
                    and utc_datetime(item["latest_validation"]) <= generated_at,
                    "future or unavailable guidance validation")
            require(isinstance(item["evidence"], list) and 1 <= len(item["evidence"]) <= 3
                    and all(isinstance(ref, dict) and ref.keys() == {"source_kind", "source_id"}
                            and ref["source_kind"] in {"drawer", "session_turn"}
                            and text(ref["source_id"]) for ref in item["evidence"]),
                    "guidance evidence unavailable")
    return data
