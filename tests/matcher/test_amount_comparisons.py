from __future__ import annotations

from matcher.ai_matcher.ai_utils import (
    _filter_candidates_for_bank,
    _passes_1to1_bouncer,
    _passes_many_to_one_bouncer,
)
from matcher.exact_match import exact_match
from matcher.memory_match import memory_matcher
from schema import BankStatement, LedgerFormat
from matcher.utils.helper import _amounts_equal


def test_amount_comparison_uses_integer_paise() -> None:
    assert _amounts_equal(100.10, 100.1000000001, 0.0)
    assert not _amounts_equal(100.10, 100.11, 0.0)
    assert _amounts_equal(100.10, 100.11, 0.01)


def test_exact_match_accepts_float_artifact_with_zero_tolerance() -> None:
    ledger = LedgerFormat(
        ledger_id="L-1",
        account_name="Vendor",
        transaction_date="2025-01-15",
        debit_amount=100.10,
    )
    bank = BankStatement(
        row_index=1,
        bank_name="Test Bank",
        template_version="1",
        date="2025-01-15",
        debit_amount=100.1000000001,
    )

    result = exact_match([ledger], [bank], amount_tol=0.0)

    assert len(result["EXACT_MATCHES"]) == 1


def test_ai_candidate_filter_keeps_same_paise_amounts() -> None:
    ledger = LedgerFormat(
        ledger_id="L-1",
        account_name="Vendor",
        transaction_date="2025-01-15",
        debit_amount=100.1000000001,
    )
    bank = BankStatement(
        row_index=1,
        bank_name="Test Bank",
        template_version="1",
        date="2025-01-15",
        debit_amount=100.10,
    )

    assert _filter_candidates_for_bank(
        bank,
        [ledger],
        date_window=0,
        tol=0.0,
    ) == [ledger]


def test_ai_bouncers_compare_single_amounts_and_totals_in_paise() -> None:
    ledger = [
        LedgerFormat(ledger_id="L-1", account_name="Vendor A", debit_amount=0.1),
        LedgerFormat(ledger_id="L-2", account_name="Vendor B", debit_amount=0.2),
    ]
    bank = BankStatement(
        row_index=1,
        bank_name="Test Bank",
        template_version="1",
        debit_amount=0.3,
    )

    single_ledger = LedgerFormat(
        ledger_id="L-3",
        account_name="Vendor C",
        debit_amount=100.10,
    )
    single_bank = BankStatement(
        row_index=2,
        bank_name="Test Bank",
        template_version="1",
        debit_amount=100.1000000001,
    )

    assert _passes_1to1_bouncer(single_ledger, single_bank, tol=0.0)[0]
    assert _passes_many_to_one_bouncer(ledger, bank, tol=0.0)[0]


def test_memory_match_accepts_float_artifact_with_zero_tolerance() -> None:
    class RecognizedMemory:
        @staticmethod
        def is_recognized(_ledger_text: str, _bank_text: str) -> bool:
            return True

        @staticmethod
        def lookup(_ledger_text: str, _bank_text: str):
            return None

    ledger = LedgerFormat(
        ledger_id="L-1",
        account_name="Vendor",
        debit_amount=100.10,
    )
    bank = BankStatement(
        row_index=1,
        bank_name="Test Bank",
        template_version="1",
        narration="Vendor",
        debit_amount=100.1000000001,
    )

    matches, ledger_left, bank_left = memory_matcher(
        [ledger],
        [bank],
        memory=RecognizedMemory(),
        same_side=True,
        amount_tol=0.0,
    )

    assert len(matches) == 1
    assert ledger_left == []
    assert bank_left == []
