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
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402  (Postgres test DB + reset)

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
        helpers.reset_operational_tables()
        self.c = helpers.connect()
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

    def test_priority_has_every_job_type_including_reschedule(self):
        """Regression: SITE_VISIT_RESCHEDULE was missing from PRIORITY, which
        raised KeyError the moment such a job was scheduled."""
        from core.orchestration_service import PRIORITY, schedule_job
        from core.workflow_models import JobType

        for jt in JobType:
            self.assertIn(jt, PRIORITY, f"PRIORITY missing {jt.value}")
        job_id = schedule_job(
            self.c, lead_id=self.lead, job_type=JobType.SITE_VISIT_RESCHEDULE,
            source="campaign", due_at=datetime.now(timezone.utc) + timedelta(hours=1),
            key="sv-resched:1", source_type="site_visit", source_id="1",
        )
        row = self.c.execute(
            "SELECT job_type,priority,eligible_pool FROM workflow_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self.assertEqual((row["job_type"], row["priority"], row["eligible_pool"]),
                         ("site_visit_reschedule", 4, "sandbox3_nurture"))

    def test_interest_moves_lead_to_sandbox3(self):
        """Plan flowchart: Interested → Sandbox 3 (nurture, P7/P8)."""
        now = datetime(2026, 8, 3, 11, 0, tzinfo=TZ)
        interested(self.c, lead_id=self.lead, source="campaign", now=now, interest_cycle="i-sb3")
        row = self.c.execute(
            "SELECT sandbox,lifecycle_status FROM leads WHERE id=?", (self.lead,)
        ).fetchone()
        self.assertEqual((row["sandbox"], row["lifecycle_status"]), (3, "interested"))
        # Follow-up jobs land in the SB3 nurture pool.
        fu = self.c.execute(
            "SELECT eligible_pool FROM workflow_jobs WHERE job_type='whatsapp_followup_24h'"
        ).fetchone()
        self.assertEqual(fu["eligible_pool"], "whatsapp")

    def test_failed_call_moves_lead_to_sandbox2(self):
        """Plan flowchart: failed call → Sandbox 2 (retry engine, P4-P6)."""
        t = datetime(2026, 8, 3, 18, 30, tzinfo=TZ)
        failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r-sb2",
                    attempt=1, from_number="P2", outcome="no_answer", ended_at=t)
        row = self.c.execute(
            "SELECT sandbox,lifecycle_status FROM leads WHERE id=?", (self.lead,)
        ).fetchone()
        self.assertEqual((row["sandbox"], row["lifecycle_status"]), (2, "failed_retry_waiting"))

    def test_callback_stage_maps_back_to_sandbox1(self):
        """Plan flowchart: scheduled callbacks dial back through Sandbox 1 lines."""
        from core.workflow_models import LeadStage, sandbox_for_stage

        self.assertEqual(sandbox_for_stage(LeadStage.CALLBACK_REQUESTED), 1)
        due = datetime(2026, 8, 4, 16, 0, tzinfo=TZ)
        from core.orchestration_service import schedule_callback

        schedule_callback(self.c, lead_id=self.lead, source="campaign", due_at=due, reason="busy")
        row = self.c.execute("SELECT sandbox,lifecycle_status FROM leads WHERE id=?", (self.lead,)).fetchone()
        self.assertEqual((row["sandbox"], row["lifecycle_status"]), (1, "callback_requested"))

    def test_mcp_create_lead_enqueues_fresh_call_and_status_survives(self):
        """Plan §1.2: create_lead injects into the SB1 queue (fresh_call job on
        the source-appropriate pool) and get_lead_status returns a sandbox
        without crashing on the JSON-string ``extra`` column."""
        import asyncio
        from mcp_server import call_mcp_tool

        res = asyncio.run(call_mcp_tool(
            "create_lead",
            {
                "phone_number": "+91 99999-88888",
                "name": "MCP Lead",
                "source": "digital",
                "budget": "1.5 Cr",
                "preferred_location": "Sarjapur",
                "property_type": "3BHK",
            },
        ))
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["job_enqueued"], "fresh_call")
        self.assertEqual(res["source"], "digital")
        lead_id = res["lead_id"]
        row = self.c.execute(
            "SELECT source,sandbox,extra FROM leads WHERE id=?", (lead_id,)
        ).fetchone()
        self.assertEqual((row["source"], row["sandbox"]), ("digital", 1))
        import json as _json

        extra = _json.loads(row["extra"])
        self.assertEqual(extra.get("budget"), "1.5 Cr")
        self.assertEqual(extra.get("preferred_location"), "Sarjapur")
        job = self.c.execute(
            "SELECT job_type,eligible_pool FROM workflow_jobs WHERE lead_id=?", (lead_id,)
        ).fetchone()
        self.assertEqual((job["job_type"], job["eligible_pool"]), ("fresh_call", "sandbox1_digital"))
        # get_lead_status must not 500 on the string extra column.
        st = asyncio.run(call_mcp_tool("get_lead_status", {"phone_number": "+91 99999-88888"}))
        self.assertEqual(st["status"], "found")
        self.assertEqual(st["current_sandbox"], 1)
        self.assertEqual(st["lifecycle_status"], "new")

    def test_mcp_manual_call_respects_dnc(self):
        """trigger_manual_call aborts for a number on the DNC register."""
        import asyncio
        from mcp_server import call_mcp_tool

        self.c.execute("INSERT INTO do_not_contact(normalized_phone,lead_id) VALUES('9999988888',?)",
                       (self.lead,))
        self.c.commit()
        res = asyncio.run(call_mcp_tool(
            "trigger_manual_call", {"phone_number": "+91 99999-88888", "role": "sales_1"}
        ))
        self.assertEqual(res["status"], "blocked")
        # No job may have been created for the opted-out lead.
        n = self.c.execute(
            "SELECT COUNT(*) FROM workflow_jobs WHERE lead_id=?", (self.lead,)
        ).fetchone()[0]
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
