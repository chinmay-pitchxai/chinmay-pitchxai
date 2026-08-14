"""Hard block for outbound campaign dialing outside allowed local hours (default IST).
Also provides campaign-config-aware time checks (calling window, days, holidays)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from config import settings
from services.callback_time import zoneinfo_safe


def _parse_hhmm(raw: str, default_h: int, default_m: int) -> time:
    s = (raw or "").strip()
    if not s:
        return time(default_h, default_m)
    parts = s.split(":")
    if len(parts) != 2:
        return time(default_h, default_m)
    try:
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("out of range")
        return time(h, m)
    except ValueError:
        return time(default_h, default_m)


def campaign_quiet_start() -> time:
    """First minute of the blocked window (inclusive), e.g. 19:30."""
    return _parse_hhmm(settings.campaign_quiet_start, 19, 30)


def campaign_quiet_end() -> time:
    """Last blocked minute ends when clock reaches this time (exclusive), e.g. 09:30."""
    return _parse_hhmm(settings.campaign_quiet_end, 9, 30)


def _now_in_tz() -> datetime:
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    return datetime.now(tz)


def is_campaign_quiet_hours(now: datetime | None = None) -> bool:
    """True when outbound campaign dialing must not run (overnight window by default)."""
    if not settings.campaign_quiet_hours_enabled:
        return False
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    t = now.time()
    start = campaign_quiet_start()
    end = campaign_quiet_end()
    if start > end:
        return t >= start or t < end
    return start <= t < end


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def quiet_hours_block_message() -> str:
    """Human-readable reason returned from preflight / API."""
    tz = (settings.transcript_callback_tz or "Asia/Kolkata").strip()
    qs = _fmt_time(campaign_quiet_start())
    qe = _fmt_time(campaign_quiet_end())
    return (
        f"Campaigns are blocked during quiet hours ({qs}–{qe} {tz}). "
        f"Outbound calling is allowed {qe}–{qs} only."
    )


def get_campaign_hours_status(now: datetime | None = None) -> dict[str, Any]:
    """Snapshot for dashboard / start-button gating."""
    tz_name = (settings.transcript_callback_tz or "Asia/Kolkata").strip()
    qs = campaign_quiet_start()
    qe = campaign_quiet_end()
    enabled = settings.campaign_quiet_hours_enabled
    in_quiet = is_campaign_quiet_hours(now) if enabled else False
    now_local = (now or _now_in_tz())
    if now_local.tzinfo:
        now_local = now_local.astimezone(zoneinfo_safe(tz_name))
    return {
        "enabled": enabled,
        "in_quiet_hours": in_quiet,
        "tz": tz_name,
        "quiet_start": _fmt_time(qs),
        "quiet_end": _fmt_time(qe),
        "allowed_start": _fmt_time(qe),
        "allowed_end": _fmt_time(qs),
        "local_time": now_local.strftime("%H:%M"),
        "block_message": quiet_hours_block_message() if in_quiet else "",
    }


# ── Campaign-config-aware time checks ──


def is_within_calling_window(cfg: dict, now: datetime | None = None) -> bool:
    """True if current time is within the campaign's configured calling window.

    If no window is configured (both start and end are None), returns True (anytime).
    """
    window_start = cfg.get("window_start") or cfg.get("calling_window_start")
    window_end = cfg.get("window_end") or cfg.get("calling_window_end")
    if not window_start and not window_end:
        return True
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    current_time = now.time()
    start = _parse_hhmm(window_start, 0, 0) if window_start else time(0, 0)
    end = _parse_hhmm(window_end, 23, 59) if window_end else time(23, 59)
    if start <= end:
        return start <= current_time <= end
    return current_time >= start or current_time <= end


def campaign_dial_window_active(cfg: dict, now: datetime | None = None) -> bool:
    """True when this campaign may dial right now.

    A configured calling window is authoritative (it overrides the global quiet
    hours). When the campaign has NO window configured, falls back to the global
    quiet-hours gate so the overnight default (e.g. 19:30–09:30) still applies.
    """
    has_window = bool(
        (cfg or {}).get("window_start")
        or (cfg or {}).get("calling_window_start")
        or (cfg or {}).get("window_end")
        or (cfg or {}).get("calling_window_end")
    )
    if has_window:
        return (
            is_within_calling_window(cfg, now)
            and is_calling_day(cfg, now)
            and not is_holiday_check(cfg, now)
        )
    return not is_campaign_quiet_hours(now)


def is_calling_day(cfg: dict, now: datetime | None = None) -> bool:
    """True if today's weekday is in the campaign's configured calling days.

    If no calling_days is configured, returns True (any day).
    calling_days is a list of integers: 0=Monday, 6=Sunday
    """
    calling_days = cfg.get("calling_days")
    if not calling_days:
        return True
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    return now.weekday() in calling_days


def is_holiday_check(cfg: dict, now: datetime | None = None) -> bool:
    """True if today's date is in the campaign's configured holidays list.

    holidays is a list of date strings in ISO format (YYYY-MM-DD).
    """
    holidays = cfg.get("holidays")
    if not holidays:
        return False
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    today_str = now.date().isoformat()
    return today_str in holidays


def get_skip_recently_days(cfg: dict) -> int:
    """Return the number of days to skip recently-called leads."""
    return int(cfg.get("skip_recently_days") or 0)


def get_concurrent_call_limit(cfg: dict) -> int:
    """Return the max concurrent calls for this campaign (default 1)."""
    return max(1, int(cfg.get("concurrent_call_limit") or 1))


def get_retry_count(cfg: dict) -> int:
    """Return the max retry count for this campaign (default 3)."""
    return max(1, int(cfg.get("retry_count") or 3))


def get_campaign_time_status(role: str, cfg: dict, now: datetime | None = None) -> dict[str, Any]:
    """Comprehensive time status for a campaign role."""
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    in_quiet = is_campaign_quiet_hours(now)
    in_window = is_within_calling_window(cfg, now)
    is_day = is_calling_day(cfg, now)
    is_hol = is_holiday_check(cfg, now)
    can_dial = in_window and is_day and not is_hol
    if in_quiet:
        can_dial = False
    return {
        "can_dial": can_dial,
        "in_quiet_hours": in_quiet,
        "in_calling_window": in_window,
        "is_calling_day": is_day,
        "is_holiday": is_hol,
        "current_time": now.strftime("%H:%M"),
        "current_weekday": now.weekday(),
        "skip_recently_days": get_skip_recently_days(cfg),
        "concurrent_call_limit": get_concurrent_call_limit(cfg),
        "retry_count": get_retry_count(cfg),
    }


def seconds_until_calling_window(cfg: dict, now: datetime | None = None) -> float:
    """Estimate seconds until the next calling window opens.

    Returns 0 if currently within the window.
    """
    if is_within_calling_window(cfg, now) and is_calling_day(cfg, now) and not is_holiday_check(cfg, now):
        return 0.0
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    window_start = cfg.get("window_start") or cfg.get("calling_window_start")
    if not window_start:
        return 0.0
    start = _parse_hhmm(window_start, 0, 0)
    today_start = datetime.combine(now.date(), start, tz)
    if now < today_start:
        return (today_start - now).total_seconds()
    tomorrow_start = datetime.combine(now.date() + timedelta(days=1), start, tz)
    return (tomorrow_start - now).total_seconds()
