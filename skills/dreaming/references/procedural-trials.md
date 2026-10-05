# Deliberate, bounded candidate trials

This playbook uses the existing repository-scoped procedural-v1 interfaces.
It adds no enrollment mode, score policy, metrics database or automatic Stop
outcome. A trial is a permitted, one-task attempt to observe what following an
eligible candidate changes, not a claim that the advice works.

The [procedural contract](procedural.md) governs storage, original-source
integrity, exact payloads, budgets and eligibility. These instructions work
with manual `outcome`; feedback drafts or later review tooling are not
prerequisites. All examples use the existing `$MPY`, `$DREAM_SCRIPTS`,
`$PALACE`, `$STORE` and external `$ARTIFACTS` conventions from that contract.

## Keep the decisions separate

| Decision | What it establishes — and does not |
|---|---|
| Ordinary lesson convergence | The separate ordinary two-session convergence process can yield reviewed, fallible recall context. It is neither a prerequisite for procedural enrollment nor a maturity test. |
| Procedural enrollment | A target-scoped proposal needs three distinct original sessions in the exact target repository. Repeated references, raw turns plus diary from one session, generated lessons and captures do not multiply support. Enrollment does not approve use. |
| Candidate | The initial maturity label, not permission and not established usefulness. Three supporting enrollment sessions do not constitute three helpful outcomes. |
| Approval | Review of original support, explicit contrast search, conditions, exceptions and adverse evidence. Approval requires the existing three-target-session support gate and adds zero helpful credit. |
| Trial | Current target permission plus deliberate selection of a currently eligible approved candidate, a bounded task and fresh applicability/use checks. Selection, reading and performing are not helpfulness. |
| Established/proven delivery | Separate outcome-based, time-sensitive [maturity and eligibility policy](procedural.md#versioned-usefulness-policy), not graduation after one trial or a successful task. |

Hashes and quotations establish provenance, not causal attribution or semantic
fit. A wing, repository label, high similarity or mature source rule cannot
settle those judgments.

## Short compatible-local path

1. **Establish current target consent and ordinary recall.** Resolve the live
   repository, activity, constraints, session and actor. Ordinary recall comes
   first; present applicable facts and accepted lessons separately from
   procedures. Existing permission must cover advice and deliberate candidate
   trials in this target. Fresh permission checking does not itself require
   asking for fresh human consent. Unknown identity or permission abstains from
   procedural use, not otherwise permitted ordinary work.
2. **Reuse valid local support and review.** For an already enrolled local
   candidate, check current eligibility and compare task, architecture, tooling,
   constraints and every exception with its supported conditions. Compatible
   reuse needs no repeat enrollment, transfer dossier, new fixed session count
   or mandatory contemplation. If enrollment/review is absent, follow the
   existing [proposal](procedural.md#exact-envelope) and
   [validation/review](procedural.md#validation-and-review) gates first:
   three distinct target-original sessions, support plus explicit contrast
   query, source dispositions and approval. Insufficient support remains a
   non-enrolled hypothesis. Material drift, adverse evidence or materially
   unknown fit withholds the affected advice pending assessment; do not erase
   originals or automatically demote maturity just because a file/framework
   changed.
3. **Inspect a bounded selection, not a license to act.**

   ```bash
   "$MPY" "$DREAM_SCRIPTS/dream_procedure.py" guidance \
     --palace "$PALACE" --wing project --repository owner/repository \
     --task 'The actual bounded target task' --include-candidates --max-items 1
   ```

   `--include-candidates` allows eligible approved candidates alongside
   established advice. The single top item can be established: only an actual
   item in `trials` with `delivery="approved_candidate_trial"` is a candidate
   trial selection. If none is selected, abstain from a candidate trial; do not
   relabel an established item or force selection by removing safety gates.
   Default guidance excludes candidates. Healthy empty selection differs from
   unavailable/corrupt evidence or another nonzero error.
4. **Fill the local trial card below for one selected rule and one task.**
   Prefer reversible, permitted work. State the expected observable difference
   from ordinary recall alone, alternatives and the actual risk/stop bound
   before acting. If the condition or any complete exception cannot be checked,
   withhold the trial.
5. **Use the current task-bound path before action.** The legacy `guidance`
   response and local card are not bound permission. Obtain fresh
   [`task-guidance`](procedural.md#task-bound-delivery-and-cooperative-use-checks)
   with independently resolved current context, mode
   `approved_candidate_trial`, target `advice=allow` and `trials=allow`.
   Verify that the selected rule is still an actual trial in that packet.
   Read its full statement, condition, all exceptions and sources; assess
   current constraints and supported context in the explicit
   [applicability witness](procedural.md#applicability-is-explicit-reasoning-not-a-ranking-oracle).
   Run `use-check` for that one rule with fresh current/permission/applicability
   inputs. Act only on its fresh usable full item, never on a frozen packet.
   Changed content requires a new offer, full rereading and reassessment, not
   merely new timestamps.
6. **Acknowledge, act within the bound, then observe.** Visibly name the read
   rule, intended action, trigger and checked exceptions. If any tool, event or
   time intervenes, refresh the witnesses and run `use-check` immediately before
   each advised action, even in the same unchanged task. Report the performed
   action with its locator, or why it was not performed; unknown stays unknown.
   Stop on the conditions below. Optional historical receipts need separate
   consent and never count as an original observation or permission renewal.

### Local trial card

This is a transient preparation/checklist, **not** a new stored event schema,
original evidence, transfer dossier or scoring artifact. Keep it outside the
palace and checkout if saved. Reference existing verified sources rather than
manufacturing a new account of earlier sessions.

| Required slot | Record before the trial |
|---|---|
| Identity and scope | Actual rule ID, current session/actor, target repository and one activity/task; current advice/trial permission references. |
| Behavior and fit | Complete rule statement and intended behavior, full `applies_when` trigger and **all** exceptions; why the trigger holds and each exception is absent. Check actual architecture, tooling, constraints and supported target conditions. Unknown material context stays unknown. |
| Bound and stop | One-task scope, allowed reversible action, concrete risk boundary, what would exceed it, and observable stop conditions. Use real task constraints; this playbook invents no timeout, retry quota or risk score. |
| Expected effect | What observable difference following this particular rule could make, and what result would disconfirm that expectation. Do not promise benefit. |
| Alternatives and baseline | The action ordinary recall/live instructions would support without this candidate, and viable alternatives. This is a comparison baseline, not a fabricated control run or proof of causality. |
| Observation plan | Where an actual independent observation can be located; preserve original repository, session, UTC observation time, full-source hash and exact quote, plus relevant known context. Name evidence needed to distinguish the expected effect from alternative explanations. |

## Separate transfer checklist

Use this only for a new target or a broader applicability claim, not unchanged
compatible-local reuse.

- Identify the source rule and **original** observation origins separately from
  the target hypothesis. A broad or “universal” claim is a hypothesis, not a
  privileged scope.
- Compare transferable conditions, target architecture/tooling/constraints,
  material differences, complete exceptions and counterexamples. Record what
  remains unknown. Foreign adverse evidence can matter even though it cannot
  become target harm/helpfulness credit.
- State the expected target behavior/effect, ordinary-recall baseline,
  alternatives, risk/stop boundary and original observation requirements.
  Forks, shared originals and generated copies retain lineage; they cannot
  multiply independent observations.
- Source approval, support counts, helpfulness, maturity and consent do not
  permit a target trial or carry score. Resolve current **target** permission
  separately. Repositoryless originals remain ordinary recall material where
  authorized, not evidence that can be relabeled as target-original support.
- Under the existing repository-only contract, use in the target requires a
  target-scoped proposal with **three distinct target-original sessions** and
  the existing contrast/review/approval gate. Below that support, retain a
  **non-enrolled hypothesis**, not an executable candidate trial. Do not lower
  the source count for cold start, borrow foreign sessions or treat an
  applicability witness/receipt as enrollment.
- Only then enter the bounded candidate path above. Target maturity still
  depends on actual attributable target outcomes under the existing policy;
  several successful source repositories do not prove universality.

## Stop, preserve and adjudicate — or abstain

Stop the trial on observed harm, an applicable exception, a crossed risk bound,
changed task/repository/constraints, material context drift, lost permission or
new source/eligibility failure. Continue only independently permitted ordinary
work; a stopped trial does not authorize a retry or a wider experiment. Refresh
context, recall, source checks, offer and assessment before any later advised
action.

Preserve adverse originals, exact provenance and the existing review/outcome
history. Merely acknowledging valid harm, waiting for score decay, repeating a
receipt or obtaining overall task success cannot clear it. Follow the existing
grounded review/disposition rules; do not suppress harm by omission, silently
change the rule or generate a negated anti-pattern.

After actual work, compare applicability, performed behavior, observed effect
and alternatives using **actual independent original observations**:

- `helpful` or `harmful` requires an attributable effect of following this rule,
  supported by the original evidence and its target conditions.
- Explicit `neutral` still requires attributable evidence about this rule's
  performed behavior/effect. Unsupported attribution **abstains, not neutral**.
  No action, unknown effect, passing tests or a successful task alone does not
  justify an outcome.
- Keep original repository/session/time/hash/quote intact; filing/retry time,
  generated cards, packets, receipts, reviews and derived lessons are not new
  observations. Multiple filings of one session do not add support or credit.
- Use the existing [manual outcome](procedural.md#explicit-outcomes) artifact
  and `outcome` command only when that evidence supports the explicit judgment.
  The schema/provenance checks do not prove causality; the reviewer remains
  responsible for attribution and adverse alternatives. When unresolved, keep
  the observations for review and publish no guessed polarity.

No automatic Stop outcomes, metrics database, automatic trial enrollment or
source-count reduction is implied. The contract tests accompanying this
reference demonstrate existing admission/delivery/score boundaries on
controlled fixtures, not empirical usefulness or guaranteed agent compliance.
