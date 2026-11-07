from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.utils import secure_filename

from app.celery import celery_app
from service import ResultBankRec, RunBankRec, RecJournalPosting, _log_call
from tasks.generate_report_tasks import generate_report_bank_rec

from shared.message import EntryProcessMsg, T_ENTRY_PROCESS
from shared.status import get_status, set_status
from shared.storage import resolve, run_ref, save_stream


app = Blueprint("bank_rec_api", __name__)

_run_bank_rec: Optional[RunBankRec] = None
_result_service: Optional[ResultBankRec] = None
_journal_posting: Optional[RecJournalPosting] = None


def _services() -> Tuple[RunBankRec, ResultBankRec, RecJournalPosting]:
    global _run_bank_rec, _result_service, _journal_posting
    if _run_bank_rec is None:
        _run_bank_rec = RunBankRec()
        
    if _result_service is None:
        _result_service = ResultBankRec(db_manager=_run_bank_rec.db_manager)
        
    if _journal_posting is None:
        _journal_posting = RecJournalPosting(db_manager=_run_bank_rec.db_manager)
        
    return _run_bank_rec, _result_service, _journal_posting


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"csv", "xlsx"}


@app.route("/reconciliation", methods=["POST"])
@jwt_required()
@_log_call
def start_reconciliation():
    ledger_file = request.files.get("ledger_file")
    bank_file = request.files.get("bank_file")
    run_name = (request.form.get("run_name") or "").strip() or None
    
    if not ledger_file or not bank_file or not ledger_file.filename or not bank_file.filename:
        return jsonify({
            "ok": False, 
            "error": "ledger_file and bank_file are required"
        }), 400
        
    if not _allowed(ledger_file.filename) or not _allowed(bank_file.filename):
        return jsonify({
            "ok": False, 
            "error": "Only CSV and XLSX files are supported"
        }), 400

    run_id = uuid.uuid4().hex
    user_id = str(get_jwt_identity())
    ledger_extension = Path(secure_filename(ledger_file.filename)).suffix.lower()
    bank_extension = Path(secure_filename(bank_file.filename)).suffix.lower()
    ledger_ref = run_ref(run_id, "raw", f"ledger{ledger_extension}")
    bank_ref = run_ref(run_id, "raw", f"bank{bank_extension}")
    message = EntryProcessMsg(
        run_id=run_id,
        user_id=user_id,
        ledger_ref=ledger_ref,
        bank_ref=bank_ref,
        options={"run_name": run_name},
    )
    
    try:
        save_stream(
            ref=ledger_ref, 
            fileobj=ledger_file.stream
        )
        save_stream(
            ref=bank_ref, 
            fileobj=bank_file.stream
        )
        set_status(
            run_id=run_id, 
            stage="entry", 
            state="queued", 
            user_id=user_id
        )
        celery_app.send_task(
            T_ENTRY_PROCESS,
            args=[message.to_dict()],
            task_id=run_id,
        )
    except Exception:
        resolve(ledger_ref).unlink(missing_ok=True)
        resolve(bank_ref).unlink(missing_ok=True)
        set_status(
            run_id=run_id,
            stage="entry",
            state="failed",
            error="Could not submit reconciliation to the worker queue",
            user_id=user_id,
        )
        raise

    return jsonify({
        "ok": True,
        "run_id": run_id,
        "task_id": run_id,
        "run_name": run_name,
        "status_url": f"/api/bank-rec/reconciliation/{run_id}/status",
    }), 202


@app.route("/reconciliation/<run_id>/status", methods=["GET"])
@jwt_required()
@_log_call
def run_status(run_id: str):
    user_id = str(get_jwt_identity())
    status_data = get_status(run_id)
    owner_id = status_data.get("user_id")
    
    if owner_id is not None and owner_id != user_id:
        return jsonify({
            "ok": False, 
            "error": "Run not found"
        }), 404

    stages = status_data.get("stages", {})
    if owner_id is None:
        _, result_service, _ = _services()
        result = result_service.get_run_for_user(
            run_id=run_id,
            user_id=user_id,
            include_records=False,
        )
        if result is None:
            return jsonify({
                "ok": False, 
                "error": "Run not found"
            }), 404
        
        return jsonify(result), 200

    if any(stage.get("state") == "failed" for stage in stages.values()):
        state = "FAILURE"
    elif all(
        stages.get(stage, {}).get("state") == "done"
        for stage in ("entry", "inputs", "matcher", "service", "results")
    ):
        state = "SUCCESS"
    elif any(stage.get("state") == "running" for stage in stages.values()):
        state = "STARTED"   
    else:
        state = "PENDING"

    payload: dict[str, Any] = {
        "run_id": run_id, 
        "state": state, 
        "stages": stages
    }
    
    if state == "FAILURE":
        payload["error"] = next(
            (
                stage.get("error")
                for stage in stages.values()
                if stage.get("state") == "failed"
            ),
            "Reconciliation failed",
        )
    elif state == "SUCCESS":
        _, result_service, _ = _services()
        result = result_service.get_run_for_user(
            run_id=run_id,
            user_id=user_id,
            include_records=False,
        )
        if result is None:
            return jsonify({"ok": False, "error": "Run not found"}), 404
        payload["result"] = result

    return jsonify(payload), 200


