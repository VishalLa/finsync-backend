from __future__ import annotations

from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from matcher.ai_matcher import ai_matche
from schema import BankStatement, LedgerFormat


def test_batch_match_includes_records_beyond_first_window_page(monkeypatch) -> None:
    prompts = []
    target_ledger_id = "L-20"
    target_bank_id = 20

    def respond(prompt_value):
        prompt = prompt_value.to_string()
        prompts.append(prompt)
        if target_ledger_id in prompt and f"- {target_bank_id} |" in prompt:
            return SimpleNamespace(
                matches=[
                    SimpleNamespace(
                        ledger_id=target_ledger_id,
                        bank_id=target_bank_id,
                        confidence=1.0,
                        model_dump=lambda: {
                            "ledger_id": target_ledger_id,
                            "bank_id": target_bank_id,
                            "confidence": 1.0,
                            "reasoning": "Matching test records",
                        },
                    )
                ]
            )
        return SimpleNamespace(matches=[])

    monkeypatch.setattr(ai_matche, "_safe_parse_json", lambda output, _schema: output)
    matcher = ai_matche.AIMatcher(
        config=SimpleNamespace(),
        llm=RunnableLambda(respond),
        tol=0.0,
    )
    ledger = [
        LedgerFormat(
            ledger_id=f"L-{index}",
            account_name=f"Vendor {index}",
            transaction_date="2025-01-15",
            debit_amount=100.0,
        )
        for index in range(21)
    ]
    bank = [
        BankStatement(
            row_index=index,
            bank_name="Test Bank",
            template_version="1",
            date="2025-01-15",
            narration=f"Payment {index}",
            debit_amount=100.0,
        )
        for index in range(21)
    ]

    result, ledger_left, bank_left = matcher.batch_match({
        "UNRECONCILED_LEDGER": ledger,
        "UNRECONCILED_BANK": bank,
        "MATCHED": [],
    })

    assert len(prompts) == 4
    assert all(
        sum(line.startswith("- L-") for line in prompt.splitlines()) <= 20
        for prompt in prompts
    )
    assert any(
        target_ledger_id in prompt and f"- {target_bank_id} |" in prompt
        for prompt in prompts
    )
    assert result["AI_MATCHES"][0]["ledger_id"] == target_ledger_id
    assert [record.ledger_id for record in ledger_left] == [
        record.ledger_id for record in ledger if record.ledger_id != target_ledger_id
    ]
    assert [record.row_index for record in bank_left] == [
        record.row_index for record in bank if record.row_index != target_bank_id
    ]
