"""Central Gemini API authentication (AQ auth keys + legacy query keys)."""

from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from config import settings

_ACTIVE_KEY: str = ""
_VALIDATED: bool = False

GEMINI_LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_REST_BASE = "https://generativelanguage.googleapis.com/v1beta"


def gemini_auth_headers(api_key: Optional[str] = None) -> dict[str, str]:
    key = (api_key or get_gemini_api_key() or "").strip()
    if not key:
        return {}
    return {"x-goog-api-key": key}


def get_gemini_api_key() -> str:
    if _ACTIVE_KEY:
        return _ACTIVE_KEY
    return (settings.gemini_api_key or "").strip()


def gemini_generate_content_url(model: str, *, api_key: Optional[str] = None) -> str:
    del api_key  # header auth only — AQ auth keys reject ?key= query auth
    model_id = (model or "").strip()
    if model_id.startswith("models/"):
        model_id = model_id.split("/", 1)[1]
    return f"{GEMINI_REST_BASE}/models/{model_id}:generateContent"


def _probe_key(key: str) -> bool:
    key = (key or "").strip()
    if not key:
        return False
    url = gemini_generate_content_url("gemini-flash-latest")
    body = {"contents": [{"parts": [{"text": "Reply OK"}]}]}
    try:
        r = httpx.post(url, json=body, headers=gemini_auth_headers(key), timeout=12.0)
        return r.status_code == 200
    except Exception as exc:
        logger.debug("Gemini key probe failed: {}", exc)
        return False


def init_gemini_api_key() -> str:
    """Validate primary key; fall back to GEMINI_API_KEY_FALLBACK if configured."""
    global _ACTIVE_KEY, _VALIDATED
    if _VALIDATED:
        return get_gemini_api_key()

    primary = (settings.gemini_api_key or "").strip()
    fallback = (getattr(settings, "gemini_api_key_fallback", "") or "").strip()

    if primary and _probe_key(primary):
        _ACTIVE_KEY = primary
        logger.info("Gemini API key validated (primary, header auth)")
    elif fallback and _probe_key(fallback):
        _ACTIVE_KEY = fallback
        logger.warning(
            "Primary GEMINI_API_KEY failed validation — using GEMINI_API_KEY_FALLBACK. "
            "Enable Generative Language API on the new Google Cloud project or wait for AQ key activation."
        )
    elif primary:
        _ACTIVE_KEY = primary
        logger.error(
            "GEMINI_API_KEY failed validation (401). Voice calls will fail until the key works. "
            "For new AQ keys: enable Generative Language API on the project in Google AI Studio."
        )
    else:
        _ACTIVE_KEY = ""
        logger.warning("GEMINI_API_KEY not set")

    _VALIDATED = True
    return _ACTIVE_KEY
