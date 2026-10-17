"""Бот графіків відключень (РОЕ, Рівненська область).

Що вміє:
  • /grafic — надсилає актуальний графік і підтримує його в актуальному стані;
  • перевіряє сайт кожні CHECK_INTERVAL секунд, редагує повідомлення тільки
    коли графік справді змінився і шле сповіщення-відповідь (видаляється через
    NOTIFY_TTL секунд);
  • кольорові inline-кнопки (Bot API 9.4+): 🟦 primary, 🟩 success, 🟥 danger;
  • стан зберігається у файлі — після перезапуску бот не «забуває» чати.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import socket
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import urllib3.util.connection as urllib3_cn
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from bs4 import BeautifulSoup

# ───────────────────────── Налаштування ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
SCHEDULE_URL = os.getenv("SCHEDULE_URL", "https://www.roe.vsei.ua/disconnections")
QUEUE_PDF_URL = os.getenv(
    "QUEUE_PDF_URL",
    "https://www.roe.vsei.ua/wp-content/uploads/2026/02/GPV_cherga_Rivnenska_oblast.pdf",
)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))  # секунди
NOTIFY_TTL = int(os.getenv("NOTIFY_TTL", "3600"))  # через скільки видалити сповіщення
PORT = int(os.getenv("PORT", "8000"))  # Koyeb підставляє PORT сам
PROXY_URL = os.getenv("PROXY_URL")  # необов'язково: http://user:pass@host:port
FORCE_IPV4 = os.getenv("FORCE_IPV4", "1") == "1"
CACHE_TTL = 30  # не ходимо на сайт частіше ніж раз на 30 с
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

if FORCE_IPV4:  # деякі сайти «висять» при спробі з'єднання по IPv6
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

dp = Dispatcher()


def now_kyiv() -> str:
    return datetime.now(TZ).strftime("%H:%M")


# ───────────────────────── Стан (з диску) ─────────────────────────
@dataclass
class Tracked:
    chat_id: int
    message_id: int
    thread_id: int | None = None


class State:
    """Список повідомлень для автооновлення + хеш останнього графіка."""

    def __init__(self, path: Path):
        self.path = path
        self.messages: list[Tracked] = []
        self.last_hash: str | None = None
        self.lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.messages = [Tracked(**m) for m in data.get("messages", [])]
            self.last_hash = data.get("last_hash")
            log.info("Стан завантажено: %d повідомлень", len(self.messages))
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Не вдалося прочитати %s — починаю з чистого стану", self.path)

    def save(self) -> None:
        payload = {"messages": [asdict(m) for m in self.messages], "last_hash": self.last_hash}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)  # атомарно — файл ніколи не буде напівзаписаним
        except OSError:
            log.exception("Не вдалося зберегти стан")

    def find(self, chat_id: int, thread_id: int | None) -> Tracked | None:
        return next(
            (m for m in self.messages if m.chat_id == chat_id and m.thread_id == thread_id),
            None,
        )

    def remove(self, tracked: Tracked) -> None:
        if tracked in self.messages:
            self.messages.remove(tracked)
            self.save()


state = State(STATE_FILE)
_background: set[asyncio.Task] = set()  # тримаємо посилання, щоб задачі не збирались GC


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ───────────────────────── Парсинг сайту ─────────────────────────
@dataclass
class Schedule:
    text: str
    hash: str
    fetched_at: float


_cache: Schedule | None = None
_fetch_lock = asyncio.Lock()


def parse_schedule(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    container = soup.find("div", {"id": "fetched-data-container"})
    table = container.find("table") if container else None
    if table is None:
        raise ValueError("Таблицю графіка не знайдено — можливо, змінилась верстка сайту")

    parts: list[str] = []
    for row in table.find_all("tr")[4:]:
        cols = row.find_all("td")
        if not cols:
            continue

        date = html.escape(cols[0].get_text(strip=True))
        times = [html.escape(c.get_text(" ", strip=True)) for c in cols[1:]]

        lines: list[str] = []
        for i in range(0, len(times), 2):
            queue = i // 2 + 1
            for sub, idx in ((1, i), (2, i + 1)):
                value = times[idx] if idx < len(times) else ""
                if value and value != "—":
                    lines.append(f"⚡ Черга {queue}.{sub}: {value}")

        parts.append(f"📅 <b>{date}</b>")
        body = "\n\n".join(lines) or "Відключень немає ✅"
        parts.append(f"<pre>{body}</pre>")

    if not parts:
        raise ValueError("Графік порожній")
    return "\n".join(parts)


def _download_sync() -> str:
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    resp = requests.get(SCHEDULE_URL, timeout=(10, 30), proxies=proxies)
    resp.raise_for_status()
    return resp.text


async def _download() -> str:
    last_exc: Exception | None = None
    for attempt in range(1, 3):
        try:
            return await asyncio.to_thread(_download_sync)  # не блокує event loop
        except Exception as exc:  # мережа, таймаут, HTTP-статус
            last_exc = exc
            log.warning("Спроба %d/2 завантажити сайт: %s: %s", attempt, type(exc).__name__, exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"Сайт недоступний: {type(last_exc).__name__}: {last_exc}")


async def get_schedule(force: bool = False) -> Schedule:
    """Повертає графік; не частіше ніж раз на CACHE_TTL секунд ходить на сайт."""
    global _cache
    async with _fetch_lock:
        if _cache and not force and time.monotonic() - _cache.fetched_at < CACHE_TTL:
            return _cache
        page = await _download()
        text = await asyncio.to_thread(parse_schedule, page)  # bs4 не блокує event loop
        digest = hashlib.sha256(text.encode()).hexdigest()
        _cache = Schedule(text, digest, time.monotonic())
        return _cache


def render(schedule: Schedule, changed_at: str | None = None) -> str:
    stamp = changed_at or now_kyiv()
    return (
        "⚡️ <b>Графік відключень</b>\n\n"
        f"{schedule.text}\n\n"
        f"🕒 Оновлено о {stamp}"
    )


# ───────────────────────── Кнопки ─────────────────────────
# style: "primary" (синя) · "success" (зелена) · "danger" (червона)
def schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Дізнатись свою чергу",
                    url=QUEUE_PDF_URL,
                    style="primary",
                )
            ]
        ]
    )


def notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❓ Що змінилось?", callback_data="explain", style="primary")]]
    )


# ───────────────────────── Безпечні виклики Telegram ─────────────────────────
class Gone(Exception):
    """Повідомлення/чат більше недоступні — слід прибрати зі стану."""


async def safe_edit(bot: Bot, tracked: Tracked, text: str) -> None:
    for attempt in range(2):
        try:
            await bot.edit_message_text(
                chat_id=tracked.chat_id,
                message_id=tracked.message_id,
                text=text,
                reply_markup=schedule_keyboard(),
                link_preview_options={"is_disabled": True},
            )
            return
        except TelegramRetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return
            if any(s in msg for s in ("not found", "can't be edited", "chat not found")):
                raise Gone from exc
            raise
        except TelegramForbiddenError as exc:
            raise Gone from exc


async def push_update(bot: Bot, schedule: Schedule, *, notify: bool) -> None:
    """Оновлює всі відстежувані повідомлення (+ за потреби шле сповіщення)."""
    text = render(schedule)
    notified: set[tuple[int, int | None]] = set()

    for tracked in list(state.messages):
        try:
            await safe_edit(bot, tracked, text)
        except Gone:
            log.info("Чат %s більше недоступний — прибираю", tracked.chat_id)
            state.remove(tracked)
            continue
        except Exception:
            log.exception("Помилка оновлення чату %s", tracked.chat_id)
            continue

        key = (tracked.chat_id, tracked.thread_id)
        if notify and key not in notified:
            notified.add(key)
            await send_notification(bot, tracked)
        await asyncio.sleep(0.05)  # м'яко до лімітів Telegram


async def send_notification(bot: Bot, tracked: Tracked) -> None:
    try:
        sent = await bot.send_message(
            chat_id=tracked.chat_id,
            message_thread_id=tracked.thread_id,
            text=f"🔄 <b>Зафіксовано зміну у графіку!</b>\n🕒 {now_kyiv()}",
            reply_markup=notify_keyboard(),
            reply_parameters=ReplyParameters(
                message_id=tracked.message_id, allow_sending_without_reply=True
            ),
        )
    except Exception:
        log.exception("Не вдалося надіслати сповіщення")
        return
    spawn(delete_later(bot, tracked.chat_id, sent.message_id, NOTIFY_TTL))


async def delete_later(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    with suppress(TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


# ───────────────────────── Хендлери ─────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>Вітаю! Я бот графіків відключень.</b>\n\n"
        "⚡ <b>Команди</b>\n"
        "/grafic — показати актуальний графік\n"
        "/start — це повідомлення\n\n"
        "📊 <b>Як це працює</b>\n"
        f"• графік перевіряється кожні {CHECK_INTERVAL // 60} хв\n"
        "• при змінах — повідомлення оновиться і прийде сповіщення\n"
        "• працюю і в групах, і в темах (topics)\n\n"
        "Натисни /grafic, щоб почати 👇"
    )


@dp.message(Command("grafic"))
async def cmd_grafic(message: Message, bot: Bot) -> None:
    try:
        schedule = await get_schedule()
    except Exception as exc:
        log.error("Помилка отримання графіка: %s", exc)
        reason = html.escape(f"{type(exc).__name__}: {exc}"[:300])
        await message.answer(
            "😕 Не вдалося отримати графік із сайту. Спробуй за хвилину.\n"
            f"<code>{reason}</code>"
        )
        return

    thread_id = message.message_thread_id if message.is_topic_message else None

    async with state.lock:
        # Старе повідомлення прибираємо, щоб нове було внизу чату
        if old := state.find(message.chat.id, thread_id):
            with suppress(TelegramBadRequest, TelegramForbiddenError):
                await bot.delete_message(old.chat_id, old.message_id)
            state.messages.remove(old)

        sent = await message.answer(render(schedule), reply_markup=schedule_keyboard(),
                                    link_preview_options={"is_disabled": True})
        state.messages.append(Tracked(message.chat.id, sent.message_id, thread_id))
        state.last_hash = schedule.hash
        state.save()


@dp.callback_query(F.data.in_({"explain", "update_explanation"}))
async def cb_explain(call: CallbackQuery) -> None:
    await call.answer(
        "ℹ️ Графік відключень змінився\n\nМожливі зміни:\n• Зміни в днях\n• Зміни в годинах",
        show_alert=True,
        cache_time=60,
    )


# ───────────────────────── Фонова перевірка ─────────────────────────
async def auto_update(bot: Bot) -> None:
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        if not state.messages:
            continue
        try:
            schedule = await get_schedule(force=True)
            async with state.lock:
                if schedule.hash == state.last_hash:
                    log.info("✔ Змін немає")
                    continue
                first_run = state.last_hash is None
                state.last_hash = schedule.hash
                state.save()
                log.info("🔄 Виявлено зміни у графіку")
                await push_update(bot, schedule, notify=not first_run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Помилка автооновлення: %s", exc)


# ───────────────────────── HTTP для UptimeRobot / Koyeb ─────────────────────────
async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "tracked_chats": len(state.messages)})


async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)  # GET і HEAD (UptimeRobot використовує HEAD)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("🌐 HTTP-сервер слухає порт %d", PORT)
    return runner


# ───────────────────────── Запуск ─────────────────────────
async def startup_check() -> None:
    """Одразу після запуску перевіряє доступ до сайту — результат видно в логах."""
    try:
        schedule = await get_schedule(force=True)
        log.info("✅ Сайт доступний, графік отримано (%d символів)", len(schedule.text))
    except Exception as exc:
        log.error("❌ Сайт недоступний при старті: %s: %s", type(exc).__name__, exc)


async def main() -> None:
    if not TOKEN:
        sys.exit("❌ Змінна середовища BOT_TOKEN не задана")

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    runner = await start_web()
    spawn(startup_check())
    updater = asyncio.create_task(auto_update(bot))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("🚀 Бот запущено")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        updater.cancel()
        for task in list(_background):
            task.cancel()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
