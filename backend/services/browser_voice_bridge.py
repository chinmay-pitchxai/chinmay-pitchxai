"""Browser WebSocket ↔ Gemini Live (no Vobiz). Used by /ws/web-demo and /ws/voice-test."""

from __future__ import annotations

import asyncio
import base64
import json

import websockets as ws_client
from fastapi import WebSocket
from loguru import logger
from starlette.websockets import WebSocketDisconnect

from config import settings
from core.state import get_state, normalize_console_role
from prompts.role_prompts import build_role_system_prompt

from services.vobiz_bridge.audio import pcm_resample
from services.vobiz_bridge.constants import GEMINI_OUT_SR, VOBIZ_SR
from core.gemini_auth import gemini_auth_headers, get_gemini_api_key
from services.vobiz_bridge.gemini_protocol import (
    build_live_setup,
    gemini_send_pcm_silence_kick,
    gemini_live_ws_url,
)
from services.vobiz_bridge.turn_taking_addon import apply_live_voice_turn_addon


async def handle_browser_voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    role = normalize_console_role(websocket.query_params.get("role") or "sales_1")
    api_key = get_gemini_api_key()
    if not api_key:
        logger.error("Browser voice WS: missing gemini_api_key in settings")
        await websocket.close(code=1011)
        return

    voice = settings.gemini_live_voice
    model = settings.gemini_live_model
    role_config = get_state(role)
    system_prompt = build_role_system_prompt(role, role_config, embed_rag=False)
    system_prompt = apply_live_voice_turn_addon(system_prompt)

    gemini_url = gemini_live_ws_url()
    vad_ultra = role == "sales_1"
    setup = build_live_setup(
        model=model,
        system_instruction=system_prompt,
        voice=voice,
        vad_ultra=vad_ultra,
    )

    try:
        async with ws_client.connect(
            gemini_url,
            extra_headers=gemini_auth_headers(api_key),
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
        ) as gem:
            await gem.send(json.dumps(setup))
            logger.info("Browser voice: Gemini setup sent (role={})", role)
            try:
                await gemini_send_pcm_silence_kick(gem, duration_ms=220)
                await gemini_send_pcm_silence_kick(gem, duration_ms=80)
            except Exception as exc:
                logger.warning("Browser voice: silence kick failed: {}", exc)

            pending_audio_24k = bytearray()
            flush_bytes = int(GEMINI_OUT_SR * 2 * 0.02)  # 20ms threshold (was 40ms)
            _ratecv_state: object = None

            async def flush_pending_to_browser() -> None:
                """Send any whole 20ms frames remaining in pending_audio_24k (stateful resample)."""
                nonlocal pending_audio_24k, _ratecv_state
                while len(pending_audio_24k) >= flush_bytes:
                    chunk = bytes(pending_audio_24k[:flush_bytes])
                    del pending_audio_24k[:flush_bytes]
                    pcm_16k, _ratecv_state = pcm_resample(chunk, GEMINI_OUT_SR, VOBIZ_SR, _ratecv_state)
                    out_b64 = base64.b64encode(pcm_16k).decode("ascii")
                    await websocket.send_text(json.dumps({"type": "audio", "data": out_b64}))

            async def pump_browser_to_gemini() -> None:
                while True:
                    try:
                        raw = await websocket.receive_text()
                    except WebSocketDisconnect:
                        return
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "audio":
                        continue
                    b64 = obj.get("data") or ""
                    if not b64:
                        continue
                    await gem.send(
                        json.dumps(
                            {
                                "realtimeInput": {
                                    "audio": {
                                        "data": b64,
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }
                        )
                    )

            async def pump_gemini_to_browser() -> None:
                nonlocal pending_audio_24k, _ratecv_state
                async for raw in gem:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue

                    if obj.get("error"):
                        logger.error("Browser voice: Gemini upstream error: {}", obj.get("error"))

                    sc = obj.get("serverContent") or {}

                    if sc.get("interrupted"):
                        logger.info("Browser voice: user barge-in (interrupted)")
                        pending_audio_24k.clear()
                        try:
                            await websocket.send_text(json.dumps({"type": "interrupted"}))
                        except Exception:
                            return

                    mt = sc.get("modelTurn") or {}
                    for part in mt.get("parts") or []:
                        inline = part.get("inlineData") or part.get("inline_data")
                        if not inline:
                            continue
                        mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
                        if not mime.startswith("audio/"):
                            continue
                        b64_in = inline.get("data") or ""
                        if not b64_in:
                            continue
                        try:
                            pcm = base64.b64decode(b64_in)
                        except Exception:
                            continue
                        pending_audio_24k.extend(pcm)
                        try:
                            await flush_pending_to_browser()
                        except Exception:
                            return

                    if sc.get("turnComplete") or sc.get("generationComplete"):
                        if pending_audio_24k:
                            chunk = bytes(pending_audio_24k)
                            pending_audio_24k.clear()
                            pcm_16k, _ratecv_state = pcm_resample(chunk, GEMINI_OUT_SR, VOBIZ_SR, _ratecv_state)
                            out_b64 = base64.b64encode(pcm_16k).decode("ascii")
                            try:
                                await websocket.send_text(json.dumps({"type": "audio", "data": out_b64}))
                            except Exception:
                                return

            in_task = asyncio.create_task(pump_browser_to_gemini())
            out_task = asyncio.create_task(pump_gemini_to_browser())
            _, pending = await asyncio.wait(
                {in_task, out_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        logger.info("Browser voice: client disconnected")
    except Exception as exc:
        logger.exception("Browser voice session failed: {}", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
