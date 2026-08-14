"""Browser-only voice test WebSockets (Gemini Live); not used for PSTN / Vobiz."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

router = APIRouter(tags=["voice"])


@router.websocket("/ws/web-demo")
async def websocket_web_demo(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text('{"error":"Web demo not available"}')
    try:
        await websocket.close()
    except WebSocketDisconnect:
        pass


@router.websocket("/ws/voice-test")
async def websocket_voice_test(websocket: WebSocket) -> None:
    await handle_browser_voice_ws(websocket)
