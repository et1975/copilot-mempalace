# Opt-in procedural memory (procedural-v1)

Procedural advice is empirically reviewed guidance, not a deductive premise.
Quote/hash checks establish provenance, not whether a conclusion follows or
whether the claimed causal attribution is correct. Agents/humans must review
applicability, exceptions, counterexamples and rule-specific effects. A passing
test, successful task, repeated retrieval or repeated filing alone supplies no
feedback. No command invents a negated anti-pattern, enables ontology rules,
reconciles KG provenance or changes KG authority.

## Storage and support boundary

Events are ordinary drawers in an explicit wing's `procedural`
room. Each holds `kind=procedural_event`, `schema_version=1`, and `event`
metadata, natively or in the existing `<!--dreaming-meta: ...-->` trailer.
Event chunks are reassembled without inserted delimiters. Records are historic,
not instructions: resolve them through `guidance`/`explain` before use.

New procedural commands support **existing SQLite-exact palaces only**. Strict
read commands (`validate`, `guidance`, `task-guidance`, `use-check`, `explain`, `status`, `draft`,
`receipt-get`, `delivery-status`, and write-command preparation/
dry-run) use WAL-aware read-only connections, including while a writer remains
open. They observe committed, uncheckpointed data and prohibit application-data
and schema writes. SQLite may update SHM reader-coordination bytes; those are
not procedural state changes. Incomplete WAL/SHM pairs fail explicitly before
opening the backend. Reads never checkpoint, ignore pending WAL, or require
writer shutdown. Do not delete WAL/SHM files. A shared directory lock excludes
this package's mutations during the read without taking a write lock;
unrelated external writers are not coordinated. Write commands acquire and
release the installed MCP writer lease as well as the package's directory lock;
ownership refusals are errors, not an invitation to bypass the owning writer.

The installed Chroma backend does not honor read-only opening; commands refuse it
rather than silently migrate/initialize storage. There is no automatic backend
conversion. Legacy dreaming operations still use their ordinary backend;
their live procedural protection lookup reuses that backend and does not claim
strict nonmutation. Legacy contemplation may reconcile provenance on premise
loading and explicit ontology commands can write; it is not uniformly read-only.

No packages/models are downloaded. The verified embedding path requires the
palace's **already installed MiniLM** model, with a complete local ONNX cache.
Remote embedding configurations and other unverified model loaders are refused.
The read path calls the existing MiniLM forward pass, not its download bootstrap
(HF offline flags alone do not disable that bootstrap). Configured/stored model
identity is checked by the palace backend; procedural embedding also rejects
unknown stored identity and dimension mismatches. Unavailable/invalid embeddings
are explicit errors. A separate local
Copilot session store supplies original repository/session authority **at
acquisition**, opened read-only without creating a missing file. Published
evidence resolves from the selected palace and wing, not from that host store.

### Self-contained evidence and explicit legacy capture

Publication captures admitted source witnesses in `procedural-sources`
(`kind=procedural_source`, schema version 1, author `dream-procedure-source`).
These are generated provenance records, never additional independent support.
Validation can resolve a search hit on a capture back to the verified original
raw field; it never uses the record ID as evidence. Raw-turn records contain the **exact full
original field**, preserving whitespace, Unicode and code fences; drawer
witnesses do not copy the original drawer body. Each witness preserves the
verified repository, original session, observation timestamp and source hash.
Its key includes the exact as-written source ID, including a physical chunk ID,
but excludes the quotation. Capture actor/time are audit data, not new sessions.

Every append path, including direct `append_event` without a callback, performs
mandatory admission under the package mutation lock. It first recognizes an
already committed event, then checks policy and original/captured sources,
preflights every missing record's size, captures missing sources, verifies fresh
palace-only readback, and finally appends/verifies the event. Source capture may
leave protected orphan records if publication fails; retry the **same immutable
event artifact**. Already captured sources do not need their old host input.
An already committed retry succeeds before fresh acquisition, freshness or
head checks. No event/reference IDs, hashes, quotes, timestamps or scoring
anchors are rewritten.

Once captured, raw evidence survives removal of the session DB and the entire
host session directory. Drawer evidence still requires its original palace
drawer: deletion, changed text/stamps, generated reclassification or conflicting
provenance remains a failure. Captured record corruption or conflicting
same-key witnesses fails closed. Hashes/digests are integrity checks, not
authentication against an attacker able to rewrite all palace records.

Old retained events are **not** silently repaired on `guidance`, `explain` or
`validate`. While their original session store is still available, run:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" capture-sources --palace "$PALACE" --wing project \
  --session-store "$STORE" --dry-run
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" capture-sources --palace "$PALACE" --wing project \
  --session-store "$STORE"
