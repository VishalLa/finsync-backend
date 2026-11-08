from __future__ import annotations

from types import SimpleNamespace

import pytest

from matcher.ai_matcher import ai_utils
from matcher.ai_matcher.ai_matche import AIMatcher
from matcher.ai_matcher.residual_reconciler import _generate_journal_drafts
from schema import BankStatement


def _config() -> SimpleNamespace:
    return SimpleNamespace(OLLAMA_NAME="test-model", OLLAMA_URL="http://localhost")


def test_prepare_llm_uses_8192_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    def create_llm(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(ai_utils, "ChatOllama", create_llm)

    ai_utils._prepare_llm(_config())

    assert observed["num_ctx"] == 8192


def test_matcher_reports_and_logs_llm_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ai_utils.reset_shared_llm()

    def fail_initialization(**_kwargs):
        raise ValueError("invalid test configuration")

    monkeypatch.setattr(ai_utils, "ChatOllama", fail_initialization)

    result = AIMatcher(config=_config()).run(
        {"UNRECONCILED_LEDGER": [], "UNRECONCILED_BANK": []}
    )["FINAL_RESULT"]

    assert result["ai_skipped"] is True
    assert "invalid test configuration" in result["ai_skip_reason"]
    assert "Failed to initialize ChatOllama" in caplog.text
    assert "invalid test configuration" in caplog.text
    ai_utils.reset_shared_llm()


def test_residual_drafts_fall_back_when_llm_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ai_utils.reset_shared_llm()

    def fail_initialization(**_kwargs):
        raise ValueError("invalid test configuration")

    monkeypatch.setattr(ai_utils, "ChatOllama", fail_initialization)
    bank_record = BankStatement(
        row_index=1,
        bank_name="Test",
        template_version="1",
        date="2025-01-01",
        narration="unknown vendor",
        debit_amount=100.0,
    )

    drafts, unresolved, skipped, reason = _generate_journal_drafts(
        [{"item": bank_record}],
        config=_config(),
    )

    assert skipped is True
    assert "invalid test configuration" in reason
    assert unresolved == []
    assert drafts[0]["source"] == "heuristic_fallback"
    ai_utils.reset_shared_llm()
