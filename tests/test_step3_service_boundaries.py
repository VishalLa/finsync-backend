from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("task_module", "is_database_service"),
    [
        ("entry_point.tasks", False),
        ("matcher.tasks", False),
        ("service.task.tasks", True),
    ],
)
def test_task_modules_respect_import_boundaries(
    task_module: str,
    is_database_service: bool,
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = f"""
import importlib
import sys

importlib.import_module("{task_module}")
forbidden = ["services", "tasks"]
{"assert any(name == 'database' or name.startswith('database.') for name in sys.modules)" if is_database_service else "forbidden.extend(['database', 'flask', 'service'])"}
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
assert not loaded, loaded
"""
    env = {
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "STORAGE_DIR": str(tmp_path / task_module.replace(".", "-")),
        "OLLAMA_URL": "http://127.0.0.1:11434",
        "PYTHONPATH": str(project_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_db_mark_failed_message_preserves_schema_version() -> None:
    from app.shared.message import DBMarkFailedMsg, SCHEMA_VERSION, T_DB_MARK_FAILED

    message = DBMarkFailedMsg(
        run_id="run-1",
        user_id="user-1",
        error="matcher failed",
    )

    assert SCHEMA_VERSION == 1
    assert T_DB_MARK_FAILED == "db.mark_run_failed"
    assert DBMarkFailedMsg.from_dict(message.to_dict()) == message


def test_fail_stage_records_storage_status_and_sends_db_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import failure
    from app.shared.message import DBMarkFailedMsg, T_DB_MARK_FAILED

    calls = []

    class FakeCelery:
        def send_task(self, task_name, args):
            calls.append((task_name, args))

    status_calls = []
    monkeypatch.setattr(failure, "set_status", lambda **kwargs: status_calls.append(kwargs))
    error = RuntimeError("matcher failed")

    failure.fail_stage(
        FakeCelery(),
        run_id="run-1",
        user_id="user-1",
        stage="matcher",
        error=error,
    )

    assert status_calls == [
        {
            "run_id": "run-1",
            "stage": "matcher",
            "state": "failed",
            "error": "matcher failed",
            "user_id": "user-1",
        }
    ]
    assert len(calls) == 1
    task_name, args = calls[0]
    assert task_name == T_DB_MARK_FAILED
    assert DBMarkFailedMsg.from_dict(args[0]) == DBMarkFailedMsg(
        run_id="run-1",
        user_id="user-1",
        error="matcher failed",
    )


def test_security_tokens_still_use_flask_app_configuration() -> None:
    from flask import Flask

    from core.security import generate_verification_token, verify_token

    app = Flask(__name__)
    app.config["JWT_SECRET_KEY"] = "step-three-regression-secret"
    with app.app_context():
        token = generate_verification_token("step3@example.test")
        assert verify_token(token) == "step3@example.test"


def test_db_mark_failed_task_repeats_mark_status_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    from service.task import tasks as db_tasks
    from app.shared.message import DBMarkFailedMsg

    updates = []

    class FakeBase:
        get_manager = object()

    class ResultService:
        def mark_run_status(self, run_id, status, error_message=None):
            updates.append((run_id, status, error_message))
            return True

    monkeypatch.setattr(db_tasks, "Base", FakeBase)
    monkeypatch.setattr(
        db_tasks,
        "ResultBankRec",
        lambda db_manager: ResultService(),
    )
    payload = DBMarkFailedMsg(
        run_id="run-1",
        user_id="user-1",
        error="matcher failed",
    ).to_dict()

    assert db_tasks.mark_run_failed.run(payload) == {
        "run_id": "run-1",
        "status": "failed",
    }
    assert db_tasks.mark_run_failed.run(payload) == {
        "run_id": "run-1",
        "status": "failed",
    }
    assert updates == [
        ("run-1", "failed", "matcher failed"),
        ("run-1", "failed", "matcher failed"),
    ]


def test_api_import_does_not_load_database_modules(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    env = {
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "STORAGE_DIR": str(tmp_path / "api-storage"),
        "SECRET_KEY": "test-secret-key-not-used-in-production",
        "JWT_SECRET_KEY": "another-test-secret-key-not-production",
        "PYTHONPATH": str(project_root),
    }
    script = """
import sys
import app
assert not any(
    name == 'database' or name.startswith('database.') for name in sys.modules
)
assert not any(
    name == 'sqlalchemy' or name.startswith('sqlalchemy.') for name in sys.modules
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_api_cors_allows_configured_frontend_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from flask import Flask

    from app import _configure_cors

    origin = "https://bank-rec-frontend.vercel.app"
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origin)
    app = Flask(__name__)
    _configure_cors(app, debug=False)

    response = app.test_client().options(
        "/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "authorization" in response.headers["Access-Control-Allow-Headers"].lower()
