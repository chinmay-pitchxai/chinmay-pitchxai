"""
Tata Tele Smartflo Voice Streaming — WebSocket bridge to Gemini Live.

Protocol: Smartflo sends base64 μ-law (G.711) 8kHz audio over WebSocket.
We decode → resample to 24kHz → send to Gemini Live → receive 24kHz → resample to 8kHz → encode μ-law → send back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter(tags=["tata"])


def _ulaw_decode(sample: int) -> int:
    sample = ~sample & 0xFF
    sign = 1 if sample & 0x80 else -1
    exponent = (sample >> 4) & 0x07
    mantissa = sample & 0x0F
    sample = (mantissa << (exponent + 3)) + (0x80 << exponent) - 0x84
    return sign * sample


def _ulaw_encode(pcm: int) -> int:
    sign = 0
    if pcm < 0:
        sign = 0x80
        pcm = -pcm
    pcm = min(pcm, 32767)
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (pcm & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _mulaw_to_pcm16k(b64_audio: str) -> bytes:
    raw = base64.b64decode(b64_audio)
    pcm_8k = b""
    for byte in raw:
        pcm_8k += struct.pack("!h", _ulaw_decode(byte))
    return _resample_8k_to_16k(pcm_8k)


def _pcm16k_to_mulaw_b64(pcm_16k: bytes) -> str:
    pcm_8k = _resample_16k_to_8k(pcm_16k)
    mulaw = b""
    for i in range(0, len(pcm_8k), 2):
        if i + 2 <= len(pcm_8k):
            sample = struct.unpack("!h", pcm_8k[i:i + 2])[0]
            mulaw += bytes([_ulaw_encode(sample)])
    return base64.b64encode(mulaw).decode()


def _pcm16k_to_24k(pcm_16k: bytes) -> bytes:
    samples = []
    for i in range(0, len(pcm_16k) - 1, 2):
        samples.append(struct.unpack("!h", pcm_16k[i:i + 2])[0])
    if not samples:
        return pcm_16k
    out = b""
    ratio = 24000 / 16000
    for i in range(int(len(samples) * ratio)):
        src_idx = i / ratio
        idx = int(src_idx)
        if idx >= len(samples) - 1:
            idx = len(samples) - 2
        frac = src_idx - idx
        val = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        out += struct.pack("!h", max(-32768, min(32767, val)))
    return out


def _pcm24k_to_16k(pcm_24k: bytes) -> bytes:
    samples = []
    for i in range(0, len(pcm_24k) - 1, 2):
        samples.append(struct.unpack("!h", pcm_24k[i:i + 2])[0])
    if not samples:
        return pcm_24k
    out = b""
    ratio = 16000 / 24000
    for i in range(int(len(samples) * ratio)):
        src_idx = i / ratio
        idx = int(src_idx)
        if idx >= len(samples) - 1:
            idx = len(samples) - 2
        frac = src_idx - idx
        val = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        out += struct.pack("!h", max(-32768, min(32767, val)))
    return out


def _resample_8k_to_16k(pcm_8k: bytes) -> bytes:
    samples = []
    for i in range(0, len(pcm_8k) - 1, 2):
        s1 = struct.unpack("!h", pcm_8k[i:i + 2])[0]
        s2 = struct.unpack("!h", pcm_8k[i + 2:i + 4])[0] if i + 4 <= len(pcm_8k) else s1
        samples.append(s1)
        samples.append((s1 + s2) >> 1)
    if not samples and len(pcm_8k) >= 2:
        s = struct.unpack("!h", pcm_8k[:2])[0]
        samples = [s, s]
    return b"".join(struct.pack("!h", s) for s in samples)


def _resample_16k_to_8k(pcm_16k: bytes) -> bytes:
    samples = []
    for i in range(0, len(pcm_16k) - 1, 4):
        samples.append(struct.unpack("!h", pcm_16k[i:i + 2])[0])
    if not samples and len(pcm_16k) >= 2:
        samples = [struct.unpack("!h", pcm_16k[:2])[0]]
    return b"".join(struct.pack("!h", s) for s in samples)


@router.api_route("/tata/wss", methods=["GET", "POST"])
async def tata_dynamic_endpoint():
    """Dynamic endpoint — returns the actual wss:// URL to Smartflo."""
    return {"success": True, "wss_url": "wss://srv1782911.hstgr.cloud/ws/vobiz?provider=tata"}


