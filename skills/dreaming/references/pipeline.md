# Dreaming pipeline — contract & reference

Design basis for the dreaming scripts. Filed in the palace under wing
`copilot-mempalace`, room `dreaming` / `api`; summarised here for offline use.

For command examples, set `MPY` to the absolute path of the existing MemPalace
Python interpreter and `DREAM_SCRIPTS` to the absolute path of the dreaming
skill's `scripts/` directory. Use the provisioned interpreter, not a parsed
launcher shebang. Run from the external session workspace so relative artifacts
remain outside the checkout and installed skill.

## Layered responsibilities

- **Substrate — mempalace** (passive): stores drawers + embeddings + KG; serves
  read (`get_collection`) and write (MCP tool handlers). No cognition.
- **Mechanics — Python scripts**: `dream_lib.py` (pure core), `dream_palace.py`
  (mempalace adapter), `dream_harvest.py`, `dream_adopt.py`.
- **Cognition — the dreaming skill**: the agent, in its own fresh context.

Ordinary drawers, diary entries and KG facts may cite tracked tasks as supporting
evidence, but the reserved task logstream and its sidecar reducer own task state.
Consolidation must not claim/release/complete work or infer current ownership or
readiness from those memories. Preserve source task/event IDs when retaining
evidence and use the sidecar for current state. The task projection pipeline and
its rebuild command were removed; dreaming does not recreate task projections.

## Default: session review, then relevant recall

Use the already provisioned MemPalace interpreter (`MPY`, absolute path) and
the checkout/installed `skills/dreaming/scripts/` directory (`DREAM_SCRIPTS`,
absolute path). Run examples from the external session workspace.

```bash
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p> --repository owner/repository \
  --wings <project-wing> --worklists-dir <session-files>/dream-worklists
```

Implicit dreaming requires an **exact repository and explicit memory wing**;
there is no alias/wing inference. The first run covers **all eligible history**.
Later runs cover the frozen UTC **`[lower, upper)`** interval, from the prior
successfully completed cutoff to the current **run start**. There is **no
input-count or candidate-seed cap**. Sources include new sessions, continuing
sessions with new timestamped turns, and original memories from all project
rooms in that wing, using memory filing/creation timestamps. Session coverage
contains full original user/assistant turns before upper, not a cropped body.
Generated lessons, reflections, procedural and control records are excluded as
fresh evidence. Original memory IDs do not establish independent sessions.

Survey emits `reflect.incremental.json`, including an **empty window**. It
contains full source `coverage` with `review: null`, empty proposal `items` and
`completion: null`. Empty items alone do not establish review or abstention.
Missing/incomplete sources and invalid timestamps are errors, not successful
empty input. No default maintenance, KG scan, ontology work or adoption runs.
`--instructions` steers review only. Partial source/since/count/room filters
and candidate thresholds require explicit preview tasks and cannot complete
an incremental window.

The agent reviews **every coverage record**, in batches if necessary, dedups
against existing knowledge and proposes at most **five actionable lessons
total**. This is an output budget, never a source limit. Each lesson describes
a trigger, action/avoidance, scope/exceptions, original evidence and expected
difference in the existing reflect conclusion text. Missing/weak support or
no useful novelty means abstention. A one-off factual correction uses ordinary
filing, not a weakened generalization gate.

