import asyncio
import requests
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
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
    html = requests.get(URL, timeout=30).text
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

        schedule_parts.append(f"📅 <b>{date}</b>")

        # Формуємо всі черги
        all_queues = []
        for i in range(0, len(times), 2):
            t1 = times[i] if times[i] else "—"
            t2 = times[i+1] if i+1 < len(times) and times[i+1] else "—"
            queue_number = (i // 2) + 1
            
            if t1 != "—":
                all_queues.append(f"⚡ Черга {queue_number}.1: {t1}")
            if t2 != "—":
                all_queues.append(f"⚡ Черга {queue_number}.2: {t2}")

        # Додаємо порожні рядки між чергами
        queue_text = ""
        for i, queue_line in enumerate(all_queues):
            queue_text += queue_line
            if i < len(all_queues) - 1:  # Якщо не остання черга
                queue_text += "\n\n"

        schedule_parts.append(f"<pre>{queue_text}</pre>")

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
        now = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%H:%M")
        text = f"🔄 <b>Зафіксовано зміну у графіку!</b>\n🕒 {now}"
        
        # Створюємо клавіатуру з кнопкою "?"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❓",
                        callback_data="update_explanation"
                    )
                ]
            ]
        )
        
        send_kwargs = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard  # Додаємо клавіатуру
        }
        
        if thread_id:
            send_kwargs["message_thread_id"] = thread_id
            
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
        await asyncio.sleep(delay_seconds)
        
        # Спробуємо видалити кількома способами
        try:
            # Спосіб 1: Звичайне видалення
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑️ Сповіщення видалено")
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # Спосіб 2: Якщо це тред, спробуємо без message_thread_id
            if "thread" in error_msg:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🗑️ Сповіщення в треді видалено")
                except:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Не вдалося видалити повідомлення в треді")
                    
            elif "message to delete not found" in error_msg:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ℹ️ Повідомлення вже видалено")
                
            elif "message can't be deleted" in error_msg:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Немає прав на видалення повідомлення")
                
            else:
                print(f"[Помилка видалення] {e}")
                
    except Exception as e:
        print(f"[Загальна помилка в delete_after_delay] {e}")
            
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

@dp.callback_query(F.data == "update_explanation")
async def explain_update(callback_query: CallbackQuery):
    """Показує пояснення щодо сповіщення про оновлення"""
    explanation_text = (
        "ℹ️ Графік відключень змінився\n\n"
        "Можливі зміни:\n"
        "• Зміни в днях\n"
        "• Зміни в годинах"
    )
    
    # Відправляємо alert з поясненням
    await callback_query.answer(
        text=explanation_text,
        show_alert=True,
        cache_time=60
    )
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
        "• Графік оновлюється кожні 10 хвилин\n"
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


