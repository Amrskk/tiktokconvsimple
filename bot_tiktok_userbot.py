import os
import re
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from yt_dlp import YoutubeDL
from dotenv import load_dotenv
import json
import subprocess
import asyncio
from typing import List, Iterable
import shutil
import sys

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "tiktok_userbot")
ONLY_PRIVATE = os.getenv("ONLY_PRIVATE", "true").strip().lower() not in {"0", "false", "no"}
ALLOWED = {x.strip().lower() for x in os.getenv("ALLOWED_CHATS", "").split(",") if x.strip()}
COOKIES = os.getenv("TIKTOK_COOKIES")
MAX_MB = float(os.getenv("MAX_MB", "2000"))  # per-file cap for sending
AUTO_CLEAN = os.getenv("AUTO_CLEAN", "true").strip().lower() not in {"0", "false", "no"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# TikTok URL detector
TT_REGEX = re.compile(
    r"(https?://(?:(?:www|vt|vm|m)\.)?tiktok\.com/[^\s]+)",
    re.IGNORECASE,
)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ---------- Helpers ----------
def normalize_tiktok_url(u: str) -> str:
    parts = urlsplit(u)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), "", ""))

def ydl_opts_for(url: str, target: Path | None) -> dict:
    """Opts for yt-dlp (probe if target=None; download if target is file path)."""
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": UA},
    }
    if target is not None:
        opts.update({
            "outtmpl": str(target),
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        })
    if COOKIES and Path(COOKIES).exists():
        opts["cookiefile"] = COOKIES
    return opts

def human_mb(bytes_size: int) -> float:
    return round(bytes_size / (1024 * 1024), 2)

def peer_str(peer) -> str:
    if isinstance(peer, (PeerUser, PeerChat, PeerChannel)):
        return str(peer)
    return str(peer)

@asynccontextmanager
async def typing_action(client: TelegramClient, entity):
    try:
        await client.send_chat_action(entity, "typing")
        yield
    finally:
        pass  # auto-clears after sending a message

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

def _chat_key(event: events.NewMessage.Event) -> str | None:
    if getattr(event.chat, "username", None):
        return f"@{event.chat.username}".lower()

    for attr in ("id",):
        if hasattr(event.chat, attr):
            return str(getattr(event.chat, attr))
    peer = event.message.peer_id
    for attr in ("user_id", "chat_id", "channel_id"):
        if hasattr(peer, attr):
            return str(getattr(peer, attr))
    return None

def chat_is_allowed(event: events.NewMessage.Event) -> bool:
    if ONLY_PRIVATE and not event.is_private:
        return False
    if not ALLOWED:
        return True
    key = _chat_key(event)
    return key is not None and key.lower() in ALLOWED

# ---------- Core: detect + download ----------

def probe_tiktok(url: str) -> dict:
    try:
        with YoutubeDL(ydl_opts_for(url, None)) as ydl:
            info = ydl.extract_info(url, download=False)
            return info or {}
    except Exception as e:
        msg = str(e)
        return {"_probe_error": msg, "_unsupported": ("Unsupported URL" in msg) or ("RegexMatchError" in msg)}

def is_slideshow_from_info(info: dict) -> bool:
    if not info or "_probe_error" in info:
        return False
    duration = info.get("duration", None)
    thumbs = info.get("thumbnails") or []
    # If we see multiple images in "entries" (playlist-like), treat as slideshow
    entries = info.get("entries") or []
    if entries and all(isinstance(x, dict) for x in entries):
        # Some TikTok photo posts expose multiple items
        return True
    return (duration in (None, 0)) and (len(thumbs) >= 2)

def run_gallery_dl(url: str, out_dir: Path) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--quiet",
        "-d", str(out_dir),
        "--http-header", f"User-Agent={UA}",
    ]
    if COOKIES and Path(COOKIES).exists():
        cmd.extend(["--cookies", COOKIES])
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True)

def collect_images(dir_path: Path) -> List[Path]:
    """
    Collect images (common formats) from a dir, sorted by natural name.
    """
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
    imgs = [p for p in dir_path.rglob("*") if p.suffix.lower() in exts and p.is_file()]
    # Sort by path for stable order
    imgs.sort(key=lambda p: str(p))
    return imgs

