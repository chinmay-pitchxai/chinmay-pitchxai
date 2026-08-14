"""Full cleanup: site_visit + interested fake transcripts. Run on VPS with --apply."""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import wave
from pathlib import Path

BASE = Path("/opt/technopolis")
DB = BASE / "backend/data/vernika.db"
TEST_PHONE10 = "7204955388"
MIN_REAL_SEC = 20.0
FAKE_MARKERS = (
    "surya meadows", "2.2 crore", "mr. rahul", "mr. amit", "developer mode",
    "december 2025", "dec 2025", "2400 to 4000", "international airport",
)

import sys
sys.path.insert(0, str(BASE / "backend"))
from services.transcript_hybrid import coalesce_jsonl_turns
from services.transcript_roles import transcript_has_severe_speaker_swap


def phone10(p: str) -> str:
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-10:] if d else ""


def wav_dur(path: str) -> float | None:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def find_recording_sec(log_id: str) -> float | None:
    best = None
    for root in (BASE / "backend/data/call_recordings", BASE / "backend/data/Technopolis_Call_Recordings"):
        for p in glob.glob(str(root / "**" / f"*{log_id}*"), recursive=True):
            if p.endswith(".wav") and ("_mixed" in p or "_full" in p):
                d = wav_dur(p)
                if d and (best is None or d > best):
                    best = d
    return best


def has_live_jsonl(log_id: str) -> bool:
    for p in glob.glob(str(BASE / "backend/data/conversation_logs/**" / f"{log_id}.jsonl"), recursive=True):
        if Path(p).stat().st_size > 30:
            return True
    return False


def read_jsonl(role: str, log_id: str) -> str:
    for base in (BASE / "backend/data/conversation_logs", BASE / "backend/data" / role / "logs", BASE / "data" / role / "logs"):
        for p in glob.glob(str(base / "**" / f"{log_id}.jsonl"), recursive=True):
            return Path(p).read_text(encoding="utf-8", errors="replace")
    return ""


