from shared.celery_factory import create_celery

celery_app = create_celery(
    name="reports",
    include=["services.reports.tasks"],
)
