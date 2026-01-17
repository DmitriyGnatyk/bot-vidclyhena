import asyncio
import requests
from zoneinfo import ZoneInfo
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
#   ФУНКЦІЯ ДЛЯ ВІДПРАВКИ ПОВІДОМЛЕНЬ ПРО ОНОВЛЕННЯ
# ---------------------------------------
async def send_update_notification(chat_id, thread_id=None, reply_to_message_id=None):
    """Відправляє повідомлення про оновлення графіка як відповідь на повідомлення з графіком"""
    try:
        text = (
            "🔄 <b>Графік оновлено!</b>\n\n"
            "• Можливі зміни в годинах відключень\n"
            "• Або оновився список з новим днем\n"
            "• Перевірте актуальний графік вище\n\n"
            "<i>Оновлення відбувається автоматично кожні 10 хвилин</i>"
        )
        
        send_kwargs = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        if thread_id:
            send_kwargs["message_thread_id"] = thread_id
            
        # Додаємо відповідь на повідомлення з графіком, якщо вказано
        if reply_to_message_id:
            send_kwargs["reply_to_message_id"] = reply_to_message_id

        # Відправляємо повідомлення і отримуємо його ID
        sent_message = await bot.send_message(**send_kwargs)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📢 Надіслано сповіщення про оновлення")
        
        # Запускаємо завдання на видалення через 1 годину
        asyncio.create_task(delete_after_delay(
            chat_id=chat_id,
            message_id=sent_message.message_id,
            thread_id=thread_id,
            delay_seconds=3600  # 1 година = 3600 секунд
        ))
        
    except Exception as e:
        print(f"[Помилка відправки сповіщення] {e}")

async def delete_after_delay(chat_id, message_id, thread_id=None, delay_seconds=3600):
    """Видаляє повідомлення через вказану затримку"""
    try:
        # Чекаємо вказану кількість секунд
        await asyncio.sleep(delay_seconds)
        
        # Видаляємо повідомлення
        delete_kwargs = {
            "chat_id": chat_id,
            "message_id": message_id
        }
        
        if thread_id:
            delete_kwargs["message_thread_id"] = thread_id
            
        await bot.delete_message(**delete_kwargs)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑️ Сповіщення про оновлення видалено")
        
    except Exception as e:
        # Якщо повідомлення вже видалено або немає прав - ігноруємо помилку
        if "message to delete not found" not in str(e).lower() and "message can't be deleted" not in str(e).lower():
            print(f"[Помилка видалення сповіщення] {e}")
            
# ---------------------------------------
#   КОМАНДА /grafic
# ---------------------------------------
@dp.message(F.text == "/grafic")
async def cmd_grafic(message: Message):
    sched = fetch_schedule()
    now = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
    text = f"⚡️ Графік відключень:\n{sched}\n\n🕒 Оновлено о {now}"

    for msg in state["messages"]:
        if msg["chat_id"] == message.chat.id and msg.get("thread_id") == message.message_thread_id:
            try:
                await bot.edit_message_text(
                    chat_id=msg["chat_id"],
                    message_id=msg["message_id"],
                    text=text,
                    parse_mode="HTML",
                    reply_markup=get_queue_button()
                )
                return
            except:
                pass

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
#   АВТО-ОНОВЛЕННЯ КОЖНІ 5 ХВ
# ---------------------------------------
async def auto_update():
    while True:
        if not state["messages"]:
            await asyncio.sleep(60)
            continue

        try:
            new_data = fetch_schedule()
            now = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
            text = f"⚡️ Графік відключень:\n{new_data}\n\n🕒 Оновлено о {now}"

            for msg in state["messages"]:
                try:
                    await bot.edit_message_text(
                        chat_id=msg["chat_id"],
                        message_id=msg["message_id"],
                        text=text,
                        parse_mode="HTML",
                        reply_markup=get_queue_button()
                    )
                except Exception as e:
                    print(f"[Помилка оновлення повідомлення] {e}")

            if new_data != state["last_data"]:
                # Відправляємо сповіщення про оновлення як відповідь на повідомлення з графіком
                sent_chats = set()
                
                for msg in state["messages"]:
                    chat_key = (msg["chat_id"], msg.get("thread_id"))
                    
                    if chat_key not in sent_chats:
                        # Відправляємо відповідь на конкретне повідомлення з графіком
                        await send_update_notification(
                            chat_id=msg["chat_id"],
                            thread_id=msg.get("thread_id"),
                            reply_to_message_id=msg["message_id"]  # ← Ось це робить відповідь
                        )
                        sent_chats.add(chat_key)
                
                state["last_data"] = new_data
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Графік оновлено - виявлені зміни")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✔ Графік без змін - оновлено лише час")

        except Exception as e:
            print(f"[Помилка автооновлення] {e}")

        await asyncio.sleep(600)
        
# ---------------------------------------
#   КОМАНДА /start
# ---------------------------------------
@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    welcome_text = (
        "👋 <b>Ласкаво просимо до бота графіків відключень!</b>\n\n"
        "⚡ <b>Доступні команди:</b>\n"
        "/grafic - Показати актуальний графік відключень\n"
        "/start - Це повідомлення\n\n"
        "📊 <b>Автоматичні оновлення:</b>\n"
        "• Графік оновлюється кожні 5 хвилин\n"
        "• При змінах - надсилається сповіщення\n"
        "• Завжди актуальна інформація\n\n"
        "🔔 <b>Бот працює в групах!</b>\n"
        "Додайте мене до групи та використовуйте /grafic"
    )
    
    await message.answer(welcome_text, parse_mode="HTML")

# ---------------------------------------
#   ЗАПУСК
# ---------------------------------------
async def main():
    asyncio.create_task(auto_update())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())


