#!/usr/bin/env python3
"""dream_adopt — adopt a saved native review or an explicit legacy decisions file.

Reads a ``decisions.json`` (a worklist whose merge items have an approved
``decision``) and applies it to the palace: each approved merge adds the
synthesised drawer, then deletes the superseded originals. Purely mechanical —
no cognition. ``--dry-run`` prints what would change without writing.

A decision is ``item["decision"]``:
    {"action": "merge", "wing": "...", "room": "...", "text": "...",
     "supersedes": ["<physical id>", ...]}      # or {"action": "skip"}

If a merge decision omits ``wing``/``room``/``supersedes``, they default to the
first member's wing/room and the item's ``supersedes`` list.

Usage:
    "$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --palace ~/.mempalace/palace --decisions decisions.json
    "$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --decisions decisions.json --dry-run
    "$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --decisions decisions.json  # palace_path from config

Select absolute MPY (the provisioned MemPalace interpreter) and DREAM_SCRIPTS
paths. Run from an external session workspace for relative artifact paths.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from typing import Any

import dream_harvest
import dream_palace
import dream_lib as _dream_lib
from dream_lib import (
    apply_contradiction_decisions,
    apply_derive_decisions,
    apply_merge_decisions,
    apply_pattern_decisions,
    apply_prune_decisions,
    apply_reflect_decisions,
    skip_markers_for_rejected_rules,
)
from dream_palace import load_logical_drawers, _palace_embed
from dream_reflect import validate_reflect, is_novel
from dream_metadata import content_hash, is_generated_observation, is_procedural_record, is_control_record, strict_json


def _resolve_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete merge/skip decisions from an adjudicated worklist."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision:
            resolved.append({"action": "skip"})
            continue
        if decision.get("action") != "merge":
            resolved.append({"action": "skip"})
            continue
        members = item.get("members", [])
        first = members[0] if members else {}
        supersedes = decision.get("supersedes") or item.get("supersedes")
        if not supersedes:
            supersedes = [pid for member in members for pid in member.get("member_ids", [member["id"]])]
        resolved.append(
            {
                "action": "merge",
                "wing": decision.get("wing") or first.get("wing"),
                "room": decision.get("room") or first.get("room"),
                "text": decision.get("text", ""),
                "supersedes": supersedes,
                "sources": [
                    {
                        "id": member["id"],
                        "content_hash": member.get("content_hash") or (item.get("content_hashes") or {}).get(member["id"]),
                    }
                    for member in members
                ],
            }
        )
    return resolved


def _candidate_triple_ids(candidate: dict[str, Any]) -> list[Any]:
    triple_ids = list(candidate.get("triple_ids") or [])
    if candidate.get("triple_id") is not None and candidate["triple_id"] not in triple_ids:
        triple_ids.append(candidate["triple_id"])
    return triple_ids


def _candidate_matches(candidate: dict[str, Any], selected: Any) -> bool:
    return selected is not None and selected in [
        candidate.get("object"),
        candidate.get("object_id"),
        candidate.get("triple_id"),
        *list(candidate.get("triple_ids") or []),
    ]


def _valid_selector(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())) or type(value) is int


def _resolve_contradiction_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve public invalidate decisions to exact roots and canonical identities."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "invalidate":
            resolved.append({"action": "skip"})
            continue
        candidates = item.get("candidates", [])
        kept = None
        if "keep" in decision:
            keep = decision["keep"]
            if not _valid_selector(keep):
                raise ValueError("invalid keep selector")
            matches = [c for c in candidates if _candidate_matches(c, keep)]
            if len(matches) != 1:
                raise ValueError("unknown or ambiguous keep selector")
            kept = matches[0]
            if not _candidate_triple_ids(kept):
                raise ValueError("keep candidate has no exact triple IDs")
            new_object = kept.get("object_id") or kept.get("object")
            if not _valid_selector(new_object):
                raise ValueError("keep resolution requires a canonical object identity")
        selected = decision.get("invalidate")
        if selected is None:
            if kept is None:
                raise ValueError("invalidate requires a keep selector or explicit IDs")
            invalidate = [
                triple_id
                for c in candidates
                if c is not kept
                for triple_id in _candidate_triple_ids(c)
            ]
        else:
            if not isinstance(selected, list) or not all(_valid_selector(v) for v in selected):
                raise ValueError("invalidate must be a list of selectors")
            invalidate = []
            for value in selected:
                matches = [c for c in candidates if _candidate_matches(c, value)]
                if len(matches) > 1:
                    raise ValueError("ambiguous invalidate selector")
                if matches:
                    ids = _candidate_triple_ids(matches[0])
                    # An explicit root ID never authorizes its sibling assertions.
                    invalidate.extend([value] if value in ids else ids)
                elif kept is not None:
                    raise ValueError("unknown invalidate selector with keep")
                else:
                    invalidate.append(value)
        invalidate = list(dict.fromkeys(invalidate))
        result = {
            "action": "invalidate",
            "subject": item.get("subject_id") or item["subject"],
            "predicate": item["predicate"],
            "invalidate": invalidate,
        }
        if kept is not None:
            keep_ids = _candidate_triple_ids(kept)
            if set(keep_ids).intersection(invalidate):
                raise ValueError("cannot invalidate the keep candidate")
            old = [c for c in candidates if set(_candidate_triple_ids(c)).intersection(invalidate)]
            if len(old) == 1:
                old_object = old[0].get("object_id") or old[0].get("object")
                if not _valid_selector(old_object):
                    raise ValueError("keep resolution requires canonical object identities")
                result.update(action="supersede", old_object=old_object,
                              new_object=new_object, keep_triple_ids=keep_ids)
        resolved.append(result)
    return resolved


def _first_scope_room(scope: dict[str, Any]) -> str | None:
    rooms = scope.get("rooms")
    if isinstance(rooms, str):
        rooms = [room.strip() for room in rooms.split(",") if room.strip()]
    if rooms:
        return rooms[0]
    return scope.get("room")


def _resolve_pattern_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete pattern surface/skip decisions from an adjudicated worklist."""
    resolved = []
    scope = worklist.get("scope", {})
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "surface":
            resolved.append({"action": "skip"})
            continue
        members = item.get("members", [])
        first = members[0] if members else {}
        evidence = item.get("evidence", {})
        supported_by = list(decision.get("supported_by") or [])
        resolved.append(
            {
                "action": "surface",
                "wing": decision.get("wing") or first.get("wing") or scope.get("wing"),
                "room": decision.get("room") or first.get("room") or _first_scope_room(scope),
                "text": decision["text"],
                "supported_by": supported_by,
                "allowed_support": list(evidence.get("support_ids", [])),
            }
        )
    return resolved


