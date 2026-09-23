from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


WEEKDAYS = {
    "понедельник": 0,
    "понедельникам": 0,
    "вторник": 1,
    "вторникам": 1,
    "среду": 2,
    "среда": 2,
    "средам": 2,
    "четверг": 3,
    "четвергам": 3,
    "пятницу": 4,
    "пятница": 4,
    "пятницам": 4,
    "субботу": 5,
    "суббота": 5,
    "субботам": 5,
    "воскресенье": 6,
    "воскресеньям": 6,
}

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

MAX_TITLE_LEN = 200


@dataclass
class ParsedReminder:
    title: str
    due_at: datetime | None
    recurrence: str | None
    lead_minutes: int | None
    needs_time: bool
    confidence: float
    original_text: str


def parse_reminder(text: str, now: datetime, tz_name: str) -> ParsedReminder:
    tz = ZoneInfo(tz_name)
    now = now.astimezone(tz)
    raw = " ".join(text.strip().split())
    lower = raw.lower().replace("ё", "е")

    lead_minutes = _parse_lead_minutes(lower)
    recurrence = _parse_recurrence(lower)
    relative_due = _parse_relative_due(lower, now)
    due_at = relative_due or _parse_absolute_due(lower, now, recurrence)
    needs_time = due_at is None or (not _has_time(lower) and not _has_relative_time(lower))

    if due_at and due_at <= now and relative_due is None:
        if recurrence:
            while due_at <= now:
                due_at = next_occurrence(due_at, recurrence, tz_name)
        else:
            due_at += timedelta(days=1)

    if lead_minutes is None and re.search(r"\b(выйти|выходить|выехать|выезд)\b", lower):
        lead_minutes = 15

    title = _clean_title(lower)
    confidence = 0.9 if due_at and title else 0.45
    if needs_time:
        confidence = min(confidence, 0.55)

    return ParsedReminder(
        title=_trim_title(title or raw),
        due_at=due_at,
        recurrence=recurrence,
        lead_minutes=lead_minutes,
        needs_time=needs_time,
        confidence=confidence,
        original_text=raw,
    )


def apply_time(parsed: ParsedReminder, hhmm: str, now: datetime, tz_name: str) -> ParsedReminder:
    hour, minute = map(int, hhmm.split(":"))
    _validate_time(hour, minute)
    tz = ZoneInfo(tz_name)
    base = parsed.due_at.astimezone(tz).date() if parsed.due_at else now.astimezone(tz).date()
    due_at = datetime.combine(base, time(hour, minute), tz)
    now = now.astimezone(tz)
    if due_at <= now:
        if parsed.recurrence:
            while due_at <= now:
                due_at = next_occurrence(due_at, parsed.recurrence, tz_name)
        else:
            due_at += timedelta(days=1)
    return ParsedReminder(
        title=parsed.title,
        due_at=due_at,
        recurrence=parsed.recurrence,
        lead_minutes=parsed.lead_minutes,
        needs_time=False,
        confidence=max(parsed.confidence, 0.85),
        original_text=parsed.original_text,
    )


