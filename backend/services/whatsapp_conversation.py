"""Conversation history store + Gemini analysis for WhatsApp auto-replies."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from config import settings

_CONVERSATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversation_messages(phone);
CREATE INDEX IF NOT EXISTS idx_conv_phone_created ON conversation_messages(phone, created_at);
"""


def _ensure_table():
    from core.storage import _get_conn

    conn = _get_conn()
    for stmt in _CONVERSATIONS_TABLE.split(";"):
        s = stmt.strip()
        if s:
            conn.execute(s)
    conn.commit()


def add_message(phone: str, role: str, text: str) -> None:
    _ensure_table()
    from core.storage import _get_conn

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversation_messages (phone, role, message, created_at) VALUES (?, ?, ?, ?)",
        (phone, role, text, now),
    )
    conn.commit()


def get_history(phone: str, limit: int = 10) -> list[dict[str, str]]:
    _ensure_table()
    from core.storage import _get_conn

    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, message FROM conversation_messages WHERE phone = ? ORDER BY created_at DESC LIMIT ?",
        (phone, limit),
    ).fetchall()
    messages = []
    for r in reversed(rows):
        messages.append({"role": r["role"], "message": r["message"]})
    return messages


def clear_history(phone: str) -> None:
    _ensure_table()
    from core.storage import _get_conn

    conn = _get_conn()
    conn.execute("DELETE FROM conversation_messages WHERE phone = ?", (phone,))
    conn.commit()


_VISIT_KEYWORDS = (
    "visit", "schedule", "willing", "tomorrow", "weekend", "come see",
    "see the site", "see site", "visit the site", "visit site",
)
_NOT_INTERESTED_PHRASES = (
    "not interested", "no thanks", "no thank you", "don't want", "dont want",
    "no more", "remove me", "unsubscribe", "stop", "don't call", "dont call",
    "do not call", "no need",
)


def _detect_site_visit_intent(message_text: str) -> bool:
    """Deterministic direct-booking bypass check (plan Phase 6).

    Works without Gemini so a willing-to-visit reply is honored even when the
    API key is missing or the API call fails.
    """
    low = (message_text or "").lower()
    if any(p in low for p in _NOT_INTERESTED_PHRASES):
        return False
    return any(k in low for k in _VISIT_KEYWORDS)


async def _apply_site_visit_bypass(phone: str, message_text: str) -> None:
    """Route a willing-to-visit WhatsApp reply straight to the Site Visit state.

    Cancels pending WhatsApp nudge / no-reply follow-up jobs so no further
    automation runs after the direct booking (plan Phase 6 'Direct Booking
    Bypass').
    """
    try:
        from core.storage import find_lead_by_phone_any_role, _get_conn

        lead = await find_lead_by_phone_any_role(phone)
        if not lead:
            return
        conn = _get_conn()
        conn.execute(
            "UPDATE leads SET status='site_visit',"
            "lifecycle_status='site_visit_scheduled', sandbox=3,"
            "updated_at=datetime('now') WHERE id=?",
            (int(lead["id"]),),
        )
        conn.execute(
            "UPDATE workflow_jobs SET status='cancelled',"
            "error='Site visit agreed on WhatsApp',updated_at=datetime('now')"
            " WHERE lead_id=? AND job_type IN ('whatsapp_followup_24h','interested_followup')"
            "  AND status IN ('scheduled','ready','claimed')",
            (int(lead["id"]),),
        )
        conn.commit()
        logger.info("Lead {} transitioned to site visit via WhatsApp", phone)
    except Exception as err:
        logger.error("Failed to update lead status on WhatsApp site visit reply: {}", err)


