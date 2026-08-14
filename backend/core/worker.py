"""Campaign worker — dials leads one-at-a-time per role; roles run in parallel."""

from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from loguru import logger
from core import kv_cache
from core.state import (
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    _LAST_WORKER_ACTIVITY,
    acquire_vobiz_call_slot,
    release_vobiz_call_slot,
    role_has_active_vobiz_call,
    active_vobiz_calls_for_role,
    total_active_vobiz_calls,
    vobiz_auth_can_accept_call,
    get_state,
    normalize_console_role,
    phone_is_busy,
    acquire_phone_slot,
    release_phone_slot,
)
from core.storage import (
    due_schedules,
    expired_running_schedules,
    mark_schedule_status,
    promote_due_scheduled_callbacks,
    role_has_future_callback_scheduled,
    get_leads,
    update_lead_status,
    update_lead_sandbox,
    update_lead_call_info,
    save_role_state,
    reset_leads,
    wipe_leads,
    get_lead_counts,
    export_leads_csv,
    set_campaign_want_running,
    get_next_immediate_callback,
    update_scheduled_callback_status,
    is_duplicate_lead,
)
from core.state import add_leads_bulk
from core.utils import _build_opening_line
from core.greeting_pcm import load_recorded_greeting_pcm
from config import settings
from core.campaign_hours import is_campaign_quiet_hours, quiet_hours_block_message
from core.vobiz_credentials import resolve_vobiz_credentials
from core.outbound_numbers import get_all_outbound_numbers
from core.call_line_coordinator import (
    failed_retry_delay_sec,
    phone_is_resting,
    phone_rest_remaining,
    rest_retry_enabled,
    run_phone_rest_cycle,
)

import threading

_PHONE_HOURLY_LIMITS: dict[str, int] = {}
_ACTIVE_DIALER_PHONES: set[str] = set()
_ACTIVE_DIALER_BY_ROLE: dict[str, set[str]] = {}
_DIALER_PHONE_ROLE: dict[str, str] = {}
_DIALER_LOCK = threading.Lock()
_CAMPAIGN_MODE: dict[str, str] = {}


def set_campaign_mode(role: str, mode: str) -> None:
    _CAMPAIGN_MODE[(role or "sales_1").strip().lower()] = mode


def get_campaign_mode(role: str) -> str:
    return _CAMPAIGN_MODE.get((role or "sales_1").strip().lower(), "general")

def _sales_roles_use_separate_vobiz_accounts() -> bool:
    return False


def acquire_dialer_slot(phone_number: str, role: str = "") -> bool:
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    r = (role or "").strip().lower()
    with _DIALER_LOCK:
        if norm in _ACTIVE_DIALER_PHONES:
            return True
        _max_slots = max(1, int(getattr(settings, "max_concurrent_calls", 4) or 4))
        if r in ("sales_1",):
            role_set = _ACTIVE_DIALER_BY_ROLE.setdefault(r, set())
            if len(role_set) >= _MAX_LINES_PER_ROLE:
                return False
        if len(_ACTIVE_DIALER_PHONES) >= _max_slots:
            return False
        _ACTIVE_DIALER_PHONES.add(norm)
        if r in ("sales_1",):
            _ACTIVE_DIALER_BY_ROLE.setdefault(r, set()).add(norm)
            _DIALER_PHONE_ROLE[norm] = r
        logger.info(f"Phone {norm} acquired active dialer slot (role={r or '?'}). Active: {list(_ACTIVE_DIALER_PHONES)}")
        return True


def release_dialer_slot(phone_number: str, role: str = "") -> None:
    from core.phone_norm import norm_phone_str
    norm = norm_phone_str(phone_number)
    r = (role or _DIALER_PHONE_ROLE.get(norm) or "").strip().lower()
    with _DIALER_LOCK:
        if norm in _ACTIVE_DIALER_PHONES:
            _ACTIVE_DIALER_PHONES.remove(norm)
        if r and r in _ACTIVE_DIALER_BY_ROLE and norm in _ACTIVE_DIALER_BY_ROLE[r]:
            _ACTIVE_DIALER_BY_ROLE[r].discard(norm)
        _DIALER_PHONE_ROLE.pop(norm, None)
        logger.info(f"Phone {norm} released active dialer slot. Active: {list(_ACTIVE_DIALER_PHONES)}")


_background_tasks: set[asyncio.Task] = set()
_callback_tasks_in_flight: set[str] = set()  # roles with an active callback task in scheduler loop
_ROLE_LEAD_LOCKS: dict[str, asyncio.Lock] = {}

def get_role_lead_lock(role: str) -> asyncio.Lock:
    if role not in _ROLE_LEAD_LOCKS:
        _ROLE_LEAD_LOCKS[role] = asyncio.Lock()
    return _ROLE_LEAD_LOCKS[role]

_MAX_LINES_PER_ROLE = max(1, int(os.getenv("MAX_LINES_PER_ROLE", "2")))
_GLOBAL_CALL_SEMAPHORE = asyncio.Semaphore(max(1, int(getattr(settings, "max_concurrent_calls", 4) or 4)))
_ROLE_SEMAPHORES = {
    "sales_1": asyncio.Semaphore(_MAX_LINES_PER_ROLE),
}

def yield_alternating_turn(role: str):
    return None

async def check_and_acquire_alternating_turn(role: str) -> bool:
    return True





# Once a lead is in ``dialing`` longer than this (process restart or hung WS), recycle it.
_STALE_DIALING_AFTER_SEC = 600
# Wait time when the queue becomes empty before exiting (gives the operator a chance to upload mid-run).
_EMPTY_QUEUE_GRACE_SEC = 30
# Fallback gap (seconds) between consecutive outbound calls.
# Sales roles: 120–180s pause per line (2–3 min). ~28–30 calls/phone/hr → ~48–50/hr per role.
_ENV_INTER_CALL_GAP_SEC = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_SEC", "150"))
_OUTBOUND_GAP_MIN_SEC = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_MIN_SEC", "120"))
_OUTBOUND_GAP_MAX_SEC = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_MAX_SEC", "180"))
# Per outbound line: ~28–30/hr → ~48–50/hr per role (2 lines each, both roles parallel)
_CAMPAIGN_HOURLY_CALLS_PER_PHONE = int(os.getenv("CAMPAIGN_HOURLY_CALLS_PER_PHONE", "30"))
_CAMPAIGN_HOURLY_CALLS_PER_ROLE = int(os.getenv("CAMPAIGN_HOURLY_CALLS_PER_ROLE", "60"))
_INTER_CALL_GAP_MIN = 0.0
_INTER_CALL_GAP_MAX = 1200.0  # 20 min cap

# Round-robin phone number state per role
# Tracks: {role: {"phone_index": int, "hour_start": float, "total_calls_this_hour": int}}
_PHONE_ROUND_ROBIN_STATE: dict[str, dict] = {}
# Maximum calls per phone number before rotating to next number
_CALLS_PER_PHONE_MIN = 1
_CALLS_PER_PHONE_MAX = 1
# Maximum total calls per hour per role (sales_1 targets 40–50)
_MAX_CALLS_PER_HOUR = _CAMPAIGN_HOURLY_CALLS_PER_ROLE
# Maximum calls per upload source per day (IST midnight boundary)
_CALLS_PER_SOURCE_DAILY_MAX = 5000
# Round-robin cursor so multiple upload files share the dial queue fairly.
_SOURCE_RR_CURSOR: dict[str, int] = {}
_PENDING_FETCH_LIMIT = max(200, int(os.getenv("CAMPAIGN_PENDING_FETCH_LIMIT", "2500")))


def _lead_upload_source(lead: dict) -> str:
    try:
        raw_ext = lead.get("extra", "{}")
        p_extra = json.loads(raw_ext) if isinstance(raw_ext, str) else (raw_ext or {})
    except Exception:
        p_extra = {}
    return str(p_extra.get("upload_source") or "Manual Calls / Direct Entry").strip()


def _fair_interleave_pending_by_source(pending: list[dict], role: str) -> list[dict]:
    """Interleave pending leads across upload_source buckets (prevents one Excel file starving others)."""
    if len(pending) <= 1:
        return pending
    buckets: dict[str, list[dict]] = {}
    for p in pending:
        src = _lead_upload_source(p)
        buckets.setdefault(src, []).append(p)
    if len(buckets) <= 1:
        return pending
    sources = sorted(buckets.keys())
    start = _SOURCE_RR_CURSOR.get(role, 0) % len(sources)
    ordered = sources[start:] + sources[:start]
    _SOURCE_RR_CURSOR[role] = (start + 1) % len(sources)
    max_len = max(len(b) for b in buckets.values())
    out: list[dict] = []
    for i in range(max_len):
        for src in ordered:
            if i < len(buckets[src]):
                out.append(buckets[src][i])
    return out


def _is_vobiz_concurrent_limit_error(ve: object) -> bool:
    msg = str(getattr(ve, "message", "") or "").lower()
    return "concurrent" in msg and "limit" in msg


def inter_call_gap_display_seconds_for_role(role: str) -> float:
    """Stable inter-call gap for dashboard/API polls (no random resample every request)."""
    from core.storage import STRICT_CORE_GAP_SEC, is_strict_gap_core_role, _get_role_state_sync

    role_key = (role or "sales_1").strip().lower()
    if is_strict_gap_core_role(role_key):
        return float(STRICT_CORE_GAP_SEC)
    try:
        rs = _get_role_state_sync(role_key)
        if rs and rs.get("delay_sec") is not None:
            return float(rs["delay_sec"])
    except Exception:
        pass
    return 1.0


def inter_call_gap_seconds_for_role(role: str) -> float:
    """Pause after each dial before the next pending lead."""
    from core.storage import STRICT_CORE_GAP_SEC, is_strict_gap_core_role, _get_role_state_sync

    role_key = (role or "sales_1").strip().lower()
    if is_strict_gap_core_role(role_key):
        return float(STRICT_CORE_GAP_SEC)
    try:
        rs = _get_role_state_sync(role_key)
        if rs and rs.get("delay_sec") is not None:
            return max(0.5, float(rs["delay_sec"]))
    except Exception:
        pass
    return 1.0


async def inter_call_gap_seconds_for_phone(phone_number: str, role: str) -> float:
    """Per-line pause after each outbound leg — respects role_state delay_sec."""
    from core.storage import (
        STRICT_CORE_GAP_MAX_SEC,
        STRICT_CORE_GAP_MIN_SEC,
        STRICT_CORE_GAP_SEC,
        get_role_state,
        is_strict_gap_core_role,
    )

    role_key = (role or "sales_1").strip().lower()
    if is_strict_gap_core_role(role_key):
        try:
            rs = await get_role_state(role_key)
            delay = float(rs.get("delay_sec") or STRICT_CORE_GAP_SEC)
            return max(STRICT_CORE_GAP_MIN_SEC, min(STRICT_CORE_GAP_MAX_SEC, delay))
        except Exception:
            return 5.0
    try:
        rs = await get_role_state(role_key)
        if rs and rs.get("delay_sec") is not None:
            delay = float(rs["delay_sec"])
            logger.info("Pacing: using set inter-call gap {:.1f}s for phone={} role={}", delay, phone_number, role_key)
            return max(0.5, delay)
    except Exception:
        pass
    return 1.0


async def flush_pending_whatsapp_after_call(role: str, camp_id: str) -> None:
    """Send queued WhatsApp package the moment the call disconnects (keeps AI talking during call)."""
    from core.state import _CAMPAIGN_DATA

    info = _CAMPAIGN_DATA.get(camp_id) if camp_id else None
    if not isinstance(info, dict) or not info.get("_whatsapp_pending"):
        return
    if info.get("_whatsapp_sent"):
        info.pop("_whatsapp_pending", None)
        return
    summary = str(info.get("_whatsapp_pending_summary") or "Project Details")
    name = str(info.get("name") or "")
    lead_id = info.get("_lead_id")
    try:
        from services.whatsapp_outcome import send_agent_promised_whatsapp

        result = await send_agent_promised_whatsapp(
            role=role,
            camp_id=camp_id,
            lead_id=int(lead_id) if lead_id else None,
            lead_name=name,
            summary=summary,
        )
        if result.get("sent"):
            info.pop("_whatsapp_pending", None)
            info.pop("_whatsapp_pending_summary", None)
            info["_whatsapp_sent"] = True
    except Exception as e:
        logger.exception("Post-call WhatsApp flush failed camp_id={}: {}", camp_id, e)


async def _notify_email_sent_event(role: str, lead_id: int | None) -> None:
    try:
        from core.events import get_event_bus
        await get_event_bus().publish("email_sent", role=role, lead_id=lead_id)
    except Exception:
        pass


async def _send_failed_dial_whatsapp(
    role: str,
    lead_id: int | None,
    phone: str,
    lead_name: str,
    *,
    disposition: str = "Failed",
) -> None:
    """Post-failed-dial WhatsApp using disposition-aware messaging."""
    if role not in ("sales_1",) or not phone:
        return
    try:
        from core.storage import get_lead_whatsapp_sent, mark_whatsapp_sent
        from services.whatsapp_leads import send_whatsapp_disposition_message

        if lead_id and await get_lead_whatsapp_sent(lead_id):
            return
        wa_result = await send_whatsapp_disposition_message(
            phone,
            disposition=disposition,
            summary="We tried reaching you regarding Solitaire Unity.",
            lead_name=lead_name,
        )
        if wa_result.get("sent") and lead_id:
            await mark_whatsapp_sent(lead_id)
    except Exception as e:
        logger.exception("Failed-dial WhatsApp send failed for {}: {}", phone, e)


def get_next_phone_number(role: str, vobiz_cfg: dict) -> str:
    """Get the next phone number to use for dialing (alternate after every call)."""
    from core.outbound_numbers import dialable_outbound_numbers

    numbers = dialable_outbound_numbers(role, vobiz_cfg)
    if not numbers:
        # Fallback to single number resolution
        auth_id, auth_token, from_number, public_url = resolve_vobiz_credentials(role, vobiz_cfg)
        return from_number
    
    if len(numbers) == 1:
        return numbers[0]
    
    now = time.time()
    state = _PHONE_ROUND_ROBIN_STATE.get(role, {})
    
    # Initialize state if needed
    if not state:
        state = {
            "phone_index": 0,
            "calls_on_current_phone": 0,
            "hour_start": now,
            "total_calls_this_hour": 0,
        }
        _PHONE_ROUND_ROBIN_STATE[role] = state
    
    # Check if an hour has passed since we started tracking
    hour_elapsed = now - state.get("hour_start", now)
    if hour_elapsed >= 3600:
        # Reset hourly counters
        state["hour_start"] = now
        state["total_calls_this_hour"] = 0
        state["phone_index"] = 0
        state["calls_on_current_phone"] = 0
    
    # Check if we've exceeded hourly limit
    if state["total_calls_this_hour"] >= _MAX_CALLS_PER_HOUR:
        logger.info(f"Hourly call limit ({_MAX_CALLS_PER_HOUR}) reached for {role}")
        return numbers[state.get("phone_index", 0) % len(numbers)]
    
    # Get current phone number
    phone_index = state.get("phone_index", 0) % len(numbers)
    selected_number = numbers[phone_index]
    
    # Increment calls on current phone
    state["calls_on_current_phone"] = state.get("calls_on_current_phone", 0) + 1
    
    # Check if we've exceeded max calls for this phone number
    max_for_phone = _CALLS_PER_PHONE_MAX
    if state["calls_on_current_phone"] >= max_for_phone:
        # Rotate to next phone number
        next_index = (phone_index + 1) % len(numbers)
        state["phone_index"] = next_index
        state["calls_on_current_phone"] = 0
        logger.info(f"Rotating phone number: {phone_index + 1} -> {next_index + 1} after {max_for_phone} calls")
    else:
        # Keep same phone number for next call
        pass
    
    # Increment hourly counter
    state["total_calls_this_hour"] = state.get("total_calls_this_hour", 0) + 1
    
    logger.info(f"Selected phone {phone_index + 1} for {role}, next will be phone {state['phone_index'] + 1}")
    
    return selected_number


async def _cancellable_sleep(role: str, total_seconds: float) -> bool:
    """Sleep in 0.5s slices but bail out as soon as the campaign is stopped.

    Returns True if the wait completed normally, False if the campaign was cancelled.
    """
    end = time.time() + max(0.0, total_seconds)
    while time.time() < end:
        if not _CAMPAIGN_TASKS.get(role):
            return False
        await asyncio.sleep(min(0.5, end - time.time()))
    return True


async def release_orphaned_dialing_leads(
    role: str,
    *,
    to_status: str = "failed",
    error: str = "Campaign stopped before call completed.",
) -> int:
    """Mark in-flight ``dialing`` rows terminal when the worker is not running (stop / quiet hours)."""
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to release orphaned dialing leads role={}", role)
        return 0
    released = 0
    for r in rows:
        try:
            await update_lead_status(int(r["id"]), to_status, error=error)
            released += 1
        except Exception:
            logger.exception("release dialing lead id={}", r.get("id"))
    if released:
        logger.info(
            "Released {} orphaned dialing lead(s) → {} for role={}",
            released,
            to_status,
            role,
        )
    return released


async def _recover_stale_dialing(role: str) -> int:
    """Worker startup: previous process may have crashed with leads stuck on ``dialing``.

    Reset them to ``pending`` so this run can pick them up. Returns count recovered.
    """
    try:
        rows = await get_leads(role, status="dialing", limit=10000)
    except Exception:
        logger.exception("Failed to recover stale dialing leads")
        return 0
    recovered = 0
    for r in rows:
        await update_lead_status(r["id"], "pending")
        recovered += 1
    if recovered:
        logger.info(f"Recovered {recovered} stale 'dialing' leads → 'pending' for role={role}")
    return recovered



def _prime_opening_audio(call_id: str, role: str, opening: str) -> None:
    """Sync fast-path: load ``greeting_{role}.pcm`` from disk if present.

    For calls that need on-demand capture, use ``ensure_opening_pcm`` (async) instead.
    """
    if settings.gemini_live_first_opening:
        logger.debug(
            "Skip opening PCM prime for call_id={} — Gemini Live speaks first",
            call_id,
        )
        return
    if call_id not in _CAMPAIGN_DATA:
        return
    recorded = load_recorded_greeting_pcm(role, greeting_text=(opening or "").strip())
    if recorded:
        _CAMPAIGN_DATA[call_id]["opening_pcm"] = recorded
        logger.info(
            "Primed recorded greeting for call_id={} role={} ({} bytes @ {} Hz)",
            call_id,
            role,
            len(recorded[0]),
            recorded[1],
        )


async def _execute_scheduled_callback(role: str, cb: dict, outbound_phone: str = None) -> None:
    """Execute a single scheduled callback immediately.

    Reuses the same ``make_vobiz_call`` + ``_CAMPAIGN_DATA`` infrastructure
    as normal campaign leads. Returns when the call completes or fails.

    Creates a lead record so the transcript + analysis are saved and visible
    in the dashboard like any other campaign call.
    """
    from services.vobiz_bridge import make_vobiz_call, VobizCallError

    cb_id = int(cb["id"])
    cb_phone = cb.get("phone", "")
    cb_name = cb.get("name", "") or "Callback"

    if not cb_phone:
        await update_scheduled_callback_status(cb_id, "failed", error="No phone number")
        return

    await update_scheduled_callback_status(cb_id, "calling")

    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)
    cb_outbound = (cb.get("outbound_phone") or outbound_phone or "").strip()
    if cb_outbound:
        v_from = cb_outbound
    elif outbound_phone:
        v_from = outbound_phone

    if not v_auth_id or not v_token or not v_base or not v_from:
        await update_scheduled_callback_status(cb_id, "failed", error="Telephony not configured")
        return

    call_id = f"sched_cb_{role}_{cb_id}_{uuid.uuid4().hex[:8]}"

    # Create (or reuse) a lead record so the dashboard shows this callback call
    # and auto-analysis (transcript + rating) is triggered. Prefer the original
    # lead_id from the scheduled callback row for conversation memory.
    lead_id = None
    try:
        from core.storage import (
            find_or_create_callback_lead as _find_or_create_cb,
            update_lead_call_info as _cb_call_info,
            get_lead as _get_lead_cb,
        )
        cb_lead_id = cb.get("lead_id")
        if cb_lead_id:
            lead_id = int(cb_lead_id)
            orig = await _get_lead_cb(role, lead_id)
            if not orig:
                lead_id = await _find_or_create_cb(role, phone=cb_phone, name=cb_name)
            elif orig.get("name"):
                cb_name = str(orig.get("name") or cb_name or "Callback").strip()
        else:
            lead_id = await _find_or_create_cb(role, phone=cb_phone, name=cb_name)
        await update_lead_status(lead_id, "dialing")
        await _cb_call_info(lead_id, start_time=time.time(), outbound_phone=v_from)
        logger.info("Callback lead ready: id={} phone={} (memory linked)", lead_id, cb_phone)
    except Exception:
        logger.exception("Failed to create callback lead for {}", cb_phone)
        lead_id = None
    phone_slot_held = False
    cb_type = (cb.get("callback_type") or "").strip()
    fu_num = cb.get("follow_up_number")
    cb_analysis: dict = {}
    raw_aj = cb.get("analysis_json") or "{}"
    try:
        cb_analysis = json.loads(raw_aj) if isinstance(raw_aj, str) else (raw_aj or {})
    except Exception:
        cb_analysis = {}

    _CAMPAIGN_DATA[call_id] = {
        "name": cb_name,
        "phone": cb_phone,
        "_role": role,
        "_scheduled_callback_id": cb_id,
        "_is_scheduled_callback": True,
        "_callback_type": cb_type,
        "_follow_up_number": fu_num,
        "_follow_up_memory": cb_analysis,
    }
    if lead_id is not None:
        _CAMPAIGN_DATA[call_id]["_lead_id"] = lead_id
        _CAMPAIGN_DATA[call_id]["id"] = lead_id

    from core.dnc import is_phone_blocked
    if is_phone_blocked(cb_phone):
        logger.warning(f"Aborting scheduled callback dialing: {cb_phone} is in DNC list")
        await update_scheduled_callback_status(cb_id, "failed", error="Phone number is blocked (DNC)")
        if lead_id is not None:
            await update_lead_status(lead_id, "failed", error="Phone number is blocked (DNC)")
        return

    acquire_phone_slot(v_from)
    phone_slot_held = True

    slot_acquired = False
    sem_acquired = False
    try:
        await _GLOBAL_CALL_SEMAPHORE.acquire()
        sem_acquired = True

        opening = _build_opening_line(_CAMPAIGN_DATA[call_id], role)
        from core.greeting_pcm import ensure_opening_pcm, ensure_name_verify_pcm_for_call

        await ensure_opening_pcm(call_id, role, opening)
        await ensure_name_verify_pcm_for_call(call_id, role)

        from core.camp_session import prepare_outbound_call_session

        await prepare_outbound_call_session(
            call_id, role, _CAMPAIGN_DATA[call_id], v_base, lead_id=lead_id
        )

        if not vobiz_auth_can_accept_call(role):
            logger.warning("Scheduled callback deferred — Vobiz account at concurrent cap for role={}", role)
            await update_scheduled_callback_status(cb_id, "scheduled", error="Vobiz concurrent limit — retry soon")
            return

        if not acquire_vobiz_call_slot(role):
            logger.warning("Scheduled callback deferred — could not acquire Vobiz slot for role={}", role)
            return
        slot_acquired = True
        logger.info(f"Scheduled callback call: {cb_name} ({cb_phone})")

        try:
            hangup_url = f"{v_base.rstrip('/')}/vobiz/hangup" if v_base else ""
            await make_vobiz_call(
                to=cb_phone,
                from_=v_from,
                answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}&role={role}",
                auth_id=v_auth_id,
                auth_token=v_token,
                hangup_url=hangup_url,
                record=True,
            )
        except VobizCallError as ve:
            from core.outbound_numbers import (
                is_vobiz_insufficient_balance_error,
                mark_vobiz_auth_low_balance,
            )

            if is_vobiz_insufficient_balance_error(ve):
                mark_vobiz_auth_low_balance(v_auth_id)
                logger.error(
                    "Vobiz insufficient balance (auth {}) — callback {} rescheduled, lead {} pending",
                    v_auth_id,
                    cb_id,
                    lead_id,
                )
                await update_scheduled_callback_status(
                    cb_id,
                    "scheduled",
                    error=f"Vobiz {ve.status}: {ve.message} — retry after top-up",
                )
                if lead_id is not None:
                    await update_lead_status(lead_id, "pending", error=None)
                return
            await update_scheduled_callback_status(
                cb_id, "failed", error=f"Vobiz {ve.status}: {ve.message}"
            )
            if lead_id is not None:
                await update_lead_status(lead_id, "no response", error=f"Vobiz {ve.status}: {ve.message}")
                if role in ("sales_1",):
                    await _send_failed_dial_whatsapp(role, lead_id, cb_phone, cb_name, disposition="No Response")
            return

        answered = False
        call_started_at = time.time()
        max_ring_wait = float(os.getenv("OUTBOUND_MAX_RING_WAIT_SEC", "45") or 45)
        MAX_TOTAL_WAIT = 360

        while True:
            from core.camp_session import poll_camp_session_into_memory

            info = await poll_camp_session_into_memory(call_id, v_base)
            if not answered and info.get("_call_connected_at"):
                answered = True
                logger.info(f"Scheduled callback connected: {cb_name} ({cb_phone})")
            if answered and info.get("_call_ended_at"):
                logger.info(f"Scheduled callback ended: {cb_name}")
                break

            elapsed = time.time() - call_started_at
            if not answered and elapsed >= max_ring_wait:
                logger.warning(f"Scheduled callback no answer: {cb_phone}")
                break
            if elapsed >= MAX_TOTAL_WAIT:
                logger.warning(f"Scheduled callback exceeded max time: {cb_phone}")
                break

            await asyncio.sleep(2)

        if not answered and lead_id is not None:
            await update_lead_status(lead_id, "no answer", error="No answer / Timeout")
            if role in ("sales_1",):
                await _send_failed_dial_whatsapp(role, lead_id, cb_phone, cb_name, disposition="No Answer")

        await update_scheduled_callback_status(
            cb_id,
            "completed" if answered else "failed",
            error=None if answered else "No answer / Timeout",
        )

        # Mark original lead as callback_completed so it doesn't get re-dialed
        if answered and cb.get("lead_id"):
            try:
                await update_lead_status(cb["lead_id"], "callback_completed")
            except Exception:
                logger.warning(f"Failed to mark original lead {cb['lead_id']} as callback_completed")
        elif not answered and cb.get("lead_id"):
            try:
                orig_lead_id = cb["lead_id"]
                await _schedule_failed_call_retry(role, orig_lead_id, cb_phone, cb_name)
            except Exception as re:
                logger.exception("Failed to schedule retry for failed callback lead {}", cb.get("lead_id"))

    except Exception as e:
        logger.exception(f"Scheduled callback failed for {cb_phone}")
        await update_scheduled_callback_status(cb_id, "failed", error=str(e)[:300])
    finally:
        if slot_acquired:
            release_vobiz_call_slot(role)
        if phone_slot_held:
            release_phone_slot(v_from)
        _CAMPAIGN_DATA.pop(call_id, None)
        if sem_acquired:
            await asyncio.sleep(1.0)
            _GLOBAL_CALL_SEMAPHORE.release()


