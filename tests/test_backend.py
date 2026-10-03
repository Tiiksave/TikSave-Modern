from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import base64
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import app as server  # noqa: E402


class DownloaderSafetyTests(unittest.TestCase):
    def setUp(self):
        server.app.config.update(TESTING=True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_patch = patch.object(server, "DB_PATH", self.temp_path / "jobs.sqlite3")
        self.downloads_patch = patch.object(server, "DOWNLOADS_DIR", self.temp_path / "downloads")
        self.db_patch.start()
        self.downloads_patch.start()
        server.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        server.init_db()
        with patch.dict(os.environ, {"YTDLP_COOKIE_FILE": "", "YTDLP_COOKIE_BASE64": ""}):
            self.assertEqual(server.cookie_configuration_status(), "not_configured")
        self.client = server.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.downloads_patch.stop()
        self.temp_dir.cleanup()

    def insert_job(self, job_id, status="queued", **values):
        now = values.pop("updated_at", time.time())
        fields = {
            "id": job_id,
            "url": "https://www.youtube.com/watch?v=example",
            "status": status,
            "progress": 0,
            "created_at": now,
            "updated_at": now,
            **values,
        }
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with server.db() as conn:
            conn.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )

    def test_recognized_platform_urls_are_accepted_without_network(self):
        self.assertTrue(server.valid_url("https://www.youtube.com/watch?v=example"))
        self.assertTrue(server.valid_url("https://www.tiktok.com/@creator/video/123"))

    def test_generic_and_private_urls_are_rejected(self):
        self.assertFalse(server.valid_url("https://example.invalid/video"))
        self.assertFalse(server.valid_url("http://127.0.0.1/video"))
        self.assertFalse(server.valid_url("file:///etc/passwd"))
        self.assertFalse(server.valid_url("https://user:pass@youtube.com/watch?v=example"))

    def test_download_requires_a_valid_json_payload(self):
        response = self.client.post("/api/download", json={"url": "https://example.invalid/video"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_home_and_health_routes_are_available(self):
        home = self.client.get("/")
        health = self.client.get("/api/health")
        try:
            self.assertEqual(home.status_code, 200)
            self.assertIn("منصات التواصل".encode(), home.data)
            health_payload = health.get_json()
            self.assertEqual(health_payload["status"], "ok")
            self.assertEqual(health_payload["downloader"], "yt-dlp")
            self.assertEqual(health_payload["cookies"], "not_configured")
        finally:
            home.close()
            health.close()

    def test_download_capacity_returns_a_retryable_response(self):
        acquired = []
        try:
            for _ in range(server.MAX_CONCURRENT):
                acquired.append(server.ACTIVE_SLOTS.acquire(blocking=False))
            response = self.client.post(
                "/api/download",
                json={"url": "https://www.youtube.com/watch?v=example", "quality": "best"},
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "30")
        finally:
            for did_acquire in acquired:
                if did_acquire:
                    server.ACTIVE_SLOTS.release()

    def test_status_does_not_disclose_server_file_path(self):
        self.insert_job(
            "test-job",
            status="done",
            progress=100,
            file="C:/private/downloads/secret.mp4",
            filename="secret.mp4",
        )

        response = self.client.get("/api/status?id=test-job")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("file", response.get_json())
        self.assertNotIn("C:/private", response.get_data(as_text=True))

    def test_cookie_file_is_optional_and_checked(self):
        with patch.dict(os.environ, {"YTDLP_COOKIE_FILE": "", "YTDLP_COOKIE_BASE64": ""}):
            self.assertIsNone(server.cookie_file())

        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            with patch.dict(os.environ, {"YTDLP_COOKIE_FILE": str(cookie_path)}):
                self.assertEqual(server.cookie_file(), str(cookie_path.resolve()))
            with patch.dict(os.environ, {"YTDLP_COOKIE_FILE": str(cookie_path) + ".missing"}):
                with self.assertRaises(RuntimeError):
                    server.cookie_file()

    def test_base64_cookie_secret_accepts_wrapped_netscape_file(self):
        cookie_bytes = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tTEST\tvalue\n"
        encoded = base64.b64encode(cookie_bytes).decode("ascii")
        wrapped = encoded[:12] + "\n" + encoded[12:]
        with patch("app.tempfile.gettempdir", return_value=str(self.temp_path)), \
             patch.dict(os.environ, {"YTDLP_COOKIE_FILE": "", "YTDLP_COOKIE_BASE64": wrapped}):
            cookie_path = Path(server.cookie_file())
            self.assertEqual(cookie_path.read_bytes(), cookie_bytes)
            self.assertEqual(server.cookie_configuration_status(), "configured")

    def test_worker_passes_configured_cookie_file_to_yt_dlp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            cookie_path = temp_path / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            job_id = "cookie-test"
            options_seen = {}

            class FakeYoutubeDL:
                def __init__(self, options):
                    options_seen.update(options)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def extract_info(self, _url, download):
                    self.assert_download = download
                    output_path = options_seen["outtmpl"].replace("%(ext)s", "mp4")
                    Path(output_path).write_bytes(b"media")
                    return {"title": "test"}

            with patch.object(server, "DOWNLOADS_DIR", temp_path), \
                 patch.object(server.yt_dlp, "YoutubeDL", FakeYoutubeDL), \
                 patch.object(server, "ffmpeg_bin", return_value=None), \
                 patch.dict(os.environ, {"YTDLP_COOKIE_FILE": str(cookie_path), "YTDLP_COOKIE_BASE64": ""}):
                self.insert_job(job_id)
                self.assertTrue(server.ACTIVE_SLOTS.acquire(blocking=False))
                server.worker(job_id, "https://www.youtube.com/watch?v=example", "best")

            self.assertEqual(options_seen["cookiefile"], str(cookie_path.resolve()))
            self.assertEqual(server.get_job(job_id)["status"], "done")

    def test_expired_jobs_and_their_files_are_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            download_dir = Path(temp_dir)
            old_file = download_dir / "expired-job.mp4"
            old_file.write_bytes(b"temporary media")
            stale_time = time.time() - server.JOB_TTL_SECONDS - 1
            with patch.object(server, "DOWNLOADS_DIR", download_dir):
                self.insert_job(
                    "expired-job",
                    status="done",
                    file=str(old_file),
                    updated_at=stale_time,
                )
                server.cleanup_expired()
                self.assertFalse(old_file.exists())
                self.assertIsNone(server.get_job("expired-job"))

    def test_login_errors_are_explained_without_exposing_raw_details(self):
        message = server.friendly_error(
            RuntimeError("Sign in to confirm you are not a bot"),
            cookies_configured=False,
        )
        self.assertIn("لا يوجد ملف كوكيز مهيأ", message)
        self.assertNotIn("not a bot", message)
        stale_message = server.friendly_error(
            RuntimeError("Sign in to confirm you are not a bot"),
            cookies_configured=True,
        )
        self.assertIn("رفض جلسة الكوكيز", stale_message)

    def test_internal_download_paths_are_not_returned_in_errors(self):
        message = server.friendly_error(RuntimeError("failed to write /srv/private/downloads/secret.part"))
        self.assertNotIn("/srv/private", message)


if __name__ == "__main__":
    unittest.main()
