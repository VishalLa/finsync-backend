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
    
