import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import reminder_bot.bot as bot
from reminder_bot.parser import ParsedReminder


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = bot.DB_PATH
        self.old_pending = dict(bot.PENDING)
        bot.DB_PATH = Path(self.temp.name) / "test.sqlite3"
        bot.PENDING.clear()
        bot.init_db()
        self.sent = []
        self.edited = []
        self.send_patch = patch.object(bot, "send_message", side_effect=self.capture_send)
        self.edit_patch = patch.object(bot, "edit_message", side_effect=self.capture_edit)
        self.answer_patch = patch.object(bot, "answer_callback", lambda *args, **kwargs: None)
        self.send_patch.start()
        self.edit_patch.start()
        self.answer_patch.start()

    def tearDown(self):
        self.answer_patch.stop()
        self.edit_patch.stop()
        self.send_patch.stop()
        bot.PENDING.clear()
        bot.PENDING.update(self.old_pending)
        bot.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def capture_send(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    def capture_edit(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text, reply_markup))

    def callback(self, data, user_id=1):
        bot.handle_callback(
            {
                "id": "callback-id",
                "data": data,
                "from": {"id": user_id},
                "message": {"chat": {"id": user_id}, "message_id": 10},
            }
        )

    def test_missing_time_is_required(self):
        bot.parse_and_reply(1, 1, "завтра купить продукты")
        self.assertIn("Вы не указали время", self.sent[-1][1])
        self.assertIn("time|11:00", str(self.sent[-1][2]))

    def test_invalid_date_gets_human_error(self):
        bot.parse_and_reply(1, 1, "31.02 в 10:00 позвонить")
        self.assertIn("Некорректная дата", self.sent[-1][1])
        self.assertNotIn(1, bot.PENDING)

    def test_confirm_saves_reminder(self):
        bot.parse_and_reply(1, 1, "завтра в 19:00 оплатить интернет")
        self.callback(f"confirm|yes|{bot.PENDING[1]['token']}")

        with bot.db() as conn:
            rows = conn.execute("select title, status from reminders").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "оплатить интернет")
        self.assertEqual(rows[0]["status"], "active")
        self.assertIn("Напоминание сохранено", self.edited[-1][2])

    def test_time_button_then_confirm(self):
        bot.parse_and_reply(1, 1, "завтра купить продукты")
        token = bot.PENDING[1]["token"]
        self.callback(f"time|11:00|{token}")
        self.callback(f"confirm|yes|{token}")

        with bot.db() as conn:
            row = conn.execute("select title, due_at from reminders").fetchone()
        self.assertEqual(row["title"], "купить продукты")
        self.assertTrue(row["due_at"].endswith("11:00:00+03:00"))

    def test_custom_bad_time_does_not_crash(self):
        bot.parse_and_reply(1, 1, "завтра купить продукты")
        self.callback(f"time|custom|{bot.PENDING[1]['token']}")
        bot.handle_message({"chat": {"id": 1}, "from": {"id": 1}, "text": "25:99"})
        self.assertIn("Введите время", self.sent[-1][1])

    def test_old_confirm_button_cannot_save_new_pending(self):
        bot.parse_and_reply(1, 1, "завтра в 10:00 первое")
        old_token = bot.PENDING[1]["token"]
        bot.parse_and_reply(1, 1, "завтра в 11:00 второе")
        new_token = bot.PENDING[1]["token"]

        self.callback(f"confirm|yes|{old_token}")
        with bot.db() as conn:
            self.assertEqual(conn.execute("select count(*) from reminders").fetchone()[0], 0)
        self.assertIn("старое подтверждение", self.edited[-1][2])

        self.callback(f"confirm|yes|{new_token}")
        with bot.db() as conn:
            row = conn.execute("select title from reminders").fetchone()
        self.assertEqual(row["title"], "второе")

    def test_settings_toggle_confirmation(self):
        self.callback("settings:confirm")
        user = bot.ensure_user(1)
        self.assertEqual(user["confirm_enabled"], 0)

    def test_scheduler_sends_lead_and_due(self):
        user = bot.ensure_user(1)
        now = bot.now_for(user)
        parsed = ParsedReminder("выйти из дома", now + timedelta(minutes=10), None, 15, False, 0.9, "")
        reminder_id = bot.save_reminder(1, parsed, user)

        bot.scheduler_tick()
        self.assertIn("Через 15 мин.", self.sent[-1][1])

        with bot.db() as conn:
            conn.execute("update reminders set due_at = ? where id = ?", ((now - timedelta(minutes=1)).isoformat(), reminder_id))

        bot.scheduler_tick()
        self.assertIn("Напоминание: выйти из дома", self.sent[-1][1])

    def test_scheduler_keeps_running_after_send_error(self):
        user = bot.ensure_user(1)
        due = bot.now_for(user) - timedelta(minutes=1)
        first_id = bot.save_reminder(1, ParsedReminder("сломанная отправка", due, None, None, False, 0.9, ""), user)
        second_id = bot.save_reminder(2, ParsedReminder("рабочая отправка", due, None, None, False, 0.9, ""), bot.ensure_user(2))

        def flaky_send(chat_id, text, reply_markup=None):
            if chat_id == 1:
                raise RuntimeError("blocked")
            self.capture_send(chat_id, text, reply_markup)

        with patch.object(bot, "send_message", side_effect=flaky_send), patch.object(bot.traceback, "print_exc", lambda: None):
            bot.scheduler_tick()

        with bot.db() as conn:
            first = conn.execute("select sent_due_at from reminders where id = ?", (first_id,)).fetchone()
            second = conn.execute("select sent_due_at from reminders where id = ?", (second_id,)).fetchone()
        self.assertIsNone(first["sent_due_at"])
        self.assertIsNotNone(second["sent_due_at"])
        self.assertIn("рабочая отправка", self.sent[-1][1])

    def test_done_archives_one_time_reminder(self):
        user = bot.ensure_user(1)
        parsed = ParsedReminder("позвонить", bot.now_for(user) + timedelta(hours=1), None, None, False, 0.9, "")
        reminder_id = bot.save_reminder(1, parsed, user)

        self.callback(f"done:{reminder_id}")

        with bot.db() as conn:
            reminder = conn.execute("select status from reminders where id = ?", (reminder_id,)).fetchone()
            archive = conn.execute("select status from archive where reminder_id = ?", (reminder_id,)).fetchone()
        self.assertEqual(reminder["status"], "выполнено")
        self.assertEqual(archive["status"], "выполнено")

    def test_done_does_not_stop_recurring_reminder(self):
        user = bot.ensure_user(1)
        parsed = ParsedReminder("оплатить занятия", bot.now_for(user) - timedelta(minutes=1), "weekly:0", None, False, 0.9, "")
        reminder_id = bot.save_reminder(1, parsed, user)

        bot.scheduler_tick()
        self.callback(f"done:{reminder_id}")

        with bot.db() as conn:
            row = conn.execute("select status, recurrence, due_at from reminders where id = ?", (reminder_id,)).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["recurrence"], "weekly:0")
        self.assertGreater(datetime.fromisoformat(row["due_at"]), bot.now_for(user))

    def test_edit_recurring_time_advances_to_future(self):
        user = bot.ensure_user(1)
        past = bot.now_for(user) - timedelta(days=1)
        reminder_id = bot.save_reminder(1, ParsedReminder("ежедневно", past, "daily:", None, False, 0.9, ""), user)

        bot.update_reminder_time(1, reminder_id, "00:01", user)

        with bot.db() as conn:
            row = conn.execute("select due_at from reminders where id = ?", (reminder_id,)).fetchone()
        self.assertGreater(datetime.fromisoformat(row["due_at"]), bot.now_for(user))

    def test_other_user_cannot_open_reminder_card(self):
        user = bot.ensure_user(1)
        reminder_id = bot.save_reminder(1, ParsedReminder("секрет", bot.now_for(user) + timedelta(hours=1), None, None, False, 0.9, ""), user)

        self.callback(f"open:{reminder_id}", user_id=2)

        self.assertEqual(self.edited[-1][2], "Напоминание не найдено.")


