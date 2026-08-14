"""Dependency-free business-hour arithmetic for orchestration scheduling."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def add_working_hours(
    start: datetime, hours: float, *, timezone_name: str | None = None,
    work_start: time | None = None, work_end: time | None = None,
    skip_weekends: bool = False,
) -> datetime:
    if hours < 0:
        raise ValueError("hours must be non-negative")
    try:
        from config import settings
        effective_tz = timezone_name if timezone_name is not None else (
            settings.orchestration_business_tz or "Asia/Kolkata"
        )
        if work_start is None:
            work_start = datetime.strptime(settings.orchestration_work_start, "%H:%M").time()
        if work_end is None:
            work_end = datetime.strptime(settings.orchestration_work_end, "%H:%M").time()
    except Exception:
        effective_tz = timezone_name or "Asia/Kolkata"
        if work_start is None:
            work_start = time(11, 0)
        if work_end is None:
            work_end = time(19, 30)
    tz = ZoneInfo(effective_tz)
    cursor = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)
    remaining = timedelta(hours=hours)
    while True:
        if skip_weekends and cursor.weekday() >= 5:
            cursor = datetime.combine(cursor.date() + timedelta(days=1), work_start, tz)
            continue
        day_start = datetime.combine(cursor.date(), work_start, tz)
        day_end = datetime.combine(cursor.date(), work_end, tz)
        if cursor < day_start:
            cursor = day_start
        elif cursor >= day_end:
            cursor = datetime.combine(cursor.date() + timedelta(days=1), work_start, tz)
            continue
        available = day_end - cursor
        if remaining <= available:
            return cursor + remaining
        remaining -= available
        cursor = datetime.combine(cursor.date() + timedelta(days=1), work_start, tz)

