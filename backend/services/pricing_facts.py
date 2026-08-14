"""Authoritative Solitaire Unity pricing — live calls, transcription, and analysis."""

from __future__ import annotations

import re

# Box prices from the Solitaire Unity price sheet (Kondapur, Hyderabad)
B2BHK_CR = 1.20
B25BHK_CR = 1.52
B3BHK_CR = 1.37
BASIC_RATE_PER_SQFT = 9799

AUTHORITATIVE_PRICING_PROMPT = f"""
AUTHORITATIVE PRICING — NEVER INVENT OTHER NUMBERS:
- 2 BHK: from approx ₹{B2BHK_CR:.2f} Crore onwards (1,225–1,615 sq.ft)
- 2.5 BHK: approx ₹{B25BHK_CR:.2f} Crore (1,555 sq.ft)
- 3 BHK: from approx ₹{B3BHK_CR:.2f} Crore onwards (1,400–2,300 sq.ft)
- Basic Rate: ₹{BASIC_RATE_PER_SQFT:,}/sq.ft
- Final pricing varies based on unit size, floor, facing, corner preference and
  applicable additional/statutory charges. Never quote other prices.
""".strip()

_CORRECT_PRICING_SNIPPET = (
    "2 BHK from approx ₹1.20 Crore, 2.5 BHK approx ₹1.52 Crore, 3 BHK from approx ₹1.37 Crore"
)

# Wrong prices that must be corrected (legacy row-villa numbers)
_WRONG_START_CR = re.compile(
    r"\b(?:our\s+)?(?:villas?|row[- ]?villas?|project|properties|apartments)\s+"
    r"(?:start(?:s|ing)?|begin(?:s)?)\s+(?:at|from)\s+"
    r"₹?\s*(3\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|4\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|2\.(?:5\d?|6\d?|7\d?|8\d?|9\d?))\s*"
    r"(?:crore|cr\.?|crores?)\b",
    re.I,
)

_WRONG_AGENT_PRICE = re.compile(
    r"\b(?:starting|starts|start)\s+(?:at|from)\s+"
    r"₹?\s*(3\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|4\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|2\.(?:5\d?|6\d?|7\d?|8\d?|9\d?))\s*"
    r"(?:crore|cr\.?|crores?)\b",
    re.I,
)

# Standalone wrong crore mentions in agent pitch context (3.50, 4.59, 2.50, etc.)
_WRONG_STANDALONE_CR = re.compile(
    r"\b₹?\s*(3\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|4\.(?:5\d?|6\d?|7\d?|8\d?|9\d?)|2\.(?:5\d?|6\d?|7\d?|8\d?|9\d?))\s*(?:crore|cr\.?|crores?)\b",
    re.I,
)

_BUDGET_RESPONSE_TEMPLATE = (
    "At Solitaire Unity in Kondapur, our 2 BHK starts from approx ₹1.20 Crore, "
    "the 2.5 BHK is approx ₹1.52 Crore, and the 3 BHK starts from approx ₹1.37 Crore."
)


def has_wrong_pricing(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if "surya meadows" in low or "row villas" in low or "regal" in low:
        return True
    return bool(
        _WRONG_START_CR.search(text)
        or _WRONG_AGENT_PRICE.search(text)
        or _WRONG_STANDALONE_CR.search(text)
    )


def sanitize_pricing_in_text(text: str) -> str:
    """Fix known wrong prices in transcripts or summaries (recording/QA only)."""
    if not text:
        return text
    out = text
    if _WRONG_START_CR.search(out):
        out = _WRONG_START_CR.sub(_BUDGET_RESPONSE_TEMPLATE, out, count=1)
    elif _WRONG_AGENT_PRICE.search(out):
        out = _WRONG_AGENT_PRICE.sub(
            f"2 BHK from approx ₹{B2BHK_CR:.2f} Crore, 3 BHK from approx ₹{B3BHK_CR:.2f} Crore",
            out,
            count=1,
        )
    # Replace remaining standalone wrong prices (e.g. "4.59 crores" in summary)
    if _WRONG_STANDALONE_CR.search(out) and "1.20" not in out:
        out = _WRONG_STANDALONE_CR.sub(_CORRECT_PRICING_SNIPPET, out, count=1)
    return out


def sanitize_transcript_turns(turns: list[dict]) -> list[dict]:
    out: list[dict] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        content = str(turn.get("content") or "")
        fixed = sanitize_pricing_in_text(content)
        if fixed != content:
            turn = {**turn, "content": fixed}
        out.append(turn)
    return out


def sanitize_analysis_dict(analysis: dict) -> dict:
    """Correct wrong pricing in post-call analysis fields."""
    if not isinstance(analysis, dict):
        return analysis
    out = dict(analysis)
    for key in ("summary", "next_steps", "emotion_rationale"):
        val = out.get(key)
        if isinstance(val, str) and val:
            out[key] = sanitize_pricing_in_text(val)
    na = out.get("next_action")
    if isinstance(na, dict) and na.get("details"):
        na = dict(na)
        na["details"] = sanitize_pricing_in_text(str(na["details"]))
        out["next_action"] = na
    if has_wrong_pricing(str(out.get("summary") or "")):
        out["summary"] = sanitize_pricing_in_text(str(out.get("summary") or ""))
    return out
