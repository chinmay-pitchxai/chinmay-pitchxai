"""Fix speaker-role swaps in post-call and live transcripts."""

from __future__ import annotations

import re
from typing import Iterable

_AGENT_INTRO = (
    "this is vernika", "i am vernika", "this is vernika from",
    "i am vernika from", "from technopolis constructions",
    "channel partner", "cp commission", "three percent commission", "3% commission",
    "3 percent commission", "regal edition", "4.59 crore", "4.59 cr", "3950 sq",
    "pending units", "account manager", "luxury features", "private pool",
    "home automation", "commission structure",
    "personal assistant for technopolis", "solitaire unity", "you'd enquired", "you'd inquired",
    "am i speaking with", "may i know your name", "private backyard", "row villa", "row-villa",
    "9.5 acres", "40,000 square", "clubhouse", "would you like to know more",
    "which details would you like", "scheduled callback", "calling you back",
    "phase 3", "phase 1", "phase 2", "townhouse", "duplex villa", "elysium club",
    "prestige", "brigade", "godrej",
)

_USER_MARKERS = (
    "panther chinmay", "developer mode", "developed you", "voice agent", "change the code",
    "already bought", "already visited", "already booked", "book maadidini", "site nodidde",
    "i have developed", "are you ai", "are you a robot", "real person",
)

_USER_SHORT = (
    "yeah", "yes", "hello", "hi", "ok", "okay", "tell me", "uh", "hmm",
    "what did you say", "are you there", "bye", "goodbye", "no problem",
    "just need to know", "call me back", "after five", "after 5",
)

_AGENT_NAMES = ("vernika",)


_AGENT_QUESTIONS = (
    "looking for a villa or a plot",
    "looking for a villa",
    "what is your budget",
    "what's your budget",
    "preferred location",
    "when are you planning to buy",
    "would you like to schedule a site visit",
    "schedule a site visit",
    "is this a good time to talk",
    "i'll call you back to confirm",
    "call you back to confirm",
    "send you the brochure",
    "shall i send",
)

def _is_agent_content(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False
    if any(k in t for k in _AGENT_INTRO):
        return True
    if any(q in t for q in _AGENT_QUESTIONS):
        return True
    if re.search(r"\bour (luxury |premium )?(villa|project|clubhouse)", t):
        return True
    if len(t) > 80 and ("surya" in t or "villa" in t or "bhk" in t):
        return True
    return False


def transcript_has_severe_speaker_swap(turns: list[dict]) -> bool:
    """True when multiple obvious agent script lines are labeled as user."""
    if not turns:
        return False
    bad = sum(
        1
        for t in turns
        if (t.get("role") or "").lower() == "user" and _is_agent_content(str(t.get("content") or ""))
    )
    return bad >= 2


def _is_user_short(text: str) -> bool:
    t = (text or "").lower().strip()
    if not t or len(t) > 120:
        return False
    if any(t == s or t.startswith(s + " ") or t.startswith(s + ",") for s in _USER_SHORT):
        return True
    if t in ("hello.", "hello!", "hello hello", "hello hello hello"):
        return True
    return False


def fix_transcript_speaker_roles(turns: list[dict], agent_name: str = "") -> list[dict]:
    """Re-attribute obvious mislabeled turns (agent pitch labeled as user, etc.)."""
    if not turns:
        return turns

    agent_lower = (agent_name or "").strip().lower()
    out: list[dict] = []

    for i, turn in enumerate(turns):
        role = (turn.get("role") or "user").strip().lower()
        content = (turn.get("content") or turn.get("text") or "").strip()
        if not content:
            continue

        c_lower = content.lower()

        # Never label callee name alone as agent
        if agent_lower and c_lower == agent_lower:
            role = "user"
        elif _is_agent_content(content):
            role = "assistant"
        elif _is_user_short(content):
            role = "user"
        elif any(m in c_lower for m in _USER_MARKERS):
            role = "user"
        elif i == 0 and _is_agent_content(content):
            role = "assistant"
        elif i > 0:
            prev_role = out[-1]["role"] if out else "assistant"
            if prev_role == "assistant" and _is_user_short(content):
                role = "user"
            elif prev_role == "user" and _is_agent_content(content) and len(content) > 40:
                role = "assistant"

        # Caller name in transcript (e.g. Chinmay) speaking short line -> user
        if any(c_lower.startswith(n) for n in _AGENT_NAMES if n != agent_lower):
            if not _is_agent_content(content):
                role = "user"

        rec = dict(turn)
        rec["role"] = "assistant" if role == "assistant" else "user"
        rec["content"] = content
        out.append(rec)

    # Global swap if majority of long agent pitches sit under user
    user_agentish = sum(1 for t in out if t["role"] == "user" and _is_agent_content(t["content"]))
    asst_usershort = sum(1 for t in out if t["role"] == "assistant" and _is_user_short(t["content"]))
    if user_agentish >= 2 and user_agentish > asst_usershort:
        for t in out:
            t["role"] = "user" if t["role"] == "assistant" else "assistant"

    return out


def repair_jsonl_transcript_roles(transcript: str, agent_name: str = "") -> str:
    """Re-label obvious agent/user swaps in JSONL transcript before analysis."""
    if not (transcript or "").strip():
        return transcript or ""
    import json

    turns: list[dict] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            turns.append(obj)
    if not turns:
        return transcript
    fixed = fix_transcript_speaker_roles(turns, agent_name=agent_name)
    return "\n".join(json.dumps(t, ensure_ascii=False) for t in fixed) + "\n"
