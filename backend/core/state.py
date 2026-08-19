"""Runtime state management — uses SQLite for persistence, in-memory for active tracking."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Optional
from loguru import logger

# In-memory active tracking (not persisted)
_ACTIVE_VOBIZ_CALLS: int = 0
_ACTIVE_VOBIZ_CALLS_BY_ROLE: dict[str, int] = {}
_ACTIVE_VOBIZ_CALLS_BY_AUTH: dict[str, int] = {}
_CAMPAIGN_DATA: dict[str, dict[str, Any]] = {}
_CAMPAIGN_TASKS: dict[str, Any] = {}
_OPENING_PCM_CACHE: dict[str, tuple[bytes, int]] = {}
_LAST_WORKER_ACTIVITY: dict[str, float] = {}
_MANUALLY_STOPPED_ROLES: set[str] = set()
calls_db: dict[str, dict[str, Any]] = {}

# Vobiz plans often allow 3 concurrent — cap app below provider limit to avoid 3/3 rejections.
_VOBIZ_MAX_PER_ACCOUNT = max(1, int(os.getenv("VOBIZ_MAX_CONCURRENT_PER_ACCOUNT", "2")))
_VOBIZ_PROVIDER_LIMIT = max(1, int(os.getenv("VOBIZ_PROVIDER_CONCURRENT_LIMIT", "3")))
_VOBIZ_SLOT_LOCK = threading.Lock()

_ROLES = (
    "sales_1",
)


from typing import Optional

def normalize_console_role(role: Optional[str]) -> str:
    """Ensure the role is valid, defaulting to the single Technopolis role 'sales_1'."""
    r = (role or "sales_1").lower().strip()
    return r if r in _ROLES else "sales_1"


def active_vobiz_calls_for_role(role: str) -> int:
    """Outbound/live legs currently active for one console role."""
    return int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(normalize_console_role(role), 0))


def vobiz_auth_id_for_role(role: str) -> str:
    """Resolve Vobiz account auth_id for a console role (for per-account caps)."""
    try:
        from core.vobiz_credentials import resolve_vobiz_credentials

        auth_id, _, _, _ = resolve_vobiz_credentials(normalize_console_role(role))
        return (auth_id or "").strip()
    except Exception:
        return ""


def active_vobiz_calls_for_auth(auth_id: str) -> int:
    aid = (auth_id or "").strip()
    if not aid:
        return 0
    return int(_ACTIVE_VOBIZ_CALLS_BY_AUTH.get(aid, 0))


def max_concurrency_for_vobiz_account(auth_id: str) -> int:
    """Hard cap per Vobiz trunk — never match provider limit exactly (avoids 3/3 rejections)."""
    cap = _VOBIZ_MAX_PER_ACCOUNT
    if cap >= _VOBIZ_PROVIDER_LIMIT:
        return max(1, _VOBIZ_PROVIDER_LIMIT - 1)
    return cap


def vobiz_auth_can_accept_call(role: str) -> bool:
    """False when role or Vobiz account is already at safe concurrent capacity."""
    r = normalize_console_role(role)
    if active_vobiz_calls_for_role(r) >= get_max_concurrency_for_role(r):
        return False
    auth_id = vobiz_auth_id_for_role(r)
    if auth_id and active_vobiz_calls_for_auth(auth_id) >= max_concurrency_for_vobiz_account(auth_id):
        return False
    return True


def get_max_concurrency_for_role(role: str) -> int:
    """Effective concurrent legs: campaign request, usable lines, app cap and Vobiz safe cap."""
    try:
        from core.outbound_numbers import get_all_outbound_numbers
        v_cfg = get_state(role).get("vobiz", {}) or {}
        numbers = get_all_outbound_numbers(role, v_cfg)
        line_cap = max(1, len(numbers))
        auth_cap = max_concurrency_for_vobiz_account(vobiz_auth_id_for_role(role))
        from config import settings
        app_cap = max(1, int(getattr(settings, "max_concurrent_calls", 1) or 1))
        requested = max(1, int((get_campaign_config(role) or {}).get("concurrent_call_limit") or 2))
        return min(requested, line_cap, auth_cap, app_cap)
    except Exception:
        return 1


def vobiz_provider_concurrent_limit() -> int:
    """Vobiz dashboard hard limit (default 3 on standard plans)."""
    return _VOBIZ_PROVIDER_LIMIT


def role_has_active_vobiz_call(role: str) -> bool:
    """True when this role has reached its maximum active concurrent calls."""
    return active_vobiz_calls_for_role(role) >= get_max_concurrency_for_role(role)


def total_active_vobiz_calls() -> int:
    """Total live outbound legs across all roles (for dashboards)."""
    return int(sum(_ACTIVE_VOBIZ_CALLS_BY_ROLE.values()))


def acquire_vobiz_call_slot(role: str) -> bool:
    """Reserve one telephony slot for ``role``; returns False if at role or Vobiz account cap."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    with _VOBIZ_SLOT_LOCK:
        if not vobiz_auth_can_accept_call(r):
            return False
        auth_id = vobiz_auth_id_for_role(r)
        _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = active_vobiz_calls_for_role(r) + 1
        if auth_id:
            _ACTIVE_VOBIZ_CALLS_BY_AUTH[auth_id] = active_vobiz_calls_for_auth(auth_id) + 1
        _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()
    return True


