"""In-memory live transcript + supervisor stream stub for the campaign dashboard."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Optional

from fastapi import WebSocket
from loguru import logger

_MAX_SNAPSHOT: int = 200
_MAX_BACKLOG_PER_CALL: int = 400

_transcripts: dict[str, deque[dict[str, Any]]] = {}
_subscribers: set[WebSocket] = set()
_lock = asyncio.Lock()
_active_camp_id: Optional[str] = None


def set_active_campaign_call(camp_id: Optional[str]) -> None:
    global _active_camp_id
    _active_camp_id = (camp_id or "").strip() or None


def get_active_campaign_call() -> Optional[str]:
    return _active_camp_id


def _ensure_deque(camp_id: str) -> deque[dict[str, Any]]:
    d = _transcripts.get(camp_id)
    if d is None:
        d = deque(maxlen=_MAX_BACKLOG_PER_CALL)
        _transcripts[camp_id] = d
    return d


def push_transcript(camp_id: Optional[str], role: str, text: str) -> None:
    if not camp_id or not (text or "").strip():
        return
    line = {
        "role": role,
        "text": (text or "").strip(),
        "ts": time.time(),
    }
    d = _ensure_deque(camp_id)
    d.append(line)
    set_active_campaign_call(camp_id)
    payload = json.dumps({"type": "transcript", "call_id": camp_id, **line})
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for ws in list(_subscribers):
        loop.create_task(_send_safe(ws, payload))


async def _send_safe(ws: WebSocket, text: str) -> None:
    try:
        await ws.send_text(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Campaign live subscriber drop: {}", exc)
        _subscribers.discard(ws)


async def register_transcript_subscriber(websocket: WebSocket) -> None:
    await websocket.accept()
    async with _lock:
        _subscribers.add(websocket)
    try:
        snap: list[dict[str, Any]] = []
        if _active_camp_id and _active_camp_id in _transcripts:
            snap = list(_transcripts[_active_camp_id])[-_MAX_SNAPSHOT:]
        cap = {
            "type": "hello",
            "active_call_id": _active_camp_id,
            "transcript": snap,
        }
        await websocket.send_text(json.dumps(cap))
    except Exception:
        _subscribers.discard(websocket)
        return

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong", "t": msg.get("t")})
                )
    except Exception:
        pass
    finally:
        _subscribers.discard(websocket)


def clear_transcript_session(camp_id: str) -> None:
    if camp_id in _transcripts:
        del _transcripts[camp_id]
    if _active_camp_id == camp_id:
        set_active_campaign_call(None)

def purge_old_transcripts(max_calls: int = 100):
    """Prevent memory leaks by purging old call transcripts."""
    if len(_transcripts) > max_calls:
        # Sort by keys (ids usually have timestamp/uuid) or just pop first N
        to_remove = list(_transcripts.keys())[:-max_calls]
        for k in to_remove:
            if k != _active_camp_id:
                _transcripts.pop(k, None)


async def register_supervisor_stream(websocket: WebSocket) -> None:
    """Accept supervisor mic chunks (stub; future barge-in to Gemini)."""
    await websocket.accept()
    n_chunks = 0
    n_bytes = 0
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "supervisor",
                    "ok": True,
                    "hint": "Mic audio received; barge-in mixing to live call is not enabled yet.",
                }
            )
        )
        while True:
            m = await websocket.receive()
            t = m.get("type")
            if t == "websocket.disconnect":
                break
            if t == "websocket.receive":
                b = m.get("bytes")
                if b:
                    n_chunks += 1
                    n_bytes += len(b)
                    if n_chunks % 200 == 0:
                        try:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "supervisor_ack",
                                        "chunks": n_chunks,
                                        "bytes": n_bytes,
                                    }
                                )
                            )
                        except Exception:
                            break
    except Exception:
        pass
    try:
        await websocket.close()
    except Exception:
        pass