```

`--session-store` is mandatory for this explicit migration. All retained
references are checked, including retired/replaced rules and adverse history.
Missing/conflicting originals or legacy references without `session_id` return
`status=blocked`, nonzero exit and diagnostic counts; they are never inferred
from caller metadata, generated mirrors or a new session. Preflight failures
write nothing. Storage failures after some captures can leave durable witnesses;
rerun the command to resume. Events are never rewritten.

Migration JSON includes `event_count`, `reference_count`, `source_count`, `origin_count`,
`already_captured`, `pending`, `captured`, `failed`, `failures`, `record_count`,
`encoded_bytes`, `pending_encoded_bytes`, `capacity` and `warnings`.
Statuses are `dry_run`, `complete`, or `blocked`. Capacity reports projected
record/byte totals, the 4-MiB per-record bound, and remaining headroom before
warnings (80% of 5,000 records / 64 MiB); aggregate thresholds are warnings,
not publication caps. Warnings also appear on stderr. `pending_encoded_bytes`
is the preflight's trailer-inclusive estimate, even after a successful write.

`explain.source_diagnostics` and migration failures include a `code`:
`uncaptured`, `missing_session`, `original_drawer_missing`,
`original_drawer_drift`, `corrupt_capture`, `session_store_unavailable`,
`original_session_missing`, `original_turn_missing`, `invalid_original`,
`invalid_evidence`, or `source_size`, as applicable. Ambiguous preparation uses
`ambiguous_source` (invalid request, exit 2). Storage-envelope errors
can stop the whole command rather than produce a partial report. Uncaptured
legacy evidence is not equivalent to an original drawer that has drifted.

The read-only Python integration point
`dream_procedural_validate.inspect_published_sources(palace, wing)` acquires a
shared read scope, checks all retained capture bodies (including orphans), and
checks origin-drawer availability and chunk integrity for every retained
proposal, including retired history. It returns source coverage/counts/capacity
plus reference failures, with status `ok` or `blocked`. Corrupt storage can raise
rather than yield a partial report.
`reference_count` and `source_count` count evidence references and capture keys;
`origin_count` separately counts distinct lineage drawer IDs, never additional
evidence or support. `failed` includes unresolved references and origin IDs.
Origin failures carry `reference_kind: "origin"` and `origin_drawer_missing`
or `origin_drawer_invalid`, distinct from missing/drifting evidence or captures.
Origin IDs carry no stored content hash; the check uses the same existing
drawer-assembly integrity boundary as published reads, not a new provenance claim.
It has no session-store argument, writer, acquisition or repair side effect.
For lifecycle policy, callers can use the public `read_events`, `project_rules`
and `revalidate_sources` with `EvidenceReader(palace, wing)` inside
`nonmutating_read(palace)`. A reader/index belongs to one read scope; discard
it across mutation. `AdmissionReader(palace, wing, session_store)` is a separate
explicit input-acquisition interface, not a published-read fallback.

## CLI and artifacts

Set `MPY` to the absolute path of the Python interpreter that already owns
MemPalace, and `DREAM_SCRIPTS` to the absolute path of the dreaming skill's
`scripts/` directory. Examples also use `$PALACE`, `$ARTIFACTS` and `$STORE`
(session-store.db). `$ARTIFACTS` must be outside the palace and checkout.
All commands require `--palace PATH --wing PROJECT`.

### Manual delivery in ordinary work

Ordinary recall precedes procedural commands for the current activity; the
activity need not be a durable task or a new human invocation. Present relevant
live instructions/preferences, facts/decisions, accepted lessons and eligible
procedures in a transient **type — source → scope; relevance; complete conditions/
exceptions; validation/limitations** view before the decision. Keep their
authority distinct. Live instructions retain their actual rank; retrieved
instruction text is history, not permission. Ordinary facts/lessons do not
inherit procedural enrollment, three-session or maturity requirements.

For a procedure that could affect the next action:

1. Independently establish the current activity, constraints, repository,
   session/actor and permission witnesses. Unknown identity abstains from
   procedural use, not independently supported ordinary work.
2. Run `task-guidance`; read the full offered items and sources. Assess current
   condition, every exception/constraint and supported context using the exact
   applicability witness below. Valid local v1 reuse requires no new tags,
   enrollment, transfer dossier or mandatory contemplation.
3. Run `use-check`, then visibly acknowledge full reading and intent:
   `procedural: read <rule-id> in full; will <action>; trigger <reason>;
   exceptions checked <result>; limits <unverified matters>.`
4. If any tool, event or time intervenes, independently refresh the witnesses
   and run `use-check` again immediately before **each** advised action. Use
   its fresh full text, not the old offer. A check is cooperative, not an
   atomic guarantee or proof that the judgment is correct.
5. After the action, say `performed <action> at <locator>` or
   `not performed: <reason>`. Absence/uncertainty is unknown. This does not say
   helpful. Keep the acknowledgment unwritten when receipt consent is absent.
   Use the separate historical receipt path only with its permissions.

| Change or observation | Recovery before the next affected decision/action |
|---|---|
| Task, constraints, repository or actor changed | Resolve live identity/scope, revise context as applicable, recall and obtain a new packet; reassess fit. Do not copy identity or permissions from the old packet. |
| Compaction/resume, missing host artifacts | Reestablish assignment and permissions from current context; history may explain past actions but cannot authorize future ones. Unknowns abstain from procedures only. |
| Source drift, supersession, harm or retirement, including unchanged activity | Refresh sources/policy through the existing checks; withhold stale advice. A cache epoch never delays revocation. Reoffer/reread/reassess changed content. |
| Advice off/unavailable, no eligible advice, or receipt-only refusal | Keep valid ordinary context usable. Distinguish disabled, nonzero unavailable/error and healthy empty. Receipt refusal permits visible unwritten acknowledgment and otherwise eligible advice. |
| Unknown receipt write outcome | Preserve and retry the exact immutable artifact/ID/digest. A new occurrence is not a retry; committed acknowledgment is not new permission. |

Offered pointer ≠ full reading ≠ intention ≠ performed application ≠ helpfulness.
The optional hook emits only the first; Stop may prepare a review-only draft,
never automatic attribution. Manual delivery does not require authenticated
host certification. Automatic registration/enablement and stronger pinned-cache
capabilities remain separate, unproven deployment decisions.

### Separately consented historical receipts

`receipt` stores optional **agent-reported history**, never an outcome, original
observation, scope-transfer assessment, permission grant or eligibility token.
Receipt publication cannot increase support, score, maturity or KG authority.
The actual target repository/rule and task context remain those of the original
packet; foreign source helpfulness and consent do not transfer.
The legacy schema-1 adapter receipt used by `draft` is unchanged and is not this
new persisted envelope.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" receipt --palace "$PALACE" --wing project \
  --input "$ARTIFACTS/unsigned-receipt.json" --permissions "$ARTIFACTS/permissions.json" \
  --prepare --out "$ARTIFACTS/receipt.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" receipt --palace "$PALACE" --wing project \
  --input "$ARTIFACTS/receipt.json" --permissions "$ARTIFACTS/permissions.json" --dry-run
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" receipt --palace "$PALACE" --wing project \
  --input "$ARTIFACTS/receipt.json" --permissions "$ARTIFACTS/permissions.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" receipt-get --palace "$PALACE" --wing project \
  --receipt-id "delivery:$REQUEST_UUID"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" delivery-status --palace "$PALACE" --wing project \
  --context "$ARTIFACTS/context.json"
```

Preparation fills **only a missing digest**; identity, time, acknowledgment,
action and permission-reference facts must already be supplied. It never grants
publication. Prepare/dry-run/history do not construct a writer. Outputs use
exclusive creation outside the palace. `context.json` contains `TaskContext`
itself, not the current-use `{context, checked_at}` wrapper.

The exact receipt fields are `schema_version:1`, `kind:"procedural_receipt"`,
`receipt_id`, `record_type`, UTC `recorded_at`, `authority:"agent_reported"`,
`context_digest`, `receipt_permission_ref`, `payload`, and `digest`.
The digest is SHA-256 of canonical UTF-8 JSON excluding `digest` itself.
Unknown fields (including outcome, score, scope or independent-evidence claims)
are rejected, not ignored.

