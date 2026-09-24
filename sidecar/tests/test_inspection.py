"""Read-only human views against the authority's concrete query contract."""

from copy import deepcopy
import io
import json
import unittest

from mempalace_tasks.client import TaskClientError
from mempalace_tasks.inspection import build_frame, render_json, render_text, run_inspection


AS_OF = "2026-09-23T00:01:00Z"
TASK_ID = "task-first"


def metadata(**changes):
    return {
        "schema_version": 1,
        "authority_id": "11111111-1111-1111-1111-111111111111",
        "as_of": AS_OF,
        "last_verified_at": AS_OF,
        "raw_cursor": "evt-000004",
        "domain_head": "evt-000004",
        "domain_ordinal": 4,
        "fresh": True,
        "reason": None,
        **changes,
    }


def row(**changes):
    return {
        "id": TASK_ID, "project": "demo", "goal_id": None, "title": "Verify output",
        "kind": "task", "status": "in_progress", "priority": 1, "version": 3,
        "claim_generation": 2, "lease_revision": 3,
        "lease_expires_at": "2026-09-23T00:01:30Z",
        "resource_keys": [], "recovery": None, "assignee": "worker",
        "attempt_id": "att_11111111-1111-1111-1111-111111111112",
        "attempt_status": "running", "progress_deadline": "2026-09-23T00:02:00Z",
        "hard_deadline": "2026-09-23T00:10:00Z",
        "checkpoint": {"sequence": 2, "reference": "artifact:verified", "at": "2026-09-23T00:00:40Z"},
        "ready": False, "reasons": [{"code": "in_progress"}, {"code": "assigned", "assignee": "worker"}],
        "needs_attention": False, "blockers": [],
        **changes,
    }


def snapshot(**changes):
    return {
        **metadata(), "snapshot_id": "22222222-2222-2222-2222-222222222222",
        "summary": {"total": 1, "statuses": {"in_progress": 1}, "ready": 0, "needs_attention": 0},
        "rows": [row()], "next_cursor": None, **changes,
    }


def detail():
    task = {
        "id": TASK_ID, "project": "demo", "kind": "task", "title": "Verify output",
        "description": "A bounded task", "acceptance": "Verified output",
        "priority": 1, "hold_reason": None, "deferred_until": None,
        "execution_class": "isolated", "execution_profile": "local", "resource_keys": [],
        "policy": {"lease_ttl_seconds": 30, "renewal_seconds": 10, "sweep_seconds": 5,
                   "progress_timeout_seconds": 60, "hard_timeout_seconds": 600,
                   "automatic_retries": 2},
        "goal_id": None, "intent_key": None, "admitted": True,
        "status": "in_progress", "version": 3, "claim_generation": 2, "lease_revision": 3,
        "lease_expires_at": "2026-09-23T00:01:30Z", "assignee": "worker",
        "attempt": {
            "id": row()["attempt_id"], "number": 2, "owner": "worker",
            "supervisor_id": "supervisor", "started_at": "2026-09-23T00:00:00Z",
            "status": "running", "progress_deadline": "2026-09-23T00:02:00Z",
            "hard_deadline": "2026-09-23T00:10:00Z", "checkpoint": row()["checkpoint"],
            "failure_reason": None, "resource_fences": {}, "preparation": {"evidence": "prepared"},
            "settlement": None,
        },
        "automatic_retries_used": 1, "retry_budget_granted": 0, "retry_not_before": None,
        "recovery": None, "escalation": None, "created_at": "2026-09-23T00:00:00Z",
        "updated_at": "2026-09-23T00:00:40Z", "completion": None,
        "cancellation_reason": None, "sealed": False, "graph_revision": 0, "goal_policy": None,
    }
    return {**metadata(), "task": task,
            "eligibility": {"ready": False, "reasons": row()["reasons"], "as_of": AS_OF},
            "relations": [{"edge_type": "blocks", "source": "task-before", "target": TASK_ID}]}


