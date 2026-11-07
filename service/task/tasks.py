from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from functools import lru_cache
import os
from typing import Any

from flask import Flask
from flask_jwt_extended import JWTManager

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import NoInspectionAvailable

from shared.failure import reject_message, retry_stage
from shared.message import (
    BadMessage,
    DBMarkFailedMsg,
    DBPersistInputsMsg,
    DBPersistResultsMsg,
    MatcherRunMsg,
    T_DB_MARK_FAILED,
    T_DB_STORE_INPUTS,
    T_DB_STORE_RESULTS,
    T_MATCHER_RUN,
    T_SERVICE_SAVE,
    ServiceSaveMsg,
)
from shared.settings import get_settings
from shared.status import set_status
from shared.storage import read_json, resolve

from ..celery_app import celery_app

from .._base import Base
from ..bank_rec_service.run_result_service_bank_rec import ResultBankRec
from ..bank_rec_service.store_bank_rec_data import PushBankRecData

from ..generate_reports import GenerateReports

from ..ledger_service.rebuild_service_ledger import RebuildServiceLedger
from ..ledger_service.rec_journal_posting import RecJournalPosting

from ..run_pipelines import RunBill
from ..user_service import UserService


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    try:
        mapper = sqlalchemy_inspect(value).mapper
    except NoInspectionAvailable as exc:
        raise TypeError(
            f"Unsupported RPC result type: {type(value).__name__}"
        ) from exc
    return {
        column.key: _jsonable(getattr(value, column.key))
        for column in mapper.column_attrs
    }


_RPC_METHODS = {
    "user": {
        "register", "login", "verify_email", "logout", "get_user",
        "delete_account",
    },
    "run_bill": {"create_bill", "list_bills"},
    "result": {
        "get_run_for_user", "list_runs_for_user", "find_run",
        "get_data_from_db", "delete_run_for_user",
    },
    "reports": {"export_bank_rec_report"},
    "ledger": {
        "get_trial_balance", "get_journal_entries", "get_tds_register",
    },
    "journal": {"approve_journal_entries"},
    "admin": {"promote_user"},
}


def _run_api_call(operation: str, arguments: dict) -> Any:
    try:
        target, method = operation.split(".", 1)
    except ValueError as exc:
        raise ValueError(f"Invalid service operation: {operation!r}") from exc
    
    if method not in _RPC_METHODS.get(target, set()):
        raise ValueError(f"Unsupported service operation: {operation!r}")

    kwargs = dict(arguments)
    if target == "admin":
        from service.promote_admin import promote

        return promote(**kwargs)

    for key in ("as_on", "date_from", "date_to", "period_start", "period_end"):
        value = kwargs.get(key)
        if isinstance(value, str):
            kwargs[key] = date.fromisoformat(value)
            
    if isinstance(kwargs.get("expires_at"), str):
        kwargs["expires_at"] = datetime.fromisoformat(kwargs["expires_at"])

    flask_app = None
    if target == "user":
        config = Base().config
        flask_app = Flask("service-rpc")
        flask_app.config["JWT_SECRET_KEY"] = config.JWT_SECRET_KEY
        flask_app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(
            minutes=int(os.environ.get("JWT_ACCESS_TOKEN_MINUTES", "60"))
        )
        JWTManager(flask_app)

    instance = _get_service_instance(target)
    if flask_app is not None:
        with flask_app.app_context():
            return _jsonable(getattr(instance, method)(**kwargs))
        
    result = getattr(instance, method)(**kwargs)
    
    if target == "result" and method == "find_run" and result is not None:
        return {"id": result.id}
    
    return _jsonable(result)


@lru_cache(maxsize=6)
def _get_service_instance(target: str):
    base = Base()
    manager = base.get_manager
    factories = {
        "user": lambda: UserService(db_manager=manager),
        "run_bill": RunBill,
        "result": lambda: ResultBankRec(db_manager=manager),
        "reports": lambda: GenerateReports(db_manager=manager),
        "ledger": lambda: RebuildServiceLedger(db_manager=manager),
        "journal": lambda: RecJournalPosting(db_manager=manager),
    }
    return factories[target]()


@celery_app.task(name="service.api_call")
def api_call(operation: str, arguments: dict) -> Any:
    return _run_api_call(operation, arguments)