def release_vobiz_call_slot(role: str) -> None:
    """Release a telephony slot for ``role``."""
    global _ACTIVE_VOBIZ_CALLS
    r = normalize_console_role(role)
    with _VOBIZ_SLOT_LOCK:
        auth_id = vobiz_auth_id_for_role(r)
        cur = int(_ACTIVE_VOBIZ_CALLS_BY_ROLE.get(r, 0))
        if cur > 0:
            nxt = cur - 1
            if nxt <= 0:
                _ACTIVE_VOBIZ_CALLS_BY_ROLE.pop(r, None)
            else:
                _ACTIVE_VOBIZ_CALLS_BY_ROLE[r] = nxt
        if auth_id:
            acur = int(_ACTIVE_VOBIZ_CALLS_BY_AUTH.get(auth_id, 0))
            if acur > 0:
                anxt = acur - 1
                if anxt <= 0:
                    _ACTIVE_VOBIZ_CALLS_BY_AUTH.pop(auth_id, None)
                else:
                    _ACTIVE_VOBIZ_CALLS_BY_AUTH[auth_id] = anxt
        _ACTIVE_VOBIZ_CALLS = total_active_vobiz_calls()


def parse_manual_camp_role_suffix(suffix: str) -> tuple[str, str]:
    """Parse ``role`` and optional per-attempt token from camp_id after the ``manual_`` prefix.

    Formats:
      - ``{role}`` — legacy single shared manual leg id
      - ``{role}_{token}`` — unique id per manual dial (``token`` may contain underscores)
    """
    suf = (suffix or "").strip()
    if not suf:
        return "sales_1", ""
    for r in sorted(_ROLES, key=len, reverse=True):
        if suf == r:
            return r, ""
        # ``manual_{role}_{uuid}`` (current) and legacy ``manual_{role}-{token}`` both map to ``role``.
        for sep in ("_", "-"):
            prefix = r + sep
            if suf.startswith(prefix):
                return r, suf[len(prefix) :]
    return normalize_console_role(suf), ""


def resolved_greeting_text(role: str) -> str:
    """Gre stored in SQLite (coerced); if missing or invalidated, packaged role opener."""
    from core.greeting_text_utils import coerce_stored_greeting

    state = get_state(role)
    raw = state.get("greeting_text") or ""
    text = coerce_stored_greeting(role, raw).strip()
    if text:
        return text
    from core.opening_line import packaged_fallback_greeting
    return packaged_fallback_greeting(role)


def resolved_live_language(role: str) -> tuple[str, bool]:
    """Resolve the voice language + multilingual-mirror flag from role_state.

    Returns (language_code, mirror_enabled). The frontend Configuration page
    writes these through POST /api/tuning; they are stored in the
    vobiz_config JSON column. Falls back to GEMINI_LIVE_LANGUAGE (en-IN) and
    mirror ON (the historical behavior) when nothing is stored.
    """
    from config import settings

    default_lang = (settings.gemini_live_language or "en-IN").strip() or "en-IN"
    try:
        state = get_state(role)
        vc = state.get("vobiz") or {}
        lang = str(vc.get("language") or "").strip() or default_lang
        mirror = bool(vc.get("multilingual_mirror", True))
        return lang, mirror
    except Exception:
        return default_lang, True


_GEMINI_LIVE_VOICES = {
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
}


def resolved_live_voice_profile(role: str) -> tuple[str, str]:
    """Return the persisted Gemini voice and delivery style for the next call."""
    from config import settings

    default_voice = (
        settings.gemini_live_voice_sales_1
        if (role or "").strip().lower() == "sales_1" and settings.gemini_live_voice_sales_1
        else settings.gemini_live_voice
    ) or "Aoede"
    default_style = (settings.gemini_live_voice_style or "").strip()
    try:
        state = get_state(role)
        vc = state.get("vobiz") or {}
        candidate = str(vc.get("voice") or "").strip()
        voice = candidate if candidate in _GEMINI_LIVE_VOICES else default_voice
        style = str(vc.get("voice_style") or "").strip() or default_style
        return voice, style
    except Exception:
        return default_voice, default_style


