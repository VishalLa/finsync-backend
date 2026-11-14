from __future__ import annotations

from typing import Any, Dict, List, Optional

from schema import BankStatement, LedgerFormat

from .fuzzy_match import FuzzyMatcher
from .relationship import Relationship
from .business_calendar import BusinessCalendar
from .subset_sum import find_subsets

def run_fuzzy_matcher(
    pending_ledger: List[LedgerFormat],
    pending_bank: List[BankStatement],
    tolerances: Optional[Dict[str, float | int]] = None,
    same_side: bool = True,
    calendar: Any = None,
) -> Dict[str, Any]:
    """Run standard fuzzy strategies followed by relationship strategies."""
    fuzzy = FuzzyMatcher(
        pending_ledger=pending_ledger,
        pending_bank=pending_bank,
        same_side=same_side,
        tolerances=tolerances,
        calendar=calendar,
    )
    fuzzy_result = fuzzy.match()

    relationships = Relationship(
        pending_ledger=fuzzy_result["UNRECONCILED_ITEMS"]["ledger"],
        pending_bank=fuzzy_result["UNRECONCILED_ITEMS"]["bank"],
        same_side=same_side,
        tolerances=tolerances,
        calendar=calendar,
    )
    relationships.match()

    fuzzy.fuzzy_matches.extend(relationships.fuzzy_matches)
    fuzzy.proposed_matches = relationships.proposed_matches
    fuzzy.m2m_stats = relationships.m2m_stats
    fuzzy.ledger_pool = relationships.ledger_pool
    fuzzy.bank_pool = relationships.bank_pool
    
    return fuzzy.to_result()


__all__ = [
    "run_fuzzy_matcher",
    "FuzzyMatcher",
    "Relationship",
    "BusinessCalendar",
    "find_subsets"
]
