import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.parser import apply_time, parse_reminder


class ParserTest(unittest.TestCase):
    def test_tomorrow_with_time(self):
        now = datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("завтра в 19:00 оплатить интернет", now, "Europe/Moscow")
        self.assertEqual(parsed.title, "оплатить интернет")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-09-24 19:00")
        self.assertFalse(parsed.needs_time)

    def test_relative_minutes(self):
        now = datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("через 30 минут позвонить", now, "Europe/Moscow")
        self.assertEqual(parsed.due_at.strftime("%H:%M"), "12:30")
        self.assertEqual(parsed.title, "позвонить")

    def test_zero_relative_minutes_stays_immediate(self):
        now = datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("через 0 минут позвонить", now, "Europe/Moscow")
        self.assertEqual(parsed.due_at, now)

    def test_recurring_monthly(self):
        now = datetime(2026, 9, 23, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("каждое 15 число в 22:00 передать показания", now, "Europe/Moscow")
        self.assertEqual(parsed.recurrence, "monthly_day:15")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-10-15 22:00")

    def test_leaving_gets_default_lead(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("выйти из дома в 9:00", now, "Europe/Moscow")
        self.assertEqual(parsed.lead_minutes, 15)
        self.assertEqual(parsed.title, "выйти из дома")

    def test_invalid_time_is_rejected(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        with self.assertRaises(ValueError):
            parse_reminder("сегодня в 25:99 позвонить", now, "Europe/Moscow")

    def test_invalid_date_is_rejected(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        with self.assertRaises(ValueError):
            parse_reminder("31.02 в 10:00 позвонить", now, "Europe/Moscow")

    def test_february_29_moves_to_next_leap_year(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("29 февраля в 10:00 поздравить", now, "Europe/Moscow")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2028-02-29 10:00")

    def test_invalid_monthly_day_is_rejected(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        with self.assertRaises(ValueError):
            parse_reminder("каждое 99 число в 10:00 позвонить", now, "Europe/Moscow")

    def test_every_month_phrase(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("каждый месяц 1 числа в 10:00 оплатить подписку", now, "Europe/Moscow")
        self.assertEqual(parsed.recurrence, "monthly_day:1")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-10-01 10:00")

    def test_every_month_phrase_cleans_title(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("каждый месяц 31 числа в 10:00 оплатить подписку", now, "Europe/Moscow")
        self.assertEqual(parsed.title, "оплатить подписку")

    def test_lead_hours_plural(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("в субботу в 23:59 сдать дз за 7 часов", now, "Europe/Moscow")
        self.assertEqual(parsed.lead_minutes, 420)
        self.assertEqual(parsed.title, "сдать дз")

    def test_dash_time_and_filler_words(self):
        now = datetime(2026, 9, 23, 19, 26, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("напомни мне сегодня в 19-30 что нужно поккакать", now, "Europe/Moscow")
        self.assertEqual(parsed.title, "поккакать")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-09-23 19:30")

    def test_spaced_time_and_filler_words(self):
        now = datetime(2026, 9, 23, 16, 26, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("напомни мне в 17 00 сегодня я должен посрать", now, "Europe/Moscow")
        self.assertEqual(parsed.title, "посрать")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-09-23 17:00")

    def test_explicit_today_past_time_is_rejected(self):
        now = datetime(2026, 9, 23, 19, 26, tzinfo=ZoneInfo("Europe/Moscow"))
        with self.assertRaises(ValueError):
            parse_reminder("напомни мне сегодня в 17:00 посрать", now, "Europe/Moscow")

    def test_whisper_common_transcription_variant(self):
        now = datetime(2026, 9, 23, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("Напомним мне сегодня в 19-30 купить хлеб.", now, "Europe/Moscow")
        self.assertEqual(parsed.title, "купить хлеб")
        self.assertEqual(parsed.due_at.strftime("%Y-%m-%d %H:%M"), "2026-09-23 19:30")

    def test_apply_time_advances_recurring_to_future(self):
        now = datetime(2026, 9, 23, 18, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("каждый день купить хлеб", now, "Europe/Moscow")
        updated = apply_time(parsed, "09:00", now, "Europe/Moscow")
        self.assertEqual(updated.due_at.strftime("%Y-%m-%d %H:%M"), "2026-09-24 09:00")

    def test_long_title_is_trimmed(self):
        now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Moscow"))
        parsed = parse_reminder("сегодня в 10:00 " + ("очень " * 80), now, "Europe/Moscow")
        self.assertLessEqual(len(parsed.title), 200)


if __name__ == "__main__":
    unittest.main()
