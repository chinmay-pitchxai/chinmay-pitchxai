"""Vobiz hangup hints, REST DELETE, and mixed outbound teardown."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import WebSocket
from loguru import logger


async def vobiz_send_hangup(ws: WebSocket) -> None:
    try:
        await ws.send_text(json.dumps({"event": "hangup"}))
    except Exception:
        pass


async def vobiz_rest_hangup(call_uuid: str, auth_id: str, auth_token: str) -> bool:
    if not call_uuid or not auth_id or not auth_token:
        return False
    url = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(url, headers=headers)
        if r.status_code < 400:
            logger.info("Vobiz REST hangup OK (call_uuid={}, status={})", call_uuid, r.status_code)
            return True
        logger.warning(
            "Vobiz REST hangup non-2xx (call_uuid={}, status={}, body={!r})",
            call_uuid,
            r.status_code,
            (r.text or "")[:200],
        )
        return False
    except Exception as exc:
        logger.warning("Vobiz REST hangup failed (call_uuid={}): {}", call_uuid, exc)
        return False


async def terminate_call(
    ws: WebSocket,
    *,
    call_uuid: str,
    auth_id: str,
    auth_token: str,
    drain_seconds: float = 0.9,
) -> None:
    try:
        await asyncio.sleep(max(0.0, drain_seconds))
    except asyncio.CancelledError:
        pass
    await vobiz_send_hangup(ws)
    await vobiz_rest_hangup(call_uuid, auth_id, auth_token)
    try:
        await ws.close()
    except Exception:
        pass


async def vobiz_send_clear_audio(ws: WebSocket, *, stream_id: str = "") -> None:
    try:
        msg: dict[str, str] = {"event": "clearAudio"}
        if stream_id:
            msg["streamId"] = stream_id
        await ws.send_text(json.dumps(msg))
    except Exception:
        pass
