"""Cooperative native sessions: durable observations, never runtime supervision.

The authenticated local transport is the trust boundary. Session UUIDs identify
the opting-in parent, not credentials or physical execution capabilities.
"""

from copy import deepcopy
from functools import lru_cache
from uuid import UUID, uuid5

from jsonschema import Draft202012Validator

from .model import identifier, instant, integer, object_fields, require, text


MODE = "cooperative_native"
ACTIONS = ("bootstrap", "expand", "update", "claim", "start", "checkpoint", "complete",
           "request_changes", "release", "reconcile", "resume", "cancel", "goal_close")


def _object(properties, required=()):
    return {"type": "object", "properties": properties, "required": sorted(required),
            "additionalProperties": False}


def schema():
    """One action-specific schema shared by HTTP admission and journal decisions."""
    string = {"type": "string", "minLength": 1}
    uuid = {"type": "string",
            "pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}
    positive = {"type": "integer", "minimum": 1}
    refs = {"type": "array", "items": {**string, "maxLength": 2048},
            "minItems": 1, "maxItems": 20, "uniqueItems": True}
    content = {"title": string, "description": {"type": "string"},
               "acceptance": {"type": "string"},
               "priority": {"type": "integer", "minimum": 0, "maximum": 4},
               "hold_reason": {"type": ["string", "null"]},
               "deferred_until": {"type": ["string", "null"]}}
    scope = {"project": string, "goal_id": string}
    task = {**scope, "task_id": string, "expected_version": positive}
    tokens = {**task, "attempt_id": string, "claim_generation": positive,
              "native_agent_id": {"type": ["string", "null"], "minLength": 1}}
    running = {**tokens, "native_agent_id": string}
    outcome = {"summary": string, "evidence": refs, "parent_acceptance": string}
    finding_id = {**string, "maxLength": 128}
    findings = {"type": "array", "minItems": 1, "maxItems": 20, "items": _object({
        "id": finding_id, "criterion": {**string, "maxLength": 2048},
        "feedback": {**string, "maxLength": 2048},
    }, ("id", "criterion", "feedback"))}
    resolution = _object({
        "review_id": uuid,
        "findings": {"type": "array", "minItems": 1, "maxItems": 20, "items": _object({
            "id": finding_id, "summary": {**string, "maxLength": 8192}, "evidence": refs,
        }, ("id", "summary", "evidence"))},
    }, ("review_id", "findings"))
    observed = {"reason": string, "observations": refs}
    specs = {
        "bootstrap": _object({
            "project": string, **{k: content[k] for k in
                                 ("title", "description", "acceptance", "priority")},
            "planning_task": _object(content, ("title", "description", "acceptance")),
            "goal_policy": _object({
                "scope": string, "max_tasks": {"type": "integer", "minimum": 1, "maximum": 10000},
                "max_batch": {"type": "integer", "minimum": 1, "maximum": 50},
            }, ("scope",)),
            "plan_references": refs,
        }, ("project", "title", "description", "acceptance", "planning_task", "goal_policy")),
        "claim": _object(task, task),
        "update": _object({**task, "patch": _object(content)}, set(task) | {"patch"}),
        "cancel": _object({**task, **observed}, set(task) | set(observed)),
        "start": _object(running, running),
        "checkpoint": _object({**running, "checkpoint_sequence": positive, "reference": string},
                              set(running) | {"checkpoint_sequence", "reference"}),
        "request_changes": _object({
            **running, "summary": {**string, "maxLength": 8192}, "evidence": refs,
            "next_action": {"enum": ["rework", "hold"]}, "findings": findings,
        }, set(running) | {"summary", "evidence", "next_action", "findings"}),
        "complete": {"oneOf": [
            _object({**running, **outcome, "review_resolution": resolution}, set(running) | set(outcome)),
            _object({**task, **outcome}, set(task) | set(outcome)),
        ]},
        "release": _object({**tokens, **observed}, set(tokens) | set(observed)),
        "reconcile": _object({**tokens, **observed,
                              "decision": {"enum": ["hold", "retry", "cancel"]}},
                             set(tokens) | set(observed) | {"decision"}),
        "resume": _object({
            **scope, "expected_version": positive, "expected_session_id": uuid, **observed,
        }, set(scope) | {"expected_version", "expected_session_id"} | set(observed)),
        "goal_close": _object({
            **scope, "expected_version": positive, "expected_graph_revision": positive, **outcome,
        }, set(scope) | {"expected_version", "expected_graph_revision"} | set(outcome)),
        "expand": _object({
            **scope, "expected_graph_revision": positive, "source_task_id": string,
            "expected_version": positive, "attempt_id": string, "claim_generation": positive,
            "native_agent_id": string,
            "source_disposition": {"enum": ["continue", "yield", "complete"]},
            "checkpoint": _object({"sequence": positive, "reference": string}, ("sequence", "reference")),
            **observed, **outcome, "review_resolution": resolution,
            "tasks": {"type": "array", "maxItems": 50, "items": _object({
                **content, "kind": {"enum": ["task", "epic"]}, "intent_key": string,
                "admitted": {"type": "boolean"}, "reuse_task_id": string, "expected_version": positive,
            }, ("intent_key",))},
            "edges": {"type": "array", "maxItems": 200, "items": _object({
                "source": string, "target": string,
                "edge_type": {"enum": ["blocks", "parent_child", "related", "discovered_from",
                                      "duplicates", "supersedes"]},
            }, ("source", "target", "edge_type"))},
        }, set(scope) | {"expected_graph_revision", "source_disposition", "tasks"}),
    }
    return {
        **_object({"action": {"enum": list(ACTIONS)}, "session_id": uuid, "command_id": uuid,
                   "expected_epoch": uuid, "payload": {"type": "object"}},
                  ("action", "session_id", "command_id", "expected_epoch", "payload")),
        "oneOf": [{"properties": {"action": {"const": action}, "payload": payload}}
                  for action, payload in specs.items()],
    }


@lru_cache(maxsize=1)
def _validator():
    request = schema()
    del request["properties"]["expected_epoch"]
    request["required"].remove("expected_epoch")
    request["properties"]["operation"] = {"const": "native"}
    request["required"].append("operation")
    return Draft202012Validator(request)


def validate_command(command):
    if command.get("operation") == "native_interrupt":
        fields = {"operation", "command_id", "task_id", "expected_version", "attempt_id",
                  "claim_generation", "reason", "observations"}
        object_fields(command, fields, fields, "native interruption")
        return
    require(next(_validator().iter_errors(command), None) is None,
            "Arguments do not match the native action schema")
    identifier(command["session_id"], "session_id")


def _observations(reason, observations):
    from .domain import _strings
    text(reason, "reason", 8192, byte_limit=True)
    return _strings(observations, "observations", nonempty=True)


def _tokens(task, command):
    attempt = task["attempt"]
    require(attempt is not None and command.get("attempt_id") == attempt["id"]
            and type(command.get("claim_generation")) is int
            and command["claim_generation"] == task["claim_generation"],
            "Native attempt is superseded", "stale_generation")
    return attempt


def live_attempt(task, command, *, starting=False, allow_inherited=False):
    attempt = _tokens(task, command)
    require(task["status"] == "in_progress"
            and ((attempt["owner"] == command["session_id"]
                  and attempt["session_generation"] == command["session_generation"])
                 or allow_inherited),
            "Cooperative ownership is uncertain or superseded", "stale_generation")
    if starting:
        require(attempt["status"] == "preparing" and attempt["native_agent_id"] is None,
                "Native attempt is already bound", "invalid_transition")
    else:
        require(command.get("native_agent_id") == attempt["native_agent_id"],
                "Native agent association is stale", "stale_generation")
    return attempt


def interrupt(task, at, reason, observations):
    evidence = _observations(reason, observations)
    task.pop("assignee", None)
    task["attempt"]["status"] = "revoked"
    task["status"] = "recovering"
    task["recovery"] = {"reason": reason, "observations": evidence, "started_at": at,
                        "barrier_satisfied": False, "mode": MODE}


def pending_review(task):
    return task.get("native", {}).get("review", {}).get("status") == "changes_requested"


def _findings(value, *, resolved=False):
    from .domain import _strings
    require(type(value) is list and 1 <= len(value) <= 20, "Review requires 1-20 findings")
    fields = {"id", "summary", "evidence"} if resolved else {"id", "criterion", "feedback"}
    indexed = {}
    for finding in value:
        object_fields(finding, fields, fields, "review finding")
        key = text(finding["id"], "finding id", 128, byte_limit=True)
        require(key not in indexed, "Finding IDs must be unique")
        if resolved:
            text(finding["summary"], "finding summary", 8192, byte_limit=True)
            _strings(finding["evidence"], "finding evidence", nonempty=True)
        else:
            text(finding["criterion"], "finding criterion", 2048, byte_limit=True)
            text(finding["feedback"], "finding feedback", 2048, byte_limit=True)
        indexed[key] = finding
    return indexed


def _resolution(review, value):
    fields = {"review_id", "findings"}
    object_fields(value, fields, fields, "review resolution")
    identifier(value["review_id"], "review_id")
    require(value["review_id"] == review["id"], "Resolution must name the current review")
    findings = _findings(value["findings"], resolved=True)
    require(set(findings) == {finding["id"] for finding in review["findings"]},
            "Resolution must cover every current finding exactly once")


def _request_changes(task, command, at):
    from .domain import _enum, _strings
    attempt = live_attempt(task, command)
    require(attempt["status"] == "running", "Review requires running work", "invalid_transition")
    summary = text(command["summary"], "summary", 8192, byte_limit=True)
    evidence = _strings(command["evidence"], "evidence", nonempty=True)
    next_action = _enum(command["next_action"], {"rework", "hold"}, "next_action")
    findings = _findings(command["findings"])
    if pending_review(task):
        require(all(previous["id"] in findings
                    and findings[previous["id"]]["criterion"] == previous["criterion"]
                    for previous in task["native"]["review"]["findings"]),
                "Outstanding finding IDs and criteria must be retained")
    task["native"]["review"] = {
        "id": command["command_id"], "status": "changes_requested",
        "summary": summary, "evidence": evidence, "next_action": next_action,
        "findings": deepcopy(command["findings"]), "actor": command["session_id"], "at": at,
        "attempt_id": attempt["id"], "claim_generation": task["claim_generation"],
    }
    if next_action == "hold":
        interrupt(task, at, summary, evidence)


def complete_task(state, task, command, at, summary, evidence):
    from .domain import TERMINAL, _wait_reasons
    acceptance = text(command.get("parent_acceptance"), "parent_acceptance", 8192, byte_limit=True)
    require(task["admitted"] and not _wait_reasons(state, task, at),
            "Task has unresolved gates", "not_ready")
    if task["kind"] == "task":
        attempt = live_attempt(task, command)
        require(attempt["status"] == "running", "Native attempt has not started", "invalid_transition")
        if pending_review(task):
            review = task["native"]["review"]
            _resolution(review, command.get("review_resolution"))
            review["resolution"] = {
                **deepcopy(command["review_resolution"]), "actor": command["session_id"], "at": at,
                "attempt_id": attempt["id"], "claim_generation": task["claim_generation"],
            }
            review["status"] = "accepted"
        else:
            require("review_resolution" not in command, "No pending review to resolve")
        attempt["status"] = "completed"
        task.pop("assignee", None)
    else:
        require("review_resolution" not in command, "Epics cannot resolve task reviews")
        require(not {"attempt_id", "claim_generation", "native_agent_id"} & command.keys(),
                "Epics cannot carry execution tokens")
        require(task["status"] == "open", "Epic is not open", "invalid_transition")
        require(all(state.tasks[e["target"]]["status"] in TERMINAL for e in state.edges
                    if e["edge_type"] == "parent_child" and e["source"] == task["id"]),
                "Epic has unfinished children", "not_ready")
    task["status"] = "closed"
    task["completion"] = {"summary": summary, "evidence": evidence, "at": at,
                          "actor": command["session_id"]}
    task["native"]["parent_acceptance"] = acceptance


def execute(state, command, at):
    from .domain import (CONTENT, TERMINAL, _bootstrap, _check_task_input, _checkpoint, _close_goal,
                         _complete, _enum, _expand, _expected, _goal, _strings, _task, task_eligibility)
    require(state.configuration is not None, "Authority must be created first", "invalid_transition")
    touched = set()
    if command["operation"] == "native_interrupt":
        task = _task(state, command["task_id"])
        require(task.get("coordination_mode") == MODE, "Not native work", "coordination_mode_mismatch")
        _expected(task, command["expected_version"])
        _tokens(task, command)
        require(task["status"] == "in_progress", "Native attempt is not active", "invalid_transition")
        interrupt(task, at, command["reason"], command["observations"])
        return "NativeInterrupted", {task["id"]}, {"task_id": task["id"], "coordination_mode": MODE}

    session, action = command["session_id"], command["action"]
    payload = command["payload"]
    fields = {**deepcopy(payload), "command_id": command["command_id"],
              "session_id": session, "actor": session}
    response = {"session_id": session, "coordination_mode": MODE}
    if action == "bootstrap":
        references = (_strings(payload["plan_references"], "plan_references", nonempty=True)
                      if "plan_references" in payload else [])
        native = {"session_id": session, "issuing_session_id": session,
                  "session_generation": 1, "plan_references": references}
        response.update(_bootstrap(state, fields, at, touched, native=native))
        return "NativeBootstrapped", touched, response

    goal = _goal(state, payload["goal_id"])
    require(goal.get("coordination_mode") == MODE and goal["project"] == payload["project"],
            "Goal is outside this cooperative scope", "not_owner")
    fields["session_generation"] = goal["native"]["session_generation"]
    response["goal_id"] = goal["id"]
    if action == "resume":
        _expected(goal, payload["expected_version"])
        require(goal["native"]["session_id"] == payload["expected_session_id"],
                "Current parent session does not match", "not_owner")
        require(session != payload["expected_session_id"], "Resume requires an explicit new session")
        observations = _observations(payload["reason"], payload["observations"])
        goal["native"]["session_id"] = session
        goal["native"]["session_generation"] = integer(
            goal["native"]["session_generation"] + 1, "session_generation", 1)
        touched.add(goal["id"])
        goal["native"]["transfer"] = {"from_session_id": payload["expected_session_id"],
                                     "reason": payload["reason"], "observations": observations, "at": at}
        return "NativeResumed", touched, response

    require(goal["native"]["session_id"] == session,
            "Session does not own this opted-in goal", "not_owner")
    if action == "expand":
        source_id = payload.get("source_task_id")
        disposition = payload["source_disposition"]
        if source_id is None:
            require("native_agent_id" not in payload, "No source agent to bind")
        if disposition != "yield":
            require("observations" not in payload, "Only yielding expansion accepts observations")
        if disposition == "complete":
            text(payload.get("parent_acceptance"), "parent_acceptance", 8192, byte_limit=True)
        else:
            require("parent_acceptance" not in payload, "Only completion accepts parent acceptance")
            require("review_resolution" not in payload, "Only completion accepts review resolution")
        response.update(_expand(state, fields, at, touched, native=True))
    elif action == "goal_close":
        response.update(_close_goal(state, fields, at, touched, native=True))
    else:
        task = _task(state, payload["task_id"])
        require(task.get("coordination_mode") == MODE and task["goal_id"] == goal["id"]
                and task["project"] == payload["project"],
                "Task is outside this cooperative scope", "not_owner")
        require(task["kind"] == "task"
                or action in {"complete", "cancel", "update"} and task["id"] != goal["id"],
                "Use goal_close for roots; epics cannot execute", "invalid_transition")
        _expected(task, payload["expected_version"])
        task["native"]["session_id"] = session
        task["native"]["session_generation"] = goal["native"]["session_generation"]
        touched.add(task["id"])
        response["task_id"] = task["id"]
        if action == "update":
            require(task["status"] == "open" and task["recovery"] is None and "assignee" not in task,
                    "Only open work can be updated without attempt reconciliation", "invalid_transition")
            patch = object_fields(payload["patch"], CONTENT, name="patch")
            require(bool(patch), "Patch must not be empty")
            candidate = {**task, **deepcopy(patch)}
            _check_task_input(state, candidate)
            state.tasks[task["id"]] = candidate
        elif action == "cancel":
            require(task["status"] == "open" and task["recovery"] is None and "assignee" not in task,
                    "Only open work can be cancelled without attempt reconciliation", "invalid_transition")
            _observations(payload["reason"], payload["observations"])
            children = [state.tasks[edge["target"]] for edge in state.edges
                        if edge["edge_type"] == "parent_child" and edge["source"] == task["id"]]
            require(all(child["status"] in TERMINAL and child["recovery"] is None for child in children),
                    "Cancel or complete nested children explicitly first", "not_ready")
            task["status"], task["cancellation_reason"] = "cancelled", payload["reason"]
        elif action == "complete" and task["kind"] == "epic":
            _complete(state, task, fields, at, native=True)
        elif action == "claim":
            eligibility = task_eligibility(state, task, at)
            require(eligibility["ready"], "Task is not ready", "not_ready", reasons=eligibility["reasons"])
            task["claim_generation"] += 1
            task["lease_revision"] += 1
            previous = task["attempt"]
            task["attempt"] = {
                "id": "att_" + str(uuid5(UUID(command["command_id"]), task["id"])),
                "number": task["claim_generation"], "owner": session, "supervisor_id": None,
                "session_generation": goal["native"]["session_generation"],
                "native_agent_id": None, "started_at": at, "status": "preparing",
                "progress_deadline": None, "hard_deadline": None,
                "checkpoint": deepcopy(previous["checkpoint"]) if previous else None,
                "failure_reason": None, "resource_fences": {}, "preparation": None, "settlement": None,
            }
            task["status"], task["assignee"] = "in_progress", session
        elif action == "reconcile":
            attempt = _tokens(task, fields)
            require(payload["native_agent_id"] == attempt["native_agent_id"],
                    "Native agent association is stale", "stale_generation")
            require(task["status"] == "recovering", "Release uncertain work before reconciliation",
                    "invalid_transition")
            observations = _observations(payload["reason"], payload["observations"])
            decision = _enum(payload["decision"], {"hold", "retry", "cancel"}, "decision")
            task["native"]["reconciliation"] = {
                "reason": payload["reason"], "observations": observations, "decision": decision, "at": at,
            }
            if decision != "hold":
                task["recovery"] = None
                task["status"] = "open" if decision == "retry" else "cancelled"
                if decision == "cancel":
                    task["cancellation_reason"] = payload["reason"]
        else:
            attempt = live_attempt(task, fields, starting=action == "start",
                                   allow_inherited=action == "release")
            if action == "start":
                attempt["native_agent_id"] = text(payload["native_agent_id"], "native_agent_id")
                attempt["status"] = "running"
            elif action == "checkpoint":
                require(attempt["status"] == "running", "Progress requires started work", "invalid_transition")
                _checkpoint(task, payload["checkpoint_sequence"], payload["reference"], at)
            elif action == "complete":
                _complete(state, task, fields, at, native=True)
            elif action == "request_changes":
                _request_changes(task, fields, at)
            elif action == "release":
                interrupt(task, at, payload["reason"], payload["observations"])
    return "Native" + action.title().replace("_", ""), touched, response


def validate_task(state, task):
    """Validate native snapshots without interpreting observations as proof."""
    from .domain import _enum, _goal, _strings
    native = task["native"]
    object_fields(native, {"session_id", "issuing_session_id", "session_generation",
                           "plan_references", "parent_acceptance",
                           "transfer", "reconciliation", "review"},
                  {"session_id", "issuing_session_id", "session_generation", "plan_references"},
                  "native session")
    identifier(native["session_id"], "session_id")
    identifier(native["issuing_session_id"], "issuing_session_id")
    integer(native["session_generation"], "session_generation", 1)
    _strings(native["plan_references"], "plan_references")
    if "parent_acceptance" in native:
        text(native["parent_acceptance"], "parent_acceptance", 8192, byte_limit=True)
    for field in ("transfer", "reconciliation"):
        if field not in native:
            continue
        record = native[field]
        keys = {"reason", "observations", "at",
                "from_session_id" if field == "transfer" else "decision"}
        object_fields(record, keys, keys, field)
        _observations(record["reason"], record["observations"])
        require(instant(record["at"]) <= instant(state.last_event_at), "Future native observation")
        if field == "transfer":
            identifier(record["from_session_id"], "from_session_id")
        else:
            _enum(record["decision"], {"hold", "retry", "cancel"}, "decision")
    goal = _goal(state, task["goal_id"], open_only=False)
    require(goal.get("coordination_mode") == MODE
            and goal["native"]["issuing_session_id"] == native["issuing_session_id"]
            and native["session_generation"] <= goal["native"]["session_generation"],
            "Native session/goal mismatch")
    require(task["lease_expires_at"] is None and task["retry_not_before"] is None
            and task["automatic_retries_used"] == task["retry_budget_granted"] == 0
            and task["escalation"] is None, "Cooperative work cannot have managed leases or retries")
    attempt = task["attempt"]
    require((attempt is None) == (task["claim_generation"] == 0), "Attempt generation mismatch")
    if attempt is None:
        require(task["status"] != "in_progress" and task["recovery"] is None,
                "Active work requires an attempt")
    else:
        fields = {"id", "number", "owner", "supervisor_id", "session_generation",
                  "native_agent_id", "started_at", "status",
                  "progress_deadline", "hard_deadline", "checkpoint", "failure_reason",
                  "resource_fences", "preparation", "settlement"}
        object_fields(attempt, fields, fields, "native attempt")
        require(type(attempt["id"]) is str and attempt["id"].startswith("att_"), "Invalid attempt")
        identifier(attempt["id"][4:], "attempt UUID")
        identifier(attempt["owner"], "owner")
        integer(attempt["number"], "attempt number", 1)
        integer(attempt["session_generation"], "session_generation", 1)
        require(attempt["number"] == task["claim_generation"], "Attempt generation mismatch")
        require(attempt["session_generation"] <= goal["native"]["session_generation"],
                "Attempt belongs to a future parent session generation")
        require(all(attempt[key] is None for key in
                    ("supervisor_id", "progress_deadline", "hard_deadline", "preparation",
                     "settlement", "failure_reason")) and attempt["resource_fences"] == {},
                "Native observations cannot claim managed execution guarantees")
        require(instant(task["created_at"]) <= instant(attempt["started_at"])
                <= instant(state.last_event_at), "Invalid attempt timestamp")
        if attempt["native_agent_id"] is not None:
            text(attempt["native_agent_id"], "native_agent_id")
        _enum(attempt["status"], {"preparing", "running", "completed", "revoked"}, "attempt status")
        if task["status"] == "in_progress":
            require(attempt["owner"] == native["session_id"] == task["assignee"]
                    and attempt["session_generation"] == native["session_generation"]
                    and attempt["status"] in {"preparing", "running"}, "Invalid active native owner")
        else:
            require(attempt["status"] in {"revoked", "completed"}, "Inactive native attempt is active")
        require(attempt["status"] not in {"running", "completed"} or attempt["native_agent_id"] is not None,
                "Started native work requires an observed agent association")
        checkpoint = attempt["checkpoint"]
        if checkpoint is not None:
            object_fields(checkpoint, {"sequence", "reference", "at"}, {"sequence", "reference", "at"},
                          "checkpoint")
            integer(checkpoint["sequence"], "sequence", 1)
            text(checkpoint["reference"], "reference", 2048, byte_limit=True)
            require(instant(checkpoint["at"]) <= instant(state.last_event_at), "Future checkpoint")
    recovery = task["recovery"]
    require((recovery is not None) == (task["status"] == "recovering"), "Recovery mismatch")
    require(task["status"] != "quarantined", "Native work has no managed quarantine")
    if recovery is not None:
        keys = {"reason", "observations", "started_at", "barrier_satisfied", "mode"}
        object_fields(recovery, keys, keys, "native recovery")
        _observations(recovery["reason"], recovery["observations"])
        instant(recovery["started_at"])
        require(recovery["mode"] == MODE and recovery["barrier_satisfied"] is False,
                "Native reconciliation is not a physical barrier")
    if task["status"] == "closed":
        text(native.get("parent_acceptance"), "parent_acceptance", 8192, byte_limit=True)
    if "review" in native:
        _validate_review(state, task)


def _review_provenance(state, task, record):
    identifier(record["actor"], "review actor")
    integer(record["claim_generation"], "review claim_generation", 1, task["claim_generation"])
    attempt_id = text(record["attempt_id"], "review attempt_id")
    require(attempt_id.startswith("att_"), "Invalid review attempt")
    identifier(attempt_id[4:], "review attempt UUID")
    at = instant(record["at"])
    require(instant(task["created_at"]) <= at <= instant(state.last_event_at),
            "Invalid review timestamp")
    attempt = task["attempt"]
    require(attempt is not None, "Review requires an attempt")
    if record["claim_generation"] == task["claim_generation"]:
        require(attempt_id == attempt["id"] and record["actor"] == attempt["owner"]
                and attempt["native_agent_id"] is not None and attempt["status"] != "preparing"
                and at >= instant(attempt["started_at"]), "Review attempt mismatch")
    else:
        require(attempt_id != attempt["id"] and at <= instant(attempt["started_at"]),
                "Review predates the current attempt")


def _validate_review(state, task):
    from .domain import _enum, _strings
    require(task["kind"] == "task", "Only leaf tasks have reviews")
    review = task["native"]["review"]
    fields = {"id", "status", "summary", "evidence", "next_action", "findings",
              "actor", "at", "attempt_id", "claim_generation"}
    object_fields(review, fields | {"resolution"}, fields, "native review")
    identifier(review["id"], "review id")
    _enum(review["status"], {"changes_requested", "accepted"}, "review status")
    text(review["summary"], "review summary", 8192, byte_limit=True)
    _strings(review["evidence"], "review evidence", nonempty=True)
    _enum(review["next_action"], {"rework", "hold"}, "review next_action")
    _findings(review["findings"])
    _review_provenance(state, task, review)
    if review["status"] == "changes_requested":
        require("resolution" not in review and task["status"] != "closed"
                and task["attempt"]["status"] != "completed"
                and task["completion"] is None and "parent_acceptance" not in task["native"],
                "Pending review cannot carry acceptance")
        require(review["next_action"] != "hold"
                or review["claim_generation"] < task["claim_generation"]
                or task["status"] != "in_progress", "Held review cannot retain a live binding")
    else:
        require(task["status"] == "closed" and task["attempt"]["status"] == "completed",
                "Accepted review requires completed work")
        resolved = review.get("resolution")
        fields = {"review_id", "findings", "actor", "at", "attempt_id", "claim_generation"}
        object_fields(resolved, fields, fields, "accepted review resolution")
        _resolution(review, {key: resolved[key] for key in ("review_id", "findings")})
        _review_provenance(state, task, resolved)
        completion = task["completion"]
        require(resolved["claim_generation"] == task["claim_generation"]
                and type(completion) is dict
                and resolved["actor"] == completion.get("actor")
                and resolved["at"] == completion.get("at")
                and instant(review["at"]) <= instant(resolved["at"]),
                "Resolution must match current parent acceptance")