@celery_app.task(bind=True, max_retries=3, name=T_DB_STORE_INPUTS)
def persist_inputs(self, payload: dict) -> dict[str, str]:
    try:
        message = DBPersistInputsMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "inputs", exc, run_exists=False)
        raise

    set_status(message.run_id, "inputs", "running", user_id=message.user_id)
    inputs_persisted = False
    
    try:
        ledger_records = read_json(message.ledger_ref)
        bank_records = read_json(message.bank_ref)
        metadata = read_json(message.metadata_ref)
        
        if not isinstance(ledger_records, list) or not all(
            isinstance(item, dict) for item in ledger_records
        ):
            raise ValueError("ledger records must be a list of objects")
        
        if not isinstance(bank_records, list) or not all(
            isinstance(item, dict) for item in bank_records
        ):
            raise ValueError("bank records must be a list of objects")
        
        if not isinstance(metadata, dict):
            raise ValueError("input metadata must be an object")

        pusher = PushBankRecData(db_manager=Base().get_manager)
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
        
        set_status(message.run_id, "inputs", "done", user_id=message.user_id)
        set_status(message.run_id, "matcher", "queued", user_id=message.user_id)
        
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
            celery_app, self, message.run_id, message.user_id, "inputs", exc, run_exists=inputs_persisted,
        )


@celery_app.task(bind=True, max_retries=3, name=T_DB_STORE_RESULTS)
def persist_results(self, payload: dict) -> dict[str, str]:
    try:
        message = DBPersistResultsMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "results", exc)
        raise

    set_status(message.run_id, "results", "running", user_id=message.user_id)
    try:
        result = read_json(message.result_ref)
        if not isinstance(result, dict):
            raise ValueError("matcher result must be an object")
        
        base = Base()
        pusher = PushBankRecData(db_manager=base.get_manager)
        result_service = ResultBankRec(db_manager=base.get_manager)
        
        run = result_service.find_run(message.run_id, message.user_id)
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
            if isinstance(item, dict):
                ignored = dict(item)
                
                if "ledger_id" in ignored:
                    ignored["row_ref"] = str(ignored.pop("ledger_id"))
                elif "row_index" in ignored:
                    ignored["row_ref"] = str(ignored.pop("row_index"))
                    
                ignored_records.append(ignored)
                
        audit_records = result.get("AUDIT_INVESTIGATION", [])
        summary = result.get("summary", {})
        
        if not isinstance(audit_records, list) or not all(
            isinstance(item, dict) for item in audit_records
        ):
            raise ValueError("audit items must be a list of objects")
        
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
            celery_app, self, message.run_id, message.user_id, "results", exc
        )


@celery_app.task(
    bind=True,
    max_retries=3,
    name=T_DB_MARK_FAILED,
    acks_late=True,
)
def mark_run_failed(self, payload: dict) -> dict[str, str]:
    try:
        message = DBMarkFailedMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "failure", exc, run_exists=False)
        raise
    try:
        if not ResultBankRec(db_manager=Base().get_manager).mark_run_status(
            message.run_id,
            "failed",
            error_message=message.error,
        ):
            raise LookupError(f"Reconciliation run {message.run_id!r} was not found")
        
    except Exception as exc:
        retry_stage(
            celery_app, self, message.run_id, message.user_id, "failure", exc,
            run_exists=False,
        )
    return {
        "run_id": message.run_id, 
        "status": "failed"
    }


@celery_app.task(bind=True, max_retries=2, name=T_SERVICE_SAVE)
def save_results(self, payload: dict) -> dict[str, str]:
    try:
        message = ServiceSaveMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "service", exc)
        raise
    
    set_status(message.run_id, "service", "running", user_id=message.user_id)
    
    try:
        if message.source != "matcher":
            raise ValueError(f"Unsupported result source: {message.source!r}")
        
        result = read_json(message.result_ref)
        if not isinstance(result, dict):
            raise ValueError("matcher result must be an object")
        
        ledger_payload = read_json(f"runs/{message.run_id}/norm/ledger.json")
        bank_payload = read_json(f"runs/{message.run_id}/norm/bank.json")
        
        if not isinstance(ledger_payload, list) or not isinstance(bank_payload, list):
            raise ValueError("normalized ledger and bank data must be lists")
        
        from schema import BankStatement, LedgerFormat
        from shared.bank_recon_xlsx import write_bank_recon_xlsx
        from pathlib import Path

        report_path = Path(get_settings().STORAGE_DIR) / f"bank-recon-{message.run_id}.xlsx"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_bank_recon_xlsx(
            result,
            [LedgerFormat.model_validate(item) for item in ledger_payload],
            [BankStatement.model_validate(item) for item in bank_payload],
            str(report_path),
        )
        
        db_message = DBPersistResultsMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            result_ref=message.result_ref,
        )
        
        set_status(message.run_id, "service", "done", user_id=message.user_id)
        set_status(message.run_id, "results", "queued", user_id=message.user_id)
        
        celery_app.send_task(T_DB_STORE_RESULTS, args=[db_message.to_dict()])
        return {
            "status": "queued", 
            "run_id": message.run_id, 
            "report_file": report_path.name
        }
    except Exception as exc:
        retry_stage(
            celery_app, self, message.run_id, message.user_id, "service", exc
        )
