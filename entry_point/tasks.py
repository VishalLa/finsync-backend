from __future__ import annotations

from typing import Any, Dict, List

from celery import chord, group
from entry_point import load_bank_statement, load_ledger

from shared.failure import reject_message, retry_stage
from shared.message import (
    BadMessage,
    DBPersistInputsMsg,
    EntryProcessMsg,
    T_DB_STORE_INPUTS,
    T_ENTRY_PROCESS,
)
from shared.status import set_status
from shared.storage import resolve, run_ref, write_json

from .celery_app import celery_app


def _record_payload(record: Any) -> Dict[str, Any]:
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    if hasattr(record, "to_dict"):
        return record.to_dict()
    raise TypeError(f"Unsupported parsed record type: {type(record).__name__}")


@celery_app.task(bind=True, max_retries=2, name=T_ENTRY_PROCESS)
def process_run(self, payload: Dict[str, Any]) -> Dict[str, str]:
    try:
        message = EntryProcessMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "entry", exc, run_exists=False)
        raise

    set_status(
        run_id=message.run_id,
        stage="entry",
        state="running",
        user_id=message.user_id,
    )

    try:
        extraction_tasks = group(
            extract_ledger.s(
                message.run_id,
                message.user_id,
                message.ledger_ref,
                message.options.get("date_format", "%d-%m-%Y"),
            ),
            extract_bank.s(
                message.run_id,
                message.user_id,
                message.bank_ref,
                message.options.get("bank_name"),
            ),
        )

        workflow = chord(
            extraction_tasks,
            persist_extracted_inputs.s(message.to_dict()),
        )
        workflow.apply_async()

        return {"status": "queued", "run_id": message.run_id}
    except Exception as exc:
        retry_stage(
            celery_app,
            self,
            run_id=message.run_id,
            user_id=message.user_id,
            stage="entry",
            error=exc,
            run_exists=False,
        )


@celery_app.task(bind=True, max_retries=2, name="entry.extract_ledger")
def extract_ledger(
    self,
    run_id: str,
    user_id: str,
    ledger_ref: str,
    date_format: str,
) -> Dict[str, Any]:
    try:
        ledger_data = load_ledger(
            filepath=str(resolve(ledger_ref)),
            date_format=date_format,
        )
        normalized_ref = run_ref(run_id, "norm", "ledger.json")
        records: List[Any] = ledger_data.get("records", [])

        write_json(
            normalized_ref,
            [_record_payload(record) for record in records],
        )
        return {
            "kind": "ledger",
            "ref": normalized_ref,
            "source": ledger_data.get("source"),
            "warnings": ledger_data.get("warnings", []),
        }
    except Exception as exc:
        retry_stage(
            celery_app,
            self,
            run_id=run_id,
            user_id=user_id,
            stage="entry",
            error=exc,
            run_exists=False,
        )


@celery_app.task(bind=True, max_retries=2, name="entry.extract_bank")
def extract_bank(
    self,
    run_id: str,
    user_id: str,
    bank_ref: str,
    bank_name: str | None = None,
) -> Dict[str, Any]:
    try:
        bank_data = load_bank_statement(filepath=str(resolve(bank_ref)))
        if bank_name:
            bank_data["bank_name"] = bank_name
            for record in bank_data.get("records", []):
                record.bank_name = bank_name
        normalized_ref = run_ref(run_id, "norm", "bank.json")
        records: List[Any] = bank_data.get("records", [])
        write_json(
            normalized_ref,
            [_record_payload(record) for record in records],
        )
        return {
            "kind": "bank",
            "ref": normalized_ref,
            "bank_name": bank_data.get("bank_name"),
            "template_version": bank_data.get("template_version"),
            "warnings": bank_data.get("warnings", []),
        }
    except Exception as exc:
        retry_stage(
            celery_app,
            self,
            run_id=run_id,
            user_id=user_id,
            stage="entry",
            error=exc,
            run_exists=False,
        )


@celery_app.task(
    bind=True, max_retries=2, name="entry.persist_extracted_inputs"
)
def persist_extracted_inputs(
    self,
    extraction_results: List[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Dict[str, str]:
    try:
        message = EntryProcessMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "entry", exc, run_exists=False)
        raise

    try:
        results_by_kind = {result["kind"]: result for result in extraction_results}
        if (
            len(extraction_results) != 2
            or set(results_by_kind) != {"bank", "ledger"}
        ):
            raise ValueError("Both bank and ledger extraction results are required.")

        ledger_result = results_by_kind["ledger"]
        bank_result = results_by_kind["bank"]
        metadata_ref = run_ref(message.run_id, "norm", "metadata.json")
        write_json(
            metadata_ref,
            {
                "run_name": message.options.get("run_name"),
                "ledger_source": ledger_result.get("source"),
                "bank_name": bank_result.get("bank_name") or message.options.get("bank_name"),
                "enable_ai_matching": message.options.get("enable_ai_matching", True),
                "template_version": bank_result.get("template_version"),
                "warnings": ledger_result.get("warnings", [])
                + bank_result.get("warnings", []),
                "ledger_raw_ref": message.ledger_ref,
                "bank_raw_ref": message.bank_ref,
            },
        )
        set_status(
            run_id=message.run_id,
            stage="entry",
            state="done",
            user_id=message.user_id,
        )
        set_status(
            run_id=message.run_id,
            stage="inputs",
            state="queued",
            user_id=message.user_id,
        )
        db_message = DBPersistInputsMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            ledger_ref=ledger_result["ref"],
            bank_ref=bank_result["ref"],
            metadata_ref=metadata_ref,
        )
        celery_app.send_task(T_DB_STORE_INPUTS, args=[db_message.to_dict()])
        return {"status": "queued", "run_id": message.run_id}
    except Exception as exc:
        retry_stage(
            celery_app,
            self,
            run_id=message.run_id,
            user_id=message.user_id,
            stage="entry",
            error=exc,
            run_exists=False,
        )
