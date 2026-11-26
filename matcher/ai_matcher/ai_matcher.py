from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple

from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate

from schema import BankStatement, LedgerFormat, AIWindowOutput, AIManyToOneOutput
from app.shared.settings import Settings
from ..base import Matcher
from .ai_utils import (
    get_shared_llm,
    reset_shared_llm,
    _safe_parse_json,
    _passes_1to1_bouncer,
    _passes_many_to_one_bouncer,
    _find_candidate_subsets_for_bank,
    _format_record_for_prompt,
    invoke_with_metrics,
)

CONFIDENCE_THRESHOLD: float = 0.75
CANDIDATE_DATE_WINDOW_DAYS: int = 10
WINDOW_OVERLAP_DAYS: int = 7
BATCH_WINDOW_DAYS: int = 30
MAX_RECORDS_PER_WINDOW: int = 20
MAX_RESIDUAL_BANKS_PER_CALL: int = 2


class AIMatcher(Matcher):
    """
    AI-assisted reconciliation: a 1-to-1 batch matcher over sliding time
    windows, followed by a many-to-1 residual matcher for whatever's left.

    Bundling `llm`, `tol`, and `same_side` on the instance is the main thing
    this buys over the free-function version — every method below stops
    needing to thread those three values through as parameters, and the
    "is this the shared LLM or one I was handed" bookkeeping lives in one
    place instead of being reasoned about at every call site.
    """

    def __init__(
        self,
        config: Settings,
        llm: Optional[ChatOllama] = None,
        tol: float = 0.50,
        same_side: bool = True,
        pending_ledger: Optional[List[LedgerFormat]] = None,
        pending_bank: Optional[List[BankStatement]] = None,
    ) -> None:
        super().__init__(
            pending_ledger=pending_ledger,
            pending_bank=pending_bank,
            config=config,
            llm=llm,
            same_side=same_side,
            tolerances={"AI_MATCHER": tol},
        )
        self._owns_shared_llm = llm is None
        self._config = config
        self.tol = tol
        self._llm_error: Optional[Exception] = None


    def _initialize_llm(self) -> Optional[str]:
        """Initialize the shared client without spending a generation on a ping."""
        try:
            if self.llm is None:
                self.llm = get_shared_llm(self._config)
            return None
        
        except Exception as exc:
            if self._owns_shared_llm:
                reset_shared_llm()
            return str(exc)

    def _invoke_llm(
        self,
        chain: Any,
        payload: Dict[str, Any],
        *,
        phase: str,
        details: str = "",
    ) -> Any:
        try:
            return invoke_with_metrics(chain, payload, phase=phase, details=details)
        except Exception as exc:
            self._llm_error = exc
            raise

    def _require_llm(self) -> ChatOllama:
        if self.llm is None:
            raise RuntimeError("LLM is not initialized")
        return self.llm


    # Phase A — 1-to-1 batch matcher, sliding time windows
    _BATCH_PROMPT = ChatPromptTemplate.from_messages([
        ("system",
         "You are a financial reconciliation expert.\n"
         "Find 1-to-1 matches between ledger records and bank statement rows "
         "within the provided time window.\n"
         "MATCH CRITERIA:\n"
         "  - Amounts match within ±{tol} (absolute difference).\n"
         "  - Dates fall inside the stated time window.\n"
         "  - Narration/account names share semantic meaning "
         "(abbreviations, synonyms, reversed word order are acceptable).\n"
         "CONFIDENCE: 0.0 = no evidence  |  1.0 = certain. "
         "Only propose matches you rate ≥ {confidence_threshold}.\n"
         "Only return ledger/bank pairs listed in CANDIDATE PAIRS.\n"
         "OUTPUT FORMAT: Strict JSON ONLY. No markdown. No prose outside schema.\n"
         'Schema: {{"matches": [{{"ledger_id": "string", "bank_id": number, '
         '"confidence": 0.0-1.0}}]}}'),
        ("human",
         "TOLERANCE: ±{tol}\n"
         "TIME WINDOW: {start_date} to {end_date}\n\n"
         "LEDGER ENTRIES:\n{ledger_list}\n\n"
         "BANK ENTRIES:\n{bank_list}\n\n"
         "CANDIDATE PAIRS (ledger_id -> bank_ids):\n{candidate_pairs}"),
    ])

    def batch_match(
        self,
        unreconciled: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[LedgerFormat], List[BankStatement]]:

        gl_by_id: Dict[str, LedgerFormat] = {}
        bk_by_id: Dict[str, BankStatement] = {}
        
        for record in unreconciled["UNRECONCILED_LEDGER"]:
            ledger_id = str(record.ledger_id)
            if ledger_id in gl_by_id:
                raise ValueError(f"Duplicate ledger ID in AI matching pool: {ledger_id}")
            gl_by_id[ledger_id] = record
            
        for record in unreconciled["UNRECONCILED_BANK"]:
            bank_id = str(record.row_index)
            if bank_id in bk_by_id:
                raise ValueError(f"Duplicate bank ID in AI matching pool: {bank_id}")
            bk_by_id[bank_id] = record
            
        used_ledger: set[str] = set()
        used_bank: set[str] = set()
        evaluated_pairs: set[Tuple[str, str]] = set()
        llm_calls = 0
        ai_matches: List[dict] = []
        audit_queue: List[dict] = []
        gl_remaining = list(gl_by_id.values())
        bk_remaining = list(bk_by_id.values())

        empty = {
            "AI_MATCHES": ai_matches,
            "AUDIT_QUEUE": audit_queue,
            "MATCHED": unreconciled["MATCHED"],
        }

        if not gl_remaining or not bk_remaining:
            return empty, gl_remaining, bk_remaining

        dates = sorted(
            [r.date for r in bk_remaining if r.date] +
            [r.transaction_date for r in gl_remaining if r.transaction_date]
        )
        if not dates:
            return empty, gl_remaining, bk_remaining

        window_size = timedelta(days=BATCH_WINDOW_DAYS)
        window_step = window_size - timedelta(days=WINDOW_OVERLAP_DAYS)
        current_start = datetime.strptime(dates[0][:10], "%Y-%m-%d")
        final_end = datetime.strptime(dates[-1][:10], "%Y-%m-%d")

        chain = self._BATCH_PROMPT | self._require_llm()

        while current_start <= final_end:
            window_end = current_start + window_size
            window_mid = current_start + window_size / 2

            gl_chunk = sorted(
                (
                    r for ledger_id, r in gl_by_id.items()
                    if ledger_id not in used_ledger
                    if r.transaction_date
                    and current_start.date()
                    <= datetime.strptime(r.transaction_date[:10], "%Y-%m-%d").date()
                    <= window_end.date()
                ),
                key=lambda r: abs(
                    (datetime.strptime(r.transaction_date[:10], "%Y-%m-%d") - window_mid).days
                ),
            )
            bk_chunk = sorted(
                (
                    r for bank_id, r in bk_by_id.items()
                    if bank_id not in used_bank
                    if r.date
                    and current_start.date()
                    <= datetime.strptime(r.date[:10], "%Y-%m-%d").date()
                    <= window_end.date()
                ),
                key=lambda r: abs(
                    (datetime.strptime(r.date[:10], "%Y-%m-%d") - window_mid).days
                ),
            )

            for gl_offset in range(0, len(gl_chunk), MAX_RECORDS_PER_WINDOW):
                gl_batch = [
                    record
                    for record in gl_chunk[gl_offset:gl_offset + MAX_RECORDS_PER_WINDOW]
                    if str(record.ledger_id) not in used_ledger
                ]
                for bk_offset in range(0, len(bk_chunk), MAX_RECORDS_PER_WINDOW):
                    bk_batch = [
                        record
                        for record in bk_chunk[bk_offset:bk_offset + MAX_RECORDS_PER_WINDOW]
                        if str(record.row_index) not in used_bank
                    ]
                    if not gl_batch or not bk_batch:
                        continue

                    candidate_pairs: List[Tuple[str, str]] = []
                    for gl_item in gl_batch:
                        gl_id = str(gl_item.ledger_id)
                        gl_date = datetime.strptime(
                            gl_item.transaction_date[:10], "%Y-%m-%d"
                        ).date() if gl_item.transaction_date else None
                        
                        for bk_item in bk_batch:
                            bk_id = str(bk_item.row_index)
                            pair = (gl_id, bk_id)
                            if pair in evaluated_pairs:
                                continue
                            
                            if not _passes_1to1_bouncer(
                                gl_item, bk_item, self.tol, self.same_side
                            )[0]:
                                continue
                            
                            bk_date = datetime.strptime(
                                bk_item.date[:10], "%Y-%m-%d"
                            ).date() if bk_item.date else None
                            
                            if (
                                gl_date is not None
                                and bk_date is not None
                                and abs((gl_date - bk_date).days) > BATCH_WINDOW_DAYS
                            ):
                                continue
                            
                            candidate_pairs.append(pair)

                    if not candidate_pairs:
                        continue

                    candidate_pair_set = set(candidate_pairs)
                    candidate_ledger_ids = {pair[0] for pair in candidate_pairs}
                    candidate_bank_ids = {pair[1] for pair in candidate_pairs}
                    
                    gl_batch = [
                        record for record in gl_batch
                        if str(record.ledger_id) in candidate_ledger_ids
                    ]
                    bk_batch = [
                        record for record in bk_batch
                        if str(record.row_index) in candidate_bank_ids
                    ]

                    gl_ctx = [
                        _format_record_for_prompt(r.model_dump(), False)
                        for r in gl_batch
                    ]
                    bk_ctx = [
                        _format_record_for_prompt(r.model_dump(), True)
                        for r in bk_batch
                    ]
                    pairs_by_ledger: Dict[str, List[str]] = {}
                    for ledger_id, bank_id in candidate_pairs:
                        pairs_by_ledger.setdefault(ledger_id, []).append(bank_id)
                        
                    candidate_pair_text = "\n".join(
                        f"{ledger_id} -> {', '.join(sorted(bank_ids))}"
                        for ledger_id, bank_ids in sorted(pairs_by_ledger.items())
                    )

                    payload = {
                        "tol": f"{self.tol:.2f}",
                        "confidence_threshold": str(CONFIDENCE_THRESHOLD),
                        "start_date": current_start.strftime("%Y-%m-%d"),
                        "end_date": window_end.strftime("%Y-%m-%d"),
                        "ledger_list": "\n".join(gl_ctx),
                        "bank_list": "\n".join(bk_ctx),
                        "candidate_pairs": candidate_pair_text,
                    }
                    raw = self._invoke_llm(
                        chain,
                        payload,
                        phase="1to1",
                        details=(
                            f"window={current_start:%Y-%m-%d}:{window_end:%Y-%m-%d} "
                            f"ledger={len(gl_batch)} bank={len(bk_batch)} "
                            f"candidate_pairs={len(candidate_pairs)}"
                        ),
                    )
                    llm_calls += 1
                    evaluated_pairs.update(candidate_pair_set)
                    result = _safe_parse_json(raw, AIWindowOutput)

                    if result and hasattr(result, "matches"):
                        for m in result.matches:
                            ledger_id = str(m.ledger_id)
                            gl_item = gl_by_id.get(ledger_id)
                            bank_id = str(m.bank_id)
                            bk_item = bk_by_id.get(bank_id)

                            if (
                                gl_item is None
                                or bk_item is None
                                or ledger_id in used_ledger
                                or bank_id in used_bank
                            ):
                                print(f" GHOST REFERENCE: Ledger '{m.ledger_id}' or "
                                      f"Bank '{m.bank_id}' not found in pools - skipped.")
                                continue

                            if (ledger_id, bank_id) not in candidate_pair_set:
                                print(
                                    f" REJECTED HALLUCINATION (1-to-1): "
                                    f"[Ledger {ledger_id}] ↔ [Bank {bank_id}] "
                                    "was not an eligible candidate pair."
                                )
                                continue

                            if m.confidence < CONFIDENCE_THRESHOLD:
                                audit_queue.append({
                                    **m.model_dump(),
                                    "flag": "LOW_CONFIDENCE",
                                    "action": "Route to human audit - confidence below threshold.",
                                })
                                print(f" LOW CONFIDENCE ({m.confidence:.0%}): "
                                      f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] → audit queue.")
                                continue

                            passed, reason = _passes_1to1_bouncer(
                                gl_item, bk_item, self.tol, self.same_side
                            )
                            if not passed:
                                print(f" REJECTED HALLUCINATION (1-to-1): "
                                      f"[Ledger {m.ledger_id}] ↔ [Bank {m.bank_id}] - {reason}")
                                continue

                            ai_matches.append(m.model_dump())
                            used_ledger.add(ledger_id)
                            used_bank.add(bank_id)

            current_start += window_step

        print(
            f"[ai] 1to1 complete calls={llm_calls} evaluated_pairs={len(evaluated_pairs)} "
            f"accepted={len(ai_matches)} audited={len(audit_queue)}"
        )

        gl_remaining = [
            record for ledger_id, record in gl_by_id.items()
            if ledger_id not in used_ledger
        ]
        bk_remaining = [
            record for bank_id, record in bk_by_id.items()
            if bank_id not in used_bank
        ]
        return {
            "AI_MATCHES": ai_matches,
            "AUDIT_QUEUE": audit_queue,
            "MATCHED": unreconciled["MATCHED"],
        }, gl_remaining, bk_remaining


    # Phase B — many-to-1 residual matcher
    _RESIDUAL_PROMPT = ChatPromptTemplate.from_messages([
        ("system",
         "You are a financial reconciliation expert.\n"
         "Choose the best semantic match from the precomputed combinations below.\n"
         "Every combination has already passed amount, direction, and date checks.\n"
         "For each bank row, select at most one listed combination.\n"
         "Do not reuse a ledger ID across different bank rows.\n"
         "Do not add, remove, or substitute IDs. Omit rows with no plausible match.\n"
         "Only propose matches you rate ≥ {confidence_threshold} confidence.\n"
         "OUTPUT FORMAT: Strict JSON ONLY. No markdown.\n"
         'Schema: {{"matches": [{{"bank_id": number, '
         '"ledger_ids": [{{"ledger_id": "string"}}], '
         '"confidence": 0.0-1.0}}]}}'),
        ("human",
         "TOLERANCE: ±{tol}\n\n"
         "BANK ENTRIES AND THEIR VALID AMOUNT-VERIFIED COMBINATIONS:\n"
         "{bank_options}"),
    ])

    def residual_match(
        self,
        gl_remaining: List[LedgerFormat],
        bk_remaining: List[BankStatement],
    ) -> Tuple[Dict[str, Any], List[LedgerFormat]]:
        ai_many_matches: List[dict] = []
        audit_queue: List[dict] = []
        gl_by_id: Dict[str, LedgerFormat] = {}
        
        for record in gl_remaining:
            ledger_id = str(record.ledger_id)
            if ledger_id in gl_by_id:
                raise ValueError(f"Duplicate ledger ID in AI residual pool: {ledger_id}")
            
            gl_by_id[ledger_id] = record

        used_ledger: set[str] = set()
        gl_left = list(gl_by_id.values())
        
        if not bk_remaining:
            return {
                "AI_MANY_MATCHES": ai_many_matches, 
                "AUDIT_QUEUE": audit_queue
            }, gl_left
        if self._llm_error is not None:
            return {
                "AI_MANY_MATCHES": ai_many_matches, 
                "AUDIT_QUEUE": audit_queue
            }, gl_left

        chain = self._RESIDUAL_PROMPT | self._require_llm()
        calls = 0
        eligible_bank_ids: set[str] = set()

        def find_job(bank: BankStatement) -> Optional[dict]:
            available = [
                record for ledger_id, record in gl_by_id.items()
                if ledger_id not in used_ledger
            ]
            
            subsets, exhaustive = _find_candidate_subsets_for_bank(
                bank,
                available,
                date_window=CANDIDATE_DATE_WINDOW_DAYS,
                tol=self.tol,
                same_side=self.same_side,
            )
            
            if not exhaustive:
                audit_queue.append({
                    "bank_id": bank.row_index,
                    "flag": "SUBSET_SEARCH_INCOMPLETE",
                    "action": (
                        "Route to human audit - bounded subset search found "
                        "too many possibilities to safely select a match."
                    ),
                })
                return None
            
            if not subsets:
                return None

            valid_options = {}
            for subset in subsets:
                ids = tuple(sorted(str(record.ledger_id) for record in subset))
                valid_options[ids] = subset
            eligible_bank_ids.add(str(bank.row_index))
            
            return {
                "bank": bank,
                "options": valid_options,
                "ledger_ids": {
                    ledger_id 
                    for ids in valid_options 
                    for ledger_id in ids
                },
            }

        def invoke_group(jobs: List[dict]) -> None:
            nonlocal calls
            if not jobs:
                return

            blocks = []
            for job in jobs:
                bank = job["bank"]
                options = []
                for index, (subset_ids, subset) in enumerate(job["options"].items(), start=1):
                    entries = "\n".join(
                        _format_record_for_prompt(record.model_dump(), False)
                        for record in subset
                    )
                    options.append(
                        f"Option {index} (ledger_ids: {', '.join(subset_ids)}):\n{entries}"
                    )
                    
                blocks.append(
                    f"BANK ENTRY:\n{_format_record_for_prompt(bank.model_dump(), True)}\n"
                    f"VALID COMBINATIONS:\n" + "\n\n".join(options)
                )

            payload = {
                "tol": f"{self.tol:.2f}",
                "confidence_threshold": str(CONFIDENCE_THRESHOLD),
                "bank_options": "\n\n---\n\n".join(blocks),
            }
            try:
                raw = self._invoke_llm(
                    chain,
                    payload,
                    phase="many_to_one",
                    details=(
                        f"bank_rows={len(jobs)} "
                        f"options={sum(len(job['options']) for job in jobs)}"
                    ),
                )
            except Exception:
                return
            
            calls += 1
            result = _safe_parse_json(raw, AIManyToOneOutput)
            if not result or not hasattr(result, "matches"):
                print("[ai] phase=many_to_one returned invalid JSON; leaving rows unmatched")
                return

            jobs_by_bank = {str(job["bank"].row_index): job for job in jobs}
            responded_banks: set[str] = set()
            for match in result.matches:
                bank_id = str(match.bank_id)
                job = jobs_by_bank.get(bank_id)
                
                if job is None or bank_id in responded_banks:
                    print(f"[ai] rejected many-to-one response for unknown/duplicate bank {bank_id}")
                    continue
                responded_banks.add(bank_id)

                bank = job["bank"]
                matched_ids = tuple(sorted(str(item.ledger_id) for item in match.ledger_ids))
                if matched_ids not in job["options"]:
                    print(
                        f"[ai] rejected many-to-one bank={bank_id}: "
                        "ledger IDs were not a verified subset"
                    )
                    continue

                if match.confidence < CONFIDENCE_THRESHOLD:
                    audit_queue.append({
                        **match.model_dump(),
                        "flag": "LOW_CONFIDENCE",
                        "action": "Route to human audit - confidence below threshold.",
                    })
                    continue

                if any(ledger_id in used_ledger for ledger_id in matched_ids):
                    print(f"[ai] rejected many-to-one bank={bank_id}: ledger row already used")
                    continue
                
                gl_items = [gl_by_id[ledger_id] for ledger_id in matched_ids]
                passed, reason = _passes_many_to_one_bouncer(
                    gl_items, bank, self.tol, self.same_side
                )
                
                if not passed:
                    print(f"[ai] rejected many-to-one bank={bank_id}: {reason}")
                    continue

                ai_many_matches.append(match.model_dump())
                used_ledger.update(matched_ids)

        jobs: List[dict] = []
        group_ledger_ids: set[str] = set()
        for bank in bk_remaining:
            if self._llm_error is not None:
                break
            
            job = find_job(bank)
            if job is None:
                continue
            
            conflicts = bool(group_ledger_ids & job["ledger_ids"])
            if jobs and (
                len(jobs) >= MAX_RESIDUAL_BANKS_PER_CALL or conflicts
            ):
                invoke_group(jobs)
                jobs = []
                group_ledger_ids = set()
                if self._llm_error is not None:
                    break
                
                # Rebuild after earlier matches may have consumed ledger rows.
                job = find_job(bank)
                if job is None:
                    continue
                
            jobs.append(job)
            group_ledger_ids.update(job["ledger_ids"])

        if self._llm_error is None:
            invoke_group(jobs)

        gl_left = [
            record for ledger_id, record in gl_by_id.items()
            if ledger_id not in used_ledger
        ]
        print(
            f"[ai] many_to_one complete calls={calls} eligible_banks={len(eligible_bank_ids)} "
            f"accepted={len(ai_many_matches)} audited={len(audit_queue)}"
        )
        return {"AI_MANY_MATCHES": ai_many_matches, "AUDIT_QUEUE": audit_queue}, gl_left


    # Orchestration
    def match(
        self, 
        result: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Runs both phases against `result` (expects UNRECONCILED_LEDGER,
        UNRECONCILED_BANK, MATCHED keys) and returns a single FINAL_RESULT
        dict, mirroring the shape the rest of the pipeline already expects.
        """
        if result is None:
            result = {
                "UNRECONCILED_LEDGER": self.ledger_pool,
                "UNRECONCILED_BANK": self.bank_pool,
                "MATCHED": [],
            }
        gl_input = result.get("UNRECONCILED_LEDGER", [])
        bk_input = result.get("UNRECONCILED_BANK", [])

        conn_err = self._initialize_llm()
        if conn_err is not None:
            print(
                f"\n AI LAYER UNAVAILABLE: {conn_err}\n"
                "   Skipping AI matching. All remaining records are "
                "returned as UNRECONCILED for manual review.\n"
            )
            return {
                "FINAL_RESULT": {
                    "AI_MATCHES": [],
                    "AI_MANY_MATCHES": [],
                    "AUDIT_QUEUE": [],
                    "FINAL_RESIDUALS_LEDGER": gl_input,
                    "FINAL_RESIDUALS_BANK": bk_input,
                    "MATCHED": result.get("MATCHED", []),
                    "ai_skipped": True,
                    "ai_skip_reason": conn_err,
                }
            }

        print(" AI Time-Window Batch Matcher (1-to-1 Semantic)...")
        try:
            batch_res, gl_rem, bk_rem = self.batch_match(result)
        except Exception as e:
            print(f" AI Batch Matcher crashed: {e}. Falling through to residual matcher.")
            batch_res = {
                "AI_MATCHES": [], 
                "AUDIT_QUEUE": [], 
                "MATCHED": result.get("MATCHED", [])
            }
            gl_rem, bk_rem = gl_input, bk_input

        if not bk_rem:
            print(" All remaining records reconciled via AI Batch.")
            return {
                "FINAL_RESULT": {
                    **batch_res,
                    "AI_MANY_MATCHES": [],
                    "FINAL_RESIDUALS_LEDGER": gl_rem,
                    "FINAL_RESIDUALS_BANK": [],
                }
            }

        print(" AI One-to-Many Residual Matcher...")
        try:
            residual_res, final_gl_left = self.residual_match(gl_rem, bk_rem)
        except Exception as e:
            print(f" AI Residual Matcher crashed: {e}. Returning pools as unreconciled.")
            residual_res = {
                "AI_MANY_MATCHES": [], 
                "AUDIT_QUEUE": []
            }
            final_gl_left = gl_rem

        combined_audit = batch_res.get("AUDIT_QUEUE", []) + residual_res.get("AUDIT_QUEUE", [])

        return {
            "FINAL_RESULT": {
                "AI_MATCHES": batch_res.get("AI_MATCHES", []),
                "AI_MANY_MATCHES": residual_res.get("AI_MANY_MATCHES", []),
                "AUDIT_QUEUE": combined_audit,
                "MATCHED": batch_res.get("MATCHED", []),
                "FINAL_RESIDUALS_LEDGER": final_gl_left,
                "FINAL_RESIDUALS_BANK": bk_rem,
                "ai_skipped": self._llm_error is not None,
                "ai_skip_reason": (
                    str(self._llm_error) if self._llm_error is not None else None
                ),
            }
        }

    def run(self, result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Backward-compatible alias for callers using the prior API."""
        return self.match(result)
    
