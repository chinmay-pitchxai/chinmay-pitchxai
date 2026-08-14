"""Site visit scheduling + follow-up lifecycle (Follow-up 1 eve, Follow-up 2 day-of)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from config import settings
from services.callback_time import zoneinfo_safe


def _parse_sv_datetime(sv_date_str: str, tz) -> datetime | None:
    s = (sv_date_str or "").strip()
    if not s or s.upper() == "TBD":
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        sv_dt = datetime.fromisoformat(s)
        if sv_dt.tzinfo is None:
            sv_dt = sv_dt.replace(tzinfo=tz)
        else:
            sv_dt = sv_dt.astimezone(tz)
        return sv_dt
    except Exception:
        return None


def _visit_day_label(sv_dt: datetime) -> str:
    return sv_dt.strftime("%A")


def compute_site_visit_callback_times(sv_dt: datetime, tz) -> tuple[datetime, datetime]:
    """Return (eve_dt at 10:00 day before, day_of_dt at 9:00 or 2h before visit)."""
    eve_dt = datetime.combine(
        sv_dt.date() - timedelta(days=1),
        datetime.min.time().replace(hour=10, minute=0),
    ).replace(tzinfo=tz)
    if sv_dt.hour < 12:
        day_of_dt = sv_dt - timedelta(hours=2)
        if day_of_dt.hour < 9:
            day_of_dt = day_of_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        day_of_dt = sv_dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return eve_dt, day_of_dt


def build_transcript_excerpt(transcript: str, max_turns: int = 5) -> str:
    lines: list[str] = []
    for line in (transcript or "").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            role = str(obj.get("role") or obj.get("type") or "").lower()
            content = str(obj.get("content") or obj.get("text") or obj.get("message") or "").strip()
            if content:
                lines.append(f"{role}: {content[:200]}")
        except Exception:
            if len(line) > 2:
                lines.append(line[:200])
    return "\n".join(lines[-max_turns:])


def build_memory_payload(
    *,
    prior_log_id: str,
    prior_summary: str,
    site_visit_datetime_iso: str,
    transcript_excerpt: str = "",
    prior_agent_name: str = "",
    site_visit_headcount: int | None = None,
) -> dict[str, Any]:
    return {
        "prior_log_id": prior_log_id or "",
        "prior_summary": (prior_summary or "")[:2000],
        "site_visit_datetime_iso": site_visit_datetime_iso or "",
        "transcript_excerpt": (transcript_excerpt or "")[:3000],
        "prior_agent_name": prior_agent_name or "",
        "site_visit_headcount": site_visit_headcount,
    }


def _follow_up_plan_has_pending(existing: list, visit_date_iso: str, cb_type: str) -> bool:
    for entry in existing:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != cb_type:
            continue
        if entry.get("status") not in ("scheduled", "queued", "calling"):
            continue
        if visit_date_iso and entry.get("site_visit_datetime_iso") == visit_date_iso:
            return True
    return False


async def apply_site_visit_lifecycle(
    *,
    role: str,
    lead_id: int,
    phone: str,
    name: str,
    outbound_phone: str,
    analysis: dict,
    extra: dict,
    attempt_number: int,
    log_id: str,
    transcript: str = "",
    agent_name: str = "",
) -> tuple[dict, dict, float | None]:
    """Write lifecycle state + schedule Follow-up 1 (eve) and Follow-up 2 (day-of).

    Returns (updated_extra, updated_analysis, earliest_callback_epoch).
    """
    from core.storage import add_scheduled_callback

    tz = zoneinfo_safe(settings.transcript_callback_tz)
    _next_act = analysis.get("next_action") or {}
    _sv_date_str = (
        (_next_act.get("datetime_iso") or analysis.get("requested_callback_datetime_iso") or "")
        .strip()
    )
    sv_dt = _parse_sv_datetime(_sv_date_str, tz)
    visit_iso = sv_dt.isoformat() if sv_dt else (_sv_date_str or "TBD")
    visit_label = _visit_day_label(sv_dt) if sv_dt else "TBD"

    extra = dict(extra or {})
    extra["lifecycle_stage"] = "site_visit_scheduled"
    extra["site_visit_datetime_iso"] = visit_iso
    extra["site_visit_scheduled_at_epoch"] = time.time()
    extra["site_visit_scheduled_from_attempt"] = int(attempt_number or 1)
    if not sv_dt:
        extra["site_visit_details"] = str(
            _next_act.get("details") or analysis.get("summary") or ""
        )[:500]

    existing_plan = extra.get("follow_up_plan")
    if not isinstance(existing_plan, list):
        existing_plan = []

    memory = build_memory_payload(
        prior_log_id=log_id or "",
        prior_summary=str(analysis.get("summary") or ""),
        site_visit_datetime_iso=visit_iso,
        transcript_excerpt=build_transcript_excerpt(transcript),
        prior_agent_name=agent_name,
    )

    earliest_future: float | None = None
    new_entries: list[dict] = []

    if sv_dt:
        eve_dt, day_of_dt = compute_site_visit_callback_times(sv_dt, tz)
        schedule_specs = [
            (1, "site_visit_eve", eve_dt, f"{name} (Follow-up 1 — Day-before confirm)"),
            (2, "site_visit_day", day_of_dt, f"{name} (Follow-up 2 — Morning-of confirm)"),
        ]
        for fu_num, cb_type, sched_dt, cb_name in schedule_specs:
            if _follow_up_plan_has_pending(existing_plan, visit_iso, cb_type):
                logger.info(
                    "Skip duplicate {} for lead {} visit={}",
                    cb_type,
                    lead_id,
                    visit_iso,
                )
                continue
            epoch = sched_dt.timestamp()
            if epoch <= time.time():
                continue
            sc_id = await add_scheduled_callback(
                role=role,
                phone=phone,
                name=cb_name,
                scheduled_at=epoch,
                lead_id=lead_id,
                outbound_phone=outbound_phone,
                callback_type=cb_type,
                follow_up_number=fu_num,
                analysis_json=memory,
            )
            label = (
                "Follow-up 1 — Day-before site visit confirmation"
                if fu_num == 1
                else "Follow-up 2 — Morning-of site visit confirmation"
            )
            entry = {
                "follow_up_number": fu_num,
                "type": cb_type,
                "label": label,
                "scheduled_at_epoch": epoch,
                "scheduled_callback_id": sc_id,
                "status": "scheduled",
                "source_attempt_number": int(attempt_number or 1),
                "site_visit_datetime_iso": visit_iso,
                "log_id": None,
            }
            new_entries.append(entry)
            if earliest_future is None or epoch < earliest_future:
                earliest_future = epoch
            logger.info(
                "Scheduled {} (#{}) for lead {} at {}",
                cb_type,
                fu_num,
                lead_id,
                sched_dt,
            )

    if new_entries:
        extra["follow_up_plan"] = existing_plan + new_entries
    elif existing_plan:
        extra["follow_up_plan"] = existing_plan

    if earliest_future:
        analysis = dict(analysis)
        analysis["callback_reminder_epoch"] = earliest_future
        analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(
            earliest_future, tz
        ).isoformat()

    return extra, analysis, earliest_future


async def apply_interested_followup_lifecycle(
    *,
    role: str,
    lead_id: int,
    phone: str,
    name: str,
    outbound_phone: str,
    analysis: dict,
    extra: dict,
    attempt_number: int,
    log_id: str,
    transcript: str = "",
    agent_name: str = "",
) -> tuple[dict, dict, float | None]:
    """Schedule Follow-up 1 for Interested leads without site visit date."""
    from core.storage import add_scheduled_callback

    if analysis.get("site_visit_agreed"):
        return extra, analysis, None
    if extra.get("prefer_whatsapp_only") or analysis.get("prefer_whatsapp_only"):
        return extra, analysis, None
    emotion = str(analysis.get("emotion_label") or "").lower()
    if emotion in ("skeptical", "suspicious"):
        return extra, analysis, None

    tz = zoneinfo_safe(settings.transcript_callback_tz)
    now = datetime.now(tz)
    follow_at = now + timedelta(hours=24)
    if follow_at.hour < 9 or follow_at.hour >= 19:
        follow_at = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    epoch = follow_at.timestamp()
    if epoch <= time.time():
        return extra, analysis, None

    existing_plan = extra.get("follow_up_plan")
    if isinstance(existing_plan, list):
        for entry in existing_plan:
            if isinstance(entry, dict) and entry.get("type") == "interested_followup":
                if entry.get("status") in ("scheduled", "queued", "calling"):
                    return extra, analysis, None

    memory = build_memory_payload(
        prior_log_id=log_id or "",
        prior_summary=str(analysis.get("summary") or ""),
        site_visit_datetime_iso="",
        transcript_excerpt=build_transcript_excerpt(transcript),
        prior_agent_name=agent_name,
    )

    sc_id = await add_scheduled_callback(
        role=role,
        phone=phone,
        name=f"{name} (Follow-up 1 — Interested)",
        scheduled_at=epoch,
        lead_id=lead_id,
        outbound_phone=outbound_phone,
        callback_type="interested_followup",
        follow_up_number=1,
        analysis_json=memory,
    )

    extra = dict(extra or {})
    extra["lifecycle_stage"] = "interested_nurture"
    plan = list(existing_plan) if isinstance(existing_plan, list) else []
    plan.append(
        {
            "follow_up_number": 1,
            "type": "interested_followup",
            "label": "Follow-up 1 — Interested nurture",
            "scheduled_at_epoch": epoch,
            "scheduled_callback_id": sc_id,
            "status": "scheduled",
            "source_attempt_number": int(attempt_number or 1),
            "log_id": None,
        }
    )
    extra["follow_up_plan"] = plan

    analysis = dict(analysis)
    analysis["callback_reminder_epoch"] = epoch
    analysis["requested_callback_datetime_iso"] = follow_at.isoformat()
    return extra, analysis, epoch


def update_follow_up_plan_on_complete(
    extra: dict,
    *,
    scheduled_callback_id: int | None,
    follow_up_number: int | None,
    callback_type: str,
    log_id: str,
) -> dict:
    """Mark follow-up plan entry completed after a follow-up call."""
    extra = dict(extra or {})
    plan = extra.get("follow_up_plan")
    if not isinstance(plan, list):
        return extra
    updated = False
    for entry in plan:
        if not isinstance(entry, dict):
            continue
        match = False
        if scheduled_callback_id and entry.get("scheduled_callback_id") == scheduled_callback_id:
            match = True
        elif follow_up_number and entry.get("follow_up_number") == follow_up_number:
            if not callback_type or entry.get("type") == callback_type:
                match = True
        if match:
            entry["status"] = "completed"
            entry["log_id"] = log_id or entry.get("log_id")
            updated = True
            cb_type = entry.get("type") or callback_type
            if cb_type == "site_visit_day":
                extra["lifecycle_stage"] = "site_visit_confirmed_day_of"
            elif cb_type == "site_visit_eve":
                extra["lifecycle_stage"] = "site_visit_eve_confirmed"
            break
    if updated:
        extra["follow_up_plan"] = plan
    return extra


def extract_site_visit_fields_from_analysis(analysis: dict, extra: dict) -> dict:
    """Pull headcount / arrival time from follow-up call analysis into extra."""
    extra = dict(extra or {})
    summary = str(analysis.get("summary") or "").lower()
    details = str((analysis.get("next_action") or {}).get("details") or "").lower()
    blob = f"{summary} {details}"

    import re

    for pat in (
        r"(\d+)\s*(?:people|persons|members|visitors|of us|guests)",
        r"(?:party of|group of)\s*(\d+)",
    ):
        m = re.search(pat, blob)
        if m:
            try:
                extra["site_visit_headcount"] = int(m.group(1))
            except ValueError:
                pass
            break

    na = analysis.get("next_action") or {}
    dt_iso = na.get("datetime_iso") or analysis.get("requested_callback_datetime_iso")
    if dt_iso:
        extra["site_visit_arrival_time_iso"] = str(dt_iso)
    return extra
