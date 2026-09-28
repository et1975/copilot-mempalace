"""Deterministic decisions over JSON state; no clock, storage, or execution I/O.

Actors are registered identities, not client-supplied role assertions.
``authority_create`` records operator configuration before other commands.
Transport authentication and evidence verification belong to the authority/host.
"""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid5

from .codec import canonical_json, task_id_for
from .model import (DomainError, State, identifier, instant, integer, object_fields,
                    require, text, utc)


DEFAULT_POLICY = {
    "lease_ttl_seconds": 300, "renewal_seconds": 100, "sweep_seconds": 15,
    "progress_timeout_seconds": 1800, "hard_timeout_seconds": 7200,
    "automatic_retries": 3,
}
CLASSES = {"isolated", "resource_fenced", "shared_unfenced"}
ROLES = {"operator", "coordinator", "supervisor", "worker", "system"}
TERMINAL = {"closed", "cancelled"}
STATUSES = {"open", "in_progress", "recovering", "quarantined"} | TERMINAL
EDGE_TYPES = {"blocks", "parent_child", "related", "discovered_from", "duplicates", "supersedes"}
CONTENT = {"title", "description", "acceptance", "priority", "hold_reason", "deferred_until"}
EXECUTION = {"execution_class", "execution_profile", "resource_keys", "policy"}
TASK_INPUT = CONTENT | EXECUTION | {"project", "kind", "goal_id", "intent_key", "admitted"}
TOKENS = {"attempt_id", "claim_generation"}
VERSIONED = {"task_id", "expected_version"}
BASE = {"operation", "command_id", "actor"}
SCHEMAS = {
    "authority_create": ({"execution_profiles", "actors", "supervisors", "policy"},
                         {"execution_profiles", "actors", "supervisors"}),
    "create": (TASK_INPUT, {"project", "kind", "title", "description", "acceptance"}),
    "update": (VERSIONED | TOKENS | {"patch"}, VERSIONED | {"patch"}),
    "claim": (VERSIONED | {"supervisor_id", "worker_id"}, VERSIONED | {"supervisor_id"}),
    "renew": ({"task_id", "expected_lease_revision"} | TOKENS,
              {"task_id", "expected_lease_revision"} | TOKENS),
    "checkpoint": (VERSIONED | TOKENS | {"checkpoint_sequence", "reference"},
                   VERSIONED | TOKENS | {"checkpoint_sequence", "reference"}),
    "attempt_report": (VERSIONED | TOKENS | {"report_kind", "evidence", "reason", "permanent"},
                       VERSIONED | TOKENS | {"report_kind", "evidence"}),
    "expire": (VERSIONED | TOKENS | {"expected_lease_revision", "reason"},
               VERSIONED | TOKENS | {"expected_lease_revision"}),
    "release": (VERSIONED | TOKENS | {"reason"}, VERSIONED | TOKENS | {"reason"}),
    "recover": (VERSIONED | {"evidence", "retry_decision", "additional_retries"},
                VERSIONED | {"evidence", "retry_decision"}),
    "transition": (VERSIONED | TOKENS | {"target", "summary", "evidence", "reason"},
                   VERSIONED | {"target"}),
    "add_dependency": ({"source", "target", "edge_type", "expected_source_version",
                        "expected_target_version"},
                       {"source", "target", "edge_type", "expected_source_version",
                        "expected_target_version"}),
    "remove_dependency": ({"source", "target", "edge_type", "expected_source_version",
                           "expected_target_version", "reason"},
                          {"source", "target", "edge_type", "expected_source_version",
                           "expected_target_version", "reason"}),
    "note": (VERSIONED | TOKENS | {"tag", "text", "references"}, VERSIONED | {"tag", "text"}),
    "bootstrap": ({"project", "title", "description", "acceptance", "priority",
                  "planning_task", "goal_policy"},
                 {"project", "title", "description", "acceptance", "planning_task", "goal_policy"}),
    "expand": ({"goal_id", "expected_graph_revision", "source_task_id", "expected_version",
               "tasks", "edges", "source_disposition", "checkpoint", "reason", "summary",
               "evidence"} | TOKENS,
              {"goal_id", "expected_graph_revision", "tasks", "source_disposition"}),
    "goal_close": ({"goal_id", "expected_version", "expected_graph_revision", "summary", "evidence"},
                   {"goal_id", "expected_version", "expected_graph_revision", "summary", "evidence"}),
}


def _enum(value, values, name):
    require(type(value) is str and value in values, f"Invalid {name}")
    return value


def _boolean(value, name):
    require(type(value) is bool, f"{name} must be boolean")
    return value


def _deadline(at, seconds):
    try:
        return instant(at) + timedelta(seconds=seconds)
    except OverflowError as error:
        raise DomainError("validation_error", "Deadline exceeds supported UTC range") from error


def _strings(value, name, maximum=20, *, nonempty=False, width=2048):
    require(type(value) is list and len(value) <= maximum and (not nonempty or bool(value)),
            f"Invalid {name} list")
    for item in value:
        text(item, name, width, byte_limit=True)
    require(len(set(value)) == len(value), f"{name} must be distinct")
    return deepcopy(value)


def _policy(overrides, base=None):
    object_fields(overrides, DEFAULT_POLICY, name="policy")
    result = {**(DEFAULT_POLICY if base is None else base), **overrides}
    for key, value in result.items():
        integer(value, key, 0 if key == "automatic_retries" else 1,
                3 if key == "automatic_retries" else 86400)
    require(result["renewal_seconds"] < result["lease_ttl_seconds"],
            "Renewal cadence must be shorter than TTL")
    require(result["progress_timeout_seconds"] <= result["hard_timeout_seconds"],
            "Progress timeout must not exceed hard timeout")
    return result


def _configuration(command):
    profiles = command["execution_profiles"]
    actors = command["actors"]
    supervisors = command["supervisors"]
    for value, name in ((profiles, "execution_profiles"), (actors, "actors"),
                        (supervisors, "supervisors")):
        require(type(value) is dict and len(value) <= 500, f"Invalid {name}")
        for key in value:
            text(key, name)
    for role in actors.values():
        _enum(role, ROLES, "actor role")
    require(actors.get(command["actor"]) == "operator",
            "Authority creator must be a configured operator", "not_owner")
    normalized_profiles = {}
    for name, profile in profiles.items():
        object_fields(profile, {"execution_class", "available", "supports_fencing",
                                "supports_reconciliation"}, {"execution_class"}, "profile")
        normalized = {"available": True, "supports_fencing": False,
                      "supports_reconciliation": False, **profile}
        _enum(normalized["execution_class"], CLASSES, "execution_class")
        for key in ("available", "supports_fencing", "supports_reconciliation"):
            _boolean(normalized[key], key)
        if normalized["execution_class"] == "resource_fenced":
            require(normalized["supports_fencing"] and normalized["supports_reconciliation"],
                    "Fenced profile requires tested fencing and reconciliation",
                    "unsupported_execution_profile")
        normalized_profiles[name] = normalized
    for name, supervisor in supervisors.items():
        require(actors.get(name) == "supervisor", "Supervisor must have registered role")
        object_fields(supervisor, {"profiles", "workers"}, {"profiles", "workers"}, "supervisor")
        _strings(supervisor["profiles"], "profiles", 500, nonempty=True, width=256)
        _strings(supervisor["workers"], "workers", 500, nonempty=True, width=256)
        require(all(p in profiles for p in supervisor["profiles"]), "Unknown supervisor profile")
        require(all(actors.get(w) == "worker" for w in supervisor["workers"]),
                "Supervisor workers must have registered worker roles")
    return {"execution_profiles": normalized_profiles, "actors": deepcopy(actors),
            "supervisors": deepcopy(supervisors), "policy": _policy(command.get("policy", {}))}


