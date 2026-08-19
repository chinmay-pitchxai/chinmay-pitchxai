import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from api.routes.dashboard import _workflow_analytics


class DashboardSandboxHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY,
                role TEXT,
                sandbox INTEGER,
                status TEXT,
                analysis TEXT
            );
            CREATE TABLE workflow_jobs (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER,
                eligible_pool TEXT,
                claimed_at REAL
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_first_call_stays_in_sandbox_one_charts_after_lead_moves(self):
        claimed_at = time.time()
        self.conn.execute(
            "INSERT INTO leads(id,role,sandbox,status,analysis) VALUES(1,'sales_1',2,'failed',?)",
            (json.dumps({"disposition": "No Answer"}),),
        )
        self.conn.execute(
            "INSERT INTO workflow_jobs(id,lead_id,eligible_pool,claimed_at) VALUES(1,1,'sandbox1_digital',?)",
            (claimed_at,),
        )

        with patch("api.routes.dashboard._get_conn", return_value=self.conn):
            sandbox_one = _workflow_analytics("sales_1", 1)
            sandbox_two = _workflow_analytics("sales_1", 2)

        self.assertEqual(sum(sandbox_one["timeline_total_calls"]), 1)
        self.assertEqual(sum(sandbox_one["hourly_counts"]), 1)
        self.assertEqual(sum(sandbox_two["timeline_total_calls"]), 0)

    def test_unclaimed_and_whatsapp_jobs_are_not_phone_calls(self):
        self.conn.execute(
            "INSERT INTO leads(id,role,sandbox,status,analysis) VALUES(1,'sales_1',3,'interested',?)",
            (json.dumps({"disposition": "Interested"}),),
        )
        self.conn.executemany(
            "INSERT INTO workflow_jobs(id,lead_id,eligible_pool,claimed_at) VALUES(?,?,?,?)",
            [
                (1, 1, "sandbox1_digital", None),
                (2, 1, "whatsapp", time.time()),
            ],
        )

        with patch("api.routes.dashboard._get_conn", return_value=self.conn):
            analytics = _workflow_analytics("sales_1", 0)

        self.assertEqual(analytics["total_calls"], 0)
        self.assertEqual(sum(analytics["hourly_counts"]), 0)

    def test_latest_outcome_is_not_duplicated_across_sandboxes(self):
        now = time.time()
        self.conn.execute(
            "INSERT INTO leads(id,role,sandbox,status,analysis) VALUES(1,'sales_1',3,'completed',?)",
            (json.dumps({"disposition": "Interested"}),),
        )
        self.conn.executemany(
            "INSERT INTO workflow_jobs(id,lead_id,eligible_pool,claimed_at) VALUES(?,?,?,?)",
            [
                (1, 1, "sandbox1_digital", now - 60),
                (2, 1, "sandbox3_nurture", now),
            ],
        )

        with patch("api.routes.dashboard._get_conn", return_value=self.conn):
            sandbox_one = _workflow_analytics("sales_1", 1)
            sandbox_three = _workflow_analytics("sales_1", 3)

        self.assertEqual(sum(sandbox_one["timeline_total_calls"]), 1)
        self.assertEqual(sum(sandbox_one["timeline_interested"]), 0)
        self.assertEqual(sum(sandbox_three["timeline_interested"]), 1)
        self.assertEqual(sum(sandbox_three["hourly_interested"]), 1)


if __name__ == "__main__":
    unittest.main()