def _orchestrate_call_failure(
    role: str, lead_id: int, orchestration_job: dict | None,
    from_number: str, outcome: str,
) -> None:
    """Hand a failed live dial off to the bounded 3-attempt retry engine.

    Only runs in validated live mode (shadow mode just records the lead state
    change and leaves the workflow job untouched).
    """
    from core.orchestration_runtime import runtime_status
    if runtime_status()["mode"] != "live":
        return
    from datetime import datetime, timezone
    from core.orchestration_service import failed_call
    from core.storage import _get_conn
    attempt = int((orchestration_job or {}).get("attempt_number") or 1)
    cycle = str((orchestration_job or {}).get("source_id") or "")
    failed_call(
        _get_conn(), lead_id=lead_id,
        source="campaign",
        retry_cycle=cycle or str(lead_id), attempt=attempt, from_number=from_number,
        outcome=outcome, ended_at=datetime.now(timezone.utc),
    )


async def _process_single_lead(
    role: str, lead: dict, from_number: str, *, external_managed: bool = False,
    orchestration_job: dict | None = None,
):
    """Dial one lead with the given from_number, wait for result, finalize.

    Orchestration entry point: reuses the exact same Vobiz/session/transcript
    infrastructure as the campaign sub-workers but handles a single lead so the
    autonomous workflow dispatcher can dial job-by-job. Returns the answered
    state and call id for the dispatcher to finalize the workflow job.
    """
    lead_id = lead["id"]
    lead_phone = lead["phone"]
    lead_name = lead.get("name", "Unknown")

    # Cross-dialer lock: atomically mark the lead in-progress BEFORE dialing so
    # the source-blind legacy worker's pending scan can never double-grab a lead
    # that the orchestrator has already claimed in workflow_jobs. Every exit path
    # below resets the status (pending / no answer / failed / analysis outcome).
    await update_lead_status(lead_id, "dialing")

    call_id = str(uuid.uuid4())
    await update_lead_call_info(lead_id, start_time=time.time(), call_id=call_id)

    _CAMPAIGN_DATA[call_id] = {
        **lead,
        "_lead_id": lead_id,
        "_leadIndex": -1,
        "_role": role,
        "_call_id": call_id,
        "_outbound_phone": from_number,
    }

    # Memory bridge: attach stored lead memory so the live session continues the
    # conversation instead of starting cold (frozen read-only facts).
    try:
        from core import lead_memory as _lm
        from core.storage import _get_conn as _mem_conn
        _mem_txt = _lm.memory_context(_mem_conn(), lead_id)
        if _mem_txt:
            _CAMPAIGN_DATA[call_id]["_lead_memory_text"] = _mem_txt
    except Exception as _mem_e:
        logger.debug("lead memory attach skipped for lead {}: {}", lead_id, _mem_e)

    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    v_auth_id, v_token, _v_old_from, v_base = resolve_vobiz_credentials(role, v_cfg)
    v_from = from_number

    if not v_auth_id or not v_token or not v_base or not v_from:
        logger.error(f"Telephony not configured for role={role}. Skipping orchestration lead.")
        await update_lead_status(lead_id, "failed", error="Telephony not configured")
        _CAMPAIGN_DATA.pop(call_id, None)
        return {"answered": False, "call_id": call_id, "error": "Telephony not configured"}

    from services.vobiz_bridge import make_vobiz_call, VobizCallError
    phone_slot_held = False
    slot_acquired = False
    sem_acquired = False
    answered = False
    try:
        try:
            from services.campaign_live import set_active_campaign_call, clear_transcript_session
            set_active_campaign_call(call_id)
            clear_transcript_session(call_id)
        except Exception as _ce:
            logger.exception("campaign_live setup skipped: {}", _ce)

        opening = _build_opening_line(_CAMPAIGN_DATA[call_id], role)
        from core.greeting_pcm import ensure_opening_pcm, ensure_name_verify_pcm_for_call

        await ensure_opening_pcm(call_id, role, opening)
        if settings.scripted_name_verify_pcm:
            await ensure_name_verify_pcm_for_call(call_id, role)

        from core.camp_session import prepare_outbound_call_session

        await prepare_outbound_call_session(
            call_id, role, _CAMPAIGN_DATA[call_id], v_base, lead_id=lead_id
        )

        if phone_is_busy(v_from):
            await update_lead_status(lead_id, "pending")
            _CAMPAIGN_DATA.pop(call_id, None)
            return {"answered": False, "call_id": call_id, "error": "Line busy"}
        acquire_phone_slot(v_from)
        phone_slot_held = True

        await _GLOBAL_CALL_SEMAPHORE.acquire()
        sem_acquired = True

        if not acquire_vobiz_call_slot(role):
            await update_lead_status(lead_id, "pending")
            return {"answered": False, "call_id": call_id, "error": "Vobiz concurrent cap"}

        slot_acquired = True
        logger.info(
            f"Orchestration dial: {lead_name} ({lead_phone}) from {v_from} "
            f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
        )

        try:
            hangup_url = f"{v_base.rstrip('/')}/vobiz/hangup" if v_base else ""
            await make_vobiz_call(
                to=lead_phone, from_=v_from,
                answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}&role={role}",
                auth_id=v_auth_id, auth_token=v_token,
                hangup_url=hangup_url, record=True,
            )
        except VobizCallError as ve:
            from core.outbound_numbers import (
                is_vobiz_from_line_blocked_error,
                is_vobiz_insufficient_balance_error,
                mark_outbound_line_blocked,
                mark_vobiz_auth_low_balance,
            )
            if is_vobiz_insufficient_balance_error(ve):
                mark_vobiz_auth_low_balance(v_auth_id)
                await update_lead_status(lead_id, "pending", error=None)
            elif is_vobiz_from_line_blocked_error(ve):
                mark_outbound_line_blocked(v_from)
                await update_lead_status(lead_id, "pending")
            else:
                await update_lead_status(lead_id, "no response", error=f"Vobiz {ve.status}: {ve.message}")
            if orchestration_job and orchestration_job.get("job_type") in ("fresh_call", "failed_retry"):
                try:
                    _orchestrate_call_failure(role, lead_id, orchestration_job, v_from, f"vobiz_{ve.status}")
                except Exception:
                    logger.exception("Failed-call orchestration handoff failed for lead {}", lead_id)
            return {"answered": False, "call_id": call_id, "error": f"Vobiz {ve.status}"}

        call_started_at = time.time()
        max_ring_wait = float(os.getenv("OUTBOUND_MAX_RING_WAIT_SEC", "45") or 45)
        MAX_TOTAL_WAIT = 360

        while True:
            from core.camp_session import poll_camp_session_into_memory

            info = await poll_camp_session_into_memory(call_id, v_base)
            if not answered and info.get("_call_connected_at"):
                answered = True
                logger.info(f"Orchestration call connected with {lead_name} ({lead_phone})")
            if answered and info.get("_call_ended_at"):
                logger.info(f"Orchestration call ended naturally with {lead_name}")
                break

            elapsed = time.time() - call_started_at
            if not answered and elapsed >= max_ring_wait:
                logger.warning(f"No answer for {lead_name} ({lead_phone}) after {max_ring_wait:.0f}s — moving on.")
                break
            if elapsed >= MAX_TOTAL_WAIT:
                logger.warning(f"Call to {lead_name} exceeded {MAX_TOTAL_WAIT}s — forcing next.")
                break

            await asyncio.sleep(2)

        if not answered:
            logger.info(f"Lead {lead_name} did not connect — marking no answer.")
            await update_lead_status(lead_id, "no answer", error="No answer / Timeout")
            if orchestration_job and orchestration_job.get("job_type") in ("fresh_call", "failed_retry"):
                try:
                    _orchestrate_call_failure(role, lead_id, orchestration_job, v_from, "no_answer")
                except Exception:
                    logger.exception("Failed-call orchestration handoff failed for lead {}", lead_id)

        log_id = (_CAMPAIGN_DATA.get(call_id, {}) or {}).get("_log_id")
        if log_id:
            try:
                await update_lead_call_info(lead_id, log_id=log_id, call_id=call_id)
            except Exception as exc:
                logger.exception(f"Persist log_id failed for lead {lead_id}")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception(f"Orchestration call trigger failed for {lead_phone}")
        await update_lead_status(lead_id, "failed", error=str(e))
        if orchestration_job and orchestration_job.get("job_type") in ("fresh_call", "failed_retry"):
            try:
                _orchestrate_call_failure(role, lead_id, orchestration_job, v_from, "call_failed")
            except Exception:
                logger.exception("Failed-call orchestration handoff failed for lead {}", lead_id)
    finally:
        if slot_acquired:
            release_vobiz_call_slot(role)
        if phone_slot_held:
            release_phone_slot(v_from)
        _CAMPAIGN_DATA.pop(call_id, None)
        if sem_acquired:
            _GLOBAL_CALL_SEMAPHORE.release()
    return {"answered": answered, "call_id": call_id}


def _parse_log_id_date(log_id: str) -> str | None:
    """Extract YYYY-MM-DD from log_id patterns like camp-xxx-20260513T07291 or vobiz-live-20260518T161022-xxx."""
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", log_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _read_transcript_jsonl(role: str, log_id: str) -> str:
    """Locate the JSONL transcript for a log_id and return its raw text.

    Scans the per-role ``data/<role>/logs/`` tree in both current and legacy
    systems. Parses the date from the log_id for exact-day lookup, then falls
    back to recent days. Returns empty string if nothing is found.
    """
    from datetime import datetime, timedelta, timezone

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(backend_dir)
    candidate_dirs: list[str] = []

    def _add_log_dir(base: str, day: str) -> None:
        for sub in (os.path.join(base, role, "logs", day), os.path.join(base, "logs", day)):
            if sub not in candidate_dirs:
                candidate_dirs.append(sub)

    # Date-prefixed lookup: extract date from log_id like camp-xxx-20260513T07291
    date_hint = _parse_log_id_date(log_id)
    if date_hint:
        _add_log_dir(os.path.join(backend_dir, "data"), date_hint)
        _add_log_dir(os.path.join(project_root, "data"), date_hint)
        # Conversation logs (turn-by-turn JSONL from live session)
        conv_base = Path(settings.conversation_log_dir)
        if not conv_base.is_absolute():
            conv_base = Path(backend_dir) / conv_base
        candidate_dirs.append(str(conv_base / date_hint))
        for legacy_base in (
            "/root/technopolis/backend/data",
            "/root/technopolis/agent/data",
            "/root/technopolis/backend/data",
        ):
            _add_log_dir(legacy_base, date_hint)

    # Fallback: scan recent days across all known log trees (60d for older campaigns)
    today = datetime.now(timezone.utc).date()
    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = Path(backend_dir) / conv_base
    for delta in range(0, 60):
        d = (today - timedelta(days=delta)).isoformat()
        _add_log_dir(os.path.join(backend_dir, "data"), d)
        _add_log_dir(os.path.join(project_root, "data"), d)
        candidate_dirs.append(str(conv_base / d))
        for legacy_base in (
            "/root/technopolis/backend/data",
            "/root/technopolis/agent/data",
        ):
            _add_log_dir(legacy_base, d)

    found_by_stem: dict[str, str] = {}
    for d in candidate_dirs:
        for ext in ("jsonl", "txt"):
            for stem in (log_id, f"{log_id}_resolved", f"{log_id}_merged", f"{log_id}_audio"):
                p = os.path.join(d, f"{stem}.{ext}")
                if os.path.exists(p) and stem not in found_by_stem:
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            found_by_stem[stem] = f.read()
                    except OSError:
                        continue
    if not found_by_stem:
        return ""
    from services.transcript_thin import pick_richer_transcript, user_speech_stats, plausible_user_speech_stats

    live_raw = found_by_stem.get(log_id) or ""
    live_user, _, _ = plausible_user_speech_stats(live_raw)
    raw_live_user, _, _ = user_speech_stats(live_raw)
    resolved = (found_by_stem.get(f"{log_id}_resolved") or "").strip()
    merged = (found_by_stem.get(f"{log_id}_merged") or "").strip()
    audio = (found_by_stem.get(f"{log_id}_audio") or "").strip()

    # Never surface audio-only user dialogue when live STT captured no plausible customer speech.
    if live_user < max(1, settings.transcript_min_user_turns) and audio:
        audio_user, _, _ = plausible_user_speech_stats(audio)
        if audio_user >= 1:
            audio = ""
    if live_user < max(1, settings.transcript_min_user_turns):
        side_user = max(
            plausible_user_speech_stats(resolved)[0],
            plausible_user_speech_stats(merged)[0],
        )
        if side_user >= 1 and live_user < 1 and raw_live_user < 1:
            if resolved and user_speech_stats(resolved)[0] >= side_user:
                resolved = ""
            if merged and user_speech_stats(merged)[0] >= side_user:
                merged = ""

    def _pick(*texts: str) -> str:
        pool = [t for t in texts if (t or "").strip()]
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        return pick_richer_transcript(*pool)

    if live_user >= max(1, settings.transcript_min_user_turns):
        for side in (resolved, merged):
            if side and user_speech_stats(side)[0] >= live_user:
                return side
        return live_raw or _pick(resolved, merged)
    if (live_raw or "").strip():
        return live_raw
    return _pick(resolved, merged)


def _read_live_jsonl_only(role: str, log_id: str) -> str:
    """Raw turn-by-turn live session JSONL only (never audio-transcription fallback)."""
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(backend_dir)
    candidate_dirs: list[str] = []

    def _add_log_dir(base: str, day: str) -> None:
        for sub in (os.path.join(base, role, "logs", day), os.path.join(base, "logs", day)):
            if sub not in candidate_dirs:
                candidate_dirs.append(sub)

    date_hint = _parse_log_id_date(log_id)
    if date_hint:
        _add_log_dir(os.path.join(backend_dir, "data"), date_hint)
        _add_log_dir(os.path.join(project_root, "data"), date_hint)
        conv_base = Path(settings.conversation_log_dir)
        if not conv_base.is_absolute():
            conv_base = Path(backend_dir) / conv_base
        candidate_dirs.append(str(conv_base / date_hint))

    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = Path(backend_dir) / conv_base
    for delta in range(0, 60):
        d = (today - timedelta(days=delta)).isoformat()
        _add_log_dir(os.path.join(backend_dir, "data"), d)
        _add_log_dir(os.path.join(project_root, "data"), d)
        candidate_dirs.append(str(conv_base / d))

    for d in candidate_dirs:
        for ext in ("jsonl", "txt"):
            p = os.path.join(d, f"{log_id}.{ext}")
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read()
                except OSError:
                    continue
    return ""


ANALYSIS_TIMEOUT_SEC = int(os.getenv("ANALYSIS_TIMEOUT_SEC", "180"))


def _heuristic_analysis_safe(transcript: str, *, gemini_error: str = "") -> dict:
    """Always-available transcript summary — never return blank timeout placeholders."""
    try:
        from services.call_analyzer import heuristic_analysis

        a = heuristic_analysis(transcript or "", gemini_error=gemini_error)
        if isinstance(a, dict) and a.get("summary"):
            a = dict(a)
            a["analysis_pending"] = True
            return a
    except Exception as exc:
        logger.warning("Heuristic analysis failed: {}", exc)
    snippet = (transcript or "").strip().replace("\n", " ")[:160]
    return {
        "summary": (
            f"Call connected. Auto-summary from transcript: {snippet}"
            if snippet
            else "Call connected; transcript was empty."
        ),
        "rating": 1 if snippet else 0,
        "next_steps": "Review transcript and follow up.",
        "disposition": "Answered" if snippet else "No Response",
        "emotion_label": "Neutral",
        "emotion_rationale": "",
        "emotion_confidence": 0.4,
        "requested_callback_datetime_iso": None,
        "site_visit_agreed": False,
        "preferred_location": None,
        "preferred_budget": None,
        "email_address": None,
        "analysis_pending": True,
    }


async def _background_upgrade_lead_analysis(
    role: str,
    lead_id: int,
    transcript: str,
    *,
    log_id: str = "",
    camp_id: str = "",
) -> None:
    """Full Gemini analysis without wait_for — upgrades heuristic summary in place."""
    if not (transcript or "").strip() or not lead_id:
        return
    try:
        from services.call_analyzer import analyze_call_transcript, canonical_disposition
        from services.callback_time import annotate_analysis_callback_epoch
        from services.pricing_facts import sanitize_analysis_dict

        analysis = await analyze_call_transcript(transcript, role=role)
        annotate_analysis_callback_epoch(
            analysis,
            tz_name=settings.transcript_callback_tz,
            transcript_text=transcript,
        )
        analysis = sanitize_analysis_dict(analysis)
        analysis["analysis_pending"] = False
        analysis["proof_verified"] = True

        # Preserve current status; only refresh analysis blob + disposition-driven status when clear
        from core.storage import get_lead, update_lead_status

        lead = await get_lead(role, lead_id)
        cur_status = str((lead or {}).get("status") or "completed").strip().lower()
        canon = canonical_disposition(analysis.get("disposition"))
        new_status = cur_status
        if cur_status in ("completed", "answered", "dialing", ""):
            new_status = _disposition_to_status(canon) if canon else "completed"
        await update_lead_status(lead_id, new_status or "completed", analysis=analysis)
        try:
            from core.dashboard_state import invalidate_role as _dash_invalidate_role

            _dash_invalidate_role(role)
        except Exception:
            pass
        logger.info(
            "Background analysis upgrade complete lead_id={} disposition={!r}",
            lead_id,
            analysis.get("disposition"),
        )
    except Exception as exc:
        logger.warning(
            "Background analysis upgrade failed lead_id={}: {}",
            lead_id,
            exc,
        )


async def _background_upgrade_incoming_analysis(
    role: str,
    camp_id: str,
    live_log_id: str,
    transcript: str,
) -> None:
    if not (transcript or "").strip() or not camp_id:
        return
    try:
        from services.call_analyzer import analyze_call_transcript
        from core.storage import finalize_incoming_call_record, incoming_call_row_by_camp_id

        analysis = await analyze_call_transcript(transcript, role=role)
        analysis["analysis_pending"] = False
        row = await incoming_call_row_by_camp_id(camp_id)
        dur = (row or {}).get("duration_sec")
        await finalize_incoming_call_record(camp_id, live_log_id, dur, analysis)
        try:
            from core.events import get_event_bus

            updated = await incoming_call_row_by_camp_id(camp_id)
            if updated:
                await get_event_bus().publish(
                    "incoming_call_completed",
                    role=role,
                    camp_id=camp_id,
                    id=updated.get("id"),
                    summary=updated.get("summary"),
                    disposition=updated.get("disposition"),
                    log_id=updated.get("log_id"),
                )
        except Exception:
            pass
        logger.info("Background incoming analysis upgrade complete camp_id={}", camp_id)
    except Exception as exc:
        logger.warning("Background incoming analysis upgrade failed camp_id={}: {}", camp_id, exc)


def _session_is_voicemail(*, camp_id: str = "", log_id: str = "") -> bool:
    """Read live-session voicemail flag from _CAMPAIGN_DATA (keyed by camp_id)."""
    try:
        cid = (camp_id or "").strip()
        lid = (log_id or "").strip()
        if cid and cid in _CAMPAIGN_DATA and _CAMPAIGN_DATA[cid].get("is_voicemail"):
            return True
        if lid and lid in _CAMPAIGN_DATA and _CAMPAIGN_DATA[lid].get("is_voicemail"):
            return True
        for key, row in _CAMPAIGN_DATA.items():
            if not isinstance(row, dict) or not row.get("is_voicemail"):
                continue
            if cid and key == cid:
                return True
            if lid and (key == lid or str(row.get("log_id") or "") == lid):
                return True
    except Exception:
        pass
    return False


def _session_silent_no_response(*, camp_id: str = "", log_id: str = "") -> bool:
    try:
        for key in ((camp_id or "").strip(), (log_id or "").strip()):
            if key and key in _CAMPAIGN_DATA and _CAMPAIGN_DATA[key].get("silent_no_response"):
                return True
        for row in _CAMPAIGN_DATA.values():
            if isinstance(row, dict) and row.get("silent_no_response"):
                if log_id and str(row.get("_log_id") or "") == log_id:
                    return True
                if camp_id and str(row.get("camp_id") or "") == camp_id:
                    return True
    except Exception:
        pass
    return False


def _voicemail_analysis_dict(*, for_manual: bool = False) -> dict:
    base = {
        "summary": "Call went to voicemail / answering machine.",
        "rating": 0,
        "disposition": "Voice Mail",
        "emotion_label": "Unknown",
        "emotion_rationale": "Answering machine or carrier voicemail prompt detected.",
        "emotion_confidence": None,
    }
    if for_manual:
        base["next_steps"] = "Retry call later; callee did not answer live."
    else:
        base.update({
            "site_visit_agreed": False,
            "requested_callback_datetime_iso": None,
            "preferred_location": None,
            "preferred_budget": None,
            "email_address": None,
        })
    return base


def _transcript_indicates_voicemail(transcript: str) -> bool:
    from services.transcript_interest import is_voicemail_or_screening_transcript

    text = (transcript or "").strip()
    return bool(text) and is_voicemail_or_screening_transcript(text)


