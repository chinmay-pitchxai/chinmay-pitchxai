"""Copy existing session recordings into Technopolis_Call_Recordings/{date}/{name_phone}/ for outcome leads."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from services.call_recording import (  # noqa: E402
    _recording_roots,
    _safe_folder_name,
    _safe_stem,
    recording_duration_sec,
    resolve_session_recording_path,
)


def _day_from_log_id(log_id: str) -> str:
    m = re.search(r"(20\d{6})", log_id or "")
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def main() -> None:
    db = Path(settings.db_path if hasattr(settings, "db_path") else BACKEND / "data" / "vernika.db")
    if not db.is_file():
        db = BACKEND / "data" / "vernika.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT l.id, l.name, l.phone, l.role, l.status,
               COALESCE(NULLIF(TRIM(l._log_id), ''), ca.log_id) AS log_id
        FROM leads l
        LEFT JOIN call_attempts ca ON ca.lead_id = l.id AND COALESCE(TRIM(ca.log_id), '') != ''
        WHERE l.status IN ('site_visit', 'site_visited', 'interested')
           OR lower(json_extract(l.analysis, '$.disposition')) LIKE '%site visit%'
           OR (
                lower(json_extract(l.analysis, '$.disposition')) LIKE '%interested%'
                AND lower(json_extract(l.analysis, '$.disposition')) NOT LIKE '%not interested%'
              )
        GROUP BY l.id
        """
    ).fetchall()
    _, archive_base = _recording_roots(None)
    copied = 0
    for row in rows:
        log_id = str(row["log_id"] or "").strip()
        if not log_id:
            continue
        src = resolve_session_recording_path(log_id)
        if not src or not src.is_file() or src.stat().st_size <= 44:
            print(f"SKIP {row['id']} {row['name']}: no recording for {log_id}")
            continue
        day_dir = archive_base / _day_from_log_id(log_id)
        folder = day_dir / _safe_folder_name(str(row["name"] or ""), str(row["phone"] or ""))
        folder.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(log_id)
        dest = folder / f"{stem}_mixed.wav"
        if not dest.is_file():
            dest.write_bytes(src.read_bytes())
        meta_path = folder / f"{stem}_meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "session_id": log_id,
                    "lead_id": row["id"],
                    "lead_name": row["name"],
                    "phone": row["phone"],
                    "role": row["role"],
                    "status": row["status"],
                    "source": str(src),
                    "archive": str(dest),
                    "duration_sec": recording_duration_sec(log_id),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        copied += 1
        print(f"OK {row['id']} {row['name']} -> {dest}")
    conn.close()
    print(f"Archived {copied} recording(s) under {archive_base}")


if __name__ == "__main__":
    main()