async def analyze_inbound_message(
    phone: str,
    message_text: str,
) -> dict[str, Any]:
    """Analyze an inbound WhatsApp message using Gemini.

    Returns:
        {"should_respond": bool, "intent": str, "response": str,
         "send_project_details": bool}
    """
    history = get_history(phone)
    history.append({"role": "user", "message": message_text})

    # Deterministic direct-booking bypass first (plan Phase 6): a clear
    # willing-to-visit reply is honored even without Gemini. Only "not
    # interested" style negations suppress it.
    if _detect_site_visit_intent(message_text):
        await _apply_site_visit_bypass(phone, message_text)
        return {
            "should_respond": True,
            "intent": "site_visit",
            "response": "",
            "send_project_details": True,
            "site_visit_agreed": True,
        }

    rag_block = ""
    try:
        from services.chunk_rag import full_chunk_block

        rag_block = full_chunk_block("sales_1", max_chars=30000)
    except Exception as exc:
        logger.debug("WhatsApp full RAG skipped: {}", exc)

    prompt_path = os.path.join(
        os.path.dirname(__file__), "..", "prompts", "whatsapp_assistant_prompt.txt"
    )
    try:
        with open(prompt_path, encoding="utf-8") as f:
            system_prompt = f.read()
    except FileNotFoundError:
        system_prompt = _DEFAULT_PROMPT

    if rag_block:
        system_prompt += f"\n\n{rag_block}"

    from core.gemini_auth import gemini_auth_headers, gemini_generate_content_url, get_gemini_api_key

    key = get_gemini_api_key()
    if not key:
        logger.warning("WhatsApp conversation: GEMINI_API_KEY not set, skipping analysis")
        return _no_response()

    model = (settings.gemini_call_analysis_model or "gemini-3.1-flash-lite").strip()
    url = gemini_generate_content_url(model)

    contents = []
    for msg in history:
        contents.append({
            "role": "user" if msg["role"] == "user" else "model",
            "parts": [{"text": msg["message"]}],
        })

    body = {
        "systemInstruction": {
            "parts": [{"text": system_prompt}],
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }

    last_error = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, json=body, headers=gemini_auth_headers(key))
                if resp.status_code >= 500 and attempt < 3:
                    last_error = f"Gemini {resp.status_code}"
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                parsed = json.loads(text)
                if not isinstance(parsed, dict):
                    return _no_response()
                    
                intent = str(parsed.get("intent", "other")).lower()
                send_details = bool(parsed.get("send_project_details", False))
                
                # Check for direct Site Visit willingness bypass
                is_site_visit = "visit" in intent or "site_visit" in intent or any(
                    w in message_text.lower() for w in ("visit", "schedule", "willing", "tomorrow", "weekend", "come see")
                )
                
                if is_site_visit:
                    try:
                        from core.storage import find_lead_by_phone_any_role, _get_conn
                        lead = await find_lead_by_phone_any_role(phone)
                        if lead:
                            conn = _get_conn()
                            conn.execute(
                                """UPDATE leads SET status='site_visit',
                                lifecycle_status='site_visit_scheduled', sandbox=3,
                                updated_at=datetime('now') WHERE id=?""",
                                (int(lead["id"]),),
                            )
                            conn.execute(
                                """UPDATE workflow_jobs SET status='cancelled',
                                error='Site visit agreed on WhatsApp',updated_at=datetime('now')
                                WHERE lead_id=? AND job_type IN ('whatsapp_followup_24h','interested_followup')
                                  AND status IN ('scheduled','ready','claimed')""",
                                (int(lead["id"]),),
                            )
                            conn.commit()
                            logger.info("Lead {} transitioned to site visit via WhatsApp", phone)
                    except Exception as err:
                        logger.error("Failed to update lead status on WhatsApp site visit reply: {}", err)

                return {
                    "should_respond": bool(parsed.get("should_respond", False)),
                    "intent": intent,
                    "response": str(parsed.get("response", "")),
                    "send_project_details": send_details,
                    "site_visit_agreed": is_site_visit,
                }
        except Exception as e:
            last_error = str(e)
            logger.warning("Gemini analysis attempt {}/3 failed: {}", attempt, e)

    logger.warning("WhatsApp analysis failed after retries: {}", last_error)
    return _no_response()


def _no_response() -> dict[str, Any]:
    return {
        "should_respond": False,
        "intent": "other",
        "response": "",
        "send_project_details": False,
        "site_visit_agreed": False,
    }


_DEFAULT_PROMPT = """You are the WhatsApp assistant for Technopolis Constructions (Solitaire Unity premium apartments in Kondapur, Hyderabad).
Review the conversation and determine what the customer is asking for.
Output JSON: {"should_respond": bool, "intent": str, "response": str, "send_project_details": bool}
Only respond when they ask about project details, pricing, site visits, or the brochure.
For greetings or chit-chat, set should_respond to false.
"""
