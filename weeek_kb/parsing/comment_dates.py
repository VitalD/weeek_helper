"""Парсинг дат из meta-info, задач и комментариев (RU + ISO)."""

from __future__ import annotations

import re
from datetime import date, datetime

_RU_MONTHS = {
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


def parse_meta_date(value: str | None) -> date:
    """ДД.ММ.ГГГГ из meta-info.json."""
    if not value or not str(value).strip():
        raise ValueError("пустая дата meta-info")
    s = str(value).strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if not m:
        raise ValueError(f"ожидался ДД.ММ.ГГГГ, получено: {s!r}")
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return date(y, mo, d)


def parse_task_finish_date(value: str | None) -> date | None:
    """датаЗавершения из board JSON: ISO, ДД.ММ.ГГГГ, D.MM.YYYY."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.fromisoformat(s[:10]).date()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return parse_comment_date(s)


def parse_comment_date(value: str | None) -> date | None:
    """
    Дата комментария: «14 апреля 2026, 12:39», ISO, ДД.ММ.ГГГГ.
    Возвращает None, если распознать не удалось.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            return datetime.fromisoformat(s[:10]).date()
        except ValueError:
            pass

    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    m = re.search(
        r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})",
        s.lower().replace("ё", "е"),
    )
    if m:
        day = int(m.group(1))
        month_name = m.group(2)
        year = int(m.group(3))
        month = _RU_MONTHS.get(month_name)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def done_window(start_anchor: date, *, days: int = 7) -> tuple[date, date]:
    """Интервал [anchor - (days-1), anchor] включительно."""
    from datetime import timedelta

    return start_anchor - timedelta(days=days - 1), start_anchor
