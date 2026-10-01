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

For a handoff label rather than an exact task ID, enumerate
`mptask_snapshot({"filters":{"goal_id":"tsk_goal_fixture"}})` across all statuses
(omit `status`) and exhaust `next_cursor`. Inspect candidate IDs with `mptask_get`;
require a unique match to the intended scope/intent and verified repository/file
scope in the task and plan. `get` accepts an exact ID, not a label resolver;
`ready` omits blocked/claimed work. `project` is a task tag, not a repository
selector: use the stored project value in mutations, not the checkout's name.
Ambiguous matches do not authorize a guessed ID or duplicate task.

One retained native parent owns dispatch and lifecycle publication even when
the goal's tasks span repositories. Repository-scoped workers report to that
parent. Discovery in another repository does not authorize an independent
session's leaf claim, borrowing the root owner's UUID, whole-goal `resume`, or
another coordinator. Explicit named-goal transfer is a different operation,
not cross-repository participation.

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
| `claim` | Task, after choosing a supported path through the execution capability gate below. Reserves an attempt; status is `in_progress`, attempt status `preparing`, agent ID null. No execution yet. |
| `start` | Bound, with actual returned native agent ID (or parent session UUID). Checks the current reserved/preparing attempt, not running authorization. Binds once and makes attempt `running`. |
| `checkpoint` | Bound plus positive `checkpoint_sequence` and `reference`; sequence increases and reference changes. |
| `request_changes` | Bound plus `summary`, `evidence`, `findings`, and `next_action` (`rework` or `hold`). Current running leaf only; records outstanding acceptance findings. See the correction loop below. |
| `complete` task | Bound plus Accepted; requires a current running attempt and `review_resolution` when a review is outstanding. |
| `complete` non-root epic | Task plus Accepted, **without** attempt/generation/agent fields; children/gates must be resolved. A root uses `goal_close`. |
| `release` | Bound plus Observed; `native_agent_id` may be null only for an unbound reservation. Current parent may release its own or an inherited active attempt. Moves that task to `recovering`; no adoption or automatic retry. |
| `reconcile` | Bound plus Observed plus `decision` = `hold`, `retry` or `cancel`; requires recovering work, whose execution authorization is false. `hold` remains recovering; `retry` explicitly opens for a new claim; `cancel` records cancellation. Never accepts completion. |
| `resume` | Scope plus current goal `expected_version`, `expected_session_id`, Observed. Envelope carries the **new** parent UUID. Bounded root-only CAS changes owner/version, leaving all member snapshots unchanged but fencing old-parent publication. Follow paginated discovery and per-task recovery below; no adoption or goal-wide recovery gate. |
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
- `complete`: requires all Accepted fields, including `parent_acceptance`, and
  `review_resolution` if the source has outstanding review findings.
  No checkpoint/reason/observations. `continue` and `yield` cannot carry resolution.

Up to 50 tasks and 200 edges per expansion, further bounded by `goal_policy`.
References in edges can use declared intent keys/task IDs, `"$goal"` and
`"$source"`. Stable intent keys deduplicate work; command IDs deduplicate requests.

## Execution capability gate

Before **any claim/start**, inspect the parent's actual exposed `task` and
messaging schemas, not a worker/reviewer's possibly different surface.
Background dispatch is usable only when that schema supports it (use
`mode="background"` only when advertised) **and** `write_agent` supports
post-return delivery of binding confirmation to the launched worker.
Do not invent a mode argument or a synchronous/one-shot WAIT-then-message handshake.
If either capability is absent, select accessible parent-owned execution before
claim/start, binding the actual parent UUID; if the parent cannot access the
verified repository, inputs or required tools, report that explicit blocker
without claiming the task.

Failed or ambiguous dispatch, start **or binding notification** requires fresh
task/binding inspection and inspection of any known agent. Resolve uncertain task commands with
their original epoch/command/payload, not a new request inferred from messaging
failure. When execution remains uncertain, release/reconcile with current
observations before replacement; hold unknown effects. A failed message is not
physical stop, settled effects or durable task closure.

## Start-to-finish application

1. Preserve the approved plan through MemPalace artifact MCP and file its
   searchable index. Bootstrap using the envelope above; retain actual returned
   goal and planning IDs.
2. Select accessible parent-owned import execution through the capability gate.
   Read the import task; claim it with Task fields. Read its reserved attempt;
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