def next_occurrence(due_at: datetime, recurrence: str, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    current = due_at.astimezone(tz)
    kind, _, value = recurrence.partition(":")

    if kind == "daily":
        return current + timedelta(days=1)
    if kind == "weekdays":
        candidate = current + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        return current + timedelta(days=7)
    if kind == "monthly_day":
        day = int(value)
        year, month = current.year, current.month + 1
        if month == 13:
            year, month = year + 1, 1
        while True:
            try:
                return current.replace(year=year, month=month, day=day)
            except ValueError:
                month += 1
                if month == 13:
                    year, month = year + 1, 1
    if kind == "yearly":
        month, day = map(int, value.split("-"))
        year = current.year + 1
        while True:
            try:
                return current.replace(year=year, month=month, day=day)
            except ValueError:
                year += 1
    return current + timedelta(days=1)


def recurrence_label(recurrence: str | None) -> str:
    if not recurrence:
        return "один раз"
    kind, _, value = recurrence.partition(":")
    labels = {
        "daily": "каждый день",
        "weekdays": "каждый будний день",
        "weekly": "каждую неделю",
    }
    if kind == "weekly":
        names = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
        return f"каждый {names[int(value)]}"
    if kind == "monthly_day":
        return f"каждое {int(value)} число"
    if kind == "yearly":
        month, day = map(int, value.split("-"))
        month_name = next(k for k, v in MONTHS.items() if v == month)
        return f"каждый год {day} {month_name}"
    return labels.get(kind, recurrence)


def _parse_relative_due(text: str, now: datetime) -> datetime | None:
    if "через полчаса" in text:
        return now + timedelta(minutes=30)

    match = re.search(r"через\s+(\d+)\s*(минут[уы]?|мин|час[аов]?|дн[яей])", text)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("мин"):
        return now + timedelta(minutes=amount)
    if unit.startswith("час"):
        return now + timedelta(hours=amount)
    return now + timedelta(days=amount)


def _parse_absolute_due(text: str, now: datetime, recurrence: str | None) -> datetime | None:
    day = _parse_day(text, now, recurrence)
    parsed_time = _parse_time(text)
    if not parsed_time:
        return datetime.combine(day, time(0, 0), now.tzinfo) if day else None
    if not day:
        day = now.date()
    return datetime.combine(day, parsed_time, now.tzinfo)


def _parse_day(text: str, now: datetime, recurrence: str | None) -> date | None:
    if "послезавтра" in text:
        return now.date() + timedelta(days=2)
    if "завтра" in text:
        return now.date() + timedelta(days=1)
    if "сегодня" in text:
        return now.date()

    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", text)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = int(match.group(3) or now.year)
        if year < 100:
            year += 2000
        return _date_or_raise(year, month, day)

    match = re.search(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", text)
    if match:
        day, month = int(match.group(1)), MONTHS[match.group(2)]
        year = now.year
        candidate = _next_calendar_date(year, month, day, now.date())
        return candidate

    for word, weekday in WEEKDAYS.items():
        if re.search(rf"\b(?:в\s+)?{word}\b", text):
            delta = (weekday - now.weekday()) % 7
            if delta == 0 and not recurrence:
                delta = 7
            return now.date() + timedelta(days=delta)

    if recurrence and recurrence.startswith("monthly_day:"):
        day = int(recurrence.split(":", 1)[1])
        year, month = now.year, now.month
        while True:
            try:
                candidate = date(year, month, day)
            except ValueError:
                candidate = None
            if candidate and candidate >= now.date():
                return candidate
            month += 1
            if month == 13:
                year, month = year + 1, 1

    return None


def _parse_time(text: str) -> time | None:
    match = re.search(r"\b(?:в\s*)?(\d{1,2})[:.](\d{2})\b", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        _validate_time(hour, minute)
        return time(hour, minute)

    match = re.search(r"\bв\s+(\d{1,2})(?:\s*(утра|вечера|дня|ночи))?\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    part = match.group(2)
    if part in {"вечера", "дня"} and hour < 12:
        hour += 12
    if part == "ночи" and hour == 12:
        hour = 0
    _validate_time(hour, 0)
    return time(hour, 0)


def _parse_recurrence(text: str) -> str | None:
    if re.search(r"\b(каждый день|ежедневно)\b", text):
        return "daily:"
    if "каждый будний" in text or "по будням" in text:
        return "weekdays:"

    for word, weekday in WEEKDAYS.items():
        if re.search(rf"\b(каждый|каждую|по)\s+{word}\b", text):
            return f"weekly:{weekday}"

    match = re.search(r"\bкажд(?:ое|ый)\s+(\d{1,2})(?:-?е|-?го)?\s+числ", text)
    if not match:
        match = re.search(r"\bкаждый\s+месяц\s+(\d{1,2})(?:-?е|-?го)?\s+числ", text)
    if match:
        day = int(match.group(1))
        if not 1 <= day <= 31:
            raise ValueError("Некорректный день месяца")
        return f"monthly_day:{day}"

    match = re.search(r"\bкаждый год\s+(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", text)
    if match:
        month, day = MONTHS[match.group(2)], int(match.group(1))
        _date_or_raise(2024, month, day)
        return f"yearly:{month}-{day}"

    return None


def _parse_lead_minutes(text: str) -> int | None:
    match = re.search(r"\bза\s+(\d+)\s*(минут[уы]?|мин|час(?:а|ов)?)\b", text)
    if not match:
        if "за час" in text:
            return 60
        return None
    amount = int(match.group(1))
    return amount if match.group(2).startswith("мин") else amount * 60


def _has_time(text: str) -> bool:
    return bool(re.search(r"\b(?:в\s*)?\d{1,2}[:.]\d{2}\b|\bв\s+\d{1,2}(?:\s*(утра|вечера|дня|ночи))?\b", text))


def _has_relative_time(text: str) -> bool:
    return bool(re.search(r"через\s+(\d+|полчаса)", text))


def _clean_title(text: str) -> str:
    cleaned = text
    patterns = [
        r"\b(напомни(ть)?|мне надо|надо|нужно|хочу|пожалуйста)\b",
        r"\b(сегодня|завтра|послезавтра)\b",
        r"\bчерез\s+(\d+\s*)?(минут[уы]?|мин|час[аов]?|дн[яей]|полчаса)\b",
        r"\bза\s+\d+\s*(минут[уы]?|мин|час(?:а|ов)?)\b",
        r"\bза\s+час\b",
        r"\b(?:в\s*)?\d{1,2}[:.]\d{2}\b",
        r"\bв\s+\d{1,2}(?:\s*(утра|вечера|дня|ночи))?\b",
        r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b",
        r"\b\d{1,2}\s+(" + "|".join(MONTHS) + r")\b",
        r"\bкаждый\s+месяц\s+\d{1,2}(?:-?е|-?го)?\s+числ[оа]?\b",
        r"\bкажд(?:ый|ую|ое)\s+\S+(?:\s+число)?\b",
        r"\bпо\s+будням\b",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned)
    for word in WEEKDAYS:
        cleaned = re.sub(rf"\b(?:в\s+)?{word}\b", " ", cleaned)
    return " ".join(cleaned.strip(" .,;:-").split())


def _date_or_raise(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError("Некорректная дата") from exc


def _next_calendar_date(year: int, month: int, day: int, minimum: date) -> date:
    for candidate_year in range(year, year + 8):
        try:
            candidate = date(candidate_year, month, day)
        except ValueError:
            continue
        if candidate >= minimum:
            return candidate
    raise ValueError("Некорректная дата")


def _validate_time(hour: int, minute: int) -> None:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Некорректное время")


def _trim_title(title: str) -> str:
    title = title.strip()
    return title if len(title) <= MAX_TITLE_LEN else title[: MAX_TITLE_LEN - 1].rstrip() + "…"
