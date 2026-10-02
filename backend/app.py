from __future__ import annotations

import glob
import ipaddress
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp
from yt_dlp.extractor import gen_extractor_classes

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


def positive_setting(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_CONCURRENT_DOWNLOADS = positive_setting("MAX_CONCURRENT_DOWNLOADS", 2)
MAX_DOWNLOAD_MB = positive_setting("MAX_DOWNLOAD_MB", 500)
JOB_TTL_SECONDS = positive_setting("JOB_TTL_SECONDS", 3600, 60)
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def cookie_file() -> str | None:
    configured_path = os.environ.get("YTDLP_COOKIE_FILE", "").strip()
    if not configured_path:
        return None
    path = Path(configured_path).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise RuntimeError("ملف الكوكيز مضبوط بمسار غير موجود أو غير قابل للقراءة.")
    return str(path.resolve())


def friendly_download_error(error: Exception) -> str:
    message = str(error)
    normalized = message.lower()
    if any(term in normalized for term in ("cookie", "cookies", "sign in", "log in", "login", "authentication", "not a bot")):
        return "المنصة تطلب تسجيل الدخول. أضف ملف كوكيز صالحاً كـ Secret File في إعدادات الخادم، ثم أعد المحاولة."
    if "ملف الكوكيز" in message:
        return message
    if "ffmpeg" in normalized:
        return "تعذر تجهيز صيغة الفيديو المطلوبة على الخادم. جرّب جودة أقل أو أعد المحاولة لاحقاً."
    if any(term in normalized for term in ("unsupported url", "unsupported site", "no suitable", "not available")):
        return "الرابط غير متاح أو أن المنصة لا تدعم هذا المقطع حالياً. تأكد من أن الرابط عام وصحيح."
    return "تعذر تنزيل الفيديو من المنصة حالياً. تأكد من أن الرابط عام وصحيح، ثم أعد المحاولة."


def normalize_quality(raw_quality: str | None) -> str:
    if raw_quality in (None, "", "best"):
        return "best"
    try:
        q = int(str(raw_quality).strip())
    except (TypeError, ValueError):
        return "best"
    return str(max(240, min(q, 2160)))


def has_supported_extractor(url: str) -> bool:
    """Accept URLs recognized by a real yt-dlp site extractor, not its catch-all."""
    try:
        extractors = gen_extractor_classes()
    except Exception:
        return False

    for extractor in extractors:
        try:
            if extractor.ie_key().lower() != "generic" and extractor.suitable(url):
                return True
        except Exception:
            continue
    return False


def is_valid_url(value: str) -> bool:
    if not value or len(value) > 4096:
        return False
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return False

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return False
    if parsed.username or parsed.password or (port is not None and port != (443 if scheme == "https" else 80)):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    return has_supported_extractor(value.strip())


def clean_partial_files(prefix: str) -> None:
    for path in glob.glob(prefix + ".*"):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def cleanup_expired_jobs() -> None:
    now = time.time()
    with jobs_lock:
        expired_ids = [
            job_id for job_id, job in jobs.items()
            if job.get("status") in {"done", "error"}
            and now - job.get("updated_at", job.get("created_at", now)) >= JOB_TTL_SECONDS
        ]
        for job_id in expired_ids:
            jobs.pop(job_id, None)
    for job_id in expired_ids:
        clean_partial_files(str(DOWNLOADS_DIR / job_id))


def hook(job_id: str, payload: dict) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return

        status = payload.get("status")
        job["updated_at"] = time.time()
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            done = payload.get("downloaded_bytes") or 0
            if total > 0:
                job["progress"] = round((done * 100) / total, 1)
            else:
                job["progress"] = job.get("progress", 0)
        elif status == "finished":
            job["progress"] = 100
            job["status"] = "processing"
        elif status == "error":
            job["status"] = "error"
            job["error"] = payload.get("error") or "فشل تحميل الفيديو"


def worker(job_id: str, url: str, quality: str) -> None:
    try:
        with jobs_lock:
            jobs[job_id]["status"] = "preparing"
            jobs[job_id]["progress"] = 0
            jobs[job_id]["error"] = None

        prefix = str(DOWNLOADS_DIR / job_id)
        opts = {
            "outtmpl": prefix + ".%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "skip_download": False,
            "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
            "progress_hooks": [lambda payload, ji=job_id: hook(ji, payload)],
        }

        configured_cookies = cookie_file()
        if configured_cookies:
            opts["cookiefile"] = configured_cookies

        ffmpeg = ffmpeg_bin()
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
            opts["merge_output_format"] = "mp4"
            if quality == "best":
                opts["format"] = "bestvideo+bestaudio/best"
            else:
                q = int(quality)
                opts["format"] = f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
        else:
            # Managed Python hosts may not include ffmpeg. Select a single-file
            # format there so downloads still work without a merge step.
            opts["format"] = "best" if quality == "best" else f"best[height<={quality}]/best"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        files = [
            x for x in glob.glob(prefix + ".*")
            if os.path.isfile(x) and not x.endswith(".part")
        ]
        if not files:
            raise RuntimeError("لم يتم إنشاء ملف الفيديو")

        final_path = max(files, key=os.path.getmtime)
        title = info.get("title", "TiikSave") if isinstance(info, dict) else "TiikSave"
        file_name = os.path.basename(final_path)

        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "progress": 100,
                "file": final_path,
                "filename": file_name,
                "title": title,
                "updated_at": time.time(),
            })
    except Exception as exc:
        clean_partial_files(str(DOWNLOADS_DIR / job_id))
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "progress": 0,
                "error": friendly_download_error(exc),
                "file": None,
                "updated_at": time.time(),
            })
    finally:
        download_slots.release()


