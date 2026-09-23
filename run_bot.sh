#!/bin/sh
cd /Users/ilyaosipov/Documents/Codex/2026-09-23/19-00-19-00-30-19/work/telegram-reminder-bot || exit 1
export PYTHONPATH=.
exec ./.venv/bin/python -u -m reminder_bot.bot