async def _finalize_voicemail_lead(
    role: str,
    lead_id: int,
    log_id: str,
    extra: dict,
    *,
    duration_sec: float | None = None,
) -> None:
    """Mark lead as failed voicemail and record the call attempt for history."""
    if log_id:
        try:
            await update_lead_call_info(lead_id, log_id=log_id)
        except Exception:
            pass
    analysis = _voicemail_analysis_dict()
    if duration_sec is not None:
        analysis["duration"] = duration_sec
    await update_lead_status(
        lead_id,
        status="no answer",
        analysis=analysis,
    )
    try:
        from core.storage import add_call_attempt

        attempt_num = int(extra.get("failed_call_retries") or 0) + 1
        await add_call_attempt(
            lead_id=lead_id,
            role=role,
            attempt_number=attempt_num,
            log_id=log_id,
            status="no answer",
            disposition="Voicemail",
            summary=str(analysis.get("summary", "") or "")[:3000],
            rating=0,
            duration_sec=duration_sec,
            callback_scheduled_at=None,
        )
    except Exception as exc:
        logger.warning("Failed to record voicemail call attempt for lead {}: {}", lead_id, exc)
    try:
        from core.events import get_event_bus

        await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass


async def _resolve_call_transcript(role: str, log_id: str) -> tuple[str, str]:
    """Hybrid transcript: coalesced live JSONL when sufficient, else audio. Returns (text, source)."""
    from prompts.role_prompts import extract_agent_name
    from services.transcriber import transcribe_audio
    from services.transcript_hybrid import build_call_transcript

    agent_nm = extract_agent_name(role) if role in ("sales_1",) else ""
    transcript, source = await build_call_transcript(
        log_id=log_id,
        role=role,
        read_jsonl=_read_live_jsonl_only,
        transcribe_audio=transcribe_audio,
        agent_name=agent_nm or "",
    )
    if (transcript or "").strip() and source not in ("empty",):
        _persist_resolved_transcript(role, log_id, transcript, source)
    if source != "empty":
        logger.info("Resolved transcript log_id={} source={}", log_id, source)
    return (transcript or ""), (source or "empty")


def _persist_resolved_transcript(role: str, log_id: str, transcript: str, source: str) -> None:
    """Write resolved hybrid transcript beside live JSONL for dashboard / re-analysis."""
    if not (log_id or "").strip() or not (transcript or "").strip():
        return
    from datetime import datetime, timezone
    from pathlib import Path

    from config import settings

    backend_dir = Path(__file__).resolve().parent.parent
    date_hint = _parse_log_id_date(log_id) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bases = [
        backend_dir / "data" / "conversation_logs" / date_hint,
        backend_dir / "data" / role / "logs" / date_hint,
        Path(__file__).resolve().parent.parent.parent / "data" / role / "logs" / date_hint,
    ]
    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = backend_dir / conv_base
    bases.insert(0, conv_base / date_hint)
    suffix = "_merged" if source in ("live_jsonl_merged", "audio", "audio_merged") else "_resolved"
    payload = transcript.strip() + "\n"
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            out = base / f"{log_id}{suffix}.jsonl"
            out.write_text(payload, encoding="utf-8")
            logger.debug("Persisted resolved transcript {} ({})", out, source)
            return
        except OSError as exc:
            logger.warning("Failed to persist resolved transcript under {}: {}", base, exc)


def _live_conversation_jsonl_exists(role: str, log_id: str) -> bool:
    """True when turn-by-turn live session log exists (not audio-transcription output)."""
    import json
    from pathlib import Path

    from config import settings

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(backend_dir)
    date_hint = _parse_log_id_date(log_id)
    candidates: list[Path] = []

    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = Path(backend_dir) / conv_base
    if date_hint:
        candidates.append(conv_base / date_hint / f"{log_id}.jsonl")

    for base in (
        Path(project_root) / "data" / role / "logs",
        Path(backend_dir) / "data" / role / "logs",
    ):
        if date_hint:
            candidates.append(base / date_hint / f"{log_id}.jsonl")

    for p in candidates:
        if not p.is_file() or p.stat().st_size <= 20:
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[:40]:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") in ("turn", "session") or obj.get("session_id"):
                    return True
        except Exception:
            continue
    return False


def _transcript_unreliable_for_sales_outcome(
    *, source: str, log_id: str, role: str, transcript: str = ""
) -> tuple[bool, str]:
    """Block Site Visit / Interested when transcript is unreliable."""
    from services.call_recording import recording_duration_sec
    from services.transcript_hybrid import coalesce_jsonl_turns
    from services.transcript_roles import transcript_has_severe_speaker_swap
    from services.transcript_thin import transcript_is_thin

    turns = coalesce_jsonl_turns(transcript or "")
    thin, thin_reason = transcript_is_thin(transcript or "")
    if thin and source not in ("live_jsonl",):
        return True, f"thin_transcript:{thin_reason}"
    if turns and transcript_has_severe_speaker_swap(turns):
        return True, "speaker_role_swap"

    live_stt_raw = (_read_live_jsonl_only(role, log_id) or "").strip()
    if live_stt_raw:
        from services.transcript_thin import user_speech_stats as _live_stats, plausible_user_speech_stats as _plausible_stats

        live_stt_user, _, _ = _plausible_stats(live_stt_raw)
        raw_live_user, _, _ = _live_stats(live_stt_raw)
        if live_stt_user < max(1, settings.transcript_min_user_turns):
            if raw_live_user >= 1:
                return True, f"implausible_user_stt:{raw_live_user}"
            if source in ("live_jsonl_short", "live_jsonl_raw", "audio", "live_jsonl_merged"):
                return True, f"live_stt_no_user:{live_stt_user}"
            tx_user, _, _ = _live_stats(transcript or "")
            if tx_user >= max(1, settings.transcript_min_user_turns):
                return True, "audio_hallucination_vs_live_stt"

    if source in ("live_jsonl_short", "live_jsonl_raw") and turns:
        user_n = sum(
            1 for t in turns
            if t.get("role") == "user" and str(t.get("content") or "").strip()
        )
        asst_n = sum(
            1 for t in turns
            if t.get("role") == "assistant" and str(t.get("content") or "").strip()
        )
        if user_n < max(1, settings.transcript_min_user_turns):
            return True, f"live_jsonl_no_user:{user_n}"
        if user_n >= 2 and asst_n >= 1:
            dur_early = recording_duration_sec(log_id)
            min_short = float(os.getenv("CALL_RECORDING_MIN_ANALYZE_SEC", "8"))
            if dur_early is not None and dur_early < min_short:
                return True, f"recording_too_short:{dur_early:.1f}s"
            return False, ""

    if source not in ("audio", "live_jsonl_short", "live_jsonl_raw"):
        # Audio-written jsonl without live session log — diarization often wrong even on long calls.
        if source == "empty" and turns and not _live_conversation_jsonl_exists(role, log_id):
            if transcript_has_severe_speaker_swap(turns):
                return True, "speaker_role_swap"
        elif source not in ("live_jsonl", "live_jsonl_voicemail"):
            pass
        else:
            return False, ""

    dur = recording_duration_sec(log_id)
    min_sec = float(getattr(settings, "call_recording_min_analyze_sec", 0) or os.getenv("CALL_RECORDING_MIN_ANALYZE_SEC", "8"))
    if dur is not None and dur < min_sec:
        return True, f"recording_too_short:{dur:.1f}s"
    if source == "audio" and _live_conversation_jsonl_exists(role, log_id):
        live_raw = (_read_live_jsonl_only(role, log_id) or "").strip()
        live_turns = coalesce_jsonl_turns(live_raw)
        live_user_n = sum(
            1 for t in live_turns
            if t.get("role") == "user" and str(t.get("content") or "").strip()
        )
        if live_user_n < 1:
            return True, "live_stt_no_user_audio_hallucination_risk"
        if dur is None or dur < max(min_sec, 45.0):
            return True, "audio_without_live_jsonl"
        if turns and transcript_has_severe_speaker_swap(turns):
            return True, "speaker_role_swap"
    blob = (transcript or "").lower()
    from services.pricing_facts import has_wrong_pricing

    if source == "audio" and not _live_conversation_jsonl_exists(role, log_id):
        if has_wrong_pricing(transcript or ""):
            return True, "hallucinated_pricing"
        from services.transcript_thin import transcript_is_thin, user_speech_stats

        thin_live, live_reason = transcript_is_thin(_read_live_jsonl_only(role, log_id) or "")
        if thin_live:
            user_n, _, _ = user_speech_stats(transcript or "")
            if user_n < max(1, settings.transcript_min_user_turns):
                return True, f"audio_only_no_live_user:{live_reason}"
    for marker in (
        "surya meadows", "mr. rahul", "mr. amit", "developer mode",
        "december 2025", "dec 2025", "2400 to 4000",
    ):
        if marker in blob:
            return True, f"fake_marker:{marker}"
    return False, ""


def _call_proof_verified(*, log_id: str, role: str, tx_source: str, transcript: str) -> tuple[bool, str]:
    """Recording + live transcript proof for Interested / Site Visit dashboard outcomes."""
    from services.call_recording import recording_duration_sec, resolve_dashboard_recording_path
    from services.transcript_hybrid import coalesce_jsonl_turns

    min_sec = float(
        getattr(settings, "call_recording_min_analyze_sec", 0)
        or os.getenv("CALL_RECORDING_MIN_ANALYZE_SEC", "8")
    )
    rp = resolve_dashboard_recording_path(log_id)
    if not rp or not rp.is_file():
        return False, "no_recording_file"
    dur = recording_duration_sec(log_id)
    if dur is None or dur < min_sec:
        return False, f"recording_too_short:{dur}"
    has_live = tx_source in (
        "live_jsonl", "live_jsonl_short", "live_jsonl_raw", "live_jsonl_voicemail",
        "live_jsonl_merged",
    ) or _live_conversation_jsonl_exists(role, log_id)
    if not has_live:
        return False, "no_live_jsonl"
    turns = coalesce_jsonl_turns(transcript or "")
    if turns:
        user_n = sum(1 for t in turns if t.get("role") == "user" and str(t.get("content") or "").strip())
        if user_n < max(1, settings.transcript_min_user_turns):
            return False, "no_user_turns_in_transcript"
    live_raw = _read_live_jsonl_only(role, log_id)
    if live_raw.strip() and has_live:
        from services.transcript_thin import user_speech_stats, plausible_user_speech_stats

        live_user, _, _ = plausible_user_speech_stats(live_raw)
        raw_live_user, _, _ = user_speech_stats(live_raw)
        resolved_user, _, _ = plausible_user_speech_stats(transcript or "")
        if live_user < max(1, settings.transcript_min_user_turns):
            if raw_live_user >= 1:
                return False, "implausible_user_stt"
            if tx_source in ("live_jsonl_short", "live_jsonl_raw", "live_jsonl", "live_jsonl_merged"):
                return False, "live_jsonl_no_user_turns"
            return False, "live_jsonl_no_user_turns"
        if resolved_user < max(1, settings.transcript_min_user_turns) and raw_live_user >= 1:
            return False, "implausible_user_stt"
    unreliable, reason = _transcript_unreliable_for_sales_outcome(
        source=tx_source, log_id=log_id, role=role, transcript=transcript
    )
    if unreliable:
        return False, reason
    return True, ""


def _disposition_to_status(disposition: str) -> str:
    """Map analyzer disposition → lead status the dashboard expects.

    Dispositions are normalised via ``canonical_disposition`` so punctuation,
    synonyms, and minor model rephrasings map deterministically.
    """
    from services.call_analyzer import canonical_disposition

    canon = canonical_disposition(disposition)
    if canon == "Interested":
        return "completed"
    if canon == "Not Interested":
        return "not_interested"
    if canon == "Wrong Number":
        return "failed"
    if canon in ("Voicemail", "Call Screened", "No Answer"):
        return "no response"
    if canon == "Busy":
        return "busy"
    # Call Later, Answered, anything unknown → successful connection bucket
    return "completed"


def _persist_lead_memory(lead_id: int, analysis: dict | None, canon_disp: str = "") -> None:
    """Persist compact facts + summary to the ``lead_memory`` table.

    Shared by the campaign path (``_analyze_and_update_lead``) and the manual /
    incoming finalizers so every completed conversation updates the rolling
    memory — making scheduled-callback, manual, and inbound legs continuous.
    """
    try:
        from core.orchestration_service import update_memory as _update_lead_memory
        from core.storage import _get_conn as _mem_conn

        _mem_facts: dict = {}
        _aj = analysis or {}
        for _k in (
            "preferred_budget", "budget", "preferred_location", "location",
            "property_type", "timeline", "email_address", "loan_need",
            "decision_maker", "family", "occupation", "purpose", "objections",
        ):
            _v = _aj.get(_k)
            if _v not in (None, "", [], {}):
                _mem_facts[_k] = str(_v)[:400]
        if _aj.get("site_visit_agreed") is True:
            _mem_facts["site_visit_agreed"] = "yes"
        if _aj.get("requested_callback_datetime_iso"):
            _mem_facts["callback_requested_at"] = str(_aj["requested_callback_datetime_iso"])
        if _aj.get("callback_reminder_epoch"):
            _mem_facts["callback_reminder_epoch"] = str(_aj["callback_reminder_epoch"])
        _mem_facts.setdefault("last_disposition", str(_aj.get("disposition") or canon_disp or ""))
        _mem_summary = str(_aj.get("summary") or "")[:3000]
        if _mem_facts or _mem_summary:
            _update_lead_memory(
                _mem_conn(), lead_id, facts=_mem_facts, summary=_mem_summary,
            )
    except Exception as _mem_w_e:
        logger.debug("Lead memory write skipped for lead {}: {}", lead_id, _mem_w_e)


