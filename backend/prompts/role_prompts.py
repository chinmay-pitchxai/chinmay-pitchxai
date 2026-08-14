"""Voice agent system prompt — role prompt loading."""

import os
import re

from loguru import logger


def extract_agent_name(role: str) -> str:
    """Extract agent name from the role's PROMPT — DB (user-edited config)
    first, then the packaged prompt file. Callers use this for persona
    anchors, opening lines and transcripts, so it must reflect what the
    operator saved in the Configuration page, not a stale file."""

    def _from_text(content: str) -> str:
        m = re.search(r'##\s*Agent:\s*(.+)', content)
        if m:
            return m.group(1).strip()
        m = re.search(r'You are\s+\*\*(.+?)\*\*', content)
        if m:
            return m.group(1).strip()
        m = re.search(r'\*\*Name:\*\*\s*(.+)', content)
        if m:
            return m.group(1).strip()
        m = re.search(r'You are\s+([A-Z][a-z]+)', content)
        if m:
            return m.group(1).strip()
        return ""

    try:
        from core.state import get_state

        db_p = (get_state(role).get("prompt") or "").strip()
        if db_p:
            name = _from_text(db_p)
            if name:
                return name
    except Exception:
        pass

    path = os.path.join(os.path.dirname(__file__), f"{role}_prompt.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return _from_text(f.read())
    return ""


def get_role_prompt_text(role: str) -> str:
    """Read the prompt for a specific role from its dedicated file.

    Single-source consolidation: only ``sales_1_prompt.txt`` exists; any other
    role (legacy sandboxes) resolves to the Technopolis prompt.
    """
    path = os.path.join(os.path.dirname(__file__), f"{role}_prompt.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    path = os.path.join(os.path.dirname(__file__), "sales_1_prompt.txt")
    if not os.path.exists(path):
        logger.error("Role prompt file missing for role={!r}: {}", role, os.path.abspath(path))
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_role_prompt_text(role: str, prompt: str) -> None:
    """Save the prompt for a specific role to its dedicated file."""
    path = os.path.join(os.path.dirname(__file__), f"{role}_prompt.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(prompt.strip())


def get_role_rag_source_text(role: str) -> str:
    """Read the RAG source text for a role from ``data/{role}/rag_source.txt``.

    Single-source consolidation: non-``sales_1`` roles resolve to the shared
    ``data/sales_1/rag_source.txt`` knowledge base.
    """
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", role)
    path = os.path.join(base, "rag_source.txt")
    if os.path.exists(path) and role == "sales_1":
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    fallback = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "sales_1", "rag_source.txt"
    )
    if os.path.exists(fallback):
        with open(fallback, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def get_default_rag_source_text() -> str:
    """Technopolis Constructions KB (sales_1 role)."""
    text = get_role_rag_source_text("sales_1")
    if text:
        return text
    return get_role_rag_source_text("sales_1")


def set_role_rag_source_text(role: str, text: str) -> None:
    """Save the RAG source text for a role to data/{role}/rag_source.txt."""
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", role)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "rag_source.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip())


from typing import Optional


def _resolved_prompt_and_rag(
    role: str, role_config: Optional[dict] = None
) -> tuple[str, str]:
    """File prompt is source of truth for packaged roles; DB wins for custom sandboxes.

    Result is served from the KV prompt cache when warm (10 min TTL) and
    invalidated whenever the console saves role tuning.
    """
    try:
        from core import kv_cache

        cached = kv_cache.prompt_get(role)
        if cached is not None:
            return cached[0], cached[1]
    except Exception:
        cached = None

    file_p = get_role_prompt_text(role)
    file_r = get_role_rag_source_text(role)
    db_p = ""
    db_r = ""
    if isinstance(role_config, dict):
        db_p = (role_config.get("prompt") or "").strip()
        db_r = (role_config.get("rag") or "").strip()
    from core.role_sandbox import coerce_role_prompt, coerce_role_rag, PACKAGED_CONSOLE_ROLES

    if role in PACKAGED_CONSOLE_ROLES:
        out_p, out_r = db_p or file_p, db_r or file_r
    else:
        out_p = coerce_role_prompt(role, db_p, file_p)
        out_r = coerce_role_rag(role, db_r, file_r)

    try:
        from core import kv_cache as _kv2

        _kv2.prompt_set(role, out_p, out_r)
    except Exception:
        pass
    return out_p, out_r


def build_role_system_prompt(
    role: str,
    role_config: Optional[dict] = None,
    *,
    embed_rag: bool = False,
) -> str:
    """Construct the system prompt for the model.

    When ``embed_rag`` is False (default for live PSTN), factual KB is delivered
    at runtime via RAG inject — not baked into the prompt (keeps setup lean + fast).
    """
    prompt, rag = _resolved_prompt_and_rag(role, role_config)
    if embed_rag and rag:
        prompt += f"\n\n[KNOWLEDGE BASE]\n{rag}"
    elif rag:
        prompt += (
            "\n\n[KNOWLEDGE BASE — LIVE RAG]\n"
            "Factual project specs (pricing, configurations, amenities, phases, location) "
            "arrive during the call as [SYSTEM RAG CONTEXT] messages. "
            "Treat those as the authoritative source for factual answers. "
            "Do not guess numbers or amenities — use RAG context when present.\n"
        )
    return prompt

