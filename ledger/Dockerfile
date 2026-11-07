FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/uploads

WORKDIR /app
COPY ledger/requirements.txt .
RUN pip install -r requirements.txt
COPY shared/ shared/
COPY schema/ schema/
COPY ledger/ ledger/

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /app /data

USER appuser
CMD ["celery", "-A", "ledger.celery_app:celery_app", "worker", \
     "-Q", "ledger.queue", "-n", "ledger@%h", "-c", "2", "--loglevel=info"]
