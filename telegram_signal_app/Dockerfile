FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    LOCAL_PREVIEW=false \
    DB_PATH=/data/signals.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Explicitly copy only application code; never bake .env or a local DB into the image.
COPY app/ ./app/

# app.main reads the PORT supplied by Railway. Run one process for bot polling/SQLite.
CMD ["python", "-m", "app.main"]
