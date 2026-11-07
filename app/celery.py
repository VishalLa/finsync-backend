from shared.celery_factory import create_celery

DEFAULT_INCLUDE = [
    "tasks.bank_rec",
    "tasks.bill_pipeline",
    "tasks.full_run",
    "tasks.generate_report_tasks",
    "tasks.entry_task",
    "tasks.run_matcher",
    "tasks.service_task",
    "tasks.db_storage",
]

celery_app = create_celery(name="Lattic", include=DEFAULT_INCLUDE)
