"""Бот графіків відключень (РОЕ, Рівненська область).

Що вміє:
  • /grafic — надсилає КАРТИНКУ з актуальним графіком і підтримує її в актуальному стані;
  • картинка ОДНА на всіх: її знімає screenshotbase.com лише коли графік змінився,
    а /grafic просто віддає вже готову (через file_id) — квота не витрачається;
  • перевіряє сайт кожні CHECK_INTERVAL секунд, міняє картинку тільки
    коли графік справді змінився і шле сповіщення-відповідь (видаляється через
    NOTIFY_TTL секунд);
  • кілька API-ключів: ліміт на одному скінчився — береться наступний;
    якщо недоступні всі — бот шле звичайний ТЕКСТ;
  • кольорові inline-кнопки (Bot API 9.4+): 🟦 primary, 🟩 success, 🟥 danger;
  • стан зберігається у файлі — після перезапуску бот не «забуває» чати.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import socket
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
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
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InputMediaPhoto,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)
from bs4 import BeautifulSoup
from PIL import Image

try:  # необов'язково: підхоплює змінні з файлу .env (зручно для локального тесту)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from render import (
    MAX_DAYS,
    demo_days as make_demo,
    interval_labels,
    parse_intervals,
    render_html,
    weekday_name,
)

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
SCREENSHOT_API_URL = os.getenv("SCREENSHOT_API_URL", "https://api.screenshotbase.com/v1/take")
DEMO = os.getenv("DEMO") == "1"  # тестові дані замість сайту
DEMO_PERIOD = int(os.getenv("DEMO_PERIOD", "120"))  # як часто міняються тестові дані, с
CACHE_TTL = 30  # не ходимо на сайт частіше ніж раз на 30 с
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
IMAGE_FILE = STATE_FILE.with_name(STATE_FILE.stem + "_image.jpg")  # остання спільна картинка
GRAFIC_COOLDOWN = 5  # с: захист від спаму /grafic з одного чату
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
    photo: bool = False  # True — повідомлення вже з картинкою (старі, текстові, замінимо)


class State:
    """Список повідомлень для автооновлення + хеш останнього графіка."""

    def __init__(self, path: Path):
        self.path = path
        self.messages: list[Tracked] = []
        self.last_hash: str | None = None
        # Спільна картинка (одна на всіх): для якого хешу знята, коли, file_id в Telegram, байти
        self.img_hash: str | None = None
        self.img_stamp: str = ""
        self.img_file_id: str | None = None
        self.img_bytes: bytes | None = None
        self.lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.messages = [Tracked(**m) for m in data.get("messages", [])]
            self.last_hash = data.get("last_hash")
            img = data.get("image") or {}
            self.img_hash = img.get("hash")
            self.img_stamp = img.get("stamp", "")
            self.img_file_id = img.get("file_id")
            if self.img_hash:
                with suppress(OSError):
                    self.img_bytes = IMAGE_FILE.read_bytes()
                if not (self.img_bytes or self.img_file_id):
                    self.img_hash = None
            log.info("Стан завантажено: %d повідомлень, картинка: %s",
                     len(self.messages), "є" if self.img_hash else "немає")
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Не вдалося прочитати %s — починаю з чистого стану", self.path)

    def save(self) -> None:
        payload = {
            "messages": [asdict(m) for m in self.messages],
            "last_hash": self.last_hash,
            "image": {"hash": self.img_hash, "stamp": self.img_stamp, "file_id": self.img_file_id},
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)  # атомарно — файл ніколи не буде напівзаписаним
        except OSError:
            log.exception("Не вдалося зберегти стан")

    def set_image(self, digest: str, stamp: str, jpg: bytes) -> None:
        """Зберігає НОВУ спільну картинку (байти — на диск, щоб пережити перезапуск)."""
        self.img_hash, self.img_stamp, self.img_bytes, self.img_file_id = digest, stamp, jpg, None
        tmp = IMAGE_FILE.with_suffix(".tmp")
        try:
            tmp.write_bytes(jpg)
            tmp.replace(IMAGE_FILE)
        except OSError:
            log.exception("Не вдалося записати картинку на диск")
        self.save()

    def invalidate_image(self) -> None:
        self.img_hash, self.img_file_id, self.img_bytes = None, None, None
        self.save()

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
    days: list[dict]  # [{"date": "...", "slots": [["1.1", "08:00-12:00"], ...]}]
    hash: str
    fetched_at: float


_cache: Schedule | None = None
_fetch_lock = asyncio.Lock()


def parse_schedule(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    container = soup.find("div", {"id": "fetched-data-container"})
    table = container.find("table") if container else None
    if table is None:
        raise ValueError("Таблицю графіка не знайдено — можливо, змінилась верстка сайту")

    days: list[dict] = []
    for row in table.find_all("tr")[4:]:
        cols = row.find_all("td")
        if not cols:
            continue
        date = cols[0].get_text(strip=True)
        times = [c.get_text(" ", strip=True) for c in cols[1:]]
        slots = []
        for idx, value in enumerate(times):  # колонки йдуть парами: 1.1, 1.2, 2.1, 2.2 ...
            slots.append([f"{idx // 2 + 1}.{idx % 2 + 1}", "" if value == "—" else value])
        days.append({"date": date, "slots": slots})

    if not days:
        raise ValueError("Графік порожній")
    return days


def demo_days() -> list[dict]:
    """Тестові дані: міняються кожні DEMO_PERIOD секунд, щоб перевірити оновлення."""
    return make_demo(int(time.time() // DEMO_PERIOD), datetime.now(TZ).date())


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
        if DEMO:
            days = demo_days()
        else:
            page = await _download()
            days = await asyncio.to_thread(parse_schedule, page)  # bs4 не блокує event loop
        digest = hashlib.sha256(json.dumps(days, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        _cache = Schedule(days, digest, time.monotonic())
        return _cache


def render_text(schedule: Schedule, stamp: str) -> str:
    """Текстова версія — запасна, коли немає картинки."""
    parts: list[str] = ["⚡️ <b>Графік відключень</b>"]
    for day in schedule.days[:MAX_DAYS]:
        outages: list[str] = []
        free: list[str] = []
        for queue, raw in day["slots"]:
            labels = interval_labels(raw, parse_intervals(raw))
            if labels:
                outages.append(f"{queue:<4}{html.escape(', '.join(labels))}")
            else:
                free.append(queue)
        weekday = weekday_name(day["date"])
        title = f"{weekday}, {day['date']}" if weekday else day["date"]
        block = f"📅 <b>{html.escape(title)}</b>\n"
        if outages:
            block += f"<pre>{chr(10).join(outages)}</pre>"
            if free:
                block += f"\n✅ Без відключень: {', '.join(free)}"
        else:
            block += "✅ Відключень немає"
        parts.append(block)
    return "\n\n".join(parts) + f"\n\n🕒 Оновлено о {stamp}"


def render_caption(stamp_iso: str) -> str:
    """Підпис під картинкою. Картинка може бути зі вчора — тоді показуємо й дату."""
    try:
        dt = datetime.fromisoformat(stamp_iso).astimezone(TZ)
    except ValueError:
        return "⚡️ <b>Графік відключень</b>"
    if dt.date() == datetime.now(TZ).date():
        when = f"о {dt:%H:%M}"
    else:
        when = f"{dt:%d.%m} о {dt:%H:%M}"
    return f"⚡️ <b>Графік відключень</b>\n🕒 Оновлено {when}"


# ───────────────────────── Картинка (screenshotbase.com) ─────────────────────────
# Одна картинка на всіх. Знімаємо її ЛИШЕ коли змінився графік (хеш), усі /grafic
# віддають уже готову через file_id Telegram — API при цьому не чіпається взагалі.
TG_MAX_SIDE_SUM = 10000   # ліміт Telegram: ширина + висота
TG_MAX_RATIO = 20         # ліміт Telegram: співвідношення сторін
TG_MAX_BYTES = 10 * 1024 * 1024
MONTH = 32 * 86400


def normalize_image(raw: bytes) -> bytes:
    """Перекодовує знімок у «безпечний» для Telegram JPEG (RGB, без альфи/профілів).

    Telegram дає IMAGE_PROCESS_FAILED на незвичні PNG (альфа, 16 біт, профілі)
    та на завеликі/задовгі картинки — тут усе це вирівнюється.
    """
    img = Image.open(io.BytesIO(raw))
    img.load()
    log.info("Знімок від API: %s %sx%s, %d байт", img.format, img.width, img.height, len(raw))
    if img.width < 100 or img.height < 100:
        raise RuntimeError(
            f"API повернув заглушку {img.width}x{img.height} (sandbox-ключ або порожній елемент #schedule)"
        )

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    w, h = img.size
    if w + h > TG_MAX_SIDE_SUM:
        k = TG_MAX_SIDE_SUM / (w + h)
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    w, h = img.size
    if max(w, h) / max(1, min(w, h)) > TG_MAX_RATIO:
        raise RuntimeError(f"Дивні пропорції знімка: {w}x{h}")

    for quality in (92, 85, 75):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True)
        if buf.tell() <= TG_MAX_BYTES:
            break
    log.info("Підготовлено для Telegram: JPEG %sx%s, %d КБ", img.width, img.height, buf.tell() // 1024)
    return buf.getvalue()


# ── API-ключі ──
@dataclass
class ApiKey:
    key: str
    n: int
    blocked_until: float = 0.0  # time.time(), до якого ключ не чіпаємо

    @property
    def label(self) -> str:  # у логах ніколи не світимо ключ повністю
        return f"ключ #{self.n} (…{self.key[-4:]})"


def _parse_keys() -> list[ApiKey]:
    """SCREENSHOT_API_KEYS=key1,key2,key3 (кома / пробіл / новий рядок); старий SCREENSHOT_API_KEY теж працює."""
    raw = " ".join(filter(None, (os.getenv("SCREENSHOT_API_KEYS"), os.getenv("SCREENSHOT_API_KEY"))))
    keys: list[str] = []
    for part in re.split(r"[,;\s]+", raw):
        if part and part not in keys:
            keys.append(part)
    return [ApiKey(k, i) for i, k in enumerate(keys, 1)]


API_KEYS = _parse_keys()


class KeyUnavailable(Exception):
    """Цей ключ тимчасово непридатний (ліміт / відхилено) — пробуємо наступний."""

    def __init__(self, seconds: int, reason: str):
        super().__init__(reason)
        self.seconds, self.reason = seconds, reason


class RenderFailed(Exception):
    """API відхилив сам запит (422/400) — інший ключ не допоможе."""


_gen_lock = asyncio.Lock()      # одна генерація за раз: 100 людей = 1 запит до API
_gen_cooldown_until = 0.0       # після збою (не через ліміт) не довбемо API кілька хвилин


def _seconds_until_reset(body, headers) -> int:
    """Скільки ключ «відпочиває» після quota_exceeded: до quota.resets_at, інакше Retry-After, інакше 1 год."""
    quota = body.get("quota") if isinstance(body, dict) else None
    raw = quota.get("resets_at") if isinstance(quota, dict) else None
    if raw is not None:
        try:
            if isinstance(raw, (int, float)) or str(raw).isdigit():
                ts = float(raw)
                ts = ts / 1000 if ts > 1e11 else ts
            else:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                ts = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
            return max(60, min(int(ts - time.time()), MONTH))
        except (ValueError, OverflowError):
            pass
    ra = headers.get("Retry-After", "")
    if ra.isdigit():
        return max(60, min(int(ra), MONTH))
    return 3600


async def render_via_key(session: aiohttp.ClientSession, k: ApiKey, html_doc: str) -> bytes:
    """Один запит до screenshotbase: шлемо готовий HTML, у відповідь — сама картинка #schedule."""
    payload = {
        "html": html_doc,
        "format": "png",
        "selector": "#schedule",
        "error_on_selector_not_found": True,
        "viewport_width": 800,
        "viewport_height": 900,
        "device_scale_factor": 2,
        "wait_until": "load",
        "timeout": 30,
    }
    async with session.post(SCREENSHOT_API_URL, json=payload, headers={"apikey": k.key}) as r:
        if r.status != 200:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = {}
            err = body.get("error") if isinstance(body, dict) else None
            code = (err.get("code") if isinstance(err, dict) else "") or ""
            retry_after = r.headers.get("Retry-After", "")
            if r.status == 429:
                if "quota" in code:
                    raise KeyUnavailable(_seconds_until_reset(body, r.headers), "ліміт на місяць вичерпано")
                # rate_limit_exceeded / concurrency_limit_exceeded — тимчасово
                raise KeyUnavailable(int(retry_after) if retry_after.isdigit() else 30, code or "429")
            if r.status in (401, 403):
                raise KeyUnavailable(86400, f"ключ відхилено ({r.status} {code})")
            if r.status in (400, 422):
                raise RenderFailed(f"API {r.status} {code}: {str(body)[:300]}")
            raise RuntimeError(f"API {r.status} {code}: {str(body)[:300]}")
        remaining = r.headers.get("X-RateLimit-Remaining-Quota-Month")
        if remaining is not None:
            log.info("screenshotbase, %s: залишилось на місяць: %s", k.label, remaining)
        ctype = r.headers.get("Content-Type", "")
        raw = await r.read()
    if not ctype.startswith("image/"):
        raise RuntimeError(f"API повернув не картинку ({ctype}, {len(raw)} байт)")
    return await asyncio.to_thread(normalize_image, raw)


