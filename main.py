# main.py

import asyncio
import os
import tempfile
import shutil
import logging
import time
import uuid
import aiohttp
import tempfile
import os
import asyncio
from yt_dlp import YoutubeDL
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
    FSInputFile, BotCommand
)
from yt_dlp import YoutubeDL

# ----------------- Настройки -----------------
API_TOKEN = "8736949755:AAG8So7fVUlyNpJxmGQptWQNk5bx7kjPoLs"   # <- вставь токен
DB_PATH = "bot_users.db"
DOWNLOAD_WORKERS = 1
LOG_LEVEL = logging.INFO

# Админы (только они могут выдавать премиум)
# Узнать свой ID можно несколькими способами (например, написать боту @userinfobot)
ADMIN_IDS = [6705555401]  # <- вставь сюда свой Telegram user id (число)

# Лимиты по уровням
LIMITS = {"обычный": 4, "золотой": 10, "алмазный": None}  # None = неограниченно

# Форматы yt-dlp
YDL_FORMATS = {
    "diamond": "bestvideo+bestaudio/best",
    "normal": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
}

YDL_COMMON_OPTS = {
    "noplaylist": True,
    "no_warnings": True,
    "quiet": True,
}

# Логи
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ----------------- Бот / очередь -----------------
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

@dataclass
class DownloadJob:
    id: str
    user_id: int
    chat_id: int
    url: str
    premium_level: str
    request_time: float

download_queue: deque[DownloadJob] = deque()
queue_lock = asyncio.Lock()
awaiting_link: dict[int, bool] = {}  # user_id -> waiting for link

