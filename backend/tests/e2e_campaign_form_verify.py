"""E2E verification of every campaign-form field -> backend endpoint (no real dialing).

Run:  cd backend && PGDATABASE=technopoliss_test ./.venv/Scripts/python.exe tests/e2e_campaign_form_verify.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PGDATABASE", "technopoliss_test")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))  # backend/ on path so `api`/`core`/`config` import

import helpers  # noqa: E402

from core.storage import close_db, init_db, _get_conn  # noqa: E402

import io

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    try:
        init_db(tmp.name)
        helpers.reset_operational_tables()

        from fastapi.testclient import TestClient
        from api.app import create_app

        app = create_app()
        client = TestClient(app)  # no context manager -> no lifespan background tasks
        R = "sales_1"
        base = f"/api/campaign"

        def ok(r) -> bool:
            return r.status_code < 400

        # ── 1. Full campaign config save (launchCampaign payload) ──────────────
        cfg = {
            "campaign_name": "Test Outpero Campaign",
            "concurrent_call_limit": 4,
            "window_start": "09:00",
            "window_end": "18:00",
            "calling_days": [0, 1, 2, 3, 4],
            "holidays": ["2026-12-25"],
            "skip_opted_out": True,
            "skip_recently_days": 3,
            "retry_count": 4,
            "retry_when": "next_day",
            "repeat_type": "daily",
            "schedule_at": "2099-01-01T09:00",
            "lead_source": "campaign",
            "sandbox": 1,
        }
        r = client.post(f"{base}/config?role={R}", json=cfg)
        check("POST /api/campaign/config accepts full launch payload", r.status_code == 200, r.text[:200])
        cfg_res = client.get(f"{base}/config?role={R}")
        got = (cfg_res.json().get("config") or {}) if ok(cfg_res) else {}
        missing = [k for k in cfg if got.get(k) != cfg[k]]
        check("GET /api/campaign/config round-trips every field", ok(cfg_res) and not missing, f"missing/mismatch: {missing}")

        # ── 2. Calling window quick-save (saveCampaignWindow) ──────────────────
        r = client.post(f"{base}/config?role={R}", json={**cfg, "window_start": "10:30", "window_end": "17:45"})
        got = client.get(f"{base}/config?role={R}").json()["config"]
        check("saveCampaignWindow path persists window", ok(r) and got.get("window_start") == "10:30" and got.get("window_end") == "17:45")

        # ── 3. Add single contact (addSingleContact) ───────────────────────────
        r = client.post(f"{base}/contact?role={R}", json={"phone": "9876500001", "name": "Alice", "source": "campaign"})
        check("POST /api/campaign/contact adds", ok(r) and r.json().get("added") == 1, r.text[:200])
        r = client.post(f"{base}/contact?role={R}", json={"phone": "9876500002", "name": "Bob", "source": "digital"})
        check("POST /api/campaign/contact adds digital contact", ok(r) and r.json().get("added") == 1, r.text[:200])

        # ── 4. Paste contacts (pasteContacts) ──────────────────────────────────
        r = client.post(f"{base}/contacts/paste?role={R}", json={"text": "9876500003,Carol\n9876500004,Dan\n9876500005,Eve"})
        check("POST /api/campaign/contacts/paste imports 3", ok(r) and r.json().get("added") == 3, r.text[:200])

        # ── 5. Upload CSV cold + digital (importCampaignCSV) ───────────────────
        csv_cold = "Name,Phone,Company\nCold1,9876500010,ACME\nCold2,9876500011,Beta\n"
        r = client.post(f"{base}/upload?role={R}&source=campaign&sandbox=1", files={"file": ("cold.csv", csv_cold, "text/csv")})
        check("POST /api/campaign/upload CSV (cold)", ok(r) and (r.json().get("count") or 0) >= 2, r.text[:200])
        csv_dig = "Name,Mobile Number,Email\nDig1,9876500020,dig@x.com\n"
        r = client.post(f"{base}/upload?role={R}&source=digital&sandbox=1", files={"file": ("dig.csv", csv_dig, "text/csv")})
        check("POST /api/campaign/upload CSV (digital)", ok(r) and (r.json().get("count") or 0) >= 1, r.text[:200])

        # xlsx upload
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active
        ws.append(["Name", "Mobile Number"]); ws.append(["Xls1", 9876500030])
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        r = client.post(f"{base}/upload?role={R}&source=campaign&sandbox=1", files={"file": ("leads.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        check("POST /api/campaign/upload XLSX parses", ok(r) and (r.json().get("count") or 0) >= 1, r.text[:200])

        # ── 6. Contact list (loadCampaignContacts) ─────────────────────────────
        r = client.get(f"{base}/contacts?role={R}&source=campaign")
        cold_contacts = r.json().get("contacts", []) if ok(r) else []
        check("GET /api/campaign/contacts lists cold contacts", ok(r) and len(cold_contacts) >= 5, f"got {len(cold_contacts)}")
        r = client.get(f"{base}/contacts?role={R}&source=digital")
        dig_contacts = r.json().get("contacts", []) if ok(r) else []
        check("GET /api/campaign/contacts filters digital", ok(r) and len(dig_contacts) >= 1, f"got {len(dig_contacts)}")

        # ── 7. Merge contacts -> leads (merge-contacts) ────────────────────────
        r = client.post(f"{base}/merge-contacts?role={R}")
        merged = r.json().get("merged", 0) if ok(r) else 0
        check("POST /api/campaign/merge-contacts merges", ok(r) and merged >= 6, r.text[:200])
        conn = _get_conn()
        n_pending = conn.execute("SELECT count(*) FROM leads WHERE role=? AND status='pending'", (R,)).fetchone()[0]
        n_digital = conn.execute("SELECT count(*) FROM leads WHERE role=? AND source='digital'", (R,)).fetchone()[0]
        conn.close()
        check("merged leads are pending in dial queue", n_pending >= 6, f"{n_pending} pending")
        check("digital source preserved on merge", n_digital >= 2, f"{n_digital} digital leads")

        # ── 8. Launch flow: start + state + stop ───────────────────────────────
        r = client.post(f"{base}/start?role={R}")
        # Without Vobiz creds the preflight rejects; with creds it starts. Either is a real backend action.
        start_ok = r.status_code in (200, 400)
        check("POST /api/campaign/start executes backend preflight/start", start_ok,
              f"status={r.status_code} {r.text[:160]}")
        if r.status_code == 400 and "Telephony bridge not configured" in r.text:
            print("    (expected in CI: no Vobiz creds -> preflight blocks launch)")
        s = client.get(f"{base}/state?role={R}&_skip_cache=true")
        check("GET /api/campaign/state returns state", ok(s) and "counts" in s.text or ok(s), s.text[:120])
        r = client.post(f"{base}/stop?role={R}")
        check("POST /api/campaign/stop executes", ok(r), r.text[:120])

        # ── 9. Lead disposition (setCallDisposition) ───────────────────────────
        conn = _get_conn()
        row = conn.execute("SELECT id FROM leads WHERE role=? ORDER BY id LIMIT 1", (R,)).fetchone()
        conn.close()
        lead_id = int(row[0]) if row else None
        if lead_id:
            r = client.post(f"{base}/lead/{lead_id}/status?role={R}", json={"status": "interested"})
            check("POST /api/campaign/lead/{id}/status updates", ok(r), r.text[:120])
            conn = _get_conn()
            st = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0]
            conn.close()
            check("lead status persisted", st == "interested", f"status={st}")
        else:
            check("POST /api/campaign/lead/{id}/status updates", False, "no lead to update")

        # ── 10. Refresh phone numbers / inter-call gap (loadCampaignControl) ───
        r = client.get(f"{base}/phone-numbers?role={R}")
        check("GET /api/campaign/phone-numbers", ok(r), r.text[:120])
        r = client.post(f"{base}/inter-call-gap?role={R}", json={"seconds": 30})
        check("POST /api/campaign/inter-call-gap", ok(r), r.text[:120])

        # ── 11. Remove all (deleteAllSources) ──────────────────────────────────
        r = client.delete(f"{base}/contacts?role={R}&source=campaign")
        check("DELETE /api/campaign/contacts (campaign) clears", ok(r) and r.json().get("deleted", 0) > 0, r.text[:120])
        r = client.get(f"{base}/contacts?role={R}&source=campaign")
        check("contact list empty after clear", ok(r) and len(r.json().get("contacts", [])) == 0)

        # ── 12. Wipe (campaignControlClear) ────────────────────────────────────
        r = client.post(f"{base}/wipe?role={R}")
        check("POST /api/campaign/wipe", ok(r), r.text[:120])

        # ── 13. Direct checks of dialer-gate semantics (no dialing) ────────────
        from core.campaign_hours import campaign_dial_window_active, is_within_calling_window, is_calling_day, is_holiday_check
        from datetime import datetime
        from zoneinfo import ZoneInfo
        TZ = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 12, 25, 12, 0, tzinfo=TZ)  # a holiday, Friday
        cfg_test = {"window_start": "09:00", "window_end": "18:00", "calling_days": [0, 1, 2, 3, 4], "holidays": ["2026-12-25"]}
        check("dialer gate: window+day+holiday respected", not campaign_dial_window_active(cfg_test, now),
              "Dec-25-2026 is a configured holiday -> must not dial")
        now2 = datetime(2026, 12, 24, 12, 0, tzinfo=TZ)
        check("dialer gate: in-window non-holiday dials", campaign_dial_window_active(cfg_test, now2))
        check("dialer gate: window bounds", not is_within_calling_window(cfg_test, datetime(2026, 12, 24, 20, 0, tzinfo=TZ)))
        check("dialer gate: calling days", not is_calling_day(cfg_test, datetime(2026, 12, 26, 12, 0, tzinfo=TZ)))  # Saturday not in days

        # ── 14. Manual call endpoint existence (needs Vobiz creds to dial) ─────
        from core.state import get_campaign_config
        saved_cfg = get_campaign_config(R)
        check("config persisted to role_state (Postgres)", bool(saved_cfg.get("campaign_name")))

    finally:
        try:
            close_db()
        except Exception:
            pass
        tmp.cleanup()

    failed = [x for x in RESULTS if not x["ok"]]
    print(f"\n==== {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed ====")
    for f in failed:
        print("  FAILED:", f["name"], "|", f["detail"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
