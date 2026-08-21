"""
SIP Bridge Server for Tata Tele Smartflo.

Receives SIP calls from Tata Tele, bridges audio to Gemini Live AI.
Architecture: Tata Tele → SIP/RTP → Gemini Live WebSocket → AI response → RTP → Tata Tele
"""
import asyncio
import base64
import json
import os
import struct
import time
import websockets
from typing import Optional

from loguru import logger

from .sip_handler import SIPServer, SIPCall
from .rtp_handler import RTPSession


async def _connect_gemini_live(api_key: str, role: str = "sales_1"):
    """Connect to Gemini Live WebSocket and return (ws, send_task)."""
    url = (
        f"wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={api_key}"
    )

    from config import settings
    system_instruction = (
        settings.gemini_live_system_instruction
        if hasattr(settings, "gemini_live_system_instruction") else ""
    )
    voice = settings.gemini_live_voice if hasattr(settings, "gemini_live_voice") else "Leda"
    model = settings.gemini_live_model if hasattr(settings, "gemini_live_model") else "models/gemini-3.1-flash-live-preview"

    ws = await websockets.connect(url, ping_interval=20, ping_timeout=20)

    setup_msg = {
        "setup": {
            "model": model,
            "generation_config": {
                "response_modalities": ["AUDIO"],
                "speech_config": {
                    "voice_config": {"prebuilt_voice_config": {"voice_name": voice}},
                },
            },
            "system_instruction": {"parts": [{"text": system_instruction}]} if system_instruction else {},
        }
    }
    await ws.send(json.dumps(setup_msg))

    first_resp = await asyncio.wait_for(ws.recv(), timeout=10)
    first_data = json.loads(first_resp)
    if "setupComplete" not in first_data:
        logger.warning("Gemini setup response: {}", json.dumps(first_data)[:200])
    else:
        logger.info("Gemini Live connected — voice={} model={}", voice, model)

    return ws


def _pcm16k_to_24k(pcm_16k: bytes) -> bytes:
    """Convert 16kHz PCM to 24kHz using linear interpolation."""
    samples_16k = struct.unpack(f"<{len(pcm_16k)//2}h", pcm_16k)
    ratio = 24000 / 16000
    num_out = int(len(samples_16k) * ratio)
    samples_24k = []
    for i in range(num_out):
        pos = i / ratio
        idx = int(pos)
        frac = pos - idx
        if idx + 1 < len(samples_16k):
            val = samples_16k[idx] * (1 - frac) + samples_16k[idx + 1] * frac
        else:
            val = samples_16k[idx]
        samples_24k.append(int(max(-32768, min(32767, val))))
    return struct.pack(f"<{len(samples_24k)}h", *samples_24k)


def _pcm24k_to_16k(pcm_24k: bytes) -> bytes:
    """Resample 24kHz 16-bit PCM to 16kHz."""
    samples_out = []
    for i in range(0, len(pcm_24k) - 3, 6):
        s = struct.unpack("!h", pcm_24k[i:i + 2])[0]
        samples_out.append(s)
    if not samples_out and len(pcm_24k) >= 2:
        samples_out = [struct.unpack("!h", pcm_24k[:2])[0]]
    return b"".join(struct.pack("!h", s) for s in samples_out)


async def _handle_sip_call(call: SIPCall):
    """Handle a single SIP call — bridge RTP audio to Gemini Live."""
    logger.info("Handling SIP call {} → {}:{}", call.call_id[:12], call.remote_ip, call.remote_port)

    from config import settings
    api_key = os.getenv("GEMINI_API_KEY", settings.gemini_api_key if hasattr(settings, "gemini_api_key") else "")
    if not api_key:
        logger.error("GEMINI_API_KEY not set — cannot bridge to AI")
        return

    gemini_ws = None
    rtp = None
    try:
        gemini_ws = await _connect_gemini_live(api_key)

        rtp = RTPSession(
            remote_ip=call.remote_ip,
            remote_port=call.remote_port,
            local_port=call.local_rtp_port,
            codec="PCMU",
            sample_rate=8000,
        )
        await rtp.start()

        await asyncio.sleep(0.5)

        async def _send_to_gemini():
            """Receive PCM from RTP and send to Gemini Live."""
            audio_queue = asyncio.Queue()

            def on_audio(pcm_16k: bytes):
                try:
                    audio_queue.put_nowait(pcm_16k)
                except asyncio.QueueFull:
                    pass

            rtp.on_audio_from_call = on_audio

            while rtp.active and gemini_ws.open:
                try:
                    pcm_16k = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
                    pcm_24k = _pcm16k_to_24k(pcm_16k)
                    b64_audio = base64.b64encode(pcm_24k).decode()
                    msg = {
                        "realtimeInput": {
                            "mediaChunks": [{
                                "mimeType": "audio/pcm;rate=24000",
                                "data": b64_audio,
                            }]
                        }
                    }
                    await gemini_ws.send(json.dumps(msg))
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    logger.debug("Gemini send error: {}", e)
                    break

        async def _receive_from_gemini():
            """Receive audio from Gemini Live and send to RTP."""
            while rtp.active and gemini_ws.open:
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
                                        await rtp.send_audio(pcm_16k)

                        if sc.get("turnComplete"):
                            pass

                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    logger.debug("Gemini recv error: {}", e)
                    break

        await asyncio.gather(_send_to_gemini(), _receive_from_gemini())

    except asyncio.TimeoutError:
        logger.error("Gemini Live connection timed out for call {}", call.call_id[:12])
    except Exception as e:
        logger.error("SIP call handler error: {}", e)
    finally:
        if rtp:
            await rtp.stop()
        if gemini_ws and gemini_ws.open:
            await gemini_ws.close()
        logger.info("SIP call {} handler ended", call.call_id[:12])


class SIPBridgeServer:
    def __init__(self):
        self._sip: Optional[SIPServer] = None
        self._active_tasks: dict[str, asyncio.Task] = {}

    async def start(self):
        self._sip = SIPServer(
            on_call_start=self._on_call_start,
            on_call_answer=self._on_call_answer,
            on_call_end=self._on_call_end,
        )
        await self._sip.start()
        logger.info("SIP Bridge Server started")

    async def stop(self):
        if self._sip:
            await self._sip.stop()
        for task in self._active_tasks.values():
            task.cancel()

    def _on_call_start(self, call: SIPCall):
        logger.info("SIP call started: {} from {}:{}", call.call_id[:12], call.remote_ip, call.remote_port)

    def _on_call_answer(self, call: SIPCall):
        logger.info("SIP call answered: {} — launching Gemini bridge", call.call_id[:12])
        task = asyncio.create_task(_handle_sip_call(call))
        self._active_tasks[call.call_id] = task
        task.add_done_callback(lambda t, cid=call.call_id: self._active_tasks.pop(cid, None))

    def _on_call_end(self, call: SIPCall):
        logger.info("SIP call ended: {}", call.call_id[:12])
        task = self._active_tasks.pop(call.call_id, None)
        if task and not task.done():
            task.cancel()
