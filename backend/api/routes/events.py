"""Live dashboard events — SSE fallback + WebSocket primary."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from loguru import logger

from config import live_dashboard_meta, settings
from core.events import get_event_bus

router = APIRouter(tags=["events"])


async def _build_state(role: str, *, fresh: bool = False) -> dict | None:
    """Rebuild campaign state payload — reads materialized state (<5ms)."""
    try:
        from core import kv_cache
        from core import storage as lead_storage

        if not fresh:
            cached = kv_cache.state_get(role)
            if cached is not None:
                return cached

        from core.dashboard_state import build_api_payload_sync
        payload = build_api_payload_sync(role)
        if payload is None:
            return None

        payload["campaign_paused"] = await lead_storage.is_campaign_globally_paused()
        payload.update(live_dashboard_meta())
        kv_cache.state_set(role, payload, ttl=settings.live_kv_cache_ttl_sec)
        return payload
    except Exception as e:
        logger.warning("Live build_state failed for role={}: {}", role, e)
        return None


async def _fetch_lead(role: str, lead_id: int) -> dict | None:
    """Fetch a single lead row (for pushing the changed lead to the client)."""
    try:
        from core import storage as lead_storage
        from core.campaign_payload import slim_lead_for_api
        row = await lead_storage.get_lead(role, lead_id)
        if row:
            return slim_lead_for_api(dict(row), role=role)
    except Exception as e:
        logger.warning("Live fetch_lead failed: {}", e)
    return None


def _resolve_role_from_token(token: str) -> str | None:
    if not token:
        return None
    try:
        from core.auth import _decode_jwt
        payload = _decode_jwt(token)
        if payload and payload.get("role"):
            return str(payload["role"])
    except Exception:
        pass
    return None


def _resolve_role(request: Request) -> str | None:
    """Extract role from JWT (header or access_token query param)."""
    try:
        for src in (
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            request.query_params.get("access_token", ""),
            request.query_params.get("token", ""),
        ):
            role = _resolve_role_from_token(src)
            if role:
                return role
    except Exception:
        pass
    return None


async def _live_payload(
    role: str,
    event_type: str,
    *,
    lead_id: int | None = None,
    fresh: bool = False,
    extra: dict | None = None,
) -> dict:
    state = await _build_state(role, fresh=fresh)
    changed_lead = None
    if lead_id:
        changed_lead = await _fetch_lead(role, lead_id)
    pkt: dict = {
        "type": event_type,
        "state": state,
        "changed_lead": changed_lead,
    }
    if extra:
        pkt.update(extra)
    return pkt


async def _sse_payload(
    role: str,
    event_type: str,
    *,
    lead_id: int | None = None,
    fresh: bool = False,
) -> str | None:
    pkt = await _live_payload(role, event_type, lead_id=lead_id, fresh=fresh)
    if pkt.get("state") is None and event_type not in ("incoming_call", "incoming_call_completed", "upload_started", "upload_progress", "upload_complete", "ping"):
        if event_type == "state":
            return f"data: {json.dumps({'type': 'state', 'state': None, 'changed_lead': None})}\n\n"
        return None
    return f"data: {json.dumps(pkt)}\n\n"


async def _dispatch_bus_message(role: str, msg: dict) -> dict | None:
    """Map EventBus message to dashboard payload for a subscribed role."""
    msg_role = msg.get("role")
    if msg_role and msg_role != role:
        return None
    et = msg.get("type")
    if et == "lead_updated":
        return await _live_payload(role, "lead_updated", lead_id=msg.get("lead_id"), fresh=True)
    if et == "inbound_interest":
        return await _live_payload(role, "inbound_interest", lead_id=msg.get("lead_id"), fresh=True)
    if et == "incoming_call" and msg.get("role") == role:
        return {
            "type": "incoming_call",
            "role": role,
            "from_phone": msg.get("from_phone"),
            "caller_name": msg.get("caller_name"),
            "camp_id": msg.get("camp_id"),
        }
    if et == "incoming_call_completed" and msg.get("role") == role:
        pkt = await _live_payload(role, "incoming_call_completed", fresh=True)
        pkt["from_phone"] = msg.get("from_phone")
        pkt["camp_id"] = msg.get("camp_id")
        pkt["status"] = msg.get("status")
        return pkt
    if et == "whatsapp_sent" and msg.get("role") == role:
        return await _live_payload(role, "whatsapp_sent", lead_id=msg.get("lead_id"), fresh=True)
    if et == "email_sent" and msg.get("role") == role:
        return await _live_payload(role, "email_sent", lead_id=msg.get("lead_id"), fresh=True)
    if et in ("upload_started", "upload_progress", "upload_complete") and msg_role == role:
        return dict(msg)
    return None


@router.websocket("/ws/dashboard")
async def dashboard_websocket(
    websocket: WebSocket,
    role: str = Query("sales_1"),
    access_token: str = Query(""),
    token: str = Query(""),
):
    """WebSocket live feed for dashboard KPIs, lead updates, and upload progress."""
    jwt_role = _resolve_role_from_token(access_token or token)
    if jwt_role:
        role = jwt_role
    await websocket.accept()
    bus = get_event_bus()
    q = bus.subscribe()
    tick_sec = max(1.0, settings.live_sse_tick_ms / 1000.0)
    try:
        initial = await _live_payload(role, "state", fresh=True)
        await websocket.send_json(initial)
        while True:
            try:
                raw = await asyncio.wait_for(q.get(), timeout=tick_sec)
                msg = json.loads(raw)
                pkt = await _dispatch_bus_message(role, msg)
                if pkt:
                    await websocket.send_json(pkt)
            except asyncio.TimeoutError:
                tick = await _live_payload(role, "tick", fresh=False)
                if tick.get("state") is not None:
                    await websocket.send_json(tick)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Dashboard WS closed for role={}: {}", role, e)
    finally:
        bus.unsubscribe(q)


@router.get("/api/events/stream")
async def sse_stream(request: Request, role: str = Query("sales_1")):
    """Server-Sent Events stream. Pushes state on events and on a fast periodic tick."""
    jwt_role = _resolve_role(request)
    if jwt_role:
        role = jwt_role
    bus = get_event_bus()
    q = bus.subscribe()
    tick_sec = max(0.2, settings.live_sse_tick_ms / 1000.0)

    async def event_generator():
        try:
            pkt = await _sse_payload(role, "state", fresh=True)
            if pkt:
                yield pkt
            else:
                yield f"data: {json.dumps({'type': 'state', 'state': None, 'changed_lead': None})}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=tick_sec)
                    msg = json.loads(raw)
                    pkt_obj = await _dispatch_bus_message(role, msg)
                    if pkt_obj:
                        yield f"data: {json.dumps(pkt_obj)}\n\n"
                except asyncio.TimeoutError:
                    pkt = await _sse_payload(role, "tick", fresh=False)
                    if pkt:
                        yield pkt
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
