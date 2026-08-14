"""Resolve Vobiz auth + CLI per console role (env overrides stale DB for dedicated trunks)."""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

from config import settings
from core.outbound_numbers import resolve_outbound_from_number
from core.state import normalize_console_role


def _sales_1_env_configured() -> bool:
    return bool(
        (settings.vobiz_sales_1_auth_id or "").strip()
        and (settings.vobiz_sales_1_auth_token or "").strip()
        and (settings.vobiz_sales_1_phone_1 or "").strip()
    )


def _is_unreachable_vobiz_public_url(url: str) -> bool:
    """Vobiz cannot POST to localhost — reject private URLs for answer_url."""
    u = (url or "").strip().lower()
    if not u:
        return True
    return any(
        x in u
        for x in (
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            "[::1]",
        )
    )


def _normalize_vobiz_public_url(*candidates: str) -> str:
    """Pick first public HTTPS URL; skip localhost even if set in systemd root .env."""
    https_urls: list[str] = []
    http_urls: list[str] = []
    for raw in candidates:
        u = (raw or "").strip().rstrip("/")
        if _is_unreachable_vobiz_public_url(u):
            continue
        if u.startswith("https://"):
            https_urls.append(u)
        elif u.startswith("http://"):
            http_urls.append(u)
    return https_urls[0] if https_urls else (http_urls[0] if http_urls else "")


def resolve_vobiz_credentials(
    role: str,
    vobiz_cfg: Optional[Mapping[str, object]] = None,
) -> Tuple[str, str, str, str]:
    """
    Return (auth_id, auth_token, from_number, public_url) for outbound dial.

    Uses the Sales 1 (Technopolis) dedicated trunk when configured, otherwise the global DID.
    """
    r = normalize_console_role(role)
    vc = dict(vobiz_cfg or {})

    env_public = (settings.vobiz_public_base_url or "").strip().rstrip("/")
    db_public = str(vc.get("public_url") or "").strip().rstrip("/")
    public_url = _normalize_vobiz_public_url(env_public, db_public)

    if r == "sales_1" and _sales_1_env_configured():
        return (
            settings.vobiz_sales_1_auth_id.strip(),
            settings.vobiz_sales_1_auth_token.strip(),
            settings.vobiz_sales_1_phone_1.strip(),
            public_url,
        )

    auth_id = str(vc.get("auth_id") or settings.vobiz_auth_id or "").strip()
    auth_token = str(vc.get("auth_token") or settings.vobiz_auth_token or "").strip()
    from_number = resolve_outbound_from_number(role, vc)
    return auth_id, auth_token, from_number, public_url
