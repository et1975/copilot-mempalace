# Original-evidence feedback and explicit adjudication

`feedback-prepare` is an opt-in, read-only handoff for explicit review. It does
**not** select a polarity, infer application or causality, capture sources,
publish an outcome, change eligibility or consent, or add score/maturity credit.
Retrieval, delivery, approval, reuse, task completion and passing tests are not
outcomes. `feedback-adjudicate` adds deliberate review, producing an ordinary
outcome **artifact** or an abstention; it still does not publish. Unsupported
attribution should abstain, not become neutral.

Use the interpreter that already owns the installed MemPalace runtime (`MPY`)
and this checkout's or installed dreaming script directory (`DREAM_SCRIPTS`):

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" feedback-prepare \
  --palace "$PALACE" --wing "$WING" --repository owner/repository \
  --input selection.json --out feedback.json
```

The repository is required and must exactly equal the selection's canonical
lowercase `owner/repository`; no repository is inferred from cwd, wing, filenames,
receipts or missing source metadata. The command uses its own current UTC clock;
there is no caller-supplied time flag.

Without `--session-store`, the command uses only published palace-native
`EvidenceReader` sources. There is no host-default fallback. Explicit
`--session-store /absolute/original-sessions.db` selects `AdmissionReader`, which
can acquire a missing capture from that supplied original store, read-only.
It does not persist the acquired source. Captured full raw fields survive host
loss; a captured drawer witness still requires its intact original drawer.
Missing, corrupt, changed, unstamped or ambiguous evidence is an explicit
nonzero error, never an empty successful packet.

## Explicit selection

Supply exactly these fields (replace illustrative IDs/hashes with verified
original values; placeholders are not admissible):

```json
{
  "schema_version": 1,
  "repository": "owner/repository",
  "rule_id": "proc:<64 lowercase hexadecimal characters>",
  "evidence": [
    {
      "source_kind": "session_turn",
      "source_id": "<original session ID>",
      "session_id": "<same original session ID>",
      "turn_index": 2,
      "field": "user_message",
      "source_hash": "<SHA-256 of the complete original field's UTF-8 bytes>",
      "quote": "An exact original observation relevant to the selected rule."
    }
  ]
}
```

The other existing source kind is `drawer`: use its exact source ID and original
session ID, omit `turn_index` and `field`, and hash the complete original drawer.
Drawer observations need an unambiguous `SESSION_ID` plus grounded original
`OBSERVED_AT`/existing observed-time metadata. Filing time, receipt reporting
time and a guessed timestamp cannot repair a missing original time.

- Actual input bytes, including whitespace, must be at most **24 KiB** of strict
  UTF-8 JSON, checked before parsing. Duplicate keys, nonfinite numbers,
  malformed identity/hash, unknown fields and unsupported versions fail.
- Session-turn `source_id` and `session_id` must be exactly equal as written
  before any original lookup; normalization cannot join one session's repository
  to another session's text or timestamp.
- Submit **1–8 references**, counted before duplicate checks. Each quote is
  nonblank and at most **800 Unicode code points**, not bytes. Quotes are never
  normalized, trimmed or truncated. Each is checked against its full hashed
  original field/body.
- Exact duplicate references fail. Distinct quotes or user/assistant fields
  remain distinct references and are canonically sorted, not extra independent
  sessions.
- All references must resolve to **one target repository, original session and
  observation timestamp**. Mixed observations fail `split_observations`; future
  originals fail. Split them into separate reviews rather than changing stamps.
- A selected rule must have a target-scoped definition. Held, stale and retired
  definitions remain reportable, including adverse observations; preparation is
  not a guidance eligibility check.

Choose actual later turns or saved originals explicitly when a current-task
draft is insufficient. Preserve relevant observed task, tooling, constraints and
exceptions through the verbatim quotations and resolvable full originals; an
unobserved environmental fact remains unknown. Foreign and repositoryless
observations cannot be relabeled as target support. A source-repository rule or
generated report may inform a transfer hypothesis, never provide target-original
credit. Valid legacy v1 source/observation semantics are unchanged.

There is no new tool-result source kind. A command result not supported by an
existing original-source contract cannot be manufactured into a turn, nor can an
assistant's description of it become independent command verification.

## Optional legacy draft handoff

The current legacy schema-1 `procedural_draft` producer emits `original_references`,
`lineage_references`, `delivered_rule_ids`, review status and missing requirements.
Keep the **complete original draft file**, bounded to 64 KiB, alongside the
review. Do not pass a selection in place of a draft or rewrite draft provenance.

The module's pure helper makes the manual mapping explicit:

```python
selection = selection_from_draft(draft, selected_delivered_rule_id, [0])
```

Indexes address only `original_references`, in their original order; they must
be distinct integers, not booleans. The helper requires a delivered rule,
`status="requires_review"`, `omitted_count=0` and all four unresolved obligations:

1. `rule_specific_causal_attribution`
2. `explicit_polarity`
3. `applicability_and_exceptions_review`
4. `original_evidence_review`

Then pass `--draft-origin original-draft.json` with that selection. The command
strictly validates the actual legacy draft schema and exact selected-reference
mapping, independently re-resolves originals, and retains a canonical digest of
the **complete** draft plus its status, all obligations and all lineage. Those
are lineage/non-authority, not fulfilled review.

A pending draft creates no automatic selection. Resolve missing originals and
make a fresh draft, or use the separate explicit-reference route without
`--draft-origin`. Later corrections/drawers absent from that draft belong to the
explicit route. Never substitute a nearby successful turn or copy a lineage
reference into evidence. Generated echo lineage remains generated.

## Optional legacy receipt, not native acknowledgment receipts

Receipt-free preparation is first-class. `--receipt legacy-receipt.json` accepts
only the existing **24-KiB legacy schema-1 transport** with exactly
`schema_version`, `repository`, `session_id`, `generation`, `task`, `task_digest`,
`delivered_rule_ids`, and `guidance_as_of`. The repository, original session and
selected delivered rule must agree. The full receipt is retained as unverified
delivery lineage: it proves neither reading, application, permission nor effect.
Its task/context cannot supply missing original environmental facts.

This is **not** the G1 native `procedural_receipt` contract, and those receipts are
rejected here. For future interoperability only, the final G1 contract has one
embedded context plus constraints, parent/context digests, and separate current
permissions. Its delivery packet is bounded to **32 KiB** and its complete
encoded receipt to **64 KiB**. These shape corrections do not add a native receipt
reader, imply interoperability approval, or introduce a G1 dependency. Reporting
time never becomes an original observation/decay anchor, and nonapplication or
uncertainty never becomes neutral credit.

## Packet contract for subsequent explicit review

The return value, JSON stdout and exclusive output file contain exactly:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `kind` | `procedural_feedback` |
| `status` | `requires_adjudication` |
| `repository`, `rule_id` | Exact selected target scope and rule |
| `definition` | Complete current definition, including conditions and exceptions |
| `history_digest` | SHA-256 of canonical JSON sorted `(event_id, digest)` pairs for this rule |
| `source_session_id`, `observed_at` | Independently resolved original observation identity/time |
| `evidence` | Canonically sorted, complete selected references with verbatim quotes |
| `delivery_receipt` | Full validated legacy transport or `null`; lineage only |
| `draft_origin` | `null`, or exactly `{digest,status,missing_requirements,lineage_references}` |
| `packet_id` | SHA-256 of canonical JSON of all other fields |

The **entire encoded packet, including its terminating newline, is at most
64 KiB**. Over-budget content fails; neither definitions nor quotations are
silently reduced. This command emits no event ID, observation label, outcome,
polarity or reviewer identity. The separate `feedback-adjudicate` command
re-resolves originals and current history and compares the **complete canonical
packet**; the original draft file is necessary to reproduce a non-null origin.
A changed source, definition, history, packet or draft requires fresh review,
not a trusted cached packet or a replacement hash.

The Python API is:

```python
prepare_feedback(selection, *, projection, reader, as_of,
                 receipt=None, draft_origin=None) -> dict
