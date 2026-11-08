from __future__ import annotations

from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from matcher.ai_matcher import ai_matche
from schema import BankStatement, LedgerFormat


def test_residual_match_searches_all_candidates_and_limits_llm_to_valid_subsets(
    monkeypatch,
) -> None:
    target_ids = ("L-5000", "L-120", "L-80")
    prompts = []
    selected_match = SimpleNamespace(
        bank_id=99,
        ledger_ids=[SimpleNamespace(ledger_id=ledger_id) for ledger_id in target_ids],
        confidence=0.95,
        model_dump=lambda: {
            "bank_id": 99,
            "ledger_ids": [{"ledger_id": ledger_id} for ledger_id in target_ids],
            "confidence": 0.95,
            "reasoning": "The verified ledger entries correspond to this settlement.",
        },
    )

    def respond(prompt_value):
        prompts.append(prompt_value.to_string())
        return SimpleNamespace(matches=[selected_match])

    monkeypatch.setattr(ai_matche, "_safe_parse_json", lambda output, _schema: output)
    matcher = ai_matche.AIMatcher(
        config=SimpleNamespace(),
        llm=RunnableLambda(respond),
        tol=0.0,
    )
    ledger = [
        LedgerFormat(
            ledger_id=f"L-{index}",
            account_name=f"Unrelated Vendor {index}",
            transaction_date="2025-01-15",
            debit_amount=600.0,
        )
        for index in range(13)
    ]
    ledger.extend([
        LedgerFormat(
            ledger_id="L-5000",
            account_name="Acme Settlement",
            transaction_date="2025-01-15",
            debit_amount=5000.0,
        ),
        LedgerFormat(
            ledger_id="L-120",
            account_name="Acme Settlement",
            transaction_date="2025-01-15",
            debit_amount=120.0,
        ),
        LedgerFormat(
            ledger_id="L-80",
            account_name="Acme Settlement",
            transaction_date="2025-01-15",
            debit_amount=80.0,
        ),
    ])
    bank = BankStatement(
        row_index=99,
        bank_name="Test Bank",
        template_version="1",
        date="2025-01-15",
        narration="Acme Settlement",
        debit_amount=5200.0,
    )

    result, ledger_left = matcher.residual_match(ledger, [bank])

    assert len(prompts) == 1
    assert all(ledger_id in prompts[0] for ledger_id in target_ids)
    assert all(f"- L-{index} |" not in prompts[0] for index in range(13))
    assert result["AI_MANY_MATCHES"][0]["bank_id"] == 99
    assert [record.ledger_id for record in ledger_left] == [
        record.ledger_id for record in ledger[:13]
    ]
