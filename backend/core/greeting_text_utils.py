"""Normalize stored greeting lines per role (delegates to role sandbox)."""

from __future__ import annotations

import re

from core.role_sandbox import coerce_stored_greeting as _coerce_stored_greeting

# Name verification is always a live Gemini turn with the authoritative lead name —
# never baked into prerecorded greeting PCM (avoids wrong/stale names + double ask).
_NAME_VERIFY_TAIL_PATTERNS = (
    re.compile(r"\s*am i speaking with[^?.!]*[?.!]?\s*", re.I),
    re.compile(r"\s*may i know who i(?:'m| am) speaking with[^?.!]*[?.!]?\s*", re.I),
    re.compile(r"\s*may i know your (?:good )?name[^?.!]*[?.!]?\s*", re.I),
    re.compile(r"\s*could i confirm (?:that )?i(?:'m| am) speaking with[^?.!]*[?.!]?\s*", re.I),
    re.compile(r"\s*can i (?:just )?know your name[^?.!]*[?.!]?\s*", re.I),
)


def intro_only_greeting(text: str | None) -> str:
    """Return greeting intro without name-verify tail (PCM + transcript intro only)."""
    s = (text or "").strip()
    if not s:
        return ""
    for pat in _NAME_VERIFY_TAIL_PATTERNS:
        s = pat.sub(" ", s)
    return " ".join(s.split()).strip()


def _greeting_normalized(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def coerce_stored_greeting(role: str, text: str | None) -> str:
    raw = _coerce_stored_greeting(role, text)
    return intro_only_greeting(raw)
