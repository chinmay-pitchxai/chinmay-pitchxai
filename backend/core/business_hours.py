"""Dependency-free business-hour arithmetic for orchestration scheduling."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def working_window(*, timezone_name: str | None = None) -> tuple[ZoneInfo, time, time]:
    """Resolve the configured orchestration timezone and daily calling window."""
    try:
        from config import settings

        tz_name = timezone_name or settings.orchestration_business_tz or "Asia/Kolkata"
        start = datetime.strptime(settings.orchestration_work_start, "%H:%M").time()
        end = datetime.strptime(settings.orchestration_work_end, "%H:%M").time()
    except Exception:
        tz_name, start, end = timezone_name or "Asia/Kolkata", time(11, 0), time(19, 30)
    if start >= end:
        raise ValueError("orchestration work start must be earlier than work end")
    return ZoneInfo(tz_name), start, end


def is_within_working_hours(value: datetime) -> bool:
    tz, start, end = working_window()
    local = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return start <= local.time() < end


def next_working_time(value: datetime) -> datetime:
    """Keep an in-window time unchanged or slide it to the next opening."""
    tz, start, end = working_window()
    local = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    if local.time() < start:
        return datetime.combine(local.date(), start, tz)
    if local.time() >= end:
        return datetime.combine(local.date() + timedelta(days=1), start, tz)
    return local


def add_working_hours(
    start: datetime, hours: float, *, timezone_name: str | None = None,
    work_start: time | None = None, work_end: time | None = None,
    skip_weekends: bool = False,
) -> datetime:
    if hours < 0:
        raise ValueError("hours must be non-negative")
    if work_start is None and work_end is None:
        tz, work_start, work_end = working_window(timezone_name=timezone_name)
    else:
        tz, default_start, default_end = working_window(timezone_name=timezone_name)
        work_start = work_start or default_start
        work_end = work_end or default_end
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
