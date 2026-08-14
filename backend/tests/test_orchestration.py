from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.business_hours import add_working_hours
from core.number_allocator import allocate_number, configured_pools, pool_for, relationship_number_for_source, validate_live_pools
from core.workflow_models import JobType, LeadStage, NumberPool, can_transition
from core.workflow_queue import claim_next, complete_job, create_job, promote_due
from core.orchestration_dispatcher import dispatch_once
from core.storage import init_db
from core.orchestration_service import (
    complete_site_visit, failed_call, feedback_no_answer, interested, opt_out,
    record_feedback, relationship_no_answer, reschedule_site_visit, schedule_callback,
    schedule_no_reply_call, schedule_site_visit,
    update_memory,
)


SCHEMA = """
CREATE TABLE leads(id INTEGER PRIMARY KEY, phone TEXT, role TEXT DEFAULT 'campaign', source TEXT DEFAULT 'campaign');
CREATE TABLE do_not_contact(normalized_phone TEXT PRIMARY KEY, lead_id INTEGER);
CREATE TABLE workflow_jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, job_type TEXT NOT NULL,
 source_type TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'scheduled', due_at_utc REAL NOT NULL, eligible_pool TEXT NOT NULL,
 attempt_number INTEGER NOT NULL DEFAULT 0 CHECK(attempt_number BETWEEN 0 AND 3),
 claimed_by_number TEXT, claim_token TEXT, claimed_at REAL, lease_expires_at REAL,
 idempotency_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL DEFAULT '{}', error TEXT DEFAULT '',
 created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);
"""


class StateTests(unittest.TestCase):
    def test_allowed_and_terminal_transitions(self):
        self.assertTrue(can_transition(LeadStage.NEW, LeadStage.CAMPAIGN_CALLING))
        self.assertTrue(can_transition(LeadStage.CONNECTED, LeadStage.INTERESTED))
        self.assertFalse(can_transition(LeadStage.BOOKED, LeadStage.FOLLOW_UP))
        self.assertFalse(can_transition(LeadStage.NEW, LeadStage.BOOKED))


class NumberTests(unittest.TestCase):
    def test_exact_ownership(self):
        self.assertEqual(pool_for(JobType.FRESH_CALL, "campaign"), NumberPool.SANDBOX1_FRESH)
        self.assertEqual(pool_for(JobType.FRESH_CALL, "digital_marketing"), NumberPool.SANDBOX1_DIGITAL)
        self.assertEqual(pool_for(JobType.FAILED_RETRY, "campaign", 2), NumberPool.SANDBOX2_RETRY_2)
        self.assertEqual(pool_for(JobType.FAILED_RETRY, "campaign", 3), NumberPool.SANDBOX2_RETRY_3_COLD)
        self.assertEqual(pool_for(JobType.FAILED_RETRY, "digital_marketing", 2), NumberPool.SANDBOX2_RETRY_2)
        self.assertEqual(pool_for(JobType.FAILED_RETRY, "digital_marketing", 3), NumberPool.SANDBOX2_RETRY_3_DIGITAL)
        self.assertEqual(pool_for(JobType.CALLBACK, "campaign"), NumberPool.SANDBOX1_CALLBACK)
        self.assertEqual(pool_for(JobType.INTERESTED_FOLLOWUP, "campaign"), NumberPool.SANDBOX3_NURTURE)
        self.assertEqual(pool_for(JobType.POST_VISIT_FEEDBACK, "campaign"), NumberPool.SANDBOX4_FEEDBACK)

    def test_relationship_number_follows_lead_sandbox(self):
        pools = {
            NumberPool.SANDBOX1_FRESH: (10,), NumberPool.SANDBOX1_DIGITAL: (20,),
            NumberPool.SANDBOX1_CALLBACK: (10, 20), NumberPool.SANDBOX2_RETRY_2: (10,),
            NumberPool.SANDBOX2_RETRY_3_COLD: (10,), NumberPool.SANDBOX2_RETRY_3_DIGITAL: (20,),
            NumberPool.SANDBOX3_NURTURE: (10, 20), NumberPool.SANDBOX4_FEEDBACK: (10,),
            NumberPool.WHATSAPP: (),
        }
        self.assertEqual(relationship_number_for_source("campaign", pools), 10)
        self.assertEqual(relationship_number_for_source("digital_marketing", pools), 20)

    def test_full_nine_line_config_is_valid(self):
        class Full:
            pass
        f = Full()
        for i, num in enumerate(("+91A", "+91B", "+91C", "+91D", "+91E", "+91F", "+91G", "+91H", "+91I"), 1):
            setattr(f, f"p{i}_number", num)
        pools = configured_pools(f)
        self.assertEqual(validate_live_pools(pools), [])

    def test_missing_digital_sandbox_is_fail_closed(self):
        class JustCold:
            pass
        j = JustCold()
        for i, num in enumerate(("+91A", "+91B", "", "+91D", "+91E", "+91F", "+91G", "+91H", "+91I"), 1):
            setattr(j, f"p{i}_number", num)
        errors = validate_live_pools(configured_pools(j))
        self.assertTrue(any("digital" in e.lower() for e in errors))

    def test_same_number_in_both_sandboxes_rejected(self):
        class Same:
            pass
        s = Same()
        for i, num in enumerate(("+91A", "+91B", "+91A", "+91D", "+91E", "+91F", "+91G", "+91H", "+91I"), 1):
            setattr(s, f"p{i}_number", num)
        errors = validate_live_pools(configured_pools(s))
        self.assertTrue(any("different" in e.lower() for e in errors))

    def test_live_config_is_fail_closed(self):
        class Empty: pass
        pools = configured_pools(Empty())
        self.assertTrue(validate_live_pools(pools))

    def test_attempt_four_rejected(self):
        with self.assertRaises(ValueError):
            pool_for(JobType.FAILED_RETRY, "campaign", 4)


