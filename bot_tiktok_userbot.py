import os
import re
from pathlib import Path
from contextlib import asynccontextmanager

from telethon import TelegramClient, events
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from yt_dlp import YoutubeDL
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "tiktok_userbot")
ONLY_PRIVATE = os.getenv("ONLY_PRIVATE", "true").strip().lower() not in {"0", "false", "no"}
ALLOWED = {x.strip().lower() for x in os.getenv("ALLOWED_CHATS", "").split(",") if x.strip()}
COOKIES = os.getenv("TIKTOK_COOKIES")
MAX_MB = float(os.getenv("MAX_MB", "2000"))  

# TikTok URL detector
TT_REGEX = re.compile(
    r"(https?://(?:(?:www|vt|vm|m)\.)?tiktok\.com/[^\s]+)",
    re.IGNORECASE,
)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

def ydl_opts_for(url: str, target: Path) -> dict:
    opts = {
        "outtmpl": str(target),        # exact file path
        "noplaylist": True,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
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

@client.on(events.NewMessage(pattern=TT_REGEX))
async def tiktok_handler(event: events.NewMessage.Event):
    if not chat_is_allowed(event):
        return

    m = event.message.message or ""
    match = TT_REGEX.search(m)
    if not match:
        return

    url = match.group(0)
    reply = await event.reply(f" Обрабатываю ссылку:\n{url}")

    # Unique filename per task
    base = DOWNLOAD_DIR / f"tt_{event.id}"
    tmp_mp4 = base.with_suffix(".mp4")

    try:
        with YoutubeDL(ydl_opts_for(url, tmp_mp4)) as ydl:
            info = ydl.extract_info(url, download=True)
            # If yt-dlp changed the filename, use its final name
            final_path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
            if final_path.exists():
                tmp_mp4 = final_path

        if not tmp_mp4.exists():
            await reply.edit(" Не удалось скачать видео.")
            return

        size_mb = human_mb(tmp_mp4.stat().st_size)
        if size_mb > MAX_MB:
            await reply.edit(f" Файл слишком большой ({size_mb} MB > {MAX_MB} MB).")
            tmp_mp4.unlink(missing_ok=True)
            return

        await reply.edit("Отправляю видео…")
        await event.respond(file=str(tmp_mp4), caption=f" TikTok • {size_mb} MB")
        await reply.delete()
    except Exception as e:
        await reply.edit(f" Ошибка: {e}")
    finally:
        # Clean up
        try:
            tmp_mp4.unlink(missing_ok=True)
        except Exception:
            pass

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    if not chat_is_allowed(event):
        return
    await event.reply(
        "Привет! Пришли ссылку на TikTok в ЛС — я верну видео файлом.\n"
        "Бот отвечает только в приватных чатах."
    )

def main():
    print("Starting TikTok userbot (private-only)…")
    client.start()
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
