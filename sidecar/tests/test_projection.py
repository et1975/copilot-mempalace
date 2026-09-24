"""Owned projection transport fixtures; no user palace or service is contacted."""

from copy import deepcopy
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from mempalace_tasks.domain import apply_event, decide
from mempalace_tasks.journal import JournalError, JsonStore
from mempalace_tasks.model import new_state, state_to_dict
from mempalace_tasks.palace import PalaceError
from mempalace_tasks.projection import ProjectionError, ProjectionRecord, TaskProjector


AUTHORITY = "11111111-1111-4111-8111-111111111111"
AT = "2026-09-23T12:00:00Z"
WARNING = "historical projection; use mptask_get for current state"
ADD = "mempalace_add_drawer"
GET = "mempalace_get_drawer"
KG_ADD = "mempalace_kg_add"
KG_QUERY = "mempalace_kg_query"


def descriptor(name, fields, required):
    return {
        "name": name,
        "description": "Owned fixture of the installed MemPalace 3.8.0 contract.",
        "inputSchema": {
            "type": "object",
            "properties": {field: {"type": "string"} for field in fields},
            "required": required,
        },
    }


class OwnedTransport:
    """Precise PalaceClient boundary, including the KG query's provenance gap."""

    def __init__(self):
        self.tools = {
            ADD: descriptor(ADD, ["wing", "room", "content", "added_by", "source_file"],
                            ["wing", "room", "content"]),
            GET: descriptor(GET, ["drawer_id"], ["drawer_id"]),
            KG_ADD: descriptor(
                KG_ADD, ["subject", "predicate", "object", "valid_from", "valid_to",
                         "source_closet", "source_file", "source_drawer_id"],
                ["subject", "predicate", "object"]),
            KG_QUERY: descriptor(KG_QUERY, ["entity", "as_of", "direction"], ["entity"]),
        }
        self.calls = []
        self.drawers = {}
        self.triples = []
        self.entities = {}
        self.failures = {}
        self.readback_changes = {}
        self.late_writes = []
        self.lost_drawer_responses = 0
        self.physical_duplicates = False
        self.candidate_id = None
        self.fail_part = None
        self.part_failures = 0
        self.corrupt_part = None
        self.drawer_prefix = "drawer_"

    @staticmethod
    def entity_id(value):
        return value.lower().replace(" ", "_").replace("'", "")

    @staticmethod
    def kg_value(value):
        value = value.strip()
        if not value or len(value) > 128 or "\x00" in value:
            raise PalaceError("upstream_error", "KG entity must contain 1..128 non-NUL characters")
        return value

    def discover(self):
        self.calls.append(("discover", {}))
        self._fail("discover")
        return {"profile": "mempalace-ordered-v1", "tools": deepcopy(self.tools)}

    def _fail(self, name):
        if self.failures.get(name, 0):
            self.failures[name] -= 1
            raise PalaceError("fixture_unavailable", f"{name} unavailable", ambiguous=True)

    def call_tool(self, name, arguments):
        if name not in self.tools:
            raise AssertionError(f"Unadvertised tool {name}")
        schema = self.tools[name]["inputSchema"]
        if not set(schema["required"]) <= set(arguments) <= set(schema["properties"]):
            raise AssertionError(f"Invalid fields for {name}: {arguments}")
        if any(type(value) is not str for value in arguments.values()):
            raise AssertionError("Projection tools take only strings")
        self.calls.append((name, deepcopy(arguments)))
        self._fail(name)
        if name == ADD:
            if arguments["room"] != "tasks" or arguments["added_by"] != "mempalace-tasks":
                raise AssertionError("Incorrect drawer routing")
            if len(arguments["content"]) > 100000 or "\x00" in arguments["content"]:
                raise PalaceError("upstream_error", "Drawer content violates hub limits")
            if self.part_failures and f"/part-{self.fail_part:06d}-of-" in arguments["source_file"]:
                self.part_failures -= 1
                raise PalaceError("part_unavailable", "Selected drawer part unavailable", ambiguous=True)
            if self.candidate_id is not None:
                return {"success": True, "reason": "already_exists",
                        "drawer_id": self.candidate_id}
            drawer_id = self.drawer_prefix + hashlib.sha256(
                (arguments["wing"] + arguments["room"] + arguments["content"]).encode()
            ).hexdigest()
            if self.physical_duplicates:
                drawer_id += "_" + str(len(self.calls))
            row = {
                "drawer_id": drawer_id, "content": arguments["content"],
                "wing": arguments["wing"], "room": arguments["room"],
                "salience": {"strength": 1.0},
                "metadata": {
                    "wing": arguments["wing"], "room": arguments["room"],
                    "source_file": arguments["source_file"], "added_by": arguments["added_by"],
                },
            }
            if self.lost_drawer_responses:
                self.lost_drawer_responses -= 1
                self.late_writes.append(row)
                raise PalaceError("upstream_timeout", "Drawer reply lost", ambiguous=True)
            self.drawers[drawer_id] = row
            return {"success": True, "drawer_id": drawer_id,
                    "wing": arguments["wing"], "room": arguments["room"], "chunks": 1}
        if name == GET:
            row = deepcopy(self.drawers[arguments["drawer_id"]])
            if self.corrupt_part is not None and (
                    f"/part-{self.corrupt_part:06d}-of-" in row["metadata"]["source_file"]):
                row["content"] += "unexpected suffix"
            row["metadata"]["source_file"] = Path(row["metadata"]["source_file"]).name
            row.update(deepcopy(self.readback_changes))
            return row
        if name == KG_ADD:
            if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", arguments["valid_from"]):
                raise PalaceError("upstream_error", "KG requires whole-second UTC validity")
            arguments = dict(arguments)
            for field in ("subject", "object"):
                arguments[field] = self.kg_value(arguments[field])
                self.entities.setdefault(self.entity_id(arguments[field]), arguments[field])
            arguments["predicate"] = arguments["predicate"].strip().lower().replace(" ", "_")
            if not any(
                    self.entity_id(row["subject"]) == self.entity_id(arguments["subject"])
                    and row["predicate"] == arguments["predicate"]
                    and self.entity_id(row["object"]) == self.entity_id(arguments["object"])
                    for row in self.triples):
                self.triples.append(deepcopy(arguments))
            return {"success": True, "triple_id": "triple_fixture",
                    "fact": f"{arguments['subject']} → {arguments['predicate']} → {arguments['object']}"}
        if name == KG_QUERY:
            if arguments != {"entity": arguments["entity"], "direction": "outgoing"}:
                raise AssertionError("Query must use exact entity and outgoing direction")
            entity = self.kg_value(arguments["entity"])
            facts = [
                {"direction": "outgoing", "subject": entity,
                 "predicate": row["predicate"],
                 "object": self.entities.get(self.entity_id(row["object"]), row["object"]),
                 "valid_from": row["valid_from"], "valid_to": None, "confidence": 1.0,
                 "source_closet": row.get("source_closet"), "current": True}
                for row in self.triples if self.entity_id(row["subject"]) == self.entity_id(entity)
            ]
            return {"entity": entity, "as_of": None,
                    "facts": facts, "count": len(facts)}
        raise AssertionError(f"Unexpected tool {name}")

    def count(self, name):
        return sum(call_name == name for call_name, _ in self.calls)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("MPTASK_TEST_TMPDIR")
        if not root:
            self.fail("Set MPTASK_TEST_TMPDIR to the session files directory")
        self.directory = tempfile.TemporaryDirectory(prefix="projection-owned-", dir=root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = JsonStore(self.root / "projection.json")
        self.client = OwnedTransport()
        self.projector = TaskProjector(self.client, self.store, {"project": "test-project"})
        self.state = new_state(AUTHORITY)
        self.records = []
        self.emit(
            "authority_create",
            actors={"op": "operator", "worker": "worker", "sup": "supervisor"},
            execution_profiles={"local": {"execution_class": "isolated"}},
            supervisors={"sup": {"profiles": ["local"], "workers": ["worker"]}},
        )

    def emit(self, operation, actor="op", at=AT, **fields):
        ordinal = len(self.records) + 1
        command = {"operation": operation, "command_id": str(UUID(int=ordinal)),
                   "actor": actor, **fields}
        event = decide(self.state, command, at)
        self.state = apply_event(self.state, event)
        record = ProjectionRecord(f"opaque/event {ordinal}", ordinal, event)
        self.records.append(record)
        return record

    def create(self, **fields):
        return self.emit("create", project="project", kind="task",
                         title="Exact title café", description="  Exact\n description  ",
                         acceptance="  pass ✓\n", execution_class="isolated",
                         execution_profile="local", **fields)

    def current(self, task_id):
        return {"task_id": task_id, "expected_version": self.state.tasks[task_id]["version"]}

    def tokens(self, task_id):
        task = self.state.tasks[task_id]
        return {**self.current(task_id), "attempt_id": task["attempt"]["id"],
                "claim_generation": task["claim_generation"]}

    def bootstrap(self):
        return self.emit("bootstrap", project="project", title="Goal", description="Goal text",
                         acceptance="Accepted", planning_task={
                             "title": "Plan", "description": "Plan text", "acceptance": "Bounded",
                             "execution_class": "isolated", "execution_profile": "local"},
                         goal_policy={"scope": "this project"})

    def large_expansion(self):
        goal = self.bootstrap().event["response"]["goal_id"]
        return self.emit(
            "expand", goal_id=goal, expected_graph_revision=1, source_disposition="continue",
            at="2026-09-23T12:00:00.234567Z",
            tasks=[
                {"intent_key": f"large-{index}", "title": f"Large {index}",
                 "description": f"START-{index}\n" + "long café text\n" * 850 + f"\nEND-{index}",
                 "acceptance": "acceptance verbatim\n" * 300,
                 "execution_class": "isolated", "execution_profile": "local"}
                for index in (1, 2)
            ],
        )

    def assert_error(self, code, operation):
        with self.assertRaises(ProjectionError) as raised:
            operation()
        self.assertEqual(code, raised.exception.code)
        health = self.projector.health()
        self.assertTrue(health["paused"])
        self.assertEqual(code, health["last_error"]["code"])
        return raised.exception

    def literal_values(self, entity):
        sources = [row["object"] for row in self.client.triples
                   if row["subject"] == entity and row["predicate"] == "has_literal_source"]
        self.assertTrue(sources, "Literal must have queryable source/value mapping")
        decoded = []
        for source in sources:
            rows = [row for row in self.client.triples if row["subject"] == source]
            count = next(row["object"] for row in rows if row["predicate"] == "value_parts")
            parts = sorted((row["predicate"], row["object"]) for row in rows
                           if row["predicate"].startswith("value_part_"))
            self.assertEqual(int(count.removeprefix("mptask:count:")), len(parts))
            self.assertEqual([f"value_part_{index:06d}" for index in range(1, len(parts) + 1)],
                             [predicate for predicate, _ in parts])
            encoded = "".join(value.removeprefix("mptask:utf8:") for _, value in parts)
            value = bytes.fromhex(encoded).decode("utf-8")
            self.assertTrue(entity.endswith(hashlib.sha256(value.encode("utf-8")).hexdigest()))
            decoded.append(value)
        return set(decoded)

    @contextmanager
    def reset_sync_fault(self, failures):
        real_fsync, real_unlink, real_replace = os.fsync, os.unlink, os.replace
        root = self.root.stat()
        state = {"pending": False, "remaining": failures}
        operations = []

        def unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if path == self.store.path.name:
                state["pending"] = True
                operations.append("unlink")
            return result

        def replace(source, destination, *args, **kwargs):
            result = real_replace(source, destination, *args, **kwargs)
            if destination == self.store.path.name:
                state["pending"] = True
                operations.append("replace")
            return result

        def fsync(fd):
            metadata = os.fstat(fd)
            if state["pending"] and (metadata.st_dev, metadata.st_ino) == (root.st_dev, root.st_ino):
                if state["remaining"]:
                    state["remaining"] -= 1
                    operations.append("sync_failed")
                    raise OSError(errno.EIO, "Injected checkpoint directory fsync failure")
                result = real_fsync(fd)
                state["pending"] = False
                operations.append("synced")
                return result
            return real_fsync(fd)

        with patch("mempalace_tasks.journal.os.unlink", side_effect=unlink), \
                patch("mempalace_tasks.journal.os.replace", side_effect=replace), \
                patch("mempalace_tasks.journal.os.fsync", side_effect=fsync):
            yield operations

    def test_long_and_normalization_colliding_refs_are_lossless_queryable_literals(self):
        task_id = self.create().event["response"]["task_id"]
        references = ["r" * 130, "é" * 1024, "artifact:A", "artifact:a",
                      "artifact:A B", "artifact:a_b", "artifact:a'b", "artifact:ab",
                      "  exact padded reference  "]
        record = self.emit("note", tag="evidence", text="Keep exact references",
                           references=references, **self.current(task_id))
        self.projector.run_batch(self.records)
        revision = f"mptask:{AUTHORITY}:{task_id}:v2"
        entities = {row["object"] for row in self.client.triples
                    if row["subject"] == revision and row["predicate"] == "evidence"}
        self.assertEqual(len(references), len(entities))
        decoded = set()
        for entity in entities:
            self.assertRegex(entity, r"^mptask:literal:evidence:[0-9a-f]{64}$")
            decoded.update(self.literal_values(entity))
        self.assertEqual(set(references), decoded)
        payloads = [row["content"].split("\nprojection_payload:\n", 1)[1]
                    for row in self.client.drawers.values()
                    if f"ordinal: {record.ordinal}\n" in row["content"]]
        self.assertEqual(references, json.loads(payloads[0].split("\naccepted_event:\n", 1)[1])
                         ["command"]["references"])
        repeat = self.emit("note", at="2026-09-23T12:00:02Z", tag="evidence",
                           text="Same values, later revision", references=references,
                           **self.current(task_id))
        self.projector.project_event(repeat)
        for row in self.client.triples:
            self.assertLessEqual(len(row["subject"]), 128)
            self.assertLessEqual(len(row["object"]), 128)
            self.assertIn("source_file", row)
            self.assertIn(row["source_drawer_id"], self.client.drawers)
        self.assertEqual(repeat.ordinal, self.store.read()["ordinal"])

    def test_case_distinct_refs_do_not_collapse_under_native_kg_entity_normalization(self):
        task_id = self.create().event["response"]["task_id"]
        self.emit("note", tag="evidence", text="Both cases",
                  references=["artifact:A", "artifact:a"], **self.current(task_id))
        self.projector.run_batch(self.records)
        evidence = [row["object"] for row in self.client.triples if row["predicate"] == "evidence"]
        self.assertEqual(2, len(evidence))
        self.assertEqual({"artifact:A", "artifact:a"},
                         set().union(*(self.literal_values(entity) for entity in evidence)))

    def test_long_opaque_event_and_drawer_ids_are_bounded_literals(self):
        record = self.create()
        event_id = "Opaque Event'A-" + "Z" * 180
        self.records[-1] = ProjectionRecord(event_id, record.ordinal, record.event)
        self.client.drawer_prefix = "DrawerCase_" + "Q" * 128
        self.projector.run_batch(self.records)
        event_literals = [row["object"] for row in self.client.triples
                          if row["predicate"] == "source_event"]
        self.assertEqual({event_id}, self.literal_values(event_literals[0]))
        drawer_literals = [row["object"] for row in self.client.triples
                           if row["predicate"] == "source_drawer"]
        self.assertEqual(set(self.client.drawers),
                         set().union(*(self.literal_values(entity) for entity in drawer_literals)))
        self.assertTrue(all(len(row[key]) <= 128 for row in self.client.triples
                            for key in ("subject", "object")))

    def test_long_registered_owner_is_preserved_as_a_bounded_literal(self):
        owner = "Worker'A-" + "x" * 130 + "é"
        self.state, self.records = new_state(AUTHORITY), []
        self.emit("authority_create", actors={"op": "operator", owner: "worker", "sup": "supervisor"},
                  execution_profiles={"local": {"execution_class": "isolated"}},
                  supervisors={"sup": {"profiles": ["local"], "workers": [owner]}})
        task_id = self.create().event["response"]["task_id"]
        self.emit("claim", actor=owner, supervisor_id="sup", **self.current(task_id))
        self.emit("note", tag="decision", text="Record owner", **self.current(task_id))
        self.projector.run_batch(self.records)
        owners = {row["object"] for row in self.client.triples if row["predicate"] == "owner"}
        self.assertEqual(1, len(owners))
        entity = owners.pop()
        self.assertLessEqual(len(entity), 128)
        self.assertEqual({owner}, self.literal_values(entity))

    def test_literal_encoding_does_not_weaken_exact_tuple_readback(self):
        task_id = self.create().event["response"]["task_id"]
        self.projector.run_batch(self.records)
        reference = "artifact:Exact"
        entity = "mptask:literal:evidence:" + hashlib.sha256(reference.encode()).hexdigest()
        self.client.entities[entity] = entity.upper()
        record = self.emit("note", tag="evidence", text="Exact namespace conflict",
                           references=[reference], **self.current(task_id))
        self.assert_error("kg_mismatch", lambda: self.projector.project_event(record))
        self.assertEqual(record.ordinal - 1, self.store.read()["ordinal"])

    def test_reset_retries_real_directory_sync_and_reopens_at_ordinal_one(self):
        self.create()
        self.projector.run_batch(self.records)
        with self.reset_sync_fault(1) as operations:
            self.projector.reset()
        self.assertEqual(1, operations.count("sync_failed"))
        self.assertIn("synced", operations, "Reset cannot succeed from unsynced disappearance")
        self.assertNotIn("unlink", operations)
        self.assertEqual("mptask.projection.reset", self.store.read()["kind"])
        reopened = TaskProjector(self.client, self.store, {"project": "test-project"})
        self.assertIsNone(reopened.health()["checkpoint"])
        reopened.run_batch(self.records)
        self.assertEqual(2, self.store.read()["ordinal"])

    def test_reset_sync_exhaustion_is_paused_and_a_later_retry_reestablishes_durability(self):
        self.projector.project_event(self.records[0])
        before = self.projector.health()["checkpoint"]
        with self.reset_sync_fault(3) as operations:
            self.assert_error("io_error", self.projector.reset)
        self.assertEqual(3, operations.count("sync_failed"))
        self.assertEqual(before, self.projector.health()["checkpoint"])
        self.projector.reset()
        self.assertIsNone(self.projector.health()["checkpoint"])
        reopened = TaskProjector(self.client, self.store, {"project": "test-project"})
        self.assertFalse(reopened.project_event(self.records[0])["replayed"])

    def test_reset_marker_validation_and_authority_binding_survive_reopen(self):
        self.projector.project_event(self.records[0])
        self.projector.reset()
        marker = self.store.read()
        reopened = TaskProjector(self.client, self.store, {"project": "test-project"})
        foreign = deepcopy(self.records[0].event)
        foreign["authority_id"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(ProjectionError) as raised:
            reopened.project_event(ProjectionRecord("foreign", 1, foreign))
        self.assertEqual("checkpoint_mismatch", raised.exception.code)
        for mutation in ({"schema_version": True}, {"authority_id": "bad"}, {"extra": 1}):
            with self.subTest(mutation=mutation):
                self.store.write({**marker, **mutation})
                with self.assertRaises(ProjectionError) as raised:
                    TaskProjector(self.client, self.store, {"project": "test-project"})
                self.assertEqual("corrupt_checkpoint", raised.exception.code)

    def test_constructor_is_nonmutating_and_skipped_genesis_persists(self):
        self.assertEqual([], self.client.calls)
        self.assertFalse(self.store.path.exists())
        self.assertTrue(self.projector.health()["enabled"])
        result = self.projector.project_event(self.records[0])
        self.assertTrue(result["skipped"])
        self.assertEqual({}, self.client.drawers)
        self.assertEqual(1, self.store.read()["ordinal"])
        self.assertEqual(0, self.client.count(ADD))

    def test_create_is_verbatim_source_marked_and_exactly_read_back(self):
        record = self.create()
        state_before = state_to_dict(self.state)
        event_before = deepcopy(record.event)
        result = self.projector.run_batch(self.records)
        self.assertEqual(2, result["processed"])
        self.assertEqual(1, len(self.client.drawers))
        row = next(iter(self.client.drawers.values()))
        task = record.event["tasks"][0]
        for exact in (WARNING, task["title"], task["description"], task["acceptance"],
                      f"authority: {AUTHORITY}", f"task: {task['id']}", "version: 1",
                      "event: opaque/event 2", "ordinal: 2", "operation: create", f"at: {AT}"):
            self.assertIn(exact, row["content"])
        self.assertTrue(row["metadata"]["source_file"].startswith(
            f"mptask-projection:v1/{AUTHORITY}/opaque%2Fevent%202/drawer/"))
        self.assertIn(row["metadata"]["source_file"], row["content"])
        self.assertEqual("test-project", row["wing"])
        self.assertEqual("tasks", row["room"])
        self.assertEqual(1, self.client.count(GET))
        self.assertEqual(event_before, record.event)
        self.assertEqual(state_before, state_to_dict(self.state))

    def test_claim_renew_checkpoint_are_skipped_but_note_and_close_project(self):
        task_id = self.create().event["response"]["task_id"]
        self.emit("claim", actor="worker", supervisor_id="sup", **self.current(task_id))
        self.emit("attempt_report", actor="sup", report_kind="started",
                  evidence={"references": ["artifact:prepared"], "prepared": True},
                  **self.tokens(task_id))
        self.emit("renew", actor="sup", task_id=task_id,
                  attempt_id=self.state.tasks[task_id]["attempt"]["id"],
                  claim_generation=1, expected_lease_revision=1)
        self.emit("checkpoint", actor="sup", checkpoint_sequence=1,
                  reference="artifact:checkpoint", **self.tokens(task_id))
        note = self.emit("note", tag="decision", text="  exact note\ncafé ✓  ",
                         references=["artifact:note"], **self.current(task_id))
        close = self.emit("transition", actor="worker", target="closed", summary="Verified\n",
                          evidence=["commit:abc"], **self.tokens(task_id))
        self.projector.run_batch(self.records)
        self.assertEqual(3, len(self.client.drawers))
        contents = [row["content"] for row in self.client.drawers.values()]
        self.assertTrue(any(note.event["command"]["text"] in text for text in contents))
        self.assertTrue(any("Verified\n" in text for text in contents))
        self.assertEqual(close.ordinal, self.store.read()["ordinal"])
        revision = f"mptask:{AUTHORITY}:{task_id}:v{close.event['tasks'][0]['version']}"
        owner = next(row["object"] for row in self.client.triples
                     if row["subject"] == revision and row["predicate"] == "owner")
        evidence = next(row["object"] for row in self.client.triples
                        if row["subject"] == revision and row["predicate"] == "evidence")
        self.assertEqual({"worker"}, self.literal_values(owner))
        self.assertEqual({"commit:abc"}, self.literal_values(evidence))

    def test_bootstrap_expand_and_goal_close_use_canonical_commands(self):
        boot = self.bootstrap()
        goal = boot.event["response"]["goal_id"]
        planner = boot.event["response"]["planning_task_id"]
        self.emit("mptask_expand", goal_id=goal, expected_graph_revision=1,
                  tasks=[{"intent_key": "child", "title": "Child", "description": "Child text",
                          "acceptance": "Child pass", "execution_class": "isolated",
                          "execution_profile": "local"}], source_disposition="continue")
        child = self.records[-1].event["response"]["admitted_task_ids"][0]
        self.emit("transition", target="cancelled", reason="No longer needed",
                  **self.current(child))
        self.emit("claim", actor="worker", supervisor_id="sup", **self.current(planner))
        self.emit("attempt_report", actor="sup", report_kind="started",
                  evidence={"references": ["artifact:p"], "prepared": True}, **self.tokens(planner))
        self.emit("transition", actor="worker", target="closed", summary="Delivered",
                  evidence=["commit:p"], **self.tokens(planner))
        self.emit("goal_close", goal_id=goal, expected_version=self.state.tasks[goal]["version"],
                  expected_graph_revision=self.state.tasks[goal]["graph_revision"],
                  summary="Goal accepted", evidence=["acceptance:verified"])
        self.projector.run_batch(self.records)
        contents = [row["content"] for row in self.client.drawers.values()]
        self.assertEqual(7, len(contents))
        self.assertTrue(any("operation: expand" in text for text in contents))
        self.assertTrue(any("operation: goal_close" in text for text in contents))
        self.assertTrue(any(row["predicate"] == "parent_child"
                            and row["subject"].endswith(f"{goal}:v1")
                            and row["object"] == f"mptask:{AUTHORITY}:{planner}"
                            for row in self.client.triples))

    def test_revision_kg_has_exact_validity_and_drawer_provenance(self):
        record = self.create()
        self.projector.run_batch(self.records)
        task = record.event["tasks"][0]
        stable = f"mptask:{AUTHORITY}:{task['id']}"
        revision = stable + ":v1"
        drawer_id = next(iter(self.client.drawers))
        expected = {(stable, "has_revision", revision), (revision, "status", "open"),
                    (revision, "priority", "2"),
                    (revision, "source_event", "mptask:literal:source_event:"
                     + hashlib.sha256(record.event_id.encode()).hexdigest()),
                    (revision, "source_ordinal", "2")}
        actual = {(row["subject"], row["predicate"], row["object"])
                  for row in self.client.triples}
        self.assertTrue(expected <= actual)
        for triple in self.client.triples:
            self.assertEqual(record.event["at"], triple["valid_from"])
            self.assertEqual(drawer_id, triple["source_drawer_id"])
            self.assertIn("/kg/", triple["source_file"])
            self.assertNotIn("valid_to", triple)
        self.assertEqual(len(self.client.triples),
                         len({row["source_file"] for row in self.client.triples}))
        self.assertFalse(any("current" in row["predicate"] for row in self.client.triples))

    def test_kg_without_optional_drawer_provenance_is_explicitly_supported(self):
        del self.client.tools[KG_ADD]["inputSchema"]["properties"]["source_drawer_id"]
        self.create()
        self.projector.run_batch(self.records)
        self.assertFalse(self.projector.health()["capabilities"]["drawer_provenance"])
        self.assertTrue(all("source_drawer_id" not in row for row in self.client.triples))

    def test_similar_drawer_is_never_treated_as_exact_success(self):
        self.create()
        self.projector.project_event(self.records[0])
        self.client.drawers["unrelated"] = {
            "drawer_id": "unrelated", "content": "Exact title café", "wing": "test-project",
            "room": "tasks", "metadata": {"source_file": "user:original"}}
        self.client.candidate_id = "unrelated"
        self.assert_error("drawer_mismatch", lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(3, self.client.count(GET))
        self.assertEqual(1, self.store.read()["ordinal"])
        self.assertEqual(0, self.client.count(KG_ADD))
        self.assertEqual("user:original", self.client.drawers["unrelated"]["metadata"]["source_file"])

    def test_exact_content_with_wrong_marker_or_location_is_rejected(self):
        self.create()
        self.projector.project_event(self.records[0])
        for mutation in ({"metadata": {"source_file": "different"}},
                         {"wing": "different"}, {"room": "different"},
                         {"drawer_id": "different"}):
            with self.subTest(mutation=mutation):
                self.client.readback_changes = mutation
                self.assert_error("drawer_mismatch",
                                  lambda: self.projector.project_event(self.records[1]))
                self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])

    def test_get_drawer_basename_preserves_full_embedded_source_validation(self):
        record = self.create()
        self.projector.run_batch(self.records)
        row = next(iter(self.client.drawers.values()))
        self.assertIn(row["metadata"]["source_file"], row["content"])
        readback = self.client.call_tool(GET, {"drawer_id": row["drawer_id"]})
        self.assertEqual(Path(row["metadata"]["source_file"]).name,
                         readback["metadata"]["source_file"])
        self.assertEqual(record.ordinal, self.store.read()["ordinal"])

    def test_embedded_source_is_verified_when_optional_metadata_is_omitted(self):
        self.create()
        self.client.readback_changes = {"metadata": {}}
        self.projector.run_batch(self.records)
        self.assertEqual(2, self.store.read()["ordinal"])
        self.projector.reset()
        self.client.readback_changes = {"metadata": {}, "content": "Similar but not exact"}
        self.projector.project_event(self.records[0])
        self.assert_error("drawer_mismatch",
                          lambda: self.projector.project_event(self.records[1]))

    def test_partial_kg_failure_keeps_cursor_then_exact_replay_finishes(self):
        self.create()
        self.projector.project_event(self.records[0])
        self.client.failures[KG_ADD] = 3
        error = self.assert_error("fixture_unavailable",
                                 lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(3, error.details["attempts"])
        self.assertEqual(KG_ADD, error.details["operation"])
        self.assertEqual(1, len(self.client.drawers))
        self.assertEqual(1, self.store.read()["ordinal"])
        self.projector.project_event(self.records[1])
        self.assertEqual(1, len(self.client.drawers))
        self.assertEqual(2, self.store.read()["ordinal"])
        self.assertFalse(self.projector.health()["paused"])
        self.assertIsNone(self.projector.health()["last_error"])

    def test_delayed_duplicate_has_same_logical_identity_at_least_once(self):
        self.create()
        self.client.physical_duplicates = True
        self.client.lost_drawer_responses = 3
        self.projector.project_event(self.records[0])
        self.assert_error("upstream_timeout", lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(1, self.store.read()["ordinal"])
        for row in self.client.late_writes:
            self.client.drawers[row["drawer_id"]] = row
        self.projector.project_event(self.records[1])
        self.assertEqual(4, len(self.client.drawers))
        self.assertEqual(1, len({row["metadata"]["source_file"]
                                for row in self.client.drawers.values()}))
        self.assertEqual("at_least_once", self.projector.health()["delivery"])

    def test_kg_readback_with_wrong_validity_never_advances(self):
        record = self.create()
        task_id = record.event["tasks"][0]["id"]
        self.client.triples.append({
            "subject": f"mptask:{AUTHORITY}:{task_id}", "predicate": "has_revision",
            "object": f"mptask:{AUTHORITY}:{task_id}:v1", "valid_from": "2020-01-01",
            "source_file": "user:existing",
        })
        self.projector.project_event(self.records[0])
        self.assert_error("kg_mismatch", lambda: self.projector.project_event(record))
        self.assertEqual(1, self.store.read()["ordinal"])
        self.assertEqual("2020-01-01", self.client.triples[0]["valid_from"])

    def test_checkpoint_failure_keeps_memory_cursor_and_replay_repairs_durability(self):
        self.create()
        self.projector.project_event(self.records[0])
        real_write = self.store.write

        def persisted_but_unsynced(value):
            real_write(value)
            raise JournalError("io_error", "Checkpoint directory sync failed", ambiguous=True)

        with patch.object(self.store, "write", side_effect=persisted_but_unsynced) as write:
            self.assert_error("io_error", lambda: self.projector.project_event(self.records[1]))
            self.assertEqual(3, write.call_count)
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])
        self.assertEqual(2, self.store.read()["ordinal"])
        self.projector.project_event(self.records[1])
        self.assertEqual(2, self.projector.health()["checkpoint"]["ordinal"])
        restarted = TaskProjector(self.client, self.store, {"project": "test-project"})
        before = len(self.client.calls)
        self.assertTrue(restarted.project_event(self.records[1])["replayed"])
        self.assertEqual(before, len(self.client.calls))

    def test_missing_mapping_fails_before_any_remote_mutation(self):
        self.projector = TaskProjector(self.client, self.store, {})
        self.create()
        self.projector.project_event(self.records[0])
        self.assert_error("missing_project_mapping",
                          lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(0, self.client.count(ADD))

    def test_missing_or_incompatible_capabilities_fail_before_drawer_write(self):
        self.create()
        self.projector.project_event(self.records[0])
        cases = [
            ("missing", lambda tools: tools.pop(KG_QUERY)),
            ("wrong_type", lambda tools: tools[ADD]["inputSchema"]["properties"]
             ["source_file"].update(type="integer")),
            ("unknown_required", lambda tools: tools[KG_ADD]["inputSchema"]
             ["required"].append("source_closet")),
            ("unsupported_schema", lambda tools: tools[ADD]["inputSchema"]["properties"]
             ["content"].update(pattern=".*")),
            ("bound", lambda tools: tools[ADD]["inputSchema"]["properties"]
             ["content"].update(maxLength=5)),
        ]
        for label, change in cases:
            with self.subTest(case=label):
                self.client = OwnedTransport()
                change(self.client.tools)
                self.projector = TaskProjector(self.client, self.store, {"project": "test-project"})
                self.assert_error("unsupported_capability",
                                  lambda: self.projector.project_event(self.records[1]))
                self.assertEqual(0, self.client.count(ADD))

    def test_no_output_events_do_not_need_optional_projection_tools(self):
        self.client.tools = {}
        self.projector.project_event(self.records[0])
        self.assertEqual(0, self.client.count("discover"))
        self.assertEqual(1, self.store.read()["ordinal"])

    def test_raw_and_settled_away_records_cannot_enter_projection(self):
        for raw in ({"record_type": "mptask.settle", "outcome": "abandoned"},
                    {"record_type": "mptask.command", "event": self.records[0].event}):
            with self.subTest(raw=raw):
                self.assert_error("invalid_record", lambda: self.projector.project_event(raw))
                self.assert_error("invalid_record", lambda: self.projector.project_event(
                    ProjectionRecord("raw", 1, raw)))
        self.assertFalse(self.store.path.exists())
        self.assertEqual([], self.client.calls)

    def test_checkpoint_rejects_event_id_ordinal_authority_and_hash_mismatches(self):
        self.create()
        self.projector.run_batch(self.records)
        event = deepcopy(self.records[-1].event)
        event["command"]["description"] = "changed"
        other_authority = deepcopy(self.records[-1].event)
        other_authority["authority_id"] = "22222222-2222-4222-8222-222222222222"
        invalid = [
            ProjectionRecord("different-id", 2, self.records[-1].event),
            ProjectionRecord(self.records[-1].event_id, 2, event),
            ProjectionRecord("next", 4, self.records[-1].event),
            ProjectionRecord(self.records[-1].event_id, 3, self.records[-1].event),
            ProjectionRecord("other", 3, other_authority),
            self.records[0],
        ]
        before = len(self.client.calls)
        for record in invalid:
            with self.subTest(id=record.event_id, ordinal=record.ordinal):
                self.assert_error("checkpoint_mismatch",
                                  lambda: self.projector.project_event(record))
        self.assertEqual(before, len(self.client.calls))
        self.assertEqual(2, self.store.read()["ordinal"])

    def test_noncontiguous_start_and_invalid_ordinals_fail(self):
        for ordinal in (True, 0, -1, 2):
            with self.subTest(ordinal=ordinal):
                code = "invalid_record" if ordinal is True or ordinal <= 0 else "checkpoint_mismatch"
                self.assert_error(code, lambda: self.projector.project_event(
                    ProjectionRecord("event", ordinal, self.records[0].event)))
        self.assertFalse(self.store.path.exists())

    def test_corrupt_checkpoint_is_explicit_not_reset_silently(self):
        self.store.write({"ordinal": 1})
        with self.assertRaises(ProjectionError) as raised:
            TaskProjector(self.client, self.store, {"project": "test-project"})
        self.assertEqual("corrupt_checkpoint", raised.exception.code)
        self.assertEqual({"ordinal": 1}, self.store.read())
        self.store.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ProjectionError) as raised:
            TaskProjector(self.client, self.store, {"project": "test-project"})
        self.assertEqual("corrupt_state", raised.exception.code)

    def test_reset_only_own_checkpoint_and_rebuilds_without_authority_mutation(self):
        authority_files = ("pending.json", "verified_head.json", "authority.lock", "clock.json")
        for name in authority_files:
            (self.root / name).write_text("authority-owned:" + name, encoding="utf-8")
        self.create()
        snapshot = state_to_dict(self.state)
        self.projector.run_batch(self.records)
        old_sources = {row["metadata"]["source_file"] for row in self.client.drawers.values()}
        self.projector.reset()
        self.assertIsNone(self.projector.health()["checkpoint"])
        self.assertEqual({"kind": "mptask.projection.reset", "schema_version": 1,
                          "authority_id": AUTHORITY}, self.store.read())
        self.projector.run_batch(self.records)
        self.assertEqual(old_sources, {row["metadata"]["source_file"]
                                     for row in self.client.drawers.values()})
        self.assertEqual(snapshot, state_to_dict(self.state))
        for name in authority_files:
            self.assertEqual("authority-owned:" + name,
                             (self.root / name).read_text(encoding="utf-8"))

    def test_authority_state_path_cannot_be_used_as_checkpoint(self):
        for name in ("pending.json", "verified_head.json", "authority.lock", "clock.json"):
            with self.subTest(name=name):
                with self.assertRaises(ProjectionError):
                    TaskProjector(self.client, JsonStore(self.root / name), {"project": "test-project"})
                self.assertFalse((self.root / name).exists())

    def test_batch_limit_does_not_consume_extra_records_and_errors_stop_batch(self):
        self.create()
        consumed = []

        def records():
            for record in self.records:
                consumed.append(record.ordinal)
                yield record

        result = self.projector.run_batch(records(), limit=1)
        self.assertEqual([1], consumed)
        self.assertEqual(1, result["processed"])
        self.client.failures[ADD] = 3
        with self.assertRaises(ProjectionError):
            self.projector.run_batch(self.records[1:])
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])
        self.assertEqual(3, self.client.count(ADD))

    def test_health_is_detached_and_discovery_has_bounded_retries(self):
        self.create()
        self.projector.project_event(self.records[0])
        self.client.failures["discover"] = 3
        self.assert_error("fixture_unavailable", lambda: self.projector.project_event(self.records[1]))
        health = self.projector.health()
        health["checkpoint"]["ordinal"] = 999
        health["last_error"]["code"] = "mutated"
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])
        self.assertEqual("fixture_unavailable", self.projector.health()["last_error"]["code"])
        self.assertEqual(3, self.client.count("discover"))

    def test_fractional_validity_ceil_succeeds_and_preserves_exact_source_at(self):
        record = self.create(at="2026-09-23T12:00:00.123456Z")
        original_event, original_state = deepcopy(record.event), state_to_dict(self.state)
        self.projector.run_batch(self.records)
        self.assertEqual(record.ordinal, self.store.read()["ordinal"])
        self.assertTrue(all(row["valid_from"] == "2026-09-23T12:00:01Z"
                            for row in self.client.triples))
        revision = f"mptask:{AUTHORITY}:{record.event['response']['task_id']}:v1"
        self.assertTrue(any(row["subject"] == revision and row["predicate"] == "source_at"
                            and row["object"] == "2026-09-23T12:00:00.123456Z"
                            for row in self.client.triples))
        self.assertIn("at: 2026-09-23T12:00:00.123456Z",
                      next(iter(self.client.drawers.values()))["content"])
        self.assertEqual(original_event, record.event)
        self.assertEqual(original_state, state_to_dict(self.state))

    def test_same_second_revisions_keep_distinct_exact_source_times(self):
        created = self.create(at="2026-09-23T12:00:00.100000Z")
        task_id = created.event["response"]["task_id"]
        noted = self.emit("note", at="2026-09-23T12:00:00.900000Z", tag="decision",
                          text="Distinct revision", **self.current(task_id))
        self.projector.run_batch(self.records)
        revisions = {
            f"mptask:{AUTHORITY}:{task_id}:v1": "2026-09-23T12:00:00.100000Z",
            f"mptask:{AUTHORITY}:{task_id}:v2": "2026-09-23T12:00:00.900000Z",
        }
        source_times = {row["subject"]: row["object"] for row in self.client.triples
                        if row["predicate"] == "source_at"}
        self.assertEqual(revisions, source_times)
        self.assertTrue(all(row["valid_from"] == "2026-09-23T12:00:01Z"
                            for row in self.client.triples))
        self.assertEqual(2, len(self.client.drawers))
        self.assertEqual(noted.ordinal, self.store.read()["ordinal"])

    def test_fractional_validity_ceiling_rolls_over_the_calendar_day(self):
        self.create(at="2026-09-23T23:59:59.999999Z")
        self.projector.run_batch(self.records)
        self.assertTrue(all(row["valid_from"] == "2026-09-24T00:00:00Z"
                            for row in self.client.triples))

    def test_large_accepted_event_splits_losslessly_into_bounded_linked_parts(self):
        record = self.large_expansion()
        event_before = deepcopy(record.event)
        self.assertGreater(len(json.dumps(record.event, ensure_ascii=False)), 100000)
        result = self.projector.run_batch(self.records)
        drawer_ids = result["results"][-1]["drawers"]
        self.assertGreater(len(drawer_ids), len(record.event["tasks"]))
        groups = {}
        for drawer_id in drawer_ids:
            row = self.client.drawers[drawer_id]
            self.assertLessEqual(len(row["content"]), 100000)
            source = row["metadata"]["source_file"]
            self.assertIn(f"source_file: {source}\n", row["content"])
            self.assertIn(WARNING, row["content"])
            match = re.fullmatch(r"(.+)/part-(\d{6})-of-(\d{6})", source)
            self.assertIsNotNone(match)
            group, index, count = match.groups()
            groups.setdefault(group, []).append((int(index), int(count), row))
        self.assertEqual(len(record.event["tasks"]), len(groups))
        for parts in groups.values():
            parts.sort()
            self.assertEqual(list(range(1, len(parts) + 1)), [index for index, _, _ in parts])
            self.assertTrue(all(count == len(parts) for _, count, _ in parts))
            payload = "".join(row["content"].split("\nprojection_payload:\n", 1)[1]
                              for _, _, row in parts)
            self.assertEqual(record.event, json.loads(payload.split("\naccepted_event:\n", 1)[1]))
            self.assertIn("at: 2026-09-23T12:00:00.234567Z", payload)
            payload_hash = hashlib.sha256(payload.encode()).hexdigest()
            self.assertTrue(all(f"payload_sha256: {payload_hash}\n" in row["content"]
                                for _, _, row in parts))
        links = [fact for fact in self.client.triples
                 if fact["predicate"] == "source_drawer"
                 and fact["source_drawer_id"] in drawer_ids]
        linked = set().union(*(self.literal_values(fact["object"]) for fact in links))
        self.assertEqual(set(drawer_ids), linked)
        for fact in links:
            self.assertEqual({fact["source_drawer_id"]}, self.literal_values(fact["object"]))
            self.assertTrue(any(link["predicate"] == "has_projection_part"
                                and link["object"] == fact["subject"]
                                for link in self.client.triples))
        self.assertEqual(event_before, record.event)
        self.assertEqual(record.ordinal, self.store.read()["ordinal"])

    def test_partial_part_failure_retries_same_identities_and_defers_checkpoint(self):
        record = self.large_expansion()
        self.projector.run_batch(self.records[:-1])
        old_cursor = self.store.read()
        previous_drawers = set(self.client.drawers)
        self.client.fail_part, self.client.part_failures = 2, 3
        self.assert_error("part_unavailable", lambda: self.projector.project_event(record))
        self.assertEqual(old_cursor, self.store.read())
        partial = set(self.client.drawers) - previous_drawers
        self.assertEqual(1, len(partial))
        partial_id = next(iter(partial))
        original = deepcopy(self.client.drawers[partial_id])
        result = self.projector.project_event(record)
        self.assertIn(partial_id, result["drawers"])
        self.assertEqual(original, self.client.drawers[partial_id])
        self.assertEqual(record.ordinal, self.store.read()["ordinal"])
        attempts = [args for name, args in self.client.calls if name == ADD
                    and f"/opaque%2Fevent%20{record.ordinal}/" in args["source_file"]]
        first_part_calls = [args for args in attempts
                            if args["source_file"] == original["metadata"]["source_file"]]
        self.assertEqual(2, len(first_part_calls))
        self.assertEqual(first_part_calls[0], first_part_calls[1])

    def test_large_rebuild_preserves_part_identities_and_complete_reconstruction(self):
        self.large_expansion()
        self.projector.run_batch(self.records)
        original = deepcopy(self.client.drawers)
        self.projector.reset()
        self.projector.run_batch(self.records)
        self.assertEqual(original, self.client.drawers)
        self.assertEqual(len(original), len({row["metadata"]["source_file"]
                                            for row in self.client.drawers.values()}))

    def test_corrupt_second_part_readback_cannot_complete_an_event(self):
        record = self.large_expansion()
        self.projector.run_batch(self.records[:-1])
        old_cursor = self.store.read()
        self.client.corrupt_part = 2
        self.assert_error("drawer_mismatch", lambda: self.projector.project_event(record))
        self.assertEqual(old_cursor, self.store.read())
        self.client.corrupt_part = None
        result = self.projector.project_event(record)
        self.assertGreater(len(result["drawers"]), len(record.event["tasks"]))
        self.assertEqual(record.ordinal, self.store.read()["ordinal"])

    def test_null_checkpoint_is_corruption_not_a_missing_file(self):
        self.store.write(None)
        with self.assertRaises(ProjectionError) as raised:
            TaskProjector(self.client, self.store, {"project": "test-project"})
        self.assertEqual("corrupt_checkpoint", raised.exception.code)
        self.assertTrue(self.store.path.exists())

    def test_changed_checkpoint_is_not_overwritten_by_running_projector(self):
        self.projector.project_event(self.records[0])
        self.create()
        foreign = {"owner": "another-service"}
        self.store.write(foreign)
        self.assert_error("corrupt_checkpoint",
                          lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(foreign, self.store.read())
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])

    def test_malformed_snapshot_types_fail_as_projection_errors(self):
        record = self.create()
        self.projector.project_event(self.records[0])
        for field, value in (("status", []), ("attempt", False), ("completion", True)):
            with self.subTest(field=field):
                event = deepcopy(record.event)
                event["tasks"][0][field] = value
                event["response"]["tasks"] = deepcopy(event["tasks"])
                self.assert_error("invalid_record", lambda: self.projector.project_event(
                    ProjectionRecord(record.event_id, record.ordinal, event)))
        self.assertEqual(0, self.client.count(ADD))

    def test_get_schema_restrictions_are_preflighted_before_writes(self):
        self.create()
        self.projector.project_event(self.records[0])
        self.client.tools[GET]["inputSchema"]["properties"]["drawer_id"]["enum"] = ["drawer"]
        self.assert_error("unsupported_capability",
                          lambda: self.projector.project_event(self.records[1]))
        self.assertEqual(0, self.client.count(ADD))

    def test_checkpoint_readback_failure_does_not_advance_or_succeed(self):
        self.projector.project_event(self.records[0])
        self.create()
        with patch.object(self.store, "write", return_value=None) as writes:
            self.assert_error("checkpoint_mismatch",
                              lambda: self.projector.project_event(self.records[1]))
            self.assertEqual(3, writes.call_count)
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])

    def test_explicit_reset_refuses_corruption_and_reports_marker_write_failure(self):
        self.projector.project_event(self.records[0])
        with patch.object(self.store, "write", side_effect=JournalError("io_error", "write failed")):
            self.assert_error("io_error", self.projector.reset)
        self.assertEqual(1, self.projector.health()["checkpoint"]["ordinal"])
        self.store.write({"not": "a projection checkpoint"})
        self.assert_error("corrupt_checkpoint", self.projector.reset)
        self.assertEqual({"not": "a projection checkpoint"}, self.store.read())


if __name__ == "__main__":
    unittest.main()
