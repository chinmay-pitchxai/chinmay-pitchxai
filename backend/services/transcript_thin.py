"""Detect thin / incomplete live transcripts (greeting-only, no customer speech)."""

from __future__ import annotations

import os
import re

from services.transcript_hybrid import coalesce_jsonl_turns, meaningful_turn_count

# Latin + Indic scripts common on Indian sales calls; reject CJK/Hangul STT garbage.
_PLAUSIBLE_LETTERS = re.compile(
    r"[\u0900-\u097F\u0980-\u09FF\u0A00-\u0A7F\u0A80-\u0AFF\u0B00-\u0B7F"
    r"\u0B80-\u0BFF\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F"
    r"a-zA-Z]",
    re.UNICODE,
)


def _min_user_turns() -> int:
    return max(0, int(os.getenv("TRANSCRIPT_MIN_USER_TURNS", "1")))


def _min_user_chars() -> int:
    return max(0, int(os.getenv("TRANSCRIPT_MIN_USER_CHARS", "12")))


def _has_unlikely_script(text: str) -> bool:
    """True when text is dominated by scripts live STT should not emit on Indian PSTN."""
    for ch in text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:  # Hangul
            return True
        if 0x4E00 <= cp <= 0x9FFF:  # CJK
            return True
        if 0x3040 <= cp <= 0x30FF:  # Japanese kana
            return True
        if 0x0400 <= cp <= 0x04FF:  # Cyrillic
            return True
    return False


def user_turn_is_plausible(content: str) -> bool:
    """Reject garbage/hallucinated STT that should not unlock sales outcomes."""
    t = (content or "").strip()
    if len(t) < 2:
        return False
    if _has_unlikely_script(t):
        return False
    if not _PLAUSIBLE_LETTERS.search(t):
        return False
    low = t.lower().strip(".,?! ")
    if low in ("silence", "noise", "[silence]", "[noise]", "…"):
        return False
    return True


def user_speech_stats(transcript: str) -> tuple[int, int, int]:
    """Return (user_turns, user_chars, total_turns) from JSONL or plain text."""
    turns = coalesce_jsonl_turns(transcript or "")
    if not turns:
        return 0, 0, 0
    user_turns = 0
    user_chars = 0
    for t in turns:
        if str(t.get("role") or "").lower() != "user":
            continue
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        user_turns += 1
        user_chars += len(content)
    return user_turns, user_chars, len(turns)


def plausible_user_speech_stats(transcript: str) -> tuple[int, int, int]:
    """Like user_speech_stats but counts only plausible customer speech."""
    turns = coalesce_jsonl_turns(transcript or "")
    if not turns:
        return 0, 0, 0
    user_turns = 0
    user_chars = 0
    for t in turns:
        if str(t.get("role") or "").lower() != "user":
            continue
        content = str(t.get("content") or "").strip()
        if not content or not user_turn_is_plausible(content):
            continue
        user_turns += 1
        user_chars += len(content)
    return user_turns, user_chars, len(turns)


def transcript_is_thin(transcript: str) -> tuple[bool, str]:
    """True when transcript lacks enough customer speech to trust sales outcomes."""
    if not (transcript or "").strip():
        return True, "empty"
    user_n, user_chars, total = plausible_user_speech_stats(transcript)
    raw_user_n, _, _ = user_speech_stats(transcript)
    min_turns = _min_user_turns()
    min_chars = _min_user_chars()
    if raw_user_n >= 1 and user_n < min_turns:
        return True, f"implausible_user_turns:{raw_user_n}"
    if user_n < min_turns:
        return True, f"user_turns:{user_n}<{min_turns}"
    if user_chars < min_chars:
        return True, f"user_chars:{user_chars}<{min_chars}"
    meaningful = meaningful_turn_count(coalesce_jsonl_turns(transcript))
    if total >= 1 and meaningful < 2 and user_n < 2:
        return True, f"meaningful_turns:{meaningful}"
    return False, ""


def pick_richer_transcript(*candidates: str) -> str:
    """Choose the candidate with the most plausible user speech (for dashboard display)."""
    best = ""
    best_score = (-1, -1, -1)
    for raw in candidates:
        text = (raw or "").strip()
        if not text:
            continue
        user_n, user_chars, total = plausible_user_speech_stats(text)
        raw_n, raw_chars, _ = user_speech_stats(text)
        score = (user_n or raw_n, user_chars or raw_chars, total)
        if score > best_score:
            best_score = score
            best = text
    return best
