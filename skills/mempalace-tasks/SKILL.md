---
name: mempalace-tasks
description: >-
  Use when explicitly tracking or resuming a MemPalace goal, including native
  Copilot fleet work, claiming tracked tasks, publishing discoveries, closing
  tracked tasks or goals, or handling uncertain ownership, leases, outcomes, or recovery.
---

# MemPalace Task Safety

## Announcement

> Using mempalace-tasks to preserve current ownership and safe task publication.

## Authority boundary

Tracking is **explicit opt-in per goal**. Agent selection, available tools,
installed guidance, ordinary `/fleet` or recall does not enroll work. Ordinary
dispatch/planning needs no task service. Named resume retains only that goal's
scope; another goal needs separate opt-in.
Phrases such as `/fleet execute and track this as a goal` or `track this as a
goal` are explicit opt-ins; they do not require the user to repeat the current
approved plan or separately select the workflow agent.

Task truth belongs to the shared sidecar authority, not a drawer, KG projection,
native worker result, or remembered assignment. Existing memory routing
still applies to knowledge and conventions, not task ownership.

Discover the sidecar's advertised `mptask_*` tools and argument schemas before
calling them. Select tools by their configured MCP server and advertised name,
not an unqualified name shared with another tool provider. All task storage reads
and writes use `mempalace-tasks` MCP; MemPalace context and evidence storage use
`mempalace` MCP. If the needed MCP server is unavailable, report that storage
operation as blocked.
Choose the goal's **stored coordination mode** before writing. New explicitly
tracked native goals use the cooperative native API; existing managed goals keep
their managed-host contract. A named resume does not migrate modes.
Absent native metadata means legacy managed; unknown explicit modes fail closed.

- Native: `mptask_native(action, session_id, command_id, expected_epoch, payload)`.
  Use the actual issuing parent session UUID and the strict advertised payload
  for that action; **no actor name** or separate actor provisioning.
- Managed: use registered actor identity and the existing mutation tools with
  `command_id`, `actor`, and `expected_epoch`. Task completion remains
  `mptask_transition(target="closed")`, not `"done"`.

Obtain `expected_epoch` from a current
read/health response's `epoch_id`. Freeze the authority, epoch, command ID and
exact payload for each logical request; never update its epoch on retry.
A new owner epoch revokes old execution
authorization, even if owner/attempt/generation fields otherwise match.
Missing service or required tool/schema support blocks tracked mutations.
Missing registered actor/supervisor blocks managed execution, **not native
cooperative mode** when the native API is available.
Report the request and blocker in the conversation without fabricating IDs,
starting another writer, or substituting another store or direct KG, drawer, or
event-log writes.
Native `mempalace_task_create` / `mempalace_event_ack` delegation acknowledgments
are not sidecar claims, leases, or completion evidence.

## Native cooperative ownership

Native fleet's **existing parent remains the sole dispatcher**. It issues durable
lifecycle commands and binds workers; no extra coordinator agent, daemon,
supervisor, periodic timer or independent queue consumer is required.
This also applies to a goal spanning repositories: one retained parent dispatches
repository-scoped workers and publishes their lifecycle. Discovering relevant
work in another checkout does not authorize whole-goal `resume`, borrowing the
owner's UUID, or a second coordinator. An independent session cannot claim a leaf
without owning its root; transfer requires an explicit named-goal resume.
The service's existing authenticated MCP connection is the trust boundary.
Session UUIDs identify owners; they are not secrets, bearer tokens or per-user
authentication.

Use one canonical issuing-session UUID: retain the harness UUID when available,
otherwise issue it once. Keep it stable through compaction/resume. A fork/new parent has a different
UUID; it must explicitly transfer the named goal with native `resume`, current
CAS state and reconciliation. Do not copy another session's UUID to impersonate
continuity. Lost binding requires inspection/explicit transfer, not guessed
continuity. One session may own multiple **separately opted-in** goals.

