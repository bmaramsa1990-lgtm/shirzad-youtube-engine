"""Shirzad YouTube on-demand pilot for Render Free.

No hosted YouTube access is guaranteed; this service processes public videos
that users are authorized to obtain.  Only YouTube IDs are accepted.
No permanent media store. Background threads are best-effort on free hosting.
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

VERSION = "1.5.1-youtube-render-queue"
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_QUALITIES = {144, 240, 360, 480}
MAX_FILE_BYTES = 49_000_000  # below Telegram hosted API 50,000,000 bytes
# In-memory, not saved; user can re-request if a free instance restarts.
pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shirzad-yt")
active_lock = threading.Lock()
active_users: set[int] = set()
MAX_IN_FLIGHT = 3
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shirzad-yt")
app = FastAPI(title="Shirzad YouTube backend", version=VERSION)


class Job(BaseModel):
    video_id: str = Field(min_length=11, max_length=11)
    chat_id: int = Field(gt=0)
    quality: int | None = None
    required_channels: list[int] = Field(default_factory=list, max_length=12)


def get_config():
    key = os.environ.get("SHIRZAD_BACKEND_KEY", "")
    token = os.environ.get("BOT_TOKEN", "")
    return key, token


def bot(method: str, *, data=None, files=None, timeout=50):
    token = get_config()[1]
    if not token:
        raise RuntimeError("BOT_TOKEN is missing from backend environment")
    with requests.Session() as s:
        r = s.post(f"https://api.telegram.org/bot{token}/{method}",
                   data=data, files=files, timeout=timeout)
    try:
        result = r.json()
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError(f"Telegram {method}: HTTP {r.status_code} non-JSON response") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {str(result.get('description', r.status_code))[:130]}")
    return result.get("result")


def say(chat_id: int, message: str, keyboard=None):
    payload = {"chat_id": str(chat_id), "text": message[:3900], "parse_mode": "HTML",
               "disable_web_page_preview": "true"}
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    return bot("sendMessage", data=payload)


def verify_channels(chat_id: int, channels: list[int]):
    for channel in channels:
        if not isinstance(channel, int) or channel >= 0:
            return False
        try:
            data = bot("getChatMember", data={"chat_id": str(channel), "user_id": str(chat_id)}, timeout=15)
            status = data.get("status", "")
            if status in ("member", "administrator", "creator"):
                continue
            if status == "restricted" and data.get("is_member"):
                continue
            return False
        except Exception:
            # Fail closed: no unverified memberships allowed.
            return False
    return bool(channels)


def format_choices(video_id: str):
    # yt-dlp real media inspection in a worker thread. We don't invent sizes.
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
           "--skip-download", "--dump-single-json", "--socket-timeout", "12",
           "--retries", "1", "--extractor-retries", "1",
           f"https://www.youtube.com/watch?v={video_id}"]
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=100)
    if result.returncode != 0:
        raise RuntimeError("اطلاعات ویدئو از یوتیوب دریافت نشد (ممکن است دسترسی سرور محدود باشد).")
    data = json.loads(result.stdout)
    if data.get("is_live") or data.get("live_status") in {"is_live", "is_upcoming"}:
        raise RuntimeError("پخش زنده در نسخه آزمایشی پشتیبانی نمی‌شود.")
    if data.get("availability") in {"private", "premium_only", "subscriber_only"}:
        raise RuntimeError("این ویدئو عمومی نیست.")
    title = html.escape(str(data.get("title", "ویدیوی یوتیوب"))[:125])
    heights = set()
    for f in data.get("formats", []):
        h = int(f.get("height") or 0)
        if h > 0 and str(f.get("vcodec", "none")) != "none":
            heights.add(h)
    choices = [q for q in sorted(ALLOWED_QUALITIES) if heights and any(x <= q for x in heights)]
    if not choices:
        raise RuntimeError("کیفیت یکپارچهٔ قابل دانلود زیر ۴۸۰p از سرور دریافت نشد.")
    return title, choices


def yt_download(video_id: str, quality: int, target: Path):
    # Codec-aware mux: H264+M4A then progressively less strict fallbacks.
    fmt = (
        f"bv[height<={quality}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={quality}][ext=mp4]/"
        f"bv[height<={quality}]+ba/"
        f"b[height<={quality}]"
    )
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
           "--no-part", "--no-mtime", "--restrict-filenames",
           "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
           "--extractor-retries", "1", "--max-filesize", "49M",
           "--merge-output-format", "mp4", "-f", fmt,
           "-o", str(target / "video.%(ext)s"),
           f"https://www.youtube.com/watch?v={video_id}"]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=200)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("زمان دانلود از محدودیت موتور رایگان عبور کرد.") from e
    if result.returncode:
        diagnostic = (result.stderr or result.stdout or "").lower()
        if "sign in" in diagnostic or "bot" in diagnostic or "403" in diagnostic:
            raise RuntimeError("یوتیوب دسترسی سرور رایگان را محدود کرد. ثبت‌نام یا دسترسی شخصی دور زده نمی‌شود.")
        raise RuntimeError("این کیفیت در سرور آزمایشی قابل دریافت نبود.")
    files = [p for p in target.glob("video.*") if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm"}]
    if not files:
        raise RuntimeError("فایل خروجی تولید نشد.")
    file = max(files, key=lambda x: x.stat().st_size)
    size = file.stat().st_size
    if not (0 < size <= MAX_FILE_BYTES):
        raise RuntimeError("حجم خروجی از سقف ۵۰ مگابایت Bot API معمولی بیشتر است؛ کیفیت پایین‌تر را انتخاب کن.")
    return file


def job_runner(job: Job):
    try:
        if not verify_channels(job.chat_id, job.required_channels):
            say(job.chat_id, "🔐 برای دانلود باید عضو همه کانال‌های تعیین‌شده باشی. در ربات اصلی «بررسی عضویت» را بزن.")
            return
        if job.quality is None:
            title, choices = format_choices(job.video_id)
            buttons = [[{"text": f"🎞 تا {q}p — حجم پس از دریافت مشخص می‌شود",
                         "callback_data": f"yt:{job.video_id}:{q}"}] for q in choices]
            say(job.chat_id, f"🎬 <b>{title}</b>\n\nکیفیت را انتخاب کن. اگر فایل از سقف تلگرام بزرگ‌تر باشد، اعلام می‌کنم.", buttons)
            return
        if job.quality not in ALLOWED_QUALITIES:
            say(job.chat_id, "⚠️ کیفیت معتبر نیست.")
            return
        say(job.chat_id, f"⏳ دانلود واقعی {job.quality}p آغاز شد. ممکن است کمی طول بکشد.")
        with tempfile.TemporaryDirectory(prefix="shirzad-") as temp:
            result = yt_download(job.video_id, job.quality, Path(temp))
            field = "video" if result.suffix.lower() == ".mp4" else "document"
            method = "sendVideo" if field == "video" else "sendDocument"
            form = {"chat_id": str(job.chat_id),
                    "caption": f"🎬 YouTube · حداکثر {job.quality}p"}
            if field == "video":
                form["supports_streaming"] = "true"
            with result.open("rb") as file:
                bot(method, data=form, files={field: (result.name, file, "video/mp4" if field == "video" else "application/octet-stream")}, timeout=180)
        # TemporaryDirectory automatically removes the media immediately.
    except Exception as e:
        log.warning("YouTube job failed: %s", type(e).__name__)
        try:
            say(job.chat_id, "❌ <b>دریافت یوتیوب انجام نشد.</b>\n" + html.escape(str(e))[:250] +
                "\n\nاگر دسترسی سرور محدود بود، این مشکل به معنی خصوصی‌بودن ویدئو نیست.")
        except Exception:
            log.error("Failed to send job error")
    finally:
        with active_lock:
            active_users.discard(job.chat_id)


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "engine": "yt-dlp + EJS + Deno + FFmpeg",
            "configured": bool(all(get_config())), "storage": "ephemeral"}


@app.post("/submit", status_code=202)
def submit(job: Job, x_backend_key: str | None = Header(default=None)):
    config_key, token = get_config()
    if not config_key or not token:
        raise HTTPException(503, "Backend secrets are not configured")
    if not x_backend_key or not hmac.compare_digest(x_backend_key, config_key):
        raise HTTPException(403, "Forbidden")
    if not YOUTUBE_ID.fullmatch(job.video_id):
        raise HTTPException(422, "Invalid YouTube video ID")
    if job.quality is not None and job.quality not in ALLOWED_QUALITIES:
        raise HTTPException(422, "Unsupported quality")
    if not job.required_channels:
        raise HTTPException(403, "Membership channels required")
    with active_lock:
        if job.chat_id in active_users:
            raise HTTPException(429, "A job is already running for this user")
        if len(active_users) >= MAX_IN_FLIGHT:
            raise HTTPException(429, "Free worker queue is full")
        active_users.add(job.chat_id)
    try:
        pool.submit(job_runner, job)
    except Exception:
        with active_lock:
            active_users.discard(job.chat_id)
        raise
    return {"ok": True, "accepted": True, "note": "Output will be sent directly to Telegram"}
