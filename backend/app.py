from __future__ import annotations

import glob
import logging
import os
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp

# ========== إعدادات الملفات والمجلدات ==========
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
DOWNLOADS_DIR = BASE_DIR / "downloads"
LOGS_DIR = BASE_DIR / "logs"

DOWNLOADS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# ========== إعدادات السجلات ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / 'tiksave.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== إنشاء تطبيق Flask ==========
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
app.config["PROPAGATE_EXCEPTIONS"] = True

CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# ========== قاموس المهام والقفل ==========
jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()
MAX_JOBS_IN_MEMORY = 500
JOB_EXPIRY_HOURS = 24

# ========== User-Agent و HTTP Headers للتعامل مع YouTube وغيره ==========
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

# ========== المنصات المدعومة ==========
SUPPORTED_PLATFORMS = [
    "tiktok",
    "youtube",
    "instagram",
    "facebook",
    "twitter",
    "twitch",
    "reddit",
    "pinterest",
    "vimeo",
    "dailymotion",
]


def cleanup_old_jobs() -> None:
    """حذف المهام القديمة والمكتملة"""
    with jobs_lock:
        now = datetime.now()
        expired_jobs = []
        
        for job_id, job in list(jobs.items()):
            # حذف المهام التي مرّ عليها أكثر من 24 ساعة
            if job.get("created_at"):
                created = datetime.fromisoformat(job["created_at"])
                if now - created > timedelta(hours=JOB_EXPIRY_HOURS):
                    expired_jobs.append(job_id)
            
            # حذف إذا تجاوزنا حد المهام
            if len(jobs) > MAX_JOBS_IN_MEMORY:
                if job.get("status") in ["done", "error"]:
                    expired_jobs.append(job_id)
        
        # حذف الملفات والمهام المنتهية
        for job_id in expired_jobs:
            try:
                job = jobs[job_id]
                if job.get("file") and os.path.isfile(job["file"]):
                    os.remove(job["file"])
                    logger.info(f"Deleted file for expired job: {job_id}")
                del jobs[job_id]
            except Exception as e:
                logger.error(f"Error cleaning up job {job_id}: {e}")


def ffmpeg_bin() -> str:
    """البحث عن مسار FFmpeg في النظام"""
    candidates = [
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/opt/homebrew/bin/ffmpeg"),
        Path("/usr/bin/ffmpeg.exe"),
        "ffmpeg",
    ]
    for candidate in candidates:
        if isinstance(candidate, Path):
            if candidate.exists():
                logger.info(f"Found FFmpeg at: {candidate}")
                return str(candidate)
        else:
            result = os.system(f"which {candidate} >/dev/null 2>&1")
            if result == 0:
                logger.info(f"Found FFmpeg: {candidate}")
                return candidate
    
    logger.warning("FFmpeg not found in standard paths, using 'ffmpeg'")
    return "ffmpeg"


def normalize_quality(raw_quality: str | None) -> str:
    """تطبيع جودة الفيديو"""
    if raw_quality in (None, "", "best"):
        return "best"
    try:
        q = int(str(raw_quality).strip())
        return str(max(240, min(q, 2160)))
    except (TypeError, ValueError):
        return "best"


def is_valid_url(value: str) -> bool:
    """التحقق من صحة الرابط"""
    if not value:
        return False
    url = value.strip()
    if len(url) < 10:
        return False
    if not ("http://" in url or "https://" in url):
        return False
    # منع الروابط المحلية
    if any(x in url for x in ["localhost", "127.0.0.1", "192.168", "10.0"]):
        return False
    return True


def is_supported_platform(url: str) -> bool:
    """التحقق من أن المنصة مدعومة"""
    url_lower = url.lower()
    for platform in SUPPORTED_PLATFORMS:
        if platform in url_lower:
            return True
    return False


def clean_partial_files(prefix: str) -> None:
    """تنظيف الملفات المؤقتة والناقصة"""
    try:
        for path in glob.glob(prefix + ".*"):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as e:
                logger.warning(f"Failed to remove file {path}: {e}")
    except Exception as e:
        logger.error(f"Error cleaning partial files: {e}")


def hook(job_id: str, payload: dict) -> None:
    """معالج تحديثات yt-dlp"""
    try:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return

            status = payload.get("status")
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
    except Exception as e:
        logger.error(f"Error in hook: {e}")


def worker(job_id: str, url: str, quality: str) -> None:
    """عامل معالجة التحميل"""
    try:
        with jobs_lock:
            if job_id not in jobs:
                return
            jobs[job_id]["status"] = "preparing"
            jobs[job_id]["progress"] = 5
            jobs[job_id]["error"] = None

        logger.info(f"Starting download for job {job_id} with quality {quality} from {url}")
        
        prefix = str(DOWNLOADS_DIR / job_id)
        opts = {
            "outtmpl": prefix + ".%(ext)s",
            "noplaylist": True,
            "quiet": False,
            "no_warnings": False,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 60,
            "skip_download": False,
            "ffmpeg_location": ffmpeg_bin(),
            "merge_output_format": "mp4",
            "progress_hooks": [lambda payload, ji=job_id: hook(ji, payload)],
            "http_headers": REQUEST_HEADERS,
            "extractor_args": {
                "youtube": {"lang": ["en"]},
            },
            "age_limit": None,
            "prefer_free_formats": False,
            "youtube_include_dash_manifest": True,
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertProcessor",
                    "preferedformat": "mp4",
                }
            ],
        }

        if quality == "best":
            opts["format"] = "bestvideo+bestaudio/best"
        else:
            q = int(quality)
            opts["format"] = f"bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"

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
        file_size = os.path.getsize(final_path)

        logger.info(f"Download completed for job {job_id}: {file_name} ({file_size} bytes)")

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "done",
                    "progress": 100,
                    "file": final_path,
                    "filename": file_name,
                    "title": title,
                    "size": file_size,
                    "completed_at": datetime.now().isoformat(),
                })
    except Exception as exc:
        logger.error(f"Download failed for job {job_id}: {exc}", exc_info=True)
        clean_partial_files(str(DOWNLOADS_DIR / job_id))
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "error",
                    "progress": 0,
                    "error": str(exc)[:500],
                    "file": None,
                })
    finally:
        cleanup_old_jobs()


