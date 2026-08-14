#!/usr/bin/env python3
"""Copy all leads for a role from a source SQLite DB into the live Technopolis DB.

Usage:
  python scripts/restore_leads_from_db.py --role sales_1 \\
    --source /root/technopolis/agent/data/vernika.db \\
    --dest /root/technopolis/backend/data/vernika.db --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LEAD_COLS = (
    "id",
    "role",
    "name",
    "phone",
    "email",
    "company",
    "details",
    "extra",
    "status",
    "analysis",
    "start_time",
    "error",
    "_log_id",
    "_call_id",
    "created_at",
    "updated_at",
)


def _ensure_extra_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()}
    if "extra" not in cols:
        try:
            conn.execute("ALTER TABLE leads ADD COLUMN extra TEXT DEFAULT '{}'")
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _row_to_insert(row: sqlite3.Row, role: str) -> dict:
    d = {k: row[k] for k in row.keys() if k in LEAD_COLS}
    d["role"] = role
    if "extra" not in d or d["extra"] is None:
        d["extra"] = "{}"
    return d


def restore_role(source: Path, dest: Path, role: str, *, apply: bool) -> dict:
    src = sqlite3.connect(source)
    src.row_factory = sqlite3.Row
    n_src = src.execute(
        "SELECT COUNT(*) FROM leads WHERE role = ?", (role,)
    ).fetchone()[0]

    if not apply:
        return {"role": role, "source_rows": n_src, "mode": "dry-run"}

    backup = dest.parent / f"vernika-pre-restore-{role}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
    shutil.copy2(dest, backup)

    dst = sqlite3.connect(dest)
    _ensure_extra_column(dst)
    deleted = dst.execute("DELETE FROM leads WHERE role = ?", (role,)).rowcount

    rows = src.execute("SELECT * FROM leads WHERE role = ? ORDER BY id", (role,)).fetchall()
    placeholders = ", ".join("?" for _ in LEAD_COLS)
    col_list = ", ".join(LEAD_COLS)
    sql = f"INSERT INTO leads ({col_list}) VALUES ({placeholders})"

    inserted = 0
    for row in rows:
        d = _row_to_insert(row, role)
        vals = [d.get(c) for c in LEAD_COLS]
        dst.execute(sql, vals)
        inserted += 1

    dst.commit()
    dst.execute("DELETE FROM sqlite_sequence WHERE name='leads'")
    max_id = dst.execute("SELECT MAX(id) FROM leads").fetchone()[0]
    if max_id:
        dst.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('leads', ?)", (max_id,))
    dst.commit()
    dst.close()
    src.close()

    return {
        "role": role,
        "source_rows": n_src,
        "deleted": deleted,
        "inserted": inserted,
        "backup": str(backup),
        "mode": "apply",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, help="sales_1")
    parser.add_argument("--source", required=True, help="Source vernika.db path")
    parser.add_argument(
        "--dest",
        default="",
        help="Destination DB (default: backend/data/vernika.db next to script)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    role = args.role.strip().lower()
    source = Path(args.source).resolve()
    dest = (
        Path(args.dest).resolve()
        if args.dest
        else Path(__file__).resolve().parent.parent / "data" / "vernika.db"
    )

    if not source.is_file():
        print(f"Source not found: {source}", file=sys.stderr)
        return 1
    if not dest.is_file():
        print(f"Destination not found: {dest}", file=sys.stderr)
        return 1

    result = restore_role(source, dest, role, apply=args.apply)
    for k, v in result.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