* Delivery: ID `delivery:<packet request UUID>`, `record_type:"delivery"`,
  payload `{packet, acknowledgment:"read_full_packet"}`. Report only after
  reading the complete packet. The packet and full context are stored **once**.
  The receipt time cannot precede packet generation.
* Application: ID `application:<action UUID>`, `record_type:"application"`,
  payload `{delivery_receipt_id, rule_id, disposition, action, action_reference}`.
  `disposition` is `applied` (already performed, not intended) or `not_applied`.
  `action` is nonblank, at most 1,600 characters; for `not_applied` it is the
  explicit reason. `action_reference` is null or a nonblank locator of at most
  512 characters. Resolve context from the retained delivery parent, verify
  exact context digest and rule membership, and report no earlier than the
  parent. An empty packet cannot have applications. Missing reports mean
  **unknown**, not nonapplication or success.

Every **new** prepare/dry-run/append requires fresh separate advice and receipt
permissions for the packet's exact repository/wing/session/actor; both must be
`allow`, with distinct user-message references. The receipt's
`receipt_permission_ref` must equal current `receipts_ref`. The witness expires
after 60 seconds and future times fail. Publication rereads the permission file
under the sanctioned MCP writer lease **and** palace mutation lock, after a
second race lookup. Receipt-only opt-out leaves ordinary advice possible;
advice opt-out also stops fresh receipt publication. These are cooperative
checks, not host-authenticated consent or an atomic guarantee against an
external revocation after the last check.

An initial **read-only exact-ID/digest lookup precedes writer construction and
permission access**. Identical committed retries return `already_exists` even
after opt-out, without `--permissions`, or when the installed writer is disabled.
A changed envelope at that ID is a conflict. New records return `appended` only
after exact readback. Ambiguous write/readback failures return
`receipt_outcome_unknown` (nonzero): preserve and retry the **identical artifact**,
never manufacture a replacement occurrence ID. Missing permission is allowed
only for that verified already-committed acknowledgment.

Historical reporting deliberately does **not** call `use-check`, re-rank rules,
or require present-day rule eligibility: a truthful retrospective report can
mention a now-retired rule or an old packet. Fresh persistence permission is
still required. History remains readable after host artifacts are lost.
`delivery-status` reports exact matching and prior-revision delivery IDs,
application reports and `(delivery_receipt_id, rule_id)` pairs with unknown
application history. Its notice is “Agent-reported history; not current
eligibility or helpfulness.” Empty history is neither disabled advice nor
healthy empty guidance. Existing procedural health/status projections do not
read receipt bodies.

Receipts live in `procedural-receipts`, with `kind=procedural_receipt`,
`generated_summary=true`, and author `dream-procedure-receipt`. One shared
encoder enforces a 32-KiB packet, 4-KiB wrapper, 2-KiB metadata/header budget and
64-KiB complete UTF-8 receipt budget including serialized newlines, native
metadata **and** fallback trailer. Escaping counts; no truncation is allowed.
Strict paged exact chunk reads cap each wing at 5,000 logical storage records,
including identical physical copies. Corruption, incomplete chunks, missing
delivery parents, disagreement and overflow are errors, never empty history.

Native/trailer metadata and identifiable full-text copies are generated evidence.
Direct original admission, captured revalidation and draft echo checks reject
whole receipt fields even when a narrow quote omits the marker, including
raw/fenced/prose/JSON-string wrappers. Genuine later independent originals remain
subject to the existing source gates. Deliberately removed markers or paraphrases
are not reliably detectable.

Reserved receipt drawers and all their physical chunks remain protected from
package deletion even if their bodies are corrupt. Existing rule/source-parent
retention remains unchanged after retirement; a corrupt receipt does not alter
rule projection. Whole-palace physical restore validates receipts and parent
closure before publication. Logical wing import and `dream_restore` refuse
identity-reminting replay, including wrapped trailers and partial selections.
Keep these guards when disabling receipt production or rolling back delivery.

### Task-bound delivery and cooperative use checks

`guidance` retains its existing interface and v1 meaning. The additive
`task-guidance` command offers complete eligible advice in a bounded packet;
it is not acknowledgment, intention, application, helpfulness, enrollment or
an authorization token. Ordinary recall and actual repository opt-in precede
these commands. They never write palace records or construct a writer.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" task-guidance \
  --palace "$PALACE" --wing project --current "$ARTIFACTS/current.json" \
  --permissions "$ARTIFACTS/permissions.json" --request-id "$REQUEST_UUID" \
  --out "$ARTIFACTS/delivery.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" use-check \
  --palace "$PALACE" --wing project --packet "$ARTIFACTS/delivery.json" \
  --current "$ARTIFACTS/current.json" --permissions "$ARTIFACTS/permissions.json" \
  --rule-id 'proc:SHA256' --applicability "$ARTIFACTS/applicability.json"
