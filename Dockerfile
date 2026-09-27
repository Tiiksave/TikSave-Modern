# Multi-stage build
FROM python:3.12-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY backend/ ./backend/
COPY frontend/ ./frontend/
RUN mkdir -p /app/downloads /app/logs
WORKDIR /app/backend
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 CMD curl -fsS http://localhost:8080/api/health || exit 1
# One worker is intentional: jobs are stored in memory until a shared database/queue is added.
CMD ["gunicorn","app:app","--bind","0.0.0.0:8080","--workers","1","--threads","4","--timeout","300","--access-logfile","-","--error-logfile","-","--log-level","info"]
