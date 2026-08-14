#!/usr/bin/env python3
"""Copy ``_log_id`` / ``start_time`` from duplicate leads (same role+phone) onto rows missing them."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def _norm_phone(p: object) -> str:
    return "".join(c for c in str(p or "") if c.isdigit())[-10:]


def backfill(db_path: Path, role: str, *, apply: bool) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, phone, _log_id, start_time FROM leads WHERE role = ?", (role,)
    ).fetchall()
    by_phone: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        ph = _norm_phone(r["phone"])
        if ph:
            by_phone[ph].append(r)

    updated = 0
    for ph, group in by_phone.items():
        donor = None
        for r in group:
            if str(r["_log_id"] or "").strip():
                donor = r
                break
        if not donor:
            continue
        log_id = str(donor["_log_id"] or "").strip()
        st = donor["start_time"]
        for r in group:
            if str(r["_log_id"] or "").strip():
                continue
            updated += 1
            if apply:
                conn.execute(
                    "UPDATE leads SET _log_id = ?, start_time = COALESCE(start_time, ?) WHERE id = ?",
                    (log_id, st, r["id"]),
                )
    if apply:
        conn.commit()
    conn.close()
    return {"role": role, "updated": updated, "mode": "apply" if apply else "dry-run"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", default="sales_1")
    p.add_argument("--db", default="")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    db = Path(args.db or Path(__file__).resolve().parents[1] / "data" / "vernika.db")
    if not db.is_file():
        print(f"DB not found: {db}", file=sys.stderr)
        return 1
    import json

    print(json.dumps(backfill(db, args.role.strip().lower(), apply=args.apply), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