def _resolve_prune_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract concrete prune/keep decisions from an adjudicated worklist."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "prune":
            resolved.append({"action": "keep"})
            continue
        resolved.append(
            {
                "action": "prune",
                "id": decision.get("id") or item["id"],
                "member_ids": decision.get("member_ids") or item.get("member_ids", [item["id"]]),
                "wing": decision.get("wing") or item.get("wing"),
                "room": decision.get("room") or item.get("room"),
                "text": decision.get("text") or item.get("text", ""),
                "content_hash": decision.get("content_hash") or item.get("content_hash"),
                "pinned": decision.get("pinned", item.get("pinned", False)),
                "topic": decision.get("topic") or item.get("topic"),
                "salience": item.get("salience", {}),
            }
        )
    return resolved


def _resolve_derive_decisions(worklist: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract derive decisions while preserving harvested proof/conclusion fields."""
    resolved = []
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if isinstance(decision, dict) and decision.get("action"):
            resolved.append({**item, **decision})
        elif item.get("action"):
            resolved.append(dict(item))
    return resolved


def _resolve_reflect_decisions(worklist):
    """Extract concrete reflect surface/skip decisions from an adjudicated worklist."""
    resolved = []
    scope = worklist.get("scope", {})
    for item in worklist.get("items", []):
        decision = item.get("decision")
        if not decision or decision.get("action") != "surface":
            resolved.append({"action": "skip"})
            continue
        conclusion = decision.get("conclusion") or {}
        resolved.append({
            "action": "surface",
            "reflect_kind": decision.get("reflect_kind") or conclusion.get("kind"),
            "text": conclusion.get("text", ""),
            "conclusion": conclusion,
            "premises": list(decision.get("premises") or []),
            "member_ids": list(item.get("member_ids") or []),
            "members": list(item.get("members") or []),
            "min_support": max(2, int((worklist.get("params") or {}).get("min_support", 3))),
            "evidence": item.get("evidence"),
            "wing": decision.get("wing") or scope.get("wing") or "copilot-mempalace",
            "room": decision.get("room") or "reflections",
            "tunnel": decision.get("tunnel"),
        })
    return resolved


def _recurrence_error(dec, live_by_id):
    import dream_sessions
    sessions = set()
    members = dec.get("members") or []
    if not members or {m["id"] for m in members} != set(dec.get("member_ids") or []):
        return "missing_recurrence_sources"
    for member in members:
        source_id = member["id"]
        if source_id.startswith("session:"):
            session_id = source_id[len("session:"):]
            if "_incremental_sessions" in dec:
                source = dec["_incremental_sessions"].get(source_id)
                if not source or source.get("session_id") != session_id or not source.get("text"):
                    return "missing_recurrence_source"
                text = source["text"]
            else:
                turns = dream_sessions.load_session_turns(session_id)
                text = dream_palace._strip_context_boilerplate(dream_sessions._bounded_text([
                    turn["user_message"] for turn in turns
                    if isinstance(turn.get("user_message"), str) and turn["user_message"]]))
                if not turns or not text:
                    return "missing_recurrence_source"
        else:
            live = live_by_id.get(source_id)
            if live is None or is_generated_observation(live):
                return "invalid_recurrence_source"
            text = live.get("text", "")
            session_id, ambiguous = dream_palace._session_id_state(text)
            if ambiguous or not session_id:
                return "invalid_recurrence_session"
        if member.get("session_id") != session_id or member.get("content_hash") != content_hash(text):
            return "recurrence_source_drift"
        sessions.add(session_id)
    if sessions != set((dec.get("evidence") or {}).get("support_ids") or []):
        return "forged_recurrence_support"
    if len(sessions) < dec["min_support"]:
        return "weak_recurrence"
    return None


def _preflight_reflect_decisions(path, decisions):
    """Validate reflect decisions: grounding + novelty. Fail-closed."""
    drawers = [drawer for drawer in load_logical_drawers(path) if not is_control_record(drawer)]
    live_by_id = {str(d["id"]): d for d in drawers}
    full_by_id = {str(d["id"]): d.get("text", "") for d in drawers}
    existing_vecs = [d.get("embedding") or [] for d in drawers]
    kept, errors = [], []
    for dec in decisions:
        if dec.get("action") != "surface":
            kept.append(dec)
            continue
        if dec.get("reflect_kind") == "converge":
            reason = _recurrence_error(dec, live_by_id)
            if reason:
                errors.append({"reason": reason})
                kept.append({"action": "skip"})
                continue
        else:
            allowed = {str(mid) for mid in (dec.get("member_ids") or [])}
            members_by_id = {mid: full_by_id[mid] for mid in allowed if mid in full_by_id
                             and not is_procedural_record(live_by_id[mid])}
            if "_incremental_memories" in dec:
                members_by_id = {mid: source["text"] for mid, source in dec["_incremental_memories"].items()
                                 if mid in allowed}
            candidate = {"conclusion": dec.get("conclusion"), "premises": dec.get("premises")}
            v = validate_reflect(candidate, members_by_id)
            if not v["ok"]:
                errors.append({"reason": "invalid_reflect", "rejects": v["rejects"]})
                kept.append({"action": "skip"})
                continue
            if dec.get("reflect_kind") == "connect":
                tunnel = dec.get("tunnel") or {}
                if not all(tunnel.get(k) for k in
                           ("source_wing", "source_room", "target_wing", "target_room")):
                    errors.append({"reason": "connect_missing_tunnel"})
                    kept.append({"action": "skip"})
                    continue
        vec = _palace_embed(path, [dec.get("text", "")])[0]
        if not is_novel(vec, existing_vecs):
            errors.append({"reason": "not_novel", "text": dec.get("text")})
            kept.append({"action": "skip"})
            continue
        kept.append(dec)
    return kept, errors


def _task_from_worklist(worklist: dict[str, Any]) -> str:
    """Derive the task key used for dispatch from the worklist.

    The harvest labels contemplate worklists ``task:"contemplate"``; adopt must
    look at item kind to determine the sub-task (e.g. ``"derive"``).
    """
    task = worklist.get("task")
    if task in ("merge", "contradiction", "pattern", "prune"):
        return task
    # "contemplate" and unknown labels: infer from item kind
    items = worklist.get("items", [])
    if items:
        return items[0].get("kind", "derive")
    # empty contemplate worklist — resolve to derive so the branch runs as a no-op
    if task == "contemplate":
        return "derive"
    return task or "merge"


def _default_palace_from_config() -> str | None:
    config_path = os.environ.get("MEMPALACE_CONFIG") or os.path.expanduser("~/.mempalace/config.json")
    try:
        with open(os.path.expanduser(config_path), encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    palace_path = config.get("palace_path") if isinstance(config, dict) else None
    if not isinstance(palace_path, str) or not palace_path.strip():
        return None
    return os.path.expanduser(palace_path)


class _DryRunWriter:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def add_drawer(
        self,
        wing: str,
        room: str,
        content: str,
        added_by: str = "dreaming",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        preview = content.replace("\n", " ")[:60]
        self.planned.append(f"ADD  {wing}/{room}: {preview}...")
        return {"drawer_id": "dry-run"}

    def delete_drawer(self, drawer_id: str) -> Any:
        self.planned.append(f"DEL  {drawer_id}")
        return {"success": True}


class _DryRunTunneler:
    def __init__(self) -> None:
        self.planned: list[str] = []
    def create_tunnel(self, source_wing, source_room, target_wing, target_room, label):
        self.planned.append(f"[dry-run] tunnel {source_wing}/{source_room} -> "
                            f"{target_wing}/{target_room} ({label})")
        return {"ok": True}


class _DryRunKgWriter:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def invalidate_triples(self, triple_ids: list[str], ended: str | None = None) -> int:
        self.planned.append(f"INVALIDATE_TRIPLES {', '.join(str(tid) for tid in triple_ids)}")
        return len(triple_ids)

    def supersede(self, subject, predicate, old_object, new_object, *,
                  old_triple_ids, keep_triple_ids, at=None):
        self.planned.append(
            f"SUPERSEDE {subject} -{predicate}-> {old_object} with {new_object}; "
            f"retire {old_triple_ids}, keep {keep_triple_ids}")
        return {"invalidated": len(old_triple_ids), "retired_ids": list(old_triple_ids),
                "kept_ids": list(keep_triple_ids), "cascaded_ids": [], "survived_ids": []}

    def add_derived(self, conclusion, rule_id, premise_ids, premise_drawer_ids,
                    ontology_version, confidence, valid_from, valid_to) -> dict[str, Any]:
        pred = conclusion.get("predicate", "?")
        subj = conclusion.get("subject") or conclusion.get("subject_id", "?")
        obj = conclusion.get("object") or conclusion.get("object_id", "?")
        self.planned.append(f"DERIVE  {subj} -{pred}-> {obj}  (rule={rule_id})")
        return {"ok": True, "triple_id": "dry-run", "dry_run": True}


class _DryRunArchiver:
    def __init__(self) -> None:
        self.planned: list[str] = []

    def archive_then_delete(self, record: dict[str, Any]) -> dict[str, Any]:
        member_ids = record.get("member_ids", [record["id"]])
        if record.get("reason") == "merge":
            self.planned.append(f"ARCHIVE+DELETE {member_ids}")
        else:
            self.planned.append(f"PRUNE {record['id']} (archive+delete)")
        return {"archived": record["id"], "deleted": record.get("member_ids", [record["id"]])}


def _min_support(worklist: dict[str, Any]) -> int:
    return int((worklist.get("params") or {}).get("min_support", 1))


def _archive_writability_errors(archive_file: str | None) -> list[dict[str, Any]]:
    if archive_file is None:
        return []
    parent = os.path.abspath(os.path.dirname(archive_file) or os.getcwd())
    probe = parent
    while probe and not os.path.exists(probe):
        next_probe = os.path.dirname(probe)
        if next_probe == probe:
            break
        probe = next_probe
    if not os.path.isdir(probe) or not os.access(probe, os.W_OK | os.X_OK):
        return [{"stage": "preflight", "error": f"archive path is not writable: {archive_file}"}]
    return []


def _same_text_drawer_exists(path: str, decision: dict[str, Any]) -> bool:
    text = (decision.get("text") or "").strip()
    if not text:
        return False
    try:
        drawers = dream_palace.load_logical_drawers(path, wing=decision.get("wing"), room=decision.get("room"))
    except Exception:  # noqa: BLE001 - dry-run duplicate checks are best effort
        return False
    return any((drawer.get("text") or "").strip() == text for drawer in drawers)


def _filter_duplicate_adds(path: str, decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filtered = []
    errors = []
    for decision in decisions:
        if decision.get("action") in {"merge", "surface"} and _same_text_drawer_exists(path, decision):
            errors.append({"stage": "duplicate", "error": "drawer text already exists", "decision": decision})
            filtered.append({"action": "skip"})
        else:
            filtered.append(decision)
    return filtered, errors


def _merge_drift_errors(path: str, decision: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    for source in decision.get("sources", []):
        expected = source.get("content_hash")
        if not expected:
            continue
        drawer_id = source["id"]
        try:
            live = dream_palace.load_drawer_by_id(path, drawer_id)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "drift", "error": str(exc), "drawer_id": drawer_id, "decision": decision})
            continue
        if live is None:
            errors.append({"stage": "drift", "error": "drawer missing", "drawer_id": drawer_id, "decision": decision})
        elif live.get("content_hash") != expected:
            errors.append({"stage": "drift", "error": "content hash changed", "drawer_id": drawer_id, "decision": decision})
    return errors


def _preflight_merge_decisions(path: str, decisions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from dream_procedural_palace import live_protected_drawer_ids
    protected = live_protected_drawer_ids(path)
    filtered = []
    errors = []
    for decision in decisions:
        if decision.get("action") != "merge":
            filtered.append(decision)
            continue
        if protected.intersection(decision.get("supersedes") or []):
            errors.append({"stage": "protected", "error": "procedural evidence/event drawer", "decision": decision})
            filtered.append({"action": "skip"})
            continue
        drift_errors = _merge_drift_errors(path, decision)
        if drift_errors:
            errors.extend(drift_errors)
            filtered.append({"action": "skip"})
        else:
            filtered.append(decision)
    return filtered, errors


def _usage_changed_since_harvest(before: dict, after: dict) -> bool:
    if before and not after:
        return True
    if after.get("access_count", 0) != before.get("access_count", 0):
        return True
    if after.get("access_count", 0) == 0:
        return False
    return (
        after.get("last_activated") != before.get("last_activated")
        or any(after.get(key, 0) > before.get(key, 0) for key in ("strength", "stability"))
    )


def _preflight_prune_decisions(path: str, decisions: list[dict[str, Any]],
                              worklist: dict | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from dream_procedural_palace import live_protected_drawer_ids, exclude_protected_drawers
    if not any(d.get("action") == "prune" for d in decisions):
        return decisions, []
    scope = (worklist or {}).get("scope") or {}
    params = (worklist or {}).get("params") or {}
    try:
        protected = live_protected_drawer_ids(path)
        degrees = dream_palace.kg_protection_degree(path)
        usage = dream_palace.load_drawer_usage(path, wing=scope.get("wing"), room=scope.get("room"))
        drawers = dream_palace.load_logical_drawers(path, wing=scope.get("wing"), room=scope.get("room"))
        scored = dream_harvest.score_prune_drawers(exclude_protected_drawers(path, drawers), degrees, usage)
        fresh_by_id = {d["id"]: d for d in scored}
        eligible = {d["id"] for d in _dream_lib.select_prune_candidates(
            scored, v_min=params.get("v_min", dream_harvest.DEFAULT_V_MIN),
            age_floor_days=params.get("age_floor_days", dream_harvest.DEFAULT_AGE_FLOOR_DAYS))}
    except Exception as exc:  # noqa: BLE001
        return (
            [{"action": "keep"} if d.get("action") == "prune" else d for d in decisions],
            [{"stage": "preflight", "error": str(exc), "decision": d} for d in decisions if d.get("action") == "prune"],
        )

    filtered = []
    errors = []
    for decision in decisions:
        if decision.get("action") != "prune":
            filtered.append(decision)
            continue
        drawer_id = decision["id"]
        if protected.intersection([drawer_id, *decision.get("member_ids", [])]):
            errors.append({"stage": "protected", "error": "procedural evidence/event drawer", "decision": decision})
            filtered.append({"action": "keep"})
            continue
        try:
            live = dream_palace.load_drawer_by_id(path, drawer_id)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "drift", "error": str(exc), "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        if live is None:
            errors.append({"stage": "drift", "error": "drawer missing", "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        expected = decision.get("content_hash")
        if expected and live.get("content_hash") != expected:
            errors.append({"stage": "drift", "error": "content hash changed", "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        if protected.intersection([drawer_id, *live.get("member_ids", [])]) or is_procedural_record(live):
            errors.append({"stage": "protected", "error": "procedural evidence/event drawer", "decision": decision})
            filtered.append({"action": "keep"})
            continue
        live_pinned = bool((live.get("metadata") or {}).get("pinned", False))
        live_degree = dream_harvest._degree_for(live, degrees)
        if live_pinned or live_degree > 0:
            reason = "pinned" if live_pinned else "kg-connected"
            errors.append({"stage": "protected", "error": reason, "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        before = (decision.get("salience") or {}).get("usage") or {}
        after = usage.get(drawer_id) or {}
        if _usage_changed_since_harvest(before, after):
            errors.append({"stage": "usage", "error": "usage changed since harvest; re-harvest required",
                           "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        if drawer_id not in eligible:
            errors.append({"stage": "salience", "error": "drawer no longer eligible for prune",
                           "drawer_id": drawer_id, "decision": decision})
            filtered.append({"action": "keep"})
            continue
        if "member_ids" in live and set(live["member_ids"]) != set(decision["member_ids"]):
            errors.append({"stage": "drift", "error": "physical member IDs changed", "decision": decision})
            filtered.append({"action": "keep"})
            continue
        salience = fresh_by_id[drawer_id]["salience"]
        filtered.append({**decision, "pinned": live_pinned, "salience": salience})
    return filtered, errors


def _add_preflight_errors(report: dict[str, Any], errors: list[dict[str, Any]]) -> dict[str, Any]:
    report["errors"].extend(errors)
    return report


def _print_errors(report: dict[str, Any]) -> None:
    for err in report["errors"]:
        print(f"  ERROR {err}", file=sys.stderr)


def _verify_reharvest(task: str, worklist: dict[str, Any], path: str) -> int:
    """Re-run the same harvest after adopt and return the residual item count.

    Reconstructs the harvest scope/params from the worklist so a caller can run
    adopt+verify in a single invocation (one technical step instead of two).
    """
    scope = worklist.get("scope") or {}
    params = worklist.get("params") or {}
    if task == "merge":
        return len(dream_harvest.harvest_merge_worklist(
            path, wing=scope.get("wing"), room=scope.get("room"),
            tau=params.get("tau", 0.9))["items"])
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "verify.json")
        argv = ["--palace", path, "--task", task, "--out", out]
        if task == "pattern":
            if scope.get("wing"):
                argv += ["--wing", scope["wing"]]
            if scope.get("rooms"):
                argv += ["--rooms", ",".join(scope["rooms"])]
            if params.get("min_support") is not None:
                argv += ["--min-support", str(params["min_support"])]
        elif task == "prune":
            if scope.get("wing"):
                argv += ["--wing", scope["wing"]]
            if scope.get("room"):
                argv += ["--room", scope["room"]]
            if params.get("v_min") is not None:
                argv += ["--v-min", str(params["v_min"])]
            if params.get("age_floor_days") is not None:
                argv += ["--age-floor-days", str(params["age_floor_days"])]
        # contradiction takes no scope/param args (KG is palace-global)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = dream_harvest.main(argv)
        if rc != 0:
            raise RuntimeError(f"verification harvest failed with exit code {rc}")
        with open(out, encoding="utf-8") as fh:
            return len(json.load(fh)["items"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--palace", default=None, help="Path to the mempalace palace directory")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--decisions", help="Path to the adjudicated decisions.json")
    source.add_argument("--run-id", help="Adopt the saved native review for this run")
    ap.add_argument("--task", default=None,
                    choices=["merge", "contradiction", "pattern", "prune", "derive", "reflect"],
                    help="Override task (default: derived from worklist)")
    ap.add_argument("--archive-file", default=None,
                    help="Optional JSONL export in addition to the mandatory native archive")
    ap.add_argument("--dry-run", action="store_true", help="Print planned changes; write nothing")
    ap.add_argument("--rules", default=None,
                    help="Explicit legacy ontology input (default: native palace configuration)")
    ap.add_argument("--skips", default=None,
                    help="Optional skip-marker JSONL export (default: native palace markers)")
    ap.add_argument("--max-depth", type=int, default=3,
                    help="Maximum derivation depth for derive (default 3)")
    ap.add_argument("--max-iterations", type=int, default=10,
                    help="Maximum closure iterations for derive (default 10)")
    ap.add_argument("--max-candidates", type=int, default=500,
                    help="Maximum candidates for derive (default 500)")
    ap.add_argument("--verify", action="store_true",
                    help="After adopt, re-harvest and print residual count")
    ap.add_argument("--strict", action="store_true",
                    help="With --verify, return exit code 1 if any residual candidates remain")
    args = ap.parse_args(argv)

    args.palace = args.palace or _default_palace_from_config()
    if not args.palace:
        print(
            "error: --palace omitted and no readable palace_path found in "
            "MEMPALACE_CONFIG or ~/.mempalace/config.json",
            file=sys.stderr,
        )
        return 2

    path = dream_palace.bind_palace(args.palace)
    try:
        if args.run_id:
            import dream_incremental
            worklist = dream_incremental.load_run(path, args.run_id)
        else:
            with open(args.decisions, encoding="utf-8") as fh:
                worklist = strict_json(fh.read())
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"error: cannot load decisions: {exc}", file=sys.stderr)
        return 1
    if "incremental" in worklist:
        import dream_incremental
        try:
            if args.task not in (None, "reflect"):
                raise ValueError("incremental review cannot override its reflection task")
            report = dream_incremental.complete(path, worklist, dry_run=args.dry_run)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"error: incremental adoption failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 0
    task = args.task or _task_from_worklist(worklist)
    if task == "contradiction":
        try:
            contradiction_decisions = _resolve_contradiction_decisions(worklist)
        except ValueError as exc:
            print(f"error: invalid contradiction decision: {exc}", file=sys.stderr)
            return 2

    if args.dry_run:
        if task == "contradiction":
            decisions = contradiction_decisions
            kg_writer = _DryRunKgWriter()
            report = apply_contradiction_decisions(decisions, kg_writer)
            for line in kg_writer.planned:
                print(line)
            print(
                f"[dry-run] would invalidate {report['invalidated']}, skip {report['skipped']}",
                file=sys.stderr,
            )
            _print_errors(report)
            return 1 if report["errors"] else 0

        if task == "pattern":
            decisions = _resolve_pattern_decisions(worklist)
            decisions, duplicate_errors = _filter_duplicate_adds(path, decisions)
            writer = _DryRunWriter()
            report = _add_preflight_errors(
                apply_pattern_decisions(decisions, writer, _min_support(worklist)),
                duplicate_errors,
            )
            for line in writer.planned:
                print(line)
            _print_errors(report)
            print(
                f"[dry-run] would surface {report['surfaced']}, skip {report['skipped']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        if task == "merge":
            decisions = _resolve_decisions(worklist)
            decisions, preflight_errors = _preflight_merge_decisions(path, decisions)
            decisions, duplicate_errors = _filter_duplicate_adds(path, decisions)
            writer: Any = _DryRunWriter()
            archiver = _DryRunArchiver()
            report = _add_preflight_errors(
                apply_merge_decisions(decisions, writer, archiver),
                preflight_errors + duplicate_errors + _archive_writability_errors(args.archive_file),
            )
            for line in writer.planned:
                print(line)
            for line in archiver.planned:
                print(line)
            _print_errors(report)
            print(
                f"[dry-run] would merge {report['merged']}, skip {report['skipped']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        if task == "prune":
            decisions = _resolve_prune_decisions(worklist)
            decisions, preflight_errors = _preflight_prune_decisions(path, decisions, worklist)
            archiver = _DryRunArchiver()
            report = _add_preflight_errors(
                apply_prune_decisions(decisions, archiver),
                preflight_errors + _archive_writability_errors(args.archive_file),
            )
            for line in archiver.planned:
                print(line)
            _print_errors(report)
            print(
                f"[dry-run] would prune {report['pruned']}, keep {report['kept']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        if task == "derive":
            decisions = _resolve_derive_decisions(worklist)
            report, _markers = apply_derive_decisions(decisions, _DryRunKgWriter())
            print(json.dumps(report, indent=2))
            print(
                f"[dry-run] would materialize {report['materialized']}, skip {report['skipped']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        if task == "reflect":
            decisions = _resolve_reflect_decisions(worklist)
            decisions, errs = _preflight_reflect_decisions(path, decisions)
            writer = _DryRunWriter()
            tunneler = _DryRunTunneler()
            report = _add_preflight_errors(
                apply_reflect_decisions(decisions, writer, tunneler=tunneler), errs)
            for line in writer.planned:
                print(line)
            for line in tunneler.planned:
                print(line)
            _print_errors(report)
            print(
                f"[dry-run] would surface {report['surfaced']}, skip {report['skipped']}, "
                f"errors {len(report['errors'])}",
                file=sys.stderr,
            )
            return 1 if report["errors"] else 0

        print(f"unknown dreaming task: {task}", file=sys.stderr)
        return 2

    if task == "contradiction":
        decisions = contradiction_decisions
        kg_writer = dream_palace.KgWriter(path)
        try:
            report = apply_contradiction_decisions(decisions, kg_writer)
        finally:
            kg_writer.close()
        print(
            f"adopted: invalidated {report['invalidated']}, skipped {report['skipped']}, "
            f"facts {len(report['invalidated_facts'])}, errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "pattern":
        decisions = _resolve_pattern_decisions(worklist)
        report = apply_pattern_decisions(decisions, dream_palace.MempalaceWriter(), _min_support(worklist))
        print(
            f"adopted (pattern): surfaced {report['surfaced']}, skipped {report['skipped']}, "
            f"errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "merge":
        decisions = _resolve_decisions(worklist)
        with dream_palace.palace_mutation_lock(path):
            decisions, preflight_errors = _preflight_merge_decisions(path, decisions)
            writer = dream_palace.MempalaceWriter()
            archiver = dream_palace.Archiver(path, writer=writer, archive_path=args.archive_file)
            report = _add_preflight_errors(
                apply_merge_decisions(decisions, writer, archiver),
                preflight_errors,
            )
        print(
            f"adopted: merged {report['merged']}, skipped {report['skipped']}, "
            f"deleted {len(report['deleted'])}, errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "prune":
        decisions = _resolve_prune_decisions(worklist)
        with dream_palace.palace_mutation_lock(path):
            decisions, preflight_errors = _preflight_prune_decisions(path, decisions, worklist)
            report = _add_preflight_errors(
                apply_prune_decisions(decisions, dream_palace.Archiver(path, archive_path=args.archive_file)),
                preflight_errors,
            )
        print(
            f"adopted (prune): pruned {report['pruned']}, kept {report['kept']}, "
            f"errors {len(report['errors'])}",
            file=sys.stderr,
        )
    elif task == "derive":
        decisions = _resolve_derive_decisions(worklist)
        try:
            if args.rules:
                import dream_ontology
                if not os.path.isfile(args.rules):
                    raise FileNotFoundError(f"explicit ontology input is missing: {args.rules}")
                imported = dream_ontology.read_ontology_doc(args.rules)
                dream_ontology.write_ontology_doc(None, imported, palace=path)
            if args.skips and os.path.exists(args.skips):
                imported_skips = dream_palace.load_skip_markers(args.skips)
                dream_palace.append_skip_markers(None, imported_skips, palace=path)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"error: native derive state import failed: {exc}", file=sys.stderr)
            return 1
        writer = dream_palace.KgDeriveWriter(args.palace)
        try:
            report, skip_markers = apply_derive_decisions(decisions, writer)
        finally:
            writer.close()
        onto_ver = worklist.get("ontology_version") or _dream_lib.ontology_version(
            dream_palace.load_ontology_config(args.rules, palace=path))
        rejected = [(d.get("rule") or {}).get("id") for d in decisions if d.get("action") == "reject_rule"]
        skip_markers += skip_markers_for_rejected_rules(
            worklist.get("items", []), [r for r in rejected if r], onto_ver)
        skips_path = args.skips
        dream_palace.append_skip_markers(skips_path, skip_markers, palace=path)
        print(json.dumps(report, indent=2))
        _print_errors(report)
        print(
            f"adopt: task=derive materialized={report['materialized']} "
            f"skipped={report['skipped']} errors={len(report['errors'])}",
            file=sys.stderr,
        )
        if args.verify:
            rules = dream_palace.load_ontology_config(args.rules, palace=path)
            triples = dream_palace.load_premises(path, purpose="durable")
            residual = _dream_lib.deductive_closure(
                triples, rules, max_depth=args.max_depth,
                max_iterations=args.max_iterations, max_candidates=args.max_candidates)
            residual = _dream_lib.filter_skipped(
                residual, dream_palace.load_skip_markers(skips_path, palace=path), _dream_lib.ontology_version(rules))
            print(f"verify: {len(residual)} residual candidate(s)", file=sys.stderr)
            if args.strict and residual:
                return 1
        return 1 if report["errors"] else 0
    elif task == "reflect":
        decisions = _resolve_reflect_decisions(worklist)
        decisions, errs = _preflight_reflect_decisions(path, decisions)
        report = _add_preflight_errors(
            apply_reflect_decisions(decisions, dream_palace.MempalaceWriter(),
                                    tunneler=dream_palace.MempalaceTunneler()), errs)
        _print_errors(report)
        print(
            f"adopted (reflect): surfaced {report['surfaced']}, skipped {report['skipped']}, "
            f"errors {len(report['errors'])}",
            file=sys.stderr,
        )
    else:
        print(f"unknown dreaming task: {task}", file=sys.stderr)
        return 2

    if args.verify and task in ("merge", "contradiction", "pattern", "prune"):
        try:
            residual = _verify_reharvest(task, worklist, path)
        except Exception as exc:  # CLI boundary: failed scans are never convergence.
            report["errors"].append({"stage": "verify", "error": str(exc)})
            _print_errors(report)
            return 1
        print(f"verify: {residual} residual {task} candidate(s) after re-harvest", file=sys.stderr)
        if args.strict and residual:
            _print_errors(report)
            return 1

    _print_errors(report)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
