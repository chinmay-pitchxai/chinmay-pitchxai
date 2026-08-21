#!/usr/bin/env python3
"""Attach ``_log_id`` to leads by matching ``start_time`` to transcript JSONL session timestamps."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.call_recording import resolve_session_recording_path


def _session_epoch_from_jsonl(path: Path) -> float | None:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                ts = obj.get("ts") or obj.get("timestamp")
                if not ts:
                    continue
                s = str(ts).strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s).timestamp()
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return None


def _collect_sessions(role: str) -> list[tuple[str, float, Path]]:
    sessions: list[tuple[str, float, Path]] = []
    bases = [
        BACKEND_DIR / "data" / role / "logs",
        Path("/root/vernika/backend/data") / role / "logs",
        Path("/root/vernika/agent/data") / role / "logs",
    ]
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for day_dir in base.iterdir():
            if not day_dir.is_dir() or len(day_dir.name) != 10:
                continue
            for p in day_dir.glob("*.jsonl"):
                sid = p.stem
                if sid in seen:
                    continue
                ep = _session_epoch_from_jsonl(p)
                if ep is None:
                    continue
                seen.add(sid)
                sessions.append((sid, ep, p))
    return sessions


def backfill(role: str, db_path: Path, *, tolerance_sec: float, apply: bool) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    leads = conn.execute(
        """
        SELECT id, start_time, _log_id FROM leads
        WHERE role = ? AND (trim(COALESCE(_log_id, '')) = '')
          AND start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
        """,
        (role,),
    ).fetchall()

    sessions = _collect_sessions(role)
    used_sessions: set[str] = set()
    matched = 0
    with_recording = 0

    for lead in leads:
        lid = lead["id"]
        target = float(lead["start_time"])
        best_sid = None
        best_delta = tolerance_sec + 1.0
        for sid, ep, _p in sessions:
            if sid in used_sessions:
                continue
            delta = abs(ep - target)
            if delta < best_delta:
                best_delta = delta
                best_sid = sid
        if best_sid is None or best_delta > tolerance_sec:
            continue
        used_sessions.add(best_sid)
        matched += 1
        if resolve_session_recording_path(best_sid):
            with_recording += 1
        if apply:
            conn.execute(
                "UPDATE leads SET _log_id = ?, updated_at = datetime('now') WHERE id = ?",
                (best_sid, lid),
            )

    if apply:
        conn.commit()
    conn.close()
    return {
        "role": role,
        "leads_missing_log": len(leads),
        "sessions_indexed": len(sessions),
        "matched": matched,
        "with_recording": with_recording,
        "apply": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--db", default="")
    parser.add_argument("--tolerance-sec", type=float, default=180.0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else BACKEND_DIR / "data" / "vernika.db"
    result = backfill(args.role.strip().lower(), db_path, tolerance_sec=args.tolerance_sec, apply=args.apply)
    for k, v in result.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
