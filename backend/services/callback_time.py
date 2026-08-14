"""Parse deferred recall times emitted by transcript QA (e.g. \"call me at 5 pm\")."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger


def zoneinfo_safe(name: str) -> ZoneInfo:
    try:
        return ZoneInfo((name or "UTC").strip() or "UTC")
    except Exception:
        logger.warning(f"Invalid TRANSCRIPT_CALLBACK_TZ={name!r}; falling back to UTC")
        return ZoneInfo("UTC")


_ISO_STRIP_MS_THRESHOLD = 1_000_000  # anything above this is likely milliseconds, not a year


def _try_parse_iso_variations(s: str, tz: ZoneInfo) -> float | None:
    """Try multiple ISO format variations and return UTC epoch, or None."""
    candidates: list[str] = []

    # 1. Original string (after basic normalization in caller)
    candidates.append(s)

    # 2. Replace space with T (some models omit the T separator)
    if " " in s and "T" not in s.upper()[:12]:
        candidates.append(s.replace(" ", "T", 1))

    # 3. Remove trailing Z (already done in caller, but try again)
    if s.endswith("Z") or s.endswith("z"):
        candidates.append(s[:-1])

    # 4. Try common datetime formats that fromisoformat may reject
    from datetime import timedelta

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc).timestamp()
        except ValueError:
            continue

    # 5. Handle "+05:30" / "-07:00" offset variations that fromisoformat may reject
    #    by stripping offset and re-adding
    import re
    offset_match = re.search(r'([+-])(\d{2}):(\d{2})$', s)
    if offset_match:
        base = s[:offset_match.start()]
        for fmt_base in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                         "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(base, fmt_base)
                sign = 1 if offset_match.group(1) == "+" else -1
                hours = int(offset_match.group(2))
                mins = int(offset_match.group(3))
                offset = timedelta(hours=hours, minutes=mins) * sign
                dt = dt.replace(tzinfo=timezone.utc) - offset
                return dt.timestamp()
            except ValueError:
                continue

    return None


def parse_requested_callback_iso_to_utc_epoch(
    raw: Any,
    default_tz_name: str,
) -> float | None:
    """Return UTC epoch seconds for an ISO-ish string from the LLM, or None."""

    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None

    tz = zoneinfo_safe(default_tz_name)

    normalized = s
    if normalized.endswith("z") or normalized.endswith("Z"):
        normalized = normalized[:-1]
    normalized = normalized.replace(" ", "T", 1) if " " in normalized and "T" not in normalized.upper()[:12] else normalized

    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        utc = dt.astimezone(timezone.utc)
        return utc.timestamp()
    except ValueError:
        pass

    # Fallback: try multiple format variations
    epoch = _try_parse_iso_variations(normalized, tz)
    if epoch is not None:
        return epoch

    # Last resort: try original string variations
    epoch = _try_parse_iso_variations(s, tz)
    return epoch


def parse_relative_time_from_transcript(transcript_text: str, base_time: datetime) -> datetime | None:
    if not transcript_text:
        return None

    import json
    import re
    from datetime import timedelta

    user_texts = []
    for line in transcript_text.splitlines():
        try:
            obj = json.loads(line)
            role = obj.get("role") or obj.get("type", "")
            content = obj.get("content") or obj.get("text") or obj.get("message", "")
            if role == "user" and content:
                user_texts.append(content.strip().lower())
        except Exception:
            continue

    if not user_texts:
        # Fallback to plain text search if it's not JSONL
        full_text = transcript_text.lower()
    else:
        full_text = " ".join(user_texts[-3:])  # Focus on the last few turns

    word_to_num = {
        "one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "fifteen": 15, "thirty": 30, "half": 30
    }

    # 1. Check minute offsets: "after 1 minute", "in 5 minutes", "after one minute", etc.
    m_rel = re.search(r'\b(?:exactly\s+)?(?:after|in)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|thirty|half)\s*(?:minutes?|mins?)\b', full_text)
    if m_rel:
        val = m_rel.group(1)
        num = int(val) if val.isdigit() else word_to_num.get(val, 0)
        if num > 0:
            return base_time + timedelta(minutes=num)

    m_rel_simple = re.search(r'\b(\d+|one|two|five|ten|fifteen|thirty)\s+(?:minutes?|mins?)\b', full_text)
    if m_rel_simple:
        val = m_rel_simple.group(1)
        num = int(val) if val.isdigit() else word_to_num.get(val, 0)
        if num > 0:
            return base_time + timedelta(minutes=num)

    # 2. Check "one hour" or "1 hour"
    if "one hour" in full_text or "1 hour" in full_text:
        return base_time + timedelta(hours=1)
    if "two hours" in full_text or "2 hours" in full_text:
        return base_time + timedelta(hours=2)

    # 3. Check explicit times: "at 5:30 PM", "at 6:00 PM", "6 PM", "6:00 PM"
    time_match = re.search(r'\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(pm|am)\b', full_text)
    is_tomorrow = "tomorrow" in full_text or "kal" in full_text
    is_day_after = "day after" in full_text or "parso" in full_text

    if time_match:
        from datetime import timedelta
        hours = int(time_match.group(1))
        minutes = int(time_match.group(2)) if time_match.group(2) else 0
        ampm = time_match.group(3).lower()
        if ampm == "pm" and hours < 12:
            hours += 12
        elif ampm == "am" and hours == 12:
            hours = 0

        target_date = base_time
        if is_day_after:
            target_date += timedelta(days=2)
        elif is_tomorrow:
            target_date += timedelta(days=1)

        try:
            return target_date.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        except ValueError:
            pass

    # 4. Check general "tomorrow" or "day after" without time
    if is_day_after:
        return base_time + timedelta(days=2)
    if is_tomorrow:
        return base_time + timedelta(days=1)

    return None


def annotate_analysis_callback_epoch(
    analysis: dict[str, Any],
    *,
    tz_name: str,
    transcript_text: str = "",
) -> None:
    """Set ``analysis['callback_reminder_epoch']`` from ``requested_callback_datetime_iso``."""

    epoch = None

    if transcript_text:
        try:
            from datetime import timedelta
            tz = zoneinfo_safe(tz_name)
            local_now = datetime.now(tz)
            parsed_dt = parse_relative_time_from_transcript(transcript_text, local_now)
            if parsed_dt:
                epoch = parsed_dt.timestamp()
                analysis["requested_callback_datetime_iso"] = parsed_dt.isoformat()
                logger.info(
                    "Relative parser successfully matched callback time: {} (epoch={:.0f})",
                    analysis["requested_callback_datetime_iso"],
                    epoch
                )
        except Exception as e:
            logger.warning("Relative parser failed to process: {}", e)

    if epoch is None:
        epoch = parse_requested_callback_iso_to_utc_epoch(
            analysis.get("requested_callback_datetime_iso"),
            tz_name,
        )

    if epoch is None:
        analysis.pop("callback_reminder_epoch", None)
    else:
        analysis["callback_reminder_epoch"] = epoch

