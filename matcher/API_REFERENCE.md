# Matcher API reference

This document describes the matcher package as implemented. `LedgerFormat`,
`BankStatement`, `Settings`, `MatchedPair`, `SameSideDetection`,
`IgnoredMetadataRecord`, `AuditInvestigationItem`, and
`DraftAccountSuggestion` are schema/configuration types imported from the
`schema` and `app.shared` packages; their definitions live outside `matcher`.
Amounts are currency units (not paise) at the API boundary. Matching helpers
convert to integer paise where exact currency comparisons are required.

## Main pipeline — `matcher/__init__.py`

### `reconcile(config, ledger_result, bank_result, all_warnings, same_side=True, auto_detect_same_side=True, memory=None, llm=None, enable_ai_matching=True, enable_residual_reconciliation=True)`

- `ledger_result`, `bank_result`: mappings containing `records`; bank metadata
  may also contain `bank_name` and `template_version`.
- `all_warnings`: mutable list; detected orientation mismatches and AI fallback
  notices are appended here.
- `same_side`: whether GL debit corresponds to bank debit. If false, the
  orientation is reversed.
- `auto_detect_same_side`: run detection and warn on a confident mismatch;
  it does not override the supplied orientation.
- `config`: required `Settings` used to configure the shared AI client.
- `llm`: optional LLM client supplied to AI and residual-draft phases.
- `memory`: optional `MatchMemory`; omit to disable cross-run recognition.
- `enable_ai_matching`: skip the AI matching and AI journal-drafting calls when
  false; exact, fuzzy, memory, and deterministic residual matching still run.
- `enable_residual_reconciliation`: enable the final timing/split/draft phase.
- Returns a report mapping containing per-phase matches, audit/review queues,
  unmatched records, warnings, and summary statistics.

Pipeline order: optional orientation detection, exact, fuzzy, memory, AI, then
residual reconciliation. Each phase receives only the prior phase's leftovers.
The AI phase can be disabled with `enable_ai_matching=False`; residual matching
then uses its deterministic rules and heuristic journal-draft fallback.

`_quality_summary(all_matches)` accepts confidence-annotated match mappings and
returns the average, low-confidence count/percentage, and per-match-type totals.
`LOW_QUALITY_CONFIDENCE_BAR` is the cutoff (0.5).

## Orientation — `same_side_detect.py`

`detect_same_side(gl_records, bank_records) -> SameSideDetection` compares
same-orientation debit/debit and credit/credit amounts with opposite-orientation
debit/credit and credit/debit amounts. Pairs are greedily one-to-one within
`_AMOUNT_TOL` (0.50). It returns a confident result only when at least
`_MIN_MATCHED_PAIRS` (3) amount pairs exist and the winning orientation has at
least `_CONFIDENCE_RATIO` (65%) of votes. Too few pairs returns an uncertain
same-side default and a reason. It never changes records.

Internal helpers: `_safe_amount(obj, attr)` converts an absent/invalid field to
zero; `_greedy_match_pairs(sorted_a, sorted_b, tol)` pairs sorted amount lists
once each; `_to_matched_pairs(pairs, orientation, amount_type)` builds the
schema `MatchedPair` objects for the result.

## Exact matching — `exact_match.py`

`exact_match(gl_records, bank_records, same_side=True, amount_tol=0.05,
strict_ref_conflict=False, show=True) -> dict` returns `EXACT_MATCHES`,
`PENDING_FUZZY_LEDGER`, and `PENDING_FUZZY_BANK`.

Acceptance criteria: same normalized calendar date, compatible debit/credit
orientation, and absolute amount difference within `amount_tol`. Candidate
priority is exact `reference_id`/`txn_id`, then matching extracted UTR, then
amount/date alone. Contradictory UTRs veto a candidate; with
`strict_ref_conflict=True`, a ledger reference plus bank UTR conflict also
vetoes it. Candidates are ranked by reference tier, text similarity, and
amount difference, then greedily consumed one-to-one. Amounts are compared in
integer paise. `competing_candidates` records the number of plausible bank
choices for the ledger row. `show` is retained as an API argument; the matcher
returns data and does not use it to change match criteria.

`_cents(x)` converts absolute currency amount to paise;
`_date_key(record, is_bank)` normalizes a record's date for comparison.

## Fuzzy matching — `fuzzy_matcher/fuzzy_match.py`

### `FuzzyMatcher(pending_ledger, pending_bank, same_side=True, tolerances=None, calendar=None)`

