"""Weekly Audio Zip Archive Worker.

Runs every Monday at 6:00 AM (or when triggered manually) to package the
PREVIOUS calendar week's call audio recordings into a dated ZIP archive under
`backend/data/exports/`, then prune the archived originals (plan Phase 7).

Previous calendar week = [Monday 00:00, current Monday 00:00) in Asia/Kolkata,
so a Monday 6 AM run archives exactly the prior Mon-Sun window and never the
current week's recordings.
"""

from __future__ import annotations

import datetime
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

IST = ZoneInfo("Asia/Kolkata")


def _previous_week_bounds(now: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    """Return [start, end) of the previous calendar week in Asia/Kolkata.

    ``end`` is the most recent Monday 00:00 IST; ``start`` is the Monday one
    week earlier. Both are returned as aware datetimes in IST.
    """
    now = now or datetime.datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now_ist = now.astimezone(IST)
    # Monday == 0 in Python's weekday().
    days_since_monday = now_ist.weekday()
    this_monday = (now_ist - datetime.timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prev_monday = this_monday - datetime.timedelta(days=7)
    return prev_monday, this_monday


def run_weekly_zip_archive(
    output_dir: str | Path | None = None,
    recordings_dir: str | Path | None = None,
    *,
    dry_run: bool = False,
) -> Path | None:
    backend_dir = Path(__file__).resolve().parent.parent
    data_dir = backend_dir / "data"
    rec_dir = Path(recordings_dir) if recordings_dir else (data_dir / "call_recordings")

    if not rec_dir.is_dir():
        logger.warning(f"Recordings directory {rec_dir} does not exist. Nothing to archive.")
        return None

    export_dir = Path(output_dir) if output_dir else (data_dir / "exports")
    export_dir.mkdir(parents=True, exist_ok=True)

    start, end = _previous_week_bounds()
    # ZIP name reflects the archived week (the previous calendar week).
    week_label = start.date().isoformat()
    zip_filename = f"leads_audio_{week_label}.zip"
    zip_path = export_dir / zip_filename

    if dry_run:
        logger.info(
            "DRY RUN: would archive recordings between {} and {} into {}",
            start.isoformat(), end.isoformat(), zip_path,
        )

    archived_count = 0
    to_prune: list[Path] = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in sorted(rec_dir.glob("*.*")):
            if file_path.suffix.lower() not in (".wav", ".mp3", ".m4a", ".pcm"):
                continue
            mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime, IST)
            if start <= mtime < end:
                zipf.write(file_path, arcname=file_path.name)
                archived_count += 1
                to_prune.append(file_path)

    if archived_count == 0:
        # Avoid leaving an empty zip behind — nothing to archive this week.
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("No recordings in the previous calendar week; no archive created.")
        return None

    if not dry_run:
        # Plan Phase 7: prune the archived originals after a successful zip.
        for file_path in to_prune:
            try:
                file_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("Could not prune archived recording {}: {}", file_path.name, exc)
        logger.info(
            "Created weekly archive {} with {} recordings (week {}); pruned {} originals.",
            zip_path, archived_count, week_label, len(to_prune),
        )
    else:
        logger.info("DRY RUN: {} recordings would be archived to {}", archived_count, zip_path)

    return zip_path


if __name__ == "__main__":
    archive_file = run_weekly_zip_archive()
    if archive_file:
        print(f"Archive generated at: {archive_file}")
    else:
        print("No recordings found to archive.")