async def _analyze_and_update_lead(
    role: str,
    lead_id: int,
    log_id: str,
    callback_id: int | None = None,
    camp_id: str = "",
):
    """Read the call's transcript, analyze it, and finalize the lead status.

    Writes terminal statuses — including ``callback_scheduled`` when the callee asks
    to be recalled at a specific future time parsed from QA (campaign promotes to
    ``pending`` when that moment passes).

    If ``callback_id`` is provided (scheduled callback), write the outcome back
    to the ``scheduled_callbacks`` row so the dashboard shows it.
    """
    # Heartbeat: stamp that this role's worker is still alive and processing calls.
    _LAST_WORKER_ACTIVITY[role] = time.time()
    if not log_id:
        logger.warning(f"Analyze: no log_id for lead {lead_id}; marking completed.")
        await update_lead_status(lead_id, "completed")
        return

    resolved_camp = (camp_id or "").strip()
    if not resolved_camp:
        try:
            from core.state import _CAMPAIGN_DATA

            for cid, meta in list(_CAMPAIGN_DATA.items()):
                if not isinstance(meta, dict):
                    continue
                if str(meta.get("_log_id") or "").strip() == log_id:
                    resolved_camp = str(cid)
                    break
                if lead_id is not None and meta.get("_lead_id") == lead_id:
                    resolved_camp = str(cid)
                    break
        except Exception:
            pass
    if not resolved_camp and lead_id is not None:
        try:
            from core.storage import get_lead

            lead_row = await get_lead(role, lead_id)
            if lead_row:
                resolved_camp = str(lead_row.get("_call_id") or "").strip()
        except Exception:
            pass

    try:
        from services.call_recording import prepare_playback_recording

        playback = await prepare_playback_recording(log_id, camp_id=resolved_camp)
        if playback:
            logger.info("Analyze: playback recording ready log_id={} path={}", log_id, playback)
    except Exception as exc:
        logger.warning("Analyze: prepare_playback_recording failed log_id={}: {}", log_id, exc)

    duration_sec = None
    try:
        from core.worker import _CAMPAIGN_DATA
        if log_id in _CAMPAIGN_DATA:
            duration_sec = _CAMPAIGN_DATA[log_id].get("call_duration_sec")
    except Exception:
        pass

    # Ensure _log_id is persisted on the lead row so transcript/recording
    # lookups resolve correctly (live_session.py sets it on connect, but this
    # provides a fallback for edge cases).
    extra = {}
    if lead_id is not None:
        try:
            from core.storage import get_lead
            lead_row = await get_lead(role, lead_id)
            if lead_row:
                raw_extra = lead_row.get("extra")
                if raw_extra:
                    extra = json.loads(raw_extra) if isinstance(raw_extra, str) else raw_extra
        except Exception as e:
            logger.warning("Failed to load lead extra info for lead_id={}: {}", lead_id, e)

    try:
        from core.state import _CAMPAIGN_DATA as _camp_meta
        for _cid, _meta in list(_camp_meta.items()):
            if not isinstance(_meta, dict):
                continue
            if _meta.get("_lead_id") == lead_id or _meta.get("_log_id") == log_id:
                if _meta.get("_prefer_whatsapp_only"):
                    extra["prefer_whatsapp_only"] = True
                if _meta.get("_scheduled_callback_id") and callback_id is None:
                    callback_id = int(_meta["_scheduled_callback_id"])
                if _meta.get("_callback_type"):
                    extra["_callback_type"] = _meta["_callback_type"]
                if _meta.get("_follow_up_number") is not None:
                    extra["_follow_up_number"] = _meta["_follow_up_number"]
                if _meta.get("_follow_up_memory"):
                    extra["_follow_up_memory"] = _meta["_follow_up_memory"]
                break
    except Exception:
        pass

    try:
        await update_lead_call_info(lead_id, log_id=log_id)
    except Exception:
        logger.warning("Analyze: failed to persist log_id for lead {}", lead_id)

    # ── Voicemail override check (live session flag + transcript) ──
    is_voicemail_flag = _session_is_voicemail(log_id=log_id)
    if not is_voicemail_flag:
        # camp_id may be embedded in log_id key for legacy rows
        is_voicemail_flag = _session_is_voicemail(camp_id=log_id)

    if _session_silent_no_response(camp_id=log_id, log_id=log_id):
        logger.info("Lead {} silent pickup (no speech) — marking No Response", lead_id)
        await update_lead_status(
            lead_id,
            status="no response",
            analysis={
                "summary": "Call connected but lead did not speak.",
                "rating": 0,
                "disposition": "No Response",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech from callee after connect.",
                "site_visit_agreed": False,
            },
        )
        from core.storage import get_lead
        _lead_for_retry = await get_lead(role, lead_id)
        if _lead_for_retry:
            await _schedule_failed_call_retry(
                role, lead_id, _lead_for_retry.get("phone"), _lead_for_retry.get("name"),
            )
            # ── Sandbox transition: move lead to Sandbox 2 (Retry Engine) ──
            try:
                await update_lead_sandbox(lead_id, 2)
                logger.info("Lead {} sandbox updated to 2 (retry) — silent pickup", lead_id)
            except Exception as _sb_e:
                logger.warning("Failed to update sandbox for lead {} to SB2 (silent): {}", lead_id, _sb_e)
        return

    if is_voicemail_flag:
        logger.info(f"Lead {lead_id} call marked as Voicemail early in session — bypass LLM.")
        await _finalize_voicemail_lead(role, lead_id, log_id, extra, duration_sec=duration_sec)
        return

    # Check live JSONL before hybrid resolver — short voicemail prompts must not
    # fall through to audio transcription (which hallucinates Interested summaries).
    raw_live_jsonl = (_read_live_jsonl_only(role, log_id) or "").strip()
    if raw_live_jsonl and _transcript_indicates_voicemail(raw_live_jsonl):
        logger.info("Lead {} live JSONL indicates voicemail — marking Voice Mail", lead_id)
        await _finalize_voicemail_lead(role, lead_id, log_id, extra, duration_sec=duration_sec)
        return

    transcript, tx_source = await _resolve_call_transcript(role, log_id)

    tx_unreliable = False
    tx_unreliable_reason = ""
    unreliable, unrel_reason = _transcript_unreliable_for_sales_outcome(
        source=tx_source, log_id=log_id, role=role, transcript=transcript
    )
    if unreliable:
        tx_unreliable = True
        tx_unreliable_reason = unrel_reason
        logger.warning(
            "Lead {} transcript flagged unreliable (source={}, reason={}) — analysis continues, high-value outcomes capped",
            lead_id,
            tx_source,
            unrel_reason,
        )

    if _transcript_indicates_voicemail(transcript):
        logger.info("Lead {} transcript indicates voicemail — marking Voice Mail", lead_id)
        await _finalize_voicemail_lead(role, lead_id, log_id, extra, duration_sec=duration_sec)
        return

    if not (transcript or "").strip():
        logger.info(f"No transcript for lead {lead_id} (log_id={log_id})")
        analysis_dict = {"summary": "Call connected; transcript unavailable.", "rating": 0, "disposition": "Answered"}
        if duration_sec is not None:
            analysis_dict["duration"] = duration_sec
        await update_lead_status(
            lead_id,
            status="completed",
            analysis=analysis_dict,
        )
        return

    # Count how many turns are from the lead vs the AI
    # Supports JSONL (live_session), single-JSON (audio transcription), and plain text formats
    import re as _re
    lead_turns = 0

    def _is_valid_turn_content(text: str) -> bool:
        t = str(text or "").lower().strip(".,?![]() ")
        if not t or len(t) <= 1:
            return False
        # Ignore purely noise/silence turns
        if t in ("silence", "noise", "background noise", "cough", "sigh", "snort", "[silence]", "[noise]", "[cough]"):
            return False
        return True

    for line in transcript.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Try JSONL format first
        try:
            obj = json.loads(line)
            role_label = (obj.get("role") or obj.get("type", "")).lower()
            turn_content = (obj.get("content") or obj.get("text") or obj.get("message", "")).strip()
            if role_label == "user" and _is_valid_turn_content(turn_content):
                from services.transcript_thin import user_turn_is_plausible

                if user_turn_is_plausible(turn_content):
                    lead_turns += 1
                continue
            # Audio transcription wraps entire conversation in one JSON with role="assistant"
            # and USER:/ASSISTANT: prefixes embedded in content. Count USER: segments.
            if turn_content and "USER:" in turn_content.upper():
                user_parts = _re.findall(r'USER:\s*(.+?)(?=ASSISTANT:|$)', turn_content, _re.IGNORECASE)
                lead_turns += sum(1 for p in user_parts if _is_valid_turn_content(p))
            continue
        except Exception:
            pass
        # Plain text format: "ASSISTANT: ..." or "USER: ..."
        upper = line.upper()
        if upper.startswith("USER:") and len(line) > 6 and len(line.strip()) > 6:
            turn_text = line[5:].strip()
            if _is_valid_turn_content(turn_text):
                lead_turns += 1

    if lead_turns < 1:
        logger.info(f"Lead {lead_id} had no verbal response — marking as No Response.")
        await update_lead_status(
            lead_id,
            status="no response",
            analysis={
                "summary": "Call connected but lead did not speak / no conversation.",
                "rating": 0,
                "disposition": "No Response",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured from the lead.",
                "emotion_confidence": None,
                "site_visit_agreed": False,
                "requested_callback_datetime_iso": None,
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            },
        )
        from core.storage import get_lead
        _lead_for_retry = await get_lead(role, lead_id)
        if _lead_for_retry:
            await _schedule_failed_call_retry(
                role, lead_id, _lead_for_retry.get("phone"), _lead_for_retry.get("name"),
            )
            # ── Sandbox transition: move lead to Sandbox 2 (Retry Engine) ──
            try:
                await update_lead_sandbox(lead_id, 2)
                logger.info("Lead {} sandbox updated to 2 (retry) — no verbal response", lead_id)
            except Exception as _sb_e:
                logger.warning("Failed to update sandbox for lead {} to SB2 (no verbal): {}", lead_id, _sb_e)
        return

    # Short transcript guardrail — skip LLM only when truly no dialogue.
    total_words = len(transcript.split())
    if lead_turns < 1 and total_words < 12:
        logger.info(
            "Lead {} very short transcript ({} words, {} user turns) — marking as No Response, skipping LLM.",
            lead_id,
            total_words,
            lead_turns,
        )
        await update_lead_status(
            lead_id,
            status="no response",
            analysis={
                "summary": "Call connected but lead did not speak / no conversation.",
                "rating": 0,
                "disposition": "No Response",
                "emotion_label": "Unknown",
                "emotion_rationale": "No speech captured from the lead.",
                "emotion_confidence": None,
                "site_visit_agreed": False,
                "requested_callback_datetime_iso": None,
                "next_action": {"action_type": "None", "datetime_iso": None, "details": "Lead did not speak during the call."},
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            },
        )
        from core.storage import get_lead
        _lead_for_retry = await get_lead(role, lead_id)
        if _lead_for_retry:
            await _schedule_failed_call_retry(
                role, lead_id, _lead_for_retry.get("phone"), _lead_for_retry.get("name"),
            )
            # ── Sandbox transition: move lead to Sandbox 2 (Retry Engine) ──
            try:
                await update_lead_sandbox(lead_id, 2)
                logger.info("Lead {} sandbox updated to 2 (retry) — short transcript", lead_id)
            except Exception as _sb_e:
                logger.warning("Failed to update sandbox for lead {} to SB2 (short transcript): {}", lead_id, _sb_e)
        return

    from services.pricing_facts import has_wrong_pricing, sanitize_pricing_in_text

    if transcript and has_wrong_pricing(transcript):
        transcript = sanitize_pricing_in_text(transcript)

    # ── Overall analysis timeout
    # If transcription + LLM analysis takes longer than 30s, we write
    # a fallback analysis so the lead never stays "stuck in dialing".
    try:
        try:
            from services.call_analyzer import analyze_call_transcript, canonical_disposition
            from services.callback_time import annotate_analysis_callback_epoch
            from services.transcript_interest import apply_interest_disposition_override
        except ModuleNotFoundError:
            from backend.services.call_analyzer import analyze_call_transcript, canonical_disposition
            from backend.services.callback_time import annotate_analysis_callback_epoch
            from backend.services.transcript_interest import apply_interest_disposition_override

        async def _run_analysis() -> dict:
            a = await analyze_call_transcript(transcript, role=role)
            annotate_analysis_callback_epoch(a, tz_name=settings.transcript_callback_tz, transcript_text=transcript)
            _summary_first = (getattr(settings, "outcome_mode", "summary_first") or "").strip().lower() == "summary_first"
            if not _summary_first:
                a = apply_interest_disposition_override(a, transcript)
            from services.pricing_facts import sanitize_analysis_dict

            return sanitize_analysis_dict(a)

        analysis = await asyncio.wait_for(_run_analysis(), timeout=ANALYSIS_TIMEOUT_SEC)
        analysis["analysis_pending"] = False
    except asyncio.TimeoutError:
        logger.warning(f"Analysis timed out ({ANALYSIS_TIMEOUT_SEC}s) for lead {lead_id} — heuristic + background retry")
        analysis = _heuristic_analysis_safe(transcript, gemini_error="timeout")
        asyncio.create_task(
            _background_upgrade_lead_analysis(
                role, lead_id, transcript, log_id=log_id, camp_id=resolved_camp
            )
        )
    except Exception as e:
        logger.exception(f"Analyzer call failed for lead {lead_id}")
        analysis = _heuristic_analysis_safe(transcript, gemini_error=str(e)[:120])
        asyncio.create_task(
            _background_upgrade_lead_analysis(
                role, lead_id, transcript, log_id=log_id, camp_id=resolved_camp
            )
        )

    try:
        rem_f = float(analysis.get("callback_reminder_epoch"))
    except (TypeError, ValueError):
        rem_f = None

    # ── Callback epoch — only when callee explicitly asked to be called back ──
    check_tx_cb = raw_live_jsonl or transcript or ""
    from services.transcript_interest import analysis_indicates_user_callback

    user_wants_callback = analysis_indicates_user_callback(analysis, check_tx_cb)
    if rem_f is None and user_wants_callback:
        from services.callback_time import zoneinfo_safe
        from datetime import datetime

        rem_f = time.time() + 86400
        analysis["callback_reminder_epoch"] = rem_f
        tz = zoneinfo_safe(settings.transcript_callback_tz)
        analysis["requested_callback_datetime_iso"] = datetime.fromtimestamp(rem_f, tz).isoformat()
        logger.info(
            "User-requested callback for lead {} — scheduled {}",
            lead_id,
            analysis["requested_callback_datetime_iso"],
        )
    elif rem_f is not None and not user_wants_callback:
        # LLM invented a callback time without user asking — discard
        logger.info(
            "Lead {} had callback epoch but no user callback in transcript — ignoring rem_f={}",
            lead_id,
            rem_f,
        )
        rem_f = None
        analysis.pop("callback_reminder_epoch", None)
        analysis.pop("requested_callback_datetime_iso", None)

    # Live JSONL beats audio transcription — never mark voicemail as Interested.
    _summary_first = (getattr(settings, "outcome_mode", "summary_first") or "").strip().lower() == "summary_first"
    if raw_live_jsonl and _transcript_indicates_voicemail(raw_live_jsonl):
        logger.info("Lead {} live JSONL is voicemail — overriding LLM analysis", lead_id)
        analysis = _voicemail_analysis_dict()
        if duration_sec is not None:
            analysis["duration"] = duration_sec
        rem_f = None
    elif _summary_first:
        logger.debug("Lead {} summary_first outcome mode — trusting Gemini disposition", lead_id)
        analysis["proof_verified"] = True
    else:
        from services.transcript_interest import (
            infer_interest_from_transcript,
            infer_site_visit_from_transcript,
        )

        check_tx = raw_live_jsonl or transcript
        _tx_trusted = (not tx_unreliable) and (
            tx_source in ("live_jsonl", "live_jsonl_short", "live_jsonl_raw", "live_jsonl_merged")
            or not _transcript_unreliable_for_sales_outcome(
                source=tx_source, log_id=log_id, role=role, transcript=transcript
            )[0]
        )
        if check_tx and infer_site_visit_from_transcript(check_tx):
            if _tx_trusted:
                analysis["site_visit_agreed"] = True
                analysis["disposition"] = "Site Visit"
                analysis.pop("outcome_from_transcript", None)
        elif check_tx and infer_interest_from_transcript(check_tx):
            if _tx_trusted:
                analysis["disposition"] = "Interested"
                analysis["outcome_from_transcript"] = True
            else:
                analysis["disposition"] = "Answered"
                analysis.pop("outcome_from_transcript", None)
        else:
            from services.call_analyzer import canonical_disposition as _canon_pre

            if _canon_pre(analysis.get("disposition")) == "Interested":
                analysis["disposition"] = "Answered"
                analysis.pop("outcome_from_transcript", None)

    canon_disp = canonical_disposition(analysis.get("disposition"))

    if tx_unreliable:
        analysis["transcript_unreliable"] = tx_unreliable_reason
        analysis["transcript_source"] = tx_source

    if not _summary_first:
        try:
            emo_conf = float(analysis.get("emotion_confidence") or 0)
        except (TypeError, ValueError):
            emo_conf = None
        if canon_disp == "Interested" and emo_conf is not None and emo_conf < 0.40:
            logger.info(
                "Lead {} Interested capped — low emotion confidence {:.2f}",
                lead_id,
                emo_conf,
            )
            analysis["disposition"] = "Answered"
            analysis.pop("outcome_from_transcript", None)
            canon_disp = "Answered"

        # ── Thin transcript guard: brief ack-only calls stay Answered ───
        check_tx_stats = raw_live_jsonl or transcript or ""
        if check_tx_stats.strip():
            from services.transcript_interest import (
                infer_interest_from_transcript,
                thin_transcript_blocks_interest,
            )
            from services.transcript_thin import plausible_user_speech_stats, transcript_is_thin, user_speech_stats

            user_n, user_chars, total_n = user_speech_stats(check_tx_stats)
            p_user_n, p_user_chars, _ = plausible_user_speech_stats(check_tx_stats)
            analysis["user_speech_stats"] = {
                "user_turns": user_n,
                "user_chars": user_chars,
                "plausible_user_turns": p_user_n,
                "plausible_user_chars": p_user_chars,
                "total_turns": total_n,
            }
            thin, thin_reason = transcript_is_thin(check_tx_stats)
            if thin:
                analysis["transcript_thin"] = True
                analysis["transcript_thin_reason"] = thin_reason
            if thin_transcript_blocks_interest(check_tx_stats) and not infer_interest_from_transcript(check_tx_stats):
                logger.info(
                    "Lead {} thin transcript ({}) — capping Answered, clearing LLM sales follow-ups",
                    lead_id,
                    thin_reason,
                )
                analysis["disposition"] = "Answered"
                analysis["site_visit_agreed"] = False
                analysis.pop("outcome_from_transcript", None)
                canon_disp = "Answered"
                analysis["summary"] = (
                    "The customer answered with a brief acknowledgment only; "
                    "no project interest was expressed."
                )
                analysis["next_steps"] = ""
                analysis["next_action"] = {
                    "action_type": "None",
                    "datetime_iso": None,
                    "details": "",
                }
                analysis.pop("suggested_action", None)
                analysis.pop("callback_reminder_epoch", None)
                analysis.pop("requested_callback_datetime_iso", None)
                rem_f = None
                user_wants_callback = False

        # ── Proof bundle gate (recording + live JSONL) ─────────────────
        _high_value = canon_disp in ("Interested", "Site Visit") or analysis.get("site_visit_agreed")
        proof_ok, proof_reason = _call_proof_verified(
            log_id=log_id, role=role, tx_source=tx_source, transcript=transcript or raw_live_jsonl
        )
        if _high_value and not proof_ok:
            logger.warning(
                "Lead {} high-value outcome blocked by proof gate: {}",
                lead_id,
                proof_reason,
            )
            analysis["site_visit_agreed"] = False
            if canon_disp in ("Interested", "Site Visit"):
                analysis["disposition"] = "Answered"
                canon_disp = "Answered"
            analysis["proof_verified"] = False
            analysis["proof_block_reason"] = proof_reason
            if any(
                k in (proof_reason or "")
                for k in ("implausible", "thin", "no_user", "unreliable", "hallucin")
            ):
                analysis["summary"] = (
                    "Call connected but customer speech could not be verified in the live transcript."
                )
                analysis["next_action"] = {
                    "action_type": "None",
                    "datetime_iso": None,
                    "details": "",
                }
                analysis.pop("next_steps", None)
        else:
            analysis["proof_verified"] = bool(proof_ok)
    elif not analysis.get("proof_verified"):
        analysis["proof_verified"] = True
    
    # Busy calls are unified with failed retries 24-hours scheduler below
    now_t = time.time()
    is_cb = bool(user_wants_callback and rem_f is not None and rem_f > now_t)

    if is_cb:
        new_status = "callback_scheduled"
    elif rem_f is not None and rem_f <= now_t:
        new_status = "pending"
    elif role in ("sales_1",):
        if canon_disp in ("Voicemail", "Call Screened"):
            new_status = "no answer"
        elif canon_disp in ("No Answer", "Wrong Number"):
            new_status = "failed" if canon_disp == "Wrong Number" else "no answer"
        elif canon_disp == "No Response":
            new_status = "no response"
        elif canon_disp == "Busy":
            new_status = "busy"
        else:
            new_status = "completed"
    else:
        new_status = _disposition_to_status(analysis.get("disposition", ""))
        if canon_disp == "Busy" and int(extra.get("failed_call_retries") or 0) >= max(0, settings.failed_call_max_attempts - 1):
            new_status = "no response"

    # ── Call Quality Auto-Retry (Learning Loop) ───────────────────
    is_poor_connection = False
    
    # 1. Check transcript text for keywords or repetition patterns
    trans_lower = (transcript or "").lower()
    static_words = ["static", "inaudible", "breaking", "cannot hear", "not clear", "bad connection", "connection issue", "disconnected"]
    has_static_keyword = any(w in trans_lower for w in static_words)
    
    # Check for repeated "can you hear me" / "hello" loops
    hello_count = trans_lower.count("hello") + trans_lower.count("can you hear me") + trans_lower.count("i can hear you")
    has_hello_loop = (hello_count >= 5)
    
    # 2. Check analysis summary and rating
    summary_lower = str(analysis.get("summary") or "").lower()
    has_poor_summary = any(w in summary_lower for w in ("static", "inaudible", "cannot hear", "disconnected", "connection issue", "unclear"))
    
    rating_val = None
    try:
        rating_val = int(analysis.get("rating") or 0)
    except Exception:
        pass
        
    if (has_static_keyword or has_hello_loop or has_poor_summary) and (rating_val is None or rating_val <= 2):
        is_poor_connection = True
        
    if is_poor_connection:
        from services.callback_time import zoneinfo_safe
        from datetime import datetime
        tz = zoneinfo_safe(settings.transcript_callback_tz)

        retry_at = time.time() + 300
        analysis["system_redial_epoch"] = retry_at
        analysis["system_redial"] = True
        analysis["disposition"] = "No Response"
        analysis["summary"] = "Call had poor connection / unclear audio. System redial scheduled in 5 minutes."
        new_status = "no response"
        is_cb = False
        rem_f = None
        analysis.pop("callback_reminder_epoch", None)
        analysis.pop("requested_callback_datetime_iso", None)
        logger.info(
            "Call quality issue for lead {} — system redial in 5m (not user callback)",
            lead_id,
        )
        try:
            from core.storage import add_scheduled_callback, get_lead as _get_lead_q

            _lr = await _get_lead_q(role, lead_id)
            if _lr:
                await add_scheduled_callback(
                    role,
                    phone=_lr.get("phone", ""),
                    name=f"{_lr.get('name', '')} (Quality retry)",
                    scheduled_at=retry_at,
                    lead_id=lead_id,
                    outbound_phone=(_lr.get("outbound_phone") or "").strip(),
                    callback_type="quality_retry",
                )
        except Exception:
            logger.exception("Failed to schedule quality retry for lead {}", lead_id)

    # ── Site Visit override (buyer campaigns only) ──
    _sv_action_type = (analysis.get("next_action") or {}).get("action_type", "")
    if (
        analysis.get("site_visit_agreed") or _sv_action_type.strip().lower() in ("site visit", "site_visit")
    ):
        if _transcript_unreliable_for_sales_outcome(
            source=tx_source, log_id=log_id, role=role, transcript=transcript
        )[0]:
            analysis["site_visit_agreed"] = False
            analysis["disposition"] = "Answered"
            if new_status == "site_visit":
                new_status = "no response"
            logger.warning(
                "Lead {} site_visit blocked — unreliable transcript source={}",
                lead_id,
                tx_source,
            )
        else:
            new_status = "site_visit"
            analysis["disposition"] = "Site Visit"
            logger.info(
                "Lead {} agreed to site visit (site_visit_agreed={}, action_type={}) — status promoted to site_visit",
                lead_id, analysis.get("site_visit_agreed"), _sv_action_type,
            )

            # ── Sandbox transition: move lead to Sandbox 3 (Nurture) ──
            # Site visit means the lead showed interest → route to SB3 for nurturing
            try:
                await update_lead_sandbox(lead_id, 3)
                logger.info("Lead {} sandbox updated to 3 (nurture) — site visit agreed", lead_id)
            except Exception as _sb_e:
                logger.warning("Failed to update sandbox for lead {} to SB3: {}", lead_id, _sb_e)

            # ── Site Visit lifecycle + Follow-up 1/2 scheduling ─────────────
            try:
                from core.storage import get_lead as _get_lead_for_sv_sch
                from core.site_visit_lifecycle import apply_site_visit_lifecycle

                _lead_row_sv = await _get_lead_for_sv_sch(role, lead_id)
                if _lead_row_sv:
                    phone = _lead_row_sv.get("phone", "")
                    name = _lead_row_sv.get("name", "") or "Valued Customer"
                    sv_outbound = (_lead_row_sv.get("outbound_phone") or "").strip()
                    _ca_attempt_num = int(extra.get("failed_call_retries") or 0) + 1
                    agent_nm = "Vernika" if role == "sales_1" else "Vernika"
                    extra, analysis, _ = await apply_site_visit_lifecycle(
                        role=role,
                        lead_id=lead_id,
                        phone=phone,
                        name=name,
                        outbound_phone=sv_outbound,
                        analysis=analysis,
                        extra=extra,
                        attempt_number=_ca_attempt_num,
                        log_id=log_id or "",
                        transcript=transcript or raw_live_jsonl,
                        agent_name=agent_nm,
                    )
            except Exception:
                logger.exception("Failed to auto-schedule site visit follow-up callbacks for lead {}", lead_id)

    # ── Interested nurture follow-up (buyer campaigns only) ─────────────
    elif (
        canon_disp == "Interested"
        and not analysis.get("site_visit_agreed")
        and proof_ok
    ):
        try:
            from core.storage import get_lead as _get_lead_int
            from core.site_visit_lifecycle import apply_interested_followup_lifecycle

            _lead_int = await _get_lead_int(role, lead_id)
            if _lead_int:
                _ca_attempt_num = int(extra.get("failed_call_retries") or 0) + 1
                agent_nm = "Vernika" if role == "sales_1" else "Vernika"
                extra, analysis, _ = await apply_interested_followup_lifecycle(
                    role=role,
                    lead_id=lead_id,
                    phone=_lead_int.get("phone", ""),
                    name=_lead_int.get("name", "") or "Valued Customer",
                    outbound_phone=(_lead_int.get("outbound_phone") or "").strip(),
                    analysis=analysis,
                    extra=extra,
                    attempt_number=_ca_attempt_num,
                    log_id=log_id or "",
                    transcript=transcript or raw_live_jsonl,
                    agent_name=agent_nm,
                )
        except Exception:
            logger.exception("Failed to schedule interested follow-up for lead {}", lead_id)

    # ── Interested lead sandbox transition (not site visit) ──
    # If the lead is interested but not a site visit, also move to Sandbox 3
    if (
        canon_disp == "Interested"
        and not analysis.get("site_visit_agreed")
        and proof_ok
    ):
        try:
            await update_lead_sandbox(lead_id, 3)
            logger.info("Lead {} sandbox updated to 3 (nurture) — interested disposition", lead_id)
        except Exception as _sb_e:
            logger.warning("Failed to update sandbox for lead {} to SB3 (interested): {}", lead_id, _sb_e)

    # ── Follow-up call completion (site visit eve/day — buyer campaigns only) ──
    _cb_type_fu = str(extra.pop("_callback_type", "") or "").strip()
    _fu_num = extra.pop("_follow_up_number", None)
    _fu_mem = extra.pop("_follow_up_memory", None)
    if _cb_type_fu in (
        "site_visit_eve",
        "site_visit_day",
        "interested_followup",
    ):
        from core.site_visit_lifecycle import (
            extract_site_visit_fields_from_analysis,
            update_follow_up_plan_on_complete,
        )

        extra = extract_site_visit_fields_from_analysis(analysis, extra)
        extra = update_follow_up_plan_on_complete(
            extra,
            scheduled_callback_id=callback_id,
            follow_up_number=int(_fu_num) if _fu_num is not None else None,
            callback_type=_cb_type_fu,
            log_id=log_id or "",
        )
        # If user agrees site visit on interested follow-up, schedule eve/day
        if _cb_type_fu == "interested_followup" and analysis.get("site_visit_agreed"):
            try:
                from core.storage import get_lead as _gl_sv2
                from core.site_visit_lifecycle import apply_site_visit_lifecycle

                _lr = await _gl_sv2(role, lead_id)
                if _lr:
                    _ca_n = int(extra.get("failed_call_retries") or 0) + 1
                    extra, analysis, _ = await apply_site_visit_lifecycle(
                        role=role,
                        lead_id=lead_id,
                        phone=_lr.get("phone", ""),
                        name=_lr.get("name", "") or "Valued Customer",
                        outbound_phone=(_lr.get("outbound_phone") or "").strip(),
                        analysis=analysis,
                        extra=extra,
                        attempt_number=_ca_n,
                        log_id=log_id or "",
                        transcript=transcript or raw_live_jsonl,
                        agent_name="Vernika" if role == "sales_1" else "Vernika",
                    )
                    new_status = "site_visit"
            except Exception:
                logger.exception("Failed site visit lifecycle after interested follow-up lead {}", lead_id)

    # ── Retake Retry Increment ───────────────────────────────────
    # If this is a campaign lead and the call connected (e.g. status is NOT terminal failed/busy/no_answer now),
    # but the previous status of the lead was in ('failed', 'busy', 'no answer', 'error'),
    # then this was a successful manual or automated "Retake" call.
    # We increment the failed_call_retries counter in extra so the dashboard Retake badge shows.
    if lead_id is not None and new_status not in ("failed", "busy", "no answer"):
        try:
            from core.storage import get_lead
            _prev_lead = await get_lead(role, lead_id)
            if _prev_lead:
                _prev_status = _prev_lead.get("status")
                if _prev_status in ("failed", "busy", "no answer", "error"):
                    _retries = int(extra.get("failed_call_retries") or 0)
                    extra["failed_call_retries"] = min(2, _retries + 1)
                    logger.info("Incrementing manual/callback retake count for lead {}: {} -> {}", lead_id, _retries, extra["failed_call_retries"])
        except Exception as e:
            logger.warning("Failed to calculate retake count for lead {}: {}", lead_id, e)

    from core.storage import update_lead_retry_state
    if extra.get("prefer_whatsapp_only"):
        analysis["prefer_whatsapp_only"] = True
    if duration_sec is None:
        try:
            from services.call_recording import recording_duration_sec

            duration_sec = recording_duration_sec(log_id)
        except Exception:
            duration_sec = None
    if duration_sec is not None:
        analysis["duration"] = duration_sec
    await update_lead_retry_state(lead_id, status=new_status, extra=extra, analysis=analysis)

    # ── Lead memory write (plan Phase 5 — memory-aware dials) ──────────
    # Persist the compact facts + summary so the next call (retry, callback,
    # nurture, feedback) can continue the conversation instead of starting cold.
    _persist_lead_memory(lead_id, analysis, canon_disp=canon_disp)

    # Record this call attempt so the dashboard can show historical retakes.
    try:
        from core.storage import add_call_attempt, get_lead as _ca_get_lead
        if log_id:
            try:
                await update_lead_call_info(lead_id, log_id=log_id)
            except Exception:
                pass
        _ca_lead = await _ca_get_lead(role, lead_id)
        _ca_attempt_num = int(extra.get("failed_call_retries") or 0) + 1
        _call_cat = "initial"
        _fu_n = None
        if _cb_type_fu in ("site_visit_eve", "site_visit_day", "interested_followup"):
            _call_cat = "follow_up"
            _fu_n = int(_fu_num) if _fu_num is not None else None
        elif _ca_attempt_num > 1:
            _call_cat = "retake"
        await add_call_attempt(
            lead_id=lead_id,
            role=role,
            attempt_number=_ca_attempt_num,
            log_id=log_id,
            status=new_status or "completed",
            disposition=canon_disp or analysis.get("disposition", ""),
            summary=str(analysis.get("summary", "") or "")[:3000],
            rating=analysis.get("rating"),
            duration_sec=duration_sec,
            callback_scheduled_at=rem_f,
            call_category=_call_cat,
            follow_up_number=_fu_n,
        )
        try:
            from core.storage import sync_lead_best_attempt
            await sync_lead_best_attempt(lead_id)
        except Exception:
            pass
    except Exception as _ca_e:
        logger.warning("Failed to record call attempt for lead {}: {}", lead_id, _ca_e)

    try:
        from core.events import get_event_bus
        await get_event_bus().publish("lead_updated", role=role, lead_id=lead_id)
    except Exception:
        pass

    try:
        from core import kv_cache
        kv_cache.invalidate_role(role)
    except Exception:
        pass

    try:
        from core.dashboard_state import invalidate_role as _dash_invalidate_role
        _dash_invalidate_role(role)
    except Exception:
        pass

    logger.info(
        f"Analysis updated for lead {lead_id}: status={new_status} disposition={analysis.get('disposition')!r} "
        f"rating={analysis.get('rating')} callback_epoch={analysis.get('callback_reminder_epoch')!r}"
    )

    # ── Reschedule Voicemail / Analyzed Failures ─────────────────
    if new_status in ("failed", "no answer", "busy"):
        try:
            from core.storage import get_lead
            lead_row = await get_lead(role, lead_id)
            if lead_row:
                await _schedule_failed_call_retry(role, lead_id, lead_row.get("phone"), lead_row.get("name"))
                logger.info("Automatically scheduled retry for voicemail / analyzed failure for lead {}", lead_id)

                # ── Sandbox transition: move lead to Sandbox 2 (Retry Engine) ──
                # Failed call → retry engine in SB2 (P4/P5/P6)
                try:
                    await update_lead_sandbox(lead_id, 2)
                    logger.info("Lead {} sandbox updated to 2 (retry engine) — call failed", lead_id)
                except Exception as _sb_e:
                    logger.warning("Failed to update sandbox for lead {} to SB2: {}", lead_id, _sb_e)
        except Exception as retry_ex:
            logger.exception("Failed to schedule retry for voicemail/failed lead {}: {}", lead_id, retry_ex)

    if new_status == "callback_scheduled" and is_cb and rem_f is not None:
        try:
            from core.storage import add_scheduled_callback, get_lead as _get_lead_for_cb
            _lead_row = await _get_lead_for_cb(role, lead_id)
            if _lead_row:
                await add_scheduled_callback(
                    role,
                    phone=_lead_row.get("phone", ""),
                    name=_lead_row.get("name", ""),
                    scheduled_at=rem_f,
                    lead_id=lead_id,
                    outbound_phone=(_lead_row.get("outbound_phone") or "").strip(),
                    callback_type="user_requested",
                )
                logger.info(
                    f"Scheduled callback entry created for lead {lead_id} "
                    f"(phone={_lead_row.get('phone')}, scheduled_at={rem_f})"
                )
        except Exception as e:
            logger.exception(f"Failed to create scheduled_callback entry for lead {lead_id}")

    # ── WhatsApp + Email bulk auto-send ───────────────────────────
    try:
        from services.whatsapp_outcome import send_outcome_whatsapp_if_eligible

        from core.storage import get_lead as _get_lead_for_wa

        _lead_row_wa = await _get_lead_for_wa(role, lead_id)
        if _lead_row_wa:
            phone_wa = _lead_row_wa.get("phone", "")
            wa_name = _lead_row_wa.get("name", "")
            wa_email = (_lead_row_wa.get("email") or analysis.get("email_address") or "").strip()
            _camp_id_for_wa = log_id
            try:
                from core.state import _CAMPAIGN_DATA as _cd_wa

                for _cid, _meta in list(_cd_wa.items()):
                    if isinstance(_meta, dict) and (
                        _meta.get("_lead_id") == lead_id or _meta.get("_log_id") == log_id
                    ):
                        _camp_id_for_wa = str(_cid)
                        break
            except Exception:
                pass
            await send_outcome_whatsapp_if_eligible(
                role=role,
                phone=phone_wa,
                lead_name=wa_name,
                disposition=str(analysis.get("disposition") or canon_disp or ""),
                status=new_status or "",
                analysis=analysis,
                lead_id=lead_id,
                camp_id=_camp_id_for_wa,
                email_on_file=wa_email,
            )
            # If agent promised WhatsApp mid-call but outcome analysis did not qualify, send anyway.
            try:
                from core.state import _CAMPAIGN_DATA as _cd_pend
                from services.whatsapp_outcome import send_agent_promised_whatsapp

                _pend_meta = _cd_pend.get(_camp_id_for_wa) if _camp_id_for_wa else None
                if isinstance(_pend_meta, dict) and _pend_meta.get("_whatsapp_pending"):
                    await send_agent_promised_whatsapp(
                        role=role,
                        camp_id=_camp_id_for_wa,
                        lead_id=lead_id,
                        lead_name=wa_name,
                        summary=str(_pend_meta.get("_whatsapp_pending_summary") or analysis.get("summary") or ""),
                    )
            except Exception as _pend_wa:
                logger.warning("Agent-promised WhatsApp fallback failed lead {}: {}", lead_id, _pend_wa)
    except Exception as e:
        logger.exception(f"WhatsApp auto-send failed for lead {lead_id}: {e}")

    # ── Email auto-send ──
    try:
        from core.storage import get_lead_email_sent, mark_email_sent, get_lead as _get_lead_for_email
        from services.email_leads import send_bulk_project_email
        _lead_row_email = await _get_lead_for_email(role, lead_id)
        email_to = ""
        if _lead_row_email:
            email_to = (_lead_row_email.get("email") or "").strip()
        if not email_to or "@" not in email_to:
            email_to = (analysis.get("email_address") or "").strip()

        _wa_only = bool(
            extra.get("prefer_whatsapp_only")
            or analysis.get("prefer_whatsapp_only")
        )
        if _wa_only:
            logger.info("Lead {} prefers WhatsApp only — skipping post-call email", lead_id)
        elif email_to and "@" in email_to:
            db_email = (_lead_row_email.get("email") or "").strip() if _lead_row_email else ""
            if db_email != email_to:
                from core.storage import update_lead_info
                await update_lead_info(lead_id, email=email_to)
                logger.info("Updated lead {} email column to {}", lead_id, email_to)

            if await get_lead_email_sent(lead_id):
                logger.info("Email already sent for lead {} — skipping duplicate", lead_id)
            else:
                em_summary = analysis.get("summary", "")
                em_name = _lead_row_email.get("name", "") if _lead_row_email else ""
                em_outbound = (_lead_row_email.get("outbound_phone") or "").strip() if _lead_row_email else ""
                email_result = await send_bulk_project_email(
                    email_to, summary=em_summary, lead_name=em_name, outbound_phone=em_outbound,
                )
                logger.info("Bulk email sent for lead {} ({}): {}", lead_id, email_to, email_result)
                if email_result.get("sent"):
                    await mark_email_sent(lead_id)
                    try:
                        from core.events import get_event_bus
                        await get_event_bus().publish("email_sent", role=role, lead_id=lead_id)
                    except Exception:
                        pass
        else:
            logger.info("Lead {} has no email address — skipping email send", lead_id)
    except Exception as e:
        logger.exception(f"Email auto-send failed for lead {lead_id}: {e}")

    # ── Callback outcome write-back ──────────────────────────────
    # If this lead was created by a scheduled callback, write the
    # analysis outcome back to the scheduled_callbacks row so the
    # dashboard displays the result (interested, not interested, etc.).
    if callback_id is not None:
        try:
            from core.storage import update_scheduled_callback_analysis
            await update_scheduled_callback_analysis(
                callback_id,
                disposition=analysis.get("disposition", ""),
                summary=analysis.get("summary", ""),
                rating=analysis.get("rating"),
                next_action=analysis.get("next_action"),
                analysis_json=analysis,
            )
            logger.info(
                "Callback {} outcome saved: disposition={!r} rating={}",
                callback_id, analysis.get("disposition"), analysis.get("rating"),
            )
        except Exception as e:
            logger.exception(f"Failed to save callback {callback_id} outcome: {e}")

    # ── Virtual Meet tracking ────────────────────────────────────
    # When analysis detects Virtual Meet was discussed, persist to DB.
    try:
        _vm_next = analysis.get("next_action") or {}
        if (_vm_next.get("action_type") or "").strip().lower() in ("virtual meet", "virtual"):
            from core.storage import add_virtual_meet as _add_vm
            _vm_date = _vm_next.get("datetime_iso", "") or analysis.get("requested_callback_datetime_iso", "")
            _vm_details = _vm_next.get("details", "") or analysis.get("summary", "")
            _vm_notes = f"{_vm_date} | {_vm_details}" if _vm_date else _vm_details
            await _add_vm(lead_id, role, _vm_date or "TBD", "TBD", notes=_vm_notes)
            logger.info("Virtual meet recorded for lead {}: {}", lead_id, _vm_notes)
    except Exception as e:
        logger.exception(f"Virtual meet tracking failed for lead {lead_id}: {e}")

    # ── Site Visit tracking ──────────────────────────────────────
    # When analysis detects the customer agreed to a physical site visit,
    # log it for dashboard display and follow-up scheduling.
    try:
        _sv_next = analysis.get("next_action") or {}
        if analysis.get("site_visit_agreed") or (_sv_next.get("action_type") or "").strip().lower() in ("site visit", "site_visit"):
            _sv_date = _sv_next.get("datetime_iso", "") or analysis.get("requested_callback_datetime_iso", "")
            _sv_details = _sv_next.get("details", "") or analysis.get("summary", "")
            logger.info(
                "Site visit agreed for lead {}: date={} details={}",
                lead_id, _sv_date or "not specified", _sv_details,
            )
    except Exception as e:
        logger.exception(f"Site visit tracking failed for lead {lead_id}: {e}")


