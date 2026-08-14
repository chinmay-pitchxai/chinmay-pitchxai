"""WhatsApp proxy bridge — OpenWA API Gateway (replaces old whatsapp-web.js sidecar)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel, Field

from config import settings
from services.whatsapp_leads import (
    process_whatsapp_inbound,
    send_whatsapp_project_details,
)

router = APIRouter(tags=["whatsapp-proxy"])


class ProxyInboundMessage(BaseModel):
    from_phone: str = Field(..., description="E.164-ish phone from sidecar")
    profile_name: str = ""
    text: str = ""
    message_id: str = ""
    timestamp: int | None = None
    from_wa_id: str = ""


def _verify_proxy_secret(x_proxy_secret: str | None) -> None:
    expected = (settings.whatsapp_proxy_secret or "").strip()
    if not expected:
        return
    if (x_proxy_secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid proxy secret")


def _openwa_base() -> str:
    return (settings.openwa_api_url or "http://127.0.0.1:2785").rstrip("/")


@router.post("/api/whatsapp/proxy/message")
async def whatsapp_proxy_inbound(
    body: dict,
    x_proxy_secret: str | None = Header(None, alias="X-Proxy-Secret"),
):
    """Inbound relay from OpenWA webhook or old whatsapp-web.js sidecar."""
    _verify_proxy_secret(x_proxy_secret)

    # Try to parse as OpenWA webhook payload first
    from_phone = ""
    profile_name = ""
    message_text = ""
    wa_message_id = ""
    from_me = False

    if "event" in body and body.get("event") == "message.received":
        data = body.get("data") or {}
        from_me = bool(data.get("fromMe", False))
        if from_me:
            return {"status": "ignored", "reason": "fromMe — skipping own messages"}

        raw_from = str(data.get("from", ""))
        from_phone = raw_from.split("@")[0] if "@" in raw_from else raw_from
        message_text = str(data.get("body", ""))
        wa_message_id = str(data.get("id", ""))
        contact = data.get("contact") or {}
        profile_name = str(contact.get("name") or contact.get("pushName", ""))
    else:
        # Fall back to old sidecar format
        msg = ProxyInboundMessage(**body)
        from_phone = msg.from_phone
        profile_name = msg.profile_name
        message_text = msg.text
        wa_message_id = msg.message_id

    if not from_phone or not message_text:
        return {"status": "ignored", "reason": "empty phone or text"}

    try:
        result = await process_whatsapp_inbound(
            from_phone=from_phone,
            profile_name=profile_name,
            message_text=message_text,
            wa_message_id=wa_message_id,
        )
        return {"status": "ok", **result}
    except Exception as e:
        logger.exception("Proxy inbound failed: {}", e)
        raise HTTPException(status_code=500, detail="Failed to ingest WhatsApp message") from e


@router.post("/api/whatsapp/proxy/analyze")
async def whatsapp_proxy_analyze(
    body: dict,
    x_proxy_secret: str | None = Header(None, alias="X-Proxy-Secret"),
):
    """Analyze inbound message + auto-respond via AI.

    Called after message ingestion to optionally respond based on intent.
    Body: {"from_phone": "...", "text": "..."}
    """
    _verify_proxy_secret(x_proxy_secret)

    from_phone = (body.get("from_phone") or "").strip()
    text = (body.get("text") or "").strip()
    if not from_phone or not text:
        return {"status": "ignored", "reason": "empty phone or text"}

    from services.whatsapp_conversation import (
        add_message,
        analyze_inbound_message,
        get_history,
    )

    add_message(from_phone, "user", text)
    analysis = await analyze_inbound_message(from_phone, text)

    if not analysis.get("should_respond"):
        return {"status": "ok", "analyzed": True, "action": "no_response"}

    response_text = analysis.get("response", "")
    intent = analysis.get("intent", "other")
    send_project_details = analysis.get("send_project_details", False)

    actions = []
    if response_text:
        from services.whatsapp_leads import send_whatsapp_text_message

        send_result = await send_whatsapp_text_message(from_phone, response_text)
        if send_result.get("sent"):
            add_message(from_phone, "assistant", response_text)
            actions.append("sent_response")
        else:
            actions.append("response_failed")

    if send_project_details:
        details_result = await send_whatsapp_project_details(from_phone)
        if details_result.get("sent"):
            add_message(from_phone, "assistant", "Sent project details brochure")
            actions.append("sent_details")
        else:
            actions.append("details_failed")

    return {
        "status": "ok",
        "analyzed": True,
        "intent": intent,
        "actions": actions,
    }


@router.get("/api/whatsapp/proxy/status")
async def whatsapp_proxy_status():
    """OpenWA session status (authenticated / phone / QR ready)."""
    if not settings.openwa_enabled:
        return {"enabled": False, "authenticated": False}
    api_key = settings.openwa_api_key.strip()
    if not api_key:
        return {"enabled": True, "error": "OPENWA_API_KEY not configured"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            from services.whatsapp_leads import _get_openwa_session_uuid
            uuid = await _get_openwa_session_uuid(client)
            if not uuid:
                return {"enabled": True, "connected": False, "error": "Could not resolve OpenWA session UUID"}
            resp = await client.get(
                f"{_openwa_base()}/api/sessions/{uuid}",
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "unknown")
            return {
                "enabled": True,
                "connected": status == "ready",
                "authenticated": status == "ready",
                "has_qr": status == "qr_ready",
                "status": status,
                "phone": data.get("phone", ""),
                "pushname": data.get("pushName", ""),
            }
    except Exception as e:
        logger.warning("OpenWA status failed: {}", e)
        return {"enabled": True, "connected": False, "error": str(e)}


@router.get("/api/whatsapp/proxy/qr")
async def whatsapp_proxy_qr():
    """OpenWA pairing QR code (PNG)."""
    if not settings.openwa_enabled:
        raise HTTPException(status_code=503, detail="OPENWA_ENABLED=0")
    api_key = settings.openwa_api_key.strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENWA_API_KEY not configured")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            from services.whatsapp_leads import _get_openwa_session_uuid
            uuid = await _get_openwa_session_uuid(client)
            if not uuid:
                raise HTTPException(status_code=503, detail="Could not resolve OpenWA session UUID")
            resp = await client.get(
                f"{_openwa_base()}/api/sessions/{uuid}/qr",
                headers={"X-API-Key": api_key},
            )
            if resp.status_code == 204:
                return Response(status_code=204)
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text[:200])
            data = resp.json()
            qr_code = data.get("qrCode", "")
            if qr_code.startswith("data:image"):
                import base64
                import re

                b64 = re.sub(r"^data:image/\w+;base64,", "", qr_code)
                img_bytes = base64.b64decode(b64)
                return Response(
                    content=img_bytes,
                    media_type="image/png",
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
            raise HTTPException(status_code=503, detail="QR code not available")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("OpenWA QR fetch failed: {}", e)
        raise HTTPException(status_code=503, detail="OpenWA QR not available") from e


@router.post("/api/whatsapp/proxy/send")
async def whatsapp_proxy_send(request: Request):
    """Outgoing text via OpenWA (internal / console use)."""
    if not settings.openwa_enabled:
        raise HTTPException(status_code=503, detail="OPENWA_ENABLED=0")
    body = await request.json()
    to_phone = (body.get("to") or body.get("phone") or "").strip()
    text = (body.get("text") or body.get("message") or "").strip()
    if not to_phone or not text:
        raise HTTPException(status_code=400, detail="to and text required")

    from services.whatsapp_leads import send_whatsapp_text_message

    result = await send_whatsapp_text_message(to_phone, text)
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return result
