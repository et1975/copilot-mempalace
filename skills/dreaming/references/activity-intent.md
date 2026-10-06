# Offline tool-activity intent evidence

This workflow recovers **recorded intent**, not hidden reasoning or proof of
usefulness, from existing tool history. It is separate from ordinary dreaming
survey/adoption and procedural enrollment. Nothing is added to the foreground
agent, its tool calls, or its completion path.

## Boundaries

The host adapter reads an explicitly selected Copilot `events.jsonl`. The pure
core consumes a language-neutral evidence packet and groups observed calls
through explicit tool-parent/delegation relationships. A direct tool call with
no artifact is a valid activity. Scripts, queries, documents and other files
are optional associated artifacts; no extension or orchestrator skill is
required.

The adapter knows Copilot event structure; the core does not. A later host
adapter can produce the same packet without changing intent extraction.
Copilot journal `parentId` links events, not necessarily tool calls: it is not
used to group activities. Temporal adjacency, equal tool names and common file
names are not grouping evidence.

Descriptions and delegation prompts are attributed to the **agent** that
provided the tool arguments, not silently upgraded to user requests. A session's
overall user request is not copied into every call. Generic tool schemas,
command source and result text do not supply intent automatically.

## Run

Use the existing Python 3.11+ interpreter and the dreaming skill's scripts
directory. This workflow uses only the standard library; it needs no palace,
embedding model, network, package installation or running task service.
Keep output and review files in a private directory outside the checkout.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_activity_cli.py" \
  --events "$SESSION/events.jsonl" --session-id "$SESSION_ID" \
  --out "$ARTIFACTS/activities.json"

"$MPY" "$DREAM_SCRIPTS/dream_activity_cli.py" \
  --events "$SESSION/events.jsonl" --session-id "$SESSION_ID" \
  --view artifacts --out "$ARTIFACTS/artifact-review.json"
```

The JSON printed to stdout is identical to the output file. The file is created
exclusively: existing files, symlinks and input/output aliases are not overwritten.
There is no `--force`. Use a new destination for a new observation.

Exit zero includes a valid empty history or an empty artifact review. Missing,
malformed, changing or over-budget input is an error, not empty success.
Diagnostics go to stderr. Argument errors exit 2; processing errors exit nonzero.

## Evidence and intent

The adapter's packet has `schema_version: 1`, `source`, `calls` and `warnings`.
The source records its kind, explicit path/session ID, exact snapshot SHA-256,
byte count and event count. Each call retains:

- Its tool-call ID, tool name, observed agent ID and explicit parent-call ID.
- Source references with line, event ID/timestamp when present, and JSON field.
- Bounded, attributed description/delegation claims and truncation flags.
- Explicit artifact-path associations and their source references.
- Completion presence, tool success and process exit code as separate fields.

The `activity_intents` report retains source/warnings and exposes `activities`.
Each activity has a stable ID, root call ID, member call IDs, root-scoped intent,
member calls with their own intent, and `visibility: observed_calls_only`.

Intent states are:

| State | Meaning |
|---|---|
| `stated` | A unique complete declaration is available at the selected priority. |
| `ambiguous` | Conflicting declarations have equal priority. |
| `unknown` | There is no complete declaration adequate to state the intent. |
| `inferred` | An explicit review supplied an interpretation; it is not an observed declaration. |

Select among complete declarations: delegation prompts have priority over
descriptions. Exact duplicates do not create ambiguity. Truncated declarations
remain visible as evidence but do not erase a complete narrower description or
become complete intent themselves. A root's intent describes its
activity; it does not erase or rewrite the more specific intents of child calls.

An observed completion is not proof of task success. A false tool-success flag
or nonzero process exit produces execution status `failed`; absent completion
is `incomplete`; no observed success indicator is `unknown`. `succeeded` means
only the recorded tool/process status, not correctness or usefulness.

## Explicit review

If interpretation is needed, review the bounded evidence outside the primary
task. Review files are JSON lists. Every entry supplies `activity_id`,
`source_sha256` equal to the report's source snapshot hash,
`status: inferred`, nonempty `text`, nonempty `rationale`, and `evidence` copied
exactly from source references attached to that activity. Unknown activities,
foreign references, duplicate reviews and malformed entries are rejected.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_activity_cli.py" \
  --events "$SESSION/events.jsonl" --session-id "$SESSION_ID" \
  --reviews "$ARTIFACTS/reviews.json" --out "$ARTIFACTS/reviewed-activities.json"
```

The original intent remains under `observed_intent`; the interpretation and
review record remain distinct. Reference and snapshot matching establish provenance only:
it does not prove the reviewer's conclusion. Recompute against the same source
snapshot; do not treat a review of changed evidence as validated merely because
some line numbers still match. Generated reviews are not independent recurrence
support and are not automatically inserted into memory, KG or procedural advice.

## First consumer: artifact reuse review

`--view artifacts` projects associated artifacts into an
`artifact_reuse_review` report. Each candidate retains its observed associations,
activity intent and execution evidence and is marked `assessment: needs_review`.
Equal reported paths can be collected within the selected source; this does not
establish cross-session equivalence or an immutable executable version.

This is material for later review, not a promotion mechanism. It does not score
usefulness, certify safety, execute files, generalize code, copy to a library,
install skills or enroll procedural rules. Artifact-free activities are retained
in the activity view but produce no artifact candidates.

## Bounds, privacy and limitations

| Flag | Default |
|---|---|
| `--max-bytes` | 16 MiB input |
| `--max-events` | 20,000 events |
| `--max-calls` | 2,000 calls |
| `--max-text-chars` | 2,000 characters per claim |
| `--max-output-bytes` | 4 MiB serialized JSON, including newline |

All limits must be positive. Input/event/call/output overflow fails explicitly
before publishing a report. Artifact projection and serialization enforce the
output budget while constructing the result, rather than fully expanding an
oversized report first. Quote shortening is explicit; complete claims are
not manufactured from clipped text. The adapter checks file identity, size and
modification time across its bounded read and rejects observed changes. This is
a checked local snapshot, not a universal transaction or tamper-proof archive.

The reader never follows artifact paths, result spill paths or instructions in
recorded content. It does not expose raw code, shell-command bodies or execution
result bodies by default. Descriptions, delegation prompts and artifact paths
can still contain sensitive information: output is **not** a general-purpose
redactor. Keep reports private and subject to the source's access/retention rules.

The event-snapshot hash does not certify the exact bytes of a script when it
ran. Unknown versions remain unknown. Shell path references do not prove those
paths were executed; unsupported shell syntax is not simulated. An outer script
invocation does not reveal internal calls unless the host independently recorded
them. Missing completion/parent observations remain visible, not inferred from
silence. Tests use synthetic events, never copied private transcripts.
