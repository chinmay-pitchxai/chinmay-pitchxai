"""Real-data API for the Voice Calling Dashboard (Technopolis).

Returns leads shaped exactly like the dashboard's ``allLeads`` entries so the
existing rendering functions in ``app.js`` keep working unchanged, plus a
sandbox overview derived from real lead state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Query
from loguru import logger

from core.storage import _get_conn

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

SANDBOX_PURPOSE = {
    1: "Initial Outreach — fresh cold & digital leads",
    2: "Failed-Call Retry — attempts 2 and 3",
    3: "Nurture & Site Visits — callbacks, reminders, WhatsApp",
    4: "Post-Visit Feedback — after completed visits / sales handover",
}

_DISPO_MAP = {
    "Interested": "Interested",
    "Not Interested": "Not Interested",
    "Call Later": "Call Later",
    "Callback": "Callback",
    "Failed": "Failed",
    "No Answer": "Failed",
    "Busy": "Failed",
    "No Response": "Answered",
    "Answered": "Answered",
    "Voice Mail": "Answered",
    "Voicemail": "Answered",
    "No Conversation": "Answered",
    "Site Visit": "Site Visit",
    "Site Visited": "Site Visit",
    "": "Pending",
}


def _disposition(lead: dict) -> str:
    raw = ""
    analysis = lead.get("analysis")
    if analysis:
        try:
            parsed = json.loads(analysis) if isinstance(analysis, str) else analysis
            raw = (parsed.get("disposition") or "").strip()
        except Exception:
            raw = ""
    mapped = _DISPO_MAP.get(raw, "Pending")
    status = (lead.get("status") or "").strip().lower()
    if status == "pending" and raw:
        mapped = _DISPO_MAP.get(raw, "Pending")
    if status in ("callback_scheduled", "callback_completed") and raw not in ("Interested", "Not Interested"):
        mapped = "Callback"
    return mapped


def _summary(lead: dict) -> str:
    analysis = lead.get("analysis")
    if analysis:
        try:
            parsed = json.loads(analysis) if isinstance(analysis, str) else analysis
            summary = (parsed.get("summary") or "").strip()
            if summary:
                return summary
        except Exception:
            pass
    status = (lead.get("status") or "").strip().lower()
    if status == "pending":
        return "Lead added, call pending dispatch."
    if status in ("failed", "no answer", "no_answer"):
        return "Why this failed: " + (lead.get("error") or "Call not completed")
    return "No analysis summary yet."


def _rating(lead: dict) -> Any:
    analysis = lead.get("analysis")
    if analysis:
        try:
            parsed = json.loads(analysis) if isinstance(analysis, str) else analysis
            rating = parsed.get("rating")
            if isinstance(rating, (int, float)) and rating > 0:
                return int(rating)
        except Exception:
            pass
    return "—"


def _sandbox_of(lead: dict) -> int:
    """Determine which sandbox this lead belongs to.

    Sandbox mapping per the implementation plan:
      1 = Initial Outreach (fresh cold P1/P2 + digital P3)
      2 = Failed-Call Retry (P4 attempt 2, P5/P6 attempt 3)
      3 = Nurture & Site Visits (P7/P8 — interested leads from SB1+SB2)
      4 = Post-Visit Feedback (P9)

    Rule: always use the explicit ``sandbox`` column stored in the database.
    The sandbox column is updated by the worker when a lead transitions
    between sandboxes (SB1→SB2 on failed call, SB1/SB2→SB3 on interested,
    SB3→SB4 on site-visit completed).

    Never derive sandbox from status/disposition heuristics — that causes
    the same leads to appear in multiple sandbox views.
    """
    explicit = lead.get("sandbox")
    if explicit is not None:
        try:
            return max(1, min(4, int(explicit)))
        except (ValueError, TypeError):
            pass
    # Absolute fallback: unclassified leads belong to Sandbox 1
    return 1


def _transcript(lead: dict) -> str:
    """Best available transcript text for a lead row (raw JSONL / plain text).

    Resolves the session log_id (row ``_log_id`` or latest call attempt), then
    reads the persisted JSONL transcript just like the campaign transcript API.
    Falls back to transcript text embedded in the analysis blob when no on-disk
    JSONL exists yet. Returns "" only when no transcript of any kind exists so
    the frontend can fall back to the live transcript API.
    """
    role = str(lead.get("role") or "sales_1").strip() or "sales_1"
    log_id = str(lead.get("_log_id") or lead.get("log_id") or "").strip()
    if not log_id:
        try:
            from core.storage import resolve_lead_session_log_id_sync

            log_id = resolve_lead_session_log_id_sync(
                role,
                lead.get("id"),
                lead.get("phone") or "",
                current_log_id=log_id,
            )
        except Exception:
            pass
    if log_id:
        try:
            from core.worker import _read_transcript_jsonl

            raw = _read_transcript_jsonl(role, log_id)
            if (raw or "").strip():
                return raw
        except Exception:
            pass
    analysis = lead.get("analysis")
    if analysis:
        try:
            parsed = json.loads(analysis) if isinstance(analysis, str) else analysis
            derived = (parsed.get("transcript") or parsed.get("transcript_text") or "").strip()
            if derived:
                return derived
        except Exception:
            pass
    return ""


def _transcript_url(lead: dict) -> str:
    log_id = str(lead.get("_log_id") or lead.get("log_id") or "").strip()
    if not log_id or lead.get("id") is None:
        return ""
    role_key = str(lead.get("role") or "sales_1").strip() or "sales_1"
    return f"/api/campaign/lead/{int(lead['id'])}/transcript?role={role_key}&log_id={log_id}"


def _to_ts(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except Exception:
            continue
    return 0.0


def _lead_payload(row: Any) -> dict:
    lead = dict(row)
    status = (lead.get("status") or "").strip().lower()
    start_time = lead.get("start_time")
    called_at_iso = None
    ts_start = _to_ts(start_time)
    if ts_start:
        called_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_start))
    duration = 0
    analysis = lead.get("analysis")
    if analysis:
        try:
            parsed = json.loads(analysis) if isinstance(analysis, str) else analysis
            duration = int(parsed.get("call_duration_sec") or 0)
        except Exception:
            duration = 0
    # Upload provenance: file uploads store the original filename in
    # ``extra.upload_source``; Google Sheet broker rows store ``extra.broker_id``.
    extra_obj = {}
    extra_raw = lead.get("extra")
    if extra_raw:
        try:
            parsed = json.loads(extra_raw) if isinstance(extra_raw, str) else extra_raw
            if isinstance(parsed, dict):
                extra_obj = parsed
        except Exception:
            extra_obj = {}
    return {
        "id": int(lead["id"]),
        "name": lead.get("name") or "",
        "phone": lead.get("phone") or "",
        "email": lead.get("email") or "",
        "company": lead.get("company") or "",
        "segment": lead.get("segment") or lead.get("source_file") or "",
        "source": lead.get("source") or "campaign",
        "role": lead.get("role") or "",
        "upload_source": (extra_obj.get("upload_source") or lead.get("source_file") or ""),
        "broker_id": extra_obj.get("broker_id") or "",
        "extra": extra_obj,
        "status": (lead.get("status") or "pending").strip(),
        "disposition": _disposition(lead),
        "error": lead.get("error") or "—",
        "rating": _rating(lead),
        "summary": _summary(lead),
        "transcript": _transcript(lead),
        "log_id": lead.get("_log_id") or lead.get("log_id") or "",
        "transcript_url": _transcript_url(lead),
        "duration_sec": duration,
        "created_at": _to_ts(lead.get("created_at")),
        "start_time": ts_start if ts_start else None,
        "called_at_iso": called_at_iso,
        "sandbox": _sandbox_of(lead),
        "whatsapp_sent": bool(lead.get("whatsapp_sent")),
        "site_visit_scheduled": (
            _disposition(lead) == "Site Visit"
            or str(lead.get("status") or "").strip().lower() in ("site_visit", "site_visited")
        ),
    }


def _fetch_leads(limit: int, role: str = "") -> list[dict]:
    conn = _get_conn()
    params: list[Any] = []
    where = ""
    if role:
        wanted_role = role.strip().lower()
        where = "WHERE role = ?"
        params.append(wanted_role)
    rows = conn.execute(
        f"""SELECT id, role, name, phone, email, company, status, analysis,
                  error, extra, start_time, created_at, whatsapp_sent,
                  failed_call_retries, segment, source_file, sandbox, source, _log_id
            FROM leads {where}
            ORDER BY COALESCE(start_time, 0) DESC LIMIT ?""",
        tuple(params + [limit]),
    ).fetchall()
    return [_lead_payload(r) for r in rows]


@router.get("/leads")
async def dashboard_leads(
    sandbox: int = Query(0, ge=0, le=4),
    limit: int = Query(20000, ge=1, le=100000),
    role: str = Query(""),
    lead_source: str = Query(""),
):
    """Return real leads shaped like the dashboard's ``allLeads`` array."""
    role_key = (role or "sales_1").strip().lower()
    leads = _fetch_leads(limit, role=role_key)
    if lead_source in ("campaign", "digital"):
        digital_sources = {"digital", "digital_marketing"}
        leads = [
            l for l in leads
            if ((str(l.get("source") or "").strip().lower() in digital_sources) == (lead_source == "digital"))
        ]
    if sandbox:
        leads = [l for l in leads if l["sandbox"] == sandbox]
    return {"as_of": time.time(), "sandbox": sandbox, "count": len(leads), "leads": leads}