Proposals remain session artifacts until reviewed and accepted. Add-only
adoption files accepted lessons in the relevant project wing's non-mined
`lessons` room. Lead with task/trigger vocabulary for retrieval. After ordinary
task-start recall, reuse the same scoped search to consider at most three directly
applicable lessons, checking trigger, scope, exceptions and original evidence.
No match means no advice; search failure is not empty recall. Advice is fallible
context, not instructions or proof of efficacy. This does not enroll procedural
learning/outcomes, enable ontology rules, write KG truth, or track durable tasks;
historic procedural records still require explicit opt-in and `guidance` /
`explain`. The [skill](../SKILL.md#session-lesson-review) owns the review recipe.

### Migration: explicit maintenance

The previous full survey is still available by explicit request:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p> \
  --tasks contradiction,induce-rules,pattern,reflect,merge,prune \
  --worklists-dir <session-files>/dream-maintenance
# Separate explicit diary reflection:
"$MPY" "$DREAM_SCRIPTS/dream_survey.py" --palace <p> --tasks reflect --source diary \
  --worklists-dir <session-files>/dream-diary
```

The full sweep keeps pattern diary-backed and reflect drawer-cluster-backed;
it rejects `--source diary` on the mixed maintenance list. Explicit diary
reflection is a separate command above, not the default session review. Bare
`dream_harvest.py` now means incremental reflection and requires exact
`--repository` plus `--wing`;
old implicit merge callers must add `--task merge`. Explicit task selections
keep their meaning: `--task pattern` defaults to diary, and `--task reflect`
without `--source` retains drawer-cluster reflection. Use `--source sessions`
explicitly for raw-session reflection. Explicit legacy session tasks retain
oldest-first, uncapped behavior unless bounds are supplied. Explicit tasks
are previews/legacy operations and never advance incremental completion.
The specialized contracts below are unchanged; a survey never adopts, and
legacy collection/KG initialization remains subject to the read boundary below.

## Incremental checkpoint contract

`dream-checkpoints.json` version 2 is palace-local run control keyed by exact
repository + memory wing. Each completed scope retains `cutoff`,
`session_store`, `run_id`, `review_hash` and cumulative `reviewed_versions`
source fingerprints; it is not lesson evidence.
The immutable worklist `incremental` block contains `version`, `scope`
(`repository`, `wing`, `session_store`), `lower`, `upper`, prior `base`,
`source_hash` and `run_id`.

- Keep source contents and metadata intact. Each `coverage` record needs
  `review: {"action":"reviewed","reason":"<specific rationale/abstention>"}`.
- Author `items` as `{proposal_id, source_ids, decision}` referencing coverage.
  `decision.conclusion` uses existing `text`, `kind`, `decision_or_prediction`.
  `converge` requires at least two independent raw session sources and empty
  premises; memory-grounded kinds require at least two original memories and
  exact `{drawer_id, quote}` premises. Never invent session support for memories.
- Set top-level `completion: {"action":"complete","reason":"<review summary>"}`
  only after all sources and proposals were reviewed, including no-lesson and
  empty windows. The [skill's examples](../SKILL.md#session-lesson-review) are
  edits to a harvested manifest, not replacements for its evidence.
- Existing `dream_adopt.py --palace <p> --decisions decisions.json` revalidates
  full coverage, source rereads/hashes and the prior checkpoint. After accepted
  additions and write readback succeed, it atomically advances to frozen upper.
  Harvest, proposal-only, dry-run, missing reviews, partial input, failed writes,
  source drift and stale overlapping runs do not advance.
- Retry the unchanged review after a partial write; generated receipts identify
  already-adopted proposals. For drift/stale scope, re-harvest and review from
  the current completed checkpoint rather than editing cutoff/hash metadata.
  New events at or after upper wait for the next run. Unseen or changed source
  versions with older timestamps are also included, recovering late-persisted
  turns, backfilled memories and historical edits. Unchanged reviewed versions
  stay excluded across empty windows. Source versions are checked, not merely
  filtered by event time; deleted intermediate versions cannot be reconstructed.
- Native naive memory `filed_at` values are local time; naive session timestamps
  are UTC. Explicit offsets are honored and boundaries normalized to UTC.
- Version 1 checkpoint scopes receive a full source reconciliation on their
  next harvest; only successful completion publishes version 2. Pending version
  1 worklists must be re-harvested.

## The dream as a function

`Δ : (M_in, S, θ) ↦ M_out`, with `M_in` immutable. The store `M` includes
logical drawers in a wing/room and the palace-local temporal KG.
This is a reasoning model, not a transactional snapshot guarantee. Legacy
collection opening/KG premise loading may initialize or reconcile storage;
ontology proposal commands explicitly write candidates. "No adoption" is not
equivalent to "no filesystem writes."

### Task: dedup / merge (v1)

- Merge candidate edges come from native `mempalace_find_duplicates` distances,
  converted to similarity with `sim = 1 - distance`. The native representative
  and neighbor-search semantics are not assumed identical to the historical
  local mean-embedding all-pairs implementation.
- Near-duplicate `a ~_τ b ⟺ sim ≥ τ`. Symmetric but **not transitive** →
  clusters are connected components of the returned `~_τ` graph (union-find).
  Room partitioning and logical/physical protected-ID exclusions precede
  component rebuilding, so an excluded drawer cannot bridge two survivors.
  Canonical text and physical chunk membership still come from the existing
  drawer loader; native display text is not used as an archival identity.
- Fold `μ(C)` = one synthesised drawer per cluster (the agent's job, Phase 2).
- Soundness constraint: `μ(C)` must preserve every atomic fact in `C`.
  This is an agent review obligation, not proved by cosine similarity.
- Both merge and prune archive full original records to fsynced JSONL before
  sanctioned deletion. A successful add or an archive alone does not establish
  semantic preservation. Re-harvest is a residual-work measurement; skipped
  groups and concurrent changes mean zero clusters is not guaranteed.
  Native errors, unavailable vectors and truncated responses are failures, not
  zero candidates. Limits cannot be applied before client-side scope/protection
  filtering and then interpreted as an exhaustive result.
  Standalone `dream_verify.py` and adoption's `--verify` share the harvest
  candidate definition; raw cross-room or protected-only matches do not make
  an otherwise empty actionable worklist fail convergence.

### Task: contradiction / staleness

- Detection is mechanical: harvest reads currently-active KG triples and groups
  every `(subject, predicate)` pair with **2+ distinct objects**.
- These groups are only **candidates**. Some predicates are legitimately
  multi-valued (`knows`); others are functional (`lives_in`, `status_is`) where
  multiple current objects are stale or contradictory.
- Harvest provides a recency hint (`newest_object`) by sorting candidates on
  `(valid_from || "", extracted_at || "")` descending, but it never auto-resolves.
- Adjudication is cognitive: the agent decides whether the predicate should be
  functional, which object is authoritative, and which objects to retire.
- Adoption is non-destructive belief revision: it sets `valid_to` on retired
  facts and ends their supporting provenance, cascading invalidation to
  dependent conclusions without an alternative valid proof. It does **not**
  delete rows. Reports count affected root facts, not requested IDs or cascades;
  a shortfall is an explicit adoption failure.
- A functional single-old-object resolution uses one transactional supersession
  boundary. `keep` may identify an object or candidate triple, but is resolved
  to the canonical kept candidate before writing. The kept fact already exists:
  preserve its identity, interval and support rather than creating a new
  unsupported assertion. Multi-retire decisions use exact triple-ID
  invalidation through the same support-aware machinery.
- Fixpoint: re-harvest after adoption should remove the resolved functional
  contradiction. Legitimately multi-valued skipped groups may still surface.

> **KG path safety:** do not write contradictions through the
> `mempalace_kg_invalidate` MCP handler from these scripts. In mempalace 3.5.0,
> the handler resolves the palace-local KG only when the MCP server process was
> started with a CLI `--palace` flag (`_palace_flag_given`). Library imports have
> no such flag, so the handler can target the user's default
> `~/.mempalace/knowledge_graph.sqlite3` regardless of
> `MEMPALACE_PALACE_PATH`. `dream_palace.KgWriter` therefore constructs
> `KnowledgeGraph` against the explicitly resolved palace KG and performs
> support-aware mutations there. A bare native supersede/invalidate call alone
> does not maintain this package's provenance tables.

### Task: reflect / pattern

- Constructive `reflect` is the **net-new-insight** task: `distill`, `generalize`,
  `name_gap`, `connect`, `converge`, `tension`, `shared_constraint`.
  Current `--task pattern` aliases `reflect/converge` for recurrence-gated
  generalization; it is not the only constructive operation.
- Adoption is **ADD-ONLY**: approved decisions add a surfaced lesson drawer and
  never delete drawers or invalidate KG facts.
- Detection is mechanical: a theme is a connected component of the `≥ τ`
  similarity graph over observations (`τ` defaults to `0.75` for pattern).
- Observation extraction and rule synthesis are cognitive: the agent reads the
  theme members, extracts atomic observations, judges whether a generalizable
  rule exists, and writes the final lesson.
- Groundedness invariant: converge must cite the worklist's declared
  `min_support` **distinct original sessions** (at least two; the pattern
  examples use three). Adoption re-reads original text/hashes and rejects
  missing/drifted sources or forged session IDs. A diary/raw mirror is one
  session, not two. `lesson`, `reflect`, procedural and marked generated
  summaries are not independent recurrence support. Legacy pattern adoption
  still rejects empty `supported_by`; newly harvested worklists use reflect.
- Other ordinary reflect kinds retain at least two quote-grounded member
  drawers. Exact quotes establish provenance, not entailment. Separate optional
  procedural enrollment always needs three original sessions, never just two
  reflections or a derived summary.
- `session_id` is the join key. Session identity is host-owned and orthogonal to
  mempalace; it is stamped onto diary entries at write time because a diary entry
  is a memory **about** a host session. `extract_session_id` parses a
  `SESSION_ID:<guid>` token; legacy entries without that token contribute no
  support.
- Two substrates / two modes:
  - **Diary-mode** (portable, mempalace-native, v1): `load_observation_entries`
    reads diary drawers, groups chunks by `parent_entry_id` / `parent_drawer_id`,
    and extracts stamped `session_id`s from text.
  - **Session-mode** (host amplifier): `dream_sessions.py` is a read-only adapter
    over `~/.copilot/session-store.db` (or `COPILOT_SESSION_STORE`) that loads
    sessions, turns, and session-attributed observations. It imports only the
    stdlib, not mempalace, isolating HOST coupling from mempalace coupling.
- Two-tier substrate: because diary carries `session_id`, a diary theme can drill
  down to raw host sessions; induced rules cite exact session ids.
- Weaker fixpoint than merge/contradiction: pattern is add-only, so there is no
  destructive convergence. Convergence comes from excluding already-adopted
  lessons from mining plus a dedup gate during adjudication so high-support
  themes become "covered."

Harvest:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --palace <palace> --task pattern --wing <wing> \
  --rooms diary --min-support 3 --out worklist.json
```

### Task: prune / forget

- This is the **FORGETTING** task. Its safety model
  is therefore inverted: **remove carefully, reversibly**. `merge` preserves
  source facts by add-then-delete, `contradiction` soft-invalidates KG facts,
  and `pattern` is add-only; `prune` may delete drawers after approval.
- The base score combines age from `filed_at`, KG protection degree, redundancy
  (maximum cosine similarity to neighbours), and ephemeral marker negatives
  (`for now`, `one-off`, `scratch`, etc.). Native usage is an additive,
  protection-only signal with default weight `0.2`, not a replacement for these
  gates. Missing telemetry and zero `access_count` leave the base score
  unchanged, including a never-accessed drawer with high initial strength.
  Strength uses the native `0.05..5` scale, normalized above its floor; usage
  snapshots and the added boost are included in salience for review. For
  positive access count `n`, the usage signal averages `n/(n+1)` with
  `clamp((strength-0.05)/4.95, 0, 1)`. Its weighted contribution is added before
  the existing final score clamp; positive usage never lowers the score.
- Usage is read across the complete scoped metadata set and combined
  conservatively across physical chunks. The native `drawer_salience` tool
  returns at most 100 records with no pagination, so absence from that response
  cannot be treated as absence of usage. Reads do not potentiate drawers.
  Partial snapshots omit unavailable fields; reading must not invent a fresh
  activation timestamp or count.
  Retrieval is not a helpful outcome and never promotes procedural advice.
- Candidate selection is the guardrail heart: **multi-gate AND**, never OR. A
  drawer is proposed only when `v < v_min` AND `age_days >= age_floor_days` AND
  `kg_degree == 0` AND it is not pinned. The `kg_degree == 0` gate also means the
  pruned drawer sourced no KG triples, so deletion cannot orphan the graph.
- Adoption is archive-**before**-delete: each pruned drawer is appended as a full record
  (including `salience` and `archived_at`) to an append-only JSONL cold store,
  flushed and `fsync`ed, and only then deleted through the sanctioned
  `mempalace_delete_drawer` handler, which purges the closet/AAAK index. A
  failed archive deletes nothing; the archive is lossless and reversible.
- Apply has a protected re-check: drawers with `kg_degree > 0` or `pinned` are
  refused even if adjudication said `prune`. Usage is also refreshed under the
  existing mutation lock. Retrieval advanced since harvest or a refreshed score
  outside the approved worklist's eligibility policy invalidates prune approval.
  The same core scorer is used at harvest and apply.
- All retained procedural events and their original source/lineage drawers
  are excluded at harvest and checked live under the shared mutation lock at
  apply, including logical/physical chunk IDs. Terminal/out-of-scope history
  does not release protection; old worklists cannot bypass it.
- Steering-sensitive: this is where steering `θ` bites hardest. "Focus on X"
  reweights salience; "preserve X" pins a fixed point that should not be pruned.
- Fixpoint is a maintenance loop, not one-shot convergence: re-harvest should
  remove approved candidates from the current pass, but cold/redundant drawers
  will appear over time as the palace evolves.

Harvest:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_harvest.py" --palace <palace> --task prune --wing <wing> \
  --room <room> --v-min 0.35 --age-floor-days 30 --out worklist.json
```

Adopt:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl --dry-run
"$MPY" "$DREAM_SCRIPTS/dream_adopt.py" --palace <palace> --decisions decisions.json \
  --archive-file archive.jsonl
```

### Future task shape

Additional worklist `kind`s should keep the same harvest/adjudicate/adopt shape.

### Optional procedural lifecycle (separate CLI)

`dream_procedure.py` owns `propose`, `validate`, `review`, `outcome`, `guidance`,
and `explain`. It does not add a harvest task, migrate old reflections, enable
ontology rules, update KG schemas or infer task outcomes. The existing dreaming
skill reviews normative statements; mechanics project immutable drawer events.

The [procedural contract](procedural.md) specifies exact JSON schemas, digest
preparation/retries, three-session grounding, support/contrast dispositions,
explicit rule-specific attribution, review-head joins and source retention.
Usefulness decay/maturity never grants logical authority. Anti-patterns require
independently reviewed wording, not automatic inversion.

Default guidance delivers only eligible established/proven rules in the exact
repository. Approved candidates require deliberately requested labeled trials.
Guidance has a combined five-item / 6,000-character serialized ceiling; explain
retains full score terms, review dispositions and lineage. No retrieval creates
feedback and no read writes scores. Unsupported read-only backends, incomplete
WAL/SHM states, missing models/evidence and incomplete histories fail explicitly.
Verified support is WAL-aware SQLite-exact storage and an installed local MiniLM
cache; legacy destructive safety lookups keep their existing backend semantics.

## Artifacts (session workspace — never commit)

For explicit legacy worklists, Phase-2 adjudication can use the human-readable
renderer instead of opening large raw JSON:

```bash
"$MPY" "$DREAM_SCRIPTS/dream_show.py" --worklist <worklist.json>
"$MPY" "$DREAM_SCRIPTS/dream_show.py" --worklist <worklist.json> --task derive --full
```

Incremental worklists initially have no candidate items. Review their full
`coverage` in batches; an empty candidate rendering is not completed review.

The renderer prints one compact block/line per legacy candidate and avoids the 20KB file
view limit. Use it for merge, contradiction, pattern, prune, and derive worklists
before filling `item["decision"]`.

### `worklist.json` (harvest → agent)

```jsonc
{
  "version": 1,
  "task": "merge",
  "scope": {"palace": "<path>", "wing": "<w>", "room": "<r|null>"},
  "params": {"tau": 0.9},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "merge",
      "cluster_id": 0,
      "members": [
        {"id": "<logical id>", "member_ids": ["<physical id>", ...],
         "text": "<drawer text>", "wing": "<w>", "room": "<r>"}
      ],
      "supersedes": ["<physical id>", ...],   // union of all member_ids
      "evidence": {"pair_sims": [{"a": "id", "b": "id", "sim": 0.97}], "size": 2},
      "decision": null                        // agent fills this
    }
  ]
}
```

Contradiction worklist:

```jsonc
{
  "version": 1,
  "task": "contradiction",
  "scope": {"palace": "<path>", "task": "contradiction"},
  "params": {},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "contradiction",
      "cluster_id": 0,
      "subject": "Alice",
      "predicate": "lives_in",
      "candidates": [
        {"object": "Seattle", "valid_from": "2025-01-01", "extracted_at": "2025-01-02"},
        {"object": "Portland", "valid_from": "2024-01-01", "extracted_at": "2024-01-02"}
      ],
      "evidence": {"size": 2, "newest_object": "Seattle"},
      "decision": null
    }
  ]
}
```

Legacy pattern worklist (retained for older artifacts; current `--task pattern`
produces `reflect/converge`, as described in the skill):

```jsonc
{
  "version": 1,
  "task": "pattern",
  "scope": {"palace": "<path>", "wing": "<w>", "rooms": ["diary"], "task": "pattern"},
  "params": {"tau": 0.75, "min_support": 3},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "pattern",
      "cluster_id": 0,
      "members": [
        {"id": "<entry id>", "text": "<diary text>", "session_id": "<session id>",
         "agent": "<agent|null>", "date": "<date|null>", "topic": "<topic|null>"}
      ],
      "evidence": {
        "size": 3,
        "support": 3,
        "support_ids": ["<session id>", "..."],
        "pair_sims": [{"a": "id", "b": "id", "sim": 0.82}]
      },
      "decision": null
    }
  ]
}
```

Prune worklist:

```jsonc
{
  "version": 1,
  "task": "prune",
  "scope": {"palace": "<path>", "wing": "<w>", "room": "<r|null>", "task": "prune"},
  "params": {"v_min": 0.35, "age_floor_days": 30},
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "prune",
      "id": "<logical id>",
      "member_ids": ["<physical id>", "..."],
      "text": "<drawer text>",
      "wing": "<w>",
      "room": "<r>",
      "salience": {
        "age_days": 45,
        "kg_degree": 0,
        "redundancy": 0.94,
        "negatives": true,
        "v": 0.12
      },
      "decision": null
    }
  ]
}
```

Derive / contemplate worklist:

```jsonc
{
  "version": 1,
  "task": "contemplate",
  "scope": {"palace": "<path>"},
  "params": {"max_depth": 3, "max_iterations": 10, "max_candidates": 500},
  "ontology_version": "onto:<hash>",
  "rules": ["<ontology rules...>"],
  "instructions": "<optional steering|null>",
  "items": [
    {
      "kind": "derive",
      "candidate_id": "derive:<stable hash>",
      "conclusion": {
        "subject_id": "<entity id>",
        "subject": "<display name|null>",
        "predicate": "<derived predicate>",
        "object_id": "<entity id>",
        "object": "<display name|null>"
      },
      "rule": {"id": "<rule id>", "family": "transitive", "predicate": "<base predicate>"},
      "proof": {
        "depth": 2,
        "premise_ids": ["<kg triple id>", "..."],
        "premise_drawer_ids": ["<source drawer id|null>", "..."]
      },
      "evidence": {
        "already_active": false,
        "confidence": 1.0,
        "valid_from": "<premise interval start|null>",
        "valid_to": "<premise interval end|null>"
      },
      "decision": null,
      "ontology_version": "onto:<hash>"
    }
  ]
}
```

The derive conclusion is nested at `item["conclusion"]`; there are no top-level
`subject`, `predicate`, or `object` fields on derive items. See
[`derive.md`](derive.md) for the full derive schema and decision contract.

### `decisions.json` (agent → adopt)

Same document with each `item.decision` set to one of:

Merge:

```jsonc
{"action": "merge", "wing": "<w>", "room": "<r>",
 "text": "<synthesised drawer>", "supersedes": ["<physical id>", ...]}
```

Contradiction:

```jsonc
{"action": "invalidate", "keep": "<object>", "invalidate": ["<stale object>", ...]}
```
If `invalidate` is omitted, adoption invalidates every candidate object except
`keep`. Use this only after judging that the predicate is functional and the kept
object is authoritative.

Legacy pattern:

```jsonc
{"action": "surface", "wing": "<w>", "room": "<r>",
 "text": "<induced rule/lesson>",
 "supported_by": ["<session id>", "..."]}
```

If `wing`/`room` are omitted, adoption falls back to the first member when that
metadata is present, then to the worklist scope. If `supported_by` is omitted,
adoption falls back to `evidence.support_ids`; empty support is rejected.

Prune:

```jsonc
{"action": "prune"}
```

```jsonc
{"action": "keep"}
```

If a prune decision is omitted, adoption defaults to `keep` (conservative). A
`prune` decision may also repeat `id`, `member_ids`, `wing`, `room`, `text`, or
`salience`; omitted values fall back to the worklist item.

```jsonc
{"action": "skip"}
```

`wing`/`room`/`supersedes` default to the item's values if omitted.
For contradiction items, `skip` means the group is legitimately multi-valued or
not safe to adjudicate.
For pattern items, `skip` means the rule is unsupported, not generalizable, or
already covered by an existing filed lesson.
For prune items, use `keep` rather than `skip`; missing or non-`prune` decisions
are treated as keep.

Derive:

```jsonc
{"action": "materialize"}
```

```jsonc
{"action": "skip", "reason": "<why this candidate should not materialize>"}
```

```jsonc
{"action": "reject_rule", "reason": "<why this ontology rule is unsound>"}
```

For derive items, write the decision into `item["decision"]`. The subject,
predicate, and object remain nested under `item["conclusion"]`; adoption relies
on that shape when materializing approved facts.

## Historical mempalace API facts (mempalace 3.5.0)

These are version-specific legacy notes, not the new procedural backend
contract. The installed SQLite-exact adapter also supports comparison filters
such as `$ne`; the procedural integration tests exercise its real handlers,
exact chunk reads and strict nonmutation boundary.

- **Read**: `from mempalace.palace import get_collection;
  col = get_collection(palace_path)`. `col.get(include=["documents",
  "metadatas", "embeddings"], where=<filter>)` returns 384-dim (minilm)
  embeddings. `where` is equality-only (`$and` of `{wing:..},{room:..}`); there
  is **no** `$ne`/`$nin`, so an unscoped search surfaces every wing — the reason
  any shadow/candidate content must live in a separate `palace_path`, not a
  `<wing>__dream` wing of the live palace.
- **Chunking**: large drawers split into physical rows sharing
  `metadata["parent_drawer_id"]` with `chunk_index`. `group_logical_drawers`
  rebuilds logical drawers; `tool_delete_drawer(id)` works on **physical** ids,
  not the logical group handle.
- **Write** (the sanctioned path): `from mempalace.mcp_server import TOOLS;
  TOOLS["mempalace_add_drawer"]["handler"](wing, room, content, added_by=...)`
  and `TOOLS["mempalace_delete_drawer"]["handler"](drawer_id=...)`. The durable
  alternative `mempalace.service.run_mcp_tool` accepts write-classified tools
  only.
- **Palace targeting**: bind and verify the requested palace before native
  reads. Changing `MEMPALACE_PALACE_PATH` alone is not a cache-refresh guarantee
  after the embedded server has already been imported. Embedded imports must
  restore Python stdout and descriptor 1; the server's startup redirection is
  not appropriate for JSON-producing command-line clients.
- **KG read/write**: the palace-local KG is
  `<palace_path>/knowledge_graph.sqlite3`; active triples are rows where
  `valid_to IS NULL`. For contradiction adoption, use
  the explicit-path, support-aware `KgWriter` instead of the MCP
  `mempalace_kg_invalidate` handler because of the `_palace_flag_given` gate
  described above. The package's provenance records are part of the mutation
  contract, not supplied by the native supersede signature.

## Invariants

| Invariant | Enforced by |
|-----------|-------------|
| Approved mutations / reversibility | no implicit adoption; legacy initialization/reconciliation and explicit ontology candidates are read-only exceptions; failed add skips delete; merge/prune archive full records before delete; failed archive deletes nothing |
| Provenance | `supersedes` on every merge |
| Groundedness | converge revalidates declared `min_support`, original session IDs and hashes; mirrors/generated records cannot inflate support; quotes do not prove semantic entailment |
| Salience-gated protected classes | prune requires `v < v_min` AND age floor AND `kg_degree == 0` AND not pinned; apply refreshes protected state and usage before honoring approval |
| Auditability | merge/prune archives retain drawer text, physical members and `archived_at`; procedural events retain all original evidence even after retirement |
| Operational verification | Phase 5 measures remaining candidates, not a universal zero-cluster guarantee; reflection/pattern/prune are maintenance loops |
| Bounded cost | scope by wing/room; `tau` gates the pairwise graph |
| Procedural authority | optional reviewed advice only; explicit attributed outcomes, no feedback from retrieval and no automatic KG/ontology authority |

## Substrate capabilities and limitations

The scripts still require a Python interpreter that can import the installed
MemPalace package. Merge uses its native `mempalace_find_duplicates` handler,
not a separate MCP transport. Canonical source reconstruction, complete usage
metadata, prune redundancy and reflective operations still need collection
access. This is not a claim that the whole pipeline is remote-MCP-native or
universally read-only.

The duplicate handler must provide logical member IDs, valid pairwise distances
and a successful, non-truncated scan. Missing capabilities, malformed responses,
unavailable vectors and explicit incompleteness are errors. A package version
number alone is not proof of this contract. No runtime package or model download
is attempted, and the adapter does not silently switch to a different clustering
algorithm when a capability fails.

The installed native finder also bounds neighbor discovery (currently 512
physical records). The adapter refuses scans when that bound cannot establish
coverage of the actual native scope. On builds with the two-key filter defect,
requesting both wing and room scans the wing natively and filters the room
locally; a small room therefore cannot hide an incomplete wing scan. Narrow
to a supported native scope or use a substrate with sufficient coverage.
This limitation is explicit rather than a claim of unlimited consolidation.

Usage metadata is additive: older drawers without it retain the previous score.
The capped native salience listing is unsuitable as an exhaustive usage oracle.
Contradiction supersession additionally requires this package's support-aware
transactional writer, rather than bare native KG calls.

The historical proposals explain the motivation:
[`upstream-find-duplicates-proposal.md`](upstream-find-duplicates-proposal.md)
and [`upstream-drawer-salience-proposal.md`](upstream-drawer-salience-proposal.md).