def _role(state, actor):
    role = state.configuration["actors"].get(actor)
    require(role is not None, "Actor is not registered", "not_owner")
    return role


def _admin(state, actor, *, operator=False):
    require(_role(state, actor) in ({"operator"} if operator else {"operator", "coordinator"}),
            "Administrative actor required", "not_owner")


def _task(state, task_id):
    text(task_id, "task_id", 40)
    require(task_id in state.tasks, "Task not found", "not_found", task_id=task_id)
    return state.tasks[task_id]


def _expected(task, value, field="version"):
    integer(value, "expected_" + field)
    require(value == task[field], "Version does not match", "version_conflict",
            task_id=task["id"], field=field, expected=value, actual=task[field])


def _goal(state, goal_id, *, open_only=True):
    goal = _task(state, goal_id)
    require(goal["kind"] == "epic" and goal["goal_id"] == goal_id and goal["goal_policy"] is not None,
            "Task is not a root goal")
    if open_only:
        require(not goal["sealed"] and goal["status"] == "open",
                "Goal admission is sealed or inactive", "invalid_transition")
    return goal


def _check_task_input(state, data):
    text(data["project"], "project")
    _enum(data["kind"], {"task", "epic"}, "kind")
    text(data["title"], "title")
    text(data["description"], "description", 16384, empty=True, byte_limit=True)
    text(data["acceptance"], "acceptance", 8192, empty=True, byte_limit=True)
    integer(data["priority"], "priority", 0, 4)
    if data["hold_reason"] is not None:
        text(data["hold_reason"], "hold_reason", 8192, byte_limit=True)
    if data["deferred_until"] is not None:
        data["deferred_until"] = utc(instant(data["deferred_until"], "deferred_until"))
    _boolean(data["admitted"], "admitted")
    if data["intent_key"] is not None:
        text(data["intent_key"], "intent_key")
        require(not data["intent_key"].startswith("$"), "Intent key must not use reserved prefix")
    resources = _strings(data["resource_keys"], "resource_keys", 32, width=256)
    require(resources == sorted(resources), "resource_keys must be sorted")
    data["policy"] = _policy(data["policy"], state.configuration["policy"])
    if data["kind"] == "epic":
        require(data["execution_class"] is None and data["execution_profile"] is None and not resources,
                "Epics must not declare execution resources or profiles")
    else:
        _enum(data["execution_class"], CLASSES, "execution_class")
        text(data["execution_profile"], "execution_profile")
        profile = state.configuration["execution_profiles"].get(data["execution_profile"])
        require(not data["admitted"]
                or (profile is not None and profile["execution_class"] == data["execution_class"]),
                "Unregistered or incompatible execution profile", "unsupported_execution_profile")
        require(bool(resources) == (data["execution_class"] != "isolated"),
                "Shared tasks require resources; isolated tasks must not reserve shared resources")


def _new_task(state, task_id, inputs, at):
    object_fields(inputs, TASK_INPUT, {"project", "kind", "title", "description", "acceptance"},
                  "task")
    task = {"id": task_id, "priority": 2, "hold_reason": None, "deferred_until": None,
            "execution_class": None, "execution_profile": None, "resource_keys": [],
            "policy": {}, "goal_id": None, "intent_key": None, "admitted": True,
            **deepcopy(inputs)}
    _check_task_input(state, task)
    task.update(status="open", version=1, claim_generation=0, lease_revision=0,
                lease_expires_at=None, attempt=None, automatic_retries_used=0,
                retry_budget_granted=0, retry_not_before=None, recovery=None, escalation=None,
                created_at=at, updated_at=at, completion=None, cancellation_reason=None,
                sealed=False, graph_revision=0, goal_policy=None)
    require(task_id not in state.tasks, "Task already exists", "already_exists")
    state.tasks[task_id] = task
    return task


def _generation(task, command):
    attempt = task["attempt"]
    require(attempt is not None and task["status"] == "in_progress"
            and command.get("attempt_id") == attempt["id"]
            and type(command.get("claim_generation")) is int
            and command["claim_generation"] == task["claim_generation"],
            "Attempt authorization has been revoked or superseded", "stale_generation")
    return attempt


def _live(state, task, command, at, *, supervisor_only=False, allow_admin=False,
          allow_expired=False):
    attempt = _generation(task, command)
    actor = command["actor"]
    allowed = {attempt["supervisor_id"]} if supervisor_only else {
        task.get("assignee"), attempt["supervisor_id"]}
    if allow_admin and _role(state, actor) == "operator":
        allowed.add(actor)
    require(actor in allowed, "Actor does not own this attempt", "not_owner")
    if not allow_expired:
        require(instant(at) < min(instant(task["lease_expires_at"]),
                                 instant(attempt["progress_deadline"]),
                                 instant(attempt["hard_deadline"])),
                "Attempt lease or deadline has expired", "lease_expired")
    return attempt


def _evidence(value):
    object_fields(value, {"references", "prepared", "resource_fences", "publication_revoked",
                          "process_stopped", "effects_reconciled", "installed_fences",
                          "fencing_unavailable"},
                  {"references"}, "execution evidence")
    _strings(value["references"], "evidence references", nonempty=True)
    for key in {"prepared", "publication_revoked", "process_stopped", "effects_reconciled",
                "fencing_unavailable"} & value.keys():
        _boolean(value[key], key)
    for key in {"resource_fences", "installed_fences"} & value.keys():
        require(type(value[key]) is dict, f"{key} must be an object")
        for resource, counter in value[key].items():
            text(resource, "resource key")
            integer(counter, "fence counter", 1)
    return deepcopy(value)


def _settlement_evidence(execution_class, evidence):
    evidence = _evidence(evidence)
    if execution_class == "shared_unfenced":
        require(evidence.get("process_stopped") is True and evidence.get("effects_reconciled") is True,
                "Require stopped process and reconciled outstanding effects", "recovery_required")
    elif execution_class == "resource_fenced":
        require(evidence.get("effects_reconciled") is True
                and evidence.get("fencing_unavailable") is not True,
                "Fenced settlement requires reconciled effects and available fencing", "recovery_required")
    return evidence


def _barrier(state, task, evidence, *, recovering):
    if not recovering:
        return _settlement_evidence(task["execution_class"], evidence)
    evidence = _evidence(evidence)
    execution_class = task["execution_class"]
    if execution_class == "isolated":
        require(not recovering or evidence.get("publication_revoked") is True,
                "Isolated publication authority must be revoked", "recovery_required")
    elif execution_class == "shared_unfenced":
        require(evidence.get("process_stopped") is True and evidence.get("effects_reconciled") is True,
                "Require stopped process and reconciled outstanding effects", "recovery_required")
    else:
        require(evidence.get("effects_reconciled") is True,
                "Fenced effects require reconciliation", "recovery_required")
        if evidence.get("fencing_unavailable") is True:
            require(recovering and evidence.get("process_stopped") is True,
                    "Unavailable fencing requires stopped process and reconciled effects",
                    "recovery_required")
        elif recovering:
            counters = evidence.get("installed_fences", {})
            require(set(counters) == set(task["resource_keys"])
                    and all(counters[key] == state.resources[key]["counter"] + 1 for key in counters),
                    "Recovery requires next resource-wide fences installed at write boundary",
                    "recovery_required")
            for key, counter in counters.items():
                state.resources[key]["counter"] = counter
    return evidence


def _release_resources(state, task):
    for key in task["resource_keys"]:
        record = state.resources[key]
        reservation = record["reservation"]
        require(reservation is not None and reservation["task_id"] == task["id"]
                and reservation["attempt_id"] == task["attempt"]["id"]
                and reservation["claim_generation"] == task["claim_generation"],
                "Resource reservation owner mismatch", "invariant_violation")
        record["reservation"] = None


