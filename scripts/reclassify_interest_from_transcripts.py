#!/usr/bin/env python3
"""Re-apply soft-interest rules to existing leads (sales_1) from transcripts + summaries."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.worker import _read_transcript_jsonl
from services.call_analyzer import canonical_disposition
from services.transcript_interest import apply_interest_disposition_override


def reclassify(db_path: Path, role: str, *, apply: bool) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, analysis, _log_id FROM leads WHERE role = ?", (role,)
    ).fetchall()
    upgraded = 0
    skipped = 0
    for row in rows:
        raw = row["analysis"]
        aj = json.loads(raw) if raw else {}
        canon = canonical_disposition(aj.get("disposition"))
        if canon in ("Interested", "Not Interested", "Call Later", "Busy", "Wrong Number"):
            skipped += 1
            continue
        log_id = str(row["_log_id"] or "").strip()
        transcript = _read_transcript_jsonl(role, log_id) if log_id else ""
        new_aj = apply_interest_disposition_override(aj, transcript or None)
        if canonical_disposition(new_aj.get("disposition")) != "Interested":
            continue
        upgraded += 1
        if apply:
            conn.execute(
                "UPDATE leads SET analysis = ? WHERE id = ?",
                (json.dumps(new_aj, ensure_ascii=False), row["id"]),
            )
    if apply:
        conn.commit()
    conn.close()
    return {"role": role, "upgraded_to_interested": upgraded, "skipped": skipped, "mode": "apply" if apply else "dry-run"}


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
    print(json.dumps(reclassify(db, args.role.strip().lower(), apply=args.apply), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
