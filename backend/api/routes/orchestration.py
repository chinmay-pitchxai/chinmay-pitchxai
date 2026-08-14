"""Read-only autonomous orchestration health and queue summary."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query

from core.orchestration_runtime import runtime_status
from core.number_allocator import DEFAULT_POOLS
from core.workflow_models import NumberPool
from core.storage import _get_conn
from config import settings

router = APIRouter(prefix="/api/orchestration", tags=["orchestration"])

ROLE_ALIASES = {"sales_1": "sales_1"}
NUMBER_PURPOSE = {
    "P1": "Cold Calling fresh calls", "P2": "Cold Calling fresh calls",
    "P3": "Digital Leads fresh calls",
    "P4": "Retry - attempt 2 (all sources)",
    "P5": "Retry - attempt 3 (cold leads)",
    "P6": "Retry - attempt 3 (digital leads)",
    "P7": "Nurture & callbacks (interested leads)", "P8": "Nurture & callbacks (interested leads)",
    "P9": "Post-visit feedback calls",
}
NUMBER_ROLE = {f"P{i}": "sales_1" for i in range(1, 10)}
POOL_LOGICAL = {pool.value: list(numbers) for pool, numbers in DEFAULT_POOLS.items()}


def _role(value: str) -> str:
    return "sales_1"


def _number_map() -> dict[str, str]:
    return {f"P{i}": str(getattr(settings, f"p{i}_number", "") or "").strip() for i in range(1, 10)}


def _logical_for(real_number: str | None) -> str:
    if not real_number:
        return ""
    for logical, configured in _number_map().items():
        if configured and configured == real_number:
            return logical
    if str(real_number).upper() in NUMBER_PURPOSE:
        return str(real_number).upper()
    return "Unmapped"


def _routing_reason(job_type: str, attempt: int, logical: str) -> str:
    reasons = {
        "fresh_call": "Fresh lead routed to the operation's fresh-call pool",
        "failed_retry": f"Failed call routed to retry attempt {attempt} pool",
        "callback": "Customer callback routed to the lead's originating sandbox pool",
        "interested_followup": "Interested-lead follow-up routed to Sandbox 3 nurture pool (P7/P8)",
        "post_visit_feedback": "Post-visit feedback routed to Sandbox 4 pool (P9)",
        "site_visit_reminder_day_before": "Site-visit reminder routed to Sandbox 3 nurture pool (P7/P8)",
        "site_visit_reminder_morning": "Site-visit reminder routed to Sandbox 3 nurture pool (P7/P8)",
        "site_visit_reschedule": "Site-visit reschedule routed to Sandbox 3 nurture pool (P7/P8)",
        "whatsapp_followup_24h": "WhatsApp follow-up scheduled after 24 working hours",
        "whatsapp_package": "Immediate WhatsApp information package",
    }
    base = reasons.get(job_type, "Workflow routing rule")
    return f"{base} -> {logical or 'number pending'}"


@router.get("/status")
async def orchestration_status():
    conn = _get_conn()
    queue = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT status,COUNT(*) FROM workflow_jobs GROUP BY status ORDER BY status"
        ).fetchall()
    }
    by_type = {
        row[0]: row[1]
        for row in conn.execute(
            """SELECT job_type,COUNT(*) FROM workflow_jobs
            WHERE status IN ('scheduled','ready','claimed','running')
            GROUP BY job_type ORDER BY job_type"""
        ).fetchall()
    }
    return {**runtime_status(), "queue": queue, "active_by_type": by_type}


@router.get("/numbers")
async def orchestration_numbers(role: str = Query("campaign")):
    role = _role(role)
    conn = _get_conn()
    configured = _number_map()
    allowed = [i for i in range(1, 10) if role in str(NUMBER_ROLE.get(f"P{i}", "")).split(",")]
    rows = []
    for i in allowed:
        logical = f"P{i}"
        actual = configured[logical]
        metrics = conn.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN lower(outcome) IN ('answered','completed','connected','interested') THEN 1 ELSE 0 END),
                      SUM(CASE WHEN lower(outcome) IN ('failed','no_answer','busy','error','call_failed') THEN 1 ELSE 0 END),
                      MAX(COALESCE(ended_at,started_at))
               FROM call_attempts WHERE from_number IN (?,?)""",
            (actual or "__not_configured__", logical),
        ).fetchone()
        busy = conn.execute(
            "SELECT COUNT(*) FROM workflow_jobs WHERE claimed_by_number IN (?,?) AND status IN ('claimed','running')",
            (actual or "__not_configured__", logical),
        ).fetchone()[0]
        rows.append({
            "logical_number": logical, "phone_number": actual, "configured": bool(actual),
            "purpose": NUMBER_PURPOSE[logical], "status": "Busy" if busy else ("Ready" if actual else "Not configured"),
            "calls": int(metrics[0] or 0), "answered": int(metrics[1] or 0), "failed": int(metrics[2] or 0),
            "last_used_at": metrics[3], "role": role,
        })
    return {"role": role, "as_of": time.time(), "numbers": rows}


