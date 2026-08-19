"""SQLite-backed idempotent workflow queue with atomic claims and lead locks."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any


ACTIVE_STATUSES = ("scheduled", "ready", "claimed", "running")


def create_job(
    conn: sqlite3.Connection, *, lead_id: int, job_type: str, priority: int,
    due_at_utc: float, eligible_pool: str, idempotency_key: str,
    source_type: str = "", source_id: str = "", attempt_number: int = 0,
    payload: dict[str, Any] | None = None,
) -> int:
    if attempt_number < 0 or attempt_number > 3:
        raise ValueError("attempt_number must be between 0 and 3")
    conn.execute(
        """INSERT OR IGNORE INTO workflow_jobs
        (lead_id,job_type,source_type,source_id,priority,status,due_at_utc,
         eligible_pool,attempt_number,idempotency_key,payload_json)
        VALUES (?,?,?,?,?,'scheduled',?,?,?,?,?)""",
        (lead_id, job_type, source_type, source_id, priority, due_at_utc,
         eligible_pool, attempt_number, idempotency_key, json.dumps(payload or {})),
    )
    row = conn.execute(
        "SELECT id FROM workflow_jobs WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    conn.commit()
    return int(row[0])


def promote_due(conn: sqlite3.Connection, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE workflow_jobs SET status='ready',updated_at=datetime('now') "
        "WHERE status='scheduled' AND due_at_utc<=?", (now,)
    )
    conn.commit()
    return cur.rowcount


def _is_postgres(conn) -> bool:
    """True when ``conn`` is the Postgres-backed shim (core.db.PostgresConnection)."""
    try:
        from core.db import PostgresConnection
        return isinstance(conn, PostgresConnection)
    except Exception:
        return False


def _unclaim(conn: sqlite3.Connection, job_id: int, token: str | None = None) -> None:
    """Release a claimed job back to ``ready`` (same transaction as the caller)."""
    if token:
        conn.execute(
            """UPDATE workflow_jobs SET status='ready',claim_token=NULL,
            claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL,
            updated_at=datetime('now') WHERE id=? AND claim_token=?""",
            (job_id, token),
        )
    else:
        conn.execute(
            """UPDATE workflow_jobs SET status='ready',claim_token=NULL,
            claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL,
            updated_at=datetime('now') WHERE id=?""",
            (job_id,),
        )
    # claim_next marks the lead dialing inside the same transaction. If the
    # physical line loses the capacity race, undo that display state together
    # with the claim so the dashboard never shows a call that was not placed.
    conn.execute(
        """UPDATE leads SET status='pending',updated_at=datetime('now')
        WHERE id=(SELECT lead_id FROM workflow_jobs WHERE id=?)
          AND lower(COALESCE(status,'pending'))='dialing'""",
        (job_id,),
    )


def claim_next(
    conn: sqlite3.Connection, *, eligible_pool: str, number: str,
    now: float | None = None, lease_seconds: float = 300,
    number_resolver=None, skip_recently_days: int = 0,
) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    token = uuid.uuid4().hex
    pg = _is_postgres(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Campaign config "Skip recently-called numbers": never claim a lead
        # whose first call happened within the last N days.
        recently_filter = ""
        if skip_recently_days and skip_recently_days > 0:
            recently_filter = (
                " AND (l.first_called_at IS NULL OR l.first_called_at <= ?)\n"
            )
        # Recover abandoned claims before selecting.
        conn.execute(
            """UPDATE workflow_jobs SET status='ready',claim_token=NULL,
            claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL
            WHERE status IN ('claimed','running') AND lease_expires_at<?""", (now,)
        )
        if pg:
            # PostgreSQL: atomic job claim + lead lock. ``FOR UPDATE OF j, l
            # SKIP LOCKED`` (plan Phase 4) makes the claim race-free: a second
            # worker either sees the job already claimed (conditional UPDATE
            # below fails) or skips the row while the first worker's
            # transaction holds it. Locking the lead row too prevents two
            # *different* jobs for the same lead from being claimed by two
            # workers concurrently (the lead-lock NOT EXISTS check alone is not
            # atomic under READ COMMITTED without the row lock).
            row = conn.execute(
                f"""SELECT j.id FROM workflow_jobs j
                JOIN leads l ON l.id=j.lead_id
                WHERE j.status='ready' AND j.due_at_utc<=? AND j.eligible_pool=?
                  AND NOT EXISTS (
                    SELECT 1 FROM workflow_jobs x
                    WHERE x.lead_id=j.lead_id AND x.status IN ('claimed','running')
                  )
                  AND NOT EXISTS (SELECT 1 FROM do_not_contact d WHERE d.lead_id=j.lead_id)
                  AND NOT EXISTS (
                    SELECT 1 FROM do_not_contact d
                    WHERE d.normalized_phone = right(replace(replace(l.phone,'+',''),' ',''), 10)
                  )
                {recently_filter}
                ORDER BY j.priority ASC,j.due_at_utc ASC,j.id ASC
                LIMIT 1 FOR UPDATE OF j, l SKIP LOCKED""",
                (now, eligible_pool) + ((now - skip_recently_days * 86400,) if skip_recently_days > 0 else ()),
            ).fetchone()
        else:
            row = conn.execute(
                f"""SELECT j.id FROM workflow_jobs j
                JOIN leads l ON l.id=j.lead_id
                WHERE j.status='ready' AND j.due_at_utc<=? AND j.eligible_pool=?
                  AND NOT EXISTS (
                    SELECT 1 FROM workflow_jobs x
                    WHERE x.lead_id=j.lead_id AND x.status IN ('claimed','running')
                  )
                  AND NOT EXISTS (SELECT 1 FROM do_not_contact d WHERE d.lead_id=j.lead_id)
                  AND NOT EXISTS (
                    SELECT 1 FROM do_not_contact d
                    WHERE d.normalized_phone = substr(replace(replace(l.phone,'+',''),' ',''), -10)
                  )
                {recently_filter}
                ORDER BY j.priority ASC,j.due_at_utc ASC,j.id ASC LIMIT 1""",
                (now, eligible_pool) + ((now - skip_recently_days * 86400,) if skip_recently_days > 0 else ()),
            ).fetchone()
        if not row:
            conn.commit()
            return None
        job_id = int(row[0])
        cur = conn.execute(
            """UPDATE workflow_jobs SET status='claimed',claimed_by_number=?,
            claim_token=?,claimed_at=?,lease_expires_at=?,updated_at=datetime('now')
            WHERE id=? AND status='ready'""",
            (number, token, now, now + lease_seconds, job_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        result = conn.execute("SELECT * FROM workflow_jobs WHERE id=?", (job_id,)).fetchone()
        columns = [d[0] for d in conn.execute("SELECT * FROM workflow_jobs LIMIT 0").description]
        job = dict(zip(columns, result))
        # Flip the LEAD to 'dialing' at claim time so the dashboard shows a
        # call in progress immediately — not 'pending'. The lead-level status
        # write is idempotent and later corrected by the call outcome.
        try:
            conn.execute(
                "UPDATE leads SET status='dialing', updated_at=datetime('now') WHERE id=? AND status IN ('pending','scheduled','ready')",
                (job["lead_id"],),
            )
        except Exception:
            pass  # best-effort; outcome handlers own the final status
        if number_resolver is not None:
            # Re-resolve the dialing line from the *actually claimed* job (the
            # candidate the caller pre-peeked may have been claimed by another
            # worker in the meantime). Kept inside the same transaction so the
            # claim and its line assignment stay atomic.
            final_number = number_resolver(conn, job)
            if not final_number:
                _unclaim(conn, job_id)
                conn.commit()
                return None
            if final_number != number:
                conn.execute(
                    "UPDATE workflow_jobs SET claimed_by_number=? WHERE id=?",
                    (final_number, job_id),
                )
                job["claimed_by_number"] = final_number
        conn.commit()
        return job
    except Exception:
        conn.rollback()
        raise


def complete_job(conn: sqlite3.Connection, job_id: int, claim_token: str) -> bool:
    cur = conn.execute(
        "UPDATE workflow_jobs SET status='completed',updated_at=datetime('now') "
        "WHERE id=? AND claim_token=? AND status IN ('claimed','running')",
        (job_id, claim_token),
    )
    conn.commit()
    return cur.rowcount == 1


def defer_job(
    conn: sqlite3.Connection,
    job_id: int,
    claim_token: str,
    *,
    delay_seconds: float = 1.0,
    reason: str = "Dialing capacity temporarily unavailable",
) -> bool:
    """Return a transiently blocked job to the runnable queue.

    A carrier/line-capacity miss is not a completed call attempt. Keeping the
    same job and idempotency key prevents duplicate rows while guaranteeing the
    next dispatcher cycle can try the lead again.
    """
    due_at = time.time() + max(0.25, float(delay_seconds))
    cur = conn.execute(
        """UPDATE workflow_jobs SET status='scheduled',due_at_utc=?,error=?,
        claim_token=NULL,claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL,
        updated_at=datetime('now')
        WHERE id=? AND claim_token=? AND status IN ('claimed','running')""",
        (due_at, reason[:1000], job_id, claim_token),
    )
    if cur.rowcount:
        conn.execute(
            "UPDATE leads SET status='pending',updated_at=datetime('now') WHERE id=(SELECT lead_id FROM workflow_jobs WHERE id=?)",
            (job_id,),
        )
    conn.commit()
    return cur.rowcount == 1


def recover_completed_pending_phone_jobs(conn: sqlite3.Connection) -> int:
    """Repair jobs incorrectly completed before a phone call was dispatched.

    Older builds completed a workflow job even when the worker reported a
    transient ``Line busy``/carrier-capacity result and reset the lead to
    ``pending``. Those rows had no active job left and could never be dialed.
    This startup repair safely requeues only pending/dialing phone leads that
    have no other active workflow job.
    """
    now = time.time()
    rows = conn.execute(
        """SELECT j.id,j.lead_id FROM workflow_jobs j
        JOIN leads l ON l.id=j.lead_id
        WHERE j.status='completed'
          AND j.job_type IN ('fresh_call','failed_retry')
          AND lower(COALESCE(l.status,'pending')) IN ('pending','dialing')
          AND NOT EXISTS (
            SELECT 1 FROM workflow_jobs x
            WHERE x.lead_id=j.lead_id
              AND x.status IN ('scheduled','ready','claimed','running')
          )
          AND j.id=(SELECT MAX(j2.id) FROM workflow_jobs j2 WHERE j2.lead_id=j.lead_id)"""
    ).fetchall()
    for job_id, lead_id in rows:
        conn.execute(
            """UPDATE workflow_jobs SET status='scheduled',due_at_utc=?,
            error='Recovered incomplete dispatch',claim_token=NULL,
            claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL,
            updated_at=datetime('now') WHERE id=? AND status='completed'""",
            (now, int(job_id)),
        )
        conn.execute(
            "UPDATE leads SET status='pending',updated_at=datetime('now') WHERE id=?",
            (int(lead_id),),
        )
    conn.commit()
    return len(rows)


def fail_job(conn: sqlite3.Connection, job_id: int, claim_token: str, error: str) -> bool:
    cur = conn.execute(
        "UPDATE workflow_jobs SET status='failed',error=?,updated_at=datetime('now') "
        "WHERE id=? AND claim_token=? AND status IN ('claimed','running')",
        (error[:1000], job_id, claim_token),
    )
    conn.commit()
    return cur.rowcount == 1


def cancel_lead_jobs(conn: sqlite3.Connection, lead_id: int, reason: str = "") -> int:
    cur = conn.execute(
        """UPDATE workflow_jobs SET status='cancelled',error=?,updated_at=datetime('now')
        WHERE lead_id=? AND status IN ('scheduled','ready','claimed','running')""",
        (reason, lead_id),
    )
    conn.commit()
    return cur.rowcount