def _revoke(state, task, at, reason, disposition, *, failed=False, permanent=False):
    text(reason, "reason", 8192, byte_limit=True)
    task.pop("assignee", None)
    task["lease_expires_at"] = None
    task["lease_revision"] += 1
    task["attempt"]["status"] = "revoked"
    task["attempt"]["failure_reason"] = reason if failed else None
    task["recovery"] = {"reason": reason, "disposition": disposition, "failed": failed,
                        "permanent": permanent, "started_at": at, "barrier_satisfied": False,
                        "evidence": None}
    profile = state.configuration["execution_profiles"][task["execution_profile"]]
    unavailable = task["execution_class"] != "isolated" and not profile["supports_reconciliation"]
    task["status"] = "quarantined" if unavailable or permanent else "recovering"
    if task["status"] == "quarantined":
        task["escalation"] = {"code": "recovery_required" if unavailable else "permanent_failure",
                              "at": at, "reason": reason}
    return task["status"] == "quarantined"


def _checkpoint(task, sequence, reference, at):
    integer(sequence, "checkpoint_sequence", 1)
    text(reference, "reference", 2048, byte_limit=True)
    previous = task["attempt"]["checkpoint"]
    require(previous is None or (sequence > previous["sequence"] and reference != previous["reference"]),
            "Checkpoint must have a fresh sequence and changed durable reference")
    task["attempt"]["checkpoint"] = {"sequence": sequence, "reference": reference, "at": at}
    task["attempt"]["progress_deadline"] = utc(min(
        _deadline(at, task["policy"]["progress_timeout_seconds"]),
        instant(task["attempt"]["hard_deadline"])))


def _wait_reasons(state, task, at):
    reasons = []
    if task["hold_reason"] is not None:
        reasons.append({"code": "held", "reason": task["hold_reason"]})
    if task["deferred_until"] is not None and instant(at) < instant(task["deferred_until"]):
        reasons.append({"code": "deferred", "until": task["deferred_until"]})
    for edge in state.edges:
        if edge["edge_type"] == "blocks" and edge["target"] == task["id"]:
            if state.tasks[edge["source"]]["status"] != "closed":
                reasons.append({"code": "blocked", "task_id": edge["source"]})
    return reasons


def task_eligibility(state, task, as_of):
    at = utc(instant(as_of, "as_of"))
    if type(task) is str:
        task = _task(state, task)
    else:
        require(type(task) is dict and "id" in task, "Expected task snapshot")
        current = _task(state, task["id"])
        require(task == current, "Eligibility requires a current snapshot", "version_conflict")
        task = current
    reasons = []
    if task["kind"] != "task":
        reasons.append({"code": "epic"})
    if task["status"] != "open":
        reasons.append({"code": task["status"]})
    if "assignee" in task:
        reasons.append({"code": "assigned", "assignee": task["assignee"]})
    if not task["admitted"]:
        reasons.append({"code": "awaiting_admission"})
    reasons.extend(_wait_reasons(state, task, at))
    if task["recovery"] is not None:
        reasons.append({"code": "recovery_required"})
    if task["retry_not_before"] is not None and instant(at) < instant(task["retry_not_before"]):
        reasons.append({"code": "retry_backoff", "until": task["retry_not_before"]})
    if task["goal_id"] is not None:
        goal = _goal(state, task["goal_id"], open_only=False)
        if goal["sealed"] or goal["status"] != "open":
            reasons.append({"code": "goal_sealed"})
    for key in task["resource_keys"]:
        record = state.resources.get(key)
        if record and record["reservation"] is not None:
            reasons.append({"code": "resource_busy", "resource_key": key,
                            "task_id": record["reservation"]["task_id"]})
    if task["kind"] == "task":
        profile = state.configuration["execution_profiles"].get(task["execution_profile"])
        if (profile is None or profile["execution_class"] != task["execution_class"]
                or not profile["available"] or not any(
                task["execution_profile"] in sup["profiles"]
                for sup in state.configuration["supervisors"].values())):
            reasons.append({"code": "unsupported_execution_profile"})
    return {"ready": not reasons, "reasons": deepcopy(reasons), "as_of": at}


def ready_tasks(state, filters, as_of):
    object_fields(filters, {"project", "goal_id", "priority_ceiling", "limit", "execution_profile"},
                  name="ready filters")
    for key in {"project", "goal_id", "execution_profile"} & filters.keys():
        text(filters[key], key)
    limit = integer(filters.get("limit", 100), "limit", 1, 500)
    ceiling = integer(filters.get("priority_ceiling", 4), "priority_ceiling", 0, 4)
    instant(as_of, "as_of")
    result = [task for task in state.tasks.values()
              if task["priority"] <= ceiling and all(task[key] == filters[key]
                  for key in {"project", "goal_id", "execution_profile"} & filters.keys())
              and task_eligibility(state, task, as_of)["ready"]]
    return deepcopy(sorted(result, key=lambda t: (t["priority"], t["created_at"], t["id"]))[:limit])


def _edge(source, target, edge_type):
    _enum(edge_type, EDGE_TYPES, "edge_type")
    if edge_type == "related":
        source, target = sorted((source, target))
    return {"source": source, "target": target, "edge_type": edge_type}


def _acyclic(state):
    graph = {task_id: set() for task_id in state.tasks}
    for edge in state.edges:
        if edge["edge_type"] == "blocks":
            graph[edge["target"]].add(edge["source"])
        elif edge["edge_type"] == "parent_child":
            graph[edge["source"]].add(edge["target"])
    # Iterative topological elimination also handles deeply nested epics.
    incoming = {node: 0 for node in graph}
    for targets in graph.values():
        for target in targets:
            incoming[target] += 1
    pending = [node for node, count in incoming.items() if count == 0]
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        for target in graph[node]:
            incoming[target] -= 1
            if incoming[target] == 0:
                pending.append(target)
    require(visited == len(graph), "Combined dependency/parent wait cycle", "dependency_cycle")


def _add_edge(state, source, target, edge_type, touched, *, reuse=False, yielding=None):
    a, b = _task(state, source), _task(state, target)
    require(source != target and a["project"] == b["project"],
            "Relationships require distinct same-project endpoints")
    edge = _edge(source, target, edge_type)
    if edge in state.edges:
        require(reuse, "Relationship already exists", "already_exists")
        return
    if edge_type in {"blocks", "parent_child"}:
        require(a["goal_id"] == b["goal_id"], "Operational edges must remain in one goal scope")
        if a["goal_id"] is not None:
            _goal(state, a["goal_id"])
    if edge_type == "blocks":
        require(b["status"] == "open"
                or (target == yielding and b["status"] in {"recovering", "quarantined"}),
                "New blockers require an open target or atomic yield", "invalid_transition")
    if edge_type == "parent_child":
        require(a["kind"] == "epic" and a["status"] == "open" and b["status"] == "open"
                and "assignee" not in b, "Parent changes require open epic and open child",
                "invalid_transition")
        require(not any(e["edge_type"] == "parent_child" and e["target"] == target for e in state.edges),
                "A child may have only one parent", "invalid_transition")
    state.edges.append(edge)
    _acyclic(state)
    touched.update((source, target))


def _touch_goals(state, task_ids, touched):
    goals = {state.tasks[tid]["goal_id"] for tid in task_ids}
    for goal_id in goals - {None}:
        goal = _goal(state, goal_id)
        goal["graph_revision"] += 1
        touched.add(goal_id)


