from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeoutError

from app.shared.celery_factory import create_celery
from app.shared.settings import get_settings

celery_app = create_celery(name="api-service-client")
_SERVICE_CALL_TIMEOUT = 60


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def call_service(operation: str, **kwargs: Any) -> Any:
    result = celery_app.send_task(
        "service.api_call",
        args=[operation, _jsonable(kwargs)],
        queue=get_settings().Q_SERVICE,
    )
    try:
        return result.get(timeout=_SERVICE_CALL_TIMEOUT, propagate=True)
    except CeleryTimeoutError as exc:
        raise TimeoutError(
            f"Service operation {operation!r} did not finish within "
            f"{_SERVICE_CALL_TIMEOUT} seconds"
        ) from exc


def enqueue_task(
    task_name: str,
    *,
    args: list | None = None,
    kwargs: dict | None = None,
    task_id: str | None = None,
):
    return celery_app.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        task_id=task_id,
    )


class ServiceClient:
    def __init__(self, target: str) -> None:
        self._target = target
        if target == "run_bill":
            self.config = SimpleNamespace(STORAGE_DIR=get_settings().STORAGE_DIR)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        def call(*args: Any, **kwargs: Any) -> Any:
            if args:
                raise TypeError("Service RPC calls must use keyword arguments")
            return call_service(f"{self._target}.{method}", **kwargs)

        return call
