from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402  (Postgres test DB + reset)

from core.storage import close_db, init_db
from services.digital_excel_ingest import ingest_digital_file, ingest_digital_rows, read_digital_rows


class DigitalExcelIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = init_db(self.temp.name)
        helpers.reset_operational_tables()
        self.path = Path(self.temp.name) / "digital-leads.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Leads"
        sheet.append(["Customer Name", "Mobile Number", "Email", "Project", "Budget"])
        sheet.append(["Asha", 9876543210, "asha@example.com", "Solitaire Unity", "1.8Cr"])
        sheet.append(["Duplicate Asha", 9876543210, "duplicate@example.com", "Solitaire Unity", "2Cr"])
        sheet.append(["Ravi", "+91 99887 76655", "ravi@example.com", "Solitaire Unity", "2.2Cr"])
        workbook.save(self.path)

    def tearDown(self):
        close_db()
        self.temp.cleanup()

    def test_rows_are_normalized_as_sandbox_1_2_digital(self):
        rows = read_digital_rows(self.path, "Leads")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["source"] == "digital" for row in rows))
        self.assertTrue(all(row["sandbox"] == 1 for row in rows))
        self.assertEqual(rows[0]["phone"], "+919876543210")
        self.assertEqual(rows[0]["extra"]["Budget"], "1.8Cr")

    def test_ingest_is_idempotent_and_all_jobs_route_to_p3_pool(self):
        first = ingest_digital_file(self.path, role="sales_1", sheet_name="Leads")
        second = ingest_digital_file(self.path, role="sales_1", sheet_name="Leads")
        self.assertEqual(first["saved"], 2)
        self.assertEqual(second["saved"], 0)
        conn = helpers.connect()
        try:
            lead_count = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
            jobs = conn.execute(
                "SELECT eligible_pool,attempt_number FROM workflow_jobs ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(lead_count, 2)
        self.assertEqual([tuple(j) for j in jobs], [("sandbox1_digital", 1), ("sandbox1_digital", 1)])

    def test_broker_rows_queue_only_new_phones_in_sandbox_1_2(self):
        rows = [
            {"name": "Asha", "phone": "9876543210", "row_id": "11:2"},
            {"name": "Asha duplicate", "phone": "9876543210", "row_id": "11:3"},
            {"name": "Bad", "phone": "", "row_id": "11:4"},
        ]
        first = ingest_digital_rows(rows, broker_id="broker_1")
        second = ingest_digital_rows(rows, broker_id="broker_1")

        self.assertEqual(first["saved"], 1)
        self.assertEqual(first["queued"], 1)
        self.assertEqual(first["duplicates"], 1)
        self.assertEqual(len(first["rejected"]), 1)
        self.assertEqual(
            [result["status"] for result in first["results"]],
            ["queued", "duplicate", "rejected"],
        )
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["duplicates"], 2)

        conn = helpers.connect()
        try:
            job = conn.execute(
                "SELECT eligible_pool,attempt_number,source_type,payload_json "
                "FROM workflow_jobs"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(job["eligible_pool"], "sandbox1_digital")
        self.assertEqual(job["attempt_number"], 1)
        self.assertEqual(job["source_type"], "google_sheets")
        self.assertIn('"sub_sandbox": "1.2"', job["payload_json"])


if __name__ == "__main__":
    unittest.main()