def _complete(state, task, command, at):
    summary = text(command.get("summary"), "summary", 8192, byte_limit=True)
    evidence = _strings(command.get("evidence"), "evidence", nonempty=True)
    if task["kind"] == "task":
        _live(state, task, command, at)
    require(not _wait_reasons(state, task, at), "Task has unresolved gates", "not_ready")
    require(task["admitted"], "Proposed work cannot be completed before admission", "not_ready")
    if task["kind"] == "task":
        attempt = _live(state, task, command, at)
        require(command["actor"] == task.get("assignee"),
                "Only the live owner may close a task", "not_owner")
        require(attempt["status"] in {"running", "settled"}, "Attempt has not started", "invalid_transition")
        if task["execution_class"] != "isolated":
            require(attempt["settlement"] is not None, "Shared effects are not settled", "recovery_required")
            _settlement_evidence(task["execution_class"], attempt["settlement"])
        _release_resources(state, task)
        attempt["status"] = "completed"
        task.pop("assignee", None)
        task["lease_expires_at"] = None
        task["lease_revision"] += 1
    else:
        _admin(state, command["actor"])
        require(task["status"] == "open", "Epic is not open", "invalid_transition")
        children = [e["target"] for e in state.edges
                    if e["edge_type"] == "parent_child" and e["source"] == task["id"]]
        require(all(state.tasks[tid]["status"] in TERMINAL for tid in children),
                "Epic has unfinished children", "not_ready")
    task["status"] = "closed"
    task["completion"] = {"summary": summary, "evidence": evidence, "at": at, "actor": command["actor"]}


def _goal_policy(value):
    object_fields(value, {"scope", "max_tasks", "max_batch"}, {"scope"}, "goal_policy")
    text(value["scope"], "scope", 8192, byte_limit=True)
    return {"scope": value["scope"], "max_tasks": integer(value.get("max_tasks", 100), "max_tasks", 1, 10000),
            "max_batch": integer(value.get("max_batch", 25), "max_batch", 1, 50)}


def _expand(state, command, at, touched):
    goal = _goal(state, command["goal_id"])
    _expected(goal, command["expected_graph_revision"], "graph_revision")
    source_id = command.get("source_task_id")
    disposition = _enum(command["source_disposition"], {"continue", "yield", "complete"},
                        "source_disposition")
    if source_id is None:
        _admin(state, command["actor"])
        require(disposition == "continue" and not (TOKENS | {"expected_version"}) & command.keys(),
                "Coordinator admission has no source execution disposition")
        source = None
    else:
        source = _task(state, source_id)
        require(source["goal_id"] == goal["id"] and source["admitted"],
                "Source must be admitted in the goal")
        _live(state, source, command, at)
        _expected(source, command.get("expected_version"))
        require(source["attempt"]["status"] == "running"
                or (disposition == "complete" and source["attempt"]["status"] == "settled"),
                "Discovery requires running work or settled work being completed", "invalid_transition")
    tasks = command["tasks"]
    require(type(tasks) is list and len(tasks) <= goal["goal_policy"]["max_batch"],
            "Expansion exceeds bounded batch size")
    edges = command.get("edges", [])
    require(type(edges) is list and len(edges) <= 200, "Expansion edges exceed limit")
    if disposition == "yield":
        checkpoint = command.get("checkpoint")
        object_fields(checkpoint, {"sequence", "reference"}, {"sequence", "reference"}, "checkpoint")
        _checkpoint(source, checkpoint["sequence"], checkpoint["reference"], at)
        _revoke(state, source, at, command.get("reason"), "release")
        touched.add(source_id)
    else:
        require("checkpoint" not in command and "reason" not in command,
                "Only yielding expansion accepts checkpoint/reason")
    if disposition != "complete":
        require("summary" not in command and "evidence" not in command,
                "Only completing expansion accepts completion evidence")
    aliases = {"$goal": goal["id"]}
    if source_id is not None:
        aliases["$source"] = source_id
    admitted, proposed = [], []
    created = []
    reused_versions = {}
    seen = set()
    for spec in tasks:
        object_fields(spec, (TASK_INPUT - {"project", "goal_id"}) |
                      {"reuse_task_id", "expected_version"}, {"intent_key"}, "expansion task")
        key = text(spec["intent_key"], "intent_key")
        if "admitted" in spec:
            _boolean(spec["admitted"], "admitted")
        require(not key.startswith("$") and key not in seen, "Repeated/reserved intent key")
        seen.add(key)
        existing = [t for t in state.tasks.values()
                    if t["goal_id"] == goal["id"] and t["intent_key"] == key]
        if "reuse_task_id" in spec:
            require(set(spec) <= {"intent_key", "reuse_task_id", "expected_version", "admitted"},
                    "Explicit reuse cannot redefine task content")
            target = _task(state, spec["reuse_task_id"])
            _expected(target, spec.get("expected_version"))
            require(target["goal_id"] == goal["id"] and target["intent_key"] == key,
                    "Explicit reuse must match goal and intent")
        elif existing:
            target = existing[0]
            if "expected_version" in spec:
                _expected(target, spec["expected_version"])
            # An intent collision may reuse, but must not silently change its definition.
            compared_fields = (TASK_INPUT - {"admitted"}) & spec.keys()
            candidate = deepcopy(target)
            candidate.update({field: deepcopy(spec[field]) for field in compared_fields})
            _check_task_input(state, candidate)
            for field in compared_fields:
                require(target[field] == candidate[field], "Intent already has a different definition",
                        "already_exists", intent_key=key)
        else:
            require("expected_version" not in spec, "New tasks have no expected version")
            task_uuid = str(uuid5(UUID(command["command_id"]), key))
            inputs = {"kind": "task", **spec, "project": goal["project"], "goal_id": goal["id"]}
            target = _new_task(state, task_id_for(state.authority_id, task_uuid), inputs, at)
            created.append(target["id"])
        if target["id"] not in created:
            reused_versions[target["id"]] = spec.get("expected_version")
        if spec.get("admitted", target["admitted"]) != target["admitted"]:
            _admin(state, command["actor"])
            _expected(target, spec.get("expected_version"))
            require(spec["admitted"] is True and target["status"] == "open",
                    "Only open proposals can be admitted", "invalid_transition")
            target["admitted"] = True
            _check_task_input(state, target)
            touched.add(target["id"])
        aliases[key] = target["id"]
        aliases[target["id"]] = target["id"]
        (admitted if target["admitted"] else proposed).append(target["id"])
        if source_id is not None:
            _add_edge(state, target["id"], source_id, "discovered_from", touched, reuse=True)
    for edge in edges:
        object_fields(edge, {"source", "target", "edge_type"}, {"source", "target", "edge_type"}, "edge")
        text(edge["source"], "edge source")
        text(edge["target"], "edge target")
        require(edge["source"] in aliases and edge["target"] in aliases,
                "Expansion edges must name source, goal or declared tasks")
        _add_edge(state, aliases[edge["source"]], aliases[edge["target"]], edge["edge_type"],
                  touched, reuse=True, yielding=source_id if disposition == "yield" else None)
    for task_id in created:
        if not any(edge["edge_type"] == "parent_child" and edge["target"] == task_id
                   for edge in state.edges):
            _add_edge(state, goal["id"], task_id, "parent_child", touched)
    for task_id, version in reused_versions.items():
        if task_id in touched:
            _expected(state.tasks[task_id], version)
    members = [t for t in state.tasks.values() if t["goal_id"] == goal["id"] and t["id"] != goal["id"]]
    require(sum(t["admitted"] for t in members) <= goal["goal_policy"]["max_tasks"],
            "Goal task admission budget exhausted")
    require(sum(not t["admitted"] and t["status"] not in TERMINAL for t in members) <= 100,
            "Pending proposal limit exceeded")
    if disposition == "complete":
        _complete(state, source, command, at)
        touched.add(source_id)
    if touched:
        goal["graph_revision"] += 1
        touched.add(goal["id"])
    return {"goal_id": goal["id"], "admitted_task_ids": admitted, "proposed_task_ids": proposed}


