"""Event-to-job orchestration around the existing (unchanged) conversation engine."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from core.business_hours import add_working_hours, next_working_time
from core.number_allocator import pool_for
from core.workflow_models import JobType, LeadStage, TERMINAL_STAGES, require_transition
from core.workflow_queue import cancel_lead_jobs, create_job


PRIORITY = {
    JobType.CALLBACK: 1, JobType.FAILED_RETRY: 2,
    JobType.INTERESTED_FOLLOWUP: 3,
    JobType.SITE_VISIT_REMINDER_DAY_BEFORE: 4,
    JobType.SITE_VISIT_REMINDER_MORNING: 4,
    JobType.SITE_VISIT_RESCHEDULE: 4,
    JobType.POST_VISIT_FEEDBACK: 5, JobType.FRESH_CALL: 6,
    JobType.WHATSAPP_PACKAGE: 3, JobType.WHATSAPP_FOLLOWUP_24H: 3,
}


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _set_sandbox(conn: sqlite3.Connection, lead_id: int, sandbox: int) -> None:
    """Best-effort sync of the lead's sandbox column (dashboard signal)."""
    try:
        from core.storage import _update_lead_sandbox_sync

        _update_lead_sandbox_sync(int(lead_id), int(sandbox))
    except Exception:
        pass


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
    # Optional fail-closed consent gate used in regulated deployments. This is
    # checked at queue creation as well as campaign start so MCP/manual/digital
    # ingestion cannot bypass the operator's TRAI consent confirmation.
    try:
        from config import settings
        enforce_consent = bool(settings.orchestration_enforce_consent)
    except Exception:
        enforce_consent = False
    if enforce_consent:
        try:
            from core.state import get_campaign_config
            role = str(lead["role"] or "sales_1")
            if not bool((get_campaign_config(role) or {}).get("consent_confirmed")):
                raise PermissionError("Outbound consent has not been confirmed for this campaign")
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("Outbound consent could not be verified") from exc
    pool = pool_for(job_type, source, attempt)
    # User-requested callbacks are an explicit-time contract. Every other
    # automated touch is constrained to the configured TRAI-safe calling
    # window, including immediately enqueued fresh leads and 09:00 reminders.
    if job_type != JobType.CALLBACK:
        due_at = next_working_time(due_at)
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
    # Plan flowchart: failed call hands off to Sandbox 2 (Retry Engine, P4-P6).
    _set_sandbox(conn, lead_id, 2)
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
        "WHERE lead_id=? AND job_type='callback' AND status IN ('scheduled','ready')",
        (lead_id,),
    )
    conn.execute("UPDATE leads SET lifecycle_status='callback_requested' WHERE id=?", (lead_id,)); conn.commit()
    # Plan flowchart: scheduled callbacks dial back through Sandbox 1 lines
    # (P1/P2 cold, P3 digital).
    _set_sandbox(conn, lead_id, 1)
    return schedule_job(
        conn, lead_id=lead_id, job_type=JobType.CALLBACK, source=source,
        due_at=due_at, key=f"callback:{lead_id}:{int(due_at.timestamp())}", payload={"reason": reason},
    )


def interested(conn, *, lead_id: int, source: str, now: datetime, interest_cycle: str) -> tuple[int, int]:
    conn.execute("UPDATE leads SET lifecycle_status='interested' WHERE id=?", (lead_id,)); conn.commit()
    # Plan flowchart: an Interested lead transitions immediately to Sandbox 3
    # (Nurture & callbacks — P7/P8).
    _set_sandbox(conn, lead_id, 3)
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
    _set_sandbox(conn, lead_id, 3)
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


