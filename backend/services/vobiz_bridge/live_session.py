"""Live WebSocket session: Vobiz media ↔ Gemini Live."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any, Optional

import websockets as ws_client
from fastapi import WebSocket
from loguru import logger

from config import settings
from core.state import release_vobiz_call_slot
from services.conversation_log import (
    append_artifact,
    append_session_meta,
    append_turn,
    new_session_id,
)

from .audio import (
    drain_gemini_24k_to_vobiz_16k,
    load_background_audio,
    mix_voice_and_background_tick,
    noise_suppress_inbound_pcm,
    pcm_resample,
    pcm_s16le_has_voice,
    pcm_s16le_rms,
    pop_l16_chunk,
    coerce_pcm_sr_pair,
    prepare_scripted_greeting_pcm,
    send_play_audio,
    vobiz_frame_bytes_16k,
    vobiz_inbound_pcm_to_le,
)
from services.call_recording import CallRecorder
from .constants import VOBIZ_SR
from core.gemini_auth import gemini_auth_headers, get_gemini_api_key
from .gemini_protocol import (
    GEMINI_LIVE_URL_TMPL,
    gemini_live_ws_url,
    build_live_setup,
    gemini_send_first_turn_phrase_nudge,
    gemini_send_live_opening_turn_nudge,
    gemini_send_live_rag,
    gemini_send_pcm_silence_kick,
    gemini_send_post_pcm_name_verify_nudge,
    gemini_send_post_name_confirm_pitch_nudge,
    gemini_send_phase3_pitch_nudge,
    gemini_send_dev_mode_nudge,
    gemini_send_rag_question_turn,
    gemini_send_respond_now_nudge,
    gemini_send_voicemail_beep_message_nudge,
    gemini_send_voicemail_screening_nudge,
)
from .turn_taking_addon import (
    apply_anti_loop_closing_addon,
    apply_live_voice_turn_addon,
    apply_site_visit_confirmation_addon,
)
from .voicemail import (
    classify_callee_from_stt,
    classify_voicemail_stt,
    looks_like_live_human_after_screening,
)
from .paths import backend_dir
from .session_state import VobizSessionState
from .telephony import terminate_call, vobiz_send_clear_audio
from .vobiz_client import extract_vobiz_start_numbers

# Call quality metrics collector
from services import call_quality_metrics as cqm

# Track fire-and-forget background tasks so they aren't garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _normalize_phone_digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _dev_mode_whitelist() -> set[str]:
    raw = os.getenv("DEV_MODE_PHONES", "") or ""
    return {_normalize_phone_digits(p) for p in raw.split(",") if p.strip()}


def _dev_mode_codeword() -> str:
    return (os.getenv("DEV_MODE_CODEWORD") or "panther chinmay").strip().lower()


def _phone_on_dev_whitelist(phone: str) -> bool:
    digits = _normalize_phone_digits(phone)
    if not digits:
        return False
    for allowed in _dev_mode_whitelist():
        if not allowed:
            continue
        if digits == allowed or digits.endswith(allowed) or allowed.endswith(digits):
            return True
    return False


def _stt_matches_dev_codeword(stt: str) -> bool:
    codeword = _dev_mode_codeword()
    if not codeword:
        return False
    t = (stt or "").lower()
    if codeword in t:
        return True
    # STT may split words: "panther ... chinmay" in one utterance
    parts = [p for p in codeword.split() if p]
    if len(parts) >= 2 and all(p in t for p in parts):
        return True
    return False


def _stt_mentions_dev_without_codeword(stt: str) -> bool:
    t = (stt or "").lower()
    if not t or _stt_matches_dev_codeword(stt):
        return False
    if re.search(r"\bdeveloper\s+mode\b", t):
        return True
    if re.search(r"\bpanther\b", t):
        return True
    return False


def _stt_triggers_phase3(stt: str) -> bool:
    t = (stt or "").lower()
    patterns = (
        r"\balready\s+(bought|booked|visited|seen)\b",
        r"\b(bought|booked)\s+(a\s+)?(villa|flat|unit|property)\b",
        r"\bvisited\s+(the\s+)?(site|project)\b",
        r"\bsite\s+(nodidde|hogidde|hogiddini)\b",
        r"\bbook\s+(maadidini|aadide|aadidini)\b",
        r"\balready\s+been\s+to\b",
    )
    return any(re.search(p, t) for p in patterns)


def _stt_asks_ai_identity(stt: str) -> bool:
    """True when caller asks if the agent is AI, a bot, or automated."""
    t = (stt or "").lower()
    if not t:
        return False
    patterns = (
        r"\bare you (an? )?(ai|a\.i\.|bot|robot|automated|computer|machine|real person|human)\b",
        r"\byou('?re| are) (an? )?(ai|a\.i\.|bot|robot|automated)\b",
        r"\bis this (ai|a\.i\.|a bot|automated|a robot)\b",
        r"\bam i speaking (to|with) (an? )?(ai|a\.i\.|bot|robot)\b",
        r"\b(real person|human being|actual person)\b",
        r"\bchat\s*gpt\b",
        r"\bvoice\s*bot\b",
    )
    return any(re.search(p, t) for p in patterns)


def _is_cp_campaign_role(role: str) -> bool:
    # CP (channel-partner commission) flow removed — home-buyer persona only.
    return False


def _stt_asks_units_inventory(stt: str) -> bool:
    t = (stt or "").lower()
    if not t:
        return False
    patterns = (
        r"\bsold\s*out\b",
        r"\bhow\s+many\s+(?:villas?|units?|homes?)\s+(?:sold|left|available|remaining)",
        r"\bunits?\s+(?:left|available|remaining|sold)",
        r"\bvillas?\s+(?:left|available|remaining|sold)",
        r"\bhow\s+many\s+(?:have\s+)?(?:been\s+)?sold",
        r"\bavailability\b",
        r"\bpending\s+units?\b",
    )
    return any(re.search(p, t) for p in patterns)


def _stt_asks_pricing_recap(stt: str) -> bool:
    t = (stt or "").lower()
    if not t:
        return False
    return bool(
        re.search(
            r"\b(price|pricing|cost|crore|budget|how\s+much|villa\s+cost)\b",
            t,
        )
    )


def _user_asked_factual_question(stt: str) -> bool:
    """True when caller asks price/units/features — never treat as CTA-ready."""
    t = (stt or "").strip()
    if not t:
        return False
    if _stt_asks_pricing_recap(t) or _stt_asks_units_inventory(t):
        return True
    low = t.lower()
    return bool(
        re.search(
            r"\b(elevator|eon|legrand|pool|pergola|commission|voucher|"
            r"walk[\s-]?in|feature|amenit|what\s+(?:is|are|does|else)|"
            r"explain|tell\s+me\s+(?:about|the|what)|what\s+else|namage|artha)\b",
            low,
        )
    )


def _stt_complains_about_refusal(stt: str) -> bool:
    t = (stt or "").lower()
    if not t:
        return False
    patterns = (
        r"language\s+model",
        r"large\s+language",
        r"don'?t\s+say",
        r"do\s+not\s+say",
        r"why\s+you\s+(?:are\s+)?say",
        r"can'?t\s+help",
        r"cannot\s+help",
        r"stop\s+saying",
    )
    return any(re.search(p, t) for p in patterns)


def _is_explicit_refusal_leak(text: str) -> bool:
    """True only for clear LLM safety refusal phrases — not generic text."""
    low = (text or "").lower()
    if not low.strip():
        return False
    explicit = (
        "i'm just a language model",
        "i am just a language model",
        "im just a language",
        "just a language model",
        "large language model",
        "language model and can't",
        "language model and cannot",
        "i can't help with that",
        "i cannot help with that",
        "as a language model",
        "as an ai",
        "i'm an ai",
        "i am an ai",
    )
    return any(p in low for p in explicit)


def _is_partial_refusal_leak(text: str) -> bool:
    """Catch refusal phrases early in streaming before the full sentence forms."""
    low = (text or "").lower()
    if not low.strip():
        return False
    if _is_explicit_refusal_leak(text):
        return True
    partial = (
        "can't help",
        "cannot help",
        "language model",
        "large language",
        "as an ai",
        "as a language",
        "i'm an ai",
        "i am an ai",
        "i'm a large",
        "i am a large",
        "just a language",
    )
    return any(p in low for p in partial)


def _stt_is_cp_complaint(stt: str) -> bool:
    """Caller upset about weird agent phrasing — not an AI-identity question."""
    t = (stt or "").lower()
    if not t:
        return False
    patterns = (
        r"why (are you|do you) say",
        r"what (are you|did you) say",
        r"why you say",
        r"can't help",
        r"cannot help",
        r"language model",
        r"personal assistant",
        r"you('?re| are) (weird|wrong|confused|repeating)",
        r"stop (saying|repeating)",
        r"what are you talking",
        r"why are you (?:over )?laugh",
        r"over laugh",
        r"\bmadam\b.*\?",
    )
    return any(re.search(p, t) for p in patterns)


def _looks_like_cta_fragment(text: str) -> bool:
    """Orphan CTA tail like 'with you?' without full Account Manager sentence."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if re.search(
        r"account manager|cp account|manager to connect|connect with you",
        low,
    ):
        return True
    return bool(re.search(r"(?:^|[.!?]\s*)(?:with you\?|connect with you\?)\s*$", low))


def _looks_like_account_manager_confirmation(text: str) -> bool:
    """A confirmed handoff is not another Account Manager CTA question."""
    low = (text or "").strip().lower()
    return bool(
        re.search(
            r"(?:our|the)\s+(?:cp\s+)?account manager\s+(?:will|shall|is going to)\s+"
            r"(?:connect|reach out|get in touch)",
            low,
        )
    )


def _strip_account_manager_cta(text: str) -> str:
    """Remove Account Manager CTA sentences from assistant transcript."""
    t = (text or "").strip()
    if not t:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", t)
    kept = [
        p
        for p in parts
        if p
        and not re.search(
            r"account manager|cp account|manager to connect|connect with you|^(?:with you\?|connect with you\?)$",
            p,
            re.I,
        )
    ]
    out = " ".join(kept).strip() if kept else ""
    return re.sub(r"[.\s]*(?:with you\?|connect with you\?)\s*$", "", out, flags=re.I).strip()


def _cp_script_milestone_count(text: str) -> int:
    """How many CP script steps appear in one assistant turn (intro/features/commission/CTA)."""
    low = (text or "").lower()
    if not low:
        return 0
    n = 0
    if re.search(r"premium|gated community|solitaire unity|ready.to.move", low):
        n += 1
    if re.search(r"elevator|legrand|swimming pool|pergola|sky party", low):
        n += 1
    if re.search(r"\b(3%|12[ ]?lakh|walk[\s-]?in|voucher|50,?000)\b", low):
        n += 1
    if _looks_like_cta_fragment(low):
        n += 1
    return n


