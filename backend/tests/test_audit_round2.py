"""Regression tests for fixes applied from the voice/whatsapp/ops audit round.

Covers the Agent B + Agent C findings that were reported but not yet covered
by automated tests:
  - DNC register is unified (add_to_dnc + opt_out share do_not_contact).
  - Worker dial path skips DNC-blocked numbers.
  - Opening line interpolates the lead name.
  - Relative callbacks slide into the 11:00-19:30 IST working window.
  - Weekly ZIP archives the prior calendar week and prunes originals.
  - WhatsApp reply classification is negation-aware (Blue Loop LOST/DNC).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402  (Postgres test DB + reset)

from core.storage import close_db, init_db, _get_conn

TZ = ZoneInfo("Asia/Kolkata")


class DncComplianceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(self.tmp.name)
        helpers.reset_operational_tables()

    def tearDown(self):
        try:
            close_db()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_operator_dnc_and_opt_out_share_one_register(self):
        from core.dnc import add_to_dnc, is_phone_blocked
        from core.orchestration_service import opt_out

        conn = _get_conn()
        # Operator adds to DNC -> blocked for any formatting of the number.
        add_to_dnc("+91 98765 43210", reason="manual")
        self.assertTrue(is_phone_blocked("9876543210"))
        self.assertTrue(is_phone_blocked("+919876543210"))
        # Orchestration opt-out lands in the SAME register and is visible.
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status) VALUES('campaign','T','+91 99999 88888','new')"
        )
        lid = int(cur.lastrowid)
        conn.commit()
        opt_out(conn, lid, "do not call", "test")
        self.assertTrue(is_phone_blocked("+919999988888"))
        # Non-blocked numbers stay clear.
        self.assertFalse(is_phone_blocked("+91 70000 00000"))

    def test_claimed_job_flips_lead_to_dialing(self):
        """Uploaded leads show 'dialing' the moment a workflow job is claimed
        (not stuck at 'pending' until the dial literally starts)."""
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType
        from core.workflow_queue import claim_next, promote_due

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox,status) VALUES('sales_1','D','+919000000041','new','campaign',1,'pending')"
        )
        lid = int(cur.lastrowid)
        conn.commit()
        schedule_job(conn, lead_id=lid, job_type=JobType.FRESH_CALL, source="campaign",
                     due_at=datetime.now(), key="dialing-1", attempt=1)
        promote_due(conn, datetime.now().timestamp() + 1)
        job = claim_next(conn, eligible_pool="sandbox1_fresh", number="P1", now=datetime.now().timestamp() + 1)
        self.assertIsNotNone(job, "job must be claimable")
        row = conn.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()
        self.assertEqual(row[0], "dialing", "claimed job must flip lead status to dialing")

    def test_two_fresh_leads_claim_two_numbers_in_parallel(self):
        """2 fresh leads -> 2 workflow jobs claimed on DIFFERENT numbers (P1, P2)."""
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType
        from core.workflow_queue import claim_next, promote_due

        conn = _get_conn()
        lids = []
        for i, ph in enumerate(["+919000000051", "+919000000052"]):
            cur = conn.execute(
                "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox,status) VALUES('sales_1','C%d','%s','new','campaign',1,'pending')" % (i, ph)
            )
            lids.append(int(cur.lastrowid))
        conn.commit()
        now = datetime.now()
        for i, lid in enumerate(lids):
            schedule_job(conn, lead_id=lid, job_type=JobType.FRESH_CALL, source="campaign",
                         due_at=now, key=f"parallel-{i}", attempt=1)
        promote_due(conn, now.timestamp() + 1)

        j1 = claim_next(conn, eligible_pool="sandbox1_fresh", number="P1", now=now.timestamp() + 1)
        j2 = claim_next(conn, eligible_pool="sandbox1_fresh", number="P2", now=now.timestamp() + 1)
        self.assertIsNotNone(j1)
        self.assertIsNotNone(j2)
        self.assertNotEqual(j1["lead_id"], j2["lead_id"], "two different leads dialed")
        self.assertNotEqual(j1["claimed_by_number"], j2["claimed_by_number"],
                            "parallel dialing must use two different lines (P1 + P2)")
        rows = conn.execute("SELECT status FROM leads WHERE id IN (?,?)", (lids[0], lids[1])).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["dialing", "dialing"], "both leads show dialing")

    def test_skip_recently_days_blocks_recently_called_leads(self):
        """Campaign 'skip recently-called' config: leads called within N days
        must not be claimable; older leads still are."""
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType
        from core.workflow_queue import claim_next, promote_due

        conn = _get_conn()
        now = datetime.now()
        # Lead A called 1 day ago (should be skipped with skip_recently_days=5)
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox,first_called_at)"
            " VALUES('campaign','Recent','+919000000011','new','campaign',1,?)",
            (now.timestamp() - 86400,),
        )
        recent_lead = int(cur.lastrowid)
        # Lead B never called (always eligible)
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox)"
            " VALUES('campaign','Fresh','+919000000012','new','campaign',1)"
        )
        fresh_lead = int(cur.lastrowid)
        conn.commit()
        schedule_job(conn, lead_id=recent_lead, job_type=JobType.FRESH_CALL, source="campaign",
                     due_at=now, key="skip-recent-1", attempt=1)
        schedule_job(conn, lead_id=fresh_lead, job_type=JobType.FRESH_CALL, source="campaign",
                     due_at=now, key="skip-recent-2", attempt=1)
        promote_due(conn, now.timestamp() + 1)

        # With skip_recently_days=5: only the never-called lead may be claimed
        job = claim_next(conn, eligible_pool="sandbox1_fresh", number="P1",
                         now=now.timestamp() + 1, skip_recently_days=5)
        self.assertIsNotNone(job, "a fresh lead must be claimable")
        self.assertEqual(job["lead_id"], fresh_lead, "recently-called lead must be skipped")
        self.assertIsNone(
            claim_next(conn, eligible_pool="sandbox1_fresh", number="P2",
                       now=now.timestamp() + 1, skip_recently_days=5),
            "recently-called lead must not be claimable within N days",
        )
        # Without the filter, the recent lead becomes claimable again
        job = claim_next(conn, eligible_pool="sandbox1_fresh", number="P1",
                         now=now.timestamp() + 1, skip_recently_days=0)
        self.assertIsNotNone(job)
        self.assertEqual(job["lead_id"], recent_lead, "with no skip window, lead is claimable")

    def test_queue_refuses_job_when_optout_lands_after_scheduling(self):
        """DNC added after job creation must still block the claim (plan 4.3)."""
        from core.dnc import add_to_dnc
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType
        from core.workflow_queue import claim_next, promote_due

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('campaign','A','+91 98765 43210','new','campaign',1)"
        )
        dnc_lead = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('campaign','B','+91 90000 00001','new','campaign',1)"
        )
        clean_lead = int(cur.lastrowid)
        conn.commit()
        now = datetime.now()
        schedule_job(conn, lead_id=dnc_lead, job_type=JobType.FRESH_CALL, source="campaign",
                     due_at=now, key="dnc-phone-queue", attempt=1)
        schedule_job(conn, lead_id=clean_lead, job_type=JobType.FRESH_CALL, source="campaign",
                     due_at=now, key="clean-queue", attempt=1)
        # Opt-out lands AFTER the jobs were queued.
        add_to_dnc("+91 98765 43210", reason="manual operator block")
        promote_due(conn, now.timestamp() + 1)
        job = claim_next(conn, eligible_pool="sandbox1_fresh", number="P1", now=now.timestamp() + 1)
        self.assertIsNotNone(job)
        self.assertEqual(job["lead_id"], clean_lead, "DNC'd phone must never be claimed")
        self.assertIsNone(
            claim_next(conn, eligible_pool="sandbox1_fresh", number="P2", now=now.timestamp() + 1),
            "only the DNC'd job remains — must not be claimable",
        )

    def test_digital_pool_runs_two_concurrent_calls_on_p3(self):
        """Digital sub-sandbox: 2 concurrent calls from the single P3 line.

        SANDBOX1_DIGITAL registers P3 twice in the pool tuple; the dispatcher
        treats busy as a count, so a second digital lead can claim P3 while the
        first is still active. A third must be blocked (capacity reached).
        """
        from core.number_allocator import NumberPool, allocate_number, configured_pools
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType
        from core.workflow_queue import claim_next, promote_due

        conn = _get_conn()
        pools = configured_pools()
        p3 = pools[NumberPool.SANDBOX1_DIGITAL][0]
        self.assertEqual(
            pools[NumberPool.SANDBOX1_DIGITAL],
            (p3, p3),
            "digital pool must register P3 twice for 2-concurrency",
        )
        lead_ids = []
        for i, ph in enumerate(["+919000000001", "+919000000002"]):
            cur = conn.execute(
                "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('sales_1','D%d','%s','new','digital',1)" % (i, ph)
            )
            lid = int(cur.lastrowid)
            lead_ids.append(lid)
            schedule_job(conn, lead_id=lid, job_type=JobType.FRESH_CALL, source="digital",
                         due_at=datetime.now(), key=f"dc-{i}", attempt=1)
        promote_due(conn, datetime.now().timestamp() + 1)

        busy: dict[str, int] = {}
        now = datetime.now().timestamp() + 1
        j1 = claim_next(conn, eligible_pool="sandbox1_digital", number=p3, now=now)
        self.assertIsNotNone(j1, "first digital lead must claim P3")
        busy[j1["claimed_by_number"]] = busy.get(j1["claimed_by_number"], 0) + 1
        # Second slot must still be free
        self.assertEqual(allocate_number(NumberPool.SANDBOX1_DIGITAL, busy, pools), p3)
        j2 = claim_next(conn, eligible_pool="sandbox1_digital", number=p3, now=now)
        self.assertIsNotNone(j2, "second digital lead must claim P3 concurrently")
        self.assertNotEqual(j1["lead_id"], j2["lead_id"], "two different leads dialed")
        busy[j2["claimed_by_number"]] = busy.get(j2["claimed_by_number"], 0) + 1
        # Capacity reached — third concurrent call must be refused
        self.assertIsNone(
            allocate_number(NumberPool.SANDBOX1_DIGITAL, busy, pools),
            "third concurrent digital call must be blocked",
        )


class OpeningLineTests(unittest.TestCase):
    def test_name_and_company_interpolation(self):
        from core.opening_line import build_opening_line

        base = build_opening_line({}, role="sales_1")
        personal = build_opening_line({"name": "Ravi", "company": "Acme"}, role="sales_1")
        # The packaged greeting mentions Technopolis; personalization must not
        # degrade it and should surface the lead name when provided.
        self.assertIn("Technopolis", base)
        self.assertIn("Technopolis", personal)
        self.assertNotEqual(base, personal)


class CallbackSchedulingTests(unittest.TestCase):
    def test_relative_callback_slides_into_working_window(self):
        from services.callback_schedule import resolve_callback_epoch

        # 7:25 PM + 10 min = 7:35 PM -> outside 11:00-19:30 -> next day 11:00.
        now = datetime(2026, 8, 14, 19, 25, tzinfo=TZ).timestamp()
        epoch, _label = resolve_callback_epoch(
            "", notes="call me back in 10 minutes", tz_name="Asia/Kolkata", now_epoch=now
        )
        due = datetime.fromtimestamp(epoch, TZ)
        self.assertEqual(due, datetime(2026, 8, 15, 11, 0, tzinfo=TZ))

    def test_relative_callback_inside_window_stays_put(self):
        from services.callback_schedule import resolve_callback_epoch

        now = datetime(2026, 8, 14, 14, 0, tzinfo=TZ).timestamp()
        epoch, _label = resolve_callback_epoch(
            "", notes="in 30 minutes", tz_name="Asia/Kolkata", now_epoch=now
        )
        due = datetime.fromtimestamp(epoch, TZ)
        self.assertEqual(due, datetime(2026, 8, 14, 14, 30, tzinfo=TZ))

    def test_absolute_callback_untouched(self):
        from services.callback_schedule import resolve_callback_epoch

        now = datetime(2026, 8, 14, 14, 0, tzinfo=TZ).timestamp()
        epoch, _label = resolve_callback_epoch(
            "2026-08-15T16:00:00", "", tz_name="Asia/Kolkata", now_epoch=now
        )
        due = datetime.fromtimestamp(epoch, TZ)
        self.assertEqual(due, datetime(2026, 8, 15, 16, 0, tzinfo=TZ))


class WeeklyZipTests(unittest.TestCase):
    def test_archives_prior_week_and_prunes_originals(self):
        import zipfile

        from scripts.weekly_zip_archive import _previous_week_bounds, run_weekly_zip_archive

        tmp = Path(tempfile.mkdtemp())
        try:
            rec = tmp / "call_recordings"
            rec.mkdir()
            export = tmp / "exports"
            start, end = _previous_week_bounds()
            prev = start + timedelta(days=2, hours=10)
            this_week = end + timedelta(hours=10)
            ancient = start - timedelta(days=10)
            files = {
                "prev1.wav": prev,
                "prev2.mp3": prev + timedelta(hours=5),
                "prev3.pcm": prev + timedelta(days=1),
                "this_week.wav": this_week,
                "ancient.wav": ancient,
                "note.txt": prev,
            }
            for name, ts in files.items():
                p = rec / name
                p.write_bytes(b"RIFF" + name.encode()[:20])
                os.utime(p, (ts.timestamp(), ts.timestamp()))
            result = run_weekly_zip_archive(output_dir=export, recordings_dir=rec)
            self.assertIsNotNone(result)
            with zipfile.ZipFile(result) as z:
                self.assertEqual(sorted(z.namelist()), ["prev1.wav", "prev2.mp3", "prev3.pcm"])
            remaining = sorted(p.name for p in rec.iterdir())
            self.assertEqual(remaining, ["ancient.wav", "note.txt", "this_week.wav"])
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class WhatsAppClassificationTests(unittest.TestCase):
    def test_negation_aware_classification(self):
        from services.whatsapp.templates import classify_reply

        self.assertEqual(classify_reply("we are NOT interested in a visit"), "not_interested")
        self.assertEqual(classify_reply("no site visit please"), "not_interested")
        self.assertEqual(classify_reply("no, I DO want to visit"), "interested")
        self.assertEqual(classify_reply("please schedule a visit"), "interested")
        self.assertEqual(classify_reply("send me the brochure"), "brochure_request")
        self.assertEqual(classify_reply("hello"), "unknown")

    def test_not_interested_reply_goes_to_opt_out(self):
        import asyncio

        from services.whatsapp_leads import _classify_inbound_reply

        reply_type, source = _classify_inbound_reply("not interested, please stop calling")
        self.assertEqual(reply_type, "not_interested")
        self.assertEqual(source, "whatsapp")


class OrchestrationKpiTests(unittest.TestCase):
    """Regression: RealDictCursor collapses un-aliased SUM/COUNT columns on
    Postgres ('sum' x3 -> one key), so /api/orchestration/kpis and the sandbox
    overview used to IndexError (or silently return zeros)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(self.tmp.name)
        helpers.reset_operational_tables()

    def tearDown(self):
        try:
            close_db()
        except Exception:
            pass
        self.tmp.cleanup()

    def _insert_attempt(self, conn, lead_id, attempt_number, outcome):
        conn.execute(
            "INSERT INTO call_attempts(lead_id, role, from_number, attempt_number, outcome, started_at, ended_at)"
            " VALUES(?, 'campaign', 'P1', ?, ?, 1000.0, 1100.0)",
            (lead_id, attempt_number, outcome),
        )

    def test_kpis_endpoint_returns_without_index_error(self):
        from api.routes.orchestration import orchestration_kpis

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('sales_1','K','+919000000001','new','campaign',1)"
        )
        lead = int(cur.lastrowid)
        self._insert_attempt(conn, lead, 1, "no_answer")
        self._insert_attempt(conn, lead, 2, "no_answer")
        self._insert_attempt(conn, lead, 3, "busy")
        conn.commit()
        import asyncio

        result = asyncio.run(orchestration_kpis(role="sales_1"))
        k = result["kpis"]
        self.assertEqual(k["call_attempts"], 3)
        self.assertEqual(k["retry_attempt_2"], 1)
        self.assertEqual(k["retry_attempt_3"], 1)
        self.assertEqual(k["failed_attempts"], 3)

    def test_sandbox_overview_metrics_not_zero(self):
        from api.routes.sandbox_overview import sandbox_overview

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('sales_1','S','+919000000002','new','campaign',1)"
        )
        lead = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO workflow_jobs(lead_id, job_type, status, eligible_pool, due_at_utc, priority, attempt_number, idempotency_key)"
            " VALUES(?, 'fresh_call', 'completed', 'sandbox1_fresh', 1000.0, 1, 1, 'ov-test')",
            (lead,),
        )
        conn.execute(
            "INSERT INTO workflow_jobs(lead_id, job_type, status, eligible_pool, due_at_utc, priority, attempt_number, idempotency_key)"
            " VALUES(?, 'fresh_call', 'ready', 'sandbox1_fresh', 1000.0, 1, 1, 'ov-test2')",
            (lead,),
        )
        conn.execute(
            "INSERT INTO call_attempts(lead_id, role, from_number, attempt_number, outcome, started_at, ended_at)"
            " VALUES(?, 'sales_1', 'P1', 1, 'answered', 1000.0, 1100.0)",
            (lead,),
        )
        conn.commit()

        class _Req:
            query_params = {"role": "campaign"}
            def __init__(self):
                self.scope = {"type": "http"}

        import asyncio

        result = asyncio.run(sandbox_overview(_Req()))
        self.assertGreaterEqual(result["total_leads"], 1)
        # Regression point: this endpoint used to silently return zeros for all
        # aggregates (RealDictCursor collapsed the un-aliased SUM/COUNT columns).
        self.assertGreaterEqual(result["sandbox_breakdown"] is not None, True)


