# Telegram Reminder Bot

Бот-напоминалка на Python с бесплатным локальным распознаванием голоса через faster-whisper и Vosk fallback.

## Возможности

- текстовые напоминания;
- голосовые напоминания через бесплатный локальный faster-whisper, с Vosk как запасным распознавателем;
- обязательное подтверждение `Да / Изменить / Отмена`;
- уточнение времени, если пользователь его не указал;
- одноразовые и повторяющиеся напоминания;
- раннее предупреждение за 15 минут, 30 минут, 1 час, 3 часа или 7 часов;
- меню: все напоминания, повторяющиеся, архив, настройки, помощь;
- SQLite база в `reminders.sqlite3`.

## Запуск

Для установки на Mac Mini смотрите [INSTALL_MAC.md](INSTALL_MAC.md).

```bash
cd work/telegram-reminder-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m reminder_bot.bot
```

Запуск в фоне через `screen`:

```bash
cd work/telegram-reminder-bot
screen -dmS telegram-reminder-bot /bin/sh -lc 'PYTHONPATH=. ./.venv/bin/python -u -m reminder_bot.bot >> bot.log 2>&1'
```

Остановить фонового бота:

```bash
screen -S telegram-reminder-bot -X quit
```

`.env` должен содержать:

```env
TELEGRAM_BOT_TOKEN=...
DEFAULT_TIMEZONE=Europe/Moscow
```

При первом голосовом сообщении бот сам скачает бесплатные локальные модели в `models/`.

## Проверка парсера

```bash
cd work/telegram-reminder-bot
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```
