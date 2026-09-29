# Cooperative native MCP reference

This reference matches `sidecar/src/mempalace_tasks/native.py` (`schema` and
`execute`) and the native read/receipt handling in `authority.py`/`server.py`.
Discover the deployed `mptask_native` descriptor before use: checkout changes do
not update an installed service. Missing native action support is a version
blocker, not a request to provision an actor or supervisor.

## Envelope and observations

Call **`mempalace-tasks/mptask_native`** with exactly:

```json
{
  "action": "bootstrap",
  "session_id": "11111111-1111-4111-8111-111111111111",
  "command_id": "22222222-2222-4222-8222-222222222222",
  "expected_epoch": "33333333-3333-4333-8333-333333333333",
  "payload": {
    "project": "demo",
    "title": "Deliver the approved plan",
    "description": "Implement only the approved scope; exact plan is referenced below.",
    "acceptance": "All planned deliverables verified and aggregate evidence reviewed.",
    "planning_task": {
      "title": "Import approved plan",
      "description": "Read the exact artifact and publish tasks with their initial blockers.",
      "acceptance": "Every actionable plan unit represented or explicitly proposed."
    },
    "goal_policy": {
      "scope": "Approved plan only",
      "max_tasks": 20,
      "max_batch": 10
    },
    "plan_references": ["artifact:exact-approved-plan-fixture"]
  }
}
```

All IDs/references in examples are **synthetic fixtures**. Before an actual call,
replace them with the retained issuing-session UUID, a new command UUID, current
health/read `epoch_id` and actual artifact/task/attempt references. Freeze that
envelope for retries of the same logical request; never update its epoch or
payload while retaining its command ID. Do not send `actor`, `operation`,
supervisor fields or credentials.

UUIDs are canonical lowercase strings. Use the harness session UUID if available;
otherwise issue one UUID for this parent and retain it in the handoff. Same-session compaction
does not change it. A fork/new parent uses a different UUID and explicit transfer.
Session identity is not a secret or transport authentication.

Bootstrap `description`, `acceptance` and the planning task's corresponding
strings are required. `goal_policy.scope` is required; current defaults are
`max_tasks=100`, `max_batch=25` (limits 10,000 and 50). `plan_references`, when
supplied, is a nonempty distinct list of strings, not artifact descriptor objects.
Use actual artifact IDs/references; retain returned SHA-256/size in the plan index.
Each reference is at most 2,048 UTF-8 bytes; reference/evidence/observations lists
are bounded to 20 entries. Actual approved-plan preservation remains required by
the workflow even though the API permits bootstrap without plan references.

Existing reads remain:

- `mptask_health({})`: current authority/epoch and freshness.
- `mptask_get({"task_id":"tsk_fixture"})`: complete `task` and current
  `authorization`. Native tasks have `coordination_mode="cooperative_native"`,
  `native.session_id`, `native.issuing_session_id` (original), and
  `native.plan_references`. **The root goal's `native.session_id` is the current
  parent.** On a member, that field is only its last recorded session;
  it need not match the current parent after transfer. Fresh
  `authorization.session_id` resolves the current parent from the root.
  The root also carries monotonic internal `native.session_generation`;
  `task.attempt.session_generation` records the generation at claim. Member
  `native.session_generation` is historical recorded metadata, not the current fence.
  Tasks with neither `coordination_mode` nor native metadata are legacy managed
  and retain their registered actor/profile contract. Unknown explicit modes
  fail closed; never infer cooperative enrollment or convert a named managed goal.
- `mptask_ready({"filters":{"project":"demo","goal_id":"tsk_goal_fixture"}})`:
  candidates, not claims. No managed execution-profile filter for native work.
- `mptask_snapshot`/`mptask_history`: scoped inspection and provenance, not mutation.
- `mptask_outcome({"epoch_id":"<original UUID>","command_id":"<original UUID>"})`:
  historical resolution only, never fresh execution authorization.

Receipts retain existing `outcome`, replay, task snapshots and authorization
shapes. Native response fields include `coordination_mode`, `session_id`,
`goal_id`; bootstrap adds `planning_task_id`, individual-task actions add
`task_id`, and expand adds `admitted_task_ids`/`proposed_task_ids`.
Copy current `task.version`, `task.graph_revision` for the goal,
`task.attempt.id`, `task.claim_generation` and
`task.attempt.native_agent_id` from fresh reads, not guessed values.