def _execute(state, command, at):
    op = command["operation"]
    actor = command["actor"]
    touched = set()
    response = {}
    if op == "authority_create":
        require(state.configuration is None and not state.tasks, "Authority exists", "already_exists")
        state.configuration = _configuration(command)
        return "AuthorityCreated", touched, {"authority_id": state.authority_id}
    require(state.configuration is not None, "Authority must be created first", "invalid_transition")
    role = _role(state, actor)
    if op == "create":
        _admin(state, actor)
        require(command.get("goal_id") is None,
                "Goal tasks must be published atomically through expand")
        require(command.get("intent_key") is None and command.get("admitted", True) is True,
                "Standalone tasks cannot use goal admission fields")
        task = _new_task(state, task_id_for(state.authority_id, command["command_id"]),
                         {k: v for k, v in command.items() if k in TASK_INPUT}, at)
        touched.add(task["id"])
        kind, response = "TaskCreated", {"task_id": task["id"]}
    elif op == "bootstrap":
        _admin(state, actor)
        goal_id = task_id_for(state.authority_id, command["command_id"])
        goal = _new_task(state, goal_id, {k: command[k] for k in
                         ("project", "title", "description", "acceptance", "priority") if k in command}
                         | {"kind": "epic", "goal_id": goal_id}, at)
        goal["goal_policy"] = _goal_policy(command["goal_policy"])
        goal["graph_revision"] = 1
        planner_spec = command["planning_task"]
        object_fields(planner_spec, CONTENT | EXECUTION, {"title", "description", "acceptance"},
                      "planning_task")
        planner_id = task_id_for(state.authority_id, str(uuid5(UUID(command["command_id"]), "planning")))
        _new_task(state, planner_id, {**planner_spec, "project": goal["project"], "kind": "task",
                                     "goal_id": goal_id, "intent_key": "planning"}, at)
        _add_edge(state, goal_id, planner_id, "parent_child", touched)
        kind, response = "GoalBootstrapped", {"goal_id": goal_id, "planning_task_id": planner_id}
    elif op == "expand":
        response = _expand(state, command, at, touched)
        kind = "TasksExpanded"
    elif op == "goal_close":
        _admin(state, actor)
        goal = _goal(state, command["goal_id"])
        _expected(goal, command["expected_version"])
        _expected(goal, command["expected_graph_revision"], "graph_revision")
        members = [t for t in state.tasks.values() if t["goal_id"] == goal["id"] and t["id"] != goal["id"]]
        require(bool(members) and any(t["status"] == "closed" for t in members),
                "Goal requires completed work, not only an empty/cancelled frontier", "not_ready")
        require(all(t["status"] in TERMINAL and t["recovery"] is None for t in members),
                "Goal has unfinished work, proposals or recovery", "not_ready")
        require(not any(r["reservation"] is not None
                        and r["reservation"]["task_id"] in {t["id"] for t in members}
                        for r in state.resources.values()), "Goal retains resources", "not_ready")
        _complete(state, goal, command, at)
        goal["sealed"] = True
        goal["graph_revision"] += 1
        touched.add(goal["id"])
        kind, response = "GoalClosed", {"goal_id": goal["id"]}
    elif op in {"add_dependency", "remove_dependency"}:
        _admin(state, actor)
        a, b = _task(state, command["source"]), _task(state, command["target"])
        _expected(a, command["expected_source_version"])
        _expected(b, command["expected_target_version"])
        if op == "add_dependency":
            _add_edge(state, a["id"], b["id"], command["edge_type"], touched)
            kind = "DependencyAdded"
        else:
            text(command["reason"], "reason", 8192, byte_limit=True)
            edge = _edge(a["id"], b["id"], command["edge_type"])
            require(edge in state.edges, "Relationship does not exist", "not_found")
            if edge["edge_type"] == "parent_child":
                require(a["status"] == b["status"] == "open" and "assignee" not in b,
                        "Parent changes require open endpoints", "invalid_transition")
                require(b["goal_id"] is None, "Goal membership cannot be removed")
            state.edges.remove(edge)
            touched.update((a["id"], b["id"]))
            kind = "DependencyRemoved"
        if command["edge_type"] in {"blocks", "parent_child"}:
            _touch_goals(state, {a["id"], b["id"]}, touched)
    else:
        task = _task(state, command["task_id"])
        if op != "renew":
            _expected(task, command["expected_version"])
        touched.add(task["id"])
        response["task_id"] = task["id"]
        if op == "update":
            require(task["status"] not in TERMINAL, "Terminal content is immutable", "invalid_transition")
            patch = object_fields(command["patch"], CONTENT | EXECUTION, name="patch")
            require(bool(patch), "Patch must not be empty")
            if task["status"] == "in_progress":
                _live(state, task, command, at)
                require(actor == task.get("assignee"), "Only owner may edit active content", "not_owner")
                require(not (EXECUTION | {"hold_reason", "deferred_until"}) & patch.keys(),
                        "Release/recover before changing execution policy or gates", "invalid_transition")
            else:
                _admin(state, actor)
                require(not TOKENS & command.keys(), "Inactive edits cannot carry execution tokens")
                require(task["status"] == "open", "Recover before editing", "invalid_transition")
            if task["goal_id"] is not None:
                _goal(state, task["goal_id"])
            candidate = {**task, **deepcopy(patch)}
            _check_task_input(state, candidate)
            state.tasks[task["id"]] = candidate
            kind = "TaskUpdated"
        elif op == "claim":
            require(role in {"worker", "supervisor"}, "Registered worker/supervisor required", "not_owner")
            require(task["kind"] == "task", "Epics are not claimable", "not_ready")
            supervisor_id = command["supervisor_id"]
            text(supervisor_id, "supervisor_id")
            worker = command.get("worker_id", actor)
            text(worker, "worker_id")
            require((role == "worker" and worker == actor) or actor == supervisor_id,
                    "Supervisor cannot impersonate another supervisor", "not_owner")
            supervisor = state.configuration["supervisors"].get(supervisor_id)
            require(supervisor is not None and worker in supervisor["workers"]
                    and task["execution_profile"] in supervisor["profiles"],
                    "No compatible registered supervisor/worker profile", "unsupported_execution_profile")
            eligibility = task_eligibility(state, task, at)
            require(eligibility["ready"], "Task is not ready", "not_ready",
                    reasons=eligibility["reasons"])
            task["claim_generation"] += 1
            task["lease_revision"] = 1
            task["status"], task["assignee"] = "in_progress", worker
            generation = task["claim_generation"]
            attempt_id = "att_" + str(uuid5(UUID(command["command_id"]), task["id"]))
            fences = {}
            for key in task["resource_keys"]:
                record = state.resources.setdefault(key, {"counter": 0, "reservation": None})
                require(record["reservation"] is None, "Resource already reserved", "resource_busy")
                record["counter"] += 1
                record["reservation"] = {"task_id": task["id"], "attempt_id": attempt_id,
                                         "claim_generation": generation}
                fences[key] = record["counter"]
            hard = _deadline(at, task["policy"]["hard_timeout_seconds"])
            progress = min(hard, _deadline(at, task["policy"]["progress_timeout_seconds"]))
            task["attempt"] = {"id": attempt_id, "number": generation, "owner": worker,
                               "supervisor_id": supervisor_id, "started_at": at, "status": "preparing",
                               "progress_deadline": utc(progress), "hard_deadline": utc(hard),
                               "checkpoint": task["attempt"]["checkpoint"] if task["attempt"] else None,
                               "failure_reason": None, "resource_fences": fences,
                               "preparation": None, "settlement": None}
            task["lease_expires_at"] = utc(min(progress, hard,
                                              _deadline(at, task["policy"]["lease_ttl_seconds"])))
            task["retry_not_before"] = None
            kind = "TaskClaimed"
        elif op == "renew":
            attempt = _live(state, task, command, at, supervisor_only=True)
            _expected(task, command["expected_lease_revision"], "lease_revision")
            task["lease_revision"] += 1
            task["lease_expires_at"] = utc(min(_deadline(at, task["policy"]["lease_ttl_seconds"]),
                instant(attempt["progress_deadline"]),
                instant(attempt["hard_deadline"])))
            touched.clear()
            kind = "LeaseRenewed"
        elif op == "checkpoint":
            attempt = _live(state, task, command, at, supervisor_only=True)
            require(attempt["status"] == "running", "Progress requires running work", "invalid_transition")
            _checkpoint(task, command["checkpoint_sequence"], command["reference"], at)
            kind = "ProgressRecorded"
        elif op == "attempt_report":
            report = _enum(command["report_kind"], {"started", "settled", "failed", "recovery_started"},
                           "report_kind")
            if role == "system" and report == "recovery_started":
                attempt = _generation(task, command)
            else:
                attempt = _live(state, task, command, at, supervisor_only=True)
            require(report in {"failed", "recovery_started"}
                    or not {"reason", "permanent"} & command.keys(),
                    "Start/settlement reports do not accept failure inputs")
            evidence = _evidence(command["evidence"])
            if report == "started":
                require(attempt["status"] == "preparing", "Attempt already started", "invalid_transition")
                require(evidence.get("prepared") is True, "Preparation evidence is required")
                if task["execution_class"] == "resource_fenced":
                    require(evidence.get("resource_fences") == attempt["resource_fences"],
                            "Installed fences must match the claim", "recovery_required")
                attempt["status"], attempt["preparation"] = "running", evidence
                kind = "AttemptStarted"
            elif report == "settled":
                require(attempt["status"] == "running", "Attempt must be running", "invalid_transition")
                attempt["settlement"] = _barrier(state, task, evidence, recovering=False)
                attempt["status"] = "settled"
                kind = "AttemptSettled"
            else:
                permanent = _boolean(command.get("permanent", False), "permanent")
                quarantined = _revoke(state, task, at, command.get("reason"), "requeue",
                                      failed=report == "failed", permanent=permanent)
                task["recovery"]["evidence"] = evidence
                kind = "TaskQuarantined" if quarantined else (
                    "AttemptFailed" if report == "failed" else "AttemptRecoveryStarted")
        elif op == "expire":
            require(role == "system", "Expiry requires registered system actor", "not_owner")
            require(task["status"] == "in_progress" and task["attempt"] is not None
                    and command["attempt_id"] == task["attempt"]["id"]
                    and type(command["claim_generation"]) is int
                    and command["claim_generation"] == task["claim_generation"],
                    "Expiry generation is stale", "stale_generation")
            _expected(task, command["expected_lease_revision"], "lease_revision")
            require(instant(at) >= min(instant(task["lease_expires_at"]),
                                      instant(task["attempt"]["progress_deadline"]),
                                      instant(task["attempt"]["hard_deadline"])),
                    "Lease is not due", "invalid_transition")
            quarantined = _revoke(state, task, at, command.get("reason", "lease_expired"),
                                  "requeue", failed=True)
            kind = "TaskQuarantined" if quarantined else "LeaseExpired"
        elif op == "release":
            _live(state, task, command, at, allow_admin=True)
            quarantined = _revoke(state, task, at, command["reason"], "release")
            kind = "TaskQuarantined" if quarantined else "TaskReleased"
        elif op == "recover":
            require(role == "operator" or (role == "supervisor" and task["attempt"] is not None
                    and actor == task["attempt"]["supervisor_id"]), "Recovery actor is not authorized",
                    "not_owner")
            require(task["status"] in {"recovering", "quarantined"} and task["recovery"] is not None,
                    "Task does not require recovery", "invalid_transition")
            recovery = task["recovery"]
            decision = _enum(command["retry_decision"], {"preserve", "grant"}, "retry_decision")
            if recovery["barrier_satisfied"] and decision != "grant":
                raise DomainError(
                    "retry_budget_exhausted" if task["escalation"]["code"] == "retry_budget_exhausted"
                    else "recovery_required",
                    "Recovery already settled; an explicit bounded retry grant is required")
            if decision == "grant":
                _admin(state, actor, operator=True)
                task["retry_budget_granted"] += integer(command.get("additional_retries"),
                                                       "additional_retries", 1, 3)
            else:
                require("additional_retries" not in command, "preserve does not grant a budget")
            evidence = _evidence(command["evidence"])
            if not recovery["barrier_satisfied"]:
                evidence = _barrier(state, task, evidence, recovering=True)
                _release_resources(state, task)
                recovery["barrier_satisfied"] = True
            recovery["evidence"] = evidence
            limit = task["policy"]["automatic_retries"] + task["retry_budget_granted"]
            if recovery["disposition"] == "cancel":
                task["status"] = "cancelled"
                task["cancellation_reason"] = recovery["reason"]
            elif (recovery["permanent"] and decision != "grant") or (
                    recovery["failed"] and task["automatic_retries_used"] >= limit):
                task["status"] = "quarantined"
                task["escalation"] = {"code": "permanent_failure" if recovery["permanent"]
                                      else "retry_budget_exhausted", "at": at,
                                      "reason": recovery["reason"]}
            else:
                task["status"] = "open"
                if recovery["failed"]:
                    task["automatic_retries_used"] += 1
                    backoff = 30 * 2 ** min(task["automatic_retries_used"] - 1, 2)
                    task["retry_not_before"] = utc(_deadline(at, backoff))
            if task["status"] != "quarantined":
                task["recovery"] = None
                task["escalation"] = None
            kind = "TaskQuarantined" if task["status"] == "quarantined" else "AttemptRecovered"
        elif op == "transition":
            target = _enum(command["target"], {"closed", "cancelled"}, "target")
            if task["goal_policy"] is not None:
                require(False, "Use goal_close for root goals", "invalid_transition")
            if target == "closed":
                _complete(state, task, command, at)
                kind = "TaskClosed"
            else:
                text(command.get("reason"), "reason", 8192, byte_limit=True)
                require(task["status"] not in TERMINAL, "Task is terminal", "invalid_transition")
                if task["status"] == "in_progress":
                    _live(state, task, command, at, allow_admin=True)
                    quarantined = _revoke(state, task, at, command["reason"], "cancel")
                    kind = "TaskQuarantined" if quarantined else "AttemptRecoveryStarted"
                else:
                    _admin(state, actor)
                    require(task["recovery"] is None, "Recover before cancelling", "recovery_required")
                    if task["kind"] == "epic":
                        require(all(state.tasks[e["target"]]["status"] in TERMINAL for e in state.edges
                                    if e["edge_type"] == "parent_child" and e["source"] == task["id"]),
                                "Epic has unfinished children", "not_ready")
                    task["status"], task["cancellation_reason"] = "cancelled", command["reason"]
                    kind = "TaskCancelled"
        elif op == "note":
            if role in {"worker", "supervisor"}:
                _live(state, task, command, at)
            else:
                _admin(state, actor)
                require(not TOKENS & command.keys(),
                        "Administrative notes cannot carry execution tokens")
            text(command["tag"], "tag")
            text(command["text"], "text", 8192, byte_limit=True)
            _strings(command.get("references", []), "references")
            kind = "NoteRecorded"
        else:
            raise DomainError("validation_error", "Unsupported operation")
    return kind, touched, response


