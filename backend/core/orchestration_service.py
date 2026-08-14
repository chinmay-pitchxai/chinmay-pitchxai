"""Event-to-job orchestration around the existing (unchanged) conversation engine."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from core.business_hours import add_working_hours
from core.number_allocator import pool_for
from core.workflow_models import JobType, LeadStage, TERMINAL_STAGES, require_transition
from core.workflow_queue import cancel_lead_jobs, create_job


PRIORITY = {
    JobType.CALLBACK: 1, JobType.FAILED_RETRY: 2,
    JobType.INTERESTED_FOLLOWUP: 3,
    JobType.SITE_VISIT_REMINDER_DAY_BEFORE: 4,
    JobType.SITE_VISIT_REMINDER_MORNING: 4,
    JobType.POST_VISIT_FEEDBACK: 5, JobType.FRESH_CALL: 6,
    JobType.WHATSAPP_PACKAGE: 3, JobType.WHATSAPP_FOLLOWUP_24H: 3,
}


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise ValueError(f"Unknown lead {lead_id}")
    return row


def set_stage(conn: sqlite3.Connection, lead_id: int, target: LeadStage | str) -> None:
    row = _lead(conn, lead_id)
    current = LeadStage(row["lifecycle_status"] or "new")
    target = LeadStage(target)
    require_transition(current, target)
    conn.execute(
        "UPDATE leads SET lifecycle_status=?,orchestration_version=orchestration_version+1,updated_at=datetime('now') WHERE id=?",
        (target.value, lead_id),
    )
    conn.commit()


def schedule_job(conn, *, lead_id: int, job_type: JobType, source: str,
                 due_at: datetime, key: str, attempt: int = 0,
                 source_type: str = "", source_id: str = "", payload=None) -> int:
    lead = _lead(conn, lead_id)
    if conn.execute("SELECT 1 FROM do_not_contact WHERE normalized_phone=?", (_phone(lead["phone"]),)).fetchone():
        raise PermissionError("Lead is opted out")
    pool = pool_for(job_type, source, attempt)
    return create_job(
        conn, lead_id=lead_id, job_type=job_type.value,
        priority=PRIORITY[job_type], due_at_utc=due_at.astimezone(timezone.utc).timestamp(),
        eligible_pool=pool.value, idempotency_key=key, attempt_number=attempt,
        source_type=source_type, source_id=source_id, payload=payload,
    )


def opt_out(conn: sqlite3.Connection, lead_id: int, reason: str, source_interaction: str = "") -> None:
    lead = _lead(conn, lead_id)
    conn.execute(
        "INSERT OR IGNORE INTO do_not_contact(normalized_phone,lead_id,reason,source_interaction) VALUES(?,?,?,?)",
        (_phone(lead["phone"]), lead_id, reason, source_interaction),
    )
    conn.execute(
        "UPDATE leads SET lifecycle_status='opted_out',orchestration_version=orchestration_version+1,updated_at=datetime('now') WHERE id=?",
        (lead_id,),
    )
    conn.commit()
    cancel_lead_jobs(conn, lead_id, "global opt-out")


def failed_call(conn: sqlite3.Connection, *, lead_id: int, source: str,
                retry_cycle: str, attempt: int, from_number: str,
                outcome: str, ended_at: datetime) -> int | None:
    lead = _lead(conn, lead_id)
    role = str(lead["role"] if lead["role"] else "campaign")
    max_retries = _get_max_retries_for_role(role)
    if not (1 <= attempt <= max_retries):
        raise ValueError(f"Attempt {attempt} out of range (1-{max_retries})")
    conn.execute(
        """INSERT OR IGNORE INTO call_attempts
        (lead_id,role,retry_cycle,attempt_number,from_number,outcome,ended_at)
        VALUES(?,?,?,?,?,?,?)""",
        (lead_id, role, retry_cycle, attempt, from_number, outcome, ended_at.timestamp()),
    )
    if attempt >= max_retries:
        conn.execute("UPDATE leads SET lifecycle_status='lost' WHERE id=?", (lead_id,))
        conn.commit()
        return None
    next_attempt = attempt + 1
    wait_hours = 12 if attempt == 1 else 24
    due = add_working_hours(ended_at, wait_hours)
    conn.execute("UPDATE leads SET lifecycle_status='failed_retry_waiting' WHERE id=?", (lead_id,))
    conn.commit()
    return schedule_job(
        conn, lead_id=lead_id, job_type=JobType.FAILED_RETRY, source=source,
        due_at=due, key=f"retry:{lead_id}:{retry_cycle}:{next_attempt}",
        attempt=next_attempt, source_type="retry_cycle", source_id=retry_cycle,
        payload={"previous_outcome": outcome},
    )


def _get_max_retries_for_role(role: str) -> int:
    """Get max retry count from campaign_config, fallback to 3."""
    try:
        from core.state import get_campaign_config
        from core.campaign_hours import get_retry_count
        cfg = get_campaign_config(role) or {}
        return get_retry_count(cfg)
    except Exception:
        return 3


def schedule_callback(conn, *, lead_id: int, source: str, due_at: datetime, reason: str) -> int:
    conn.execute(
        "UPDATE workflow_jobs SET status='cancelled',error='callback rescheduled' "
        "WHERE lead_id=? AND job_type='callback' AND status IN ('scheduled','ready','claimed')",
        (lead_id,),
    )
    conn.execute("UPDATE leads SET lifecycle_status='callback_requested' WHERE id=?", (lead_id,)); conn.commit()
    return schedule_job(
        conn, lead_id=lead_id, job_type=JobType.CALLBACK, source=source,
        due_at=due_at, key=f"callback:{lead_id}:{int(due_at.timestamp())}", payload={"reason": reason},
    )


def interested(conn, *, lead_id: int, source: str, now: datetime, interest_cycle: str) -> tuple[int, int]:
    conn.execute("UPDATE leads SET lifecycle_status='interested' WHERE id=?", (lead_id,)); conn.commit()
    package = schedule_job(
        conn, lead_id=lead_id, job_type=JobType.WHATSAPP_PACKAGE, source=source,
        due_at=now, key=f"wa-package:{lead_id}:{interest_cycle}", source_type="interest_cycle", source_id=interest_cycle,
    )
    followup_due = add_working_hours(now, 24)
    followup = schedule_job(
        conn, lead_id=lead_id, job_type=JobType.WHATSAPP_FOLLOWUP_24H, source=source,
        due_at=followup_due, key=f"wa-followup:{lead_id}:{interest_cycle}", source_type="interest_cycle", source_id=interest_cycle,
    )
    return package, followup


def whatsapp_package_sent(conn, *, lead_id: int, source: str, sent_at: datetime, interest_cycle: str) -> int:
    """Schedule only the 24-working-hour follow-up after confirmed package delivery."""
    conn.execute("UPDATE leads SET lifecycle_status='interested' WHERE id=?", (lead_id,)); conn.commit()
    return schedule_job(
        conn, lead_id=lead_id, job_type=JobType.WHATSAPP_FOLLOWUP_24H, source=source,
        due_at=add_working_hours(sent_at, 24), key=f"wa-followup:{lead_id}:{interest_cycle}",
        source_type="interest_cycle", source_id=interest_cycle,
    )


def schedule_no_reply_call(conn, *, lead_id: int, source: str, sent_at: datetime, interest_cycle: str) -> int:
    from config import settings
    wait_hours = max(2, min(3, int(settings.whatsapp_no_reply_call_hours or 3)))
    return schedule_job(
        conn, lead_id=lead_id, job_type=JobType.INTERESTED_FOLLOWUP, source=source,
        due_at=add_working_hours(sent_at, wait_hours),
        key=f"wa-no-reply-call:{lead_id}:{interest_cycle}",
        source_type="interest_cycle", source_id=interest_cycle,
        payload={"reason": f"No reply {wait_hours} working hours after WhatsApp nudge"},
    )


def schedule_site_visit(conn, *, lead_id: int, source: str, scheduled_at: datetime,
                        family_members="", preferred_unit="", budget="", location="", notes="") -> int:
    cur = conn.execute(
        """INSERT INTO site_visits(lead_id,scheduled_at_utc,family_members,preferred_unit,budget,location,notes)
        VALUES(?,?,?,?,?,?,?)""",
        (lead_id, scheduled_at.astimezone(timezone.utc).timestamp(), family_members, preferred_unit, budget, location, notes),
    )
    visit_id = int(cur.lastrowid)
    conn.execute("UPDATE leads SET lifecycle_status='site_visit_scheduled' WHERE id=?", (lead_id,)); conn.commit()
    day_before = scheduled_at - timedelta(days=1)
    morning = scheduled_at.replace(hour=9, minute=0, second=0, microsecond=0)
    for job_type, due, suffix in (
        (JobType.SITE_VISIT_REMINDER_DAY_BEFORE, day_before, "day-before"),
        (JobType.SITE_VISIT_REMINDER_MORNING, morning, "morning"),
    ):
        schedule_job(conn, lead_id=lead_id, job_type=job_type, source=source, due_at=due,
                     key=f"visit:{visit_id}:v1:{suffix}", source_type="site_visit", source_id=str(visit_id))
    return visit_id


def reschedule_site_visit(conn, *, visit_id: int, source: str, scheduled_at: datetime) -> None:
    conn.row_factory = sqlite3.Row
    visit = conn.execute("SELECT * FROM site_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        raise ValueError("Unknown site visit")
    version = int(visit["version"]) + 1
    conn.execute(
        "UPDATE workflow_jobs SET status='cancelled',error='visit rescheduled' "
        "WHERE source_type='site_visit' AND source_id=? AND status IN ('scheduled','ready','claimed')",
        (str(visit_id),),
    )
    conn.execute(
        "UPDATE site_visits SET scheduled_at_utc=?,version=?,updated_at=datetime('now') WHERE id=?",
        (scheduled_at.astimezone(timezone.utc).timestamp(), version, visit_id),
    )
    conn.commit()
    for job_type, due, suffix in (
        (JobType.SITE_VISIT_REMINDER_DAY_BEFORE, scheduled_at - timedelta(days=1), "day-before"),
        (JobType.SITE_VISIT_REMINDER_MORNING, scheduled_at.replace(hour=9, minute=0, second=0, microsecond=0), "morning"),
    ):
        schedule_job(
            conn, lead_id=visit["lead_id"], job_type=job_type, source=source, due_at=due,
            key=f"visit:{visit_id}:v{version}:{suffix}", source_type="site_visit", source_id=str(visit_id),
        )


def complete_site_visit(conn, *, visit_id: int, source: str, completed_at: datetime) -> int:
    conn.row_factory = sqlite3.Row
    visit = conn.execute("SELECT * FROM site_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        raise ValueError("Unknown site visit")
    conn.execute("UPDATE site_visits SET status='completed',completed_at=? WHERE id=?", (completed_at.timestamp(), visit_id))
    conn.execute("UPDATE leads SET lifecycle_status='feedback_pending' WHERE id=?", (visit["lead_id"],)); conn.commit()
    # ── Sandbox transition: SB3 → SB4 (Post-Visit Feedback, P9) ──
    # Completed site visit moves the lead into the feedback sandbox so the
    # dashboard's SB4 view reflects leads awaiting the post-visit feedback call.
    try:
        from core.storage import _update_lead_sandbox_sync

        _update_lead_sandbox_sync(int(visit["lead_id"]), 4)
    except Exception:
        pass
    return schedule_job(
        conn, lead_id=visit["lead_id"], job_type=JobType.POST_VISIT_FEEDBACK, source=source,
        due_at=completed_at + timedelta(days=1), key=f"feedback:visit:{visit_id}",
        source_type="site_visit", source_id=str(visit_id),
    )


def record_feedback(conn, *, visit_id: int, job_id: int, outcome: str, details=None) -> None:
    conn.row_factory = sqlite3.Row
    visit = conn.execute("SELECT * FROM site_visits WHERE id=?", (visit_id,)).fetchone()
    lead_id = int(visit["lead_id"])
    conn.execute(
        "INSERT OR IGNORE INTO feedback_records(lead_id,site_visit_id,job_id,outcome,details_json) VALUES(?,?,?,?,?)",
        (lead_id, visit_id, job_id, outcome, json.dumps(details or {})),
    )
    target = {
        "booked": "booked", "follow_up": "follow_up", "revisit": "site_visit_scheduled",
        "not_interested": "not_interested", "lost": "lost",
    }.get(outcome)
    if target:
        conn.execute("UPDATE leads SET lifecycle_status=? WHERE id=?", (target, lead_id)); conn.commit()
    if outcome in {"booked", "not_interested", "lost"}:
        cancel_lead_jobs(conn, lead_id, f"feedback outcome: {outcome}")


def feedback_no_answer(conn, *, visit_id: int, source: str, attempt: int, ended_at: datetime) -> int | None:
    """Bounded P6/P7 feedback policy: initial attempt plus one 24-working-hour retry."""
    if attempt not in (1, 2):
        raise ValueError("Feedback attempt must be 1 or 2")
    if attempt == 2:
        return None
    visit = conn.execute("SELECT lead_id FROM site_visits WHERE id=?", (visit_id,)).fetchone()
    if not visit:
        raise ValueError("Unknown site visit")
    due = add_working_hours(ended_at, 24)
    return schedule_job(
        conn, lead_id=int(visit[0]), job_type=JobType.POST_VISIT_FEEDBACK, source=source,
        due_at=due, key=f"feedback:visit:{visit_id}:attempt:2", attempt=2,
        source_type="site_visit", source_id=str(visit_id), payload={"relationship_attempt": 2},
    )


def relationship_no_answer(conn, *, job: dict, source: str, ended_at: datetime) -> int | None:
    """One bounded P6/P7 retry for callbacks, follow-ups, reminders and feedback."""
    attempt = int(job.get("attempt_number") or 1)
    if attempt >= 2:
        return None
    job_type = JobType(job["job_type"])
    if job_type == JobType.POST_VISIT_FEEDBACK and job.get("source_id"):
        return feedback_no_answer(
            conn, visit_id=int(job["source_id"]), source=source,
            attempt=attempt, ended_at=ended_at,
        )
    if job_type not in {
        JobType.CALLBACK, JobType.INTERESTED_FOLLOWUP,
        JobType.SITE_VISIT_REMINDER_DAY_BEFORE, JobType.SITE_VISIT_REMINDER_MORNING,
        JobType.SITE_VISIT_RESCHEDULE,
    }:
        return None
    return schedule_job(
        conn, lead_id=int(job["lead_id"]), job_type=job_type, source=source,
        due_at=add_working_hours(ended_at, 24),
        key=f"relationship-retry:{job['id']}:2", attempt=2,
        source_type=str(job.get("source_type") or ""), source_id=str(job.get("source_id") or ""),
        payload={"relationship_attempt": 2},
    )


def update_memory(conn: sqlite3.Connection, lead_id: int, facts: dict, summary: str = "", at: datetime | None = None) -> int:
    row = conn.execute("SELECT facts_json,version FROM lead_memory WHERE lead_id=?", (lead_id,)).fetchone()
    existing = json.loads(row[0] or "{}") if row else {}
    existing.update({k: v for k, v in facts.items() if v not in (None, "", [], {})})
    version = (int(row[1]) + 1) if row else 1
    stamp = (at or datetime.now(timezone.utc)).timestamp()
    conn.execute(
        """INSERT INTO lead_memory(lead_id,facts_json,summary,last_interaction_at,version)
        VALUES(?,?,?,?,?) ON CONFLICT(lead_id) DO UPDATE SET
        facts_json=excluded.facts_json,
        summary=CASE WHEN excluded.summary!='' THEN excluded.summary ELSE lead_memory.summary END,
        last_interaction_at=excluded.last_interaction_at,version=excluded.version,
        updated_at=datetime('now')""",
        (lead_id, json.dumps(existing), summary, stamp, version),
    )
    conn.commit()
    return version
