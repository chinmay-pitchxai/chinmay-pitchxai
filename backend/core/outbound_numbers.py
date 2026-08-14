"""Resolve outbound Vobiz ``from`` number per role without duplicating branching logic."""

from __future__ import annotations

import os
import re
import time
from typing import Mapping, Optional

from config import settings
from core.state import normalize_console_role

# Vobiz may block specific outbound DIDs (HTTP 403). Skip until TTL expires.
_BLOCKED_FROM_LINES: dict[str, float] = {}
_BLOCKED_FROM_TTL_SEC: float = 86400.0

# Vobiz HTTP 402 (insufficient balance) — pause dials for that auth account.
_LOW_BALANCE_AUTH: dict[str, float] = {}
_LOW_BALANCE_TTL_SEC: float = 3600.0


def _digits(s: object) -> str:
    """Keep only digits for CLI comparison."""

    return re.sub(r"\D", "", str(s or ""))


def _cli_same_number(a: str, b: str) -> bool:
    """Cheap E164-ish equality using last ten digits when both long enough."""

    da = _digits(a)
    db = _digits(b)
    if len(da) >= 10 and len(db) >= 10:
        return da[-10:] == db[-10:]
    if da and db:
        return da == db
    return (str(a or "").strip() == str(b or "").strip())


def _norm_line_key(phone: str) -> str:
    p = str(phone or "").strip()
    return p if p.startswith("+") else f"+{_digits(p)}" if _digits(p) else p


def _load_env_blocked_lines() -> None:
    raw = (getattr(settings, "vobiz_blocked_from_lines", None) or os.environ.get("VOBIZ_BLOCKED_FROM_LINES") or "").strip()
    if not raw:
        return
    now = time.time()
    for part in raw.split(","):
        line = _norm_line_key(part.strip())
        if line:
            _BLOCKED_FROM_LINES[line] = now + _BLOCKED_FROM_TTL_SEC


def is_outbound_line_blocked(phone: str) -> bool:
    _load_env_blocked_lines()
    key = _norm_line_key(phone)
    until = _BLOCKED_FROM_LINES.get(key, 0.0)
    if until and time.time() < until:
        return True
    if until:
        _BLOCKED_FROM_LINES.pop(key, None)
    return False


def mark_outbound_line_blocked(phone: str, *, ttl_sec: float | None = None) -> None:
    key = _norm_line_key(phone)
    if not key:
        return
    ttl = float(ttl_sec if ttl_sec is not None else _BLOCKED_FROM_TTL_SEC)
    _BLOCKED_FROM_LINES[key] = time.time() + max(60.0, ttl)


def filter_dialable_outbound_numbers(numbers: list[str]) -> list[str]:
    _load_env_blocked_lines()
    out: list[str] = []
    for n in numbers:
        if n and not is_outbound_line_blocked(n):
            out.append(n)
    return out


def is_vobiz_from_line_blocked_error(exc: Exception) -> bool:
    from services.vobiz_bridge import VobizCallError

    if not isinstance(exc, VobizCallError):
        return False
    msg = (exc.message or "").lower()
    return exc.status == 403 and "blocked" in msg


def is_vobiz_insufficient_balance_error(exc: Exception) -> bool:
    from services.vobiz_bridge import VobizCallError

    if not isinstance(exc, VobizCallError):
        return False
    msg = (exc.message or "").lower()
    return exc.status == 402 or "insufficient balance" in msg


def mark_vobiz_auth_low_balance(auth_id: str, *, ttl_sec: float | None = None) -> None:
    key = str(auth_id or "").strip()
    if not key:
        return
    ttl = float(ttl_sec if ttl_sec is not None else _LOW_BALANCE_TTL_SEC)
    _LOW_BALANCE_AUTH[key] = time.time() + max(300.0, ttl)


def is_vobiz_auth_low_balance(auth_id: str) -> bool:
    key = str(auth_id or "").strip()
    until = _LOW_BALANCE_AUTH.get(key, 0.0)
    if until and time.time() < until:
        return True
    if until:
        _LOW_BALANCE_AUTH.pop(key, None)
    return False


def resolve_outbound_from_number(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> str:
    """Pick CLI: stored ``vobiz.from_number`` unless polluted; then per-role env; then global fallback."""

    vc = dict(vobiz_cfg or {})
    explicit = str(vc.get("from_number") or "").strip()

    r = normalize_console_role(role)

    fb_global = (settings.vobiz_from_number or "").strip()

    if explicit:
        return explicit

    if r == "sales_1":
        per_role_raw = settings.vobiz_sales_1_phone_1 or settings.vobiz_from_number
    else:
        per_role_raw = ""
    per_role = str(per_role_raw or "").strip()
    if per_role:
        return per_role
    return str(settings.vobiz_from_number or "").strip()


def get_all_outbound_numbers(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> list[str]:
    """Return all configured outbound phone numbers for a role (for round-robin dialing)."""
    vc = dict(vobiz_cfg or {})
    r = normalize_console_role(role)
    
    # Check if phone_numbers are stored in vobiz config
    stored_numbers = vc.get("phone_numbers", [])
    if stored_numbers:
        return [n for n in stored_numbers if n]
    
    # Fallback to env vars (2 parallel sub-workers per role — one per outbound line)
    if r == "sales_1":
        numbers = [
            settings.vobiz_sales_1_phone_1 or "",
            settings.vobiz_sales_1_phone_2 or "",
        ]
    else:
        numbers = [settings.vobiz_from_number or ""]
    
    return [n for n in numbers if n]


def dialable_outbound_numbers(role: str, vobiz_cfg: Optional[Mapping[str, object]] = None) -> list[str]:
    """Outbound lines that are configured and not temporarily blocked by Vobiz."""
    return filter_dialable_outbound_numbers(get_all_outbound_numbers(role, vobiz_cfg))


def build_phone_to_role_map() -> dict[str, str]:
    """Build reverse mapping: phone_digits -> role for incoming call routing."""
    mapping: dict[str, str] = {}

    def _add(num: str, role: str) -> None:
        digits = re.sub(r"\D", "", num or "")
        if digits:
            mapping[digits] = role
            if len(digits) > 10:
                mapping[digits[-10:]] = role

    roles_nums = {
        "sales_1": [
            settings.vobiz_sales_1_phone_1 or "",
            settings.vobiz_sales_1_phone_2 or "",
        ],
    }
    # WhatsApp / BotSpice business line if routed on Vobiz for inbound
    for num in (
        settings.botspice_whatsapp_number or "",
        settings.whatsapp_business_number or "",
        settings.vobiz_from_number or "",
    ):
        if num:
            _add(num, "sales_1")
    for role, nums in roles_nums.items():
        for num in nums:
            _add(num, role)
    if not mapping and settings.vobiz_from_number:
        _add(settings.vobiz_from_number, "sales_1")
    return mapping

