from __future__ import annotations

from types import SimpleNamespace

import matcher
from matcher import reconcile
from schema import BankStatement, LedgerFormat


def test_confident_orientation_detection_does_not_override_caller_setting(
    monkeypatch,
) -> None:
    ledger = [LedgerFormat(ledger_id="L-1", account_name="Vendor")]
    bank = [BankStatement(row_index=1, bank_name="Test", template_version="1")]
    observed = {}

    monkeypatch.setattr(
        matcher,
        "detect_same_side",
        lambda *_args: SimpleNamespace(
            confident=True,
            same_side=False,
            reason="test detection",
        ),
    )

    def exact_match(**kwargs):
        observed["exact"] = kwargs["same_side"]
        return {
            "EXACT_MATCHES": [],
            "PENDING_FUZZY_LEDGER": ledger,
            "PENDING_FUZZY_BANK": bank,
        }

    def fuzzy_matcher(**kwargs):
        observed["fuzzy"] = kwargs["same_side"]
        return {
            "FUZZY_MATCHES": [],
            "UNRECONCILED_ITEMS": {"ledger": ledger, "bank": bank},
            "IGNORED_METADATA": [],
            "AUDIT_INVESTIGATION": [],
        }

    def ai_matcher(*, result, same_side, **_kwargs):
        observed["ai"] = same_side
        return {
            "FINAL_RESULT": {
                "AI_MATCHES": [],
                "AI_MANY_MATCHES": [],
                "AUDIT_QUEUE": [],
                "FINAL_RESIDUALS_LEDGER": result["UNRECONCILED_LEDGER"],
                "FINAL_RESIDUALS_BANK": result["UNRECONCILED_BANK"],
                "ai_skipped": True,
                "ai_skip_reason": "test",
            }
        }

    monkeypatch.setattr(matcher, "exact_match", exact_match)
    monkeypatch.setattr(matcher, "fuzzy_matcher", fuzzy_matcher)
    monkeypatch.setattr(matcher, "ai_matcher", ai_matcher)
    warnings = []

    reconcile(
        config=SimpleNamespace(),
        ledger_result={"records": ledger, "warnings": []},
        bank_result={"records": bank, "warnings": []},
        all_warnings=warnings,
        same_side=True,
        enable_residual_reconciliation=False,
    )

    assert observed == {"exact": True, "fuzzy": True, "ai": True}
    assert any(
        "continuing with the configured value" in warning
        for warning in warnings
    )
