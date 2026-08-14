"""Priority dispatcher. Dependency injection keeps it fully testable without calls."""

from __future__ import annotations

import inspect
import sqlite3
import time
from collections.abc import Awaitable, Callable

from core.number_allocator import allocate_number, relationship_number_for_source
from core.workflow_models import NumberPool
from core.workflow_queue import claim_next, complete_job, fail_job, promote_due

Executor = Callable[[dict, str | None], Awaitable[None] | None]


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


async def dispatch_once(
    conn: sqlite3.Connection, *, pools: dict[NumberPool, tuple[str, ...]],
    busy_numbers: set[str], phone_executor: Executor, whatsapp_executor: Executor,
    now: float | None = None, lease_seconds: float = 300,
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
        job = claim_next(
            conn, eligible_pool=pool.value, number=number or "WHATSAPP",
            now=now, lease_seconds=lease_seconds,
        )
        if not job:
            continue
        if number:
            busy_numbers.add(number)
        try:
            result = executor(job, number)
            if inspect.isawaitable(result):
                await result
            complete_job(conn, job["id"], job["claim_token"])
            return job
        except Exception as exc:
            fail_job(conn, job["id"], job["claim_token"], str(exc))
            return job
        finally:
            if number:
                busy_numbers.discard(number)
    return None
