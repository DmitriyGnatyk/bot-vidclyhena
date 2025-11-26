import asyncio
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from datetime import datetime
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import WebAppInfo

TOKEN = "8252620965:AAE2U_zv4arez-9JMMAbp4ywJbZCFpkGm6s"
URL = "https://www.roe.vsei.ua/disconnections"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------------------------------------
#   СТЕЙТ: ЗБЕРІГАЄМО ВСІ ПОВІДОМЛЕННЯ ДЛЯ ОНОВЛЕННЯ
# ---------------------------------------
state = {
    "messages": [],  # список словників: {"chat_id", "message_id", "thread_id"}
    "last_data": None
}

# ---------------------------------------
#   ПАРСИНГ ГРАФІКУ В НОВОМУ ФОРМАТІ
# ---------------------------------------
def fetch_schedule():
    html = requests.get(URL, timeout=10).text
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("div", {"id": "fetched-data-container"}).find("table")
    rows = table.find_all("tr")

    schedule_parts = []

    for row in rows[4:]:
        cols = row.find_all("td")
        if not cols:
            continue

        date = cols[0].get_text(strip=True)
        times = [c.get_text(" ", strip=True) for c in cols[1:]]

        # Додаємо дату як звичайний текст
        schedule_parts.append(f"📅 <b>{date}</b>")

        # Черги для цієї дати в <pre>
        queue_lines = []
        for i in range(0, len(times), 2):
            t1 = times[i] if times[i] else "—"
            t2 = times[i+1] if i+1 < len(times) and times[i+1] else "—"
            queue_number = (i // 2) + 1
            queue_lines.append(f"⚡ Черга {queue_number}.1: {t1}")
            queue_lines.append(f"⚡ Черга {queue_number}.2: {t2}")

        schedule_parts.append("<pre>" + "\n".join(queue_lines) + "</pre>")

    return "\n".join(schedule_parts)
    
def get_queue_button():
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Дізнатись свою чергу ⚡️",
                    url="https://t.me/energo_infobot/webapp")
            ]
        ]
    )
    return keyboard

# ---------------------------------------
#   КОМАНДА /grafic
# ---------------------------------------
@dp.message(F.text == "/grafic")
async def cmd_grafic(message: Message):
    sched = fetch_schedule()
    now = datetime.now().strftime("%H:%M")
    text = f"⚡️ Графік відключень:\n{sched}\n\n🕒 Оновлено о {now}"

    # Перевіряємо, чи повідомлення вже є у списку
    for msg in state["messages"]:
        if msg["chat_id"] == message.chat.id and msg.get("thread_id") == message.message_thread_id:
            try:
                edit_kwargs = {
                    "chat_id": msg["chat_id"],
                    "message_id": msg["message_id"],
                    "text": text,
                    "parse_mode": "HTML"
                }
                if msg.get("thread_id"):
                    edit_kwargs["message_thread_id"] = msg["thread_id"]
                await bot.edit_message_text(**edit_kwargs)
                return
            except:
                pass  # раптом видалене — створимо нове

    # Відправляємо нове повідомлення
    send_kwargs = {
        "chat_id": message.chat.id,
        "text": text,
        "parse_mode": "HTML"
    }
    if message.message_thread_id:
        send_kwargs["message_thread_id"] = message.message_thread_id

    keyboard = get_queue_button()
    send_kwargs["reply_markup"] = keyboard
    sent = await bot.send_message(**send_kwargs)

    # Додаємо його у список для автооновлення
    state["messages"].append({
        "chat_id": message.chat.id,
        "message_id": sent.message_id,
        "thread_id": message.message_thread_id
    })
    state["last_data"] = sched

# ---------------------------------------
#   АВТО-ОНОВЛЕННЯ КОЖНУ 1 ХВ
# ---------------------------------------
async def auto_update():
    while True:
        if not state["messages"]:
            await asyncio.sleep(60)
            continue

        try:
            new_data = fetch_schedule()
            now = datetime.now().strftime("%H:%M")
            text = f"⚡️ Графік відключень:\n{new_data}\n\n🕒 Оновлено о {now}"

            for msg in state["messages"]:
                edit_kwargs = {
                    "chat_id": msg["chat_id"],
                    "message_id": msg["message_id"],
                    "text": text,
                    "parse_mode": "HTML"
                }
                if msg.get("thread_id"):
                    edit_kwargs["message_thread_id"] = msg["thread_id"]
                try:
                    await bot.edit_message_text(**edit_kwargs)
                except Exception as e:
                    print(f"[Помилка оновлення повідомлення] {e}")

            if new_data != state["last_data"]:
                state["last_data"] = new_data
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Графік оновлено")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✔ Без змін, тільки час оновлено")

        except Exception as e:
            print(f"[Помилка автооновлення] {e}")

        await asyncio.sleep(60)

# ---------------------------------------
#   ЗАПУСК
# ---------------------------------------
async def main():
    asyncio.create_task(auto_update())
    await dp.start_polling(bot)

asyncio.run(main())



