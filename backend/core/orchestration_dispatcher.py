"""Priority dispatcher. Dependency injection keeps it fully testable without calls."""

from __future__ import annotations

import inspect
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable

from core.number_allocator import allocate_number, relationship_number_for_source
from core.workflow_models import NumberPool
from core.workflow_queue import claim_next, complete_job, fail_job, promote_due

Executor = Callable[[dict, str | None], Awaitable[None] | None]

# Guards the busy-numbers capacity check+increment so multiple dispatcher
# workers (orchestration_worker_count>1) cannot both pass the pre-check for
# the last free slot of a pool (e.g. the 2nd concurrent P3 digital call).
_BUSY_LOCK = threading.Lock()

# Per-pool campaign config "skip recently-called" days (0 = disabled).
_SKIP_RECENTLY_CACHE: dict[str, int] = {}
_SKIP_RECENTLY_AT: float = 0.0


def _skip_recently_days_for(conn: sqlite3.Connection, pool: NumberPool) -> int:
    """Best-effort read of the campaign's skip_recently_days (cached 30s).

    Reads role_state.campaign_config for the console role; falls back to 0 on
    any error so a config hiccup never blocks dialing.
    """
    global _SKIP_RECENTLY_CACHE, _SKIP_RECENTLY_AT
    try:
        if time.time() - _SKIP_RECENTLY_AT < 30:
            return _SKIP_RECENTLY_CACHE.get(pool.value, 0)
        from core.campaign_hours import get_skip_recently_days
        from core.state import _ROLES, get_state

        result: dict[str, int] = {}
        for role in _ROLES:
            cfg = (get_state(role).get("campaign_config") or {})
            result[role] = get_skip_recently_days(cfg)
        _SKIP_RECENTLY_CACHE = {NumberPool(p).value: v for p, v in result.items()}
        _SKIP_RECENTLY_AT = time.time()
        return _SKIP_RECENTLY_CACHE.get(pool.value, 0)
    except Exception:
        return 0


def _peek_relationship_number(
    conn: sqlite3.Connection,
    pools: dict[NumberPool, tuple[str, ...]],
    now: float,
) -> str | None:
    """Relationship jobs ring from the lead's originating sandbox line (P1/P2 cold, P3 digital)."""
    row = conn.execute(
        """SELECT j.lead_id,l.role,l.source FROM workflow_jobs j
        JOIN leads l ON l.id=j.lead_id
        WHERE j.status='ready' AND j.due_at_utc<=? AND j.eligible_pool=?
          AND NOT EXISTS (
            SELECT 1 FROM workflow_jobs x
            WHERE x.lead_id=j.lead_id AND x.status IN ('claimed','running')
          )
          AND NOT EXISTS (SELECT 1 FROM do_not_contact d WHERE d.lead_id=j.lead_id)
        ORDER BY j.priority ASC,j.due_at_utc ASC,j.id ASC LIMIT 1""",
        (now, NumberPool.SANDBOX1_CALLBACK.value),
    ).fetchone()
    if not row:
        return None
    # Digital detection must come from the lead's source column (production leads
    # always have role=sales_1, so role alone would wrongly send digital callbacks
    # to the cold P1 line). Fall back to role only when source is empty.
    lead_source = str(row[2] if row[2] else row[1] if row[1] else "campaign")
    return relationship_number_for_source(lead_source, pools)


def _job_lead_source(conn: sqlite3.Connection, job: dict) -> str:
    """Resolve a job's lead source (digital vs cold) for originating-line routing."""
    row = conn.execute(
        "SELECT source,role FROM leads WHERE id=?", (job["lead_id"],)
    ).fetchone()
    if not row:
        return "campaign"
    return str(row[0] if row[0] else (row[1] if row[1] else "campaign"))


async def dispatch_once(
    conn: sqlite3.Connection, *, pools: dict[NumberPool, tuple[str, ...]],
    busy_numbers: dict[str, int], phone_executor: Executor, whatsapp_executor: Executor,
    now: float | None = None, lease_seconds: float = 300,
    number_cooling: Callable[[str], bool] | None = None,
) -> dict | None:
    now = time.time() if now is None else now
    promote_due(conn, now)
    candidates = conn.execute(
        """SELECT eligible_pool,MIN(priority) AS p,MIN(due_at_utc) AS due
        FROM workflow_jobs WHERE status='ready' AND due_at_utc<=?
        GROUP BY eligible_pool ORDER BY p ASC,due ASC""", (now,)
    ).fetchall()
    for pool_name, _priority, _due in candidates:
        pool = NumberPool(pool_name)
        if pool == NumberPool.WHATSAPP:
            number = None
            executor = whatsapp_executor
        elif pool == NumberPool.SANDBOX1_CALLBACK:
            number = _peek_relationship_number(conn, pools, now)
            executor = phone_executor
            if not number:
                continue
        else:
            number = allocate_number(pool, busy_numbers, pools)
            executor = phone_executor
            if not number:
                continue
        if number and number_cooling and number_cooling(number):
            continue
        # Callbacks must ring from the *claimed* lead's originating line
        # (P1/P2 cold, P3 digital). Resolve inside the claim transaction so the
        # line always matches the job that was actually claimed — the peek above
        # is only a pre-flight check for line availability.
        resolver = None
        if pool == NumberPool.SANDBOX1_CALLBACK:
            resolver = (
                lambda c, j: relationship_number_for_source(_job_lead_source(c, j), pools)
            )
        job = claim_next(
            conn, eligible_pool=pool.value, number=number or "WHATSAPP",
            now=now, lease_seconds=lease_seconds, number_resolver=resolver,
            skip_recently_days=_skip_recently_days_for(conn, pool),
        )
        if not job:
            continue
        claimed_number = job.get("claimed_by_number") or number
        if claimed_number and pool != NumberPool.WHATSAPP:
            # The resolved line may differ from the pre-peeked one — re-check
            # line lock and anti-spam cooldown against the actual line before
            # dialing, releasing the claim back to ready if it is unavailable.
            # Capacity = pool-tuple occurrences (P3 x2 in the digital pool
            # allows two concurrent calls on that one line).
            pool_capacity = sum(1 for x in pools.get(pool, ()) if x == claimed_number) or 1
            # busy_numbers is a dict (number -> active count) in production;
            # legacy callers/tests may still pass a plain set — handle both.
            busy_is_dict = isinstance(busy_numbers, dict)
            with _BUSY_LOCK:
                active = busy_numbers.get(claimed_number, 0) if busy_is_dict else (1 if claimed_number in busy_numbers else 0)
                if active >= pool_capacity or (
                    number_cooling and number_cooling(claimed_number)
                ):
                    from core.workflow_queue import _unclaim
                    _unclaim(conn, job["id"], job.get("claim_token"))
                    conn.commit()
                    continue
                if busy_is_dict:
                    busy_numbers[claimed_number] = busy_numbers.get(claimed_number, 0) + 1
                else:
                    busy_numbers.add(claimed_number)
        try:
            result = executor(job, claimed_number or None)
            if inspect.isawaitable(result):
                await result
            complete_job(conn, job["id"], job["claim_token"])
            if claimed_number and number_cooling and hasattr(number_cooling, "record"):
                number_cooling.record(claimed_number)
            return job
        except Exception as exc:
            fail_job(conn, job["id"], job["claim_token"], str(exc))
            return job
        finally:
            if claimed_number and pool != NumberPool.WHATSAPP:
                if isinstance(busy_numbers, dict):
                    busy_numbers[claimed_number] = max(0, busy_numbers.get(claimed_number, 0) - 1)
                else:
                    busy_numbers.discard(claimed_number)
    return None