```

Both commands require a freshly, independently authored current observation:
`{"context": TaskContext, "checked_at": UTC}`. Do not copy it from the packet,
retained history or a previous session. Resolve current assignment, constraints,
actual session and actor from the live conversation. Unknown identity abstains;
never invent a worker identity. `TaskContext` has **exactly**:

| Field | Contract |
|---|---|
| `repository`, `wing` | Canonical exact scope; each at most 256 UTF-8 bytes |
| `session_id`, `actor_id`, `task_id` | Canonical UUIDs; `task_id` is an activity occurrence, not a durable task claim |
| `revision` | Nonnegative integer, never boolean |
| `task` | Nonblank, at most 16,384 UTF-8 bytes |
| `constraints` | Explicit list of at most eight strings, at most 512 characters each |
| `mode` | `established` or deliberate `approved_candidate_trial` |

The canonical encoded context is at most 20 KiB. Changed requirements,
constraints, actor, occurrence, repository or wing require a new context and
packet; update the revision for changed requirements. Identical task wording
does not identify the same occurrence. Constraint ordering is part of the
exact context identity: reordering invalidates the packet, even if the same
strings remain. Inputs are never silently sorted.

The independent permission witness has exactly `repository`, `wing`,
`session_id`, `actor_id`, `checked_at`, `advice`, `receipts`, `trials`,
`advice_ref`, `receipts_ref`. Decisions are `allow`, `deny` or `unknown`.
The first four fields must match the context. References are actual
user-message locators, at most 512 characters, nullable unless the corresponding
permission is `allow`. Advice-only permission uses `receipts=deny`.
Candidate delivery additionally requires `trials=allow`; source-repository
consent or approval is not target permission. The separate `receipt` command
requires both permissions and separate references; `task-guidance` and
`use-check` never publish receipts implicitly.

All witnesses require explicit UTC timestamps, neither future nor more than
60 seconds old, rechecked after source/model work. The CLI also rereads current,
permission and applicability files before returning usable advice or emitting
a packet; changed input refuses the operation. This catches old/changed
snapshots, not fabrication or dishonest relabeling of stale input.

The exact packet fields are `schema_version:1`,
`kind:"procedural_delivery_packet"`, `status:"bound_guidance"`, `request_id`,
`context`, `context_digest`, `guidance`, `guidance_digest`. Digests are SHA-256
of canonical JSON (sorted keys, compact separators, Unicode retained, no
nonfinite numbers, no trailing newline). Complete packet serialization,
including its newline, is at most 32 KiB. Guidance retains the existing combined
five-item, 6,000-character limit and adds the 8,192-byte transport bound.
Whole items can be omitted to fit; conditions and exceptions are never
truncated. Unknown fields, duplicate JSON keys, nonfinite/bool scores, malformed
IDs, wrong sections, unsupported policy/mode and future times are rejected.
Output files are exclusively created outside the palace.

#### Applicability is explicit reasoning, not a ranking oracle

Before intended use, read the **complete** offered condition, exceptions and
sources. `use-check` requires a separate transient applicability witness to
return usable items. It does not embed new scope/maturity annotations in the
packet or enroll another repository. Missing or materially unknown fit
withholds action. Similarity only ranks candidate advice; no keyword/model
classifier determines whether an architecture, task or constraint is compatible.

The applicability witness has exactly:

```json
{
  "schema_version": 1,
  "kind": "procedural_applicability_witness",
  "authority": "agent_reported",
  "context_digest": "<canonical TaskContext SHA-256>",
  "checked_at": "<fresh UTC>",
  "assessments": [{
    "rule_id": "proc:<SHA-256>",
    "rule_digest": "<complete rule-content SHA-256>",
    "condition": {
      "text": "<complete applies_when>",
      "verdict": "satisfied",
      "reason": "<why this condition holds in the live task>"
    },
    "exceptions": [{
      "text": "<complete exception, in original order>",
      "verdict": "absent",
      "reason": "<why this exception does not apply>"
    }],
    "constraints": [{
      "text": "<complete current constraint, in original order>",
      "verdict": "compatible",
      "reason": "<how the advice respects this constraint>"
    }],
    "supported_context": {
      "verdict": "compatible",
      "reason": "<compare supported target conditions with current architecture/tooling>"
    }
  }]
}
```

Use empty exception/constraint arrays only when the complete rule/context
arrays are empty. Assessments cover exactly the selected IDs (repeat
`--rule-id` to select multiple). `rule_digest` hashes canonical JSON containing
only `rule_id`, `rule_type`, `statement`, `applies_when`, `exceptions`, `evidence`;
`dream_procedural_delivery.rule_content_digest` provides the implementation.
Volatile ranking/decay scores do not invalidate a still-complete assessment.
Every reason is nonblank and at most 512 characters; the entire transient
witness including newline is at most 32 KiB.
It is a transient generated transport, never an original observation or new
support. Its marker is mandatory; do not remove it when copying the artifact.

Condition verdicts are `satisfied|not_satisfied|unknown`; exception verdicts
`absent|present|unknown`; constraint and supported-context verdicts
`compatible|incompatible|unknown`. Only the first verdict of each set permits
use. This is a cooperative reasoning witness, not semantic proof: fabricated
reasons cannot be detected without an independent host/reviewer.
The digest is compared with the **fresh** item's complete content, not the
packet's old copy. Changed content rejects the stale witness. Recovery is a
new `task-guidance` offer, complete rereading, and a newly authored current
assessment; neither refreshing a timestamp nor rehashing without reassessment
establishes fit. Each command still enforces the same 60-second/future boundary.

Compatible, already enrolled local advice needs no repeat enrollment, dossier,
fixed new evidence count or transfer assessment. Same-repository architecture
drift, a present exception or materially unknown support withholds use without
erasing evidence or demoting maturity. Foreign proven advice remains lineage
for a target hypothesis: packet relabeling and attestations cannot bypass fresh
exact-repository source, eligibility and target-original review gates.
Legacy v1 records need no new annotation migration.

Every use check executes fresh `read_events → project_rules(now) →
revalidate_sources(EvidenceReader) → get_task_guidance`, even when context is
unchanged. Ranking input is canonical `{"task": ..., "constraints": [...]}`.
Selected IDs must occur in both the packet and fresh result. Return
`status=usable`, fresh full `items`, and `checked_at`; act on that text, not
cached items/scores. Use-check responses carry `kind=procedural_use_check` and
`authority=agent_reported`; successful mechanical checks do not verify semantic
applicability. Harm, retirement, conflicting reviews, staleness or healthy
nonselection returns exit 0, `status=withheld`, no usable items.
Missing/corrupt/drifted sources, unavailable models and storage failures remain
nonzero errors, never a healthy empty result. Invalid contexts/witnesses are
nonzero invalid requests.

Sequence: ordinary recall/live permission → offer → full reading → use-check →
visible intent. Recheck immediately before each advised action whenever any
tool, event or time intervened. No atomic guarantee spans a cooperative check
and later work; external writers or user changes require another check.
Compaction/resume independently refreshes identity, context and permissions.
Cached text, compact projections, old grants and future receipts cannot renew
permission or postpone revocation. Hooks remain off; this is an explicit CLI
path, not a claim of authenticated host interception.

The optional adapter resolves shared validation from the trusted configured
`procedure_script` directory, not from the hook's installation directory.
Keep `dream_procedural_guidance.py`, `dream_procedural.py` and
`dream_metadata.py` beside that script. Both delivery and retained-packet
checks load these files in an isolated invocation scope; foreign module caches
are neither reused nor replaced. Missing or untrusted files make advice
unavailable without fallback to another checkout.

#### Generated packet exclusion before admission

`dream_metadata.generated_transport_kind` and `reject_generated_transport`
recognize `procedural_delivery_packet` and `procedural_receipt` throughout the
**complete source field/body**, as well as the transient `procedural_applicability_witness`
and `procedural_use_check` transports. These two markers prevent assessment and
refreshed-advice copies from becoming a new evidence route; they introduce no
persisted packet, receipt or authority. Raw/fenced/prose-wrapped copies, mixed commentary, JSON-equivalent
escaped markers and supported JSON-string wrappers are generated evidence,
even with genuine session/repository/time stamps and a narrow quote omitting
the marker. Direct proposal/review/outcome preparation/admission, capture
construction, retained raw captures, live drawer witnesses and drafts share
this check. Old marked captures remain stored but cannot supply current
evidence; there is no host fallback, automatic splitting, deletion or rewriting.

The helper is a narrow transport-kind registry, not a new runtime authority or
semantic classifier. Four decoding layers bound inspection; exceeding them is
explicit `transport_inspection_limit`, not independent evidence.
Recognized copies report a generated-evidence diagnostic. Native/trailer
generated metadata checks remain independently required. Deliberately removed
markers, paraphrases and otherwise unrecognizable provenance cannot be
reliably classified; a negative result never certifies independence.
Genuinely independent later observations remain eligible under existing
scope/hash/time/review requirements. Receipt production uses this same registry
and source guards rather than introducing a separate classifier.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal-draft.json" \
  --prepare --out "$ARTIFACTS/proposal.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal.json" --dry-run
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" validate --palace "$PALACE" --wing project \
  --session-store "$STORE" --rule-id 'proc:SHA256' \
  --contrast-query 'When did a focused regression test mislead this fix?' \
  --out "$ARTIFACTS/packet.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" review --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/review.json"
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" outcome --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/outcome.json"
```

