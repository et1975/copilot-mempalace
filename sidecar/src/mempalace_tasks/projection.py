"""Optional, at-least-once historical projections of accepted domain events.

The caller supplies acceptance, contiguous accepted-event ordinals, a dedicated
PalaceClient (never the authority's I/O worker), and a dedicated JsonStore.
This module neither acquires authority locks nor decides whether a proposal won.
Only one projector may own a checkpoint; calls on that instance are serialized.
Drawers are lossless source-marked parts; KG validity is conservatively ceiled
to whole seconds, while source_at and the full immutable payload retain exact
domain time. This coarse historical boundary never replaces task truth.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import hashlib
from itertools import islice
import re
import threading
from urllib.parse import quote

from .codec import canonical_json
from .journal import JournalError, JsonStore
from .model import DomainError, identifier, instant, utc
from .palace import PalaceError


_ADD = "mempalace_add_drawer"
_GET = "mempalace_get_drawer"
_KG_ADD = "mempalace_kg_add"
_KG_QUERY = "mempalace_kg_query"
_WARNING = "historical projection; use mptask_get for current state"
_CHECKPOINT_KIND = "mptask.projection.checkpoint"
_RESET_KIND = "mptask.projection.reset"
_LITERAL_PREDICATES = {"evidence", "owner", "source_event", "source_drawer"}
_MISSING = object()
_PART_PAYLOAD_CHARACTERS = 90000
_DRAWER_CHARACTER_LIMIT = 100000
_OPERATIONS = {
    "authority_create", "create", "update", "claim", "renew", "checkpoint",
    "attempt_report", "expire", "release", "recover", "transition",
    "add_dependency", "remove_dependency", "note", "bootstrap", "expand", "goal_close",
}
_EVENT_FIELDS = {
    "schema_version", "authority_id", "kind", "command", "at", "tasks",
    "edges_added", "edges_removed", "resources", "configuration", "changes", "response",
}
_CHECKPOINT_FIELDS = {
    "kind", "schema_version", "authority_id", "event_id", "ordinal", "event_hash",
}
_RESERVED_FILES = {
    "authority.lock", "pending.json", "verified_head.json", "clock.json",
    "execution.json", "handles.json",
}


class ProjectionError(Exception):
    """An explicit projection-lane failure; task authority remains unaffected."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(details or {})

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": deepcopy(self.details)}


@dataclass(frozen=True)
class ProjectionRecord:
    """A trusted accepted-event delivery, NOT a raw proposal/settlement receipt."""

    event_id: str
    ordinal: int
    event: dict


def _need(condition, code, message):
    if not condition:
        raise ProjectionError(code, message)


def _text(value):
    return type(value) is str and bool(value.strip())


def _references(value):
    return type(value) is list and all(_text(item) for item in value)


def _kg_valid_from(at):
    value = instant(at)
    if value.microsecond:
        try:
            # Legacy KG accepts whole seconds only. Never assert historical
            # validity before the exact source event; defer it by less than 1s.
            value = value.replace(microsecond=0) + timedelta(seconds=1)
        except OverflowError as error:
            raise ProjectionError("invalid_record", "KG validity ceiling exceeds UTC range") from error
    return utc(value)


def _kg_fact(prefix, valid_from, subject, predicate, obj):
    identity = hashlib.sha256(canonical_json([subject, predicate, obj]).encode()).hexdigest()
    return {"subject": subject, "predicate": predicate, "object": obj,
            "valid_from": valid_from, "source_file": f"{prefix}/kg/{identity}"}


