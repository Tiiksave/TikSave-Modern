from __future__ import annotations

import glob
import ipaddress
import os
import shutil
import threading
import uuid
from functools import lru_cache
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
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

jobs: dict[str, dict] = {}
jobs_lock = threading.RLock()


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def normalize_quality(raw_quality: str | None) -> str:
    if raw_quality in (None, "", "best"):
        return "best"
    try:
        q = int(str(raw_quality).strip())
    except (TypeError, ValueError):
        return "best"
    return str(max(240, min(q, 2160)))


@lru_cache(maxsize=512)
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
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return False

    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False
    if parsed.username or parsed.password or port not in {None, 80, 443}:
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
            if os.path.isfile(path) and not path.endswith(".part"):
                continue
            os.remove(path)
        except OSError:
            pass


def hook(job_id: str, payload: dict) -> None:
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
            "progress_hooks": [lambda payload, ji=job_id: hook(ji, payload)],
        }

        ffmpeg = ffmpeg_bin()
        bun = shutil.which("bun")
        if bun:
            opts["js_runtimes"] = {"bun": {"path": bun}}
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
            })
    except Exception as exc:
        clean_partial_files(str(DOWNLOADS_DIR / job_id))
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "progress": 0,
                "error": str(exc)[:1000],
                "file": None,
            })


@app.get("/")
def index():
    return send_from_directory(str(FRONTEND_DIR), "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/download")
def api_download():
    url = request.args.get("url", "").strip()
    quality = normalize_quality(request.args.get("quality", "best"))

    if not is_valid_url(url):
        return jsonify({"error": "الرابط غير صالح أو لا ينتمي إلى موقع يدعمه محرك التحميل"}), 400

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
        }

    threading.Thread(target=worker, args=(job_id, url, quality), daemon=True).start()
    return jsonify({"id": job_id, "status": "queued"})


@app.get("/api/status")
def api_status():
    job_id = request.args.get("id", "").strip()
    with jobs_lock:
        job = dict(jobs.get(job_id, {}))

    if not job:
        return jsonify({"status": "error", "error": "المهمة غير موجودة"}), 404
    return jsonify(job)


@app.get("/api/file")
def api_file():
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
