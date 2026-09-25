---
name: mempalace-tasks
description: >-
  Use when working on tracked MemPalace tasks, claiming or resuming task work,
  publishing discoveries, closing tasks or goals, or handling uncertain task
  ownership, leases, command outcomes, or recovery.
---

# MemPalace Task Safety

## Announcement

> Using mempalace-tasks to preserve current ownership and safe task publication.

## Authority boundary

This skill protects tracked work; ordinary memory filing and trivial conversations
do not require task creation. Task truth belongs to the shared sidecar authority,
not a drawer, KG projection, local todo, or remembered assignment.

Discover the sidecar's advertised `mptask_*` tools and argument schemas before
calling them. Use the registered actor identity and actual fields; every mutation
requires `command_id`, `actor`, and `expected_epoch`. Completion is
`target="closed"`, not `"done"`. Obtain `expected_epoch` from a current
read/health response's `epoch_id`. Freeze the authority, epoch, command ID and
exact payload for each logical request; never update its epoch on retry.
A new owner epoch revokes old execution
authorization, even if owner/attempt/generation fields otherwise match.
Missing service/schema support blocks tracked mutations; report it rather than
starting another writer or substituting direct KG, drawer, or event-log writes.
Native `mempalace_task_create` / `mempalace_event_ack` delegation acknowledgments
are not sidecar claims, leases, or completion evidence.

## Current execution authorization

Before starting or resuming work:

1. Read current task state with `mptask_get`; match its owner, `attempt_id`, and
   `claim_generation` to the host-provided worker identity and attempt tokens.
   Check task `version`, lease/deadlines, execution profile and resource barriers.
   An assignee name or an old successful receipt is insufficient.
2. A claim uses `task_id`, `expected_version`, and `supervisor_id`; the registered
   supervisor may claim for its worker using `worker_id`. A committed claim is
   **preparing**, not permission to execute. The assigned supervisor establishes
   preparation and reports it before work starts.
3. Execution requires current `mptask_get.authorization` **or** a freshly evaluated
   receipt's authorization: `authorization.fresh` and the matching task entry's
   `matches_current`, `lease_live`, and `authorized` must be true. Match owner and
   attempt/generation to the host packet, not merely any authorized task.
   A receipt's original `response`/`tasks` remain historical. `input.json` may hold
   a preparing-time snapshot; obtain a fresh read, not a mutation just to get
   authorization. Revalidate on resumption, conflict or changed generation/epoch.
   Historical `mptask_outcome` lookup never grants current authorization.

The registered host supervisor owns renewal, durable checkpoint reporting, worker
containment, and physical settlement/recovery attestations. It renews with
`expected_lease_revision`; progress reporting uses `checkpoint_sequence` and a
changed durable `reference`. Chat activity and notes are not progress. An
unconfirmed renewal grants **no additional time**: notify the supervisor. The host
may continue within the prior confirmed authorization bound, stopping by its
earliest lease/progress/hard cutoff or earlier explicit revocation. With no
confirmed bound, stop. Transient renewal response loss alone does not require an
immediate stop while that bound remains valid; it never extends authority.

## Publish discoveries with the source disposition

Use **one `mptask_expand`** to publish definitions/reuse, goal membership, initial
edges, and the source's disposition. Its worker-source fields are:

`goal_id`, `expected_graph_revision`, `source_task_id`, `expected_version`,
`attempt_id`, `claim_generation`, `tasks`, `edges`, `source_disposition`.

Each new task has a stable `intent_key` and its definition, acceptance and execution
requirements. An explicit reuse entry has `reuse_task_id`, `intent_key`, and
`expected_version`. Intent keys deduplicate discoveries; command IDs deduplicate
requests. In edges, use `{source, target, edge_type}`; endpoints can be intent
keys, declared task IDs, `"$source"`, or `"$goal"`.

For source A and discovery B, choose this recipe:

| Observable relationship | Atomic publication |
|---|---|
| B is independently actionable; A can continue | `source_disposition="continue"`; add B and `discovered_from(B,A)`. |
| B must wait for A | `continue`; also add `blocks(A,B)` before B becomes visible. |
| **A needs B before it can proceed** | **`source_disposition="yield"`**, `checkpoint={sequence,reference}`, `reason`, B, `discovered_from(B,A)`, and **`blocks(B,A)` in the same expansion**. Obtain the durable checkpoint from the supervisor. |
| A is finished and discovers final work | `source_disposition="complete"`, `summary`, `evidence=[durable references]`, and final tasks/edges together; alternatively confirm publication with `continue` before closing A. |
| B exceeds scope, budget, or available capabilities | Publish a non-runnable proposal with `admitted=false` for an explicit admission decision. |

