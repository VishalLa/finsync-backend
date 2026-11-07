from __future__ import annotations

from typing import Any

from app.celery import celery_app
from Backend.tasks.entry_point import load_bank_statement, load_ledger

from shared.message import (
    BadMessage,
    DBPersistInputsMsg,
    EntryProcessMsg,
    T_DB_STORE_INPUTS,
    T_ENTRY_PROCESS,
)
from shared.status import set_status
from shared.storage import resolve, run_ref, write_json

from .helper import reject_message, retry_stage


def _record_payload(record: Any) -> dict:
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    
    if hasattr(record, "to_dict"):
        return record.to_dict()
    
    raise TypeError(f"Unsupported parsed record type: {type(record).__name__}")


@celery_app.task(
    bind=True,
    max_retries=2,
    name=T_ENTRY_PROCESS,
)
def process_run(self, payload: dict) -> dict[str, str]:
    try:
        message = EntryProcessMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(
            payload=payload, 
            stage="entry", 
            error=exc, 
            run_exists=False
        )
        raise

    set_status(
        run_id=message.run_id, 
        stage="entry", 
        state="running", user_id=message.user_id
    )
    
    try:
        ledger_data = load_ledger(
            filepath=str(resolve(message.ledger_ref)),
            date_format=message.options.get("date_format", "%d-%m-%Y"),
        )
        bank_data = load_bank_statement(
            filepath=str(resolve(message.bank_ref))
        )
        ledger_records = ledger_data.get("records", [])
        bank_records = bank_data.get("records", [])

        ledger_ref = run_ref(message.run_id, "norm", "ledger.json")
        bank_ref = run_ref(message.run_id, "norm", "bank.json")
        metadata_ref = run_ref(message.run_id, "norm", "metadata.json")
        
        write_json(ledger_ref, [_record_payload(record) for record in ledger_records])
        write_json(bank_ref, [_record_payload(record) for record in bank_records])
        write_json(metadata_ref, {
            "run_name": message.options.get("run_name"),
            "ledger_source": ledger_data.get("source"),
            "bank_name": bank_data.get("bank_name"),
            "template_version": bank_data.get("template_version"),
            "warnings": (
                ledger_data.get("warnings", [])
                + bank_data.get("warnings", [])
            ),
            "ledger_raw_ref": message.ledger_ref,
            "bank_raw_ref": message.bank_ref,
        })

        set_status(
            run_id=message.run_id, 
            stage="entry", 
            state="done", 
            user_id=message.user_id
        )
        set_status(
            run_id=message.run_id, 
            stage="inputs", 
            state="queued", 
            user_id=message.user_id
        )
        
        db_message = DBPersistInputsMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            ledger_ref=ledger_ref,
            bank_ref=bank_ref,
            metadata_ref=metadata_ref,
        )
        celery_app.send_task(T_DB_STORE_INPUTS, args=[db_message.to_dict()])
        return {
            "status": "queued", 
            "run_id": message.run_id
        }
    except Exception as exc:
        retry_stage(
            self,
            run_id=message.run_id,
            user_id=message.user_id,
            stage="entry",
            error=exc,
            run_exists=False,
        )
