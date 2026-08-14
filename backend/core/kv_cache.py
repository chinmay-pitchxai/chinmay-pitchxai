"""In-memory KV cache with TTL for dashboard state.

Reduces repeated DB queries and enrichment when the frontend polls
``/api/campaign/state`` every few seconds.  Cache is invalidated
whenever a lead is updated / added / reset / wiped.

Multi-VPS note: explicit invalidation writes ``last_cache_invalidation_time``
to SQLite so that the *other* VPS process clears its local cache on the next
``get()`` call (within ~2 s). Regular ``set()`` is local only; otherwise each
dashboard poll would invalidate the other VPS and defeat the cache.
"""

import time
from threading import Lock

_cache: dict[str, tuple[float, object]] = {}
_lock = Lock()
_DEFAULT_TTL = 0.4  # seconds — short TTL for live dashboard feel
_last_invalidation_time = 0.0
_last_db_invalidation_check = 0.0
_DB_INVALIDATION_CHECK_INTERVAL = 2.0  # avoid SQLite read on every dashboard poll


def _write_invalidation_ts_to_db(ts: float) -> None:
    """Persist the invalidation timestamp so other VPS processes pick it up."""
    global _last_invalidation_time
    _lock.acquire()
    try:
        _last_invalidation_time = ts
    finally:
        _lock.release()

    conn = None
    try:
        from core.storage import _get_conn
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)",
            ("last_cache_invalidation_time", str(ts)),
        )
        conn.commit()
    except Exception as e:
        from loguru import logger
        logger.error("Failed to write cache invalidation TS to DB: {}", e)
        if conn:
            try:
                conn.rollback()
            except Exception as re:
                logger.error("Failed to rollback in write_invalidation_ts: {}", re)


def _read_db_invalidation_ts() -> float | None:
    try:
        from core.storage import _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            ("last_cache_invalidation_time",),
        ).fetchone()
        if row:
            return float(row["value"])
    except Exception as e:
        from loguru import logger
        logger.error("Failed to read cache invalidation TS from DB: {}", e)
    return None


def _maybe_sync_invalidation_from_db() -> None:
    """Cross-VPS invalidation — throttled so high-frequency polls do not hammer SQLite."""
    global _last_invalidation_time, _last_db_invalidation_check
    now = time.monotonic()
    if now - _last_db_invalidation_check < _DB_INVALIDATION_CHECK_INTERVAL:
        return
    _last_db_invalidation_check = now
    db_val = _read_db_invalidation_ts()
    if db_val is None:
        return
    _lock.acquire()
    try:
        if db_val != _last_invalidation_time:
            _last_invalidation_time = db_val
            _cache.clear()
    finally:
        _lock.release()


def get(key: str) -> object | None:
    _maybe_sync_invalidation_from_db()

    _lock.acquire()
    try:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del _cache[key]
            return None
        return value
    finally:
        _lock.release()


def set(key: str, value: object, ttl: float | None = None) -> None:
    expires_at = time.monotonic() + (ttl if ttl is not None else _DEFAULT_TTL)
    _lock.acquire()
    try:
        _cache[key] = (expires_at, value)
    finally:
        _lock.release()


def delete(key: str) -> None:
    _lock.acquire()
    try:
        _cache.pop(key, None)
    finally:
        _lock.release()


def clear() -> None:
    _lock.acquire()
    try:
        _cache.clear()
    finally:
        _lock.release()
    _write_invalidation_ts_to_db(time.time())


def invalidate_cross_vps() -> None:
    """Signal the other VPS to clear its cache without touching our own local cache."""
    _write_invalidation_ts_to_db(time.time())


def invalidate_role(role: str) -> None:
    """Remove cached entries for a given role.

    Clears the dashboard ``campaign_state:{role}`` blob plus the live
    ``role_prompt:{role}`` prompt/rag cache and the Solitaire Unity KB block,
    so console prompt/RAG edits take effect on the very next call.
    """
    prefix = f"campaign_state:{role}"
    prom_key = prompt_cache_key(role)
    _lock.acquire()
    try:
        for k in list(_cache.keys()):
            if k.startswith(prefix) or k == prom_key:
                del _cache[k]
    finally:
        _lock.release()
    _write_invalidation_ts_to_db(time.time())


def invalidate_all() -> None:
    """Remove every cached campaign state entry (all roles)."""
    _lock.acquire()
    try:
        for k in list(_cache.keys()):
            if k.startswith("campaign_state:"):
                del _cache[k]
    finally:
        _lock.release()
    _write_invalidation_ts_to_db(time.time())


def state_cache_key(role: str) -> str:
    return f"campaign_state:{role}"


def state_set(role: str, value: object, ttl: float | None = None) -> None:
    set(state_cache_key(role), value, ttl if ttl is not None else _DEFAULT_TTL)


def state_get(role: str) -> object | None:
    return get(state_cache_key(role))


# ── Prompt / RAG / persona KV cache ──────────────────────────────────────
# The live session builds the system prompt on every call. Role prompt+RAG,
# the Solitaire Unity knowledge base are all cached here (long TTL) and
# invalidated when the operator edits role tuning via the console.

_PROMPT_TTL = 600.0  # 10 min — prompts change only via console tuning saves


def prompt_cache_key(role: str) -> str:
    return f"role_prompt:{role}"


def prompt_get(role: str) -> tuple[str, str] | None:
    """Return cached (prompt, rag) for a role, or None."""
    return get(prompt_cache_key(role))


def prompt_set(role: str, prompt: str, rag: str) -> None:
    set(prompt_cache_key(role), (prompt or "", rag or ""), ttl=_PROMPT_TTL)


def invalidate_prompt(role: str) -> None:
    """Drop the cached prompt/rag for a role (call after console tuning saves)."""
    delete(prompt_cache_key(role))