`--prepare --out NEW_FILE` is available on all three write commands. It fills
only missing source hashes, proposal rule ID, validation digest and envelope
digest, then performs the same preflight without constructing a writer. It
does not invent quotations, event IDs, timestamps, reviews or outcomes. Existing
digests are checked, not silently repaired. Output is exclusively created so an
immutable retry artifact cannot be accidentally overwritten. `--dry-run` uses
the same admission and source-size preflight without a writer or source capture.
For a missing hash, preparation can use a uniquely captured version. Multiple
captured versions of the exact identity require an explicit `source_hash`;
there is no latest-wins rule. Retain prepared artifacts across retries.
`guidance`, `explain` and `status` never consult `--session-store`, even if supplied.

Stdout is JSON; diagnostics go to stderr. Exit 0 is a valid result (including
valid abstention), 1 means storage/integrity/evidence unavailable, 2 means an
invalid request. A missing source/store is not a successful empty validation.

### Read-only health

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" status --palace "$PALACE" --wing project \
  --repository owner/repository
```

Schema 1 reports `repository`, UTC `as_of`, `status` (`ok`/`blocked`),
`readiness` (`no_rules`, `ready`, `requires_review`, `blocked`),
`capture_coverage`, `rule_counts`, `suppression_counts`, `evidence_errors`,
`record_count`, `encoded_bytes`, `warnings` and `capacity`.
`ready` means at least one eligible established/proven rule, **not** evidence
that any rule is useful for the current task. A healthy empty repository is
`no_rules`, not an unavailable palace disguised as empty.

Rule/maturity/suppression counts are repository-scoped. Capture integrity is
wing-wide (`coverage_scope="wing"`), including retained adverse/retired history
and orphan records. Coverage contains `event_count`, `reference_count`,
`source_count` (distinct as-written source keys), `failed`, and
`resolved_reference_count`. Reference counts include distinct quotations;
record copies do not increase original-source counts or support. Errors retain
source/event IDs and distinguish uncaptured or missing-session legacy evidence
from missing original drawers, drift and corrupt captures. Storage envelopes
that cannot be trusted return an error/nonzero exit, not partial healthy counts.
Conflicts/stale reviews remain visible in suppression counts. Capacity uses the
same per-record limit and aggregate warning headroom as explicit capture.

Health uses no embedding, host-session lookup, writer, capture, migration,
repair or stored score. Python:
`dream_procedural_palace.procedural_status(palace, wing, repository, *, as_of)`.

### Nonpublishing grounded drafts

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" draft --palace "$PALACE" --wing project \
  --repository owner/repository --input "$ARTIFACTS/receipt.json" \
  --session-store "$STORE" --out "$ARTIFACTS/new-draft.json"
```

`--session-store` is optional **explicit input** here. Drafting never consults
the default host DB. If supplied, it opens SQLite read-only and selects only
the unique exact task text in the receipt's original session/repository,
checking turn identity and original timestamp. It never chooses a nearby
successful task or a matching older task when a newer turn is present.
A missing/unpersisted task, absent response, duplicate identity
or ambiguous repeated task remains `pending_original_evidence`. If the input
store/session is absent, bounded palace capture lookup can recover exact raw
task fields; missing/incomplete capture is also pending, not invented evidence.

Receipt schema 1 has **exactly** these fields:

```json
{
  "schema_version": 1,
  "repository": "owner/repository",
  "session_id": "<original session>",
  "generation": 1,
  "task": "<exact task text>",
  "task_digest": "<SHA256 of UTF-8 task, without newline>",
  "delivered_rule_ids": ["proc:<64 lowercase hex characters>"],
  "guidance_as_of": "2026-09-23T00:00:00Z"
}
```

`guidance_as_of` can be null. Generation is a nonnegative integer, not a turn
index; IDs are distinct, at most five. Receipt JSON is capped at 24 KiB, task
UTF-8 at 16 KiB. Repository/digest/type/ID mismatches are invalid requests.
A receipt proves neither following advice nor causal attribution.

Stdout and the exclusively created file contain the same complete canonical
UTF-8 JSON plus newline, capped at 64 KiB:

```json
{
  "schema_version": 1,
  "kind": "procedural_draft",
  "status": "pending_original_evidence",
  "repository": "owner/repository",
  "session_id": "<original session>",
  "task_digest": "<task hash>",
  "delivered_rule_ids": [],
  "original_references": [],
  "lineage_references": [],
  "missing_requirements": ["original_task_turn_missing"],
  "independent_sessions": 0,
  "omitted_count": 1
}
```