def decide(state, command: dict, now: str) -> dict:
    require(isinstance(state, State), "Expected State")
    canonical_json(command)
    require(type(command) is dict, "Command must be an object")
    operation = command.get("operation")
    require(type(operation) is str, "operation must be text")
    operation = operation.removeprefix("mptask_")
    require(operation in SCHEMAS, "Unknown operation", operation=operation)
    allowed, required = SCHEMAS[operation]
    object_fields(command, BASE | allowed, BASE | required, "command")
    identifier(command["command_id"], "command_id")
    text(command["actor"], "actor")
    normalized = {**deepcopy(command), "operation": operation}
    at = utc(instant(now))
    require(state.last_event_at is None or instant(at) >= instant(state.last_event_at),
            "Decision time must not move backwards")
    working = deepcopy(state)
    kind, touched, response = _execute(working, normalized, at)
    for task_id in touched:
        task = working.tasks[task_id]
        if task_id in state.tasks:
            task["version"] = state.tasks[task_id]["version"] + 1
        task["updated_at"] = at
    tasks = [deepcopy(task) for task_id, task in sorted(working.tasks.items())
             if task_id not in state.tasks or task != state.tasks[task_id]]
    added = [deepcopy(edge) for edge in working.edges if edge not in state.edges]
    removed = [deepcopy(edge) for edge in state.edges if edge not in working.edges]
    resources = {key: deepcopy(record) for key, record in sorted(working.resources.items())
                 if key not in state.resources or record != state.resources[key]}
    event = {"schema_version": 1, "authority_id": state.authority_id, "kind": kind,
             "command": normalized, "at": at, "tasks": tasks, "edges_added": added,
             "edges_removed": removed, "resources": resources,
             "configuration": deepcopy(working.configuration) if working.configuration != state.configuration else None,
             "changes": {"task_versions": sorted(touched),
                         "lease_revisions": [t["id"] for t in tasks if t["lease_revision"]
                                             != state.tasks.get(t["id"], {}).get("lease_revision", 0)],
                         "resource_counters": [key for key in resources if resources[key]["counter"]
                                               != state.resources.get(key, {}).get("counter", 0)],
                         "resource_reservations": [key for key in resources if resources[key]["reservation"]
                                                   != state.resources.get(key, {}).get("reservation")]},
             "response": {**response, "tasks": deepcopy(tasks)}}
    require(len(canonical_json(event).encode("utf-8")) <= 240 * 1024,
            "Domain event exceeds 240 KiB")
    return event


