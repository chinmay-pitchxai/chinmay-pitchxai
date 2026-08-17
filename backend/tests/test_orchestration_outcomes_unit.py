from __future__ import annotations

import sqlite3
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.orchestration_service import reconcile_analyzed_outcome, schedule_job
from core.workflow_models import JobType


SCHEMA = """
CREATE TABLE leads (
  id INTEGER PRIMARY KEY, phone TEXT, role TEXT DEFAULT 'sales_1',
  source TEXT DEFAULT 'campaign', lifecycle_status TEXT DEFAULT 'new',
  sandbox INTEGER DEFAULT 1, orchestration_version INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE workflow_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, job_type TEXT,
  source_type TEXT, source_id TEXT, priority INTEGER, status TEXT,
  due_at_utc REAL, eligible_pool TEXT, attempt_number INTEGER,
  idempotency_key TEXT UNIQUE, payload_json TEXT, error TEXT,
  updated_at TEXT, claim_token TEXT, claimed_by_number TEXT,
  claimed_at REAL, lease_expires_at REAL, worker_id TEXT
);
CREATE TABLE lead_memory (
  lead_id INTEGER PRIMARY KEY, facts_json TEXT DEFAULT '{}', summary TEXT DEFAULT '',
  last_interaction_at REAL, version INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE site_visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, scheduled_at_utc REAL,
  family_members TEXT, preferred_unit TEXT, budget TEXT, location TEXT, notes TEXT,
  status TEXT DEFAULT 'scheduled', version INTEGER DEFAULT 1, updated_at TEXT
);
CREATE TABLE do_not_contact (
  normalized_phone TEXT PRIMARY KEY, lead_id INTEGER, reason TEXT,
  source_interaction TEXT, created_at TEXT
);
"""


class OutcomeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO leads(id,phone,role,source) VALUES(1,'+919999999999','sales_1','campaign')"
        )
        self.conn.commit()
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()

    def test_interested_enters_sandbox_three_and_schedules_one_nudge(self):
        result = reconcile_analyzed_outcome(
            self.conn, lead_id=1, source="campaign",
            analysis={"disposition": "Interested", "summary": "Budget is 1.5 Cr", "preferred_budget": "1.5 Cr"},
            interaction_id="call-1", occurred_at=self.now,
        )
        self.assertEqual(result["outcome"], "interested")
        lead = self.conn.execute("SELECT lifecycle_status,sandbox FROM leads WHERE id=1").fetchone()
        self.assertEqual(tuple(lead), ("interested", 3))
        jobs = self.conn.execute("SELECT job_type,eligible_pool FROM workflow_jobs").fetchall()
        self.assertEqual([tuple(row) for row in jobs], [("whatsapp_followup_24h", "whatsapp")])
        # Same transcript cannot create a duplicate nudge.
        reconcile_analyzed_outcome(
            self.conn, lead_id=1, source="campaign",
            analysis={"disposition": "Interested"}, interaction_id="call-1", occurred_at=self.now,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0], 1)

    def test_callback_routes_back_to_sandbox_one(self):
        due = self.now + timedelta(hours=2)
        result = reconcile_analyzed_outcome(
            self.conn, lead_id=1, source="campaign",
            analysis={"disposition": "Callback", "callback_reminder_epoch": due.timestamp()},
            interaction_id="call-2", occurred_at=self.now,
        )
        self.assertEqual(result["outcome"], "callback_requested")
        lead = self.conn.execute("SELECT lifecycle_status FROM leads WHERE id=1").fetchone()[0]
        job = self.conn.execute("SELECT job_type,eligible_pool,due_at_utc FROM workflow_jobs").fetchone()
        self.assertEqual(lead, "callback_requested")
        self.assertEqual((job[0], job[1]), ("callback", "sandbox1_callback"))
        self.assertEqual(job[2], due.timestamp())

    def test_not_interested_is_terminal_and_dnc(self):
        reconcile_analyzed_outcome(
            self.conn, lead_id=1, source="campaign",
            analysis={"disposition": "Not Interested"},
            interaction_id="call-3", occurred_at=self.now,
        )
        lead = self.conn.execute("SELECT lifecycle_status,sandbox FROM leads WHERE id=1").fetchone()
        self.assertEqual(tuple(lead), ("not_interested", 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM do_not_contact").fetchone()[0], 1)

    def test_consent_enforcement_cannot_be_bypassed_at_queue_creation(self):
        from config import settings

        with patch.object(settings, "orchestration_enforce_consent", True), patch(
            "core.state.get_campaign_config", return_value={"consent_confirmed": False}
        ):
            with self.assertRaises(PermissionError):
                schedule_job(
                    self.conn, lead_id=1, job_type=JobType.FRESH_CALL,
                    source="campaign", due_at=self.now, key="consent-blocked", attempt=1,
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM workflow_jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
