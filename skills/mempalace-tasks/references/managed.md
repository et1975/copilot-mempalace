# Managed-host safety contract

**Required reading before managed task operations.** These existing requirements
do not apply to cooperative native goals and cannot be bypassed by native actions.

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

For managed work dispatched natively, a separately supplied compatible supervisor
must provide the actual owner, epoch, attempt/generation, prepared execution
arrangement and current authorization. A prompt path is not an isolation fence.
Do not invent a native `cwd` parameter, credentials, retry tokens, or preparation/
settlement evidence. Workers revalidate authorization under this contract; native
permissions, model selection and cancellation mechanics remain unchanged.

The registered host supervisor owns renewal, durable checkpoint reporting, worker
containment, and physical settlement/recovery attestations. It renews with
`expected_lease_revision`; progress reporting uses `checkpoint_sequence` and a
changed durable `reference`. Chat activity and notes are not progress. An
unconfirmed renewal grants **no additional time**: notify the supervisor. The host
may continue within the prior confirmed authorization bound, stopping by its
earliest lease/progress/hard cutoff or earlier explicit revocation. With no
confirmed bound, stop. Transient renewal response loss alone does not require an
immediate stop while that bound remains valid; it never extends authority.

## Recovery and completion

Managed recovery/settlement evidence must reflect actual effects. These guarantees
are not conferred by cooperative native release, reconciliation or completion:

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
and nonempty `evidence`. Shared completion also needs accepted class-specific
settlement. Respect retry/backoff/quarantine and operator escalation; never reset
counters or remove holds merely to obtain runnable work.
