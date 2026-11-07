from __future__ import annotations

from typing import Any

from app.celery import celery_app

from shared.message import (
    BadMessage,
    DBPersistInputsMsg,
    DBPersistResultsMsg,
    MatcherRunMsg,
    T_DB_STORE_INPUTS,
    T_DB_STORE_RESULTS,
    T_MATCHER_RUN,
)
from shared.status import set_status
from shared.storage import read_json, resolve

from .helper import (
    get_persistence_services,
    reject_message,
    retry_stage,
)


def _record_dicts(value: Any, label: str) -> list[dict]:
    if (
        not isinstance(value, list) or 
        any(not isinstance(item, dict) for item in value)
    ):
        raise ValueError(f"{label} must contain a list of record objects")
    return value


@celery_app.task(
    bind=True,
    max_retries=3,
    name=T_DB_STORE_INPUTS,
)
def persist_inputs(self, payload: dict) -> dict[str, str]:
    try:
        message = DBPersistInputsMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(
            payload=payload, 
            stage="inputs", 
            error=exc, run_exists=False
        )
        raise

    set_status(
        run_id=message.run_id, 
        stage="inputs", 
        state="running", 
        user_id=message.user_id
    )
    inputs_persisted = False
    
    try:
        ledger_records = _record_dicts(read_json(message.ledger_ref), "ledger records")
        bank_records = _record_dicts(read_json(message.bank_ref), "bank records")
        metadata = read_json(message.metadata_ref)
        
        if not isinstance(metadata, dict):
            raise ValueError("input metadata must be an object")

        pusher, _ = get_persistence_services()
        pusher.persist_run_inputs(
            celery_task_id=message.run_id,
            user_id=message.user_id,
            run_name=metadata.get("run_name"),
            bank_name=metadata.get("bank_name"),
            template_version=metadata.get("template_version"),
            ledger_source=metadata.get("ledger_source"),
            bank_csv_path=str(resolve(metadata["bank_raw_ref"])),
            ledger_csv_path=str(resolve(metadata["ledger_raw_ref"])),
            statements_data=bank_records,
            ledgers_data=ledger_records,
        )
        inputs_persisted = True

        set_status(
            run_id=message.run_id, 
            stage="inputs", 
            state="done", 
            user_id=message.user_id
        )
        set_status(
            run_id=message.run_id, 
            stage="matcher", 
            state="queued", 
            user_id=message.user_id
        )
        
        matcher_message = MatcherRunMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            ledger_ref=message.ledger_ref,
            bank_ref=message.bank_ref,
            bank_name=metadata.get("bank_name") or "UNKNOWN",
            template_version=metadata.get("template_version") or "v1",
            options={"warnings": metadata.get("warnings", [])},
        )
        
        celery_app.send_task(T_MATCHER_RUN, args=[matcher_message.to_dict()])
        return {
            "status": "queued", 
            "run_id": message.run_id
        }
    except Exception as exc:
        retry_stage(
            self,
            run_id=message.run_id,
            user_id=message.user_id,
            stage="inputs",
            error=exc,
            run_exists=inputs_persisted,
        )


@celery_app.task(
    bind=True,
    max_retries=3,
    name=T_DB_STORE_RESULTS,
)
def persist_results(self, payload: dict) -> dict[str, str]:
    try:
        message = DBPersistResultsMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(
            payload=payload, 
            stage="results", 
            error=exc
        )
        raise

    set_status(
        run_id=message.run_id, 
        stage="results", state="running", 
        user_id=message.user_id
    )
    try:
        result = read_json(message.result_ref)
        if not isinstance(result, dict):
            raise ValueError("matcher result must be an object")

        pusher, result_service = get_persistence_services()
        run = result_service.find_run(
            run_id=message.run_id, 
            user_id=message.user_id
        )
        if run is None:
            raise PermissionError("Reconciliation run was not found for this user")

        all_matches = (
            result.get("EXACT_MATCHES", [])
            + result.get("FUZZY_MATCHES", [])
            + result.get("MEMORY_MATCHES", [])
            + result.get("AI_MATCHES", [])
        )
        if any(not isinstance(item, dict) for item in all_matches):
            raise ValueError("matcher results contain an invalid match record")

        ignored_records = []
        for item in result.get("IGNORED_METADATA", []):
            if not isinstance(item, dict):
                continue
            
            ignored = dict(item)
            if "ledger_id" in ignored:
                ignored["row_ref"] = str(ignored.pop("ledger_id"))
            elif "row_index" in ignored:
                ignored["row_ref"] = str(ignored.pop("row_index"))
                
            ignored_records.append(ignored)

        audit_records = _record_dicts(
            result.get("AUDIT_INVESTIGATION", []),
            "audit items",
        )
        
        summary = result.get("summary", {})
        if not isinstance(summary, dict):
            raise ValueError("matcher summary must be an object")

        writes = (
            pusher.push_match_result_rows(
                run_id=run.id,
                timing_matches=result.get("RESIDUAL_TIMING_MATCHES", []),
                split_matches=result.get("RESIDUAL_SPLIT_MATCHES", []),
                suggested_journal_entries=result.get("SUGGESTED_JOURNAL_ENTRIES", []),
                other_matches=all_matches,
                replace_existing=True,
            ),
            pusher.update_run_summary(run_id=run.id, summary=summary),
            pusher.push_ignored_records(ignored_records, run_id=run.id),
            pusher.push_audit_items(audit_records, run_id=run.id),
        )
        if not all(writes):
            raise RuntimeError("One or more reconciliation result writes failed")
        
        if not result_service.mark_run_status(message.run_id, "success"):
            raise RuntimeError("Could not mark reconciliation run as successful")

        set_status(message.run_id, "results", "done", user_id=message.user_id)
        return {
            "status": "success", 
            "run_id": message.run_id
        }
        
    except Exception as exc:
        retry_stage(
            self, 
            run_id=message.run_id, 
            user_id=message.user_id, 
            stage="results", 
            error=exc
        )
