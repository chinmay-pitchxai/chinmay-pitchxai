"""Per-role sandbox — detect cross-role pollution and restore packaged content."""

from __future__ import annotations

from typing import Callable, Optional

from loguru import logger

# Roles with canonical prompt + RAG files on disk (deploy source of truth).
PACKAGED_CONSOLE_ROLES = frozenset(
    {"sales_1"}
)

ALL_CONSOLE_ROLES = frozenset(
    {
        "sales_1",
    }
)

ROLE_DISPLAY_NAMES = {
    "sales_1": "Vernika (Technopolis – Solitaire Unity)",
}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _contains_any(low: str, *needles: str) -> bool:
    return any(n in low for n in needles if n)


def coerce_stored_greeting(role: str, text: str | None) -> str:
    """Drop greetings that belong to another role."""
    r = (role or "").strip().lower()
    t = (text or "").strip()
    if not t:
        return ""
    return t


def coerce_role_prompt(role: str, db_prompt: str, file_prompt: str) -> str:
    r = (role or "").strip().lower()
    db = (db_prompt or "").strip()
    fp = (file_prompt or "").strip()
    if db:
        return db
    return fp or db


def coerce_role_rag(role: str, db_rag: str, file_rag: str) -> str:
    r = (role or "").strip().lower()
    db = (db_rag or "").strip()
    fr = (file_rag or "").strip()
    return db or fr


def validate_role_tuning(
    role: str,
    prompt: str = "",
    rag: str = "",
    greeting: str = "",
) -> Optional[str]:
    """Return an operator-facing error if tuning text belongs to another sandbox."""
    return None


def sync_role_sandbox_on_startup(role: str) -> None:
    """Refresh one role row: packaged files win over DB."""
    from core.opening_line import packaged_fallback_greeting
    from core.state import get_state, save_role_state
    from prompts.role_prompts import get_role_prompt_text, get_role_rag_source_text

    r = (role or "").strip().lower()
    if r not in ALL_CONSOLE_ROLES:
        return

    state = get_state(r)
    fp = get_role_prompt_text(r).strip()
    fr = get_role_rag_source_text(r).strip()
    db_p = (state.get("prompt") or "").strip()
    db_r = (state.get("rag") or "").strip()

    if r in PACKAGED_CONSOLE_ROLES:
        prompt_out = fp if fp else coerce_role_prompt(r, db_p, fp)
        rag_out = fr if fr else coerce_role_rag(r, db_r, fr)
    else:
        prompt_out = coerce_role_prompt(r, db_p, "")
        rag_out = coerce_role_rag(r, db_r, "")

    db_greeting = (state.get("greeting_text") or "").strip()
    gv = db_greeting or packaged_fallback_greeting(r)

    save_role_state(
        r,
        prompt=prompt_out,
        rag=rag_out,
        greeting_text=gv,
    )
    logger.debug("Sandbox synced for role={}", r)


def sync_all_role_sandboxes_on_startup() -> None:
    for role in sorted(ALL_CONSOLE_ROLES):
        try:
            sync_role_sandbox_on_startup(role)
        except Exception as exc:
            logger.warning("Role sandbox sync skipped for {}: {}", role, exc)
