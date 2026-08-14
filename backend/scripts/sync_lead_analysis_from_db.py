#!/usr/bin/env python3
"""Merge ``analysis`` (and optional ``status``) from a source DB into the live DB by phone+role.

Use when a restore was followed by a bulk re-analyze that overwrote QA dispositions with generic
``Answered``, while the agent copy still has Interested / Not Interested labels.

Usage:
  python scripts/sync_lead_analysis_from_db.py --role sales_1 \\
    --source /root/technopolis/agent/data/vernika.db \\
    --dest /root/technopolis/backend/data/vernika.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _norm_phone(p: object) -> str:
    return "".join(c for c in str(p or "") if c.isdigit())[-10:]


def _parse_analysis(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _disposition_from_row(analysis: dict, status: str) -> str:
    d = str(analysis.get("disposition") or "").strip()
    if d in ("Interested", "Not Interested", "Call Later", "Busy", "Callback", "Wrong Number"):
        return d
    s = str(status or "").strip().lower()
    if s == "not_interested":
        return "Not Interested"
    return d


def sync_role(source: Path, dest: Path, role: str, *, apply: bool, sync_status: bool) -> dict:
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    by_phone: dict[str, dict] = {}
    for row in src.execute(
        "SELECT phone, analysis, status FROM leads WHERE role = ?", (role,)
    ):
        phone = _norm_phone(row["phone"])
        if not phone:
            continue
        aj = _parse_analysis(row["analysis"])
        disp = _disposition_from_row(aj, row["status"] or "")
        if not disp and not aj:
            continue
        by_phone[phone] = {
            "analysis": aj,
            "status": row["status"],
            "disposition": disp,
        }
    src.close()

    dst = sqlite3.connect(dest)
    dst.row_factory = sqlite3.Row
    updated = 0
    skipped = 0
    for row in dst.execute(
        "SELECT id, phone, analysis, status FROM leads WHERE role = ?", (role,)
    ):
        phone = _norm_phone(row["phone"])
        src_row = by_phone.get(phone)
        if not src_row:
            skipped += 1
            continue
        src_aj = src_row["analysis"]
        src_disp = src_row["disposition"]
        if not src_disp or src_disp == "Answered":
            cur_aj = _parse_analysis(row["analysis"])
            cur_disp = _disposition_from_row(cur_aj, row["status"] or "")
            if cur_disp in ("Interested", "Not Interested"):
                skipped += 1
                continue
        if not src_aj:
            skipped += 1
            continue
        new_status = row["status"]
        if sync_status and src_row.get("status"):
            st = str(src_row["status"]).strip().lower()
            if st in ("completed", "failed", "not_interested", "pending", "callback_scheduled"):
                new_status = src_row["status"]
        if apply:
            dst.execute(
                "UPDATE leads SET analysis = ?, status = ? WHERE id = ? AND role = ?",
                (json.dumps(src_aj, ensure_ascii=False), new_status, row["id"], role),
            )
        updated += 1
    if apply:
        dst.commit()
    dst.close()
    return {
        "role": role,
        "source_phones": len(by_phone),
        "updated": updated,
        "skipped": skipped,
        "mode": "apply" if apply else "dry-run",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--dest", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--no-status",
        action="store_true",
        help="Only sync analysis JSON, leave lead status unchanged",
    )
    args = parser.parse_args()
    role = args.role.strip().lower()
    source = Path(args.source).resolve()
    dest = (
        Path(args.dest).resolve()
        if args.dest
        else Path(__file__).resolve().parents[1] / "data" / "vernika.db"
    )
    if not source.is_file():
        print(f"Source missing: {source}", file=sys.stderr)
        return 1
    if not dest.is_file():
        print(f"Dest missing: {dest}", file=sys.stderr)
        return 1
    out = sync_role(
        source, dest, role, apply=args.apply, sync_status=not args.no_status
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