3. Read scoped readiness and select an executable path through the capability
   gate **before claiming** the actual admitted task.
   On the supported background-worker path, claim, then dispatch in the advertised
   background mode with a packet requiring **wait for start binding**. After
   `task` returns the actual agent ID, start with that ID, confirm the receipt and
   notify that worker through `write_agent`. It verifies fresh binding before work.
   On the accessible parent-owned path, claim/start with the actual parent UUID
   and execute locally; no worker/message handshake is needed. Otherwise report
   the explicit blocker without claiming. Handle failed notification as well as
   dispatch/start uncertainty through inspection and recovery before replacement.
   WAIT is a cooperative instruction/acknowledgment, not runtime suspension.
   Parent-only lifecycle publishing is workflow discipline under shared bearer
   trust, not service-enforced per-worker authentication. Verify both the current
   root parent and task/attempt/generation/agent tuple.
4. Checkpoint meaningful progress using Bound plus
   `checkpoint_sequence`/`reference`. On the worker path, follow notifications
   for the known agent; result text is evidence to assess, not a durable state
   transition.
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
   If acceptance fails, use the correction loop below rather than leave rejection
   only in chat. Outstanding findings require an exact `review_resolution` on
   eventual completion, including atomic expand/complete.
7. Read the complete scoped graph, not just readiness. Reject unwanted open
   proposals with `cancel` as below; reconcile recovering work and assess aggregate
   acceptance. Use `goal_close` with
   current goal version/graph revision and Accepted fields. Confirm `closed`
   and `sealed=true` before reporting goal completion.

## Acceptance rejection and correction loop

The parent records a failed acceptance assessment with `request_changes` before
sending correction instructions. This records a decision; it does not contact a
worker or launch another attempt. Use the existing native envelope and this
payload shape with actual Bound values:

```json
{
  "project": "demo",
  "goal_id": "tsk_goal_fixture",
  "task_id": "tsk_task_fixture",
  "expected_version": 3,
  "attempt_id": "att_task_fixture",
  "claim_generation": 1,
  "native_agent_id": "returned-worker-fixture",
  "summary": "The output does not meet the retry-safety criterion.",
  "evidence": ["artifact:failed-retry-test-fixture"],
  "next_action": "rework",
  "findings": [
    {
      "id": "AC-1",
      "criterion": "Retrying the same request must not duplicate its effects.",
      "feedback": "The second request created another record; fix and rerun the retry case."
    }
  ]
}
```

`findings` contains 1-20 entries with distinct, nonblank IDs (at most 128 UTF-8
bytes). `criterion` and `feedback` are nonblank and at most 2,048 UTF-8 bytes each.
Summary and evidence use the existing bounds and must be nonblank.
The command UUID becomes `native.review.id`; the record preserves findings,
decision/evidence, parent, time and reviewed attempt. A later `request_changes`
must carry every outstanding finding ID with the original criterion. It may
revise feedback and add findings within the bound; its new review ID makes an old
resolution stale. Earlier reviews remain in command history.

- `rework` preserves the running task and worker binding. After confirmed
  publication, the parent sends the known worker the review ID, findings and
  expected evidence through supported same-task messaging, then reassesses the
  returned correction. Parent-owned work follows the same review gate locally.
- `hold` atomically moves the task to `recovering` and revokes publication. It
  proves neither physical stop nor settled effects. Inspect/reconcile before
  another attempt. For replacement after `rework`, explicitly release, reconcile
  with `retry`, and claim/start a fresh attempt; never silently rebind the worker.
- A failed/ambiguous notification is not delivered feedback. Inspect the known
  worker and current task, resolve uncertain command outcomes using the original
  envelope, and release/reconcile uncertain execution. Do not launch a duplicate.

An unresolved review appears in `needs_attention` and survives retry, transfer
and restart. Attention is not a release list: an authorized worker correcting
its result may continue. After an explicit retry, review findings do not prevent
readiness; they prevent acceptance. Unrelated eligible work remains independent.

Once the parent has examined corrected evidence, add this field to the normal
`complete` payload (or source `expand` with disposition `complete`):

```json
{
  "review_resolution": {
    "review_id": "22222222-2222-4222-8222-222222222222",
    "findings": [
      {
        "id": "AC-1",
        "summary": "Repeated request now returns the same record without a second write.",
        "evidence": ["artifact:passing-retry-test-fixture"]
      }
    ]
  }
}
```

Replace `review_id` with the exact current persisted review ID. Cover every
finding exactly once, with a nonblank summary and nonempty evidence references;
missing, duplicate, unknown or stale IDs are rejected atomically. Existing
`summary`, `evidence`, `parent_acceptance` and current Bound fields are still
required. Successful completion stores the resolution and marks the review
accepted. Without an outstanding review, omit `review_resolution`; it is also
invalid on epic/goal closure or non-completing expansions.