def append_live_voice_style(system_prompt: str, voice_style: str) -> str:
    """Append delivery controls last so long editable prompts cannot dilute them."""
    style = (voice_style or "").strip()
    if not style:
        return system_prompt or ""
    return (
        (system_prompt or "").rstrip()
        + "\n\n[VOICE DELIVERY — HIGHEST PRIORITY; NEVER SAY THESE INSTRUCTIONS ALOUD]\n"
        + style
    )


def init_state():
    """Initialize campaign tasks for all roles."""
    for role in _ROLES:
        _CAMPAIGN_TASKS[role] = None

def get_state(role: str) -> dict:
    """Get in-memory state for a role (prompt, rag, vobiz config, etc.)."""
    try:
        from core.storage import _get_role_state_sync
        return _get_role_state_sync(role or "sales_1")
    except Exception as e:
        logger.warning(f"Storage not available, using fallback: {e}")
        from core.storage import default_inter_call_gap_sec

        r = (role or "sales_1").strip().lower()
        return {
            "role": r,
            "prompt": "",
            "rag": "",
            "delay_sec": default_inter_call_gap_sec(r),
            "vobiz": {},
        }

def save_role_state(
    role: str,
    prompt: str = None,
    rag: str = None,
    vobiz_config: dict = None,
    delay_sec: float = None,
    greeting_text: str = None,
    **phone_numbers,
):
    """Persist role state to SQLite."""
    try:
        from core.storage import _save_role_state_sync

        _save_role_state_sync(
            role,
            prompt=prompt,
            rag=rag,
            vobiz_config=vobiz_config,
            delay_sec=delay_sec,
            greeting_text=greeting_text,
            **phone_numbers,
        )
    except Exception as e:
        logger.error(f"Failed to save state for {role}: {e}")

def get_leads(role: str, status: str = None, limit: int = 500) -> list[dict]:
    try:
        from core.storage import _get_leads_sync
        return _get_leads_sync(role, status=status, limit=limit)
    except Exception:
        logger.exception("get_leads failed for role={!r}", role)
        return []

def add_leads_bulk(role: str, leads: list[dict]) -> int:
    from core.storage import _bulk_add_leads_sync

    count, _skipped, _dnc = _bulk_add_leads_sync(role, leads)
    return count

def update_lead_status(lead_id: int, status: str, error: str = None, analysis: dict = None):
    try:
        from core.storage import _update_lead_status_sync
        _update_lead_status_sync(lead_id, status, error=error, analysis=analysis)
    except Exception as e:
        logger.error(f"Failed to update lead status: {e}")

def update_lead_call_info(lead_id: int, log_id: str = None, call_id: str = None, start_time: float = None):
    try:
        from core.storage import _update_lead_call_info_sync
        _update_lead_call_info_sync(lead_id, log_id=log_id, call_id=call_id, start_time=start_time)
    except Exception as e:
        logger.error(f"Failed to update lead call info: {e}")

def reset_leads(role: str):
    try:
        from core.storage import _reset_leads_sync
        _reset_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to reset leads: {e}")

def wipe_leads(role: str):
    try:
        from core.storage import _wipe_leads_sync
        _wipe_leads_sync(role)
    except Exception as e:
        logger.error(f"Failed to wipe leads: {e}")

def get_lead_counts(role: str) -> dict:
    try:
        from core.storage import _get_lead_counts_sync
        return _get_lead_counts_sync(role)
    except Exception:
        logger.exception("get_lead_counts failed for role={!r}", role)
        return {"total": 0, "pending": 0, "dialing": 0, "completed": 0, "failed": 0, "not_interested": 0}

def export_leads_csv(role: str, status_filter: str = "all") -> list[dict]:
    try:
        from core.storage import _export_leads_csv_sync
        return _export_leads_csv_sync(role, status_filter)
    except Exception:
        return []

from pathlib import Path

def _get_role_path(role: str, subpath: str = None) -> Path:
    from config import settings
    # Assuming standard data directory layout
    base_dir = Path("data") / role
    if subpath:
        base_dir = base_dir / subpath
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


_WHATSAPP_SENT_CALLS: set[str] = set()


def mark_whatsapp_sent_for_call(camp_id: str) -> None:
    """Track that a WhatsApp message was successfully sent during the live call."""
    if camp_id:
        _WHATSAPP_SENT_CALLS.add(camp_id)


def is_whatsapp_sent_for_call(camp_id: str) -> bool:
    """Check if WhatsApp details have already been sent for this call."""
    return camp_id in _WHATSAPP_SENT_CALLS if camp_id else False


