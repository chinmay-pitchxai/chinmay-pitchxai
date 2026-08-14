from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from config import settings


def _analysis_provider() -> str:
    return (os.getenv("CALL_ANALYSIS_PROVIDER") or "gemini").strip().lower()


def _local_analyzer_available() -> bool:
    backend_dir = Path(__file__).resolve().parent.parent
    model_abs = backend_dir / "models" / "LFM2.5-1.2B-Instruct"
    return model_abs.is_dir()


def heuristic_analysis(transcript_text: str, *, gemini_error: str = "") -> dict:
    """Public alias for timeout/error fallbacks — always returns a usable summary."""
    return _heuristic_analysis(transcript_text, gemini_error=gemini_error)


def _heuristic_analysis(transcript_text: str, *, gemini_error: str = "") -> dict:
    from services.transcript_interest import (
        _NEGATIVE,
        caller_text_from_transcript,
        infer_interest_from_transcript,
        is_voicemail_or_screening_transcript,
    )

    user_text = caller_text_from_transcript(transcript_text)
    dispo = "Answered"
    if not user_text.strip():
        dispo = "No Response"
        summary = "The call connected, but the recipient did not speak or respond."
    elif is_voicemail_or_screening_transcript(transcript_text):
        dispo = "Voice Mail"
        summary = "Call reached voicemail or automated screening."
    elif _NEGATIVE.search(user_text.lower()):
        dispo = "Not Interested"
        summary = f"Recipient declined. User said: '{user_text[:120]}'"
    elif infer_interest_from_transcript(transcript_text):
        dispo = "Interested"
        summary = f"Recipient expressed interest. User said: '{user_text[:120]}'"
    else:
        summary = f"The call was answered. Recipient said: '{user_text[:120]}'"

    if gemini_error:
        err_low = gemini_error.lower()
        if "no longer available to new users" in err_low and "2.5-flash" in err_low:
            summary = (
                "Call connected. Full AI summary unavailable: gemini-3.1-flash-lite is blocked "
                "for new Google API keys. Summaries will work once Google enables 2.5 Flash "
                "on your key, or use an older API key with 2.5 access."
            )
        else:
            summary = f"{summary} (Auto-summary; Gemini unavailable: {gemini_error[:80]})"

    return {
        "summary": summary,
        "rating": 1 if dispo != "No Response" else 0,
        "next_steps": (
            "Attempt to call the customer again at a later time."
            if dispo != "Interested"
            else "Send requested details via email or WhatsApp."
        ),
        "disposition": dispo,
        "emotion_label": "Neutral",
        "emotion_rationale": "",
        "emotion_confidence": 0.5,
        "requested_callback_datetime_iso": None,
        "site_visit_agreed": False,
        "preferred_location": None,
        "preferred_budget": None,
        "email_address": None,
    }


async def _try_local_analyzer(transcript_text: str) -> dict | None:
    if not _local_analyzer_available():
        return None
    from services.local_analyzer import analyze_local

    return await analyze_local(transcript_text)


async def analyze_call_transcript(transcript_text: str, *, role: str = "") -> dict:
    """Analyze call transcript — default: Gemini API (``GEMINI_API_KEY``).

    Fallback chain: Gemini → local analyzer (if installed) → heuristics.
    Never raises — always returns a valid analysis dict.
    """
    provider = _analysis_provider()

    if provider == "local":
        try:
            result = await _try_local_analyzer(transcript_text)
            if result is not None:
                return result
        except Exception as e:
            logger.warning("Local analyzer failed: {}", e)
        return _heuristic_analysis(transcript_text)

    key = (settings.gemini_api_key or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key:
        logger.warning("No GEMINI_API_KEY — using heuristic analysis")
        result = await _try_local_analyzer(transcript_text)
        if result is not None:
            return result
        return _heuristic_analysis(transcript_text)

    from services.gemini_analyzer import analyze_gemini

    try:
        result = await analyze_gemini(transcript_text, role=role)
        from services.pricing_facts import sanitize_analysis_dict, sanitize_pricing_in_text

        if isinstance(result, dict):
            if result.get("summary"):
                result["summary"] = sanitize_pricing_in_text(str(result["summary"]))
            result = sanitize_analysis_dict(result)
        return result
    except Exception as e:
        logger.warning("Gemini analysis failed ({}), trying fallbacks", e)
        try:
            result = await _try_local_analyzer(transcript_text)
            if result is not None:
                return result
        except Exception as e2:
            logger.error("Local analyzer also failed: {}", e2)
        try:
            return _heuristic_analysis(transcript_text, gemini_error=str(e))
        except Exception as e3:
            logger.error("Heuristic fallback builder failed: {}", e3)
            return {
                "summary": f"Analysis temporarily unavailable. Gemini error: {e}",
                "rating": 0,
                "next_steps": "Retry analysis later",
                "disposition": "Answered",
                "emotion_label": "Unknown",
                "emotion_rationale": "",
                "emotion_confidence": None,
                "requested_callback_datetime_iso": None,
                "site_visit_agreed": False,
                "preferred_location": None,
                "preferred_budget": None,
                "email_address": None,
            }


def canonical_disposition(raw: str | None) -> str:
    """Normalize analyzer-output disposition strings → stable buckets for SQLite status mapping."""

    text = str(raw or "").strip()
    if not text:
        return "Answered"

    lowered = " ".join(text.lower().replace("_", " ").split())

    ALLOWED_EXACT = {
        "Interested",
        "Not Interested",
        "Site Visit",
        "Call Later",
        "Busy",
        "Answered",
        "Wrong Number",
        "Callback",
    }

    for label in ALLOWED_EXACT:
        if lowered == label.lower():
            return "Call Later" if label == "Callback" else label

    if "not interested" in lowered:
        return "Not Interested"
    if "site visit" in lowered or lowered.replace(" ", "_") == "site_visit":
        return "Site Visit"
    if "wrong number" in lowered or "wrong no" in lowered:
        return "Wrong Number"
    if "voice mail" in lowered or "voicemail" in lowered or "answering machine" in lowered:
        return "Voicemail"
    if "call screened" in lowered or "screening" in lowered:
        return "Call Screened"
    if "no response" in lowered or lowered == "no answer":
        return "No Answer"

    interested_hit = ("interested" in lowered) and "not interested" not in lowered
    if interested_hit:
        return "Interested"

    if lowered.startswith("busy") or lowered == "busy":
        return "Busy"
    if "call later" in lowered or lowered.startswith("callback"):
        return "Call Later"
    if lowered.startswith("answered") or lowered == "answer":
        return "Answered"

    return text.strip()