def apply_event(state, event: dict) -> State:
    canonical_json(event)
    require(type(event) is dict and event.get("authority_id") == state.authority_id
            and event.get("schema_version") == 1, "Invalid event authority/schema", "invariant_violation")
    require("command" in event and "at" in event, "Missing event decision inputs", "invariant_violation")
    expected = decide(state, event["command"], event["at"])
    require(canonical_json(expected) == canonical_json(event),
            "Event does not match deterministic decision", "invariant_violation")
    result = deepcopy(state)
    for task in event["tasks"]:
        result.tasks[task["id"]] = deepcopy(task)
    for edge in event["edges_removed"]:
        result.edges.remove(edge)
    result.edges.extend(deepcopy(event["edges_added"]))
    result.resources.update(deepcopy(event["resources"]))
    if event["configuration"] is not None:
        result.configuration = deepcopy(event["configuration"])
    result.last_event_at = event["at"]
    return result


def _validate_attempt(state, task):
    attempt = task["attempt"]
    require((attempt is None) == (task["claim_generation"] == 0),
            "Attempt and claim generation disagree")
    if attempt is None:
        require(task["status"] != "in_progress" and task["recovery"] is None,
                "Execution requires an attempt")
        return
    fields = {"id", "number", "owner", "supervisor_id", "started_at", "status", "progress_deadline",
              "hard_deadline", "checkpoint", "failure_reason", "resource_fences",
              "preparation", "settlement"}
    object_fields(attempt, fields, fields, "attempt")
    text(attempt["id"], "attempt id", 40)
    require(attempt["id"].startswith("att_"), "Invalid attempt ID")
    identifier(attempt["id"][4:], "attempt UUID")
    integer(attempt["number"], "attempt number", 1)
    require(attempt["number"] == task["claim_generation"], "Attempt generation mismatch")
    text(attempt["owner"], "attempt owner")
    text(attempt["supervisor_id"], "supervisor_id")
    supervisor = state.configuration["supervisors"].get(attempt["supervisor_id"])
    require(supervisor is not None and attempt["owner"] in supervisor["workers"],
            "Attempt supervisor/worker registration mismatch")
    _enum(attempt["status"], {"preparing", "running", "settled", "revoked", "completed"}, "attempt status")
    start = instant(attempt["started_at"])
    require(start <= instant(attempt["progress_deadline"]) <= instant(attempt["hard_deadline"]),
            "Attempt deadline ordering is invalid")
    require(start >= instant(task["created_at"]) and start <= instant(state.last_event_at),
            "Attempt time is outside task history")
    if task["status"] == "in_progress":
        require(attempt["status"] in {"preparing", "running", "settled"}
                and task["assignee"] == attempt["owner"], "Active attempt owner/status mismatch")
        require(start <= instant(task["lease_expires_at"])
                <= min(instant(attempt["progress_deadline"]), instant(attempt["hard_deadline"])),
                "Lease exceeds progress/hard cap")
    else:
        require(attempt["status"] in {"revoked", "completed"}, "Inactive task has an active attempt")
    checkpoint = attempt["checkpoint"]
    if checkpoint is not None:
        object_fields(checkpoint, {"sequence", "reference", "at"}, {"sequence", "reference", "at"},
                      "checkpoint")
        integer(checkpoint["sequence"], "checkpoint sequence", 1)
        text(checkpoint["reference"], "checkpoint reference", 2048, byte_limit=True)
        require(instant(checkpoint["at"]) <= instant(state.last_event_at), "Checkpoint is in the future")
    if attempt["failure_reason"] is not None:
        text(attempt["failure_reason"], "failure reason", 8192, byte_limit=True)
    require(type(attempt["resource_fences"]) is dict, "Resource fences must be an object")
    for key, counter in attempt["resource_fences"].items():
        text(key, "resource key")
        integer(counter, "resource fence", 1)
    if task["status"] in {"in_progress", "recovering", "quarantined"}:
        require(set(attempt["resource_fences"]) == set(task["resource_keys"]),
                "Attempt resources mismatch")
    for field in ("preparation", "settlement"):
        if attempt[field] is not None:
            _evidence(attempt[field])
    if attempt["status"] in {"running", "settled", "completed"}:
        require(attempt["preparation"] is not None and attempt["preparation"].get("prepared") is True,
                "Started attempts require preparation")
    if attempt["status"] == "settled" or (
            attempt["status"] == "completed" and task["execution_class"] != "isolated"):
        require(attempt["settlement"] is not None, "Settled attempt has no evidence")
    if attempt["settlement"] is not None and attempt["status"] != "revoked":
        _settlement_evidence(task["execution_class"], attempt["settlement"])


def _validate_recovery(task):
    recovery = task["recovery"]
    require((recovery is not None) == (task["status"] in {"recovering", "quarantined"}),
            "Recovery and task status disagree")
    if recovery is not None:
        fields = {"reason", "disposition", "failed", "permanent", "started_at", "barrier_satisfied", "evidence"}
        object_fields(recovery, fields, fields, "recovery")
        text(recovery["reason"], "recovery reason", 8192, byte_limit=True)
        _enum(recovery["disposition"], {"requeue", "release", "cancel"}, "recovery disposition")
        for key in ("failed", "permanent", "barrier_satisfied"):
            _boolean(recovery[key], key)
        instant(recovery["started_at"])
        if recovery["evidence"] is not None:
            _evidence(recovery["evidence"])
        require(not recovery["barrier_satisfied"] or recovery["evidence"] is not None,
                "Satisfied barrier requires evidence")
    escalation = task["escalation"]
    require((escalation is not None) == (task["status"] == "quarantined"),
            "Quarantine requires an escalation")
    if escalation is not None:
        object_fields(escalation, {"code", "at", "reason"}, {"code", "at", "reason"}, "escalation")
        _enum(escalation["code"], {"recovery_required", "permanent_failure", "retry_budget_exhausted"},
              "escalation code")
        instant(escalation["at"])
        text(escalation["reason"], "escalation reason", 8192, byte_limit=True)


