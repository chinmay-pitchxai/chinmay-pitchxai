from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.business_hours import is_within_working_hours, next_working_time
from core.number_allocator import DEFAULT_POOLS
from core.sandbox_catalog import load_sandbox_catalog, sandbox_for_job
from core.workflow_models import JobType, NumberPool


class SandboxCatalogTests(unittest.TestCase):
    def test_catalog_has_four_sandboxes_and_all_nine_lines(self):
        catalog = load_sandbox_catalog()
        self.assertEqual([entry["sandbox"] for entry in catalog], [1, 2, 3, 4])
        lines = [line for entry in catalog for line in entry["phone_lines"]]
        self.assertEqual(lines, [f"P{i}" for i in range(1, 10)])

    def test_every_job_type_has_exactly_one_sandbox_owner(self):
        owners = {
            job_type.value: sandbox_for_job(job_type.value)["sandbox"]
            for job_type in JobType
        }
        self.assertEqual(owners["fresh_call"], 1)
        self.assertEqual(owners["callback"], 1)
        self.assertEqual(owners["failed_retry"], 2)
        self.assertEqual(owners["interested_followup"], 3)
        self.assertEqual(owners["post_visit_feedback"], 4)

    def test_catalog_line_ownership_matches_allocator(self):
        catalog_lines = {
            entry["sandbox"]: set(entry["phone_lines"])
            for entry in load_sandbox_catalog()
        }
        self.assertEqual(set(DEFAULT_POOLS[NumberPool.SANDBOX1_FRESH]), {"P1", "P2"})
        self.assertEqual(set(DEFAULT_POOLS[NumberPool.SANDBOX1_DIGITAL]), {"P3"})
        self.assertEqual(
            set(DEFAULT_POOLS[NumberPool.SANDBOX2_RETRY_2])
            | set(DEFAULT_POOLS[NumberPool.SANDBOX2_RETRY_3_COLD])
            | set(DEFAULT_POOLS[NumberPool.SANDBOX2_RETRY_3_DIGITAL]),
            catalog_lines[2],
        )
        self.assertEqual(set(DEFAULT_POOLS[NumberPool.SANDBOX3_NURTURE]), catalog_lines[3])
        self.assertEqual(set(DEFAULT_POOLS[NumberPool.SANDBOX4_FEEDBACK]), catalog_lines[4])


class WorkingWindowTests(unittest.TestCase):
    def test_automated_job_before_open_slides_to_open(self):
        original = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
        shifted = next_working_time(original)
        self.assertEqual((shifted.hour, shifted.minute), (11, 0))
        self.assertTrue(is_within_working_hours(shifted))

    def test_automated_job_after_close_slides_to_next_day(self):
        original = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)  # 20:30 IST
        shifted = next_working_time(original)
        self.assertEqual(shifted.date().isoformat(), "2026-08-18")
        self.assertEqual((shifted.hour, shifted.minute), (11, 0))


if __name__ == "__main__":
    unittest.main()