def history(**changes):
    return {
        **metadata(), "snapshot_id": "33333333-3333-3333-3333-333333333333",
        "task_id": TASK_ID, "after_record_seq": 0, "upper_record_seq": 8,
        "rows": [{
            "record_seq": 4, "event_id": "evt-000004", "record_type": "proposal",
            "command_id": "44444444-4444-4444-4444-444444444444",
            "task_ids": [TASK_ID], "disposition": "accepted", "outcome": "committed",
            "payload": {"record_type": "proposal", "command": {"operation": "claim"}},
        }], "next_cursor": None, **changes,
    }


class Clock:
    def __init__(self):
        self.value = 5000.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Client:
    def __init__(self, *results, clock=None, delays=None, timeout=1.0):
        self.results = list(results)
        self.calls = []
        self.clock = clock
        self.delays = list(delays or [])
        self.timeout = timeout
        self.observed_timeouts = []

    def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments), self.clock.value if self.clock else None))
        self.observed_timeouts.append(self.timeout)
        if self.delays:
            self.clock.value += self.delays.pop(0)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class InspectionTests(unittest.TestCase):
    def run_view(self, client, command="status", options=None, clock=None):
        out, err = io.StringIO(), io.StringIO()
        clock = clock or Clock()
        code = run_inspection(client, command, options or {}, stdout=out, stderr=err,
                              monotonic=clock.monotonic, sleep=clock.sleep)
        return code, out.getvalue(), err.getvalue()

    def test_malformed_nested_attempt_is_reported_without_crashing(self):
        for attempt in (["invalid"], "invalid", 42):
            with self.subTest(attempt=attempt):
                data = detail()
                data["task"]["attempt"] = attempt
                code, output, errors = self.run_view(
                    Client(data), command="show", options={"task_id": TASK_ID}
                )
                self.assertNotEqual(0, code)
                self.assertIn("OUTCOME UNKNOWN", output)
                self.assertIn("Task detail or eligibility unknown", output)
                self.assertEqual("", errors)

    def test_credential_like_resource_names_preserve_numeric_fences(self):
        data = detail()
        counters = {"secrets/db": 7, "credential-store": 8, "api_key": 9}
        data["task"].update(
            execution_class="resource_fenced", execution_profile="fenced",
            resource_keys=list(counters),
        )
        data["task"]["attempt"]["resource_fences"] = counters
        data["task"]["attempt"]["preparation"] = {
            "prepared": True, "references": ["fixture:fences"],
            "resource_fences": counters,
        }
        data["task"]["description"] = "password=must-not-be-shown"
        code, output, _ = self.run_view(
            Client(data), command="show", options={"task_id": TASK_ID, "json": True}
        )
        self.assertEqual(0, code)
        shown = json.loads(output)["data"]["task"]
        self.assertEqual(counters, shown["attempt"]["resource_fences"])
        self.assertEqual(counters, shown["attempt"]["preparation"]["resource_fences"])
        self.assertNotIn("must-not-be-shown", output)

    def test_snapshot_queries_only_relevant_fields_and_uses_full_counts(self):
        data = snapshot(summary={"total": 19, "statuses": {"open": 18, "in_progress": 1},
                                 "ready": 15, "needs_attention": 3}, next_cursor="signed-next")
        options = {
            "project": "demo", "goal_id": "goal", "status": "in_progress",
            "assignee": "worker", "kind": "task", "needs_attention": False,
            "task_id": "irrelevant", "after_record_seq": 2, "limit": 1, "cursor": None,
            "json": False, "refresh_seconds": 5.0, "duration_seconds": 600.0,
        }
        for command in ("status", "list"):
            with self.subTest(command=command):
                client = Client(data)
                code, text, err = self.run_view(client, command, options)
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertEqual(client.calls[0][:2], ("mptask_snapshot", {
                    "filters": {key: options[key] for key in (
                        "project", "goal_id", "status", "assignee", "kind", "needs_attention")},
                    "limit": 1,
                }))
                self.assertIn("total=19", text)
                self.assertIn("ready=15", text)
                self.assertIn("needs_attention=3", text)
                self.assertIn("full filtered scope", text)
                self.assertIn("page rows=1", text)
                self.assertIn("signed-next", text)
                self.assertIn("CURRENT", text)

    def test_show_uses_get_and_retains_details_and_server_derived_ages(self):
        client = Client(detail())
        code, text, _ = self.run_view(client, "show", {"task_id": TASK_ID, "limit": 3})
        self.assertEqual(code, 0)
        self.assertEqual(client.calls[0][:2], ("mptask_get", {"task_id": TASK_ID}))
        for expected in ("acceptance", "Verified output", "automatic_retries_used",
                         "resource_fences", "recovery", "task-before", "prepared", "30s", "20s"):
            self.assertIn(expected, text)
        frame = build_frame("show", detail())
        self.assertEqual(frame["display"]["tasks"][0]["lease_remaining_seconds"], 30)
        self.assertEqual(frame["display"]["tasks"][0]["progress_age_seconds"], 20)

    def test_history_is_read_only_preserves_raw_records_and_bounds(self):
        client = Client(history())
        code, text, _ = self.run_view(client, "history", {
            "task_id": TASK_ID, "after_record_seq": 3, "limit": 4, "cursor": "opaque-page",
            "project": "irrelevant",
        })
        self.assertEqual(code, 0)
        self.assertEqual(client.calls[0][:2], ("mptask_history", {
            "task_id": TASK_ID, "after_record_seq": 3, "limit": 4, "cursor": "opaque-page",
        }))
        for expected in ("upper_record_seq=8", "record_seq=4", "evt-000004", "accepted", "committed"):
            self.assertIn(expected, text)
        for disposition in ("duplicate", "settled", "abandoned"):
            payload = history()
            payload["rows"][0]["disposition"] = disposition
            self.assertIn(disposition, render_text(build_frame("history", payload)))

    def test_expired_deadlines_do_not_rewrite_stored_status_or_compute_readiness(self):
        data = snapshot(rows=[row(lease_expires_at=AS_OF, needs_attention=True)],
                        summary={"total": 1, "statuses": {"in_progress": 1},
                                 "ready": 0, "needs_attention": 1})
        before = deepcopy(data)
        code, text, _ = self.run_view(Client(data))
        self.assertEqual(code, 0)
        self.assertIn("in_progress", text)
        self.assertIn("pending expiry", text)
        self.assertIn("EXPIRED", text)
        self.assertEqual(data, before)
        for field in ("progress_deadline", "hard_deadline"):
            with self.subTest(field=field):
                frame = build_frame("list", snapshot(rows=[row(**{field: AS_OF})]))
                self.assertIn("pending expiry", render_text(frame))

    def test_every_status_and_attention_reason_is_rendered_without_domain_recomputation(self):
        statuses = ("open", "in_progress", "recovering", "quarantined", "closed", "cancelled")
        reasons = [
            {"code": "held", "reason": "human review"},
            {"code": "dependency", "task_id": "blocker-id"},
            {"code": "resource_busy", "resource_key": "shared-db", "task_id": "resource-owner"},
            {"code": "retry_backoff", "until": "2026-09-24T00:00:00Z"},
            {"code": "unsupported_execution_profile"}, {"code": "awaiting_admission"},
            {"code": "goal_sealed"}, {"code": "deferred"},
        ]
        data = snapshot(rows=[row(id=f"id-{status}", status=status, needs_attention=True,
                                  reasons=reasons, blockers=["blocker-id"]) for status in statuses],
                        summary={"total": 6, "statuses": dict.fromkeys(statuses, 1),
                                 "ready": 0, "needs_attention": 6})
        code, text, _ = self.run_view(Client(data))
        self.assertEqual(code, 0)
        for value in (*statuses, "shared-db", "resource-owner", "blocker-id", "human review",
                      "awaiting_admission", "retry_backoff", "goal_sealed", "needs_attention=6"):
            self.assertIn(value, text)

    def test_stale_and_unknown_payloads_render_with_nonzero_exit_and_ages(self):
        for reason, label in (
            ("upstream_unavailable", "STALE"), ("reboot_pending", "STALE"),
            ("persistence_unconfirmed", "STALE"), ("pending_command", "OUTCOME UNKNOWN"),
            ("uninitialized", "OUTCOME UNKNOWN"), ("unverified", "OUTCOME UNKNOWN"),
        ):
            with self.subTest(reason=reason):
                data = snapshot(fresh=False, reason=reason,
                                last_verified_at="2026-09-23T00:00:00Z")
                code, text, _ = self.run_view(Client(data))
                self.assertNotEqual(code, 0)
                self.assertIn(label, text)
                self.assertIn(reason, text)
                self.assertIn("60s", text)
                self.assertIn("not live", text)
                self.assertEqual(build_frame("list", data)["display"]["verification_age_seconds"], 60)

    def test_pinned_pages_are_historical_not_live_and_counts_remain_full_scope(self):
        data = snapshot(fresh=False, reason="pinned_snapshot", next_cursor="next",
                        summary={"total": 8, "statuses": {"in_progress": 8},
                                 "ready": 0, "needs_attention": 0})
        code, text, _ = self.run_view(Client(data), "list", {"cursor": "page-2"})
        self.assertNotEqual(code, 0)
        self.assertIn("HISTORICAL SNAPSHOT", text)
        self.assertIn("captured cursor scope", text)
        self.assertIn("total=8", text)
        self.assertIn("not live", text)

    def test_unknown_counts_and_missing_metadata_never_manufacture_current_empty_scope(self):
        for changes in (
            {"summary": None}, {"fresh": None}, {"as_of": None},
            {"summary": {"total": 1, "statuses": {}, "ready": 0, "needs_attention": 0}},
        ):
            with self.subTest(changes=changes):
                code, text, _ = self.run_view(Client(snapshot(**changes)))
                self.assertNotEqual(code, 0)
                self.assertIn("UNKNOWN", text)
                self.assertNotIn("No tasks in verified scope", text)
        self.assertIn("counts=unknown", self.run_view(Client(snapshot(summary=None)))[1])

    def test_empty_verified_scope_is_distinct_from_service_unavailable(self):
        code, text, _ = self.run_view(Client(snapshot(rows=[], summary={
            "total": 0, "statuses": {}, "ready": 0, "needs_attention": 0,
        })))
        self.assertEqual(code, 0)
        self.assertIn("No tasks in verified scope", text)
        code, text, _ = self.run_view(Client(TaskClientError("transport_error", "Unavailable")))
        self.assertNotEqual(code, 0)
        self.assertIn("No verified task state", text)
        self.assertNotIn("No tasks in verified scope", text)
        self.assertIn("OUTCOME UNKNOWN", text)

    def test_json_canonical_preserves_payload_content_and_text_escapes_terminal_controls(self):
        title = "danger\x1b[2J\r\n\t\b\u009b31m\u202ereversed\u2066hidden\u2028line"
        data = snapshot(rows=[row(title=title)])
        before = deepcopy(data)
        code, text, _ = self.run_view(Client(data))
        self.assertEqual(code, 0)
        for raw in ("\x1b", "\r", "\t", "\b", "\u009b", "\u202e", "\u2066", "\u2028"):
            self.assertNotIn(raw, text)
        self.assertIn("\\x1b", text)
        self.assertIn("\\u202e", text)
        code, output, err = self.run_view(Client(data), options={"json": True})
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(output.splitlines()), 1)
        frame = json.loads(output)
        self.assertEqual(frame["data"], before)
        self.assertEqual(data, before)
        self.assertEqual(output, render_json(frame) + "\n")
        self.assertEqual(output.strip(), json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True))

    def test_secrets_redacted_recursively_in_payload_errors_and_text(self):
        data = detail()
        data["task"]["attempt"]["preparation"] = {
            "access_token": "secret-a", "nested": {"password": "secret-b"},
            "note": "Authorization: Bearer secret-c",
        }
        for json_mode in (False, True):
            code, output, err = self.run_view(Client(data), "show", {"task_id": TASK_ID, "json": json_mode})
            self.assertEqual(code, 0)
            for secret in ("secret-a", "secret-b", "secret-c"):
                self.assertNotIn(secret, output + err)
            self.assertIn("[redacted]", output)
        error = TaskClientError("outcome_unknown", "Bearer secret-d", ambiguous=True,
                                details={"api_key": "secret-e"})
        code, output, err = self.run_view(Client(error), options={"json": True})
        self.assertNotEqual(code, 0)
        self.assertIn("OUTCOME UNKNOWN", output)
        self.assertNotIn("secret-d", output + err)
        self.assertNotIn("secret-e", output + err)

    def test_invalid_options_are_local_errors_before_any_tool_call(self):
        invalid = [
            {"extra": True}, {"limit": True}, {"limit": 0}, {"limit": 501},
            {"limit": "10"}, {"after_record_seq": -1}, {"after_record_seq": True},
            {"project": ""}, {"status": "queued"}, {"kind": "feature"},
            {"needs_attention": "false"}, {"json": "true"}, {"cursor": 3},
            {"refresh_seconds": 0}, {"duration_seconds": -1},
            {"refresh_seconds": float("nan")}, {"duration_seconds": float("inf")},
        ]
        for options in invalid:
            with self.subTest(options=options):
                client = Client()
                code, output, err = self.run_view(client, options=options)
                self.assertEqual(code, 2)
                self.assertEqual(client.calls, [])
                self.assertIn("invalid_options", output + err)
        for command in ("show", "history", "sweep", "ready"):
            with self.subTest(command=command):
                client = Client()
                self.assertEqual(self.run_view(client, command)[0], 2)
                self.assertEqual(client.calls, [])
        code, output, _ = self.run_view(Client(), options={"json": True, "token": "do-not-echo"})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_options")
        self.assertNotIn("do-not-echo", output)

    def test_watch_emits_finite_json_frames_and_only_refreshes_first_page(self):
        clock = Clock()
        data = snapshot(next_cursor="never-follow", summary={
            "total": 20, "statuses": {"in_progress": 20}, "ready": 0, "needs_attention": 0,
        })
        client = Client(data, data, data, clock=clock)
        code, output, err = self.run_view(client, "watch", {
            "json": True, "duration_seconds": 11, "refresh_seconds": 5, "limit": 1,
        }, clock)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(frames), 3)
        self.assertEqual([call[2] for call in client.calls], [5000, 5005, 5010])
        self.assertEqual(clock.value, 5011)
        self.assertTrue(all(call[:2] == ("mptask_snapshot", {"limit": 1}) for call in client.calls))
        self.assertTrue(all(frame["data"]["summary"]["total"] == 20 for frame in frames))
        self.assertTrue(all(frame["display"]["tasks"][0]["lease_remaining_seconds"] == 30 for frame in frames))

    def test_watch_defaults_are_finite_and_do_not_fetch_graph_pages(self):
        clock = Clock()
        client = Client(*[snapshot() for _ in range(120)], clock=clock)
        code, output, _ = self.run_view(client, "watch", {"json": True}, clock)
        self.assertEqual(code, 0)
        self.assertEqual(len(output.splitlines()), 120)
        self.assertEqual(len(client.calls), 120)
        self.assertEqual(clock.value, 5600)
        self.assertEqual(client.calls[-1][2], 5595)

    def test_watch_disconnect_renders_last_success_stale_then_reconnects(self):
        clock = Clock()
        client = Client(snapshot(), TaskClientError("transport_error", "Disconnected"),
                        snapshot(as_of="2026-09-23T00:01:10Z",
                                 last_verified_at="2026-09-23T00:01:10Z"), clock=clock)
        code, output, _ = self.run_view(client, "watch", {
            "duration_seconds": 11, "json": True,
        }, clock)
        self.assertEqual(code, 0)
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual([f["display"]["state"] for f in frames], ["CURRENT", "STALE", "CURRENT"])
        self.assertEqual(frames[1]["display"]["observation_age_seconds"], 5)
        self.assertEqual(frames[1]["display"]["as_of"], AS_OF)
        self.assertEqual(frames[1]["display"]["tasks"][0]["lease_remaining_seconds"], 30)
        self.assertEqual(frames[2]["display"]["tasks"][0]["lease_remaining_seconds"], 20)
        self.assertTrue(frames[1]["data"]["fresh"])
        self.assertFalse(frames[1]["display"]["current"])

    def test_watch_final_disconnect_or_stale_payload_exits_nonzero(self):
        for failure in (
            TaskClientError("transport_error", "Disconnected"),
            snapshot(fresh=False, reason="pending_command"),
        ):
            with self.subTest(failure=failure):
                clock = Clock()
                client = Client(snapshot(), failure, clock=clock)
                code, output, _ = self.run_view(client, "watch", {"duration_seconds": 6}, clock)
                self.assertEqual(code, 1)
                self.assertIn("not live", output)
        clock = Clock()
        client = Client(TaskClientError("http_error", "Unavailable"),
                        TaskClientError("timeout", "Unavailable"), clock=clock)
        code, output, _ = self.run_view(client, "watch", {"duration_seconds": 6}, clock)
        self.assertEqual(code, 1)
        self.assertEqual(output.count("No verified task state"), 2)

    def test_watch_slow_calls_skip_ticks_and_never_change_shared_timeout(self):
        clock = Clock()
        client = Client(snapshot(), snapshot(), clock=clock, delays=[7, 7], timeout=8)
        code, output, err = self.run_view(client, "watch", {
            "duration_seconds": 22, "refresh_seconds": 5, "json": True,
        }, clock)
        self.assertEqual(code, 0)
        self.assertEqual([call[2] for call in client.calls], [5000, 5010])
        self.assertEqual(client.timeout, 8)
        self.assertEqual(client.observed_timeouts, [8, 8])
        self.assertLessEqual(clock.value, 5022)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("timeout", err)

    def test_watch_does_not_start_calls_that_cannot_fit_deadline(self):
        clock = Clock()
        client = Client(clock=clock, timeout=10)
        code, output, _ = self.run_view(client, "watch", {
            "duration_seconds": 9, "json": True,
        }, clock)
        self.assertEqual(code, 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(json.loads(output)["error"]["code"], "watch_budget_exhausted")
        for timeout in (None, 0, float("inf"), True):
            with self.subTest(timeout=timeout):
                client = Client(timeout=timeout)
                self.assertEqual(self.run_view(client, "watch")[0], 2)
                self.assertEqual(client.calls, [])

    def test_watch_cursor_expiry_discards_pin_without_merging_cached_rows(self):
        clock = Clock()
        client = Client(TaskClientError("snapshot_expired", "Expired"),
                        snapshot(rows=[row(id="new-capture")]), clock=clock)
        code, output, _ = self.run_view(client, "watch", {
            "cursor": "old-page", "duration_seconds": 6, "json": True,
        }, clock)
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(client.calls[0][1], {"limit": 100, "cursor": "old-page"})
        self.assertEqual(client.calls[1][1], {"limit": 100})
        self.assertEqual(frames[1]["data"]["rows"][0]["id"], "new-capture")
        self.assertEqual(frames[1]["display"]["scope"], {})
        self.assertTrue(any("new capture" in warning for warning in frames[0]["display"]["warnings"]))

    def test_watch_accepted_pinned_page_is_not_reused_on_next_refresh(self):
        clock = Clock()
        client = Client(snapshot(fresh=False, reason="pinned_snapshot"), snapshot(), clock=clock)
        code, output, _ = self.run_view(client, "watch", {
            "cursor": "old-page", "duration_seconds": 6, "json": True,
        }, clock)
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(frames[0]["display"]["state"], "HISTORICAL SNAPSHOT")
        self.assertEqual(client.calls[1][1], {"limit": 100})

    def test_ctrl_c_exits_cleanly_during_read_or_sleep(self):
        for during in ("read", "sleep"):
            with self.subTest(during=during):
                clock = Clock()
                client = Client(KeyboardInterrupt() if during == "read" else snapshot(), clock=clock)
                if during == "sleep":
                    def interrupt(_):
                        raise KeyboardInterrupt()
                    clock.sleep = interrupt
                code, output, err = self.run_view(client, "watch", {"json": True}, clock)
                self.assertEqual(code, 130)
                frames = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(frames[-1]["error"]["code"], "interrupted")
                self.assertNotIn("Traceback", output + err)

    def test_unexpected_client_errors_never_echo_untrusted_exception_text(self):
        client = Client(RuntimeError("Bearer untrusted-secret \x1b[2J"))
        code, output, err = self.run_view(client, options={"json": True})
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"]["code"], "inspection_error")
        self.assertNotIn("untrusted-secret", output + err)

    def test_partial_or_contradictory_pages_and_task_shapes_are_explicitly_unknown(self):
        invalid = []
        for key in ("snapshot_id", "next_cursor"):
            data = snapshot()
            del data[key]
            invalid.append(data)
        invalid.extend([
            snapshot(next_cursor=[]), snapshot(rows=[{}]),
            snapshot(rows=[row(ready="false")]), snapshot(rows=[row(status="imaginary")]),
            snapshot(summary={"total": 0, "statuses": {}, "ready": 0, "needs_attention": 0}),
            snapshot(rows=[]),
        ])
        for data in invalid:
            with self.subTest(data=data):
                code, output, _ = self.run_view(Client(data), options={"json": True})
                self.assertNotEqual(code, 0)
                frame = json.loads(output)
                self.assertEqual(frame["display"]["state"], "OUTCOME UNKNOWN")
                self.assertTrue(frame["display"]["warnings"])
        for command, data, options in (
            ("show", {**metadata(), "task": {}, "eligibility": {}}, {"task_id": TASK_ID}),
            ("history", history(rows=[{}]), {"task_id": TASK_ID}),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_view(Client(data), command, options)[0], 1)

    def test_unknown_checkpoint_age_never_uses_wallclock_or_invented_started_time(self):
        for checkpoint in (None, {"at": "2026-09-23T00:01:10Z"}, {"at": "invalid"}):
            with self.subTest(checkpoint=checkpoint):
                frame = build_frame("list", snapshot(rows=[row(checkpoint=checkpoint)]))
                self.assertIsNone(frame["display"]["tasks"][0]["progress_age_seconds"])
                self.assertIn("progress_age=unknown", render_text(frame))
        data = snapshot(last_verified_at="2026-09-23T00:01:01Z")
        self.assertEqual(self.run_view(Client(data))[0], 1)

    def test_camel_case_secret_fields_and_invalid_command_values_are_not_echoed(self):
        data = detail()
        data["task"]["attempt"]["preparation"] = {
            "apiKey": "secret-camel-api", "accessToken": "secret-camel-token",
            "clientSecret": "secret-camel-client", "privateKey": "secret-camel-private",
        }
        code, output, err = self.run_view(Client(data), "show", {"task_id": TASK_ID, "json": True})
        self.assertEqual(code, 0)
        self.assertNotIn("secret-camel", output + err)
        code, output, err = self.run_view(Client(), "Bearer secret-command", {"json": True})
        self.assertEqual(code, 2)
        self.assertNotIn("secret-command", output + err)

    def test_watch_cached_cursor_page_keeps_original_scope_on_next_read_failure(self):
        clock = Clock()
        client = Client(snapshot(fresh=False, reason="pinned_snapshot"),
                        TaskClientError("transport_error", "Lost"), clock=clock)
        code, output, _ = self.run_view(client, "watch", {
            "json": True, "cursor": "old-page", "duration_seconds": 6,
        }, clock)
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(frames[1]["display"]["scope"], {"captured_cursor": True})
        self.assertEqual(frames[1]["display"]["state"], "STALE")

    def test_one_shot_ctrl_c_and_overlong_read_are_noncurrent(self):
        code, output, _ = self.run_view(Client(KeyboardInterrupt()), options={"json": True})
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(output)["error"]["code"], "interrupted")
        clock = Clock()
        client = Client(snapshot(), clock=clock, timeout=1, delays=[3])
        code, output, _ = self.run_view(client, "watch", {
            "json": True, "duration_seconds": 2,
        }, clock)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"]["code"], "watch_deadline_exceeded")
        self.assertEqual(len(client.calls), 1)

    def test_watch_does_not_treat_invalid_cursor_or_query_as_expired_capture(self):
        for error_code in ("invalid_cursor", "unknown_tool", "invalid_configuration",
                           "invalid_arguments", "validation_error", "not_found"):
            with self.subTest(error_code=error_code):
                clock = Clock()
                client = Client(TaskClientError(error_code, "Rejected"), snapshot(), clock=clock)
                code, output, _ = self.run_view(client, "watch", {
                    "cursor": "invalid", "duration_seconds": 6, "json": True,
                }, clock)
                self.assertEqual(code, 1)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(json.loads(output)["error"]["code"], error_code)

    def test_pure_render_helpers_do_not_alias_or_modify_caller_data(self):
        data = detail()
        scope = {"task_id": TASK_ID}
        original = deepcopy(data)
        frame = build_frame("show", data, scope=scope)
        before = deepcopy(frame)
        render_text(frame)
        render_json(frame)
        self.assertEqual(frame, before)
        frame["data"]["task"]["title"] = "changed"
        frame["display"]["scope"]["task_id"] = "changed"
        self.assertEqual(data, original)
        self.assertEqual(scope, {"task_id": TASK_ID})
        clock = Clock()
        options = {"cursor": "page", "duration_seconds": 6}
        original_options = deepcopy(options)
        client = Client(snapshot(fresh=False, reason="pinned_snapshot"), snapshot(), clock=clock)
        self.run_view(client, "watch", options, clock)
        self.assertEqual(options, original_options)

    def test_watch_extreme_unrepresentable_time_options_fail_locally(self):
        for options in (
            {"refresh_seconds": 1e-300}, {"duration_seconds": 10**500},
            {"refresh_seconds": 10**500},
        ):
            with self.subTest(options=options):
                client = Client()
                self.assertEqual(self.run_view(client, "watch", options)[0], 2)
                self.assertEqual(client.calls, [])

    def test_watch_malformed_response_does_not_replace_last_coherent_observation(self):
        clock = Clock()
        client = Client(snapshot(), {}, TaskClientError("transport_error", "Lost"), clock=clock)
        code, output, _ = self.run_view(client, "watch", {
            "json": True, "duration_seconds": 11,
        }, clock)
        frames = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(frames[1]["display"]["state"], "OUTCOME UNKNOWN")
        self.assertEqual(frames[2]["data"]["rows"][0]["id"], TASK_ID)
        self.assertEqual(frames[2]["display"]["observation_age_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
