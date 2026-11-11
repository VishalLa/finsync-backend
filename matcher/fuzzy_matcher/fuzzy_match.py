from __future__ import annotations

from datetime import date, timedelta
from bisect import bisect_left, bisect_right
from functools import lru_cache, partial
from typing import Any, Callable, List, Optional, Tuple, Set, Dict

from schema import BankStatement, LedgerFormat, IgnoredMetadataRecord, AuditInvestigationItem

from .business_calendar import BusinessCalendar
from .subset_sum import find_subsets

from ..utils.helper import (
    _NSF_RE,
    _INTEREST_RE,
    _REVERSAL_RE,
    
    _safe_amount,
    _safe_str,
    _get_amt,
    _get_date,
    _amounts_equal,
    _days_between,
    _crosses_month_boundary,
    _digit_multiset,
    _to_date,

    text_similarity as _text_similarity,
    is_transposition,
)

text_similarity = lru_cache(maxsize=262_144)(_text_similarity)
_EPS = 1e-9

# An edge is one *candidate* match:
#   (score, ledger_indices, bank_indices, builder)
# `builder()` lazily returns (match_dict, optional_audit_item).
Edge = Tuple[float, Tuple[int, ...], Tuple[int, ...], Callable[[], Tuple[dict, Any]]]


class _AmountIndex:
    """Sorted (amount, idx) list. between(lo, hi) -> idx list via binary search.
        It only *prunes*; callers still re-apply the original exact predicate."""
    
    __slots__ = ("_keys", "_ids")
    def __init__(self, pairs) -> None:
        pairs = sorted(pairs)
        self._keys = [p[0] for p in pairs]
        self._ids = [p[1] for p in pairs]
    
    def between(self, lo: float, hi: float) -> List[int]:
        i = bisect_left(self._keys, lo - _EPS)
        j = bisect_right(self._keys, hi + _EPS)
        return self._ids[i:j]


