"""Meta WhatsApp Cloud API webhook + Dariaan QR / proxy pairing page."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from loguru import logger

from config import settings
from services.whatsapp_leads import (
    parse_meta_webhook_messages,
    process_whatsapp_inbound,
    send_whatsapp_project_details,
    send_whatsapp_text_message,
    wa_me_link,
)

router = APIRouter(tags=["whatsapp"])


@router.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    """Meta webhook verification (subscribe in WhatsApp → Configuration)."""
    expected = (settings.whatsapp_verify_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="WHATSAPP_VERIFY_TOKEN not set on server — add it to .env first",
        )
    if hub_mode == "subscribe" and hub_verify_token == expected:
        logger.info("WhatsApp webhook verified")
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verify token")


@router.post("/api/whatsapp/webhook")
async def whatsapp_webhook_events(request: Request):
    """Inbound Meta Cloud API messages → lead ingest + AI chatbot auto-reply."""
    if not settings.whatsapp_inbound_leads_enabled:
        return {"status": "ignored", "reason": "WHATSAPP_INBOUND_LEADS_ENABLED=0"}

    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid json"}

    messages = parse_meta_webhook_messages(body)
    if not messages:
        return {"status": "ok", "processed": 0}

    results = []
    for msg in messages:
        from_phone = msg["from"]
        profile_name = msg.get("profile_name") or ""
        message_text = msg.get("text") or ""
        wa_message_id = msg.get("message_id") or ""
        result = {}
        try:
            # Step 1: Process lead (match/create/update lead record)
            result = await process_whatsapp_inbound(
                from_phone=from_phone,
                profile_name=profile_name,
                message_text=message_text,
                wa_message_id=wa_message_id,
            )
        except Exception as e:
            logger.exception("WhatsApp lead ingest failed: {}", e)
            result = {"error": str(e)}

        # Step 2: AI chatbot auto-reply with conversation memory
        try:
            from services.whatsapp_conversation import add_message, analyze_inbound_message
            from services.whatsapp_leads import send_whatsapp_project_details

            # Store user message in conversation memory
            add_message(from_phone, "user", message_text)

            # Analyze with Gemini (uses conversation history + RAG + lead context)
            ai_result = await analyze_inbound_message(from_phone, message_text)

            # Send AI text response if warranted
            if ai_result.get("should_respond") and ai_result.get("response"):
                from services.whatsapp_leads import send_whatsapp_text_message
                await send_whatsapp_text_message(from_phone, ai_result["response"])
                # Store assistant response in conversation memory
                add_message(from_phone, "assistant", ai_result["response"])
                result["ai_responded"] = True
                result["ai_response"] = ai_result["response"][:120]
            else:
                result["ai_responded"] = False

            # Optionally send brochure/project details
            if ai_result.get("send_project_details"):
                await send_whatsapp_project_details(from_phone)
                result["ai_sent_details"] = True

            # Publish event for dashboard reflection
            try:
                from core.events import get_event_bus
                await get_event_bus().publish("whatsapp_inbound", role="sales_1",
                    phone=from_phone, message=message_text[:100],
                    ai_replied=result.get("ai_responded", False))
            except Exception:
                pass

        except Exception as e:
            logger.warning("WhatsApp AI chatbot failed for {}: {}", from_phone, e)
            result["ai_error"] = str(e)

        results.append(result)

    return {"status": "ok", "processed": len(results), "results": results}


@router.get("/dariaan/whatsapp/qr.png")
async def dariaan_whatsapp_qr_png():
    """Downloadable QR — OpenWA live pairing QR (linked WhatsApp) or wa.me fallback."""
    from fastapi.responses import FileResponse, RedirectResponse, Response
    from config import FRONTEND_DIR

    # Prefer OpenWA live session QR (the QR you scan to LINK the bot's WhatsApp).
    if getattr(settings, "openwa_enabled", False):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                from services.whatsapp_leads import _get_openwa_session_uuid
                uuid = await _get_openwa_session_uuid(client)
                if uuid:
                    base = (settings.openwa_api_url or "http://127.0.0.1:2785").rstrip("/")
                    resp = await client.get(
                        f"{base}/api/sessions/{uuid}/qr",
                        headers={"X-API-Key": settings.openwa_api_key},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        qr_code = data.get("qrCode") or ""
                        if qr_code.startswith("data:image"):
                            import base64 as _b64
                            import re as _re
                            b64 = _re.sub(r"^data:image/\w+;base64,", "", qr_code)
                            return Response(
                                content=_b64.b64decode(b64),
                                media_type="image/png",
                                headers={"Cache-Control": "no-store, max-age=0"},
                            )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("OpenWA QR png fallback: {}", e)

    if settings.whatsapp_proxy_enabled:
        try:
            base = (settings.whatsapp_proxy_url or "http://127.0.0.1:3001").rstrip("/")
            async with httpx.AsyncClient(timeout=15.0) as client:
                st = await client.get(f"{base}/status")
                if st.status_code == 200 and st.json().get("authenticated"):
                    raise HTTPException(status_code=204, detail="Already linked")
                qr = await client.get(f"{base}/qr")
                if qr.status_code == 200:
                    return Response(
                        content=qr.content,
                        media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Proxy QR png fallback: {}", e)

    static_png = FRONTEND_DIR / "static" / "dariaan_whatsapp_qr.png"
    if static_png.is_file():
        return FileResponse(
            static_png,
            media_type="image/png",
            filename="dariaan_whatsapp_qr.png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    number = settings.dariaan_whatsapp_number.strip()
    prefill = settings.dariaan_whatsapp_qr_message.strip()
    link = wa_me_link(number, prefill)
    return RedirectResponse(
        url=f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote(link, safe='')}",
        status_code=302,
    )


def _proxy_pairing_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Dariaan — Link WhatsApp</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 440px; margin: 40px auto; padding: 24px; text-align: center; color: #1a1a1a; }
    h1 { font-size: 1.35rem; margin-bottom: 0.25rem; }
    .sub { color: #555; font-size: 0.95rem; margin-bottom: 1rem; line-height: 1.4; }
    .warn { background: #fff8e6; border: 1px solid #f0d78c; border-radius: 8px; padding: 10px; font-size: 12px; color: #664; margin-bottom: 1rem; text-align: left; }
    #status { font-size: 14px; margin: 12px 0; padding: 10px; border-radius: 8px; background: #f4f4f4; }
    #status.ok { background: #e8f8ee; color: #186a3b; }
    img { border: 1px solid #ddd; border-radius: 12px; max-width: 320px; width: 100%; }
    .steps { font-size: 12px; color: #666; text-align: left; margin-top: 1.5rem; line-height: 1.5; }
  </style>
</head>
<body>
  <h1>Dariaan — Link WhatsApp</h1>
  <p class="sub">Scan with your phone to connect WhatsApp. New messages → lead list → AI calls Vernika.</p>
  <div class="warn"><strong>Unofficial Web link.</strong> Use a spare number. Meta may ban automated personal WhatsApp. Inbound read + low outbound only.</div>
  <div id="status">Checking connection…</div>
  <img id="qr" alt="WhatsApp pairing QR" width="320" height="320"/>
  <div class="steps">
    <strong>Steps:</strong>
    <ol>
      <li>Open WhatsApp on your phone</li>
      <li>Menu → <strong>Linked devices</strong> → <strong>Link a device</strong></li>
      <li>Scan the QR above</li>
    </ol>
  </div>
  <script>
    const qrEl = document.getElementById('qr');
    const stEl = document.getElementById('status');
    function setStatus(text, ok) {
      stEl.textContent = text;
      stEl.className = ok ? 'ok' : '';
    }
    async function refresh() {
      try {
        const st = await fetch('/api/whatsapp/proxy/status');
        const data = await st.json();
        if (data.authenticated && data.connected) {
          setStatus('Connected: ' + (data.phone || data.pushname || 'WhatsApp linked'), true);
          qrEl.style.display = 'none';
          return;
        }
        setStatus(data.has_qr ? 'Scan QR with WhatsApp → Linked devices' : 'Starting sidecar… refresh in a few seconds');
        qrEl.style.display = '';
        qrEl.src = '/api/whatsapp/proxy/qr?t=' + Date.now();
      } catch (e) {
        setStatus('Sidecar not reachable — is whatsapp-proxy running on port 3001?');
        qrEl.style.display = 'none';
      }
    }
    refresh();
    setInterval(refresh, 4000);
  </script>
</body>
</html>"""