async def send_in_albums(
    chat_id,
    files: List[Path],
    caption_head: str,
    per_album: int = 10,
):
    """
    Send images in Telegram albums (max 10 per album).
    """
    # First album carries the caption; next ones have no caption to avoid repetition
    for i in range(0, len(files), per_album):
        batch = files[i:i + per_album]
        cap = caption_head if i == 0 else None
        await client.send_file(chat_id, [str(x) for x in batch], caption=cap)

def safe_unlink(p: Path):
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass

def clean_video_files(files: Iterable[Path]):
    for p in files:
        safe_unlink(p)
    # remove possible partial fragments
    for p in files:
        for frag in p.parent.glob(p.stem + ".*.part"):
            safe_unlink(frag)

def clean_slideshow_dir(dir_path: Path):
    try:
        shutil.rmtree(dir_path, ignore_errors=True)
    except Exception:
        pass

# ---------- Handler ----------

@client.on(events.NewMessage(pattern=TT_REGEX))
async def tiktok_handler(event: events.NewMessage.Event):
    if not chat_is_allowed(event):
        return

    m = event.message.message or ""
    match = TT_REGEX.search(m)
    if not match:
        return

    raw_url = match.group(0)
    url = normalize_tiktok_url(raw_url)
    reply = await event.reply(f"Сканирую твою ссылку..(звучит круто да):\n{url}")

    base = DOWNLOAD_DIR / f"tt_{event.id}"
    tmp_mp4 = base.with_suffix(".mp4")
    slide_dir = base.with_suffix("")  # folder for slideshow images
    slide_dir.mkdir(exist_ok=True)

    success_video = False
    success_slideshow = False

    try:
        info = probe_tiktok(url)
        slideshow = is_slideshow_from_info(info)
        if info.get("_unsupported"):
            slideshow = True
        if slideshow:
            await reply.edit("А блин бро тут картинки, ща скачаю тогда")
            proc = run_gallery_dl(url, slide_dir)
            if proc.returncode != 0:
                err = proc.stderr.strip() or proc.stdout.strip()
                await reply.edit(f"Не получилось бро сорян (gallery-dl): {err[:500]}")
                return

            imgs = collect_images(slide_dir)
            if not imgs:
                await reply.edit("Лол че за хуня хахсхсхахвха, не нашлось картинок")
                return

            # Filter by size limit per file
            filtered = []
            skipped = 0
            for p in imgs:
                size_mb = human_mb(p.stat().st_size)
                if size_mb <= MAX_MB:
                    filtered.append(p)
                else:
                    skipped += 1

            if not filtered:
                await reply.edit(
                    f"Все изображения превышают лимит {MAX_MB} MB на файл. Нечего отправлять."
                )
                return

            await reply.edit(
                f"На те {len(filtered)} картинок"
                + (f" (пропущено {skipped} из-за размера)" if skipped else "")
                + "…"
            )
            await send_in_albums(
                event.chat_id,
                filtered,
                caption_head=f"мяу {len(filtered)} картинононок",
                per_album=10,
            )
            await reply.delete()
            success_slideshow = True

            # Auto-clean slideshow images after successful send
            if AUTO_CLEAN:
                clean_slideshow_dir(slide_dir)
            return

        # Otherwise treat as a normal video
        await reply.edit("качаю тикток видос жди говнюк")
        with YoutubeDL(ydl_opts_for(url, tmp_mp4)) as ydl:
            info = ydl.extract_info(url, download=True)
            # If yt-dlp changed the filename, use its final name
            final_path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
            if final_path.exists():
                tmp_mp4 = final_path

        if not tmp_mp4.exists():
            await reply.edit("Че за видос ты мне скинул, не скачался")
            return

        size_mb = human_mb(tmp_mp4.stat().st_size)
        if size_mb > MAX_MB:
            await reply.edit(f"Файл слишком большой ({size_mb} MB > {MAX_MB} MB).")
            safe_unlink(tmp_mp4)
            return

        await reply.edit("На те видео лол")
        await client.send_file(event.chat_id, file=str(tmp_mp4), caption=f"видос {size_mb} MB")
        await reply.delete()
        success_video = True

        # Auto-clean video file after successful send
        if AUTO_CLEAN:
            clean_video_files([tmp_mp4])

    except Exception as e:
        await reply.edit(f"Ошибка: {e}")
    finally:
        try:
            if AUTO_CLEAN:
                clean_video_files([tmp_mp4])
            else:
                safe_unlink(tmp_mp4)
        except Exception:
            pass

# ---------- Entrypoint ----------

def main():
    print("Starting TikTok userbot (private-only)…")
    client.start()
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
