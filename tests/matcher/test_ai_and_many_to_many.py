from __future__ import annotations


from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from matcher.ai_matcher import ai_matcher
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

    monkeypatch.setattr(ai_matcher, "_safe_parse_json", lambda output, _schema: output)
    matcher = ai_matcher.AIMatcher(
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



from types import SimpleNamespace

import pytest

from matcher.ai_matcher import ai_utils
from matcher.ai_matcher.ai_matcher import AIMatcher
from matcher.residual_reconciler import ResidualReconciler
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

    result = AIMatcher(config=_config()).match(
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

    class UnavailableLLM:
        def invoke(self, _prompt):
            raise RuntimeError("invalid test configuration")

    reconciler = ResidualReconciler(
        pending_ledger=[],
        pending_bank=[bank_record],
        config=_config(),
        llm=UnavailableLLM(),
    )
    drafts, unresolved, skipped, reason = reconciler._generate_journal_drafts(
        [{"item": bank_record}]
    )

    assert skipped is True
    assert "invalid test configuration" in reason
    assert unresolved == []
    assert drafts[0]["source"] == "heuristic_fallback"
    ai_utils.reset_shared_llm()


"""Focused, Ollama-free checks for the AI and residual matching stages."""

import json

from langchain_core.runnables import RunnableLambda

from app.shared.settings import Config
from matcher.ai_matcher.ai_matcher import AIMatcher
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
    result = AIMatcher(
        config=_config(),
        pending_ledger=[ledger],
        pending_bank=[bank],
        same_side=True,
        llm=stub_llm,
    ).match()["FINAL_RESULT"]

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



from types import SimpleNamespace

from langchain_core.runnables import RunnableLambda

from matcher.ai_matcher import ai_matcher
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

    monkeypatch.setattr(ai_matcher, "_safe_parse_json", lambda output, _schema: output)
    matcher = ai_matcher.AIMatcher(
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



import itertools
import random
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
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