def _kg_facts(prefix, valid_from, subject, predicate, obj):
    if predicate not in _LITERAL_PREDICATES:
        return [_kg_fact(prefix, valid_from, subject, predicate, obj)]
    encoded = obj.encode("utf-8")
    literal = f"mptask:literal:{predicate}:" + hashlib.sha256(encoded).hexdigest()
    source_hash = hashlib.sha256(canonical_json([prefix, literal]).encode()).hexdigest()
    source = "mptask:literal-source:" + source_hash
    # All KG names are <=128 characters and survive the hub's case/space/
    # apostrophe normalization. Byte chunks remain lossless when reassembled.
    parts = [encoded[offset:offset + 48].hex() for offset in range(0, len(encoded), 48)]
    triples = [
        (subject, predicate, literal),
        (literal, "has_literal_source", source),
        (source, "projected_from", subject),
        (source, "value_parts", f"mptask:count:{len(parts)}"),
        *((source, f"value_part_{index:06d}", f"mptask:utf8:{part}")
          for index, part in enumerate(parts, 1)),
    ]
    return [_kg_fact(prefix, valid_from, src, relation, target)
            for src, relation, target in triples]


def _part_facts(revision, drawer, drawer_id, valid_from):
    marker = drawer["source_file"]
    prefix = marker.partition("/drawer/")[0]
    part_entity = "mptask:part:" + hashlib.sha256(marker.encode()).hexdigest()
    return [_kg_fact(prefix, valid_from, revision, "has_projection_part", part_entity),
            *_kg_facts(prefix, valid_from, part_entity, "source_drawer", drawer_id)]


def _checkpoint(value):
    if value is None:
        return None
    code = "corrupt_checkpoint"
    if type(value) is dict and value.get("kind") == _RESET_KIND:
        _need(set(value) == {"kind", "schema_version", "authority_id"}
              and type(value["schema_version"]) is int and value["schema_version"] == 1,
              code, "Malformed projection reset marker")
        if value["authority_id"] is not None:
            try:
                identifier(value["authority_id"], "authority_id")
            except DomainError as error:
                raise ProjectionError(code, error.message) from error
        return deepcopy(value)
    _need(type(value) is dict and set(value) == _CHECKPOINT_FIELDS, code,
          "Projection checkpoint has an unexpected shape")
    _need(value["kind"] == _CHECKPOINT_KIND
          and type(value["schema_version"]) is int and value["schema_version"] == 1
          and type(value["ordinal"]) is int and value["ordinal"] > 0
          and _text(value["event_id"]) and type(value["event_hash"]) is str
          and re.fullmatch(r"[0-9a-f]{64}", value["event_hash"]) is not None,
          code, "Projection checkpoint has invalid identity or hash fields")
    try:
        identifier(value["authority_id"], "authority_id")
    except DomainError as error:
        raise ProjectionError(code, error.message) from error
    return deepcopy(value)


