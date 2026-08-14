"""Adapters from workflow jobs to the existing call and WhatsApp engines."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from core.storage import _get_conn


async def execute_phone_job(job: dict, number: str | None) -> None:
    if not number:
        raise RuntimeError("Phone workflow job has no eligible DID")
    conn = _get_conn()
    row = conn.execute("SELECT * FROM leads WHERE id=?", (job["lead_id"],)).fetchone()
    if not row:
        raise RuntimeError(f"Lead {job['lead_id']} no longer exists")
    lead = dict(row)
    role = str(lead.get("role") or "campaign")
    from core.worker import _process_single_lead
    result = await _process_single_lead(
        role, lead, number, external_managed=True, orchestration_job=job,
    )
    if not (result or {}).get("answered") and job["job_type"] not in ("fresh_call", "failed_retry"):
        from core.orchestration_service import relationship_no_answer
        relationship_no_answer(
            conn, job=job, source=role, ended_at=datetime.now(timezone.utc),
        )


async def execute_whatsapp_job(job: dict, _number: str | None) -> None:
    conn = _get_conn()
    row = conn.execute("SELECT name,phone FROM leads WHERE id=?", (job["lead_id"],)).fetchone()
    if not row:
        raise RuntimeError(f"Lead {job['lead_id']} no longer exists")
    name = (row["name"] or "there").strip()
    if job["job_type"] == "whatsapp_package":
        # Immediate brochure package — plan Phase 6: WhatsApp Immediate Brochure.
        from services.whatsapp.brochure import send_full_package

        try:
            await send_full_package(row["phone"], name)
        except Exception as exc:
            raise RuntimeError(f"WhatsApp package send failed: {exc}") from exc
        try:
            conn.execute(
                "UPDATE leads SET whatsapp_sent=1,whatsapp_sent_at=? WHERE id=?",
                (time.time(), job["lead_id"]),
            )
            conn.commit()
        except Exception:
            pass
        return
    if job["job_type"] != "whatsapp_followup_24h":
        raise RuntimeError(f"Unsupported WhatsApp job type: {job['job_type']}")
    from services.whatsapp.client import send_text
    text = (
        f"Hello {name},\n\nJust checking whether you had a chance to review "
        "the project details shared earlier. Please let us know if you would "
        "like more information or would like to schedule a site visit."
    )
    result = await send_text(row["phone"], text)
    if result.get("error"):
        raise RuntimeError(f"WhatsApp follow-up failed: {result['error']}")
    from core.orchestration_service import schedule_no_reply_call
    schedule_no_reply_call(
        conn, lead_id=int(job["lead_id"]),
        source=str(conn.execute("SELECT role FROM leads WHERE id=?", (job["lead_id"],)).fetchone()[0] or "campaign"),
        sent_at=datetime.now(timezone.utc), interest_cycle=str(job.get("source_id") or job["id"]),
    )
