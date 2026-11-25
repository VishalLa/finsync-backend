from __future__ import annotations

import os

os.environ.setdefault("STORAGE_DIR", "/tmp/latice-rate-limit-tests")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")

import pytest
from flask_jwt_extended import create_access_token

from app import create_app
from app.shared import run_quota
from app.shared.rate_limit import fail_closed, limiter, rate_limit_settings
from app.shared.settings import get_settings


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("SECRET_KEY", "s" * 40)
    monkeypatch.setenv("JWT_SECRET_KEY", "j" * 40)
    monkeypatch.setenv("DEBUG", "True")
    monkeypatch.setenv("RATELIMIT_REDIS_URL", "memory://")
    monkeypatch.setenv("RATELIMIT_ENABLED", "True")
    monkeypatch.setenv("RATELIMIT_DEFAULT", "200 per minute")
    monkeypatch.setenv("RATELIMIT_NORMAL", "3 per minute")
    rate_limit_settings.cache_clear()
    get_settings.cache_clear()
    application = create_app()
    application.config.update(TESTING=True)
    yield application
    rate_limit_settings.cache_clear()
    get_settings.cache_clear()


def _token(application, user_id: str) -> str:
    with application.app_context():
        return create_access_token(identity=user_id)


def test_normal_limit_returns_json_and_retry_after(app, monkeypatch):
    monkeypatch.setenv("RATELIMIT_NORMAL", "3 per minute")
    rate_limit_settings.cache_clear()
    token = _token(app, "user-1")
    client = app.test_client()

    responses = [
        client.get(
            "/api/bank-rec/reconciliation/options",
            headers={"Authorization": f"Bearer {token}"},
        )
        for _ in range(4)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 429]
    assert responses[3].json["error"] == "Too Many Requests"
    assert "message" in responses[3].json
    assert responses[3].headers.get("Retry-After")
    assert responses[3].headers.get("X-RateLimit-Limit")
    assert responses[3].headers.get("X-RateLimit-Remaining")
    assert responses[3].headers.get("X-RateLimit-Reset")


def test_authenticated_users_have_independent_buckets(app, monkeypatch):
    monkeypatch.setenv("RATELIMIT_NORMAL", "1 per minute")
    rate_limit_settings.cache_clear()
    client = app.test_client()
    first = _token(app, "user-1")
    second = _token(app, "user-2")

    assert client.get(
        "/api/bank-rec/reconciliation/options",
        headers={"Authorization": f"Bearer {first}"},
    ).status_code == 200
    assert client.get(
        "/api/bank-rec/reconciliation/options",
        headers={"Authorization": f"Bearer {second}"},
    ).status_code == 200
    assert client.get(
        "/api/bank-rec/reconciliation/options",
        headers={"Authorization": f"Bearer {first}"},
    ).status_code == 429


def test_healthz_is_exempt_from_default_limit(app, monkeypatch):
    monkeypatch.setenv("RATELIMIT_DEFAULT", "1 per minute")
    rate_limit_settings.cache_clear()
    client = app.test_client()

    assert [client.get("/healthz").status_code for _ in range(5)] == [200] * 5


def test_fail_closed_returns_503_when_storage_check_fails(app, monkeypatch):
    app.add_url_rule(
        "/test-fail-closed",
        view_func=fail_closed(lambda: ("ok", 200)),
    )
    with app.app_context():
        monkeypatch.setattr(limiter.storage, "check", lambda: False)

    response = app.test_client().get("/test-fail-closed")

    assert response.status_code == 503
    assert response.json["error"] == "Service Unavailable"
    assert response.headers["Retry-After"] == "5"


def test_run_quota_blocks_at_max_and_releases_capacity(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.runs: dict[str, dict[str, float]] = {}

        def eval(self, _script, _key_count, key, now, cutoff, maximum, _ttl, run_id):
            entries = self.runs.setdefault(key, {})
            self.runs[key] = entries = {
                member: score for member, score in entries.items() if score > cutoff
            }
            if len(entries) >= maximum:
                return 0
            entries[run_id] = now
            return 1

        def zrem(self, key, run_id):
            return int(self.runs.get(key, {}).pop(run_id, None) is not None)

    fake_redis = FakeRedis()
    monkeypatch.setattr(run_quota, "_redis_client", lambda: fake_redis)
    monkeypatch.setenv("MAX_ACTIVE_RUNS_PER_USER", "2")
    monkeypatch.setenv("ACTIVE_RUN_TTL_SECONDS", "7200")
    rate_limit_settings.cache_clear()

    assert run_quota.acquire("user-1", "run-1")
    assert run_quota.acquire("user-1", "run-2")
    assert not run_quota.acquire("user-1", "run-3")
    run_quota.release("user-1", "run-1")
    assert run_quota.acquire("user-1", "run-3")

