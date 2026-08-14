"""Lead workflow engine — manages state transitions and triggers WhatsApp actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional

from loguru import logger


class LeadStage(str, Enum):
    NEW = "new"
    CALLED = "called"
    CONNECTED = "connected"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    DETAILS_SHARED = "details_shared"
    VISIT_SCHEDULED = "visit_scheduled"
    VISIT_CONFIRMED = "visit_confirmed"
    VISIT_COMPLETED = "visit_completed"
    VISIT_NO_SHOW = "visit_no_show"
    ALTERNATIVE_SENT = "alternative_sent"
    BOOKED = "booked"
    LOST = "lost"
    ARCHIVED = "archived"


# Allowed stage transitions
_TRANSITIONS: dict[LeadStage, set[LeadStage]] = {
    LeadStage.NEW: {LeadStage.CALLED},
    LeadStage.CALLED: {LeadStage.CONNECTED, LeadStage.NOT_INTERESTED},
    LeadStage.CONNECTED: {LeadStage.INTERESTED, LeadStage.NOT_INTERESTED, LeadStage.ALTERNATIVE_SENT},
    LeadStage.INTERESTED: {LeadStage.DETAILS_SHARED, LeadStage.VISIT_SCHEDULED},
    LeadStage.NOT_INTERESTED: {LeadStage.ALTERNATIVE_SENT, LeadStage.ARCHIVED},
    LeadStage.DETAILS_SHARED: {LeadStage.VISIT_SCHEDULED, LeadStage.NOT_INTERESTED},
    LeadStage.VISIT_SCHEDULED: {LeadStage.VISIT_CONFIRMED, LeadStage.VISIT_NO_SHOW},
    LeadStage.VISIT_CONFIRMED: {LeadStage.VISIT_COMPLETED, LeadStage.VISIT_NO_SHOW},
    LeadStage.VISIT_COMPLETED: {LeadStage.BOOKED, LeadStage.LOST},
    LeadStage.VISIT_NO_SHOW: {LeadStage.VISIT_SCHEDULED, LeadStage.LOST},
    LeadStage.ALTERNATIVE_SENT: {LeadStage.INTERESTED, LeadStage.ARCHIVED},
    LeadStage.BOOKED: set(),
    LeadStage.LOST: {LeadStage.ARCHIVED},
    LeadStage.ARCHIVED: set(),
}


def can_transition(current: LeadStage, target: LeadStage) -> bool:
    return target in _TRANSITIONS.get(current, set())


def get_lead_stage(lead: dict) -> LeadStage:
    raw = (lead.get("workflow_stage") or lead.get("status") or "new").strip().lower()
    try:
        return LeadStage(raw)
    except ValueError:
        return LeadStage.NEW


async def transition_lead(
    lead_id: int,
    target_stage: LeadStage,
    *,
    meta: dict | None = None,
    auto_action: bool = True,
) -> dict[str, Any]:
    """Transition a lead to a new stage, update DB, and optionally trigger WhatsApp actions."""
    from core.storage import _get_conn

    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        return {"error": "lead_not_found"}

    lead = dict(row)
    current = get_lead_stage(lead)

    if not can_transition(current, target_stage):
        return {"error": f"invalid_transition: {current.value} -> {target_stage.value}"}

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    extra = {}
    raw_extra = lead.get("extra")
    try:
        extra = json.loads(raw_extra) if isinstance(raw_extra, (str, bytes, bytearray)) else (raw_extra or {})
        if not isinstance(extra, dict):
            extra = {}
    except (json.JSONDecodeError, TypeError):
        extra = {}
    if meta:
        extra.update(meta)

    conn.execute(
        "UPDATE leads SET workflow_stage = ?, extra = ?, updated_at = ? WHERE id = ?",
        (target_stage.value, json.dumps(extra, ensure_ascii=False), now_iso, lead_id),
    )
    conn.commit()

    logger.info("Lead {} transitioned: {} -> {}", lead_id, current.value, target_stage.value)

    if auto_action:
        await _on_transition(lead_id, lead, current, target_stage, extra)

    return {"lead_id": lead_id, "from": current.value, "to": target_stage.value}


async def _on_transition(
    lead_id: int,
    lead: dict,
    from_stage: LeadStage,
    to_stage: LeadStage,
    extra: dict,
) -> None:
    """Trigger WhatsApp actions based on stage transitions."""
    from services.whatsapp.client import send_text_meta
    from services.whatsapp.templates import (
        greeting_after_call,
        visit_scheduling_prompt,
        visit_confirmed,
        visit_reminder,
        followup_after_no_show,
        alternative_options,
        final_booking,
        archieve_lead,
    )

    phone = (lead.get("phone") or "").strip()
    name = (lead.get("name") or "there").strip()

    if not phone:
        return

    if to_stage == LeadStage.INTERESTED:
        msg = greeting_after_call(name)
        await send_text_meta(phone, msg)
        # Plan Phase 6: WhatsApp Immediate Brochure. The durable
        # whatsapp_package workflow job (live_job_executor) is the source of
        # truth, but send the brochure right here too so an interested lead
        # always gets it even if the orchestration job loop is down/backlogged.
        try:
            from services.whatsapp.brochure import send_full_package

            await send_full_package(phone, name)
        except Exception as exc:
            logger.warning("Immediate brochure send failed for {}: {}", phone, exc)

    elif to_stage == LeadStage.DETAILS_SHARED:
        from services.whatsapp.brochure import brochure_message
        msg = brochure_message(name)
        await send_text_meta(phone, msg)

    elif to_stage == LeadStage.VISIT_SCHEDULED:
        msg = visit_scheduling_prompt(name)
        await send_text_meta(phone, msg)

    elif to_stage == LeadStage.VISIT_CONFIRMED:
        vd = extra.get("visit_date", "TBD")
        vt = extra.get("visit_time", "TBD")
        addr = extra.get("address", "Contact for address")
        msg = visit_confirmed(name, vd, vt, addr)
        await send_text_meta(phone, msg)

    elif to_stage == LeadStage.VISIT_NO_SHOW:
        msg = followup_after_no_show(name)
        await send_text_meta(phone, msg)

    elif to_stage == LeadStage.ALTERNATIVE_SENT:
        msg = alternative_options(name)
        await send_text_meta(phone, msg)

    elif to_stage == LeadStage.BOOKED:
        prop = extra.get("property_name", "your property")
        msg = final_booking(name, prop)
        await send_text_meta(phone, msg)

    elif to_stage in (LeadStage.ARCHIVED, LeadStage.LOST):
        msg = archieve_lead(name)
        await send_text_meta(phone, msg)


async def create_lead(
    *,
    phone: str,
    name: str = "",
    email: str = "",
    details: str = "",
    source: str = "manual",
    meta: dict | None = None,
) -> int:
    """Create a new lead and return its ID."""
    from core.storage import _get_conn

    conn = _get_conn()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    extra = {"source": source, "created_at": now_iso}
    if meta:
        extra.update(meta)

    cur = conn.execute(
        "INSERT INTO leads (role, name, phone, email, details, extra, status, workflow_stage) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        ("sales_1", (name or "Unknown").strip(), phone.strip(), email.strip(),
         (details or "")[:500], json.dumps(extra, ensure_ascii=False), LeadStage.NEW.value),
    )
    conn.commit()
    lead_id = int(cur.lastrowid)
    logger.info("New lead created: id={} phone={}", lead_id, phone)
    return lead_id


async def get_lead(lead_id: int) -> dict | None:
    from core.storage import _get_conn
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id = ? AND role = 'sales_1'", (lead_id,)).fetchone()
    return dict(row) if row else None


async def list_leads(
    stage: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    from core.storage import _get_conn
    conn = _get_conn()
    if stage:
        rows = conn.execute(
            "SELECT * FROM leads WHERE role = 'sales_1' AND workflow_stage = ? "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (stage, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE role = 'sales_1' "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]