# ----------------- База данных -----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                premium TEXT DEFAULT 'обычный',
                downloads_today INTEGER DEFAULT 0,
                last_reset TEXT
            )
        """)
        await db.commit()
    logger.info("DB initialized")

async def ensure_user(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(id, username, last_reset) VALUES(?,?,?)",
                         (user_id, username, datetime.utcnow().isoformat()))
        await db.commit()

async def get_user_row(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, username, premium, downloads_today, last_reset FROM users WHERE id=?", (user_id,)) as cur:
            return await cur.fetchone()

async def set_premium(user_id: int, level: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET premium=? WHERE id=?", (level, user_id))
        await db.commit()

async def increment_download(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET downloads_today = downloads_today + 1 WHERE id=?", (user_id,))
        await db.commit()

async def reset_if_needed(user_id: int):
    row = await get_user_row(user_id)
    if not row:
        return
    last_reset = row[4]
    if not last_reset:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET last_reset=? WHERE id=?", (datetime.utcnow().isoformat(), user_id))
            await db.commit()
        return
    last_dt = datetime.fromisoformat(last_reset)
    if datetime.utcnow() - last_dt >= timedelta(days=1):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET downloads_today=0, last_reset=? WHERE id=?", (datetime.utcnow().isoformat(), user_id))
            await db.commit()

async def can_user_download(user_id: int) -> bool:
    await reset_if_needed(user_id)
    row = await get_user_row(user_id)
    if not row:
        return True
    premium = row[2]
    downloads_today = row[3] or 0
    limit = LIMITS.get(premium, 4)
    if limit is None:
        return True
    return downloads_today < limit

# ----------------- UI / команды -----------------
def main_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🎬 Скачать видео", callback_data="download")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
        [InlineKeyboardButton(text="💎 Премиум подписка", callback_data="premium")],
    ])

# регистрируем команды (чтобы они появлялись при вводе '/')
async def register_commands():
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Профиль"),
        BotCommand(command="download", description="Скачать видео"),
        BotCommand(command="about", description="О боте"),
        BotCommand(command="premium", description="Информация о премиум"),
        BotCommand(command="grant_premium", description="(Админ) выдать премиум: /grant_premium <user_id> <level>")
    ]
    await bot.set_my_commands(commands)

# ----------------- Обработчики -----------------
@dp.message(CommandStart())
async def start_handler(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username)
    await msg.answer(
        "Привет! 👋\nЭтот бот скачивает YouTube Shorts и TikTok.\nНажми кнопку «Скачать видео» и отправь ссылку.",
        reply_markup=main_buttons()
    )

@dp.message(Command("profile"))
async def cmd_profile(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.username)
    row = await get_user_row(msg.from_user.id)
    if row:
        _, username, premium, downloads_today, _ = row
        await msg.answer(f"👤 Профиль\nЮзер: @{username or msg.from_user.id}\nПремиум: {premium}\nСкачиваний сегодня: {downloads_today}")
    else:
        await msg.answer("Профиль не найден. Нажми /start")

@dp.message(Command("about"))
async def cmd_about(msg: Message):
    await msg.answer("Этот бот скачивает YouTube Shorts и TikTok (через yt-dlp). Файлы удаляются после отправки.")

@dp.message(Command("download"))
async def cmd_download(msg: Message):
    awaiting_link[msg.from_user.id] = True
    await msg.answer("📩 Отправь ссылку на YouTube Shorts или TikTok")

@dp.message(Command("premium"))
async def cmd_premium(msg: Message):
    await msg.answer(
        "💎 Премиум уровни:\n"
        "- обычный: 4 видео/день (по умолчанию)\n"
        "- золотой: 10 видео/день\n"
        "- алмазный: неограниченно + приоритет\n\n"
        "Выдать премиум может только админ (т.е. ты)."
    )

# админ: /grant_premium <user_id> <level>
@dp.message(Command("grant_premium"))
async def cmd_grant_premium(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        await msg.answer("❌ Только админ может выдавать премиум.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.answer("Использование: /grant_premium <user_id> <обычный|золотой|алмазный>")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await msg.answer("Неверный user_id. Передай числовой ID получателя.")
        return
    level = parts[2].lower()
    if level not in LIMITS:
        await msg.answer("Уровень премиум некорректен. Возможные: обычный, золотой, алмазный")
        return
    await ensure_user(target_id, None)
    await set_premium(target_id, level)
    await msg.answer(f"✅ Премиум {level} выдан пользователю {target_id}.")
    try:
        await bot.send_message(target_id, f"Тебе выдали премиум: {level} (админ {msg.from_user.id})")
    except Exception:
        pass

# колбэки главного меню
@dp.callback_query(lambda c: c.data == "profile")
async def cb_profile(cq: CallbackQuery):
    await cmd_profile(cq.message)

@dp.callback_query(lambda c: c.data == "about")
async def cb_about(cq: CallbackQuery):
    await cmd_about(cq.message)

@dp.callback_query(lambda c: c.data == "premium")
async def cb_premium(cq: CallbackQuery):
    await cmd_premium(cq.message)

@dp.callback_query(lambda c: c.data == "download")
async def cb_download(cq: CallbackQuery):
    awaiting_link[cq.from_user.id] = True
    await cq.message.answer("📩 Отправь ссылку на YouTube Shorts или TikTok")
    await cq.answer()

# ----------------- Очередь загрузок -----------------
async def enqueue_download(job: DownloadJob):
    async with queue_lock:
        if job.premium_level == "алмазный":
            download_queue.appendleft(job)
        else:
            download_queue.append(job)
    logger.info("Job queued: %s", job)

# blocking yt-dlp call (runs in executor)
def run_yt_dlp_blocking(url: str, outdir: str, fmt: str):
    opts = YDL_COMMON_OPTS.copy()
    opts.update({
        "format": fmt,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
    })
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
    return filename, info

async def download_worker():
    logger.info("Download worker started")
    loop = asyncio.get_event_loop()
    while True:
        job = None
        async with queue_lock:
            if download_queue:
                job = download_queue.popleft()
        if not job:
            await asyncio.sleep(0.5)
            continue

        logger.info("Processing job: %s", job)
        # проверка лимита
        if not await can_user_download(job.user_id):
            try:
                await bot.send_message(job.chat_id, "❌ Лимит скачиваний на сегодня достигнут.")
            except Exception:
                logger.exception("notify error")
            continue

        tmpdir = tempfile.mkdtemp(prefix="bot_dl_")
        try:
            fmt = YDL_FORMATS["diamond"] if job.premium_level == "алмазный" else YDL_FORMATS["normal"]

            # ---- NEW: поддержка TikTok ----
            filename = None
            info = {}
            # если это TikTok — используем специальную async функцию
            if "tiktok" in job.url or "vm.tiktok" in job.url:
                try:
                    filename = await download_tiktok(job.url)
                    # info для TikTok может быть пустым — это нормально
                    info = {}
                except Exception as e:
                    logger.exception("TikTok download error for %s", job.url)
                    try:
                        await bot.send_message(job.chat_id, f"❌ Ошибка при скачивании TikTok: {e}")
                    except Exception:
                        pass
                    # очистим tmpdir и продолжим к следующей задаче
                    try:
                        shutil.rmtree(tmpdir)
                    except Exception:
                        pass
                    continue
            else:
                # стандартный путь: yt-dlp в executor
                def blocking():
                    return run_yt_dlp_blocking(job.url, tmpdir, fmt)
                try:
                    filename, info = await loop.run_in_executor(None, blocking)
                except Exception as e:
                    logger.exception("Download error for %s", job.url)
                    await bot.send_message(job.chat_id, f"❌ Ошибка при скачивании: {e}")
                    continue

            # thumbnail
            thumb_path = None
            thumbnail_url = info.get("thumbnail") if isinstance(info, dict) else None
            if thumbnail_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(thumbnail_url, timeout=15) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                thumb_path = os.path.join(tmpdir, "thumb.jpg")
                                with open(thumb_path, "wb") as f:
                                    f.write(data)
                except Exception:
                    thumb_path = None

            if filename and os.path.exists(filename):
                try:
                    await bot.send_chat_action(job.chat_id, "upload_video")
                    fs = FSInputFile(filename)
                    if thumb_path and os.path.exists(thumb_path):
                        thumb = FSInputFile(thumb_path)
                        await bot.send_video(job.chat_id, video=fs, thumbnail=thumb, supports_streaming=True)
                    else:
                        await bot.send_video(job.chat_id, video=fs, supports_streaming=True)
                    size_mb = os.path.getsize(filename) / 1024 / 1024
                    await bot.send_message(job.chat_id, f"✅ Готово! {size_mb:.1f} MB")
                    await increment_download(job.user_id)
                except Exception as e:
                    logger.exception("Failed to send video")
                    try:
                        await bot.send_message(job.chat_id, f"❌ Ошибка отправки видео: {e}")
                    except Exception:
                        pass
                finally:
                    # удаляем файл и (если нужно) дополнительные временные папки
                    try:
                        os.remove(filename)
                    except Exception:
                        pass
                    try:
                        if thumb_path and os.path.exists(thumb_path):
                            os.remove(thumb_path)
                    except Exception:
                        pass
                    # Если файл был создан в отдельной временной папке (download_tiktok),
                    # попробуем удалить родительскую папку, если это tmp
                    try:
                        parent = os.path.dirname(filename)
                        if parent and parent != tmpdir and parent.startswith(tempfile.gettempdir()):
                            shutil.rmtree(parent)
                    except Exception:
                        pass
            else:
                await bot.send_message(job.chat_id, "❌ Файл не найден после скачивания.")
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
        await asyncio.sleep(0.2)

# ----------------- Обработка сообщений (ссылки) -----------------
@dp.message()
async def handle_message(msg: Message):
    user_id = msg.from_user.id
    text = (msg.text or "").strip()
    if awaiting_link.get(user_id):
        awaiting_link[user_id] = False
        if not ("youtube.com" in text or "youtu.be" in text or "tiktok.com" in text):
            await msg.answer("❌ Пожалуйста, отправь ссылку на YouTube Shorts или TikTok.")
            return
        await ensure_user(user_id, msg.from_user.username)
        row = await get_user_row(user_id)
        premium_level = row[2] if row else "обычный"
        if not await can_user_download(user_id):
            await msg.answer("❌ Лимит скачиваний на сегодня исчерпан.")
            return
        job = DownloadJob(id=str(uuid.uuid4()), user_id=user_id, chat_id=msg.chat.id, url=text, premium_level=premium_level, request_time=time.time())
        await enqueue_download(job)
        await msg.answer("✔️ Ваша заявка поставлена в очередь. Ожидайте уведомления.")
    else:
        # подсказка: показываем команды
        await msg.answer("Нажми «Скачать видео» или используй /download. Для справки /about", reply_markup=main_buttons())

async def download_tiktok(url: str):
    temp_dir = tempfile.mkdtemp(prefix="tt_dl_")
    out_file = os.path.join(temp_dir, "video.mp4")

    loop = asyncio.get_event_loop()

    def run_ydl():
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename

    try:
        # ПЕРВАЯ попытка — через yt-dlp (самая стабильная)
        filename = await loop.run_in_executor(None, run_ydl)
        if filename and os.path.exists(filename):
            return filename
    except Exception as e:
        logger.debug("yt-dlp failed for TikTok: %s", e)

    # ВТОРАЯ попытка — через резервный API
    api = f"https://api.tikwm.com/?url={url}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(api, timeout=20) as resp:
                if resp.status != 200:
                    raise Exception(f"API returned {resp.status}")
                data = await resp.json()
                # безопасно получить ссылку
                video_url = (data.get("data") or {}).get("play") or (data.get("data") or {}).get("download")
                if not video_url:
                    # try find link in response text
                    text = await resp.text()
                    import re
                    urls = re.findall(r'https?://[^\s"\']+', text)
                    candidates = [u for u in urls if ".mp4" in u or "v.tiktok" in u or "vm.tiktok" in u]
                    video_url = candidates[0] if candidates else None
                if not video_url:
                    raise Exception("No video URL found in API response")

                # скачиваем видео напрямую
                async with session.get(video_url, timeout=60) as vf:
                    if vf.status != 200:
                        raise Exception(f"Video URL returned {vf.status}")
                    with open(out_file, "wb") as f:
                        while True:
                            chunk = await vf.content.read(1024 * 32)
                            if not chunk:
                                break
                            f.write(chunk)
                    if os.path.exists(out_file) and os.path.getsize(out_file) > 1000:
                        return out_file
                    else:
                        raise Exception("Downloaded file is too small or missing")
        except Exception as e:
            # очистим temp и пробросим ошибку
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
            raise Exception(f"TikTok download failed: {e}")

# ----------------- Запуск -----------------
async def main():
    await init_db()
    # зарегистрировать команды (чтобы появлялись при вводе '/')
    await register_commands()
    # старт workers
    workers = [asyncio.create_task(download_worker()) for _ in range(DOWNLOAD_WORKERS)]
    try:
        logger.info("Bot starting polling")
        await dp.start_polling(bot)
    finally:
        for w in workers:
            w.cancel()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())

ADMIN_USERNAME = "KRONIK568"  # твой юзернейм
premium_users = set()  # сюда будут добавляться админы/премиум

@dp.callback_query()
async def handle_callback(call: CallbackQuery):
    if call.data == "make_admin":
        if call.from_user.username == ADMIN_USERNAME:
            premium_users.add(call.from_user.id)
            await call.message.answer("✅ Ты теперь админ и премиум!")
        else:
            await call.message.answer("❌ Только владелец бота может это сделать.")

InlineKeyboardButton("🔑 Выдать себе админку", callback_data="make_admin")