# Original-evidence feedback preparation

`feedback-prepare` is an opt-in, read-only handoff for explicit review. It does
**not** select a polarity, infer application or causality, capture sources,
publish an outcome, change eligibility or consent, or add score/maturity credit.
Retrieval, delivery, approval, reuse, task completion and passing tests are not
outcomes. Unsupported attribution should abstain, not become neutral.

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
polarity or reviewer identity. It does not implement adjudication. A subsequent
review consumer must re-resolve originals and current history and compare a
freshly prepared packet; the original draft file is necessary to reproduce a
non-null origin. A changed source/history requires fresh review, not a trusted
cached packet.

The Python API is:

```python
prepare_feedback(selection, *, projection, reader, as_of,
                 receipt=None, draft_origin=None) -> dict
```

Use a fresh reader/projection inside the existing `nonmutating_read` scope.
Only the existing explicit `outcome` publication path may eventually capture
sources and append a reviewed event under its ordinary gates. Preparing a
packet does not authorize that action.

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

Rollback means stopping use of `feedback-prepare`, not deleting sources or
history. Retain the generated-copy guards after UX rollback so old packets
cannot become new independent support.
