#!/usr/bin/env python3
"""Export leads for a role to CSV (optional disposition filter)."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.campaign_payload import enrich_lead_for_console, effective_disposition_console
from core.storage import init_db, _get_leads_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="sales_1")
    parser.add_argument(
        "--filter",
        default="all",
        help="all | Interested | Not Interested | Called | Pending",
    )
    parser.add_argument("--db", default="", help="Path to vernika.db")
    parser.add_argument("-o", "--output", required=True, help="Output CSV path")
    args = parser.parse_args()

    role = args.role.strip().lower()
    db_path = Path(args.db) if args.db else BACKEND_DIR / "data" / "vernika.db"
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    init_db(str(db_path.parent))
    rows = _get_leads_sync(role, limit=50_000, order="activity")
    filt = (args.filter or "all").strip()

    def lead_is_called(lead: dict) -> bool:
        return bool(lead.get("start_time") or lead.get("_log_id") or lead.get("called_at_iso"))

    out_rows: list[dict] = []
    for raw in rows:
        lead = enrich_lead_for_console(dict(raw))
        dispo = effective_disposition_console(lead)
        if filt == "Interested" and dispo != "Interested":
            continue
        if filt == "Not Interested" and dispo != "Not Interested":
            continue
        if filt == "Called" and not lead_is_called(lead):
            continue
        if filt == "Pending" and (lead.get("status") or "").lower() not in ("pending", ""):
            continue
        out_rows.append(
            {
                "id": lead.get("id"),
                "name": lead.get("name") or "",
                "phone": lead.get("phone") or "",
                "email": lead.get("email") or "",
                "company": (lead.get("company") or "").replace("\n", " "),
                "status": lead.get("status") or "",
                "disposition": dispo,
                "summary": (lead.get("summary") or "").replace("\n", " "),
                "called_at_iso": lead.get("called_at_iso") or "",
                "log_id": lead.get("_log_id") or lead.get("log_id") or "",
            }
        )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "name",
        "phone",
        "email",
        "company",
        "status",
        "disposition",
        "summary",
        "called_at_iso",
        "log_id",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Wrote {len(out_rows)} rows → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