**Executing or publishing work from a running source** requires fresh native
authorization, `matches_current=true` and `authorized=true` for the matching
task/attempt/generation/session/agent. **`lease_live=false`,
`lease_expires_at=null` and `physical_supervision=false` are intentional.**
Do not apply the managed `lease_live=true` gate. This is **not a prerequisite for
every native action**: bootstrap, update, claim, start, release, reconcile, resume,
cancel, source-free expansion and non-running epic/goal closure use their own session,
version, state and acceptance checks below. In particular, reserved/preparing and
recovering or inherited attempts intentionally return `authorized=false`; demanding true would
prevent the start/reconciliation needed to progress. A receipt's recorded snapshots
remain historical; revalidate after compaction, conflict, transfer or epoch change.
`native_agent_id` is an observed opaque nonempty string. The service checks its
binding on later commands, not whether that ID physically belongs to a runtime
worker. The parent must supply the actual dispatch result (its session UUID for
parent work); this provenance is cooperative, not runtime identity attestation.
The service checks both parent UUID and internal session generation. Transfer
A→B→A cannot revive the original A attempt. **Do not send `session_generation` in
the envelope or payload**; the service derives it. Public CAS/version and
attempt/`claim_generation` fields remain unchanged.

## Exact action payloads

The following shorthand names are documentation only; send their actual fields,
not nested objects named `Scope`, `Task` or `Bound`.

| Shorthand | Actual fields |
|---|---|
| Scope | `project`, `goal_id` |
| Task | Scope plus `task_id`, `expected_version` |
| Bound | Task plus `attempt_id`, `claim_generation`, `native_agent_id` |
| Accepted | `summary`, nonempty `evidence` string list, nonempty `parent_acceptance` text |
| Observed | nonempty `reason`, nonempty `observations` string list |

`parent_acceptance` is required for every completion form and is bounded to 8,192
UTF-8 bytes. A supplied evidence/reference string is exact, not a fuzzy plan name.

| Action | Payload / constraints |
|---|---|
| `bootstrap` | Full example above; creates root and import task atomically. |
| `update` | Task plus nonempty `patch` of content fields only. Open non-root task/epic, no assignee/recovery. See held/deferred example below; no execution/admission/mode edits. |
| `claim` | Task. Reserves an attempt; status is `in_progress`, attempt status `preparing`, agent ID null. No execution yet. |
| `start` | Bound, with actual returned native agent ID (or parent session UUID). Checks the current reserved/preparing attempt, not running authorization. Binds once and makes attempt `running`. |
| `checkpoint` | Bound plus positive `checkpoint_sequence` and `reference`; sequence increases and reference changes. |
| `complete` task | Bound plus Accepted; requires a current running attempt. |
| `complete` non-root epic | Task plus Accepted, **without** attempt/generation/agent fields; children/gates must be resolved. A root uses `goal_close`. |
| `release` | Bound plus Observed; `native_agent_id` may be null only for an unbound reservation. Current parent may release its own or an inherited active attempt. Moves that task to `recovering`; no adoption or automatic retry. |
| `reconcile` | Bound plus Observed plus `decision` = `hold`, `retry` or `cancel`; requires recovering work, whose execution authorization is false. `hold` remains recovering; `retry` explicitly opens for a new claim; `cancel` records cancellation. Never accepts completion. |
| `resume` | Scope plus current goal `expected_version`, `expected_session_id`, Observed. Envelope carries the **new** parent UUID. Bounded root-only CAS changes owner/version, leaving all member snapshots unchanged but fencing old-parent publication. Inherited active work needs per-task release/reconcile, never adoption. |
| `cancel` | Task plus Observed, **no attempt/generation/agent tokens**. Only open non-root work/proposals without assignee/recovery. Nested epics require terminal, recovery-free direct children; no cascade. Preparing/running/recovering/terminal/root work is rejected. |
| `goal_close` | Scope plus current goal `expected_version`, `expected_graph_revision`, Accepted. Seals intake only after aggregate closure checks. |

`expand` requires Scope, `expected_graph_revision`, `source_disposition` and
`tasks`. Optional `edges` have `source`, `target`, `edge_type`.
Each task entry requires `intent_key`; new tasks supply title/description/
acceptance. Other supported fields are `kind` (`task`/`epic`), `priority`,
`hold_reason`, `deferred_until`, `admitted`, or explicit reuse fields
`reuse_task_id`/`expected_version`. Do not send managed execution-class/profile/
resource fields. File/effect scope, input artifacts and plan-section pointers
belong in descriptions/acceptance, not invented JSON properties.