def image_current(schedule: Schedule) -> bool:
    """Чи є вже готова спільна картинка саме для цього графіка."""
    return state.img_hash == schedule.hash and bool(state.img_file_id or state.img_bytes)


def api_available() -> bool:
    """Чи є сенс пробувати API зараз (є незаблокований ключ і немає паузи після збою)."""
    now = time.time()
    return now >= _gen_cooldown_until and any(k.blocked_until <= now for k in API_KEYS)


async def ensure_image(schedule: Schedule) -> bool:
    """Гарантує спільну картинку для цього графіка. True — є, False — шлемо текст.

    Якщо картинка вже є — API не чіпається (тут і ховається економія квоти).
    Нова знімається лише коли змінився графік, і одним запитом на всіх.
    """
    global _gen_cooldown_until
    if image_current(schedule):
        return True
    if not API_KEYS:
        log.warning("SCREENSHOT_API_KEYS не задано — текстовий режим")
        return False

    async with _gen_lock:
        if image_current(schedule):  # поки чекали на замок, хтось уже зробив
            return True
        if time.time() < _gen_cooldown_until:
            return False

        stamp = datetime.now(TZ)
        html_doc = render_html(schedule.days, stamp.strftime("%H:%M"), stamp.date())
        failed = False
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=130)) as session:
            for k in API_KEYS:
                if k.blocked_until > time.time():
                    continue
                try:
                    jpg = await render_via_key(session, k, html_doc)
                except KeyUnavailable as exc:
                    k.blocked_until = time.time() + exc.seconds
                    log.warning("screenshotbase, %s: %s — пауза %d хв, пробую наступний ключ",
                                k.label, exc.reason, exc.seconds // 60)
                except RenderFailed as exc:
                    log.error("screenshotbase відхилив запит: %s", exc)
                    failed = True
                    break
                except Exception as exc:
                    log.warning("screenshotbase, %s: %s: %s", k.label, type(exc).__name__, exc)
                    failed = True  # мережа/5xx — пробуємо наступний ключ
                else:
                    state.set_image(schedule.hash, stamp.isoformat(), jpg)
                    log.info("🖼 Нова спільна картинка (%s)", k.label)
                    return True

        if failed:
            _gen_cooldown_until = time.time() + 120
        else:
            log.warning("Усі API-ключі вичерпані/недоступні — текстовий режим")
        return False


def photo_input():
    """Що слати в Telegram: file_id (без завантаження) або байти."""
    if state.img_file_id:
        return state.img_file_id
    return BufferedInputFile(state.img_bytes, "schedule.jpg")


def remember_file_id(fid: str | None) -> None:
    if fid and fid != state.img_file_id:
        state.img_file_id = fid
        state.save()


async def answer_shared_photo(message: Message) -> Message:
    """Відповідає на /grafic готовою спільною картинкою — без жодного виклику API."""
    photo = photo_input()

    async def send(p):
        return await message.answer_photo(
            p, caption=render_caption(state.img_stamp), reply_markup=schedule_keyboard()
        )

    try:
        sent = await send(photo)
    except TelegramBadRequest:
        if not isinstance(photo, str):
            raise
        state.img_file_id = None  # file_id протух
        if state.img_bytes:
            sent = await send(BufferedInputFile(state.img_bytes, "schedule.jpg"))
        else:
            state.invalidate_image()  # байтів нема — наступного разу знімемо заново (1 раз)
            raise
    remember_file_id(_file_id(sent))
    return sent


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


def _file_id(result) -> str | None:
    if isinstance(result, Message) and result.photo:
        return result.photo[-1].file_id
    return None


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _guarded(call):
    """Виконує виклик Telegram: ретрай на flood-ліміт, перетворює «чат зник» на Gone.

    Повертає None, якщо повідомлення вже має такий самий вміст.
    """
    for attempt in range(2):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if attempt:
                raise
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramBadRequest as exc:
            msg = str(exc).lower()
            if "not modified" in msg:
                return None
            if any(x in msg for x in ("not found", "can't be edited", "chat not found")):
                raise Gone from exc
            raise
        except TelegramForbiddenError as exc:
            raise Gone from exc
    return None


async def _swap_message(bot: Bot, tracked: Tracked, send, *, photo: bool):
    """Шле НОВЕ повідомлення, старе видаляє (фото↔текст редагувати не можна)."""
    try:
        sent = await send()
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        raise Gone from exc
    with suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.delete_message(tracked.chat_id, tracked.message_id)
    tracked.message_id, tracked.photo = sent.message_id, photo
    state.save()
    return sent


async def safe_update(bot: Bot, tracked: Tracked, photo, caption: str, text: str) -> str | None:
    """Оновлює повідомлення: картинка (якщо є) або текст. Повертає file_id фото."""
    if photo is None:  # картинки немає → текст
        if tracked.photo:
            await _swap_message(
                bot,
                tracked,
                lambda: bot.send_message(
                    chat_id=tracked.chat_id,
                    message_thread_id=tracked.thread_id,
                    text=text,
                    reply_markup=schedule_keyboard(),
                    link_preview_options=NO_PREVIEW,
                ),
                photo=False,
            )
        else:
            await _guarded(
                lambda: bot.edit_message_text(
                    text=text,
                    chat_id=tracked.chat_id,
                    message_id=tracked.message_id,
                    reply_markup=schedule_keyboard(),
                    link_preview_options=NO_PREVIEW,
                )
            )
        return None

    if not tracked.photo:  # було текстове → міняємо на картинку
        sent = await _swap_message(
            bot,
            tracked,
            lambda: bot.send_photo(
                chat_id=tracked.chat_id,
                message_thread_id=tracked.thread_id,
                photo=photo,
                caption=caption,
                reply_markup=schedule_keyboard(),
            ),
            photo=True,
        )
        return _file_id(sent)

    res = await _guarded(
        lambda: bot.edit_message_media(
            media=InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
            chat_id=tracked.chat_id,
            message_id=tracked.message_id,
            reply_markup=schedule_keyboard(),
        )
    )
    return _file_id(res)


async def push_update(bot: Bot, schedule: Schedule, *, notify: bool) -> None:
    """Оновлює всі відстежувані повідомлення (+ за потреби шле сповіщення).

    Є картинка — міняємо на картинку, ні — на текст.
    """
    has_img = await ensure_image(schedule)
    caption = render_caption(state.img_stamp) if has_img else ""
    text = render_text(schedule, now_kyiv())
    photo = photo_input() if has_img else None
    notified: set[tuple[int, int | None]] = set()

    for tracked in list(state.messages):
        try:
            file_id = await safe_update(bot, tracked, photo, caption, text)
            if file_id:
                remember_file_id(file_id)
                photo = file_id  # для решти чатів (і для /grafic) — без повторного завантаження
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


_last_grafic: dict[int, float] = {}


@dp.message(Command("grafic"))
async def cmd_grafic(message: Message, bot: Bot) -> None:
    now = time.monotonic()
    if now - _last_grafic.get(message.chat.id, 0.0) < GRAFIC_COOLDOWN:
        return  # занадто часто з одного чату — ігноруємо
    _last_grafic[message.chat.id] = now
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

        # Є готова спільна картинка → віддаємо її (API не чіпаємо).
        # Знімати нову доведеться лише якщо графік змінився, і то один раз на всіх.
        is_photo = False
        sent = None
        if await ensure_image(schedule):
            try:
                sent = await answer_shared_photo(message)
                is_photo = True
            except Exception:
                log.exception("Не вдалося надіслати картинку — шлю текстом")
        if sent is None:
            sent = await message.answer(
                render_text(schedule, now_kyiv()),
                reply_markup=schedule_keyboard(),
                link_preview_options=NO_PREVIEW,
            )

        state.messages.append(Tracked(message.chat.id, sent.message_id, thread_id, is_photo))
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
                changed = schedule.hash != state.last_hash
                if not changed:
                    # Графік той самий, але раніше шли текстом (API було недоступне) —
                    # щойно ключ ожив (напр. новий місяць), тихо повертаємо картинку.
                    if image_current(schedule) or not api_available() or not await ensure_image(schedule):
                        log.info("✔ Змін немає")
                        continue
                    log.info("🖼 API знову працює — повертаю картинку")
                else:
                    log.info("🔄 Виявлено зміни у графіку")
                first_run = state.last_hash is None
                await push_update(bot, schedule, notify=changed and not first_run)
                # хеш зберігаємо ТІЛЬКИ після успіху — інакше при збої не буде повторної спроби
                state.last_hash = schedule.hash
                state.save()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Помилка автооновлення: %s", exc)


# ───────────────────────── HTTP для UptimeRobot / Koyeb ─────────────────────────
async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "tracked_chats": len(state.messages)})


async def view(request: web.Request) -> web.Response:
    """Сторінка з графіком — лише для налагодження дизайну в браузері (API отримує HTML напряму)."""
    try:
        schedule = await get_schedule()
    except Exception as exc:
        return web.Response(status=503, text=f"Графік недоступний: {exc}")
    stamp = request.query.get("t") or now_kyiv()
    return web.Response(text=render_html(schedule.days, stamp, datetime.now(TZ).date()), content_type="text/html")


async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)  # GET і HEAD (UptimeRobot використовує HEAD)
    app.router.add_get("/health", health)
    app.router.add_get("/view", view)
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
        log.info("✅ Сайт доступний, днів у графіку: %d", len(schedule.days))
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
