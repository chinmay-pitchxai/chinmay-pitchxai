#!/usr/bin/env python3
"""Delete campaign leads (and related rows) with activity before a calendar cutoff.

Usage:
  python scripts/purge_leads_before_date.py --before 2026-05-15 --roles sales_1 --apply

Default is dry-run. Removes matching transcript JSONL and recording WAVs under backend/data/.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.call_recording import resolve_session_recording_path  # noqa: E402


def _lead_is_old_clause() -> str:
    return """
        (updated_at IS NOT NULL AND date(updated_at) < date(?))
        OR (
            start_time IS NOT NULL AND CAST(start_time AS REAL) > 0
            AND date(datetime(CAST(start_time AS REAL), 'unixepoch')) < date(?)
        )
    """


def _fetch_old_log_ids(conn: sqlite3.Connection, role: str, before_iso: str) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT trim(_log_id) AS lid FROM leads
        WHERE role = ? AND trim(COALESCE(_log_id, '')) != ''
          AND ({_lead_is_old_clause()})
        """,
        (role, before_iso, before_iso),
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def _unlink_log_files(role: str, log_ids: list[str], before_iso: str) -> int:
    removed = 0
    for log_id in log_ids:
        date_hint = None
        m = re.search(r"(\d{4})(\d{2})(\d{2})T", log_id)
        if m:
            date_hint = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if date_hint:
            for sub in (f"data/{role}/logs/{date_hint}", f"data/logs/{date_hint}"):
                d = BACKEND_DIR / sub
                for ext in ("jsonl", "txt"):
                    p = d / f"{log_id}.{ext}"
                    if p.is_file():
                        try:
                            p.unlink()
                            removed += 1
                        except OSError:
                            pass
        rec = resolve_session_recording_path(log_id)
        if rec and rec.is_file():
            try:
                rec.unlink()
                removed += 1
            except OSError:
                pass

    logs_root = BACKEND_DIR / "data" / role / "logs"
    if logs_root.is_dir():
        for day_dir in logs_root.iterdir():
            if day_dir.is_dir() and len(day_dir.name) == 10 and day_dir.name < before_iso:
                for f in list(day_dir.iterdir()):
                    if f.is_file():
                        try:
                            f.unlink()
                            removed += 1
                        except OSError:
                            pass
                try:
                    day_dir.rmdir()
                except OSError:
                    pass
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="YYYY-MM-DD cutoff (exclusive)")
    parser.add_argument("--roles", default="sales_1")
    parser.add_argument("--db", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    before_iso = args.before.strip()
    try:
        datetime.strptime(before_iso, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid --before: {before_iso!r}", file=sys.stderr)
        return 1

    db_path = Path(args.db) if args.db else BACKEND_DIR / "data" / "vernika.db"
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    roles = [r.strip().lower() for r in args.roles.split(",") if r.strip()]
    conn = sqlite3.connect(db_path)

    print(f"Database: {db_path}")
    print(f"Delete activity before: {before_iso}")
    print(f"Roles: {', '.join(roles)}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")

    plan: dict[str, dict] = {}
    for role in roles:
        n = conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE role = ? AND ({_lead_is_old_clause()})",
            (role, before_iso, before_iso),
        ).fetchone()[0]
        log_ids = _fetch_old_log_ids(conn, role, before_iso)
        inbound = conn.execute(
            "SELECT COUNT(*) FROM inbound_callbacks WHERE role = ? AND date(created_at) < date(?)",
            (role, before_iso),
        ).fetchone()[0]
        manual = conn.execute(
            "SELECT COUNT(*) FROM manual_calls WHERE role = ? AND date(started_at) < date(?)",
            (role, before_iso),
        ).fetchone()[0]
        plan[role] = {"leads": n, "log_ids": log_ids, "inbound": inbound, "manual": manual}
        print(f"  {role}: {n} leads, {len(log_ids)} log files, {inbound} inbound, {manual} manual")

    if not args.apply:
        print("\nDry-run only. Pass --apply to delete.")
        conn.close()
        return 0

    files_removed = 0
    for role in roles:
        log_ids = plan[role]["log_ids"]
        files_removed += _unlink_log_files(role, log_ids, before_iso)
        conn.execute(
            f"DELETE FROM leads WHERE role = ? AND ({_lead_is_old_clause()})",
            (role, before_iso, before_iso),
        )
        conn.execute(
            "DELETE FROM inbound_callbacks WHERE role = ? AND date(created_at) < date(?)",
            (role, before_iso),
        )
        conn.execute(
            "DELETE FROM manual_calls WHERE role = ? AND date(started_at) < date(?)",
            (role, before_iso),
        )
    conn.commit()
    conn.close()

    total_leads = sum(p["leads"] for p in plan.values())
    print(f"\nDeleted {total_leads} leads; removed ~{files_removed} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
