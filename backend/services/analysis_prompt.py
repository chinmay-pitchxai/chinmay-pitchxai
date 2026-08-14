"""Shared transcript QA prompt + JSON parsing for call analysis backends."""

from __future__ import annotations

import json
from datetime import datetime

from config import settings
from services.callback_time import zoneinfo_safe


def empty_transcript_result(*, summary: str, rationale: str) -> dict:
    return {
        "summary": summary,
        "rating": 0,
        "next_steps": "N/A",
        "disposition": "Answered",
        "emotion_label": "Unknown",
        "emotion_rationale": rationale,
        "emotion_confidence": None,
        "requested_callback_datetime_iso": None,
        "site_visit_agreed": False,
        "preferred_location": None,
        "preferred_budget": None,
        "email_address": None,
    }


def _truncate_transcript_for_analysis(transcript_text: str, *, max_chars: int = 8000) -> str:
    """Keep head + tail of long transcripts so Gemini stays under latency budget."""
    text = transcript_text or ""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 80
    return (
        text[:head]
        + "\n\n[... middle of transcript omitted for length ...]\n\n"
        + text[-max(0, tail) :]
    )


def parse_json_from_text(text: str) -> dict | None:
    raw = text.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    if "```" in raw:
        blob = raw
        if "```json" in blob:
            blob = blob.split("```json", 1)[-1]
        elif "```" in blob:
            blob = blob.split("```", 1)[-1]
        blob = blob.split("```", 1)[0].strip()
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass
    brace0 = raw.find("{")
    brace1 = raw.rfind("}")
    if brace0 >= 0 and brace1 > brace0:
        try:
            return json.loads(raw[brace0 : brace1 + 1])
        except json.JSONDecodeError:
            pass
    return None