Creates mutable ledger/bank working pools. `same_side` selects how GL debit
and credit fields map to bank debit/credit fields. `tolerances` overrides the
`DEFAULT`, `TIMING_DIFFERENCE`, `ROUNDING_DIFFERENCE`, `TRANSPOSITION`, and
`EXACT` thresholds; missing keys use the defaults in the implementation.
`calendar` controls business-day lag calculations and defaults to Indian bank
holidays. Call `run()` to execute strategies in order and get the result dict.

`run()` strategy order and acceptance criteria:

1. `cleanse_zero_amount_metadata`: separates zero-value or metadata-only rows
   into ignored metadata output.
2. `flag_ghost_reversals`: identifies reversal/duplicate/bounce-like entries
   for investigation, rather than accepting them as ordinary matches.
3. `match_deposit_in_transit`: GL receipt with no bank clearance in the timing
   window; emits an outstanding timing item.
4. `match_outstanding_checks`: GL payment/check not cleared on the bank in the
   timing window.
5. `match_bank_service_charges`: bank fee/charge text and compatible GL amount
   candidates under the configured tolerance.
6. `match_text_similarity`: narration/account-name similarity plus compatible
   amount and date constraints.
7. `match_transposition_errors`: equal digit multiset with transposed amount
   digits and compatible date/direction; uses transposition tolerance.
8. `match_rounding_differences`: compatible direction/date and amount delta
   within rounding tolerance.
9. `match_nsf_returned_items`: NSF, returned, bounced, or dishonored text and
   corresponding ledger candidate criteria.
10. `match_interest_income`: interest-credit text and compatible ledger credit.
11. `match_discounts_and_taxes`: discount/tax narration and a ledger amount
    consistent with the tax/discount adjustment rules.
12–13. `match_bank_side_zero_sum` / `match_ledger_side_zero_sum`: find groups
    on one side whose signed amounts net to zero within the strategy tolerance.
14. `match_aggregated_transactions`: find grouped entries whose total matches
    an opposite-side transaction.
15. `match_many_to_one_aggregation`: find multiple entries on one side summing
    to a single entry on the other side.
16. `match_split_charge`: one GL amount equals half a bank charge; flags the
    likely missing second half for review.
17. `match_base_fee_plus_tax`: one base-fee row and nearby tax/GST row together
    match a ledger amount.

Combination searches are bounded (pool, combination count, node limits) and
use amount/text/date prefilters; a bounded search is not proof that no other
combination exists. Timing and date matching use the configured calendar and
month-boundary widening where applicable. Each accepted record is removed from
both working pools before the next strategy. `run()` returns match records,
remaining pools, ignored metadata, and investigation records.

`fuzzy_matcher(pending_ledger, pending_bank, tolerances=None,
same_side=True)` is the functional wrapper that constructs `FuzzyMatcher` and
calls `run()`. The wrapper does not expose a calendar argument; pass a custom
calendar to `FuzzyMatcher` directly.

`_AmountIndex(pairs)` indexes `(amount, item_index)` pairs; `between(lo, hi)`
returns candidate indices in a range. It only prunes candidates: each strategy
rechecks its full predicate.

## Memory matching — `memory_match.py`

### Pattern record

`RecognizedPattern` is the schema record persisted by memory backends. It stores normalized ledger and bank signatures, the last match phase and adjustment type, and the number of times the pair has been seen.

### Backends

- `MemoryBackend`: protocol; `load() -> dict` reads serialized patterns and
  `save(data)` persists them.
- `InMemoryBackend()`: session-only backend; `load()` returns current data and
  `save(data)` replaces it.
- `JSONFileBackend(path)`: loads/saves a JSON pattern mapping at `path`.
- `SQLAlchemyBackend(session_factory, model_class=None)`: loads/saves using a
  SQLAlchemy session and pattern model; model lookup can use the project's
  default model when omitted.

### `MatchMemory(backend=None)`
Loads normalized patterns from the backend, defaulting to memory-only storage.
`lookup(ledger_text, bank_text)` returns the most specific recognized pattern
when the normalized ledger signature matches and all stored bank-signature
tokens are contained in the current bank text. `is_recognized(...)` is its
boolean form. `record_match(ledger_text, bank_text, match_phase,
adjustment_type=None)` adds or updates a stable signature; inputs without a
usable core token are ignored. `record_matches_from_records(matches, gl_by_id,
bank_by_id, match_phase)` records match dictionaries by resolving their IDs
to source text. `stats()` returns total/recurring pattern counts. `save()`
persists the current patterns through the backend.

`memory_matcher(residual_ledger, residual_bank, memory, same_side, amount_tol)`
returns `(matches, ledger_left, bank_left)`. It accepts a remembered pair only
when signatures match, amounts are within `amount_tol`, and the debit/credit
directions agree with `same_side`; records are consumed one-to-one. Duplicate
record IDs raise `ValueError`.