def validate_state(state):
    """Reject malformed serialized caches; authoritative replay still verifies events."""
    require(type(state.tasks) is dict and type(state.edges) is list and type(state.resources) is dict,
            "Invalid state collections")
    if state.configuration is None:
        require(not state.tasks and not state.edges and not state.resources and state.last_event_at is None,
                "Uninitialized state must be empty")
        return
    config = state.configuration
    object_fields(config, {"actors", "execution_profiles", "supervisors", "policy"},
                  {"actors", "execution_profiles", "supervisors", "policy"}, "configuration")
    require(type(config["actors"]) is dict, "actors must be an object")
    operators = [actor for actor, role in config["actors"].items() if role == "operator"]
    require(bool(operators), "Configuration requires an operator")
    require(_configuration({**config, "actor": operators[0]}) == config,
            "Configuration is not normalized")
    require(state.last_event_at is not None, "Initialized state needs last_event_at")
    instant(state.last_event_at)
    snapshot_fields = TASK_INPUT | {
        "id", "status", "version", "claim_generation", "lease_revision", "lease_expires_at",
        "attempt", "automatic_retries_used", "retry_budget_granted", "retry_not_before",
        "recovery", "escalation", "created_at", "updated_at", "completion", "cancellation_reason",
        "sealed", "graph_revision", "goal_policy",
    }
    for task in state.tasks.values():
        object_fields(task, snapshot_fields | {"assignee"}, snapshot_fields, "task snapshot")
    for task_id, task in state.tasks.items():
        require(task_id == task["id"] and task_id.startswith("tsk_"), "Task ID mismatch")
        identifier(task_id[4:], "task UUID")
        normalized = deepcopy(task)
        _check_task_input(state, normalized)
        require(task["policy"] == normalized["policy"],
                "Snapshot policy must be complete and normalized")
        _enum(task["status"], STATUSES, "status")
        for field in ("version", "claim_generation", "lease_revision", "automatic_retries_used",
                      "retry_budget_granted", "graph_revision"):
            integer(task[field], field, 1 if field == "version" else 0)
        _boolean(task["sealed"], "sealed")
        for field in ("created_at", "updated_at"):
            instant(task[field], field)
        require(instant(task["created_at"]) <= instant(task["updated_at"])
                <= instant(state.last_event_at), "Task timestamps are inconsistent")
        for field in ("lease_expires_at", "retry_not_before"):
            if task[field] is not None:
                instant(task[field], field)
        require(("assignee" in task) == (task["status"] == "in_progress"),
                "Assignee and active status disagree")
        require((task["lease_expires_at"] is not None) == (task["status"] == "in_progress"),
                "Lease and active status disagree")
        _validate_attempt(state, task)
        _validate_recovery(task)
        require((task["completion"] is not None) == (task["status"] == "closed"),
                "Closed status requires completion")
        if task["completion"] is not None:
            completion = task["completion"]
            object_fields(completion, {"summary", "evidence", "at", "actor"},
                          {"summary", "evidence", "at", "actor"}, "completion")
            text(completion["summary"], "summary", 8192, byte_limit=True)
            _strings(completion["evidence"], "evidence", nonempty=True)
            instant(completion["at"])
            text(completion["actor"], "completion actor")
            require(completion["actor"] in state.configuration["actors"], "Unknown completion actor")
        require((task["cancellation_reason"] is not None) == (task["status"] == "cancelled"),
                "Cancelled status requires a reason")
        if task["cancellation_reason"] is not None:
            text(task["cancellation_reason"], "cancellation_reason", 8192, byte_limit=True)
        if task["goal_policy"] is not None:
            require(task["kind"] == "epic" and task["goal_id"] == task_id
                    and task["sealed"] == (task["status"] == "closed")
                    and task["status"] in {"open", "closed"}, "Root goal lifecycle is inconsistent")
            require(task["goal_policy"] == _goal_policy(task["goal_policy"])
                    and task["graph_revision"] >= 1, "Invalid goal policy/revision")
        else:
            require(not task["sealed"] and task["graph_revision"] == 0,
                    "Only root goals may carry admission seals/revisions")
        if task["goal_id"] is not None:
            goal = _goal(state, task["goal_id"], open_only=False)
            require(goal["project"] == task["project"], "Goal project mismatch")
    parents = {}
    for edge in state.edges:
        object_fields(edge, {"source", "target", "edge_type"}, {"source", "target", "edge_type"}, "edge")
        a, b = _task(state, edge["source"]), _task(state, edge["target"])
        require(a["id"] != b["id"] and a["project"] == b["project"], "Invalid edge endpoints")
        require(_edge(a["id"], b["id"], edge["edge_type"]) == edge, "Edge is not normalized")
        if edge["edge_type"] in {"blocks", "parent_child"}:
            require(a["goal_id"] == b["goal_id"], "Cross-goal operational edge")
        if edge["edge_type"] == "parent_child":
            require(a["kind"] == "epic" and b["id"] not in parents, "Invalid/multiple parents")
            parents[b["id"]] = a["id"]
    require(len({canonical_json(e) for e in state.edges}) == len(state.edges), "Duplicate edges")
    _acyclic(state)
    intents = set()
    for task_id, task in state.tasks.items():
        if task["goal_id"] is not None and task_id != task["goal_id"]:
            ancestor = parents.get(task_id)
            while ancestor is not None and ancestor != task["goal_id"]:
                ancestor = parents.get(ancestor)
            require(ancestor == task["goal_id"], "Goal member is orphaned")
            if task["intent_key"] is not None:
                intent = (task["goal_id"], task["intent_key"])
                require(intent not in intents, "Duplicate goal intent")
                intents.add(intent)
        if task["goal_policy"] is not None and task["sealed"]:
            members = [t for t in state.tasks.values() if t["goal_id"] == task_id and t["id"] != task_id]
            require(bool(members) and all(t["status"] in TERMINAL for t in members)
                    and any(t["status"] == "closed" for t in members),
                    "Sealed goal has unfinished or no completed work")
        reserved = task["status"] == "in_progress" or (
            task["recovery"] is not None and not task["recovery"]["barrier_satisfied"])
        if reserved:
            for key in task["resource_keys"]:
                record = state.resources.get(key)
                require(record is not None and type(record) is dict, "Missing reserved resource")
                require(record.get("reservation") == {
                    "task_id": task_id, "attempt_id": task["attempt"]["id"],
                    "claim_generation": task["claim_generation"]}, "Missing attempt reservation")
                require(record.get("counter") == task["attempt"]["resource_fences"][key],
                        "Reserved resource fence is inconsistent")
    for key, record in state.resources.items():
        text(key, "resource key")
        object_fields(record, {"counter", "reservation"}, {"counter", "reservation"}, "resource")
        integer(record["counter"], "counter", 1)
        reservation = record["reservation"]
        if reservation is not None:
            object_fields(reservation, {"task_id", "attempt_id", "claim_generation"},
                          {"task_id", "attempt_id", "claim_generation"}, "reservation")
            task = _task(state, reservation["task_id"])
            require(task["attempt"] is not None and reservation["attempt_id"] == task["attempt"]["id"]
                    and reservation["claim_generation"] == task["claim_generation"]
                    and task["status"] in {"in_progress", "recovering", "quarantined"}
                    and not (task["recovery"] is not None and task["recovery"]["barrier_satisfied"])
                    and key in task["resource_keys"], "Invalid resource reservation")
