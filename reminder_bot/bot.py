from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import traceback
import tempfile
import time as time_module
import urllib.parse
import urllib.request
from urllib.error import HTTPError
import wave
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from reminder_bot.parser import ParsedReminder, apply_time, next_occurrence, parse_reminder, recurrence_label


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "reminders.sqlite3"
MODEL_DIR = ROOT / "models" / "vosk-model-small-ru-0.22"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
PENDING: dict[int, dict[str, Any]] = {}


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env or environment")

API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
DEFAULT_TZ = os.environ.get("DEFAULT_TIMEZONE", "Europe/Moscow")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            create table if not exists users (
                user_id integer primary key,
                tz text not null default 'Europe/Moscow',
                confirm_enabled integer not null default 1,
                default_lead_minutes integer not null default 0
            );
            create table if not exists reminders (
                id integer primary key autoincrement,
                user_id integer not null,
                title text not null,
                due_at text not null,
                recurrence text,
                lead_minutes integer not null default 0,
                status text not null default 'active',
                sent_lead_at text,
                sent_due_at text,
                created_at text not null
            );
            create table if not exists archive (
                id integer primary key autoincrement,
                reminder_id integer,
                user_id integer not null,
                title text not null,
                due_at text not null,
                status text not null,
                happened_at text not null
            );
            """
        )


def api(method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data or {}).encode()
    request = urllib.request.Request(f"{API}/{method}", data=encoded)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload["result"]


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    data: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    api("sendMessage", data)


def answer_callback(callback_id: str, text: str = "") -> None:
    api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    data: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    api("editMessageText", data)


def inline(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows]}


def menu() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "➕ Добавить"}, {"text": "📋 Все напоминания"}],
            [{"text": "🔁 Повторяющиеся"}, {"text": "🗄 Архив"}],
            [{"text": "⚙️ Настройки"}, {"text": "❓ Помощь"}],
        ],
        "resize_keyboard": True,
    }


def ensure_user(user_id: int) -> sqlite3.Row:
    with db() as conn:
        conn.execute("insert or ignore into users(user_id, tz) values(?, ?)", (user_id, DEFAULT_TZ))
        return conn.execute("select * from users where user_id = ?", (user_id,)).fetchone()


def now_for(user: sqlite3.Row) -> datetime:
    return datetime.now(ZoneInfo(user["tz"]))


def fmt_dt(value: str | datetime, tz_name: str) -> str:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    dt = dt.astimezone(ZoneInfo(tz_name))
    return dt.strftime("%d.%m.%Y %H:%M")


def reminder_text(parsed: ParsedReminder, user: sqlite3.Row) -> str:
    lead = parsed.lead_minutes if parsed.lead_minutes is not None else user["default_lead_minutes"]
    lead_text = "нет" if not lead else f"за {lead} мин."
    when = fmt_dt(parsed.due_at, user["tz"]) if parsed.due_at else "нужно уточнить время"
    return (
        f"Напомнить: {parsed.title}\n"
        f"Когда: {when}\n"
        f"Повтор: {recurrence_label(parsed.recurrence)}\n"
        f"Предупредить заранее: {lead_text}"
    )


def remember_pending(user_id: int, parsed: ParsedReminder) -> str:
    token = secrets.token_hex(4)
    PENDING[user_id] = {"parsed": parsed, "token": token}
    return token


def pending_matches(user_id: int, token: str | None) -> bool:
    return token is None or PENDING.get(user_id, {}).get("token") == token


def confirm_keyboard(token: str) -> dict[str, Any]:
    return inline(
        [[("Да", f"confirm|yes|{token}"), ("Изменить", f"confirm|edit|{token}"), ("Отмена", f"confirm|cancel|{token}")]]
    )


def time_keyboard(token: str) -> dict[str, Any]:
    return inline(
        [
            [("09:00", f"time|09:00|{token}"), ("11:00", f"time|11:00|{token}"), ("18:00", f"time|18:00|{token}")],
            [("Выбрать время", f"time|custom|{token}"), ("Отмена", f"confirm|cancel|{token}")],
        ]
    )


def lead_keyboard(reminder_id: int | None = None) -> dict[str, Any]:
    prefix = f"lead:{reminder_id}:" if reminder_id else "pendinglead:"
    return inline(
        [
            [("Без предупреждения", f"{prefix}0"), ("15 минут", f"{prefix}15")],
            [("30 минут", f"{prefix}30"), ("1 час", f"{prefix}60")],
            [("3 часа", f"{prefix}180"), ("7 часов", f"{prefix}420")],
        ]
    )


def save_reminder(user_id: int, parsed: ParsedReminder, user: sqlite3.Row) -> int:
    if not parsed.due_at:
        raise ValueError("У напоминания нет времени")
    lead = parsed.lead_minutes if parsed.lead_minutes is not None else user["default_lead_minutes"]
    with db() as conn:
        cur = conn.execute(
            """
            insert into reminders(user_id, title, due_at, recurrence, lead_minutes, created_at)
            values(?, ?, ?, ?, ?, ?)
            """,
            (user_id, parsed.title, parsed.due_at.isoformat(), parsed.recurrence, lead, now_for(user).isoformat()),
        )
        return int(cur.lastrowid)


def parse_and_reply(chat_id: int, user_id: int, text: str) -> None:
    user = ensure_user(user_id)
    try:
        parsed = parse_reminder(text, now_for(user), user["tz"])
    except ValueError as exc:
        send_message(chat_id, f"{exc}. Отправьте напоминание заново, например: завтра в 19:00 оплатить интернет.", menu())
        return
    token = remember_pending(user_id, parsed)

    if parsed.needs_time:
        send_message(chat_id, "Вы не указали время. Во сколько напомнить?", time_keyboard(token))
        return

    if user["confirm_enabled"]:
        send_message(chat_id, reminder_text(parsed, user), confirm_keyboard(token))
        return

    reminder_id = save_reminder(user_id, parsed, user)
    send_message(chat_id, f"Напоминание сохранено.\n\n{reminder_card(reminder_id, user_id, user)}")


def get_reminder(reminder_id: int, user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("select * from reminders where id = ? and user_id = ?", (reminder_id, user_id)).fetchone()


def reminder_card(reminder_id: int, user_id: int, user: sqlite3.Row) -> str:
    row = get_reminder(reminder_id, user_id)
    if not row:
        return "Напоминание не найдено."
    lead = "нет" if not row["lead_minutes"] else f"за {row['lead_minutes']} мин."
    return (
        f"{row['title']}\n"
        f"Когда: {fmt_dt(row['due_at'], user['tz'])}\n"
        f"Повтор: {recurrence_label(row['recurrence'])}\n"
        f"Предупредить заранее: {lead}"
    )


def reminder_actions(reminder_id: int) -> dict[str, Any]:
    return inline(
        [
            [("Изменить текст", f"edittext:{reminder_id}"), ("Изменить время", f"edittime:{reminder_id}")],
            [("Предупредить заранее", f"editlead:{reminder_id}")],
            [("Готово", f"done:{reminder_id}"), ("Удалить", f"delete:{reminder_id}")],
        ]
    )


def list_reminders(chat_id: int, user_id: int, repeating: bool = False) -> None:
    user = ensure_user(user_id)
    now = now_for(user)
    query = "select * from reminders where user_id = ? and status = 'active' and recurrence is {} null order by due_at".format(
        "not" if repeating else ""
    )
    with db() as conn:
        rows = conn.execute(query, (user_id,)).fetchall()
    if not rows:
        send_message(chat_id, "Активных напоминаний нет.", menu())
        return

    if repeating:
        text = "Повторяющиеся напоминания:"
        buttons = [[(f"{row['title'][:28]} · {fmt_dt(row['due_at'], user['tz'])}", f"open:{row['id']}")] for row in rows[:20]]
        send_message(chat_id, text, inline(buttons))
        return

    buckets = {"Сегодня": [], "Завтра": [], "Позже": []}
    for row in rows:
        due = datetime.fromisoformat(row["due_at"]).astimezone(ZoneInfo(user["tz"]))
        if due.date() == now.date():
            buckets["Сегодня"].append(row)
        elif due.date() == (now + timedelta(days=1)).date():
            buckets["Завтра"].append(row)
        else:
            buckets["Позже"].append(row)

    lines = ["Все напоминания:"]
    buttons: list[list[tuple[str, str]]] = []
    for name, items in buckets.items():
        if not items:
            continue
        lines.append(f"\n{name}:")
        for row in items[:10]:
            lines.append(f"• {fmt_dt(row['due_at'], user['tz'])} — {row['title']}")
            buttons.append([(f"{name}: {row['title'][:24]}", f"open:{row['id']}")])
    send_message(chat_id, "\n".join(lines), inline(buttons))


def list_archive(chat_id: int, user_id: int) -> None:
    user = ensure_user(user_id)
    with db() as conn:
        rows = conn.execute("select * from archive where user_id = ? order by happened_at desc limit 30", (user_id,)).fetchall()
    if not rows:
        send_message(chat_id, "Архив пуст.", menu())
        return
    lines = ["Архив:"]
    for row in rows:
        lines.append(f"• {fmt_dt(row['due_at'], user['tz'])} — {row['title']} ({row['status']})")
    send_message(chat_id, "\n".join(lines), menu())


def settings(chat_id: int, user_id: int) -> None:
    user = ensure_user(user_id)
    confirm = "вкл" if user["confirm_enabled"] else "выкл"
    lead = "нет" if not user["default_lead_minutes"] else f"за {user['default_lead_minutes']} мин."
    send_message(
        chat_id,
        f"Настройки:\nПодтверждать перед сохранением: {confirm}\nПредупреждать заранее по умолчанию: {lead}\nЧасовой пояс: {user['tz']}",
        inline(
            [
                [("Переключить подтверждение", "settings:confirm")],
                [("Предупреждение по умолчанию", "settings:lead")],
            ]
        ),
    )


def handle_message(message: dict[str, Any]) -> None:
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    ensure_user(user_id)

    if "voice" in message:
        try:
            text = transcribe_voice(message["voice"]["file_id"])
        except Exception:
            traceback.print_exc()
            text = None
        if not text:
            send_message(chat_id, "Не удалось распознать голос. Отправьте текстом.", menu())
            return
        parse_and_reply(chat_id, user_id, text)
        return

    text = message.get("text", "").strip()
    if not text:
        return

    state = PENDING.get(user_id)
    if state and state.get("await") == "custom_time":
        if "parsed" not in state:
            PENDING.pop(user_id, None)
            send_message(chat_id, "Это старый выбор времени. Отправьте напоминание заново.", menu())
            return
        normalized_time = parse_hhmm(text)
        if not normalized_time:
            send_message(chat_id, "Введите время в формате 19:00.")
            return
        try:
            parsed = apply_time(state["parsed"], normalized_time, now_for(ensure_user(user_id)), ensure_user(user_id)["tz"])
        except ValueError as exc:
            send_message(chat_id, f"{exc}. Введите время в формате 19:00.")
            return
        token = state["token"]
        PENDING[user_id] = {"parsed": parsed, "token": token}
        send_message(chat_id, reminder_text(parsed, ensure_user(user_id)), confirm_keyboard(token))
        return
    if state and state.get("await") == "edit_text":
        update_reminder(user_id, state["reminder_id"], title=text)
        PENDING.pop(user_id, None)
        send_message(chat_id, "Текст обновлен.", menu())
        return
    if state and state.get("await") == "edit_time":
        user = ensure_user(user_id)
        normalized_time = parse_hhmm(text)
        if not normalized_time:
            send_message(chat_id, "Введите время в формате 19:00.")
            return
        update_reminder_time(user_id, state["reminder_id"], normalized_time, user)
        PENDING.pop(user_id, None)
        send_message(chat_id, "Время обновлено.", menu())
        return

    if text == "/start":
        send_message(chat_id, "Бот-напоминалка готов. Отправьте текст или голос: «завтра в 19:00 оплатить интернет».", menu())
    elif text in {"/help", "❓ Помощь"}:
        send_message(chat_id, HELP, menu())
    elif text == "➕ Добавить":
        send_message(chat_id, "Отправьте напоминание текстом или голосом.")
    elif text == "📋 Все напоминания":
        list_reminders(chat_id, user_id)
    elif text == "🔁 Повторяющиеся":
        list_reminders(chat_id, user_id, repeating=True)
    elif text == "🗄 Архив":
        list_archive(chat_id, user_id)
    elif text == "⚙️ Настройки":
        settings(chat_id, user_id)
    else:
        parse_and_reply(chat_id, user_id, text)


def parse_hhmm(text: str) -> str | None:
    import re

    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def handle_callback(callback: dict[str, Any]) -> None:
    data = callback["data"]
    chat_id = callback["message"]["chat"]["id"]
    message_id = callback["message"]["message_id"]
    user_id = callback["from"]["id"]
    user = ensure_user(user_id)
    answer_callback(callback["id"])

    if data.startswith("confirm|"):
        _, action, token = data.split("|", 2)
        if not pending_matches(user_id, token):
            edit_message(chat_id, message_id, "Это старое подтверждение. Отправьте напоминание заново.")
            return
        if action == "cancel":
            PENDING.pop(user_id, None)
            edit_message(chat_id, message_id, "Отменено.")
        elif action == "edit":
            PENDING.pop(user_id, None)
            send_message(chat_id, "Отправьте напоминание заново одним сообщением.")
        elif action == "yes":
            parsed = PENDING.pop(user_id, {}).get("parsed")
            if not parsed or not parsed.due_at:
                send_message(chat_id, "Не нашел напоминание для сохранения.")
                return
            reminder_id = save_reminder(user_id, parsed, user)
            edit_message(chat_id, message_id, f"Напоминание сохранено.\n\n{reminder_card(reminder_id, user_id, user)}")
    elif data.startswith("time|"):
        _, value, token = data.split("|", 2)
        if not pending_matches(user_id, token):
            edit_message(chat_id, message_id, "Это старый выбор времени. Отправьте напоминание заново.")
            return
        if value == "custom":
            PENDING.setdefault(user_id, {})["await"] = "custom_time"
            send_message(chat_id, "Введите время в формате 19:00.")
            return
        parsed = PENDING.get(user_id, {}).get("parsed")
        if not parsed:
            return
        parsed = apply_time(parsed, value, now_for(user), user["tz"])
        PENDING[user_id] = {"parsed": parsed, "token": token}
        edit_message(chat_id, message_id, reminder_text(parsed, user), confirm_keyboard(token))
    elif data == "confirm:cancel":
        PENDING.pop(user_id, None)
        edit_message(chat_id, message_id, "Отменено.")
    elif data == "confirm:edit":
        send_message(chat_id, "Отправьте напоминание заново одним сообщением.")
    elif data == "confirm:yes":
        parsed = PENDING.pop(user_id, {}).get("parsed")
        if not parsed or not parsed.due_at:
            send_message(chat_id, "Не нашел напоминание для сохранения.")
            return
        reminder_id = save_reminder(user_id, parsed, user)
        edit_message(chat_id, message_id, f"Напоминание сохранено.\n\n{reminder_card(reminder_id, user_id, user)}")
    elif data.startswith("time:"):
        value = data.split(":", 1)[1]
        if value == "custom":
            PENDING.setdefault(user_id, {})["await"] = "custom_time"
            send_message(chat_id, "Введите время в формате 19:00.")
            return
        parsed = PENDING.get(user_id, {}).get("parsed")
        if not parsed:
            return
        parsed = apply_time(parsed, value, now_for(user), user["tz"])
        token = PENDING.get(user_id, {}).get("token") or secrets.token_hex(4)
        PENDING[user_id] = {"parsed": parsed, "token": token}
        edit_message(chat_id, message_id, reminder_text(parsed, user), confirm_keyboard(token))
    elif data.startswith("open:"):
        reminder_id = int(data.split(":", 1)[1])
        if not get_reminder(reminder_id, user_id):
            edit_message(chat_id, message_id, "Напоминание не найдено.")
            return
        edit_message(chat_id, message_id, reminder_card(reminder_id, user_id, user), reminder_actions(reminder_id))
    elif data.startswith("done:"):
        reminder_id = int(data.split(":", 1)[1])
        finish_reminder(user_id, reminder_id, "выполнено")
        edit_message(chat_id, message_id, "Готово.")
    elif data.startswith("delete:"):
        reminder_id = int(data.split(":", 1)[1])
        finish_reminder(user_id, reminder_id, "удалено")
        edit_message(chat_id, message_id, "Удалено.")
    elif data.startswith("snooze:"):
        _, reminder_id, minutes = data.split(":")
        snooze(user_id, int(reminder_id), int(minutes))
        edit_message(chat_id, message_id, "Отложено.")
    elif data.startswith("edittext:"):
        PENDING[user_id] = {"await": "edit_text", "reminder_id": int(data.split(":", 1)[1])}
        send_message(chat_id, "Отправьте новый текст напоминания.")
    elif data.startswith("edittime:"):
        PENDING[user_id] = {"await": "edit_time", "reminder_id": int(data.split(":", 1)[1])}
        send_message(chat_id, "Введите новое время в формате 19:00.")
    elif data.startswith("editlead:"):
        reminder_id = int(data.split(":", 1)[1])
        send_message(chat_id, "Когда предупредить заранее?", lead_keyboard(reminder_id))
    elif data.startswith("lead:"):
        _, reminder_id, minutes = data.split(":")
        update_reminder(user_id, int(reminder_id), lead_minutes=int(minutes))
        send_message(chat_id, "Предупреждение обновлено.", menu())
    elif data == "settings:confirm":
        with db() as conn:
            conn.execute("update users set confirm_enabled = 1 - confirm_enabled where user_id = ?", (user_id,))
        settings(chat_id, user_id)
    elif data == "settings:lead":
        send_message(chat_id, "Выберите предупреждение по умолчанию.", lead_keyboard(None))
    elif data.startswith("pendinglead:"):
        minutes = int(data.split(":", 1)[1])
        with db() as conn:
            conn.execute("update users set default_lead_minutes = ? where user_id = ?", (minutes, user_id))
        send_message(chat_id, "Настройка обновлена.", menu())


def update_reminder(user_id: int, reminder_id: int, reset_delivery: bool = True, **fields: Any) -> None:
    if not fields:
        return
    assignments = [f"{key} = ?" for key in fields]
    if reset_delivery:
        assignments.extend(["sent_lead_at = null", "sent_due_at = null"])
    with db() as conn:
        conn.execute(
            f"update reminders set {', '.join(assignments)} where id = ? and user_id = ?",
            (*fields.values(), reminder_id, user_id),
        )


def update_reminder_time(user_id: int, reminder_id: int, hhmm: str, user: sqlite3.Row) -> None:
    with db() as conn:
        row = conn.execute("select * from reminders where id = ? and user_id = ?", (reminder_id, user_id)).fetchone()
    if not row:
        return
    hour, minute = map(int, hhmm.split(":"))
    old = datetime.fromisoformat(row["due_at"]).astimezone(ZoneInfo(user["tz"]))
    due = old.replace(hour=hour, minute=minute, second=0, microsecond=0)
    now = now_for(user)
    if due <= now:
        if row["recurrence"]:
            while due <= now:
                due = next_occurrence(due, row["recurrence"], user["tz"])
        else:
            due += timedelta(days=1)
    update_reminder(user_id, reminder_id, due_at=due.isoformat())


def finish_reminder(user_id: int, reminder_id: int, status: str) -> None:
    with db() as conn:
        row = conn.execute("select * from reminders where id = ? and user_id = ?", (reminder_id, user_id)).fetchone()
        if not row:
            return
        if row["recurrence"] and status == "выполнено":
            return
        conn.execute(
            "insert into archive(reminder_id, user_id, title, due_at, status, happened_at) values(?, ?, ?, ?, ?, ?)",
            (reminder_id, user_id, row["title"], row["due_at"], status, datetime.now().isoformat()),
        )
        conn.execute("update reminders set status = ? where id = ?", (status, reminder_id))


def snooze(user_id: int, reminder_id: int, minutes: int) -> None:
    with db() as conn:
        row = conn.execute("select * from reminders where id = ? and user_id = ?", (reminder_id, user_id)).fetchone()
        if not row:
            return
        due = datetime.fromisoformat(row["due_at"]) + timedelta(minutes=minutes)
        conn.execute(
            "update reminders set due_at = ?, sent_lead_at = null, sent_due_at = null where id = ?",
            (due.isoformat(), reminder_id),
        )


def reminder_delivery_keyboard(reminder_id: int) -> dict[str, Any]:
    return inline(
        [
            [("Готово", f"done:{reminder_id}"), ("Отложить", f"snooze_menu:{reminder_id}"), ("Удалить", f"delete:{reminder_id}")],
            [("10 минут", f"snooze:{reminder_id}:10"), ("1 час", f"snooze:{reminder_id}:60"), ("Завтра", f"snooze:{reminder_id}:1440")],
        ]
    )


def scheduler_tick() -> None:
    with db() as conn:
        rows = conn.execute("select * from reminders where status = 'active'").fetchall()
    for row in rows:
        try:
            user = ensure_user(row["user_id"])
            now = now_for(user)
            due = datetime.fromisoformat(row["due_at"]).astimezone(ZoneInfo(user["tz"]))
            lead = int(row["lead_minutes"] or 0)
            if lead and not row["sent_lead_at"] and now >= due - timedelta(minutes=lead):
                send_message(row["user_id"], f"Через {lead} мин.: {row['title']}")
                update_reminder(row["user_id"], row["id"], reset_delivery=False, sent_lead_at=now.isoformat())
            if not row["sent_due_at"] and now >= due:
                send_message(row["user_id"], f"Напоминание: {row['title']}", reminder_delivery_keyboard(row["id"]))
                if row["recurrence"]:
                    archive_sent_and_advance(row, user)
                else:
                    update_reminder(row["user_id"], row["id"], reset_delivery=False, sent_due_at=now.isoformat())
        except Exception:
            traceback.print_exc()


def archive_sent_and_advance(row: sqlite3.Row, user: sqlite3.Row) -> None:
    due = datetime.fromisoformat(row["due_at"]).astimezone(ZoneInfo(user["tz"]))
    next_due = next_occurrence(due, row["recurrence"], user["tz"])
    with db() as conn:
        conn.execute(
            "insert into archive(reminder_id, user_id, title, due_at, status, happened_at) values(?, ?, ?, ?, ?, ?)",
            (row["id"], row["user_id"], row["title"], row["due_at"], "сработало", datetime.now().isoformat()),
        )
        conn.execute(
            "update reminders set due_at = ?, sent_lead_at = null, sent_due_at = null where id = ?",
            (next_due.isoformat(), row["id"]),
        )


def transcribe_voice(file_id: str) -> str | None:
    file_info = api("getFile", {"file_id": file_id})
    file_url = f"{FILE_API}/{file_info['file_path']}"
    suffix = Path(file_info["file_path"]).suffix or ".ogg"
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / f"voice{suffix}"
        wav = Path(temp_dir) / "voice.wav"
        with urllib.request.urlopen(file_url, timeout=60) as response:
            source.write_bytes(response.read())
        return vosk_transcribe(convert_to_wav(source, wav))


def convert_to_wav(source: Path, wav: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000", str(wav)],
        check=True,
    )
    return wav


def ensure_vosk_model() -> Path:
    if MODEL_DIR.exists():
        return MODEL_DIR

    models_root = MODEL_DIR.parent
    models_root.mkdir(exist_ok=True)
    archive = models_root / "vosk-model-small-ru-0.22.zip"
    print("Downloading free Vosk Russian model...")
    urllib.request.urlretrieve(MODEL_URL, archive)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(models_root)
    archive.unlink(missing_ok=True)
    return MODEL_DIR


def vosk_transcribe(wav: Path) -> str | None:
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError as exc:
        raise RuntimeError("Установите зависимость: pip install vosk") from exc

    SetLogLevel(-1)
    model = Model(str(ensure_vosk_model()))
    with wave.open(str(wav), "rb") as audio:
        recognizer = KaldiRecognizer(model, audio.getframerate())
        while True:
            data = audio.readframes(4000)
            if not data:
                break
            recognizer.AcceptWaveform(data)
        result = json.loads(recognizer.FinalResult()).get("text", "").strip()
        return result or None
    return None


HELP = """Примеры:
• завтра в 19:00 оплатить интернет
• через 30 минут позвонить
• каждый понедельник в 10:00 оплатить занятия
• каждое 15 число в 22:00 передать показания
• в субботу в 23:59 сдать домашнее задание за 1 час
• выйти из дома в 9:00
"""


def main() -> None:
    init_db()
    offset = 0
    last_tick = 0.0
    print("Reminder bot is running")
    while True:
        if time_module.time() - last_tick > 20:
            scheduler_tick()
            last_tick = time_module.time()
        try:
            updates = api(
                "getUpdates",
                {"offset": offset, "timeout": 20, "allowed_updates": json.dumps(["message", "callback_query"])},
            )
        except HTTPError as exc:
            if exc.code == 409:
                print("Telegram getUpdates conflict; waiting for the previous poll to finish.")
                time_module.sleep(5)
                continue
            raise
        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    if update["callback_query"]["data"].startswith("snooze_menu:"):
                        answer_callback(update["callback_query"]["id"], "Выберите срок ниже")
                    else:
                        handle_callback(update["callback_query"])
            except Exception as exc:
                target = update.get("message") or update.get("callback_query", {}).get("message")
                if target:
                    try:
                        send_message(target["chat"]["id"], f"Ошибка: {exc}")
                    except Exception:
                        traceback.print_exc()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc()
        raise