Internal signature helpers: `_signature_tokens(text)` tokenizes useful
alphanumeric terms; `_normalize_signature(text)` creates a stable normalized
signature; `_pattern_key(ledger_sig, bank_sig)` creates the storage key;
`_has_core(tokens)` checks that a signature has enough identifying content.

## AI matching — `ai_matcher/ai_matcher.py`

### `AIMatcher(config, llm=None, tol=0.50, same_side=True)`
Stores settings, optional LLM, amount tolerance and orientation. `run(result)`
expects `UNRECONCILED_LEDGER`, `UNRECONCILED_BANK`, and optional `MATCHED`;
returns `{"FINAL_RESULT": ...}`. It initializes the shared Ollama client
without a separate generation ping. A failed first request marks AI matching
skipped and leaves unprocessed rows residual.

`batch_match(unreconciled)` performs 1:1 proposals in overlapping date windows.
It evaluates amount/direction/date candidate pairs once across overlapping
windows and sends only records participating in eligible pairs. A proposal
must be a submitted candidate, within `tol`, have compatible direction, pass
the LLM confidence threshold (0.75), and pass the deterministic bouncer.
Unknown IDs, duplicate IDs, low confidence, or failed validation are not
accepted; reviewable proposals are put in the audit queue.

`residual_match(gl_remaining, bk_remaining)` checks bank rows against bounded,
amount-verified subsets of ledger rows. Candidate entries must satisfy the
configured direction and candidate date window; subset sizes and search work
are bounded. Up to two bank rows are grouped when their candidate ledger sets
are disjoint. The LLM selects only from verified combinations; confidence and
sum/direction bouncers apply. Incomplete searches are flagged for audit.

`ai_match(config, result, tol=0.50, same_side=True, llm=None)` is the functional
wrapper. `AIMatcher._initialize_llm()` returns `None` or a client construction
error string; `_require_llm()` returns the initialized client or raises
`RuntimeError`.

## AI utilities — `ai_matcher/ai_utils.py`

- `get_shared_llm(config)`: return the cached Ollama client, creating it from
  `Settings` on the first call; later configs are ignored until reset;
  `reset_shared_llm()` clears that cache; `_prepare_llm(config)` constructs it.
- `_safe_parse_json(output, schema)`: parse model output into a Pydantic schema;
  malformed output returns no parsed value rather than a trusted match.
- `_make_parser(schema)`: create the structured output parser used by prompts.
- `_format_record_for_prompt(record, is_bank)`: render typed fields for model
  input without changing the source record.
- `invoke_with_metrics(chain, payload, phase, details="")`: invoke Ollama and
  log elapsed time plus approximate prompt/response character counts.
- `_get_direction_amount(record)`: return the nonzero amount and its debit/
  credit direction according to record fields.
- `_directions_compatible(gl_direction, bank_direction, same_side)`: apply the
  orientation mapping.
- `_filter_candidates_for_bank(bank, ledger_pool, date_window, tol)`: retain
  entries inside the calendar-day window and whose amount is not greater than
  the bank amount plus tolerance. Direction is checked by the subset helper.
- `_find_candidate_subsets_for_bank(bank, gl_records, date_window, tol,
  same_side)`: bounded subset-sum candidates plus whether search was exhaustive.
- `_passes_1to1_bouncer(gl_item, bk_item, tol, same_side=True)` and
  `_passes_many_to_one_bouncer(gl_items, bk_item, tol, same_side=True)` return
  `(accepted, reason)` after deterministic amount and direction validation.
  They are rejection gates, not match generators.

## Residual reconciliation — `residual_reconciler.py`

### `ResidualReconciler(pending_ledger, pending_bank, config, llm=None, same_side=True, calendar=None)`
Copies the pending records into instance pools and stores config, LLM,
orientation, and calendar. `reconcile_residuals()` takes no parameters and
returns timing matches, split matches, suggested journal entries, a human
review queue, stats, and still-unreconciled records.

Classification checks each item against the opposite pool. Invalid/zero amount
becomes `UNCLASSIFIABLE`. A compatible subset of 2–4 entries within the split
tolerance is labeled `SPLIT_CANDIDATE` first; otherwise an equal amount within
the existence tolerance is `TIMING_CANDIDATE`. With neither signal, a bank
narration containing at least one word of three or more letters becomes
`MISSING_ENTRY_CANDIDATE`; a ledger item gets that label when its best bank
narration similarity reaches 0.60. Remaining rows are `UNCLASSIFIABLE`.
Direction compatibility follows `same_side`.
Timing resolution selects the closest compatible equal-amount candidate up to
45 open days away according to the configured business calendar. Split
resolution searches combinations up to 8 rows within 2.00 amount units; ties
or multiple solutions lower confidence.
For a missing bank row, draft generation asks the LLM for account/narrative
only; debit/credit direction and amount come from the bank row. If the LLM is
unavailable or parsing fails, keyword heuristics suggest Bank Charges,
Interest Received, or Suspense. Drafts are marked `pending_review`.

