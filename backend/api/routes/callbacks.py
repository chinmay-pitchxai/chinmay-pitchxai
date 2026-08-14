"""Agent-scheduled individual callbacks.

Agents can schedule a callback for a specific phone number at a specific
future time. The campaign worker picks these up at the scheduled moment
and calls them immediately, bypassing the normal inter-call gap.

If the role is currently on a call when the callback becomes due, it is
marked ``queued`` and executed as soon as the current call finishes.

Endpoints
---------
- ``GET    /api/callbacks?role=<role>``   — list scheduled callbacks
- ``POST   /api/callbacks?role=<role>``   — schedule a new callback
- ``DELETE /api/callbacks/{id}``          — cancel a pending callback
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from core.state import normalize_console_role
from core.storage import (
    add_scheduled_callback,
    list_scheduled_callbacks,
    get_scheduled_callback,
    cancel_scheduled_callback,
    update_scheduled_callback_status,
    update_scheduled_callback_review,
)
from core.phone_norm import norm_phone_str

router = APIRouter(prefix="/api/callbacks", tags=["callbacks"])


def _role(request: Request, fallback: Optional[str] = None) -> str:
    from core.auth import console_role_from_request

    role_param = (request.query_params.get("role") or "").strip()
    if role_param:
        return normalize_console_role(role_param)
    return console_role_from_request(request, default=fallback or "sales_1")


class CallbackCreate(BaseModel):
    phone: str
    name: str = ""
    lead_id: Optional[int] = None
    outbound_phone: Optional[str] = None
    scheduled_at_iso: Optional[str] = None
    scheduled_at: Optional[float] = None


class CallbackReview(BaseModel):
    review: str  # interested | not_interested | "" (clear)


@router.get("")
async def list_callbacks(request: Request, limit: int = 500):
    role = _role(request)
    cap = max(1, min(int(limit), 5000))
    items = await list_scheduled_callbacks(role, limit=cap)
    return {"role": role, "callbacks": items, "now": time.time()}


@router.post("")
async def create_callback(payload: CallbackCreate, request: Request):
    role = _role(request)

    phone = norm_phone_str((payload.phone or "").strip())
    if not phone:
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number — enter 10 digits (after +91), or a full number starting with +.",
        )

    # TRAI/DND compliance (plan §1.4 / §4.3): never schedule a callback for a
    # number on the do-not-contact register. The register is keyed by the last
    # 10 digits (see orchestration_service.opt_out), so normalize the same way.
    try:
        from core.storage import _get_conn

        _digits = "".join(ch for ch in phone if ch.isdigit())
        _dnc_key = _digits[-10:] if len(_digits) >= 10 else _digits
        if _get_conn().execute(
            "SELECT 1 FROM do_not_contact WHERE normalized_phone=?", (_dnc_key,)
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Phone number is registered in the do-not-contact list — callback blocked.",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    # Resolve scheduled_at from iso or epoch
    epoch: float | None = None
    if payload.scheduled_at is not None:
        epoch = float(payload.scheduled_at)
    elif payload.scheduled_at_iso:
        iso = payload.scheduled_at_iso.strip()
        if iso.endswith("Z") or iso.endswith("z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid scheduled_at_iso: {e}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
    else:
        raise HTTPException(status_code=400, detail="Provide scheduled_at or scheduled_at_iso.")

    now = time.time()
    # Allow 15s clock-skew window
    if epoch < now - 15:
        raise HTTPException(
            status_code=400,
            detail="Scheduled time is in the past. Pick a future time.",
        )

    out_phone = (payload.outbound_phone or "").strip()
    if not out_phone and payload.lead_id:
        try:
            from core.storage import get_lead
            _lr = await get_lead(role, int(payload.lead_id))
            if _lr:
                out_phone = str(_lr.get("outbound_phone") or "").strip()
        except Exception:
            pass

    callback_id = await add_scheduled_callback(
        role=role,
        phone=phone,
        name=(payload.name or "").strip(),
        scheduled_at=epoch,
        lead_id=payload.lead_id,
        outbound_phone=out_phone,
    )

    logger.info(
        "Scheduled callback id={} role={!r} phone={} at={:.0f} (in {}s)",
        callback_id, role, phone, epoch, max(0, int(epoch - now)),
    )

    cb = await get_scheduled_callback(callback_id)
    return {"status": "ok", "id": callback_id, "callback": cb}


@router.delete("/{callback_id}")
async def remove_callback(callback_id: int):
    ok = await cancel_scheduled_callback(callback_id)
    if not ok:
        cb = await get_scheduled_callback(callback_id)
        if cb is None:
            raise HTTPException(status_code=404, detail="Callback not found.")
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel — callback is {cb.get('status')}.",
        )
    logger.info("Cancelled scheduled callback id={}", callback_id)
    return {"status": "ok", "id": callback_id}


@router.patch("/{callback_id}/review")
async def review_callback(callback_id: int, payload: CallbackReview, request: Request):
    """Mark a scheduled callback lead as interested (✓) or not interested (✗)."""
    role = _role(request)
    cb = await get_scheduled_callback(callback_id)
    if cb is None:
        raise HTTPException(status_code=404, detail="Callback not found.")
    if (cb.get("role") or "").strip().lower() != role:
        raise HTTPException(status_code=403, detail="Callback belongs to a different role.")

    review = (payload.review or "").strip().lower()
    if review not in ("interested", "not_interested", ""):
        raise HTTPException(
            status_code=400,
            detail="review must be 'interested', 'not_interested', or empty string to clear.",
        )

    ok = await update_scheduled_callback_review(callback_id, review)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid review value.")

    updated = await get_scheduled_callback(callback_id)
    return {"status": "ok", "id": callback_id, "callback": updated}
