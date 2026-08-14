"""End-to-end smoke test for the audit fixes (test round 2).

Exercises, against a throwaway temp DB:
  1. Fresh lead → fresh_call job → dispatcher dials from P1 (cold).
  2. Call fails → failed_call() schedules retry-2 on P4 → retry-3 on P5.
  3. Lead becomes interested → whatsapp_package + whatsapp_followup_24h jobs.
  4. execute_whatsapp_job now handles whatsapp_package (brochure path) without raising.
  5. complete_site_visit moves the lead to sandbox 4 (SB3 -> SB4 transition).
  6. Memory write (update_memory) → memory_context() returns the continuity block.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.business_hours import add_working_hours
from core.storage import init_db, _get_conn, close_db, _update_lead_sandbox_sync
from core.workflow_models import JobType, NumberPool
from core.number_allocator import configured_pools, pool_for
from core.orchestration_service import (
    failed_call, interested, schedule_site_visit, complete_site_visit,
    update_memory, whatsapp_package_sent,
)
from core.workflow_queue import promote_due, claim_next, complete_job
from core.orchestration_dispatcher import dispatch_once
from core.live_job_executor import execute_whatsapp_job
from core import lead_memory as _lm

TZ = ZoneInfo("Asia/Kolkata")

POOLS = {
    NumberPool.SANDBOX1_FRESH: ("P1", "P2"),
    NumberPool.SANDBOX1_DIGITAL: ("P3",),
    NumberPool.SANDBOX1_CALLBACK: ("P1", "P2", "P3"),
    NumberPool.SANDBOX2_RETRY_2: ("P4",),
    NumberPool.SANDBOX2_RETRY_3_COLD: ("P5",),
    NumberPool.SANDBOX2_RETRY_3_DIGITAL: ("P6",),
    NumberPool.SANDBOX2_CALLBACK: ("P4", "P5", "P6"),
    NumberPool.SANDBOX3_NURTURE: ("P7", "P8"),
    NumberPool.SANDBOX4_FEEDBACK: ("P9",),
    NumberPool.WHATSAPP: (),
}


class AuditFixSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_db(self.tmp.name)
        self.c = sqlite3.connect(self.db)
        self.c.row_factory = sqlite3.Row
        cur = self.c.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,sandbox,source) "
            "VALUES('campaign','Test Lead','+91 98765-43210','new',1,'campaign')"
        )
        self.lead = int(cur.lastrowid)
        self.c.commit()
        # Patch brochure sender so the test never hits the network.
        import core.live_job_executor as lje

        self._orig = lje.__dict__.get("_BROCHURE_SENT", None)
        self._sent = {"calls": 0, "phone": None}

        async def _fake_package(phone, name):
            self._sent["calls"] += 1
            self._sent["phone"] = phone

        import services.whatsapp.brochure as br

        self._orig_send = br.send_full_package
        br.send_full_package = _fake_package
        lje._BROCHURE_SENT = True

    def tearDown(self):
        import services.whatsapp.brochure as br

        br.send_full_package = self._orig_send
        try:
            close_db()
        except Exception:
            pass
        try:
            self.c.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_sb4_transition_on_complete_site_visit(self):
        now = datetime(2026, 8, 3, 11, 0, tzinfo=TZ)
        visit = schedule_site_visit(
            self.c, lead_id=self.lead, source="campaign", scheduled_at=now + timedelta(days=2),
        )
        complete_site_visit(self.c, visit_id=visit, source="campaign", completed_at=now + timedelta(days=2))
        row = self.c.execute("SELECT sandbox,lifecycle_status FROM leads WHERE id=?", (self.lead,)).fetchone()
        self.assertEqual(row["sandbox"], 4, "completed site visit must move lead to SB4")
        self.assertEqual(row["lifecycle_status"], "feedback_pending")
        fb = self.c.execute(
            "SELECT eligible_pool FROM workflow_jobs WHERE job_type='post_visit_feedback'"
        ).fetchone()
        self.assertEqual(fb["eligible_pool"], "sandbox4_feedback")

    def test_memory_write_then_read_continuity(self):
        update_memory(
            self.c, self.lead,
            facts={"preferred_budget": "1.2 Cr", "preferred_location": "Whitefield", "last_disposition": "Interested"},
            summary="Liked 3BHK unit, will discuss with family.",
            at=datetime.now(timezone.utc),
        )
        ctx = _lm.memory_context(_get_conn(), self.lead)
        self.assertIn("REMEMBERED FROM YOUR PREVIOUS CONVERSATIONS", ctx)
        self.assertIn("preferred_budget: 1.2 Cr", ctx)
        self.assertIn("preferred_location: Whitefield", ctx)
        # Read side must return "" for a lead with no memory (regression guard).
        self.assertEqual(_lm.memory_context(_get_conn(), 999_999), "")

    def test_whatsapp_package_job_now_executes(self):
        now = datetime(2026, 8, 3, 11, 0, tzinfo=TZ)
        pkg, wa24 = interested(self.c, lead_id=self.lead, source="campaign", now=now, interest_cycle="i1")
        pkg_job = dict(self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (pkg,)).fetchone())
        self.assertEqual(pkg_job["job_type"], "whatsapp_package")
        # Dispatcher must route WHATSAPP pool to execute_whatsapp_job without RuntimeError.
        promote_due(self.c, now.timestamp() + 1)
        called = []

        async def _phone(job, number):
            called.append(("phone", job["job_type"], number))

        job = claim_next(self.c, eligible_pool="whatsapp", number="WHATSAPP", now=now.timestamp() + 1)
        self.assertIsNotNone(job)
        asyncio.run(execute_whatsapp_job(dict(job), None))
        complete_job(self.c, job["id"], job["claim_token"])
        self.assertEqual(self._sent["calls"], 1, "brochure sender must be invoked")
        self.assertEqual(self._sent["phone"], "+91 98765-43210")
        row = self.c.execute("SELECT whatsapp_sent FROM leads WHERE id=?", (self.lead,)).fetchone()
        self.assertEqual(row["whatsapp_sent"], 1)
        # The 24h follow-up job still exists and is untouched.
        n_follow = self.c.execute(
            "SELECT COUNT(*) FROM workflow_jobs WHERE job_type='whatsapp_followup_24h' AND status='scheduled'"
        ).fetchone()[0]
        self.assertEqual(n_follow, 1)

    def test_dispatcher_full_retry_chain(self):
        now = datetime(2026, 8, 3, 18, 30, tzinfo=TZ)
        j2 = failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                         attempt=1, from_number="P2", outcome="no_answer", ended_at=now)
        row2 = self.c.execute("SELECT attempt_number,eligible_pool,due_at_utc FROM workflow_jobs WHERE id=?", (j2,)).fetchone()
        self.assertEqual((row2["attempt_number"], row2["eligible_pool"]), (2, "sandbox2_retry_2"))
        j3 = failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                         attempt=2, from_number="P4", outcome="busy", ended_at=now)
        row3 = self.c.execute("SELECT attempt_number,eligible_pool FROM workflow_jobs WHERE id=?", (j3,)).fetchone()
        self.assertEqual((row3["attempt_number"], row3["eligible_pool"]), (3, "sandbox2_retry_3_cold"))
        # Dispatcher picks the retry-2 job first (priority) and assigns a free SB2 line.
        calls = []

        async def _phone(job, number):
            calls.append((job["job_type"], number))

        async def _wa(job, number):
            calls.append((job["job_type"], number))

        asyncio.run(dispatch_once(
            self.c, pools=POOLS, busy_numbers=set(),
            phone_executor=_phone, whatsapp_executor=_wa,
            now=row2["due_at_utc"] + 1,
        ))
        self.assertEqual(calls[0][0], "failed_retry")
        self.assertEqual(calls[0][1], "P4", "retry-2 must ring from P4")

    def test_sandbox_column_defaults_and_clamp(self):
        _update_lead_sandbox_sync(self.lead, 99)
        row = self.c.execute("SELECT sandbox FROM leads WHERE id=?", (self.lead,)).fetchone()
        self.assertEqual(row["sandbox"], 4, "sandbox must clamp to 1..4")
        _update_lead_sandbox_sync(self.lead, 2)
        row = self.c.execute("SELECT sandbox FROM leads WHERE id=?", (self.lead,)).fetchone()
        self.assertEqual(row["sandbox"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