With a source, use `source_task_id` rather than `task_id`, plus source
`expected_version`, `attempt_id`, `claim_generation` and `native_agent_id`.
Source-free expansion is `continue` only, without execution tokens/version.

- `continue`: no completion or yield fields.
- `yield`: requires `checkpoint={"sequence":N,"reference":"..."}`, `reason`,
  `observations`; include new prerequisites and blockers in this same call.
- `complete`: requires all Accepted fields, including `parent_acceptance`.
  No checkpoint/reason/observations.

Up to 50 tasks and 200 edges per expansion, further bounded by `goal_policy`.
References in edges can use declared intent keys/task IDs, `"$goal"` and
`"$source"`. Stable intent keys deduplicate work; command IDs deduplicate requests.

## Start-to-finish application

1. Preserve the approved plan through MemPalace artifact MCP and file its
   searchable index. Bootstrap using the envelope above; retain actual returned
   goal and planning IDs.
2. Read the import task; claim it with Task fields. Read its reserved attempt;
   start using Bound fields with `native_agent_id` equal to the parent UUID.
   Refresh the goal graph and import version. Import with `action="expand"` and
   this payload shape (substitute current values):

```json
{
  "project": "demo",
  "goal_id": "tsk_goal_fixture",
  "expected_graph_revision": 1,
  "source_task_id": "tsk_import_fixture",
  "expected_version": 3,
  "attempt_id": "att_import_fixture",
  "claim_generation": 1,
  "native_agent_id": "11111111-1111-4111-8111-111111111111",
  "source_disposition": "complete",
  "summary": "Approved plan imported as an executable graph.",
  "evidence": ["artifact:exact-approved-plan-fixture"],
  "parent_acceptance": "Reviewed deliverables, acceptance and prerequisite coverage against the exact plan.",
  "tasks": [
    {
      "intent_key": "deliver",
      "title": "Deliver approved unit",
      "description": "Scope: approved files only. Input: artifact:exact-approved-plan-fixture#unit-1.",
      "acceptance": "Required implementation and validation evidence are present."
    }
  ],
  "edges": []
}
```

3. Read scoped readiness and claim the actual admitted task. Dispatch only after
   reservation, with a worker packet requiring **wait for start binding**.
   `task` returns the native agent ID; start with that actual ID, confirm the
   receipt and notify that worker. It verifies fresh binding before work.
   WAIT is a cooperative instruction/acknowledgment, not runtime suspension.
   Parent-only lifecycle publishing is workflow discipline under shared bearer
   trust, not service-enforced per-worker authentication. Verify both the current
   root parent and task/attempt/generation/agent tuple.
4. Checkpoint meaningful progress using Bound plus
   `checkpoint_sequence`/`reference`. Follow notifications for the known agent;
   result text is evidence to assess, not a durable state transition.
5. If A needs B, expand/yield with current source fields, checkpoint, reason,
   observations, B and these edges:

```json
[
  {"source": "new-prerequisite", "target": "$source", "edge_type": "discovered_from"},
  {"source": "new-prerequisite", "target": "$source", "edge_type": "blocks"}
]
```

   A becomes recovering. Explicitly reconcile A; once deliberately reopened,
   its blocker still prevents claim until B closes. Reclaim A with a new
   generation afterward. Yield/retry does not prove physical stop.
6. Inspect results against acceptance and current Bound identity. Complete with
   `summary`, actual `evidence` and the parent's explicit `parent_acceptance`.
   For example, acceptance text can describe which tests and artifact changes
   were examined; it must not claim physical settlement.
7. Read the complete scoped graph, not just readiness. Reject unwanted open
   proposals with `cancel` as below; reconcile recovering work and assess aggregate
   acceptance. Use `goal_close` with
   current goal version/graph revision and Accepted fields. Confirm `closed`
   and `sealed=true` before reporting goal completion.

## Rejecting a proposal without dispatch

An unwanted `admitted=false` proposal is still unresolved work until explicitly
rejected. Use the ordinary native envelope with `action="cancel"` and this payload
shape, replacing fixtures with fresh observed values:

```json
{
  "project": "demo",
  "goal_id": "tsk_goal_fixture",
  "task_id": "tsk_proposal_fixture",
  "expected_version": 1,
  "reason": "Discovery is outside the explicitly approved scope.",
  "observations": ["Parent reviewed the proposal and rejected it; no work was dispatched."]
}
```

