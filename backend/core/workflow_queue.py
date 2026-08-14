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


def claim_next(
    conn: sqlite3.Connection, *, eligible_pool: str, number: str,
    now: float | None = None, lease_seconds: float = 300,
) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    token = uuid.uuid4().hex
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Recover abandoned claims before selecting.
        conn.execute(
            """UPDATE workflow_jobs SET status='ready',claim_token=NULL,
            claimed_by_number=NULL,claimed_at=NULL,lease_expires_at=NULL
            WHERE status IN ('claimed','running') AND lease_expires_at<?""", (now,)
        )
        row = conn.execute(
            """SELECT j.id FROM workflow_jobs j
            WHERE j.status='ready' AND j.due_at_utc<=? AND j.eligible_pool=?
              AND NOT EXISTS (
                SELECT 1 FROM workflow_jobs x
                WHERE x.lead_id=j.lead_id AND x.status IN ('claimed','running')
              )
              AND NOT EXISTS (SELECT 1 FROM do_not_contact d WHERE d.lead_id=j.lead_id)
            ORDER BY j.priority ASC,j.due_at_utc ASC,j.id ASC LIMIT 1""",
            (now, eligible_pool),
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
        conn.commit()
        columns = [d[0] for d in conn.execute("SELECT * FROM workflow_jobs LIMIT 0").description]
        return dict(zip(columns, result))
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
