"""WhatsApp client — sends messages via Meta Cloud API or OpenWA REST API."""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from config import settings

def _openwa_config() -> tuple[str, str, str]:
    """Return the single canonical OpenWA configuration used by the app.

    Config previously read ``OPENWA_BASE_URL`` here while the rest of the
    application exposed ``OPENWA_API_URL``.  That silently sent messages to
    localhost even when Configuration showed a different gateway.
    """
    return (
        settings.openwa_api_url.rstrip("/"),
        settings.openwa_api_key,
        settings.openwa_session_id,
    )


async def send_text(
    phone: str,
    text: str,
    *,
    session_id: str = "",
) -> dict:
    """Send a text message via OpenWA. Returns the API response."""
    base_url, api_key, configured_session = _openwa_config()
    sid = session_id or configured_session
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits or not text:
        return {"error": "invalid_phone_or_text"}

    if not sid:
        return {"error": "missing_openwa_session_id"}
    url = f"{base_url}/api/sessions/{sid}/messages/send-text"
    payload = {"chatId": f"{digits}@c.us", "text": text}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("WA sent to {}: {}", digits, text[:80])
                return resp.json()
            logger.warning("WA send failed {}: {}", resp.status_code, resp.text[:200])
            return {"error": resp.status_code, "detail": resp.text[:500]}
    except Exception as e:
        logger.warning("WA send error: {}", e)
        return {"error": str(e)}


async def send_template(phone: str, template_name: str, lang: str = "en") -> dict:
    """Send a WhatsApp template message."""
    base_url, api_key, sid = _openwa_config()
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return {"error": "invalid_phone"}

    if not sid:
        return {"error": "missing_openwa_session_id"}
    url = f"{base_url}/api/sessions/{sid}/messages/send-template"
    payload = {"chatId": f"{digits}@c.us", "template": {"name": template_name, "lang": lang}}
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            return resp.json() if resp.status_code < 400 else {"error": resp.status_code}
    except Exception as e:
        return {"error": str(e)}


async def send_image(phone: str, image_url: str, caption: str = "") -> dict:
    """Send an image with optional caption.

    Uses OpenWA's flat SendMediaMessageDto: { chatId, url, mimetype, caption }.
    """
    base_url, api_key, sid = _openwa_config()
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return {"error": "invalid_phone"}

    if not sid:
        return {"error": "missing_openwa_session_id"}
    url = f"{base_url}/api/sessions/{sid}/messages/send-image"
    payload = {
        "chatId": f"{digits}@c.us",
        "url": image_url,
        "mimetype": "image/jpeg",
        "caption": caption,
    }
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("WA image sent to {}: {}", digits, image_url[:80])
                return resp.json()
            logger.warning("WA image send failed {}: {}", resp.status_code, resp.text[:200])
            return {"error": resp.status_code, "detail": resp.text[:500]}
    except Exception as e:
        return {"error": str(e)}


async def send_document(
    phone: str,
    document_url: str,
    filename: str = "document.pdf",
    caption: str = "",
    *,
    session_id: str = "",
) -> dict:
    """Send a document (PDF, etc.) via OpenWA. Returns the API response."""
    base_url, api_key, configured_session = _openwa_config()
    sid = session_id or configured_session
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits or not document_url:
        return {"error": "invalid_phone_or_url"}

    if not sid:
        return {"error": "missing_openwa_session_id"}
    url = f"{base_url}/api/sessions/{sid}/messages/send-document"
    payload = {
        "chatId": f"{digits}@c.us",
        "url": document_url,
        "filename": filename,
        "mimetype": "application/pdf",
        "caption": caption,
    }
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("WA document sent to {}: {}", digits, filename)
                return resp.json()
            logger.warning("WA document send failed {}: {}", resp.status_code, resp.text[:200])
            return {"error": resp.status_code, "detail": resp.text[:500]}
    except Exception as e:
        logger.warning("WA document send error: {}", e)
        return {"error": str(e)}


async def get_session_status() -> dict:
    """Check OpenWA session status."""
    base_url, api_key, sid = _openwa_config()
    if not sid:
        return {"error": "missing_openwa_session_id"}
    url = f"{base_url}/api/sessions/{sid}"
    headers = {"X-API-Key": api_key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            return resp.json() if resp.status_code < 400 else {"error": resp.status_code}
    except Exception as e:
        return {"error": str(e)}


# ── Meta WhatsApp Cloud API ───────────────────────────────────────────────

META_GRAPH_URL = "https://graph.facebook.com/v21.0"


async def send_text_meta(phone: str, text: str) -> dict:
    """Send a text message via Meta WhatsApp Cloud API."""
    pid = settings.whatsapp_phone_number_id.strip()
    token = settings.whatsapp_access_token.strip()
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not pid or not token or not digits or not text:
        return {"error": "missing_credentials_or_phone_or_text"}

    url = f"{META_GRAPH_URL}/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": digits,
        "type": "text",
        "text": {"body": text},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("Meta text sent to {}: {}", digits, text[:80])
                return resp.json()
            logger.warning("Meta text send failed {}: {}", resp.status_code, resp.text[:200])
            return {"error": resp.status_code, "detail": resp.text[:500]}
    except Exception as e:
        logger.warning("Meta text send error: {}", e)
        return {"error": str(e)}


async def send_document_meta(
    phone: str,
    document_url: str,
    caption: str = "",
    filename: str = "document.pdf",
) -> dict:
    """Send a document (PDF) via Meta WhatsApp Cloud API."""
    pid = settings.whatsapp_phone_number_id.strip()
    token = settings.whatsapp_access_token.strip()
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not pid or not token or not digits or not document_url:
        return {"error": "missing_credentials_or_phone_or_url"}

    url = f"{META_GRAPH_URL}/{pid}/messages"
    payload: dict = {
        "messaging_product": "whatsapp",
        "to": digits,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
        },
    }
    if caption:
        payload["document"]["caption"] = caption
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("Meta document sent to {}: {}", digits, filename)
                return resp.json()
            logger.warning("Meta document send failed {}: {}", resp.status_code, resp.text[:200])
            return {"error": resp.status_code, "detail": resp.text[:500]}
    except Exception as e:
        logger.warning("Meta document send error: {}", e)
        return {"error": str(e)}
