FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/uploads

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY api/requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/uploads \
    && chown -R appuser:appuser /app /data

USER appuser
EXPOSE 8000
CMD ["gunicorn", "wsgi:app", "-c", "gunicorn.conf.py"]