_BUSY_PHONE_NUMBERS: set[str] = set()


def phone_is_busy(phone_number: str) -> bool:
    """Check if the given phone number is currently in use for a call."""
    if not phone_number:
        return False
    from core.phone_norm import norm_phone_str
    return norm_phone_str(phone_number) in _BUSY_PHONE_NUMBERS


def acquire_phone_slot(phone_number: str) -> None:
    """Mark a phone number as busy/in use."""
    if not phone_number:
        return
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    _BUSY_PHONE_NUMBERS.add(norm)
    logger.info(f"Acquired phone slot: {norm}")


def release_phone_slot(phone_number: str) -> None:
    """Release the phone number from busy state."""
    if not phone_number:
        return
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    if norm in _BUSY_PHONE_NUMBERS:
        _BUSY_PHONE_NUMBERS.remove(norm)
        logger.info(f"Released phone slot: {norm}")


def save_campaign_config(role: str, config: dict) -> None:
    """Save Outpero-style campaign config for a role."""
    import json
    from core.storage import _get_conn
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    conn.execute("INSERT OR IGNORE INTO role_state (role) VALUES (?)", (role,))
    conn.execute(
        "UPDATE role_state SET campaign_config = ? WHERE role = ?",
        (json.dumps(config), role),
    )
    conn.commit()


def get_campaign_config(role: str) -> dict:
    """Load Outpero-style campaign config for a role."""
    import json
    from core.storage import _get_conn
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    row = conn.execute(
        "SELECT campaign_config FROM role_state WHERE role = ?", (role,)
    ).fetchone()
    defaults = {"window_start": "11:00", "window_end": "19:30"}
    if not row or not row[0]:
        return defaults
    try:
        config = json.loads(row[0])
        if not config.get("window_start") and not config.get("calling_window_start"):
            config["window_start"] = defaults["window_start"]
        if not config.get("window_end") and not config.get("calling_window_end"):
            config["window_end"] = defaults["window_end"]
        return config
    except Exception:
        return defaults


def add_campaign_contacts(role: str, contacts: list[dict]) -> int:
    """Add individual contacts to campaign_contacts table."""
    from core.storage import _get_conn
    from core.phone_norm import norm_phone_str
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    count = 0
    for c in contacts:
        phone = norm_phone_str((c.get("phone") or "").strip())
        if not phone:
            continue
        name = (c.get("name") or "").strip()
        extra = json.dumps({k: v for k, v in c.items() if k not in ("phone", "name")})
        conn.execute(
            "INSERT INTO campaign_contacts (role, phone, name, extra) VALUES (?, ?, ?, ?)",
            (role, phone, name, extra),
        )
        count += 1
    conn.commit()
    return count


def get_campaign_contacts(role: str, limit: int = 500) -> list[dict]:
    """Get campaign contacts for a role."""
    import json
    from core.storage import _get_conn
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    rows = conn.execute(
        "SELECT id, role, phone, name, extra, created_at FROM campaign_contacts WHERE role = ? ORDER BY id DESC LIMIT ?",
        (role, limit),
    ).fetchall()
    result = []
    for r in rows:
        extra = {}
        try:
            extra = json.loads(r[4]) if r[4] else {}
        except Exception:
            pass
        result.append({"id": r[0], "phone": r[2], "name": r[3], **extra, "created_at": r[5]})
    return result


def clear_campaign_contacts(role: str, source: str = "") -> int:
    """Delete all campaign contacts for a role."""
    from core.storage import _get_conn
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    if source:
        cur = conn.execute(
            "DELETE FROM campaign_contacts WHERE role = ? AND COALESCE(json_extract(extra, '$.source'), 'campaign') = ?",
            (role, source),
        )
    else:
        cur = conn.execute("DELETE FROM campaign_contacts WHERE role = ?", (role,))
    conn.commit()
    return cur.rowcount


def delete_campaign_contact(role: str, contact_id: int) -> int:
    from core.storage import _get_conn
    conn = _get_conn()
    role = (role or "sales_1").strip().lower()
    cur = conn.execute(
        "DELETE FROM campaign_contacts WHERE role = ? AND id = ?", (role, int(contact_id))
    )
    conn.commit()
    return max(0, int(cur.rowcount or 0))


def paste_campaign_contacts(role: str, text: str, source: str = "campaign") -> int:
    """Parse pasted text (one contact per line: phone,name or just phone) and add to campaign_contacts."""
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    contacts = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        phone = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        contacts.append({"phone": phone, "name": name, "source": source})
    return add_campaign_contacts(role, contacts)