async def sweep_stale_call_capacity(max_age_sec: float = 180.0) -> int:
    """Release orphaned manual/callback slots blocking new outbound dials."""
    from core.state import _CAMPAIGN_DATA, _ACTIVE_VOBIZ_CALLS_BY_ROLE

    released = 0
    now = time.time()
    total_active = int(sum(_ACTIVE_VOBIZ_CALLS_BY_ROLE.values()) or 0)

    for camp_id, meta in list(_CAMPAIGN_DATA.items()):
        if not isinstance(meta, dict):
            continue
        cid = str(camp_id)
        if not (cid.startswith("manual_") or cid.startswith("sched_cb_")):
            continue
        if meta.get("_call_ended_at") or meta.get("_slots_released"):
            continue
        started = float(meta.get("_registered_at") or meta.get("_call_started_at") or 0)
        if started and (now - started) < max_age_sec:
            continue
        role = str(meta.get("_role") or "sales_1")
        await release_manual_call_resources(camp_id, role)
        _CAMPAIGN_DATA.pop(camp_id, None)
        released += 1
        logger.warning("Swept stale outbound leg camp_id={} role={}", camp_id, role)

    if total_active == 0:
        for camp_id, meta in list(_CAMPAIGN_DATA.items()):
            if not isinstance(meta, dict) or not meta.get("_global_sem_acquired"):
                continue
            role = str(meta.get("_role") or "sales_1")
            await release_manual_call_resources(camp_id, role)
            released += 1
            logger.warning(
                "Released orphaned semaphore holder camp_id={} (no active Vobiz calls)",
                camp_id,
            )

    return released


async def release_manual_call_resources(camp_id: str, role: str | None = None) -> None:
    """Idempotently release phone/vobiz/semaphore slots held by a manual call leg."""
    from core.state import _CAMPAIGN_DATA, release_vobiz_call_slot, release_phone_slot

    meta = _CAMPAIGN_DATA.get(camp_id) or {}
    if meta.get("_slots_released"):
        return
    r = (role or meta.get("_role") or "sales_1").strip().lower()
    outbound_phone = meta.get("_outbound_phone")
    release_vobiz_call_slot(r)
    if outbound_phone:
        release_phone_slot(outbound_phone)
        logger.info("Released slots for manual call {}: phone={}", camp_id, outbound_phone)
    if meta.get("_global_sem_acquired"):
        try:
            _GLOBAL_CALL_SEMAPHORE.release()
        except ValueError:
            pass
        meta.pop("_global_sem_acquired", None)
    meta["_slots_released"] = True


async def _manual_call_slot_watchdog(camp_id: str, role: str, wait_sec: float = 180.0) -> None:
    """Release stuck manual-call slots if the leg never connected or finalized."""
    await asyncio.sleep(max(30.0, float(wait_sec)))
    from core.state import _CAMPAIGN_DATA

    meta = _CAMPAIGN_DATA.get(camp_id) or {}
    if meta.get("_call_ended_at") or meta.get("_slots_released"):
        return
    logger.warning("Manual call watchdog: releasing stale slots for camp_id={}", camp_id)
    await release_manual_call_resources(camp_id, role)
    try:
        from core.storage import mark_manual_call_failed

        await mark_manual_call_failed(camp_id, "Call timed out without completing")
    except Exception:
        pass
    _CAMPAIGN_DATA.pop(camp_id, None)


async def _finalize_manual_call_leg(
    role: str, camp_id: str, live_log_id: str, duration_sec: float | None = None
) -> None:
    """Post-call analyzer + SQLite row for console **Make a Call** legs (no lead row)."""
    from core.storage import finalize_manual_call_record, manual_call_row_by_camp_id

    if not await manual_call_row_by_camp_id(camp_id):
        logger.warning("Manual call finalize: no manual_calls row for camp_id={}", camp_id)
        return

    try:
        from services.call_recording import prepare_playback_recording

        playback = await prepare_playback_recording(live_log_id, camp_id=camp_id)
        if playback:
            logger.info(
                "Manual call: playback recording ready log_id={} path={}",
                live_log_id,
                playback,
            )
    except Exception as exc:
        logger.warning(
            "Manual call: prepare_playback_recording failed log_id={}: {}",
            live_log_id,
            exc,
        )

    # ── Voicemail override check (live session flag + transcript) ──
    is_voicemail_flag = _session_is_voicemail(camp_id=camp_id, log_id=live_log_id)

    if is_voicemail_flag:
        logger.info("Manual call {} marked as Voicemail in session — bypass LLM.", camp_id)
        await finalize_manual_call_record(
            camp_id, live_log_id, duration_sec, _voicemail_analysis_dict(for_manual=True)
        )
        return

    raw_live_jsonl = (_read_transcript_jsonl(role, live_log_id) or "").strip()
    if raw_live_jsonl and _transcript_indicates_voicemail(raw_live_jsonl):
        logger.info("Manual call {} live JSONL indicates voicemail — marking Voice Mail", camp_id)
        await finalize_manual_call_record(
            camp_id, live_log_id, duration_sec, _voicemail_analysis_dict(for_manual=True)
        )
        return

    transcript, _tx_source = await _resolve_call_transcript(role, live_log_id)

    if _transcript_indicates_voicemail(transcript):
        logger.info("Manual call {} transcript indicates voicemail — marking Voice Mail", camp_id)
        await finalize_manual_call_record(
            camp_id, live_log_id, duration_sec, _voicemail_analysis_dict(for_manual=True)
        )
        return

    analysis: dict
    if not (transcript or "").strip():
        analysis = {
            "summary": "Call ended; transcript unavailable.",
            "rating": 0,
            "next_steps": "N/A",
            "disposition": "Answered",
            "emotion_label": "Unknown",
            "emotion_rationale": "No speech captured in transcript.",
            "emotion_confidence": None,
        }
    else:
        from services.call_analyzer import analyze_call_transcript
        from services.callback_time import annotate_analysis_callback_epoch
        from services.transcript_interest import apply_interest_disposition_override

        async def _run_manual_analysis() -> dict:
            a = await analyze_call_transcript(transcript)
            annotate_analysis_callback_epoch(
                a,
                tz_name=settings.transcript_callback_tz,
                transcript_text=transcript,
            )
            a = apply_interest_disposition_override(a, transcript)
            if raw_live_jsonl and _transcript_indicates_voicemail(raw_live_jsonl):
                a = _voicemail_analysis_dict(for_manual=True)
            return a

        try:
            analysis = await asyncio.wait_for(_run_manual_analysis(), timeout=ANALYSIS_TIMEOUT_SEC)
            analysis["analysis_pending"] = False
        except asyncio.TimeoutError:
            logger.warning(
                "Manual call analysis timed out ({}s) for camp_id={} — heuristic + background retry",
                ANALYSIS_TIMEOUT_SEC,
                camp_id,
            )
            analysis = _heuristic_analysis_safe(transcript, gemini_error="timeout")
            analysis["next_steps"] = analysis.get("next_steps") or "N/A"
        except Exception as e:
            logger.exception("Manual call analysis failed for camp_id={}", camp_id)
            analysis = _heuristic_analysis_safe(transcript, gemini_error=str(e)[:120])
            analysis["next_steps"] = analysis.get("next_steps") or "N/A"

    try:
        from core.state import _CAMPAIGN_DATA
        if (_CAMPAIGN_DATA.get(camp_id) or {}).get("_prefer_whatsapp_only"):
            analysis["prefer_whatsapp_only"] = True
    except Exception:
        pass

    # ── Schedule callback if requested ─────────────────────────────
    try:
        rem_f_manual = None
        try:
            rem_f_manual = float(analysis.get("callback_reminder_epoch"))
        except (TypeError, ValueError):
            pass

        if rem_f_manual is not None and rem_f_manual > time.time():
            from core.storage import add_scheduled_callback as _add_cb_manual, manual_call_row_by_camp_id as _mcr_cb, find_lead_by_phone
            _mc_cb = await _mcr_cb(camp_id)
            if _mc_cb:
                _cb_phone = _mc_cb.get("to_phone", "")
                _cb_name = _mc_cb.get("callee_name", "") or "Manual Call"
                if _cb_phone:
                    _out_manual = ""
                    _cb_lead_id = None
                    try:
                        from core.state import _CAMPAIGN_DATA as _cd
                        _out_manual = str((_cd.get(camp_id) or {}).get("_outbound_phone") or "").strip()
                    except Exception:
                        pass
                    try:
                        _lr = await find_lead_by_phone(role, _cb_phone)
                        if _lr:
                            _cb_lead_id = int(_lr["id"])
                    except Exception:
                        pass
                    _cb_id = await _add_cb_manual(
                        role,
                        phone=_cb_phone,
                        name=_cb_name,
                        scheduled_at=rem_f_manual,
                        lead_id=_cb_lead_id,
                        outbound_phone=_out_manual,
                    )
                    logger.info(
                        "Manual call: scheduled callback id={} for {} ({}) at epoch={:.0f} (camp_id={})",
                        _cb_id, _cb_name, _cb_phone, rem_f_manual, camp_id,
                    )
    except Exception as e:
        logger.exception("Manual call: callback scheduling failed for camp_id={}: {}", camp_id, e)

    # ── Lead memory persistence for manual legs ─────────────────────
    # Manual calls don't own a campaign lead, but when the dialed number
    # matches an existing lead, update that lead's rolling memory so a later
    # scheduled-callback / campaign leg continues the conversation.
    try:
        from core.storage import find_lead_by_phone, manual_call_row_by_camp_id as _mcr_mem
        _mc_mem = await _mcr_mem(camp_id)
        if _mc_mem and _mc_mem.get("to_phone"):
            _mem_lead = await find_lead_by_phone(role, _mc_mem["to_phone"])
            if _mem_lead and _mem_lead.get("id"):
                _persist_lead_memory(
                    int(_mem_lead["id"]), analysis,
                    canon_disp=str(analysis.get("disposition") or ""),
                )
                logger.info(
                    "Manual call lead memory updated for lead {} (camp_id={})",
                    _mem_lead["id"], camp_id,
                )
    except Exception as _mem_man_e:
        logger.debug("Manual call lead memory write skipped camp_id={}: {}", camp_id, _mem_man_e)

    await finalize_manual_call_record(camp_id, live_log_id, duration_sec, analysis)
    try:
        from core.events import get_event_bus
        await get_event_bus().publish("lead_updated", role=role, lead_id=None)
    except Exception:
        pass
    logger.info(
        "Manual call outcome saved camp_id={} disposition={!r}",
        camp_id,
        analysis.get("disposition"),
    )
    
    # Release active and phone slots for manual calls
    try:
        await release_manual_call_resources(camp_id, role)
    except Exception as e:
        logger.exception("Failed to release slots for manual call {}: {}", camp_id, e)

    # ── WhatsApp auto-send for manual calls (same rules as campaign) ──
    try:
        from core.storage import find_lead_by_phone, manual_call_row_by_camp_id as _mcr
        from services.whatsapp_outcome import send_outcome_whatsapp_if_eligible

        _mc_row = await _mcr(camp_id)
        if _mc_row:
            _phone_wa = _mc_row.get("to_phone", "")
            _name_wa = _mc_row.get("callee_name", "")
            _linked_lead_id = None
            if _phone_wa:
                _dl = await find_lead_by_phone(role, _phone_wa)
                if _dl:
                    _linked_lead_id = int(_dl["id"])
            _manual_status = ""
            if analysis.get("site_visit_agreed"):
                _manual_status = "site_visit"
            elif analysis.get("callback_reminder_epoch") or analysis.get("requested_callback_datetime_iso"):
                _manual_status = "callback_scheduled"
            await send_outcome_whatsapp_if_eligible(
                role=role,
                phone=_phone_wa,
                lead_name=_name_wa,
                disposition=str(analysis.get("disposition") or ""),
                status=_manual_status,
                analysis=analysis,
                lead_id=_linked_lead_id,
                camp_id=camp_id,
            )
            try:
                from core.state import _CAMPAIGN_DATA as _cd_mc
                from services.whatsapp_outcome import send_agent_promised_whatsapp

                _mc_meta = _cd_mc.get(camp_id) if camp_id else None
                if isinstance(_mc_meta, dict) and _mc_meta.get("_whatsapp_pending"):
                    await send_agent_promised_whatsapp(
                        role=role,
                        camp_id=camp_id,
                        lead_id=_linked_lead_id,
                        lead_name=_name_wa,
                        summary=str(_mc_meta.get("_whatsapp_pending_summary") or analysis.get("summary") or ""),
                    )
            except Exception as _mc_wa:
                logger.warning("Manual call agent-promised WhatsApp failed {}: {}", camp_id, _mc_wa)
    except Exception as e:
        logger.exception("WhatsApp auto-send failed for manual call {}: {}", camp_id, e)

    # ── Email auto-send for manual calls ───────────────────────────
    try:
        if not analysis.get("prefer_whatsapp_only"):
            from core.storage import manual_call_row_by_camp_id as _mcr_em, find_lead_by_phone
            from services.email_leads import send_bulk_project_email
            _mc_em = await _mcr_em(camp_id)
            if _mc_em:
                _phone_em = _mc_em.get("to_phone", "")
                email_to = (analysis.get("email_address") or "").strip()
                if not email_to and _phone_em:
                    _dl = await find_lead_by_phone(role, _phone_em)
                    if _dl:
                        email_to = (_dl.get("email") or "").strip()
                if email_to and "@" in email_to:
                    em_result = await send_bulk_project_email(
                        email_to,
                        summary=analysis.get("summary", ""),
                        lead_name=_mc_em.get("callee_name", ""),
                    )
                    logger.info("Email sent for manual call {} ({}): {}", camp_id, email_to, em_result)
    except Exception as e:
        logger.exception("Email auto-send failed for manual call {}: {}", camp_id, e)

    # ── Virtual Meet tracking for manual calls ─────────────────────
    try:
        _vm_next = analysis.get("next_action") or {}
        if (_vm_next.get("action_type") or "").strip().lower() in ("virtual meet", "virtual"):
            from core.storage import manual_call_row_by_camp_id as _mcr_vm
            _mc_row_vm = await _mcr_vm(camp_id)
            if _mc_row_vm:
                _vm_details = _vm_next.get("details") or analysis.get("summary", "")
                logger.info("Virtual Meet requested in manual call {}: {}", camp_id, _vm_details)
    except Exception as e:
        logger.exception("Virtual Meet tracking failed for manual call {}: {}", camp_id, e)

    # ── Site Visit tracking for manual calls ─────────────────────
    try:
        _sv_next_mc = analysis.get("next_action") or {}
        if analysis.get("site_visit_agreed") or (_sv_next_mc.get("action_type") or "").strip().lower() in ("site visit", "site_visit"):
            _sv_details_mc = _sv_next_mc.get("details") or analysis.get("summary", "")
            logger.info("Site Visit agreed in manual call {}: {}", camp_id, _sv_details_mc)
            
            # Replicate campaign site visit callback scheduling for manual calls
            from datetime import datetime, timedelta
            from core.storage import manual_call_row_by_camp_id as _mcr_sv_mc, add_scheduled_callback as _add_cb_sv_mc
            from services.callback_time import zoneinfo_safe
            
            _mc_row_sv = await _mcr_sv_mc(camp_id)
            if _mc_row_sv:
                _phone = _mc_row_sv.get("to_phone", "")
                _name = _mc_row_sv.get("callee_name", "") or "Manual Call"
                
                # Check if we have a matching lead_id in the db first
                from core.storage import find_lead_by_phone
                _db_lead = await find_lead_by_phone(role, _phone)
                _lead_id = _db_lead.get("id") if _db_lead else None
                _out_mc = ""
                try:
                    from core.state import _CAMPAIGN_DATA
                    _out_mc = str((_CAMPAIGN_DATA.get(camp_id) or {}).get("_outbound_phone") or "").strip()
                except Exception:
                    pass
                
                # Extract site visit date/time
                _sv_date_str = (_sv_next_mc.get("datetime_iso") or analysis.get("requested_callback_datetime_iso") or "").strip()
                if _sv_date_str and _phone:
                    tz = zoneinfo_safe(settings.transcript_callback_tz)
                    if _sv_date_str.endswith("Z") or _sv_date_str.endswith("z"):
                        _sv_date_str = _sv_date_str[:-1] + "+00:00"
                    
                    sv_dt = datetime.fromisoformat(_sv_date_str)
                    if sv_dt.tzinfo is None:
                        sv_dt = sv_dt.replace(tzinfo=tz)
                    else:
                        sv_dt = sv_dt.astimezone(tz)
                        
                    now_dt = datetime.now(tz)
                    
                    # 1. Day-Before Re-confirmation Call
                    recon_dt = datetime.combine(
                        sv_dt.date() - timedelta(days=1),
                        now_dt.time()
                    ).replace(tzinfo=tz)
                    recon_epoch = recon_dt.timestamp()
                    
                    # 2. Day-of Site Visit Call
                    if sv_dt.hour < 12:
                        day_of_dt = sv_dt - timedelta(hours=2)
                        if day_of_dt.hour < 9:
                            day_of_dt = day_of_dt.replace(hour=9, minute=0, second=0, microsecond=0)
                    else:
                        day_of_dt = sv_dt.replace(hour=10, minute=0, second=0, microsecond=0)
                    day_of_epoch = day_of_dt.timestamp()
                    
                    if recon_epoch > time.time():
                        await _add_cb_sv_mc(
                            role=role,
                            phone=_phone,
                            name=f"{_name} (Re-confirm Site Visit)",
                            scheduled_at=recon_epoch,
                            lead_id=_lead_id,
                            outbound_phone=_out_mc,
                        )
                        logger.info(
                            "Manual Call: Automatically scheduled day-before site visit re-confirmation callback for {} ({}) at {}",
                            _name, _phone, recon_dt
                        )
                        
                    if day_of_epoch > time.time():
                        await _add_cb_sv_mc(
                            role=role,
                            phone=_phone,
                            name=f"{_name} (Day of Site Visit)",
                            scheduled_at=day_of_epoch,
                            lead_id=_lead_id,
                            outbound_phone=_out_mc,
                        )
                        logger.info(
                            "Manual Call: Automatically scheduled day-of site visit confirmation callback for {} ({}) at {}",
                            _name, _phone, day_of_dt
                        )
    except Exception as e:
        logger.exception("Site Visit tracking failed for manual call {}: {}", camp_id, e)


