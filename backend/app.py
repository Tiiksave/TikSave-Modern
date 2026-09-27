from __future__ import annotations

import glob
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR = BASE_DIR / "logs"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler(LOGS_DIR / "tiksave.log")])
log = logging.getLogger("tiksave")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
CORS(app, resources={r"/api/*": {"origins": "*"}})

jobs: dict[str, dict] = {}
lock = threading.RLock()
MAX_JOBS = 500
EXPIRY = timedelta(hours=24)
SUPPORTED = {"tiktok.com", "youtube.com", "youtu.be", "instagram.com", "facebook.com", "twitter.com", "x.com", "twitch.tv", "reddit.com", "pinterest.com", "vimeo.com", "dailymotion.com"}
HEADERS = {"User-Agent": os.getenv("DOWNLOAD_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"), "Accept-Language": "en-US,en;q=0.9"}


def valid_url(value: str) -> bool:
    try:
        p = urlparse(value.strip())
        host = (p.hostname or "").lower().removeprefix("www.")
        blocked = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        return p.scheme in {"http", "https"} and bool(host) and host not in blocked and not host.startswith(("10.", "192.168.", "172.16."))
    except ValueError:
        return False


def supported(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower().removeprefix("www.")
        return any(host == domain or host.endswith("." + domain) for domain in SUPPORTED)
    except ValueError:
        return False


def quality(value: str | None) -> str:
    if not value or value == "best":
        return "best"
    try:
        return str(max(240, min(2160, int(value))))
    except (TypeError, ValueError):
        return "best"


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def cookie_file() -> str | None:
    configured = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates += [BASE_DIR / "cookies.txt", Path("/app/cookies.txt")]
    for path in candidates:
        if path and path.is_file() and path.stat().st_size > 0:
            return str(path)
    return None


def youtube_options(opts: dict) -> None:
    # Cookies are optional. Configure YTDLP_COOKIES_FILE in the deployment environment;
    # never commit a personal cookies file to the repository.
    cookies = cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    opts["extractor_args"] = {"youtube": {"player_client": ["android", "web_safari", "web"], "lang": ["en"]}}


def cleanup() -> None:
    cutoff = datetime.now() - EXPIRY
    with lock:
        ids = [i for i, j in jobs.items() if datetime.fromisoformat(j["created_at"]) < cutoff]
        if len(jobs) > MAX_JOBS:
            ids += [i for i, j in sorted(jobs.items(), key=lambda x: x[1]["created_at"]) if j["status"] in {"done", "error"}][: max(0, len(jobs) - MAX_JOBS)]
        for job_id in set(ids):
            job = jobs.pop(job_id, {})
            path = job.get("file")
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def progress(job_id: str, data: dict) -> None:
    with lock:
        job = jobs.get(job_id)
        if not job:
            return
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            job["progress"] = round((data.get("downloaded_bytes", 0) / total) * 100, 1) if total else job["progress"]
            job["status"] = "downloading"
        elif data.get("status") == "finished":
            job.update(status="processing", progress=100)


def worker(job_id: str, url: str, q: str) -> None:
    prefix = str(DOWNLOADS_DIR / job_id)
    try:
        with lock:
            jobs[job_id].update(status="preparing", progress=3)
        opts = {
            "outtmpl": prefix + ".%(ext)s", "noplaylist": True, "quiet": True,
            "no_warnings": False, "retries": 5, "fragment_retries": 5,
            "socket_timeout": 60, "ffmpeg_location": ffmpeg(), "merge_output_format": "mp4",
            "http_headers": HEADERS, "progress_hooks": [lambda d: progress(job_id, d)],
            "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        }
        opts["format"] = "bestvideo+bestaudio/best" if q == "best" else f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
        if "youtube" in url.lower() or "youtu.be" in url.lower():
            youtube_options(opts)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [p for p in glob.glob(prefix + ".*") if os.path.isfile(p) and not p.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("لم يتم إنشاء ملف الفيديو")
        path = max(files, key=os.path.getmtime)
        with lock:
            jobs[job_id].update(status="done", progress=100, file=path, filename=os.path.basename(path), title=(info or {}).get("title", "TikSave"), size=os.path.getsize(path), completed_at=datetime.now().isoformat())
    except Exception as exc:
        text = str(exc)
        if "Sign in to confirm" in text or "not a bot" in text:
            text = "YouTube رفض الطلب. أضف ملف cookies.txt على الخادم واضبط YTDLP_COOKIES_FILE=/app/cookies.txt."
        log.exception("job %s failed", job_id)
        for path in glob.glob(prefix + ".*"):
            try:
                os.remove(path)
            except OSError:
                pass
        with lock:
            if job_id in jobs:
                jobs[job_id].update(status="error", progress=0, error=text[:500], file=None)
    finally:
        cleanup()


@app.get("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok", version="3.0.0", ffmpeg=bool(shutil.which("ffmpeg")), youtube_cookies=bool(cookie_file()), timestamp=datetime.now().isoformat())


@app.get("/api/download")
def download():
    url = request.args.get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="الرابط غير صالح"), 400
    if not supported(url):
        return jsonify(error="المنصة غير مدعومة"), 400
    job_id = uuid.uuid4().hex
    with lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0, "error": None, "file": None, "created_at": datetime.now().isoformat()}
    threading.Thread(target=worker, args=(job_id, url, quality(request.args.get("quality"))), daemon=True).start()
    return jsonify(id=job_id, status="queued")


@app.get("/api/status")
def status():
    job_id = request.args.get("id", "").strip()
    with lock:
        job = dict(jobs.get(job_id, {}))
    return (jsonify(job), 200) if job else (jsonify(status="error", error="المهمة غير موجودة"), 404)


@app.get("/api/file")
def file():
    job_id = request.args.get("id", "").strip()
    with lock:
        job = dict(jobs.get(job_id, {}))
    path = job.get("file")
    if job.get("status") != "done" or not path or not os.path.isfile(path):
        return jsonify(error="الملف غير جاهز"), 404
    return send_file(path, as_attachment=True, download_name=job.get("filename", "TikSave.mp4"), mimetype="video/mp4")


@app.get("/api/stats")
def stats():
    with lock:
        values = list(jobs.values())
    return jsonify(total=len(values), completed=sum(j["status"] == "done" for j in values), failed=sum(j["status"] == "error" for j in values), processing=sum(j["status"] in {"queued", "preparing", "downloading", "processing"} for j in values))


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify(error="غير موجود"), 404
    return send_from_directory(str(FRONTEND_DIR), "404.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
