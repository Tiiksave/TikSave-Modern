from __future__ import annotations

import base64
import binascii
import glob
import ipaddress
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
from yt_dlp.extractor import gen_extractor_classes

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", str(BASE_DIR / "downloads"))).expanduser()
DB_PATH = Path(os.environ.get("JOB_DB_PATH", str(DOWNLOADS_DIR / "jobs.sqlite3"))).expanduser()
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
allowed_origins = {
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "https://tiiksave.pages.dev").split(",")
    if origin.strip()
}
CORS(app, resources={r"/api/*": {"origins": list(allowed_origins)}})


def setting_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_CONCURRENT = max(1, setting_int("MAX_CONCURRENT_DOWNLOADS", 2, 1))
MAX_DOWNLOAD_MB = setting_int("MAX_DOWNLOAD_MB", 4096)
MAX_DURATION_SECONDS = setting_int("MAX_VIDEO_DURATION_SECONDS", 0)
JOB_TTL_SECONDS = max(300, setting_int("JOB_TTL_SECONDS", 3600, 300))
ACTIVE_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="download")
COOKIE_LOCK = threading.Lock()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                error TEXT,
                file TEXT,
                filename TEXT,
                title TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute(
            "UPDATE jobs SET status='error', error=?, updated_at=? WHERE status IN ('queued','preparing','downloading','processing')",
            ("انقطعت المهمة بسبب إعادة تشغيل الخادم. أعد المحاولة.", time.time()),
        )


init_db()


def update_job(job_id: str, **values: object) -> None:
    if not values:
        return
    columns = ", ".join(f"{key}=?" for key in values)
    with db() as conn:
        conn.execute(
            f"UPDATE jobs SET {columns}, updated_at=? WHERE id=?",
            (*values.values(), time.time(), job_id),
        )