async def handle_tata_ws(websocket: WebSocket):
    """Handle bi-directional audio streaming from Tata Tele Smartflo."""
    await websocket.accept()
    logger.info("Tata Tele WS connected")

    stream_sid = None
    call_direction = None
    gemini_ws = None
    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    gemini_connected = asyncio.Event()

    try:
        api_key = os.getenv("GEMINI_API_KEY", "")
        from config import settings
        if not api_key and hasattr(settings, "gemini_api_key"):
            api_key = settings.gemini_api_key

        if not api_key:
            logger.error("GEMINI_API_KEY not set — cannot bridge to AI")
            await websocket.close()
            return

        gemini_url = (
            f"wss://generativelanguage.googleapis.com/ws/"
            f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={api_key}"
        )

        voice = settings.gemini_live_voice if hasattr(settings, "gemini_live_voice") else "Leda"
        model = settings.gemini_live_model if hasattr(settings, "gemini_live_model") else "models/gemini-3.1-flash-live-preview"
        system_instruction = settings.gemini_live_system_instruction if hasattr(settings, "gemini_live_system_instruction") else ""

        gemini_ws = await websockets.connect(gemini_url, ping_interval=20, ping_timeout=20)

        setup_msg = {
            "setup": {
                "model": model,
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
                    },
                },
            }
        }
        if system_instruction:
            setup_msg["setup"]["system_instruction"] = {"parts": [{"text": system_instruction}]}

        await gemini_ws.send(json.dumps(setup_msg))
        first_resp = await asyncio.wait_for(gemini_ws.recv(), timeout=10)
        first_data = json.loads(first_resp)
        if "setupComplete" in first_data:
            logger.info("Tata Tele: Gemini Live connected — voice={}", voice)
        else:
            logger.warning("Tata Tele: Gemini setup: {}", json.dumps(first_data)[:200])

        gemini_connected.set()

        async def _gemini_to_smartflo():
            """Receive audio from Gemini → encode μ-law → send to Smartflo."""
            while True:
                try:
                    raw = await asyncio.wait_for(gemini_ws.recv(), timeout=1.0)
                    data = json.loads(raw)
                    if "serverContent" in data:
                        sc = data["serverContent"]
                        if "modelTurn" in sc:
                            for part in sc["modelTurn"].get("parts", []):
                                if "inlineData" in part:
                                    b64 = part["inlineData"].get("data", "")
                                    mime = part["inlineData"].get("mimeType", "")
                                    if "pcm" in mime or "audio" in mime:
                                        pcm_24k = base64.b64decode(b64)
                                        pcm_16k = _pcm24k_to_16k(pcm_24k)
                                        mulaw_b64 = _pcm16k_to_mulaw_b64(pcm_16k)
                                        msg = json.dumps({
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {
                                                "payload": mulaw_b64,
                                                "chunk": 1,
                                            },
                                        })
                                        await websocket.send_text(msg)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug("Gemini→Smartflo error: {}", e)
                    break

        async def _smartflo_to_gemini():
            """Receive μ-law from Smartflo → decode → send to Gemini Live."""
            nonlocal stream_sid
            while True:
                try:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    event = msg.get("event", "")

                    if event == "connected":
                        logger.info("Tata Tele WS: connected event")

                    elif event == "start":
                        stream_sid = msg.get("streamSid") or msg.get("stream_sid")
                        start_data = msg.get("start", {})
                        call_direction = start_data.get("direction", "")
                        media_fmt = start_data.get("mediaFormat", {})
                        logger.info(
                            "Tata Tele WS: start — streamSid={} direction={} encoding={} sampleRate={}",
                            stream_sid, call_direction,
                            media_fmt.get("encoding", "?"), media_fmt.get("sampleRate", "?"),
                        )

                    elif event == "media":
                        payload = msg.get("media", {}).get("payload", "")
                        if payload:
                            pcm_16k = _mulaw_to_pcm16k(payload)
                            pcm_24k = _pcm16k_to_24k(pcm_16k)
                            b64_audio = base64.b64encode(pcm_24k).decode()
                            gem_msg = {
                                "realtimeInput": {
                                    "mediaChunks": [{
                                        "mimeType": "audio/pcm;rate=24000",
                                        "data": b64_audio,
                                    }]
                                }
                            }
                            await gemini_ws.send(json.dumps(gem_msg))

                    elif event == "stop":
                        logger.info("Tata Tele WS: stop event")
                        break

                    elif event == "mark":
                        pass

                    elif event == "clear":
                        pass

                    else:
                        logger.debug("Tata Tele WS: unknown event '{}'", event)

                except WebSocketDisconnect:
                    logger.info("Tata Tele WS: disconnected")
                    break
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug("Smartflo→Gemini error: {}", e)
                    break

        smartflo_task = asyncio.create_task(_smartflo_to_gemini())
        gemini_task = asyncio.create_task(_gemini_to_smartflo())

        done, pending = await asyncio.wait(
            [smartflo_task, gemini_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()

    except asyncio.TimeoutError:
        logger.error("Tata Tele: Gemini Live connection timed out")
    except Exception as e:
        logger.error("Tata Tele WS error: {}", e)
    finally:
        if gemini_ws and gemini_ws.open:
            await gemini_ws.close()
        logger.info("Tata Tele WS session ended")
