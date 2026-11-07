FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/uploads

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY req.txt .
COPY service/requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads /home/appuser/.paddleocr \
    && chown -R appuser:appuser /app /data /home/appuser

USER appuser
CMD ["celery", "-A", "service.celery_app:celery_app", "worker", \
     "-Q", "service.queue,db_storage.queue", "-n", "service@%h", \
     "-c", "2", "--max-tasks-per-child=50", "--loglevel=info"]