def reconcile_analyzed_outcome(
    conn: sqlite3.Connection, *, lead_id: int, source: str, analysis: dict,
    interaction_id: str, occurred_at: datetime | None = None,
) -> dict[str, int | str | None]:
    """Translate a completed voice analysis into the autonomous state machine.

    The legacy transcript analyzer remains the source of the disposition. This
    function is the production bridge that makes that outcome create/cancel
    workflow-queue jobs, so changing a dashboard status alone cannot strand a
    lead between sandboxes. Calls are idempotent per ``interaction_id``.
    """
    now = occurred_at or datetime.now(timezone.utc)
    disposition = str(analysis.get("disposition") or "").strip().lower().replace("_", " ")
    next_action = analysis.get("next_action") or {}
    if not isinstance(next_action, dict):
        next_action = {}
    action = str(next_action.get("action_type") or "").strip().lower().replace("_", " ")
    cycle = re.sub(r"[^A-Za-z0-9_.:-]+", "-", interaction_id or f"lead-{lead_id}")[:160]

    facts = {
        "budget": analysis.get("preferred_budget") or analysis.get("budget"),
        "preferred_location": analysis.get("preferred_location"),
        "property_type": analysis.get("property_type") or analysis.get("preferred_unit"),
        "timeline": analysis.get("timeline"),
        "last_disposition": analysis.get("disposition"),
        "callback_requested_at": analysis.get("requested_callback_datetime_iso"),
        "site_visit_datetime_iso": analysis.get("site_visit_datetime_iso"),
    }
    update_memory(conn, lead_id, facts, str(analysis.get("summary") or ""), now)

    def cancel_waiting(reason: str) -> None:
        conn.execute(
            "UPDATE workflow_jobs SET status='cancelled',error=?,updated_at=datetime('now') "
            "WHERE lead_id=? AND status IN ('scheduled','ready')",
            (reason, lead_id),
        )
        conn.commit()

    if disposition in {"opted out", "opt out", "dnc", "do not call"}:
        opt_out(conn, lead_id, "Explicit opt-out in analyzed call", cycle)
        return {"outcome": "opted_out", "job_id": None}

    if disposition in {"not interested", "wrong number"}:
        cancel_waiting(f"terminal call outcome: {disposition}")
        lead = _lead(conn, lead_id)
        conn.execute(
            "INSERT OR IGNORE INTO do_not_contact(normalized_phone,lead_id,reason,source_interaction) "
            "VALUES(?,?,?,?)",
            (_phone(lead["phone"]), lead_id, f"Terminal outcome: {disposition}", cycle),
        )
        conn.execute(
            "UPDATE leads SET lifecycle_status='not_interested',sandbox=0,updated_at=datetime('now') WHERE id=?",
            (lead_id,),
        )
        conn.commit()
        return {"outcome": "not_interested", "job_id": None}

    callback_epoch = analysis.get("callback_reminder_epoch")
    try:
        callback_epoch = float(callback_epoch) if callback_epoch is not None else None
    except (TypeError, ValueError):
        callback_epoch = None
    if callback_epoch and callback_epoch > now.timestamp():
        cancel_waiting("buyer requested callback")
        job_id = schedule_callback(
            conn, lead_id=lead_id, source=source,
            due_at=datetime.fromtimestamp(callback_epoch, timezone.utc),
            reason=str(next_action.get("details") or "Buyer-requested callback"),
        )
        return {"outcome": "callback_requested", "job_id": job_id}

    site_visit = bool(analysis.get("site_visit_agreed")) or action == "site visit" or disposition == "site visit"
    if site_visit:
        cancel_waiting("site visit agreed")
        raw_visit = analysis.get("site_visit_datetime_iso") or next_action.get("datetime_iso")
        visit_at = None
        if raw_visit:
            try:
                visit_at = datetime.fromisoformat(str(raw_visit).replace("Z", "+00:00"))
                if visit_at.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    visit_at = visit_at.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            except (TypeError, ValueError):
                visit_at = None
        if visit_at:
            existing = conn.execute(
                "SELECT id FROM site_visits WHERE lead_id=? AND scheduled_at_utc=? ORDER BY id DESC LIMIT 1",
                (lead_id, visit_at.astimezone(timezone.utc).timestamp()),
            ).fetchone()
            visit_id = int(existing[0]) if existing else schedule_site_visit(
                conn, lead_id=lead_id, source=source, scheduled_at=visit_at,
                preferred_unit=str(facts.get("property_type") or ""),
                budget=str(facts.get("budget") or ""),
                location=str(facts.get("preferred_location") or ""),
                notes=str(analysis.get("summary") or "")[:1000],
            )
            return {"outcome": "site_visit", "job_id": visit_id}
        conn.execute(
            "UPDATE leads SET lifecycle_status='site_visit_scheduled',sandbox=3,updated_at=datetime('now') WHERE id=?",
            (lead_id,),
        )
        conn.commit()
        return {"outcome": "site_visit", "job_id": None}

    if disposition == "interested":
        cancel_waiting("lead entered nurture")
        conn.execute(
            "UPDATE leads SET lifecycle_status='interested',sandbox=3,updated_at=datetime('now') WHERE id=?",
            (lead_id,),
        )
        conn.commit()
        # The existing outcome sender delivers the immediate brochure. The
        # autonomous queue owns the delayed nudge and ensuing Blue Loop call.
        job_id = schedule_job(
            conn, lead_id=lead_id, job_type=JobType.WHATSAPP_FOLLOWUP_24H,
            source=source, due_at=add_working_hours(now, 24),
            key=f"wa-followup:{lead_id}:{cycle}", source_type="interest_cycle",
            source_id=cycle,
        )
        return {"outcome": "interested", "job_id": job_id}

    conn.execute(
        "UPDATE leads SET lifecycle_status='connected',updated_at=datetime('now') "
        "WHERE id=? AND lifecycle_status IN ('new','campaign_calling','failed_retry_waiting')",
        (lead_id,),
    )
    conn.commit()
    return {"outcome": "connected", "job_id": None}