@router.get("/overview")
async def dashboard_overview(role: str = Query("")):
    """Per-sandbox KPIs from the real database."""
    role_key = (role or "sales_1").strip().lower()
    leads = _fetch_leads(100000, role=role_key)
    by_sandbox: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    for l in leads:
        by_sandbox.setdefault(l["sandbox"], []).append(l)

    sandboxes = []
    for sb in (1, 2, 3, 4):
        box = by_sandbox.get(sb, [])
        called = [l for l in box if l["called_at_iso"]]
        interested = [l for l in box if l["disposition"] == "Interested"]
        sandboxes.append({
            "id": sb,
            "display_name": f"Sandbox {sb}",
            "purpose": SANDBOX_PURPOSE[sb],
            "total_leads": len(box),
            "called_count": len(called),
            "interested": len(interested),
            "whatsapp_sent": sum(1 for l in box if l["whatsapp_sent"]),
            "site_visit_scheduled": sum(1 for l in box if l["site_visit_scheduled"]),
            "not_interested": sum(1 for l in box if l["disposition"] == "Not Interested"),
            "callbacks": sum(1 for l in box if l["disposition"] in ("Call Later", "Callback")),
            "failed": sum(1 for l in box if l["disposition"] == "Failed"),
            "conversion_rate": round(len(interested) / len(called) * 100, 1) if called else 0,
        })
    total = leads
    return {
        "as_of": time.time(),
        "total_leads": len(total),
        "called_count": sum(1 for l in total if l["called_at_iso"]),
        "interested": sum(1 for l in total if l["disposition"] == "Interested"),
        "sandboxes": sandboxes,
    }