def _looks_like_cp_script_rush(text: str) -> bool:
    """Multiple script steps in one breath — must stop and listen.
    
    NOTE: Turn 3 (features) legitimately has 3 sentences. Only flag
    when we see 2+ distinct script milestones together (e.g. intro+features,
    or features+commission), not just sentence count alone.
    """
    t = (text or "").strip()
    if not t:
        return False
    # 2+ distinct script sections = definite rush
    if _cp_script_milestone_count(t) >= 3:
        return True
    sentences = [p for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    # 5+ sentences AND 2+ questions = monologue dump
    return len(sentences) >= 5 and t.count("?") >= 2


def _truncate_cp_monologue(text: str) -> str:
    """Keep only the first script beat when the model rushes multiple turns.
    
    Important: Turn 3 (features) legitimately spans 3-4 sentences.
    We only truncate when MULTIPLE script milestones appear together.
    """
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    # Only cut mid-sentence if 2+ milestones detected AND the cut point is after Turn 2 intro
    milestone_count = _cp_script_milestone_count(t)
    cut_patterns = (
        # Cut before Turn 4 commission if Turn 2/3 already present
        r"\bupon successful sale\b",
        r"\b3% commission\b",
        r"\bchannel partner commission\b",
        r"\bwould you like to know how the commission\b",
        # Cut before Turn 5 CTA
        r"\baccount manager\b",
    )
    # Only apply truncation if we detected a genuine multi-step rush
    if milestone_count >= 2:
        earliest = len(t)
        for pat in cut_patterns:
            m = re.search(pat, low)
            if m and m.start() > 20:
                earliest = min(earliest, m.start())
        if earliest < len(t):
            t = t[:earliest].strip()
    sentences = [p for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    # Allow up to 5 sentences (Turn 3 features needs 3-4, intro needs 2)
    if len(sentences) > 5:
        t = " ".join(sentences[:5]).strip()
    return _strip_account_manager_cta(t)


def _stt_is_audio_checkin(stt: str) -> bool:
    """Hello / can't hear you — presence check, not a script restart."""
    t = (stt or "").lower()
    if not t:
        return False
    if re.search(r"can'?t\s+hear|cannot\s+hear|not\s+hear|audible|voice\s+break", t):
        return True
    if re.search(
        r"\b(hello|hi|hey|haan|han|ji|are you there|can you hear|you there)\b",
        t,
    ):
        return not re.search(
            r"\b(tell me|go ahead|price|cost|commission|sold|units?|villa)\b",
            t,
        )
    return False


def _append_dev_mode_instruction(
    *,
    instruction: str,
    call_id: str,
    role: str,
    phone: str,
) -> None:
    text = (instruction or "").strip()
    if not text:
        return
    try:
        from datetime import datetime, timezone

        log_path = backend_dir / "data" / "dev_mode_instructions.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "call_id": call_id,
            "role": role,
            "phone": phone,
            "instruction": text,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("Dev mode instruction saved: {!r}", text[:120])
    except Exception as exc:
        logger.warning("Failed to save dev mode instruction: {}", exc)


def _resolve_authoritative_lead_name(camp_row: Optional[dict]) -> str:
    """First addressable name from camp/lead row; empty if missing or placeholder."""
    if not isinstance(camp_row, dict):
        return ""
    from core.opening_line import looks_like_real_name

    raw_nm = str(camp_row.get("name", "") or "").strip()
    if not looks_like_real_name(raw_nm):
        return ""
    try:
        from core.campaign_payload import addressable_name as _addr_auth

        return _addr_auth(raw_nm)
    except Exception:
        return raw_nm.split()[0].capitalize() if raw_nm.split() else raw_nm


async def _build_continuity_memory_block(
    camp_row: Optional[dict],
    lead_history: Optional[dict],
    authoritative_name: str = "",
) -> str:
    """Load the stored ``lead_memory`` facts for a resolved lead and render them
    as a "[CONTINUITY MEMORY]" system-prompt section.

    Resolves the lead id from the campaign row first, then the looked-up lead
    row. Returns "" when there is no lead or no stored memory (callers skip).
    """
    from datetime import datetime

    _lead_id: Optional[int] = None
    try:
        if isinstance(camp_row, dict):
            _lead_id = int(camp_row.get("_lead_id") or camp_row.get("id") or 0) or None
        if _lead_id is None and lead_history and lead_history.get("id"):
            try:
                _lead_id = int(lead_history["id"])
            except (TypeError, ValueError):
                _lead_id = None
    except (TypeError, ValueError):
        _lead_id = None
    if _lead_id is None:
        return ""

    try:
        from core.storage import get_lead_memory

        _mem = await get_lead_memory(_lead_id)
    except Exception:
        return ""
    if not _mem:
        return ""

    _facts: dict = {}
    try:
        _facts = json.loads(_mem.get("facts_json") or "{}")
        if not isinstance(_facts, dict):
            _facts = {}
    except Exception:
        _facts = {}
    _m_summary = str(_mem.get("summary") or "").strip()
    _m_last = _mem.get("last_interaction_at")
    _m_last_str = ""
    if _m_last:
        try:
            _m_last_dt = datetime.fromtimestamp(float(_m_last))
            _m_last_str = _m_last_dt.strftime("%B %d, %Y %I:%M %p")
        except Exception:
            _m_last_str = ""

    _cont_lines = [
        "\n[CONTINUITY MEMORY — STORED FACTS FROM PREVIOUS CONVERSATIONS]",
        "Use these stored facts about this person to keep the call continuous — "
        "reference what was already discussed and do NOT re-ask questions already answered.",
    ]
    if authoritative_name:
        _cont_lines.append(
            f"[CONTINUITY HEADER] You are following up with {authoritative_name}. "
            "Reference this context immediately so the call feels continuous."
        )
    _fact_label = {
        "budget": "Stated budget",
        "preferred_budget": "Stated budget",
        "preferred_location": "Preferred location",
        "location": "Preferred location",
        "property_type": "Property type",
        "timeline": "Timeline",
        "email_address": "Email",
        "loan_need": "Loan need",
        "decision_maker": "Decision maker",
        "family": "Family",
        "occupation": "Occupation",
        "purpose": "Purpose",
        "objections": "Objections",
        "site_visit_agreed": "Site visit agreed",
        "callback_requested_at": "Requested callback",
        "last_disposition": "Previous outcome",
    }
    _ordered_keys = (
        "budget", "preferred_budget", "preferred_location", "location",
        "property_type", "timeline", "email_address", "loan_need",
        "decision_maker", "family", "occupation", "purpose", "objections",
        "site_visit_agreed", "callback_requested_at", "last_disposition",
    )
    for _k in _ordered_keys:
        _v = _facts.get(_k)
        if _v in (None, "", [], {}):
            continue
        _cont_lines.append(f"- {_fact_label.get(_k, _k.replace('_', ' ').title())}: {str(_v)[:400]}")
    for _k, _v in _facts.items():
        if _k in _ordered_keys or _v in (None, "", [], {}):
            continue
        _cont_lines.append(f"- {str(_k).replace('_', ' ').title()}: {str(_v)[:300]}")
    if _m_summary:
        _cont_lines.append(f"- Prior conversation summary: {_m_summary[:1000]}")
    if _m_last_str:
        _cont_lines.append(f"- Last interaction: {_m_last_str}")
    if len(_cont_lines) <= 2:
        return ""
    return "\n".join(_cont_lines) + "\n\n"


def _extract_spoken_name_from_stt(stt, *, fallback=""):
    """Best-effort capture of the caller's spoken name from STT, used only when
    there is no pre-known (authoritative) name and the agent just asked the caller
    for their name.  Without a pre-known name the old code could never 'confirm'
    the caller, so the project pitch nudge never fired -> dead silence right after
    the caller answered.  This captures and returns a short plausible name."""
    import re as _re
    if not stt:
        return fallback or ""
    t = _re.sub(r"[^A-Za-z' .\-]", " ", stt)
    t = _re.sub(r"\s+", " ", t).strip()
    t = (t.replace(" my name is ", " ").replace(" i am ", " ")
           .replace(" this is ", " ").replace(" speaking ", " ")
           .replace(" yes ", " ").replace(" yeah ", " "))
    stop = {"am","i","me","my","the","a","an","and","or","its","it","is","for","to",
            "of","on","hi","hello","hey","okay","ok","sure","please","name","call",
            "are","you","your","yes","yeah","yep","right","correct","speaking",
            "this","that","sir","madam"}
    words = [w for w in t.split() if w.lower() not in stop]
    if not words:
        return fallback or ""
    return " ".join(words[:2])[:48].strip() or (fallback or "")


def _lead_name_speech_guard(authoritative_name: str) -> str:
    """Prompt line that forbids company/agent names without blocking the lead's first name."""
    n = (authoritative_name or "").strip()
    if not n:
        return ""
    return (
        f"CRITICAL: Lead first name on file is {n}. Use ONLY '{n}' in name verification. "
        "Do NOT say the company brand 'Technopolis Constructions', agent names (Vernika), "
        "or STT misheard names unless the caller explicitly corrects you.\n"
    )


def _build_scripted_first_turn_phrase(
    *,
    greeting_name: str,
    is_callback: bool,
    is_retry: bool,
) -> str:
    if is_callback:
        if greeting_name:
            return (
                f"Hi, am I speaking with {greeting_name}? I am calling you back as scheduled."
            )
        return "Hi, can I just know your name? I am calling you back as scheduled."
    if is_retry:
        if greeting_name:
            return (
                f"Hi, am I speaking with {greeting_name}? Actually, I tried to reach you "
                "earlier but the call didn't connect, so I'm calling back."
            )
        return (
            "Hi, can I just know your name? Actually, I tried to reach you earlier but "
            "the call didn't connect, so I'm calling back."
        )
    if greeting_name:
        return f"Am I speaking with {greeting_name}?"
    return "May I know your name, please?"


async def _await_queue_audio_bytes(
    queue: bytearray,
    *,
    min_bytes: int = 8000,
    timeout_sec: float = 2.0,
    poll_sec: float = 0.08,
) -> int:
    """Poll until ``queue`` holds at least ``min_bytes`` or timeout."""
    deadline = time.perf_counter() + timeout_sec
    while time.perf_counter() < deadline:
        if len(queue) >= min_bytes:
            return len(queue)
        await asyncio.sleep(poll_sec)
    return len(queue)


def _personalize_prompt_for_active_call(prompt: str, lead_first: str, pcm_greeting_will_play: bool = True) -> str:
    """Scrub example names and append highest-priority per-call name lock."""
    if not (prompt or "").strip() or not (lead_first or "").strip():
        return prompt
    import re

    out = re.sub(r"Mr\./Miss\s*\[Name\]", lead_first, prompt, flags=re.IGNORECASE)
    out = re.sub(r"Mr\./Ms\.\s*\[Name\]", lead_first, out, flags=re.IGNORECASE)
    out = re.sub(r"\[Name\]", lead_first, out, flags=re.IGNORECASE)
    out = re.sub(r"\[Lead Name\]", lead_first, out, flags=re.IGNORECASE)
    out = re.sub(r"\bRam\b", lead_first, out, flags=re.IGNORECASE)
    out = re.sub(r"\bRamesh\b", lead_first, out, flags=re.IGNORECASE)
    
    if pcm_greeting_will_play:
        greeting_instruction = (
            f"Lead first name on THIS call: {lead_first}. "
            "Name verification is handled by the live nudge — do NOT re-ask unless unclear.\n"
        )
    else:
        greeting_instruction = (
            f'Start the call by saying your opening line and then immediately ask ONLY: '
            f'"Am I speaking with {lead_first}?"\n'
        )

    out += (
        f"\n\n[ACTIVE CALL — FINAL OVERRIDE — HIGHEST PRIORITY]\n"
        + greeting_instruction
        + "Do NOT repeat Hi/Hello, company intro, or project pitch before name is confirmed. "
        "Do NOT use any other name.\n"
    )
    return out




class DeferredGeminiConnection:
    """A connection wrapper that defers the actual connection to a background task

    so that the outbound greeting playout loop can start immediately on pickup.
    """
    def __init__(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        self.real_ws = None
        self.conn_task = None
        self.connected_event = asyncio.Event()
        self.conn_error = None

    async def start_connect(self):
        try:
            logger.info("DeferredGeminiConnection: starting background connection to {}", self.url[:60])
            self._ctx = ws_client.connect(self.url, **self.kwargs)
            self.real_ws = await self._ctx.__aenter__()
            self.connected_event.set()
            logger.info("DeferredGeminiConnection: background connection established successfully.")
            # #region agent log
            try:
                from debug_agent_log import agent_debug

                agent_debug(
                    "A",
                    "live_session.py:DeferredGeminiConnection",
                    "gemini_ws_connected",
                    {"url_prefix": self.url[:48]},
                )
            except Exception:
                pass
            # #endregion
        except Exception as e:
            logger.exception("DeferredGeminiConnection: background connection failed")
            self.conn_error = e
            self.connected_event.set()
            # #region agent log
            try:
                from debug_agent_log import agent_debug

                agent_debug(
                    "A",
                    "live_session.py:DeferredGeminiConnection",
                    "gemini_ws_failed",
                    {"err": str(e)[:160]},
                )
            except Exception:
                pass
            # #endregion

    async def __aenter__(self):
        self.conn_task = asyncio.create_task(self.start_connect())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn_task and not self.conn_task.done():
            self.conn_task.cancel()
        if self.real_ws:
            await self._ctx.__aexit__(exc_type, exc_val, exc_tb)

    async def send(self, data):
        await self.connected_event.wait()
        if self.conn_error:
            raise self.conn_error
        if not self.real_ws:
            raise RuntimeError("Gemini connection is not established")
        await self.real_ws.send(data)

    async def recv(self):
        await self.connected_event.wait()
        if self.conn_error:
            raise self.conn_error
        if not self.real_ws:
            raise RuntimeError("Gemini connection is not established")
        return await self.real_ws.recv()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.connected_event.wait()
        if self.conn_error:
            raise self.conn_error
        if not self.real_ws:
            raise StopAsyncIteration
        try:
            return await self.real_ws.recv()
        except ws_client.ConnectionClosed:
            raise StopAsyncIteration


async def handle_vobiz_ws_live(
    ws: WebSocket,
    camp_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    manual_role: Optional[str] = None,
    lead_name: Optional[str] = None,
) -> None:
    """Bridge Vobiz <-> Gemini Live (native audio). Low-latency path.

    Resolves configuration from one of several sources, in priority order:
      1. ``_CAMPAIGN_DATA[camp_id]`` — outbound campaign call (we know the lead).
      2. ``agent_id`` — sandbox / factory agent (recovered from DB).
      3. ``camp_id`` starting with ``manual_{role}`` — Make a Call / manual dial.
      4. ``manual_role`` query param — same routing as (3) when ``camp_id`` is
         missing or stripped by the carrier; pair with ``camp_id=manual_*`` when possible.
    """
    from core.state import _CAMPAIGN_DATA, get_state, _get_role_path, normalize_console_role
    from core.utils import _build_opening_line
    from .constants import VOBIZ_SR  # bind early for greeting playout + nested pump tasks

    await ws.accept()
    live_log_id: str = ""
    logger.info(
        "Vobiz WebSocket accepted for camp={} agent={} manual_role={}",
        camp_id, agent_id, manual_role,
    )

    if camp_id and str(camp_id).startswith("incoming_"):
        try:
            from services.campaign_live import set_active_campaign_call, clear_transcript_session

            set_active_campaign_call(camp_id)
            clear_transcript_session(camp_id)
        except Exception as _inc_live_err:
            logger.warning("Incoming live transcript setup failed: {}", _inc_live_err)

    if camp_id:
        try:
            from core.camp_session import hydrate_camp_session

            await hydrate_camp_session(camp_id)
            if camp_id not in _CAMPAIGN_DATA:
                await hydrate_camp_session(camp_id)
            if camp_id in _CAMPAIGN_DATA:
                _lead_nm = str(_CAMPAIGN_DATA[camp_id].get("name") or "").strip()
                _auth_nm = _resolve_authoritative_lead_name(_CAMPAIGN_DATA[camp_id])
                logger.info(
                    "Camp session hydrated at WS accept: camp_id={} lead_name={!r} auth_name={!r}",
                    camp_id,
                    _lead_nm,
                    _auth_nm,
                )
        except Exception as exc:
            logger.warning("Camp session hydrate at WS connect failed for camp_id={}: {}", camp_id, exc)

    # Single ingress queue so we can play scripted PCM before opening Gemini Live while
    # still buffering carrier events (see drain_scripted_opening_before_live_connect).
    _vobiz_incoming: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=4096)

    async def _vobiz_ws_reader_task() -> None:
        try:
            while True:
                t = await ws.receive_text()
                await _vobiz_incoming.put(t)
        except Exception as exc:
            logger.warning("Vobiz WS reader stopped: {}", exc)
            # #region agent log
            try:
                from debug_agent_log import agent_debug

                agent_debug(
                    "E",
                    "live_session.py:_vobiz_ws_reader_task",
                    "vobiz_ws_closed",
                    {"error": str(exc)[:120]},
                )
            except Exception:
                pass
            # #endregion
        finally:
            try:
                await _vobiz_incoming.put(None)
            except Exception as exc:
                logger.debug("Vobiz incoming queue close failed: {}", exc)

    _task = asyncio.create_task(_vobiz_ws_reader_task())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)

    # 1. Resolve Configuration
    data = {}
    system_prompt = ""
    voice = settings.gemini_live_voice
    opening_line = settings.vobiz_opening_line_default
    role = "sales_1"
    log_dir = str(_get_role_path("sales_1", "logs"))
    api_key = get_gemini_api_key()
    model = settings.gemini_live_model

    if camp_id and camp_id in _CAMPAIGN_DATA:
        data = _CAMPAIGN_DATA[camp_id]
        role = data.get("_role", "sales_1")
        log_dir = str(_get_role_path(role, "logs"))
        
        # Priority: Sandbox Prompt > Role Prompt > Default
        system_prompt = data.get("_sandbox_prompt")
        voice = data.get("_sandbox_voice", voice)
        opening_line = _build_opening_line(data, role)
    elif camp_id and str(camp_id).startswith("manual_"):
        # Make a Call / manual dial: camp_id may be ``manual_{role}`` or
        # ``manual_{role}_{token}`` when each attempt gets a unique id.
        from core.state import parse_manual_camp_role_suffix

        suffix = str(camp_id)[len("manual_") :]
        role, _attempt = parse_manual_camp_role_suffix(suffix)
        log_dir = str(_get_role_path(role, "logs"))
        opening_line = _build_opening_line({"name": ""}, role)
        logger.info("Manual call leg routed to role={} (camp_id={})", role, camp_id)
    elif manual_role:
        # Telco / proxy may drop custom camp_id; answer URL can pass manual_role=... as backup.
        role = normalize_console_role(manual_role)
        log_dir = str(_get_role_path(role, "logs"))
        opening_line = _build_opening_line({"name": ""}, role)
        logger.info("Manual call leg routed to role={} (manual_role query param)", role)
    elif camp_id and str(camp_id).startswith("sched_cb_"):
        # Scheduled callback: format is sched_cb_{role}_{id}_{hash}
        suffix = str(camp_id)[len("sched_cb_"):]
        parts = suffix.split("_")
        if len(parts) >= 3:
            candidate = "_".join(parts[:-2])
            role = normalize_console_role(candidate)
            log_dir = str(_get_role_path(role, "logs"))
            opening_line = _build_opening_line({"name": ""}, role)
            logger.info("Scheduled callback leg routed to role={} (camp_id={})", role, camp_id)
    elif camp_id and str(camp_id).startswith("incoming_"):
        # Incoming (customer call-back): format is incoming_{role}_from_{phone_digits}
        suffix = str(camp_id)[len("incoming_"):]
        parts = suffix.split("_from_")
        if len(parts) >= 1:
            candidate = parts[0]
            role = normalize_console_role(candidate)
            log_dir = str(_get_role_path(role, "logs"))
            opening_line = _build_opening_line({"name": lead_name or ""}, role)
            logger.info("Incoming call leg routed to role={} (camp_id={}) lead={}", role, camp_id, lead_name or "")

    # Per-role voice override (single Technopolis console role).
    if role == "sales_1" and settings.gemini_live_voice_sales_1:
        voice = settings.gemini_live_voice_sales_1

    # Restart Resilience: If memory was lost, recover from agent_id or camp_id string
    if not system_prompt and (agent_id or (camp_id and camp_id.startswith("sandbox-"))):
        if not agent_id and camp_id:
            parts = camp_id.split("-")
            if len(parts) >= 2: agent_id = parts[1]
        
        if agent_id:
            from services.sandbox_manager import get_agent
            agent = get_agent(agent_id)
            if agent:
                role = "factory"
                system_prompt = agent.get("prompt", "")
                voice = agent.get("voice", voice)
                # Opening line for sandbox is usually handled by the trigger, but we can default here
                logger.info(f"Vobiz WS: Recovered sandbox agent {agent_id} from database")

    if not system_prompt:
        role_config = get_state(role)
        from prompts.role_prompts import build_role_system_prompt

        rag_mode = (settings.rag_mode or "chunk").strip().lower()
        embed_kb = bool(
            settings.rag_enabled
            and (
                settings.rag_embed_in_system_prompt
                or getattr(settings, "rag_embed_full_kb", False)
            )
            and rag_mode == "embed"
        )
        system_prompt = build_role_system_prompt(role, role_config, embed_rag=embed_kb)

    # Extract agent name from prompt file for dynamic persona anchoring
    from prompts.role_prompts import extract_agent_name

    agent_name = extract_agent_name(role) if role in ("sales_1",) else ""

    # Resolve caller/callee phone number for history lookup & WhatsApp dedup early
    phone_lookup = ""
    if camp_id and camp_id in _CAMPAIGN_DATA:
        phone_lookup = str(_CAMPAIGN_DATA[camp_id].get("phone") or "").strip()
    elif camp_id and str(camp_id).startswith("incoming_"):
        parts = str(camp_id).split("_from_")
        if len(parts) >= 2:
            _digits = re.sub(r"\D", "", (parts[1].split("_")[0] or parts[1]).strip())
            if len(_digits) >= 10:
                phone_lookup = "+91" + _digits[-10:]
            elif _digits:
                phone_lookup = _digits
    elif camp_id and str(camp_id).startswith("manual_"):
        # Manual (console "Make a Call") legs: resolve the dialed number from the
        # manual_calls row so lead-history + continuity memory also load.
        try:
            from core.storage import manual_call_row_by_camp_id
            _mc_row = await manual_call_row_by_camp_id(camp_id)
            if _mc_row and _mc_row.get("to_phone"):
                phone_lookup = str(_mc_row["to_phone"]).strip()
        except Exception as _mc_ph_e:
            logger.warning("Manual call phone lookup failed for camp_id={}: {}", camp_id, _mc_ph_e)

    lead_history = None
    if phone_lookup:
        try:
            from core.storage import find_lead_by_phone
            lead_history = await find_lead_by_phone(role, phone_lookup)
        except Exception as he:
            logger.warning("Failed to load conversation history memory for phone={}: {}", phone_lookup, he)

    # Highest-priority anchors so Live cannot drift to unrelated brands or names from examples elsewhere.
    camp_row = _CAMPAIGN_DATA.get(camp_id) if camp_id and camp_id in _CAMPAIGN_DATA else None
    _authoritative_lead_name = _resolve_authoritative_lead_name(camp_row)
    if (
        not _authoritative_lead_name
        and str(camp_id or "").startswith("incoming_")
        and lead_history
    ):
        from core.opening_line import looks_like_real_name

        _nm_in = str(lead_history.get("name") or lead_name or "").strip()
        if looks_like_real_name(_nm_in):
            try:
                from core.campaign_payload import addressable_name as _addr_in

                _authoritative_lead_name = _addr_in(_nm_in)
            except Exception:
                _authoritative_lead_name = (
                    _nm_in.split()[0].capitalize() if _nm_in.split() else _nm_in
                )
    whatsapp_already_sent = False
    email_already_sent = False
    _lead_id_wa = None
    if camp_row and isinstance(camp_row, dict):
        _lead_id_wa = camp_row.get("_lead_id") or camp_row.get("id")
    if _lead_id_wa:
        try:
            from core.storage import get_lead_whatsapp_sent, get_lead_email_sent
            whatsapp_already_sent = await get_lead_whatsapp_sent(int(_lead_id_wa))
            email_already_sent = await get_lead_email_sent(int(_lead_id_wa))
        except Exception:
            pass
    
    if not whatsapp_already_sent and lead_history:
        whatsapp_already_sent = bool(lead_history.get("whatsapp_sent"))
    if not email_already_sent and lead_history:
        email_already_sent = bool(lead_history.get("email_sent"))

    detail_block = ""
    extra_dict: dict = {}
    if not _authoritative_lead_name:
        _authoritative_lead_name = _resolve_authoritative_lead_name(camp_row)
    if isinstance(camp_row, dict):
        from core.opening_line import looks_like_real_name

        raw_nm = str(camp_row.get("name", "") or "").strip()
        ph = str(camp_row.get("phone", "") or "").strip()
        raw_co = str(camp_row.get("company", "") or "").strip()
        em = str(camp_row.get("email", "") or "").strip()
        # Reject pure-digit / placeholder names (e.g. a serial-number column
        # picked by mistake during CSV upload). Otherwise Gemini would treat
        # ``Callee name field: 160`` as authoritative and say
        # "Hello, is this 160?". Same guard for company.
        nm = raw_nm if looks_like_real_name(raw_nm) else ""
        co = raw_co if looks_like_real_name(raw_co) else ""
        # ``extra`` carries any non-standard CSV columns the operator uploaded
        # (e.g. rfq_subject, product, quantity, last_quote, notes, city, industry).
        # We surface them so the agent can speak about the lead's specific
        # situation — but instruct it to weave details in naturally rather than
        # reading them out as a list, and to NEVER read out emails/IDs unless
        # asked. Empty/blank fields are skipped so the prompt stays compact.
        extra_raw = camp_row.get("extra") or {}
        if isinstance(extra_raw, str):
            try:
                extra_dict = json.loads(extra_raw) if extra_raw.strip() else {}
            except Exception:
                extra_dict = {}
        else:
            extra_dict = extra_raw if isinstance(extra_raw, dict) else {}
        # Pretty key (snake_case → Title Case) for readability in the prompt.
        def _pretty_key(k: str) -> str:
            return " ".join(w.capitalize() for w in str(k).replace("_", " ").split())

        extra_lines: list[str] = []
        for k, v in extra_dict.items():
            sv = str(v).strip()
            if sv:
                extra_lines.append(f"  {_pretty_key(k)}: {sv}")
        # Trim very long values so a runaway 5KB notes column can't blow up the
        # system prompt token budget. Keep the first 600 chars per field — more
        # than enough for an RFQ subject line, a quote summary, etc.
        extra_lines = [
            (line[:600] + "…") if len(line) > 600 else line for line in extra_lines
        ]

        # When no real name is available but we do have a company, instruct
        # the model to greet the company instead of inventing a name.
        if _authoritative_lead_name:
            name_hint = _authoritative_lead_name
        elif nm:
            name_hint = nm
        elif co:
            name_hint = (
                f"(personal name not supplied — address the callee as someone "
                f"from {co}, e.g. 'calling for {co}'. NEVER speak the dialed "
                "phone number, lead ID, or any digits as if they were a name.)"
            )
        else:
            name_hint = (
                "(not supplied — use the generic opener line without inventing "
                "a name. NEVER speak the dialed phone number or lead ID as if "
                "it were a name.)"
            )
        callee_lines = [
            "\n\n[CURRENT CALL DETAILS — AUTHORITATIVE FOR THIS PSTN LEG ONLY]",
            "Use only this callee name in speech — do not substitute any other person's name "
            "(names in sample scripts elsewhere are EXAMPLES ONLY, not who is on this call). "
            "When a Company field is present below, you MUST still acknowledge that organisation "
            "once early in the call (together with any personal name) — see RFQ instructions.",
            f"Callee name field: {name_hint}",
        ]
        if _authoritative_lead_name or nm:
            callee_lines.append(
                f"AUTHORITATIVE CALLEE NAME: {_authoritative_lead_name or nm} — use ONLY this name in speech; "
                "do NOT say the company brand 'Technopolis Constructions', agent names, or STT misheard names; "
                "ignore any different names in conversation history or example scripts."
            )
        callee_lines.extend([
            f"Dialed number: {ph}",
            f"WhatsApp project details sent: {'Yes' if whatsapp_already_sent else 'No'}",
        ])
        if co:
            callee_lines.append(f"Company: {co}")
        if em:
            callee_lines.append(
                f"Email on file: {em} — confirm spelling aloud before send_email_details; "
                "never send without confirmation."
            )

        if extra_lines:
            callee_lines.append("")
            callee_lines.append("Additional context from the lead list (mention these naturally when relevant — do not read out as a list, do not invent fields that are not here):")
            callee_lines.extend(extra_lines)

        detail_block = "\n".join(callee_lines) + "\n"

    _known_email_for_rules = ""
    if isinstance(camp_row, dict):
        _known_email_for_rules = str(camp_row.get("email") or "").strip()
    elif lead_history:
        _known_email_for_rules = str(lead_history.get("email") or "").strip()
    if role in ("sales_1",):
        _email_rule_lines = [
            "\n[EMAIL + WHATSAPP RULES]",
            "When sharing project details, always say: \"I'll share on WhatsApp after the call.\"",
        ]
        if _known_email_for_rules and "@" in _known_email_for_rules:
            _email_rule_lines.append(
                f"Email on file ({_known_email_for_rules}): confirm aloud — "
                f"\"I have your email as {_known_email_for_rules} — is that correct? I'll share there too.\" "
                "Then call send_email_details after they confirm."
            )
        else:
            _email_rule_lines.append(
                "No email on file: after WhatsApp offer, optionally ask once for their email ID. "
                "If they give it, confirm spelling, then send_email_details. If not, WhatsApp only — no pressure."
            )
        detail_block += "\n".join(_email_rule_lines) + "\n"

    # Incoming call lead context (when a known lead calls back)
    if str(camp_id or "").startswith("incoming_") and (_authoritative_lead_name or lead_history):
        from core.opening_line import looks_like_real_name

        _inc_name = _authoritative_lead_name or str((lead_history or {}).get("name") or lead_name or "").strip()
        _agent_in = agent_name or "Vernika"
        _inc_opening = (
            f"Hi, this is {_agent_in} from Technopolis Constructions. "
            "Thanks for calling Technopolis Constructions — how can I help you?"
        )
        if looks_like_real_name(_inc_name):
            detail_block += (
                "\n\n[INCOMING — KNOWN LEAD]\n"
                f"This is an incoming call from **{_inc_name}** — a known lead calling YOU back.\n"
                f"Turn 1 ONLY (say ONCE, never repeat): \"{_inc_opening}\"\n"
                f"Turn 2 (after they respond): ask ONCE \"Am I speaking with {_inc_name}?\" then STOP and wait.\n"
                "Do NOT combine both lines in one breath. Do NOT repeat the greeting.\n"
                "Do NOT say \"I am calling about\" — they called you.\n"
                f"Callee name field: {_inc_name}\n"
                f"WhatsApp project details sent: {'Yes' if whatsapp_already_sent else 'No'}\n"
            )
        else:
            detail_block += (
                "\n\n[CURRENT CALL DETAILS — INCOMING CALL]\n"
                "This is an INBOUND call — the customer dialed you.\n"
                f"CRITICAL: Your FIRST spoken line must be exactly: \"{_inc_opening}\"\n"
                "Do NOT say \"I am calling about\" — they called you.\n"
                f"WhatsApp project details sent: {'Yes' if whatsapp_already_sent else 'No'}\n"
            )

    # Per-role persona anchors. Live models drift to the strongest signal in
    # the prompt; so the sales_1 persona is anchored explicitly at the very top
    # of the system prompt to prevent drift. The agent NAME comes from the
    # user's saved prompt (extract_agent_name), never hardcoded.
    _agent_display = agent_name or "Vernika"
    _PERSONA_ANCHORS: dict[str, str] = {
        "sales_1": (
            "[ANCHOR — HIGHEST PRIORITY, OVERRIDES CONFLICTING LINES BELOW]\n"
            f"You are **{_agent_display}**, a **relationship manager** at **Technopolis Constructions Private Limited** — "
            "a premium real estate builder in Hyderabad with 17+ years of experience.\n"
            "You are calling about **Solitaire Unity**, a ready-to-move gated community in Kondapur, Hyderabad.\n"
            "START EVERY CALL IN TELUGU (natural conversational Telugu/Tenglish for Hyderabad). "
            "Only switch language if the caller speaks another language first.\n"
            f"If the user asks your name, say **{_agent_display}**.\n"
            "Follow your call prompt exactly — do not deviate from it.\n\n"
        ),
    }
    anchor = _PERSONA_ANCHORS.get(role, "")
    _is_incoming = str(camp_id or "").startswith("incoming_")
    if agent_name and role in ("sales_1",):
        role_label = {
            "sales_1": "Technopolis Constructions",
        }.get(role, role)
        if _is_incoming:
            opening_line = (
                f"Hi, this is {agent_name} from Technopolis Constructions. "
                "Thanks for calling Technopolis Constructions — how can I help you?"
            )
            context_hint = (
                "The customer has called YOU. Greet them warmly, thank them for calling, "
                "and ask how you can help. Do NOT say 'I am calling about' — they called you."
            )
        else:
            opening_line = (
                f'Hi, this is {agent_name} from Technopolis Constructions. '
                "I'm calling about our ready-to-move project Solitaire Unity in Kondapur — got a quick minute?"
            )
            context_hint = ""
        anchor = (
            "[ANCHOR — HIGHEST PRIORITY, OVERRIDES CONFLICTING LINES BELOW]\n"
            f"You are **{agent_name}**, a **Channel Partner relationship executive** in **{role_label}** at **Technopolis Constructions**.\n"
            "You call real estate brokers and channel partners about a commission opportunity.\n"
            f"If the user asks your name, say **{agent_name}**.\n"
            f'Your opening line on this call: "{opening_line}"\n'
            + (context_hint + "\n" if context_hint else "")
            + "\n"
        )

    # ─── Active campaign Case ────────────────────────────────────────────────
    # The operator can define and activate one "Case" per role from the
    # dashboard (e.g. "April Steel Sheets Push", "Diwali Discount Drive").
    # When set, its description is appended *near the top* of the system
    # prompt — strong enough to steer pitch/offer, but it never replaces
    # the persona anchor or hard rules below.
    case_block = ""
    active_case_name = ""
    active_case_desc = ""
    try:
        from core.storage import get_active_case

        active_case = await get_active_case(role)
        if active_case and (active_case.get("description") or "").strip():
            active_case_name = (active_case.get("name") or "").strip() or "Active Case"
            active_case_desc = active_case["description"].strip()
            case_block = (
                "\n[ACTIVE CASE — TODAY'S CAMPAIGN INSTRUCTIONS — APPLIES TO THIS CALL]\n"
                f"Case: {active_case_name}\n"
                "Follow these instructions naturally during the conversation. "
                "These describe the *current* campaign offer / context / pitch — "
                "blend them into your normal persona and product talk. They take "
                "priority over generic examples in the role prompt below, but "
                "they NEVER override the persona anchor (your name / company / "
                "language rules / end-call rules).\n"
                f"---\n{active_case_desc}\n---\n\n"
            )
            logger.info(
                "Injecting active case into system prompt for role={}: {!r}",
                role, active_case_name,
            )
    except Exception as exc:
        logger.warning("Active-case lookup failed for role={}: {}", role, exc)

    # ─── Language enforcement (multilingual mirroring) ──────────────────────
    language_enforcement = (
        "\n[LANGUAGE RULE — ABSOLUTE — APPLIES TO EVERY SPOKEN REPLY]\n"
        "You are fully multilingual in English and major Indian languages (Hindi, Kannada, Telugu, Tamil, Malayalam, Marathi, Bengali, Gujarati, etc.).\n"
        "1. Start with the recorded greeting; from the first caller reply onward, mirror their language exactly (see INDIAN LANGUAGES MIRROR RULE).\n"
        "2. You must REACTIVELY mirror the caller's spoken language on every single turn. If the caller speaks in a different language at any point, you must immediately pivot to that language in your very next reply. No announcement, no delay, no permission needed.\n"
        "3. This language-mirroring rule is fully dynamic and works in all directions: English -> Kannada -> Hindi -> English, etc. Whichever language the user speaks (including shifting back to English), you must instantly switch to that language.\n"
        "4. Switch languages immediately the moment the caller uses a different language — mirror on the very next reply, no exceptions.\n"
        "5. NEVER announce a language switch. Do NOT say Namaste, Namaskar, Vanakkam, or any greeting ritual when switching — just continue naturally in their language.\n"
        "6. Always use English for numbers, prices, measurements, and real estate terms (like 'apartment', 'flat', 'sq ft', 'crore', 'lakh') regardless of the language you are speaking.\n"
        "7. Single-word or very short inputs: maintain current language, don't switch on ambiguity alone.\n\n"
    )

    # ─── Voice / pacing rule (Gemini Live) ──────────────────────────────────
    # Gemini Live has no ``speaking_rate`` parameter, so we steer the speech
    # speed via the system prompt. Applies to all live roles.
    style_instr = settings.gemini_tts_style_prompt_female
    _voice_label = "keep this voice"
    _site_visit_line = (
        "10. Site visits: If they ask for pickup/transport (Kannada/Hindi/English), "
        "confirm the team can coordinate visit logistics — do NOT end the call.\n"
    )
    pacing_rule = (
        "\n[VOICE, ACCENT, & HUMAN DELIVERY — APPLIES TO EVERY SPOKEN REPLY]\n"
        f"1. Voice & Accent style ({_voice_label}, do not change persona):\n{style_instr}\n"
        "2. Human delivery: Warm Indian conversational tone — like a real sales executive on a phone call. "
        "Consultative, empathetic, confident. Never sound like a rigid reader or IVR bot.\n"
        "3. Turn length: Short and punchy (1–2 sentences). Expand only when the caller asks for project details.\n"
        "4. NO DEAD AIR — CRITICAL: The instant the caller finishes speaking (or says 'hello', 'yeah', 'ok', 'tell me'), "
        "you MUST reply immediately in the SAME breath — zero pause, no silence. "
        "Do NOT wait. Do NOT stay silent. The next syllable out of your mouth comes within a fraction of a second.\n"
        "5. If the caller says 'hello' or 'yeah' mid-call: do NOT say 'Yeah, I'm here!' — skip the filler and continue directly from the NEXT unsaid script step.\n"
        "6. Never repeat a question or prompt just because there is a brief pause. Wait for the caller to respond.\n"
        "7. Light fillers okay sparingly: 'Yeah', 'Gotcha', 'Right', 'So…'. "
        "NEVER open with hollow praise: Great, Wonderful, Awesome, Perfect, Excellent, Certainly.\n"
        "8. Voice flow: Vary pitch, pace, and energy — sound alive and engaged, not flat, rushed, or robotic.\n"
        "9. AI disclosure: If asked whether you are AI/a robot, say you are the personal assistant for Technopolis Constructions — NEVER claim to be a real human person.\n"
        f"{_site_visit_line}"
        "11. Bilingual pronunciation: Hindi/Hinglish/Kannada must sound native Indian, not American or British.\n"
        "12. COMPETITOR POLICY: NEVER recommend Prestige, Brigade, Godrej, or any other developer. "
        "Redirect to Solitaire Unity only. Never end call because they mentioned a competitor.\n\n"
    )

    # Universal "use the callee's context naturally" rules. Kept here (not in
    # the user-editable role prompts) so the operator can edit prompts freely
    # without losing this behavior. Applied to all three live roles.
    context_rules = (
        "\n[USING THE CALLEE'S CONTEXT — APPLIES TO EVERY CALL]\n"
        "The block titled 'CURRENT CALL DETAILS' below carries facts about the *exact* "
        "person on this PSTN leg (name, phone, company, email, plus any extra fields "
        "the operator uploaded — like an RFQ subject, product, quantity, last quote, "
        "city, industry, notes, etc.).\n"
        "Rules for using it:\n"
        "1. Treat that block as ground truth for who you are speaking to. Do NOT "
        "invent fields that are not present, and do NOT confuse it with example "
        "scripts elsewhere in the prompt.\n"
        "2. Reference fields *naturally* in conversation when they help — e.g. "
        "'I'm calling about the {RFQ Subject} you sent us' or 'I see you're with "
        "{Company} in {City}'. Do NOT read out the whole list.\n"
        "3. Use the callee's first name only 2–3 times in the entire call — never "
        "after every sentence.\n"
        "4. Never read out the email address, phone number, or any internal IDs "
        "unless the user explicitly asks for them.\n"
        "5. If a field is empty or missing, simply skip it. Don't say 'I don't "
        "have that information' unless the user asked.\n"
        "6. If the user contradicts a field (e.g. 'that's not my company anymore'), "
        "trust them, apologize briefly, and continue.\n\n"
    )

    # Dynamic datetime injection so Gemini knows the exact current time for callback scheduling.
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(settings.transcript_callback_tz)
    except Exception:
        from datetime import timezone
        import datetime as dt
        # fallback to timezone offset if zoneinfo fails or is missing, but zoneinfo is standard in Python 3.9+
        tz = timezone(dt.timedelta(hours=5, minutes=30)) # default to IST (+5:30)

    now_local = datetime.now(tz)
    # E.g. "Thursday, June 25, 2026, 01:41 PM"
    datetime_str = now_local.strftime("%A, %B %d, %Y, %I:%M %p")
    tz_label = "IST (Indian Standard Time)" if "Kolkata" in settings.transcript_callback_tz else settings.transcript_callback_tz
    time_block = (
        f"\n[CURRENT DATE & TIME — CRITICAL FOR SCHEDULING CALLBACKS]\n"
        f"The current local date and time is: {datetime_str} ({tz_label}).\n"
        f"Use this current time as the reference when the customer asks to call back.\n"
        f"CRITICAL: All times you confirm verbally to the user MUST be expressed in {tz_label} (IST) only. "
        f"Do NOT convert to UTC. Do NOT use UTC or say 'UTC' under any circumstances.\n"
        f"For example:\n"
        f"- If they say 'call me after X minutes' (e.g. 1 minute, 5 minutes), calculate from {now_local.strftime('%I:%M %p')} and confirm you will call them at that exact computed time (e.g., if current time is 12:28 AM and they say 5 minutes, say 'I will call you at 12:33 AM').\n"
        f"- If they say 'call me tomorrow' without a specific time, confirm you will call them exactly 24 hours from now (tomorrow at {now_local.strftime('%I:%M %p')} IST).\n"
        f"- If they mention a specific date and time, confirm that exact date and time in {tz_label}.\n"
        f"- Always be precise and natural. Confirmed times must match the local clock in India.\n\n"
    )

    # Resolve caller/callee phone number for history lookup & WhatsApp dedup (already resolved early)

    history_block = ""
    if phone_lookup:
        try:
            if lead_history:
                hist_analysis = {}
                if lead_history.get("analysis"):
                    try:
                        hist_analysis = json.loads(lead_history["analysis"])
                    except Exception:
                        hist_analysis = {}
                
                hist_summary = (hist_analysis.get("summary") or lead_history.get("details") or "").strip()
                hist_disposition = (hist_analysis.get("disposition") or lead_history.get("status") or "").strip()
                hist_next_steps = (hist_analysis.get("next_steps") or "").strip()
                hist_previous_call_time = lead_history.get("updated_at") or lead_history.get("created_at") or ""
                
                # Extract what the lead asked for (e.g., "send WhatsApp details", "call back later")
                lead_requested = ""
                if hist_analysis.get("whatsapp_sent"):
                    lead_requested = "Lead requested project details via WhatsApp — details were sent."
                elif hist_analysis.get("email_sent"):
                    lead_requested = "Lead requested project details via email — details were sent."
                elif hist_disposition and "callback" in hist_disposition.lower():
                    lead_requested = "Lead requested a callback at a later time."
                elif hist_analysis.get("summary") and "whatsapp" in hist_analysis["summary"].lower():
                    lead_requested = "Lead may have requested WhatsApp details."
                
                # Computed callback time if available
                callback_time_str = ""
                cb_iso = hist_analysis.get("requested_callback_datetime_iso") or ""
                if cb_iso:
                    try:
                        if cb_iso.endswith("Z") or cb_iso.endswith("z"):
                            cb_iso = cb_iso[:-1] + "+00:00"
                        cb_dt = datetime.fromisoformat(cb_iso)
                        if cb_dt.tzinfo is None:
                            cb_dt = cb_dt.replace(tzinfo=tz)
                        else:
                            cb_dt = cb_dt.astimezone(tz)
                        callback_time_str = cb_dt.strftime("%A, %B %d at %I:%M %p %Z")
                    except Exception:
                        callback_time_str = cb_iso

                # Check if they have a scheduled site visit (buyer campaigns only — not CP)
                site_visit_details = ""
                if role not in ("sales_1",):
                    next_action = hist_analysis.get("next_action") or {}
                    if lead_history.get("status") == "site_visit" or hist_analysis.get("site_visit_agreed") or (next_action.get("action_type") or "").strip().lower() in ("site visit", "site_visit"):
                        sv_date = next_action.get("datetime_iso") or hist_analysis.get("requested_callback_datetime_iso") or ""
                        if sv_date:
                            try:
                                if sv_date.endswith("Z") or sv_date.endswith("z"):
                                    sv_date = sv_date[:-1] + "+00:00"
                                sv_dt = datetime.fromisoformat(sv_date)
                                if sv_dt.tzinfo is None:
                                    sv_dt = sv_dt.replace(tzinfo=tz)
                                else:
                                    sv_dt = sv_dt.astimezone(tz)
                                weekday_name = sv_dt.strftime("%A")
                                time_str = sv_dt.strftime("%I:%M %p")
                                date_str = sv_dt.strftime("%B %d, %Y")
                                site_visit_details = f"Scheduled site visit for {weekday_name}, {date_str} at {time_str}"
                            except Exception:
                                site_visit_details = f"Scheduled site visit date/time: {sv_date}"
                
                if hist_summary or hist_disposition or site_visit_details or lead_requested:
                    history_lines = [
                        "\n[CONVERSATION HISTORY — PREVIOUS INTERACTION MEMORY]",
                        "Use this memory of the previous call to greet and converse with the user intelligently:",
                    ]
                    
                    # Check if the previous attempt failed to connect
                    is_failed_connection = False
                    prev_status = str(lead_history.get("status") or "").lower()
                    lead_extra = {}
                    if lead_history.get("extra"):
                        try:
                            lead_extra = json.loads(lead_history["extra"]) if isinstance(lead_history["extra"], str) else (lead_history["extra"] or {})
                        except Exception:
                            pass
                    retries = int(lead_extra.get("failed_call_retries") or 0)
                    _is_scheduled_cb_leg = bool(
                        (camp_id and str(camp_id).startswith("sched_cb_"))
                        or (isinstance(data, dict) and data.get("_is_scheduled_callback"))
                    )
                    _skip_failed_retry_intro = bool(
                        camp_id
                        and str(camp_id).startswith("manual_")
                        and not _is_scheduled_cb_leg
                    )

                    if (
                        not _skip_failed_retry_intro
                        and (
                            prev_status in ("failed", "busy", "no answer", "no response", "voicemail", "error")
                            or retries > 0
                        )
                    ):
                        is_failed_connection = True

                    if is_failed_connection:
                        history_lines.extend([
                            "CRITICAL INSTRUCTION — PREVIOUS ATTEMPT DID NOT CONNECT:",
                            f"Our system tried calling this lead previously, but we couldn't connect (outcome: {hist_disposition or 'No Answer'}).",
                            "Since this is a callback attempt after a failed connection, you MUST acknowledge it immediately after greeting them.",
                            "Say something like: \"Hi, I had tried reaching you earlier, but looks like we couldn't connect...\" or \"Hi, I tried calling you a bit earlier regarding Solitaire Unity, but couldn't connect...\"",
                            "Acknowledge the failed connection naturally, then verify their name (if known/unknown) before pitching.",
                        ])
                    else:
                        history_lines.extend([
                            "If this is a CALLBACK, you MUST acknowledge the previous conversation naturally:",
                            "\"I had called you earlier regarding...\" and then continue based on what was discussed.",
                        ])
                    if hist_previous_call_time:
                        history_lines.append(f"- **Previous call date/time:** {hist_previous_call_time}")
                    if hist_disposition:
                        history_lines.append(f"- **Previous outcome:** {hist_disposition}")
                    if hist_summary:
                        history_lines.append(f"- **What was discussed:** {hist_summary}")
                    if lead_requested:
                        history_lines.append(f"- **Lead's request:** {lead_requested}")
                    if hist_next_steps:
                        history_lines.append(f"- **Next steps promised:** {hist_next_steps}")
                    if callback_time_str:
                        history_lines.append(f"- **Requested callback time:** {callback_time_str}")
                    if site_visit_details:
                        history_lines.append(f"- **Site Visit Status:** {site_visit_details}")
                        history_lines.append(
                            "They know Solitaire Unity — still pitch premium apartments briefly, "
                            "then the apartment pricing (2/2.5/3 BHK, from approx ₹1.20–1.52 Cr). "
                            "Ask what held them back if not interested (location/budget/timing)."
                        )

                    if _is_incoming:
                        history_lines.append(
                            "INSTRUCTION (INBOUND): They called you. After greeting + name confirm, "
                            "reference this history naturally — do NOT restart as if you dialed them."
                        )
                    elif not _skip_failed_retry_intro:
                        history_lines.append(
                            "INSTRUCTION: Start the call by acknowledging the previous interaction. "
                            "Say something like \"I had called you earlier regarding Solitaire Unity...\" "
                            "and then continue based on the previous outcome."
                        )
                    
                    _cb_type_hist = ""
                    _fu_mem_hist: dict = {}
                    if isinstance(camp_row, dict):
                        _cb_type_hist = str(camp_row.get("_callback_type") or "").strip()
                        _fu_mem_hist = camp_row.get("_follow_up_memory") or {}
                        if not isinstance(_fu_mem_hist, dict):
                            _fu_mem_hist = {}

                    if _cb_type_hist == "site_visit_eve" or "Follow-up 1" in (opening_line or "") or (
                        camp_row and "Day-before" in str(camp_row.get("name", ""))
                    ):
                        history_lines.append(
                            "CRITICAL — FOLLOW-UP 1 (DAY BEFORE SITE VISIT): "
                            "Reference the prior call about Solitaire Unity. "
                            "They scheduled a site visit for tomorrow. "
                            "Ask: Are you still planning to come tomorrow? How many people will be visiting?"
                        )
                    elif _cb_type_hist == "site_visit_day" or "Follow-up 2" in (opening_line or "") or (
                        camp_row and "Morning-of" in str(camp_row.get("name", ""))
                    ):
                        history_lines.append(
                            "CRITICAL — FOLLOW-UP 2 (DAY OF SITE VISIT): "
                            "Good morning — their site visit is TODAY at Solitaire Unity. "
                            "Ask what time they will arrive. Say our team will be ready and waiting at the site."
                        )
                    elif "Re-confirm Site Visit" in (opening_line or "") or (camp_row and "Re-confirm Site Visit" in str(camp_row.get("name", ""))):
                        history_lines.append(
                            "CRITICAL: This is a RE-CONFIRMATION call the day before their scheduled site visit. "
                            "You must mention they are scheduled to visit us tomorrow. Ask them politely to confirm what time they will be visiting."
                        )
                    elif "Day of Site Visit" in (opening_line or "") or (camp_row and "Day of Site Visit" in str(camp_row.get("name", ""))):
                        history_lines.append(
                            "CRITICAL: This is the DAY OF THE SITE VISIT call. "
                            "Warmly mention they are scheduled to visit us today. Ask if they are on their way, or coordinate their timing."
                        )
                    if _fu_mem_hist.get("transcript_excerpt"):
                        history_lines.append(
                            f"- Prior conversation excerpt:\n{_fu_mem_hist['transcript_excerpt'][:1500]}"
                        )
                    
                    history_block = "\n".join(history_lines) + "\n\n"
                    if _authoritative_lead_name:
                        history_block = (
                            f"\n[NAME LOCK — THIS CALL ONLY]\n"
                            f"The callee's correct name on THIS call is **{_authoritative_lead_name}**.\n"
                            "Any different names in previous call summaries or transcripts are STT errors — NEVER use them for name verification on this call.\n\n"
                            + history_block
                        )
                    logger.info("Loaded conversation history memory for phone={} (disposition={})", phone_lookup, hist_disposition)
        except Exception as he:
            logger.warning("Failed to load conversation history memory for phone={}: {}", phone_lookup, he)

    # ── Continuity memory (persistent lead_memory facts) — universal block ──
    # Runs for EVERY call path (campaign, scheduled callback, manual, incoming,
    # rest-cycle): loads the stored rolling facts + summary for the resolved
    # lead and appends them as a "[CONTINUITY MEMORY]" section so the agent
    # behaves as if it remembers the person, not just the last disposition.
    # Orchestration legs already carry the frozen ``_lead_memory_text`` fast-path
    # block (appended later) — skip to avoid duplicate memory injection.
    if not (data.get("_lead_memory_text") or "").strip():
        continuity_block = await _build_continuity_memory_block(
            camp_row, lead_history, authoritative_name=_authoritative_lead_name,
        )
        if continuity_block:
            history_block += continuity_block
            logger.info(
                "Continuity memory block appended ({:,} chars, role={})",
                len(continuity_block),
                role,
            )

    # Issue 4 fix: language_enforcement moved to END (highest recency priority in transformer context).
    # This ensures the multilingual auto-detect rule is the LAST thing Gemini reads,
    # overriding any Hindi-dominant examples earlier in the role prompt file.
    if _is_incoming:
        if _authoritative_lead_name:
            name_rule = (
                "\n[INCOMING — NAME CONFIRM AFTER GREETING]\n"
                "Step 1: Say the inbound greeting exactly as in [OPENING].\n"
                f"Step 2: Ask ONCE: \"Am I speaking with {_authoritative_lead_name}?\" and STOP — wait.\n"
                "Step 3: After they confirm, use conversation history and continue naturally.\n"
                + _lead_name_speech_guard(_authoritative_lead_name)
                + "Do NOT say \"I am calling about\" — they called you.\n"
            )
        else:
            name_rule = (
                "\n[INCOMING CALL — GREETING FIRST]\n"
                "Step 1: Say the inbound greeting exactly as in [OPENING].\n"
                "Step 2: Listen to why they called — do NOT outbound-pitch until you understand their need.\n"
            )
    elif _authoritative_lead_name:
        if role in ("sales_1",):
            name_rule = (
                "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                "You MUST verify the lead's name BEFORE any project details or pitch.\n"
                "Step 1: Say your greeting + opening line.\n"
                f"Step 2: Ask ONCE: \"Am I speaking with {_authoritative_lead_name}?\" and then STOP — wait for their response.\n"
                "Step 3: Only after the lead confirms their name, acknowledge it once "
                "and THEN proceed with the project pitch per your call prompt. ABSOLUTELY NO project details "
                "until the name is confirmed.\n"
                + _lead_name_speech_guard(_authoritative_lead_name)
                + "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                "If still no answer after two tries, proceed without the name.\n"
                "This rule OVERRIDES any conflicting instructions below.\n"
            )
        else:
            name_rule = (
                "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                "You MUST verify the lead's name BEFORE any project details or pitch.\n"
                "Step 1: Say your greeting + opening line.\n"
                f"Step 2: Ask ONCE: \"Am I speaking with {_authoritative_lead_name}?\" and then STOP — wait for their response.\n"
                "Step 3: Only after the lead confirms their name, acknowledge it once "
                "and THEN proceed with the project pitch (premium apartments first, the project when budget fits). ABSOLUTELY NO project details "
                "until the name is confirmed.\n"
                + _lead_name_speech_guard(_authoritative_lead_name)
                + "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                "If still no answer after two tries, proceed without the name.\n"
                "This rule OVERRIDES any conflicting instructions below.\n"
            )
    else:
        if role in ("sales_1",):
            name_rule = (
                "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                "You MUST verify the lead's name BEFORE any project details or pitch.\n"
                "Step 1: Say your greeting + opening line.\n"
                "Step 2: Ask ONCE: \"May I know who I am speaking with, please?\" and then STOP — wait for their response.\n"
                "Step 3: Only after the lead confirms their name, acknowledge it once "
                "and THEN proceed with the project pitch per your call prompt. ABSOLUTELY NO project details "
                "until the name is confirmed.\n"
                "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                "If still no answer after two tries, proceed without the name.\n"
                "This rule OVERRIDES any conflicting instructions below.\n"
            )
        else:
            name_rule = (
                "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                "You MUST verify the lead's name BEFORE any project details or pitch.\n"
                "Step 1: Say your greeting + opening line.\n"
                "Step 2: Ask ONCE: \"May I know who I am speaking with, please?\" and then STOP — wait for their response.\n"
                "Step 3: Only after the lead confirms their name, acknowledge it once "
                "and THEN proceed with the project pitch (premium apartments first, the project when budget fits). ABSOLUTELY NO project details "
                "until the name is confirmed.\n"
                "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                "If still no answer after two tries, proceed without the name.\n"
                "This rule OVERRIDES any conflicting instructions below.\n"
            )

    _base_system_prompt = system_prompt
    # No hardcoded persona anchor / scripted name-verify — the operator's
    # frontend prompt is the source of truth for persona.
    system_prompt = name_rule + pacing_rule + case_block + context_rules + time_block + history_block + _base_system_prompt + detail_block + language_enforcement

    logger.info(f"Vobiz WS (live): client connected for camp={camp_id} role={role}")

    _incoming_connected_at: Optional[float] = None

    # 2. Setup Recording & Callbacks
    live_log_id = new_session_id("vobiz-live")
    if camp_id:
        live_log_id = f"camp-{camp_id[:12]}-{new_session_id('').strip('-')[:14]}"

    # 2.5 Mark connection time in memory + persist log_id so the dashboard can render
    # Listen / Audit links once the recording is finalized.
    # 2.5 Mark connection time in memory + persist so the dialer (local or remote) can detect pickup.
    if camp_id:
        from core.camp_session import hydrate_camp_session, mark_camp_connected

        await hydrate_camp_session(camp_id)
        await mark_camp_connected(camp_id, time.time(), live_log_id)
        if str(camp_id).startswith("incoming_"):
            _incoming_connected_at = time.time()
            try:
                from core.storage import update_incoming_call_on_connect

                await update_incoming_call_on_connect(camp_id, live_log_id)
                try:
                    from core.events import get_event_bus

                    await get_event_bus().publish(
                        "incoming_call_connected",
                        role=role,
                        camp_id=camp_id,
                        log_id=live_log_id,
                        status="connected",
                    )
                except Exception:
                    pass
            except Exception as _inc_log_err:
                logger.warning("Persist incoming call log_id failed: {}", _inc_log_err)
        if camp_id in _CAMPAIGN_DATA:
            try:
                lead_id = _CAMPAIGN_DATA[camp_id].get("_lead_id")
                if lead_id:
                    from core.state import update_lead_call_info as _persist_call_info

                    _persist_call_info(lead_id, log_id=live_log_id, call_id=camp_id)
            except Exception as _exc:
                logger.warning("Persist live_log_id failed: {}", _exc)
        logger.info(f"Call {camp_id} connected via WebSocket (log_id={live_log_id})")

    def on_recording_started_local(cid, lid):
        try:
            from services.campaign_live import push_transcript
            push_transcript(cid, "system", f"Recording started: {lid}")
        except Exception as cb_exc:
            logger.warning("on_recording_started callback failed: {}", cb_exc)

    # 3. Setup RAG Helper (inline, no external dependency).
    # Strategy: try keyword match first (precise lines win). If nothing matches,
    # fall back to FTS from rag.db, then a compact digest so Gemini always has fresh facts.
    _RAG_DIGEST_CHAR_LIMIT = 1800
    _RAG_SOURCE_TEXT = ""
    _RAG_STORE = None
    _QUESTION_WORDS = (
        "what", "how", "where", "when", "which", "why", "who", "price", "cost",
        "rate", "sqft", "sq ft", "location", "address", "amenities", "phase",
        "villa", "villas", "plot", "budget", "emi", "payment", "visit", "brochure",
        "tell me", "explain", "details", "available", "size", "configuration",
                "bhk", "crore", "cr", "lakh", "lac", "kondapur", "hyderabad", "possession",
        "rera", "bbmp", "bda", "discount", "offer", "looking", "want", "need",
    )

    try:
        _role_cfg_rag = get_state(role)
        _db_rag = (_role_cfg_rag.get("rag") or "").strip() if isinstance(_role_cfg_rag, dict) else ""
        from prompts.role_prompts import get_role_rag_source_text
        _file_rag = get_role_rag_source_text(role)
        _RAG_SOURCE_TEXT = _db_rag or _file_rag
        if settings.rag_enabled:
            from pathlib import Path as _Path
            _rag_db = _Path(settings.rag_db_path)
            if _rag_db.exists():
                from rag import RagStore
                _RAG_STORE = RagStore(str(_rag_db))
    except Exception as _rag_init_err:
        logger.warning("Live RAG init failed: {}", _rag_init_err)

    def _rag_digest(rag: str) -> str:
        lines = [ln.strip() for ln in (rag or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
        digest, total = [], 0
        for ln in lines:
            if total + len(ln) + 1 > _RAG_DIGEST_CHAR_LIMIT:
                break
            digest.append(ln)
            total += len(ln) + 1
        return "\n".join(digest)

    def _keyword_line_match(q: str, rag: str) -> str:
        import re as _re
        q_lower = (q or "").lower()
        q_terms = set(_re.findall(r"[a-z0-9]{3,}", q_lower))
        if not q_terms:
            return ""
        scored: list[tuple[int, str]] = []
        for ln in (rag or "").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            ln_lower = ln.lower()
            score = sum(1 for t in q_terms if t in ln_lower)
            if score >= 2 or (score >= 1 and len(q_terms) <= 2):
                scored.append((score, ln))
        if not scored:
            return ""
        scored.sort(key=lambda x: -x[0])
        return "\n".join(ln for _, ln in scored[:8])

    def _looks_like_question(q: str) -> bool:
        ql = (q or "").lower()
        return "?" in ql or any(w in ql for w in _QUESTION_WORDS)

    def _is_short_ack(q: str) -> bool:
        """Name confirms / backchannels — skip RAG inject so Gemini responds instantly."""
        ql = re.sub(r"[^\w\s]", " ", (q or "").lower()).strip()
        if not ql:
            return True
        words = ql.split()
        if len(words) <= 4 and not _looks_like_question(ql):
            ack_tokens = {
                "yes", "yeah", "yep", "yup", "ok", "okay", "speaking", "correct",
                "haan", "han", "ji", "hoon", "bolo", "tell", "go", "ahead", "sure",
                "hello", "hi", "hey", "namaste", "namaskar", "who", "this", "is",
                "me", "myself", "here", "listening", "boliye", "bolo", "ha", "theek",
            }
            if all(w in ack_tokens for w in words):
                return True
        return False

    def _is_telephony_announcement(q: str) -> bool:
        """Carrier announcements are not a callee turn and must not advance the script."""
        normalized = re.sub(r"[^a-z0-9\\s]", " ", (q or "").lower()).strip()
        return any(
            phrase in normalized
            for phrase in (
                "this call is now being recorded",
                "this call may be recorded",
                "your call is being recorded",
                "call recording has started",
            )
        )

    def _is_garbled_model_output(text: str) -> bool:
        """Detect internal/token leak or corrupted TTS text — never play or log as assistant turn."""
        if _is_explicit_refusal_leak(text):
            return True
        t = (text or "").strip()
        if not t:
            return False
        low = t.lower()
        junk_markers = (
            "declaraation", "endright", ":end", "end_call", "tool_call",
            "function_call", "json{", "```", "<|", "|>",
            "text/plain", "text/plain;",
        )
        if any(m in low for m in junk_markers):
            return True
        letters = sum(c.isalpha() for c in t)
        if len(t) >= 10 and letters / max(len(t), 1) < 0.45:
            return True
        return False

    def _strip_refusal_phrases(text: str) -> str:
        """Remove refusal substrings from assistant transcript accumulation."""
        t = (text or "").strip()
        if not t:
            return ""
        low = t.lower()
        cuts = (
            "i'm just a language model", "i am just a language model",
            "i'm just a", "i am just a", "just a language model",
            "large language model", "i'm a large language model", "i am a large language model",
            "language model and can't", "language model and cannot",
            "language model", "i can't help with that", "i cannot help with that",
            "can't help with that", "cannot help with that",
            "as a language model", "as an ai", "i'm an ai", "i am an ai",
        )
        out = t
        for c in cuts:
            idx = low.find(c)
            while idx >= 0:
                out = (out[:idx] + out[idx + len(c):]).strip()
                low = out.lower()
                idx = low.find(c)
        return re.sub(r"\s+", " ", out).strip()

    def _looks_like_commission_delivered(text: str) -> bool:
        """True only when actual Turn 4 commission numbers were spoken (not Turn 3 ask)."""
        low = (text or "").lower()
        return bool(
            re.search(
                r"3\s*%|12\s*lakh|50[,\s]?000|worth vouchers|entails you|qualified walk",
                low,
            )
        )

    def _looks_like_features_pitch_delivered(text: str) -> bool:
        low = (text or "").lower()
        has_features = bool(
            re.search(
                r"clubhouse|swimming pool|gymnasium|ready to move|396 units|oc received",
                low,
            )
        )
        asked_commission = bool(
            re.search(r"commission works|channel partner commission|know how the commission", low)
        )
        return has_features and (asked_commission or "commission" in low)

    def _looks_like_account_manager_cta_asked(text: str) -> bool:
        return _looks_like_cta_fragment(text)

    def _looks_like_turn45_merged(text: str) -> bool:
        """Commission numbers + Account Manager CTA in the same assistant turn."""
        return _looks_like_commission_delivered(text) and _looks_like_account_manager_cta_asked(text)

    def _user_ready_for_commission_pitch(stt: str) -> bool:
        ql = re.sub(r"[^\w\s]", " ", (stt or "").lower()).strip()
        return bool(
            re.search(
                r"\b(okay|ok|yes|yeah|yep|sure|tell me|done|go ahead|please|fine|interested|english)\b",
                ql,
            )
        )

    def _user_ready_for_account_manager_cta(stt: str) -> bool:
        """Explicit affirmative to Account Manager connect — not factual asks like 'tell me the price'."""
        if _user_asked_factual_question(stt):
            return False
        ql = re.sub(r"[^\w\s]", " ", (stt or "").lower()).strip()
        return bool(
            re.search(
                r"\b(yes|yeah|yep|sure|sounds good|interested|please connect|that works|connect(?:\s+me)?|go ahead)\b",
                ql,
            )
        )

    def _stt_is_checkin_only(q: str) -> bool:
        """Pure hello / presence check — not when the caller also asks something."""
        raw = (q or "").strip()
        if not raw or _looks_like_question(raw):
            return False
        ql = re.sub(r"[^\w\s]", " ", raw.lower()).strip()
        if re.search(r"\b(tell me|go ahead|sure|interested|continue|where|location|price|cost)\b", ql):
            return False
        presence_words = re.search(
            r"\b(hello|hi|hey|haan|han|ji|namaste|namaskar|are you there|can you hear|you there)\b",
            ql,
        )
        if not presence_words:
            return False
        stripped = re.sub(
            r"\b(hello|hi|hey|haan|han|ji|namaste|namaskar|are you there|can you hear|you there)\b",
            " ",
            ql,
        )
        return len(stripped.split()) <= 2

    def _looks_like_silence_checkin_phrase(text: str) -> bool:
        """Assistant lines like 'still there?' / 'checking in' — max once per silence gap."""
        ql = re.sub(r"[^\w\s?]", " ", (text or "").lower()).strip()
        if not ql:
            return False
        return bool(
            re.search(
                r"\b(still there|are you there|are you still|checking in|everything okay|everything alright|can you hear|you there)\b",
                ql,
            )
        )

    def _rag_block_for_query(q: str, *, require_question: bool = True) -> Optional[str]:
        if not settings.rag_enabled or (settings.rag_mode or "chunk").strip().lower() == "off":
            return None
        q = (q or "").strip()
        if len(q) < 2:
            return None
        if require_question and not _looks_like_question(q):
            return None
        rag_mode = (settings.rag_mode or "chunk").strip().lower()
        if rag_mode == "chunk":
            try:
                from services.chunk_rag import format_chunk_context, retrieve_chunks

                chunks = retrieve_chunks(q, role, top_k=settings.rag_chunk_top_k, max_chars=settings.rag_chunk_max_chars)
                block = format_chunk_context(chunks)
                if block:
                    return block
            except Exception as _chunk_err:
                logger.debug("Chunk RAG query failed: {}", _chunk_err)
        if _RAG_SOURCE_TEXT:
            matched = _keyword_line_match(q, _RAG_SOURCE_TEXT)
            if matched:
                return f"[KB — matched lines]\n{matched}"
        if _RAG_STORE is not None:
            try:
                from rag import format_references
                items = _RAG_STORE.query(
                    q,
                    top_k=settings.rag_top_k,
                    max_chars=min(settings.rag_max_context_chars, _RAG_DIGEST_CHAR_LIMIT),
                )
                refs = format_references(items)
                if refs:
                    return f"[KB — retrieved]\n{refs}"
            except Exception as _fts_err:
                logger.debug("Live RAG FTS query failed: {}", _fts_err)
        if rag_mode == "embed" and _RAG_SOURCE_TEXT:
            digest = _rag_digest(_RAG_SOURCE_TEXT)
            if digest:
                return f"[KB — reference digest]\n{digest}"
        # ── Scoped web-search fallback (last resort) ─────────────────────────
        # RAG had nothing. Search the live web — ALWAYS scoped to the project
        # so Gemini never pulls other Technopolis projects. Hard 5s cap keeps
        # the live conversation low-delay. Injected as [SYSTEM WEB SEARCH].
        try:
            block = _scoped_web_search(q)
            if block:
                return block
        except Exception as _web_err:
            logger.debug("Scoped web search failed: {}", _web_err)
        return None

    def _scoped_web_search(q: str) -> Optional[str]:
        """DuckDuckGo search scoped to Technopolis Solitaire Unity <q>."""
        import html as _html
        import urllib.parse

        project_scope = "Technopolis Solitaire Unity Kondapur"
        query = project_scope + " " + q
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        try:
            import httpx

            resp = httpx.get(
                url,
                timeout=5.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                },
            )
            if resp.status_code != 200:
                return None
            titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', resp.text, re.S)
            snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.S)
            results: list[str] = []
            for i, t in enumerate(titles[:4]):
                title = _html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                if not title:
                    continue
                snip = ""
                if i < len(snips):
                    snip = _html.unescape(re.sub(r"<[^>]+>", "", snips[i])).strip()
                results.append(title + (" — " + snip[:200] if snip else ""))
            if not results:
                return None
            lines = ["[SYSTEM WEB SEARCH — Solitaire Unity only]"]
            lines.append("Live search results for Solitaire Unity (use ONLY these; ignore any other Technopolis project in results):")
            for i, r in enumerate(results, 1):
                lines.append(str(i) + ". " + r)
            return "\n".join(lines)[:1400]
        except Exception:
            return None

    def live_rag_context(q: str) -> Optional[str]:
        return _rag_block_for_query(q, require_question=True)

    def live_rag_warm_digest() -> Optional[str]:
        """Compact KB snapshot for connect-time inject (no question gate)."""
        if not settings.rag_enabled or (settings.rag_mode or "chunk").strip().lower() == "off":
            return None
        if _rag_embedded_in_prompt or _rag_connect_digest_embedded:
            return None
        rag_mode = (settings.rag_mode or "chunk").strip().lower()
        if rag_mode == "chunk":
            try:
                from services.chunk_rag import connect_digest_for_role

                digest = (connect_digest_for_role(role, max_chars=900) or "").strip()[:900]
                if digest:
                    return digest
            except Exception as _ov_err:
                logger.debug("Chunk connect digest warm failed: {}", _ov_err)
        if _RAG_SOURCE_TEXT:
            digest = _rag_digest(_RAG_SOURCE_TEXT)[:320]
            if digest:
                return f"[KB — project overview]\n{digest}"
        return None
    state = VobizSessionState()
    state.log_session_id = live_log_id
    state.whatsapp_sent = whatsapp_already_sent
    state.email_sent = email_already_sent
    if settings.call_recording_enabled:
        try:
            state.call_recorder = CallRecorder(
                live_log_id,
                channel="vobiz_live",
                base_dir=None,
                lead_name=str(lead_name or ""),
                phone=str(phone_lookup or ""),
                role=str(role or ""),
            )
            logger.info("CallRecorder created for session {}", live_log_id)
        except Exception as _rec_err:
            logger.warning("CallRecorder init failed: {}", _rec_err)
    vobiz_meta_logged = False
    last_user_audio_t: Optional[float] = None
    response_t0: Optional[float] = None
    # Watchdog: timestamp of the last *meaningful* event (user STT text in or model
    # turn complete out). Updated in pump_gemini_to_queue. If neither side does
    # anything meaningful for SILENCE_HANGUP_SEC, the silence_watchdog task hangs
    # up the call so the campaign worker can move to the next lead.
    last_meaningful_t: float = time.perf_counter()
    SILENCE_HANGUP_SEC: float = float(os.getenv("CALL_SILENCE_HANGUP_SEC", "180"))
    CALL_SILENCE_PRODDER_HANGUP_SEC: float = float(os.getenv("CALL_SILENCE_PRODDER_HANGUP_SEC", "0"))
    MIN_ENGAGED_CALL_SEC: float = float(os.getenv("MIN_ENGAGED_CALL_SEC", "120"))
    # Issue 1 fix: minimum user-silence gap before AI follow-up nudge fires.
    # Env-var configurable; default 8 seconds.
    USER_SILENCE_GATE_SEC: float = float(os.getenv("USER_SILENCE_GATE_SEC", "8.0"))
    _last_user_spoke_t: float = time.perf_counter()  # updated whenever user STT arrives
    _user_has_spoken: bool = False
    _opening_delivered: bool = False  # True once the AI has spoken or PCM has played
    # Issue 3 fix: voicemail / call-screening detection state.
    VOICEMAIL_DETECT_SEC: float = float(os.getenv("VOICEMAIL_DETECT_SEC", "90"))
    VOICEMAIL_HUMAN_WAIT_SEC: float = float(os.getenv("VOICEMAIL_HUMAN_WAIT_SEC", "5"))
    VOICEMAIL_NO_HUMAN_SEC: float = float(os.getenv("VOICEMAIL_NO_HUMAN_SEC", "22"))
    _call_connect_time: float = time.perf_counter()
    _is_voicemail_mode: bool = False
    _voicemail_triggered: bool = False
    _vm_phase: str = ""  # screening | message | done
    _vm_wait_until: float = 0.0
    _vm_message_armed: bool = False
    GREETING_LISTEN_DURING_PCM: bool = os.getenv("GREETING_LISTEN_DURING_PCM", "true").lower() in (
        "1", "true", "yes", "on",
    )
    _greeting_listen_active: bool = False
    _callee_class: str = "unknown"  # unknown | human | automated
    _callee_vm_kind: str = ""
    _greeting_stt_snippets: list[str] = []
    _post_greeting_grace_until: float = 0.0
    _pcm_handoff_keepalive_until: float = 0.0
    _suppress_gemini_until_nudge: bool = False
    POST_GREETING_GRACE_SEC: float = float(os.getenv("POST_GREETING_GRACE_SEC", "1.0"))
    POST_NAME_PITCH_DEFER_SEC: float = float(os.getenv("POST_NAME_PITCH_DEFER_SEC", "0.9"))
    LOCAL_BARGE_IN_RMS: int = int(os.getenv("LOCAL_BARGE_IN_RMS", "700"))
    LOCAL_BARGE_IN_ENABLED: bool = os.getenv("LOCAL_BARGE_IN_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    LOCAL_BARGE_IN_STREAK: int = max(1, int(os.getenv("LOCAL_BARGE_IN_STREAK", "2")))
    RESPOND_NUDGE_COOLDOWN_SEC: float = float(os.getenv("RESPOND_NUDGE_COOLDOWN_SEC", "0.8"))
    _last_user_stt_snippet: str = ""
    _hello_nudge_sent_at: float = 0.0
    _post_hello_grace_until: float = 0.0
    POST_HELLO_GRACE_SEC: float = float(os.getenv("POST_HELLO_GRACE_SEC", "3.0"))
    _name_confirmed: bool = False
    _pitch_delivered: bool = False
    _commission_delivered: bool = False
    _features_pitch_delivered: bool = False
    _account_manager_cta_count: int = 0
    _ACCOUNT_MANAGER_CTA_MAX: int = 2
    _commission_pitch_nudge_sent: bool = False
    _anti_refusal_recovery_sent: bool = False
    _post_name_pitch_nudge_sent: bool = False
    _hello_nudge_count: int = 0
    _last_nudge_kind: str = ""
    _fake_dev_block_sent: bool = False
    _closing_mode: bool = False
    USER_SILENCE_FIRST_PROD_SEC: float = float(os.getenv("USER_SILENCE_FIRST_PROD_SEC", "10"))
    USER_SILENCE_REPEAT_PROD_SEC: float = float(os.getenv("USER_SILENCE_REPEAT_PROD_SEC", "5"))
    USER_SILENCE_HANGUP_SEC: float = float(os.getenv("USER_SILENCE_HANGUP_SEC", "20"))
    DEAD_AIR_BREAKER_SEC: float = float(os.getenv("DEAD_AIR_BREAKER_SEC", "1.8"))
    INSTANT_RESPONSE_KICK_SEC: float = float(os.getenv("INSTANT_RESPONSE_KICK_SEC", "0.10"))
    CP_RESPONSE_GRACE_SEC: float = float(os.getenv("CP_RESPONSE_GRACE_SEC", "0.10"))
    # Recover genuine zero-audio Gemini turns, but only after all queued playout
    # audio has drained (see the turn-complete guard below).
    GHOST_TURN_NUDGE_ENABLED: bool = os.getenv("GHOST_TURN_NUDGE_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
    )
    BARGE_IN_VOICE_THRESHOLD: int = int(os.getenv("BARGE_IN_VOICE_THRESHOLD", "300"))
    CONFIRMED_SPEECH_WINDOW_SEC: float = float(os.getenv("CONFIRMED_SPEECH_WINDOW_SEC", "3.0"))
    VOICE_CONFIRM_STREAK_NEEDED: int = max(2, int(os.getenv("VOICE_CONFIRM_STREAK_NEEDED", "3")))
    _name_verify_asked: bool = False
    _last_activity_end_t: float = 0.0
    _response_started_after_user: bool = False
    _dev_mode_active: bool = False
    _dev_mode_nudge_sent: bool = False
    _phase3_nudge_sent: bool = False
    MIN_OUTBOUND_CALL_SEC: float = float(os.getenv("MIN_OUTBOUND_CALL_SEC", "30"))
    # Prefetch name-verify audio when this many greeting bytes remain (~300ms @ 16kHz mono).
    POST_GREETING_EARLY_NUDGE_BYTES: int = int(os.getenv("POST_GREETING_EARLY_NUDGE_BYTES", "9600"))
    _rag_embedded_in_prompt: bool = bool(
        settings.rag_enabled
        and (settings.rag_embed_in_system_prompt or getattr(settings, "rag_embed_full_kb", False))
        and (settings.rag_mode or "chunk").strip().lower() == "embed"
        and _RAG_SOURCE_TEXT
    )
    _rag_connect_digest_embedded: bool = False
    _pending_transcript: dict[str, str] = {"user": "", "assistant": ""}
    _post_greeting_nudge_armed: bool = False
    _prefetch_live_handoff: bool = False

    # Resolve Vobiz REST credentials for this leg so we can DELETE the call
    # when the agent fires ``end_call``. Falls back to global settings the
    # same way the campaign worker does.
    try:
        _role_state = get_state(role) or {}
    except Exception as exc:
        logger.exception("get_state failed for role={}", role)
        _role_state = {}
    _v_cfg = (_role_state.get("vobiz") or {}) if isinstance(_role_state, dict) else {}
    vobiz_auth_id = _v_cfg.get("auth_id") or settings.vobiz_auth_id or ""
    vobiz_auth_token = _v_cfg.get("auth_token") or settings.vobiz_auth_token or ""

    # ── Outbound scripted PCM + opening instructions ─────────────────────────
    # Must run *before* ``build_live_setup`` so Gemini receives CONTEXT / OPENING
    # fragments in ``system_instruction`` instead of silently mutating a local
    # string after ``setup`` is already sent.

    prior_16k_queue = bytearray()
    vobiz_stream_started = asyncio.Event()
    _vobiz_ws_events_logged = 0

    def _mark_vobiz_stream_ready(source: str, *, call_id: str = "", stream_id: str = "") -> None:
        if call_id:
            state.call_id = state.call_id or call_id
        if stream_id:
            state.stream_id = state.stream_id or stream_id
        if not vobiz_stream_started.is_set():
            vobiz_stream_started.set()
            logger.info(
                "Vobiz stream ready via {} (call={} stream={})",
                source,
                state.call_id or "?",
                state.stream_id or "?",
            )

    async def _vobiz_stream_start_fallback() -> None:
        """Some Vobiz legs never emit ``start`` — unblock greeting playout quickly."""
        await asyncio.sleep(0.75)
        if not vobiz_stream_started.is_set():
            logger.warning(
                "Vobiz stream start not received within 0.75s — unblocking playout (camp={})",
                camp_id,
            )
            _mark_vobiz_stream_ready("timeout_fallback")

    role_config = get_state(role)
    _is_incoming_leg = str(camp_id or "").startswith("incoming_")
    if not agent_name and _is_incoming_leg:
        agent_name = "Vernika"

    if _is_incoming_leg:
        opening_line = (
            f"Hi, this is {agent_name} from Technopolis Constructions. "
            "Thanks for calling Technopolis Constructions — how can I help you?"
        )
        greeting_text = opening_line
        prior_16k_queue.clear()
        # Prefer prerecorded inbound greeting so caller hears speech immediately
        _inbound_pcm_ready = False
        try:
            from core.greeting_pcm import load_recorded_greeting_pcm

            recorded = load_recorded_greeting_pcm(
                role, variant="inbound", greeting_text=opening_line
            )
            if not recorded:
                # Reuse outbound greeting PCM for this role if inbound variant missing
                recorded = load_recorded_greeting_pcm(role, greeting_text=opening_line)
            if not recorded:
                # Last resort: any on-disk greeting for role (ignore text hash mismatch for inbound)
                recorded = load_recorded_greeting_pcm(role, variant="inbound")
            if not recorded:
                recorded = load_recorded_greeting_pcm(role)
            if recorded:
                pcm_bytes, in_sr = recorded
                prior_16k_queue.extend(
                    prepare_scripted_greeting_pcm(pcm_bytes, in_sr, VOBIZ_SR, fade_edges=True)
                )
                _inbound_pcm_ready = True
                logger.info(
                    "incoming_pcm_played role={} ({} bytes) — Gemini Live connects in background",
                    role,
                    len(pcm_bytes),
                )
        except Exception as _in_pcm_err:
            logger.warning("Incoming PCM load failed: {}", _in_pcm_err)
        if _inbound_pcm_ready:
            gemini_live_first = False
        else:
            gemini_live_first = True
            logger.info(
                "Incoming call: no greeting PCM — Gemini Live inbound greeting only"
            )
    else:
        from core.state import resolved_greeting_text

        greeting_text = resolved_greeting_text(role)
        camp_open_row = _CAMPAIGN_DATA.get(camp_id, {}) if camp_id else {}
        if isinstance(camp_open_row, dict):
            from core.greeting_text_utils import coerce_stored_greeting

            cg = coerce_stored_greeting(role, (camp_open_row.get("greeting_text") or "").strip())
            if cg:
                greeting_text = cg
            elif not greeting_text:
                from core.opening_line import build_opening_line as _role_opening_line

                greeting_text = (_role_opening_line(camp_open_row, role) or "").strip()
        if not greeting_text:
            greeting_text = (opening_line or settings.vobiz_opening_line_default or "").strip()
        from core.greeting_text_utils import intro_only_greeting

        greeting_text = intro_only_greeting(greeting_text)
        if greeting_text:
            opening_line = greeting_text
        gemini_live_first = bool(settings.gemini_live_first_opening)

    if not _is_incoming_leg:
        if (
            not gemini_live_first
            and camp_id
            and camp_id in _CAMPAIGN_DATA
            and not _CAMPAIGN_DATA[camp_id].get("opening_pcm")
        ):
            try:
                from core.greeting_pcm import ensure_opening_pcm

                greet_try = (greeting_text or opening_line or "").strip()
                await ensure_opening_pcm(camp_id, role, greet_try)
            except Exception as exc:
                logger.warning("ensure_opening_pcm at WS connect failed for camp_id={}: {}", camp_id, exc)
        if not gemini_live_first:
            from core.greeting_pcm import _text_hash as _greeting_text_hash

            _want_greeting_hash = _greeting_text_hash((greeting_text or opening_line or "").strip())
            # 1) Memory primed at dial (campaign / manual) — earliest ready path for recorded audio
            if camp_id and camp_id in _CAMPAIGN_DATA:
                _mem_row = _CAMPAIGN_DATA[camp_id]
                _mem_pcm_hash = _mem_row.get("_opening_pcm_text_hash")
                if _mem_row.get("opening_pcm") and _mem_pcm_hash != _want_greeting_hash:
                    logger.info(
                        "Discarding stale opening_pcm for camp_id={} (hash {} != {})",
                        camp_id,
                        _mem_pcm_hash,
                        _want_greeting_hash,
                    )
                    _mem_row.pop("opening_pcm", None)
                prewarmed = _mem_row.get("opening_pcm")
                prewarm_pair = coerce_pcm_sr_pair(prewarmed)
                if prewarm_pair:
                    pcm_bytes, in_sr = prewarm_pair
                    prior_16k_queue.extend(prepare_scripted_greeting_pcm(pcm_bytes, in_sr, VOBIZ_SR, fade_edges=True))
                    logger.info("Loaded pre-primed greeting from campaign memory (before disk).")

            # 2) Disk: recorded greeting
            if len(prior_16k_queue) == 0:
                from core.greeting_pcm import load_recorded_greeting_pcm

                greet_for_hash = (greeting_text or opening_line or "").strip()
                recorded = load_recorded_greeting_pcm(role, greeting_text=greet_for_hash)
                if recorded:
                    pcm_bytes, in_sr = recorded
                    prior_16k_queue.extend(prepare_scripted_greeting_pcm(pcm_bytes, in_sr, VOBIZ_SR, fade_edges=True))
                    logger.info(
                        "Loaded recorded greeting from disk for role={} ({} bytes)",
                        role,
                        len(pcm_bytes),
                    )

    if len(prior_16k_queue) > 0:
        logger.info(
            "Scripted greeting ready for playout ({} bytes incl. head/tail silence)",
            len(prior_16k_queue),
        )

    _had_scripted_name_verify = False
    if (
        not _is_incoming_leg
        and settings.scripted_name_verify_pcm
        and camp_id
        and camp_id in _CAMPAIGN_DATA
        and not gemini_live_first
    ):
        try:
            from core.greeting_pcm import ensure_name_verify_pcm_for_call

            await ensure_name_verify_pcm_for_call(camp_id, role)
        except Exception as _nv_ensure_err:
            logger.warning("Name-verify PCM ensure at session start failed: {}", _nv_ensure_err)
        _nv_pre = _CAMPAIGN_DATA[camp_id].get("name_verify_pcm")
        _nv_pair = coerce_pcm_sr_pair(_nv_pre)
        if _nv_pair and len(prior_16k_queue) > 0:
            try:
                nv_bytes, nv_sr = _nv_pair
                _nv_prepared = prepare_scripted_greeting_pcm(nv_bytes, nv_sr, VOBIZ_SR, head_ms=80.0, tail_ms=120.0)
                prior_16k_queue.extend(_nv_prepared)
                _had_scripted_name_verify = True
                logger.info(
                    "Appended scripted name-verify PCM ({} bytes) after greeting — total {} bytes",
                    len(_nv_prepared),
                    len(prior_16k_queue),
                )
            except Exception as _nv_play_err:
                logger.warning("Failed to append name-verify PCM: {}", _nv_play_err)

    if len(prior_16k_queue) > 0 and _had_scripted_name_verify:
        logger.info(
            "Scripted greeting + name-verify ready ({} bytes total)",
            len(prior_16k_queue),
        )
        _name_verify_asked = True

    # #region agent log
    try:
        from debug_agent_log import agent_debug

        _nv_avail = bool(
            camp_id and camp_id in _CAMPAIGN_DATA and _CAMPAIGN_DATA[camp_id].get("name_verify_pcm")
        )
        agent_debug(
            "C",
            "live_session.py:session_init",
            "name_verify_pcm",
            {
                "appended": bool(_had_scripted_name_verify),
                "nv_in_camp_data": _nv_avail,
                "total_pcm_bytes": len(prior_16k_queue),
                "auth_name_len": len(_authoritative_lead_name or ""),
            },
        )
    except Exception:
        pass
    # #endregion

    # #region agent log
    try:
        from debug_agent_log import agent_debug
        from services.vobiz_bridge import audio as _audio_mod

        if hasattr(_audio_mod, "send_play_audio"):
            _audio_mod.send_play_audio._dbg_logged = False  # type: ignore[attr-defined]

        _raw_camp_name = ""
        if isinstance(camp_row, dict):
            _raw_camp_name = str(camp_row.get("name") or "")[:32]
        agent_debug(
            "A",
            "live_session.py:session_init",
            "session_init",
            {
                "camp_id_prefix": str(camp_id or "")[:12],
                "role": role,
                "pcm_queue_bytes": len(prior_16k_queue),
                "gemini_live_first": gemini_live_first,
                "authoritative_name_set": bool(_authoritative_lead_name),
                "raw_camp_name_len": len(_raw_camp_name),
                "suppress_until_nudge": len(prior_16k_queue) > 0,
            },
        )
        if _authoritative_lead_name:
            agent_debug(
                "B",
                "live_session.py:session_init",
                "authoritative_name",
                {"name_len": len(_authoritative_lead_name)},
            )
    except Exception:
        pass
    # #endregion

    _suppress_gemini_until_nudge = len(prior_16k_queue) > 0
    _scripted_first_turn_phrase = ""

    # Rebuild system prompt if PCM greeting will play to drop any intro/opener directives
    _pcm_greeting_will_play = len(prior_16k_queue) > 0
    if _pcm_greeting_will_play and not _is_incoming:
        _opening_delivered = True  # PCM handles the opening; silence-gate prodder should not re-fire
        
        # Clean example opening scripts from base prompt to prevent the model from copying them
        import re as _re
        _base_system_prompt = _re.sub(
            r'## SECTION 3: OPENING SCRIPTS.*?(?=## SECTION 4:)',
            '',
            _base_system_prompt,
            flags=_re.DOTALL | _re.IGNORECASE
        )
        
        # Strip 'Your opening line on this call: ...' from anchor if present
        _OPENING_SENTINEL = 'Your opening line on this call:'
        if _OPENING_SENTINEL in anchor:
            # Replace the opening-line sentence with a do-not-repeat directive
            anchor = _re.sub(
                r'Your opening line on this call:.*?\n',
                'The pre-recorded greeting was already played. Do NOT introduce yourself again. '
                'Ask the name verification question immediately.\n',
                anchor,
            )
        
        # Determine pitch target based on role
        _pitch_target = "Solitaire Unity premium apartments"
        # Rebuild system_prompt, preserving time and history blocks
        if _authoritative_lead_name:
            if role in ("sales_1",):
                _pcm_name_rule = (
                    "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                    "The pre-recorded greeting was already played — do NOT repeat your greeting or opening line.\n"
                    "Step 1 (SKIPPED — already played): Pre-recorded greeting delivered.\n"
                    f"Step 2: Ask ONCE: \"Am I speaking with {_authoritative_lead_name}?\" and then STOP — wait for their response.\n"
                    "Step 3: Only after the lead confirms their name, acknowledge it once "
                    f"and THEN proceed with the {_pitch_target}. ABSOLUTELY NO project details "
                    "until the name is confirmed.\n"
                    + _lead_name_speech_guard(_authoritative_lead_name)
                    + "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                    "If still no answer after two tries, proceed without the name.\n"
                    f"When user confirms name or says 'tell me' / 'go ahead', IMMEDIATELY pitch {_pitch_target}. "
                    "NEVER call end_call in the first 60 seconds of an answered call.\n"
                    "This rule OVERRIDES any conflicting instructions below.\n"
                )
            else:
                _pcm_name_rule = (
                    "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                    "The pre-recorded greeting was already played — do NOT repeat your greeting or opening line.\n"
                    "Step 1 (SKIPPED — already played): Pre-recorded greeting delivered.\n"
                    f"Step 2: Ask ONCE: \"Am I speaking with {_authoritative_lead_name}?\" and then STOP — wait for their response.\n"
                    "Step 3: Only after the lead confirms their name, acknowledge it once "
                    f"and THEN proceed with the {_pitch_target}. ABSOLUTELY NO project details "
                    "until the name is confirmed.\n"
                    + _lead_name_speech_guard(_authoritative_lead_name)
                    + "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                    "If still no answer after two tries, proceed without the name.\n"
                    f"When user confirms name or says 'tell me' / 'go ahead', IMMEDIATELY pitch {_pitch_target}. "
                    "NEVER call end_call in the first 60 seconds of an answered call.\n"
                    "This rule OVERRIDES any conflicting instructions below.\n"
                )
        else:
            if role in ("sales_1",):
                _pcm_name_rule = (
                    "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                    "The pre-recorded greeting was already played — do NOT repeat your greeting or opening line.\n"
                    "Step 1 (SKIPPED — already played): Pre-recorded greeting delivered.\n"
                    "Step 2: Ask ONCE: \"May I know who I am speaking with, please?\" and then STOP — wait for their response.\n"
                    "Step 3: Only after the lead confirms their name, acknowledge it once "
                    f"and THEN proceed with the {_pitch_target}. ABSOLUTELY NO project details "
                    "until the name is confirmed.\n"
                    "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                    "If still no answer after two tries, proceed without the name.\n"
                    f"When user confirms name or says 'tell me' / 'go ahead', IMMEDIATELY pitch {_pitch_target}. "
                    "NEVER call end_call in the first 60 seconds of an answered call.\n"
                    "This rule OVERRIDES any conflicting instructions below.\n"
                )
            else:
                _pcm_name_rule = (
                    "\n[STRICT RULE — NAME VERIFICATION — ABSOLUTE — DO NOT SKIP]\n"
                    "The pre-recorded greeting was already played — do NOT repeat your greeting or opening line.\n"
                    "Step 1 (SKIPPED — already played): Pre-recorded greeting delivered.\n"
                    "Step 2: Ask ONCE: \"May I know who I am speaking with, please?\" and then STOP — wait for their response.\n"
                    "Step 3: Only after the lead confirms their name, acknowledge it once "
                    f"and THEN proceed with the {_pitch_target}. ABSOLUTELY NO project details "
                    "until the name is confirmed.\n"
                    "CRITICAL: You ask the name ONCE. If they don't answer, ask ONCE more gently. "
                    "If still no answer after two tries, proceed without the name.\n"
                    f"When user confirms name or says 'tell me' / 'go ahead', IMMEDIATELY pitch {_pitch_target}. "
                    "NEVER call end_call in the first 60 seconds of an answered call.\n"
                    "This rule OVERRIDES any conflicting instructions below.\n"
                )
        # System prompt = the operator's frontend prompt + dynamic call data +
        # operational rules (time, history, language, pacing). No hardcoded
        # persona anchor and no scripted name-verify block — those conflicted
        # with the user's saved prompt (e.g. forced "Vernika" over "Priya").
        system_prompt = (
            _base_system_prompt
            + pacing_rule
            + case_block
            + context_rules
            + time_block
            + history_block
            + detail_block
            + language_enforcement
        )

    if _is_incoming:
        if not agent_name:
            agent_name = "Vernika"
        opening_line = (
            f"Hi, this is {agent_name} from Technopolis Constructions. "
            "Thanks for calling Technopolis Constructions — how can I help you?"
        )
        system_prompt += (
            f"\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
            f"You begin the conversation now. Your first audible reply must follow this scripted "
            f"opening phrase exactly to greet the inbound caller:\n"
            f'"{opening_line}"\n'
            "\n[INBOUND CALL — STRICT]\n"
            f"- The customer called YOU. Say ONLY the opening line above — nothing before it.\n"
            f"- NEVER say 'you enquired about', 'I am calling about', 'Studio Developers', or outbound pitch openers.\n"
            f"- Your name is **{agent_name}** at **Technopolis Constructions** (not Aishi, not any other name).\n"
            "- Do NOT mention Solitaire Unity or project details until after they state their need.\n"
        )
    elif gemini_live_first and opening_line and not str(camp_id or "").startswith("incoming_"):
        system_prompt += (
            "\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
            "You begin the conversation now. Your first audible reply must follow this scripted "
            "opening faithfully (adapt only pacing and natural delivery in the caller's language; "
            "keep names and factual content).\n\""
            + opening_line
            + "\""
        )
    elif gemini_live_first and not str(camp_id or "").startswith("incoming_"):
        _live_first_hints = {
            "sales_1": (
                "[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
                "You begin as **Vernika**, relationship manager at **Technopolis Constructions** (say exactly): "
                "\"Hi, this is Vernika from Technopolis Constructions Private Limited. How are you doing today?\" "
                "— then continue per your persona.\n"
            ),
        }
        hint = _live_first_hints.get(role)
        if not hint and agent_name and role in ("sales_1",):
            hint = (
                f"[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
                f"You begin as **{agent_name}**, relationship manager at **Technopolis Constructions** (say exactly): "
                f"\"Hi, this is {agent_name} from Technopolis Constructions Private Limited. How are you doing today?\" "
                f"— then continue per your persona.\n"
            )
        system_prompt += (
            f"\n\n{hint}"
            if hint
            else (
                "\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
                "You begin the conversation now with one short warm, professional greeting, "
                "then continue per your persona.\n"
            )
        )
    elif opening_line and not _pcm_greeting_will_play and not str(camp_id or "").startswith("incoming_"):
        logger.warning("No pre-warmed audio found for start of call. Forcing Gemini to initiate.")
        system_prompt += (
            f"\n\n[CRITICAL: The automated greeting failed to play. You MUST start the "
            f"conversation yourself immediately. Say: \"{opening_line}\"]"
        )

    if greeting_text:
        opening_line = greeting_text

    if opening_line:
        append_turn(
            live_log_id,
            "assistant",
            opening_line,
            "vobiz-live",
            base_dir=log_dir,
            note="scripted_opening",
        )
        if camp_id and opening_line:
            try:
                from services.campaign_live import push_transcript

                push_transcript(camp_id, "assistant", opening_line)
            except Exception as _ce:
                logger.warning("live transcript push (opening) failed: {}", _ce)

    _prior_opening_bytes_at_connect = len(prior_16k_queue)
    defer_gemini_until_scripted = len(prior_16k_queue) > 0
    opening_script_pcm = bytearray(prior_16k_queue)
    if _prior_opening_bytes_at_connect > 0:
        _post_greeting_grace_until = time.perf_counter() + POST_GREETING_GRACE_SEC
        _greeting_listen_active = GREETING_LISTEN_DURING_PCM
        logger.info(
            "Scripted greeting armed ({} bytes) — post-greeting grace {:.0f}s; "
            "greeting listen loop={}",
            _prior_opening_bytes_at_connect,
            POST_GREETING_GRACE_SEC,
            _greeting_listen_active,
        )

    mix_bg_audio = None
    mix_bg_volume = 0.0
    if getattr(settings, "background_music_enabled", False):
        try:
            bg_path = (getattr(settings, "background_music_path", "") or "").strip()
            bg_vol_raw = getattr(settings, "background_music_volume", 0.0) or 0.0
            vol = float(bg_vol_raw)
            if bg_path and vol > 0:
                mix_bg_audio = load_background_audio(bg_path)
                mix_bg_volume = vol
        except Exception as _bg_err:
            logger.warning("Background music load skipped: {}", _bg_err)

    gemini_url = gemini_live_ws_url()
    try:

        async with DeferredGeminiConnection(
            gemini_url,
            extra_headers=gemini_auth_headers(api_key),
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
        ) as gem:
            _gem_live_session_t0 = time.perf_counter()
            logger.info(
                "Audio bridge: mobile/Vobiz {} Hz L16 ↔ Gemini Live (mic in @16kHz, model out 24kHz→resample→16kHz mobile)",
                VOBIZ_SR,
            )
            system_prompt = apply_live_voice_turn_addon(system_prompt)
            _cb_type_live = ""
            _fu_mem_live: dict = {}
            if isinstance(camp_row, dict):
                _cb_type_live = str(camp_row.get("_callback_type") or "").strip()
                _fu_mem_live = camp_row.get("_follow_up_memory") or {}
                if not isinstance(_fu_mem_live, dict):
                    _fu_mem_live = {}
            system_prompt = apply_site_visit_confirmation_addon(
                system_prompt,
                callback_type=_cb_type_live,
                follow_up_memory=_fu_mem_live,
            )
            system_prompt = apply_anti_loop_closing_addon(system_prompt)

            # Inject conversational turn-taking, quick response, and hello repetition rules
            conversational_rules = (
                "\n\n[CONVERSATIONAL TURN-TAKING — HUMAN SALES CALL]\n"
                "1. When the caller is listening silently, deliver the FULL answer (2–4 short sentences, "
                "one complete thought) in a single turn, then STOP and wait. "
                "Only stop mid-sentence if the caller clearly speaks over you with real words.\n"
                "2. Hello mid-call: 'Yeah, I'm here!' then continue the last topic — never restart pitch or push WhatsApp.\n"
                "3. Human tone: Casual, empathetic, consultative sales executive.\n"
                "4. Active listening: Mirror their concern briefly before you answer.\n"
                "5. One idea per turn. One question max. Finish explaining before waiting — no stacked questions.\n"
                "6. PRIMARY close: site visit this week — NOT WhatsApp on every turn.\n"
                "7. WhatsApp only when they explicitly ask for details on WhatsApp.\n"
                "8. Off-topic STT (food, steak, taxi): assume mishearing — clarify and return to property.\n"
                "9. NEVER repeat the same sentence or question back-to-back. Never call end_call without explicit goodbye.\n"
                "10. FACTS: Use [SYSTEM RAG CONTEXT] for pricing/amenities; never invent numbers. "
                "Solitaire Unity starts from about ₹1.34 crore; 2 BHK 1,225–1,615 sq. ft., 2.5 BHK 1,555 sq. ft., "
                "3 BHK 1,655–2,300 sq. ft. Never invent prices or availability.\n"
                "11. PITCH: Present Solitaire Unity (Kondapur, Hyderabad) — ready to move, OC received, "
                "396 premium apartments, 32,000 sq. ft. clubhouse. Always close toward a site visit.\n"
                "12. EMAIL: Confirm email on file before send_email_details; ask optionally if missing.\n"
                "13. DEV MODE: NEVER say 'developer mode activated' unless real dev mode triggered. "
                "Incomplete codeword → say: 'Say panther chinmay to enter dev mode.'\n"
                "14. SILENCE CHECK-IN: NEVER say 'Are you still there?', 'Still there?', 'Checking in', or "
                "'Everything okay?' on your own. The system sends exactly ONE check-in after 8–10 seconds "
                "of true silence. After that once, wait quietly — do NOT ask again until they speak.\n"
                "15. FACTUAL QUESTIONS (price, location, amenities, phases): start with a tiny 2–4 word ack "
                "('Yeah, sure' / 'Right, so') then answer immediately in the SAME breath — never dead silence, "
                "never 'one moment' or 'please hold'.\n"
            )
            system_prompt += conversational_rules


            from services.voice_finetune import load_voice_finetune_overlay

            _voice_finetune = load_voice_finetune_overlay()
            if _voice_finetune:
                system_prompt += _voice_finetune
                logger.info("Voice fine-tune overlay loaded ({} chars)", len(_voice_finetune))

            if settings.rag_enabled and role in ("sales_1",) and not _rag_embedded_in_prompt:
                try:
                    from services.chunk_rag import connect_digest_for_role, is_chunk_rag

                    if is_chunk_rag():
                        _digest_chars = int(os.getenv("RAG_CONNECT_DIGEST_MAX_CHARS", "30000"))
                        digest = (connect_digest_for_role(role, max_chars=_digest_chars) or "").strip()
                        if digest:
                            system_prompt += "\n\n" + digest
                            _rag_connect_digest_embedded = True
                            logger.info(
                                "Connect KB digest in system prompt ({} chars, role={})",
                                len(digest),
                                role,
                            )
                except Exception as _digest_err:
                    logger.warning("Connect KB digest embed failed: {}", _digest_err)

            # KB embedded in system prompt at connect (8KB) — zero per-turn RAG inject latency.
            if _rag_embedded_in_prompt:
                logger.info(
                    "Live RAG embedded in system prompt ({} chars source) — skipping runtime inject",
                    len(_RAG_SOURCE_TEXT),
                )
            elif _RAG_SOURCE_TEXT and settings.rag_enabled:
                logger.info(
                    "Live RAG enabled ({} chars source) — connect digest + prefetch + ack-first answers",
                    len(_RAG_SOURCE_TEXT),
                )

            # Lead memory continuity: if the worker attached a memory block for
            # this lead, append it so the agent behaves as if it remembers the
            # person (plan Phase 5 — memory-aware dials). When the fast-path
            # block is empty (first-touch or non-orchestration legs), fall back
            # to the stored lead_memory facts so every call gets continuity.
            _mem_block = (data.get("_lead_memory_text") or "").strip()
            if not _mem_block and "[CONTINUITY MEMORY — STORED FACTS" not in system_prompt:
                _mem_block = await _build_continuity_memory_block(
                    camp_row, lead_history, authoritative_name=_authoritative_lead_name,
                )
            if _mem_block:
                system_prompt += "\n\n" + _mem_block
                logger.info(
                    "Lead memory continuity block appended ({} chars, role={})",
                    len(_mem_block),
                    role,
                )

            if _authoritative_lead_name:
                system_prompt = _personalize_prompt_for_active_call(
                    system_prompt,
                    _authoritative_lead_name,
                    pcm_greeting_will_play=(_prior_opening_bytes_at_connect > 0)
                )
                logger.info(
                    "Personalized system prompt for lead first name (len={})",
                    len(_authoritative_lead_name),
                )

            vad_ultra = bool(getattr(settings, "vobiz_ultra_low_latency", False))
            from core.state import resolved_live_language

            _live_lang, _mirror = resolved_live_language(role)
            if _mirror:
                _lang_instruction = (
                    "\n\n[LANGUAGE MIRROR — ABSOLUTE] The caller's language decides your language. "
                    "If they speak Kannada, Telugu, Hindi, Tamil, Hinglish, Tenglish or any Indian "
                    "language, your VERY NEXT reply must be in that language — no English lead-in, "
                    "no announcement, no delay. English is never the default when they speak another "
                    "language. Code-switch like a real local consultant."
                )
            else:
                _lang_instruction = (
                    f"\n\n[LANGUAGE] Speak only in {_live_lang} regardless of the caller's language."
                )
            system_prompt = (system_prompt or "") + _lang_instruction
            setup = build_live_setup(
                model=model,
                system_instruction=system_prompt,
                voice=voice,
                vad_ultra=vad_ultra,
                temperature=None,
                # Mirror ON: no languageCode pin — Gemini auto-detects the caller's
                # language (Kannada/Telugu/Hindi...) instead of assuming en-IN.
                # Mirror OFF: pin to the configured language.
                language=("" if _mirror else _live_lang),
            )

            async def _send_setup_and_kicks() -> None:
                try:
                    await gem.send(json.dumps(setup))
                    logger.info("Gemini Live: setup sent in background (model={}, voice={})", model, voice)
                    if not state.gemini_silence_kick_sent:
                        if _prior_opening_bytes_at_connect == 0:
                            try:
                                await gemini_send_pcm_silence_kick(gem, duration_ms=220)
                                await gemini_send_pcm_silence_kick(gem, duration_ms=80)
                                state.gemini_silence_kick_sent = True
                                logger.info("Gemini Live: early PCM silence kicks sent on connect")
                            except Exception as e:
                                logger.warning("Gemini Live: early silence kick failed: {}", e)
                except Exception as exc:
                    logger.error("Gemini Live setup send failed: {}", exc)

            gemini_setup_ready = asyncio.Event()
            _rag_warm_injected = False
            rag_prefetch_cache: dict[str, str] = {}
            rag_prefetch_inflight: set[str] = set()

            async def _warm_inject_rag_once() -> None:
                nonlocal _rag_warm_injected
                if _rag_warm_injected or _rag_embedded_in_prompt:
                    return
                try:
                    await asyncio.wait_for(gemini_setup_ready.wait(), timeout=8.0)
                except asyncio.TimeoutError:
                    logger.warning("Warm RAG: setupComplete timeout — skipping digest inject")
                    return
                block = live_rag_warm_digest()
                if not block:
                    return
                try:
                    await gemini_send_live_rag(gem, block, turn_complete=False)
                    _rag_warm_injected = True
                    rag_prefetch_cache["__warm__"] = block
                    logger.info("Warm RAG digest injected at connect ({} chars)", len(block))
                except Exception as e:
                    logger.warning("Warm RAG inject failed: {}", e)

            _setup_task = asyncio.create_task(_send_setup_and_kicks())
            _background_tasks.add(_setup_task)
            _setup_task.add_done_callback(_background_tasks.discard)
            _warm_rag_task = asyncio.create_task(_warm_inject_rag_once())
            _background_tasks.add(_warm_rag_task)
            _warm_rag_task.add_done_callback(_background_tasks.discard)

            rec_extra: dict[str, Any] = {
                "recording_source": "vobiz_application_webhook",
                "local_bridge_recording": False,
            }
            append_session_meta(
                live_log_id,
                "vobiz-live",
                path="gemini_live",
                model=model,
                base_dir=log_dir,
                **rec_extra,
            )

            # Live transcript state (inputAudioTranscription; output optional).
            last_in_user = ""
            last_out_assistant = ""
            had_model_audio_turn = False
            last_rag_inject_key = ""
            activity_end_seq = 0
            _user_has_spoken_since_nudge = True

            # While prior_16k_queue still holds opening audio, we drop Gemini model
            # audio so the first words on the line always match the scripted line.

            _outbound_playout_active = False
            _debug_echo_drops = 0
            _debug_echo_forwards = 0
            _debug_first_playout = False
            _echo_rms_threshold = max(
                200, int(getattr(settings, "vobiz_echo_suppress_rms_threshold", 900) or 900)
            )
            _echo_suppress = bool(getattr(settings, "vobiz_echo_suppress_during_playout", False))
            _playout_idle_silence = bool(getattr(settings, "vobiz_playout_idle_silence", False))
            _post_nudge_echo_relax_until = 0.0

            # ----- Task 1: Vobiz -> Gemini (audio in) -----

            async def pump_vobiz_to_gemini() -> None:
                nonlocal last_user_audio_t, vobiz_meta_logged, _last_user_spoke_t, _outbound_playout_active, _post_nudge_echo_relax_until, _debug_echo_drops, _debug_echo_forwards, last_meaningful_t, _vobiz_ws_events_logged, _local_barge_in_requested, _local_barge_in_streak, _user_has_spoken, _user_has_spoken_since_nudge, _voice_confirm_streak, _barge_in_voice_threshold_effective
                connect_t0 = time.perf_counter()
                _dbg_fwd = 0
                _dbg_drop_prior = 0
                _dbg_drop_mute = 0
                _dbg_drop_echo = 0
                # Noise suppression state (per-call)
                _ns_noise_floor: float = 120.0  # initial noise floor guess
                _ns_silence_counter: int = 0
                while True:
                    raw = await _vobiz_incoming.get()
                    if raw is None:
                        logger.info("Vobiz inbound queue closed")
                        return
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        logger.debug("Ignoring malformed JSON in pump_vobiz_to_gemini: {}", raw[:200])
                        continue
                    ev = str(msg.get("event") or "").strip()
                    ev_key = ev.lower().replace("_", "")
                    if ev_key in ("start", "connected", "streamstarted"):
                        start = msg.get("start") or msg.get("stream") or msg
                        state.call_id = str(
                            start.get("callId")
                            or start.get("callUUID")
                            or start.get("call_uuid")
                            or msg.get("callId")
                            or state.call_id
                            or ""
                        )
                        state.stream_id = str(
                            start.get("streamId")
                            or start.get("stream_id")
                            or msg.get("streamId")
                            or state.stream_id
                            or ""
                        )
                        _from_num, _to_num = extract_vobiz_start_numbers(start)
                        _is_incoming = str(camp_id or "").startswith("incoming_")
                        _answered_phone = (_from_num if _is_incoming else _to_num) or phone_lookup
                        if camp_id and camp_id in _CAMPAIGN_DATA and _answered_phone:
                            _CAMPAIGN_DATA[camp_id]["_answered_phone"] = _answered_phone.strip()
                            _CAMPAIGN_DATA[camp_id]["phone"] = _answered_phone.strip()
                        if state.call_id:
                            try:
                                from core.vobiz_credentials import resolve_vobiz_credentials
                                from services.vobiz_bridge.vobiz_recording import register_vobiz_call_mapping

                                _auth_id, _auth_token, _, _ = resolve_vobiz_credentials(role or "sales_1")
                                register_vobiz_call_mapping(
                                    call_uuid=state.call_id,
                                    camp_id=str(camp_id or ""),
                                    log_id=str(live_log_id or ""),
                                    role=str(role or ""),
                                    phone=str(_answered_phone or ""),
                                    auth_id=str(_auth_id or ""),
                                )
                                if _auth_token:
                                    from services.vobiz_bridge.vobiz_client import start_vobiz_call_recording
                                    from core.state import calls_db
                                    callback_url = f"{settings.server_url.rstrip('/')}/vobiz_recording"
                                    if state.call_id not in calls_db:
                                        calls_db[state.call_id] = {}
                                    asyncio.create_task(
                                        start_vobiz_call_recording(
                                            auth_id=_auth_id,
                                            auth_token=_auth_token,
                                            call_uuid=state.call_id,
                                            callback_url=callback_url,
                                        )
                                    )
                            except Exception as _map_err:
                                logger.debug("Vobiz call mapping register or record trigger failed: {}", _map_err)
                        if not vobiz_meta_logged:
                            vobiz_meta_logged = True
                            append_session_meta(
                                live_log_id,
                                "vobiz-live",
                                call_id=state.call_id,
                                stream_id=state.stream_id,
                                base_dir=log_dir,
                            )
                        logger.info(
                            "Vobiz live stream start call={} stream={} fmt={}",
                            state.call_id, state.stream_id, start.get("mediaFormat"),
                        )
                        _mark_vobiz_stream_ready(f"event:{ev or 'start'}", call_id=state.call_id, stream_id=state.stream_id)
                        
                        # Skip silence kick while scripted greeting plays — early kicks can
                        # trigger Gemini turns that race with the post-greeting nudge.
                        if not state.gemini_silence_kick_sent and _prior_opening_bytes_at_connect == 0:
                            logger.info("Vobiz stream started: Gemini PCM silence kick (VAD)")
                            try:
                                await gemini_send_pcm_silence_kick(gem, duration_ms=120)
                                state.gemini_silence_kick_sent = True
                            except Exception as e:
                                logger.warning("Gemini PCM silence kick on stream start failed: {}", e)
                    elif ev == "media":
                        if not vobiz_stream_started.is_set():
                            _mark_vobiz_stream_ready("first_media")
                        media = msg.get("media") or {}
                        b64 = media.get("payload") or ""
                        if not b64:
                            continue
                        try:
                            _in_pcm = base64.b64decode(b64)
                            _in_pcm = vobiz_inbound_pcm_to_le(_in_pcm)
                            # 🎧 Noise suppression: high-pass + adaptive noise gate
                            # Cleans human speech before it reaches Gemini Live or recording.
                            _in_pcm, _ns_noise_floor, _ns_silence_counter = noise_suppress_inbound_pcm(
                                _in_pcm, _ns_noise_floor, _ns_silence_counter,
                            )
                        except Exception:
                            logger.debug("Base64 decode or recording failed in pump_vobiz_to_gemini")
                            _in_pcm = b""
                        if not vobiz_meta_logged:
                            _sr_in = media.get("sampleRate") or media.get("sample_rate") or VOBIZ_SR
                            logger.info(
                                "Vobiz inbound media: sampleRate={} contentType={} (forwarding to Gemini)",
                                _sr_in,
                                media.get("contentType") or media.get("content_type") or "",
                            )
                        last_user_audio_t = time.perf_counter()

                        if len(prior_16k_queue) > 0 and not _prefetch_live_handoff:
                            if not _greeting_listen_active:
                                _dbg_drop_prior += 1
                                continue
                            # Greeting listen loop: forward mic for STT while PCM plays (model audio suppressed).

                        mute_s = max(0.0, settings.vobiz_gemini_live_forward_mute_seconds)
                        if (
                            _prior_opening_bytes_at_connect == 0
                            and mute_s > 0
                            and (time.perf_counter() - connect_t0) < mute_s
                        ):
                            _dbg_drop_mute += 1
                            continue

                        if not _in_pcm:
                            continue

                        # Record inbound (user) audio to local CallRecorder
                        if state.call_recorder is not None and settings.call_recording_enabled:
                            try:
                                state.call_recorder.add_inbound(_in_pcm)
                            except Exception as _rec_err:
                                pass

                        # Local barge-in: stop agent playout when caller speaks over us (confirmed voice).
                        if (
                            LOCAL_BARGE_IN_ENABLED
                            and state.recorded_greeting_nudge_sent
                            and len(prior_16k_queue) == 0
                            and time.perf_counter() >= _barge_in_cooldown_until
                            and (
                                _outbound_playout_active
                                or model_generation_active
                                or len(gemini_16k_queue) > 0
                            )
                        ):
                            _has_voice = pcm_s16le_has_voice(
                                _in_pcm, float(_barge_in_voice_threshold_effective)
                            )
                            if _has_voice:
                                _voice_confirm_streak += 1
                                if _voice_confirm_streak >= VOICE_CONFIRM_STREAK_NEEDED:
                                    _mark_confirmed_user_speech()
                            elif _voice_confirm_streak > 0:
                                _voice_confirm_streak -= 1
                            _mic_rms = pcm_s16le_rms(_in_pcm)
                            # Echo gate: when echo suppression is active AND we're playing out,
                            # raise the barge-in RMS floor to the echo threshold + margin.
                            # This prevents the AI's own voice (PSTN line echo) from
                            # triggering local barge-in and stopping itself mid-sentence.
                            _barge_rms_min = float(LOCAL_BARGE_IN_RMS)
                            if _echo_suppress and (_outbound_playout_active or model_generation_active):
                                _barge_rms_min = max(_barge_rms_min, float(_echo_rms_threshold) + 80.0)
                            if (
                                _has_voice
                                and _mic_rms >= _barge_rms_min
                                and _voice_confirm_streak >= VOICE_CONFIRM_STREAK_NEEDED
                            ):
                                _local_barge_in_streak += 1
                                if _local_barge_in_streak >= LOCAL_BARGE_IN_STREAK:
                                    _local_barge_in_requested = True
                                    _local_barge_in_streak = 0
                            elif _local_barge_in_streak > 0:
                                _local_barge_in_streak -= 1

                        # Keep silence hangup timers alive while callee is speaking (STT can lag on Kannada/Hindi).
                        # Never set _user_has_spoken here — echo during greeting breaks handoff if that flag flips early.
                        if (
                            pcm_s16le_has_voice(_in_pcm, 280.0)
                            and not model_generation_active
                            and len(gemini_16k_queue) == 0
                        ):
                            _now_mic = time.perf_counter()
                            _last_user_spoke_t = _now_mic
                            last_meaningful_t = _now_mic

                        # Echo guard: OFF during live conversation — dropping mic blocked real barge-in.
                        # Only optional for inbound/no-PCM legs when explicitly enabled.
                        if (
                            _echo_suppress
                            and _outbound_playout_active
                            and _prior_opening_bytes_at_connect == 0
                            and time.perf_counter() >= _post_nudge_echo_relax_until
                        ):
                            if _in_pcm and not pcm_s16le_has_voice(_in_pcm, _echo_rms_threshold):
                                _dbg_drop_echo += 1
                                continue

                        await gem.send(json.dumps({
                            "realtimeInput": {
                                "audio": {
                                    "data": b64,
                                    "mimeType": "audio/pcm;rate=16000",
                                }
                            }
                        }))
                        _dbg_fwd += 1
                        # #region agent log
                        if _dbg_fwd <= 3 or _dbg_fwd % 100 == 0:
                            try:
                                from debug_agent_log import agent_debug

                                _raw_rms = pcm_s16le_rms(_in_pcm)
                                agent_debug(
                                    "BC",
                                    "live_session.py:pump_vobiz_to_gemini",
                                    "mic_forwarded_to_gemini",
                                    {
                                        "fwd_total": _dbg_fwd,
                                        "drop_prior": _dbg_drop_prior,
                                        "drop_mute": _dbg_drop_mute,
                                        "drop_echo": _dbg_drop_echo,
                                        "raw_rms": round(_raw_rms, 1),
                                        "sent_variant": "raw_vobiz_audio",
                                        "echo_suppress": _echo_suppress,
                                        "prior_q_now": len(prior_16k_queue),
                                    },
                                )
                            except Exception:
                                pass
                        # #endregion
                    elif ev == "stop":
                        logger.info("Vobiz live stream stop: {}", msg.get("reason"))
                        return
                    elif ev and _vobiz_ws_events_logged < 6:
                        _vobiz_ws_events_logged += 1
                        logger.info(
                            "Vobiz WS event (unhandled): event={!r} keys={}",
                            ev,
                            list(msg.keys())[:10],
                        )

            # ----- Task 2 & 3: Gemini -> Queue -> Vobiz (mixed audio out + transcripts) -----
            pending_audio_24k = bytearray()
            gemini_16k_queue = bytearray()
            _greeting_handoff_pcm_buffer = bytearray()
            gemini_resample_state = None
            _local_barge_in_requested = False
            _local_barge_in_streak = 0
            _last_respond_nudge_at = 0.0
            _last_model_spoke_t = 0.0
            _last_dab_stt_key = ""
            _last_dab_at = 0.0
            _barge_in_cooldown_until = 0.0
            _agent_last_finished_t = 0.0
            _last_silence_nudge_at = 0.0
            _user_silence_nudge_count = 0
            _confirmed_user_speech_at = 0.0
            _agent_turn_audio_started_at = 0.0
            _voice_confirm_streak = 0
            _barge_in_voice_threshold_effective = BARGE_IN_VOICE_THRESHOLD
            _spurious_interrupt_count = 0
            _spurious_interrupt_window_start = 0.0
            _silence_checkin_spoken_count = 0
            _continue_explanation_nudge_sent = False
            _ai_disclosure_nudge_sent = False
            _last_turn_pcm_bytes_24k = 0
            _commission_delivered = False
            _features_pitch_delivered = False
            _account_manager_cta_count = 0
            _commission_pitch_nudge_sent = False
            _anti_refusal_recovery_count = 0
            _refusal_detected_this_turn = False
            _refusal_audio_block = False
            _barge_in_drop_audio = False
            _turn45_merged = False
            _cp_qa_mode = False
            _cta_bumped_this_turn = False
            _cp_complaint_nudge_sent = False
            _cp_units_nudge_sent_for_stt = ""
            _cp_refusal_apology_sent = False
            _user_turn_nudge_sent = False
            _nudge_suppressed_until = 0.0
            _activity_end_nudge_seq = 0
            _barge_in_drop_audio_at = 0.0
            _monologue_audio_block = False
            _expected_assistant_transcript = ""

            async def _flush_blocked_cta_audio() -> None:
                nonlocal last_out_assistant, model_generation_active, _nudge_suppressed_until
                pending_audio_24k.clear()
                gemini_16k_queue.clear()
                _greeting_handoff_pcm_buffer.clear()
                model_generation_active = False
                _nudge_suppressed_until = time.perf_counter() + 0.35
                last_out_assistant = _strip_account_manager_cta(last_out_assistant)
                _pending_transcript["assistant"] = last_out_assistant
                try:
                    await vobiz_send_clear_audio(ws, stream_id=state.stream_id or "")
                except Exception as _cta_clr_err:
                    logger.warning("Blocked CTA clear_audio failed: {}", _cta_clr_err)

            async def _flush_monologue_audio() -> None:
                nonlocal last_out_assistant, model_generation_active, _monologue_audio_block, _nudge_suppressed_until
                _monologue_audio_block = True
                pending_audio_24k.clear()
                gemini_16k_queue.clear()
                _greeting_handoff_pcm_buffer.clear()
                model_generation_active = False
                _nudge_suppressed_until = time.perf_counter() + 1.2
                last_out_assistant = _truncate_cp_monologue(last_out_assistant)
                _pending_transcript["assistant"] = last_out_assistant
                try:
                    await vobiz_send_clear_audio(ws, stream_id=state.stream_id or "")
                except Exception as _mono_clr_err:
                    logger.warning("Monologue clear_audio failed: {}", _mono_clr_err)

            def _track_account_manager_cta(source: str) -> None:
                nonlocal _account_manager_cta_count, _turn45_merged, _cp_qa_mode, _cta_bumped_this_turn
                if _cta_bumped_this_turn:
                    return
                _account_manager_cta_count = min(
                    _ACCOUNT_MANAGER_CTA_MAX,
                    _account_manager_cta_count + 1,
                )
                _turn45_merged = False
                _cp_qa_mode = True
                _cta_bumped_this_turn = True
                logger.info(
                    "Account Manager CTA detected via {} (count={}/{})",
                    source,
                    _account_manager_cta_count,
                    _ACCOUNT_MANAGER_CTA_MAX,
                )

            def _should_block_cta_in_output(text: str) -> bool:
                if not _is_cp_campaign_role(role):
                    return False
                if _looks_like_account_manager_confirmation(text):
                    return False
                # Commission-protection: if the assistant already delivered the real commission
                # numbers (3%/~12 Lakh/vouchers) and the caller asked about commission, do NOT
                # flush the whole turn. Flushing deletes the exact answer they asked for and shows
                # as '[no text transcript]' every time. Only the trailing Account-Manager CTA is
                # stripped by the caller site; the commission audio must reach the caller.
                if _looks_like_commission_delivered(text) and _account_manager_cta_count < _ACCOUNT_MANAGER_CTA_MAX:
                    return False
                if _looks_like_cta_fragment(text):
                    last_user = (_last_user_stt_snippet or last_in_user or "").strip()
                    if _cp_qa_mode or _looks_like_question(last_user) or _user_asked_factual_question(last_user):
                        return True
                    if not _user_ready_for_account_manager_cta(last_user):
                        return True
                if not _looks_like_account_manager_cta_asked(text):
                    return False
                if _account_manager_cta_count >= _ACCOUNT_MANAGER_CTA_MAX:
                    return True
                last_user = (_last_user_stt_snippet or last_in_user or "").strip()
                # Factual Q&A (price/units/features) — never append CTA, even on first ask
                if _cp_qa_mode or _user_asked_factual_question(last_user) or _looks_like_question(last_user):
                    if not _user_ready_for_account_manager_cta(last_user):
                        return True
                if _account_manager_cta_count >= 1 and _looks_like_question(last_user):
                    return True
                if (
                    _account_manager_cta_count >= 1
                    and _cp_qa_mode
                    and not _user_ready_for_account_manager_cta(last_user)
                ):
                    return True
                return False

            async def _flush_refusal_audio_now() -> None:
                nonlocal last_out_assistant, model_generation_active, _refusal_audio_block, _refusal_detected_this_turn
                _refusal_audio_block = True
                _refusal_detected_this_turn = True
                pending_audio_24k.clear()
                gemini_16k_queue.clear()
                _greeting_handoff_pcm_buffer.clear()
                model_generation_active = False
                last_out_assistant = ""
                try:
                    await vobiz_send_clear_audio(ws, stream_id=state.stream_id or "")
                except Exception as _clr_err:
                    logger.warning("Refusal leak clear_audio failed: {}", _clr_err)

            def _mark_confirmed_user_speech() -> None:
                nonlocal _confirmed_user_speech_at, _voice_confirm_streak
                _confirmed_user_speech_at = time.perf_counter()
                _voice_confirm_streak = 0

            def _user_speech_confirmed_recent() -> bool:
                return _confirmed_user_speech_at > 0 and (
                    time.perf_counter() - _confirmed_user_speech_at
                ) < CONFIRMED_SPEECH_WINDOW_SEC

            def _note_spurious_interrupt() -> None:
                nonlocal _spurious_interrupt_count, _spurious_interrupt_window_start, _barge_in_voice_threshold_effective
                _now_si = time.perf_counter()
                if _now_si - _spurious_interrupt_window_start > 30.0:
                    _spurious_interrupt_window_start = _now_si
                    _spurious_interrupt_count = 0
                _spurious_interrupt_count += 1
                if _spurious_interrupt_count >= 3:
                    _barge_in_voice_threshold_effective = min(
                        950, _barge_in_voice_threshold_effective + 80
                    )
                    logger.info(
                        "Call heal: interrupt storm — raised barge-in voice threshold to {}",
                        _barge_in_voice_threshold_effective,
                    )
                    _spurious_interrupt_count = 0
                    _spurious_interrupt_window_start = _now_si

            def _live_model_out_queue() -> bytearray:
                """During scripted greeting, buffer live model PCM for instant handoff."""
                if len(prior_16k_queue) > POST_GREETING_EARLY_NUDGE_BYTES:
                    return _greeting_handoff_pcm_buffer
                return gemini_16k_queue

            def _merge_greeting_handoff_buffer() -> None:
                if not _greeting_handoff_pcm_buffer:
                    return
                _buf_len = len(_greeting_handoff_pcm_buffer)
                if gemini_16k_queue:
                    gemini_16k_queue[:0] = _greeting_handoff_pcm_buffer
                else:
                    gemini_16k_queue.extend(_greeting_handoff_pcm_buffer)
                _greeting_handoff_pcm_buffer.clear()
                logger.info(
                    "Greeting handoff: merged {} bytes buffered live PCM (playout q={})",
                    _buf_len,
                    len(gemini_16k_queue),
                )

            async def _apply_local_barge_in(reason: str) -> None:
                nonlocal gemini_resample_state, model_generation_active, last_rag_inject_key, activity_end_seq, _user_has_spoken, _user_has_spoken_since_nudge, _last_user_spoke_t, _barge_in_cooldown_until, last_out_assistant, had_model_audio_turn, _barge_in_drop_audio
                if not _user_speech_confirmed_recent():
                    logger.debug("Local barge-in skipped — user speech not confirmed ({})", reason)
                    return
                if not (
                    _outbound_playout_active
                    or len(gemini_16k_queue) > 0
                    or model_generation_active
                ):
                    return
                logger.info(
                    "Local barge-in: {} (gem_q={} model_active={})",
                    reason,
                    len(gemini_16k_queue),
                    model_generation_active,
                )
                _user_has_spoken = True
                _user_has_spoken_since_nudge = True
                _last_user_spoke_t = time.perf_counter()
                _mark_confirmed_user_speech()
                _barge_in_cooldown_until = time.perf_counter() + 0.45
                _barge_in_drop_audio = True
                _barge_in_drop_audio_at = time.perf_counter()
                gemini_16k_queue.clear()
                _greeting_handoff_pcm_buffer.clear()
                pending_audio_24k.clear()
                gemini_resample_state = None
                model_generation_active = False
                last_out_assistant = ""
                had_model_audio_turn = False
                last_rag_inject_key = ""
                activity_end_seq += 1
                try:
                    await vobiz_send_clear_audio(ws, stream_id=state.stream_id or "")
                except Exception as _bi_err:
                    logger.warning("Local barge-in clear_audio failed: {}", _bi_err)

            async def _cp_script_step_hint(stt: str = "") -> str:
                _stt_hint = (stt or _last_user_stt_snippet or last_in_user or "").strip()
                # Explicit commission request wins over generic QA-mode: the caller
                # literally asked for the commission pitch (Turn 4). Do not route that to
                # 'FORBIDDEN: commission pitch' QA mode, or the model echoes and the caller
                # never hears the numbers.
                _stt_hint_low = _stt_hint.lower()
                _wants_commission = bool(
                    re.search(r"commission", _stt_hint_low)
                    and _user_ready_for_commission_pitch(_stt_hint)
                )
                if _wants_commission and not _commission_delivered:
                    return ("Turn 4 \u2014 commission only: 3%, ~12 Lakhs, vouchers, walk-in bonus. "
                            "Deliver the commission numbers now. Then STOP. Do NOT ask Account Manager yet.")
                # Factual Q&A always wins over Turn 5 CTA push
                if (
                    _cp_qa_mode
                    or _user_asked_factual_question(_stt_hint)
                    or (_looks_like_question(_stt_hint) and not _user_ready_for_account_manager_cta(_stt_hint))
                ):
                    return (
                        "Answer-only / QA mode — answer their question in ONE sentence from KB. "
                        "FORBIDDEN: Account Manager, 'connect with you', commission pitch unless they asked. "
                        f"CTA already asked {_account_manager_cta_count}/{_ACCOUNT_MANAGER_CTA_MAX} times."
                    )
                if not _name_confirmed and not _name_verify_asked:
                    return "Turn 1 — confirm you are speaking with the right person (name check)."
                if not _features_pitch_delivered:
                    if not _pitch_delivered and not _commission_delivered:
                        return "Turn 2 or 3 — intro to Solitaire Unity / Solitaire Unity features (natural, from KB)."
                    return "Turn 3 — explain Solitaire Unity features (elevator, pool, pergola) from KB."
                if not _commission_delivered:
                    return "Turn 4 — commission only: 3%, ~12 Lakhs, vouchers, walk-in bonus. Then STOP. Do NOT ask Account Manager yet."
                if _account_manager_cta_count >= _ACCOUNT_MANAGER_CTA_MAX:
                    return (
                        "Close warmly — thank them for their time. "
                        "Answer any follow-up from KB only. "
                        "Do NOT ask Account Manager again (already asked twice)."
                    )
                if _cp_qa_mode and _account_manager_cta_count >= 1:
                    return (
                        "Answer-only / QA mode — answer their question in ONE sentence from KB. "
                        f"Do NOT ask Account Manager connect (already asked {_account_manager_cta_count}/{_ACCOUNT_MANAGER_CTA_MAX} times)."
                    )
                if _account_manager_cta_count == 0:
                    return (
                        "Turn 5 only — Account Manager question. "
                        "Ask ONLY: 'Would you like our CP Account Manager to connect with you?' "
                        f"(max {_ACCOUNT_MANAGER_CTA_MAX} times total). "
                        "Do NOT repeat commission. Do NOT schedule a site visit."
                    )
                if _account_manager_cta_count == 1:
                    return (
                        "Second Account Manager ask allowed ONLY if they did not answer the first. "
                        "Otherwise answer their KB question in one sentence — no CTA repeat."
                    )
                return (
                    "Continue naturally — answer their question using KB facts only. "
                    "Do NOT restart the intro. Do NOT repeat commission unless they ask."
                )

            async def _maybe_respond_nudge(stt_text: str, *, source: str, user_turn: bool = False) -> None:
                nonlocal _last_respond_nudge_at, _last_dab_stt_key, _last_dab_at, _cp_qa_mode, _user_turn_nudge_sent, _expected_assistant_transcript
                _stt = (stt_text or "").strip()
                if not _stt:
                    return
                _is_user_turn_source = user_turn or source in (
                    "turn_kick",
                    "dead_air",
                    "ghost_turn",
                    "stall_recover",
                    "kb_embedded_kick",
                )
                if _user_turn_nudge_sent and not _is_user_turn_source:
                    logger.debug("Respond-now skipped (turn nudge already sent, source={})", source)
                    return
                if time.perf_counter() < _nudge_suppressed_until and not _is_user_turn_source:
                    logger.debug("Respond-now skipped (nudge suppressed after CTA flush, source={})", source)
                    return
                # Enter QA mode on any CP factual question (not only post-commission)
                if _is_cp_campaign_role(role) and (
                    _user_asked_factual_question(_stt)
                    or _looks_like_question(_stt)
                    or (_commission_delivered and _looks_like_question(_stt))
                ):
                    _cp_qa_mode = True
                if time.perf_counter() < _post_hello_grace_until:
                    logger.debug("Respond-now skipped (post-hello grace, source={})", source)
                    return
                _is_checkin = _stt_is_checkin_only(_stt)
                if _is_cp_campaign_role(role) and _stt_is_audio_checkin(_stt):
                    from .gemini_protocol import gemini_send_cp_presence_ack_nudge

                    await gemini_send_cp_presence_ack_nudge(
                        gem,
                        agent_name=agent_name or "Vernika",
                        user_stt=_stt,
                        ask_account_manager=False,
                    )
                    _user_turn_nudge_sent = True
                    logger.info("CP presence ack (source={}, stt={!r})", source, _stt[:80])
                    return
                if _is_checkin:
                    logger.debug("Respond-now skipped (check-in only STT, source={})", source)
                    return
                if _last_nudge_kind == "hello" and (time.perf_counter() - _hello_nudge_sent_at) < POST_HELLO_GRACE_SEC:
                    logger.debug("Respond-now skipped (recent hello nudge, source={})", source)
                    return
                _recent_model_guard = 0.05 if _is_user_turn_source else (
                    0.8 if role in ("sales_1",) else 0.5
                )
                if (time.perf_counter() - _last_model_spoke_t) < _recent_model_guard:
                    logger.debug(
                        "Respond-now skipped (model spoke {:.2f}s ago, guard={:.2f}s, source={})",
                        time.perf_counter() - _last_model_spoke_t,
                        _recent_model_guard,
                        source,
                    )
                    return
                if model_generation_active:
                    logger.debug("Respond-now skipped (model active, source={})", source)
                    return
                if len(gemini_16k_queue) > 0:
                    if not _is_user_turn_source:
                        logger.debug("Respond-now skipped (model queued, source={})", source)
                        return
                    if len(gemini_16k_queue) >= _MIN_AUDIBLE_MODEL_PCM_16K:
                        logger.debug(
                            "Respond-now skipped (audible model queue, source={})",
                            source,
                        )
                        return
                _key = _stt.lower()[:100]
                _now_rn = time.perf_counter()
                if _key == _last_dab_stt_key and (_now_rn - _last_dab_at) < 20.0:
                    logger.debug("Respond-now skipped (duplicate stt, source={})", source)
                    return
                if (_now_rn - _last_respond_nudge_at) < (
                    0.35 if _is_user_turn_source else RESPOND_NUDGE_COOLDOWN_SEC
                ):
                    logger.debug("Respond-now skipped (cooldown {:.1f}s, source={})", _now_rn - _last_respond_nudge_at, source)
                    return
                _last_respond_nudge_at = _now_rn
                _last_dab_stt_key = _key
                _last_dab_at = _now_rn
                _user_turn_nudge_sent = True

                if (
                    _is_cp_campaign_role(role)
                    and _account_manager_cta_count >= 1
                    and _user_ready_for_account_manager_cta(_stt)
                ):
                    from .gemini_protocol import gemini_send_account_manager_acceptance_nudge

                    _expected_assistant_transcript = (
                        "Great, our CP Account Manager will connect with you shortly. "
                        "Thank you for your time."
                    )
                    await gemini_send_account_manager_acceptance_nudge(
                        gem,
                        agent_name=agent_name or "Vernika",
                    )
                    logger.info("CP Account Manager handoff accepted (source={}, stt={!r})", source, _stt[:80])
                    return

                # Dedicated answer-only nudges for price/units (no CTA)
                if _is_cp_campaign_role(role) and _stt_asks_pricing_recap(_stt):
                    from .gemini_protocol import gemini_send_anti_refusal_account_manager_nudge

                    await gemini_send_anti_refusal_account_manager_nudge(
                        gem,
                        agent_name=agent_name or "Vernika",
                        recovery_context="pricing_question",
                        include_cta=False,
                    )
                    logger.info("CP pricing answer nudge (source={}, stt={!r})", source, _stt[:80])
                    return
                if _is_cp_campaign_role(role) and _stt_asks_units_inventory(_stt):
                    from .gemini_protocol import gemini_send_cp_units_inventory_nudge

                    await gemini_send_cp_units_inventory_nudge(
                        gem,
                        agent_name=agent_name or "Vernika",
                        user_stt=_stt,
                        include_cta=False,
                    )
                    logger.info("CP units answer nudge (source={}, stt={!r})", source, _stt[:80])
                    return

                if role in ("sales_1",):
                    from .gemini_protocol import gemini_send_natural_continue_nudge

                    _step = await _cp_script_step_hint(_stt)
                    await gemini_send_natural_continue_nudge(
                        gem,
                        agent_name=agent_name or "Vernika",
                        user_stt=_stt,
                        script_step=_step,
                    )
                else:
                    await gemini_send_respond_now_nudge(gem, user_stt=_stt)
                logger.info("Respond-now nudge (source={}, stt={!r})", source, _stt[:80])
            _resample_chunk_ms = max(10.0, float(getattr(settings, "gemini_live_resample_chunk_ms", 12.0) or 12.0))
            _min_emit_ms = float(getattr(settings, "gemini_tts_min_emit_ms", 15) or 15)
            _MIN_AUDIBLE_MODEL_PCM_24K = max(960, int(24000 * 2 * _min_emit_ms / 1000.0))
            _MIN_AUDIBLE_MODEL_PCM_16K = max(640, int(VOBIZ_SR * 2 * 0.02))
            model_generation_active = False

            bg_audio = mix_bg_audio
            bg_volume = mix_bg_volume

            # RAG prefetch cache: key = user query text, value = prebuilt KB block.
            # Populated on inputTranscription deltas in a worker thread so the context is
            # ready the instant activityEnd fires — no SQLite FTS on the hot path.

            async def _prefetch_rag(q: str) -> None:
                if not live_rag_context or not q or q in rag_prefetch_cache or q in rag_prefetch_inflight:
                    return
                rag_prefetch_inflight.add(q)
                try:
                    block = await asyncio.to_thread(live_rag_context, q)
                    rag_prefetch_cache[q] = (block or "").strip()
                except Exception as e:
                    logger.warning("Live RAG prefetch failed: {}", e)
                finally:
                    rag_prefetch_inflight.discard(q)

            def _kb_ready_at_connect() -> bool:
                """True when Gemini already has authoritative KB in system prompt or warm inject."""
                return bool(
                    _rag_embedded_in_prompt
                    or _rag_connect_digest_embedded
                    or _rag_warm_injected
                )

            async def _try_inject_live_rag(reason: str) -> None:
                """Factual question: optional extra RAG block — skipped when KB already at connect."""
                nonlocal last_rag_inject_key
                if len(prior_16k_queue) > 0:
                    return
                if (
                    settings.gemini_live_skip_blocking_rag_when_kb_embedded
                    and _kb_ready_at_connect()
                ):
                    logger.debug(
                        "RAG question turn skipped — KB already at connect ({})",
                        reason,
                    )
                    return
                q = (last_in_user or "").strip()
                if len(q) < 2 or q == last_rag_inject_key:
                    return
                if not _looks_like_question(q) or _is_short_ack(q):
                    return
                if model_generation_active or len(gemini_16k_queue) >= _MIN_AUDIBLE_MODEL_PCM_16K:
                    logger.debug("RAG question turn skipped — model already speaking ({})", reason)
                    return

                block = ""
                if live_rag_context and not _rag_embedded_in_prompt:
                    block = rag_prefetch_cache.get(q)
                    if block is None:
                        try:
                            block = (await asyncio.to_thread(live_rag_context, q) or "").strip()
                            rag_prefetch_cache[q] = block
                        except Exception as e:
                            logger.warning("Live RAG lookup failed ({}): {}", reason, e)
                            block = ""

                last_rag_inject_key = q
                try:
                    await gemini_send_rag_question_turn(gem, user_stt=q, rag_block=block or "")
                    logger.info(
                        "Gemini Live: RAG question turn ({} chars kb) [{}]",
                        len(block or ""),
                        reason,
                    )
                    append_artifact(
                        live_log_id,
                        "vobiz-live",
                        "rag_question_turn",
                        f"{len(block or '')} chars kb",
                        base_dir=log_dir,
                        stt_query_preview=q[:500],
                    )
                except Exception as e:
                    logger.warning("Gemini Live: RAG question turn failed ({}): {}", reason, e)
                    last_rag_inject_key = ""

            async def _classify_callee_during_greeting(stt: str) -> None:
                """Update human vs automated classification while prerecorded greeting plays."""
                nonlocal _callee_class, _callee_vm_kind, _greeting_stt_snippets
                snippet = (stt or "").strip()
                if not snippet:
                    return
                _greeting_stt_snippets.append(snippet[:240])
                if _callee_class == "automated":
                    return
                callee, vm_kind = classify_callee_from_stt(snippet)
                if callee == "automated" and vm_kind:
                    _callee_class = "automated"
                    _callee_vm_kind = vm_kind
                    logger.warning(
                        "Greeting listen: AUTOMATED callee ({}) — stt={!r}",
                        vm_kind,
                        snippet[:100],
                    )
                elif callee == "human" and _callee_class != "human":
                    _callee_class = "human"
                    logger.info("Greeting listen: HUMAN callee — stt={!r}", snippet[:100])

            async def _greeting_listen_loop() -> None:
                """Poll while PCM greeting plays; callee class comes from live STT."""
                if not _greeting_listen_active or _prior_opening_bytes_at_connect == 0:
                    return
                logger.info("Greeting listen loop: monitoring human vs automated during prerecorded PCM")
                _last_log = ""
                while len(prior_16k_queue) > 0:
                    await asyncio.sleep(0.3)
                    if _callee_class != _last_log:
                        _last_log = _callee_class
                        logger.info(
                            "Greeting listen: class={} vm_kind={} snippets={}",
                            _callee_class,
                            _callee_vm_kind or "-",
                            len(_greeting_stt_snippets),
                        )
                logger.info(
                    "Greeting listen loop ended — final_class={} vm_kind={}",
                    _callee_class,
                    _callee_vm_kind or "-",
                )

            async def _arm_voicemail_message() -> None:
                nonlocal _vm_phase, _vm_message_armed, SILENCE_HANGUP_SEC
                if _vm_message_armed or _vm_phase == "message":
                    return
                _vm_message_armed = True
                _vm_phase = "message"
                SILENCE_HANGUP_SEC = 12.0
                try:
                    await gemini_send_voicemail_beep_message_nudge(
                        gem, agent_name or "Vernika"
                    )
                    logger.info("Voicemail: beep/message nudge sent (agent={})", agent_name)
                except Exception as _vbe:
                    logger.warning("Voicemail beep message nudge failed: {}", _vbe)

            async def _enter_voicemail_mode(kind: str, *, source: str, stt: str = "") -> None:
                nonlocal _is_voicemail_mode, _voicemail_triggered, _vm_phase, _vm_wait_until, SILENCE_HANGUP_SEC
                if _voicemail_triggered and _vm_phase == "message":
                    return
                _is_voicemail_mode = True
                _voicemail_triggered = True
                if camp_id and camp_id in _CAMPAIGN_DATA:
                    _CAMPAIGN_DATA[camp_id]["is_voicemail"] = True
                logger.warning(
                    "Voicemail mode entered ({}, kind={}, stt={!r})",
                    source,
                    kind,
                    (stt or "")[:120],
                )
                if kind == "beep":
                    await _arm_voicemail_message()
                    return
                # screening — state reason, wait for human
                _vm_phase = "screening"
                _vm_wait_until = time.perf_counter() + VOICEMAIL_HUMAN_WAIT_SEC
                SILENCE_HANGUP_SEC = 20.0
                try:
                    await gemini_send_voicemail_screening_nudge(
                        gem, agent_name or "Vernika"
                    )
                except Exception as _vse:
                    logger.warning("Voicemail screening nudge failed: {}", _vse)

            async def _voicemail_flow_watchdog() -> None:
                """After screening nudge, leave voicemail if no human takes over."""
                while True:
                    await asyncio.sleep(0.4)
                    if not _is_voicemail_mode or _name_confirmed:
                        return
                    if _vm_phase != "screening":
                        continue
                    if time.perf_counter() < _vm_wait_until:
                        continue
                    logger.info("Voicemail: human wait expired — leaving message")
                    await _arm_voicemail_message()

            async def _voicemail_no_human_watchdog() -> None:
                """Silent pickup after connect — no response, NOT voicemail (VM needs STT/carrier prompt)."""
                while True:
                    await asyncio.sleep(1.0)
                    if _voicemail_triggered or _name_confirmed or _is_voicemail_mode:
                        return
                    if not _opening_delivered or not state.recorded_greeting_nudge_sent:
                        continue
                    age = time.perf_counter() - _call_connect_time
                    if age < VOICEMAIL_NO_HUMAN_SEC:
                        continue
                    silence = time.perf_counter() - _last_user_spoke_t
                    if silence >= 10.0 and not _name_confirmed and not _user_has_spoken:
                        if camp_id and camp_id in _CAMPAIGN_DATA:
                            _CAMPAIGN_DATA[camp_id]["silent_no_response"] = True
                        logger.info(
                            "Silent pickup {:.0f}s after connect (no name confirm, {:.0f}s since callee speech) — "
                            "switching to smooth voicemail message instead of stutter-prone limbo",
                            age,
                            silence,
                        )
                        # Leave ONE smooth message instead of letting the model monologue
                        # in fragmented bursts for minutes on a silent line (the mid-sentence
                        # 4-8s stops callers complained about).
                        await _enter_voicemail_mode("no_human", source="silent_pickup_fallback")
                        return

            def _clear_post_greeting_grace() -> None:
                nonlocal _post_greeting_grace_until
                _post_greeting_grace_until = time.perf_counter()

            def _arm_post_greeting_grace(seconds: float) -> None:
                nonlocal _post_greeting_grace_until
                cap = min(max(0.0, seconds), POST_GREETING_GRACE_SEC)
                _post_greeting_grace_until = time.perf_counter() + cap

            async def _defer_post_name_pitch(stt_text: str) -> None:
                """Wait briefly — if Gemini already replied to user audio, skip duplicate pitch nudge."""
                nonlocal _post_name_pitch_nudge_sent, _pitch_delivered
                _clear_post_greeting_grace()
                await asyncio.sleep(POST_NAME_PITCH_DEFER_SEC)
                if _pitch_delivered or _is_voicemail_mode or _voicemail_triggered:
                    return
                if _response_started_after_user or model_generation_active:
                    logger.info(
                        "Post-name pitch: model already responding — skip duplicate nudge (stt={!r})",
                        (stt_text or "")[:80],
                    )
                    return
                if len(gemini_16k_queue) >= _MIN_AUDIBLE_MODEL_PCM_16K:
                    logger.info("Post-name pitch: audio already queued — skip duplicate nudge")
                    return
                if _post_name_pitch_nudge_sent:
                    return
                _post_name_pitch_nudge_sent = True
                try:
                    await gemini_send_post_name_confirm_pitch_nudge(
                        gem,
                        lead_name=_authoritative_lead_name or "",
                        user_stt=stt_text,
                        role=role,
                    )
                    logger.info(
                        "Gemini Live: deferred post-name pitch nudge (stt={!r})",
                        (stt_text or "")[:80],
                    )
                except Exception as _pn_err:
                    logger.warning("Deferred post-name pitch nudge failed: {}", _pn_err)

            async def pump_gemini_to_queue() -> None:
                nonlocal response_t0, last_in_user, last_out_assistant, had_model_audio_turn, last_rag_inject_key, activity_end_seq, last_meaningful_t, last_user_audio_t, _last_user_spoke_t, _user_has_spoken, _opening_delivered, _is_voicemail_mode, _voicemail_triggered, SILENCE_HANGUP_SEC, gemini_resample_state, model_generation_active, _user_has_spoken_since_nudge, _last_user_stt_snippet, _hello_nudge_sent_at, _suppress_gemini_until_nudge, _post_greeting_grace_until, _name_confirmed, _pitch_delivered, _post_name_pitch_nudge_sent, _hello_nudge_count, _name_verify_asked, _last_activity_end_t, _response_started_after_user, _vm_phase, _vm_wait_until, _callee_class, _callee_vm_kind, _greeting_stt_snippets, _dev_mode_active, _dev_mode_nudge_sent, _phase3_nudge_sent, _local_barge_in_requested, _last_model_spoke_t, _barge_in_cooldown_until, _agent_last_finished_t, _agent_turn_audio_started_at, _last_silence_nudge_at, _user_silence_nudge_count, _last_nudge_kind, _fake_dev_block_sent, _confirmed_user_speech_at, _silence_checkin_spoken_count, _continue_explanation_nudge_sent, _ai_disclosure_nudge_sent, _last_turn_pcm_bytes_24k, _commission_delivered, _features_pitch_delivered, _account_manager_cta_count, _commission_pitch_nudge_sent, _anti_refusal_recovery_count, _refusal_audio_block, _refusal_detected_this_turn, _barge_in_drop_audio, _barge_in_drop_audio_at, _turn45_merged, _cp_qa_mode, _cta_bumped_this_turn, _cp_complaint_nudge_sent, _cp_units_nudge_sent_for_stt, _cp_refusal_apology_sent, _user_turn_nudge_sent, _monologue_audio_block, _authoritative_lead_name, _expected_assistant_transcript, _activity_end_nudge_seq
                first_byte_logged = False
                _model_turn_pcm_bytes_24k = 0
                _goaway_nudge_sent = False
                await gem.connected_event.wait()
                if gem.conn_error:
                    logger.error(
                        "Gemini Live: WS connection FAILED — conversation cannot start. "
                        "Error: {} | Model: {} | Check GEMINI_LIVE_MODEL and GEMINI_API_KEY.",
                        gem.conn_error,
                        model,
                    )
                    return
                async for raw in gem:
                    if _local_barge_in_requested:
                        _local_barge_in_requested = False
                        await _apply_local_barge_in("mic_streak")
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                    except Exception:
                        logger.debug("Ignoring malformed JSON from Gemini Live")
                        continue
                    if obj.get("error"):
                        logger.error("Gemini Live upstream error: {}", obj.get("error"))
                    if obj.get("setupComplete") is not None and not gemini_setup_ready.is_set():
                        gemini_setup_ready.set()
                        logger.info("Gemini Live: setupComplete received")
                    if obj.get("goAway"):
                        logger.warning("Gemini Live goAway: {}", obj.get("goAway"))
                        if not _goaway_nudge_sent and _opening_delivered:
                            _goaway_nudge_sent = True
                            _goaway_stt = (_last_user_stt_snippet or "").strip()

                            async def _goaway_keepalive() -> None:
                                try:
                                    await gemini_send_respond_now_nudge(
                                        gem, user_stt=_goaway_stt or "continue the conversation"
                                    )
                                    logger.info("Gemini Live: goAway keepalive nudge sent")
                                except Exception as _gw_err:
                                    logger.warning("goAway keepalive nudge failed: {}", _gw_err)

                            _task_gw = asyncio.create_task(_goaway_keepalive())
                            _background_tasks.add(_task_gw)
                            _task_gw.add_done_callback(_background_tasks.discard)

                    # Top-level or nested tool/function calls (Gemini Live emits these at root,
                    # under serverContent, or inside modelTurn.parts).
                    tc = obj.get("toolCall") or (obj.get("serverContent") or {}).get("toolCall") or {}
                    fn_calls = list(tc.get("functionCalls") or tc.get("function_calls") or [])
                    _mt_parts = ((obj.get("serverContent") or {}).get("modelTurn") or {}).get("parts") or []
                    for _p in _mt_parts:
                        _p_fc = _p.get("functionCall") or _p.get("function_call")
                        if _p_fc and isinstance(_p_fc, dict) and _p_fc not in fn_calls:
                            fn_calls.append(_p_fc)
                    end_call_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "end_call"),
                        None,
                    )
                    if end_call_fc:
                        _call_age = time.perf_counter() - _call_connect_time
                        _recent_stt = (_last_user_stt_snippet or last_in_user or "").lower()
                        _explicit_hangup = bool(
                            re.search(
                                r"\b(bye|goodbye|not interested|stop calling|don't call|dont call|"
                                r"no thanks|leave me|do not call)\b",
                                _recent_stt,
                            )
                        )
                        _user_still_engaged = (
                            _user_has_spoken
                            and (time.perf_counter() - _last_user_spoke_t) < 120.0
                            and not _explicit_hangup
                        )
                        _block_premature_hangup = (
                            not _is_voicemail_mode
                            and (
                                (
                                    _prior_opening_bytes_at_connect > 0
                                    and (
                                        not state.recorded_greeting_nudge_sent
                                        or time.perf_counter() < _post_greeting_grace_until
                                        or not _user_has_spoken
                                    )
                                )
                                or (_name_confirmed and not _pitch_delivered)
                                or (
                                    _prior_opening_bytes_at_connect > 0
                                    and _call_age < MIN_OUTBOUND_CALL_SEC
                                )
                                or _user_still_engaged
                            )
                        )
                        if _block_premature_hangup:
                            logger.warning(
                                "Ignoring premature end_call "
                                "(nudge_sent={} grace={} user_spoke={} name_confirmed={} pitch_delivered={} call_age={:.1f}s call_uuid={})",
                                state.recorded_greeting_nudge_sent,
                                time.perf_counter() < _post_greeting_grace_until,
                                _user_has_spoken,
                                _name_confirmed,
                                _pitch_delivered,
                                _call_age,
                                state.call_id,
                            )
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": "end_call",
                                                        "id": end_call_fc.get("id"),
                                                        "response": {
                                                            "output": (
                                                                "Call still active — continue the sales conversation. "
                                                                "Offer a site visit; do NOT end_call while the user is engaged."
                                                            )
                                                        },
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception as _ack_err:
                                logger.warning("end_call grace-period ack failed: {}", _ack_err)
                            continue
                        logger.info(
                            "Gemini Live: AI triggered end_call -> draining + REST hangup (id={}, call_uuid={})",
                            end_call_fc.get("id"),
                            state.call_id,
                        )
                        # #region agent log
                        try:
                            from debug_agent_log import agent_debug

                            agent_debug(
                                "E",
                                "live_session.py:pump_gemini_to_queue",
                                "early_hangup",
                                {"reason": "end_call_tool", "call_uuid": state.call_id or ""},
                            )
                        except Exception:
                            pass
                        # #endregion
                        # Acknowledge the tool call so Gemini cleanly closes the turn.
                        try:
                            await gem.send(
                                json.dumps(
                                    {
                                        "toolResponse": {
                                            "functionResponses": [
                                                {
                                                    "name": "end_call",
                                                    "id": end_call_fc.get("id"),
                                                    "response": {"output": "Call terminated."},
                                                }
                                            ]
                                        }
                                    }
                                )
                            )
                        except Exception as _ack_err:
                            logger.warning("end_call ack failed: {}", _ack_err)
                        # Inline teardown: drain ~0.9 s so the goodbye TTS reaches Vobiz,
                        # send the WS hangup hint, DELETE the call via REST (authoritative
                        # — without this Vobiz reconnects the same camp_id WS), then close.
                        await terminate_call(
                            ws,
                            call_uuid=state.call_id,
                            auth_id=vobiz_auth_id,
                            auth_token=vobiz_auth_token,
                            drain_seconds=0.9,
                        )
                        return

                    # ── Send WhatsApp details function call ──────────────────────
                    whatsapp_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "send_whatsapp_details"),
                        None,
                    )
                    if whatsapp_fc:
                        # Session-level dedup: check session state or DB
                        _wa_flag = state.whatsapp_sent
                        _lead_id_wa = data.get("_lead_id") or data.get("id") if isinstance(data, dict) else None
                        if _lead_id_wa and not _wa_flag:
                            try:
                                from core.storage import get_lead_whatsapp_sent
                                _wa_flag = await get_lead_whatsapp_sent(_lead_id_wa)
                            except Exception:
                                pass
                        
                        if _wa_flag:
                            logger.info("WhatsApp already sent this call — skipping duplicate function call")
                            # Still acknowledge the tool call so Gemini doesn't hang
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": "send_whatsapp_details",
                                                        "id": whatsapp_fc.get("id", ""),
                                                        "response": {"output": "WhatsApp details already shared earlier. Tell the customer politely that you have already shared the details on WhatsApp."},
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception:
                                pass
                        else:
                            fc_id = whatsapp_fc.get("id", "")
                            fc_args = whatsapp_fc.get("args") or whatsapp_fc.get("parameters") or {}
                            summary = str(fc_args.get("message_summary") or "Project Details")
                            logger.info(
                                "Gemini Live: AI queued send_whatsapp_details (post-call) -> summary={!r}",
                                summary,
                            )
                            wa_output = (
                                "Queued for post-call delivery. Say ONCE: "
                                "'I'll share the details on WhatsApp after our call.' "
                                "Do NOT repeat this line again. Continue the conversation naturally."
                            )
                            # Store pending flag under both camp_id AND live_log_id so
                            # the post-call analysis can always find them (camp data may expire).
                            if camp_id and camp_id in _CAMPAIGN_DATA:
                                _CAMPAIGN_DATA[camp_id]["_whatsapp_pending"] = True
                                _CAMPAIGN_DATA[camp_id]["_whatsapp_pending_summary"] = summary
                            # Also store under live_log_id as fallback (log_id survives cleanup)
                            if live_log_id:
                                try:
                                    if live_log_id not in _CAMPAIGN_DATA:
                                        _CAMPAIGN_DATA[live_log_id] = {}
                                    _CAMPAIGN_DATA[live_log_id]["_whatsapp_pending"] = True
                                    _CAMPAIGN_DATA[live_log_id]["_whatsapp_pending_summary"] = summary
                                except Exception:
                                    pass
                            callee_phone = phone_lookup
                            if not callee_phone and camp_id and camp_id in _CAMPAIGN_DATA:
                                callee_phone = str(_CAMPAIGN_DATA[camp_id].get("phone") or "")
                            if callee_phone and camp_id and camp_id in _CAMPAIGN_DATA:
                                _CAMPAIGN_DATA[camp_id]["phone"] = callee_phone
                            # Guide AI to confirm email if needed (without claiming WA already sent)
                            _known_email = ""
                            if isinstance(data, dict):
                                _known_email = str(data.get("email") or "").strip()
                            if not _known_email and _lead_id_wa:
                                try:
                                    from core.storage import get_lead as _gl_email
                                    _lr_em = await _gl_email(role or "sales_1", int(_lead_id_wa))
                                    if _lr_em:
                                        _known_email = str(_lr_em.get("email") or "").strip()
                                except Exception:
                                    pass
                            if _known_email and "@" in _known_email:
                                wa_output += (
                                    f" If they want email too, confirm: 'I have your email as {_known_email} — "
                                    f"I'll share the brochure there as well after the call.'"
                                )
                            else:
                                wa_output += (
                                    " If they want email too, politely ask for their email ID and use "
                                    "send_email_details — but keep talking; never go silent."
                                )
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": "send_whatsapp_details",
                                                        "id": fc_id,
                                                        "response": {"output": wa_output},
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception as _ack_err:
                                logger.warning("send_whatsapp_details ack failed: {}", _ack_err)
                        # Do NOT return — let the call continue normally

                    # ── Capture Virtual Meet function call ──────────────────────
                    vm_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "capture_virtual_meet"),
                        None,
                    )
                    if vm_fc:
                        fc_id = vm_fc.get("id", "")
                        fc_args = vm_fc.get("args") or vm_fc.get("parameters") or {}
                        vm_date = str(fc_args.get("meet_date") or "")
                        vm_time = str(fc_args.get("meet_time") or "")
                        vm_notes = str(fc_args.get("notes") or "")
                        logger.info(
                            "Gemini Live: AI triggered capture_virtual_meet -> date={!r} time={!r} notes={!r}",
                            vm_date, vm_time, vm_notes,
                        )
                        # Store virtual meet info in campaign data for post-call analysis
                        if camp_id and camp_id in _CAMPAIGN_DATA:
                            _CAMPAIGN_DATA[camp_id]["virtual_meet"] = {
                                "date": vm_date,
                                "time": vm_time,
                                "notes": vm_notes,
                            }
                        vm_output = f"Virtual meet captured: {vm_date} {vm_time}"
                        try:
                            await gem.send(
                                json.dumps(
                                    {
                                        "toolResponse": {
                                            "functionResponses": [
                                                {
                                                    "name": "capture_virtual_meet",
                                                    "id": fc_id,
                                                    "response": {"output": vm_output},
                                                }
                                            ]
                                        }
                                    }
                                )
                            )
                        except Exception as _ack_err:
                            logger.warning("capture_virtual_meet ack failed: {}", _ack_err)

                    # ── Send Email function call ────────────────────────────────
                    email_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "send_email_details"),
                        None,
                    )
                    if email_fc:
                        _em_flag = state.email_sent
                        _lead_id_em = data.get("_lead_id") or data.get("id") if isinstance(data, dict) else None
                        if _lead_id_em and not _em_flag:
                            try:
                                from core.storage import get_lead_email_sent
                                _em_flag = await get_lead_email_sent(_lead_id_em)
                            except Exception:
                                pass

                        if _em_flag:
                            logger.info("Email already sent this call — skipping duplicate function call")
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": "send_email_details",
                                                        "id": email_fc.get("id", ""),
                                                        "response": {"output": "Email details already shared earlier. Tell the customer politely that you have already sent the details on email."},
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception:
                                pass
                        else:
                            fc_id = email_fc.get("id", "")
                            fc_args = email_fc.get("args") or email_fc.get("parameters") or {}
                            if state.prefer_whatsapp_only or (
                                camp_id and camp_id in _CAMPAIGN_DATA
                                and _CAMPAIGN_DATA[camp_id].get("_prefer_whatsapp_only")
                            ):
                                try:
                                    await gem.send(
                                        json.dumps(
                                            {
                                                "toolResponse": {
                                                    "functionResponses": [{
                                                        "name": "send_email_details",
                                                        "id": email_fc.get("id", ""),
                                                        "response": {"output": (
                                                            "Customer prefers WhatsApp only. Do NOT send email. "
                                                            "Acknowledge warmly and continue on WhatsApp."
                                                        )},
                                                    }]
                                                }
                                            }
                                        )
                                    )
                                except Exception:
                                    pass
                                continue
                            to_email = str(fc_args.get("email_address") or "").strip()
                            if (not to_email or "@" not in to_email) and _lead_id_em:
                                try:
                                    from core.storage import get_lead as _gl_em
                                    _lr = await _gl_em(role or "sales_1", int(_lead_id_em))
                                    if _lr:
                                        to_email = str(_lr.get("email") or "").strip()
                                except Exception:
                                    pass
                            if (not to_email or "@" not in to_email) and isinstance(data, dict):
                                to_email = str(data.get("email") or "").strip()
                            logger.info("Gemini Live: AI triggered send_email_details -> email={!r}", to_email)
                            
                            email_output = ""
                            if to_email and "@" in to_email:
                                try:
                                    from services.email_leads import send_email_project_details
                                    _out_phone = ""
                                    if camp_id and camp_id in _CAMPAIGN_DATA:
                                        _out_phone = str(_CAMPAIGN_DATA[camp_id].get("_outbound_phone") or "")
                                    result = await send_email_project_details(
                                        to_email,
                                        summary="Project details requested during call.",
                                        outbound_phone=_out_phone,
                                    )
                                    email_output = f"Email sent to {to_email}: {result.get('sent')}"
                                    logger.info("Email send result: {}", result)
                                    if result.get("sent"):
                                        state.email_sent = True
                                        if camp_id and camp_id in _CAMPAIGN_DATA:
                                            _CAMPAIGN_DATA[camp_id]["_email_sent"] = True
                                        if _lead_id_em:
                                            try:
                                                from core.storage import update_lead_email_sent_in_db
                                                await update_lead_email_sent_in_db(_lead_id_em, to_email)
                                            except Exception as db_err:
                                                logger.error("Failed to update email sent in db: {}", db_err)
                                        try:
                                            from core.events import get_event_bus
                                            await get_event_bus().publish(
                                                "email_sent",
                                                role=role or "sales_1",
                                                lead_id=_lead_id_em,
                                            )
                                        except Exception:
                                            pass
                                except Exception as email_err:
                                    logger.exception("Email send failed: {}", email_err)
                                    email_output = f"Failed to send email: {email_err}"
                            else:
                                email_output = "Invalid email address provided"
                                
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": "send_email_details",
                                                        "id": fc_id,
                                                        "response": {"output": email_output},
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception as _ack_err:
                                logger.warning("send_email_details ack failed: {}", _ack_err)

                    # ── Schedule callback function call ─────────────────────────
                    schedule_fc = next(
                        (fc for fc in fn_calls if (fc or {}).get("name") == "schedule_callback"),
                        None,
                    )
                    if schedule_fc:
                        fc_id = schedule_fc.get("id", "")
                        fc_args = schedule_fc.get("args") or schedule_fc.get("parameters") or {}
                        scheduled_iso = str(fc_args.get("scheduled_at_iso") or "").strip()
                        notes = str(fc_args.get("notes") or "Customer requested callback").strip()
                        minutes_from_now = fc_args.get("minutes_from_now")
                        cb_output = "Could not schedule callback — missing phone number."
                        _lead_id_cb = None
                        if isinstance(data, dict):
                            _lead_id_cb = data.get("_lead_id") or data.get("id")
                        callee_phone = phone_lookup
                        if not callee_phone and camp_id and camp_id in _CAMPAIGN_DATA:
                            callee_phone = str(_CAMPAIGN_DATA[camp_id].get("phone") or "")
                        callee_name = str(data.get("name") or "") if isinstance(data, dict) else ""
                        if callee_phone and (scheduled_iso or minutes_from_now or notes):
                            try:
                                from services.callback_schedule import resolve_callback_epoch
                                from core.storage import add_scheduled_callback
                                if minutes_from_now is not None:
                                    try:
                                        mins = int(minutes_from_now)
                                        if mins > 0:
                                            notes = f"{notes} after {mins} minutes"
                                    except (TypeError, ValueError):
                                        pass
                                scheduled_epoch, ist_label = resolve_callback_epoch(
                                    scheduled_iso,
                                    notes,
                                    tz_name=settings.transcript_callback_tz,
                                )
                                _outbound_cb = ""
                                if camp_id and camp_id in _CAMPAIGN_DATA:
                                    _outbound_cb = str(_CAMPAIGN_DATA[camp_id].get("_outbound_phone") or "").strip()
                                if not _outbound_cb and _lead_id_cb:
                                    try:
                                        from core.storage import get_lead as _get_lead_cb
                                        _lr_cb = await _get_lead_cb(role or "sales_1", int(_lead_id_cb))
                                        if _lr_cb:
                                            _outbound_cb = str(_lr_cb.get("outbound_phone") or "").strip()
                                    except Exception:
                                        pass
                                cb_id = await add_scheduled_callback(
                                    role=role or "sales_1",
                                    lead_id=int(_lead_id_cb) if _lead_id_cb else None,
                                    phone=callee_phone,
                                    name=callee_name,
                                    scheduled_at=scheduled_epoch,
                                    outbound_phone=_outbound_cb,
                                )
                                cb_output = (
                                    f"Callback scheduled id={cb_id} for {ist_label}. "
                                    f"Tell the customer warmly: 'Sure, I'll call you back at {ist_label}.'"
                                )
                                logger.info(
                                    "Gemini Live: schedule_callback -> id={} at {} (epoch={})",
                                    cb_id, ist_label, scheduled_epoch,
                                )
                            except Exception as cb_err:
                                logger.exception("schedule_callback failed: {}", cb_err)
                                cb_output = f"Failed to schedule callback: {cb_err}"
                        try:
                            await gem.send(
                                json.dumps(
                                    {
                                        "toolResponse": {
                                            "functionResponses": [
                                                {
                                                    "name": "schedule_callback",
                                                    "id": fc_id,
                                                    "response": {"output": cb_output},
                                                }
                                            ]
                                        }
                                    }
                                )
                            )
                        except Exception as _ack_err:
                            logger.warning("schedule_callback ack failed: {}", _ack_err)

                    # ── Fallback ack for any unhandled function calls ─────────
                    for unk_fc in fn_calls:
                        fn_name = (unk_fc or {}).get("name")
                        if fn_name and fn_name not in ("end_call", "send_whatsapp_details", "capture_virtual_meet", "send_email_details", "schedule_callback"):
                            fc_id = unk_fc.get("id", "")
                            logger.info("Gemini Live: AI called unhandled function {!r} (id={})", fn_name, fc_id)
                            try:
                                await gem.send(
                                    json.dumps(
                                        {
                                            "toolResponse": {
                                                "functionResponses": [
                                                    {
                                                        "name": fn_name,
                                                        "id": fc_id,
                                                        "response": {"output": f"Function {fn_name} processed. Continue the conversation naturally."},
                                                    }
                                                ]
                                            }
                                        }
                                    )
                                )
                            except Exception as _ack_err:
                                logger.warning("Unhandled function {!r} ack failed: {}", fn_name, _ack_err)

                    sc = obj.get("serverContent") or {}

                    it = sc.get("inputTranscription") or {}
                    if it.get("text"):
                        _incoming_stt = str(it.get("text") or "")
                        if _is_telephony_announcement(_incoming_stt):
                            logger.info("Ignoring carrier recording announcement: {!r}", _incoming_stt)
                            last_in_user = ""
                            _pending_transcript.pop("user", None)
                            continue
                        _now_t = time.perf_counter()
                        last_user_audio_t = _now_t
                        _last_user_spoke_t = _now_t
                        _agent_last_finished_t = 0.0
                        _agent_turn_audio_started_at = 0.0
                        _user_silence_nudge_count = 0
                        if len(str(it.get("text") or "").strip()) >= 2:
                            _mark_confirmed_user_speech()
                        _opening_delivered = True
                        _user_has_spoken = True
                        _user_has_spoken_since_nudge = True
                        last_in_user = _incoming_stt
                        _pending_transcript["user"] = last_in_user
                        last_meaningful_t = _now_t
                        _last_user_stt_snippet = last_in_user
                        _stt_lc_close = last_in_user.lower()
                        if any(
                            p in _stt_lc_close
                            for p in (
                                "close the call", "that's all", "thats all", "talk later",
                                "will check whatsapp", "check whatsapp", "send on whatsapp",
                                "no thank you", "goodbye", "good bye", "bye",
                            )
                        ):
                            _closing_mode = True
                        if state.recorded_greeting_nudge_sent:
                            _clear_post_greeting_grace()
                        logger.info("Gemini Live STT: {!r}", last_in_user)
                        # Greeting-era listen loop: classify human vs automated while PCM plays.
                        if _greeting_listen_active and (
                            len(prior_16k_queue) > 0 or not state.recorded_greeting_nudge_sent
                        ):
                            _task_gl = asyncio.create_task(
                                _classify_callee_during_greeting(last_in_user)
                            )
                            _background_tasks.add(_task_gl)
                            _task_gl.add_done_callback(_background_tasks.discard)
                        # Human took over after call screening — resume normal sales flow.
                        if _is_voicemail_mode and _vm_phase == "screening":
                            if looks_like_live_human_after_screening(last_in_user):
                                _is_voicemail_mode = False
                                _vm_phase = ""
                                _voicemail_triggered = False
                                _vm_wait_until = 0.0
                                if camp_id and camp_id in _CAMPAIGN_DATA:
                                    _CAMPAIGN_DATA[camp_id]["is_voicemail"] = False
                                logger.info(
                                    "Voicemail screening: human detected — resuming live conversation: {!r}",
                                    last_in_user[:80],
                                )
                        # Voicemail / call-screening detection (STT phrases, up to VOICEMAIL_DETECT_SEC).
                        _vm_kind_now = classify_voicemail_stt(last_in_user)
                        if _vm_kind_now and not (_voicemail_triggered and _vm_phase == "message"):
                            _elapsed = _now_t - _call_connect_time
                            if _elapsed <= VOICEMAIL_DETECT_SEC or _vm_kind_now == "beep":
                                if not _voicemail_triggered or _vm_kind_now == "beep":
                                    _task_vm = asyncio.create_task(
                                        _enter_voicemail_mode(
                                            _vm_kind_now,
                                            source="stt_phrase",
                                            stt=last_in_user,
                                        )
                                    )
                                    _background_tasks.add(_task_vm)
                                    _task_vm.add_done_callback(_background_tasks.discard)
                        elif _is_voicemail_mode and _vm_phase == "screening":
                            _vm_kind_late = classify_voicemail_stt(last_in_user)
                            if _vm_kind_late == "beep":
                                _task_vb = asyncio.create_task(_arm_voicemail_message())
                                _background_tasks.add(_task_vb)
                                _task_vb.add_done_callback(_background_tasks.discard)
                        # Auto-detect name confirmation from user speech (only after name-verify asked)
                        _stt_lc = last_in_user.lower()
                        _name_confirm_re = re.compile(
                            r"\b(yes speaking|speaking|this is|correct|right|you are right|"
                            r"that's me|thats me|haan ji|ji haan|yes it is|yeah it is)\b"
                        )
                        _engagement_re = re.compile(
                            r"\b(tell me|go ahead|sure|interested|continue|please tell)\b"
                        )
                        _weak_ack_re = re.compile(r"\b(yes|yeah|yep|haan|ji)\b")
                        _ambiguous_ack_re = re.compile(r"\b(think|maybe|not sure|i guess)\b")
                        if (
                            state.recorded_greeting_nudge_sent
                            and _name_verify_asked
                            and not _name_confirmed
                        ):
                            if _authoritative_lead_name and _name_confirm_re.search(_stt_lc):
                                _name_confirmed = True
                                _clear_post_greeting_grace()
                                logger.info("Name auto-confirmed from STT: {!r}", last_in_user)
                            elif _authoritative_lead_name and _engagement_re.search(_stt_lc):
                                _name_confirmed = True
                                _clear_post_greeting_grace()
                                logger.info(
                                    "Name implied confirmed from engagement STT: {!r}",
                                    last_in_user,
                                )
                            elif (
                                _authoritative_lead_name
                                and _weak_ack_re.search(_stt_lc)
                                and not _ambiguous_ack_re.search(_stt_lc)
                                and len(_stt_lc.split()) <= 4
                            ):
                                _name_confirmed = True
                                _clear_post_greeting_grace()
                                logger.info(
                                    "Name confirmed from short ack after name-verify: {!r}",
                                    last_in_user,
                                )
                        elif not _authoritative_lead_name:
                            # No pre-known name: the agent asked "May I know your name?" and the
                            # caller answered. Confirm on any substantive reply that isn't a
                            # hangup/voicemail/off-topic AI question, and capture the spoken name
                            # so the project pitch addresses them naturally. Without this, no-name
                            # leads could never be confirmed -> the pitch nudge never fired -> dead
                            # silence right after the caller gave their name.
                            if (
                                not classify_voicemail_stt(last_in_user)
                                and not _stt_asks_ai_identity(_stt_lc)
                                and not re.search(
                                    r"\b(bye|goodbye|not interested|stop|cancel|wrong number)\b",
                                    _stt_lc,
                                )
                                and len(_stt_lc.strip()) >= 2
                            ):
                                from core.opening_line import looks_like_real_name
                                try:
                                    from core.campaign_payload import addressable_name as _addr_nm
                                except Exception:
                                    _addr_nm = None
                                _spoken = _extract_spoken_name_from_stt(last_in_user)
                                _cap = ""
                                if _addr_nm and _spoken:
                                    _cap = _addr_nm(_spoken)
                                if not _cap:
                                    _cap = (_spoken[:1].upper() + _spoken[1:]) if _spoken else ""
                                if not _cap and looks_like_real_name(_spoken):
                                    _cap = _spoken
                                _name_confirmed = True
                                _clear_post_greeting_grace()
                                if _cap:
                                    _authoritative_lead_name = _cap
                                logger.info(
                                    "Gemini Live: name confirmed from reply to name-ask "
                                    "(no pre-known name; spoken={!r}, captured={!r})",
                                    last_in_user, _cap,
                                )
                        # After name verify, force project pitch — never during voicemail
                        if (
                            state.recorded_greeting_nudge_sent
                            and _name_verify_asked
                            and _name_confirmed
                            and not _pitch_delivered
                            and not _post_name_pitch_nudge_sent
                            and not _is_voicemail_mode
                            and not _voicemail_triggered
                            and not classify_voicemail_stt(last_in_user)
                        ):
                            _user_turn_nudge_sent = True
                            _task_pnp = asyncio.create_task(_defer_post_name_pitch(last_in_user))
                            _background_tasks.add(_task_pnp)
                            _task_pnp.add_done_callback(_background_tasks.discard)
                        # Fast hello / presence response when user checks if agent is there
                        _wa_only_phrases = (
                            "whatsapp only", "only whatsapp", "no email", "don't email",
                            "dont email", "mail mat", "email mat", "whatsapp pe hi",
                            "whatsapp la", "email venda", "no mail", "just whatsapp",
                        )
                        if any(p in _stt_lc for p in _wa_only_phrases):
                            state.prefer_whatsapp_only = True
                            if camp_id and camp_id in _CAMPAIGN_DATA:
                                _CAMPAIGN_DATA[camp_id]["_prefer_whatsapp_only"] = True
                            logger.info("Caller prefers WhatsApp only — email suppressed for camp={}", camp_id)
                        # Developer mode: panther chinmay on whitelisted phone
                        if (
                            not _dev_mode_active
                            and not _fake_dev_block_sent
                            and _stt_mentions_dev_without_codeword(last_in_user)
                        ):
                            _fake_dev_block_sent = True
                            try:
                                from .gemini_protocol import gemini_send_fake_dev_mode_block

                                await gemini_send_fake_dev_mode_block(gem)
                                logger.info(
                                    "Fake dev mode blocked (stt={!r})",
                                    last_in_user[:80],
                                )
                            except Exception as _fdm_err:
                                logger.warning("Fake dev mode block failed: {}", _fdm_err)
                        elif (
                            not _dev_mode_active
                            and _stt_matches_dev_codeword(last_in_user)
                            and _phone_on_dev_whitelist(phone_lookup)
                        ):
                            _dev_mode_active = True
                            if not _dev_mode_nudge_sent:
                                _dev_mode_nudge_sent = True
                                try:
                                    await gemini_send_dev_mode_nudge(gem)
                                    logger.info(
                                        "Dev mode activated (phone={!r}, stt={!r})",
                                        phone_lookup,
                                        last_in_user[:80],
                                    )
                                except Exception as _dm_err:
                                    logger.warning("Dev mode nudge failed: {}", _dm_err)
                        elif _dev_mode_active and not _stt_matches_dev_codeword(last_in_user):
                            _append_dev_mode_instruction(
                                instruction=last_in_user,
                                call_id=str(live_log_id or camp_id or ""),
                                role=role,
                                phone=phone_lookup,
                            )
                        # the project pivot when caller says visited/bought (buyer campaigns only)
                        if (
                            not _phase3_nudge_sent
                            and not _dev_mode_active
                            and not _is_cp_campaign_role(role)
                            and _stt_triggers_phase3(last_in_user)
                        ):
                            _phase3_nudge_sent = True
                            try:
                                await gemini_send_phase3_pitch_nudge(gem, user_stt=last_in_user)
                                logger.info(
                                    "the project pitch nudge sent (stt={!r})",
                                    last_in_user[:80],
                                )
                            except Exception as _p3_err:
                                logger.warning("the project pitch nudge failed: {}", _p3_err)
                        # CP complaint when caller is upset about weird phrasing
                        if (
                            _is_cp_campaign_role(role)
                            and not _cp_complaint_nudge_sent
                            and not _dev_mode_active
                            and _stt_is_cp_complaint(last_in_user)
                        ):
                            _cp_complaint_nudge_sent = True
                            _cp_qa_mode = True
                            try:
                                from .gemini_protocol import gemini_send_cp_complaint_recovery_nudge

                                await gemini_send_cp_complaint_recovery_nudge(
                                    gem,
                                    agent_name=agent_name or "Vernika",
                                    user_stt=last_in_user,
                                )
                                logger.info(
                                    "CP complaint recovery nudge sent (stt={!r})",
                                    last_in_user[:80],
                                )
                            except Exception as _cp_comp_err:
                                logger.warning("CP complaint recovery nudge failed: {}", _cp_comp_err)
                        # AI / bot identity — buyer campaigns only (not CP refusal complaints)
                        if (
                            not _ai_disclosure_nudge_sent
                            and not _dev_mode_active
                            and not _is_cp_campaign_role(role)
                            and _stt_asks_ai_identity(last_in_user)
                        ):
                            _ai_disclosure_nudge_sent = True
                            _agent_last_finished_t = 0.0
                            try:
                                from .gemini_protocol import gemini_send_ai_disclosure_nudge

                                await gemini_send_ai_disclosure_nudge(gem)
                                logger.info(
                                    "AI disclosure nudge sent (stt={!r})",
                                    last_in_user[:80],
                                )
                            except Exception as _ai_err:
                                logger.warning("AI disclosure nudge failed: {}", _ai_err)
                        if _stt_is_checkin_only(last_in_user):
                            # After pitch: Gemini hears "hello" on mic — injected nudge caused
                            # duplicate "can you hear me" + WhatsApp loops.
                            if not _pitch_delivered:
                                _during_greeting = len(prior_16k_queue) > 0 or not state.recorded_greeting_nudge_sent
                                _hello_cooldown = 2.5 if (_pitch_delivered or _name_confirmed) else 1.2
                                if (
                                    not _during_greeting
                                    and (_now_t - _hello_nudge_sent_at) > _hello_cooldown
                                    and not model_generation_active
                                    and len(gemini_16k_queue) == 0
                                    and (time.perf_counter() - _last_model_spoke_t) > 4.0
                                    and not (_post_name_pitch_nudge_sent and not _pitch_delivered)
                                    and not (_name_confirmed and _post_name_pitch_nudge_sent and not _pitch_delivered)
                                    and _hello_nudge_count < 1
                                    and _last_nudge_kind != "hello"
                                ):
                                    _hello_nudge_sent_at = _now_t
                                    _hello_name = _authoritative_lead_name or ""

                                    if _name_confirmed:
                                        ack = f" Use name '{_hello_name}' in conversation naturally." if _hello_name else ""
                                        hello_text = (
                                            "[USER CHECK-IN — RESPOND NATURALLY] "
                                            "The caller checked in. Reply warmly in 1 short sentence — "
                                            "no re-introduction, no name re-ask, no WhatsApp offer." + ack
                                        )
                                        try:
                                            await gem.send(
                                                json.dumps(
                                                    {
                                                        "clientContent": {
                                                            "turns": [
                                                                {
                                                                    "role": "user",
                                                                    "parts": [{"text": hello_text}],
                                                                }
                                                            ],
                                                            "turnComplete": True,
                                                        }
                                                    }
                                                )
                                            )
                                            _hello_nudge_count += 1
                                            _last_nudge_kind = "hello"
                                            _last_silence_nudge_at = time.perf_counter() + 14.0
                                            _agent_last_finished_t = time.perf_counter()
                                            logger.info(
                                                "Gemini Live: hello/presence nudge sent (name_confirmed={}, count={})",
                                                _name_confirmed,
                                                _hello_nudge_count,
                                            )
                                        except Exception as _hn_err:
                                            logger.warning("Hello nudge failed: {}", _hn_err)
                                    elif _name_verify_asked:
                                        # Name verify already spoken — defer pitch; model may reply on its own
                                        _name_confirmed = True
                                        _last_silence_nudge_at = time.perf_counter() + 14.0
                                        _task_hap = asyncio.create_task(_defer_post_name_pitch(last_in_user))
                                        _background_tasks.add(_task_hap)
                                        _task_hap.add_done_callback(_background_tasks.discard)
                                        logger.info(
                                            "Gemini Live: hello ack — deferred pitch (name={!r}, stt={!r})",
                                            _hello_name,
                                            last_in_user[:80],
                                        )
                                    else:
                                        _hello_nudge_count += 1
                                        _last_nudge_kind = "hello"
                                        hello_text = (
                                            "[USER CHECK-IN — RESPOND NOW IN ONE SHORT SENTENCE] "
                                            "The caller said hello or asked if you are there. "
                                            "Reply immediately and warmly: 'Yes, I can hear you!' "
                                            "Then listen — do NOT repeat the introduction or ask their name yet."
                                        )
                                        try:
                                            await gem.send(
                                                json.dumps(
                                                    {
                                                        "clientContent": {
                                                            "turns": [
                                                                {
                                                                    "role": "user",
                                                                    "parts": [{"text": hello_text}],
                                                                }
                                                            ],
                                                            "turnComplete": True,
                                                        }
                                                    }
                                                )
                                            )
                                            logger.info(
                                                "Gemini Live: hello/presence nudge sent (pre-name-verify, count={})",
                                                _hello_nudge_count,
                                            )
                                        except Exception as _hn_err:
                                            logger.warning("Hello nudge failed: {}", _hn_err)
                        if (
                            _is_cp_campaign_role(role)
                            and _pitch_delivered
                            and _stt_is_audio_checkin(last_in_user)
                            and not model_generation_active
                            and len(gemini_16k_queue) == 0
                            and (time.perf_counter() - _hello_nudge_sent_at) > 2.5
                            and (time.perf_counter() - _last_model_spoke_t) > 3.0
                            and _hello_nudge_count < 2
                        ):
                            _hello_nudge_sent_at = time.perf_counter()
                            _hello_nudge_count += 1
                            _last_nudge_kind = "hello"
                            try:
                                from .gemini_protocol import gemini_send_cp_presence_ack_nudge

                                await gemini_send_cp_presence_ack_nudge(
                                    gem,
                                    agent_name=agent_name or "Vernika",
                                    user_stt=last_in_user,
                                    ask_account_manager=False,
                                )
                                _agent_last_finished_t = time.perf_counter()
                                logger.info(
                                    "CP mid-call presence ack (count={}) stt={!r}",
                                    _hello_nudge_count,
                                    last_in_user[:80],
                                )
                            except Exception as _cp_hello_err:
                                logger.warning("CP mid-call presence ack failed: {}", _cp_hello_err)
                        # Prefetch RAG while the user is still speaking so the context is
                        # ready at activityEnd — no SQLite on the critical path.
                        if live_rag_context and len(prior_16k_queue) == 0 and not _rag_embedded_in_prompt:
                            q_now = last_in_user.strip()
                            if len(q_now) >= 2 and not _is_short_ack(q_now):
                                _task = asyncio.create_task(_prefetch_rag(q_now))
                                _background_tasks.add(_task)
                                _task.add_done_callback(_background_tasks.discard)
                    out_tx = sc.get("outputTranscription") or obj.get("outputTranscription") or {}
                    if out_tx.get("text"):
                        if len(prior_16k_queue) > 0:
                            logger.info("Discarding Gemini output text transcript while greeting plays: {}", out_tx.get("text"))
                        else:
                            _out_chunk = str(out_tx.get("text") or "")
                            # Strip function-call JSON leaks from output transcription
                            _out_chunk = re.sub(
                                r"\b(schedule_callback|end_call|send_whatsapp_details|capture_virtual_meet|send_email_details)\{[^}]*\}",
                                "",
                                _out_chunk,
                            ).strip()
                            if _out_chunk and (
                                _is_explicit_refusal_leak(_out_chunk)
                                or _is_partial_refusal_leak(_out_chunk)
                            ):
                                logger.warning(
                                    "Refusal leak in output transcript — flushing audio ({})",
                                    _out_chunk[:160],
                                )
                                await _flush_refusal_audio_now()
                            elif _out_chunk:
                                if not last_out_assistant:
                                    last_out_assistant = _out_chunk
                                elif _out_chunk.startswith(last_out_assistant):
                                    last_out_assistant = _out_chunk
                                elif last_out_assistant not in _out_chunk:
                                    last_out_assistant = f"{last_out_assistant} {_out_chunk}".strip()
                                last_out_assistant = _strip_refusal_phrases(last_out_assistant)
                                if (
                                    _is_cp_campaign_role(role)
                                    and _looks_like_cp_script_rush(last_out_assistant)
                                ):
                                    logger.warning(
                                        "CP script rush — cutting monologue ({})",
                                        last_out_assistant[:160],
                                    )
                                    await _flush_monologue_audio()
                                elif _should_block_cta_in_output(last_out_assistant):
                                    logger.warning(
                                        "Blocked excess Account Manager CTA in stream (count={}/{})",
                                        _account_manager_cta_count,
                                        _ACCOUNT_MANAGER_CTA_MAX,
                                    )
                                    await _flush_blocked_cta_audio()
                                else:
                                    _pending_transcript["assistant"] = last_out_assistant
                                    if _looks_like_features_pitch_delivered(last_out_assistant):
                                        _features_pitch_delivered = True
                                    if _looks_like_commission_delivered(last_out_assistant):
                                        _commission_delivered = True
                                    if _is_cp_campaign_role(role) and _looks_like_turn45_merged(last_out_assistant):
                                        _commission_delivered = True
                                        _track_account_manager_cta("turn45_merged_stream")
                                        logger.warning(
                                            "Turn 4+5 merged in one breath — CTA counted once"
                                        )
                                    elif _looks_like_account_manager_cta_asked(last_out_assistant):
                                        _track_account_manager_cta("output_stream")
                                    if _is_explicit_refusal_leak(last_out_assistant) or _is_partial_refusal_leak(
                                        last_out_assistant
                                    ):
                                        logger.warning(
                                            "Refusal leak in accumulated transcript — flushing audio ({})",
                                            last_out_assistant[:160],
                                        )
                                        await _flush_refusal_audio_now()
                                    elif _looks_like_silence_checkin_phrase(last_out_assistant):
                                        _user_silence_nudge_count = max(_user_silence_nudge_count, 1)
                                        _silence_checkin_spoken_count += 1
                                        _last_silence_nudge_at = time.perf_counter()
                                        _last_nudge_kind = "silence_spoken"
                                        if _silence_checkin_spoken_count >= 2:
                                            _user_silence_nudge_count = 99
                                        logger.info(
                                            "Assistant silence check-in detected — blocking further prods this gap"
                                        )

                    if sc.get("activityEnd") is not None or obj.get("activityEnd") is not None:
                        if not model_generation_active:
                            _model_turn_pcm_bytes_24k = 0
                        # Only count as user speech when STT produced text — bare activityEnd
                        # from echo/VAD must not unlock end_call or skip name-verify wait.
                        if (last_in_user or "").strip():
                            _user_has_spoken = True
                        response_t0 = time.perf_counter()
                        first_byte_logged = False
                        _last_activity_end_t = time.perf_counter()
                        _response_started_after_user = False
                        _stt_snap_ae = (last_in_user or "").strip()
                        _kb_embedded_kick_scheduled = False
                        # #region agent log
                        try:
                            from debug_agent_log import agent_debug

                            agent_debug(
                                "D",
                                "live_session.py:pump_gemini_to_queue",
                                "user_stt_turn_end",
                                {
                                    "stt_len": len(last_in_user or ""),
                                    "suppress_nudge": _suppress_gemini_until_nudge,
                                    "nudge_sent": bool(state.recorded_greeting_nudge_sent),
                                    "in_grace": time.perf_counter() < _post_greeting_grace_until,
                                    "prior_pcm_bytes": len(prior_16k_queue),
                                },
                            )
                        except Exception:
                            pass
                        # #endregion
                        _rag_question_handled = False
                        _user_turn_nudge_sent = False
                        _monologue_audio_block = False
                        _activity_end_nudge_seq += 1
                        _ae_nudge_token = _activity_end_nudge_seq
                        if len(prior_16k_queue) == 0:
                            q_ae = (last_in_user or "").strip()
                            if (
                                _is_cp_campaign_role(role)
                                and q_ae
                                and (
                                    _user_asked_factual_question(q_ae)
                                    or _looks_like_question(q_ae)
                                    or (_commission_delivered and _looks_like_question(q_ae))
                                )
                            ):
                                _cp_qa_mode = True
                            if (
                                len(q_ae) >= 2
                                and not _is_short_ack(q_ae)
                                and not model_generation_active
                                and len(gemini_16k_queue) == 0
                                and not _user_turn_nudge_sent
                                and time.perf_counter() >= _nudge_suppressed_until
                            ):
                                _kb_embedded_question = (
                                    _looks_like_question(q_ae) and _kb_ready_at_connect()
                                )
                                if _looks_like_question(q_ae):
                                    if (
                                        settings.gemini_live_skip_blocking_rag_when_kb_embedded
                                        and _kb_ready_at_connect()
                                    ):
                                        if live_rag_context and not _rag_embedded_in_prompt:
                                            _task_pf = asyncio.create_task(_prefetch_rag(q_ae))
                                            _background_tasks.add(_task_pf)
                                            _task_pf.add_done_callback(_background_tasks.discard)
                                    else:
                                        await _try_inject_live_rag("activityEnd")
                                        _rag_question_handled = True
                                elif live_rag_context and not _rag_embedded_in_prompt:
                                    _task_pf = asyncio.create_task(_prefetch_rag(q_ae))
                                    _background_tasks.add(_task_pf)
                                    _task_pf.add_done_callback(_background_tasks.discard)
                                if _kb_embedded_question:
                                    _kb_embedded_kick_scheduled = True

                                    async def _kb_embedded_kick(stt_snap: str, token: int) -> None:
                                        _kick_delay = (
                                            CP_RESPONSE_GRACE_SEC
                                            if _is_cp_campaign_role(role)
                                            else max(0.10, INSTANT_RESPONSE_KICK_SEC)
                                        )
                                        await asyncio.sleep(_kick_delay)
                                        if token != _activity_end_nudge_seq:
                                            return
                                        if _user_turn_nudge_sent or _response_started_after_user:
                                            return
                                        if len(gemini_16k_queue) >= _MIN_AUDIBLE_MODEL_PCM_16K:
                                            return
                                        try:
                                            await _maybe_respond_nudge(
                                                stt_snap, source="kb_embedded_kick", user_turn=True
                                            )
                                        except Exception as _kbk_err:
                                            logger.warning("KB embedded respond kick failed: {}", _kbk_err)

                                    _task_kbk = asyncio.create_task(_kb_embedded_kick(q_ae, _ae_nudge_token))
                                    _background_tasks.add(_task_kbk)
                                    _task_kbk.add_done_callback(_background_tasks.discard)
                        # Single delayed kick — give Gemini time to respond naturally before nudging.
                        if (
                            _stt_snap_ae
                            and len(prior_16k_queue) == 0
                            and not _rag_question_handled
                            and not _user_turn_nudge_sent
                            and not (_post_name_pitch_nudge_sent and not _pitch_delivered)
                            and not _kb_embedded_kick_scheduled
                        ):
                            async def _instant_response_kick(stt_snap: str, token: int) -> None:
                                nonlocal _user_turn_nudge_sent
                                _kick_delay = (
                                    CP_RESPONSE_GRACE_SEC
                                    if _is_cp_campaign_role(role)
                                    else max(0.10, INSTANT_RESPONSE_KICK_SEC)
                                )
                                await asyncio.sleep(_kick_delay)
                                if token != _activity_end_nudge_seq:
                                    return
                                if _user_turn_nudge_sent or (_post_name_pitch_nudge_sent and not _pitch_delivered):
                                    return
                                if time.perf_counter() < _nudge_suppressed_until:
                                    return
                                if _response_started_after_user:
                                    return
                                if (
                                    _is_cp_campaign_role(role)
                                    and _stt_is_audio_checkin(stt_snap)
                                ):
                                    return
                                if (
                                    not model_generation_active
                                    and len(gemini_16k_queue) == 0
                                    and len(prior_16k_queue) == 0
                                ):
                                    try:
                                        if _is_cp_campaign_role(role):
                                            await _maybe_respond_nudge(
                                                stt_snap, source="turn_kick", user_turn=True
                                            )
                                        else:
                                            _user_turn_nudge_sent = True
                                            await gemini_send_respond_now_nudge(
                                                gem, user_stt=stt_snap or "continue"
                                            )
                                        logger.info(
                                            "Turn-kick sent after {:.0f}ms (stt={!r})",
                                            _kick_delay * 1000.0,
                                            (stt_snap or "")[:60],
                                        )
                                    except Exception:
                                        pass

                            _task_irk = asyncio.create_task(_instant_response_kick(_stt_snap_ae, _ae_nudge_token))
                            _background_tasks.add(_task_irk)
                            _task_irk.add_done_callback(_background_tasks.discard)

                        # True dead-air only — long silence after user spoke, no model reply yet.
                        if (
                            DEAD_AIR_BREAKER_SEC > 0
                            and _stt_snap_ae
                            and len(prior_16k_queue) == 0
                            and not _is_short_ack(_stt_snap_ae)
                            and _opening_delivered
                            and not _rag_question_handled
                        ):
                            async def _dead_air_breaker(stt_text: str, token: int) -> None:
                                await asyncio.sleep(DEAD_AIR_BREAKER_SEC)
                                if token != _activity_end_nudge_seq:
                                    return
                                if _user_turn_nudge_sent or _response_started_after_user:
                                    return
                                if model_generation_active or len(gemini_16k_queue) >= _MIN_AUDIBLE_MODEL_PCM_16K:
                                    return
                                if len(prior_16k_queue) > 0:
                                    return
                                if time.perf_counter() < _nudge_suppressed_until:
                                    return
                                _in_post_name_grace = (
                                    time.perf_counter() < _post_greeting_grace_until
                                    and not (_name_confirmed and _user_has_spoken)
                                )
                                if _in_post_name_grace:
                                    return
                                try:
                                    await _maybe_respond_nudge(stt_text, source="dead_air", user_turn=True)
                                except Exception as _dab_err:
                                    logger.warning("Dead-air breaker failed: {}", _dab_err)

                            _task_dab = asyncio.create_task(_dead_air_breaker(_stt_snap_ae, _ae_nudge_token))
                            _background_tasks.add(_task_dab)
                            _task_dab.add_done_callback(_background_tasks.discard)

                    if sc.get("interrupted"):
                        # Never flush scripted PCM on barge-in — greeting must finish first.
                        if len(prior_16k_queue) > 0:
                            logger.info(
                                "Gemini Live: interrupted during scripted opening — ignoring "
                                "({} bytes still queued)",
                                len(prior_16k_queue),
                            )
                        elif (
                            _suppress_gemini_until_nudge
                            and not state.recorded_greeting_nudge_sent
                        ):
                            logger.info(
                                "Gemini Live: ignoring interrupted before post-greeting nudge"
                            )
                        else:
                            _agent_was_speaking = (
                                model_generation_active
                                or len(gemini_16k_queue) > 0
                                or had_model_audio_turn
                            )
                            # Gemini's `interrupted` event is the authoritative barge-in
                            # signal — its server-side VAD already confirmed the caller
                            # spoke over the agent. Requiring a redundant local RMS/STT
                            # confirmation here rejected real barge-ins (the STT text
                            # frequently arrives a beat AFTER `interrupted`), which left
                            # the agent talking over the caller. Trust Gemini when the
                            # agent is audibly speaking; the cooldown below debounces.
                            _accept_interrupt = _agent_was_speaking
                            if _accept_interrupt:
                                logger.info("Gemini Live: user barge-in (interrupted, speech confirmed)")
                                _user_has_spoken = True
                                _user_has_spoken_since_nudge = True
                                _last_user_spoke_t = time.perf_counter()
                                _barge_in_cooldown_until = time.perf_counter() + 0.45
                                _barge_in_drop_audio = True
                                _barge_in_drop_audio_at = time.perf_counter()
                                _clear_post_greeting_grace()
                                last_rag_inject_key = ""
                                activity_end_seq += 1
                                gemini_16k_queue.clear()
                                _greeting_handoff_pcm_buffer.clear()
                                prior_16k_queue.clear()
                                pending_audio_24k.clear()
                                gemini_resample_state = None
                                model_generation_active = False
                                _agent_turn_audio_started_at = 0.0
                                last_out_assistant = ""
                                had_model_audio_turn = False
                                first_byte_logged = False
                                _response_started_after_user = False
                                _model_turn_pcm_bytes_24k = 0
                                await vobiz_send_clear_audio(ws, stream_id=state.stream_id or "")
                            else:
                                logger.info(
                                    "Gemini Live: interrupted while agent idle — ignoring "
                                    "(agent_speaking={} confirmed={})",
                                    _agent_was_speaking,
                                    _user_speech_confirmed_recent(),
                                )

                    mt = sc.get("modelTurn") or {}
                    for part in (mt.get("parts") or []):
                        # --- Extract text from modelTurn parts (fallback for missing outputTranscription) ---
                        _part_text = (part.get("text") or "").strip()
                        if _part_text and len(prior_16k_queue) == 0:
                            # Strip function-call JSON leaks from text parts
                            _part_text = re.sub(
                                r"\b(schedule_callback|end_call|send_whatsapp_details|capture_virtual_meet|send_email_details)\{[^}]*\}",
                                "",
                                _part_text,
                            ).strip()
                            if _part_text and not (
                                _is_explicit_refusal_leak(_part_text)
                                or _is_partial_refusal_leak(_part_text)
                            ):
                                if not last_out_assistant:
                                    last_out_assistant = _part_text
                                elif _part_text.startswith(last_out_assistant):
                                    last_out_assistant = _part_text
                                elif last_out_assistant not in _part_text:
                                    last_out_assistant = f"{last_out_assistant} {_part_text}".strip()
                                last_out_assistant = _strip_refusal_phrases(last_out_assistant)
                                _pending_transcript["assistant"] = last_out_assistant
                                logger.debug("modelTurn text part accumulated: {}", _part_text[:120])
                        # --- End text part extraction ---
                        inline = part.get("inlineData") or part.get("inline_data")
                        if not inline:
                            continue
                        mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                        if not mime.startswith("audio/"):
                            continue
                        b64 = inline.get("data") or ""
                        if not b64:
                            continue
                        pcm = base64.b64decode(b64)
                        if len(pcm) < 16:
                            continue
                        if (
                            _barge_in_drop_audio
                            and _barge_in_drop_audio_at > 0
                            and (time.perf_counter() - _barge_in_drop_audio_at) >= 0.5
                        ):
                            _barge_in_drop_audio = False
                        if _refusal_audio_block or _barge_in_drop_audio or _monologue_audio_block:
                            continue
                        _model_turn_pcm_bytes_24k += len(pcm)
                        model_generation_active = True
                        if _agent_turn_audio_started_at <= 0:
                            _agent_turn_audio_started_at = time.perf_counter()

                        if _model_turn_pcm_bytes_24k >= _MIN_AUDIBLE_MODEL_PCM_24K:
                            had_model_audio_turn = True
                            _opening_delivered = True
                        # #region agent log
                        if (
                            _model_turn_pcm_bytes_24k >= _MIN_AUDIBLE_MODEL_PCM_24K
                            and not first_byte_logged
                        ):
                            try:
                                from debug_agent_log import agent_debug

                                agent_debug(
                                    "ADE",
                                    "live_session.py:pump_gemini_to_queue",
                                    "model_audio_first_chunk",
                                    {
                                        "bytes": len(pcm),
                                        "suppress": _suppress_gemini_until_nudge,
                                        "nudge_sent": bool(state.recorded_greeting_nudge_sent),
                                        "prior_q": len(prior_16k_queue),
                                        "gem_q_bytes": len(gemini_16k_queue),
                                        "since_connect_s": round(time.perf_counter() - _gem_live_session_t0, 2),
                                        "last_user_stt": (last_in_user or "")[:80],
                                    },
                                )
                            except Exception:
                                pass
                        # #endregion

                        if len(prior_16k_queue) > 0 and len(prior_16k_queue) > POST_GREETING_EARLY_NUDGE_BYTES:
                            pending_audio_24k.extend(pcm)
                            gemini_resample_state = drain_gemini_24k_to_vobiz_16k(
                                pending_audio_24k,
                                _live_model_out_queue(),
                                gemini_resample_state,
                                chunk_ms=_resample_chunk_ms,
                            )
                            if _greeting_handoff_pcm_buffer:
                                logger.debug(
                                    "Gemini Live: buffering model audio during greeting ({} bytes handoff, {} bytes greeting left)",
                                    len(_greeting_handoff_pcm_buffer),
                                    len(prior_16k_queue),
                                )
                            continue

                        if (
                            _model_turn_pcm_bytes_24k >= _MIN_AUDIBLE_MODEL_PCM_24K
                            and not first_byte_logged
                        ):
                            _response_started_after_user = True
                            t0 = response_t0 if response_t0 is not None else last_user_audio_t
                            if t0 is not None:
                                dt = (time.perf_counter() - t0) * 1000.0
                                logger.info(
                                    "Gemini Live: first model-audio chunk — {:.0f} ms since trigger, {} bytes 24kHz PCM (streaming to Vobiz)",
                                    dt,
                                    len(pcm),
                                )
                            else:
                                logger.info(
                                    "Gemini Live: first model-audio chunk — {} bytes 24kHz PCM (no latency baseline yet)",
                                    len(pcm),
                                )
                            first_byte_logged = True
                        _last_model_spoke_t = time.perf_counter()
                        
                        pending_audio_24k.extend(pcm)

                        gemini_resample_state = drain_gemini_24k_to_vobiz_16k(
                            pending_audio_24k,
                            _live_model_out_queue(),
                            gemini_resample_state,
                            chunk_ms=_resample_chunk_ms,
                        )

                    if sc.get("turnComplete") or sc.get("generationComplete"):
                        model_generation_active = False
                        _cta_bumped_this_turn = False
                        _monologue_audio_block = False
                        if not _refusal_detected_this_turn:
                            _refusal_audio_block = False
                        _refusal_detected_this_turn = False
                        # Keep barge-in drop active until playout queue drains (avoids overlap)
                        if len(gemini_16k_queue) == 0 and len(pending_audio_24k) == 0:
                            _barge_in_drop_audio = False
                        _ghost_turn = _model_turn_pcm_bytes_24k < _MIN_AUDIBLE_MODEL_PCM_24K
                        # Gemini can send an empty ``turnComplete`` while audio from the
                        # preceding turn is still waiting to play.  Treating that as a
                        # ghost turn used to clear ``gemini_16k_queue`` below, which cut
                        # the caller-facing answer off mid-sentence.  An empty completion
                        # is recoverable only when there is truly no audio left to play.
                        _recoverable_ghost_turn = (
                            _ghost_turn
                            and len(prior_16k_queue) == 0
                            and len(gemini_16k_queue) == 0
                            and len(pending_audio_24k) == 0
                        )
                        if _recoverable_ghost_turn:
                            pending_audio_24k.clear()
                            gemini_16k_queue.clear()
                            gemini_resample_state = None
                            had_model_audio_turn = False
                            _response_started_after_user = False
                            _user_turn_nudge_sent = False
                            first_byte_logged = False
                            _ghost_stt = (_last_user_stt_snippet or last_in_user or "").strip()
                            if (
                                GHOST_TURN_NUDGE_ENABLED
                                and _ghost_stt
                                and _opening_delivered
                            ):

                                async def _ghost_turn_nudge(stt_text: str) -> None:
                                    try:
                                        await _maybe_respond_nudge(
                                            stt_text, source="ghost_turn", user_turn=True
                                        )
                                        logger.warning(
                                            "Ghost turn ({} bytes PCM) — respond nudge (stt={!r})",
                                            _model_turn_pcm_bytes_24k,
                                            stt_text[:80],
                                        )
                                    except Exception as _gt_err:
                                        logger.warning("Ghost turn nudge failed: {}", _gt_err)

                                _task_gt = asyncio.create_task(_ghost_turn_nudge(_ghost_stt))
                                _background_tasks.add(_task_gt)
                                _task_gt.add_done_callback(_background_tasks.discard)
                            _model_turn_pcm_bytes_24k = 0
                            continue

                        last_meaningful_t = time.perf_counter()
                        _last_turn_pcm_bytes_24k = _model_turn_pcm_bytes_24k
                        if len(prior_16k_queue) == 0 and pending_audio_24k:
                            gemini_resample_state = drain_gemini_24k_to_vobiz_16k(
                                pending_audio_24k,
                                gemini_16k_queue,
                                gemini_resample_state,
                                chunk_ms=_resample_chunk_ms,
                                final_flush=True,
                            )
                        elif len(prior_16k_queue) > 0 and pending_audio_24k:
                            if len(prior_16k_queue) > POST_GREETING_EARLY_NUDGE_BYTES:
                                gemini_resample_state = drain_gemini_24k_to_vobiz_16k(
                                    pending_audio_24k,
                                    _greeting_handoff_pcm_buffer,
                                    gemini_resample_state,
                                    chunk_ms=_resample_chunk_ms,
                                    final_flush=True,
                                )
                            else:
                                gemini_resample_state = drain_gemini_24k_to_vobiz_16k(
                                    pending_audio_24k,
                                    gemini_16k_queue,
                                    gemini_resample_state,
                                    chunk_ms=_resample_chunk_ms,
                                    final_flush=True,
                                )
                        # Clear per-turn prefetch; keep warm connect digest for factual consistency.
                        _warm_rag = rag_prefetch_cache.get("__warm__")
                        rag_prefetch_cache.clear()
                        if _warm_rag:
                            rag_prefetch_cache["__warm__"] = _warm_rag
                        u_turn = (last_in_user or "").strip()
                        if u_turn:
                            append_turn(live_log_id, "user", u_turn, "vobiz-live", base_dir=log_dir)
                            if camp_id:
                                try:
                                    from services.campaign_live import push_transcript
                                    push_transcript(camp_id, "user", u_turn)
                                except Exception as _ce:
                                    logger.warning("live transcript push (user) failed: {}", _ce)
                            last_in_user = ""
                            _pending_transcript["user"] = ""
                        # For the deterministic accepted-handoff turn, prefer the full
                        # expected sentence over a partial streaming transcription.
                        a_turn = (_expected_assistant_transcript or last_out_assistant or "").strip()
                        if a_turn and (
                            _is_explicit_refusal_leak(a_turn) or _is_partial_refusal_leak(a_turn)
                        ):
                            logger.warning("Refusal assistant turn — dropped ({})", a_turn[:160])
                            await _flush_refusal_audio_now()
                            a_turn = ""
                            had_model_audio_turn = False
                        if a_turn and _is_cp_campaign_role(role) and _looks_like_cp_script_rush(a_turn):
                            logger.warning(
                                "CP script rush at turnComplete — truncating ({})",
                                a_turn[:160],
                            )
                            a_turn = _truncate_cp_monologue(a_turn)
                            last_out_assistant = a_turn
                            await _flush_monologue_audio()
                        if a_turn and _should_block_cta_in_output(a_turn):
                            logger.warning(
                                "Blocked excess Account Manager CTA at turnComplete (count={}/{})",
                                _account_manager_cta_count,
                                _ACCOUNT_MANAGER_CTA_MAX,
                            )
                            a_turn = _strip_account_manager_cta(a_turn)
                            await _flush_blocked_cta_audio()
                        if a_turn:
                            if _looks_like_features_pitch_delivered(a_turn):
                                _features_pitch_delivered = True
                            if _looks_like_commission_delivered(a_turn):
                                _commission_delivered = True
                            if _is_cp_campaign_role(role) and _looks_like_turn45_merged(a_turn):
                                _commission_delivered = True
                                _track_account_manager_cta("turn45_merged_complete")
                                logger.warning(
                                    "Turn 4+5 merged at turnComplete — CTA counted once"
                                )
                            elif _looks_like_account_manager_cta_asked(a_turn):
                                _track_account_manager_cta("turnComplete")
                            append_turn(live_log_id, "assistant", a_turn, "vobiz-live", base_dir=log_dir)
                            if camp_id:
                                try:
                                    from services.campaign_live import push_transcript
                                    push_transcript(camp_id, "assistant", a_turn)
                                except Exception as _ce:
                                    logger.warning("live transcript push (assistant) failed: {}", _ce)
                            last_out_assistant = ""
                            _pending_transcript["assistant"] = ""
                            _expected_assistant_transcript = ""
                        elif had_model_audio_turn and len(prior_16k_queue) == 0:
                            append_turn(
                                live_log_id,
                                "assistant",
                                "[Audio reply — Gemini Live; no text transcript]",
                                "vobiz-live",
                                base_dir=log_dir,
                                synthetic="1",
                            )
                        if (
                            had_model_audio_turn
                            and _name_confirmed
                            and _post_name_pitch_nudge_sent
                            and not _pitch_delivered
                            and len(prior_16k_queue) == 0
                        ):
                            _pitch_delivered = True
                            logger.info("Gemini Live: pitch turn delivered after name confirm")
                        if (
                            (had_model_audio_turn or a_turn)
                            and len(prior_16k_queue) == 0
                            and not _ghost_turn
                        ):
                            # Start silence timer after agent finishes; do NOT reset
                            # _user_silence_nudge_count here — only caller STT resets it.
                            _agent_last_finished_t = time.perf_counter()
                            _agent_turn_audio_started_at = 0.0
                        had_model_audio_turn = False
                        _model_turn_pcm_bytes_24k = 0
                        response_t0 = None
                        first_byte_logged = False
                        last_rag_inject_key = ""
                        # Keep gemini_resample_state across turns — resetting causes click/garble.

                logger.info("Gemini Live upstream WebSocket recv loop ended")

            async def pump_mixed_to_vobiz() -> None:
                nonlocal _last_user_spoke_t, last_meaningful_t, model_generation_active, gemini_resample_state, _post_greeting_grace_until, _suppress_gemini_until_nudge, data, _outbound_playout_active, _debug_first_playout, _authoritative_lead_name, _post_nudge_echo_relax_until, _post_greeting_nudge_armed, _prefetch_live_handoff, _local_barge_in_requested
                _stream_wait = 0.75 if _prior_opening_bytes_at_connect > 0 else 2.0
                if not vobiz_stream_started.is_set():
                    try:
                        await asyncio.wait_for(vobiz_stream_started.wait(), timeout=_stream_wait)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vobiz playout: stream start wait {:.1f}s expired — starting audio anyway (camp={})",
                            _stream_wait,
                            camp_id,
                        )
                        _mark_vobiz_stream_ready("playout_wait_timeout")
                if _prior_opening_bytes_at_connect > 0:
                    _prefetch_live_handoff = True
                    logger.info(
                        "Greeting prefetch: mic→Gemini during full greeting ({} bytes PCM)",
                        _prior_opening_bytes_at_connect,
                    )

                    async def _arm_name_verify_when_setup_ready() -> None:
                        # Deprecated: nudge must fire only after greeting PCM fully drains.
                        return

                _vobiz_ultra = bool(getattr(settings, "vobiz_ultra_low_latency", False))
                # 20 ms frames = smoother human voice on PSTN (10 ms ultra sounds choppy/robotic).
                _frame_ms = 20.0
                chunk_bytes = vobiz_frame_bytes_16k(_frame_ms)
                _tick_sec = chunk_bytes / float(VOBIZ_SR * 2)
                bg_pos = 0
                # Burst 400ms at greeting start to fill carrier jitter buffer (reference flow).
                next_wakeup = time.perf_counter()
                if _prior_opening_bytes_at_connect > 0:
                    next_wakeup -= 0.40

                _playout_pre = float(settings.vobiz_playout_prebuffer_seconds or 0.02)
                if _prior_opening_bytes_at_connect > 0:
                    _playout_pre = max(_playout_pre, 0.06)
                # Ultra mode: ~20ms floor for sub-150ms first-byte target.
                # Standard mode: 40ms floor to absorb PSTN jitter.
                _playout_floor = 0.02 if _vobiz_ultra else 0.04
                _playout_pre = max(_playout_pre, _playout_floor)
                PREBUFFER_BYTES = int(VOBIZ_SR * 2 * _playout_pre)
                MIN_PLAYOUT_BYTES = PREBUFFER_BYTES
                is_playing_gemini = False
                # Echo-gate hysteresis clock: last time we actually played a voice chunk.
                _last_outbound_voice_t = time.perf_counter()
                _playout_active_hold = float(os.getenv("PLAYOUT_ACTIVE_HOLD_SEC", "3.5"))

                async def _send_post_greeting_nudge_once() -> None:
                    nonlocal _suppress_gemini_until_nudge, last_meaningful_t, _last_user_spoke_t, _post_greeting_grace_until, _post_nudge_echo_relax_until, _name_verify_asked
                    if state.recorded_greeting_nudge_sent:
                        return
                    # Greeting listen result: automated → voicemail flow (skip name verify).
                    if _callee_class == "automated" and _callee_vm_kind:
                        logger.info(
                            "Post-greeting: automated callee detected during PCM — routing to voicemail ({})",
                            _callee_vm_kind,
                        )
                        state.recorded_greeting_nudge_sent = True
                        _suppress_gemini_until_nudge = False
                        _arm_post_greeting_grace(0.6)
                        await _enter_voicemail_mode(
                            _callee_vm_kind,
                            source="greeting_listen_handoff",
                            stt=" ".join(_greeting_stt_snippets[-4:]),
                        )
                        return
                    if _user_has_spoken and _callee_class != "human":
                        # Spoke during greeting but not classified human — still run name verify.
                        logger.info(
                            "Gemini Live: callee spoke during greeting (class={}) — continuing name verify",
                            _callee_class,
                        )
                    elif _user_has_spoken and _callee_class == "human":
                        logger.info(
                            "Gemini Live: human detected during greeting — name verify after PCM"
                        )
                    try:
                        if _had_scripted_name_verify:
                            logger.info("Gemini Live: pre-recorded name-verify played — sending wait-for-user nudge")
                            state.recorded_greeting_nudge_sent = True
                            _name_verify_asked = True
                            _suppress_gemini_until_nudge = False
                            _arm_post_greeting_grace(0.6)
                            _post_nudge_echo_relax_until = time.perf_counter() + 0.45
                            await gemini_send_post_pcm_name_verify_nudge(
                                gem,
                                greeting_text=opening_line,
                                greeting_name=_authoritative_lead_name or "",
                                is_retry=False,
                                is_callback=False,
                                wait_for_user=True,
                            )
                            last_meaningful_t = time.perf_counter()
                            _last_user_spoke_t = time.perf_counter()
                            logger.info(
                                "Gemini Live: sent wait-for-user nudge after scripted name-verify (name={!r})",
                                _authoritative_lead_name or "",
                            )
                            return
                        await gem.connected_event.wait()
                        if gem.conn_error:
                            raise gem.conn_error
                        if _setup_task and not _setup_task.done():
                            await _setup_task
                        try:
                            await asyncio.wait_for(gemini_setup_ready.wait(), timeout=3.0)
                        except asyncio.TimeoutError:
                            logger.warning("Gemini Live: setupComplete not seen within 3s — nudging anyway")
                        greeting_name = _authoritative_lead_name
                        if not greeting_name and isinstance(data, dict):
                            from core.opening_line import looks_like_real_name
                            raw_nm = str(data.get("name") or "").strip()
                            if looks_like_real_name(raw_nm):
                                greeting_name = _resolve_authoritative_lead_name(data)
                        is_callback = bool(isinstance(data, dict) and data.get("_is_scheduled_callback"))
                        lead_extra = {}
                        if isinstance(data, dict) and data.get("extra"):
                            try:
                                raw_ext = data.get("extra")
                                lead_extra = json.loads(raw_ext) if isinstance(raw_ext, str) else (raw_ext or {})
                            except Exception:
                                pass
                        is_retry = int(lead_extra.get("failed_call_retries") or 0) > 0
                        _suppress_gemini_until_nudge = False
                        state.recorded_greeting_nudge_sent = True
                        _name_verify_asked = True
                        await gemini_send_post_pcm_name_verify_nudge(
                            gem,
                            greeting_text=opening_line,
                            greeting_name=greeting_name or "",
                            is_retry=is_retry,
                            is_callback=is_callback,
                            wait_for_user=False,
                        )
                        _arm_post_greeting_grace(0.6)
                        _post_nudge_echo_relax_until = time.perf_counter() + 0.45
                        last_meaningful_t = time.perf_counter()
                        _last_user_spoke_t = time.perf_counter()
                        logger.info(
                            "Gemini Live: PCM greeting finished — sent name-verify nudge (name={!r})",
                            greeting_name or "",
                        )
                    except Exception as ne:
                        logger.warning("Gemini Live: post-PCM nudge failed: {}", ne)

                while True:
                    if _local_barge_in_requested:
                        _local_barge_in_requested = False
                        await _apply_local_barge_in("mic_streak_playout")
                    gemini_pcm = b""
                    if len(prior_16k_queue) > 0:
                        if (
                            len(prior_16k_queue) <= POST_GREETING_EARLY_NUDGE_BYTES
                            and not _prefetch_live_handoff
                        ):
                            _prefetch_live_handoff = True
                            logger.info(
                                "Scripted greeting prefetch handoff ({} bytes left) — warming live session",
                                len(prior_16k_queue),
                            )
                            # 🚀 EARLY NUDGE: Send name-verify nudge while ~600ms greeting remains.
                            # Gemini processes the nudge (text → thinking → audio) in parallel with the
                            # last PCM playout, so its response is ready the instant the greeting finishes.
                            # This cuts post-greeting delay from ~10s to near-zero.
                            if not state.recorded_greeting_nudge_sent and not _post_greeting_nudge_armed:
                                _post_greeting_nudge_armed = True
                                _task = asyncio.create_task(_send_post_greeting_nudge_once())
                                _background_tasks.add(_task)
                                _task.add_done_callback(_background_tasks.discard)
                        gemini_pcm = pop_l16_chunk(prior_16k_queue, chunk_bytes)
                        is_playing_gemini = False
                        if len(prior_16k_queue) == 0:
                            logger.info("Scripted greeting finished draining — handoff to Gemini Live.")
                            _merge_greeting_handoff_buffer()
                            _last_user_spoke_t = time.perf_counter()
                            last_meaningful_t = time.perf_counter()
                            _arm_post_greeting_grace(0.6)
                            _post_nudge_echo_relax_until = time.perf_counter() + 0.45
                            if camp_id and camp_id in _CAMPAIGN_DATA:
                                data = _CAMPAIGN_DATA[camp_id]
                                _authoritative_lead_name = _resolve_authoritative_lead_name(data)
                            if not state.recorded_greeting_nudge_sent and not _post_greeting_nudge_armed:
                                _post_greeting_nudge_armed = True
                                _task = asyncio.create_task(_send_post_greeting_nudge_once())
                                _background_tasks.add(_task)
                                _task.add_done_callback(_background_tasks.discard)
                    else:
                        q_len = len(gemini_16k_queue)
                        if not is_playing_gemini:
                            if q_len >= MIN_PLAYOUT_BYTES or (not model_generation_active and q_len > 0):
                                is_playing_gemini = True
                                logger.info(
                                    "Gemini outbound jitter buffer filled ({} bytes, min={}, active={}) — starting playout",
                                    q_len,
                                    MIN_PLAYOUT_BYTES,
                                    model_generation_active,
                                )
                        if is_playing_gemini:
                            if q_len > 0:
                                gemini_pcm = pop_l16_chunk(gemini_16k_queue, chunk_bytes)
                            else:
                                gemini_pcm = b""
                                is_playing_gemini = False


                    mixed, bg_pos = mix_voice_and_background_tick(
                        gemini_pcm or (b"\x00" * chunk_bytes),
                        bg_audio,
                        bg_volume,
                        bg_pos,
                        len(gemini_pcm) // 2 if gemini_pcm else chunk_bytes // 2,
                    )
                    # Echo-gate hysteresis: keep playout flagged "active" for
                    # _playout_active_hold seconds after the last real voice chunk so the
                    # agent's own PSTN echo cannot trip local barge-in during the
                    # micro-gaps between Gemini audio bursts. Without this the agent
                    # cut itself mid-sentence and resumed 4-8s later ("fast-forwarding").
                    if bool(gemini_pcm) and pcm_s16le_rms(gemini_pcm) >= 80.0:
                        _last_outbound_voice_t = time.perf_counter()
                    _outbound_playout_active = (
                        len(prior_16k_queue) > 0
                        or (time.perf_counter() - _last_outbound_voice_t) < _playout_active_hold
                    )
                    # #region agent log
                    if (
                        not _debug_first_playout
                        and len(prior_16k_queue) == 0
                        and bool(gemini_pcm)
                        and pcm_s16le_rms(gemini_pcm) >= 80.0
                    ):
                        _debug_first_playout = True
                        try:
                            from debug_agent_log import agent_debug

                            agent_debug(
                                "E",
                                "live_session.py:pump_mixed_to_vobiz",
                                "first_real_voice_playout_to_vobiz",
                                {
                                    "gem_pcm_bytes": len(gemini_pcm),
                                    "gem_q_remaining": len(gemini_16k_queue),
                                    "out_rms": round(pcm_s16le_rms(gemini_pcm), 1),
                                },
                            )
                        except Exception:
                            pass
                    # #endregion
                    try:
                        await send_play_audio(
                            ws,
                            mixed,
                            VOBIZ_SR,
                            call_recorder=state.call_recorder,
                            stream_id=state.stream_id or None,
                        )
                        if len(gemini_16k_queue) > 16000:
                            logger.warning("Vobiz outbound audio queue backing up: {} bytes", len(gemini_16k_queue))
                    except Exception as e:
                        logger.warning("Vobiz playAudio send failed: {}", e)

                    next_wakeup += _tick_sec
                    sleep_time = next_wakeup - time.perf_counter()
                    if sleep_time > 0:
                        bulk_sleep = sleep_time - 0.003
                        if bulk_sleep > 0:
                            await asyncio.sleep(bulk_sleep)
                        while time.perf_counter() < next_wakeup:
                            await asyncio.sleep(0.001)
                    elif sleep_time < -2.0:
                        next_wakeup = time.perf_counter()

            async def silence_watchdog() -> None:
                nonlocal last_meaningful_t
                """Hang up the call if neither side has done anything meaningful for
                ``SILENCE_HANGUP_SEC`` seconds. Belt-and-braces fallback for cases
                where the model never invokes ``end_call`` (e.g. stuck silence)."""
                while True:
                    await asyncio.sleep(5.0)
                    if time.perf_counter() < _post_greeting_grace_until:
                        last_meaningful_t = time.perf_counter()
                        continue
                    idle = time.perf_counter() - last_meaningful_t
                    if idle >= SILENCE_HANGUP_SEC:
                        logger.warning(
                            "Silence watchdog: idle for {:.0f}s (>= {:.0f}s) — REST hangup (call_uuid={})",
                            idle,
                            SILENCE_HANGUP_SEC,
                            state.call_id,
                        )
                        # #region agent log
                        try:
                            from debug_agent_log import agent_debug

                            agent_debug(
                                "E",
                                "live_session.py:silence_watchdog",
                                "early_hangup",
                                {
                                    "reason": "silence_watchdog",
                                    "idle_sec": round(idle, 1),
                                    "call_uuid": state.call_id or "",
                                },
                            )
                        except Exception:
                            pass
                        # #endregion
                        await terminate_call(
                            ws,
                            call_uuid=state.call_id,
                            auth_id=vobiz_auth_id,
                            auth_token=vobiz_auth_token,
                            drain_seconds=0.0,
                        )
                        return

            async def _user_silence_prodder() -> None:
                nonlocal _agent_last_finished_t, _user_silence_nudge_count, _last_silence_nudge_at, _last_nudge_kind, model_generation_active, _closing_mode
                while True:
                    await asyncio.sleep(1.0)
                    if time.perf_counter() < _post_greeting_grace_until:
                        continue
                    if time.perf_counter() < _last_silence_nudge_at:
                        continue
                    if not _opening_delivered or _is_voicemail_mode:
                        continue
                    if _closing_mode:
                        continue
                    if _prior_opening_bytes_at_connect > 0 and not state.recorded_greeting_nudge_sent:
                        continue
                    if len(prior_16k_queue) > 0:
                        continue
                    if len(gemini_16k_queue) > 0 or model_generation_active:
                        continue
                    if _agent_last_finished_t <= 0:
                        continue
                    # Caller is still talking — wait for STT/activityEnd before any prod.
                    if (time.perf_counter() - _last_user_spoke_t) < 1.5:
                        continue
                    silence_sec = time.perf_counter() - _agent_last_finished_t
                    if silence_sec >= USER_SILENCE_HANGUP_SEC:
                        logger.warning(
                            "Silence prodder: {:.0f}s after agent finished — hanging up (call_uuid={})",
                            silence_sec,
                            state.call_id,
                        )
                        await terminate_call(
                            ws,
                            call_uuid=state.call_id,
                            auth_id=vobiz_auth_id,
                            auth_token=vobiz_auth_token,
                            drain_seconds=0.0,
                        )
                        return

                    if _user_silence_nudge_count >= 2:
                        continue

                    _next_prod_at = (
                        USER_SILENCE_FIRST_PROD_SEC
                        + (_user_silence_nudge_count * USER_SILENCE_REPEAT_PROD_SEC)
                    )
                    if silence_sec >= _next_prod_at:
                        try:
                            from .gemini_protocol import gemini_send_user_silence_nudge

                            await gemini_send_user_silence_nudge(gem)
                            _user_silence_nudge_count += 1
                            _last_silence_nudge_at = time.perf_counter()
                            _last_nudge_kind = "silence"
                            logger.info(
                                "Silence prodder: check-in {}/2 after {:.0f}s since agent finished (call={})",
                                _user_silence_nudge_count,
                                silence_sec,
                                state.call_id,
                            )
                        except Exception as _sp_exc:
                            logger.warning("Silence prodder nudge failed: {}", _sp_exc)

            async def _call_quality_healer() -> None:
                nonlocal _user_silence_nudge_count, _continue_explanation_nudge_sent, _ai_disclosure_nudge_sent
                while True:
                    await asyncio.sleep(2.0)
                    if _is_voicemail_mode or not _opening_delivered:
                        continue
                    if _silence_checkin_spoken_count >= 2 and _user_silence_nudge_count < 99:
                        _user_silence_nudge_count = 99
                        logger.info("Call heal: blocked repeat silence check-ins for rest of call")
                    if (
                        not _continue_explanation_nudge_sent
                        and not _is_cp_campaign_role(role)
                        and _opening_delivered
                        and len(prior_16k_queue) == 0
                        and not model_generation_active
                        and len(gemini_16k_queue) == 0
                    ):
                        _stt_q = (_last_user_stt_snippet or "").lower()
                        _open_q = bool(
                            re.search(
                                r"\b(tell me|about the project|about solitaire|explain|what is|describe)\b",
                                _stt_q,
                            )
                        )
                        _ai_q = _stt_asks_ai_identity(_last_user_stt_snippet or "")
                        if (
                            (_open_q or _ai_q)
                            and 0 < _last_turn_pcm_bytes_24k < _MIN_AUDIBLE_MODEL_PCM_24K
                            and (time.perf_counter() - _agent_last_finished_t) < 8.0
                            and _agent_last_finished_t > 0
                        ):
                            _continue_explanation_nudge_sent = True
                            try:
                                from .gemini_protocol import gemini_send_continue_explanation_nudge

                                await gemini_send_continue_explanation_nudge(gem)
                                logger.info(
                                    "Call heal: continue-explanation nudge (prior turn {} bytes PCM)",
                                    _last_turn_pcm_bytes_24k,
                                )
                            except Exception as _ce_exc:
                                logger.warning("Continue-explanation nudge failed: {}", _ce_exc)

            task_in = asyncio.create_task(pump_vobiz_to_gemini())
            task_out = asyncio.create_task(pump_gemini_to_queue())
            task_mix = asyncio.create_task(pump_mixed_to_vobiz())
            task_dog = asyncio.create_task(silence_watchdog())
            task_prodder = asyncio.create_task(_user_silence_prodder())
            task_healer = asyncio.create_task(_call_quality_healer())
            task_greeting_listen = asyncio.create_task(_greeting_listen_loop())
            task_vm_flow = asyncio.create_task(_voicemail_flow_watchdog())
            task_vm_nohuman = asyncio.create_task(_voicemail_no_human_watchdog())
            task_stream_fallback = asyncio.create_task(_vobiz_stream_start_fallback())
            for _t in (task_greeting_listen, task_vm_flow, task_vm_nohuman, task_stream_fallback):
                _background_tasks.add(_t)
                _t.add_done_callback(_background_tasks.discard)

            # When no scripted PCM: prompt Gemini to speak the opening as soon as the leg is up.
            needs_live_opening_nudge = _prior_opening_bytes_at_connect == 0 and bool(
                (opening_line or "").strip()
            )
            if not needs_live_opening_nudge:
                _opening_delivered = True  # PCM handles opening; mark delivered immediately

            async def _send_live_opening_nudge(label: str) -> None:
                if not needs_live_opening_nudge:
                    return
                try:
                    await gemini_send_live_opening_turn_nudge(gem, inbound=_is_incoming_leg)
                    logger.info("Gemini Live: opening nudge sent ({})", label)
                except Exception as exc:
                    logger.warning("Gemini Live: opening nudge failed ({}): {}", label, exc)

            async def _opening_nudge_after_stream() -> None:
                nonlocal _opening_delivered, _last_user_spoke_t, _suppress_gemini_until_nudge
                try:
                    await vobiz_stream_started.wait()
                except Exception as exc:
                    logger.debug("opening nudge: vobiz stream wait ended: {}", exc)
                    return
                # Add 100ms for stream stabilisation
                await asyncio.sleep(0.1)
                if not needs_live_opening_nudge:
                    _opening_delivered = True
                    if _is_incoming_leg and _prior_opening_bytes_at_connect > 0:
                        state.recorded_greeting_nudge_sent = True
                        _suppress_gemini_until_nudge = False
                        logger.info(
                            "incoming_opening_nudge_sent skipped — PCM already queued ({} bytes)",
                            _prior_opening_bytes_at_connect,
                        )
                    return
                # Brief settle so Gemini WS setup can complete before first nudge
                await asyncio.sleep(0.5)
                await _send_live_opening_nudge("post-stream-start")
                logger.info("incoming_opening_nudge_sent" if _is_incoming_leg else "opening_nudge_sent")
                _opening_delivered = True
                if _is_incoming_leg:
                    state.recorded_greeting_nudge_sent = True
                    _suppress_gemini_until_nudge = False
                # Retry once if still no model audio after 3s
                await asyncio.sleep(3.0)
                if (
                    needs_live_opening_nudge
                    and len(gemini_16k_queue) == 0
                    and not model_generation_active
                    and len(prior_16k_queue) == 0
                ):
                    await _send_live_opening_nudge("retry-3s")

            _task = asyncio.create_task(_opening_nudge_after_stream())
            _background_tasks.add(_task)
            _task.add_done_callback(_background_tasks.discard)

            _core_tasks = {task_in, task_out, task_mix, task_dog, task_prodder, task_healer}
            pending = set(_core_tasks)
            try:
                while pending:
                    done, pending = await asyncio.wait(
                        pending,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for ft in done:
                        exc = ft.exception()
                        if exc is not None:
                            if isinstance(exc, ws_client.ConnectionClosed):
                                logger.error(
                                    "Gemini Live WebSocket closed — native audio stopped (close code={}, reason={!r}). "
                                    "Recorded/scripted greeting may already have played; this is unrelated to prior_16k_queue. "
                                    "If Google says denied access / 1008, fix API key billing, project Live entitlement, "
                                    "or Gemini Live preview access for your account.",
                                    exc.code,
                                    exc.reason,
                                )
                            else:
                                logger.error("Task {} completed with unhandled exception: {}", ft, exc, exc_info=exc)
                    # Record until Vobiz leg ends — do not stop when Gemini task exits early.
                    if task_in in done:
                        break
            finally:
                for t in _core_tasks:
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
    except Exception as exc:
        logger.exception("Vobiz live WS error: {}", exc)
    finally:
        # Flush STT buffered at disconnect (turnComplete may not fire on hangup).
        for _role_label, _content in (
            ("user", (_pending_transcript.get("user") or "").strip()),
            ("assistant", (_pending_transcript.get("assistant") or "").strip()),
        ):
            if _content:
                append_turn(live_log_id, _role_label, _content, "vobiz-live", base_dir=log_dir)
                if camp_id:
                    try:
                        from services.campaign_live import push_transcript

                        push_transcript(camp_id, _role_label, _content)
                    except Exception as _ce:
                        logger.warning("live transcript flush ({}) failed: {}", _role_label, _ce)
        _pending_transcript["user"] = ""
        _pending_transcript["assistant"] = ""

        # Release Vobiz call slot for incoming calls
        if camp_id and str(camp_id).startswith("incoming_"):
            release_vobiz_call_slot(role)
            logger.info("Released Vobiz call slot for incoming call role={}", role)

        # Track call duration in campaign data
        dur_sec: Optional[float] = None
        if camp_id and str(camp_id).startswith("incoming_") and _incoming_connected_at:
            dur_sec = round(time.time() - _incoming_connected_at, 1)
            logger.info("Incoming call {} ended — duration: {:.0f}s", camp_id, dur_sec or 0)
        if camp_id and camp_id in _CAMPAIGN_DATA:
            connected_at = _CAMPAIGN_DATA[camp_id].get("_call_connected_at")
            if connected_at:
                duration = time.time() - connected_at
                _CAMPAIGN_DATA[camp_id]["call_duration_sec"] = round(duration, 1)
                ended_at = time.time()
                _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = ended_at
                try:
                    from core.camp_session import mark_camp_ended

                    await mark_camp_ended(camp_id, ended_at)
                except Exception as exc:
                    logger.warning("Persist camp ended_at failed for camp_id={}: {}", camp_id, exc)
                dur_sec = float(_CAMPAIGN_DATA[camp_id]["call_duration_sec"])
                logger.info(f"Call {camp_id} ended — duration: {duration:.0f}s")

                # Post-call WhatsApp: send queued package immediately (AI stays responsive during call)
                try:
                    from core.worker import flush_pending_whatsapp_after_call
                    _wa_task = asyncio.create_task(flush_pending_whatsapp_after_call(role, camp_id))
                    _background_tasks.add(_wa_task)
                    _wa_task.add_done_callback(_background_tasks.discard)
                except Exception as _wa_flush_exc:
                    logger.warning("Post-call WhatsApp flush schedule failed: {}", _wa_flush_exc)
                
                # Auto-trigger analysis
                lead_id = _CAMPAIGN_DATA[camp_id].get("_lead_id")
                if lead_id:
                    from core.worker import _analyze_and_update_lead
                    _cb_id = _CAMPAIGN_DATA[camp_id].get("_scheduled_callback_id")
                    _task = asyncio.create_task(
                        _analyze_and_update_lead(
                            role,
                            lead_id,
                            live_log_id,
                            callback_id=_cb_id,
                            camp_id=str(camp_id or ""),
                        )
                    )
                    _background_tasks.add(_task)
                    _task.add_done_callback(_background_tasks.discard)

        if camp_id and live_log_id:
            mem = _CAMPAIGN_DATA.get(camp_id) if camp_id in _CAMPAIGN_DATA else None
            is_manual = bool(isinstance(mem, dict) and mem.get("_manual_leg"))
            if not is_manual and str(camp_id).startswith("manual_"):
                from core.storage import manual_call_exists_for_camp

                is_manual = await manual_call_exists_for_camp(camp_id)
            if is_manual:
                from core.worker import _finalize_manual_call_leg

                _task = asyncio.create_task(_finalize_manual_call_leg(role, camp_id, live_log_id, dur_sec))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)
            elif camp_id and str(camp_id).startswith("incoming_"):
                from core.worker import _finalize_incoming_call_leg

                _task = asyncio.create_task(_finalize_incoming_call_leg(role, camp_id, live_log_id, dur_sec))
                _background_tasks.add(_task)
                _task.add_done_callback(_background_tasks.discard)

        # Application recording: poll Vobiz Recording API after call ends (outbound + inbound).
        _call_uuid = str(getattr(state, "call_id", "") or "").strip()
        if live_log_id and _call_uuid and getattr(settings, "vobiz_trunk_recording_enabled", True):
            try:
                from core.vobiz_credentials import resolve_vobiz_credentials
                from services.vobiz_bridge.vobiz_recording import schedule_vobiz_application_recording_ingest

                _auth_id, _, _, _ = resolve_vobiz_credentials(role or "sales_1")
                _rec_task = asyncio.create_task(
                    schedule_vobiz_application_recording_ingest(
                        {
                            "CallUUID": _call_uuid,
                            "auth_id": _auth_id,
                            "Event": "Hangup",
                        },
                        delay_sec=20.0,
                    )
                )
                _background_tasks.add(_rec_task)
                _rec_task.add_done_callback(_background_tasks.discard)
                logger.info(
                    "Scheduled Vobiz application recording ingest log_id={} call_uuid={}",
                    live_log_id,
                    _call_uuid,
                )
            except Exception as _rec_exc:
                logger.warning("Application recording ingest schedule failed: {}", _rec_exc)

        if _dev_mode_active:
            _finetune_call_id = str(live_log_id or camp_id or "")
            if _finetune_call_id:
                try:
                    from services.voice_finetune import apply_dev_finetune_for_call_async

                    _ft_task = asyncio.create_task(
                        apply_dev_finetune_for_call_async(_finetune_call_id)
                    )
                    _background_tasks.add(_ft_task)
                    _ft_task.add_done_callback(_background_tasks.discard)
                    logger.info(
                        "Voice fine-tune apply scheduled for dev-mode call {}",
                        _finetune_call_id,
                    )
                except Exception as _ft_exc:
                    logger.warning("Voice fine-tune apply schedule failed: {}", _ft_exc)

        # Close the local CallRecorder (writes mixed WAV, archives, etc.)
        if state.call_recorder is not None:
            try:
                state.call_recorder.close()
                logger.info("CallRecorder closed for session {}", live_log_id)
            except Exception as _rec_close_err:
                logger.warning("CallRecorder close failed: {}", _rec_close_err)

        try:
            await ws.close()
        except Exception as exc:
            logger.debug("WebSocket close failed: {}", exc)
        logger.info("Vobiz WS (live): closed")
