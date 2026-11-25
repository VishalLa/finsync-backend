"""Direct reconciliation tests for the committed bank/ledger fixtures.

These are deliberately not API tests: files are parsed by the loader and fed
straight to ``matcher.reconcile``.  The AI phase is replaced with an empty
result so the deterministic bank matcher is tested without requiring Ollama.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Keep the test independent from host-only application settings.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["DEBUG"] = "false"
os.environ["STORAGE_DIR"] = "/tmp/latice-bank-matcher-tests"

from entry_point import load_bank_statement, load_ledger
from matcher import reconcile
from service.run_pipelines import RunBankRec


FIXTURES = Path(__file__).parent / "fixtures"


def _run_matcher(ledger_path: Path, bank_path: Path) -> dict:
    """Load one fixture pair and run the deterministic matcher directly."""
    ledger = load_ledger(filepath=str(ledger_path))
    bank = load_bank_statement(filepath=str(bank_path))
    result = reconcile(
        config=SimpleNamespace(),
        ledger_result=ledger,
        bank_result=bank,
        all_warnings=[],
        enable_ai_matching=False,
        enable_residual_reconciliation=False,
    )

    return result


def _assert_matcher_completed(result: dict, fixture_name: str) -> int:
    """Validate matcher/service output even when a fixture has no matches."""
    summary = result["summary"]
    assert summary["ledger_records"] > 0, f"{fixture_name} has no ledger rows"
    assert summary["bank_records"] > 0, f"{fixture_name} has no bank rows"

    collected_matches = RunBankRec._collect_all_matches(result)
    summary_match_count = sum(
        summary.get(key, 0)
        for key in ("exact_matches", "fuzzy_matches", "ai_matches")
    )
    assert len(collected_matches) == summary_match_count, (
        f"{fixture_name} matcher summary does not match service aggregation"
    )
    assert "UNRECONCILED_ITEMS" in result
    return len(collected_matches)


def _assert_has_matches(result: dict, fixture_name: str) -> None:
    match_count = _assert_matcher_completed(result, fixture_name)
    assert match_count > 0, f"{fixture_name} produced no bank matches"


def test_user1_follow_up_pair_reconciles_from_fixtures() -> None:
    result = _run_matcher(
        FIXTURES / "reconciliation/user1-ledger.csv",
        FIXTURES / "reconciliation/user1-bank.csv",
    )
    _assert_has_matches(result, "User 1 follow-up pair")


def test_user2_independent_pair_reconciles_from_fixtures() -> None:
    result = _run_matcher(
        FIXTURES / "reconciliation/user2-ledger.csv",
        FIXTURES / "reconciliation/user2-bank.csv",
    )
    _assert_has_matches(result, "User 2 fixture pair")
