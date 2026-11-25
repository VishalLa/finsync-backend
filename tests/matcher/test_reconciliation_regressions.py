from __future__ import annotations

from types import SimpleNamespace

import pytest

import matcher
from matcher import reconcile
from matcher.fuzzy_matcher import FuzzyMatcher, _AmountIndex
from schema import BankStatement, LedgerFormat


def _three_row_inputs() -> tuple[list[LedgerFormat], list[BankStatement]]:
    ledger = [
        LedgerFormat(
            ledger_id=f"L-{index}",
            account_name=f"Vendor {index}",
            transaction_date=f"2025-01-0{index}",
            debit_amount=100.0 + index,
            reference_id=f"REF-{index}",
        )
        for index in range(1, 4)
    ]
    bank = [
        BankStatement(
            row_index=index,
            bank_name="Regression Bank",
            template_version="1",
            date=f"2025-01-0{index}",
            narration=f"Vendor {index}",
            debit_amount=100.0 + index,
            txn_id=f"REF-{index}",
        )
        for index in range(1, 4)
    ]
    return ledger, bank


def test_three_row_reconciliation_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, bank = _three_row_inputs()
    result = reconcile(
        config=SimpleNamespace(),
        ledger_result={"records": ledger, "warnings": []},
        bank_result={"records": bank, "warnings": []},
        all_warnings=[],
        enable_ai_matching=False,
        enable_residual_reconciliation=False,
    )

    assert result["summary"]["ledger_records"] == 3
    assert result["summary"]["bank_records"] == 3
    assert result["summary"]["exact_matches"] == 3
    assert result["summary"]["fuzzy_matches"] == 0


def test_amount_index_uses_declared_slots() -> None:
    index = _AmountIndex([(10.0, 0), (12.0, 1), (20.0, 2)])

    assert index.between(11.0, 13.0) == [1]


def test_fuzzy_matcher_only_adds_present_audit_items() -> None:
    matcher = FuzzyMatcher([], [])
    ledger = LedgerFormat(ledger_id="L-1", account_name="Vendor")
    bank = BankStatement(row_index=1, bank_name="Test", template_version="1")
    matched = {"match": True}
    audit = {"flag_reason": "manual review"}

    matcher._emit(
        [
            ((0,), (0,), lambda: (matched, None)),
            ((0,), (0,), lambda: (matched, audit)),
        ],
        [ledger],
        [bank],
    )

    assert matcher.fuzzy_matches == [matched, matched]
    assert matcher.audit_investigation_items == [audit]


