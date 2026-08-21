"""Audit ALL site_visit leads on VPS (both roles). Keep real, fix fake."""
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
FAKE_MARKERS = ("surya meadows", "2.2 crore", "2.2 crores", "mr. rahul", "mr. amit", "developer mode")


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
    for root in (BASE / "backend/data/call_recordings", BASE / "backend/data/Technopolis_Call_Recordings"):
        for p in glob.glob(str(root / "**" / f"*{log_id}*"), recursive=True):
            if not p.endswith(".wav") or ("_mixed" not in p and "_full" not in p):
                continue
            d = wav_dur(p)
            if d and (best is None or d > best):
                best, best_path = d, p
    return best, best_path


def has_live_jsonl(log_id: str) -> bool:
    if not log_id:
        return False
    for p in glob.glob(str(BASE / "backend/data/conversation_logs/**" / f"{log_id}.jsonl"), recursive=True):
        if Path(p).stat().st_size > 30:
            return True
    return False


def read_any_jsonl(role: str, log_id: str) -> tuple[str, str]:
    paths = [
        BASE / "backend/data/conversation_logs",
        BASE / "backend/data" / role / "logs",
        BASE / "data" / role / "logs",
    ]
    for base in paths:
        for p in glob.glob(str(base / "**" / f"{log_id}.jsonl"), recursive=True):
            pp = Path(p)
            if pp.is_file():
                return pp.read_text(encoding="utf-8", errors="replace"), str(pp)
    return "", ""


def classify(role: str, log_id: str, analysis: dict, dur: float | None, raw: str, jpath: str) -> tuple[bool, list[str], str]:
    reasons: list[str] = []
    blob = ((raw or "") + " " + (analysis.get("summary") or "")).lower()
    for m in FAKE_MARKERS:
        if m in blob:
            reasons.append(f"marker:{m}")
    live = has_live_jsonl(log_id)
    src = "live_jsonl" if live else ("audio_transcription" if jpath and "/data/sales_" in jpath.replace("backend/data", "data") else "unknown")
    if phone10(analysis.get("_phone", "")) == TEST_PHONE10:
        return True, ["test_number"], "test"
    try:
        import sys
        backend = str(BASE / "backend")
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from services.transcript_hybrid import coalesce_jsonl_turns
        from services.transcript_roles import transcript_has_severe_speaker_swap

        turns = coalesce_jsonl_turns(raw or "")
        if turns and transcript_has_severe_speaker_swap(turns):
            reasons.append("speaker_role_swap")
    except Exception as ex:
        reasons.append(f"swap_check_err:{ex!s}"[:40])
    n_turns = sum(1 for ln in (raw or "").splitlines() if ln.strip())
    if dur is not None and dur < MIN_REAL_SEC:
        reasons.append(f"short_rec:{dur:.1f}s")
    if not live and src == "audio_transcription" and dur is not None and dur < 45:
        reasons.append("audio_no_live")
    if n_turns >= 6 and dur is not None and dur < min(n_turns * 4, 30):
        reasons.append(f"turns_vs_dur:{n_turns}/{dur:.1f}s")
    # Real: long recording OR live jsonl with adequate duration
    if not reasons and dur is not None and dur >= MIN_REAL_SEC:
        return False, [], "verified_recording"
    if not reasons and live and dur is not None and dur >= MIN_REAL_SEC:
        return False, [], "verified_live"
    if not reasons and dur is None and live:
        return False, [], "live_only"
    fake = bool(reasons) or (dur is not None and dur < MIN_REAL_SEC)
    return fake, reasons, src


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, role, name, phone, status, _log_id, analysis, first_called_at
        FROM leads
        WHERE status IN ('site_visit', 'site visit', 'site_visited')
           OR CAST(json_extract(analysis, '$.site_visit_agreed') AS INTEGER) = 1
           OR json_extract(analysis, '$.disposition') LIKE '%Site Visit%'
        ORDER BY role, id
        """
    )
    rows = list(cur.fetchall())
    print(f"TOTAL_SITE_VISIT_CANDIDATES: {len(rows)}")

    by_role = {"sales_1": []}
    keep, fix, test = [], [], []

    for r in rows:
        d = dict(r)
        lid = d["id"]
        role = d.get("role") or "sales_1"
        p10 = phone10(d.get("phone"))
        try:
            analysis = json.loads(d.get("analysis") or "{}")
        except Exception:
            analysis = {}
        analysis["_phone"] = d.get("phone")
        log_id = (d.get("_log_id") or "").strip()

        if p10 == TEST_PHONE10:
            test.append({"id": lid, "role": role, "name": d.get("name"), "phone": d.get("phone")})
            continue

        dur, wav = find_recording_sec(log_id)
        raw, jpath = read_any_jsonl(role, log_id)
        fake, reasons, src = classify(role, log_id, analysis, dur, raw, jpath)
        item = {
            "id": lid,
            "role": role,
            "name": (d.get("name") or "")[:45],
            "phone": d.get("phone"),
            "status": d.get("status"),
            "log_id": log_id,
            "dur_sec": round(dur, 1) if dur else None,
            "live_jsonl": has_live_jsonl(log_id),
            "src": src,
            "disposition": analysis.get("disposition"),
            "summary": (analysis.get("summary") or "")[:100],
            "fake": fake,
            "reasons": reasons,
        }
        if fake:
            fix.append(item)
        else:
            keep.append(item)
        by_role.setdefault(role, []).append(item)

    print("\n=== TEST (remove) ===")
    print(json.dumps(test, indent=2))
    print(f"\n=== KEEP REAL ({len(keep)}) ===")
    print(json.dumps(keep, indent=2))
    print(f"\n=== FIX/REMOVE FAKE ({len(fix)}) ===")
    print(json.dumps(fix, indent=2))
    print(f"\nBy role: sales_1 keep={sum(1 for x in keep if x['role']=='sales_1')} fix={sum(1 for x in fix if x['role']=='sales_1')}")

    if args.apply:
        for t in test:
            cur.execute(
                "UPDATE leads SET status='dnc', analysis=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps({"summary": "Test number — excluded.", "disposition": "DNC", "site_visit_agreed": False}), t["id"]),
            )
            print(f"DNC test lead {t['id']} ({t['name']})")

        for item in fix:
            new_analysis = {
                "summary": "Site visit could not be verified — recording/transcript unreliable (removed from Site Visit list).",
                "rating": 0,
                "disposition": "No Response",
                "site_visit_agreed": False,
                "emotion_label": "Unknown",
                "transcript_corrected": True,
                "correction_reason": "; ".join(item["reasons"]),
                "original_disposition": item.get("disposition"),
            }
            cur.execute(
                "UPDATE leads SET status='no response', analysis=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(new_analysis), item["id"]),
            )
            cur.execute(
                "UPDATE call_attempts SET status='no response', disposition='No Response', summary=? WHERE lead_id=?",
                (new_analysis["summary"][:500], item["id"]),
            )
            print(f"FIXED {item['id']} {item['name']} ({item['role']})")

        conn.commit()
        cur.execute("SELECT status, COUNT(*) c FROM leads WHERE status IN ('site_visit','site visit') GROUP BY status")
        print("\nRemaining site_visit status:", [dict(r) for r in cur.fetchall()])

    conn.close()


if __name__ == "__main__":
    main()
