"""Pipecat TTS via Gemini Developer API (e.g. gemini-3.1-flash-tts-preview) — AI Studio API key."""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import re
import time
from collections.abc import AsyncGenerator
from typing import Any, Literal

import httpx
from loguru import logger

from config import settings

try:
    import audioop
    import numpy as np
    from collections.abc import AsyncGenerator
    from pipecat.frames.frames import AudioRawFrame, Frame, TTSAudioRawFrame
    from pipecat.services.tts_service import TTSService

    _PIPECAT_TTS_AVAILABLE = True
except ImportError:
    _PIPECAT_TTS_AVAILABLE = False
    TTSService = object  # type: ignore[misc,assignment]

_MIME_RATE = re.compile(r"rate=(\d+)", re.I)

# Shared client: keep-alive to generativelanguage.googleapis.com (saves TLS on each TTS).
_tts_httpx: httpx.Optional[AsyncClient] = None

# In-memory PCM for the standard Vobiz opening line (warmed at startup when possible).
_default_opening_pcm: tuple[bytes, Optional[int]] = None


async def get_gemini_tts_httpx() -> httpx.AsyncClient:
    global _tts_httpx
    if _tts_httpx is None or getattr(_tts_httpx, "is_closed", False):
        _tts_httpx = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _tts_httpx


async def aclose_gemini_tts_httpx() -> None:
    global _tts_httpx
    if _tts_httpx is not None:
        await _tts_httpx.aclose()
        _tts_httpx = None


def get_default_opening_pcm() -> tuple[bytes, Optional[int]]:
    return _default_opening_pcm


def set_default_opening_pcm(pcm: bytes, sr: int) -> None:
    global _default_opening_pcm
    _default_opening_pcm = (pcm, sr)


def _parse_pcm_rate_from_mime(mime: str) -> int:
    m = _MIME_RATE.search(mime or "")
    if m:
        return int(m.group(1))
    return 24000


def resample_pcm_numpy(pcm_bytes: bytes, in_rate: int, out_rate: int) -> bytes:
    """High-quality resampling via numpy linear interpolation.

    Much cleaner than audioop.ratecv — produces smooth, glitch-free PCM at
    any ratio, ideal for 24kHz→16kHz mobile telephony conversion.
    """
    if in_rate == out_rate:
        return pcm_bytes
    arr = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    n_in = len(arr)
    n_out = max(1, int(n_in * out_rate / in_rate))
    # Linear interpolation: map output indices back to input positions
    x_out = np.linspace(0, n_in - 1, n_out) if n_out > 1 else np.array([0.0])
    x_lo = np.floor(x_out).astype(np.int64)
    x_hi = np.minimum(x_lo + 1, n_in - 1)
    frac = x_out - x_lo.astype(np.float64)
    result = arr[x_lo] * (1.0 - frac) + arr[x_hi] * frac
    return np.round(result).astype(np.int16).tobytes()


def _tts_prompt_text(text: str, style_mode: Literal["full", "opening", "none"]) -> str:
    if style_mode == "full":
        style = (settings.gemini_tts_style_prompt or "").strip()
        return f'{style} Say: "{text}"' if style else text
    if style_mode == "opening":
        style = (settings.gemini_tts_opening_style or "").strip()
        return f'{style}Say: "{text}"' if style else f'Say: "{text}"'
    return f'Say: "{text}"'


