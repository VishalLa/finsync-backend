from shared.celery_factory import create_celery

celery_app = create_celery(
    name="service",
    include=[
        "service.tasks",
        "service.bank_rec_tasks",
        "service.bill_tasks",
        "service.full_run_task",
        "service.report_tasks",
    ],
)