| Native action | Required boundary |
|---|---|
| `bootstrap` | Atomically enroll the issuing session and create scoped cooperative goal plus import/planning task. No pre-registered coordinator. |
| `expand` | Publish graph membership/definitions/initial edges and source disposition atomically, within the native goal scope. |
| `update` | Current-version content patch for open non-root work; null clears hold/defer. No admission/execution edits or recovery bypass. |
| `claim` | First select an executable path through the capability gate below; then reserve the current version's attempt/generation **before** native dispatch. Reservation is not a started worker. |
| `start` | Bind the actual agent ID returned by dispatch, or the actual parent session UUID for parent work. Match task/attempt/generation/session/epoch. |
| `checkpoint` | Preserve meaningful progress and durable references for current bound work, not a model-heartbeat lease. |
| `request_changes` | Persist criterion-specific findings for a current running leaf, with explicit `rework` or `hold`; never confuse the receipt with feedback delivery or process stop. |
| `complete` | Require current binding, acceptance assessment, durable evidence and exact resolution of outstanding review findings. Success text alone is insufficient. |
| `release` / `reconcile` | Resolve cooperative ownership with explicit known/unknown facts; do not infer stop or settled effects. |
| `resume` | Bounded root-only owner CAS; inherited active status stays unchanged but unauthorized. Discover all attention pages and classify fresh per-task state as below; never adopt inherited attempts. |
| `cancel` | Reject open non-root work/proposals with current version, reason/observations; no attempt tokens. Active/recovering work requires release/reconcile instead; never cancel the root. |
| `goal_close` | Confirm aggregate acceptance, current goal/graph revisions, resolved work/proposals and sealed intake. |

