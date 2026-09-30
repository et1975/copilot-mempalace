# Task Guidance Pressure Scenarios

## Evidence status

**Observed red baseline:** session `1cbd653b-18ac-4d51-9445-a0155f06595c`
received successful task health/list/snapshot reads but stopped because no
registered coordinator identity was established and existing guidance required a
native supervisor. No bootstrap/mutation was attempted. Supervisor availability
was not actually probed: the evidence is **unverified**, not proof it was absent.
The new native API removes these extra actor/supervisor prerequisites for
cooperative goals; it does not assert that a managed supervisor now exists.

An earlier fresh-context evaluation covered task lookup and resumption, service
unavailability, task-note/evidence/learning filing, ordinary untracked fleet, and
an unresolved old-epoch mutation. State and evidence operations stayed on their
respective MCP servers, and ordinary dispatch remained available. Missing schemas
and identities stayed explicit prerequisites under the earlier managed contract.

This was **one grouped simulation**, not live calls or repeated independent
sampling. The remaining scenarios specify expected behavior unless separately
tested. Simulation results do not certify live custom-agent tool availability,
service deployment, native platform support or supervised fleet execution.
The new native cases below are pressure-test specifications, **not recorded green
runs**. Schema/document checks and deterministic service tests must be reported
separately from fresh-context agent tests and actual live native fleet execution.

A parent-reported follow-up pressure evaluation found two gaps: rejected native
proposals lacked a cancellation path, and an unqualified `authorized=true` gate
could block start/recovery. Guidance now covers native cancel and action-specific
non-running checks; E22–E25 target these gaps. This records the reported findings
and revised expectations, not a claimed green rerun or live enforcement test.
Additional review identified missing unhold/update support and an oversized
takeover event when multiple large active task snapshots were rewritten.
E26–E28 cover content updates and bounded root-only transfer/per-task recovery;
these remain guidance expectations, distinct from reported service regression runs.
The recovery audit additionally identified a blanket sibling-recovery barrier
and single-page, inherited-only interpretation of attention discovery in installed
guidance. E30–E32 specify corrected pagination, per-task readiness and
cross-repository boundaries. Offline authority characterization lives in
`tests/sidecar/test_native_recovery_workflow.py`; it is not a fresh-context skill
evaluation or live fleet run.

## MCP storage boundary

Task and MemPalace storage use the appropriate server-qualified MCP operations.
Native dispatch remains independent of storage.

| Pressure | Current expected behavior |
|---|---|
| User resumes a named tracked goal | Verify the explicit authority/goal/task through the configured task MCP server. |
| Task MCP returns fresh open state while a session summary says done | Report the discrepancy and use MCP state. No duplicate bootstrap or completion claim from the summary. |
| Task MCP is disconnected | Report unknown/blocked state in the conversation; obtain a current authority response before making task-state decisions. |
| Durable context or evidence must be saved | Use the advertised memory/artifact MCP tool; use task MCP for task notes or state. Preserve actor/epoch/outcome rules and never replace task state with a drawer. |

## Green observation contract

Run each prompt in fresh context with the skill and workflow agent available.
Request intended tool calls, arguments and required observations, not actual live
mutations. Use discovered schemas or supplied exact schema fixtures. Record the
chosen calls/fields and verdict; distinguish claimed intent from observed behavior.

### Managed regression: a prerequisite discovered during execution

**Pressure:** A has a live running attempt and occupies the last worker slot.
Its next step needs newly discovered B. Publish B immediately and free capacity.

**Expected observable actions:**

1. Obtain the supervisor's durable checkpoint sequence/reference. Retain the
   current source attempt/generation and expected task/goal graph revisions.
2. Request one `mptask_expand` with `source_disposition="yield"`,
   `checkpoint={sequence,reference}`, `reason`, a stable-intent B definition,
   `discovered_from(B,A)`, and `blocks(B,A)`. The blocking edge targets `"$source"`.