def _validate_record(record):
    code = "invalid_record"
    _need(isinstance(record, ProjectionRecord), code, "Expected an accepted ProjectionRecord")
    _need(_text(record.event_id) and type(record.ordinal) is int and record.ordinal > 0,
          code, "Event ID and positive accepted ordinal are required")
    event = record.event
    _need(type(event) is dict and set(event) == _EVENT_FIELDS, code,
          "Expected a domain event, not a raw proposal or settlement")
    try:
        encoded = canonical_json(event)
        canonical_json(record.event_id)
        identifier(event["authority_id"], "authority_id")
        _need(utc(instant(event["at"])) == event["at"], code, "Event time must be canonical UTC")
    except DomainError as error:
        raise ProjectionError(code, error.message) from error
    _need(len(encoded.encode("utf-8")) <= 240 * 1024, code, "Domain event exceeds 240 KiB")
    _need(type(event["schema_version"]) is int and event["schema_version"] == 1
          and _text(event["kind"]) and type(event["command"]) is dict
          and type(event["command"].get("operation")) is str
          and event["command"]["operation"] in _OPERATIONS, code,
          "Unknown domain event schema or canonical operation")
    command = event["command"]
    _need(all(type(command[field]) is str for field in ("text", "summary", "reason")
              if field in command)
          and _references(command.get("references", []))
          and ("evidence" not in command or type(command["evidence"]) is dict
               or _references(command["evidence"])),
          code, "Malformed command projection text or evidence")
    _need(type(event["tasks"]) is list and type(event["edges_added"]) is list
          and type(event["edges_removed"]) is list and type(event["resources"]) is dict
          and type(event["changes"]) is dict and type(event["response"]) is dict,
          code, "Malformed domain event deltas")
    _need(event["response"].get("tasks") == event["tasks"], code,
          "Event task snapshots and response disagree")
    task_ids = set()
    for task in event["tasks"]:
        _need(type(task) is dict and _text(task.get("id")) and _text(task.get("project"))
              and type(task.get("version")) is int and task["version"] > 0
              and type(task.get("status")) is str
              and task.get("status") in {
                  "open", "in_progress", "recovering", "quarantined", "closed", "cancelled"}
              and type(task.get("priority")) is int and 0 <= task["priority"] <= 4
              and all(type(task.get(field)) is str for field in
                      ("title", "description", "acceptance"))
              and task["id"] not in task_ids, code, "Malformed or duplicate task snapshot")
        _need((task.get("attempt") is None
               or (type(task["attempt"]) is dict and _text(task["attempt"].get("owner"))))
              and (task.get("completion") is None
                   or (type(task["completion"]) is dict
                       and _references(task["completion"].get("evidence"))))
              and (task.get("assignee") is None or _text(task["assignee"]))
              and (task.get("goal_id") is None or _text(task["goal_id"])),
              code, "Malformed task ownership, goal or completion evidence")
        task_ids.add(task["id"])
    for edge in event["edges_added"] + event["edges_removed"]:
        _need(type(edge) is dict and set(edge) == {"source", "target", "edge_type"}
              and all(_text(value) for value in edge.values()),
              code, "Malformed relationship delta")
    return deepcopy(event), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _schema_arguments(schema, arguments):
    """Fail closed on the simple, advertised projection string schemas."""
    code = "unsupported_capability"
    annotations = {"description", "title", "default", "examples", "$schema"}
    _need(type(schema) is dict and schema.get("type") == "object"
          and set(schema) <= annotations | {
              "type", "properties", "required", "additionalProperties"},
          code, "Unsupported projection tool object schema")
    properties, required = schema.get("properties"), schema.get("required", [])
    _need(type(properties) is dict and type(required) is list
          and all(type(key) is str and key in properties for key in required)
          and len(required) == len(set(required))
          and type(schema.get("additionalProperties", True)) is bool,
          code, "Malformed projection tool properties or required fields")
    _need(set(required) <= set(arguments) <= set(properties), code,
          "Projection tool does not support the required argument set")
    for field, value in arguments.items():
        rule = properties[field]
        _need(type(rule) is dict and rule.get("type") == "string"
              and set(rule) <= annotations | {"type", "enum", "minLength", "maxLength"},
              code, f"Unsupported projection argument schema: {field}")
        _need(type(value) is str, code, f"Projection argument must be text: {field}")
        for key in ("minLength", "maxLength"):
            if key in rule:
                _need(type(rule[key]) is int and rule[key] >= 0, code,
                      f"Malformed projection schema bound: {field}")
        if "minLength" in rule:
            _need(len(value) >= rule["minLength"], code, f"Projection argument too short: {field}")
        if "maxLength" in rule:
            _need(len(value) <= rule["maxLength"], code, f"Projection argument too long: {field}")
        if "enum" in rule:
            _need(type(rule["enum"]) is list
                  and all(type(item) is str for item in rule["enum"])
                  and value in rule["enum"], code, f"Unsupported projection enum: {field}")


