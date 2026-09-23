FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt

RUN python3 -m pip install --break-system-packages --no-cache-dir -r /app/requirements.txt && \
    python3 -m pip install --break-system-packages --no-cache-dir "bgutil-ytdlp-pot-provider==2.0.0"

RUN git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil && \
    cd /opt/bgutil/server && \
    npm ci && \
    npx tsc

COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

WORKDIR /app/backend

CMD ["sh","-c","node /opt/bgutil/server/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil.log 2>&1 & sleep 3; gunicorn snap_entry:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --timeout 300"]
