"""Audit + fix today's Interested/Site Visit on VPS. Run: python backend/scripts/audit_fix_today.py [--apply]"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import wave
from pathlib import Path

BASE = Path("/opt/technopolis")
DB = BASE / "backend/data/vernika.db"
TODAY = "2026-07-21"
TEST_PHONE10 = "7204955388"
MIN_REAL_SEC = 15.0
FAKE_MARKERS = ("surya meadows", "2.2 crore", "2.2 crores")


def phone10(p: str) -> str:
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-10:] if d else ""


def wav_dur(path: str) -> float | None:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def find_recording_sec(log_id: str) -> tuple[float | None, str | None]:
    if not log_id:
        return None, None
    best = None
    best_path = None
    for root in (
        BASE / "backend/data/call_recordings",
        BASE / "backend/data/Technopolis_Call_Recordings",
    ):
        for p in glob.glob(str(root / "**" / f"*{log_id}*"), recursive=True):
            if not p.endswith(".wav"):
                continue
            if "_mixed" not in p and "_full" not in p:
                continue
            d = wav_dur(p)
            if d and (best is None or d > best):
                best, best_path = d, p
    return best, best_path


def read_jsonl(role: str, log_id: str) -> tuple[str, str]:
    if not log_id:
        return "", ""
    candidates = [
        BASE / "backend/data/conversation_logs" / TODAY / f"{log_id}.jsonl",
        BASE / "backend/data" / role / "logs" / TODAY / f"{log_id}.jsonl",
        BASE / "data" / role / "logs" / TODAY / f"{log_id}.jsonl",
    ]
    for p in candidates:
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace"), str(p)
    return "", ""


def classify_transcript_source(path: str) -> str:
    if not path:
        return "none"
    if "conversation_logs" in path:
        return "live_jsonl"
    if "/data/sales_" in path.replace("backend/data", "data"):
        return "audio_transcription"
    return "jsonl_other"


def is_fake(log_id: str, role: str, analysis: dict, dur: float | None, raw: str, src: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    blob = ((raw or "") + " " + (analysis.get("summary") or "")).lower()
    for m in FAKE_MARKERS:
        if m in blob:
            reasons.append(f"hallucination:{m}")
    if dur is not None and dur < MIN_REAL_SEC:
        reasons.append(f"short_recording:{dur:.1f}s")
    if src == "audio_transcription" and dur is not None and dur < 30:
        reasons.append("audio_on_short_clip")
    n_turns = sum(1 for ln in (raw or "").splitlines() if ln.strip())
    if n_turns >= 6 and dur is not None and dur < min(n_turns * 4, 25):
        reasons.append(f"turns_vs_duration:{n_turns}turns/{dur:.1f}s")
    # Audio-written jsonl with no live log
    live_path = BASE / "backend/data/conversation_logs" / TODAY / f"{log_id}.jsonl"
    if not live_path.is_file() and src == "audio_transcription" and dur is not None and dur < 45:
        reasons.append("no_live_jsonl+audio")
    return bool(reasons), reasons


def corrected_status(reasons: list[str], dur: float | None) -> tuple[str, dict]:
    if any("hallucination" in r or "audio_on_short" in r or "no_live_jsonl" in r for r in reasons):
        return "no response", {
            "summary": "Call too short or transcript unreliable (AI-generated from brief audio). Not counted as Interested/Site Visit.",
            "rating": 0,
            "disposition": "No Response",
            "emotion_label": "Unknown",
            "emotion_rationale": "Insufficient verified conversation.",
            "emotion_confidence": None,
            "site_visit_agreed": False,
            "next_action": {"action_type": "None", "datetime_iso": None, "details": ""},
            "preferred_location": None,
            "preferred_budget": None,
            "email_address": None,
            "transcript_corrected": True,
            "correction_reason": "; ".join(reasons),
        }
    if dur is not None and dur < MIN_REAL_SEC:
        return "no answer", {
            "summary": "Brief connection; no verified conversation.",
            "rating": 0,
            "disposition": "No Answer",
            "site_visit_agreed": False,
            "transcript_corrected": True,
            "correction_reason": "; ".join(reasons),
        }
    return "completed", {
        "summary": "Review manually — could not auto-verify transcript.",
        "rating": 0,
        "disposition": "Answered",
        "site_visit_agreed": False,
        "transcript_corrected": True,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write corrections to DB")
    ap.add_argument("--date", default=TODAY)
    args = ap.parse_args()
    day = args.date

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("=== CAMPAIGN / TODAY OVERVIEW ===")
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%campaign%'")
        print("tables:", [r[0] for r in cur.fetchall()])
    except Exception:
        pass

    cur.execute(
        """
        SELECT status, COUNT(*) c FROM leads
        WHERE first_called_at IS NOT NULL
          AND date(datetime(first_called_at, 'unixepoch', '+5 hours', '+30 minutes')) = ?
        GROUP BY status ORDER BY c DESC
        """,
        (day,),
    )
    print("called_today_by_status:", [dict(r) for r in cur.fetchall()])

    cur.execute(
        "SELECT COUNT(*) FROM leads WHERE first_called_at IS NOT NULL AND date(datetime(first_called_at, 'unixepoch', '+5 hours', '+30 minutes')) = ?",
        (day,),
    )
    print("total_called_today:", cur.fetchone()[0])

    cur.execute(
        """
        SELECT id, role, name, phone, status, _log_id, analysis
        FROM leads
        WHERE status IN ('interested', 'site_visit', 'site visit')
          AND (
            date(datetime(first_called_at, 'unixepoch', '+5 hours', '+30 minutes')) = ?
            OR date(updated_at) = ?
          )
        ORDER BY id
        """,
        (day, day),
    )
    rows = list(cur.fetchall())
    print(f"interested_site_visit_today: {len(rows)}")

    suspect: list[dict] = []
    ok: list[dict] = []
    test_ids: list[int] = []

    for r in rows:
        d = dict(r)
        lid = int(d["id"])
        if phone10(d.get("phone")) == TEST_PHONE10:
            test_ids.append(lid)
            continue
        log_id = (d.get("_log_id") or "").strip()
        role = d.get("role") or "sales_1"
        try:
            analysis = json.loads(d.get("analysis") or "{}")
        except Exception:
            analysis = {}
        dur, wav = find_recording_sec(log_id)
        raw, jpath = read_jsonl(role, log_id)
        src = classify_transcript_source(jpath)
        fake, reasons = is_fake(log_id, role, analysis, dur, raw, src)
        item = {
            "id": lid,
            "name": (d.get("name") or "")[:50],
            "phone": d.get("phone"),
            "status": d.get("status"),
            "disposition": analysis.get("disposition"),
            "dur_sec": round(dur, 1) if dur else None,
            "src": src,
            "fake": fake,
            "reasons": reasons,
        }
        (suspect if fake else ok).append(item)

    print(f"\nTEST_LEADS_TO_REMOVE (phone {TEST_PHONE10}): {test_ids}")
    print(f"\nSUSPECT_FAKE ({len(suspect)}):")
    print(json.dumps(suspect, indent=2))
    print(f"\nLIKELY_OK ({len(ok)}):")
    print(json.dumps(ok, indent=2))

    if args.apply:
        for lid in test_ids:
            cur.execute(
                "UPDATE leads SET status='dnc', analysis=?, updated_at=datetime('now') WHERE id=?",
                (
                    json.dumps(
                        {
                            "summary": "Test number (Chinmay dev) — excluded from campaign.",
                            "disposition": "DNC",
                            "rating": 0,
                            "site_visit_agreed": False,
                        }
                    ),
                    lid,
                ),
            )
            cur.execute("DELETE FROM call_attempts WHERE lead_id=?", (lid,))
            print(f"REMOVED test lead {lid}")

        for item in suspect:
            lid = item["id"]
            new_status, new_analysis = corrected_status(item["reasons"], item.get("dur_sec"))
            cur.execute(
                "UPDATE leads SET status=?, analysis=?, updated_at=datetime('now') WHERE id=?",
                (new_status, json.dumps(new_analysis), lid),
            )
            cur.execute(
                "UPDATE call_attempts SET status=?, disposition=?, summary=? WHERE lead_id=?",
                (
                    new_status,
                    new_analysis.get("disposition", "No Response"),
                    new_analysis.get("summary", "")[:500],
                    lid,
                ),
            )
            print(f"CORRECTED lead {lid} -> {new_status} ({new_analysis.get('disposition')})")

        conn.commit()
        print("\nAPPLIED corrections.")

        cur.execute(
            """
            SELECT status, COUNT(*) c FROM leads
            WHERE first_called_at IS NOT NULL
              AND date(datetime(first_called_at, 'unixepoch', '+5 hours', '+30 minutes')) = ?
            GROUP BY status ORDER BY c DESC
            """,
            (day,),
        )
        print("after_fix_called_today:", [dict(r) for r in cur.fetchall()])

    conn.close()


if __name__ == "__main__":
    main()