@app.get("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.after_request
def set_api_cache_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/download", methods=["GET", "POST"])
def api_download():
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "بيانات الطلب غير صالحة"}), 400
        url = str(payload.get("url", "")).strip()
        quality = normalize_quality(payload.get("quality", "best"))
    else:
        url = request.args.get("url", "").strip()
        quality = normalize_quality(request.args.get("quality", "best"))

    if not is_valid_url(url):
        return jsonify({"error": "الرابط غير صالح أو لا ينتمي إلى موقع يدعمه محرك التحميل"}), 400

    cleanup_expired_jobs()
    if not download_slots.acquire(blocking=False):
        response = jsonify({"error": "الخادم مشغول حالياً. انتظر قليلاً ثم أعد المحاولة."})
        response.headers["Retry-After"] = "30"
        return response, 429

    job_id = uuid.uuid4().hex
    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "error": None,
            "file": None,
            "filename": None,
            "title": None,
            "created_at": now,
            "updated_at": now,
        }

    try:
        threading.Thread(target=worker, args=(job_id, url, quality), daemon=True).start()
    except RuntimeError:
        download_slots.release()
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify({"error": "تعذر بدء التحميل. حاول مرة أخرى."}), 503
    return jsonify({"id": job_id, "status": "queued"})


@app.get("/api/status")
def api_status():
    cleanup_expired_jobs()
    job_id = request.args.get("id", "").strip()
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))

    if not job:
        return jsonify({"status": "error", "error": "المهمة غير موجودة"}), 404
    job.pop("file", None)
    job.pop("created_at", None)
    job.pop("updated_at", None)
    return jsonify(job)


@app.get("/api/file")
def api_file():
    cleanup_expired_jobs()
    job_id = request.args.get("id", "").strip()
    with jobs_lock:
        job = jobs.get(job_id)

    if not job or job.get("status") != "done":
        return jsonify({"error": "الملف غير جاهز"}), 404

    file_path = job.get("file")
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "الملف غير موجود"}), 404

    return send_file(file_path, as_attachment=True, download_name=job.get("filename", "TiikSave.mp4"))


@app.errorhandler(404)
def page_not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "الصفحة غير موجودة"}), 404
    return send_from_directory(str(FRONTEND_DIR), "404.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True, debug=False)


# Compatibility for deploy wrappers that import app from backend/app.py
__all__ = ["app"]