class TelegramAPIWrapperTest(unittest.TestCase):
    def telegram_error(self, method, description, code=400):
        return bot.TelegramAPIError(method, code, description, {"ok": False, "description": description})

    def test_edit_message_ignores_unchanged_message(self):
        with patch.object(
            bot,
            "api",
            side_effect=self.telegram_error("editMessageText", "Bad Request: message is not modified"),
        ) as api:
            bot.edit_message(1, 10, "Текст")

        api.assert_called_once()

    def test_edit_message_falls_back_to_send_message_when_edit_is_gone(self):
        calls = []

        def fake_api(method, data=None):
            calls.append((method, data))
            if method == "editMessageText":
                raise self.telegram_error("editMessageText", "Bad Request: message to edit not found")
            return {"message_id": 11}

        with patch.object(bot, "api", side_effect=fake_api):
            bot.edit_message(1, 10, "Новый текст")

        self.assertEqual(calls[0][0], "editMessageText")
        self.assertEqual(calls[1][0], "sendMessage")
        self.assertEqual(calls[1][1]["text"], "Новый текст")

    def test_answer_callback_ignores_stale_query(self):
        with patch.object(
            bot,
            "api",
            side_effect=self.telegram_error("answerCallbackQuery", "Bad Request: query is too old and response timeout expired"),
        ) as api:
            bot.answer_callback("old-callback")

        api.assert_called_once()

    def test_send_message_ignores_blocked_chat(self):
        with patch.object(
            bot,
            "api",
            side_effect=self.telegram_error("sendMessage", "Forbidden: bot was blocked by the user", 403),
        ) as api:
            bot.send_message(1, "Текст")

        api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
