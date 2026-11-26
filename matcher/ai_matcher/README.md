# AI Matching and Residual Reconciliation

This guide describes the AI-assisted matcher and the final residual pass in
`matcher/`. The pipeline first runs exact, fuzzy, and memory matching. Phase 4
then gets the remaining ledger and bank rows. Phase 5 classifies whatever still
remains and may draft journal suggestions for some unmatched bank rows.

## Ollama client

`ai_utils.get_shared_llm()` creates one cached `ChatOllama` client per matcher
worker. The configured model defaults to `gemma3:4b`; the client uses
temperature `0`, an 8,192-token context window, repeat penalty `1.1`, and a
768-token output limit. `OLLAMA_NAME` and `OLLAMA_URL` can override the model and
endpoint.

The matcher does not make a separate generated `"ping"` request. It initializes
the client and measures the first real generation. If that request fails, the
current matching pass stops making more model calls and leaves the unprocessed
rows for later review or residual handling.

All requests run synchronously in the `matcher.run` Celery task. They are not
sent to a separate AI queue. On a CPU-only Ollama host, each generation blocks
that matcher worker until it completes.

## Phase 4: AI matching

`reconcile()` constructs `AIMatcher` with the remaining rows, the current
`same_side` setting, and the AI amount tolerance. In the standard pipeline the
AI tolerance is ±5.00. The matcher runs two passes in order.

### 1-to-1 semantic matching

`AIMatcher.batch_match()` divides the combined record date range into 30-day
windows that advance 23 days at a time, giving a 7-day overlap. Each window is
ordered by distance from its midpoint and split into ledger and bank batches of
at most 20 rows each.

Before an Ollama request, the matcher forms candidate pairs using:

- Amount difference within the AI tolerance.
- Compatible debit/credit direction according to `same_side`.
- Both records in the current date window, so their calendar-date gap is at
  most 30 days.

Only rows participating in a candidate pair are included in the prompt. The
prompt also lists the allowed `ledger_id -> bank_ids` pairs, and the model is
asked to return compact JSON with IDs and confidence. A pair already evaluated
in an earlier overlapping window is not sent again.

Every proposal is checked against the current pools, the submitted candidate
pairs, and the deterministic amount/direction bouncer. A proposal is accepted
only at confidence ≥ 0.75. Lower-confidence proposals go to the audit queue;
invalid IDs or pairs are discarded. The model's confidence is its own stated
score, not a calibrated probability.

### Many-to-one residual matching

If bank rows remain, `AIMatcher.residual_match()` searches for ledger subsets
for each bank row. Candidate ledgers must be within 10 calendar days, have a
compatible direction, and not individually exceed the bank amount plus
tolerance. The deterministic subset search looks for 2–6 ledger rows, with a
200,000-node limit and up to 20 valid options. An incomplete search is routed
to audit rather than passed to the model as complete.

The matcher can ask about up to two bank rows in one Ollama request when their
candidate ledger ID sets are disjoint. If the sets overlap, it sends the
current group first, updates used ledger IDs, and rebuilds candidates for the
next bank row. The model can choose only from the listed amount-verified
subsets. Returned bank IDs and ledger subsets are validated, the confidence
threshold is applied, and the final amount/direction bouncer runs before a
match is accepted.

If no valid subset exists, no Ollama request is made for that bank row. If
there are no remaining bank rows after 1-to-1 matching, the residual matcher is
skipped.

## Phase 5: residual reconciliation

`ResidualReconciler` is a separate phase after AI matching. Most of it is
deterministic:

- It classifies residual rows using amount, direction, date, split-sum, and
  narration evidence.
- It resolves exact-amount timing candidates using a wider 45-business-day
  window.
- It searches deterministic split candidates before creating a human review
  queue.

For eligible unmatched **bank** rows, `_generate_journal_drafts()` can ask
Ollama for a counter-account and a short entry narrative. These are suggestions
marked `pending_review`; they are not posted automatically. This can generate
one model request per eligible bank row. If a generation fails, the current
and remaining rows use heuristic fallback suggestions. Unmatched ledger rows
go to review rather than receiving these bank-side journal drafts.

## Runtime diagnostics

Every Ollama call prints a line with its phase, elapsed seconds, approximate
prompt character count, response character count, and relevant batch details.
For example:

```text
[ai] phase=1to1 finished in 8.421s prompt_chars=3120 response_chars=394 window=2026-08-01:2026-08-31 ledger=12 bank=9 candidate_pairs=17
[ai] 1to1 complete calls=4 evaluated_pairs=53 accepted=7 audited=1
[ai] phase=many_to_one finished in 9.102s prompt_chars=2210 response_chars=112 bank_rows=2 options=6
[ai] many_to_one complete calls=3 eligible_banks=5 accepted=2 audited=0
[ai] phase=journal_draft finished in 6.508s prompt_chars=123 response_chars=96 bank_id=41
```

`prompt_chars` is a rough size indicator, not a token count. The summary lines
show call counts and outcomes; compare the per-call times to see whether
latency comes from 1-to-1 batches, residual subset decisions, or journal
drafting.

## Safety and fallback behavior

- Amount and direction checks remain deterministic; the LLM cannot override
  them.
- The residual LLM only selects among deterministic, amount-verified subsets.
- Invalid JSON, low confidence, incomplete subset search, and model errors do
  not create a match. Rows remain unreconciled or go to human review.
- The `reasoning` fields in the output schemas are optional. The prompts omit
  them to reduce generated tokens.
