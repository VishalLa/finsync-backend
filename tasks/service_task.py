from __future__ import annotations

from pathlib import Path

from app.celery import celery_app
from core.config import Config
from schema import BankStatement, LedgerFormat
from service.reports.bank_recon_xlsx import write_bank_recon_xlsx

from shared.message import (
    BadMessage,
    DBPersistResultsMsg,
    ServiceSaveMsg,
    T_DB_STORE_RESULTS,
    T_SERVICE_SAVE,
)
from shared.status import set_status
from shared.storage import read_json, run_ref

from .helper import reject_message, retry_stage


@celery_app.task(
    bind=True,
    max_retries=2,
    name=T_SERVICE_SAVE,
)
def save_results(self, payload: dict) -> dict[str, str]:
    try:
        message = ServiceSaveMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(
            payload=payload, 
            stage="service", 
            error=exc
        )
        raise

    set_status(
        run_id=message.run_id, 
        stage="service", 
        state="running", 
        user_id=message.user_id
    )
    
    try:
        if message.source != "matcher":
            raise ValueError(f"Unsupported result source: {message.source!r}")
        
        result = read_json(message.result_ref)
        if not isinstance(result, dict):
            raise ValueError("matcher result must be an object")

        ledger_payload = read_json(run_ref(message.run_id, "norm", "ledger.json"))
        bank_payload = read_json(run_ref(message.run_id, "norm", "bank.json"))
        
        if not isinstance(ledger_payload, list) or not isinstance(bank_payload, list):
            raise ValueError("normalized ledger and bank data must be lists")
        
        ledger_records = [LedgerFormat.model_validate(item) for item in ledger_payload]
        bank_records = [BankStatement.model_validate(item) for item in bank_payload]

        config = Config.from_env()
        report_path = Path(config.STORAGE_DIR) / f"bank-recon-{message.run_id}.xlsx"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_bank_recon_xlsx(
            result,
            ledger_records,
            bank_records,
            str(report_path),
        )

        db_message = DBPersistResultsMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            result_ref=message.result_ref,
        )
        
        set_status(
            run_id=message.run_id, 
            stage="service", 
            state="done", 
            user_id=message.user_id
        )
        set_status(
            run_id=message.run_id, 
            stage="results", 
            state="queued", 
            user_id=message.user_id
        )
        
        celery_app.send_task(T_DB_STORE_RESULTS, args=[db_message.to_dict()])
        return {
            "status": "queued",
            "run_id": message.run_id,
            "report_file": report_path.name,
        }
        
    except Exception as exc:
        retry_stage(
            self, 
            run_id=message.run_id, 
            user_id=message.user_id, 
            stage="service", error=exc
        )
