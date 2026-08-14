"""Phone normalization to E.164. India-first defaults (+91); plain digits assume Indian mobile."""

from __future__ import annotations

import re


def norm_phone_str(raw: str) -> str:
    """
    Normalize a phone string toward E.164.

    - Strips float trailing .0 from pandas/Excel cells first.
    - 10-digit mobile/phone → +91…
    - Leading 0 trunk → stripped, then +91…
    - ``91xxxxxxxxxx`` without "+" → "+91xxxxxxxxxx".
    - ``+`` prefix keeps explicit country codes.
    """
    if not raw:
        return ""
    
    # 1. Clean float trailing .0 from pandas/Excel cell representations
    s = str(raw).strip()
    s = re.sub(r'\.0+$', '', s)
    
    cleaned = re.sub(r"[^\d+]", "", s)
    if not cleaned:
        return ""

    plus_stripped = cleaned
    leading_plus_count = 0
    while plus_stripped.startswith("+"):
        leading_plus_count += 1
        plus_stripped = plus_stripped[1:]
        if leading_plus_count > 2:
            return ""

    has_plus = leading_plus_count >= 1
    digits = plus_stripped.lstrip("+")
    if not digits:
        return ""

    def ind10(s: str) -> bool:
        return len(s) == 10

    # 10 digits -> +91xxxxxxxxxx
    if ind10(digits):
        return f"+91{digits}"

    # 09876543210 -> +91xxxxxxxxxx
    if len(digits) == 11 and digits.startswith("0") and ind10(digits[1:]):
        return f"+91{digits[1:]}"

    # Common duplicate leading 9 or sheet glitch -> e.g. 99876543210 -> +919876543210
    if len(digits) == 11 and digits[0] == "9" and ind10(digits[1:]):
        return f"+91{digits[1:]}"

    # 91xxxxxxxxxx -> +91xxxxxxxxxx
    if len(digits) == 12 and digits.startswith("91") and ind10(digits[2:]):
        return f"+{digits}"

    # Mis-synced "+98xxxx…" rows where tail is clearly 10 digits
    if (
        digits.startswith("98")
        and not digits.startswith("989")
        and 11 <= len(digits) <= 13
        and ind10(digits[-10:])
    ):
        return f"+91{digits[-10:]}"

    # Explicit +91xxxxxxxxxx
    if has_plus and 12 <= len(digits) <= 13 and digits.startswith("91") and ind10(digits[2:]):
        return f"+{digits}"

    if has_plus and 10 <= len(digits) <= 15:
        return f"+{digits}"

    if not has_plus and 11 <= len(digits) <= 15:
        return f"+{digits}"

    if len(digits) >= 7:
        return f"+{digits}" if not has_plus else f"+{digits}"

    return ""
