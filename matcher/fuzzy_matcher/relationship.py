from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date
from time import perf_counter
from typing import Any, Dict, List, Optional, Set, Tuple

from schema import BankStatement, LedgerFormat

from .fuzzy_match import FuzzyMatcher
from .business_calendar import BusinessCalendar
from .subset_sum import find_subsets

from ..base import Matcher
from ..utils.helper import (
    _amounts_equal,
    _safe_str,
    _to_date,
    extract_utr,
    text_similarity,
)


def _enumerate_sums(
    amounts: List[int], 
    max_size: int, 
    budget: int
) -> Tuple[List[Tuple[int, Tuple[int, ...]]], int, bool]:
    """Return bounded subset sums as (sum, original indices) rows."""
    rows: List[Tuple[int, Tuple[int, ...]]] = []
    nodes = 0
    stopped = False

    def visit(start: int, chosen: List[int], total: int) -> None:
        nonlocal nodes, stopped
        if stopped:
            return
        
        nodes += 1
        if nodes > budget:
            stopped = True
            return
        
        if 2 <= len(chosen) <= max_size:
            rows.append((total, tuple(chosen)))
            
        if len(chosen) >= max_size:
            return
        
        for index in range(start, len(amounts)):
            chosen.append(index)
            visit(index + 1, chosen, total + amounts[index])
            chosen.pop()
            if stopped:
                return

    visit(0, [], 0)
    return rows, nodes, not stopped