class WorkingHoursTests(unittest.TestCase):
    def test_twelve_hours_crosses_two_days(self):
        tz = ZoneInfo("Asia/Kolkata")
        start = datetime(2026, 8, 3, 18, 30, tzinfo=tz)
        self.assertEqual(add_working_hours(start, 12), datetime(2026, 8, 5, 13, 30, tzinfo=tz))

    def test_twenty_four_hours(self):
        tz = ZoneInfo("Asia/Kolkata")
        start = datetime(2026, 8, 3, 11, 0, tzinfo=tz)
        self.assertEqual(add_working_hours(start, 24), datetime(2026, 8, 5, 18, 0, tzinfo=tz))

    def test_after_hours_starts_next_day(self):
        tz = ZoneInfo("Asia/Kolkata")
        start = datetime(2026, 8, 3, 21, 0, tzinfo=tz)
        self.assertEqual(add_working_hours(start, 1), datetime(2026, 8, 4, 12, 0, tzinfo=tz))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "q.db"
        conn = sqlite3.connect(self.path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO leads(id,phone) VALUES(1,'9999999999')")
        conn.commit(); conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        return sqlite3.connect(self.path, timeout=5)

    def test_idempotent_create_and_complete(self):
        c = self.conn()
        try:
            a = create_job(c, lead_id=1, job_type="callback", priority=1, due_at_utc=1,
                           eligible_pool="sandbox1_callback", idempotency_key="same")
            b = create_job(c, lead_id=1, job_type="callback", priority=1, due_at_utc=1,
                           eligible_pool="sandbox1_callback", idempotency_key="same")
            self.assertEqual(a, b)
            self.assertEqual(c.execute("SELECT count(*) FROM workflow_jobs").fetchone()[0], 1)
            promote_due(c, 2)
            job = claim_next(c, eligible_pool="sandbox1_callback", number="P1", now=2)
            self.assertIsNotNone(job)
            self.assertTrue(complete_job(c, job["id"], job["claim_token"]))
            self.assertIsNone(claim_next(c, eligible_pool="sandbox1_callback", number="P2", now=2))
        finally:
            c.close()

    def test_two_workers_one_winner(self):
        c = self.conn()
        create_job(c, lead_id=1, job_type="post_visit_feedback", priority=5, due_at_utc=1,
                   eligible_pool="sandbox4_feedback", idempotency_key="feedback:visit:1")
        promote_due(c, 2); c.close()
        results = []
        barrier = threading.Barrier(2)
        def worker(number):
            conn = self.conn(); barrier.wait()
            try: results.append(claim_next(conn, eligible_pool="sandbox4_feedback", number=number, now=2))
            finally: conn.close()
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("P9", "P9")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_dispatcher_priority_and_number_connection(self):
        import asyncio
        c = self.conn()
        calls = []
        async def phone(job, number): calls.append((job["job_type"], number))
        async def wa(job, number): calls.append((job["job_type"], number))
        try:
            create_job(c, lead_id=1, job_type="fresh_call", priority=6, due_at_utc=1,
                       eligible_pool="sandbox1_fresh", idempotency_key="fresh")
            create_job(c, lead_id=1, job_type="callback", priority=1, due_at_utc=1,
                       eligible_pool="sandbox1_callback", idempotency_key="callback")
            pools = {
                NumberPool.SANDBOX1_FRESH: ("COLD",), NumberPool.SANDBOX1_DIGITAL: ("DIG",),
                NumberPool.SANDBOX1_CALLBACK: ("COLD", "DIG"), NumberPool.SANDBOX2_RETRY_2: ("COLD",),
                NumberPool.SANDBOX2_RETRY_3_COLD: ("COLD",), NumberPool.SANDBOX2_RETRY_3_DIGITAL: ("DIG",),
                NumberPool.SANDBOX3_NURTURE: ("COLD", "DIG"), NumberPool.SANDBOX4_FEEDBACK: ("COLD",),
                NumberPool.WHATSAPP: (),
            }
            asyncio.run(dispatch_once(c, pools=pools, busy_numbers=set(), phone_executor=phone, whatsapp_executor=wa, now=2))
            # Callback wins on priority and must ring from the cold DID (lead role=campaign).
            self.assertEqual(calls, [("callback", "COLD")])
            self.assertEqual(c.execute("SELECT status FROM workflow_jobs WHERE idempotency_key='callback'").fetchone()[0], "completed")
        finally:
            c.close()

    def test_callback_uses_lead_sandbox_number(self):
        import asyncio
        c = self.conn()
        # Production digital leads carry role=sales_1 but source=digital — the
        # dispatcher must use the source column so callbacks ring from P3.
        c.execute("INSERT INTO leads(id,phone,role,source) VALUES(2,'8888888888','sales_1','digital')")
        c.commit()
        calls = []
        async def phone(job, number): calls.append((job["job_type"], number))
        async def wa(job, number): calls.append((job["job_type"], number))
        try:
            create_job(c, lead_id=2, job_type="callback", priority=1, due_at_utc=1,
                       eligible_pool="sandbox1_callback", idempotency_key="cb:digital")
            pools = {
                NumberPool.SANDBOX1_FRESH: ("COLD",), NumberPool.SANDBOX1_DIGITAL: ("DIG",),
                NumberPool.SANDBOX1_CALLBACK: ("COLD", "DIG"), NumberPool.SANDBOX2_RETRY_2: ("COLD",),
                NumberPool.SANDBOX2_RETRY_3_COLD: ("COLD",), NumberPool.SANDBOX2_RETRY_3_DIGITAL: ("DIG",),
                NumberPool.SANDBOX3_NURTURE: ("COLD", "DIG"), NumberPool.SANDBOX4_FEEDBACK: ("COLD",),
                NumberPool.WHATSAPP: (),
            }
            asyncio.run(dispatch_once(c, pools=pools, busy_numbers=set(), phone_executor=phone, whatsapp_executor=wa, now=2))
            self.assertEqual(calls, [("callback", "DIG")])
        finally:
            c.close()

    def test_dnc_lead_never_dialed(self):
        import asyncio
        c = self.conn()
        c.execute("INSERT INTO do_not_contact(normalized_phone,lead_id) VALUES('9999999999',1)")
        c.commit()
        calls = []
        async def phone(job, number): calls.append((job["job_type"], number))
        async def wa(job, number): calls.append((job["job_type"], number))
        try:
            create_job(c, lead_id=1, job_type="callback", priority=1, due_at_utc=1,
                       eligible_pool="sandbox1_callback", idempotency_key="cb:dnc")
            pools = {
                NumberPool.SANDBOX1_FRESH: ("COLD",), NumberPool.SANDBOX1_DIGITAL: ("DIG",),
                NumberPool.SANDBOX1_CALLBACK: ("COLD", "DIG"), NumberPool.SANDBOX2_RETRY_2: ("COLD",),
                NumberPool.SANDBOX2_RETRY_3_COLD: ("COLD",), NumberPool.SANDBOX2_RETRY_3_DIGITAL: ("DIG",),
                NumberPool.SANDBOX3_NURTURE: ("COLD", "DIG"), NumberPool.SANDBOX4_FEEDBACK: ("COLD",),
                NumberPool.WHATSAPP: (),
            }
            asyncio.run(dispatch_once(c, pools=pools, busy_numbers=set(), phone_executor=phone, whatsapp_executor=wa, now=2))
            self.assertEqual(calls, [])
            self.assertEqual(c.execute("SELECT status FROM workflow_jobs WHERE idempotency_key='cb:dnc'").fetchone()[0], "ready")
        finally:
            c.close()


class EndToEndFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = init_db(self.tmp.name)
        self.c = sqlite3.connect(self.db)
        self.c.row_factory = sqlite3.Row
        cur = self.c.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status) VALUES('campaign','Test Lead','+91 98765-43210','new')"
        )
        self.lead = int(cur.lastrowid); self.c.commit()
        self.tz = ZoneInfo("Asia/Kolkata")

    def tearDown(self):
        try:
            from core.storage import close_db

            close_db()
        except Exception:
            pass
        self.c.close()
        self.tmp.cleanup()

    def jobs(self, job_type=None):
        q = "SELECT * FROM workflow_jobs WHERE lead_id=?"
        args = [self.lead]
        if job_type:
            q += " AND job_type=?"; args.append(job_type)
        return self.c.execute(q, args).fetchall()

    def test_three_attempt_campaign_loop(self):
        t = datetime(2026, 8, 3, 18, 30, tzinfo=self.tz)
        j2 = failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                         attempt=1, from_number="P2", outcome="no_answer", ended_at=t)
        row2 = self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (j2,)).fetchone()
        self.assertEqual((row2["attempt_number"], row2["eligible_pool"]), (2, "sandbox2_retry_2"))
        j3 = failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                         attempt=2, from_number="P4", outcome="busy", ended_at=t)
        row3 = self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (j3,)).fetchone()
        self.assertEqual((row3["attempt_number"], row3["eligible_pool"]), (3, "sandbox2_retry_3_cold"))
        self.assertIsNone(failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                                      attempt=3, from_number="P5", outcome="no_answer", ended_at=t))
        self.assertEqual(self.c.execute("SELECT lifecycle_status FROM leads WHERE id=?", (self.lead,)).fetchone()[0], "lost")
        self.assertEqual(self.c.execute("SELECT count(*) FROM call_attempts WHERE lead_id=?", (self.lead,)).fetchone()[0], 3)
        with self.assertRaises(ValueError):
            failed_call(self.c, lead_id=self.lead, source="campaign", retry_cycle="r1",
                        attempt=4, from_number="P5", outcome="no_answer", ended_at=t)

    def test_digital_retry_uses_attempt_pools(self):
        t = datetime(2026, 8, 3, 12, 0, tzinfo=self.tz)
        for attempt, number, expected in ((1, "P3", "sandbox2_retry_2"), (2, "P4", "sandbox2_retry_3_digital")):
            job = failed_call(self.c, lead_id=self.lead, source="digital_marketing", retry_cycle="d1",
                              attempt=attempt, from_number=number, outcome="failed", ended_at=t)
            row = self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (job,)).fetchone()
            self.assertEqual(row["eligible_pool"], expected)

    def test_interested_visit_feedback_booked(self):
        now = datetime(2026, 8, 3, 11, 0, tzinfo=self.tz)
        package, wa24 = interested(self.c, lead_id=self.lead, source="campaign", now=now, interest_cycle="i1")
        self.assertNotEqual(package, wa24)
        wa = self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (wa24,)).fetchone()
        due = datetime.fromtimestamp(wa["due_at_utc"], timezone.utc).astimezone(self.tz)
        self.assertEqual(due, datetime(2026, 8, 5, 18, 0, tzinfo=self.tz))
        visit_at = datetime(2026, 8, 9, 14, 0, tzinfo=self.tz)
        visit = schedule_site_visit(self.c, lead_id=self.lead, source="campaign", scheduled_at=visit_at,
                                    family_members="2", preferred_unit="3BHK", budget="1.2Cr")
        reminders = self.c.execute("SELECT count(*) FROM workflow_jobs WHERE source_type='site_visit' AND source_id=?", (str(visit),)).fetchone()[0]
        self.assertEqual(reminders, 2)
        feedback_job = complete_site_visit(self.c, visit_id=visit, source="campaign", completed_at=visit_at)
        feedback = self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (feedback_job,)).fetchone()
        self.assertEqual(feedback["eligible_pool"], "sandbox4_feedback")
        record_feedback(self.c, visit_id=visit, job_id=feedback_job, outcome="booked")
        self.assertEqual(self.c.execute("SELECT lifecycle_status FROM leads WHERE id=?", (self.lead,)).fetchone()[0], "booked")
        self.assertEqual(self.c.execute("SELECT count(*) FROM feedback_records WHERE site_visit_id=?", (visit,)).fetchone()[0], 1)

    def test_callback_is_sandbox1_and_optout_cancels_all(self):
        due = datetime(2026, 8, 4, 16, 0, tzinfo=self.tz)
        job = schedule_callback(self.c, lead_id=self.lead, source="campaign", due_at=due, reason="driving")
        self.assertEqual(self.c.execute("SELECT eligible_pool FROM workflow_jobs WHERE id=?", (job,)).fetchone()[0], "sandbox1_callback")
        opt_out(self.c, self.lead, "do not call", "test")
        self.assertEqual(self.c.execute("SELECT lifecycle_status FROM leads WHERE id=?", (self.lead,)).fetchone()[0], "opted_out")
        self.assertEqual(self.c.execute("SELECT count(*) FROM workflow_jobs WHERE lead_id=? AND status!='cancelled'", (self.lead,)).fetchone()[0], 0)
        with self.assertRaises(PermissionError):
            schedule_callback(self.c, lead_id=self.lead, source="campaign", due_at=due, reason="again")

    def test_relationship_no_answer_is_bounded_and_stays_sandbox1(self):
        due = datetime(2026, 8, 4, 16, 0, tzinfo=self.tz)
        job_id = schedule_callback(self.c, lead_id=self.lead, source="campaign", due_at=due, reason="busy")
        job = dict(self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (job_id,)).fetchone())
        retry = relationship_no_answer(self.c, job=job, source="campaign", ended_at=due)
        retry_row = dict(self.c.execute("SELECT * FROM workflow_jobs WHERE id=?", (retry,)).fetchone())
        self.assertEqual((retry_row["eligible_pool"], retry_row["attempt_number"]), ("sandbox1_callback", 2))
        self.assertIsNone(relationship_no_answer(self.c, job=retry_row, source="campaign", ended_at=due))

    def test_whatsapp_no_reply_call_routes_to_nurture(self):
        sent = datetime(2026, 8, 4, 11, 0, tzinfo=self.tz)
        job = schedule_no_reply_call(
            self.c, lead_id=self.lead, source="campaign", sent_at=sent, interest_cycle="wa1"
        )
        row = self.c.execute("SELECT eligible_pool,job_type FROM workflow_jobs WHERE id=?", (job,)).fetchone()
        self.assertEqual(tuple(row), ("sandbox3_nurture", "interested_followup"))
        due = self.c.execute("SELECT due_at_utc FROM workflow_jobs WHERE id=?", (job,)).fetchone()[0]
        self.assertEqual(
            datetime.fromtimestamp(due, timezone.utc).astimezone(self.tz),
            datetime(2026, 8, 4, 14, 0, tzinfo=self.tz),
        )

    def test_visit_reschedule_memory_and_bounded_feedback_retry(self):
        at = datetime(2026, 8, 9, 14, 0, tzinfo=self.tz)
        visit = schedule_site_visit(self.c, lead_id=self.lead, source="campaign", scheduled_at=at)
        reschedule_site_visit(self.c, visit_id=visit, source="campaign", scheduled_at=at + timedelta(days=2))
        active = self.c.execute(
            "SELECT count(*) FROM workflow_jobs WHERE source_type='site_visit' AND source_id=? AND status='scheduled'",
            (str(visit),),
        ).fetchone()[0]
        cancelled = self.c.execute(
            "SELECT count(*) FROM workflow_jobs WHERE source_type='site_visit' AND source_id=? AND status='cancelled'",
            (str(visit),),
        ).fetchone()[0]
        self.assertEqual((active, cancelled), (2, 2))
        self.assertEqual(update_memory(self.c, self.lead, {"budget": "1.2Cr", "area": "Whitefield"}), 1)
        self.assertEqual(update_memory(self.c, self.lead, {"budget": "", "unit": "3BHK"}), 2)
        facts = json.loads(self.c.execute("SELECT facts_json FROM lead_memory WHERE lead_id=?", (self.lead,)).fetchone()[0])
        self.assertEqual(facts, {"budget": "1.2Cr", "area": "Whitefield", "unit": "3BHK"})
        retry = feedback_no_answer(self.c, visit_id=visit, source="campaign", attempt=1, ended_at=at)
        row = self.c.execute("SELECT eligible_pool,attempt_number FROM workflow_jobs WHERE id=?", (retry,)).fetchone()
        self.assertEqual(tuple(row), ("sandbox4_feedback", 2))
        self.assertIsNone(feedback_no_answer(self.c, visit_id=visit, source="campaign", attempt=2, ended_at=at))


if __name__ == "__main__":
    unittest.main()