async def _finalize_incoming_call_leg(
    role: str, camp_id: str, live_log_id: str, duration_sec: float | None = None
) -> None:
    """Post-call analyzer + SQLite row for incoming (customer call-back) legs."""
    from core.storage import (
        finalize_incoming_call_record,
        incoming_call_row_by_camp_id,
        add_lead as _inbound_add_lead,
        update_lead_status as _inbound_update_status,
        update_lead_call_info as _inbound_update_info,
    )

    row = await incoming_call_row_by_camp_id(camp_id)
    if not row:
        logger.warning("Incoming call finalize: no incoming_calls row for camp_id={}", camp_id)
        return

    try:
        from services.call_recording import prepare_playback_recording

        playback = await prepare_playback_recording(live_log_id, camp_id=camp_id)
        if playback:
            logger.info(
                "Incoming call: playback recording ready log_id={} path={}",
                live_log_id,
                playback,
            )
    except Exception as exc:
        logger.warning(
            "Incoming call: prepare_playback_recording failed log_id={}: {}",
            live_log_id,
            exc,
        )

    transcript, _tx_source = await _resolve_call_transcript(role, live_log_id)

    analysis: dict
    if not (transcript or "").strip():
        analysis = {
            "summary": "Call ended; transcript unavailable.",
            "rating": 0,
            "next_steps": "N/A",
            "disposition": "Answered",
            "emotion_label": "Unknown",
            "emotion_rationale": "No speech captured in transcript.",
            "emotion_confidence": None,
        }
    else:
        from services.call_analyzer import analyze_call_transcript

        try:
            analysis = await asyncio.wait_for(
                analyze_call_transcript(transcript, role=role),
                timeout=ANALYSIS_TIMEOUT_SEC,
            )
            analysis["analysis_pending"] = False
        except asyncio.TimeoutError:
            logger.warning(
                "Incoming call analysis timed out ({}s) for camp_id={} — heuristic + background retry",
                ANALYSIS_TIMEOUT_SEC,
                camp_id,
            )
            analysis = _heuristic_analysis_safe(transcript, gemini_error="timeout")
            analysis["next_steps"] = analysis.get("next_steps") or "N/A"
            asyncio.create_task(
                _background_upgrade_incoming_analysis(role, camp_id, live_log_id, transcript)
            )
        except Exception as e:
            logger.exception("Incoming call analysis failed for camp_id={}", camp_id)
            analysis = _heuristic_analysis_safe(transcript, gemini_error=str(e)[:120])
            analysis["next_steps"] = analysis.get("next_steps") or "N/A"
            asyncio.create_task(
                _background_upgrade_incoming_analysis(role, camp_id, live_log_id, transcript)
            )

    await finalize_incoming_call_record(camp_id, live_log_id, duration_sec, analysis)
    logger.info(
        "Incoming call outcome saved camp_id={} disposition={!r}",
        camp_id,
        analysis.get("disposition"),
    )

    try:
        from core.events import get_event_bus

        updated = await incoming_call_row_by_camp_id(camp_id)
        if updated:
            await get_event_bus().publish(
                "incoming_call_completed",
                role=role,
                camp_id=camp_id,
                id=updated.get("id"),
                from_phone=updated.get("from_phone"),
                caller_name=updated.get("caller_name"),
                status=updated.get("status"),
                summary=updated.get("summary"),
                log_id=updated.get("log_id"),
                disposition=updated.get("disposition"),
            )
    except Exception:
        pass

    # Create a lead record so the dashboard shows this incoming call
    from_phone = row.get("from_phone", "")
    caller_name = row.get("caller_name", "") or "Inbound Call"
    if from_phone:
        try:
            from core.storage import find_lead_by_phone, update_lead_info
            existing_lead = await find_lead_by_phone(role, from_phone)
            if existing_lead:
                lead_id = existing_lead["id"]
                # Update caller name if previous was generic
                if existing_lead.get("name") in ("", "Inbound Call", "unknown", None) and caller_name != "Inbound Call":
                    await update_lead_info(lead_id, name=caller_name)
            else:
                lead_id = await _inbound_add_lead(role, name=caller_name, phone=from_phone)

            started_at = row.get("started_at")
            start_epoch = None
            if started_at:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
                    start_epoch = dt.timestamp() - (duration_sec or 0)
                except Exception:
                    start_epoch = time.time() - (duration_sec or 0)
            await _inbound_update_info(lead_id, log_id=live_log_id, start_time=start_epoch or time.time())
            # Avoid double Gemini analysis: reuse incoming summary when present
            _sum = str(analysis.get("summary") or "").strip()
            if _sum and "timed out" not in _sum.lower() and "unavailable" not in _sum.lower():
                from core.storage import update_lead_status as _uls

                await _uls(lead_id, "completed", analysis=analysis)
                # Persist rolling lead memory even on the precomputed path
                # (the else-branch runs _analyze_and_update_lead which writes it).
                _persist_lead_memory(
                    lead_id, analysis,
                    canon_disp=str(analysis.get("disposition") or ""),
                )
                try:
                    from core.dashboard_state import invalidate_role as _dash_inv

                    _dash_inv(role)
                except Exception:
                    pass
                if analysis.get("analysis_pending"):
                    asyncio.create_task(
                        _background_upgrade_lead_analysis(
                            role, lead_id, transcript, log_id=live_log_id, camp_id=camp_id
                        )
                    )
                logger.info(
                    "Incoming call lead updated with precomputed analysis: id={} phone={}",
                    lead_id,
                    from_phone,
                )
            else:
                await _analyze_and_update_lead(role, lead_id, live_log_id, camp_id=camp_id)
                logger.info("Incoming call lead updated/created and analyzed: id={} phone={}", lead_id, from_phone)
        except Exception as e:
            logger.exception("Failed to create lead for incoming call: {}", e)


async def _heal_stuck_incoming_calls(max_age_minutes: int = 15) -> int:
    """Finalize inbound rows stuck at connected/ringing (dashboard transcript/recording gap)."""
    from core.storage import list_stuck_incoming_calls

    rows = await list_stuck_incoming_calls(max_age_minutes=max_age_minutes, limit=25)
    healed = 0
    for row in rows:
        camp_id = str(row.get("camp_id") or "").strip()
        role = str(row.get("role") or "sales_1").strip()
        log_id = str(row.get("log_id") or "").strip()
        if not camp_id:
            continue
        try:
            dur = row.get("duration_sec")
            if not log_id:
                log_id = f"camp-heal-{camp_id[:12]}"
            await _finalize_incoming_call_leg(role, camp_id, log_id, dur)
            healed += 1
        except Exception as exc:
            logger.warning("Stuck incoming heal failed camp_id={}: {}", camp_id, exc)
    if healed:
        logger.info("Self-heal: finalized {} stuck inbound call row(s)", healed)
    return healed


async def _sweep_pending_vobiz_recordings(hours: int = 24, limit: int = 30) -> int:
    """Background retry: pull missing Application recordings into dashboard storage."""
    from core.storage import list_pending_vobiz_recording_targets
    from services.call_recording import resolve_dashboard_recording_path
    from services.vobiz_bridge.vobiz_recording import ensure_vobiz_application_recording

    if not getattr(settings, "vobiz_trunk_recording_enabled", True):
        return 0

    targets = await asyncio.to_thread(list_pending_vobiz_recording_targets, hours, limit)
    fixed = 0
    seen: set[str] = set()
    for row in targets:
        log_id = str(row.get("log_id") or "").strip()
        if not log_id or log_id in seen:
            continue
        seen.add(log_id)
        if resolve_dashboard_recording_path(log_id):
            continue
        camp_id = str(row.get("camp_id") or "").strip()
        try:
            result = await ensure_vobiz_application_recording(
                log_id,
                camp_id=camp_id,
                initial_delay_sec=0.0,
            )
            if result.get("ok") and resolve_dashboard_recording_path(log_id):
                fixed += 1
                logger.info(
                    "Vobiz recording sweep ingested log_id={} source={}",
                    log_id,
                    row.get("source"),
                )
            else:
                logger.debug(
                    "Vobiz recording sweep pending log_id={} result={}",
                    log_id,
                    result,
                )
        except Exception as exc:
            logger.warning("Vobiz recording sweep failed log_id={}: {}", log_id, exc)
    if fixed:
        logger.info("Vobiz recording sweep: ingested {} recording(s)", fixed)
    return fixed


async def _schedule_failed_call_retry(role: str, lead_id: int, lead_phone: str, lead_name: str) -> None:
    """
    Schedule re-dial for failed / no-answer / no-response leads.

    Policy: up to FAILED_CALL_MAX_ATTEMPTS (default 3) total dial attempts,
    spaced FAILED_CALL_RETRY_HOURS (default 24h) apart at the **same local clock time**
    as the original attempt. Retries fire during the line's inter-call rest window
    when due (scheduled_callbacks), not immediately after failure.
    """
    try:
        import json
        import time
        from core.storage import get_lead, add_scheduled_callback, update_lead_retry_state
        from services.callback_time import zoneinfo_safe
        from datetime import datetime

        max_attempts = max(1, int(settings.failed_call_max_attempts))
        max_retries = max_attempts - 1  # retries after the first attempt

        lead_row = await get_lead(role, lead_id)
        if not lead_row:
            return

        current_status = (lead_row.get("status") or "").lower()
        DO_NOT_RETRY = {
            "completed", "not_interested", "callback_completed",
            "interested", "site_visit", "site_visited", "callback_scheduled", "dnc",
        }
        if current_status in DO_NOT_RETRY:
            logger.info(
                "Skipping failed-call retry for lead {} ({}) — status is already {}",
                lead_id, lead_name, current_status,
            )
            return

        extra = lead_row.get("extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            extra = {}

        retries = int(extra.get("failed_call_retries") or 0)

        if retries >= max_retries:
            from core.storage import update_lead_status
            await update_lead_status(
                lead_id, "no response",
                error=f"No answer after {max_attempts} attempts (original + {max_retries} retakes)",
            )
            logger.info(
                "Failed-call retry limit reached ({}/{} attempts) for lead {} → no response",
                retries + 1, max_attempts, lead_id,
            )
            return

        # Retry at same local clock time + N×24h from the original dial (not now+24h).
        # Optional short rest-window retry only when FAILED_CALL_REST_RETRY_ENABLED=true.
        campaign_live = bool(_CAMPAIGN_TASKS.get(role))
        anchor_epoch = float(
            extra.get("original_called_at")
            or lead_row.get("first_called_at")
            or lead_row.get("start_time")
            or time.time()
        )
        extra["original_called_at"] = anchor_epoch

        next_retry_count = retries + 1
        retry_hours = max(1, int(settings.failed_call_retry_hours))
        import datetime as _dt
        from services.callback_time import zoneinfo_safe as _zsafe

        # Campaign "When to retry" setting: 'same_run' retries within this run
        # after a short delay; 'next_day' (default) keeps the same-clock-time
        # +N×24h anchor behavior below.
        retry_when = "next_day"
        try:
            from core.state import get_state
            _cfg = (get_state(role).get("campaign_config") or {})
            retry_when = str(_cfg.get("retry_when") or _cfg.get("auto_retry_when") or "next_day").lower()
        except Exception:
            pass

        _IST = _zsafe(settings.transcript_callback_tz)
        anchor_dt = _dt.datetime.fromtimestamp(anchor_epoch, tz=_IST)
        retry_dt = anchor_dt + _dt.timedelta(hours=retry_hours * next_retry_count)
        retry_epoch = retry_dt.timestamp()

        if campaign_live and rest_retry_enabled() and role in ("sales_1",):
            retry_epoch = time.time() + failed_retry_delay_sec()
        elif retry_when in ("same_run", "same run", "same-run"):
            # Same-run retry: fire again after the inter-call rest delay so the
            # contact gets a second chance inside today's window.
            retry_epoch = time.time() + max(60.0, failed_retry_delay_sec())

        _hr = retry_dt.hour + retry_dt.minute / 60.0
        if _hr >= 19.5 or _hr < 9.5:
            _morning = retry_dt.replace(hour=9, minute=30, second=0, microsecond=0)
            if retry_dt.hour >= 19:
                _morning += _dt.timedelta(days=1)
            retry_epoch = _morning.timestamp()

        extra["failed_call_retries"] = next_retry_count

        analysis = {}
        if lead_row.get("analysis"):
            try:
                analysis = json.loads(lead_row.get("analysis"))
            except Exception:
                analysis = {}

        analysis["system_redial"] = True
        analysis["system_redial_epoch"] = retry_epoch
        analysis.pop("callback_reminder_epoch", None)
        analysis.pop("requested_callback_datetime_iso", None)
        analysis["failed_attempt_number"] = next_retry_count + 1
        analysis["failed_max_attempts"] = max_attempts

        orig_status = lead_row.get("status") or "failed"
        if orig_status == "busy":
            analysis["disposition"] = "Busy"
        elif orig_status in ("no_answer", "no answer", "no response", "no_response"):
            analysis["disposition"] = "No Answer"
        else:
            orig_disp = analysis.get("disposition")
            if orig_disp in ("Failed", "No Answer", "Busy", "Wrong Number", "Not Available", "Voicemail", "Voice Mail", "No Response"):
                analysis["disposition"] = orig_disp
            else:
                analysis["disposition"] = "Failed"

        await update_lead_retry_state(lead_id, status=orig_status, extra=extra, analysis=analysis)
        await add_scheduled_callback(
            role=role,
            phone=lead_phone,
            name=f"{lead_name} (Retake {next_retry_count}/{max_retries})",
            scheduled_at=retry_epoch,
            lead_id=lead_id,
            outbound_phone=(lead_row.get("outbound_phone") or "").strip(),
            callback_type="failed_retry",
        )
        logger.info(
            "Scheduled failed-call retake {}/{} for lead {} ({}) at {} ({})",
            next_retry_count,
            max_retries,
            lead_id,
            lead_phone,
            retry_epoch,
            "rest-window" if campaign_live and rest_retry_enabled() else f"{retry_hours}h@same-clock",
        )
    except Exception:
        logger.exception("Failed to schedule failed call retry for lead {}", lead_id)


async def _campaign_worker_role(role: str):
    """Campaign manager task that spawns concurrent sub-workers per phone line."""
    logger.info(f"Campaign manager for {role} started.")
    # Mark activity now so the stall watchdog does not treat a freshly started
    # worker as "stalled" before its first call has had time to complete.
    _LAST_WORKER_ACTIVITY[role] = time.time()
    await _recover_stale_dialing(role)

    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    from core.outbound_numbers import dialable_outbound_numbers

    numbers = dialable_outbound_numbers(role, v_cfg)
    if not numbers:
        v_auth_id, v_token, v_from, v_base = resolve_vobiz_credentials(role, v_cfg)
        numbers = [v_from] if v_from else []

    if not numbers:
        logger.error(f"No phone numbers configured for role={role}. Stopping campaign.")
        await set_campaign_want_running(role, False)
        _CAMPAIGN_TASKS[role] = None
        return

    from core.state import get_max_concurrency_for_role
    effective_concurrency = get_max_concurrency_for_role(role)
    numbers = numbers[:effective_concurrency]
    num_phones = len(numbers)
    logger.info(
        f"Spawning {num_phones} parallel sub-workers for {role}. "
        f"Pipeline: effective concurrency {effective_concurrency} "
        f"(campaign/Vobiz/line/app caps applied); "
        f"resting lines run scheduled callbacks/failed retries while idle lines dial campaign."
    )

    sub_tasks = []
    for idx, phone in enumerate(numbers):
        task = asyncio.create_task(
            _campaign_sub_worker_role(role, phone, idx, num_phones)
        )
        sub_tasks.append(task)

    try:
        await asyncio.gather(*sub_tasks)
    except asyncio.CancelledError:
        logger.info(f"Campaign manager for {role} cancelled. Cancelling sub-workers.")
        for t in sub_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*sub_tasks, return_exceptions=True)
        raise
    except Exception as e:
        logger.exception(f"Campaign manager error for {role}: {e}")
    finally:
        logger.info(f"Campaign manager for {role} finished.")