def get_job(job_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def cookie_file() -> str | None:
    configured_path = os.environ.get("YTDLP_COOKIE_FILE", "").strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_file() or not os.access(path, os.R_OK):
            raise RuntimeError("ملف الكوكيز غير موجود أو غير قابل للقراءة.")
        return str(path.resolve())

    encoded = os.environ.get("YTDLP_COOKIE_BASE64", "").strip()
    if not encoded:
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("قيمة YTDLP_COOKIE_BASE64 ليست Base64 صالحة.") from exc
    if not content.lstrip(b"\xef\xbb\xbf\r\n").startswith(
        (b"# HTTP Cookie File", b"# Netscape HTTP Cookie File")
    ):
        raise RuntimeError("ملف الكوكيز يجب أن يكون بصيغة Netscape.")
    path = Path(tempfile.gettempdir()) / f"tiksave-cookies-{os.getpid()}.txt"
    with COOKIE_LOCK:
        path.write_bytes(content)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return str(path)


def supported_extractor(url: str) -> bool:
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


def valid_url(value: str) -> bool:
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
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return False
    if port is not None and port != (443 if scheme == "https" else 80):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return supported_extractor(value)


def cleanup_prefix(job_id: str) -> None:
    for path in glob.glob(str(DOWNLOADS_DIR / f"{job_id}.*")):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def cleanup_expired() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with db() as conn:
        rows = conn.execute(
            "SELECT id, file FROM jobs WHERE status IN ('done','error') AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        conn.executemany("DELETE FROM jobs WHERE id=?", [(row["id"],) for row in rows])
    for row in rows:
        if row["file"]:
            try:
                os.remove(row["file"])
            except OSError:
                pass
        cleanup_prefix(row["id"])


def friendly_error(exc: Exception) -> str:
    message = str(exc)
    lower = message.lower()
    if any(term in lower for term in ("sign in", "log in", "login", "authentication", "not a bot", "cookies")):
        return "هذه المنصة تطلب تسجيل الدخول لهذا الفيديو. جرّب رابطاً عاماً أو أضف كوكيز صالحة كسرّ للخادم."
    if "ffmpeg" in lower:
        return "تعذّر دمج مسارات الفيديو والصوت. تأكد من تثبيت FFmpeg على الخادم أو اختر جودة أقل."
    if "unsupported url" in lower or "unsupported site" in lower:
        return "هذا الرابط من منصة لا يدعمها yt-dlp حالياً."
    if "private video" in lower or "members-only" in lower or "private" in lower:
        return "الفيديو خاص أو يتطلب صلاحية مشاهدة."
    if "maximum file size" in lower or "max_filesize" in lower:
        return "حجم الفيديو يتجاوز الحد المحدد للخادم."
    if "duration" in lower:
        return "مدة الفيديو تتجاوز الحد المحدد للخادم."
    return "تعذّر تنزيل هذا الفيديو. قد يكون الرابط غير متاح أو أن المنصة رفضت الطلب؛ جرّب رابطاً عاماً آخر."


def progress_hook(job_id: str, payload: dict) -> None:
    state = payload.get("status")
    if state == "downloading":
        total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
        done = payload.get("downloaded_bytes") or 0
        progress = round(done * 100 / total, 1) if total else 0
        update_job(job_id, status="downloading", progress=progress, error=None)
    elif state == "finished":
        update_job(job_id, status="processing", progress=99, error=None)


def worker(job_id: str, url: str, quality: str) -> None:
    prefix = str(DOWNLOADS_DIR / job_id)
    try:
        update_job(job_id, status="preparing", progress=0, error=None)
        ffmpeg = ffmpeg_bin()
        opts: dict = {
            "outtmpl": prefix + ".%(ext)s",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "fragment_retries": 8,
            "file_access_retries": 3,
            "extractor_retries": 3,
            "socket_timeout": 45,
            "continuedl": True,
            "overwrites": True,
            "progress_hooks": [lambda data: progress_hook(job_id, data)],
        }
        if MAX_DOWNLOAD_MB:
            opts["max_filesize"] = MAX_DOWNLOAD_MB * 1024 * 1024
        if MAX_DURATION_SECONDS:
            opts["match_filter"] = yt_dlp.utils.match_filter_func(
                f"duration <= {MAX_DURATION_SECONDS}"
            )
        cookies = cookie_file()
        if cookies:
            opts["cookiefile"] = cookies

        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
            opts["merge_output_format"] = "mp4"
            opts["format"] = (
                "bestvideo+bestaudio/best"
                if quality == "best"
                else f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            )
        else:
            opts["format"] = "best" if quality == "best" else f"best[height<={quality}]/best"

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        files = [
            item for item in glob.glob(prefix + ".*")
            if os.path.isfile(item) and not item.endswith((".part", ".ytdl"))
        ]
        if not files:
            raise RuntimeError("لم ينشأ ملف الفيديو.")
        final_path = max(files, key=os.path.getmtime)
        update_job(
            job_id,
            status="done",
            progress=100,
            file=final_path,
            filename=os.path.basename(final_path),
            title=(info.get("title") or "TikSave") if isinstance(info, dict) else "TikSave",
            error=None,
        )
    except Exception as exc:
        cleanup_prefix(job_id)
        update_job(job_id, status="error", progress=0, error=friendly_error(exc), file=None)
    finally:
        ACTIVE_SLOTS.release()


def normalize_quality(value: object) -> str:
    if value in (None, "", "best"):
        return "best"
    try:
        height = int(str(value).strip())
    except (TypeError, ValueError):
        return "best"
    return str(max(240, min(height, 2160)))


@app.get("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "downloader": "yt-dlp", "version": yt_dlp.version.__version__})


@app.after_request
def no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/download", methods=["GET", "POST"])
def api_download():
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "بيانات الطلب غير صالحة."}), 400
        url = str(payload.get("url", "")).strip()
        quality = normalize_quality(payload.get("quality"))
    else:
        url = request.args.get("url", "").strip()
        quality = normalize_quality(request.args.get("quality"))
    if not valid_url(url):
        return jsonify({"error": "الرابط غير صالح أو المنصة غير مدعومة حالياً."}), 400

    cleanup_expired()
    if not ACTIVE_SLOTS.acquire(blocking=False):
        response = jsonify({"error": "الخادم مشغول حالياً. انتظر قليلاً ثم أعد المحاولة."})
        response.headers["Retry-After"] = "30"
        return response, 429

    job_id = uuid.uuid4().hex
    now = time.time()
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO jobs(id,url,status,progress,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (job_id, url, "queued", 0, now, now),
            )
        POOL.submit(worker, job_id, url, quality)
    except Exception:
        ACTIVE_SLOTS.release()
        with db() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        app.logger.exception("Could not queue download job")
        return jsonify({"error": "تعذّر بدء التحميل. أعد المحاولة بعد قليل."}), 503
    return jsonify({"id": job_id, "status": "queued"}), 202


@app.get("/api/status")
def api_status():
    cleanup_expired()
    job_id = request.args.get("id", "").strip()
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "error", "error": "المهمة غير موجودة أو انتهت صلاحيتها."}), 404
    for key in ("url", "file", "created_at", "updated_at"):
        job.pop(key, None)
    return jsonify(job)


@app.get("/api/file")
def api_file():
    cleanup_expired()
    job_id = request.args.get("id", "").strip()
    job = get_job(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "الملف غير جاهز للتنزيل."}), 404
    file_path = job.get("file")
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "انتهت صلاحية الملف أو لم يعد موجوداً على الخادم."}), 404
    return send_file(
        file_path,
        as_attachment=True,
        download_name=job.get("filename") or "video.mp4",
        conditional=True,
    )


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "المسار غير موجود."}), 404
    return send_from_directory(str(FRONTEND_DIR), "404.html")


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "حجم الطلب أكبر من المسموح."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True, debug=False)


__all__ = ["app"]
