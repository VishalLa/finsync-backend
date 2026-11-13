from __future__ import annotations

from typing import Optional, Sequence

from celery import Celery
from kombu import Queue

from core.config import Config


def create_celery(
    config: Config,
    name: str = "Lattic",
    include: Optional[Sequence[str]] = None,
) -> Celery:
    
    TASK_ROUTES = {
        "entry.*": {"queue": config.Q_ENTRY},
        "ocr.*": {"queue": config.Q_OCR},
        "matcher.*": {"queue": config.Q_MATCHER},
        "ai.*": {"queue": config.Q_AI},
        "ledger.*": {"queue": config.Q_LEDGER},
        "service.*": {"queue": config.Q_SERVICE},
        "db.*": {"queue": config.Q_DB},
        "report.*": {"queue": config.Q_REPORT},
    }
    DEFAULT_INCLUDE = [
        "tasks.bank_rec",
        "tasks.bill_pipeline",
        "tasks.full_run",
        "tasks.generate_report_tasks",
    ]
    
    app = Celery(
        "Lattic",
        broker=config.REDIS_URL,
        backend=config.REDIS_URL,
        include=list(DEFAULT_INCLUDE if include is None else include)
    )
    
    app.conf.timezone = "Asia/Kolkata"
    app.conf.task_queues = (
        Queue("celery"),
        *(Queue(route["queue"]) for route in TASK_ROUTES.values())
    )
    app.conf.task_default_queue = "celery"
    app.conf.worker_prefetch_multiplier = 1
    app.conf.broker_transport_options = {"visibility_timeout": 7200}

    app.conf.broker_connection_retry_on_startup = True
    return app

celery_app = create_celery(config=Config.from_env())
