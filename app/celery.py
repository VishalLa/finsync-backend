from app.shared.celery_factory import create_celery

DEFAULT_INCLUDE: list[str] = []

celery_app = create_celery(name="Lattic", include=DEFAULT_INCLUDE)