async def gemini_synthesize_pcm(
    client: httpx.AsyncClient,
    *,
    text: str,
    voice: Optional[str] = None,
    style_mode: Literal["full", "opening", "none"] = "full",
) -> tuple[bytes, int]:
    """Call ``:generateContent`` with AUDIO modality; return mono s16le PCM and sample rate."""
    key = (settings.gemini_api_key or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY is not set")

    model = (settings.gemini_tts_model or "gemini-2.0-flash").strip()
    v = (voice or settings.gemini_tts_voice or "Leda").strip()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    # ``full`` = long style from env (Pipecat in-call TTS). ``opening`` = short style for
    # the first scripted line only — much less token work, ~2x faster first response.
    prompt_text = _tts_prompt_text(text, style_mode)
    body: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}},
            },
        },
    }
    # Gemini preview occasionally returns transient 5xx. Retry once before failing.
    r: httpx.Optional[Response] = None
    for attempt in range(2):
        r = await client.post(url, json=body, timeout=httpx.Timeout(120.0))
        if r.status_code == 200:
            break
        if r.status_code >= 500 and attempt == 0:
            await asyncio.sleep(0.35)
            continue
        break
    if r is None or r.status_code != 200:
        code = r.status_code if r is not None else "ERR"
        text_preview = (r.text[:800] if r is not None else "No response")
        raise RuntimeError(f"Gemini TTS HTTP {code}: {text_preview}")

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini TTS: no candidates: {json.dumps(data)[:1200]}")

    parts = (cands[0].get("content") or {}).get("parts") or []
    pcm_chunks: list[bytes] = []
    sr = 24000
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if not inline:
            continue
        mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
        sr = _parse_pcm_rate_from_mime(mime)
        b64 = inline.get("data") or ""
        if b64:
            pcm_chunks.append(base64.standard_b64decode(b64))

    if not pcm_chunks:
        raise RuntimeError(f"Gemini TTS: no inline audio in response: {json.dumps(data)[:1200]}")

    return b"".join(pcm_chunks), sr





if _PIPECAT_TTS_AVAILABLE:

    class GeminiHttpTTSService(TTSService):
        """Gemini 3.x Flash TTS (preview) using the Generative Language API + API key."""

        def __init__(self, **kwargs: Any):
            super().__init__(
                sample_rate=settings.pipeline_audio_out_hz,
                aggregate_sentences=True,
                **kwargs,
            )
            self._client: httpx.Optional[AsyncClient] = None

        async def start(self, frame):
            await super().start(frame)
            if self._client is None:
                self._client = httpx.AsyncClient()

        async def stop(self, frame):
            await super().stop(frame)
            if self._client is not None:
                await self._client.aclose()
                self._client = None

        async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
            text = (text or "").strip()
            if not text:
                return
            if self._client is None:
                self._client = httpx.AsyncClient()

            t0 = time.perf_counter()
            logger.info("Gemini TTS request ({} chars): {!r}", len(text), text[:120])
            try:
                pcm_in, in_sr = await gemini_synthesize_pcm(
                    self._client, text=text, voice=settings.gemini_tts_voice, style_mode="full"
                )
            except Exception as exc:
                logger.exception("Gemini TTS synthesis failed (no fallback engine): {}", exc)
                try:
                    if self._client is not None:
                        await self._client.aclose()
                except Exception:
                    pass
                self._client = None
                return

            arr = np.frombuffer(pcm_in, dtype=np.int16)
            pcm_bytes = arr.tobytes()
            if settings.voxtral_volume_gain > 0 and settings.voxtral_volume_gain != 1.0:
                pcm_bytes = audioop.mul(pcm_bytes, 2, settings.voxtral_volume_gain)

            out_sr = settings.pipeline_audio_out_hz
            if settings.pipeline_audio_resample_quality == "high" and in_sr != out_sr:
                # High-quality numpy linear-interpolation resample
                pcm_out = resample_pcm_numpy(pcm_bytes, in_sr, out_sr)
            elif in_sr != out_sr:
                pcm_out, _ = audioop.ratecv(pcm_bytes, 2, 1, in_sr, out_sr, None)
            else:
                pcm_out = pcm_bytes
            emit_ms = max(20, min(200, int(settings.gemini_tts_min_emit_ms)))
            min_emit_bytes = int(out_sr * (emit_ms / 1000.0)) * 2
            emit_buf = bytearray(pcm_out)
            while len(emit_buf) >= min_emit_bytes:
                chunk = bytes(emit_buf[:min_emit_bytes])
                del emit_buf[:min_emit_bytes]
                yield TTSAudioRawFrame(
                    audio=chunk,
                    sample_rate=out_sr,
                    num_channels=1,
                )

            if emit_buf:
                yield TTSAudioRawFrame(
                    audio=bytes(emit_buf),
                    sample_rate=out_sr,
                    num_channels=1,
                )

            dt_ms = (time.perf_counter() - t0) * 1000.0
            logger.info("Gemini TTS finished in {:.0f} ms", dt_ms)

else:

    class GeminiHttpTTSService:  # type: ignore[no-redef]
        """Unavailable when optional ``pipecat`` is not installed."""

        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError(
                "pipecat is not installed — use gemini_synthesize_pcm() for opening audio"
            )