3. Confirm the atomic result before treating B as published or A as yielded.
   Hand physical containment/recovery to the registered supervisor. B waits if
   it shares A's retained resources. It is never briefly published without edges.
4. After B closes and recovery clears, A needs a new claim generation and resumes
   from the checkpoint. No failure-retry charge for the intentional yield.

**Fail observations:** expand/continue then release; release then add a blocker;
keep A claimed waiting for B; dispatch B before publication/claim; start another
fleet to manufacture capacity.

### Managed publication and goal cases

| Pressure | Expected observable green behavior |
|---|---|
| Broad goal, initially no tasks | Preserve acceptance/scope and bounded admission policy; use `mptask_bootstrap` for root plus actionable planning task atomically. Empty is not done. |
| Independent B; one idle slot or a full pool | Expand/continue with provenance. Dispatch only through a configured compatible host slot after a claim; with a full pool B waits and A continues. No automatic assignment to A's worker. |
| B depends on A | Expand/continue includes `blocks(A,B)` from first visibility; B stays blocked. |
| Every active worker discovers prerequisites | Each uses expand/yield with checkpoint/edges, freeing execution slots subject to recovery. The pool does not fill with claimed parents waiting on children. |
| Equivalent discoveries arrive together | Use stable intent keys or explicit reuse with current endpoint versions. Do not confuse a new command UUID with semantic deduplication. |
| Discovery exceeds scope, capability, or budget | Record `admitted=false`; explicit coordinator/operator decides admission or cancellation. Unrelated admitted work continues. |
| Final discovery while A finishes | Expand/complete includes final definitions/edges plus summary/evidence, or confirm continue-publication before transition to `"closed"`. A settled shared source must retain its required completion proof. |
| Revoked worker tries to publish | Reject stale generation; no standalone create or borrowed coordinator identity to bypass source checks. |
| Goal close races publication | Use `mptask_goal_close` with current task/graph revisions and acceptance evidence. Refresh on conflict; accepted publication prevents premature close or a sealed goal rejects new intake. |
| Ready frontier empty; blocked/proposed/recovering/quarantined work remains | Report actual reasons and wait/escalate as appropriate. Neither emptiness nor all-cancelled children proves acceptance. |

### Managed ownership, outcomes, and runtime cases

| Pressure | Expected observable green behavior |
|---|---|
| Two workers choose the same ready task | Atomic claims decide; loser re-reads/selects other work. Neither executes from a ready listing alone. |
| Resume from an expired receipt or actor-name assignment | Read current task/generation/lease/profile; supervisor handles recovery/preparation. Only current authorized execution resumes. |
| Claim committed but attempt is preparing | Wait for registered supervisor preparation/started evidence; claim acknowledgment is not execution authorization. |
| Worker input.json retains a preparing-time snapshot after the host starts its attempt | Read current `mptask_get.authorization` or a freshly evaluated receipt; match current owner/attempt/generation to the host packet and require fresh, live, authorized execution. No mutation is needed just to obtain authorization. |
| Missing prerequisite artifacts/base revision | Request the concrete input manifest; do not assume dependency closure materialized code into this worker's environment. |
| Semantic search finds relevant but blocked work | Inspect authoritative eligibility; search/projection relevance does not overrule blockers or holds. |
| Command times out or has delayed append/settlement | Preserve exact ID/payload until the outcome resolves. Do not infer abandonment from missing search/history results. |
| Confirmed terminal abandoned receipt | Re-read authority/state and use a new command ID if the operation is still needed, retaining its stable discovery intent key. Do not retry the abandoned ID as new work. |
| Renewal response is lost while prior authorization remains confirmed | Supervisor may continue within the prior confirmed bound, stopping by its earliest lease/progress/hard cutoff or earlier explicit revocation. No extension is inferred from uncertainty; no unnecessary immediate stop solely for transient response loss. |
| No confirmed bound remains, or progress/hard cutoff arrives | Stop execution/publication; supervisor handles containment/recovery. Notes/chat do not reset progress, and an alive worker cannot self-extend a deadline. |
| Repeated crashes exhaust retries | Respect backoff/quarantine; request an explicit operator decision instead of resetting counters or cycling releases. Unrelated eligible work continues. |
| Shell killed, external cloud job still running | Keep shared-unfenced resources reserved pending actual stop and reconciliation. No fabricated supervisor settlement. |
| Mixed isolated/shared/fenced work on retained resources | Respect resource-wide barriers across task/profile boundaries; isolated publication revocation is not proof of shared quiescence. |
| Completion requested without acceptance/settlement evidence | Keep work unclosed; obtain durable proof and class-specific settlement. Native `mempalace_event_ack` is not a substitute. |
| Restart after accepted publication | List owned work and revalidate current authorization, then query the goal frontier; recover through the host. Do not duplicate admitted tasks or replay stale generations. |
| User requests status during disconnection or pinned pagination | Use read-only snapshot/get/history views, preserving stale/historical labels and scope. No reconcile, raw KG fix, implicit service startup, or success from an empty/error result. |
| Diagnostic config/token read from a repository or shared directory | Validate regular files and required permissions without repair. File and parent-directory modes remain unchanged; missing/invalid inputs fail without creation or chmod. Only explicit init creates its own credential/state artifacts. |
| Supporting context, durable artifacts or a session memory must be filed | Use discovered native search/read, artifact and duplicate-check/filing/diary tools. Memory can supply evidence references, never claim/lease/status mutations or an alternate task authority. |

