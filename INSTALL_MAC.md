# Установка на Mac Mini

Инструкция для чистой установки бота на Mac Mini.

## 1. Подготовить Mac

Откройте Terminal и выполните:

```bash
xcode-select --install
```

Если Homebrew не установлен, поставьте его с https://brew.sh.

Потом:

```bash
brew install git python ffmpeg screen
```

Важно: Mac Mini не должен засыпать, иначе бот тоже “заснёт”.

```bash
sudo pmset -a sleep 0
```

## 2. Скачать проект

Лучше держать бота в домашней папке:

```bash
cd ~
git clone https://github.com/steamxheat/napominalka.git
cd napominalka
```

## 3. Поставить зависимости

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

## 4. Создать `.env`

```bash
cp .env.example .env
nano .env
```

Внутри должно быть примерно так:

```env
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН_ОТ_BOTFATHER
DEFAULT_TIMEZONE=Europe/Moscow
WHISPER_MODEL_SIZE=small
TELEGRAM_POLL_TIMEOUT=0
```

Сохранить в nano: `Ctrl+O`, Enter, `Ctrl+X`.

## 5. Проверить

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
PYTHONPATH=. .venv/bin/python -u -m reminder_bot.bot
```

Если увидели:

```text
Reminder bot is running
```

значит бот запустился. Остановить проверочный запуск: `Ctrl+C`.

## 6. Запустить в фоне

```bash
chmod +x run_bot.sh
screen -dmS telegram-reminder-bot ./run_bot.sh
```

Проверить:

```bash
screen -ls
tail -f bot.log
```

Остановить:

```bash
screen -S telegram-reminder-bot -X quit
```

## 7. Автозапуск после перезагрузки

Создайте LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents
nano ~/Library/LaunchAgents/com.steamxheat.napominalka.plist
```

Вставьте. Если проект лежит не в `~/napominalka`, замените путь.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.steamxheat.napominalka</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/Users/ВАШ_ПОЛЬЗОВАТЕЛЬ/napominalka/run_bot.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/ВАШ_ПОЛЬЗОВАТЕЛЬ/napominalka</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/ВАШ_ПОЛЬЗОВАТЕЛЬ/napominalka/bot.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/ВАШ_ПОЛЬЗОВАТЕЛЬ/napominalka/bot.err.log</string>
</dict>
</plist>
```

Загрузить:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.steamxheat.napominalka.plist
launchctl kickstart -k gui/$(id -u)/com.steamxheat.napominalka
```

Проверить:

```bash
launchctl print gui/$(id -u)/com.steamxheat.napominalka | head -80
tail -f ~/napominalka/bot.log
```

Остановить автозапуск:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.steamxheat.napominalka.plist
```

## 8. Если голосовые не распознаются

Смотрите лог:

```bash
tail -200 ~/napominalka/bot.log
```

Нормально, если при первом голосовом бот скачивает модели в `models/`. Это может занять несколько минут.

В логе должны появиться строки:

```text
Voice message received
Voice downloaded
Voice converted to wav
Whisper segment
Voice transcription
```

Если их нет, значит запущен не этот бот или бот не получает updates.

Если есть `Telegram getUpdates conflict`, остановите лишние копии:

```bash
screen -S telegram-reminder-bot -X quit
pkill -f reminder_bot.bot
```

Потом запустите заново:

```bash
cd ~/napominalka
screen -dmS telegram-reminder-bot ./run_bot.sh
```