```

Use a fresh reader/projection inside the existing `nonmutating_read` scope.
Only the existing explicit `outcome` publication path may eventually capture
sources and append a reviewed event under its ordinary gates. Preparing a
packet does not authorize that action.

## Explicit review: outcome artifact or abstention

Read the complete definition, applicability, exceptions, current history and
resolved original fields before choosing. The review must distinguish actual
target behavior and effect from retrieval, task success and transferability.
Keep the immutable packet, original draft if used, and decision together:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" feedback-adjudicate \
  --palace "$PALACE" --wing "$WING" --repository owner/repository \
  --input feedback.json --decision decision.json --out reviewed.json
```

Supply `--draft-origin original-draft.json` again **iff** the packet has a
non-null origin. The full original file's canonical digest, reference mapping,
status, lineage and all four review obligations must still match. Reordered or
edited draft provenance is not silently rehashed. The legacy delivery receipt
is retained inside the packet and revalidated as lineage, not reread as a new
receipt, upgraded to a native G1 receipt, or used as evidence.

The input must be at most **64 KiB** and the decision at most **24 KiB** of actual
UTF-8 bytes before parsing, including whitespace. Both use strict JSON: duplicate
keys, nonfinite numbers, incorrect types, missing/extra fields and noncanonical
scope fail. The required CLI repository must exactly match the input. There is
no `--as-of`, implicit decision, polarity inference, or publish flag.

