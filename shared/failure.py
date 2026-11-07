from __future__ import annotations

from .message import DBMarkFailedMsg, T_DB_MARK_FAILED
from .status import set_status


def fail_stage(
    app,
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
        message = DBMarkFailedMsg(
            run_id=run_id,
            user_id=user_id,
            error=str(error)[:1000],
        )
        app.send_task(T_DB_MARK_FAILED, args=[message.to_dict()])


def reject_message(
    app,
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
            app,
            run_id,
            user_id,
            stage,
            error,
            run_exists=run_exists,
        )


def retry_stage(
    app,
    task,
    run_id: str,
    user_id: str,
    stage: str,
    error: Exception,
    run_exists: bool = True,
) -> None:
    max_retries = task.max_retries or 0
    if task.request.retries >= max_retries:
        fail_stage(app, run_id, user_id, stage, error, run_exists=run_exists)
        raise error
    set_status(
        run_id=run_id,
        stage=stage,
        state="running",
        error=str(error),
        user_id=user_id,
    )
    raise task.retry(
        exc=error,
        countdown=min(10 * (task.request.retries + 1), 60),
    )