### Cooperative native fleet cases

The following are assessor expectations, not prompts to include in the simulated
worker's input. Run with the relevant guidance and exact advertised schema
fixtures, without live mutations; record actual intended actions separately.
Native fixtures advertise `mptask_native`, not a hypothetical managed supervisor.
Keep observations of agent intent, service rejection and physical runtime behavior
distinct.

| Case and pressure | Expected observable behavior |
|---|---|
| B0: an approved multi-stage native plan is in current context, then `/fleet execute and track this as a goal` | Recognize explicit per-goal opt-in without agent-selection handoff/repeating the plan. Preserve exact artifact plus searchable objective/stages/hash/reference drawer without task state. Native bootstrap enrolls actual issuing session and creates root/import task; claim/start import as parent work. Atomically expand independently actionable tasks, membership, provenance and real blockers. Paragraph/stage order alone is not a dependency; rationale stays in plan memory, validation becomes acceptance/evidence unless independently actionable, and ambiguous/over-budget work remains `admitted=false`. Complete import with final required publication/evidence. |
| B0a: the approved plan exceeds `max_batch` or 200 initial edges | Topologically order bounded, dependency-closed expansions. Every admitted task's known prerequisites already exist or are declared in the same expansion; later tasks may depend on earlier batches when they are first published. Use non-runnable proposals when safe runnable publication is incomplete. Do not publish a runnable task and attach an initial blocker later. |
| B1: `/fleet Update docs and tests`; workflow agent selected and task tools available | Ordinary native work remains available; no automatic durable enrollment, claim, or supervisor demand. |
| B2: healthy task MCP and `mptask_native` available, actual session UUID known, but no separately configured coordinator actor or native supervisor | Proceed with cooperative native bootstrap, graph, claim/dispatch/start and acceptance workflow. Send no `actor` argument, demand no provisioning/daemon/timer and invent no managed host evidence. Parent remains sole dispatcher. This directly targets the observed red baseline. |
| B3: same-session compaction; summary says done but fresh task MCP state is open | Retain actual session UUID, refresh stored mode/ownership/epoch/versions and report discrepancy. No duplicate bootstrap or native todo mirror. Resume only current binding; reconcile uncertain work rather than retry on silence. |
| B4: launcher-mode frontend reports `connection_closed`; old expansion may have dispatched; replacement owner exists | Report transport blocker and require explicit reconnection. Status inspection must not start/restart a launcher. Resolve the original authority/epoch/command/payload through historical outcome lookup; replacement readiness does not upgrade that request or renew execution. |
| E1: health succeeds but deployed old schema lacks `mptask_native`, or task service is disconnected | Report missing native API/version or unavailable authority, not missing actor/supervisor. No direct SQL/CLI, alternate storage/writer, borrowed managed actor, made-up IDs or silent untracked fallback. Do not claim checkout changes update an installed package. |
| E2: native task ready, inputs available, idle capacity | Native parent claims before `task`; packet requires waiting for binding. Bind the actual returned agent ID through native start; notify that ID and require fresh binding verification before work. No nickname substitution, fictitious cwd/fence, second coordinator or direct worker task-state writes. Parent work binds actual parent UUID instead. |
| E3: result is for wrong task/agent or old epoch/attempt/generation/session binding | Reject publication under mismatched identity; preserve relevant evidence but do not close the current task or copy it under fresh tokens. Check each identity independently, not just task title or worker success. |
| E4: A occupies the last slot and needs new B | Native expand/yield atomically supplies checkpoint/reason/observations, B, provenance and `blocks(B,A)`. Confirm publication/revocation before B's claim; old A cannot publish. A becomes recovering, requiring explicit reconcile/retry and resolved prerequisites before fresh claim/start. No claimed parent waiting for children or claim that yield physically stopped effects. |
| E5: all native workers ended and ready is empty, but proposal/recovery remains | Report those blockers; no goal completion without accepted sealed closure. |
| E6: old request is terminal abandoned after an epoch change | Reassess with fresh state; use a new ID only if the operation is still authorized and needed, preserving the stable discovery intent. Do not revive old execution. |
| E7: native worker interrupted, notifications silent, or external job may still run | Keep outcome/effects unknown; inspect known agent and durable checkpoint, resolve command outcomes, and explicitly release/reconcile before replacement. No short-TTL heartbeat requirement, automatic retry, fake stop/settlement or equating complete with physical safety. If managed effects are required, keep that work managed/non-runnable. |
| E8: stale successful claim receipt or reserved attempt with no accepted start | Obtain fresh state and actual binding; no work/completion from old receipt or claim alone. Unknown dispatch/start must reconcile before another launch. |
| E9: source finishes while discovering independent final work | Publish definitions/edges and complete atomically with evidence, or confirm publication before a separate close. No success-shaped loss of discoveries. |
| E10: creation timeout under the same owner epoch | Preserve the exact scoped request; unknown/not-recorded is not terminal abandonment and does not justify a new ID. |
| E11: session is forked with copied transcript/summary | Use the fork's actual distinct UUID. Do not impersonate the old session, bootstrap duplicate goal, or treat summary tokens as authority. Explicit named resume uses current CAS and reconciliation. |
| E12: user explicitly resumes native goal in a new parent while old attempt is unresolved | Native resume CAS changes only root ownership/version. Members stay stored unchanged; inherited `in_progress` work is unauthorized with `native_reconciliation_required`. Use the paginated discovery/current-state recovery procedure in native.md (E30); no adoption, duplicate bootstrap or invented generation. Recovery holds do not prohibit unrelated eligible claims (E31). Conflict refreshes state; no physical stop claim. |
| E13: named goal is managed and no supervisor is available | Respect stored managed mode and existing actor/host/lease/barrier prerequisites; report managed blocker. Do not call native resume to convert it or weaken shared/fenced assurance. |
| E14: all admitted work accepted, no unresolved proposals, aggregate goal evidence present | Parent uses native goal_close with current goal/graph versions and acceptance evidence. Report completed only after durable sealed closure receipt/fresh observation; conflict requires reassessment. Complete end-to-end evidence covers plan/import, graph, actual bindings, checkpoints/discoveries, tasks and goal. |
| E15: user opts into a second goal; first goal is already tracked in this session | Enroll second goal only from its own explicit request; separate graph/scope/version checks. Same session UUID does not make cross-goal publication or implicit enrollment valid. |
| E16: harness UUID unavailable for a new opted-in goal | Issue one canonical session UUID and retain its enrolled binding across compaction/commands. Do not derive identity from username/nickname, copy another session, or issue a fresh UUID per call. If a resumed binding is lost, inspect and explicitly transfer rather than assert continuity. The UUID is not a secret/auth token. |
| E17: native authorization is fresh/matching/authorized but `lease_live=false` and `physical_supervision=false` | Recognize intentional cooperative fields; do not block on a managed lease or falsely claim supervision. Match actual session/task/agent/attempt/generation. |
| E18: worker evidence exists but parent has not assessed acceptance | No native complete, expand/complete or goal_close until nonempty evidence and explicit `parent_acceptance` describe the parent's review. Text fields record cooperative assessment, not externally verified physical proof. |
| E19: transferred goal has members recording the prior session | Determine current parent from root `native.session_id` or fresh `authorization.session_id`, not member history. Resume changes only the root; all member snapshots/versions remain unchanged. Later member mutations update recorded sessions as needed; stored active status does not authorize inherited work. |
| E20: worker ignores WAIT or another authenticated client knows the session UUID | Do not claim runtime suspension or per-worker access control. WAIT and parent-only publishing are cooperative workflow rules; UUID is not authentication. Report the violation/unknown effects and reconcile rather than claim physical enforcement. |
| E21: inspected goal has an unknown explicit coordination mode | Fail closed as unsupported mode/schema. Only absent native metadata denotes legacy managed; do not reinterpret an unknown mode as native or silently migrate it. |
| E22: claim succeeded but preparing attempt reports `authorized=false`; later a released attempt also reports false | Start checks current reservation/session/version/attempt/generation and actual returned agent ID, not running authorization. Reconcile checks recovering state/current tokens/session and observations. Do not deadlock either action behind `authorized=true`; require that gate only for executing/publishing running-source work. Non-running epic/goal closure and source-free admission retain their action-specific checks. |
| E23: accepted work is complete but an unwanted `admitted=false` proposal prevents goal closure | Read proposal version; native `cancel` with project/goal/task/version and reason/observations rejects it in place, without attempt tokens, admission or dispatch. Confirm cancelled state/history and refresh goal/graph before evidence-backed goal_close. Cancelled-only work remains insufficient acceptance. |
| E24: cancellation requested for preparing/running/recovering work, or old-session/stale-version tokens | Do not use open-work cancel to bypass uncertainty or CAS. Preparing/running work uses release, recovering work uses reconcile with explicit decision and current retained attempt/generation/agent. Wrong session/project/goal or stale version is rejected; no replacement identity or direct managed transition. |
| E25: nested epic has unfinished or recovering children and parent asks to cancel the whole branch | No cascade: resolve children explicitly before cancelling the open epic. Root uses aggregate goal_close, never native cancel. An open task after explicit reconcile/retry may cancel; a terminal task may not be newly cancelled. |
| E26: legitimate native task remains open but held/deferred after the gate is resolved | Native update uses project/goal/task/current version and nonempty content-only patch, with null hold/defer fields to clear them. Preserve task identity; no cancel/recreate, invented claim or admitted/mode/execution patch. Recheck readiness. Root/active/recovering/terminal updates fail; open nested epic content can update. |
| E27: new parent takes over eight active tasks with 16,384-byte descriptions | Resume publishes only the root owner/transfer snapshot, not eight member snapshots; same-request replay is idempotent. Members remain unchanged, but get authorization/eligibility expose `native_reconciliation_required` and snapshot `needs_attention=true`. No old or new parent can complete adopted work; per-member release/reconcile and fresh claim/start are required. Do not claim a guidance simulation proves the journal byte-limit regression. |
| E28: service restarts with the same large inherited graph | Distinguish startup from session transfer: startup journals bounded per-attempt interruption into recovery, rather than one unbounded graph rewrite. New parent inspects fresh epoch/state and explicitly reconciles recovering work; old-epoch results remain fenced. No physical stop or automatic retry claim. |
| E29: explicit transfers return from session UUID A to B and then A | Original A attempt remains unauthorized because its internal session generation predates the root's current generation. Read fresh root/member authorization; release/reconcile inherited work and claim/start a new attempt rather than revive it. Do not add internal `session_generation` to public payloads or invent an incremented `claim_generation`. |
| E30: attention snapshot has 103 rows mixing inherited active, recovering and reconciliation-held work; a task changes while pages are read | Exhaust `next_cursor` with the same pinned scope (omit filters or repeat exactly); default 100/maximum 500 is only a page size. Preserve pinned freshness/as_of, deduplicate candidate IDs and restart expired discovery rather than treating it as empty. Before each mutation refresh root ownership/epoch and task status/version/tokens. Release only still-inherited active attempts, then read/reconcile; reconcile already recovering work directly, keep unknown effects held, and reclassify changed rows rather than mutating page snapshots. |
| E31: inherited work remains held for unknown effects, an unrelated task is ready and another task depends on the held work | Current root parent may claim the unrelated eligible task; the dependent stays blocked. Do not force retry/cancel or clear every inherited sibling as a goal-wide gate. Record known/unknown facts and deliberate rationale for any retry/cancel. An open content hold is distinct from reconciliation hold and may not appear in attention results. Neither partial discovery nor empty readiness proves goal acceptance. |
| E32: another repository's checkout finds label C1 under an existing native goal, but C1 is blocked/claimed and the project's tag is not the repository name | Enumerate exact goal scope across all statuses and cursor pages, inspect candidate IDs with get, and require a unique scope/intent and repository match. Do not use get(label), ready-only lookup or a guessed project filter. One retained root parent dispatches repository-scoped workers and publishes lifecycle; discovery does not authorize an independent session's claim, whole-goal resume, borrowed UUID or second coordinator. |