For the prerequisite recipe, the blocking edge is
`{"source":"B-intent-key","target":"$source","edge_type":"blocks"}`.
Acceptance simultaneously checkpoints and revokes A. The supervisor then settles
its execution barrier; shared resources remain reserved until recovery succeeds.
A resumes only after prerequisites and recovery permit a **new claim generation**.
Intentional yield is not a failed attempt. Keeping A active while adding its new
blocker, or releasing A in a separate first/last call, does not implement this recipe.

Continue/yield require a live running source. Complete also permits a live settled
source with class-specific completion proof; settlement does not authorize new
execution. Stale sources cannot publish using a standalone create or a different
actor. Source-free admission is an explicit registered coordinator/operator action.

## Outcomes and execution barriers

| Observed command outcome | Safe next action |
|---|---|
| Timeout, `outcome_unknown`, pending, or ambiguous failure | Preserve authority, original epoch, exact command ID and payload. Resolve/retry that same scoped request; never substitute a newly read epoch. Absence from one read is not abandonment. |
| Committed, including a replay | Use the recorded result; independently revalidate current execution authorization. |
| Confirmed terminal `outcome="abandoned"` | That ID is permanently resolved. Re-read state and authority; if the operation is still needed, submit a **new command ID**. Preserve the discovery's stable `intent_key`. |

Resolve historical requests through
`mptask_outcome(epoch_id=ORIGINAL_EPOCH, command_id=ORIGINAL_ID)` using the original
epoch UUID; null is invalid. Reconnecting can supply a new transport credential but
must not upgrade the request's epoch. `resolution="not_recorded"` is not confirmed
abandonment or proof that external effects did not occur. A historical receipt
does not renew a lease or authorize execution; re-read current task authorization.

Recovery/settlement evidence must reflect actual effects:

| Execution class | Required recovery barrier |
|---|---|
| `isolated` | Revoke the old attempt's publication/acceptance capability (`publication_revoked`). |
| `shared_unfenced` | Establish `process_stopped` **and** `effects_reconciled`, including external jobs/effects. |
| `resource_fenced` | Install exact next resource-wide fences and reconcile effects; explicit fencing-unavailable fallback requires proven stop plus reconciliation. A replacement still needs fresh fence preparation. |

A dead shell is not proof that its cloud job stopped. Retained resources are not
available to another class/profile merely because its own task is ready. Workers
provide artifacts; registered supervisors/operators attest physical recovery.
The live owner closes ordinary work using `mptask_transition` with `task_id`,
`attempt_id`, `claim_generation`, `expected_version`, `target="closed"`, `summary`,
and nonempty `evidence`.
Shared completion also needs accepted class-specific settlement.

## Views and completion

Human `status`, `list`, `show`, `history`, and bounded `watch` are read-only views
through `mptask_snapshot`, `mptask_get`, and `mptask_history`. Preserve `fresh`,
`as_of`, reasons, scope and cursor information. Pinned pages, disconnected views,
drawers and KG projections are historical, not authorization. Inspection must not
claim, renew, sweep, reconcile, or silently start a writer.
Diagnostic config/token reads validate regular files and required permissions,
then fail explicitly if invalid. They do not create or chmod files or parent
directories. Only explicit init may create a missing service credential;
owner activation may create disposable runtime coordination.
Neither is read-only inspection, and private storage helpers are not
general-purpose config readers.

An empty ready frontier is **not** goal completion. Inspect running, blocked,
deferred, recovering, quarantined, and proposed work. Only an authorized
coordinator/operator requests `mptask_goal_close` with `goal_id`,
`expected_version`, `expected_graph_revision`, `summary`, and acceptance `evidence`.
The authority checks unfinished work/proposals and resource barriers, requires
completed work, and seals intake atomically. A race rejection means refresh and
reassess; cancelled-only work is not accepted success.

## Common mistakes

| Mistake | Correction |
|---|---|
| Expand a prerequisite while retaining A, then release | Use the complete atomic **yield** recipe above. |
| Treat remembered ownership or native acknowledgment as a claim | Verify the current sidecar generation, live lease and prepared execution. |
| Close because the ready queue is empty or the shell exited | Establish acceptance evidence and the appropriate goal/execution barrier. |

See [pressure scenarios](references/scenarios.md) for the baseline, parent-reported
six-case green result and remaining checks. Workflow/decomposition policy belongs
to the opt-in `palace-task-workflow` agent; neither artifact creates a fleet or
host adapter.
