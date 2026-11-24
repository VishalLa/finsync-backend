from __future__ import annotations

from typing import Sequence

from celery import Celery
from kombu import Queue

from .settings import get_settings


def create_celery(name: str, include: Sequence[str] = ()) -> Celery:
    settings = get_settings()
    routes = {
        "entry.*": {"queue": settings.Q_ENTRY},
        "ocr.*": {"queue": settings.Q_OCR},
        "matcher.*": {"queue": settings.Q_MATCHER},
        "ai.*": {"queue": settings.Q_AI},
        "ledger.*": {"queue": settings.Q_LEDGER},
        "service.*": {"queue": settings.Q_SERVICE},
        "db.*": {"queue": settings.Q_DB},
        "report.*": {"queue": settings.Q_REPORT},
        "tasks.*": {"queue": settings.Q_SERVICE},
    }
    app = Celery(
        name,
        broker=settings.REDIS_URL,
        backend=settings.REDIS_URL,
        include=list(include),
    )
    app.conf.timezone = "Asia/Kolkata"
    app.conf.task_queues = (
        Queue("celery"),
        *(Queue(route["queue"]) for route in routes.values()),
    )
    app.conf.task_routes = routes
    app.conf.task_default_queue = "celery"
    app.conf.worker_prefetch_multiplier = 1
    app.conf.broker_transport_options = {"visibility_timeout": 7200}
    app.conf.broker_connection_retry_on_startup = True
    return app