No fake admission, claim, worker or execution tokens are needed. A never-started
proposal remains `admitted=false`, with null attempt and generation zero; its
status becomes `cancelled` and snapshot `cancellation_reason` records the reason.
Exact reason/observations persist in command history. Re-read current goal/version/
graph before closure.
Cancel advances only the cancelled task's version, not the root version or graph
revision; goal_close still rechecks member states. The reason is bounded to 8,192
UTF-8 bytes; observations use the 1–20 distinct, 2,048-byte string limits above.

Cancel also permits open work after explicit reconcile/retry, because its prior
uncertainty has been resolved. It refuses preparing/running/recovering work:
release current uncertain work, then reconcile with `decision="cancel"` when
warranted. Do not forge a release for a proposal without an attempt. Nested epic
cancellation requires terminal, recovery-free children and never cascades.
The root uses `goal_close`; cancelled-only work does not prove aggregate acceptance,
and rejecting a discovery is not completion of a required deliverable.

## Updating held or deferred work

When a legitimate hold/defer condition is resolved, update the existing open task;
do not cancel/recreate it or invent a claim. Use `action="update"` and exactly
`project`, `goal_id`, `task_id`, `expected_version`, `patch`:

```json
{
  "project": "demo",
  "goal_id": "tsk_goal_fixture",
  "task_id": "tsk_held_fixture",
  "expected_version": 1,
  "patch": {
    "hold_reason": null,
    "deferred_until": null
  }
}
```

The nonempty patch permits only `title`, `description`, `acceptance`, `priority`,
`hold_reason`, `deferred_until`, with existing content validators. JSON null clears
hold/defer; priority is an integer 0–4, and non-null defer is an explicit UTC
timestamp normalized by the existing validator. Title is nonempty (maximum 256
characters); description/acceptance permit empty strings with limits of
16,384/8,192 UTF-8 bytes. Non-null hold reason is nonempty, at most 8,192 UTF-8 bytes.
Only open non-root tasks/epics without assignee/recovery can update. Root,
active/preparing, recovering and terminal work is rejected; explicit recovery must
finish before updating reopened work. Scope/current-session/version checks apply.
No `admitted`, goal/mode, execution settings or attempt tokens are accepted.
Only the target version changes, not root/graph revisions. Confirm state/readiness
before claim; removing a hold does not satisfy dependencies or remaining gates.

## Interrupted sessions and limits

Release uncertain current work with Observed facts, not fabricated stop proof.
Read its recovering task, then reconcile using the retained attempt/generation/
agent association; null is valid only if never bound. Keep `hold` when outcome or
effects remain unresolved. `retry` is a deliberate parent decision after review,
not a timeout policy, and requires a fresh claim/start before further execution.

For an explicitly resumed goal in a new parent, use `resume` with the fresh goal
version and root `native.session_id` as `expected_session_id`. Preserve original
`native.issuing_session_id`. Resume changes **only the root** ownership, internal
session generation, transfer metadata and version. It leaves every member snapshot, status, version and generation
unchanged, keeping takeover bounded rather than rewriting a large active graph.
Root `native.session_generation` starts at 1 and increments per accepted transfer;
an attempt's recorded generation does not. Returning to a previously used UUID
therefore cannot restore that attempt's authority.

Old active attempts may still be stored `in_progress`, but fresh authorization
is false on UUID **or internal session-generation mismatch**, with
`reason="native_reconciliation_required"`; `authorization.session_id`
resolves the new root parent. Eligibility includes that reason code and snapshot
rows mark `needs_attention=true`. Stored active status is not permission to work.
Neither old-parent publication nor new-parent start/checkpoint/complete/source
expansion may adopt the attempt.

For **each** inherited active attempt, read current task/version and retained
attempt/generation/agent; the new parent issues `release` with reason/observations.
This moves that member to recovering. Refresh its version, then `reconcile` with
hold/retry/cancel. Already recovering work can reconcile directly. Only release
permits the inherited attempt-owner mismatch after root/session/scope checks.
Only a later claim advances `claim_generation`; never invent its increment on
transfer or supply the internal session generation as a caller field.

Members' recorded sessions remain historical until a later mutation. Human views
distinguish `recorded_session_id` from `current_parent=goal_session`. Service restart
is a separate path: it journals native interruption per active attempt, fences the
old epoch and leaves recovering work for explicit reconciliation under the new
epoch. Neither path proves process stop or settled effects.

These are cooperative observations/publication gates, not OS isolation, heartbeat
supervision or physical settlement. Missing notifications do not expire ownership.
Managed goals retain their own actor, host, lease and effect barriers.
