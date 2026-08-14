"""Weekly Audio Zip Archive Worker.

Runs every Monday at 6:00 AM (or when triggered manually) to package all call audio recordings
from the past week into a dated ZIP archive under `backend/data/exports/`.
"""

from __future__ import annotations

import datetime
import os
import shutil
import zipfile
from pathlib import Path

from loguru import logger


def run_weekly_zip_archive(output_dir: str | Path | None = None) -> Path | None:
    backend_dir = Path(__file__).resolve().parent.parent
    data_dir = backend_dir / "data"
    recordings_dir = data_dir / "call_recordings"
    
    if not recordings_dir.is_dir():
        logger.warning(f"Recordings directory {recordings_dir} does not exist. Nothing to archive.")
        return None
        
    export_dir = Path(output_dir) if output_dir else (data_dir / "exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    
    today_str = datetime.date.today().isoformat()
    zip_filename = f"leads_audio_{today_str}.zip"
    zip_path = export_dir / zip_filename
    
    one_week_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    
    archived_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in recordings_dir.glob("*.*"):
            if file_path.suffix.lower() in (".wav", ".mp3", ".m4a", ".pcm"):
                mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime)
                if mtime >= one_week_ago:
                    zipf.write(file_path, arcname=file_path.name)
                    archived_count += 1
                    
    logger.info(f"Successfully created weekly archive {zip_path} containing {archived_count} recordings.")
    return zip_path


if __name__ == "__main__":
    archive_file = run_weekly_zip_archive()
    if archive_file:
        print(f"Archive generated at: {archive_file}")
    else:
        print("No recordings found to archive.")
