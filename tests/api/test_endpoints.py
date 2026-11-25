"""HTTP contract tests for every Flask API route.

External boundaries (Celery, Postgres-backed services, and report files) are
mocked deliberately.  These tests validate the API layer without requiring a
running Docker stack.
"""

from __future__ import annotations

from io import BytesIO
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["DEBUG"] = "false"
os.environ["STORAGE_DIR"] = "/tmp/latice-api-tests"
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["SECRET_KEY"] = "api-tests-only-secret-key-not-for-production"
os.environ["JWT_SECRET_KEY"] = "api-tests-only-jwt-secret-not-production"
os.environ["RATELIMIT_REDIS_URL"] = "memory://"
os.environ["RATELIMIT_ENABLED"] = "true"

from app.shared.settings import get_settings, rate_limit_settings
rate_limit_settings.cache_clear()
get_settings.cache_clear()

from app import create_app
from api.v1 import auth, bank_rec_api, ledger_api, pipeline_api, report_api


class Task:
    """Small stand-in for Celery tasks used by route tests."""

    def __init__(self, task_id: str = "task-123"):
        self.task_id = task_id
        self.id = task_id
        self.calls = []

    def delay(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(id=self.task_id)

    def apply_async(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(id=self.task_id)


@pytest.fixture()
def client():
    rate_limit_settings.cache_clear()
    get_settings.cache_clear()
    app = create_app()
    app.config.update(TESTING=True)
    yield app.test_client()
    rate_limit_settings.cache_clear()
    get_settings.cache_clear()


@pytest.fixture()
def headers(client):
    with client.application.app_context():
        token = create_access_token(identity="test-user", additional_claims={"role": "user"})
    return {"Authorization": f"Bearer {token}"}


def test_auth_register(client, monkeypatch):
    service = SimpleNamespace(register=lambda **_: {"ok": True, "user_id": "user-1"})
    monkeypatch.setattr(auth, "get_user_service", lambda: service)
    response = client.post("/auth/register", json={"email": "user@example.test", "password": "secret", "first_name": "Test", "phone_no": "9000000000"})
    assert response.status_code == 201
    assert response.json["ok"] is True


def test_auth_login(client, monkeypatch):
    service = SimpleNamespace(login=lambda **_: {"ok": True, "access_token": "token"})
    monkeypatch.setattr(auth, "get_user_service", lambda: service)
    response = client.post("/auth/login", json={"email": "user@example.test", "password": "secret"})
    assert response.status_code == 200
    assert response.json["access_token"] == "token"


def test_auth_login_sends_verification_email_for_unverified_user(client, monkeypatch):
    sent_to = []
    service = SimpleNamespace(
        login=lambda **_: {"ok": False, "email_verification_required": True}
    )
    monkeypatch.setattr(auth, "get_user_service", lambda: service)
    monkeypatch.setattr(
        auth,
        "EmailService",
        lambda: SimpleNamespace(send_verification_email=sent_to.append),
    )

    response = client.post(
        "/auth/login",
        json={"email": "user@example.test", "password": "secret"},
    )

    assert response.status_code == 202
    assert response.json["email_verification_required"] is True
    assert sent_to == ["user@example.test"]


def test_auth_verify_email(client, monkeypatch):
    service = SimpleNamespace(verify_email=lambda token: {"ok": token == "valid"})
    monkeypatch.setattr(auth, "get_user_service", lambda: service)

    response = client.get("/auth/verify-email?token=valid")

    assert response.status_code == 200
    assert response.json["ok"] is True


def test_auth_logout(client, headers, monkeypatch):
    monkeypatch.setattr(auth, "get_user_service", lambda: SimpleNamespace(logout=lambda **_: True))
    assert client.post("/auth/logout", headers=headers).status_code == 200


def test_auth_me(client, headers, monkeypatch):
    monkeypatch.setattr(auth, "get_user_service", lambda: SimpleNamespace(get_user=lambda **_: {"id": "test-user"}))
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200
    assert response.json["user"]["id"] == "test-user"


def test_auth_delete(client, headers, monkeypatch):
    monkeypatch.setattr(auth, "get_user_service", lambda: SimpleNamespace(delete_account=lambda **_: {"ok": True}))
    assert client.delete("/auth/delete", headers=headers, json={"password": "secret"}).status_code == 200


def test_bank_reconciliation_upload(client, headers, monkeypatch, tmp_path):
    submitted = []
    monkeypatch.setattr(bank_rec_api.run_quota, "acquire", lambda *_args: True)
    monkeypatch.setattr(bank_rec_api, "save_stream", lambda **_kwargs: None)
    monkeypatch.setattr(bank_rec_api, "set_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bank_rec_api,
        "enqueue_task",
        lambda task_name, **kwargs: submitted.append((task_name, kwargs))
        or SimpleNamespace(id=kwargs["task_id"]),
    )
    response = client.post("/api/bank-rec/reconciliation", headers=headers, data={"ledger_file": (BytesIO(b"ledger"), "ledger.csv"), "bank_file": (BytesIO(b"bank"), "bank.csv")})
    assert response.status_code == 202
    assert response.json["run_id"] == response.json["task_id"]
    assert len(response.json["run_id"]) == 32
    assert submitted[0][0] == "entry.process_run"
    payload = submitted[0][1]["args"][0]
    assert payload["run_id"] == response.json["run_id"]
    assert payload["user_id"] == "test-user"
    assert payload["ledger_ref"].endswith("/raw/ledger.csv")
    assert payload["bank_ref"].endswith("/raw/bank.csv")


def test_bank_reconciliation_status(client, headers, monkeypatch):
    monkeypatch.setattr(
        bank_rec_api,
        "get_status",
        lambda _run_id: {
            "user_id": "test-user",
            "stages": {"entry": {"state": "running"}},
        },
    )
    response = client.get("/api/bank-rec/reconciliation/run-1/status", headers=headers)
    assert response.status_code == 200
    assert response.json["state"] == "STARTED"


def test_bank_reconciliation_status_is_scoped_to_owner(client, headers, monkeypatch):
    monkeypatch.setattr(
        bank_rec_api,
        "get_status",
        lambda _run_id: {
            "user_id": "another-user",
            "stages": {"entry": {"state": "running"}},
        },
    )
    response = client.get("/api/bank-rec/reconciliation/run-1/status", headers=headers)
    assert response.status_code == 404


def test_bank_reconciliation_result(client, headers, monkeypatch):
    results = SimpleNamespace(
        get_run_for_user=lambda **_: {"state": "SUCCESS", "matches": []}
    )
    monkeypatch.setattr(bank_rec_api, "_services", lambda: (None, results, None))
    assert client.get("/api/bank-rec/reconciliation/run-1", headers=headers).status_code == 200


def test_bank_reconciliation_report(client, headers, monkeypatch):
    monkeypatch.setattr(
        bank_rec_api,
        "enqueue_task",
        lambda *_args, **_kwargs: Task("report-1"),
    )
    response = client.post("/api/bank-rec/reconciliation/run-1/report", headers=headers)
    assert response.status_code == 202
    assert response.json["task_id"] == "report-1"


def test_bank_journal_approval(client, headers, monkeypatch):
    result = SimpleNamespace(find_run=lambda **_: {"id": "run-1"})
    posting = SimpleNamespace(approve_journal_entries=lambda **_: {"ok": True})
    monkeypatch.setattr(bank_rec_api, "_services", lambda: (None, result, posting))
    response = client.post("/api/bank-rec/reconciliation/run-1/journal-entries/approve", headers=headers, json={"entries": [{"id": "entry-1"}]})
    assert response.status_code == 200
    assert response.json["ok"] is True


def test_pipeline_bill_upload(client, headers, monkeypatch):
    bill = SimpleNamespace(config=SimpleNamespace(STORAGE_DIR="/tmp"), create_bill=lambda **_: "bill-1")
    monkeypatch.setattr(pipeline_api, "_get_run_bill", lambda: bill)
    monkeypatch.setattr(
        pipeline_api, "enqueue_task", lambda *_args, **_kwargs: Task("bill-1")
    )
    response = client.post("/api/pipeline/bills", headers=headers, data={"direction": "input", "raw_data": '{"amount": 1}'})
    assert response.status_code == 202
    assert response.json["bill_id"] == "bill-1"


def test_pipeline_bill_status(client, headers, monkeypatch):
    monkeypatch.setattr(pipeline_api, "AsyncResult", lambda *_args, **_kwargs: SimpleNamespace(state="SUCCESS", result={"ok": True}))
    assert client.get("/api/pipeline/bills/bill-1/status", headers=headers).status_code == 200


def test_pipeline_fullrun(client, headers, monkeypatch, tmp_path):
    bill = SimpleNamespace(config=SimpleNamespace(STORAGE_DIR=str(tmp_path)))
    monkeypatch.setattr(pipeline_api, "_get_run_bill", lambda: bill)
    monkeypatch.setattr(
        pipeline_api, "enqueue_task", lambda *_args, **_kwargs: Task("full-1")
    )
    response = client.post("/api/pipeline/fullrun", headers=headers, data={"bank_statement": (BytesIO(b"bank"), "bank.csv"), "bill_files": (BytesIO(b"bill"), "bill.pdf")})
    assert response.status_code == 202
    assert response.json["run_id"] == "full-1"


def test_pipeline_gstr1(client, headers, monkeypatch):
    monkeypatch.setattr(
        pipeline_api, "enqueue_task", lambda *_args, **_kwargs: Task("gstr-1")
    )
    response = client.post("/api/pipeline/gstr1", headers=headers, json={"period_label": "Apr", "period_start": "2025-04-01", "period_end": "2025-04-30"})
    assert response.status_code == 202


def test_pipeline_task_status(client, headers, monkeypatch):
    monkeypatch.setattr(pipeline_api, "AsyncResult", lambda *_args, **_kwargs: SimpleNamespace(state="PENDING", result=None))
    assert client.get("/api/pipeline/tasks/task-1", headers=headers).status_code == 200


def test_report_task_status(client, headers, monkeypatch):
    monkeypatch.setattr(report_api, "AsyncResult", lambda *_args, **_kwargs: SimpleNamespace(state="SUCCESS", result={"file": "report.xlsx"}))
    assert client.get("/api/reports/tasks/task-1", headers=headers).status_code == 200


def test_report_download(client, headers, monkeypatch, tmp_path):
    report = tmp_path / "report.xlsx"
    report.write_bytes(b"report")
    monkeypatch.setattr(report_api, "AsyncResult", lambda *_args, **_kwargs: SimpleNamespace(state="SUCCESS", result={"status": "success", "path": str(report), "file": "report.xlsx"}))
    response = client.get("/api/reports/tasks/task-1/download", headers=headers)
    assert response.status_code == 200
    assert response.data == b"report"


@pytest.mark.parametrize(
    ("path", "payload", "task_name"),
    [
        ("/api/reports/bank-reconciliation", {"run_id": "run-1"}, "tasks.generate_bank_rec_report"),
        ("/api/reports/gstr1", {"period_label": "April"}, "tasks.generate_gstr1_reprot"),
        ("/api/reports/journal", {"date_from": "2025-04-01", "date_to": "2025-04-30"}, "tasks.generate_journal_report"),
        ("/api/reports/ledger", {"as_on": "2025-04-30"}, "tasks.generate_ledger_report"),
        ("/api/reports/tds", {"period_start": "2025-04-01", "period_end": "2025-04-30"}, "tasks.generate_tds_report"),
    ],
)
def test_report_queue_endpoints(client, headers, monkeypatch, path, payload, task_name):
    submitted = []
    monkeypatch.setattr(
        report_api,
        "enqueue_task",
        lambda name, **kwargs: submitted.append((name, kwargs)) or Task(task_name),
    )
    response = client.post(path, headers=headers, json=payload)
    assert response.status_code == 202
    assert response.json["status"] == "queued"
    assert submitted[0][0] == task_name


def test_ledger_trial_balance(client, headers, monkeypatch):
    ledger = SimpleNamespace(get_trial_balance=lambda **_: {"ok": True, "rows": []})
    monkeypatch.setattr(ledger_api, "_services", lambda: (None, ledger))
    response = client.get("/api/ledger/trial-balance?as_on=2025-04-30", headers=headers)
    assert response.status_code == 200
    assert response.json["ok"] is True
