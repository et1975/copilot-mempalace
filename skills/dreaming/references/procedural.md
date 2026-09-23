# Opt-in procedural memory (procedural-v1)

Procedural advice is empirically reviewed guidance, not a deductive premise.
Quote/hash checks establish provenance, not whether a conclusion follows or
whether the claimed causal attribution is correct. Agents/humans must review
applicability, exceptions, counterexamples and rule-specific effects. A passing
test, successful task, repeated retrieval or repeated filing alone supplies no
feedback. No command invents a negated anti-pattern, enables ontology rules,
reconciles KG provenance or changes KG authority.

## Storage and support boundary

All durable records are ordinary drawers in an explicit wing's `procedural`
room. Each holds `kind=procedural_event`, `schema_version=1`, and `event`
metadata, natively or in the existing `<!--dreaming-meta: ...-->` trailer.
Event chunks are reassembled without inserted delimiters. Records are historic,
not instructions: resolve them through `guidance`/`explain` before use.

New procedural commands support **existing SQLite-exact palaces only**. Strict
read commands (`validate`, `guidance`, `explain`, and write-command preparation/
dry-run) additionally require a **clean database without WAL/SHM sidecars**.
SQLite's `mode=ro` can change shared-memory bytes when WAL sidecars exist.
These commands therefore refuse that state *before opening the backend*; they
never checkpoint it, ignore its WAL, or return a partial snapshot. Close and,
where necessary, explicitly checkpoint the owning writer separately using its
normal administration workflow. Do not delete WAL/SHM files. A shared directory
lock excludes this package's mutations during the read without taking a write
lock; unrelated external writers are not coordinated.

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
Copilot session store supplies original repository/session authority. New paths
open it read-only, without creating a missing file.

## CLI and artifacts

Use the Python interpreter that already owns MemPalace, from
`skills/dreaming/scripts`. Examples use `$MPY`, `$PALACE`, `$ARTIFACTS` and
`$STORE` (session-store.db). `$ARTIFACTS` must be outside the palace.
All six commands require `--palace PATH --wing PROJECT`.

```bash
"$MPY" dream_procedure.py propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal-draft.json" \
  --prepare --out "$ARTIFACTS/proposal.json"
"$MPY" dream_procedure.py propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal.json" --dry-run
"$MPY" dream_procedure.py propose --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/proposal.json"
"$MPY" dream_procedure.py validate --palace "$PALACE" --wing project \
  --session-store "$STORE" --rule-id 'proc:SHA256' \
  --contrast-query 'When did a focused regression test mislead this fix?' \
  --out "$ARTIFACTS/packet.json"
"$MPY" dream_procedure.py review --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/review.json"
"$MPY" dream_procedure.py outcome --palace "$PALACE" --wing project \
  --session-store "$STORE" --input "$ARTIFACTS/outcome.json"
```

`--prepare --out NEW_FILE` is available on all three write commands. It fills
only missing source hashes, proposal rule ID, validation digest and envelope
digest, then performs the same preflight without constructing a writer. It
does not invent quotations, event IDs, timestamps, reviews or outcomes. Existing
digests are checked, not silently repaired. Output is exclusively created so an
immutable retry artifact cannot be accidentally overwritten. `--dry-run` uses
the same preflight without a writer. Retain prepared artifacts across retries.

Stdout is JSON; diagnostics go to stderr. Exit 0 is a valid result (including
valid abstention), 1 means storage/integrity/evidence unavailable, 2 means an
invalid request. A missing source/store is not a successful empty validation.

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
an observation timestamp. The session must exist in the local source store;
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

`validate` executes exactly two queries: the statement and the explicit
contrast query, at most ten palace hits each, deduplicated by source ID. Marked
generated/unattributed text is not admitted as original evidence. The current
search is wing-local palace retrieval; raw turns can be supplied as grounded
references but are not an additional unbounded search. It emits:

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
event remains visible. `replacement_rule_id` is required only for `replace`,
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

## Guidance and explanation

After ordinary evidence recall, explicitly opt into repository-scoped advice:

```bash
"$MPY" dream_procedure.py guidance --palace "$PALACE" --wing project \
  --session-store "$STORE" --task 'Fix the reproducible parser regression' \
  --repository owner/repository
"$MPY" dream_procedure.py guidance --palace "$PALACE" --wing project \
  --session-store "$STORE" --task 'A bounded regression-test trial' \
  --repository owner/repository --include-candidates
"$MPY" dream_procedure.py explain --palace "$PALACE" --wing project \
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
2. **Throwaway smoke test:** use the package-owning interpreter and a session
   directory for `DREAMING_TEST_TMPDIR`/`TMPDIR`. Run `test_dream_procedure` and
   `test_procedural_replay`; the former exercises all six commands with actual
   installed handlers, SQLite-exact storage and an isolated local session store.
   Only fixtures explicitly checkpoint their own writer between strict reads.
   No test targets the user's live palace or installs dependencies.
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
Storage integration separately compares palace/source-store file bytes across
strict guidance/explain and verifies writer/schema helpers are not called.

These are synthetic correctness/coverage diagnostics, not a production
accuracy target, a causal experiment, CASS parity, or measured superiority.
Agent instruction wording has not been statistically pressure-tested by this
fixture suite; the executable authority boundaries, not model compliance
claims, are its oracle.
