from __future__ import annotations

import re
import itertools
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama

from shared.settings import Settings
from schema import BankStatement, LedgerFormat, DraftAccountSuggestion
from .base import Matcher

from .utils.helper import (
    MAX_COMBINATION_POOL_SIZE,

    _amounts_equal,
    _safe_str,
    _to_date,
    _closest_candidates,

    text_similarity,
)
from .ai_matcher.ai_utils import (
    _directions_compatible,
    _get_direction_amount,
    _safe_parse_json,
    get_shared_llm
)
from .fuzzy_matcher import BusinessCalendar, find_subsets


class ResidualReconciler(Matcher):

    TIMING_WINDOW_DAYS: int = 45
    SPLIT_TOLERANCE: float = 2.0
    SPLIT_MAX_COMBINATION_SIZE: int = 8
    SPLIT_POOL_PREFILTER_THRESHOLD: int = 50
    SPLIT_PROBE_MAX_SIZE: int = 4
    NARRATION_SIMILARITY_THRESHOLD: float = 0.6
    EXISTENCE_AMOUNT_TOLERANCE: float = 1.0

    CLASSIFY_TIMING = "TIMING_CANDIDATE"
    CLASSIFY_SPLIT = "SPLIT_CANDIDATE"
    CLASSIFY_MISSING = "MISSING_ENTRY_CANDIDATE"
    CLASSIFY_UNCLASSIFIABLE = "UNCLASSIFIABLE"

    _WORD_RE = re.compile(r"[a-zA-Z]{3,}")

    def __init__(
        self,
        pending_ledger: List[LedgerFormat],
        pending_bank: List[BankStatement],
        config: Settings,
        llm: Optional[ChatOllama] = None,
        same_side: bool = True,
        calendar: Optional[BusinessCalendar] = None,
        enable_llm: bool = True,
    ) -> None:
        super().__init__(
            pending_ledger=pending_ledger,
            pending_bank=pending_bank,
            config=config,
            llm=llm,
            same_side=same_side,
            calendar=calendar,
            enable_llm=enable_llm,
        )
        self.residual_matches: List[Dict[str, Any]] = []
        self.llm = llm if llm is not None else (
            get_shared_llm(config=self.config) if enable_llm else None
        )
        
        self._cal: BusinessCalendar = self.calendar
        self._prepare_calendar()


    def _prepare_calendar(self) -> None:
        ds = [_to_date(_safe_str(g, "transaction_date")) for g in self.ledger_pool]
        ds += [_to_date(_safe_str(b, "date")) for b in self.bank_pool]
        ds = [d for d in ds if d is not None]
        if ds:
            self._cal.prepare(min(ds), max(ds))

    @staticmethod
    def _identity_remove(pool: List[Any], item: Any) -> None:
        for i, p in enumerate(pool):
            if p is item:
                del pool[i]
                return

    @staticmethod
    def _identity_contains(pool: list[Any], item: Any) -> bool:
        return any(p is item for p in pool)

    @staticmethod
    def _narration_is_draftable(narration: str) -> bool:
        return len(ResidualReconciler._WORD_RE.findall(narration or "")) >= 1

    @staticmethod
    def _cheap_subset_probe(
        target: float,
        pool: List[Tuple[Any, float]],
        tol: float,
        max_size: int = 4,
        text_of=None,
        target_text: str = "",
    ) -> bool:
        if target <= 0 or not pool:
            return False

        pool = _closest_candidates(
            valid=pool,
            target=target,
            max_pool=MAX_COMBINATION_POOL_SIZE,
            counterpart_text=target_text,
            text_of=text_of,
        )

        cap = min(max_size, len(pool)) + 1
        for r in range(2, cap):
            for combo in itertools.combinations(pool, r):
                if _amounts_equal(target, sum(x[1] for x in combo), tol):
                    return True
        return False


    def _classify_one(
        self,
        item: Any,
        is_bank: bool,
        counterpart_pool: List[Any],
        default_tol: float,
    ) -> Dict[str, Any]:
        amt, direction = _get_direction_amount(item)
        text = _safe_str(item, "narration") if is_bank else _safe_str(item, "account_name")

        if amt <= 0:
            return {
                "label": ResidualReconciler.CLASSIFY_UNCLASSIFIABLE, 
                "reason": "zero or invalid amount"
            }

        comparable: List[Tuple[Any, float]] = []
        for cp in counterpart_pool:
            cp_amt, cp_dir = _get_direction_amount(cp)

            if cp_amt <= 0 or not _directions_compatible(
                cp_dir if not is_bank else direction,
                direction if not is_bank else cp_dir,
                self.same_side,
            ):
                continue
            comparable.append((cp, cp_amt))

        existence_hit = next(
            (cp for cp, cp_amt in comparable if _amounts_equal(amt, cp_amt, default_tol)),
            None,
        )

        def cp_text(cp: Any) -> str:
            return _safe_str(cp, "account_name") if is_bank else _safe_str(cp, "narration")

        best_sim, best_sim_cp = 0.0, None
        if not is_bank:
            for cp, _ in comparable:
                sim = text_similarity(cp_text(cp), text)
                if sim > best_sim:
                    best_sim, best_sim_cp = sim, cp

        probe_pool = [
            (cp, cp_amt) 
            for cp, cp_amt in comparable 
            if cp is not existence_hit
        ]
        split_hit = ResidualReconciler._cheap_subset_probe(
            amt,
            probe_pool,
            ResidualReconciler.SPLIT_TOLERANCE,
            text_of=cp_text,
            target_text=text,
        )

        evidence = {
            "amount": amt,
            "direction": direction,
            "existence_signal": existence_hit is not None,
            "split_signal": split_hit,
            "narration_signal": best_sim >= ResidualReconciler.NARRATION_SIMILARITY_THRESHOLD,
            "best_narration_similarity": round(best_sim, 3),
            "best_narration_match": (
                (_safe_str(best_sim_cp, "ledger_id") if is_bank else str(getattr(best_sim_cp, "row_index", None)))
                if best_sim_cp is not None else None
            ),
        }

        if split_hit:
            label = ResidualReconciler.CLASSIFY_SPLIT
        elif existence_hit is not None:
            label = ResidualReconciler.CLASSIFY_TIMING
        elif is_bank and ResidualReconciler._narration_is_draftable(text):
            label = ResidualReconciler.CLASSIFY_MISSING
        elif not is_bank and best_sim >= ResidualReconciler.NARRATION_SIMILARITY_THRESHOLD:
            label = ResidualReconciler.CLASSIFY_MISSING
        else:
            label = ResidualReconciler.CLASSIFY_UNCLASSIFIABLE

        return {
            "label": label, 
            "evidence": evidence
        }


    def classify_residuals(
        self,
        default_tol: float = 1.0,
    ) -> Dict[str, List[Dict[str, Any]]]:
        bank_out = []
        for b in self.bank_pool:
            c = self._classify_one(
                item=b, 
                is_bank=True, 
                counterpart_pool=self.ledger_pool, 
                default_tol=default_tol
            )
            bank_out.append({"item": b, **c})

        ledger_out = []
        for gl in self.ledger_pool:
            c = self._classify_one(
                item=gl, 
                is_bank=False, 
                counterpart_pool=self.bank_pool, 
                default_tol=default_tol
            )
            ledger_out.append({"item": gl, **c})

        return {
            "bank": bank_out, 
            "ledger": ledger_out
        }


    def _resolve_timing_candidates(
        self,
        timing_bank: List[Dict[str, Any]],
        timing_ledger: List[Dict[str, Any]],
        tol: float = 1.0,
    ) -> Tuple[List[dict], List[Dict[str, Any]], List[Dict[str, Any]]]:
        bank_pool = list(timing_bank)
        ledger_pool = list(timing_ledger)
        matches: List[dict] = []

        for gl_entry in list(ledger_pool):
            gl = gl_entry["item"]
            gl_amt, gl_dir = _get_direction_amount(gl)
            gl_date = _safe_str(gl, "transaction_date")
            if gl_amt <= 0 or not gl_date:
                continue

            gl_dt = _to_date(gl_date)
            candidates = []
            for bank_entry in bank_pool:
                bank = bank_entry["item"]
                bk_amt, bk_dir = _get_direction_amount(bank)
                bk_date = _safe_str(bank, "date")
                if bk_amt <= 0 or not bk_date:
                    continue
                
                if not _amounts_equal(gl_amt, bk_amt, tol):
                    continue
                if not _directions_compatible(gl_dir, bk_dir, self.same_side):
                    continue
                bk_dt = _to_date(bk_date)
                if gl_dt is None or bk_dt is None:
                    continue
                diff = self._cal.lag(gl_dt, bk_dt)
                if not (0 <= abs(diff) <= ResidualReconciler.TIMING_WINDOW_DAYS):
                    continue
                candidates.append((bank_entry, diff))

            if not candidates:
                continue

            candidates.sort(key=lambda x: abs(x[1]))
            bank_entry, diff = candidates[0]
            ambiguous = len(candidates) > 1
            n_candidates = len(candidates)

            bank = bank_entry["item"]
            matches.append({
                "ledger_id": _safe_str(gl, "ledger_id"),
                "bank_id": getattr(bank, "row_index", None),
                "amount": gl_amt,
                "date_gap_days": diff,
                "adjustment_type": "Timing Difference (wide window)",
                "confidence_score": "Medium" if ambiguous else "High",
                "ambiguous": ambiguous,
                "details": (
                    f"Exact amount ({gl_amt:.2f}) matched outside the standard "
                    f"window; {diff} day(s) apart, within the {ResidualReconciler.TIMING_WINDOW_DAYS}-day "
                    f"residual-pool timing pass." + (
                        f" {n_candidates} candidates matched the same amount and "
                        "direction in this window - closest date was picked, but "
                        "flagged for review rather than auto-accepted." if ambiguous else ""
                    )
                ),
                "match_phase": "residual_timing",
            })
            ResidualReconciler._identity_remove(ledger_pool, gl_entry)
            ResidualReconciler._identity_remove(bank_pool, bank_entry)

        return matches, ledger_pool, bank_pool

    @staticmethod
    def _subset_sum_search(
        target: float,
        pool: List[Tuple[Any, float]],
        tol: float,
        max_size: int,
        text_of=None,
        target_text: str = "",
    ) -> Tuple[Optional[Tuple[Tuple[Any, float], ...]], bool]:
        if not pool:
            return None, False

        if len(pool) < 2:
            return None, False

        search = find_subsets(
            [int(round(amount * 100)) for _, amount in pool],
            int(round(target * 100)),
            int(round(tol * 100)),
            min_size=2,
            max_size=min(max_size, len(pool)),
            max_nodes=200_000,
            max_solutions=500,
        )

        if not search.solutions:
            return None, False

        best_combo: Optional[Tuple[Tuple[Any, float], ...]] = None
        best_score = float("-inf")
        ambiguous = len(search.solutions) > 1

        for indexes in search.solutions:
            combo = tuple(pool[i] for i in indexes)
            if text_of is None:
                score = 1.0
            else:
                sims = [text_similarity(target_text, text_of(item)) for item, _ in combo]
                score = sum(sims) / len(sims) if sims else 0.0

            if best_combo is None or score > best_score + 1e-12:
                best_combo = combo
                best_score = score
            elif abs(score - best_score) <= 1e-12:
                ambiguous = True

        return best_combo, ambiguous


    def _resolve_split_candidates(
        self,
        split_bank_targets: List[Dict[str, Any]],
        split_ledger_targets: List[Dict[str, Any]],
        ledger_candidate_pool: List[Dict[str, Any]],
        bank_candidate_pool: List[Dict[str, Any]],
    ) -> Tuple[List[dict], List[Dict[str, Any]], List[Dict[str, Any]]]:
        bank_pool = list(bank_candidate_pool)
        ledger_pool = list(ledger_candidate_pool)
        bank_targets = [e for e in split_bank_targets if ResidualReconciler._identity_contains(bank_pool, e)]
        ledger_targets = [e for e in split_ledger_targets if ResidualReconciler._identity_contains(ledger_pool, e)]
        matches: List[dict] = []

        def _prefilter(pool: List[Dict[str, Any]], target: float, target_dir: str) -> List[Tuple[Any, float]]:
            pairs = []
            for entry in pool:
                item = entry["item"]
                amt, item_dir = _get_direction_amount(item)
                if amt <= 0:
                    continue
                if not _directions_compatible(target_dir, item_dir, self.same_side):
                    continue
                pairs.append((entry, amt))

            if len(pairs) > ResidualReconciler.SPLIT_POOL_PREFILTER_THRESHOLD:
                pairs = [(e, a) for e, a in pairs if a <= target]
            return pairs

        for bank_entry in list(bank_targets):
            if not ResidualReconciler._identity_contains(bank_pool, bank_entry):
                continue

            bank = bank_entry["item"]
            b_amt, b_dir = _get_direction_amount(bank)
            if b_amt <= 0:
                continue

            candidates = _prefilter(ledger_pool, b_amt, b_dir)
            candidates = _closest_candidates(
                valid=candidates,
                target=b_amt,
                max_pool=MAX_COMBINATION_POOL_SIZE,
                counterpart_text=_safe_str(bank, "narration"),
                text_of=lambda e: _safe_str(e["item"], "account_name"),
            )

            found, ambiguous = ResidualReconciler._subset_sum_search(
                b_amt,
                candidates,
                ResidualReconciler.SPLIT_TOLERANCE,
                ResidualReconciler.SPLIT_MAX_COMBINATION_SIZE,
            )
            if not found:
                continue

            gl_entries = [x[0] for x in found]
            gl_components = [
                {"ledger_id": _safe_str(entry["item"], "ledger_id"), "amount": comp_amt}
                for entry, comp_amt in found
            ]

            matches.append({
                "bank_id": getattr(bank, "row_index", None),
                "ledger_id": " & ".join(c["ledger_id"] for c in gl_components),
                "ledger_components": gl_components,
                "amount": b_amt,
                "adjustment_type": f"Split Match ({len(gl_entries)} ledger items)",
                "confidence_score": "Medium" if ambiguous else "High",
                "ambiguous": ambiguous,
                "details": (
                    f"Bank amount {b_amt:.2f} = sum of {len(gl_entries)} residual "
                    f"ledger entries." + (" Multiple valid combinations exist - "
                    "flagged for review rather than auto-accepted." if ambiguous else "")
                ),
                "match_phase": "residual_split",
            })
            ResidualReconciler._identity_remove(bank_pool, bank_entry)
            for e in gl_entries:
                ResidualReconciler._identity_remove(ledger_pool, e)

        for gl_entry in list(ledger_targets):
            if not ResidualReconciler._identity_contains(ledger_pool, gl_entry):
                continue

            gl = gl_entry["item"]
            g_amt, g_dir = _get_direction_amount(gl)
            if g_amt <= 0:
                continue

            candidates = _prefilter(bank_pool, g_amt, g_dir)
            candidates = _closest_candidates(
                valid=candidates,
                target=g_amt,
                max_pool=MAX_COMBINATION_POOL_SIZE,
                counterpart_text=_safe_str(gl, "account_name"),
                text_of=lambda e: _safe_str(e["item"], "narration"),
            )

            found, ambiguous = ResidualReconciler._subset_sum_search(
                g_amt,
                candidates,
                ResidualReconciler.SPLIT_TOLERANCE,
                ResidualReconciler.SPLIT_MAX_COMBINATION_SIZE,
            )
            if not found:
                continue

            bank_entries = [x[0] for x in found]
            bank_components = [
                {
                    "bank_id": getattr(entry["item"], "row_index", None), 
                    "amount": comp_amt
                }
                for entry, comp_amt in found
            ]

            matches.append({
                "ledger_id": _safe_str(gl, "ledger_id"),
                "bank_id": " & ".join(str(c["bank_id"]) for c in bank_components),
                "bank_components": bank_components,
                "amount": g_amt,
                "adjustment_type": f"Split Match ({len(bank_entries)} bank items)",
                "confidence_score": "Medium" if ambiguous else "High",
                "ambiguous": ambiguous,
                "details": (
                    f"Ledger amount {g_amt:.2f} = sum of {len(bank_entries)} residual "
                    f"bank entries." + (" Multiple valid combinations exist - "
                    "flagged for review rather than auto-accepted." if ambiguous else "")
                ),
                "match_phase": "residual_split",
            })
            ResidualReconciler._identity_remove(ledger_pool, gl_entry)
            for e in bank_entries:
                ResidualReconciler._identity_remove(bank_pool, e)

        return matches, ledger_pool, bank_pool


    _DRAFT_PROMPT_SYSTEM = (
        "You are an accountant drafting a journal entry for a bank transaction that "
        "has no matching ledger entry. You are given the bank narration only - you "
        "do not decide the amount or direction, only which account to post against "
        "the bank account, and a short plain-English narrative.\n"
        "Prefer standard account names (Bank Charges A/c, Interest Received A/c, "
        "Interest Paid A/c, Suspense A/c) when the narration matches a generic "
        "bank-fee or interest pattern. Use a vendor/party name from the narration "
        "when one is clearly present (e.g. a UPI credit naming a person or business).\n"
        "If the narration is too garbled or generic to infer anything reliable, "
        "propose 'Suspense A/c' and confidence below 0.4.\n"
        "OUTPUT FORMAT: Strict JSON ONLY. No markdown.\n"
        'Schema: {{"counter_account": "string", "entry_narrative": "string", "confidence": 0.0-1.0}}'
    )

    @staticmethod
    def _heuristic_draft_fallback(narration: str) -> DraftAccountSuggestion:
        n = (narration or "").upper()
        if "CHARGE" in n or "FEE" in n or "AMC" in n:
            return DraftAccountSuggestion(
                counter_account="Bank Charges A/c",
                entry_narrative="Likely a bank fee or service charge.",
                confidence=0.55,
            )

        if "INT" in n and ("CREDIT" in n or "CR" in n):
            return DraftAccountSuggestion(
                counter_account="Interest Received A/c",
                entry_narrative="Likely interest credited by the bank.",
                confidence=0.55,
            )

        return DraftAccountSuggestion(
            counter_account="Suspense A/c",
            entry_narrative="Narration did not match a recognizable pattern; needs review.",
            confidence=0.25,
        )

    def _generate_journal_drafts(
        self,
        missing_bank: List[Dict[str, Any]],
    ) -> Tuple[List[dict], List[Dict[str, Any]], bool, Optional[str]]:
        if not missing_bank:
            return [], [], False, None

        llm_available = self.enable_llm
        skip_reason: Optional[str] = None
        if not self.enable_llm:
            skip_reason = "AI drafting disabled by caller"
        else:
            try:
                if self.llm is None:
                    self.llm = get_shared_llm(config=self.config)
                self.llm.invoke("ping")
            except Exception as conn_err:
                llm_available = False
                skip_reason = str(conn_err)

        drafts: List[dict] = []
        still_unresolved: List[Dict[str, Any]] = []

        prompt_template = None
        if llm_available:
            prompt_template = ChatPromptTemplate.from_messages([
                ("system", ResidualReconciler._DRAFT_PROMPT_SYSTEM),
                ("human", "BANK NARRATION: {narration}\nAMOUNT: {amount:.2f}\nDIRECTION: {direction}"),
            ])

        for entry in missing_bank:
            bank = entry["item"]
            amt, direction = _get_direction_amount(bank)
            narration = _safe_str(bank, "narration")

            suggestion: Optional[DraftAccountSuggestion] = None
            source = "llm"
            if llm_available and prompt_template is not None:
                try:
                    chain = prompt_template | self.llm
                    raw = chain.invoke({"narration": narration, "amount": amt, "direction": direction})
                    suggestion = _safe_parse_json(raw, DraftAccountSuggestion)
                except Exception:
                    suggestion = None

            if suggestion is None:
                suggestion = ResidualReconciler._heuristic_draft_fallback(narration)
                source = "heuristic_fallback"

            if direction == "debit":
                debit_account, credit_account = suggestion.counter_account, "Bank A/c"
            else:
                debit_account, credit_account = "Bank A/c", suggestion.counter_account

            drafts.append({
                "bank_id": getattr(bank, "row_index", None),
                "date": _safe_str(bank, "date"),
                "amount": amt,
                "narration": narration,
                "debit_account": debit_account,
                "credit_account": credit_account,
                "entry_narrative": suggestion.entry_narrative,
                "confidence": round(suggestion.confidence, 2),
                "source": source,
                "status": "pending_review",
            })

        return drafts, still_unresolved, not llm_available, skip_reason

    def _closest_context(
        self,
        item: Any,
        is_bank: bool,
        top_n: int = 3,
    ) -> List[dict]:
        amt, direction = _get_direction_amount(item)
        if is_bank:
            text = _safe_str(item, "narration")
            date_ = _safe_str(item, "date")
        else:
            text = _safe_str(item, "account_name")
            date_ = _safe_str(item, "transaction_date")

        scored = []
        counterpart_pool = self.ledger_pool if is_bank else self.bank_pool
        for cp in counterpart_pool:
            cp_amt, cp_dir = _get_direction_amount(cp)
            if is_bank:
                cp_text = _safe_str(cp, "account_name")
                cp_date = _safe_str(cp, "transaction_date")
            else:
                cp_text = _safe_str(cp, "narration")
                cp_date = _safe_str(cp, "date")

            amt_diff = abs(amt - cp_amt)
            date_obj = _to_date(date_)
            cp_date_obj = _to_date(cp_date)
            if date_obj is not None and cp_date_obj is not None:
                day_gap = self._cal.lag(date_obj, cp_date_obj) if is_bank else self._cal.lag(cp_date_obj, date_obj)
            else:
                day_gap = None
            sim = text_similarity(text, cp_text) if is_bank else text_similarity(cp_text, text)
            score = amt_diff - (sim * 50) + (abs(day_gap) if day_gap is not None else 999) * 0.1
            scored.append((score, cp, amt_diff, day_gap, sim))

        scored.sort(key=lambda x: x[0])
        context = []
        for _, cp, amt_diff, day_gap, sim in scored[:top_n]:
            context.append({
                "candidate_id": (
                    _safe_str(cp, "ledger_id") if is_bank else str(getattr(cp, "row_index", None))
                ),
                "amount_difference": round(amt_diff, 2),
                "date_gap_days": day_gap,
                "text_similarity": round(sim, 2),
            })
        return context

    @staticmethod
    def _suggested_action(label: str, evidence: dict) -> str:
        if label == ResidualReconciler.CLASSIFY_TIMING:
            return "Amount matches something on the other side outside the timing window - verify if this is a delayed clearance."
        if label == ResidualReconciler.CLASSIFY_SPLIT:
            return "A combination of entries may sum to this amount - verify if this payment was split."
        if label == ResidualReconciler.CLASSIFY_MISSING:
            return "No amount evidence on the other side - check if this needs to be recorded manually."
        return "No strong signal found on any check - check for a rounding adjustment or a data-quality issue (wrong date/amount/OCR error)."

    def _build_review_queue(
        self,
        entries: List[Dict[str, Any]],
        is_bank: bool,
    ) -> List[dict]:
        queue = []
        for entry in entries:
            item = entry["item"]
            label = entry["label"]
            evidence = entry.get("evidence", {})
            queue.append({
                "side": "bank" if is_bank else "ledger",
                "id": (getattr(item, "row_index", None) if is_bank else _safe_str(item, "ledger_id")),
                "amount": evidence.get("amount", _get_direction_amount(item)[0]),
                "narration_or_account": _safe_str(item, "narration") if is_bank else _safe_str(item, "account_name"),
                "date": _safe_str(item, "date") if is_bank else _safe_str(item, "transaction_date"),
                "classification": label,
                "closest_candidates": self._closest_context(item, is_bank),
                "suggested_action": ResidualReconciler._suggested_action(label, evidence),
            })
        return queue

    def match(self) -> dict:

        classified = self.classify_residuals()

        timing_bank = [e for e in classified["bank"] if e["label"] == ResidualReconciler.CLASSIFY_TIMING]
        timing_ledger = [e for e in classified["ledger"] if e["label"] == ResidualReconciler.CLASSIFY_TIMING]
        split_bank = [e for e in classified["bank"] if e["label"] == ResidualReconciler.CLASSIFY_SPLIT]
        split_ledger = [e for e in classified["ledger"] if e["label"] == ResidualReconciler.CLASSIFY_SPLIT]
        missing_bank = [e for e in classified["bank"] if e["label"] == ResidualReconciler.CLASSIFY_MISSING]
        missing_ledger = [e for e in classified["ledger"] if e["label"] == ResidualReconciler.CLASSIFY_MISSING]
        unclass_bank = [e for e in classified["bank"] if e["label"] == ResidualReconciler.CLASSIFY_UNCLASSIFIABLE]
        unclass_ledger = [e for e in classified["ledger"] if e["label"] == ResidualReconciler.CLASSIFY_UNCLASSIFIABLE]

        timing_matches, timing_ledger_left, timing_bank_left = self._resolve_timing_candidates(
            timing_bank, timing_ledger
        )

        ledger_pool_after_timing = timing_ledger_left + split_ledger + missing_ledger + unclass_ledger
        bank_pool_after_timing = timing_bank_left + split_bank + missing_bank + unclass_bank

        split_matches, split_ledger_left, split_bank_left = self._resolve_split_candidates(
            split_bank, split_ledger,
            ledger_pool_after_timing, bank_pool_after_timing,
        )

        missing_bank_remaining = [e for e in split_bank_left if e["label"] == ResidualReconciler.CLASSIFY_MISSING]
        non_missing_bank_remaining = [e for e in split_bank_left if e["label"] != ResidualReconciler.CLASSIFY_MISSING]
        non_missing_ledger_remaining = split_ledger_left

        drafts, missing_bank_unresolved, ai_skipped, ai_skip_reason = self._generate_journal_drafts(
            missing_bank_remaining,
        )

        review_bank = non_missing_bank_remaining + missing_bank_unresolved
        review_ledger = non_missing_ledger_remaining

        human_review_queue = (
            self._build_review_queue(review_bank, True) +
            self._build_review_queue(review_ledger, False)
        )

        stats = {
            "residual_ledger_in": len(self.ledger_pool),
            "residual_bank_in": len(self.bank_pool),
            "timing_resolved": len(timing_matches),
            "splits_resolved": len(split_matches),
            "drafts_generated": len(drafts),
            "still_unresolved": len(human_review_queue),
            "ai_skipped": ai_skipped,
            "ai_skip_reason": ai_skip_reason,
        }

        return {
            "timing_matches": timing_matches,
            "split_matches": split_matches,
            "suggested_journal_entries": drafts,
            "human_review_queue": human_review_queue,
            "stats": stats,
            "still_unreconciled_ledger": [e["item"] for e in review_ledger],
            "still_unreconciled_bank": [e["item"] for e in review_bank],
        }

    def reconcile_residuals(self) -> dict:
        """Compatibility alias for the matcher interface."""
        return self.match()


def reconcile_residuals(
    unreconciled_ledger: List[LedgerFormat],
    unreconciled_bank: List[BankStatement],
    *,
    config: Settings,
    same_side: bool = True,
    llm=None,
    enable_llm: bool = True,
) -> dict:
    return ResidualReconciler(
        pending_ledger=unreconciled_ledger,
        pending_bank=unreconciled_bank,
        config=config,
        llm=llm,
        same_side=same_side,
        enable_llm=enable_llm,
    ).match()