@router.get("/dariaan/whatsapp", response_class=HTMLResponse)
async def dariaan_whatsapp_qr_page():
    """Pairing page: OpenWA linked-device QR (primary) or proxy/wa.me fallback."""
    if getattr(settings, "openwa_enabled", False):
        session_status = ""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                from services.whatsapp_leads import _get_openwa_session_uuid
                uuid = await _get_openwa_session_uuid(client)
                if uuid:
                    base = (settings.openwa_api_url or "http://127.0.0.1:2785").rstrip("/")
                    resp = await client.get(
                        f"{base}/api/sessions/{uuid}",
                        headers={"X-API-Key": settings.openwa_api_key},
                    )
                    if resp.status_code == 200:
                        session_status = str(resp.json().get("status") or "unknown")
        except Exception as e:
            logger.warning("OpenWA status page probe failed: {}", e)
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Solitaire Unity — WhatsApp Bot Link</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 40px auto; padding: 24px; text-align: center; color: #1a1a1a; }}
    h1 {{ font-size: 1.35rem; margin-bottom: 0.25rem; }}
    .sub {{ color: #555; font-size: 0.95rem; margin-bottom: 1.5rem; }}
    img {{ border: 1px solid #ddd; border-radius: 12px; }}
    .status {{ font-size: 0.9rem; margin: 1rem 0; }}
    .ready {{ color: #16a34a; font-weight: 600; }}
    .steps {{ text-align: left; font-size: 0.9rem; line-height: 1.6; background: #f7f7f7; padding: 14px 16px; border-radius: 10px; }}
  </style>
</head>
<body>
  <h1>Solitaire Unity — WhatsApp Bot</h1>
  <p class="sub">Scan this QR to link the bot's WhatsApp account (Linked Devices).</p>
  <img src="/dariaan/whatsapp/qr.png" width="320" height="320" alt="OpenWA pairing QR"/>
  <p class="status">Session status: <span class="ready">{session_status or 'unknown'}</span></p>
  <div class="steps">
    <strong>To link:</strong><br/>
    1. Open WhatsApp → Settings → Linked Devices<br/>
    2. Tap "Link a Device"<br/>
    3. Scan this QR code<br/>
    4. Once "ready", the bot replies to every message 24/7.
  </div>
</body>
</html>"""
        return HTMLResponse(content=html, headers={"Cache-Control": "no-store, max-age=0"})

    if settings.whatsapp_proxy_enabled:
        return HTMLResponse(content=_proxy_pairing_html(), headers={"Cache-Control": "no-store, max-age=0"})

    number = settings.dariaan_whatsapp_number.strip()
    prefill = settings.dariaan_whatsapp_qr_message.strip()
    link = wa_me_link(number, prefill)
    if not link:
        raise HTTPException(status_code=503, detail="DARIAAN_WHATSAPP_NUMBER not configured")

    digits_display = number if number.startswith("+") else f"+{number.lstrip('+')}"
    qr_img = f"https://api.qrserver.com/v1/create-qr-code/?size=320x320&data={quote(link, safe='')}"
    webhook_hint = (settings.vobiz_public_base_url or settings.server_url or "").rstrip("/")
    webhook_url = f"{webhook_hint}/api/whatsapp/webhook" if webhook_hint else "/api/whatsapp/webhook"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Dariaan — WhatsApp QR</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px; margin: 40px auto; padding: 24px; text-align: center; color: #1a1a1a; }}
    h1 {{ font-size: 1.35rem; margin-bottom: 0.25rem; }}
    .sub {{ color: #555; font-size: 0.95rem; margin-bottom: 1.5rem; }}
    img {{ border: 1px solid #ddd; border-radius: 12px; }}
    .num {{ font-size: 1.1rem; font-weight: 600; margin: 1rem 0; }}
    a.btn {{ display: inline-block; margin-top: 1rem; padding: 12px 20px; background: #25D366; color: #fff; text-decoration: none; border-radius: 8px; font-weight: 600; }}
    .hint {{ font-size: 0.75rem; color: #888; margin-top: 2rem; text-align: left; line-height: 1.4; }}
    code {{ font-size: 0.7rem; word-break: break-all; }}
  </style>
</head>
<body>
  <h1>Dariaan</h1>
  <p class="sub">Customer QR — scan to message on WhatsApp (enable WHATSAPP_PROXY_ENABLED=1 for account linking).</p>
  <img src="/static/dariaan_whatsapp_qr.png" width="320" height="320" alt="WhatsApp QR code"
       onerror="this.src='{qr_img}'"/>
  <p class="num">{digits_display}</p>
  <a class="btn" href="{link}" target="_blank" rel="noopener">Open WhatsApp</a>
  <p class="hint">
    <strong>Meta API webhook:</strong><br/>
    <code>{webhook_url}</code>
  </p>
</body>
</html>"""
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/api/whatsapp/botspice-status")
async def whatsapp_botspice_status():
    """Check BotSpice template API configuration (no message sent)."""
    from services.whatsapp_leads import botspice_config_status

    return await botspice_config_status()


@router.post("/api/whatsapp/botspice-test")
async def whatsapp_botspice_test(payload: dict):
    """Send a BotSpice template test message.

    Payload: {"phone": "+91...", "template": "solitaire_unity_image", "disposition": "interested"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    disposition = (payload.get("disposition") or "interested").strip()
    from services.whatsapp_leads import send_whatsapp_project_details

    result = await send_whatsapp_project_details(
        phone,
        summary=(payload.get("summary") or "BotSpice test — Solitaire Unity").strip(),
        lead_name=(payload.get("name") or "Test").strip(),
        disposition=disposition,
    )
    if not result.get("sent"):
        detail = result.get("error")
        if not detail and isinstance(result.get("details"), list):
            for _, row in result["details"]:
                if isinstance(row, dict) and row.get("error"):
                    detail = row["error"]
                    break
        raise HTTPException(status_code=502, detail=detail or "BotSpice send failed")
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/botspice-test-all")
async def whatsapp_botspice_test_all(payload: dict):
    """Send all three BotSpice templates (image, document, video) to a test phone.

    Payload: {"phone": "917204955388"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    from services.whatsapp_leads import test_all_botspice_templates

    result = await test_all_botspice_templates(phone)
    if not result.get("sent"):
        failed = [r for r in result.get("results", []) if not r.get("sent")]
        detail = failed[0].get("error") if failed else "BotSpice send failed"
        raise HTTPException(status_code=502, detail=detail)
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/send-dummy")
async def whatsapp_send_dummy(payload: dict):
    """Send dummy Solitaire Unity project details to test phone for testing.

    Payload: {"phone": "+918065480885"} or {"phone": "918065480885"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    result = await send_whatsapp_project_details(phone, "Test — Solitaire Unity Project Details")
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/send-details")
async def whatsapp_send_details(payload: dict):
    """Send project details to any phone number via WhatsApp Cloud API.

    Payload: {"phone": "+918065480885", "summary": "Brochure & Price Sheet (optional)"}
    """
    phone = (payload.get("phone") or "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")
    summary = (payload.get("summary") or "Solitaire Unity — Project Details").strip()
    result = await send_whatsapp_project_details(phone, summary)
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}


@router.post("/api/whatsapp/send-message")
async def whatsapp_send_message(payload: dict):
    """Send a custom text message to any phone number via WhatsApp Cloud API.

    Payload: {"phone": "+918065480885", "text": "Your message here"}
    """
    phone = (payload.get("phone") or "").strip()
    text = (payload.get("text") or "").strip()
    if not phone or not text:
        raise HTTPException(status_code=400, detail="phone and text are required")
    result = await send_whatsapp_text_message(phone, text)
    if not result.get("sent"):
        raise HTTPException(status_code=502, detail=result.get("error", "send failed"))
    return {"status": "ok", "result": result}


# ── OpenWA Webhook (receives inbound WhatsApp messages from open-wa gateway) ──

@router.post("/api/openwa/webhook")
async def openwa_webhook(request: Request):
    """Inbound OpenWA gateway messages → lead ingest + AI chatbot auto-reply."""
    if not (getattr(settings, "openwa_enabled", False) or False):
        return {"status": "ignored", "reason": "OPENWA_ENABLED=0"}

    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid json"}

    # OpenWA sends messages in the format:
    # { "event": "message", "data": { "from": "...", "body": "...", ... } }
    event_type = str(body.get("event") or body.get("type") or "").lower()
    if event_type not in ("message", "incoming_message", "message_create", "message.received"):
        return {"status": "ok", "reason": f"ignored event: {event_type}"}

    data = body.get("data") or body.get("message") or {}
    from_phone = str(data.get("from") or data.get("author") or data.get("sender") or "").strip()
    message_text = str(data.get("body") or data.get("text") or data.get("content") or "").strip()
    wa_message_id = str(data.get("id") or data.get("messageId") or data.get("_data", {}).get("id") or "").strip()
    profile_name = str(data.get("notifyName") or data.get("pushname") or body.get("profile_name") or "").strip()

    if not from_phone or not message_text:
        return {"status": "ignored", "reason": "missing from or body"}

    from services.whatsapp_leads import process_whatsapp_inbound

    try:
        result = await process_whatsapp_inbound(
            from_phone=from_phone,
            profile_name=profile_name,
            message_text=message_text,
            wa_message_id=wa_message_id,
        )
    except Exception as e:
        logger.exception("OpenWA lead ingest failed: {}", e)
        result = {"error": str(e)}

    # AI chatbot auto-reply with conversation memory
    try:
        from services.whatsapp_conversation import add_message, analyze_inbound_message

        add_message(from_phone, "user", message_text)
        ai_result = await analyze_inbound_message(from_phone, message_text)
        reply_text = ai_result.get("response") or ""
        if ai_result.get("should_respond") and reply_text:
            await send_whatsapp_text_message(from_phone, reply_text)
            add_message(from_phone, "assistant", reply_text)
            result["ai_responded"] = True
        else:
            result["ai_responded"] = False
        if ai_result.get("send_project_details"):
            await send_whatsapp_project_details(from_phone, lead_name=profile_name)
            result["ai_sent_details"] = True
        result["site_visit_agreed"] = bool(ai_result.get("site_visit_agreed"))
    except Exception as e:
        logger.exception("OpenWA AI reply failed: {}", e)

    return {"status": "ok", "processed": True, "from": from_phone}
