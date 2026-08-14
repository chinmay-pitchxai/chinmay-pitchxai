"""Outbound greeting line text (CSV / role defaults)."""

from __future__ import annotations

import re


_ROLE_FALLBACK_GREETINGS = {
    # Technopolis — Solitaire Unity (single active role). The agent NAME is
    # interpolated from the operator's saved prompt (extract_agent_name), so
    # the AI never introduces itself with a stale hardcoded name.
    "sales_1": (
        "Hi, this is {agent_name} from Technopolis Constructions Private Limited."
    ),
}


def packaged_fallback_greeting(role: str) -> str:
    """Default opener line packaged with the repo (no DB); used after coercion/UI fallbacks."""
    r = (role or "sales_1").strip().lower()
    return _ROLE_FALLBACK_GREETINGS.get(r) or _ROLE_FALLBACK_GREETINGS["sales_1"]


def looks_like_real_name(value: str) -> bool:
    if not value:
        return False
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "null", "n/a", "na", "unknown", "-"):
        return False
    return any(ch.isalpha() for ch in s)


def _interpolate_first_name(text: str, first_name: str) -> str:
    if not text or not looks_like_real_name(first_name):
        return text
    if "{name}" in text:
        return text.replace("{name}", first_name)
    for prefix in ("Hi,", "Hello,", "Hey,"):
        if text.startswith(prefix):
            return f"{prefix[:-1]} {first_name},{text[len(prefix):]}"
    return text


def _interpolate_company(text: str, company: str) -> str:
    if not text or not looks_like_real_name(company):
        return text
    if company.lower() in text.lower():
        return text
    insert_phrase = f", calling for {company}"
    m = re.search(r"([.!?])(\s|$)", text)
    if m:
        return text[: m.start()] + insert_phrase + text[m.start() :]
    return f"{text.rstrip()} {insert_phrase.lstrip(', ').capitalize()}."


def build_opening_line(row_data: dict, role: str = "sales_1") -> str:
    # Opening line for the outbound greeting, personalized with the lead's
    # first name / company when available. The agent name comes from the
    # operator's saved prompt — never a hardcoded persona.
    r = (role or "sales_1").strip().lower()
    text = _ROLE_FALLBACK_GREETINGS.get(r) or _ROLE_FALLBACK_GREETINGS["sales_1"]
    try:
        from prompts.role_prompts import extract_agent_name

        agent_name = extract_agent_name(r) or "Vernika"
        text = text.replace("{agent_name}", agent_name)
    except Exception:
        text = text.replace("{agent_name}", "Vernika")
    row_data = row_data or {}
    text = _interpolate_first_name(text, str(row_data.get("name") or ""))
    text = _interpolate_company(text, str(row_data.get("company") or ""))
    return text