class FuzzyMatcher:
    MAX_COMBINATION_SIZE: int = 6   
    MAX_COMBINATION_POOL_SIZE: int = 40
    
    SUBSET_MAX_NODES: int = 200_000
    SUBSET_MAX_SOLUTIONS: int = 300

    _CROSS_MONTH_BUFFER: int = 15

    def __init__(
        self,
        pending_ledger: List[LedgerFormat],
        pending_bank:   List[BankStatement],
        same_side:      bool = True,
        tolerances:     dict[str, float | int] = None,
        calendar:       Optional[BusinessCalendar] = None
    ) -> None:
        self.ledger_pool:   List[LedgerFormat]  = list(pending_ledger)
        self.bank_pool:     List[BankStatement] = list(pending_bank)
        self.fuzzy_matches: List[dict]          = []

        self.ignored_metadata_records:  List[IgnoredMetadataRecord]  = []
        self.audit_investigation_items: List[AuditInvestigationItem] = []

        self.tolerances: dict = tolerances if tolerances is not None else {}
        self._tol:       float = float(self.tolerances.get("DEFAULT", 1.0))

        # Per-strategy tolerance shortcuts (derived once for efficiency)
        self._tol_timing:      float = float(self.tolerances.get("TIMING_DIFFERENCE",   2.0))
        self._tol_rounding:    float = float(self.tolerances.get("ROUNDING_DIFFERENCE", 2.0))
        self._tol_transpos:    float = float(self.tolerances.get("TRANSPOSITION",        0.0))
        self._tol_exact:       float = float(self.tolerances.get("EXACT",                0.0))

        # Direction accessors 
        if same_side:
            self.gl_out: Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "debit_amount")
            self.gl_in:  Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "credit_amount")
        else:
            self.gl_out: Callable[[LedgerFormat],  float] = lambda g: _safe_amount(g, "credit_amount")
            self.gl_in:  Callable[[LedgerFormat],  float]  = lambda g: _safe_amount(g, "debit_amount")

        self.bank_out: Callable[[BankStatement], float] = lambda b: _safe_amount(b, "debit_amount")
        self.bank_in:  Callable[[BankStatement], float] = lambda b: _safe_amount(b, "credit_amount")
        
        self._cal: BusinessCalendar = calendar if calendar is not None else BusinessCalendar.india_banks()
        self._prepare_calendar()


    def _effective_window(
        self,
        base_window:  int,
        date_iso_a:   Optional[str],
        date_iso_b:   Optional[str],
    ) -> int:
        if _crosses_month_boundary(date_iso_a, date_iso_b):
            return base_window + self._CROSS_MONTH_BUFFER
        return base_window


    @staticmethod
    def _closest_candidates(
        valid:          List[tuple],
        target:         float,
        max_pool:       int,
        counterpart_text: str = "",
        text_of:        Optional[Callable[[Any], str]] = None,
    ) -> List[tuple]:
        if len(valid) <= max_pool or target <= 0:
            return valid

        max_k = 6  

        def amount_distance(amount: float) -> float:
            return min(abs(amount - target / k) for k in range(2, max_k + 1))

        def sim_score(record: Any) -> float:
            if not text_of or not counterpart_text:
                return 0.0
            return text_similarity(counterpart_text, text_of(record))

        max_dist = max((amount_distance(x[1]) for x in valid), default=1.0) or 1.0

        def combined_score(item: tuple) -> float:
            record, amount = item
            sim = sim_score(record)
            dist_norm = amount_distance(amount) / max_dist
            return (sim * 2.0) + (1.0 - dist_norm)

        ranked = sorted(valid, key=combined_score, reverse=True)
        return ranked[:max_pool]


    def cleanse_zero_amount_metadata(self) -> None:
        remaining_bank = []
        for bk in self.bank_pool:
            if abs(_get_amt(bk)) <= 0.001:
                self.ignored_metadata_records.append(
                    IgnoredMetadataRecord(
                        source    = "bank",
                        row_ref   = str(getattr(bk, "row_index", "N/A")),
                        narration = getattr(bk, "narration", "OPENING/CLOSING BALANCE"),
                    )
                )
            else:
                remaining_bank.append(bk)
        self.bank_pool = remaining_bank

        remaining_ledger = []
        for gl in self.ledger_pool:
            if abs(_get_amt(gl)) <= 0.001:
                self.ignored_metadata_records.append(
                    IgnoredMetadataRecord(
                        source    = "ledger",
                        row_ref   = str(getattr(gl, "ledger_id", "N/A")),
                        narration = getattr(gl, "account_name", "Header/Metadata Row"),
                    )
                )
            else:
                remaining_ledger.append(gl)
        self.ledger_pool = remaining_ledger


    def flag_ghost_reversals(self) -> None:
        for bk in list(self.bank_pool):
            narration = getattr(bk, "narration", "") or ""
            if _REVERSAL_RE.search(narration):
                self.audit_investigation_items.append(
                    AuditInvestigationItem(
                        bank_row_index = getattr(bk, "row_index", -1),
                        narration      = narration,
                        amount         = _get_amt(bk),
                        direction      = "credit" if getattr(bk, "credit", 0) else "debit",
                        flag_reason    = "Banking Reversal or Duplicate Error detected.",
                        action_required= (
                            "Requires manual General Ledger journal adjustment. "
                            "Item retained in matching pool - may still match a "
                            "ledger reversal entry via Strategy 7 or later."
                        ),
                    )
                )

    _W_TEXT: float = 4.0   # narration / account-name similarity (0..1)
    _W_DAY:  float = 0.15  # penalty per day of lag
    _W_AMT:  float = 0.5   # penalty for amount difference, relative to tolerance
    _W_REF:  float = 3.0   # bonus when reference_id == txn_id
    _MAX_EDGES_PER_RECORD: int = 50   # safety valve for pathological duplicates
    
    ALLOW_BANK_BEFORE_LEDGER: bool = True
    TIMING_EARLY_BD: int = 3   # deposit-in-transit / outstanding-cheque strategies
    COMBO_EARLY_BD:  int = 2   # aggregated / many-to-one strategies
    _MAX_EDGES_PER_RECORD: int = 50   # safety valve for pathological duplicates
    
    def _prepare_calendar(self) -> None:
        ds = [_to_date(_safe_str(g, "transaction_date")) for g in self.ledger_pool]
        ds += [_to_date(_safe_str(b, "date")) for b in self.bank_pool]
        ds = [d for d in ds if d is not None]
        if ds:
            self._cal.prepare(min(ds), max(ds))
            
    def _lag(self, ledger_date, bank_date) -> int:
        """Signed business-day lag of the bank date relative to the ledger date (+ = bank later)."""
        return self._cal.lag(ledger_date, bank_date)
    
    def _dates(self, recs: List[Any], attr: str) -> list:
        return[_to_date(_safe_str(r, attr)) for r in recs]
    
    def _win(self, base: int, da, db) -> int:
        """Date-object version of _effective_window (no re-parsing)."""
        return base + self._CROSS_MONTH_BUFFER if (da.year, da.month) != (db.year, db.month) else base   
    
    def _time_score(
        self,
        sim: float,
        days: int, 
        amt_diff: float,
        tol: float,
        ref_eq: bool
    ) -> float:
        return (
            self._W_TEXT * sim
            - self._W_DAY * abs(days)
            - self._W_AMT * (amt_diff / tol if tol > 0 else 0.0)
            + (self._W_REF if ref_eq else 0.0)
        )
         
    @staticmethod
    def _ref_equal(gl: Any, bank: Any) -> bool:
        rg, rb = _safe_str(gl, "reference_id"), _safe_str(bank, "txn_id")
        return bool(rg and rb and rg == rb)
    
    def _extend_capped(
        self, 
        edges: List[Edge],
        local: List[Edge]
    ) -> None:
        if len(local) > self._MAX_EDGES_PER_RECORD:
            local.sort(key=lambda e: -e[0])
            local = local[: self._MAX_EDGES_PER_RECORD]
        edges.extend(local)
        
    @staticmethod
    def _tie_key(
        gi: Tuple[int, ...],
        bi: Tuple[int, ...],
        gls: list,
        bks: list
    ) -> tuple:
        """Tie-break on the records' own ids, not on their position in the pool,
        so equal-scoring candidates resolve the same way whatever the input order."""
        return (
            tuple(str(getattr(gls[i], "ledger_id", "")) for i in gi),
            tuple(str(getattr(bks[j], "row_index", "")) for j in bi),
        )
        
    def _select(
        self,
        edges: List[Edge],
        used_gl: Set[int],
        used_bk: Set[int], 
        gls: list,
        bks: list
    ) -> list:
        """Greedy by score; deterministic tie-break on record ids."""
        edges.sort(key=lambda e: (-e[0], self._tie_key(
            gi=e[1], bi=e[2], gls=gls, bks=bks
        )))
        accepted = []
        for _score, gi, bi, build in edges:
            if (
                any(i in used_gl for i in gi) or 
                any(j in  used_bk for j in bi)
            ):
                continue
            used_gl.update(gi)
            used_bk.update(bi)
            accepted.append((gi, bi, build))
        return accepted
    
    def _emit(
        self,
        accepted: list,
        gls: list,
        bks: list
    ) -> None:
        accepted.sort(key=lambda a: self._tie_key(
            gi=a[0], bi=a[1], gls=gls, bks=bks
        ))
        for _gi, _bi, build in accepted:
            match, audit = build()
            self.fuzzy_matches.append(match)
            
            if audit is not None:
                self.audit_investigation_items.append(audit)
                
    def _rebuild_pools(
        self,
        gls: list,
        bks: list,
        used_gl: Set[int],
        used_bk: Set[int]
    ) -> None:
        self.ledger_pool = [g for i, g in enumerate(gls) if i not in used_gl]
        self.bank_pool = [b for j, b in enumerate(bks) if j not in used_bk]
        
    def _assign(
        self,
        edges: List[Edge],
        gls: list,
        bks: list
    ) -> None:
        used_gl: Set[int] = set()
        used_bk: Set[int] = set()
        self._emit(
            self._select(
                edges=edges,
                used_gl=used_gl,
                used_bk=used_bk,
                gls=gls,
                bks=bks
            ), 
            gls=gls, bks=bks
        )        

    # Strategy 1 / 2 - Deposit in Transit  /  Outstanding Checks
    def _build_timing(
        self, gl, bank, amt, diff, adj_type, high_days, verb
    ):
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  adj_type,
            "confidence_score": "High" if diff <= high_days else "Medium",
            "details": (
                f"Amount matches ({amt:.2f}); "
                f"{verb} {diff} day(s) after ledger date."
            ),
        }, None)
        
        
    def _match_timing(
        self, *, gl_amt_of, bank_amt_of, base_window: int, adj_type: str, early_adj_type: str, high_days: int, verb: str
    ) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")
        tol = self._tol_timing
        index = _AmountIndex(
            (bank_amt_of(b), j) for j, b in enumerate(bks) if bank_amt_of(b) > 0 and bk_dt[j]
        )

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            g_amt = gl_amt_of(gl)
            if g_amt <= 0 or gl_dt[i] is None:
                continue
            
            local: List[Edge] = []
            for j in index.between(g_amt - tol, g_amt + tol):
                bank = bks[j]
                b_amt = bank_amt_of(bank)
                if not _amounts_equal(g_amt, b_amt, tol):
                    continue
                
                cal_days = (bk_dt[j] - gl_dt[i]).days
                lag = self._lag(gl_dt[i], bk_dt[j])
                if cal_days >= 1:                                   # bank after ledger (the normal case)
                    if lag > self._win(base_window, gl_dt[i], bk_dt[j]):
                        continue
                    
                    early = False
                elif cal_days <= -1 and self.ALLOW_BANK_BEFORE_LEDGER:   # bank BEFORE ledger
                    if -lag > self.TIMING_EARLY_BD:
                        continue
                    early = True
                    
                else:                                               # same calendar day: exact/rounding phases
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                score = self._timing_score(sim, lag, abs(g_amt - b_amt), tol, self._ref_equal(gl, bank), early)
                local.append((
                    score, (i,), (j,), partial(
                        self._build_timing, gl, bank, g_amt, lag, cal_days, adj_type, early_adj_type, high_days, verb, early, sim
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)

            
    def match_deposit_in_transit(self) -> None:
        self._match_timing(
            gl_amt_of=self.gl_in, 
            bank_amt_of=self.bank_in, 
            base_window=5,
            adj_type="Deposit in Transit", 
            high_days=3, 
            verb="bank cleared",
        )

    def match_outstanding_checks(self) -> None:
        self._match_timing(
            gl_amt_of=self.gl_out, 
            bank_amt_of=self.bank_out, 
            base_window=14,
            adj_type="Outstanding Check", 
            high_days=7, 
            verb="payment cleared",
        )
                        

    # Strategy 3 - Bank Service Charges
    def _build_service_charge(
        self, gl, bank, diff, fee, sim
    ):
        confidence = "High" if sim >= 0.5 else ("Medium" if sim >= 0.25 else "Low")
        match_data = {
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  "Bank Service Charge",
            "confidence_score": confidence,
            "details": (
                f"Bank debit ({self.bank_out(bank):.2f}) exceeds ledger "
                f"({self.gl_out(gl):.2f}) by {fee:.2f} - embedded fee; "
                f"narration similarity={sim:.2f}; dates {diff} day(s) apart."
            ),
        }
        if confidence == "Low":
            match_data["adjustment_type"] = "AI Agent Review (Possible Service Charge)"
        return match_data, None
    
    def match_bank_service_charges(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")
        index = _AmountIndex(
            (self.bank_out(b), j) for j, b in enumerate(bks) if self.bank_out(b) > 0 and bk_dt[j]
        )
        _MAX_PLAUSIBLE_FEE = 2500.0

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            g = self.gl_out(gl)
            if g <= 0 or gl_dt[i] is None:
                continue
            local: List[Edge] = []
            # fee must be > 0 and (fee <= 2500 or fee <= 15% of the ledger amount)
            for j in index.between(g, g + max(_MAX_PLAUSIBLE_FEE, 0.15 * g)):
                bank = bks[j]
                
                diff = self._lag(gl_dt[i], bk_dt[j])
                if abs(diff) > self._win(3, gl_dt[i], bk_dt[j]):
                    continue
                
                fee = self.bank_out(bank) - g
                if fee <= 0:
                    continue
                
                fee_pct = fee / g if g else 0
                if fee > _MAX_PLAUSIBLE_FEE and fee_pct > 0.15:
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                if sim < 0.15:
                    continue
                
                local.append((
                    sim - abs(diff) * 0.01, (i,), (j,), 
                    partial(
                        self._build_service_charge, gl, bank, diff, fee, sim
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 4 - Text Similarity Match
    def _build_text_match(
        self, gl, bank, gl_amt, sim
    ):
        confidence = "High" if sim >= 0.6 else ("Medium" if sim >= 0.3 else "Low")
        match_data = {
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  "Text Similarity Match",
            "confidence_score": confidence,
            "details": (
                f"Amount matches ({gl_amt:.2f}); reference absent on ≥1 side; "
                f"account/narration similarity={sim:.2f}."
            ),
        }
        if confidence == "Low":
            match_data["adjustment_type"] = "AI Agent Review (Text Similarity)"
        return match_data, None

    def match_text_similarity(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        out_idx = _AmountIndex(
            (self.bank_out(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_out(b) > 0
        )
        in_idx = _AmountIndex(
            (self.bank_in(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_in(b) > 0
        )

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0:
                continue
            
            idx = out_idx if is_out else in_idx
            local: List[Edge] = []
            
            for j in idx.between(gl_amt - self._tol, gl_amt + self._tol):
                bank = bks[j]
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not _amounts_equal(gl_amt, b_amt, self._tol):
                    continue
                
                ref_gl, ref_bk = _safe_str(gl, "reference_id"), _safe_str(bank, "txn_id")
                if ref_gl and ref_bk and ref_gl != ref_bk:
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                if sim < 0.3:
                    continue
                
                score = sim + (1.0 if ref_gl and ref_gl == ref_bk else 0.0) - 0.001 * abs(gl_amt - b_amt)
                local.append((
                    score, (i,), (j,), partial(
                        self._build_text_match, gl, bank, gl_amt, sim
                    )
                ))
                
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 5 - Book Error (Transposition)
    def _build_transposition(
        self, gl, bank, gl_amt, b_amt, diff, sim
    ):
        confidence = "High" if (sim >= 0.5 and diff == 0) else "Medium"
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  "Book Error (Transposition)",
            "confidence_score": confidence,
            "details": (
                f"Ledger: {gl_amt:.2f}, Bank: {b_amt:.2f} - same digits, "
                f"different order; narration similarity={sim:.2f}; "
                f"dates {diff} day(s) apart. Recommend journal entry correction."
            ),
        }, None)

    def match_transposition_errors(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")

        # Same digits => same digit multiset: bucket banks by (direction, multiset).
        buckets: Dict[Tuple[bool, str], List[int]] = {}
        for j, b in enumerate(bks):
            if bk_dt[j] is None:
                continue
            
            for is_out, amt in ((True, self.bank_out(b)), (False, self.bank_in(b))):
                if amt > 0:
                    buckets.setdefault((is_out, _digit_multiset(amt)), []).append(j)

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or gl_dt[i] is None:
                continue
            
            local: List[Edge] = []
            for j in buckets.get((is_out, _digit_multiset(gl_amt)), []):
                bank = bks[j]
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                if b_amt <= 0 or not is_transposition(gl_amt, b_amt, self._tol_transpos):
                    continue
                
                diff = self._lag(gl_dt[i], bk_dt[j])
                if abs(diff) > self._win(3, gl_dt[i], bk_dt[j]):
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                if sim < 0.3:
                    continue
                
                local.append((
                    sim - abs(diff) * 0.01, (i,), (j,), partial(
                        self._build_transposition, gl, bank, gl_amt, b_amt, diff, sim
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)



    # Strategy 6 - Rounding Differences
    def _build_rounding(
        self, gl, bank, gl_amt, b_amt, diff_amt, sim
    ):
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  "Rounding Difference",
            "confidence_score": "High",
            "details": (
                f"Ledger ({gl_amt:.2f}) and Bank ({b_amt:.2f}) differ by "
                f"{diff_amt:.2f}. Narration similarity={sim:.2f}. "
                f"Recommend writing off difference."
            ),
        }, None)

    def match_rounding_differences(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")
        tol = self._tol_rounding
        out_idx = _AmountIndex(
            (self.bank_out(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_out(b) > 0 and bk_dt[j]
        )
        in_idx = _AmountIndex(
            (self.bank_in(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_in(b) > 0 and bk_dt[j]
        )

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or gl_dt[i] is None:
                continue
            
            idx = out_idx if is_out else in_idx
            local: List[Edge] = []
            for j in idx.between(gl_amt - tol, gl_amt + tol):
                bank = bks[j]
                b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                diff_amt = abs(gl_amt - b_amt)
                if b_amt <= 0 or not (0.0 < diff_amt <= tol):
                    continue
                
                diff_days = self._lag(gl_dt[i], bk_dt[j])
                if abs(diff_days) > self._win(3, gl_dt[i], bk_dt[j]):
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                if sim < 0.5:
                    continue
                
                local.append((
                    sim - 0.001 * diff_amt, (i,), (j,), partial(
                        self._build_rounding, gl, bank, gl_amt, b_amt, diff_amt, sim
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 7 - NSF / Returned Items
    def _build_nsf(
        self, gl, bank, diff
    ):
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  "NSF / Returned Item",
            "confidence_score": "High",
            "details": (
                f"Bank narration indicates returned item "
                f"('{_safe_str(bank, 'narration')}'); "
                f"matches ledger receipt of {self.gl_in(gl):.2f} recorded "
                f"{diff} day(s) earlier. Requires a reversing journal entry."
            ),
        }, None)

    def match_nsf_returned_items(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")
        index = _AmountIndex(
            (self.gl_in(g), i) 
            for i, g in enumerate(gls)
            if self.gl_in(g) > 0 and gl_dt[i]
        )

        edges: List[Edge] = []
        for j, bank in enumerate(bks):
            if self.bank_out(bank) <= 0 or bk_dt[j] is None:
                continue
            
            if not _NSF_RE.search(_safe_str(bank, "narration")):
                continue
            
            b = self.bank_out(bank)
            local: List[Edge] = []
            for i in index.between(b - self._tol, b + self._tol):
                gl = gls[i]
                if not _amounts_equal(self.gl_in(gl), b, self._tol):
                    continue
                
                diff = (bk_dt[j] - gl_dt[i]).days
                if diff < 0:
                    continue
                
                sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                score = self._time_score(
                    sim, diff, abs(self.gl_in(gl) - b), self._tol, self._ref_equal(gl, bank)
                )
                local.append((
                    score, (i,), (j,), partial(
                        self._build_nsf, gl, bank, diff
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 8 - Interest Income (standalone bank credit)
    def match_interest_income(self) -> None:
        remaining_bank = []
        for bank in self.bank_pool:
            if self.bank_in(bank) <= 0:
                remaining_bank.append(bank)
                continue

            if not _INTEREST_RE.search(_safe_str(bank, "narration")):
                remaining_bank.append(bank)
                continue

            self.fuzzy_matches.append({
                "ledger_id":        None,
                "bank_id":          getattr(bank, "row_index", None),
                "adjustment_type":  "Interest Income (Book Adjustment)",
                "confidence_score": "Medium",
                "details": (
                    f"Standalone bank credit with interest narration "
                    f"('{_safe_str(bank, 'narration')}'); "
                    f"add {self.bank_in(bank):.2f} to book balance via journal entry."
                ),
            })

        self.bank_pool = remaining_bank


    # Strategy 9 - Discounts and Tax Withholdings
    _COMMON_DISCOUNT_RATES = (0.01, 0.02, 0.05, 0.075, 0.10, 0.20)

    def _build_discount(
        self, gl, bank, gl_amt, b_amt, implied
    ):
        implied_pct = implied * 100
        pct_label = (
            f"{implied_pct:.0f}%" if implied_pct == int(implied_pct) else f"{implied_pct:.1f}%"
        )
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  f"Discount/Withholding ({pct_label})",
            "confidence_score": "Medium",
            "details": (
                f"Bank received {b_amt:.2f}, {pct_label} less "
                f"than Ledger ({gl_amt:.2f}). Likely early payment discount "
                f"or tax withholding."
            ),
        }, None)

    def match_discounts_and_taxes(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")
        out_idx = _AmountIndex(
            (self.bank_out(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_out(b) > 0 and bk_dt[j]
        )
        in_idx = _AmountIndex(
            (self.bank_in(b), j) 
            for j, b in enumerate(bks) 
            if self.bank_in(b) > 0 and bk_dt[j]
        )

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or gl_dt[i] is None:
                continue
            
            idx = out_idx if is_out else in_idx
            local: List[Edge] = []
            seen: Set[int] = set()
            for rate in self._COMMON_DISCOUNT_RATES:
                lo = gl_amt * (1 - rate - 0.001)
                hi = gl_amt * (1 - rate + 0.001)
                for j in idx.between(lo, hi):
                    if j in seen:
                        continue
                    
                    seen.add(j)
                    bank = bks[j]
                    b_amt = self.bank_out(bank) if is_out else self.bank_in(bank)
                    if b_amt <= 0 or b_amt >= gl_amt:
                        continue
                    
                    implied = (gl_amt - b_amt) / gl_amt
                    if not any(abs(implied - r) < 0.001 for r in self._COMMON_DISCOUNT_RATES):
                        continue
                    
                    diff = self._lag(gl_dt[i], bk_dt[j])
                    if abs(diff) > self._win(5, gl_dt[i], bk_dt[j]):
                        continue
                    
                    sim = text_similarity(_safe_str(gl, "account_name"), _safe_str(bank, "narration"))
                    if sim < 0.4:
                        continue
                    
                    local.append((
                        sim, (i,), (j,), partial(
                            self._build_discount, gl, bank, gl_amt, b_amt, implied
                        )
                    ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 10 / 11 - Zero-sum contra pairs (bank side / ledger side)
    def _zero_sum_edges(
        self, recs, out_of, in_of, text_attr, date_attr, builder, side
    ) -> List[Edge]:
        dts = self._dates(recs, date_attr)
        items = []                                   # (amount, idx, is_out)
        for k, r in enumerate(recs):
            o, n = out_of(r), in_of(r)
            amt = o if o > 0 else n
            if amt > 0:
                items.append((amt, k, o > 0))
                
        amt_of = {k: a for a, k, _ in items}
        in_idx = _AmountIndex(
            (a, k) 
            for a, k, is_out in items 
            if not is_out
        )

        edges: List[Edge] = []
        for a1, k1, is_out in items:
            if not is_out:
                continue                              # every opposite pair is seen once from its "out" half
            
            local: List[Edge] = []
            for k2 in in_idx.between(a1 - self._tol, a1 + self._tol):
                if not _amounts_equal(a1, amt_of[k2], self._tol):
                    continue
                
                sim = text_similarity(_safe_str(recs[k1], text_attr), _safe_str(recs[k2], text_attr))
                d1, d2 = dts[k1], dts[k2]
                diff = abs((d2 - d1).days) if (d1 and d2) else 0
                if not (sim >= 0.6 or (sim >= 0.3 and diff <= 90)):
                    continue
                
                lo, hi = (k1, k2) if k1 < k2 else (k2, k1)
                score = sim - diff * 0.001
                member = (lo, hi)
                local.append((
                    score,
                    member if side == "ledger" else (),
                    member if side == "bank" else (),
                    partial(builder, recs[lo], recs[hi], a1, sim, diff),
                ))
                
            self._extend_capped(edges, local)
        return edges

    def _build_bank_zero_sum(
        self, b1, b2, amt, sim, diff
    ):
        reason = "Bank Error/Reversal" if sim >= 0.6 and diff <= 14 else "Temporary Advance/Refund"
        return ({
            "ledger_id":        None,
            "bank_id":          f"{getattr(b1, 'row_index', '')} & {getattr(b2, 'row_index', '')}",
            "adjustment_type":  f"Bank-Side Zero-Sum ({reason})",
            "confidence_score": "High" if sim >= 0.7 else "Medium",
            "details": (
                f"Net-zero bank entries of {amt:.2f} cancel out. "
                f"Spans {diff} day(s). Narration similarity={sim:.2f}."
            ),
        }, None)

    def _build_ledger_zero_sum(
        self, gl1, gl2, amt, sim, diff
    ):
        reason = "Ledger Reversal" if sim >= 0.6 and diff <= 14 else "Unbanked Advance/Refund"
        return ({
            "ledger_id":        f"{_safe_str(gl1, 'ledger_id')} & {_safe_str(gl2, 'ledger_id')}",
            "bank_id":          None,
            "adjustment_type":  f"Ledger-Side Zero-Sum ({reason})",
            "confidence_score": "High" if sim >= 0.7 else "Medium",
            "details": (
                f"Net-zero ledger entries of {amt:.2f} cancel out. "
                f"Spans {diff} day(s). Account name similarity={sim:.2f}."
            ),
        }, None)

    def match_bank_side_zero_sum(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        edges = self._zero_sum_edges(
            bks, self.bank_out, self.bank_in, "narration", "date",
            self._build_bank_zero_sum, "bank",
        )
        self._assign(edges, gls, bks)

    def match_ledger_side_zero_sum(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        edges = self._zero_sum_edges(
            gls, self.gl_out, self.gl_in, "account_name", "transaction_date",
            self._build_ledger_zero_sum, "ledger",
        )
        self._assign(edges, gls, bks)


    # Strategy 12 / 13 - Aggregations (1 ledger : N bank  /  N ledger : 1 bank)
    def _combos_with_sum(
        self, valid: List[tuple], target: float, r: int
    ):
        """Yield r-item combinations of `valid` (items are (record, amount)) whose amounts sum to
        `target` within tolerance. Same combinations, same order and same left-to-right float
        sums as filtering itertools.combinations(valid, r), but a branch is abandoned as soon as
        its running total exceeds target + tol (all amounts are positive)."""
        amts = [x[1] for x in valid]
        n, tol = len(valid), self._tol
        ceiling = target + tol + _EPS
        chosen: List[int] = []

        def rec(start: int, k: int, total: float):
            if k == 0:
                if _amounts_equal(target, total, tol):
                    yield tuple(valid[i] for i in chosen)
                return
            
            for i in range(start, n - k + 1):
                t = total + amts[i]
                if t > ceiling:
                    continue
                
                chosen.append(i)
                yield from rec(i + 1, k - 1, t)
                
                chosen.pop()

        yield from rec(0, r, 0.0)

    def _build_aggregated(
        self, gl, banks, amts, gl_amt
    ):
        id_str = " & ".join(str(getattr(b, "row_index", "?")) for b in banks)
        amt_str = " + ".join(f"{a:.2f}" for a in amts)
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          id_str,
            "adjustment_type":  f"Aggregated Split Transaction ({len(banks)} items)",
            "confidence_score": "High",
            "details": (
                f"Ledger ({gl_amt:.2f}) = sum of {len(banks)} bank entries ({amt_str})."
            ),
        }, None)

    def _build_many_to_one(
        self, gls_, bank, amts, b_amt
    ):
        id_str = " & ".join(_safe_str(g, "ledger_id") for g in gls_)
        amt_str = " + ".join(f"{a:.2f}" for a in amts)
        return ({
            "ledger_id":        id_str,
            "bank_id":          getattr(bank, "row_index", None),
            "adjustment_type":  f"Many-to-One Aggregation ({len(gls_)} items)",
            "confidence_score": "High",
            "details": (
                f"Bank ({b_amt:.2f}) = sum of {len(gls_)} ledger entries ({amt_str})."
            ),
        }, None)

    def _match_combos(self, anchor_is_gl: bool) -> None:
        base_window = 3
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")

        anchors = gls if anchor_is_gl else bks
        members = bks if anchor_is_gl else gls
        a_dt = gl_dt if anchor_is_gl else bk_dt
        m_dt = bk_dt if anchor_is_gl else gl_dt
        
        # (anchor amount fn, member amount fn) picked by the anchor's direction
        a_out, a_in = (self.gl_out, self.gl_in) if anchor_is_gl else (self.bank_out, self.bank_in)
        m_out, m_in = (self.bank_out, self.bank_in) if anchor_is_gl else (self.gl_out, self.gl_in)
        
        # text compared as in the original: S12 (gl name, narration); S13 passes counterpart=narration
        a_text_attr = "account_name" if anchor_is_gl else "narration"
        m_text_attr = "narration" if anchor_is_gl else "account_name"

        m_order = sorted(
            (m_dt[k].toordinal(), k) 
            for k in range(len(members)) 
            if m_dt[k] is not None
        )
        m_keys = [o for o, _ in m_order]
        used_a: Set[int] = set()
        used_m: Set[int] = set()
        used_gl = used_a if anchor_is_gl else used_m
        used_bk = used_m if anchor_is_gl else used_a

        accepted_all: list = []
        active: List[int] = [
            k for k, r in enumerate(anchors) 
            if a_dt[k] is not None
        ]

        while active:
            edges: List[Edge] = []
            has_edge: Set[int] = set()
            maybe_more: Set[int] = set()

            for a in active:
                rec = anchors[a]
                is_out = a_out(rec) > 0
                a_amt = a_out(rec) if is_out else a_in(rec)
                if a_amt <= 0:
                    continue
                
                m_amt_of = m_out if is_out else m_in

                valid, idx_of = [], {}
                widest = base_window + self._CROSS_MONTH_BUFFER
                early_bd = self.COMBO_EARLY_BD if self.ALLOW_BANK_BEFORE_LEDGER else 0
                if anchor_is_gl:
                    lo_d, hi = self._cal.date_bounds(a_dt[a], -early_bd, widest)
                else:
                    lo_d, hi_d = self._cal.date_bounds(a_dt[a], -widest, early_bd)
                lo, hi = lo_d.toordinal(), hi_d.toordinal()
                ks = sorted(m_order[q][1] for q in range(bisect_left(m_keys, lo), bisect_right(m_keys, hi)))
                for k in ks:
                    m = members[k]
                    if k in used_m or m_dt[k] is None:
                        continue
                    
                    amt = m_amt_of(m)
                    if amt <= 0:
                        continue
                    
                    # diff is always bank_date - ledger_date (as in the original)
                    d_gl, d_bk = (a_dt[a], m_dt[k]) if anchor_is_gl else (m_dt[k], a_dt[a])
                    lag = self._lag(d_gl, d_bk)
                    if -early_bd <= lag <= self._win(base_window, d_gl, d_bk):
                        valid.append((m, amt))
                        idx_of[id(m)] = k

                truncated = len(valid) > self.MAX_COMBINATION_POOL_SIZE
                valid = self._closest_candidates(
                    valid, a_amt, self.MAX_COMBINATION_POOL_SIZE,
                    counterpart_text=_safe_str(rec, a_text_attr),
                    text_of=lambda r, _attr=m_text_attr: _safe_str(r, _attr),
                )
                
                if truncated:
                    maybe_more.add(a)

                sim_cache: Dict[int, float] = {}

                def sim_of(m):
                    s = sim_cache.get(id(m))
                    if s is None:
                        if anchor_is_gl:
                            s = text_similarity(_safe_str(rec, "account_name"), _safe_str(m, "narration"))
                        else:
                            s = text_similarity(_safe_str(m, "account_name"), _safe_str(rec, "narration"))
                        sim_cache[id(m)] = s
                    return s

                # Best combination for this anchor = highest *average* narration similarity
                # (so one good row padded with unrelated rows scores poorly), with a small
                # preference for fewer items. Acceptance gate is unchanged: max similarity > 0.15.
                best = None           # (score, tie, combo)
                search = find_subsets(
                    [int(round(x[1]*100)) for x in valid],
                    int(round(a_amt * 100)),
                    int(round(self._tol * 100)),
                    min_size=2,
                    max_size=self.MAX_COMBINATION_SIZE,
                    max_nodes=self.SUBSET_MAX_NODES,
                    max_solutions=self.SUBSET_MAX_SOLUTIONS
                )
                for sel in search.solutions:
                    combo = tuple(valid[i] for i in sel)
                    r = len(combo)
                    sims = [sim_of(x[0] for x in combo)]
                    if max(sims) <= 0.15:
                        continue
                    
                    score = sum(sims) / r - 0.02 * (r - 2)
                    if best is None or score > best[0] + 1e-12:
                        best = (score, 0, combo)
                
                if best is None:
                    continue
                
                score, _tie, combo = best
                midx = tuple(sorted(idx_of[id(x[0])] for x in combo))
                recs = [x[0] for x in combo]
                amts = [x[1] for x in combo]
                
                if anchor_is_gl:
                    build = partial(self._build_aggregated, rec, recs, amts, a_amt)
                    edges.append((score, (a,), midx, build))
                else:
                    build = partial(self._build_many_to_one, recs, rec, amts, a_amt)
                    edges.append((score, midx, (a,), build))
                has_edge.add(a)

            accepted = self._select(edges, used_gl, used_bk, gls, bks)
            accepted_all.extend(accepted)
            if not accepted:
                break
            matched_anchors = {
                (gi if anchor_is_gl else bi)[0] if (gi if anchor_is_gl else bi) else None 
                for gi, bi, _ in accepted
            }
            blocked = has_edge - matched_anchors
            active = sorted((blocked | maybe_more) - matched_anchors)

        self._emit(accepted_all, gls, bks)
        self._rebuild_pools(gls, bks, used_gl, used_bk)

    def match_aggregated_transactions(self) -> None:
        self._match_combos(anchor_is_gl=True)

    def match_many_to_one_aggregation(self) -> None:
        self._match_combos(anchor_is_gl=False)


    # Strategy 14 - One-to-Many Split Charge  (1 Ledger : 1 Bank where bank = 2×)
    def _build_split_charge(
        self, bk, gl, b_amt, l_amt
    ):
        match = {
            "ledger_id":        getattr(gl, "ledger_id", "N/A"),
            "bank_id":          getattr(bk, "row_index", "N/A"),
            "adjustment_type":  "Split Charge Match (1st Half)",
            "confidence_score": "High",
            "details": (
                f"Bank Row [{getattr(bk, 'row_index', '?')}] "
                f"({_get_date(bk)}, ₹{b_amt:.2f}, "
                f"'{getattr(bk, 'narration', 'N/A')}') "
                f"is exactly 2× Ledger [{getattr(gl, 'ledger_id', '?')}] "
                f"({_get_date(gl)}, ₹{l_amt:.2f}, "
                f"'{getattr(gl, 'account_name', 'N/A')}')."
            ),
        }
        audit = AuditInvestigationItem(
            bank_row_index  = getattr(bk, "row_index", -1),
            narration       = f"{getattr(bk, 'narration', 'Bank Charge')} (Unrecorded 2nd Half)",
            amount          = l_amt,
            direction       = "debit",
            flag_reason     = "Bank deducted exactly 2× the recorded ledger expense.",
            action_required = f"Post adjusting GL expense journal entry for ₹{l_amt:.2f}.",
        )
        return match, audit

    def match_split_charge(self) -> None:
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_amts = [_get_amt(g) for g in gls]
        gl_dts = [_get_date(g) for g in gls]
        index = _AmountIndex(
            (a, i) 
            for i, a in enumerate(gl_amts) 
            if a > 0 and gl_dts[i] is not None
        )

        edges: List[Edge] = []
        for j, bk in enumerate(bks):
            b_amt, b_date = _get_amt(bk), _get_date(bk)
            if b_amt <= 0 or b_date is None:
                continue
            
            local: List[Edge] = []
            for i in index.between((b_amt - self._tol) / 2, (b_amt + self._tol) / 2):
                l_amt, l_date = gl_amts[i], gl_dts[i]
                if not _amounts_equal(b_amt, l_amt * 2, self._tol):
                    continue
                
                base_window = 30
                window = base_window + self._CROSS_MONTH_BUFFER if (
                    (b_date.year, b_date.month) != (l_date.year, l_date.month)
                ) else base_window
                days = abs((b_date - l_date).days)
                if days > window:
                    continue
                
                sim = text_similarity(_safe_str(gls[i], "account_name"), _safe_str(bk, "narration"))
                score = self._W_TEXT * sim - self._W_DAY * days
                local.append((
                    score, (i,), (j,), partial(
                        self._build_split_charge, bk, gls[i], b_amt, l_amt
                    )
                ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)


    # Strategy 15 - Base Fee + Tax Aggregation (1 Ledger : 2 Bank rows)
    FEE_TAX_GL_WINDOW_DAYS: Optional[int] = None

    def _build_fee_tax(
        self, gl, b1, b2, gl_amt, b1_amt, b2_amt, adjacency
    ):
        return ({
            "ledger_id":        _safe_str(gl, "ledger_id"),
            "bank_id":          f"{getattr(b1, 'row_index', 'N/A')} & {getattr(b2, 'row_index', 'N/A')}",
            "adjustment_type":  "Base Fee + Tax Lumped GL Match",
            "confidence_score": "High",
            "details": (
                f"Lumped ledger (₹{gl_amt:.2f}) matched bank fee "
                f"(₹{b1_amt:.2f}) + tax/GST (₹{b2_amt:.2f}). "
                f"Rows not required to be adjacent - matched by date proximity "
                f"(≤{adjacency} day(s) apart)."
            ),
        }, None)

    def match_base_fee_plus_tax(self) -> None:
        base_adjacency_days = 2
        gls, bks = list(self.ledger_pool), list(self.bank_pool)
        gl_dt, bk_dt = self._dates(gls, "transaction_date"), self._dates(bks, "date")

        edges: List[Edge] = []
        for i, gl in enumerate(gls):
            is_out = self.gl_out(gl) > 0
            gl_amt = self.gl_out(gl) if is_out else self.gl_in(gl)
            if gl_amt <= 0 or gl_dt[i] is None:
                continue
            
            amt_of = self.bank_out if is_out else self.bank_in
            cand = [
                (amt_of(b), j) for j, b in enumerate(bks)
                if amt_of(b) > 0 and bk_dt[j] is not None
                and (
                    self.FEE_TAX_GL_WINDOW_DAYS is None or 
                    abs(self._lag(gl_dt[i], bk_dt[j])) <= self.FEE_TAX_GL_WINDOW_DAYS
                )
            ]
            index = _AmountIndex(cand)
            amt_by_j = {j: a for a, j in cand}

            local: List[Edge] = []
            for a1, j1 in cand:
                for j2 in index.between(gl_amt - a1 - self._tol, gl_amt - a1 + self._tol):
                    if j2 <= j1:
                        continue                          # unordered pair, lower pool index = "base fee"
                    
                    a2 = amt_by_j[j2]
                    if not _amounts_equal(a1 + a2, gl_amt, self._tol):
                        continue
                    
                    gap = self._lag(bk_dt[j1], bk_dt[j2])
                    adjacency = self._win(base_adjacency_days, bk_dt[j1], bk_dt[j2])
                    if abs(gap) > adjacency:
                        continue
                    
                    s1 = text_similarity(_safe_str(gl, "account_name"), _safe_str(bks[j1], "narration"))
                    s2 = text_similarity(_safe_str(gl, "account_name"), _safe_str(bks[j2], "narration"))
                    score = self._W_TEXT * (s1 + s2) / 2 - self._W_DAY * abs(gap)
                    local.append((
                        score, (i,), (j1, j2), partial(
                            self._build_fee_tax, gl, bks[j1], bks[j2], gl_amt, a1, a2, adjacency
                        )
                    ))
            self._extend_capped(edges, local)
        self._assign(edges, gls, bks)
        
    
    # Orchestrator
    def run(self) -> dict:

        # Stage 0: Pre-match cleansing 
        self.cleanse_zero_amount_metadata()  
        self.flag_ghost_reversals()           

        # Stage 1-9: Standard heuristics
        self.match_deposit_in_transit()       
        self.match_outstanding_checks()     
        self.match_bank_service_charges()     
        self.match_text_similarity()          
        self.match_transposition_errors()     
        self.match_rounding_differences()     
        self.match_nsf_returned_items()       
        self.match_interest_income()          
        self.match_discounts_and_taxes()      

        # Stage 10-13: Zero-sum & aggregations 
        self.match_bank_side_zero_sum()       
        self.match_ledger_side_zero_sum()    
        self.match_aggregated_transactions()  
        self.match_many_to_one_aggregation()  

        # Stage 14-15: Specialised residuals 
        self.match_split_charge()             
        self.match_base_fee_plus_tax()        

        return {
            "FUZZY_MATCHES": self.fuzzy_matches,
            "UNRECONCILED_ITEMS": {
                "ledger": self.ledger_pool,
                "bank":   self.bank_pool,
            },
            "IGNORED_METADATA":    self.ignored_metadata_records,
            "AUDIT_INVESTIGATION": self.audit_investigation_items,
        }


def fuzzy_matcher(
    pending_ledger: List[LedgerFormat],
    pending_bank:   List[BankStatement],
    tolerances:     dict = None,
    same_side:      bool = True,
) -> dict:
    print("[fuzzy_matcher] starting")
    return FuzzyMatcher(
        pending_ledger,
        pending_bank,
        same_side  = same_side,
        tolerances = tolerances,
    ).run()
    