def _safe_int(v, default=0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def result_from_json(result: dict) -> dict:
    iso_cb = result.get("requested_callback_datetime_iso")
    if not iso_cb:
        next_action_obj = result.get("next_action") or {}
        if (next_action_obj.get("action_type") or "").strip().lower() in ("call again", "call_again", "callback"):
            iso_cb = next_action_obj.get("action_datetime_iso")
    if isinstance(iso_cb, str):
        iso_cb = iso_cb.strip() or None
    elif iso_cb is not None:
        iso_cb = None
    emotion_confidence = result.get("emotion_confidence")
    if emotion_confidence is not None:
        try:
            emotion_confidence = float(emotion_confidence)
        except Exception:
            emotion_confidence = None
    
    # Handle the old next_steps array or string fallback
    next_steps = result.get("next_steps", "N/A")
    if isinstance(next_steps, list):
        next_steps = "; ".join(str(x).strip() for x in next_steps if str(x).strip()) or "N/A"
        
    next_action_obj = result.get("next_action") or {}
    action_type = next_action_obj.get("action_type") or "None"
    action_datetime_iso = next_action_obj.get("action_datetime_iso")
    action_details = next_action_obj.get("details") or ""
        
    return {
        "summary": result.get("summary", "Analysis failed"),
        "rating": _safe_int(result.get("rating", 0)),
        "next_steps": next_steps,
        "next_action": {
            "action_type": action_type,
            "datetime_iso": action_datetime_iso,
            "details": action_details
        },
        "disposition": result.get("disposition", "Answered"),
        "emotion_label": result.get("emotion_label", "Unknown"),
        "emotion_rationale": result.get("emotion_rationale", ""),
        "emotion_confidence": emotion_confidence,
        "requested_callback_datetime_iso": iso_cb,
        "preferred_location": result.get("preferred_location"),
        "preferred_budget": result.get("preferred_budget"),
        "email_address": result.get("email_address"),
        "site_visit_agreed": bool(result.get("site_visit_agreed")),
        "account_manager_connect_agreed": bool(result.get("account_manager_connect_agreed")),
    }


def build_analysis_prompt(transcript_text: str, *, role: str = "") -> str:
    transcript_text = _truncate_transcript_for_analysis(transcript_text)
    return _build_buyer_analysis_prompt(transcript_text)


def _build_buyer_analysis_prompt(transcript_text: str) -> str:
    lines = []
    for line in transcript_text.splitlines():
        try:
            obj = json.loads(line)
            role = obj.get("role") or obj.get("type", "")
            content = obj.get("content") or obj.get("text") or obj.get("message", "")
            if role in ("user", "assistant") and content:
                lines.append(f"{role.capitalize()}: {content.strip()}")
        except Exception:
            continue

    if not lines:
        return ""

    readable_chat = "\n".join(lines)

    tz_name = settings.transcript_callback_tz.strip().lower()
    tz_display = settings.transcript_callback_tz.strip()
    if tz_name in ("asia/kolkata", "asia/calcutta"):
        tz_display = "Indian Standard Time — Asia/Kolkata (IST, UTC+05:30)"

    tz = zoneinfo_safe(settings.transcript_callback_tz)
    local_now = datetime.now(tz)
    sched_ctx = (
        f"Scheduling zone: {tz_display}. "
        f"Local 'now' is {local_now.isoformat(timespec='minutes')} "
        f"in THAT zone."
    )

    return f"""You are a QA analyst reviewing a Technopolis Constructions outbound sales call
(Solitaire Unity — premium apartments, Kondapur, Hyderabad).
Return a JSON object with these keys:
- "summary": 1-2 sentence summary of what happened on the call
- "rating": integer 1-5 (quality of engagement)
- "next_steps": concrete follow-up actions for the sales team (string or array of strings)
- "next_action": An object describing the primary structured next action the system or agent should take. Must contain:
  - "action_type": exactly one of ["WhatsApp", "Email", "Call Again", "Virtual Meet", "Site Visit", "None", "Other"]
    IMPORTANT: If the customer asked to receive details, brochure, pricing, or project info via WhatsApp, you MUST set action_type to "WhatsApp".
    If they asked for email, set it to "Email". If they asked to be called back, set it to "Call Again".
    If the customer agreed to a virtual walkthrough, virtual demo, or video meeting (with a specific date/time), set action_type to "Virtual Meet".
    If the customer explicitly agreed to physically visit the property site (e.g. "I'll come tomorrow", "kal aaunga", "let's meet at the site", "site visit kar lunga"), set action_type to "Site Visit".
    EXCEPTION: If the agent offered to send WhatsApp details but the customer explicitly declined (said no, not interested, didn't respond), do NOT set action_type to "WhatsApp" — use the appropriate action instead (e.g., "Call Again" if they asked to be called back).
  - "action_datetime_iso": RFC3339 datetime with offset. MANDATORY when a specific time was agreed for the action.
    CRITICAL: For Virtual Meet — if the customer agreed to a virtual walkthrough with a specific date/time (e.g., "tomorrow at 11am", "Saturday 10am"), you MUST compute the actual clock time using the scheduling zone provided below and populate this field. NEVER leave it null when a virtual meet date/time was discussed.
    For Call Again — compute the callback time from current time + relative duration (e.g., "5 minutes" → current time + 5 min) and populate this field.
  - "details": Detailed explanation of what exactly to send, say, or do.
- "site_visit_agreed": boolean (true or false). Set to true ONLY when the CUSTOMER (not the agent) confirmed they will physically visit the property/site.
  Set to false when the customer only asked about location ("where is it located?", "where exactly?") — that is Interested, NOT a site visit.
  Set to false when the agent offered a visit but the customer did not confirm they will come.
  TRIGGER PHRASES from the CUSTOMER (user turns only) that may set site_visit_agreed to true:
  English: "I will visit the site", "I'll come tomorrow", "Let's meet at the site", "I'll come on Saturday", "I'll be there tomorrow", "I'll come to the site"
  Hindi/Hinglish: "kal aaunga", "main kal aaunga", "site visit kar lenge", "hum aayenge dekhne", "aaunga pakka"
  CRITICAL: The agent asking "would you like to visit?" or "when can you visit?" does NOT set this to true. Only the customer's own confirmation counts.
  Do NOT set to true for virtual meets — only physical site visits. Default: false.
- "disposition": one of ["Interested", "Not Interested", "Call Later", "Busy", "Answered", "Wrong Number", "No Answer", "No Response"]
  Rules for disposition:
  - "Interested": ONLY when the CUSTOMER (user turns) showed engagement — e.g. asked a question, requested brochure/WhatsApp/email, shared budget, agreed to follow-up, or gave contact details.
    NEVER mark Interested when only the ASSISTANT spoke (agent monologue / greeting with no user reply).
    NEVER mark Interested based on what the agent offered to send unless the customer agreed or asked for it.
  - "Not Interested": only clear, direct rejection. "Stop calling", "take me off your list", "not interested" said firmly. Do NOT treat soft/mild responses as Not Interested.
  - "Answered": ONLY when there was no meaningful sales conversation (IVR only, wrong person with no contact request, or callee never engaged). Do NOT use Answered when they asked for materials to be sent.
  - "Call Later": prospect explicitly asks to be called at a future time/date.
  - "Busy": prospect says they are busy right now without scheduling a callback.
  - "Wrong Number": prospect says wrong number / person doesn't exist.
  - "No Answer": call rang but not answered, callee disconnected in <10s with no speech,
    voicemail/IVR/screening only, or automated assistant with no real conversation.
  - "No Response": call connected but USER never spoke (only ASSISTANT heard).
- "emotion_label": one of ["Neutral", "Positive", "Interested", "Excited", "Skeptical", "Frustrated", "Negative", "Angry", "Confused", "Busy"]. Analyze the customer's emotional tone throughout the conversation. Base this on their word choice, enthusiasm level, and engagement.
- "emotion_rationale": 1 sentence explaining WHY you chose this emotion label, quoting specific words or phrases from the customer if possible.
- "emotion_confidence": decimal between 0.0 and 1.0 indicating how confident you are in the emotion assessment. High (0.8-1.0) if the customer's tone was very clear. Low (0.3-0.5) if the conversation was too short or ambiguous.
- "preferred_location": string or null. If the customer mentioned a specific location, area, neighborhood, or city they prefer for the property (e.g. "Whitefield", "HSR Layout", "Koramangala", "Whitefield Bangalore"), extract it here. Null if no location preference was discussed.
- "preferred_budget": string or null. If the customer mentioned a budget, price range, or financial parameter (e.g. "50-70 Lakhs", "1.5 Cr", "under 1 crore", "80 lakhs budget"), extract it here. Null if no budget was discussed.
- "email_address": string or null. If the customer shared an email address during the call, extract it here exactly as spoken. Null if no email was shared.
- "requested_callback_datetime_iso": RFC3339 datetime string with timezone offset (e.g., "2026-06-20T18:00:00+05:30"). MANDATORY when the customer asks to be called back (disposition is "Call Later").
  CRITICAL: Compute this time relative to the current local 'now' provided in the context below:
  - If the customer says "call back in 1 minute" or "after 1 minute", compute local 'now' + 1 minute.
  - If the customer says "call back in 5 minutes" or "after 5 minutes", compute local 'now' + 5 minutes.
  - If the customer says "call me at 6:00 PM" (or any time today), compute today's date at that specific time (e.g. 18:00).
  - If the customer says "call back tomorrow" (without a specific time), compute tomorrow's date at the same time of this call (i.e. local 'now' + 24 hours).
  - If the customer says "call back tomorrow at 5:30 PM", compute tomorrow's date at 17:30.
  - If the customer says "call back day after tomorrow at 5:30 PM", compute day after tomorrow's date at 17:30.
  Set to null if no callback was requested.

CRITICAL ANTI-HALLUCINATION RULE:
If the transcript is very short (< 20 words total) or the call was disconnected
quickly with no real conversation, DO NOT invent customer names, project names,
locations, or any other details not actually present in the transcript.
Simply set disposition to "No Answer" with a minimal summary describing the
call ended without conversation.

AUTHORITATIVE PRICING (for summary/next_action — do not contradict transcript):
- 2 BHK apartment: from approx ₹1.20 Crore | 2.5 BHK approx ₹1.52 Crore | 3 BHK from approx ₹1.37 Crore
- Basic rate ₹9,799 per sq.ft; final price varies by size, floor, facing and corner preference.
- If the transcript mentions different numbers, use the transcript only for what the agent actually said;
  do not invent or "correct" to other prices.

{sched_ctx}

TRANSCRIPT:
{readable_chat}

Return ONLY a valid JSON object with these keys. No other text."""
