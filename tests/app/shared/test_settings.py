from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_matcher_and_legacy_celery_need_only_light_settings(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[3]
    script = """
import os
import sys

assert not {"SECRET_KEY", "JWT_SECRET_KEY", "DATABASE_URL"} & os.environ.keys()

import app.shared.storage

assert "pandas" not in sys.modules
assert "core" not in sys.modules
assert "database" not in sys.modules

from app.shared.settings import Config
from matcher import reconcile
from schema import BankStatement, LedgerFormat
from app.shared.celery_factory import create_celery
from app.shared.settings import Settings, get_settings

settings = get_settings()
assert issubclass(Config, Settings)
assert str(app.shared.storage.storage_dir()) == settings.STORAGE_DIR
celery_app = create_celery("step2-test", include=[])
assert celery_app.conf.broker_url == settings.REDIS_URL

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
result = reconcile(
    config=settings,
    ledger_result={"records": ledger, "warnings": []},
    bank_result={"records": bank, "warnings": []},
    all_warnings=[],
    enable_ai_matching=False,
    enable_residual_reconciliation=False,
)
assert result["summary"]["exact_matches"] == 3
"""
    env = {
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "STORAGE_DIR": str(tmp_path / "storage"),
        "OLLAMA_URL": "http://127.0.0.1:11434",
        "PYTHONPATH": str(project_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_residual_drafting_uses_supplied_settings(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from matcher import residual_reconciler
    from matcher.residual_reconciler import ResidualReconciler
    from schema import BankStatement
    from app.shared.settings import Settings

    supplied_config = Settings(
        REDIS_URL="redis://127.0.0.1:6379/0",
        STORAGE_DIR=str(tmp_path),
        OLLAMA_URL="http://127.0.0.1:11434",
    )

    class UnavailableLLM:
        def invoke(self, _prompt: str) -> None:
            raise RuntimeError("LLM unavailable")

    def get_llm(config):
        assert config is supplied_config
        return UnavailableLLM()

    monkeypatch.setattr(residual_reconciler, "get_shared_llm", get_llm)
    bank = BankStatement(
        row_index=1,
        bank_name="Regression Bank",
        template_version="1",
        date="2025-01-01",
        narration="Monthly bank service fee",
        debit_amount=15.0,
    )

    reconciler = ResidualReconciler(
        pending_ledger=[],
        pending_bank=[bank],
        config=supplied_config,
    )
    drafts, unresolved, ai_skipped, reason = reconciler._generate_journal_drafts(
        [{"item": bank}]
    )

    assert len(drafts) == 1
    assert drafts[0]["source"] == "heuristic_fallback"
    assert unresolved == []
    assert ai_skipped
    assert reason == "LLM unavailable"
