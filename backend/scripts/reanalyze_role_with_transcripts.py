#!/usr/bin/env python3
"""Re-run Gemini QA + soft-interest rules for one role (leads with ``_log_id`` only)."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.storage import init_db
from core.worker import _analyze_and_update_lead, _read_transcript_jsonl


def _user_turns(transcript: str) -> int:
    import json

    n = 0
    for line in (transcript or "").splitlines():
        try:
            obj = json.loads(line)
            if obj.get("role") == "user" and len(str(obj.get("content") or "").strip()) > 1:
                n += 1
        except json.JSONDecodeError:
            pass
    return n


async def run_role(role: str, data_dir: Path, *, limit: int) -> None:
    init_db(str(data_dir))
    conn = sqlite3.connect(data_dir / "vernika.db")
    rows = conn.execute(
        "SELECT id, _log_id FROM leads WHERE role = ? AND _log_id IS NOT NULL AND _log_id != '' "
        "ORDER BY id DESC",
        (role,),
    ).fetchall()
    conn.close()
    done = 0
    for lid, log_id in rows:
        if limit and done >= limit:
            break
        t = _read_transcript_jsonl(role, log_id) or ""
        if _user_turns(t) < 1:
            continue
        await _analyze_and_update_lead(role, int(lid), log_id)
        done += 1
        print(f"analyzed lead {lid}", flush=True)
    print(f"done {done} leads for {role}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", default="sales_1")
    p.add_argument("--db", default="")
    p.add_argument("--limit", type=int, default=0, help="0 = all eligible")
    args = p.parse_args()
    data_dir = Path(args.db).resolve().parent if args.db else Path(__file__).resolve().parents[1] / "data"
    asyncio.run(run_role(args.role.strip().lower(), data_dir, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
