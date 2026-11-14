from __future__ import annotations

import itertools
import random
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from matcher.fuzzy_matcher.relationship import Relationship, _enumerate_sums
from matcher.fuzzy_matcher.fuzzy_match import FuzzyMatcher
from schema import BankStatement, LedgerFormat


def test_sum_join_enumeration_matches_brute_force():
    rng = random.Random(12)
    for _ in range(40):
        left = [rng.randint(1, 50) for _ in range(rng.randint(2, 8))]
        right = [rng.randint(1, 50) for _ in range(rng.randint(2, 8))]
        tolerance = rng.randint(0, 4)
        left_rows, _, left_complete = _enumerate_sums(left, 5, 100_000)
        right_rows, _, right_complete = _enumerate_sums(right, 5, 100_000)
        assert left_complete and right_complete
        right_sorted = sorted(right_rows)
        right_sums = [row[0] for row in right_sorted]
        joined = set()
        for lsum, lsel in left_rows:
            lo = bisect_left(right_sums, lsum - tolerance)
            hi = bisect_right(right_sums, lsum + tolerance)
            joined.update((lsel, right_sorted[i][1]) for i in range(lo, hi))
        brute = set()
        for lsize in range(2, min(5, len(left)) + 1):
            for lsel in itertools.combinations(range(len(left)), lsize):
                lsum = sum(left[i] for i in lsel)
                for rsize in range(2, min(5, len(right)) + 1):
                    for rsel in itertools.combinations(range(len(right)), rsize):
                        if abs(lsum - sum(right[i] for i in rsel)) <= tolerance:
                            brute.add((lsel, rsel))
        assert joined == brute


def test_residual_m2m_preserves_all_component_ids_and_is_order_stable():
    def records(reverse=False):
        ledger = [LedgerFormat(ledger_id=f"L{i}", account_name="Acme vendor payment", transaction_date="2026-01-05",
                              credit_amount=amount, reference_id="UTR-77")
                  for i, amount in [(1, 60.0), (2, 40.0)]]
        bank = [BankStatement(row_index=i, bank_name="test", template_version="t", date="2026-01-06",
                              narration="Acme vendor payment", credit_amount=amount, txn_id="UTR-77")
                for i, amount in [(11, 55.0), (12, 45.0)]]
        return (ledger[::-1], bank[::-1]) if reverse else (ledger, bank)
    outputs = []
    for reverse in (False, True):
        ledger, bank = records(reverse)
        matcher = FuzzyMatcher(ledger, bank, tolerances={"DEFAULT": 1.0})
        result = Relationship(matcher).match_many_to_many()
        outputs.append((matcher.fuzzy_matches, result))
    assert outputs[0][0] == outputs[1][0]
    match = outputs[0][0][0]
    assert match["adjustment_type"] == "Many-to-Many Group (2x2)"
    assert set(match["ledger_id"].split(" & ")) == {"L1", "L2"}
    assert set(match["bank_id"].split(" & ")) == {"11", "12"}
    assert len(match["ledger_components"]) == len(match["bank_components"]) == 2
    assert outputs[0][1]["M2M_STATS"]["m2m_ledger_rows_consumed"] == 2
