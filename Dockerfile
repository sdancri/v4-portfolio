FROM python:3.12-slim AS base

# System deps minime
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Bucharest \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy requirements first (cache layer)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy app code
COPY . .

# Logs dir (mount un volum aici în producție pentru persistență logs JSONL)
RUN mkdir -p /app/logs
VOLUME ["/app/logs"]

# Default chart port — overridabil prin CHART_PORT env (compose maps 8103:8103)
EXPOSE 8103

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -sf http://localhost:${CHART_PORT:-8103}/api/status || exit 1

# `-u` unbuffered stdout (regula 14: dublu insurance peste PYTHONUNBUFFERED + line_buffering)
CMD ["python", "-u", "scripts/run_live.py"]