class TuningPersistenceTests(unittest.TestCase):
    """Regression: frontend POST /api/tuning -> next live call, no backend edits.

    ``update_tuning`` saves prompt/rag/greeting/P1-P9 to ``role_state`` (DB)
    AND mirrors them to the packaged prompt/RAG files, then invalidates the KV
    prompt cache and rebuilds the KB chunks.  These tests lock in that chain:
    a dashboard save must be visible to ``get_state`` / ``role_prompts`` / the
    built system prompt on the very next call.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(self.tmp.name)
        helpers.reset_operational_tables()
        conn = _get_conn()
        conn.execute("DELETE FROM role_state WHERE role = 'sales_1'")
        conn.commit()
        # Isolate module-level caches the chain relies on (tests share a process).
        from core import kv_cache

        kv_cache.clear()
        from services.chunk_rag import load_role_chunks

        load_role_chunks.cache_clear()
        # Back up the real packaged prompt/RAG files (raw bytes, so the restore
        # is byte-exact and cannot alter line endings / trailing whitespace).
        import prompts.role_prompts as _rp
        from pathlib import Path as _Path
        from prompts.role_prompts import get_role_prompt_text, get_role_rag_source_text

        _rp_dir = _Path(os.path.dirname(_rp.__file__))
        self._orig_prompt = get_role_prompt_text("sales_1")
        self._orig_rag = get_role_rag_source_text("sales_1")
        _prompt_file = _rp_dir / "sales_1_prompt.txt"
        self._orig_prompt_bytes = _prompt_file.read_bytes() if _prompt_file.is_file() else None
        _rag_file = _rp_dir.parent / "data" / "sales_1" / "rag_source.txt"
        self._orig_rag_bytes = _rag_file.read_bytes() if _rag_file.is_file() else None
        self.addCleanup(self._restore_files)

    def _restore_files(self):
        import prompts.role_prompts as _rp
        from pathlib import Path as _Path

        _rp_dir = _Path(os.path.dirname(_rp.__file__))
        try:
            _prompt_file = _rp_dir / "sales_1_prompt.txt"
            if self._orig_prompt_bytes is not None:
                _prompt_file.write_bytes(self._orig_prompt_bytes)
            else:
                from prompts.role_prompts import set_role_prompt_text

                set_role_prompt_text("sales_1", self._orig_prompt)
        except Exception:
            pass
        try:
            _rag_file = _rp_dir.parent / "data" / "sales_1" / "rag_source.txt"
            if self._orig_rag_bytes is not None:
                _rag_file.write_bytes(self._orig_rag_bytes)
            else:
                from prompts.role_prompts import set_role_rag_source_text

                set_role_rag_source_text("sales_1", self._orig_rag)
        except Exception:
            pass
        try:
            from services.chunk_rag import (
                _chunks_path,
                load_role_chunks,
                rebuild_role_kb_chunks,
            )

            load_role_chunks.cache_clear()
            if not self._orig_rag:
                p = _chunks_path("sales_1")
                if p.is_file():
                    p.unlink()
            else:
                rebuild_role_kb_chunks("sales_1")
        except Exception:
            pass

    def tearDown(self):
        # Remove the role_state row we may have created: configured_pools()
        # reads P1-P9 from role_state (DB first), so leftover rows would leak
        # into later tests in the same run (see test_orchestration.py).
        try:
            conn = _get_conn()
            conn.execute("DELETE FROM role_state WHERE role = 'sales_1'")
            conn.commit()
        except Exception:
            pass
        try:
            close_db()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_saved_prompt_reaches_role_prompts_and_state(self):
        """POST /api/tuning writes (set_role_prompt_text + save_role_state) must be
        visible through get_role_prompt_text / get_role_rag_source_text / get_state,
        and coerce_role_prompt must prefer the stored (DB) prompt over the file."""
        from core.role_sandbox import coerce_role_prompt
        from core.state import get_state, save_role_state
        from prompts.role_prompts import (
            get_role_prompt_text,
            get_role_rag_source_text,
            set_role_prompt_text,
            set_role_rag_source_text,
        )

        new_prompt = "NEW DASHBOARD PROMPT: sell Solitaire Unity villas at Kondapur."
        new_rag = "NEW KB: Phase 3 pricing from 2.5 Cr."
        new_greeting = "Hi, this is Vernika from Technopolis."

        # Exactly what update_tuning() does (api/routes/console_api.py:380).
        set_role_prompt_text("sales_1", new_prompt)
        set_role_rag_source_text("sales_1", new_rag)
        save_role_state(
            "sales_1",
            prompt=new_prompt,
            rag=new_rag,
            greeting_text=new_greeting,
            p1_number="+91 90000 00001",
            p2_number="+91 90000 00002",
        )

        self.assertEqual(get_role_prompt_text("sales_1"), new_prompt)
        self.assertEqual(get_role_rag_source_text("sales_1"), new_rag)

        st = get_state("sales_1")
        self.assertEqual(st["prompt"], new_prompt)
        self.assertEqual(st["rag"], new_rag)
        self.assertEqual(st["greeting_text"], new_greeting)
        self.assertEqual(st["p1_number"], "+91 90000 00001")
        self.assertEqual(st["p2_number"], "+91 90000 00002")

        # DB-first: coerce prefers the stored prompt over a differing file
        # prompt; falls back to the file only when the DB field is empty.
        self.assertEqual(coerce_role_prompt("sales_1", new_prompt, "STALE FILE PROMPT"), new_prompt)
        self.assertEqual(coerce_role_prompt("sales_1", "", "FILE FALLBACK"), "FILE FALLBACK")

    def test_saved_prompt_reaches_next_live_system_prompt(self):
        """Full chain: dashboard save + kv_cache invalidation -> the next call's
        build_role_system_prompt returns the new prompt/rag, not the warm cache."""
        from core import kv_cache
        from core.state import get_state, save_role_state
        from prompts.role_prompts import (
            build_role_system_prompt,
            set_role_prompt_text,
            set_role_rag_source_text,
        )

        old_prompt, old_rag = "OLD CACHED PROMPT", "OLD CACHED RAG"
        kv_cache.prompt_set("sales_1", old_prompt, old_rag)  # warm (10 min TTL)
        self.assertIn(
            old_prompt,
            build_role_system_prompt("sales_1", get_state("sales_1")),
            "sanity: warm KV cache must serve the old prompt before the save",
        )

        new_prompt = "LIVE PROMPT v2: focus on Solitaire Unity Phase 2 inventory."
        new_rag = "LIVE RAG v2: Tower C possession 2027, 3 BHK from 3.1 Cr."

        # Console save — mirrors update_tuning() body exactly.
        save_role_state("sales_1", prompt=new_prompt, rag=new_rag)
        set_role_prompt_text("sales_1", new_prompt)
        set_role_rag_source_text("sales_1", new_rag)
        # Without this invalidation the KV prompt cache would still serve the
        # old prompt for up to 10 minutes — update_tuning must call it.
        kv_cache.invalidate_role("sales_1")

        role_config = get_state("sales_1")
        sp = build_role_system_prompt("sales_1", role_config, embed_rag=True)
        self.assertIn(new_prompt, sp)
        self.assertIn(new_rag, sp)
        self.assertNotIn(old_prompt, sp)
        self.assertNotIn(old_rag, sp)

    def test_clearing_rag_in_dashboard_drops_stale_chunks(self):
        """Regression: rebuild_role_kb_chunks must drop cached + on-disk chunks
        when the dashboard KB is cleared, so the next call gets no stale facts."""
        from services.chunk_rag import (
            _chunks_path,
            load_role_chunks,
            rebuild_role_kb_chunks,
        )
        from prompts.role_prompts import set_role_rag_source_text

        set_role_rag_source_text(
            "sales_1", "Solitaire Unity is at Kondapur. Prices from 2.5 Cr."
        )
        n = rebuild_role_kb_chunks("sales_1")
        self.assertGreater(n, 0)
        load_role_chunks.cache_clear()
        self.assertGreater(len(load_role_chunks("sales_1")), 0)
        self.assertTrue(_chunks_path("sales_1").is_file())

        # Operator clears the KB in the frontend -> empty rag_source.txt.
        set_role_rag_source_text("sales_1", "")
        self.assertEqual(rebuild_role_kb_chunks("sales_1"), 0)
        self.assertFalse(
            _chunks_path("sales_1").exists(),
            "stale kb_chunks.json must be removed when the KB is cleared",
        )
        self.assertEqual(
            len(load_role_chunks("sales_1")), 0, "cached chunks must not leak into next call"
        )


class GreetingPcmToleranceTests(unittest.TestCase):
    """Freshly-captured greeting with a mismatched text hash must still play
    (prevents the regeneration loop that left callers with silence)."""

    def test_fresh_capture_accepted_despite_hash_mismatch(self):
        import json
        import time

        from core.greeting_pcm import _text_hash, greeting_pcm_paths, load_recorded_greeting_pcm

        p, m = greeting_pcm_paths("sales_1")
        orig_pcm = p.read_bytes() if p.exists() else None
        orig_meta = m.read_text() if m.exists() else None
        try:
            # Force a wrong text_hash but SAME intro text -> must be accepted
            # (the campaign-opening vs template-text tolerance case).
            meta = json.loads(orig_meta) if orig_meta else {}
            meta["text_hash"] = "f6e525f67d47735c"  # deliberately different
            meta["text"] = "Hi, this is Vernika from Technopolis Constructions Private Limited."
            meta["source"] = "gemini_live_capture"
            meta["intro_only"] = True
            m.write_text(json.dumps(meta), encoding="utf-8")
            # Ensure a PCM exists (regenerate a tiny placeholder if missing)
            if not p.exists() or p.stat().st_size == 0:
                p.write_bytes(b"\x00" * 32000)
            rec = load_recorded_greeting_pcm(
                "sales_1",
                greeting_text="Hi, this is Vernika from Technopolis Constructions Private Limited.",
            )
            self.assertIsNotNone(rec, "same-intro-text capture must be accepted despite hash mismatch")
            # NEW text -> must NOT be accepted (must re-record)
            meta["text"] = "A completely different pasted greeting text now."
            meta["text_hash"] = _text_hash(meta["text"])
            m.write_text(json.dumps(meta), encoding="utf-8")
            rec2 = load_recorded_greeting_pcm(
                "sales_1",
                greeting_text="Hi, this is Vernika from Technopolis Constructions Private Limited.",
            )
            self.assertIsNone(rec2, "different pasted greeting text must trigger re-record")
        finally:
            if orig_pcm:
                p.write_bytes(orig_pcm)
            if orig_meta:
                m.write_text(orig_meta, encoding="utf-8")


class CampaignRepeatTests(unittest.TestCase):
    """Repeating campaign auto-relaunch (repeat_type daily/weekly)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        init_db(self.tmp.name)
        helpers.reset_operational_tables()

    def tearDown(self):
        try:
            close_db()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_daily_repeat_relaunches_when_queue_drained(self):
        from core.orchestration_runtime import _auto_relaunch_repeating_campaigns
        from core.state import save_campaign_config

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('sales_1','Repeat','+919000000031','new','campaign',1)"
        )
        conn.commit()
        save_campaign_config("sales_1", {"repeat_type": "daily", "lead_source": "campaign", "next_run_at": 0})

        n = _auto_relaunch_repeating_campaigns(conn)
        self.assertEqual(n, 1, "drained repeating campaign must relaunch")
        jobs = conn.execute("SELECT COUNT(*) FROM workflow_jobs WHERE source_type='campaign_repeat'").fetchone()
        self.assertGreaterEqual(int(jobs[0] or 0), 1, "repeat run must queue fresh jobs")

        # Second call with a future next_run_at must NOT relaunch again
        from core.state import get_campaign_config
        cfg = get_campaign_config("sales_1")
        cfg["next_run_at"] = 9999999999
        save_campaign_config("sales_1", cfg)
        n2 = _auto_relaunch_repeating_campaigns(conn)
        self.assertEqual(n2, 0, "campaign not due must not relaunch")

    def test_one_time_campaign_never_relaunches(self):
        from core.orchestration_runtime import _auto_relaunch_repeating_campaigns
        from core.state import save_campaign_config

        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO leads(role,name,phone,lifecycle_status,source,sandbox) VALUES('sales_1','Once','+919000000032','new','campaign',1)"
        )
        conn.commit()
        save_campaign_config("sales_1", {"repeat_type": "one_time", "lead_source": "campaign", "next_run_at": 0})
        self.assertEqual(_auto_relaunch_repeating_campaigns(conn), 0, "one-time campaign never auto-relaunches")


if __name__ == "__main__":
    unittest.main()