Read the [native payload/read reference](references/native.md) and live schema
before calls. Native authorization intentionally has `lease_live=false` and
`physical_supervision=false`. Require fresh matching `authorized=true` **only for
executing/publishing running-source work**. Bootstrap, update, claim, start, release,
reconcile, resume, cancel and aggregate closure use action-specific checks; reserved or
recovering attempts intentionally are not execution-authorized. Do not require
running authorization before an action that establishes/reconciles it.
Retain and compare current session binding, epoch, task version, graph revision,
attempt and generation as applicable. Reject wrong task/agent or stale results;
an old receipt, summary, native result or remembered token cannot authorize
publication.
Current parent is the root goal's `native.session_id`, also resolved in fresh
`authorization.session_id`. Transfer leaves all member snapshots untouched;
`native_reconciliation_required` marks unauthorized inherited active work. Enumerate
`mptask_snapshot(filters={goal_id, needs_attention:true})` through every
`next_cursor`; these pinned observations include recovering work and unresolved
reviews, not only inherited active attempts. Attention is not a release list:
authorized same-attempt rework may continue.
Follow the [paginated recovery procedure](references/native.md#interrupted-sessions-and-limits):
refresh each task, current root ownership and epoch before mutation; release
inherited active work with freshly read tokens, then read/reconcile, while already
recovering work reconciles directly. Keep unknown effects on `hold`, not forced
retry/cancel. Readiness is per task and its dependencies: unrelated eligible work
may be claimed while other work remains held; recovery is not a goal-wide barrier.
Internal session-generation fencing prevents A→B→A attempt revival; callers never
supply `session_generation`. `claim_generation` changes only on claim.

### Pre-claim execution capability gate

Before claim/start, the parent inspects its **actually exposed** `task` and
messaging schemas. A worker/reviewer's different tool surface does not establish
the parent's capabilities. Choose one executable path:

- **Background worker:** only when the parent's schema supports background
  dispatch (for example `mode="background"` when advertised) and `write_agent`
  can deliver the binding confirmation after dispatch returns.
- **Parent-owned work:** choose before claim/start, only if the parent can access
  the verified repository, inputs and required tools; bind the actual parent UUID.
- **Neither available:** report the explicit capability/access blocker; do not
  claim work that cannot execute through either path.

Never guess a `mode` argument or attempt a synchronous/one-shot WAIT-then-message
handshake. On the supported background path, claim before dispatch; the initial
packet requires waiting for confirmed `start`
binding. Start with the actual returned agent ID, then confirm via `write_agent`;
the worker verifies fresh task/agent/attempt/generation before work.
If dispatch, start **or binding notification** fails or is ambiguous, inspect
current task/binding and any known agent, and resolve any uncertain task command
using its original envelope. If execution remains uncertain, release/reconcile
with current observations before replacement, retaining hold for unknown effects.
Messaging failure proves neither physical stop nor durable closure.
WAIT is an instruction/acknowledgment, not enforced suspension. Parent-only
lifecycle publishing is workflow discipline under shared MCP trust, not per-worker
access control.

No short-TTL model renewal loop or retry-on-silence applies to native work.
Compaction, long reasoning and missing notifications are not evidence of death.
Checkpoint before interruption/yield where possible, then inspect and reconcile
uncertainty explicitly. Epoch/version/attempt/generation checks fence stale task
publication; they do not stop processes or undo external effects.
Cooperative completion is accepted task evidence, **not managed-host assurance**.
Native mode cannot substitute for managed isolated/shared/fenced execution
requirements; work needing those guarantees remains managed or a non-runnable
proposal.

### Acceptance rejection boundary

When a current worker's result fails acceptance, persist `request_changes` with
bounded findings (`id`, original `criterion`, actionable `feedback`), evidence
and an explicit next action. `rework` retains the binding; `hold` revokes it into
recovering without claiming physical stop. The parent delivers correction
instructions separately and checks notification outcomes. Replacement still
requires release/reconcile/retry and a fresh claim/start.

An outstanding `native.review` survives recovery and owner transfer. Completion,
including expand/complete, requires `review_resolution` naming the current
review and covering every finding exactly once with summary/evidence. Repeated
reviews retain existing finding IDs/criteria; do not drop findings, weaken
acceptance or cancel/recreate wanted work to bypass them. A receipt validates
coverage and ownership, not semantic correctness. Inspect cited evidence before
providing `parent_acceptance`. Missing deployed action/schema support is a
version blocker, not permission to substitute chat-only acceptance.
See the [exact correction contract](references/native.md#acceptance-rejection-and-correction-loop).

## Native plan ingestion boundary

Preserve the approved plan **verbatim** through artifact MCP before decomposition;
retain canonical ID, SHA-256 and size. Duplicate-check, then file a searchable
drawer in the project's `plans` room with objective, stages, artifact ID/hash.
This index contains no task status/ownership; the artifact wins any disagreement.
Artifact failure blocks ingestion; report index failure without replacing provenance.
The root holds concise objective, aggregate acceptance, scope/budgets and artifact
reference, not the full prose.

- Define independent actionable tasks with stable intent, acceptance, execution
  requirements and plan-section references.
- Publish membership, definitions/reuse and initial blockers in **one expansion**;
  no runnable task before all its known initial blockers.
- Default to parallel. Formatting/order are not dependencies; use real prerequisite
  outputs and necessary stage-exit-to-entry edges, not all-to-all barriers.
- Keep rationale in artifact/descriptions; validation is acceptance/evidence unless
  independently actionable.
- Ambiguous, unsupported and over-budget work stays `admitted=false`.

Oversized graphs use bounded, topological, dependency-closed batches: prerequisites
already exist or appear in the same batch. If that is impossible, keep proposals
non-runnable; never attach a known initial blocker after runnable publication.

Native `bootstrap` creates the root and planning/import task. Verify parent-owned
access through the capability gate, then claim/start the import task as parent
work, publish the graph, and complete that source only when the final
required batch includes completion atomically. Managed bootstrap remains
`mptask_bootstrap`; managed coordinator-only planning without a compatible host may
publish authorized source-free batches but cannot fabricate import execution.

## Transport is not supervision

The stdio frontend proxies one pinned HTTP owner. Launcher policy permits startup;
external mode only connects. Neither supplies managed supervision.

Owner failure or transport error can close the gateway. Reconnection is explicit
operator/harness setup, not an invented workflow tool; it does not itself authorize
mutation retry. Never restart launcher mode merely to inspect: use an already
connected task MCP read or report the setup blocker. Stdio EOF closes the
frontend, not the shared owner. Canceling a native
worker neither cancels its durable task nor proves
physical settlement.

## Managed-host execution authorization

**Read [the managed-host contract](references/managed.md) before managed task
operations.** It retains actor/supervisor preparation, live leases, renewal,
checkpoints and physical recovery/settlement requirements. Do not impose those
short-lease prerequisites on native cooperative work or bypass them on managed work.

## Publish discoveries with the source disposition

Use **one atomic expansion**: native `mptask_native(action="expand")` with its
advertised payload, or managed `mptask_expand`. Both publish definitions/reuse,
goal membership, initial edges and source disposition together.
The managed worker-source fields are:

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
| **A needs B before it can proceed** | **`source_disposition="yield"`**, `checkpoint={sequence,reference}`, `reason`, B, `discovered_from(B,A)`, and **`blocks(B,A)` in the same expansion**. Native also requires `observations`; parent supplies actual checkpoint, managed supervisor supplies its checkpoint. |
| A is finished and discovers final work | `source_disposition="complete"`, `summary`, `evidence=[durable references]`, and final tasks/edges together; native also requires `parent_acceptance`. Alternatively confirm publication before closing A. |
| B exceeds scope, budget, or available capabilities | Publish a non-runnable proposal with `admitted=false` for an explicit admission decision. |

For the prerequisite recipe, the blocking edge is
`{"source":"B-intent-key","target":"$source","edge_type":"blocks"}`.
Acceptance simultaneously checkpoints and revokes A's source publication.
Native A becomes recovering; explicit reconcile must choose retry before a fresh
claim/generation. Prerequisites still block readiness; yield is not physical settlement.
For managed A, the supervisor then settles
its execution barrier; shared resources remain reserved until recovery succeeds.
A resumes only after prerequisites and recovery permit a **new claim generation**.
Intentional yield is not a failed attempt. Keeping A active while adding its new
blocker, or releasing A in a separate first/last call, does not implement this recipe.

For managed work, continue/yield require a live running source. Complete also permits a live settled
source with class-specific completion proof; settlement does not authorize new
execution. Stale sources cannot publish using a standalone create or a different
actor. Managed source-free admission is an explicit registered coordinator/operator
action; native publication stays within the issuing session's goal scope.

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
A replacement owner epoch is not automatic retry authorization for that request.

Native `complete` does not confer [managed settlement](references/managed.md#recovery-and-completion).

## MCP state and handoff references

Verify explicit authority/goal/task through fresh MCP. Handoffs/memory supply
identity hints, never current authorization/completion; resolve ambiguous references
before operations.
Labels such as `C1` are not task IDs. Enumerate the named goal with
`mptask_snapshot(filters={goal_id})`, omitting status and exhausting its cursor.
Use candidate IDs for `mptask_get` and require a unique scope/intent match before
dispatch. `project` is a stored task tag, not necessarily a repository; verify
repository/file scope from the task and plan rather than guessing from cwd.
`ready` cannot discover blocked or claimed labels, and `get(label)` cannot resolve
them. Ambiguity remains a blocker, not permission to create duplicate work.

Handoff/status packets contain bounded fresh MCP observations:

```text
authority_id: <verified authority>
goal_id: <explicitly tracked goal>
task_id: <verified task>
acceptance: <task acceptance, with references for lengthy detail>
observed_as_of: <fresh read as_of>
observed_epoch: <fresh read epoch_id>
observed_task_version: <task version>
durable_status: <observed durable status>
execution_blocker: <current reason, or none when verified>
```

These are observations, not authority or execution tokens. Include stored mode and
issuing session UUID for native work, plus native agent, attempt and generation
fields only when actually returned/supplied. Keep credentials,
retry tokens, full graphs and unrelated memories out of the packet. Durable notes
and artifacts, when needed, are filed through the appropriate task/MemPalace MCP
operation; do not copy task status into drawers or notes as a second authority.

Native session planning belongs to the harness; do not mirror the durable graph
into native todos or SQL. Use fresh MCP state for tracked
task decisions. If a native result or remembered completion conflicts with that
state, report the conflict and use the MCP state. Report accepted completion only
after confirmed durable closure; an unavailable MCP service means unknown or
blocked.

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
deferred, recovering, quarantined, and proposed work. The native parent requests
native `goal_close` using its current session binding; for managed goals only an
authorized coordinator/operator requests `mptask_goal_close` with `goal_id`,
`expected_version`, `expected_graph_revision`, `summary`, and acceptance `evidence`.
The authority checks unfinished work/proposals and resource barriers, requires
completed work, and seals intake atomically. A race rejection means refresh and
reassess; cancelled-only work is not accepted success.

## Common mistakes

| Mistake | Correction |
|---|---|
| Expand a prerequisite while retaining A, then release | Use the complete atomic **yield** recipe above. |
| Demand a separate actor/supervisor for native API work | Bootstrap the actual issuing session; keep managed requirements mode-specific. |
| Treat remembered ownership or native acknowledgment as a claim | Verify current mode/session/attempt/generation and actual binding; managed work also requires live lease/preparation. |
| Admit a rejected proposal to cancel it | Native cancel rejects it in place; preserve `admitted=false`. |
| Close because the ready queue is empty or the shell exited | Establish acceptance evidence and the appropriate goal/execution barrier. |

See [pressure scenarios](references/scenarios.md) for expected behavior,
simulation coverage and remaining checks. Workflow/decomposition policy belongs
to the opt-in `palace-task-workflow` agent. Cooperative native support is not a
native supervisor or managed-host adapter.
