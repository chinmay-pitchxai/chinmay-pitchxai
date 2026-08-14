#!/usr/bin/env python3
"""Morning preflight: DB migrations, health, proof/lifecycle sanity."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from core.storage import init_db  # noqa: E402
from core.site_visit_lifecycle import compute_site_visit_callback_times  # noqa: E402
from datetime import datetime
from services.callback_time import zoneinfo_safe
from config import settings


def _check_lifecycle_times() -> None:
    tz = zoneinfo_safe(settings.transcript_callback_tz)
    # Saturday visit at 11 AM
    sv = datetime(2026, 7, 25, 11, 0, tzinfo=tz)
    eve, day = compute_site_visit_callback_times(sv, tz)
    assert eve.hour == 10 and eve.date().weekday() == 4, f"eve wrong: {eve}"
    assert day.hour == 9 and day.date() == sv.date(), f"day wrong: {day}"
    print("OK lifecycle times: Fri 10AM eve, Sat 9AM day-of")


async def _main() -> int:
    init_db()
    print("OK init_db / migrations")
    _check_lifecycle_times()
    try:
        import httpx

        base = (settings.public_base_url or "").rstrip("/")
        if base:
            r = httpx.get(f"{base}/health", timeout=15)
            print(f"OK health {r.status_code}")
        else:
            print("SKIP health — no PUBLIC_BASE_URL")
    except Exception as exc:
        print(f"WARN health check: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