Frontend transport tests and guidance simulations answer different questions.
SDK stdio forwarding preserves schemas, freshness and errors; cooperative native
support does not claim a heartbeat supervisor, containment or physical settlement.
EOF leaves the shared owner running. `mempalace-tasks mcp` is the registered
long-lived frontend; human `start`/`connect` output is not a stdio MCP stream.

## Handoff checklist

- Record the exact source-disposition fields chosen in the prerequisite regression.
- Verify advertised `mptask_*` names and argument shapes against the deployed
  sidecar; keep native MemPalace delegation names distinct.
- Check both skill-only and workflow-agent use, including its invocation of the
  safety skill and absence of implicit fleet spawning.
- Service-scope regression must compare config-parent modes before/after diagnostic
  reads and verify invalid inputs are not repaired. This guidance records the
  expected behavior, not an executed service regression.
- Report unrun cases and wording checks explicitly. Distinguish simulated
  coverage from live tool availability, deployment, cooperative execution and
  managed supervision.
- Record grouped versus independent samples for native fleet cases. Verify
  ordinary untracked fleet remains usable with the workflow agent selected,
  and a read-only request cannot trigger launcher startup or claim recovery.
- Verify the one-line tracked-fleet phrase consumes the current approved plan,
  preserves exact provenance in an artifact plus a searchable drawer index, and
  publishes each task atomically with its initial blockers without treating
  formatting or paragraph order as dependencies.
- Verify oversized plans use dependency-closed bounded batches within advertised
  task/edge limits and never require retroactive blockers on runnable tasks.
- Verify that all task/MemPalace storage reads and writes use the configured,
  server-qualified MCP operations.
- Record the advertised native action payloads and exact mode/session/binding
  observations used. Distinguish missing native schema from absent managed actors.
- Check session compaction, fork/new-parent transfer, wrong task/agent/generation,
  interrupted work and managed mode without treating silence as physical evidence.
