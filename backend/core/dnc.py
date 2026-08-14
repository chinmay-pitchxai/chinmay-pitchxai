"""Operator DNC (Do-Not-Call) list — stored in the primary database (PostgreSQL).

One canonical DNC register: the ``do_not_contact`` table (created in
``core/storage.init_db``) keyed by ``normalized_phone`` (last 10 digits). The
orchestration layer (``orchestration_service.opt_out``) writes to the same
table, so an opt-out recorded by a call or WhatsApp reply is honored here and
vice-versa. Legacy SQLite-only ``dnc_list`` is no longer used.
"""

from loguru import logger

# Dev/test lines — never dial in production campaigns (Chinmay manual QA).
_HARDCODED_DNC_SUFFIXES = frozenset({"7204955388"})


def _conn():
    from core.storage import _get_conn

    return _get_conn()


def _normalized_key(phone: str) -> str:
    """Canonical DNC key: last 10 digits (matches orchestration_service.opt_out)."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def init_dnc_table():
    """Ensure the DNC (Do Not Call) table exists.

    The canonical ``do_not_contact`` table is created by ``init_db``; this is a
    defensive no-op guard for code paths that run before storage init.
    """
    try:
        conn = _conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS do_not_contact ("
            "normalized_phone TEXT PRIMARY KEY, "
            "lead_id INTEGER, "
            "reason TEXT DEFAULT '', "
            "source_interaction TEXT DEFAULT '', "
            "created_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to initialize DNC table: {}", e)


def add_to_dnc(phone: str, reason: str = "", source_interaction: str = ""):
    """Add a phone number to the DNC register (customer opted out)."""
    key = _normalized_key(phone)
    if not key:
        return
    init_dnc_table()
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO do_not_contact (normalized_phone, reason, source_interaction) "
            "VALUES (?, ?, ?) ON CONFLICT (normalized_phone) DO UPDATE SET "
            "reason = excluded.reason",
            (key, (reason or "operator"), source_interaction),
        )
        conn.commit()
        logger.info("Added phone to DNC register: {}", phone)
    except Exception as e:
        logger.error("Failed to add to DNC: {}", e)


def is_phone_blocked(phone: str) -> bool:
    """Check if a phone number is on the DNC register (customer opted out).

    This is NOT Vobiz telephony provider rejection — see docs for provider-blocked destinations.
    """
    if not phone:
        return False

    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return False

    if any(digits.endswith(s) for s in _HARDCODED_DNC_SUFFIXES):
        return True

    key = _normalized_key(phone)
    init_dnc_table()
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT 1 FROM do_not_contact WHERE normalized_phone = ?",
            (key,),
        ).fetchone()
        return row is not None
    except Exception as e:
        logger.error("DNC lookup error: {}", e)

    return False