This illustrates the core fields, not a publishable event template. Actual
lineage includes the receipt's session, generation, task digest and guidance
timestamp. Original references use the unchanged `session_turn` evidence
schema with full-field hashes and exact quotes of at most 800 characters.
Known generated transport echoes go to lineage with
`reason="generated_transport_echo"` instead of independent originals.
`independent_sessions` deduplicates the original session; source-record copies,
raw user/assistant fields and repeated draft creation cannot multiply support.
`omitted_count` counts unavailable/omitted lookup or field conditions, **not**
an estimate of all unseen sources.

Even `requires_review` always retains explicit missing requirements for
rule-specific causal attribution, polarity, applicability/exceptions and
original-evidence review. There is no automatic statement, applicability,
disposition, polarity, outcome, score, capture or publication. No `event_kind`
or event `digest` is present; `propose`, `review` and `outcome` reject a draft.
Output must be a new file outside the palace. Stop, retrieval, completed tasks,
passing tests, receipt repetition and reflection echoes cannot award credit.

Original SQLite fields are projected only when their complete UTF-8 encoding
fits 256 KiB; oversized fields are omitted, never prefix-hashed. Palace fallback
verifies at most 64 stored records / 8 MiB of canonical bodies and reports
`captured_lookup_limit` when incomplete. These are **post-discovery** limits:
the current collection API still requires the existing O(total source bytes)
header scan and has a 4-MiB per-record integrity bound. No extra index/database
or cache is written. Drafting is not a full health scan; run `status` separately.

Known JSON transport signatures (including fenced copies) and explicit
`[procedural-context]` markers are conservatively excluded. Unmarked paraphrases
cannot be classified mechanically; original text, provenance, applicability
and causality still require human/agent review. The receipt does not contain
a stable original turn index, so ambiguity must not be resolved by guessing.

### Exact envelope

Every prepared event has **exactly** these fields:

```json
{
  "schema_version": 1,
  "event_id": "00000000-0000-4000-8000-000000000001",
  "rule_id": "proc:<64 lowercase hex characters>",
  "event_kind": "proposal",
  "recorded_at": "2026-09-23T00:00:00Z",
  "actor_kind": "agent",
  "session_id": "<current host session ID>",
  "payload": {},
  "digest": "<64 lowercase hex characters>"
}
```

Use a fresh UUID for a new event (e.g. `uuidgen`), never for a retry. Supply
explicit original UTC timestamps; preparation does not consult the clock to
refresh them. `actor_kind` is `agent` or `human`, an attribution, not authentication.
Digest is SHA-256 over UTF-8 canonical JSON (sorted keys, no extra separators,
Unicode unescaped), excluding `digest`. Preparation handles hashing without an
author-written script. Dates use canonical UTC `Z` spelling.

Proposal `payload`:

```json
{
  "definition": {
    "rule_type": "rule",
    "statement": "Use a focused regression test before broad changes.",
    "scope": {"kind": "repository", "key": "owner/repository"},
    "category": "testing",
    "applies_when": "Fixing a reproducible defect.",
    "exceptions": ["An incident may require an immediate reversible mitigation."]
  },
  "origin_drawer_ids": ["<optional original reflection ID>"],
  "evidence": [
    {"source_kind":"drawer","source_id":"<drawer-1>","session_id":"<original-session-1>",
     "quote":"<exact original quotation>"},
    {"source_kind":"drawer","source_id":"<drawer-2>","session_id":"<original-session-2>",
     "quote":"<exact original quotation>"},
    {"source_kind":"drawer","source_id":"<drawer-3>","session_id":"<original-session-3>",
     "quote":"<exact original quotation>"}
  ]
}
```

This is a **draft** payload: `--prepare` adds `source_hash` to references.
Prepared references require it. Session-turn references instead use
`source_kind="session_turn"`, `source_id` equal to `session_id`, nonnegative
`turn_index`, and `field="user_message"` or `"assistant_response"`.
Drawer references must not carry turn fields.

Enrollment needs three distinct original source sessions in the exact
repository. Diary and raw turns from one session count once. Reflections,
lessons, marked summaries and procedural records cannot supply independent
support; reflections are permitted only as lineage in `origin_drawer_ids`.
Each drawer needs an unambiguous `SESSION_ID:` stamp plus `observed_at` metadata
or an `OBSERVED_AT: <UTC>` line; these must agree if both exist. `filed_at` is not
an observation timestamp. For a not-yet-captured source, the session must exist in the local source store;
its exact repository must match, not a substring. Drawer repository metadata,
if present, must agree with the source session. Source hashes cover full
logical text, not just the quotation or a 4,000-character mining snippet.

Definitions accept `rule_type` `rule` or `anti_pattern`; categories are
`debugging`, `testing`, `architecture`, `workflow`, `documentation`,
`integration`, `security`, `performance`. Statement/applicability are nonblank,
at most 800 characters each. Exceptions are explicit strings; an empty list
means undocumented, not nonexistent. Identity normalizes NFC/outer whitespace,
lowercases repository keys, sorts exceptions, and never lowercases normative
text. Any semantic edit creates a different ID. Anti-pattern wording must be
independently authored and reviewed.

### Validation and review

`validate` executes two logical queries: the statement and the explicit
contrast query. Each uses an ordinary-drawer channel excluding procedural
storage and, when captures exist, a separate captured-source channel, at most
ten hits per channel. Interleaving and original-identity deduplication expose
at most ten references per logical query (twenty across support/contrast).
References are deduplicated by original
kind/ID/session/turn/field/hash rather than by snapshot record or quotation. Marked
generated/unattributed text is not admitted as original evidence. The current
search is wing-local palace retrieval, including captured raw fields without
opening the host store. A capture hit is only a locator: all matching records
are verified before exposing the original raw identity once. Drawer witnesses
themselves are never substituted for original drawers. Copies can consume
capture-channel slots, never ordinary-source slots; there is no unbounded
refill/search. Search reuses verified
admitted drawer identities where available, including a
physical-ID witness when search finds another chunk of that same live original.
This does not rewrite its identity or create a logical-key witness. New search
evidence still requires original acquisition and is not captured by `validate`.
Results from the two queries are interleaved before applying the storage budget, so large supporting
results do not automatically crowd out counterevidence. Exact quotations are
limited to 256 characters, and selected references must fit a 24-KiB review
encoding estimate that includes duplicated disposition references and reserves
reasoning/envelope space beneath the 32-KiB event limit. Stderr explicitly
reports shortened quotations and omitted sources. Longer review rationale or
many additional adverse dispositions can still require a smaller packet; the
final write always enforces the actual encoded limit. It emits:

```json
{
  "validation_packet": {
    "rule_id":"proc:SHA256", "repository":"owner/repository",
    "validated_at":"2026-09-23T00:00:00Z",
    "queries":["<exact statement>","<explicit contrast query>"],
    "evidence":["<reference objects as above>"]
  },
  "validation_digest":"<SHA256 of validation_packet>",
  "notice":"No counterexample found means none within this bounded search, not none exist."
}
```

Replace illustrative strings with real reference objects. Copy
`validation_packet` and `validation_digest` (not `notice`) into review `payload`:

```json
{
  "verdict":"approve",
  "parent_review_ids":[],
  "validation_packet":{"rule_id":"proc:SHA256","repository":"owner/repository",
    "validated_at":"2026-09-23T00:00:00Z","queries":["<statement>","<contrast>"],
    "evidence":["<reference objects>"]},
  "validation_digest":"<packet digest>",
  "dispositions":[
    {"evidence_id":"<source ID>","disposition":"supports",
     "reason":"<explain applicability and exceptions>","evidence":["<exact reference object>"]}
  ],
  "acknowledged_evidence_ids":[],
  "reason":"<agent/human review rationale>",
  "replacement_rule_id":null
}
```

Every packet source requires its exact reference and a `supports`,
`contradicts`, or `not_applicable` disposition. Approval requires three distinct
in-repository supporting sessions. Labels are reviewed judgments, not facts
proved by hashing. The packet digest detects drift, not authorship or whether a
human sincerely performed the review. No counterexample found is only a bounded
search result. Approval adds **zero** helpful credit.

Verdicts: `approve`, `hold`, `retire`, `replace`. A review must list **all**
current review heads (empty only before the first review). Concurrent heads
suppress guidance until explicitly joined. Harmful outcome IDs and conflicting
evidence require acknowledgment and grounded dispositions. `invalid` and
`not_applicable` dispositions can dismiss a harmful attribution; the original
event remains visible. Counterevidence from a `hold` or any other prior verdict
also persists: a later approval cannot restore eligibility by omitting it or
relabeling it without explicit acknowledgment. `replacement_rule_id` is required only for `replace`,
must refer to an existing proposal, and cannot create a cycle. Retired/replaced
identities cannot be revived by later/concurrent approval. Review validation
stales after 90 days; silence does not refresh it.

### Explicit outcomes

Outcome `payload`:

```json
{
  "outcome":"helpful",
  "observation_id":"<stable original observation identifier>",
  "source_session_id":"<original session ID>",
  "repository":"owner/repository",
  "observed_at":"2026-09-23T00:00:00Z",
  "evidence":["<original reference objects>"],
  "attribution":"<what following THIS rule changed, not overall task success>"
}
```

Allowed polarities: `helpful`, `harmful`, `neutral`. There is no `success`,
automatic outcome classifier, retrieval counter, or feedback inference.
Observed time must exactly equal referenced original turn/drawer timestamps,
not filing/retry time. Outcomes from one session count at most once; harmful
dominates helpful. The earliest timestamp for the winning polarity anchors
decay. Later submissions cannot refresh weight. Correctness of causal wording
remains the reviewer's responsibility: code does not prove causality.

## Prune-archive replay boundary

`dream_restore.py` is a logical replay, not identity-preserving recovery. It
preflights the complete selected archive before constructing a CLI writer or
performing any writes. Native archive/row metadata is checked independently;
physical row text is never interpreted without the rest of its logical drawer.
Text checks use both the complete ordered physical concatenation and the
complete legacy newline-rendered replay, retaining the original `added_by`
context. Conflicting writer identities are an explicit preflight failure.

The shared metadata decoder peels only recognized terminal trailers. This
preserves complete fence context: a closed fenced example remains ordinary
whether chunked or not, while a sanctioned writer's genuine appended trailer
still counts after an unfinished fence. An ordinary outer wrapper cannot erase
a protected inner trailer. Peeling is bounded to 32 trailers per complete view;
exceeding the limit is an error, not truncated success. Any protected claim
blocks the complete selection, including earlier ordinary records. Reserved
source-author metadata also remains protected.

Dry-run reports the same refusal with a nonzero CLI exit, not a misleading
`WOULD restore` preview. Duplicate JSON keys cannot hide protected metadata,
even without `--strict`. Valid `--id`/`--reason` filters still limit the selection;
ordinary replay, reconstruction and per-write error reporting remain supported.
Use a coherent whole-palace physical snapshot for procedural recovery. A
historical archive or generated source witness cannot authorize new identities.

## Guidance and explanation

After ordinary evidence recall, explicitly opt into repository-scoped advice:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" guidance --palace "$PALACE" --wing project \
  --session-store "$STORE" --task 'Fix the reproducible parser regression' \
  --repository owner/repository
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" guidance --palace "$PALACE" --wing project \
  --session-store "$STORE" --task 'A bounded regression-test trial' \
  --repository owner/repository --include-candidates
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" explain --palace "$PALACE" --wing project \
  --session-store "$STORE" --rule-id 'proc:SHA256'
