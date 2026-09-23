#!/bin/sh
cd /Users/ilyaosipov/Documents/Codex/2026-09-23/19-00-19-00-30-19/work/telegram-reminder-bot || exit 1
export PYTHONPATH=.
while true; do
  ./.venv/bin/python -u -m reminder_bot.bot
  code=$?
  echo "Bot exited with code $code; restarting in 5 seconds" >&2
  sleep 5
done
