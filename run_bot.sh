#!/bin/sh
cd "$(dirname "$0")" || exit 1
export PYTHONPATH=.
while true; do
  ./.venv/bin/python -u -m reminder_bot.bot
  code=$?
  echo "Bot exited with code $code; restarting in 5 seconds" >&2
  sleep 5
done
