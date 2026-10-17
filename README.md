# ⚡ Бот графіків відключень (РОЕ)

Telegram-бот, що стежить за графіком на roe.vsei.ua, оновлює повідомлення й сповіщає про зміни.

## Запуск
```bash
pip install -r requirements.txt
export BOT_TOKEN="..."
python main.py
```
Змінні середовища — у `.env.example`. Деплой як і раніше через `Procfile` (worker).

## Кольорові кнопки
Використовується поле `style` (`primary` / `success` / `danger`) з Bot API 9.4 — потрібен `aiogram>=3.31`.
На старих версіях Telegram-клієнтів кнопки просто будуть звичайними.

> Файл `state.json` на Heroku/Railway без volume зберігається лише до перезапуску контейнера.

## Koyeb + UptimeRobot
Бот піднімає HTTP-сервер на `$PORT` (Koyeb задає його сам, за замовчуванням 8000):
- `GET/HEAD /` та `/health` повертають `200 {"status":"ok"}`.
- Service type на Koyeb — **Web service**, порт **8000**, health check — HTTP `/health`.
- В UptimeRobot додай монітор типу HTTP(s) на `https://<твій-app>.koyeb.app/health` з інтервалом 5 хв.