class Relationship(Matcher):
    """Match residual pools using group relationship strategies only."""

    MAX_COMBINATION_SIZE = 6
    M2M_MAX_COMBINATION_SIZE = 5
    M2M_MAX_NODES = 300_000
    M2M_MAX_SOLUTIONS = 200
    M2M_ACCEPT = 0.60
    M2M_REVIEW = 0.40

    def __init__(
        self,
        pending_ledger: Optional[List[LedgerFormat]] = None,
        pending_bank: Optional[List[BankStatement]] = None,
        *,
        same_side: bool = True,
        tolerances: Optional[Dict[str, float | int]] = None,
        calendar: Optional[BusinessCalendar] = None,
        **kwargs: Any,
    ) -> None:
        legacy_matcher: Optional[FuzzyMatcher] = None
        if pending_bank is None and pending_ledger is not None and hasattr(pending_ledger, "ledger_pool"):
            legacy_matcher = pending_ledger
            pending_ledger = list(legacy_matcher.ledger_pool)
            pending_bank = list(legacy_matcher.bank_pool)
            same_side = legacy_matcher.same_side
            tolerances = legacy_matcher.tolerances
            calendar = legacy_matcher.calendar

        super().__init__(
            pending_ledger=pending_ledger,
            pending_bank=pending_bank,
            same_side=same_side,
            tolerances=tolerances,
            calendar=calendar,
            **kwargs,
        )
        self._legacy_matcher = legacy_matcher
        self._tol = float(self.tolerances.get("DEFAULT", 1.0))
        self._cal = self.calendar
        self.subset_searches = {"one-to-many": 0, "many-to-one": 0}
        
        self._prepare_calendar()
        self.fuzzy_matches: List[Dict[str, Any]] = (
            legacy_matcher.fuzzy_matches if legacy_matcher is not None else []
        )
        self.proposed_matches: List[Dict[str, Any]] = []
        self.m2m_stats: Dict[str, int] = {}

    def _prepare_calendar(self) -> None:
        dates = [
            _to_date(_safe_str(record, attr))
            for record, attr in [
                *((row, "transaction_date") for row in self.ledger_pool),
                *((row, "date") for row in self.bank_pool),
            ]
        ]
        valid = [value for value in dates if value is not None]
        if valid:
            self._cal.prepare(min(valid), max(valid))

    def _amount(self, record: Any, outgoing: bool, ledger: bool) -> float:
        if ledger:
            accessor = "gl_out" if outgoing else "gl_in"
        else:
            accessor = "bank_out" if outgoing else "bank_in"
        return float(getattr(self, accessor)(record))

    def _amount_pair(
        self, 
        ledger: Any, 
        bank: Any
    ) -> Tuple[float, float]:
        for outgoing in (True, False):
            left = self._amount(ledger, outgoing, True)
            right = self._amount(bank, outgoing, False)
            
            if left > 0 and right > 0:
                return left, right
            
        return 0.0, 0.0


    def _date(
        self, 
        record: Any, 
        ledger: bool
    ) -> Optional[date]:
        return _to_date(
            _safe_str(record, "transaction_date" if ledger else "date")
        )


    def _within_window(
        self, 
        ledger: Any, 
        bank: Any, 
        days: int = 3
    ) -> bool:
        left, right = self._date(ledger, True), self._date(bank, False)
        return left is None or right is None or abs(self._cal.lag(left, right)) <= days


    def _reference_match(
        self, 
        ledgers: List[Any], 
        banks: List[Any]
    ) -> bool:
        ledger_refs = {
            value for row in ledgers
            for value in (extract_utr(_safe_str(row, "reference_id")),)
            if value
        }
        
        bank_refs = {
            value for row in banks
            for value in (
                extract_utr(_safe_str(row, "txn_id")),
                extract_utr(_safe_str(row, "narration")),
            )
            if value
        }
        return bool(ledger_refs & bank_refs)


    def _group_score(
        self, 
        ledgers: List[Any], 
        banks: List[Any], 
        difference: float
    ) -> float:
        ref_score = 0.35 if self._reference_match(ledgers, banks) else 0.0
        
        left_text = " ".join(_safe_str(row, "account_name") for row in ledgers)
        right_text = " ".join(_safe_str(row, "narration") for row in banks)
        
        text_score = 0.25 * text_similarity(left_text, right_text)
        date_score = 0.20
        
        all_dates = [self._date(row, True) for row in ledgers]
        all_dates += [self._date(row, False) for row in banks]
        all_dates = [value for value in all_dates if value is not None]
        
        if all_dates:
            span = (max(all_dates) - min(all_dates)).days
            date_score = 0.20 * max(0.0, 1.0 - span / 15.0)
            
        amount_score = 0.25 * max(0.0, 1.0 - difference / max(self._tol, 1.0))
        return round(min(1.0, ref_score + text_score + date_score + amount_score), 4)


    def _append_group_match(
        self, 
        ledgers: List[Any], 
        banks: List[Any], 
        difference: float
    ) -> None:
        ledger_total = sum(max(self.gl_out(row), self.gl_in(row)) for row in ledgers)
        bank_total = sum(max(self.bank_out(row), self.bank_in(row)) for row in banks)
        
        score = self._group_score(
            ledgers=ledgers, 
            banks=banks, 
            difference=difference
        )
        
        self.fuzzy_matches.append({
            "ledger_id": " & ".join(str(getattr(row, "ledger_id", "")) for row in ledgers),
            "bank_id": " & ".join(str(getattr(row, "row_index", "")) for row in banks),
            "ledger_components": [
                {
                    "id": getattr(row, "ledger_id", None),
                    "ledger_id": getattr(row, "ledger_id", None),
                    "amount": max(self.gl_out(row), self.gl_in(row)),
                }
                for row in ledgers
            ],
            "bank_components": [
                {
                    "id": getattr(row, "row_index", None),
                    "bank_id": getattr(row, "row_index", None),
                    "amount": max(self.bank_out(row), self.bank_in(row)),
                }
                for row in banks
            ],
            "ledger_total": round(ledger_total, 2),
            "bank_total": round(bank_total, 2),
            "difference": round(difference, 2),
            "adjustment_type": f"Many-to-Many Group ({len(ledgers)}x{len(banks)})" if len(ledgers) > 1 and len(banks) > 1 else f"Aggregated Split Transaction ({len(banks)} items)" if len(ledgers) == 1 else f"Many-to-One Aggregation ({len(ledgers)} items)",
            "confidence_score": "High" if score >= 0.75 else "Medium",
            "score": score,
            "details": "Group totals, dates, and available references were reconciled.",
            "match_phase": "fuzzy",
        })


    def _match_aggregations(
        self, 
        one_ledger: bool
    ) -> None:
        pass_name = "one-to-many" if one_ledger else "many-to-one"
        self.subset_searches[pass_name] = 0
        anchors = list(self.ledger_pool if one_ledger else self.bank_pool)
        members = list(self.bank_pool if one_ledger else self.ledger_pool)
        
        used_anchors: Set[int] = set()
        used_members: Set[int] = set()
        
        for anchor_index, anchor in enumerate(anchors):
            if anchor_index in used_anchors:
                continue
            
            found = None
            for outgoing in (True, False):
                target = self._amount(anchor, outgoing, one_ledger)
                if target <= 0:
                    continue
                
                eligible = []
                for member_index, member in enumerate(members):
                    if member_index in used_members:
                        continue
                    
                    ledger, bank = (anchor, member) if one_ledger else (member, anchor)
                    if not self._within_window(ledger, bank):
                        continue
                    
                    amount = self._amount(member, outgoing, not one_ledger)
                    if amount > 0:
                        eligible.append((member_index, amount))
                        
                self.subset_searches[pass_name] += 1
                subset = find_subsets(
                    [int(round(amount * 100)) for _, amount in eligible],
                    int(round(target * 100)),
                    int(round(self._tol * 100)),
                    min_size=2,
                    max_size=self.MAX_COMBINATION_SIZE,
                    max_solutions=30,
                )
                
                if subset.solutions:
                    found = (outgoing, [eligible[i][0] for i in subset.solutions[0]], target)
                    break
                
            if found is None:
                continue
            
            _, member_indices, _target = found
            selected = [members[index] for index in member_indices]
            ledgers, banks = ( [anchor], selected ) if one_ledger else ( selected, [anchor] )
            self._append_group_match(
                ledgers=ledgers, 
                banks=banks, 
                difference=abs(
                    sum(max(self.bank_out(row), self.bank_in(row)) for row in banks) - 
                    sum(max(self.gl_out(row), self.gl_in(row)) for row in ledgers)
                )
            )
            used_anchors.add(anchor_index)
            used_members.update(member_indices)
            
        if one_ledger:
            self.ledger_pool = [row for i, row in enumerate(anchors) if i not in used_anchors]
            self.bank_pool = [row for i, row in enumerate(members) if i not in used_members]
        else:
            self.bank_pool = [row for i, row in enumerate(anchors) if i not in used_anchors]
            self.ledger_pool = [row for i, row in enumerate(members) if i not in used_members]


    def match_one_to_many(self) -> None:
        self._match_aggregations(one_ledger=True)
        

    def match_many_to_one(self) -> None:
        self._match_aggregations(one_ledger=False)
        

    def match_many_to_many(self) -> Dict[str, Any]:
        ledgers = sorted(self.ledger_pool, key=lambda row: str(getattr(row, "ledger_id", "")))
        banks = sorted(self.bank_pool, key=lambda row: str(getattr(row, "row_index", "")))
        
        tolerance = int(round(self._tol * 100))
        candidates: List[Dict[str, Any]] = []
        
        left_nodes = right_nodes = 0
        left_complete = right_complete = True
        components_searched = 0
        
        for outgoing in (True, False):
            ledger_amounts = [
                int(round(self._amount(row, outgoing, ledger=True) * 100))
                for row in ledgers
            ]
            bank_amounts = [
                int(round(self._amount(row, outgoing, ledger=False) * 100))
                for row in banks
            ]
            if not any(ledger_amounts) or not any(bank_amounts):
                continue
            
            components_searched += 1
            ledger_rows, nodes, complete = _enumerate_sums(
                amounts=ledger_amounts, 
                max_size=self.M2M_MAX_COMBINATION_SIZE, 
                budget=self.M2M_MAX_NODES
            )
            left_nodes += nodes
            left_complete = left_complete and complete
            bank_rows, nodes, complete = _enumerate_sums(
                amounts=bank_amounts, 
                max_size=self.M2M_MAX_COMBINATION_SIZE, 
                budget=self.M2M_MAX_NODES
            )
            
            right_nodes += nodes
            right_complete = right_complete and complete
            
            sorted_bank_rows = sorted(bank_rows)
            sorted_bank_sums = [row[0] for row in sorted_bank_rows]

            for ledger_total, ledger_indices in ledger_rows:
                start = bisect_left(sorted_bank_sums, ledger_total - tolerance)
                end = bisect_right(sorted_bank_sums, ledger_total + tolerance)
                
                for bank_total, bank_indices in sorted_bank_rows[start:end]:
                    if len(ledger_indices) < 2 or len(bank_indices) < 2:
                        continue
                    
                    gls = [ledgers[index] for index in ledger_indices]
                    bks = [banks[index] for index in bank_indices]
                    if any(
                        not self._within_window(gl, bk, days=10)
                        for gl in gls for bk in bks
                    ):
                        continue
                    
                    difference = abs(ledger_total - bank_total) / 100
                    score = self._group_score(gls, bks, difference)
                    candidates.append({
                        "ledger_indices": ledger_indices,
                        "bank_indices": bank_indices,
                        "ledger_total": ledger_total,
                        "bank_total": bank_total,
                        "difference": difference,
                        "score": score,
                    })
                    
        candidates.sort(key=lambda row: (-row["score"], row["ledger_indices"], row["bank_indices"]))
        
        used_ledger: Set[int] = set()
        used_bank: Set[int] = set()
        proposals: List[Dict[str, Any]] = []
        accepted = []
        
        for candidate in candidates:
            li, bi = candidate["ledger_indices"], candidate["bank_indices"]
            if (
                any(index in used_ledger for index in li) or 
                any(index in used_bank for index in bi)
            ):
                continue
            
            gls, bks = [ledgers[index] for index in li], [banks[index] for index in bi]
            candidate_data = {
                "ledger_ids": [getattr(row, "ledger_id", None) for row in gls],
                "bank_ids": [getattr(row, "row_index", None) for row in bks],
                "ledger_total": candidate["ledger_total"] / 100,
                "bank_total": candidate["bank_total"] / 100,
                "difference": candidate["difference"],
                "score": candidate["score"],
            }
            
            if candidate["score"] >= self.M2M_ACCEPT:
                accepted.append((candidate, gls, bks))
                used_ledger.update(li)
                used_bank.update(bi)
            elif candidate["score"] >= self.M2M_REVIEW:
                proposals.append({
                    "candidate_id": f"m2m-{len(proposals) + 1:04d}",
                    **candidate_data, 
                    "reason": "below_accept"
                })
                
        for candidate, gls, bks in accepted:
            self._append_group_match(
                ledgers=gls, 
                banks=bks, 
                difference=candidate["difference"]
            )
            
        self.ledger_pool = [row for index, row in enumerate(ledgers) if index not in used_ledger]
        self.bank_pool = [row for index, row in enumerate(banks) if index not in used_bank]
        self.proposed_matches = proposals
        stats = {
            "m2m_components_skipped": 0,
            "m2m_components_searched": components_searched,
            "m2m_budget_exhaustions": int(not left_complete) + int(not right_complete),
            "m2m_accepted_matches": len(accepted),
            "m2m_ledger_rows_consumed": len(used_ledger),
            "m2m_bank_rows_consumed": len(used_bank),
            "m2m_proposed_candidates": len(proposals),
            "m2m_nodes": left_nodes + right_nodes,
        }
        self.m2m_stats = stats
        self._sync_legacy()
        return {
            "PROPOSED_MATCHES": proposals, 
            "M2M_STATS": stats
        }

    def _sync_legacy(self) -> None:
        if self._legacy_matcher is not None:
            self._legacy_matcher.ledger_pool = self.ledger_pool
            self._legacy_matcher.bank_pool = self.bank_pool
            self._legacy_matcher.fuzzy_matches = self.fuzzy_matches
            self._legacy_matcher.proposed_matches = self.proposed_matches
            self._legacy_matcher.m2m_stats = self.m2m_stats

    def match(self) -> Dict[str, Any]:
        for label, action in (
            ("one-to-many", self.match_one_to_many),
            ("many-to-one", self.match_many_to_one),
        ):
            started = perf_counter()
            print(
                f"[fuzzy] relationship {label} starting "
                f"(ledger={len(self.ledger_pool)}, bank={len(self.bank_pool)})"
            )
            try:
                action()
            finally:
                print(
                    f"[fuzzy] relationship {label} finished in "
                    f"{perf_counter() - started:.3f}s "
                    f"(ledger={len(self.ledger_pool)}, bank={len(self.bank_pool)}, "
                    f"subset_searches={self.subset_searches[label]})"
                )

        # Temporarily skip many-to-many matching to keep resource use bounded.
        # Re-enable with `return self.match_many_to_many()` when capacity allows.
        print(
            f"[fuzzy] relationship many-to-many skipped "
            f"(ledger={len(self.ledger_pool)}, bank={len(self.bank_pool)})"
        )
        stats = {
            "m2m_components_skipped": 1,
            "m2m_components_searched": 0,
            "m2m_budget_exhaustions": 0,
            "m2m_accepted_matches": 0,
            "m2m_ledger_rows_consumed": 0,
            "m2m_bank_rows_consumed": 0,
            "m2m_proposed_candidates": 0,
            "m2m_nodes": 0,
        }
        self.proposed_matches = []
        self.m2m_stats = stats
        self._sync_legacy()
        return {"PROPOSED_MATCHES": [], "M2M_STATS": stats}

