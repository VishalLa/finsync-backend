from datetime import datetime, timezone

from .storage import read_json, run_ref, write_json


def set_status(
    run_id: str, 
    stage: str, 
    state: str, 
    error: str | None = None
) -> None:
    ref = run_ref(run_id, "status.json")
    try:
        data = read_json(ref)
    except FileNotFoundError:
        data = {"run_id": run_id, "stages": {}}
    data["stages"][stage] = {
        "state": state,  # queued | running | done | failed
        "error": error,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ref, data)


def get_status(run_id: str) -> dict:
    try:
        return read_json(run_ref(run_id, "status.json"))
    except FileNotFoundError:
        return {"run_id": run_id, "stages": {}}
    