Do not cancel/recreate a wanted task to erase review findings. Legitimate
cancellation preserves rejection history and is not positive acceptance.
Tasks with no review retain their existing completion contract. This gate
enforces recorded finding coverage, not the semantic truth of evidence or
coverage of criteria that the parent never recorded. No automatic timeout,
worker restart or independent acceptance verifier is added. A disconnected or
inactive parent still needs explicit resumption; the durable review makes its
unfinished decision inspectable.

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

### Paginated discovery and current recovery decisions

After confirmed resume, enumerate attention candidates through task MCP:

```json
{"filters":{"goal_id":"tsk_goal_fixture","needs_attention":true}}
```

That is the argument object for `mptask_snapshot`, not a native mutation payload.
The default page is **100 rows** (maximum `limit=500`); goals may contain 10,000
tasks. Continue with `{"cursor":"<returned next_cursor>"}` until `next_cursor`
is null. Omit `filters` on continuation, or repeat the exact initial filters;
changing them (including sending `{}`) invalidates that cursor. An optional
`limit` is a page size, not a total-result cap.

Retain the snapshot ID, scope, `as_of` and unique candidate task IDs across pages.
Continuation pages intentionally have `fresh=false`, `reason="pinned_snapshot"`;
even a fresh first page is only an observation, never a mutation token.
Collect pages before lengthy recovery: cursors expire after 60 seconds and may
be evicted. On `snapshot_expired`, restart discovery and deduplicate task IDs;
never interpret a partial scan, read failure or missing page as complete/empty.
If a complete scan cannot be obtained, report discovery as incomplete rather
than silently dropping work. A later fresh scan can find work changed since the
pinned snapshot; it need not be empty to permit unrelated ready work.

For **each candidate**, use fresh `mptask_get` on the root and task before
mutation. Check authority/epoch freshness, stored mode/project/goal scope and
that this session still owns the root. Copy current version, attempt ID,
`claim_generation` and agent association from that task, not the page or a prior
receipt. Reclassify from current status, eligibility reasons and authorization:

| Fresh observation | Next action |
|---|---|
| `in_progress` and `native_reconciliation_required` | Current parent releases that inherited active attempt with fresh Bound fields and explicit reason/observations. Read task and root/epoch again; only then reconcile its confirmed recovering state. |
| `recovering`, including a prior reconciliation `hold` | Reconcile directly with fresh Bound fields and reviewed observations; do not release it again. |
| Current authorized active work, open/terminal work, or another attention reason | Do not apply inherited-attempt release. Follow the observed state's supported action and blockers, or report the unresolved reason. |

`needs_attention` is not an inherited-attempt-only filter: it also includes
recovering, quarantined and escalated rows. An open task's `hold_reason` alone
does not make it an attention row; inspect all statuses without that filter for
complete scope/label discovery or goal acceptance.

Reconciliation records explicit known/unknown facts and the decision rationale:
keep `decision="hold"` while outcome/effects remain unknown. Choose `retry` only
after review supports deliberately repeating the work; choose `cancel` only when
review supports abandoning it. Explain the relevant observations, remaining
uncertainty and why the chosen action is warranted, not merely that time passed
or the queue should clear. Neither decision proves physical stop or settlement;
unresolved effects are not a reason to force reopening or cancellation.
An ambiguous mutation retains its original epoch/command/payload for outcome
resolution; a conflict requires fresh reads and reassessment, not replay with
new tokens under the old command ID.

Readiness remains **per task and its dependencies**, not a requirement that all
inherited attempts or held siblings be cleared before any claim. The current
parent may claim unrelated eligible work while recovery remains held.
Dependency-blocked work stays blocked, including after its prerequisite is
reopened by retry; retry is not successful completion of that prerequisite.
Only a later claim advances `claim_generation`, followed by fresh start binding;
never invent its increment on transfer or send internal session generation.

Members' recorded sessions remain historical until a later mutation. Human views
distinguish `recorded_session_id` from `current_parent=goal_session`. Service restart
is a separate path: it journals native interruption per active attempt, fences the
old epoch and leaves recovering work for explicit reconciliation under the new
epoch. Neither path proves process stop or settled effects.

These are cooperative observations/publication gates, not OS isolation, heartbeat
supervision or physical settlement. Missing notifications do not expire ownership.
Managed goals retain their own actor, host, lease and effect barriers.
