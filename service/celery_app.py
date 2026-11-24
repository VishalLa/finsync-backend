from app.shared.celery_factory import create_celery

celery_app = create_celery(
    name="service",
    include=[
        "service.task.tasks",
        "service.task.bank_rec_tasks",
        "service.task.bill_tasks",
        "service.task.full_run_task",
        "service.task.report_tasks",
    ],
)
