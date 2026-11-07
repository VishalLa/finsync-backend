from __future__ import annotations

from functools import lru_cache

from service import (
    Base,
    ResultBankRec,
    PushBankRecData
)
from shared.status import set_status


@lru_cache(maxsize=1)
def get_persistence_services() -> tuple[PushBankRecData, ResultBankRec]:
    base = Base()
    return (
        PushBankRecData(db_manager=base.get_manager),
        ResultBankRec(db_manager=base.get_manager),
    )


def fail_stage(
    run_id: str,
    user_id: str,
    stage: str,
    error: Exception,
    run_exists: bool = True,
) -> None:
    set_status(
        run_id=run_id,
        stage=stage,
        state="failed",
        error=str(error),
        user_id=user_id,
    )
    if run_exists:
        _, result_service = get_persistence_services()
        result_service.mark_run_status(
            run_id=run_id,
            status="failed",
            error_message=str(error),
        )


def reject_message(
    payload,
    stage: str,
    error: Exception,
    run_exists: bool = True,
) -> None:
    if not isinstance(payload, dict):
        return
    run_id = payload.get("run_id")
    user_id = payload.get("user_id")
    if isinstance(run_id, str) and isinstance(user_id, str):
        fail_stage(
            run_id,
            user_id,
            stage,
            error,
            run_exists=run_exists,
        )


def retry_stage(
    task,
    run_id: str,
    user_id: str,
    stage: str,
    error: Exception,
    run_exists: bool = True,
) -> None:
    max_retries = task.max_retries or 0
    if task.request.retries >= max_retries:
        fail_stage(run_id, user_id, stage, error, run_exists=run_exists)
        raise error
    set_status(
        run_id=run_id,
        stage=stage,
        state="running",
        error=str(error),
        user_id=user_id,
    )
    raise task.retry(exc=error, countdown=min(10 * (task.request.retries + 1), 60))
