from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import matcher
from matcher import reconcile
from matcher.fuzzy_match import _AmountIndex
from schema import BankStatement, LedgerFormat


def _skip_ai_matcher(*, result: dict, **_kwargs) -> dict:
    return {
        "FINAL_RESULT": {
            "AI_MATCHES": [],
            "AI_MANY_MATCHES": [],
            "AUDIT_QUEUE": [],
            "FINAL_RESIDUALS_LEDGER": result["UNRECONCILED_LEDGER"],
            "FINAL_RESIDUALS_BANK": result["UNRECONCILED_BANK"],
            "ai_skipped": True,
            "ai_skip_reason": "disabled for regression test",
        }
    }


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
    monkeypatch.setattr(matcher, "ai_matcher", _skip_ai_matcher)

    result = reconcile(
        config=SimpleNamespace(),
        ledger_result={"records": ledger, "warnings": []},
        bank_result={"records": bank, "warnings": []},
        all_warnings=[],
        enable_residual_reconciliation=False,
    )

    assert result["summary"]["ledger_records"] == 3
    assert result["summary"]["bank_records"] == 3
    assert result["summary"]["exact_matches"] == 3
    assert result["summary"]["fuzzy_matches"] == 0


def test_amount_index_uses_declared_slots() -> None:
    index = _AmountIndex([(10.0, 0), (12.0, 1), (20.0, 2)])

    assert index.between(11.0, 13.0) == [1]


def test_blank_bank_currency_fields_are_float_zeroes() -> None:
    bank = BankStatement(
        row_index=1,
        bank_name="Regression Bank",
        template_version="1",
        debit_amount="",
        credit_amount=None,
    )

    assert bank.debit_amount == 0.0
    assert bank.credit_amount == 0.0
    assert isinstance(bank.debit_amount, float)
    assert isinstance(bank.credit_amount, float)


def test_celery_default_include_and_entry_task_import() -> None:
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["STORAGE_DIR"] = "/tmp/latice-step0-task-import"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.celery import celery_app\n"
                "include = celery_app.conf.include\n"
                "assert 'tasks.entry_task' in include\n"
                "assert 'tasks.matcher' not in include\n"
                "assert 'tasks.service' not in include\n"
                "from tasks.entry_task import process_run\n"
                "assert process_run.name == 'entry.process_run'\n"
            ),
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_requirements_file_is_utf8() -> None:
    requirements = Path(__file__).resolve().parents[1] / "req.txt"

    assert "celery==" in requirements.read_text(encoding="utf-8")