```

Guidance returns `policy_version`, `as_of`, `repository`, `status`,
`item_count`, `omitted_count`, `rules`, `anti_patterns`, and `trials`. Items
include immutable ID/type/statement, applicability, **all** exceptions,
maturity, effective score, cosine relevance, latest validation and up to three
compact original source references. Trial items carry
`delivery="approved_candidate_trial"`. `explain` includes all retained events,
review heads, dispositions, score terms/decay anchors, duplicate count,
suppression reasons and live source diagnostics. It is not a historical
snapshot query: missing live evidence is reported even for retired rules.

Exact repository matching and eligibility precede embedding/ranking. Results
sort by cosine descending, then effective score descending, then rule ID;
cosine must be at least 0.25. Similarity is not usefulness. No valid embedding
means an error, not an invented confidence. Default delivery excludes
candidates. `--include-candidates` admits only eligible, explicitly approved
candidates into labeled trials, never unsafe/unapproved rules.

The default and absolute ceiling are **five combined items and 6,000 Unicode
characters of actual serialized JSON including escaping, envelope and newline**,
not a token guarantee. `--max-items 1..5` and `--max-chars 512..6000` can lower
these bounds. Whole lowest-ranked items are dropped until output fits;
conditions/exceptions are never truncated. `omitted_count` counts scoped
definitions not delivered (safety, maturity, relevance and budget omissions).
`--max-bytes 8192` adds an optional UTF-8 wire bound to the same complete
serialized result, without raising the item/character ceilings or truncating
conditions. This does not bind advice to a current task or authorize its use.
`no_rules` differs from `no_eligible_rules` (also used when relevance/budget
filters withhold all items). Scoped missing/drifted sources produce exit 1
`evidence_unavailable`, never success-shaped empty guidance. Explain still
returns lineage and source diagnostics with that nonzero exit.

Read paths perform no drawer/KG/schema/reconciliation writes, persistent
score cache or automatic feedback. They load complete bounded history:
5,000 event drawers per wing, 100 rule definitions, 32 KiB encoded event/
record. Overflow, malformed encodings and incomplete history fail explicitly.
Source availability is rechecked, including evidence later dismissed or from a
retired rule; evidence is audit history, not disposable state.

### Versioned usefulness policy

For at most one eligible outcome per original source session:

```text
weight = 2 ** (-age_days / 90)
H = sum(helpful weights)
B = sum(harmful weights)
effective_score = H - 4 * B
```

`candidate` is the default. `established` requires at least three helpful
sessions, `H >= 3`, and positive effective score; `proven` requires at least ten
helpful sessions, `H >= 10`, and positive effective score. Because weights decay,
exactly three/ten older observations are slightly below their numeric boundary:
do not round them into maturity. More independent observations can cross it.
Neutral contributes zero. These are uncalibrated `procedural-v1` constants,
not probabilities or logical proof. Every score is projected at explicit UTC
time, never persisted as an active/proven badge.

Maturity does not imply eligibility. Unapproved, stale (90 days), conflicted,
retired, replaced, malformed or missing-source rules are withheld. Merely
acknowledging **valid** harmful/contradictory evidence does not authorize a
trial: harm/conflict remains suppressed until explicitly and groundingly
dismissed as invalid or outside scope. Changing applicability requires a new
definition. Decay cannot silently dismiss harm.

## Retry, locking and retention

An identical committed `(event_id,digest)` returns `already_exists` **before**
freshness, current-head and source checks, with the current projection. This
includes a committed review whose acknowledgment was lost. Conflicting payloads
under one ID fail closed. Every append checks handler success and reconstructs
the committed record; a failed readback is retried with the **same artifact**.

This package serializes appends and destructive adoption using a five-second
bounded POSIX `flock` on the real palace directory descriptor, not a lock file.
Final source/protection/head checks, writes/deletes and readback share that
lock, including multi-delete merges. Unsupported locking/timeouts fail before
mutation. Tested on the local Linux POSIX filesystem; no untested platform
support is claimed.

All retained events protect their original drawer evidence and lineage
(logical and physical chunk IDs) from package merge/prune, even after
retirement/replacement or an out-of-scope disposition. Old worklists cannot
bypass live checks. No automatic compaction or release of protection exists.
Stopping command use rolls back behavior without erasing audit records.

There are no cross-drawer transactions, authenticated event signatures, global
snapshot isolation or tamper-proof storage. External tools can delete/change
drawers without taking this lock. Revalidation catches missing references;
deletion of an unreferenced leaf event cannot always be detected without an
external ledger. Backup remains the palace's existing backup responsibility.

## Manual rollout and deterministic evaluation

1. **Disabled by convention:** no procedural command use means no procedural
   writes or advice. Keep normal evidence recall and ordinary reflect unchanged.
2. **Throwaway smoke test:** in a repository checkout, use a preprovisioned
   Python 3.11+ `TEST_PY` with pytest 8.4.2 and the existing MemPalace/model
   prerequisites. Run `tests/dreaming/test_dream_procedure.py` and
   `tests/dreaming/test_procedural_replay.py` from the repository root; tests are
   repository-only, not shipped with the installed skill. `SESSION_FILES` must
   be an existing external directory, and the pytest basetemp must be a
   disposable child, never that directory itself:

   ```bash
   export PYTHONDONTWRITEBYTECODE=1
   DREAMING_TEST_TMPDIR="$SESSION_FILES" TMPDIR="$SESSION_FILES" \
     "$TEST_PY" -m pytest --basetemp "$SESSION_FILES/pytest-procedural" \
     tests/dreaming/test_dream_procedure.py \
     tests/dreaming/test_procedural_replay.py -q
   ```

   Root pytest configuration supplies the source/test import paths. The former
   module exercises all six commands with actual installed handlers,
   SQLite-exact storage and an isolated local session store.
   A subprocess test exercises write commands through the actual MCP opener;
   live-WAL tests retain an open writer and read its committed data without
   checkpointing. No test targets the user's live palace, installs dependencies
   or downloads models. See `tests/README.md` in the checkout for the full
   prerequisite matrix; a partial environment is not full-suite validation.
3. **Separate opt-in:** after user approval enroll a small set for one exact
   repository. Keep original sources available. Do not bulk reinterpret old
   diary prose as instructions, fabricate historical observation stamps, or
   infer support from generated reflections.
4. **Safe cold start:** approve only after inspecting both support and contrast
   evidence; deliberately select bounded candidate trials. Record what following
   the rule changed, including neutral/harmful results. Never manufacture
   supportive trials by repeating a source session or refreshing timestamps.
5. **Inspect before expanding:** check `explain`, abstention, attribution
   quality, counterexamples and source retention before adding more rules.
   Stop command use to roll back behavior; don't remove audit history.

The chronological useful-advice fixture has six daily tasks. Retrieval sees
only strictly earlier events; each task's outcome is appended afterward.
Default useful guidance is delivered on **2/6** tasks, with **4/6** abstentions;
an unscored evidence-only baseline retrieves the source on **6/6** tasks.
Six duplicate retry copies add zero credit. Other scenarios cover misleading
correlation, harm, repository drift, stale reviews, mirrored generated text,
concurrent reviewers and reviewed attribution corrections. Assertions require
zero unsafe-rule deliveries, feedback inflation or serialized-budget violations.
Storage integration separately compares application state across strict
guidance/explain and verifies writer/schema helpers are not called. Clean-store
tests also compare file bytes; live-WAL tests allow SQLite's SHM coordination
while rejecting SQL/schema mutations.

These are synthetic correctness/coverage diagnostics, not a production
accuracy target, a causal experiment, CASS parity, or measured superiority.
Agent instruction wording has not been statistically pressure-tested by this
fixture suite; the executable authority boundaries, not model compliance
claims, are its oracle.
