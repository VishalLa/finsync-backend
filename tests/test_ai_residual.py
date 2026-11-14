"""Focused, Ollama-free checks for the AI and residual matching stages."""

import json

from langchain_core.runnables import RunnableLambda

from core.config import Config
from matcher.ai_matcher.ai_matcher import ai_match
from matcher.residual_reconciler import reconcile_residuals
from schema import BankStatement, LedgerFormat


def _config() -> Config:
    return Config(
        _env_file=None,
        DEBUG=False,
        SECRET_KEY="test-only",
        JWT_SECRET_KEY="test-only",
        REDIS_URL="redis://127.0.0.1:6379/0",
        DATABASE_URL="sqlite:////tmp/latice-matcher-tests.sqlite",
        STORAGE_DIR="/tmp/latice-matcher-tests-storage",
    )


def _records():
    ledger = LedgerFormat(
        ledger_id="L-1",
        account_name="Acme Office Supplies",
        transaction_date="2026-08-10",
        debit_amount=250.0,
        credit_amount=0.0,
        reference_id="INV-1",
    )
    bank = BankStatement(
        row_index=1,
        bank_name="Test Bank",
        template_version="test",
        date="2026-08-10",
        narration="ACME OFFICE SUPPLIES INV-1",
        debit_amount=250.0,
        credit_amount=0.0,
        txn_id="TX-1",
    )
    return ledger, bank


def test_ai_matcher_runs_with_stub_model_without_ollama():
    ledger, bank = _records()

    def respond(prompt):
        if isinstance(prompt, str) and prompt == "ping":
            return "pong"
        return json.dumps({
            "matches": [{
                "ledger_id": "L-1",
                "bank_id": 1,
                "confidence": 0.99,
                "reasoning": "Matching amount, date, and supplier reference.",
            }]
        })

    stub_llm = RunnableLambda(respond)
    result = ai_match(
        config=_config(),
        result={
            "UNRECONCILED_LEDGER": [ledger],
            "UNRECONCILED_BANK": [bank],
            "MATCHED": [],
        },
        same_side=True,
        llm=stub_llm,
    )["FINAL_RESULT"]

    assert len(result["AI_MATCHES"]) == 1
    assert result["AI_MATCHES"][0]["ledger_id"] == "L-1"
    assert result["AI_MATCHES"][0]["bank_id"] == 1


def test_residual_matcher_resolves_delayed_clearance_without_ollama():
    ledger = LedgerFormat(
        ledger_id="L-2",
        account_name="Northwind Retail",
        transaction_date="2026-08-01",
        credit_amount=1250.0,
    )
    bank = BankStatement(
        row_index=2,
        bank_name="Test Bank",
        template_version="test",
        date="2026-08-20",
        narration="NORTHWIND RETAIL SETTLEMENT",
        credit_amount=1250.0,
    )

    result = reconcile_residuals(
        unreconciled_ledger=[ledger],
        unreconciled_bank=[bank],
        config=_config(),
        same_side=True,
        enable_llm=False,
    )

    assert len(result["timing_matches"]) == 1
    assert result["stats"]["timing_resolved"] == 1
    assert result["still_unreconciled_ledger"] == []
    assert result["still_unreconciled_bank"] == []
