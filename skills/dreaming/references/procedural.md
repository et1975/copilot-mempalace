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
read commands (`validate`, `guidance`, `explain`, `status`, `draft`, and write-command preparation/
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
