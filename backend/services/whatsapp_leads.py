"""WhatsApp automation — BotSpice templates (video/audio headers) + Meta Cloud fallback.

Post-call packages use disposition-specific BotSpice templates, then supplementary
image/video/PDF assets via Meta when configured.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config import settings
from core.utils import _norm_phone_str


DARIAAN_ROLE = "sales_1"

_CAMPAIGN_ROLES = ("sales_1",)

_EMAIL_WA_PREFILL = "Hi, I'm interested in Solitaire Unity. Please share more details."


def resolve_whatsapp_business_number() -> str:
    """Business WhatsApp line for wa.me links (not VoIP outbound dialer numbers)."""
    return (settings.botspice_whatsapp_number or settings.whatsapp_business_number or "").strip()


def wa_me_link(number_e164: str, prefill: str = "") -> str:
    """Build wa.me deep link for QR codes (digits only, no +)."""
    digits = "".join(c for c in (number_e164 or "") if c.isdigit())
    if not digits:
        return ""
    from urllib.parse import quote
    base = f"https://wa.me/{digits}"
    msg = (prefill or settings.dariaan_whatsapp_qr_message or "").strip()
    if msg:
        return f"{base}?text={quote(msg)}"
    return base


async def upsert_dariaan_lead_from_whatsapp(
    *,
    from_phone: str,
    profile_name: str = "",
    message_text: str = "",
    wa_message_id: str = "",
) -> tuple[int, bool]:
    """Insert or update a Dariaan lead from WhatsApp. Returns (lead_id, is_new)."""
    from core.storage import find_lead_by_phone, _get_conn
    norm = _norm_phone_str(from_phone)
    if not norm:
        raise ValueError(f"Invalid WhatsApp sender phone: {from_phone!r}")
    display_name = (profile_name or "").strip() or "WhatsApp Lead"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    wa_meta = {
        "source": "whatsapp",
        "whatsapp_last_message": (message_text or "")[:2000],
        "whatsapp_last_message_id": wa_message_id or "",
        "whatsapp_last_at": now_iso,
    }
    existing = await find_lead_by_phone(DARIAAN_ROLE, norm)
    conn = _get_conn()
    if existing:
        lead_id = int(existing["id"])
        try:
            extra = json.loads(existing.get("extra") or "{}")
        except json.JSONDecodeError:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        extra.update(wa_meta)
        name = (existing.get("name") or "").strip()
        if name.lower() in ("", "unknown", "whatsapp lead") and display_name:
            name = display_name
        conn.execute(
            """
            UPDATE leads SET
                name = ?, extra = ?,
                status = CASE WHEN status IN ('failed','not_interested') THEN 'pending' ELSE status END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (name, json.dumps(extra, ensure_ascii=False), lead_id),
        )
        conn.commit()
        logger.info("WhatsApp lead updated id={} phone={}", lead_id, norm)
        return lead_id, False
    extra = dict(wa_meta)
    cur = conn.execute(
        """INSERT INTO leads (role, name, phone, email, company, details, extra, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (DARIAAN_ROLE, display_name, norm, "", "", (message_text or "")[:500],
         json.dumps(extra, ensure_ascii=False)),
    )
    conn.commit()
    lead_id = int(cur.lastrowid)
    logger.info("WhatsApp new lead id={} phone={} name={!r}", lead_id, norm, display_name)
    return lead_id, True


async def process_dariaan_whatsapp_inbound(
    *,
    from_phone: str,
    profile_name: str = "",
    message_text: str = "",
    wa_message_id: str = "",
) -> dict[str, Any]:
    """Shared ingest: upsert lead, optional auto-reply, optional auto-dial."""
    if not settings.whatsapp_inbound_leads_enabled:
        return {"ignored": True, "reason": "WHATSAPP_INBOUND_LEADS_ENABLED=0"}
    lead_id, is_new = await upsert_dariaan_lead_from_whatsapp(
        from_phone=from_phone, profile_name=profile_name,
        message_text=message_text, wa_message_id=wa_message_id,
    )
    out: dict[str, Any] = {"lead_id": lead_id, "new": is_new}
    if is_new:
        # AI chatbot in the Meta webhook handler now sends the response
        out["auto_dial"] = await trigger_dariaan_auto_dial()
    return out


async def trigger_dariaan_auto_dial() -> dict[str, Any]:
    """Start Dariaan campaign worker if idle so new WhatsApp leads get called."""
    if not settings.whatsapp_auto_dial_dariaan:
        return {"started": False, "reason": "WHATSAPP_AUTO_DIAL_DARIAAN=0"}
    import asyncio
    from core.state import _CAMPAIGN_TASKS
    from core.storage import set_campaign_want_running
    from core.worker import _campaign_worker_role, _schedule_preflight
    role = DARIAAN_ROLE
    run = _CAMPAIGN_TASKS.get(role)
    if run and not run.done():
        return {"started": False, "reason": "campaign_already_running"}
    err = await _schedule_preflight(role)
    if err:
        logger.warning("WhatsApp auto-dial skipped: {}", err)
        return {"started": False, "reason": err}
    await set_campaign_want_running(role, True)
    _CAMPAIGN_TASKS[role] = asyncio.create_task(
        _campaign_worker_role(role), name=f"whatsapp-auto-dial-{role}",
    )
    logger.info("WhatsApp inbound -> started Dariaan campaign for auto-dial")
    return {"started": True, "role": role}


def _classify_inbound_reply(message_text: str) -> tuple[str, str]:
    """Return (reply_type, source): interested | callback | '', and source tag."""
    text = (message_text or "").strip()
    if not text:
        return "", ""
    low = text.lower()
    if "not interested" in low or "no thanks" in low or "stop" in low:
        return "", ""

    if _EMAIL_WA_PREFILL.lower() in low or "interested in solitaire unity" in low:
        return "interested", "email_whatsapp"

    callback_hits = (
        "call me back", "call back", "callback", "call later", "ring me", "phone me",
        "call me tomorrow", "call tomorrow", "evening call", "morning call",
        "baad mein call", "kal call", "phir call", "wapas call", "dobara call",
        "busy abhi", "busy now", "later call", "after some time",
    )
    if any(k in low for k in callback_hits):
        return "callback", "whatsapp"

    from services.transcript_interest import soft_interest_in_text
    if soft_interest_in_text(text):
        return "interested", "whatsapp"

    if any(k in low for k in (
        "yes", "interested", "site visit", "buy", "purchase", "book", "visit",
        "हाँ", "हां", "जी", "ठीक", "ok", "okay",
    )):
        return "interested", "whatsapp"

    return "", ""


def _detect_inbound_interest(message_text: str) -> tuple[bool, str]:
    """Backward-compatible wrapper — True only for interested replies."""
    reply_type, source = _classify_inbound_reply(message_text)
    return reply_type == "interested", source


async def process_campaign_whatsapp_reply(
    *,
    from_phone: str,
    profile_name: str = "",
    message_text: str = "",
    wa_message_id: str = "",
) -> dict[str, Any]:
    """Match inbound WhatsApp to campaign lead; flag interested or callback replies."""
    from core.storage import find_lead_by_phone_any_role, record_inbound_whatsapp_reply, _get_conn

    lead = await find_lead_by_phone_any_role(from_phone)
    if not lead:
        return {"matched": False}

    lead_id = int(lead["id"])
    role = str(lead.get("role") or "")
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        extra = json.loads(lead.get("extra") or "{}")
    except json.JSONDecodeError:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra.update({
        "whatsapp_last_message": (message_text or "")[:2000],
        "whatsapp_last_message_id": wa_message_id or "",
        "whatsapp_last_at": now_iso,
    })
    if profile_name:
        extra["whatsapp_profile_name"] = profile_name.strip()

    conn = _get_conn()
    conn.execute(
        "UPDATE leads SET extra = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(extra, ensure_ascii=False), lead_id),
    )
    conn.commit()

    reply_type, source = _classify_inbound_reply(message_text)
    if reply_type in ("interested", "callback"):
        result = await record_inbound_whatsapp_reply(
            lead_id,
            reply_type=reply_type,
            source=source,
            message_text=message_text,
            profile_name=profile_name,
        )
        # Auto-reply on WhatsApp for callback requests
        if reply_type == "callback":
            name = (profile_name or lead.get("name") or "").strip()
            greet = f"Hi {name}, " if name else "Hi, "
            ack = (
                f"{greet}thank you for messaging *Technopolis Constructions*.\n\n"
                "We have noted your callback request and will call you back "
                "at your preferred time.\n\n"
                "Sharing *Solitaire Unity* project details here for your reference."
            )
            await send_whatsapp_text_message(from_phone, ack)
            await send_whatsapp_project_details(from_phone, lead_name=name)

        try:
            from core.events import get_event_bus
            await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
            await get_event_bus().publish("inbound_interest", role=role, lead_id=lead_id)
        except Exception:
            pass
        return {
            "matched": True,
            "interested": reply_type == "interested",
            "callback": reply_type == "callback",
            "reply_type": reply_type,
            "lead_id": lead_id,
            "role": role,
            "source": source,
            **result,
        }

    logger.info("WhatsApp reply from campaign lead {} (no interest/callback match)", lead_id)
    return {"matched": True, "interested": False, "callback": False, "lead_id": lead_id, "role": role}


async def process_whatsapp_inbound(
    *,
    from_phone: str,
    profile_name: str = "",
    message_text: str = "",
    wa_message_id: str = "",
) -> dict[str, Any]:
    """Route inbound WhatsApp: campaign lead reply first, else Dariaan ingest."""
    if not settings.whatsapp_inbound_leads_enabled:
        return {"ignored": True, "reason": "WHATSAPP_INBOUND_LEADS_ENABLED=0"}

    campaign = await process_campaign_whatsapp_reply(
        from_phone=from_phone,
        profile_name=profile_name,
        message_text=message_text,
        wa_message_id=wa_message_id,
    )
    if campaign.get("matched"):
        return campaign
    return await process_dariaan_whatsapp_inbound(
        from_phone=from_phone,
        profile_name=profile_name,
        message_text=message_text,
        wa_message_id=wa_message_id,
    )


# ── Meta Cloud API: text ───────────────────────────────────────────


async def _send_via_cloud_api(to_digits: str, text: str) -> dict[str, Any]:
    """Send a plain text message via Meta WhatsApp Cloud API."""
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"sent": False, "error": "WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID not configured"}
    import httpx
    pid = settings.whatsapp_phone_number_id.strip()
    url = f"https://graph.facebook.com/v21.0/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json=payload,
            )
            if resp.status_code >= 400:
                err_text = resp.text[:500]
                logger.warning("Cloud API send failed: {} {}", resp.status_code, err_text)
                return {"sent": False, "error": err_text, "via": "cloud_api", "status_code": resp.status_code}
            logger.info("Cloud API message sent to {}", to_digits)
            return {"sent": True, "to": to_digits, "via": "cloud_api", "status_code": resp.status_code}
    except Exception as e:
        logger.warning("Cloud API send error: {}", e)
        return {"sent": False, "error": str(e), "via": "cloud_api"}


# ── Meta Cloud API: media upload + send ────────────────────────────


async def _upload_media_cloud_api(file_path: str, mime_type: str) -> dict[str, Any]:
    """Upload a media file to Meta and return its media_id.

    https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
    """
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"sent": False, "error": "credentials not configured"}
    import httpx
    pid = settings.whatsapp_phone_number_id.strip()
    url = f"https://graph.facebook.com/v21.0/{pid}/media"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(file_path, "rb") as f:
                file_data = f.read()
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], file_data, mime_type)},
            )
            if resp.status_code >= 400:
                err = resp.text[:500]
                logger.warning("Media upload failed: {} {}", resp.status_code, err)
                return {"sent": False, "error": err, "status_code": resp.status_code}
            body = resp.json()
            media_id = body.get("id", "")
            if media_id:
                logger.info("Media uploaded: {} -> id={}", file_path, media_id)
                return {"sent": True, "media_id": media_id}
            return {"sent": False, "error": "no media_id in response"}
    except Exception as e:
        logger.warning("Media upload error: {}", e)
        return {"sent": False, "error": str(e)}


async def _send_media_message_cloud_api(
    to_digits: str, media_type: str, media_id: str,
    caption: str = "", filename: str = "",
) -> dict[str, Any]:
    """Send a pre-uploaded media message via Meta Cloud API.

    media_type: 'image', 'video', or 'document'
    """
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return {"sent": False, "error": "credentials not configured"}
    import httpx
    pid = settings.whatsapp_phone_number_id.strip()
    url = f"https://graph.facebook.com/v21.0/{pid}/messages"

    type_key = media_type
    media_obj = {"id": media_id}
    if caption:
        media_obj["caption"] = caption
    if filename and media_type == "document":
        media_obj["filename"] = filename

    payload = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": type_key,
        type_key: media_obj,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json=payload,
            )
            if resp.status_code >= 400:
                err = resp.text[:500]
                logger.warning("Media send failed ({}): {} {}", media_type, resp.status_code, err)
                return {"sent": False, "error": err, "status_code": resp.status_code}
            logger.info("{} sent to {}", media_type, to_digits)
            return {"sent": True, "to": to_digits, "media_type": media_type}
    except Exception as e:
        logger.warning("Media send error: {}", e)
        return {"sent": False, "error": str(e)}


async def _send_image_cloud_api(to_digits: str, image_path: str, caption: str = "") -> dict[str, Any]:
    """Upload and send an image via Meta Cloud API."""
    if not os.path.exists(image_path):
        return {"sent": False, "error": f"image not found: {image_path}"}
    up = await _upload_media_cloud_api(image_path, "image/jpeg")
    if not up.get("sent"):
        return up
    return await _send_media_message_cloud_api(to_digits, "image", up["media_id"], caption=caption)


async def _send_video_cloud_api(to_digits: str, video_path: str, caption: str = "") -> dict[str, Any]:
    """Upload and send a video via Meta Cloud API."""
    if not os.path.exists(video_path):
        return {"sent": False, "error": f"video not found: {video_path}"}
    up = await _upload_media_cloud_api(video_path, "video/mp4")
    if not up.get("sent"):
        return up
    return await _send_media_message_cloud_api(to_digits, "video", up["media_id"], caption=caption)


async def _send_document_cloud_api(to_digits: str, doc_path: str, filename: str = "") -> dict[str, Any]:
    """Upload and send a document via Meta Cloud API."""
    if not os.path.exists(doc_path):
        return {"sent": False, "error": f"document not found: {doc_path}"}
    up = await _upload_media_cloud_api(doc_path, "application/pdf")
    if not up.get("sent"):
        return up
    fname = filename or doc_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return await _send_media_message_cloud_api(
        to_digits, "document", up["media_id"], filename=fname,
    )


# ── BotSpice template manifest (video / audio headers per disposition) ─────

def _botspice_api_url() -> str:
    return (settings.botspice_api_url or "https://cloudwapp.botspice.com/api/wappBroad/triggerwam").strip()
_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "data" / "whatsapp_template_manifest.json"

_SERVICES_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_SERVICES_DIR)
_DEV_MEDIA_DIR = os.path.join(_BACKEND_DIR, "media", "whatsapp")
_MEDIA_DIR_VPS = "/opt/technopolis/backend/media/whatsapp/"


def _load_wa_manifest() -> dict[str, Any]:
    try:
        if _MANIFEST_PATH.is_file():
            return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("WhatsApp template manifest read failed: {}", e)
    return {}


def _normalize_wa_disposition(disposition: str) -> str:
    disp = (disposition or "").strip().lower().replace("-", "_")
    disp = disp.replace(" ", "_")
    if "site" in disp:
        return "site_visit"
    if any(k in disp for k in ("callback", "call_later", "call_back", "call back")):
        return "callback"
    return "interested"


def _template_plan_for_disposition(disposition: str = "") -> dict[str, Any]:
    """Resolve BotSpice template + primary video/audio from manifest or .env."""
    manifest = _load_wa_manifest()
    key = _normalize_wa_disposition(disposition)
    plans = manifest.get("dispositions") if isinstance(manifest, dict) else {}
    plan = dict((plans or {}).get(key) or {})

    env_map = {
        "interested": settings.botspice_template_interested or settings.botspice_template_name,
        "site_visit": settings.botspice_template_site_visit,
        "callback": settings.botspice_template_callback,
    }
    if not plan.get("template"):
        plan["template"] = (env_map.get(key) or settings.botspice_template_name or "").strip()
    if not plan.get("header_type"):
        plan["header_type"] = {
            "interested": "image",
            "site_visit": "image",
            "callback": "image",
        }.get(key, "image")
    if not plan.get("primary_media"):
        defaults = {
            "interested": "solitaire_unity_image.jpeg",
            "site_visit": "solitaire_unity_image.jpeg",
            "callback": "solitaire_unity_image.jpeg",
        }
        plan["primary_media"] = defaults.get(key, "solitaire_unity_image.jpeg")
    if not plan.get("document_name") and plan.get("header_type") == "document":
        primary = str(plan.get("primary_media") or "")
        plan["document_name"] = primary.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or "Solitaire_Unity_Brochure.pdf"
    plan["disposition_key"] = key
    plan["language_code"] = (
        plan.get("language_code")
        or manifest.get("language_code")
        or settings.botspice_language_code
        or "en"
    )
    return plan


def _supplementary_media_list(primary_filename: str = "") -> list[tuple[str, str, str]]:
    manifest = _load_wa_manifest()
    raw = manifest.get("supplementary_media") if isinstance(manifest, dict) else None
    if isinstance(raw, list) and raw:
        out: list[tuple[str, str, str]] = []
        skip = (primary_filename or "").strip().lower()
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            mtype, fname, cap = str(row[0]), str(row[1]), str(row[2])
            if skip and fname.strip().lower() == skip:
                continue
            out.append((mtype, fname, cap))
    if out:
        return out
    # Legacy fallback — Solitaire Unity by Technopolis: brochure + price sheet (image is primary).
    legacy = [
        ("document", "solitaire_unity_brochure.pdf", "Solitiare Unity - Brochure.pdf"),
        ("document", "solitaire_unity_price_sheet.pdf", "SOLITAIRE UNITY PRICE QUOTATION 11-05-2026.pdf"),
    ]
    skip = (primary_filename or "").strip().lower()
    return [(t, f, c) for t, f, c in legacy if f.lower() != skip]


def _media_path(filename: str) -> str:
    vps = _MEDIA_DIR_VPS + filename
    if os.path.exists(vps):
        return vps
    dev = os.path.join(_DEV_MEDIA_DIR, filename)
    if os.path.exists(dev):
        return dev
    return vps


def _public_media_base_url() -> str:
    """HTTPS base for BotSpice template header media (must be publicly reachable)."""
    explicit = (settings.whatsapp_media_public_base_url or "").strip()
    if explicit:
        return explicit.rstrip("/")
    base = (settings.server_url or "").strip()
    if base.startswith("https://"):
        return base.rstrip("/")
    return ""


def _public_media_url(filename: str = "") -> str:
    default = (settings.botspice_default_media_url or "").strip()
    fname = (filename or "").strip()
    base = _public_media_base_url()
    if fname and base:
        return f"{base}/media/whatsapp/{fname.lstrip('/')}"
    return default


def _parse_botspice_response(resp) -> dict[str, Any]:
    """BotSpice returns HTTP 200 with success=false on many failures."""
    try:
        data = resp.json()
    except Exception:
        data = {"success": False, "message": (resp.text or "")[:500]}
    if not isinstance(data, dict):
        data = {"success": False, "message": str(data)[:500]}
    ok = bool(data.get("success"))
    if ok:
        return {"sent": True, "raw": data, "wamid": data.get("wamid")}
    msg = str(data.get("message") or data.get("error") or resp.text or "BotSpice send failed")[:500]
    err = data.get("error")
    if isinstance(err, dict):
        nested = err.get("error") or err
        if isinstance(nested, dict) and nested.get("message"):
            msg = str(nested.get("message"))[:500]
    return {"sent": False, "error": msg, "raw": data, "status_code": resp.status_code}


def _botspice_configured() -> bool:
    return bool(settings.botspice_token and settings.botspice_phone_number_id)


def _meta_configured() -> bool:
    return bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)


def _mime_for_upload(media_type: str, filename: str) -> str:
    low = filename.lower()
    if media_type == "audio" or low.endswith((".mp3", ".m4a", ".ogg")):
        if low.endswith(".mp3"):
            return "audio/mpeg"
        if low.endswith(".m4a"):
            return "audio/mp4"
        return "audio/mpeg"
    if media_type == "video" or low.endswith(".mp4"):
        return "video/mp4"
    if media_type == "document" or low.endswith(".pdf"):
        return "application/pdf"
    return "image/jpeg"


async def _send_via_botspice(
    to_digits: str,
    template_name: str = "",
    language_code: str = "en",
    media_url: str = "",
    media_type: str = "",
    document_name: str = "",
) -> dict[str, Any]:
    """Send a template-based WhatsApp message via BotSpice / CloudWapp API.

    media.type supports: image, video, document (PDF).
    """
    if not _botspice_configured():
        logger.info("BotSpice credentials not configured, will use Meta Cloud API")
        return {"sent": False, "error": "BotSpice credentials not configured", "via": "botspice"}

    import httpx

    tpl = (template_name or settings.botspice_template_name).strip()
    payload: dict[str, Any] = {
        "phoneNumberId": settings.botspice_phone_number_id,
        "toNumber": to_digits,
        "templateName": tpl,
        "languageCode": (language_code or settings.botspice_language_code or "en").strip() or "en",
    }
    conn_name = (settings.botspice_connection_name or "").strip()
    if conn_name:
        payload["connectionName"] = conn_name
    if media_url and media_type:
        media_payload: dict[str, Any] = {"type": media_type, "url": media_url}
        if media_type == "document" and document_name:
            media_payload["name"] = document_name
        payload["media"] = media_payload

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _botspice_api_url(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.botspice_token}",
                },
                json=payload,
            )
            parsed = _parse_botspice_response(resp)
            if not parsed.get("sent"):
                err_text = parsed.get("error") or resp.text[:500]
                logger.warning(
                    "BotSpice send failed: {} {} (template={}, phoneNumberId={})",
                    resp.status_code,
                    err_text,
                    tpl,
                    settings.botspice_phone_number_id,
                )
                return {
                    "sent": False,
                    "error": err_text,
                    "via": "botspice",
                    "status_code": resp.status_code,
                    "media_type": media_type or None,
                    "botspice_response": parsed.get("raw"),
                }
            logger.info(
                "BotSpice message sent to {} (template={}, media={}, wamid={})",
                to_digits,
                tpl,
                media_type or "none",
                parsed.get("wamid"),
            )
            return {
                "sent": True,
                "to": to_digits,
                "via": "botspice",
                "template": tpl,
                "media_type": media_type or None,
                "media_url": media_url or None,
                "wamid": parsed.get("wamid"),
            }
    except Exception as e:
        logger.warning("BotSpice send error: {}", e)
        return {"sent": False, "error": str(e), "via": "botspice", "media_type": media_type or None}


async def _send_primary_botspice_template(
    to_digits: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Send ONE disposition template with its video/audio header via BotSpice."""
    if not _botspice_configured():
        return {"sent": False, "error": "BotSpice not configured", "via": "botspice"}

    template = str(plan.get("template") or "").strip()
    header_type = str(plan.get("header_type") or "image").strip().lower()
    primary = str(plan.get("primary_media") or "").strip()
    language = str(plan.get("language_code") or settings.botspice_language_code or "en").strip()

    if not template:
        return {"sent": False, "error": "No BotSpice template for disposition", "via": "botspice"}

    media_url = _public_media_url(primary) if primary else _public_media_url("")
    if media_url and not media_url.startswith("https://"):
        logger.warning(
            "BotSpice media URL is not public HTTPS ({}). Set WHATSAPP_MEDIA_PUBLIC_BASE_URL to your VPS URL.",
            media_url,
        )
        media_url = (settings.botspice_default_media_url or "").strip()
    if primary and media_url.startswith("https://"):
        path = _media_path(primary)
        if not os.path.exists(path):
            logger.info("BotSpice primary media file missing locally ({}); using public URL {}", primary, media_url)

    # WhatsApp template headers: video, image, document; BotSpice also accepts audio URLs.
    botspice_type = header_type
    if header_type == "audio" and primary.lower().endswith(".mp4"):
        botspice_type = "video"

    doc_name = str(plan.get("document_name") or "").strip()
    if not doc_name and botspice_type == "document" and primary:
        doc_name = primary.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    result = await _send_via_botspice(
        to_digits=to_digits,
        template_name=template,
        language_code=language,
        media_url=media_url,
        media_type=botspice_type if media_url else "",
        document_name=doc_name,
    )
    label = f"template_{plan.get('disposition_key', 'interested')}"
    return {
        **result,
        "label": label,
        "channel": "botspice",
        "primary_media": primary,
        "disposition": plan.get("disposition_key"),
    }


async def _send_supplementary_meta_media(
    to_digits: str,
    *,
    primary_filename: str,
    has_meta: bool,
) -> list[tuple[str, dict[str, Any]]]:
    """Send remaining brochure assets via Meta Cloud API (not via template spam)."""
    import asyncio as _aio

    results: list[tuple[str, dict[str, Any]]] = []
    if not has_meta:
        return results

    for media_type, filename, caption_or_name in _supplementary_media_list(primary_filename):
        label = filename.rsplit(".", 1)[0]
        path = _media_path(filename)
        if not os.path.exists(path):
            results.append((label, {"sent": False, "error": f"file not found: {filename}", "channel": "meta"}))
            continue
        if media_type == "image":
            out = await _send_image_cloud_api(to_digits, path, caption=caption_or_name)
        elif media_type == "video":
            out = await _send_video_cloud_api(to_digits, path, caption=caption_or_name)
        elif media_type == "audio":
            mime = _mime_for_upload("audio", filename)
            up = await _upload_media_cloud_api(path, mime)
            if up.get("sent"):
                out = await _send_media_message_cloud_api(to_digits, "audio", up["media_id"])
            else:
                out = up
        else:
            out = await _send_document_cloud_api(to_digits, path, filename=caption_or_name)
        results.append((label, {**out, "channel": "meta"}))
        await _aio.sleep(2.0)
    return results


async def _send_primary_meta_fallback(
    to_digits: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Meta fallback when BotSpice unavailable — send primary video/audio only."""
    if not _meta_configured():
        return {"sent": False, "error": "Meta not configured", "channel": "meta"}
    primary = str(plan.get("primary_media") or "")
    header_type = str(plan.get("header_type") or "video").lower()
    path = _media_path(primary)
    if not os.path.exists(path):
        return {"sent": False, "error": f"primary media missing: {primary}", "channel": "meta"}
    if header_type == "image":
        out = await _send_image_cloud_api(to_digits, path, str(plan.get("caption") or ""))
    elif header_type == "audio":
        mime = _mime_for_upload("audio", primary)
        up = await _upload_media_cloud_api(path, mime)
        if not up.get("sent"):
            return {**up, "channel": "meta"}
        out = await _send_media_message_cloud_api(to_digits, "audio", up["media_id"])
    else:
        out = await _send_video_cloud_api(to_digits, path, str(plan.get("caption") or ""))
    return {**out, "label": f"primary_{plan.get('disposition_key')}", "channel": "meta"}


async def botspice_config_status() -> dict[str, Any]:
    """Summarize BotSpice readiness for dashboard / health checks."""
    configured = _botspice_configured()
    media_base = _public_media_base_url()
    plan = _template_plan_for_disposition("interested")
    return {
        "configured": configured,
        "api_url": _botspice_api_url(),
        "phone_number_id": settings.botspice_phone_number_id or None,
        "business_number": resolve_whatsapp_business_number() or None,
        "language_code": settings.botspice_language_code or "en",
        "templates": {
            "interested": settings.botspice_template_interested or settings.botspice_template_name,
            "site_visit": settings.botspice_template_site_visit,
            "callback": settings.botspice_template_callback,
        },
        "media_public_base_url": media_base or None,
        "default_media_url": (settings.botspice_default_media_url or None),
        "sample_template_plan": plan,
        "notes": [
            "BotSpice must link phoneNumberId on their portal (connectionName not found = not linked yet).",
            "Template language must match Meta approval (usually languageCode=en).",
            "Header media URL must be public HTTPS (WHATSAPP_MEDIA_PUBLIC_BASE_URL).",
        ],
    }


async def probe_botspice_connection() -> dict[str, Any]:
    """Dry-run BotSpice API with invalid recipient to surface connection/template errors."""
    if not _botspice_configured():
        return {"ok": False, "error": "BotSpice not configured"}
    import httpx

    plan = _template_plan_for_disposition("interested")
    media_url = _public_media_url(str(plan.get("primary_media") or ""))
    payload: dict[str, Any] = {
        "phoneNumberId": settings.botspice_phone_number_id,
        "toNumber": "919999999999",
        "templateName": str(plan.get("template") or settings.botspice_template_name),
        "languageCode": str(plan.get("language_code") or settings.botspice_language_code or "en"),
    }
    if settings.botspice_connection_name:
        payload["connectionName"] = settings.botspice_connection_name
    if media_url.startswith("https://"):
        payload["media"] = {"type": str(plan.get("header_type") or "image"), "url": media_url}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                _botspice_api_url(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.botspice_token}",
                },
                json=payload,
            )
        parsed = _parse_botspice_response(resp)
        msg = str(parsed.get("error") or "")
        if parsed.get("sent"):
            return {"ok": True, "message": "BotSpice accepted template send"}
        if "connectionName not found" in msg:
            return {
                "ok": False,
                "error": msg,
                "fix": f"Ask BotSpice to link phoneNumberId {settings.botspice_phone_number_id} (+916366988484) to this token.",
            }
        if "132001" in msg or "Template name does not exist" in msg:
            return {
                "ok": False,
                "error": msg,
                "fix": "Template not approved on this WhatsApp number — verify template name and languageCode=en.",
            }
        return {"ok": False, "error": msg or "unknown BotSpice error", "raw": parsed.get("raw")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Message content ────────────────────────────────────────────────

_SOLITAIRE_UNITY_BODY = """Hi {name}, Thank you for your interest in *Solitaire Unity by Technopolis Construction.*

Introducing *SOLITAIRE UNITY* — Premium 2, 2.5 & 3 BHK Residences in Kondapur

• Premium gated residential community
• Spread across *2 Acres, 10 Guntas*
• *30+ lifestyle amenities*
• *32,000 sq.ft Clubhouse*
• 3 levels of basement parking
• Residential towers rising up to 16 floors
• Thoughtfully designed homes for modern family living
• Excellent connectivity to Kondapur, Hitech City, Gachibowli & Lingampally

*Experience:* Premium residences designed around comfort, convenience & lifestyle
*Enjoy:* Resort-style indoor and outdoor amenities
*Live:* In one of Hyderabad’s most connected residential corridors

*Configurations & Indicative Basic Pricing:*

• *2 BHK* — 1,225 to 1,615 sq.ft | Approx. ₹1.20 Cr onwards
• *2.5 BHK* — 1,555 sq.ft | Approx. ₹1.52 Cr onwards
• *3 BHK* — 1,400 to 2,300 sq.ft | Approx. ₹1.37 Cr onwards

*Basic Rate:* ₹9,799/sq.ft

*Final pricing varies based on unit size, floor, facing, corner preference and applicable additional/statutory charges.*

*Lifestyle Amenities:*
• Swimming Pool with Pool Deck
• Air-Conditioned Gymnasium
• Yoga & Meditation Room
• Indoor Games Room
• Business Centre & Co-working Space
• Banquet Hall
• Library
• Senior Citizen Lounge
• Kids Play Area & Sand Pit
• Multipurpose Lawn
• Badminton / Multipurpose Court
• Basketball Court
• Jogging Track
• Zen Garden & Meditation Gazebos

*Location:* Kondapur, Hyderabad

*Nearby Connectivity:*
• HCU — approx. 1.6 km
• Sancta Maria School — approx. 3.5 km
• MMTS Lingampally Railway Station — approx. 3.7 km
• KIMS Kondapur — approx. 4.6 km
• Hitech City — approx. 6.7 km
• Continental Hospital — approx. 7.5 km
• Oakridge International School — approx. 10 km

*Approvals & Status:*
• TS RERA: P02400003285
• GHMC Permitted
• Occupancy Certificate issued — 27 April 2026

*Payment Schedule:*
• Booking — 10% of Sale Consideration
• Balance 90% — Within 10 days from booking
• Registration charges applicable separately

📍 *Location:* https://maps.app.goo.gl/Sj1YC5jHQZXh1eZXA

For more details, latest availability, exact unit pricing or to schedule a *site visit*, reply to this message or call us."""


# ── Main send function ────────────────────────────────────────────


async def send_whatsapp_project_details(
    to_phone: str,
    summary: str = "",
    lead_name: str = "",
    *,
    disposition: str = "",
) -> dict[str, Any]:
    """Send disposition-specific BotSpice template (video/audio) + supplementary Meta package."""
    import asyncio as _aio

    normalized = _norm_phone_str(to_phone)
    digits = "".join(c for c in normalized if c.isdigit())
    if not digits:
        return {"sent": False, "error": "invalid phone number"}

    # Linked-WhatsApp mode: send via OpenWA gateway (image + brochure + price sheet).
    if getattr(settings, "openwa_enabled", False):
        pkg = await send_whatsapp_details_via_openwa(
            digits, lead_name=lead_name or "", summary=summary or "",
        )
        return pkg

    plan = _template_plan_for_disposition(disposition)
    primary_file = str(plan.get("primary_media") or "")

    body = _SOLITAIRE_UNITY_BODY.format(name=lead_name or "there")
    if summary:
        body = f"*{summary}*\n\n---\n\n" + body

    results: list[tuple[str, dict[str, Any]]] = []
    via_botspice = _botspice_configured()
    has_meta = _meta_configured() and bool(getattr(settings, "whatsapp_meta_supplementary_enabled", True))

    # ── 1) Primary template (one video/audio header per disposition) ──
    if via_botspice:
        primary = await _send_primary_botspice_template(digits, plan)
        results.append((primary.get("label", "template"), primary))
        if not primary.get("sent"):
            fallback = await _send_primary_meta_fallback(digits, plan)
            results.append(("primary_meta_fallback", fallback))
    elif has_meta:
        fallback = await _send_primary_meta_fallback(digits, plan)
        results.append(("primary_meta", fallback))
    else:
        results.append(("template", {"sent": False, "error": "No WhatsApp channel configured"}))

    await _aio.sleep(2.0)

    # ── 2) Supplementary image / videos / PDFs (Meta only) ──
    supp = await _send_supplementary_meta_media(
        digits, primary_filename=primary_file, has_meta=has_meta,
    )
    results.extend(supp)

    # ── 3) Text body + location (Meta; optional — template body often covers intro) ──
    if has_meta:
        tr = await _send_via_cloud_api(digits, body)
        results.append(("text", tr))
        await _aio.sleep(1.5)
        loc_text = "Location: https://maps.app.goo.gl/e7NA8Qfka3rjXxXY7?g_st=ac"
        lr = await _send_via_cloud_api(digits, loc_text)
        results.append(("location", lr))

    any_sent = any(r.get("sent") for _, r in results)
    via = "botspice+meta" if via_botspice and has_meta else ("botspice" if via_botspice else "meta")
    top_error = None
    if not any_sent:
        for _, row in results:
            if isinstance(row, dict) and row.get("error"):
                top_error = row["error"]
                break
    return {
        "sent": any_sent,
        "to": digits,
        "via": via,
        "template": plan.get("template"),
        "disposition": plan.get("disposition_key"),
        "primary_media": primary_file,
        "error": top_error,
        "details": results,
    }


async def send_whatsapp_disposition_message(
    to_phone: str,
    *,
    disposition: str = "",
    summary: str = "",
    lead_name: str = "",
    email_on_file: str = "",
) -> dict[str, Any]:
    """Post-call WhatsApp follow-up tailored to call outcome."""
    disp = (disposition or "").strip().lower()
    name = (lead_name or "").strip()
    greet = f"Hi {name}, " if name else "Hi, "

    if any(k in disp for k in ("interested", "site visit", "site_visit")):
        return await send_whatsapp_project_details(
            to_phone, summary=summary, lead_name=lead_name, disposition=disposition,
        )

    if any(k in disp for k in ("callback", "call later", "callback_scheduled", "callback scheduled")):
        body = (
            f"{greet}thank you for speaking with *Technopolis Constructions*.\n\n"
            "As discussed, we will call you back at your preferred time.\n\n"
            "Sharing full *Solitaire Unity* project details here on WhatsApp — "
            "brochure, videos, and floor plans for your reference."
        )
        if summary:
            body = f"*{summary}*\n\n---\n\n{body}"
        text_result = await send_whatsapp_text_message(to_phone, body)
        pkg = await send_whatsapp_project_details(
            to_phone, summary=summary, lead_name=lead_name, disposition=disposition,
        )
        any_sent = bool(text_result.get("sent") or pkg.get("sent"))
        return {
            "sent": any_sent,
            "to": pkg.get("to") or text_result.get("to"),
            "disposition": "callback",
            "details": [("text", text_result), ("package", pkg)],
        }

    if "not interested" in disp:
        body = (
            f"{greet}thank you for your time today.\n\n"
            "We respect your decision. If you change your mind about "
            "*Solitaire Unity*, we are always happy to help."
        )
        return await send_whatsapp_text_message(to_phone, body)

    if any(k in disp for k in ("no response", "no answer", "failed", "busy")):
        body = (
            f"{greet}we tried reaching you from *Technopolis Constructions* regarding "
            "*Solitaire Unity* — premium apartments in Kondapur, Hyderabad.\n\n"
            "Sharing project details here on WhatsApp for your reference."
        )
        if summary:
            body = f"*{summary}*\n\n---\n\n{body}"
        text_result = await send_whatsapp_text_message(to_phone, body)
        pkg = await send_whatsapp_project_details(to_phone, summary="", lead_name=lead_name)
        any_sent = bool(text_result.get("sent") or pkg.get("sent"))
        return {"sent": any_sent, "to": pkg.get("to") or text_result.get("to"), "details": [("text", text_result), ("package", pkg)]}

    return await send_whatsapp_project_details(to_phone, summary=summary, lead_name=lead_name)


async def _get_openwa_session_uuid(client: Any) -> Optional[str]:
    """Resolve the OpenWA session UUID.

    Priority:
      1. ``settings.openwa_session_id`` if explicitly configured.
      2. First session returned by ``GET /api/sessions`` (typical single-session setup).
    """
    api_key = (settings.openwa_api_key or "").strip()
    if not api_key:
        return None
    base = (settings.openwa_api_url or "http://127.0.0.1:2785").rstrip("/")
    explicit = (settings.openwa_session_id or "").strip()
    if explicit:
        return explicit
    try:
        resp = await client.get(
            f"{base}/api/sessions",
            headers={"X-API-Key": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        sessions = data if isinstance(data, list) else (data.get("sessions") or data.get("data") or [])
        if sessions and isinstance(sessions, list) and len(sessions) > 0:
            first = sessions[0]
            sid = (
                first.get("id")
                or first.get("sessionId")
                or first.get("uuid")
                or first.get("session_id")
                or ""
            ).strip()
            return sid or None
    except Exception as e:
        logger.warning("OpenWA session resolution failed: {}", e)
    return None


async def send_whatsapp_text_message(to_phone: str, text: str) -> dict[str, Any]:
    """Send a free-form text message.

    Prefers the OpenWA gateway (linked WhatsApp) when ``OPENWA_ENABLED=1``;
    falls back to Meta Cloud API otherwise.
    """
    normalized = _norm_phone_str(to_phone)
    digits = "".join(c for c in normalized if c.isdigit())
    if not digits or not text:
        return {"sent": False, "error": "invalid phone or empty text"}
    if getattr(settings, "openwa_enabled", False):
        try:
            from services.whatsapp.client import send_text as _openwa_send_text

            result = await _openwa_send_text(digits, text)
            if isinstance(result, dict) and result.get("error") is None:
                return {"sent": True, "to": digits, "via": "openwa", "raw": result}
            return {"sent": False, "error": str(result.get("error") or result.get("detail") or result), "via": "openwa"}
        except Exception as e:
            logger.warning("OpenWA text send failed ({}), falling back to Meta: {}", to_phone, e)
    return await _send_via_cloud_api(digits, text)


async def send_whatsapp_details_via_openwa(
    to_phone: str,
    *,
    lead_name: str = "",
    summary: str = "",
) -> dict[str, Any]:
    """Send the Solitaire Unity details package through the OpenWA gateway.

    Order: image -> brochure PDF -> price sheet PDF. Uses the public media
    URLs so the gateway can fetch them.
    """
    import asyncio as _aio

    digits = "".join(c for c in (to_phone or "") if c.isdigit())
    if not digits or not getattr(settings, "openwa_enabled", False):
        return {"sent": False, "error": "openwa not enabled or invalid phone"}

    from services.whatsapp.client import send_document, send_image

    base = _public_media_base_url()
    if not base:
        return {"sent": False, "error": "WHATSAPP_MEDIA_PUBLIC_BASE_URL / SERVER_URL not HTTPS"}

    results: list[tuple[str, dict[str, Any]]] = []
    image_url = f"{base}/media/whatsapp/solitaire_unity_image.jpeg"
    brochure_url = f"{base}/media/whatsapp/solitaire_unity_brochure.pdf"
    price_url = f"{base}/media/whatsapp/solitaire_unity_price_sheet.pdf"

    img = await send_image(digits, image_url, caption="Solitaire Unity — Premium Apartments, Kondapur")
    results.append(("image", img))
    await _aio.sleep(1.5)
    broc = await send_document(digits, brochure_url, filename="Solitiare Unity - Brochure.pdf")
    results.append(("brochure", broc))
    await _aio.sleep(1.5)
    price = await send_document(digits, price_url, filename="SOLITAIRE UNITY PRICE QUOTATION 11-05-2026.pdf")
    results.append(("price_sheet", price))

    ok = any(
        isinstance(r, dict) and r.get("error") is None and not (isinstance(r.get("error"), int) and r["error"] >= 400)
        for _, r in results
    )
    first_err = None
    for label, r in results:
        if isinstance(r, dict) and r.get("error"):
            first_err = f"{label}: {r.get('error')}"
            break
    if not ok:
        body = _SOLITAIRE_UNITY_BODY.format(name=lead_name or "there")
        txt = await send_whatsapp_text_message(digits, body)
        results.append(("text_fallback", txt))
        ok = bool(txt.get("sent"))
    return {
        "sent": ok,
        "to": digits,
        "via": "openwa",
        "details": results,
        "error": None if ok else (first_err or "no channel sent"),
    }


# ── BotSpice template test helpers ─────────────────────────────────


async def send_botspice_template_test(
    to_phone: str,
    *,
    template_name: str,
    media_type: str,
    media_url: str,
    document_name: str = "",
    language_code: str = "",
) -> dict[str, Any]:
    """Send a single BotSpice template for integration testing."""
    digits = "".join(c for c in (to_phone or "") if c.isdigit())
    if not digits:
        return {"sent": False, "error": "invalid phone"}
    return await _send_via_botspice(
        to_digits=digits,
        template_name=template_name,
        language_code=language_code or settings.botspice_language_code or "en",
        media_url=media_url,
        media_type=media_type,
        document_name=document_name,
    )


async def test_all_botspice_templates(to_phone: str) -> dict[str, Any]:
    """Send all three Solitaire Unity BotSpice templates (image, document, video)."""
    tests = [
        {
            "key": "site_visit",
            "template": settings.botspice_template_site_visit or "kings_queens_var2",
            "media_type": "image",
            "media_url": "https://picsum.photos/200/300.jpg",
        },
        {
            "key": "callback",
            "template": settings.botspice_template_callback or "charm_spain_var2",
            "media_type": "document",
            "media_url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
            "document_name": "Dummy.pdf",
        },
        {
            "key": "interested",
            "template": settings.botspice_template_interested or settings.botspice_template_name or "solitaire_unity_image",
            "media_type": "video",
            "media_url": "https://onlinetestcase.com/wp-content/uploads/2023/06/1MB.mp4",
        },
    ]
    results: list[dict[str, Any]] = []
    all_sent = True
    for spec in tests:
        out = await send_botspice_template_test(
            to_phone,
            template_name=spec["template"],
            media_type=spec["media_type"],
            media_url=spec["media_url"],
            document_name=spec.get("document_name", ""),
        )
        row = {"disposition": spec["key"], "template": spec["template"], **out}
        results.append(row)
        if not out.get("sent"):
            all_sent = False
    return {"sent": all_sent, "to": "".join(c for c in (to_phone or "") if c.isdigit()), "results": results}


# ── Auto-reply (inbound WhatsApp) ──────────────────────────────────


async def maybe_send_whatsapp_auto_reply(to_phone: str) -> None:
    """Thank-you after new inbound — via Meta Cloud API."""
    body = (
        "Thank you for contacting *Technopolis Constructions*. "
        "Our team will reach out shortly."
    )
    digits = "".join(c for c in (to_phone or "") if c.isdigit())
    if not digits:
        return
    if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
        return
    import httpx
    pid = settings.whatsapp_phone_number_id.strip()
    url = f"https://graph.facebook.com/v21.0/{pid}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": digits,
        "type": "text",
        "text": {"body": body},
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json=payload,
            )
            if resp.status_code >= 400:
                logger.warning("WhatsApp auto-reply failed: {} {}", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("WhatsApp auto-reply error: {}", e)


# ── Webhook parser ─────────────────────────────────────────────────


def parse_meta_webhook_messages(body: dict) -> list[dict[str, Any]]:
    """Extract inbound user messages from Meta WhatsApp webhook JSON."""
    out: list[dict[str, Any]] = []
    if (body.get("object") or "") != "whatsapp_business_account":
        return out
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if (change.get("field") or "") != "messages":
                continue
            value = change.get("value") or {}
            profiles = {
                str(c.get("wa_id", "")): (c.get("profile") or {}).get("name", "")
                for c in (value.get("contacts") or [])
            }
            for msg in value.get("messages") or []:
                if (msg.get("type") or "") != "text":
                    continue
                text_body = ((msg.get("text") or {}).get("body") or "").strip()
                from_id = str(msg.get("from") or "")
                out.append({
                    "from": from_id,
                    "profile_name": profiles.get(from_id, ""),
                    "text": text_body,
                    "message_id": str(msg.get("id") or ""),
                })
    return out
