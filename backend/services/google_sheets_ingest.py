"""Poll three private broker Sheets and feed the isolated Sandbox 1.2 queue."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx
from loguru import logger

from config import settings
from services.digital_excel_ingest import ingest_digital_rows


def spreadsheet_id(url: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url or "")
    return match.group(1) if match else ""


async def access_token() -> str:
    if not settings.google_sheets_refresh_token:
        return ""
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": settings.google_sheets_oauth_client_id,
            "client_secret": settings.google_sheets_oauth_client_secret,
            "refresh_token": settings.google_sheets_refresh_token,
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        return response.json()["access_token"]


async def fetch_rows(sheet_url: str, token: str) -> list[dict]:
    sid = spreadsheet_id(sheet_url)
    if not sid:
        return []
    endpoint = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/'Digital Leads'!A2:F10000"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
    rows = []
    for values in response.json().get("values", []):
        padded = list(values) + [""] * (6 - len(values))
        rows.append({"name": padded[0], "phone": padded[1], "email": padded[2],
                     "source": padded[3], "notes": padded[4]})
    return rows


async def google_sheets_broker_1_watcher() -> None:
    """Watch Broker 1's Google Sheet for new leads."""
    await _google_sheets_poll_loop("broker_1")


async def google_sheets_broker_2_watcher() -> None:
    """Watch Broker 2's Google Sheet for new leads."""
    await _google_sheets_poll_loop("broker_2")


async def google_sheets_broker_3_watcher() -> None:
    """Watch Broker 3's Google Sheet for new leads."""
    await _google_sheets_poll_loop("broker_3")


async def _google_sheets_poll_loop(broker_id: str) -> None:
    """Core polling loop for a single broker."""
    url_map = {
        "broker_1": settings.digital_broker_1_sheet_url,
        "broker_2": settings.digital_broker_2_sheet_url,
        "broker_3": settings.digital_broker_3_sheet_url,
    }
    url = url_map.get(broker_id, "")
    if not url:
        logger.info("[{}] No Google Sheet URL configured — watcher idle", broker_id)
        return
    logger.info("[{}] Google Sheets watcher started", broker_id)
    while True:
        try:
            token = await access_token()
            if not token:
                logger.warning("[{}] awaiting GOOGLE_SHEETS_REFRESH_TOKEN", broker_id)
            else:
                rows = await fetch_rows(url, token)
                result = await asyncio.to_thread(ingest_digital_rows, rows, broker_id=broker_id)
                if result.get("saved") or result.get("queued"):
                    logger.info("[GOOGLE-SHEETS] Change detected in broker {}, triggering ingestion result={}", broker_id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[{}] Google Sheets synchronization failed", broker_id)
        await asyncio.sleep(max(3.0, settings.google_sheets_poll_seconds))


async def google_sheets_watcher() -> None:
    """Legacy fallback: poll all 3 broker sheets in a single loop (for backwards compat)."""
    feeds = {
        "broker_1": settings.digital_broker_1_sheet_url,
        "broker_2": settings.digital_broker_2_sheet_url,
        "broker_3": settings.digital_broker_3_sheet_url,
    }
    while True:
        try:
            token = await access_token()
            if not token:
                logger.warning("Google Sheets poller awaiting GOOGLE_SHEETS_REFRESH_TOKEN")
            else:
                for broker_id, url in feeds.items():
                    if url:
                        rows = await fetch_rows(url, token)
                        result = await asyncio.to_thread(ingest_digital_rows, rows, broker_id=broker_id)
                        if result.get("saved") or result.get("queued"):
                            logger.info("[GOOGLE-SHEETS] Change detected in broker {}, triggering ingestion result={}", broker_id, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Google Sheets synchronization failed")
        await asyncio.sleep(max(3.0, settings.google_sheets_poll_seconds))
