"""Resolve callback datetimes from LLM tool args + relative phrases in notes."""

from __future__ import annotations

import re
import time
from datetime import datetime, time as dtime, timedelta
from typing import Optional, Tuple

from services.callback_time import parse_requested_callback_iso_to_utc_epoch, zoneinfo_safe


def _slide_into_working_hours(target: datetime, work_start, work_end, tz) -> datetime:
    """Shift a wall-clock target into the next valid 11:00-19:30 slot.

    Plan Phase 3: relative callbacks ('in 10 minutes') are computed as
    now + delta, then paused/shifted when they land outside the business
    window — never dialed inside quiet hours.
    """
    local = target.astimezone(tz)
    day_start = datetime.combine(local.date(), work_start, tz)
    day_end = datetime.combine(local.date(), work_end, tz)
    if local < day_start:
        return day_start
    if local >= day_end:
        return datetime.combine(local.date() + timedelta(days=1), work_start, tz)
    return local


def _minutes_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    t = text.lower()
    word_to_num = {
        "one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "fifteen": 15, "thirty": 30,
    }
    patterns = [
        r"(?:call\s+(?:me\s+)?)?(?:back\s+)?(?:after|in)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|thirty)\s*(?:minutes?|mins?)\b",
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|thirty)\s*(?:minutes?|mins?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            val = m.group(1)
            num = int(val) if val.isdigit() else word_to_num.get(val)
            if num and num > 0:
                return num
    return None


def resolve_callback_epoch(
    scheduled_at_iso: str,
    notes: str = "",
    *,
    tz_name: str = "Asia/Kolkata",
    now_epoch: Optional[float] = None,
) -> Tuple[float, str]:
    """
    Return (unix_epoch, human_IST_label).

    Relative phrases in ``notes`` (e.g. "after 5 minutes") override a wrong LLM ISO
    when the model miscalculates (said 1:17 instead of +5 min).
    """
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    tz = zoneinfo_safe(tz_name)
    now_local = datetime.fromtimestamp(now_epoch, tz=tz)

    rel_min = _minutes_from_text(notes or "") or _minutes_from_text(scheduled_at_iso or "")

    if rel_min:
        target = now_local + timedelta(minutes=rel_min)
        # Plan Phase 3: a relative callback that lands outside the working
        # window (11:00-19:30 IST) is shifted to the next valid slot instead of
        # being dialed during quiet hours.
        slid = _slide_into_working_hours(target, dtime(11, 0), dtime(19, 30), tz)
        epoch = slid.timestamp()
        if slid.date() == now_local.date():
            label = slid.strftime("%I:%M %p").lstrip("0") + " IST today"
        else:
            label = slid.strftime("%I:%M %p on %b %d").lstrip("0") + " IST"
        return epoch, label

    parsed = parse_requested_callback_iso_to_utc_epoch(scheduled_at_iso, tz_name)
    if parsed and parsed > now_epoch - 30:
        dt = datetime.fromtimestamp(parsed, tz=tz)
        label = dt.strftime("%I:%M %p on %b %d").lstrip("0") + " IST"
        return parsed, label

    # Fallback: 5 minutes if notes mention callback but no parseable time
    if "callback" in (notes or "").lower() or "call back" in (notes or "").lower():
        target = now_local + timedelta(minutes=5)
        slid = _slide_into_working_hours(target, dtime(11, 0), dtime(19, 30), tz)
        return slid.timestamp(), slid.strftime("%I:%M %p").lstrip("0") + " IST today"

    target = now_local + timedelta(minutes=15)
    slid = _slide_into_working_hours(target, dtime(11, 0), dtime(19, 30), tz)
    return slid.timestamp(), slid.strftime("%I:%M %p").lstrip("0") + " IST today"
