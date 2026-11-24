from __future__ import annotations

from matcher import reconcile
from schema import BankStatement, LedgerFormat

from app.shared.failure import reject_message, retry_stage
from app.shared.message import (
    BadMessage,
    MatcherRunMsg,
    ServiceSaveMsg,
    T_MATCHER_RUN,
    T_SERVICE_SAVE,
)
from app.shared.settings import get_settings
from app.shared.status import set_status
from app.shared.storage import read_json, run_ref, write_json

from .celery_app import celery_app


@celery_app.task(bind=True, max_retries=2, name=T_MATCHER_RUN)
def run_matcher(self, payload: dict) -> dict[str, str]:
    try:
        message = MatcherRunMsg.from_dict(payload)
    except BadMessage as exc:
        reject_message(celery_app, payload, "matcher", exc)
        raise

    set_status(
        run_id=message.run_id,
        stage="matcher",
        state="running",
        user_id=message.user_id,
    )
    try:
        ledger_payload = read_json(message.ledger_ref)
        bank_payload = read_json(message.bank_ref)
        if not isinstance(ledger_payload, list) or not isinstance(bank_payload, list):
            raise ValueError("normalized ledger and bank data must be lists")

        ledger_records = [LedgerFormat.model_validate(item) for item in ledger_payload]
        bank_records = [BankStatement.model_validate(item) for item in bank_payload]
        warnings = message.options.get("warnings", [])
        if not isinstance(warnings, list):
            raise ValueError("matching warnings must be a list")
        enable_ai_matching = message.options.get("enable_ai_matching", True)
        if not isinstance(enable_ai_matching, bool):
            raise ValueError("enable_ai_matching must be a boolean")
        print(
            f"[matcher-task] run_id={message.run_id} "
            f"enable_ai_matching={enable_ai_matching}"
        )

        result = reconcile(
            config=get_settings(),
            ledger_result={"records": ledger_records, "warnings": warnings},
            bank_result={
                "records": bank_records,
                "bank_name": message.bank_name,
                "template_version": message.template_version,
                "warnings": [],
            },
            all_warnings=list(warnings),
            enable_ai_matching=enable_ai_matching,
        )
        result["ai_matching_enabled"] = enable_ai_matching
        result.setdefault("summary", {})["ai_matching_enabled"] = enable_ai_matching

        result_ref = run_ref(message.run_id, "results", "matcher_result.json")
        write_json(result_ref, result)

        set_status(
            run_id=message.run_id,
            stage="matcher",
            state="done",
            user_id=message.user_id,
        )
        set_status(
            run_id=message.run_id,
            stage="service",
            state="queued",
            user_id=message.user_id,
        )
        service_message = ServiceSaveMsg(
            run_id=message.run_id,
            user_id=message.user_id,
            result_ref=result_ref,
        )
        celery_app.send_task(T_SERVICE_SAVE, args=[service_message.to_dict()])
        return {"status": "queued", "run_id": message.run_id}
    except Exception as exc:
        retry_stage(
            celery_app,
            self,
            run_id=message.run_id,
            user_id=message.user_id,
            stage="matcher",
            error=exc,
        )