@app.route("/reconciliation/<run_id>", methods=["GET"])
@jwt_required()
@_log_call
def run_result(run_id: str):
    _, result_service, _ = _services()
    result = result_service.get_run_for_user(
        run_id=run_id,
        user_id=str(get_jwt_identity()),
        include_records=False,
    )
    if result is None:
        return jsonify({
            "ok": False,
            "error": "Run not found",
        }), 404

    return jsonify(result), 200


@app.route("/reconciliation/<run_id>/report", methods=["POST"])
@jwt_required()
@_log_call
def generate_report(run_id: str):
    task = generate_report_bank_rec.delay(
        run_id=run_id, 
        user_id=get_jwt_identity()
    )
    
    return jsonify({
        "ok": True, 
        "task_id": task.id, 
        "status": "queued"
    }), 202


@app.route("/reconciliation/<run_id>/journal-entries/approve", methods=["POST"])
@jwt_required()
@_log_call
def approve_journal_entries(run_id: str):
    entries = (request.get_json(silent=True) or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        return jsonify({
            "ok": False, 
            "error": "entries must be a non-empty list"
        }), 400

    _, result_service, posting = _services()
    
    run = result_service.find_run(
        run_id=run_id, 
        user_id=get_jwt_identity()
    )
    
    if run is None:
        return jsonify({
            "ok": False, 
            "error": "Run not found"
        }), 404
        
    return jsonify(posting.approve_journal_entries(
        approved_entries=entries,
        user_id=get_jwt_identity(),
        run_id=run.id,
    )), 200


@app.route("/previous/runs/<user_id>", methods=["GET"])
@jwt_required()
@_log_call
def get_previous_runs(user_id: str):
    authenticated_user_id = str(get_jwt_identity())
    if user_id != authenticated_user_id:
        return jsonify({
            "ok": False,
            "error": "Not authorized to access these runs",
        }), 403

    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "limit and offset must be integers",
        }), 400

    if not 1 <= limit <= 200 or offset < 0:
        return jsonify({
            "ok": False,
            "error": "limit must be between 1 and 200 and offset must be non-negative",
        }), 400

    _, result_service, _ = _services()
    runs = result_service.list_runs_for_user(
        user_id=authenticated_user_id,
        limit=limit,
        offset=offset,
    )
    return jsonify({
        "runs": runs,
        "limit": limit,
        "offset": offset,
    }), 200


@app.route("/previous/runs/<user_id>/<run_id>", methods=["GET"])
@jwt_required()
@_log_call
def get_previous_run(user_id: str, run_id: str):
    authenticated_user_id = str(get_jwt_identity())
    if user_id != authenticated_user_id:
        return jsonify({
            "ok": False,
            "error": "Not authorized to access this run",
        }), 403

    include_records_raw = request.args.get("include_records", "true").lower()
    if include_records_raw not in {"true", "false"}:
        return jsonify({
            "ok": False,
            "error": "include_records must be true or false",
        }), 400

    _, result_service, _ = _services()
    run = result_service.get_run_for_user(
        user_id=authenticated_user_id,
        run_id=run_id,
        include_records=include_records_raw == "true",
    )
    if run is None:
        return jsonify({
            "ok": False,
            "error": "Run not found",
        }), 404

    return jsonify(run), 200


@app.route("/previous/runs/<user_id>/<run_id>", methods=["DELETE"])
@jwt_required()
@_log_call
def delete_previous_run(user_id: str, run_id: str):
    authenticated_user_id = str(get_jwt_identity())
    if user_id != authenticated_user_id:
        return jsonify({
            "ok": False,
            "error": "Not authorized to delete this run",
        }), 403

    _, result_service, _ = _services()
    result = result_service.delete_run_for_user(
        user_id=authenticated_user_id,
        run_id=run_id,
    )
    if result is None:
        return jsonify({
            "ok": False,
            "error": "Run not found",
        }), 404
    if not result["deleted"]:
        return jsonify({
            "ok": False,
            "error": "A reconciliation that is still processing cannot be deleted",
        }), 409

    return jsonify({
        "ok": True,
        "deleted": True,
        "cleanup_warnings": result["cleanup_warnings"],
    }), 200
