"""Real CLI boundary trace, not an agent, authority or usefulness simulation.

The fresh-agent pressure scenarios exercise the edited guidance separately.
This test records actual A/B responses on disposable installed storage, catching
stale-context acceptance, candidate/consent bypass and receipt write inflation.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import dream_palace
from delivery_fixtures import applicability, case
from dream_metadata import canonical_json
from dream_procedure import main
from dream_procedural_palace import append_event, read_events
from dream_procedural_receipts import read_receipts
from receipt_fixtures import receipts, resign
from test_dream_procedural import NOW
from test_dream_procedural_palace import installed_palace
from test_dream_procedural_validate import GroundedFixture


def test_installed_manual_delivery_context_trial_and_receipt_boundaries(tmp_path):
    fixture = GroundedFixture()
    fixture.setUp()
    fixture.storage_patch.stop()
    trace = {
        "kind": "declared_fixture_cli_trace",
        "limits": (
            "Actual CLI handlers, SQLite-exact palace and offline MiniLM. Clock "
            "and original observations/reviews/outcomes are declared fixtures. "
            "No model chooses actions here; no claim of ordinary-context delivery, "
            "agent obedience, native host certification or causal usefulness."
        ),
        "events": [],
    }
    current_file = tmp_path / "current.json"
    permission_file = tmp_path / "permissions.json"
    packet_file = tmp_path / "packet.json"
    fit_file = tmp_path / "fit.json"
    receipt_file = tmp_path / "receipt.json"
    trace_file = tmp_path / "cli-transcript.json"

    def write(path, value):
        path.write_text(canonical_json(value), encoding="utf-8")

    def invoke(command, *arguments):
        out, err = StringIO(), StringIO()
        argv = [command, "--palace", fixture.path, "--wing", "w", *arguments]
        inputs = {}
        for flag in ("--current", "--permissions", "--packet", "--applicability", "--input"):
            if flag in arguments:
                path = Path(arguments[arguments.index(flag) + 1])
                inputs[flag] = json.loads(path.read_text(encoding="utf-8"))
        with redirect_stdout(out), redirect_stderr(err), patch("dream_procedure.now_utc", return_value=NOW):
            code = main(argv)
        response = json.loads(out.getvalue())
        trace["events"].append({
            "sequence": len(trace["events"]) + 1, "argv": argv, "inputs": inputs,
            "exit_code": code, "response": response, "stderr": err.getvalue(),
        })
        trace_file.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return code, response

    try:
        with installed_palace(fixture.path):
            writer = dream_palace.MempalaceWriter()
            for ref in fixture.refs[:3]:
                ref["source_id"] = writer.add_drawer(
                    "w", "diary", ref["quote"], added_by="original-agent")["drawer_id"]
            seeded = [fixture.proposal(), fixture.review()]
            for event in seeded:
                with writer.mutation():
                    append_event(fixture.path, "w", event, writer=writer,
                                 session_store=fixture.store, clock=lambda: NOW)
            trace["original_support"] = deepcopy(fixture.refs[:3])
            trace["seed_event_ids"] = [event.event_id for event in seeded]
            _, current, permission, _ = case(
                "Fix a reproducible defect using a focused regression test.")
            write(current_file, current)
            write(permission_file, permission)
            scope = ["--current", str(current_file), "--permissions", str(permission_file)]
            def offer(destination=packet_file):
                request = str(UUID(int=904 + len(trace["events"])))
                return invoke("task-guidance", *scope, "--request-id", request,
                              "--out", str(destination))

            code, empty = offer(tmp_path / "established-empty.json")
            assert (code, empty["status"], empty["guidance"]["item_count"]) == (0, "bound_guidance", 0)
            current["context"]["mode"] = "approved_candidate_trial"
            write(current_file, current)
            code, denied = offer()
            assert code == 2 and "trial" in denied["error"]
            assert not packet_file.exists()
            permission["trials"] = "allow"
            write(permission_file, permission)
            code, packet = offer()
            assert code == 0 and len(packet["guidance"]["trials"]) == 1
            items = packet["guidance"]["trials"]
            fit = applicability(current["context"], items)
            write(fit_file, fit)
            use_args = ["--packet", str(packet_file), "--rule-id", items[0]["rule_id"],
                        "--applicability", str(fit_file)]
            code, usable = invoke("use-check", *scope, *use_args)
            assert (code, usable["status"]) == (0, "usable")
            assert usable["items"][0]["applies_when"] == items[0]["applies_when"]
            assert usable["items"][0]["exceptions"] == items[0]["exceptions"]

            before = read_events(fixture.path, "w")
            current["context"]["revision"] = 1
            current["context"]["constraints"] = ["Preserve spans", "No implementation changes"]
            write(current_file, current)
            code, changed = invoke("use-check", *scope, *use_args)
            assert code == 2 and "context changed" in changed["error"]
            code, revised = offer(tmp_path / "revised-packet.json")
            assert code == 0 and revised["context"]["revision"] == 1
            assert revised["context_digest"] != packet["context_digest"]

            # A new observation with the same repository is not sufficient fit.
            fit = applicability(current["context"], revised["guidance"]["trials"])
            fit["assessments"][0]["constraints"][1].update(
                verdict="incompatible", reason="This activity forbids the proposed implementation change.")
            write(fit_file, fit)
            use_args[1] = str(tmp_path / "revised-packet.json")
            code, withheld = invoke("use-check", *scope, *use_args)
            assert (code, withheld["status"], withheld["items"]) == (0, "withheld", [])
            assert read_events(fixture.path, "w") == before
            assert read_receipts(fixture.path, "w", now=NOW) == {}

            # History can truthfully report reading even though action is withheld.
            record, _, _ = receipts()
            record["receipt_id"] = "delivery:" + revised["request_id"]
            record["payload"]["packet"] = revised
            record["context_digest"] = revised["context_digest"]
            record = resign(record)
            write(receipt_file, record)
            receipt_args = ["--input", str(receipt_file), "--permissions", str(permission_file)]
            code, denied = invoke("receipt", *receipt_args)
            assert code == 2 and "permission" in denied["error"]
            assert read_receipts(fixture.path, "w", now=NOW) == {}
            permission.update(receipts="allow", receipts_ref="user:3")
            write(permission_file, permission)
            code, appended = invoke("receipt", *receipt_args)
            assert (code, appended["status"]) == (0, "appended")
            permission.update(advice="deny", receipts="deny")
            write(permission_file, permission)
            code, replayed = invoke("receipt", "--input", str(receipt_file))
            assert (code, replayed["status"]) == (0, "already_exists")
            assert list(read_receipts(fixture.path, "w", now=NOW)) == [record["receipt_id"]]
            assert read_events(fixture.path, "w") == before
            code, denied = invoke("use-check", *scope, *use_args)
            assert code == 2 and "permission" in denied["error"]

            # Validated originals becoming unavailable is not healthy empty.
            permission["advice"] = "allow"
            write(permission_file, permission)
            from mempalace import mcp_server
            from test_dream_procedural_palace import installed_mutation
            with installed_mutation(mcp_server) as collection:
                collection.update(
                    ids=[fixture.refs[0]["source_id"]], documents=["changed original"])
            code, error = invoke("task-guidance", *scope, "--request-id", str(UUID(int=999)),
                                 "--out", str(tmp_path / "unavailable.json"))
            assert code == 1 and error["kind"] == "evidence_unavailable"
            assert not (tmp_path / "unavailable.json").exists()
            assert read_events(fixture.path, "w") == before
    finally:
        fixture.doCleanups()