async def _campaign_sub_worker_role(role: str, phone_number: str, phone_index: int, num_phones: int):
    """Worker task that dials leads for a specific phone number/index in parallel."""
    logger.info(f"Sub-worker {phone_index} ({phone_number}) for {role} started.")

    empty_since: float | None = None
    modulo = num_phones
    remainder = (phone_index + 1) % num_phones

    while True:
        try:
            if not _CAMPAIGN_TASKS.get(role):
                logger.info(f"Campaign task cancelled for {role} (sub-worker {phone_index}).")
                break

            # ── Check Daily Calling Limit ──
            try:
                from core.storage import get_daily_call_count_for_phone
                daily_count = await get_daily_call_count_for_phone(phone_number)
                daily_limit = int(getattr(settings, "daily_call_limit_per_phone", 220) or 220)
                if daily_count >= daily_limit:
                    logger.info(
                        "Phone {} reached daily limit {}/{} — idling until tomorrow",
                        phone_number, daily_count, daily_limit,
                    )
                    release_dialer_slot(phone_number)
                    await asyncio.sleep(120.0)
                    continue
            except Exception as daily_err:
                logger.warning("Daily call count check failed for {}: {}", phone_number, daily_err)

            # ── Check Hourly Calling Limit (~28–30 calls/phone/hr → ~48–50/hr per role) ──
            try:
                from core.storage import get_recent_call_outcomes_for_phone

                limit = _PHONE_HOURLY_LIMITS.get(phone_number)
                if not limit:
                    limit = _CAMPAIGN_HOURLY_CALLS_PER_PHONE
                    _PHONE_HOURLY_LIMITS[phone_number] = limit
                    
                recent_calls = await get_recent_call_outcomes_for_phone(phone_number, time.time() - 3600)
                hourly_count = len(recent_calls)
                
                if hourly_count >= limit:
                    logger.info(
                        "Outbound phone number {} has reached its hourly limit of {} calls ({}/{}) today. Idling...",
                        phone_number,
                        limit,
                        hourly_count,
                        limit,
                    )
                    release_dialer_slot(phone_number)
                    await asyncio.sleep(30.0)
                    continue
            except Exception as hourly_err:
                logger.warning("Failed to check hourly call count for {}: {}", phone_number, hourly_err)

            # ── REST window: this line only runs failed/callback retries; others dial campaign ──
            if phone_is_resting(phone_number):
                remaining = phone_rest_remaining(phone_number)
                if remaining > 0:
                    await run_phone_rest_cycle(
                        role,
                        phone_number,
                        phone_index,
                        remaining,
                        campaign_running=lambda: bool(_CAMPAIGN_TASKS.get(role)),
                        execute_callback=_execute_scheduled_callback,
                        cancellable_sleep=_cancellable_sleep,
                        continue_existing=True,
                    )
                continue

            # Cross-role turn only when both sales roles share one Vobiz account.
            if not await check_and_acquire_alternating_turn(role):
                await asyncio.sleep(0.5)
                continue

            if not vobiz_auth_can_accept_call(role):
                release_dialer_slot(phone_number, role)
                await asyncio.sleep(2.0)
                continue

            # Per-role dial slots (2 lines each) + global cap 4 when dual Vobiz accounts.
            if not acquire_dialer_slot(phone_number, role):
                await asyncio.sleep(0.35)
                continue

            # ── Campaign window / quiet-hours gate ──
            # When the campaign configures a calling window it is authoritative;
            # otherwise fall back to the global quiet-hours gate (default 19:30–09:30).
            try:
                from core.campaign_hours import campaign_dial_window_active
                from core.state import get_campaign_config as _campwin_cfg
                _camp_win = _campwin_cfg(role) or {}
            except Exception:
                _camp_win = {}
            if not campaign_dial_window_active(_camp_win):
                release_dialer_slot(phone_number)
                # Callbacks still run outside the window — check before idling
                try:
                    from core.storage import (
                        claim_next_immediate_callback,
                        role_has_due_scheduled_callbacks_for_phone,
                    )
                    now_cb = time.time()
                    if await role_has_due_scheduled_callbacks_for_phone(role, phone_number, now_cb):
                        if not phone_is_busy(phone_number):
                            immediate_cb = await claim_next_immediate_callback(
                                role, now_cb, outbound_phone=phone_number,
                            )
                            if immediate_cb:
                                logger.info(
                                    "Outside calling window: executing due callback id={} on line {}",
                                    immediate_cb["id"], phone_number,
                                )
                                await _execute_scheduled_callback(
                                    role, immediate_cb, outbound_phone=phone_number,
                                )
                                continue
                except Exception:
                    pass
                await asyncio.sleep(5.0)
                continue

            # ── Campaign config time checks (calling window / days / holidays) ──
            try:
                from core.campaign_hours import (
                    campaign_dial_window_active, is_within_calling_window, is_calling_day,
                    is_holiday_check, seconds_until_calling_window,
                )
                from core.state import get_campaign_config as _get_campaign_cfg
                _camp_cfg = _get_campaign_cfg(role) or {}
                if not campaign_dial_window_active(_camp_cfg):
                    release_dialer_slot(phone_number)
                    wait_sec = seconds_until_calling_window(_camp_cfg)
                    if wait_sec <= 0:
                        wait_sec = 60.0
                    wait_sec = min(wait_sec, 300.0)
                    logger.info(
                        "Campaign {} outside calling window/day/holiday — sleeping {:.0f}s",
                        role, wait_sec,
                    )
                    await asyncio.sleep(wait_sec)
                    continue
            except Exception as _cfg_err:
                logger.debug("Campaign config time check skipped for {}: {}", role, _cfg_err)

            try:
                await promote_due_scheduled_callbacks(time.time())
            except Exception as e:
                logger.exception("promote_due_scheduled_callbacks failed")

            state = get_state(role)

            # ── Priority: scheduled callbacks pause campaign on THIS line only ──
            try:
                from core.storage import (
                    claim_next_immediate_callback,
                    role_has_due_scheduled_callbacks_for_phone,
                )
                now_cb = time.time()
                has_due_cb = await role_has_due_scheduled_callbacks_for_phone(role, phone_number, now_cb)
                if has_due_cb:
                    if phone_is_busy(phone_number):
                        release_dialer_slot(phone_number)
                        await asyncio.sleep(0.5)
                        continue
                    immediate_cb = await claim_next_immediate_callback(role, now_cb, outbound_phone=phone_number)
                    if immediate_cb:
                        empty_since = None
                        logger.info(
                            "Executing scheduled callback id={} for {} ({}) on line {} — campaign paused on this number",
                            immediate_cb["id"],
                            immediate_cb.get("name", "?"),
                            immediate_cb.get("phone", "?"),
                            phone_number,
                        )
                        await _execute_scheduled_callback(role, immediate_cb, outbound_phone=phone_number)
                        continue
                    release_dialer_slot(phone_number)
                    await asyncio.sleep(0.5)
                    continue
            except Exception as e:
                logger.exception("Immediate callback check failed")

            # Do not dial campaign leads while a callback is due for this outbound line
            try:
                from core.storage import role_has_due_scheduled_callbacks_for_phone as _has_due_cb_line
                if await _has_due_cb_line(role, phone_number, time.time()):
                    release_dialer_slot(phone_number)
                    await asyncio.sleep(0.5)
                    continue
            except Exception:
                pass

            lead = None
            async with get_role_lead_lock(role):
                # Fetch a large pending pool, then fair-interleave across upload files.
                pending = await get_leads(role, status="pending", limit=_PENDING_FETCH_LIMIT)

                # ISOLATION SAFETY NET: this legacy worker is source-blind and would
                # otherwise dial digital leads from the cold lines (P1/P2) and race
                # the orchestrator. Even if it is ever running (shadow mode / manual
                # start), it must NEVER claim digital-marketing leads — those belong
                # exclusively to the orchestration dispatcher's P3 pool.
                pending = [
                    p for p in pending
                    if (str(p.get("source") or "campaign").strip().lower()
                        not in ("digital", "digital_marketing"))
                ]

                # ── Filter out paused upload sources ──
                from core.storage import get_paused_sources_sync
                paused_sources = get_paused_sources_sync(role)
                if paused_sources:
                    filtered = []
                    for p in pending:
                        p_src = _lead_upload_source(p)
                        if p_src not in paused_sources:
                            filtered.append(p)
                    pending = filtered

                # ── Filter out sources that hit daily call cap ──
                from core.storage import get_daily_call_count_for_source
                capped_sources = set()
                seen_sources = set()
                filtered2 = []
                for p in pending:
                    p_src = _lead_upload_source(p)
                    if p_src in capped_sources:
                        continue
                    if p_src not in seen_sources:
                        seen_sources.add(p_src)
                        try:
                            daily_src_count = await get_daily_call_count_for_source(role, p_src)
                            if daily_src_count >= _CALLS_PER_SOURCE_DAILY_MAX:
                                capped_sources.add(p_src)
                                continue
                        except Exception:
                            pass
                    filtered2.append(p)
                pending = _fair_interleave_pending_by_source(filtered2, role)

                def _sv_score(p):
                    src = str(p.get("extra") or "")
                    st = str(p.get("status") or "")
                    an = str(p.get("analysis") or "").lower()
                    if "_follow_up_memory" in src or "_callback_type" in src:
                        return 0
                    if "Assetz SV" in src or st == "site_visit" or "site_visit" in src or "site visit" in an:
                        return 1
                    return 2
                pending = sorted(pending, key=_sv_score)

                # ── Find the first pending lead that is not duplicate or DNC blocked ──
                for candidate in pending:
                    candidate_id = candidate["id"]
                    candidate_phone = candidate["phone"]
                    
                    if await is_duplicate_lead(role, candidate_phone, candidate_id):
                        logger.info(f"Skipping duplicate lead id={candidate_id} phone={candidate_phone}")
                        await update_lead_status(candidate_id, "failed", error="Duplicate lead skipped")
                        continue
                        
                    from core.dnc import is_phone_blocked
                    if is_phone_blocked(candidate_phone):
                        logger.warning(f"Skipping DNC blocked lead id={candidate_id} phone={candidate_phone}")
                        await update_lead_status(candidate_id, "failed", error="DNC blocked number")
                        continue
                    
                    # Found a valid lead! Atomically claim it
                    lead = candidate
                    await update_lead_status(lead["id"], "dialing")
                    await update_lead_call_info(lead["id"], start_time=time.time(), outbound_phone=phone_number)
                    break

            if not lead:
                release_dialer_slot(phone_number)
                try:
                    now_t = time.time()
                    from core.storage import role_has_pending_scheduled_callbacks as _has_sched_cb
                    has_future_cb = await role_has_future_callback_scheduled(role, now_t)
                    has_sched_cb = await _has_sched_cb(role)
                    if has_future_cb or has_sched_cb:
                        if role in ("sales_1",) and active_vobiz_calls_for_role(role) == 0:
                            yield_alternating_turn(role)
                        empty_since = None
                        await _cancellable_sleep(role, 15.0)
                        continue
                except Exception as e:
                    logger.exception("Deferred callback idle check failed")

                if role in ("sales_1",) and active_vobiz_calls_for_role(role) == 0:
                    yield_alternating_turn(role)

                if empty_since is None:
                    empty_since = time.time()
                    logger.info(f"Queue empty for {role} (sub-worker {phone_index}); waiting for new leads...")
                await asyncio.sleep(5.0)
                continue
            empty_since = None

            # Each sub-worker only waits for ITS OWN phone to be free.
            # Other phones in the same role can call simultaneously.
            if phone_is_busy(phone_number):
                logger.debug(
                    f"Phone {phone_number} busy for {role} sub-worker {phone_index} — waiting."
                )
                # Release the lead back to pending so another idle phone can pick it up!
                await update_lead_status(lead["id"], "pending")
                release_dialer_slot(phone_number)
                await asyncio.sleep(2)
                continue

            lead_id = lead["id"]
            lead_phone = lead["phone"]
            lead_name = lead.get("name", "Unknown")

            call_id = str(uuid.uuid4())
            _CAMPAIGN_DATA[call_id] = {
                **lead,
                "_lead_id": lead_id,
                "_leadIndex": -1,
                "_role": role,
                "_call_id": call_id,
                "_outbound_phone": phone_number,

            }

            v_cfg = state.get("vobiz", {}) or {}
            v_auth_id, v_token, _, v_base = resolve_vobiz_credentials(role, v_cfg)
            v_from = phone_number
            logger.info(f"Using phone number {v_from} for sub-worker {phone_index} of role {role}")

            from core.outbound_numbers import is_vobiz_auth_low_balance

            if is_vobiz_auth_low_balance(v_auth_id):
                logger.warning(
                    "Vobiz auth {} low balance pause — lead {} back to pending (sub-worker {})",
                    v_auth_id,
                    lead_id,
                    phone_index,
                )
                await update_lead_status(lead_id, "pending")
                release_dialer_slot(phone_number)
                await asyncio.sleep(60.0)
                continue

            if not v_auth_id or not v_token or not v_base or not v_from:
                logger.error(
                    f"Telephony not configured for role={role} (sub-worker {phone_index}): auth_id={'set' if v_auth_id else 'missing'}, "
                    f"token={'set' if v_token else 'missing'}, base={'set' if v_base else 'missing'}, "
                    f"from_number={'set' if v_from else 'missing'}."
                )
                await update_lead_status(lead_id, "failed", error="Telephony not configured")
                _CAMPAIGN_DATA.pop(call_id, None)
                break

            from services.vobiz_bridge import make_vobiz_call, VobizCallError
            slot_acquired = False
            sem_acquired = False
            role_sem_acquired = False
            try:
                if role in _ROLE_SEMAPHORES:
                    await _ROLE_SEMAPHORES[role].acquire()
                    role_sem_acquired = True
                await _GLOBAL_CALL_SEMAPHORE.acquire()
                sem_acquired = True
                try:
                    from services.campaign_live import set_active_campaign_call, clear_transcript_session
                    set_active_campaign_call(call_id)
                    clear_transcript_session(call_id)
                except Exception as _ce:
                    logger.exception("campaign_live setup skipped: {}", _ce)

                opening = _build_opening_line(lead, role)
                from core.greeting_pcm import ensure_opening_pcm, ensure_name_verify_pcm_for_call

                await ensure_opening_pcm(call_id, role, opening)
                if settings.scripted_name_verify_pcm:
                    await ensure_name_verify_pcm_for_call(call_id, role)

                from core.camp_session import prepare_outbound_call_session

                await prepare_outbound_call_session(
                    call_id, role, _CAMPAIGN_DATA[call_id], v_base, lead_id=lead_id
                )

                acquire_phone_slot(phone_number)   # mark THIS phone as busy
                if not acquire_vobiz_call_slot(role):
                    release_phone_slot(phone_number)
                    await update_lead_status(lead_id, "pending")
                    release_dialer_slot(phone_number, role)
                    _CAMPAIGN_DATA.pop(call_id, None)
                    if sem_acquired:
                        _GLOBAL_CALL_SEMAPHORE.release()
                        sem_acquired = False
                    if role_sem_acquired:
                        _ROLE_SEMAPHORES[role].release()
                        role_sem_acquired = False
                    logger.warning(
                        "Vobiz concurrent cap — returned lead {} to pending (role={})",
                        lead_id,
                        role,
                    )
                    await asyncio.sleep(2.0)
                    continue
                slot_acquired = True
                logger.info(
                    f"Call initiated on sub-worker {phone_index}: {lead_name} ({lead_phone}) "
                    f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                )

                call_placed = False
                try:
                    hangup_url = f"{v_base.rstrip('/')}/vobiz/hangup" if v_base else ""
                    await make_vobiz_call(
                        to=lead_phone, from_=v_from,
                        answer_url=f"{v_base}/vobiz/answer?camp_id={call_id}&role={role}",
                        auth_id=v_auth_id, auth_token=v_token,
                        hangup_url=hangup_url,
                        record=True,
                    )
                    call_placed = True
                except VobizCallError as ve:
                    from core.outbound_numbers import (
                        is_vobiz_from_line_blocked_error,
                        is_vobiz_insufficient_balance_error,
                        mark_outbound_line_blocked,
                        mark_vobiz_auth_low_balance,
                    )

                    if _is_vobiz_concurrent_limit_error(ve):
                        logger.warning(
                            "Vobiz concurrent limit on {} — lead {} back to pending",
                            v_from,
                            lead_id,
                        )
                        await update_lead_status(lead_id, "pending")
                    elif is_vobiz_insufficient_balance_error(ve):
                        mark_vobiz_auth_low_balance(v_auth_id)
                        logger.error(
                            "Vobiz insufficient balance (auth {}) — lead {} back to pending (sub-worker {})",
                            v_auth_id,
                            lead_id,
                            phone_index,
                        )
                        await update_lead_status(lead_id, "pending", error=None)
                    elif is_vobiz_from_line_blocked_error(ve):
                        mark_outbound_line_blocked(v_from)
                        logger.error(
                            "Vobiz blocked outbound line {} on sub-worker {} — skipping this line for 24h",
                            v_from,
                            phone_index,
                        )
                        await update_lead_status(lead_id, "pending")
                    else:
                        logger.error(
                            f"Vobiz refused call to {lead_phone} on sub-worker {phone_index}: HTTP {ve.status} — {ve.message}"
                        )
                        await update_lead_status(lead_id, "no response", error=f"Vobiz {ve.status}: {ve.message}")
                        await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name)
                    if role in ("sales_1",) and not is_vobiz_from_line_blocked_error(ve) and not _is_vobiz_concurrent_limit_error(ve) and not is_vobiz_insufficient_balance_error(ve):
                        await _send_failed_dial_whatsapp(role, lead_id, lead_phone, lead_name, disposition="No Response")

                        # Email auto-send on failed Vobiz call
                        try:
                            from core.storage import get_lead_email_sent, mark_email_sent
                            from services.email_leads import send_email_project_details
                            email_to = (lead.get("email") or "").strip()
                            if email_to and "@" in email_to:
                                if not await get_lead_email_sent(lead_id):
                                    logger.info("Call failed (VobizCallError) — sending email details for lead {}", lead_id)
                                    email_result = await send_email_project_details(
                                        email_to, summary="Following up with details.",
                                        outbound_phone=(lead.get("outbound_phone") or phone_number or "").strip(),
                                    )
                                    if email_result.get("sent"):
                                        await mark_email_sent(lead_id)
                                        await _notify_email_sent_event(role, lead_id)
                        except Exception as email_err:
                            logger.exception("Failed to send Email for failed call: {}", email_err)

                if call_placed:
                    answered = False
                    call_started_at = time.time()
                    max_ring_wait = float(os.getenv("OUTBOUND_MAX_RING_WAIT_SEC", "45") or 45)
                    MAX_TOTAL_WAIT = 360

                    while True:
                        if not _CAMPAIGN_TASKS.get(role):
                            break

                        from core.camp_session import poll_camp_session_into_memory

                        info = await poll_camp_session_into_memory(call_id, v_base)
                        if not answered and info.get("_call_connected_at"):
                            answered = True
                            logger.info(f"Call connected on sub-worker {phone_index} with {lead_name} ({lead_phone})")
                        if answered and info.get("_call_ended_at"):
                            logger.info(f"Call ended naturally on sub-worker {phone_index} with {lead_name}")
                            break

                        elapsed = time.time() - call_started_at
                        if not answered and elapsed >= max_ring_wait:
                            logger.warning(f"No answer for {lead_name} ({lead_phone}) after {max_ring_wait:.0f}s — moving on.")
                            break
                        if elapsed >= MAX_TOTAL_WAIT:
                            logger.warning(f"Call to {lead_name} exceeded {MAX_TOTAL_WAIT}s — forcing next.")
                            break

                        lead_finalized = False
                        try:
                            rows = await get_leads(role, limit=2000)
                            for l in rows:
                                if l["id"] == lead_id and l["status"] in ("completed", "not_interested", "failed", "no response", "no_response"):
                                    logger.info(f"Lead {lead_name} status finalized as {l['status']}")
                                    lead_finalized = True
                                    break
                        except Exception:
                            logger.exception("Lead status check failed")
                        if lead_finalized:
                            break

                        await asyncio.sleep(2)

                    if not answered:
                        logger.info(f"Lead {lead_name} did not connect — marking no answer.")
                        await update_lead_status(lead_id, "no answer", error="No answer / Timeout")
                        await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name)
                        if role in ("sales_1",):
                            await _send_failed_dial_whatsapp(role, lead_id, lead_phone, lead_name, disposition="No Answer")

                            # Email auto-send on No answer / Timeout
                            try:
                                from core.storage import get_lead_email_sent, mark_email_sent
                                from services.email_leads import send_email_project_details
                                email_to = (lead.get("email") or "").strip()
                                if email_to and "@" in email_to:
                                    if not await get_lead_email_sent(lead_id):
                                        logger.info("Call failed (No answer / Timeout) — sending email details for lead {}", lead_id)
                                        email_result = await send_email_project_details(
                                            email_to,
                                            summary="Following up with details.",
                                            outbound_phone=(lead.get("outbound_phone") or phone_number or "").strip(),
                                        )
                                        if email_result.get("sent"):
                                            await mark_email_sent(lead_id)
                                            await _notify_email_sent_event(role, lead_id)
                            except Exception as email_err:
                                logger.exception("Failed to send Email for failed call: {}", email_err)

                    log_id = (_CAMPAIGN_DATA.get(call_id, {}) or {}).get("_log_id")
                    if log_id:
                        try:
                            await update_lead_call_info(lead_id, log_id=log_id, call_id=call_id)
                        except Exception as exc:
                            logger.exception(f"Persist log_id failed for lead {lead_id}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Call trigger failed for {lead_phone}")
                await update_lead_status(lead_id, "no response", error=str(e))
                await _schedule_failed_call_retry(role, lead_id, lead_phone, lead_name)
                if role in ("sales_1",):
                    await _send_failed_dial_whatsapp(role, lead_id, lead_phone, lead_name, disposition="No Response")

                    # Email auto-send on Exception fail
                    try:
                        from core.storage import get_lead_email_sent, mark_email_sent
                        from services.email_leads import send_email_project_details
                        email_to = (lead.get("email") or "").strip()
                        if email_to and "@" in email_to:
                            if not await get_lead_email_sent(lead_id):
                                logger.info("Call failed (Exception) — sending email details for lead {}", lead_id)
                                email_result = await send_email_project_details(
                                    email_to,
                                    summary="Following up with details.",
                                    outbound_phone=(lead.get("outbound_phone") or phone_number or "").strip(),
                                )
                                if email_result.get("sent"):
                                    await mark_email_sent(lead_id)
                                    await _notify_email_sent_event(role, lead_id)
                    except Exception as email_err:
                        logger.exception("Failed to send Email for failed call: {}", email_err)
            finally:
                if slot_acquired:
                    release_phone_slot(phone_number)    # free THIS phone's slot
                    release_vobiz_call_slot(role)       # update role-level dashboard counter
                    logger.info(
                          f"Call slot released for {role} phone={phone_number} sub-worker {phone_index} "
                          f"[role_active={active_vobiz_calls_for_role(role)} total={total_active_vobiz_calls()}]"
                    )
                    if role in ("sales_1",) and active_vobiz_calls_for_role(role) == 0:
                        yield_alternating_turn(role)
                _CAMPAIGN_DATA.pop(call_id, None)
                if sem_acquired:
                    await asyncio.sleep(1.0)
                    _GLOBAL_CALL_SEMAPHORE.release()
                if role_sem_acquired:
                    _ROLE_SEMAPHORES[role].release()


            if not _CAMPAIGN_TASKS.get(role):
                break

            # Post-call rest: release dialer slot so other lines dial; run failed retries in this window
            release_dialer_slot(phone_number)
            gap = await inter_call_gap_seconds_for_phone(phone_number, role)
            await run_phone_rest_cycle(
                role,
                phone_number,
                phone_index,
                gap,
                campaign_running=lambda: bool(_CAMPAIGN_TASKS.get(role)),
                execute_callback=_execute_scheduled_callback,
                cancellable_sleep=_cancellable_sleep,
            )

        except asyncio.CancelledError:
            logger.info(f"Sub-worker {phone_index} for {role} cancelled.")
            break
        except Exception as e:
            logger.exception(f"Sub-worker {phone_index} error for {role}")
            await asyncio.sleep(10)
        finally:
            release_dialer_slot(phone_number)

    logger.info(f"Sub-worker {phone_index} for {role} finished.")


# ─── Campaign Scheduler ───────────────────────────────────────────────
# Polls the ``schedules`` table every ``_SCHEDULER_POLL_SEC`` seconds and,
# for each row whose ``run_at`` has been reached and ``status='scheduled'``,
# starts the same per-role campaign worker the **Start Campaign** button
# triggers — so a user can upload a CSV in the morning and have it dial out
# automatically at, say, 5 PM.

_SCHEDULER_POLL_SEC = float(os.getenv("CAMPAIGN_SCHEDULER_POLL_SEC", "30"))


async def _run_scheduled_campaign(
    role: str,
    schedule_id: int,
    stop_at: float | None = None,
):
    """Wrapper that ties a schedule row's lifecycle to a campaign worker run.

    Uses the same ``_CAMPAIGN_TASKS[role]`` slot the manual toggle uses so the
    Stop button, status endpoint, and dashboard pill all reflect the run
    correctly without any extra plumbing.

    If ``stop_at`` (epoch-UTC seconds) is provided, the campaign is forcibly
    stopped at that moment by cancelling the worker task. The schedule row is
    marked ``completed`` (not ``cancelled``) because reaching the end of the
    operator-defined window is the intended terminal state, not a failure.
    """
    stop_watcher: asyncio.Task | None = None
    stopped_by_window = False

    async def _window_stop_watcher() -> None:
        """Sleep until ``stop_at`` then cancel the campaign worker."""
        nonlocal stopped_by_window
        if stop_at is None:
            return
        delay = max(0.0, float(stop_at) - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        active = _CAMPAIGN_TASKS.get(role)
        if active and not active.done():
            # Wait for active Vobiz call to finish before cancelling
            while role_has_active_vobiz_call(role):
                logger.info(
                    "Scheduler stop window reached for role={}, but active call is in progress. "
                    "Waiting for call to complete before cancelling worker...",
                    role,
                )
                await asyncio.sleep(5.0)
            stopped_by_window = True
            logger.info(
                f"Scheduled campaign id={schedule_id} role={role!r}: "
                f"stop window reached — cancelling worker."
            )
            active.cancel()

    try:
        await mark_schedule_status(schedule_id, "running", started_at=time.time())
        task = asyncio.create_task(_campaign_worker_role(role))
        _CAMPAIGN_TASKS[role] = task
        if stop_at is not None:
            stop_watcher = asyncio.create_task(_window_stop_watcher())

        try:
            await task
        except asyncio.CancelledError:
            # If we cancelled the worker because the stop window expired, treat
            # it as a clean completion. Otherwise (Stop button / process
            # shutdown), surface as cancelled.
            if stopped_by_window:
                await mark_schedule_status(schedule_id, "completed")
                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} "
                    f"→ completed (auto-stopped at end of window)"
                )
                return
            raise
        # Worker exited naturally (queue empty + grace period).
        await mark_schedule_status(schedule_id, "completed")
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → completed")
    except asyncio.CancelledError:
        await mark_schedule_status(
            schedule_id, "cancelled", error="Run cancelled before completion."
        )
        logger.info(f"Scheduled campaign id={schedule_id} role={role!r} → cancelled")
        raise
    except Exception as e:
        await mark_schedule_status(schedule_id, "failed", error=str(e)[:300])
        logger.exception(f"Scheduled campaign id={schedule_id} role={role!r} failed")
    finally:
        if stop_watcher and not stop_watcher.done():
            stop_watcher.cancel()


async def _schedule_preflight(role: str) -> str | None:
    """Mirror of the checks in ``/api/campaign/toggle``. Returns an error string
    if the run cannot be started right now, else ``None``.
    """
    from core.storage import is_campaign_globally_paused

    if await is_campaign_globally_paused():
        return (
            "Campaign is paused. Outbound dialing will not run until you click "
            "Start during the campaign's calling window."
        )
    try:
        from core.campaign_hours import campaign_dial_window_active
        from core.state import get_campaign_config as _pre_cfg
        if not campaign_dial_window_active(_pre_cfg(role) or {}):
            return "Outside the campaign's calling window. Dialing will start when the window is active."
    except Exception:
        pass
    if is_campaign_quiet_hours():
        return quiet_hours_block_message()
    running = _CAMPAIGN_TASKS.get(role)
    if running and not running.done():
        return "A campaign is already running for this role."
    counts = await get_lead_counts(role)
    if counts.get("pending", 0) <= 0 and counts.get("dialing", 0) <= 0:
        return "No pending leads at scheduled time. Upload a list before the schedule fires."
    state = get_state(role)
    v_cfg = state.get("vobiz", {}) or {}
    auth_id, auth_token, _from_num, base_url = resolve_vobiz_credentials(role, v_cfg)
    if not auth_id or not auth_token or not base_url:
        return "Telephony bridge not configured (Vobiz Auth ID / Token / Public URL missing)."
    return None


async def _enforce_window_stop(sched: dict) -> None:
    """Force-stop a scheduled run whose stop window has expired.

    Two cases:
      a) The campaign worker is still running in this process → cancel it.
         The wrapper's CancelledError handler will mark the schedule as
         ``completed`` (because ``stopped_by_window`` is True after we cancel).
         Actually — the wrapper only flips ``stopped_by_window`` inside its
         own watcher. Since this enforcement path comes from the polling loop
         (e.g. after a server restart that lost the inline watcher), we mark
         the row directly and rely on the worker's CancelledError path to
         exit cleanly.
      b) No worker is running for this role (process restart, manual Stop) →
         just close out the row.
    """
    schedule_id = int(sched.get("id") or 0)
    role = normalize_console_role(sched.get("role") or "sales_1")
    if not schedule_id:
        return
    active = _CAMPAIGN_TASKS.get(role)
    if active and not active.done():
        logger.info(
            f"Scheduler: stop window reached for id={schedule_id} role={role!r} "
            f"after restart — cancelling worker."
        )
        active.cancel()
    await mark_schedule_status(
        schedule_id, "completed",
        error=None,
    )