`reconcile_residuals(unreconciled_ledger, unreconciled_bank, *, config,
same_side=True, llm=None)` is the module-level compatibility wrapper. It
constructs `ResidualReconciler` and calls its no-argument method.

## Calendar — `fuzzy_matcher/business_calendar.py`

`BusinessCalendar(holidays=(), closed_weekdays=(6,), closed_saturdays=(2, 4))` models open days. Weekday numbers use Monday=0 through Sunday=6; Saturday numbers use ordinal-in-month values 1–5.
`india_banks(holidays=())`, `mon_fri(holidays=())`, and `calendar_days()` are
constructors for common calendars. `is_open(date)` checks a day;
`prepare(lo, hi)` precomputes an interval; `lag(a, b)` returns signed open-day
lag; `shift(a, n)` shifts by open days; `date_bounds(a, min_lag, max_lag)`
returns date limits. Closed weekdays, closed Saturdays, and explicitly
configured holidays are excluded. `india_banks()` does not include public or
state holidays automatically; supply those dates through `holidays`.

## Subset search — `fuzzy_matcher/subset_sum.py`

`SubsetSearch` is the result record: `solutions` contains tuples of input
indices, `nodes` measures work, and `exhaustive` indicates whether the
search completed within limits. `find_subsets(amounts, target, tol=0,
min_size=2, max_size=6, max_nodes=200000, max_solutions=500)` searches
integer values for combinations summing within tolerance. It returns all
solutions found up to the cap. `exhaustive=False` means a limit stopped the
search, so absence of a solution is inconclusive. Indices refer to the
original `amounts` order.

## Shared matching helpers — `utils/helper.py`

- `extract_utr(text)`: return a recognized payment reference/UTR or `None`.
- `_get_iso_date_str(record, is_bank=False)`, `_to_date(value)`: normalize
  record date fields and parse ISO-like dates.
- `_to_paise(amount)`, `_amounts_equal(a, b, tol)`: currency conversion and
  absolute tolerance comparison.
- `_days_between(ledger_date_iso, bank_date_iso)`,
  `_crosses_month_boundary(date_iso_a, date_iso_b)`: calendar-day distance and
  month-boundary checks.
- `_normalize_text(text)`, `_tokens(text)`, `_acronym(text)`,
  `text_similarity(account_name, narration)`: normalize and score text; score
  is a heuristic signal, not sufficient on its own to authorize all match
  types.
- `_digit_multiset(amount)`, `is_transposition(amount_a, amount_b, tol)`: test
  whether differing amounts can be explained by digit transposition.
- `_safe_amount(obj, attr, default=0.0)`, `_safe_str(obj, attr, default="")`,
  `_get_amt(record)`, `_get_date(record)`: tolerant typed-field accessors.
- `_closest_candidates(valid, target, max_pool, counterpart_text="",
text_of=None)`: rank `(record, amount)` pairs using distance from an equal-share
amount (target divided across 2–6 items) and optional text similarity, then
keep at most `max_pool`. It only prunes; callers must still apply the full
matching predicate.

## Confidence — `utils/confidence.py`

`normalize_confidence(raw)` clamps numeric values to `[0,1]` and maps
High/Medium/Low strings to 0.90/0.60/0.30. Missing or unrecognized input maps
to 0.0. `confidence_bucket(numeric)` returns High at 0.85 or above, Medium at
0.50 or above, and Low below 0.50. `annotate_match_confidence(match)` returns
a copy with `confidence_numeric` set; it uses an explicit numeric `confidence`,
then `confidence_score`, and assumes 1.0 only when both fields are absent.

## Package files and service files

`fuzzy_matcher/__init__.py` re-exports `FuzzyMatcher`, `Relationship`,
`run_fuzzy_matcher`, `BusinessCalendar`, and `find_subsets`. The AI package
exports `AIMatcher` and the `ai_match` compatibility wrapper.

`tasks.py` exposes the Celery `run_matcher(self, payload)` task; the payload
is validated as a matcher-run message, and the task returns a status mapping
after saving the result and queueing the next service stage. `celery_app.py`
creates the Celery application. `test.py` is a standalone residual
reconciliation experiment and is not imported by the production pipeline.
