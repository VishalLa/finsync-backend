from shared.celery_factory import create_celery

celery_app = create_celery(
    name="db_writer",
    include=["services.db_writer.tasks"],
)