# ========== المسارات ==========

@app.get("/")
def index():
    """الصفحة الرئيسية"""
    try:
        return send_from_directory(str(FRONTEND_DIR), "index.html")
    except Exception as e:
        logger.error(f"Error serving index: {e}")
        return jsonify({"error": "خطأ في تحميل الصفحة"}), 500


@app.get("/api/health")
def health():
    """فحص صحة الخادم"""
    return jsonify({
        "status": "ok",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat(),
        "ffmpeg": "available" if os.system("ffmpeg -version >/dev/null 2>&1") == 0 else "not_found",
    })


@app.get("/api/download")
def api_download():
    """بدء تحميل جديد"""
    try:
        url = request.args.get("url", "").strip()
        quality = normalize_quality(request.args.get("quality", "best"))

        # التحقق من الرابط
        if not is_valid_url(url):
            return jsonify({"error": "الرابط غير صالح أو مفقود"}), 400

        # التحقق من المنصة
        if not is_supported_platform(url):
            return jsonify({"error": "المنصة غير مدعومة حالياً"}), 400

        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "error": None,
                "file": None,
                "filename": None,
                "title": None,
                "size": 0,
                "created_at": datetime.now().isoformat(),
            }

        logger.info(f"New download job created: {job_id} for URL: {url[:50]}...")
        threading.Thread(target=worker, args=(job_id, url, quality), daemon=True).start()
        return jsonify({"id": job_id, "status": "queued"})
    except Exception as e:
        logger.error(f"Error in download endpoint: {e}", exc_info=True)
        return jsonify({"error": "خطأ في الخادم"}), 500


@app.get("/api/status")
def api_status():
    """التحقق من حالة المهمة"""
    try:
        job_id = request.args.get("id", "").strip()
        if not job_id:
            return jsonify({"error": "معرف المهمة مفقود"}), 400

        with jobs_lock:
            job = dict(jobs.get(job_id, {}))

        if not job:
            return jsonify({"status": "error", "error": "المهمة غير موجودة"}), 404
        return jsonify(job)
    except Exception as e:
        logger.error(f"Error in status endpoint: {e}")
        return jsonify({"error": "خطأ في الخادم"}), 500


@app.get("/api/file")
def api_file():
    """تحميل الملف"""
    try:
        job_id = request.args.get("id", "").strip()
        if not job_id:
            return jsonify({"error": "معرف المهمة مفقود"}), 400

        with jobs_lock:
            job = jobs.get(job_id)

        if not job or job.get("status") != "done":
            return jsonify({"error": "الملف غير جاهز"}), 404

        file_path = job.get("file")
        if not file_path or not os.path.isfile(file_path):
            return jsonify({"error": "الملف غير موجود"}), 404

        logger.info(f"File download started: {job.get('filename')}")
        return send_file(
            file_path,
            as_attachment=True,
            download_name=job.get("filename", "TiikSave.mp4"),
            mimetype="video/mp4"
        )
    except Exception as e:
        logger.error(f"Error in file endpoint: {e}")
        return jsonify({"error": "خطأ في الخادم"}), 500


@app.get("/api/stats")
def api_stats():
    """إحصائيات الخادم"""
    with jobs_lock:
        total_jobs = len(jobs)
        completed = sum(1 for j in jobs.values() if j.get("status") == "done")
        failed = sum(1 for j in jobs.values() if j.get("status") == "error")
        processing = sum(1 for j in jobs.values() if j.get("status") in ["downloading", "processing"])
    
    return jsonify({
        "total_jobs": total_jobs,
        "completed": completed,
        "failed": failed,
        "processing": processing,
        "timestamp": datetime.now().isoformat(),
    })


@app.errorhandler(404)
def page_not_found(_error):
    """معالج خطأ 404"""
    if request.path.startswith("/api/"):
        return jsonify({"error": "الصفحة غير موجودة"}), 404
    try:
        return send_from_directory(str(FRONTEND_DIR), "404.html")
    except Exception:
        return jsonify({"error": "الصفحة غير موجودة"}), 404


@app.errorhandler(500)
def server_error(_error):
    """معالج خطأ 500"""
    logger.error(f"Server error: {_error}")
    return jsonify({"error": "خطأ في الخادم"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_ENV", "development") == "development"
    logger.info(f"Starting TikSave on port {port}")
    logger.info(f"FFmpeg: {ffmpeg_bin()}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=debug)

__all__ = ["app"]