def run_sqlite_backup():
    # PostgreSQL: crash-safe persistence is provided by the postgres_data
    # volume; logical backups are handled by a pg_dump cron (see watchdog).
    # Kept as a no-op hook so the Super CEO scheduler loop stays intact.
    logger.debug("Super CEO: PostgreSQL — backups via volume + pg_dump cron (no-op here)")


def run_wal_checkpoint():
    # PostgreSQL runs WAL checkpointing + autovacuum automatically.
    logger.debug("Super CEO: PostgreSQL — WAL checkpoint is automatic (no-op here)")

async def check_vobiz_health(role: str) -> dict:
    from core.state import get_state
    from core.vobiz_credentials import resolve_vobiz_credentials
    import httpx

    try:
        state = get_state(role) or {}
    except Exception:
        state = {}
    v_cfg = state.get("vobiz") or {}
    auth_id, token, _, _ = resolve_vobiz_credentials(role, v_cfg)

    if not auth_id or not token:
        return {"status": "unconfigured", "balance": None, "balance_known": False}

    # Trailing slash 307-redirects to account-service.vobiz.ai over plain HTTP and breaks httpx.
    url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": token,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 401:
            return {"status": "unauthorized", "balance": None, "balance_known": False}
        if r.status_code >= 400:
            return {"status": "error", "balance": None, "balance_known": False, "code": r.status_code}

        try:
            data = r.json()
        except Exception:
            logger.warning("Super CEO: Vobiz API response was not JSON: {}", r.text[:200])
            return {"status": "error", "balance": None, "balance_known": False}

        balance_raw = (
            data.get("credit_balance")
            or data.get("credits")
            or data.get("balance")
            or data.get("available_balance")
        )
        if balance_raw is None:
            # Account payload often omits spendable credits — do not treat as zero balance.
            return {"status": "ok", "balance": None, "balance_known": False}

        balance = float(balance_raw)
        return {"status": "ok", "balance": balance, "balance_known": True}
    except Exception as e:
        logger.warning("Super CEO: Vobiz health check failed: {}", e)
        return {"status": "exception", "balance": None, "balance_known": False, "error": str(e)}

async def check_gemini_health() -> dict:
    import httpx

    from config import settings
    from core.gemini_auth import gemini_auth_headers, gemini_generate_content_url, get_gemini_api_key

    api_key = get_gemini_api_key()
    if not api_key:
        return {"status": "unconfigured"}

    fallbacks = [
        "gemini-flash-latest",
        "gemini-3-flash-preview",
        (settings.gemini_call_analysis_model or "gemini-3.1-flash-lite").strip(),
    ]
    models: list[str] = []
    for m in fallbacks:
        if m and m not in models:
            models.append(m)

    payload = {"contents": [{"parts": [{"text": "Hello"}]}]}
    last_err: dict = {"status": "error", "code": 0, "text": ""}

    for model in models:
        url = gemini_generate_content_url(model)
        for attempt in range(1, 3):
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    r = await client.post(url, json=payload, headers=gemini_auth_headers(api_key))
                if r.status_code == 200:
                    # #region agent log
                    try:
                        from debug_agent_log import agent_debug

                        agent_debug(
                            "H1",
                            "worker.py:check_gemini_health",
                            "gemini_health_ok",
                            {"model": model, "attempt": attempt},
                        )
                    except Exception:
                        pass
                    # #endregion
                    return {"status": "ok", "model": model}
                if r.status_code in (503, 429) and attempt < 2:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                last_err = {"status": "error", "code": r.status_code, "text": r.text[:300], "model": model}
                break
            except Exception as e:
                last_err = {"status": "exception", "error": str(e), "model": model}
                break

    # #region agent log
    try:
        from debug_agent_log import agent_debug

        agent_debug(
            "H1",
            "worker.py:check_gemini_health",
            "gemini_health_failed",
            {"last_err": last_err, "models_tried": models},
        )
    except Exception:
        pass
    # #endregion
    return last_err

def _is_infra_call_failure(status: str, disposition: str, error: str) -> bool:
    """True only for telephony/API failures — not no-answer, voicemail, or normal outcomes."""
    s = (status or "").strip().lower()
    d = (disposition or "").strip().lower()
    e = (error or "").strip().lower()
    if s in ("completed", "no answer", "busy", "not_interested", "not interested"):
        return False
    if "voicemail" in d:
        return False
    if s != "failed":
        return False
    blob = f"{d} {e}"
    infra_markers = (
        "vobiz", "telephony", "gemini", "unauthorized", "blocked", "timeout",
        "connection", "503", "429", "401", "403", "500", "not configured",
        "rejected", "concurrent", "capacity", "unavailable",
    )
    if any(m in blob for m in infra_markers):
        return True
    return not d or d in ("failed", "error")


def check_recent_failure_rate_sync(role: str) -> dict:
    from core.storage import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT status, disposition, error FROM call_attempts
        WHERE role = ? AND created_at::timestamp >= datetime('now', '-30 minutes')
        ORDER BY id DESC
        """,
        (role,),
    ).fetchall()

    if not rows:
        return {"consecutive_failures": 0, "failure_rate": 0.0, "total_calls": 0}

    infra_rows = [r for r in rows if _is_infra_call_failure(
        str(r["status"] or ""), str(r["disposition"] or ""), str(r["error"] or "")
    )]
    total_calls = len(infra_rows)
    failed_calls = total_calls  # infra filter already selects failures only
    failure_rate = (failed_calls / len(rows)) if rows else 0.0

    consecutive_failures = 0
    for r in rows:
        if _is_infra_call_failure(
            str(r["status"] or ""), str(r["disposition"] or ""), str(r["error"] or "")
        ):
            consecutive_failures += 1
        else:
            break

    return {
        "consecutive_failures": consecutive_failures,
        "failure_rate": failure_rate,
        "total_calls": len(rows),
        "infra_failures": total_calls,
    }

def send_super_ceo_alert(subject: str, body: str):
    from config import settings
    import smtplib
    from email.mime.text import MIMEText

    email = settings.smtp_email
    password = settings.smtp_app_password
    host = settings.smtp_host or "smtp.gmail.com"
    port = settings.smtp_port or 587

    if not email or not password:
        logger.info("Super CEO: SMTP email not configured; skipping alert email.")
        return

    msg = MIMEText(body)
    msg["Subject"] = f"[Super CEO ALERT] {subject}"
    msg["From"] = email
    msg["To"] = email

    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(email, password)
            server.sendmail(email, [email], msg.as_string())
        logger.info("Super CEO: Alert email sent successfully to {}", email)
    except Exception as e:
        logger.error("Super CEO: Failed to send alert email: {}", e)


async def _process_missed_inbound_callbacks(now: float) -> None:
    """When an outbound line frees up, schedule callback for missed inbound callers."""
    try:
        from core.storage import (
            add_scheduled_callback,
            find_lead_by_phone,
            list_missed_busy_incoming_pending,
            mark_incoming_callback_scheduled,
        )
    except Exception as exc:
        logger.warning("Missed inbound callback imports failed: {}", exc)
        return

    delay = float(os.getenv("INBOUND_MISSED_CALLBACK_DELAY_SEC", "30"))
    rows = await list_missed_busy_incoming_pending()
    if not rows:
        return

    for row in rows:
        role = normalize_console_role(str(row.get("role") or ""))
        from_phone = str(row.get("from_phone") or "").strip()
        to_phone = str(row.get("to_phone") or "").strip()
        if not role or not from_phone:
            await mark_incoming_callback_scheduled(int(row["id"]))
            continue
        if role_has_active_vobiz_call(role):
            continue
        if to_phone and phone_is_busy(to_phone):
            continue
        if to_phone and phone_is_resting(to_phone):
            continue

        lead = await find_lead_by_phone(role, from_phone)
        lead_id = int(lead["id"]) if lead and lead.get("id") else None
        name = (
            str(row.get("caller_name") or "").strip()
            or str((lead or {}).get("name") or "").strip()
            or "Inbound caller"
        )
        try:
            cb_id = await add_scheduled_callback(
                role=role,
                phone=from_phone,
                name=f"{name} (missed inbound)",
                scheduled_at=now + delay,
                lead_id=lead_id,
                outbound_phone=to_phone,
            )
            await mark_incoming_callback_scheduled(int(row["id"]))
            logger.info(
                "Missed inbound callback scheduled: incoming_id={} cb_id={} role={} phone={!r}",
                row.get("id"),
                cb_id,
                role,
                from_phone,
            )
        except Exception as exc:
            logger.warning(
                "Missed inbound callback schedule failed for incoming_id={}: {}",
                row.get("id"),
                exc,
            )


async def _scheduler_loop():
    """Long-running task that fires due schedules. Cancel-safe.

    Two responsibilities every poll:
      1. Start any ``scheduled`` rows whose ``run_at`` has passed.
      2. Force-stop any ``running`` rows whose ``stop_at`` has passed (the
         inline stop watcher handles the happy path; this is the safety net
         for process restarts).
    """
    logger.info(f"Campaign scheduler started (poll every {_SCHEDULER_POLL_SEC:.0f}s).")
    _last_quiet_hours_state = None
    _last_watchdog_check = 0.0
    _last_backup_check = 0.0
    _last_inbound_heal_check = 0.0
    _last_vobiz_rec_sweep = 0.0
    _last_weekly_zip_check = 0.0
    _last_weekly_zip_date = ""

    while True:
        try:
            now = time.time()

            # ── Stuck inbound calls: finalize rows left at connected/ringing ──
            if now - _last_inbound_heal_check > 300:
                _last_inbound_heal_check = now
                try:
                    await _heal_stuck_incoming_calls(max_age_minutes=15)
                except Exception as _inc_heal_exc:
                    logger.warning("Inbound self-heal task failed: {}", _inc_heal_exc)

            # ── Missing Vobiz Application recordings (dashboard Play) ──
            if now - _last_vobiz_rec_sweep > 120:
                _last_vobiz_rec_sweep = now
                try:
                    await _sweep_pending_vobiz_recordings(hours=24, limit=25)
                except Exception as _rec_sweep_exc:
                    logger.warning("Vobiz recording sweep failed: {}", _rec_sweep_exc)

            # ── 2d. Super CEO Watchdog: Backups & Checkpoints (Every 1 hour) ──
            if now - _last_backup_check > 3600:
                _last_backup_check = now
                try:
                    await asyncio.to_thread(run_sqlite_backup)
                    await asyncio.to_thread(run_wal_checkpoint)
                except Exception as backup_exc:
                    logger.warning("Super CEO: backup task failed: {}", backup_exc)

            # ── 2f. Weekly audio ZIP archive (Monday 06:00-06:59 IST) ──
            if now - _last_weekly_zip_check > 600:
                _last_weekly_zip_check = now
                from datetime import datetime as _dt, timezone as _tz
                _ist_now = _dt.now(_tz.utc).astimezone()
                _day = _ist_now.strftime("%A").lower()
                _today_key = _ist_now.date().isoformat()
                if _day == "monday" and _ist_now.hour == 6 and _today_key != _last_weekly_zip_date:
                    _last_weekly_zip_date = _today_key
                    try:
                        from scripts.weekly_zip_archive import run_weekly_zip_archive
                        await asyncio.to_thread(run_weekly_zip_archive)
                    except Exception as zip_exc:
                        logger.warning("Weekly ZIP archive failed: {}", zip_exc)

            # ── 2e. Super CEO Watchdog: Telephony & Key Health (Every 10 minutes) ──
            if now - _last_watchdog_check > 600:
                _last_watchdog_check = now
                try:
                    # Check Gemini Key health
                    gemini_status = await check_gemini_health()
                    if gemini_status.get("status") != "ok":
                        logger.error("Super CEO: Gemini API key health check failed: {}", gemini_status)
                        from core.storage import set_campaign_globally_paused
                        await set_campaign_globally_paused(True)
                        send_super_ceo_alert(
                            "Gemini API Key Health Failure",
                            f"Gemini API key health check failed.\nStatus: {gemini_status}\nCampaigns have been paused."
                        )
                    
                    # Check Vobiz balances for active roles
                    for _role in ("sales_1",):
                        vobiz_status = await check_vobiz_health(_role)
                        if vobiz_status.get("status") == "unauthorized":
                            logger.error("Super CEO: Vobiz account unauthorized for role={}", _role)
                            from core.storage import set_campaign_globally_paused
                            await set_campaign_globally_paused(True)
                            send_super_ceo_alert(
                                "Vobiz Telephony Unauthorized",
                                f"Vobiz telephony account is unauthorized (401) for role={_role}.\nCampaigns have been paused."
                            )
                        elif vobiz_status.get("status") == "ok" and vobiz_status.get("balance_known"):
                            balance = float(vobiz_status["balance"])
                            if balance < 5.0:
                                logger.error("Super CEO: Vobiz balance low for role={}: {} credits", _role, balance)
                                from core.storage import set_campaign_globally_paused
                                await set_campaign_globally_paused(True)
                                send_super_ceo_alert(
                                    "Low Vobiz Telephony Balance",
                                    f"Vobiz account balance is low for role={_role}: {balance:.2f} credits (Threshold: 5.0).\nCampaigns have been paused."
                                )
                                
                        # Check Failure Rate
                        fail_status = await asyncio.to_thread(check_recent_failure_rate_sync, _role)
                        consec = fail_status["consecutive_failures"]
                        infra_fails = fail_status.get("infra_failures", consec)
                        total = fail_status["total_calls"]

                        if consec >= 5:
                            logger.error("Super CEO: Role {} hit {} consecutive infra failures", _role, consec)
                            from core.storage import set_campaign_globally_paused
                            await set_campaign_globally_paused(True)
                            send_super_ceo_alert(
                                "Consecutive Telephony Failures Detected",
                                f"Campaign role={_role} detected {consec} consecutive Vobiz/API failures.\nCampaigns have been paused.",
                            )
                        elif total >= 15 and infra_fails >= 10:
                            logger.error(
                                "Super CEO: Role {} hit high infra failure count: {}/{}",
                                _role, infra_fails, total,
                            )
                            from core.storage import set_campaign_globally_paused
                            await set_campaign_globally_paused(True)
                            send_super_ceo_alert(
                                "High Telephony Failure Rate Detected",
                                f"Campaign role={_role} hit {infra_fails} infra failures in 30m ({total} attempts).\nCampaigns have been paused.",
                            )
                except Exception as watchdog_exc:
                    logger.warning("Super CEO: watchdog run failed: {}", watchdog_exc)

            try:
                await promote_due_scheduled_callbacks(now)
            except Exception as e:
                logger.exception("Scheduler: promote_due_scheduled_callbacks failed")

            try:
                await _process_missed_inbound_callbacks(now)
            except Exception as e:
                logger.exception("Scheduler: missed inbound callbacks failed")

            # ── 1. Fire due schedules ──
            try:
                due = await due_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: due_schedules query failed")
                due = []

            for sched in due:
                schedule_id = int(sched.get("id") or 0)
                role = normalize_console_role(sched.get("role") or "sales_1")
                stop_at = sched.get("stop_at")
                if not schedule_id:
                    continue

                err = await _schedule_preflight(role)
                if err:
                    await mark_schedule_status(schedule_id, "failed", error=err)
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} skipped — {err}"
                    )
                    continue

                # Edge case: stop_at already passed before we even fired (clock
                # skew / very short window). Don't bother starting.
                if stop_at is not None and float(stop_at) <= now:
                    await mark_schedule_status(
                        schedule_id, "failed",
                        error="Stop time passed before the campaign could start.",
                    )
                    logger.warning(
                        f"Scheduled campaign id={schedule_id} role={role!r} "
                        f"stop_at already past — not starting."
                    )
                    continue

                logger.info(
                    f"Scheduled campaign id={schedule_id} role={role!r} firing now "
                    f"(name={sched.get('name')!r}, stop_at={stop_at})"
                )
                # Don't await — let it run in the background while we keep polling.
                task = asyncio.create_task(
                    _run_scheduled_campaign(role, schedule_id, stop_at=stop_at)
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

            # ── 2. Enforce stop windows (safety net for restarts) ──
            try:
                expired = await expired_running_schedules(now)
            except Exception as e:
                logger.exception("Scheduler: expired_running query failed")
                expired = []
            for sched in expired:
                await _enforce_window_stop(sched)

            # ── 2b. Dispatch WhatsApp polite follow-ups (12h after details sent) ──
            try:
                from core.storage import get_due_whatsapp_reminders, mark_whatsapp_reminder_sent
                from services.whatsapp_leads import send_whatsapp_text_message

                due_reminders = await get_due_whatsapp_reminders(
                    now - (int(settings.whatsapp_followup_hours) * 3600),
                )
                for lead in due_reminders:
                    lead_id = lead["id"]
                    lead_name = lead.get("name", "there") or "there"
                    phone = lead.get("phone")
                    if not phone:
                        continue
                    text = (
                        f"Hi {lead_name}, hope you had a chance to go through the Solitaire Unity "
                        "brochure and videos we shared. Would you be interested in knowing more about "
                        "the project, or scheduling a site visit? We'd be happy to help — just reply here on WhatsApp."
                    )
                    logger.info("Sending WhatsApp polite reminder to lead_id={} ({})", lead_id, phone)
                    try:
                        res = await send_whatsapp_text_message(phone, text)
                        await mark_whatsapp_reminder_sent(lead_id)
                        logger.info("WhatsApp reminder sent successfully for lead_id={} result={}", lead_id, res)
                    except Exception as wa_err:
                        logger.error("Failed to send WhatsApp reminder to lead_id={} : {}", lead_id, wa_err)
            except Exception as e:
                logger.exception("Scheduler: whatsapp reminders dispatch failed")

            # ── 2c. Hot-reload prompt & RAG from files (every 5 min) ──
            try:
                _last_rag_sync = getattr(_scheduler_loop, "_last_rag_sync", 0.0)
                if now - _last_rag_sync > 300:
                    from core.role_sandbox import sync_all_role_sandboxes_on_startup
                    sync_all_role_sandboxes_on_startup()
                    _scheduler_loop._last_rag_sync = now
                    logger.debug("Scheduler: synced prompt/RAG from files to DB state")
            except Exception as e:
                logger.warning("Scheduler: prompt/RAG sync failed: {}", e)

            # ── 3. Execute due scheduled callbacks (runs even in quiet hours / global pause) ──
            from core.storage import claim_next_immediate_callback
            for _role in ("sales_1",):
                try:
                    if role_has_active_vobiz_call(_role):
                        continue
                    if _role in _callback_tasks_in_flight:
                        continue
                    cb = await claim_next_immediate_callback(_role, now)
                    if cb is not None:
                        logger.info(
                            "Scheduler: executing callback id={} for {} ({})",
                            cb["id"],
                            cb.get("name", "?"),
                            cb.get("phone", "?"),
                        )
                        _callback_tasks_in_flight.add(_role)

                        async def _run_cb_and_clear_flag(r=_role, cb_=cb):
                            try:
                                await _execute_scheduled_callback(r, cb_)
                            finally:
                                _callback_tasks_in_flight.discard(r)

                        task = asyncio.create_task(_run_cb_and_clear_flag())
                        _background_tasks.add(task)
                        task.add_done_callback(_background_tasks.discard)
                except Exception as e:
                    logger.exception("Scheduler: immediate callback check failed for role={}", _role)

            # ── 4. Auto-start/stop campaigns per role calling window ──
            try:
                from core.state import _MANUALLY_STOPPED_ROLES
                from core.storage import is_campaign_globally_paused, set_campaign_globally_paused
                from core.campaign_hours import campaign_dial_window_active
                from core.state import get_campaign_config as _sched_cfg
                from core.orchestration_runtime import runtime_status as _orch_status

                # When autonomous orchestration is LIVE it owns ALL outbound dialing
                # (P1-P9) and the source-blind legacy worker must never run.
                _orch_live = _orch_status()["mode"] == "live"

                _roles_any_can_dial = False
                for _role in ("sales_1",):
                    try:
                        _rcfg = _sched_cfg(_role) or {}
                        _can_dial = campaign_dial_window_active(_rcfg)
                        _active = _CAMPAIGN_TASKS.get(_role)
                        _counts = await get_lead_counts(_role)
                        _pending = int(_counts.get("pending", 0) or 0)

                        # Orchestration-live guard: cancel any stale legacy worker and
                        # never auto-start one. Also unpause-lease dialing leads back to
                        # pending so the orchestrator (which keys off workflow_jobs) can
                        # claim them.
                        if _orch_live:
                            if _active and not _active.done():
                                logger.warning(
                                    "Scheduler: orchestration live — cancelling legacy campaign worker for role={}",
                                    _role,
                                )
                                _active.cancel()
                                _CAMPAIGN_TASKS[_role] = None
                            await set_campaign_want_running(_role, False)
                            await release_orphaned_dialing_leads(
                                _role,
                                to_status="pending",
                                error="Orchestration owns outbound dialing; legacy worker disabled.",
                            )
                            continue

                        if _can_dial:
                            _roles_any_can_dial = True
                            if _role not in _MANUALLY_STOPPED_ROLES:
                                if not _active or _active.done():
                                    if _pending > 0:
                                        logger.info(
                                            "Scheduler: auto-starting campaign for role={} as its calling window is active (pending={}).",
                                            _role, _pending,
                                        )
                                        await set_campaign_want_running(_role, True)
                                        _CAMPAIGN_TASKS[_role] = asyncio.create_task(_campaign_worker_role(_role))
                        else:
                            # Role window closed → stop its campaign + release dialing leads
                            if _active and not _active.done():
                                logger.info(
                                    "Scheduler: stopping campaign for role={} (outside its calling window).",
                                    _role,
                                )
                                _active.cancel()
                                _CAMPAIGN_TASKS[_role] = None
                            await set_campaign_want_running(_role, False)
                            await release_orphaned_dialing_leads(
                                _role,
                                to_status="pending",
                                error=f"Campaign paused: outside calling window for {_role}.",
                            )
                    except Exception as _role_err:
                        logger.debug("Scheduler: per-role window check skipped for {}: {}", _role, _role_err)

                # Stall watchdog: restart a live-but-blocked worker if it goes silent while
                # leads remain and its window is still active.
                _STALL_SEC = float(os.getenv("CAMPAIGN_STALL_WATCHDOG_SEC", "600"))
                for _role in ("sales_1",):
                    if _orch_live:
                        continue
                    if _role in _MANUALLY_STOPPED_ROLES:
                        continue
                    try:
                        if not campaign_dial_window_active(_sched_cfg(_role) or {}):
                            continue
                    except Exception:
                        continue
                    _active = _CAMPAIGN_TASKS.get(_role)
                    if not _active or _active.done():
                        continue
                    _last = _LAST_WORKER_ACTIVITY.get(_role, 0.0)
                    if (now - _last) <= _STALL_SEC:
                        continue
                    _counts = await get_lead_counts(_role)
                    _pending = int(_counts.get("pending", 0) or 0)
                    if _pending <= 0:
                        continue
                    logger.warning(
                        "Scheduler watchdog: role={} worker stalled (no activity {}s, pending={}) - restarting.",
                        _role, int(now - _last), _pending,
                    )
                    _active.cancel()
                    try:
                        await asyncio.wait_for(_active, timeout=5.0)
                    except BaseException:
                        pass
                    await release_orphaned_dialing_leads(
                        _role,
                        to_status="pending",
                        error="Watchdog restart: detected stalled worker (no call activity).",
                    )
                    await set_campaign_want_running(_role, True)
                    _CAMPAIGN_TASKS[_role] = asyncio.create_task(_campaign_worker_role(_role))

                # Only pause globally when every configured role is outside its window.
                # Orchestration-live is never gated by the legacy campaign pause flag.
                if _roles_any_can_dial or _orch_live:
                    await set_campaign_globally_paused(False)
                else:
                    _quiet_start = is_campaign_quiet_hours()
                    if _quiet_start and not _MANUALLY_STOPPED_ROLES:
                        await set_campaign_globally_paused(True)
            except Exception as e:
                logger.exception("Scheduler: campaign auto-start checks failed")

        except asyncio.CancelledError:
            logger.info("Campaign scheduler cancelled.")
            raise
        except Exception as e:
            logger.exception("Scheduler loop iteration error")

        # Sleep in slices so cancellation is responsive even if poll interval is large.
        slept = 0.0
        while slept < _SCHEDULER_POLL_SEC:
            await asyncio.sleep(min(1.0, _SCHEDULER_POLL_SEC - slept))
            slept += 1.0