Every decision has exactly these common fields:

| Field | Contract |
|---|---|
| `schema_version` | Integer `1`, not boolean |
| `packet_id` | Exact current packet digest |
| `decision` | Explicit `publish` or `abstain` |
| `actor_kind` | Exactly `human` or `agent` |
| `session_id` | Canonical nonblank reviewer session text under the existing event contract |
| `recorded_at` | Canonical explicit UTC time; `observed_at <= recorded_at <= command clock` |

The historical event contract accepts session labels; it does **not** impose
G1 task-context UUID rules. An actor label does not authenticate a human.

For `abstain`, add only a nonblank `reason`. Output has exactly
`schema_version:1`, `kind:"procedural_feedback_abstention"`, `status:"abstained"`
and the unchanged `decision`. It contains no event, observation label, polarity
or neutral credit, and `outcome` rejects it. Both choices require fresh originals
and packet equality; stale/missing evidence is an error, not a fabricated
successful abstention.

For `publish`, add exactly:

```json
{
  "event_id": "<once-issued canonical UUID>",
  "observation_id": "<stable original observation label>",
  "outcome": "helpful",
  "rationale": {
    "applicability": "<actual target task/tooling/constraints, fit and material drift>",
    "exceptions": "<which exceptions were checked and whether any applied>",
    "behavior": "<what following this specific rule actually changed>",
    "effect": "<attributable target effect grounded in the original, not overall success>",
    "alternatives": "<credible alternatives and applicable local/foreign counterexamples>"
  }
}
```

These are additions to the common fields, not a standalone decision. All five
rationale entries must be nonblank strings, with no extra schema fields.
`outcome` is exactly `helpful`, `harmful` or `neutral`; `task_success` is invalid.
Neutral still requires an attributable observation, not unknown application.
The event UUID and observation label come from explicit review, never the draft,
receipt, task status or command clock.

Compatible-local conditions need no invented transfer ceremony, but repository
equality alone proves no fit. Assess changed tooling/constraints and relevant
foreign failures in the existing rationale. Foreign success, shared Git ancestry,
benchmark gains, commit-scoped code verdicts and compaction summaries supply no
target outcomes or inherited score/maturity. Unknown origin stays unknown.
Only validated original target evidence may enter the event. Applicable foreign
counterexamples remain review context without score pooling or erased harm.
Narrowing applicability requires a new immutable rule identity and the existing
target enrollment/review gates, not an edit to an enrolled definition.