class TaskProjector:
    """A bounded, opt-in derived-output lane with a separate durable cursor.

    project_event/run_batch raise ProjectionError and pause health on failure.
    An explicit subsequent call retries the same event; no background retry is
    scheduled. reset durably replaces only the projection checkpoint with a
    reset marker, never unlinks recovery state or changes remote history.
    Rebuild by replaying accepted records starting at ordinal 1.
    """

    def __init__(self, client, checkpoint_store, project_wings):
        _need(isinstance(checkpoint_store, JsonStore), "invalid_configuration",
              "Projection checkpoint_store must be a dedicated JsonStore")
        _need(checkpoint_store.path.name not in _RESERVED_FILES
              and not checkpoint_store.path.name.startswith("pending.archive-"),
              "invalid_configuration", "Authority state cannot be a projection checkpoint")
        _need(isinstance(project_wings, Mapping)
              and all(_text(key) and _text(value) for key, value in project_wings.items()),
              "invalid_configuration", "project_wings must map explicit projects to wings")
        self._client = client
        self._store = checkpoint_store
        self._wings = dict(project_wings)
        self._lock = threading.RLock()
        self._paused = False
        self._last_error = None
        self._schemas = None
        self._capabilities = None
        try:
            self._stored = self._read_checkpoint()
        except JournalError as error:
            raise ProjectionError(error.code, error.message) from error
        self._checkpoint = (self._stored if self._stored is not None
                            and self._stored["kind"] == _CHECKPOINT_KIND else None)

    def _read_checkpoint(self):
        value = self._store.read(default=_MISSING)
        if value is _MISSING:
            return None
        _need(value is not None, "corrupt_checkpoint", "JSON null is not a projection checkpoint")
        return _checkpoint(value)

    def _check_store(self, next_checkpoint):
        current = self._read_checkpoint()
        _need(current == self._stored or current == next_checkpoint,
              "checkpoint_mismatch", "Projection checkpoint changed outside this projector")

    def health(self):
        with self._lock:
            return deepcopy({
                "enabled": True, "paused": self._paused, "checkpoint": self._checkpoint,
                "last_error": self._last_error, "capabilities": self._capabilities,
                "delivery": "at_least_once",
            })

    def _retry(self, operation, action):
        for attempt in range(1, 4):
            try:
                return action()
            except (PalaceError, JournalError, ProjectionError) as error:
                if attempt == 3:
                    details = deepcopy(getattr(error, "details", {}))
                    details.update(operation=operation, attempts=attempt,
                                   ambiguous=getattr(error, "ambiguous", False))
                    raise ProjectionError(error.code, error.message, details=details) from error

    def _call(self, name, arguments):
        _schema_arguments(self._schemas[name], arguments)
        result = self._client.call_tool(name, arguments)
        _need(type(result) is dict, "invalid_response", f"{name} returned a non-object")
        _need(result.get("success") is not False and not result.get("error"),
              "upstream_error", f"{name} returned an unsuccessful result")
        return result

    def _discover(self):
        if self._schemas is not None:
            return
        result = self._retry("discover", self._client.discover)
        tools = result.get("tools") if type(result) is dict else None
        _need(type(tools) is dict, "unsupported_capability", "Discovery did not return tools")
        samples = {
            _ADD: {"wing": "project", "room": "tasks", "content": "projection",
                   "source_file": "projection", "added_by": "mempalace-tasks"},
            _GET: {"drawer_id": "drawer"},
            _KG_ADD: {"subject": "revision", "predicate": "status", "object": "open",
                      "valid_from": "2026-09-23T12:00:00Z", "source_file": "projection"},
            _KG_QUERY: {"entity": "revision", "direction": "outgoing"},
        }
        schemas = {}
        for name, sample in samples.items():
            tool = tools.get(name)
            _need(type(tool) is dict and tool.get("name") == name, "unsupported_capability",
                  f"Required optional projection tool is missing: {name}")
            schema = tool.get("inputSchema")
            if name == _KG_ADD and type(schema) is dict:
                properties = schema.get("properties")
                if type(properties) is dict and "source_drawer_id" in properties:
                    sample["source_drawer_id"] = "drawer"
            _schema_arguments(schema, sample)
            dynamic = ("drawer_id" if name == _GET else
                       "source_drawer_id" if name == _KG_ADD and "source_drawer_id" in sample else None)
            if dynamic is not None:
                _need(set(schema["properties"][dynamic]) <= {
                    "type", "description", "title", "default", "examples", "$schema"},
                    "unsupported_capability",
                    f"Projection capability restricts an opaque returned drawer ID: {dynamic}")
            schemas[name] = deepcopy(schema)
        self._schemas = schemas
        self._capabilities = {
            "drawers": True, "revision_kg": True,
            "drawer_provenance": "source_drawer_id" in schemas[_KG_ADD]["properties"],
        }

    def _plans(self, record, event):
        operation = event["command"]["operation"]
        selected = operation in {"create", "bootstrap", "expand", "note", "goal_close"}
        tasks = [task for task in event["tasks"]
                 if selected or task["status"] in {"closed", "cancelled"}]
        # Lease/checkpoint traffic must never become embedding work, even if a
        # future domain extension includes unchanged terminal snapshots.
        if operation in {"authority_create", "claim", "renew", "checkpoint"}:
            tasks = []
        plans = []
        authority = event["authority_id"]
        prefix = f"mptask-projection:v1/{authority}/{quote(record.event_id, safe='')}"
        valid_from = _kg_valid_from(event["at"]) if tasks else None
        for task in tasks:
            wing = self._wings.get(task["project"])
            _need(wing is not None, "missing_project_mapping",
                  f"No projection wing configured for project: {task['project']}")
            marker = f"{prefix}/drawer/{quote(task['id'], safe='')}/v{task['version']}"
            lines = [
                _WARNING, f"source_file: {marker}", f"authority: {authority}",
                f"task: {task['id']}", f"version: {task['version']}",
                f"event: {record.event_id}", f"ordinal: {record.ordinal}",
                f"operation: {operation}", f"at: {event['at']}",
                "title:", task["title"], "description:", task["description"],
                "acceptance:", task["acceptance"],
            ]
            for label in ("text", "summary", "reason"):
                if label in event["command"]:
                    lines.extend(["note:" if label == "text" else f"{label}:",
                                  event["command"][label]])
            lines.extend(["accepted_event:", canonical_json(event)])
            payload = "\n".join(lines)
            payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            part_count = (len(payload) + _PART_PAYLOAD_CHARACTERS - 1) // _PART_PAYLOAD_CHARACTERS
            drawers = []
            for index in range(part_count):
                offset = index * _PART_PAYLOAD_CHARACTERS
                part_marker = f"{marker}/part-{index + 1:06d}-of-{part_count:06d}"
                header = "\n".join([
                    _WARNING, f"source_file: {part_marker}", f"authority: {authority}",
                    f"task: {task['id']}", f"version: {task['version']}",
                    f"event: {record.event_id}", f"ordinal: {record.ordinal}",
                    f"operation: {operation}", f"at: {event['at']}",
                    f"part: {index + 1}/{part_count}", f"payload_offset: {offset}",
                    f"payload_length: {len(payload)}", f"payload_sha256: {payload_hash}",
                    "projection_payload:", "",
                ])
                content = header + payload[offset:offset + _PART_PAYLOAD_CHARACTERS]
                _need(len(content) <= _DRAWER_CHARACTER_LIMIT, "invalid_record",
                      "Projection source markers exceed the drawer part header budget")
                drawers.append({"wing": wing, "room": "tasks", "content": content,
                                "added_by": "mempalace-tasks", "source_file": part_marker})
            stable = f"mptask:{authority}:{task['id']}"
            revision = f"{stable}:v{task['version']}"
            triples = [
                (stable, "has_revision", revision),
                (revision, "status", task["status"]),
                (revision, "priority", str(task["priority"])),
                (revision, "source_event", record.event_id),
                (revision, "source_ordinal", str(record.ordinal)),
                (revision, "source_at", event["at"]),
            ]
            owner = task.get("assignee") or (task.get("attempt") or {}).get("owner")
            if owner:
                triples.append((revision, "owner", owner))
            evidence = list((task.get("completion") or {}).get("evidence", []))
            evidence.extend(event["command"].get("references", []))
            if type(event["command"].get("evidence")) is list:
                evidence.extend(event["command"]["evidence"])
            triples.extend((revision, "evidence", reference) for reference in sorted(set(evidence)))
            if task.get("goal_id"):
                triples.append((revision, "goal", f"mptask:{authority}:{task['goal_id']}"))
            for key, removed in (("edges_added", False), ("edges_removed", True)):
                for edge in event[key]:
                    if edge["source"] == task["id"]:
                        predicate = ("removed_" if removed else "") + edge["edge_type"]
                        triples.append((revision, predicate, f"mptask:{authority}:{edge['target']}"))
            facts = [fact for subject, predicate, obj in sorted(set(triples))
                     for fact in _kg_facts(prefix, valid_from, subject, predicate, obj)]
            plans.append((drawers, facts, revision))
        return plans

    @staticmethod
    def _check_drawer(row, drawer_id, expected):
        metadata = row.get("metadata", {})
        source = expected["source_file"]
        # The installed hub returns only Path(source_file).name. The complete
        # immutable identity is verified as part of the exact content instead.
        _need(row.get("drawer_id") == drawer_id and row.get("content") == expected["content"]
              and row.get("wing") == expected["wing"] and row.get("room") == expected["room"]
              and type(metadata) is dict
              and ("source_file" not in metadata
                   or metadata["source_file"] in (source, source.rsplit("/", 1)[-1]))
              and ("added_by" not in metadata or metadata["added_by"] == expected["added_by"])
              and f"source_file: {source}\n" in expected["content"],
              "drawer_mismatch", "Drawer readback did not match exact content, location and source")

    def _drawer(self, arguments):
        receipt = self._call(_ADD, arguments)
        drawer_id = receipt.get("drawer_id")
        _need(receipt.get("success") is True and _text(drawer_id), "invalid_response",
              "Drawer write did not return a successful drawer ID")
        row = self._call(_GET, {"drawer_id": drawer_id})
        self._check_drawer(row, drawer_id, arguments)
        return drawer_id

    def _fact(self, arguments):
        receipt = self._call(_KG_ADD, arguments)
        _need(receipt.get("success") is True and _text(receipt.get("triple_id")),
              "invalid_response", "KG write did not return a successful triple ID")
        result = self._call(_KG_QUERY, {"entity": arguments["subject"], "direction": "outgoing"})
        facts = result.get("facts")
        _need(result.get("entity") == arguments["subject"] and type(facts) is list
              and all(type(fact) is dict for fact in facts)
              and type(result.get("count")) is int and result["count"] == len(facts),
              "invalid_response", "KG query returned malformed or incomplete facts")
        matches = [fact for fact in facts
                   if all(fact.get(key) == arguments[key]
                          for key in ("subject", "predicate", "object", "valid_from"))
                   and "valid_to" in fact and fact["valid_to"] is None
                   and fact.get("direction") == "outgoing"
                   and fact.get("current") is True]
        # MemPalace 3.8.0 queries omit source_file and source_drawer_id. Validate
        # them when exposed, but do not invent a readback field/capability.
        matches = [fact for fact in matches
                   if "source_file" not in fact or fact["source_file"] == arguments["source_file"]]
        _need(bool(matches), "kg_mismatch", "KG readback did not match the exact triple and validity")

    def _persist(self, checkpoint):
        self._check_store(checkpoint)
        self._store.write(checkpoint)
        _need(self._read_checkpoint() == checkpoint, "checkpoint_mismatch",
              "Projection checkpoint readback differs from the confirmed event")

    def _project_event(self, record):
        event, event_hash = _validate_record(record)
        next_checkpoint = {
            "kind": _CHECKPOINT_KIND, "schema_version": 1,
            "authority_id": event["authority_id"], "event_id": record.event_id,
            "ordinal": record.ordinal, "event_hash": event_hash,
        }
        self._retry("checkpoint_read", lambda: self._check_store(next_checkpoint))
        if self._checkpoint is not None and next_checkpoint == self._checkpoint:
            # Re-fsync even a replay: an earlier process may have observed a
            # replaced checkpoint whose directory fsync subsequently failed.
            self._retry("checkpoint", lambda: self._persist(next_checkpoint))
            return {"event_id": record.event_id, "ordinal": record.ordinal, "drawers": [],
                    "triples": 0, "skipped": True, "replayed": True}
        prior = self._checkpoint
        _need(record.ordinal == (prior["ordinal"] + 1 if prior else 1)
              and (prior is None or (prior["authority_id"] == event["authority_id"]
                                    and prior["event_id"] != record.event_id))
              and (self._stored is None or self._stored["authority_id"] is None
                   or self._stored["authority_id"] == event["authority_id"]),
              "checkpoint_mismatch", "Accepted event identity, hash or ordinal does not follow checkpoint")
        plans = self._plans(record, event)
        if plans:
            self._discover()
            for parts, facts, revision in plans:
                planned_facts = list(facts)
                for drawer in parts:
                    _schema_arguments(self._schemas[_ADD], drawer)
                    planned_facts.extend(_part_facts(
                        revision, drawer, "drawer", facts[0]["valid_from"]))
                for fact in planned_facts:
                    candidate = dict(fact)
                    if self._capabilities["drawer_provenance"]:
                        candidate["source_drawer_id"] = "drawer"
                    _schema_arguments(self._schemas[_KG_ADD], candidate)
                    _schema_arguments(self._schemas[_KG_QUERY],
                                      {"entity": fact["subject"], "direction": "outgoing"})
        drawers, count = [], 0
        for parts, facts, revision in plans:
            part_ids = []
            for drawer in parts:
                drawer_id = self._retry(_ADD, lambda: self._drawer(drawer))
                part_ids.append(drawer_id)
                drawers.append(drawer_id)
            sourced_facts = [(fact, part_ids[0]) for fact in facts]
            for drawer, drawer_id in zip(parts, part_ids):
                sourced_facts.extend(
                    (fact, drawer_id) for fact in
                    _part_facts(revision, drawer, drawer_id, facts[0]["valid_from"]))
            for fact, source_drawer in sourced_facts:
                if self._capabilities["drawer_provenance"]:
                    fact["source_drawer_id"] = source_drawer
                self._retry(_KG_ADD, lambda: self._fact(fact))
                count += 1
        self._retry("checkpoint", lambda: self._persist(next_checkpoint))
        self._stored = next_checkpoint
        self._checkpoint = next_checkpoint
        return {"event_id": record.event_id, "ordinal": record.ordinal, "drawers": drawers,
                "triples": count, "skipped": not bool(plans), "replayed": False}

    def project_event(self, record):
        with self._lock:
            try:
                result = self._project_event(record)
            except ProjectionError as error:
                self._paused = True
                self._last_error = error.to_dict()
                raise
            self._paused, self._last_error = False, None
            return result

    def run_batch(self, records, limit=100):
        with self._lock:
            _need(type(limit) is int and 1 <= limit <= 100, "invalid_limit",
                  "Projection batch limit must be an integer from 1 to 100")
            results = [self.project_event(record) for record in islice(records, limit)]
            return {"processed": len(results), "results": results,
                    "checkpoint": deepcopy(self._checkpoint)}

    def reset(self):
        with self._lock:
            try:
                # Refuse a replaced/corrupt/foreign file, even on explicit reset.
                marker = {"kind": _RESET_KIND, "schema_version": 1,
                          "authority_id": self._stored["authority_id"] if self._stored else None}
                # Replacing and syncing the marker on EVERY retry avoids a
                # missing-file shortcut after an ambiguous unlink/fsync error.
                self._retry("reset", lambda: self._persist(marker))
            except (ProjectionError, JournalError) as error:
                failure = (error if isinstance(error, ProjectionError)
                           else ProjectionError(error.code, error.message))
                self._paused, self._last_error = True, failure.to_dict()
                if failure is error:
                    raise
                raise failure from error
            self._stored, self._checkpoint = marker, None
            self._schemas, self._capabilities = None, None
            self._paused, self._last_error = False, None
