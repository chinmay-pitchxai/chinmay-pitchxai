"""Unified WhatsApp auto-send for interested / site visit / callback outcomes."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from loguru import logger

from config import settings
from core.utils import _norm_phone_str


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().replace("_", " ").split())


def whatsapp_auto_send_allowlist() -> set[str]:
    """Normalized outcome keys from WHATSAPP_AUTO_SEND_FOR."""
    out: set[str] = set()
    raw = (settings.whatsapp_auto_send_for or "").strip()
    for part in raw.split(","):
        token = _norm(part).replace(" ", "_")
        if not token:
            continue
        out.add(token)
        if token == "site_visited":
            out.add("site_visit")
        if token == "callback":
            out.add("callback_scheduled")
    return out


def resolve_outcome_key(
    *,
    disposition: str = "",
    status: str = "",
    analysis: dict[str, Any] | None = None,
) -> Optional[str]:
    """Map call outcome → whatsapp template bucket: interested | site_visit | callback."""
    analysis = analysis if isinstance(analysis, dict) else {}
    status_n = _norm(status).replace(" ", "_")
    disp_n = _norm(disposition)

    next_action = analysis.get("next_action") if isinstance(analysis.get("next_action"), dict) else {}
    action_type = _norm(str(next_action.get("action_type") or ""))

    if status_n == "site_visit" or analysis.get("site_visit_agreed") or action_type in (
        "site visit",
        "site_visit",
    ):
        return "site_visit"

    if status_n == "callback_scheduled":
        return "callback"
    if analysis.get("callback_reminder_epoch") or analysis.get("requested_callback_datetime_iso"):
        return "callback"
    if any(k in disp_n for k in ("callback", "call later", "call back")):
        return "callback"

    if status_n == "interested" or (
        "interested" in disp_n and "not interested" not in disp_n
    ):
        return "interested"
    if "site visit" in disp_n:
        return "site_visit"

    if action_type in ("whatsapp", "send whatsapp", "send_whatsapp", "whatsapp follow up", "whatsapp follow-up"):
        return "interested"
    if "whatsapp" in action_type and "email" not in action_type:
        na_details = _norm(str(next_action.get("details") or ""))
        if "whatsapp" in na_details or "brochure" in na_details or "location" in na_details:
            return "interested"

    return None


def should_auto_send_whatsapp(
    *,
    disposition: str = "",
    status: str = "",
    analysis: dict[str, Any] | None = None,
) -> bool:
    key = resolve_outcome_key(disposition=disposition, status=status, analysis=analysis)
    if not key:
        return False
    allow = whatsapp_auto_send_allowlist()
    return key in allow


def disposition_label_for_send(outcome_key: str) -> str:
    return {
        "interested": "Interested",
        "site_visit": "Site Visit",
        "callback": "callback_scheduled",
    }.get(outcome_key, outcome_key)


async def resolve_whatsapp_recipient_phone(
    *,
    role: str = "",
    lead_id: int | None = None,
    camp_id: str | None = None,
    fallback_phone: str = "",
    analysis: dict[str, Any] | None = None,
) -> str:
    """WhatsApp goes to whoever answered — inbound caller or outbound callee that connected."""
    candidates: list[str] = []
    analysis = analysis if isinstance(analysis, dict) else {}

    if camp_id:
        try:
            from core.camp_session import hydrate_camp_session
            from core.state import _CAMPAIGN_DATA

            await hydrate_camp_session(str(camp_id))
            info = _CAMPAIGN_DATA.get(str(camp_id)) or {}
            if isinstance(info, dict):
                for key in ("_answered_phone", "phone"):
                    p = str(info.get(key) or "").strip()
                    if p:
                        candidates.append(p)
        except Exception:
            pass

    cid = str(camp_id or "")
    if cid.startswith("incoming_"):
        parts = cid.split("_from_")
        if len(parts) >= 2:
            digits = re.sub(r"\D", "", (parts[1].split("_")[0] or parts[1]).strip())
            if len(digits) >= 10:
                candidates.append("+91" + digits[-10:])
            elif digits:
                candidates.append(digits)
        try:
            from core.storage import incoming_call_row_by_camp_id

            row = await incoming_call_row_by_camp_id(cid)
            if row:
                candidates.append(str(row.get("from_phone") or ""))
        except Exception:
            pass

    if cid.startswith("manual_"):
        try:
            from core.storage import manual_call_row_by_camp_id

            row = await manual_call_row_by_camp_id(cid)
            if row:
                candidates.append(str(row.get("to_phone") or ""))
        except Exception:
            pass

    for key in ("whatsapp_phone", "contact_phone", "phone_number", "callback_phone", "mobile"):
        p = str(analysis.get(key) or "").strip()
        if p:
            candidates.append(p)

    if lead_id:
        try:
            from core.storage import get_lead, get_lead_role

            lr = await get_lead_role(int(lead_id))
            row = await get_lead(lr, int(lead_id)) if lr else None
            if row:
                candidates.append(str(row.get("phone") or ""))
        except Exception:
            pass

    if fallback_phone:
        candidates.append(fallback_phone)

    seen: set[str] = set()
    for raw in candidates:
        norm = _norm_phone_str(raw)
        if norm and norm not in seen:
            seen.add(norm)
            return norm
    return ""


async def _last_outcome_sent(lead_id: int | None, camp_id: str | None) -> str:
    if lead_id:
        try:
            from core.storage import get_lead, get_lead_role

            role = await get_lead_role(int(lead_id))
            row = await get_lead(role, int(lead_id)) if role else None
            if row:
                raw = row.get("extra") or "{}"
                extra = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                return str(extra.get("whatsapp_outcome_key") or "").strip()
        except Exception:
            pass
    if camp_id:
        try:
            from core.state import _CAMPAIGN_DATA

            meta = _CAMPAIGN_DATA.get(camp_id) or {}
            if isinstance(meta, dict):
                return str(meta.get("_whatsapp_outcome_sent") or "").strip()
        except Exception:
            pass
    return ""


async def _record_outcome_sent(
    *,
    lead_id: int | None,
    camp_id: str | None,
    outcome_key: str,
    role: str = "",
) -> None:
    if camp_id:
        try:
            from core.state import _CAMPAIGN_DATA, mark_whatsapp_sent_for_call

            mark_whatsapp_sent_for_call(camp_id)
            if isinstance(_CAMPAIGN_DATA.get(camp_id), dict):
                _CAMPAIGN_DATA[camp_id]["_whatsapp_outcome_sent"] = outcome_key
                _CAMPAIGN_DATA[camp_id]["_whatsapp_sent"] = True
        except Exception:
            pass
    if lead_id:
        try:
            from core.storage import get_lead, get_lead_role, mark_whatsapp_sent, update_lead_retry_state

            await mark_whatsapp_sent(int(lead_id))
            lr = await get_lead_role(int(lead_id))
            row = await get_lead(lr, int(lead_id)) if lr else None
            if row:
                raw = row.get("extra") or "{}"
                extra = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                extra["whatsapp_outcome_key"] = outcome_key
                raw_a = row.get("analysis") or "{}"
                analysis = json.loads(raw_a) if isinstance(raw_a, str) else (raw_a if isinstance(raw_a, dict) else {})
                await update_lead_retry_state(int(lead_id), str(row.get("status") or ""), extra, analysis)
        except Exception:
            logger.exception("Failed to record whatsapp_outcome_key for lead {}", lead_id)
    try:
        from core.events import get_event_bus

        await get_event_bus().publish(
            "whatsapp_sent",
            role=role,
            lead_id=lead_id,
            phone="",
            outcome=outcome_key,
        )
    except Exception:
        pass


async def send_agent_promised_whatsapp(
    *,
    role: str,
    camp_id: str | None = None,
    lead_id: int | None = None,
    lead_name: str = "",
    summary: str = "Project Details",
) -> dict[str, Any]:
    """Send Interested template when the agent promised WhatsApp during the call."""
    phone = await resolve_whatsapp_recipient_phone(
        role=role,
        lead_id=lead_id,
        camp_id=camp_id,
        analysis={"summary": summary, "disposition": "Interested"},
    )
    if not phone:
        return {"sent": False, "skipped": True, "reason": "no_phone"}

    from services.whatsapp_leads import send_whatsapp_disposition_message

    try:
        result = await send_whatsapp_disposition_message(
            phone,
            disposition="Interested",
            summary=summary,
            lead_name=lead_name,
        )
        if result.get("sent"):
            await _record_outcome_sent(
                lead_id=lead_id,
                camp_id=camp_id,
                outcome_key="interested",
                role=role,
            )
            try:
                from core.state import _CAMPAIGN_DATA

                if camp_id and isinstance(_CAMPAIGN_DATA.get(camp_id), dict):
                    _CAMPAIGN_DATA[camp_id].pop("_whatsapp_pending", None)
                    _CAMPAIGN_DATA[camp_id].pop("_whatsapp_pending_summary", None)
                    _CAMPAIGN_DATA[camp_id]["_whatsapp_sent"] = True
            except Exception:
                pass
            logger.info(
                "Agent-promised WhatsApp sent phone={} lead_id={} camp_id={}",
                phone,
                lead_id,
                camp_id,
            )
        return {**result, "outcome": "interested"}
    except Exception as exc:
        logger.exception("Agent-promised WhatsApp error phone={}: {}", phone, exc)
        return {"sent": False, "error": str(exc), "outcome": "interested"}


async def send_outcome_whatsapp_if_eligible(
    *,
    role: str,
    phone: str,
    lead_name: str = "",
    disposition: str = "",
    status: str = "",
    analysis: dict[str, Any] | None = None,
    lead_id: int | None = None,
    camp_id: str | None = None,
    email_on_file: str = "",
    force_resend: bool = False,
) -> dict[str, Any]:
    """Send disposition-specific WhatsApp when outcome is interested / site visit / callback."""
    phone = await resolve_whatsapp_recipient_phone(
        role=role,
        lead_id=lead_id,
        camp_id=camp_id,
        fallback_phone=phone,
        analysis=analysis,
    )
    if not phone:
        return {"sent": False, "skipped": True, "reason": "no_phone"}

    outcome_key = resolve_outcome_key(
        disposition=disposition, status=status, analysis=analysis
    )
    if not outcome_key or not should_auto_send_whatsapp(
        disposition=disposition, status=status, analysis=analysis
    ):
        logger.info(
            "WhatsApp auto-send skipped phone={} disposition={!r} status={!r} outcome={}",
            phone,
            disposition,
            status,
            outcome_key,
        )
        return {"sent": False, "skipped": True, "reason": "not_eligible", "outcome": outcome_key}

    last = await _last_outcome_sent(lead_id, camp_id)
    if last == outcome_key and not force_resend:
        logger.info(
            "WhatsApp already sent for outcome={} lead_id={} camp_id={}",
            outcome_key,
            lead_id,
            camp_id,
        )
        return {"sent": False, "skipped": True, "reason": "already_sent", "outcome": outcome_key}

    from services.whatsapp_leads import send_whatsapp_disposition_message

    wa_disp = disposition_label_for_send(outcome_key)
    summary = str((analysis or {}).get("summary") or "")
    try:
        result = await send_whatsapp_disposition_message(
            phone,
            disposition=wa_disp,
            summary=summary,
            lead_name=lead_name,
            email_on_file=email_on_file,
        )
        if result.get("sent"):
            await _record_outcome_sent(
                lead_id=lead_id,
                camp_id=camp_id,
                outcome_key=outcome_key,
                role=role,
            )
            logger.info(
                "Outcome WhatsApp sent outcome={} phone={} lead_id={} camp_id={}",
                outcome_key,
                phone,
                lead_id,
                camp_id,
            )
        else:
            logger.warning(
                "Outcome WhatsApp failed outcome={} phone={}: {}",
                outcome_key,
                phone,
                result,
            )
        return {**result, "outcome": outcome_key}
    except Exception as exc:
        logger.exception("Outcome WhatsApp send error phone={}: {}", phone, exc)
        return {"sent": False, "error": str(exc), "outcome": outcome_key}
