from datetime import datetime, timezone

from . import run_quota
from .storage import read_json, run_ref, write_json


def set_status(
    run_id: str, 
    stage: str, 
    state: str, 
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    ref = run_ref(run_id, "status.json")
    try:
        data = read_json(ref)
    except FileNotFoundError:
        data = {"run_id": run_id, "stages": {}}
    if user_id is not None:
        owner = data.get("user_id")
        if owner is not None and owner != user_id:
            raise ValueError(f"run {run_id!r} belongs to a different user")
        data["user_id"] = user_id
    data["stages"][stage] = {
        "state": state,  # queued | running | done | failed
        "error": error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ref, data)

    stages = data["stages"]
    terminal = any(item.get("state") == "failed" for item in stages.values()) or all(
        stages.get(required, {}).get("state") == "done"
        for required in ("entry", "inputs", "matcher", "service", "results")
    )
    if terminal and data.get("user_id") is not None:
        run_quota.release(data["user_id"], run_id)


def get_status(run_id: str) -> dict:
    try:
        return read_json(run_ref(run_id, "status.json"))
    except FileNotFoundError:
        return {"run_id": run_id, "stages": {}}


def request_cancel(run_id: str, user_id: str) -> None:
    """Persist a cooperative cancellation signal for every stage of a run."""
    status_ref = run_ref(run_id, "status.json")
    try:
        data = read_json(status_ref)
    except FileNotFoundError:
        data = {
            "run_id": run_id, 
            "stages": {}
        }
        
    owner = data.get("user_id")
    if owner is not None and owner != user_id:
        raise ValueError(f"run {run_id!r} belongs to a different user")
    
    data["user_id"] = user_id
    data["cancel_requested"] = True
    write_json(status_ref, data)
    # Keep the cancellation tombstone outside the run folder. The run folder
    # is deleted immediately after the stop signal is sent, while queued tasks
    # may not be visible to Celery's worker inspection yet.
    write_json(
        f"cancelled-runs/{run_id}.json",
        {"run_id": run_id, "user_id": user_id},
    )
    run_quota.release(user_id, run_id)


def is_cancel_requested(run_id: str) -> bool:
    try:
        read_json(f"cancelled-runs/{run_id}.json")
        return True
    except FileNotFoundError:
        return bool(get_status(run_id).get("cancel_requested"))
    