@router.get("/kpis")
async def orchestration_kpis(role: str = Query("campaign")):
    role = _role(role)
    conn = _get_conn()
    jobs = {row[0]: int(row[1]) for row in conn.execute(
        """SELECT job_type,COUNT(*) FROM workflow_jobs w JOIN leads l ON l.id=w.lead_id
           WHERE l.role=? GROUP BY job_type""", (role,)).fetchall()}
    states = {row[0]: int(row[1]) for row in conn.execute(
        """SELECT w.status,COUNT(*) FROM workflow_jobs w JOIN leads l ON l.id=w.lead_id
           WHERE l.role=? GROUP BY w.status""", (role,)).fetchall()}
    attempts = conn.execute(
        """SELECT COUNT(*), SUM(CASE WHEN a.attempt_number=2 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN a.attempt_number=3 THEN 1 ELSE 0 END),
                  SUM(CASE WHEN lower(a.outcome) IN ('failed','no_answer','busy','error','call_failed') THEN 1 ELSE 0 END)
           FROM call_attempts a JOIN leads l ON l.id=a.lead_id WHERE l.role=?""", (role,)).fetchone()
    total_leads = conn.execute("SELECT COUNT(*) FROM leads WHERE role=?", (role,)).fetchone()[0]
    return {"role": role, "as_of": time.time(), "kpis": {
        "total_leads": total_leads, "call_attempts": int(attempts[0] or 0),
        "retry_attempt_2": int(attempts[1] or 0), "retry_attempt_3": int(attempts[2] or 0),
        "failed_attempts": int(attempts[3] or 0), "callbacks": jobs.get("callback", 0),
        "feedback_calls": jobs.get("post_visit_feedback", 0),
        "whatsapp_packages": jobs.get("whatsapp_package", 0),
        "whatsapp_followups_24h": jobs.get("whatsapp_followup_24h", 0),
        "queue_due": states.get("ready", 0) + states.get("scheduled", 0),
        "queue_errors": states.get("failed", 0),
    }}


@router.get("/calls")
async def orchestration_calls(
    role: str = Query("campaign"), p_number: str = Query(""), job_type: str = Query(""),
    outcome: str = Query(""), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
):
    role = _role(role)
    conn = _get_conn()
    where, args = ["l.role=?"], [role]
    configured = _number_map()
    if p_number:
        logical = p_number.upper()
        if logical not in NUMBER_PURPOSE:
            raise HTTPException(400, "Unknown P-number")
        where.append("a.from_number IN (?,?)")
        args.extend([logical, configured.get(logical) or "__not_configured__"])
    if job_type:
        where.append("COALESCE(w.job_type,'fresh_call')=?"); args.append(job_type)
    if outcome:
        where.append("lower(a.outcome)=?"); args.append(outcome.lower())
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM call_attempts a JOIN leads l ON l.id=a.lead_id LEFT JOIN workflow_jobs w ON w.id=a.job_id WHERE {clause}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT a.id,a.lead_id,l.name,l.phone,l.company,l.segment,l.source_file,
                   a.attempt_number,a.from_number,a.outcome,a.started_at,a.ended_at,a.call_id,
                   COALESCE(w.job_type,'fresh_call'),COALESCE(w.source_type,''),COALESCE(w.source_id,''),
                   COALESCE(w.status,''),COALESCE(w.due_at_utc,0),COALESCE(w.error,''),COALESCE(w.payload_json,'{{}}')
            FROM call_attempts a JOIN leads l ON l.id=a.lead_id LEFT JOIN workflow_jobs w ON w.id=a.job_id
            WHERE {clause} ORDER BY COALESCE(a.ended_at,a.started_at,0) DESC LIMIT ? OFFSET ?""",
        [*args, page_size, (page - 1) * page_size],
    ).fetchall()
    items = []
    for row in rows:
        logical = _logical_for(row[8])
        try: payload = json.loads(row[19] or "{}")
        except Exception: payload = {}
        items.append({
            "id": row[0], "lead_id": row[1], "lead_name": row[2], "lead_phone": row[3],
            "source": row[4] or row[14] or "Campaign", "campaign": row[6] or row[5] or row[15] or "",
            "attempt_number": row[7], "logical_number": logical, "from_number": row[8], "outcome": row[9],
            "started_at": row[10], "ended_at": row[11], "call_id": row[12], "job_type": row[13],
            "queue_status": row[16], "next_action_at": row[17], "error": row[18],
            "routing_reason": _routing_reason(row[13], row[7], logical), "whatsapp": payload.get("whatsapp", {}),
        })
    return {"role": role, "page": page, "page_size": page_size, "total": total, "items": items}


@router.get("/campaign-routing")
async def campaign_routing(role: str = Query("campaign"), source_file: str = Query("")):
    role = _role(role)
    numbers = await orchestration_numbers(role)
    pools = (
        ["sandbox1_fresh", "sandbox1_digital", "sandbox1_callback",
         "sandbox2_retry_2", "sandbox2_retry_3_cold", "sandbox2_retry_3_digital",
         "sandbox3_nurture", "sandbox4_feedback"]
        if role == "campaign"
        else ["sandbox1_digital", "sandbox2_retry_2", "sandbox2_retry_3_digital", "sandbox3_nurture", "sandbox4_feedback"]
    )
    return {"role": role, "source_file": source_file, "numbers": numbers["numbers"],
            "routing": [{"pool": p, "logical_numbers": POOL_LOGICAL[p]} for p in pools]}
