"""Operator DNC (Do-Not-Call) list — stored in the primary database (PostgreSQL)."""

from loguru import logger

# Dev/test lines — never dial in production campaigns (Chinmay manual QA).
_HARDCODED_DNC_SUFFIXES = frozenset({"7204955388"})


def _conn():
    from core.storage import _get_conn

    return _get_conn()


def init_dnc_table():
    """Ensure the DNC (Do Not Call) table exists."""
    try:
        conn = _conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dnc_list ("
            "phone TEXT UNIQUE PRIMARY KEY, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to initialize DNC table: {}", e)


def add_to_dnc(phone: str):
    """Add a phone number to the DNC list."""
    if not phone:
        return
    # Normalize phone: extract only digits
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return

    init_dnc_table()
    try:
        conn = _conn()
        conn.execute("INSERT OR IGNORE INTO dnc_list (phone) VALUES (?)", (phone.strip(),))
        conn.commit()
        logger.info("Added phone to DNC list: {}", phone)
    except Exception as e:
        logger.error("Failed to add to DNC: {}", e)


def is_phone_blocked(phone: str) -> bool:
    """Check if a phone number is on the operator DNC list (customer opted out).

    This is NOT Vobiz telephony provider rejection — see docs for provider-blocked destinations.
    """
    if not phone:
        return False

    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return False

    if any(digits.endswith(s) for s in _HARDCODED_DNC_SUFFIXES):
        return True

    init_dnc_table()
    try:
        conn = _conn()
        rows = conn.execute("SELECT phone FROM dnc_list").fetchall()
        for r in rows:
            blocked = r["phone"] if not isinstance(r, (list, tuple)) else r[0]
            blocked_digits = "".join(c for c in str(blocked) if c.isdigit())
            if blocked_digits and digits.endswith(blocked_digits):
                return True
    except Exception as e:
        logger.error("DNC lookup error: {}", e)

    return False