def is_fake(role: str, log_id: str, analysis: dict, phone: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if phone10(phone) == TEST_PHONE10:
        return True, ["test_number"]
    dur = find_recording_sec(log_id) if log_id else None
    raw = read_jsonl(role, log_id) if log_id else ""
    blob = (raw + " " + (analysis.get("summary") or "")).lower()
    for m in FAKE_MARKERS:
        if m in blob:
            reasons.append(f"marker:{m}")
    if dur is not None and dur < MIN_REAL_SEC:
        reasons.append(f"short_rec:{dur:.1f}s")
    elif log_id and dur is None and "site visit" in str(analysis.get("disposition") or "").lower():
        reasons.append("no_recording")
    turns = coalesce_jsonl_turns(raw)
    if turns and transcript_has_severe_speaker_swap(turns):
        reasons.append("speaker_swap")
    if not has_live_jsonl(log_id) and raw and dur is not None and dur < 45:
        reasons.append("audio_no_live")
    n = sum(1 for ln in raw.splitlines() if ln.strip())
    if n >= 6 and dur is not None and dur < min(n * 4, 30):
        reasons.append(f"turns_vs_dur:{n}/{dur:.1f}s")
    return bool(reasons), reasons


def corrected_analysis(reasons: list[str]) -> dict:
    return {
        "summary": "Outcome removed — recording too short or transcript unreliable (AI-generated / wrong facts). Listen to recording manually if needed.",
        "rating": 0,
        "disposition": "No Response",
        "site_visit_agreed": False,
        "emotion_label": "Unknown",
        "emotion_rationale": "Transcript not verified.",
        "emotion_confidence": None,
        "next_action": {"action_type": "None", "datetime_iso": None, "details": ""},
        "transcript_corrected": True,
        "correction_reason": "; ".join(reasons),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Scan every lead that could appear as Site Visit / Interested on dashboard
    cur.execute(
        """
        SELECT id, role, name, phone, status, _log_id, analysis
        FROM leads
        WHERE status IN ('site_visit', 'site visit', 'site_visited', 'interested', 'completed', 'callback_scheduled')
           OR CAST(json_extract(analysis, '$.site_visit_agreed') AS INTEGER) = 1
           OR lower(json_extract(analysis, '$.disposition')) LIKE '%site visit%'
           OR lower(json_extract(analysis, '$.disposition')) LIKE '%interested%'
        ORDER BY id
        """
    )
    rows = list(cur.fetchall())
    print(f"SCANNED: {len(rows)}")

    fix_list, keep_list = [], []
    for r in rows:
        d = dict(r)
        try:
            aj = json.loads(d.get("analysis") or "{}")
        except Exception:
            aj = {}
        fake, reasons = is_fake(d.get("role") or "sales_1", d.get("_log_id") or "", aj, d.get("phone") or "")
        st = str(d.get("status") or "").lower()
        # Any site_visit / interested row must pass verification — demote if not clearly real.
        if not fake and st in ("site_visit", "site visit", "site_visited", "interested"):
            if not reasons:
                dur_chk = find_recording_sec(d.get("_log_id") or "")
                if dur_chk is None or dur_chk < MIN_REAL_SEC:
                    fake, reasons = True, (reasons or []) + ["unverified_outcome"]
        dur = find_recording_sec(d.get("_log_id") or "")
        item = {
            "id": d["id"], "name": (d.get("name") or "")[:40], "phone": d.get("phone"),
            "status": d.get("status"), "dur": round(dur, 1) if dur else None, "fake": fake, "reasons": reasons,
        }
        (fix_list if fake else keep_list).append(item)

    print(f"\nFIX ({len(fix_list)}):")
    print(json.dumps(fix_list, indent=2))
    print(f"\nKEEP ({len(keep_list)}):")
    print(json.dumps(keep_list, indent=2))

    if args.apply:
        for item in fix_list:
            lid = item["id"]
            ca = corrected_analysis(item["reasons"])
            new_st = "dnc" if "test_number" in item["reasons"] else "no response"
            if new_st == "dnc":
                ca["disposition"] = "DNC"
                ca["summary"] = "Test number — excluded."
            cur.execute(
                "UPDATE leads SET status=?, analysis=?, updated_at=datetime('now') WHERE id=?",
                (new_st, json.dumps(ca), lid),
            )
            cur.execute(
                "UPDATE call_attempts SET status=?, disposition=?, summary=? WHERE lead_id=?",
                (new_st, ca["disposition"], ca["summary"][:500], lid),
            )
            print(f"FIXED {lid} {item['name']} -> {new_st}")

        # Clear stale site_visit_agreed on any remaining non-site_visit
        cur.execute(
            """
            UPDATE leads SET analysis = json_set(analysis, '$.site_visit_agreed', 0)
            WHERE status NOT IN ('site_visit', 'site visit', 'site_visited')
              AND CAST(json_extract(analysis, '$.site_visit_agreed') AS INTEGER) = 1
            """
        )
        print("Cleared site_visit_agreed on", cur.rowcount, "rows")

        conn.commit()

        # Backfill _log_id from call_attempts for remaining site_visit / interested
        cur.execute(
            """
            UPDATE leads
            SET _log_id = (
                SELECT log_id FROM call_attempts ca
                WHERE ca.lead_id = leads.id AND ca.log_id IS NOT NULL AND ca.log_id != ''
                ORDER BY ca.id DESC LIMIT 1
            )
            WHERE (_log_id IS NULL OR _log_id = '')
              AND status IN ('site_visit', 'interested', 'completed')
              AND EXISTS (SELECT 1 FROM call_attempts ca WHERE ca.lead_id = leads.id AND ca.log_id IS NOT NULL)
            """
        )
        print("Backfilled _log_id on", cur.rowcount, "rows")

        conn.commit()
        cur.execute("SELECT COUNT(*) FROM leads WHERE status='site_visit'")
        print("Remaining site_visit:", cur.fetchone()[0])
        cur.execute(
            "SELECT id, name, phone FROM leads WHERE status='site_visit'"
        )
        print("SITE_VISIT_LIST:", [dict(r) for r in cur.fetchall()])

    conn.close()


if __name__ == "__main__":
    main()