The command constructs an ordinary v1 outcome envelope, with the original
`source_session_id`, `observed_at` and exact evidence. Its attribution contains
canonical JSON of `{packet_id,decision_digest,reviewed_rationale}`. The existing
canonical event parser and admission preflight remain mandatory, including the
**32-KiB event limit**; larger valid inputs do not permit a larger event.
Explicit-host acquisition also checks the existing capture bounds before output,
without persisting a capture. Held, stale or retired rules can report harm:
current-use eligibility is not a historical outcome gate.

The Python API is:

```python
reviewed_outcome(packet, decision, *, projection, reader, as_of,
                 draft_origin=None) -> dict
```

Use a fresh projection and reader inside `nonmutating_read`, as for preparation.
Default CLI reads remain palace-only; `--session-store` explicitly selects
read-only original acquisition. Neither branch constructs a writer, persists
captures, changes schema/config/WAL state, enables hooks, or downloads anything.
Structural validation and original provenance do **not** prove causal truth or
detect every dishonest story. Unmarked prose is not classified by keywords;
unsupported attribution requires an explicit reviewer abstention.

### Separate publication and immutable retries

After reviewing a produced outcome artifact, use only the existing publication
path. It already has its canonical digest; do not prepare a new identity:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" outcome \
  --palace "$PALACE" --wing "$WING" --input reviewed.json --dry-run
"$MPY" "$DREAM_SCRIPTS/dream_procedure.py" outcome \
  --palace "$PALACE" --wing "$WING" --input reviewed.json
```

Supply an explicit original `--session-store` to these commands when acquiring
not-yet-captured sources. Only publication captures sources and appends the event
under the existing locks. Dry-run adds no credit.

Preserve the once-issued UUID and **exact event file bytes** across partial
capture, unknown write/readback, lost acknowledgment and retry. Retry the same
`outcome --input reviewed.json`; identical `(event_id,digest)` returns
`already_exists`, including after host loss. A conflicting digest fails.
The observation label is not a uniqueness gate: per-source-session scoring,
harm precedence and original-observation decay anchors remain unchanged.
Repeated publication does not repeat score or refresh its timestamp.

Adjudication refuses existing output files. Repeating an unchanged review before
publication to a different new output yields identical bytes; after history
changes, the old packet fails fresh comparison. Do not re-adjudicate or issue a
new UUID to resolve an uncertain publication. An intentionally new observation
or correction requires a newly prepared packet and explicit review.

## Generated copies, safety and rollback

The shared generated-transport registry rejects `procedural_feedback` and
`procedural_feedback_abstention` before either can become original evidence.
Direct, raw, fenced, prose-wrapped, escaped and native/trailer-marked copies
remain generated. Full original fields are inspected: quoting just an
innocent-looking fragment cannot bypass a copied packet elsewhere in the field.
Existing generated exclusions remain in force.

Historical copies/captures are retained but unusable as evidence. Exact selected
source lookups validate every capture of that identity without turning an
unrelated retained generated copy into a failure of a distinct genuine later
original. Discovery first validates each bounded record's structural body,
digest and body-identity/header-key agreement before declaring any key absent;
misrouted copies are corruption, never permission for host acquisition. This
structural check is separate from generated-content eligibility, so valid
unrelated generated history remains retained without poisoning genuine evidence.
Partial/ambiguous lookups retain their existing strict checks. Do not strip
transport markers to fabricate an independent source.

The CLI does no host discovery, source capture, writer construction, schema/KG
changes, installation, download or hook/config enabling. It uses the existing
nonmutating read boundary and offline flags. Output must be a new file outside
the palace; existing files and symlink output paths (including symlink ancestors)
are refused. Read errors and failed checks do not create an output artifact.
Nothing grants consent, lowers enrollment/support gates or removes adverse
history.

Rollback means stopping use of `feedback-prepare` and `feedback-adjudicate`, not
deleting sources or history. Retain the generated-copy guards after UX rollback
so old packets and abstentions cannot become new independent support.
