from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import openpyxl

from core.storage import close_db, init_db
from services.digital_excel_ingest import ingest_digital_file, read_digital_rows


class DigitalExcelIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = init_db(self.temp.name)
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
        conn = sqlite3.connect(self.db_path)
        lead_count = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
        jobs = conn.execute(
            "SELECT eligible_pool,attempt_number FROM workflow_jobs ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(lead_count, 2)
        self.assertEqual(jobs, [("sandbox1_digital", 1), ("sandbox1_digital", 1)])


if __name__ == "__main__":
    unittest.main()
