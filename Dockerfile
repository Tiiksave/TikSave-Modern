# Multi-stage build للتقليل من حجم الصورة
FROM python:3.12-slim as builder

WORKDIR /app

# تثبيت المتطلبات النظام
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# نسخ ملف المتطلبات
COPY backend/requirements.txt ./requirements.txt

# تثبيت المتطلبات Python في مجلد خاص
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# ===== المرحلة النهائية =====
FROM python:3.12-slim

WORKDIR /app

# تثبيت ffmpeg و curl فقط في الصورة النهائية
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# نسخ المكتبات المثبتة من المرحلة الأولى
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# نسخ المشروع
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# إنشاء مجلدات ضرورية
RUN mkdir -p /app/downloads /app/logs

WORKDIR /app/backend

# فتح المنفذ
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# تشغيل التطبيق
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info"]
