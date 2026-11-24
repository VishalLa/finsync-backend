FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/uploads

WORKDIR /app
COPY matcher/requirements.txt .
RUN pip install -r requirements.txt
COPY app/shared/ app/shared/
COPY schema/ schema/
COPY matcher/ matcher/

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /app /data

USER appuser
CMD ["celery", "-A", "matcher.celery_app:celery_app", "worker", \
     "-Q", "matcher.queue", "-n", "matcher@%h", "-c", "2", "--loglevel=info"]
