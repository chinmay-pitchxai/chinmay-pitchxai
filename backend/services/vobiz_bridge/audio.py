"""PCM helpers, optional background decode, and pacing outbound ``playAudio`` frames."""

from __future__ import annotations

import audioop
import base64
import json
import struct
import wave
import io
from typing import Any, Optional

import numpy as np
from fastapi import WebSocket
from loguru import logger

from .constants import OUT_CHUNK_BYTES, VOBIZ_CONTENT_TYPE, VOBIZ_SR, GEMINI_OUT_SR

try:
    import miniaudio
except ImportError:
    miniaudio = None

from services.call_recording import CallRecorder


def load_background_audio(path: str, target_sr: int = 16000) -> Optional[np.ndarray]:
    if miniaudio is None or not path or not __import__("os").path.exists(path):
        return None
    try:
        decoded = miniaudio.decode_file(path, sample_rate=target_sr, nchannels=1)
        return np.frombuffer(decoded.samples, dtype=np.int16)
    except Exception as e:
        logger.error(f"Failed to load background audio: {e}")
        return None


def load_comfort_audio(path: str, target_sr: int = 16000) -> Optional[np.ndarray]:
    """Load a comfort audio file (WAV/MP3) for the silence prodder.
    Falls back to a generated 440 Hz beep tone if no file is configured.
    """
    if path and __import__("os").path.exists(path):
        if miniaudio is not None:
            try:
                decoded = miniaudio.decode_file(path, sample_rate=target_sr, nchannels=1)
                return np.frombuffer(decoded.samples, dtype=np.int16)
            except Exception as e:
                logger.warning("Comfort audio load failed, using generated tone: {}", e)
        else:
            # Try WAV fallback without miniaudio
            try:
                with wave.open(path, "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                    return np.frombuffer(raw, dtype=np.int16)
            except Exception:
                pass
    # Generate a gentle 440 Hz beep for ~0.8 seconds
    return _generate_comfort_tone(target_sr)


def _generate_comfort_tone(sr: int = 16000, freq: float = 440.0, duration_s: float = 0.8) -> np.ndarray:
    """Generate a soft 440 Hz beep tone as a 16-bit PCM numpy array."""
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    # Sine wave with fade-in/out envelope to avoid clicks
    tone = np.sin(2 * np.pi * freq * t) * 0.3  # 30% volume
    # Apply 50ms Hanning fade-in and fade-out
    fade_samples = int(0.05 * sr)
    if fade_samples > 0 and len(tone) > 2 * fade_samples:
        fade_in = np.hanning(2 * fade_samples)[:fade_samples]
        fade_out = np.hanning(2 * fade_samples)[fade_samples:]
        tone[:fade_samples] *= fade_in
        tone[-fade_samples:] *= fade_out
    return (tone * 32767).astype(np.int16)


def pcm_rms_norm(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    x = pcm.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(np.square(x))))


def vobiz_inbound_pcm_to_le(pcm_bytes: bytes) -> bytes:
    """Vobiz inbound ``media`` L16 is big-endian; Gemini/recording expect s16le."""
    if not pcm_bytes or len(pcm_bytes) < 2:
        return pcm_bytes
    return audioop.byteswap(pcm_bytes, 2)


def pcm_s16le_rms(pcm_bytes: bytes) -> float:
    if not pcm_bytes or len(pcm_bytes) < 2:
        return 0.0
    return float(audioop.rms(pcm_bytes, 2))


def pcm_s16le_has_voice(pcm_bytes: bytes, threshold: int = 400) -> bool:
    return pcm_s16le_rms(pcm_bytes) >= float(threshold)


def pcm_resample(pcm_bytes: bytes, in_sr: int, out_sr: int, state: object = None) -> tuple[bytes, object]:
    """Resample *pcm_bytes* from *in_sr* to *out_sr* Hz (16-bit mono).

    Returns ``(resampled_bytes, new_state)`` so callers can carry the
    ``audioop.ratecv`` internal state across consecutive calls and avoid
    the clicking artefacts that stateless resampling produces at every
    chunk boundary (the fractional-sample discontinuity that made the AI
    voice sound garbled / robotic on the handset).

    For one-shot use (e.g. greeting PCM pre-processing) just discard the
    returned state::

        resampled, _ = pcm_resample(pcm, 24000, 16000)
    """
    if in_sr == out_sr:
        return pcm_bytes, state
    out, new_state = audioop.ratecv(pcm_bytes, 2, 1, in_sr, out_sr, state)
    return out, new_state


def coerce_pcm_sr_pair(value: Any, default_sr: int = VOBIZ_SR) -> tuple[bytes, int] | None:
    """Normalize ``(pcm_bytes, sample_rate)`` from camp memory or sync payloads."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        raw, sr = value[0], value[1]
        if isinstance(raw, (bytes, bytearray)) and raw:
            try:
                return bytes(raw), int(sr or default_sr)
            except (TypeError, ValueError):
                return bytes(raw), default_sr
    return None


def pcm_s16le_fade_edges(pcm_bytes: bytes, *, fade_ms: float = 12.0, sr: int = VOBIZ_SR) -> bytes:
    """Apply short linear fade-in/out to avoid click/pop at PCM segment boundaries."""
    if not pcm_bytes or len(pcm_bytes) < 4:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    fade_n = min(len(samples) // 4, max(1, int(sr * fade_ms / 1000.0)))
    if fade_n <= 0:
        return pcm_bytes
    ramp_in = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
    ramp_out = ramp_in[::-1]
    samples[:fade_n] *= ramp_in
    samples[-fade_n:] *= ramp_out
    return samples.clip(-32768, 32767).astype(np.int16).tobytes()


def prepare_scripted_greeting_pcm(
    pcm_bytes: bytes,
    in_sr: int,
    out_sr: int = VOBIZ_SR,
    *,
    head_ms: float = 80.0,
    tail_ms: float = 100.0,
    fade_edges: bool = False,
) -> bytes:
    """Resample greeting PCM and add head/tail silence for carrier sync."""
    if not pcm_bytes:
        return b""
    if in_sr != out_sr:
        state = None
        parts: list[bytes] = []
        block = max(in_sr * 2, 640)
        for offset in range(0, len(pcm_bytes), block):
            chunk = pcm_bytes[offset : offset + block]
            resampled, state = pcm_resample(chunk, in_sr, out_sr, state)
            if resampled:
                parts.append(resampled)
        pcm_bytes = b"".join(parts)
    head = bytes(int(out_sr * 2 * head_ms / 1000.0))
    tail = bytes(int(out_sr * 2 * tail_ms / 1000.0))
    return head + pcm_bytes + tail


def mix_voice_and_background_tick(
    voice_pcm16: bytes,
    bg_wave: Optional[np.ndarray],
    volume: float,
    bg_position: int,
    chunk_samples: int,
) -> tuple[bytes, int]:
    """One 16-bit mono tick: blend outbound voice with a looped bed (scripted PCM or Gemini).

    ``volume`` scales the bed linearly on float samples before clipping (e.g. 0.75 ≈ 75 %).
    """
    chunk_bytes = chunk_samples * 2
    silence = bytes(chunk_bytes)
    bg_pcm = silence
    vol = float(volume)
    if vol < 0.0:
        vol = 0.0
    if bg_wave is not None and vol > 0:
        end_pos = bg_position + chunk_samples
        if end_pos > len(bg_wave):
            part1 = bg_wave[bg_position:]
            part2 = bg_wave[: end_pos - len(bg_wave)]
            bg_chunk = np.concatenate((part1, part2))
            bg_position = end_pos - len(bg_wave)
        else:
            bg_chunk = bg_wave[bg_position:end_pos]
            bg_position = end_pos

        bg_chunk = (bg_chunk.astype(np.float32) * vol).clip(-32768, 32767).astype(np.int16)
        bg_pcm = bg_chunk.tobytes()

    if voice_pcm16 == silence:
        mixed = bg_pcm
    elif bg_pcm == silence:
        mixed = voice_pcm16
    else:
        mixed = audioop.add(voice_pcm16, bg_pcm, 2)
    return mixed, bg_position


# ── Noise suppression (inbound human speech) ──────────────────────────────
# Simple but effective: high-pass + adaptive noise gate.
# The AI voice is already clean (Gemini model output); this only processes
# the microphone side where PSTN hum, background chatter, and road noise
# degrade the human speech before it reaches Gemini Live.

_NS_PROFILE_WINDOW_SEC = 3.0       # seconds of silence to establish noise floor
_NS_FLOOR_HOLD_FRAMES = 30         # frames to hold floor before re-learning
_NS_DEFAULT_NOISE_RMS = 120.0      # initial noise floor guess (silent line)
_NS_MIN_GATE_RMS = 40.0            # absolute silence floor (below this = pure silence)
_NS_HPF_COEFF = 0.92               # high-pass coefficient (80 Hz @ 16 kHz ≈ 0.92)


def noise_suppress_inbound_pcm(
    pcm_bytes: bytes,
    noise_floor: float = _NS_DEFAULT_NOISE_RMS,
    silence_counter: int = 0,
) -> tuple[bytes, float, int]:
    """Apply noise suppression to one 16 kHz 16-bit mono PCM frame.

    Steps
    -----
    1. High-pass filter (DC blocking + ~80 Hz corner) — removes AC hum / rumble.
    2. Adaptive noise gate — suppresses frames below a dynamically tracked floor.

    Returns
    -------
    (processed_bytes, updated_noise_floor, updated_silence_counter)
    """
    if not pcm_bytes or len(pcm_bytes) < 4:
        return pcm_bytes, noise_floor, silence_counter

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    if len(samples) < 2:
        return pcm_bytes, noise_floor, silence_counter

    # 1) High-pass / DC blocking: y[n] = a0 * x[n] + a1 * x[n-1] + b1 * y[n-1]
    # Single-pole HPF with coefficient near 1 = ~80 Hz corner at 16 kHz
    a0 = (1.0 + _NS_HPF_COEFF) / 2.0
    a1 = -(1.0 + _NS_HPF_COEFF) / 2.0
    b1 = _NS_HPF_COEFF
    prev_x = float(samples[0]) if len(samples) > 0 else 0.0
    prev_y = 0.0
    out = np.empty_like(samples)
    for i in range(len(samples)):
        cur_x = float(samples[i])
        cur_y = a0 * cur_x + a1 * prev_x + b1 * prev_y
        out[i] = cur_y
        prev_x = cur_x
        prev_y = cur_y

    # 2) Adaptive noise gate
    frame_rms = float(np.sqrt(np.mean(np.square(out))))
    if frame_rms < 0.001:
        frame_rms = 0.001  # avoid log(0)

    # If frame is quiet, update noise floor (adaptive)
    if frame_rms <= noise_floor * 1.5:
        silence_counter += 1
        # Smoothly track rising noise floor (e.g. after silence ended)
        if silence_counter >= _NS_FLOOR_HOLD_FRAMES:
            noise_floor = max(_NS_MIN_GATE_RMS, noise_floor * 0.95 + frame_rms * 0.05)
            silence_counter = _NS_FLOOR_HOLD_FRAMES  # cap
    else:
        silence_counter = max(0, silence_counter - 3)

    # Apply gate: below floor → gentle roll-off; above → pass through
    if frame_rms < noise_floor:
        # Attenuate proportionally below noise floor
        gain = max(0.05, frame_rms / max(1.0, noise_floor))
        out *= gain
    elif frame_rms < noise_floor * 2.5:
        # Transition zone — slight expansion to lift voice above noise
        excess_db = 20.0 * np.log10(frame_rms / max(1.0, noise_floor))
        boost = min(1.15, 1.0 + excess_db * 0.005)  # subtle 0-15 % lift
        out *= boost

    # Clip and return
    out = out.clip(-32768, 32767).astype(np.int16)
    return out.tobytes(), noise_floor, min(silence_counter, _NS_FLOOR_HOLD_FRAMES)


# ── Frame helpers ─────────────────────────────────────────────────────────

def vobiz_frame_bytes_16k(ms: float = 20.0) -> int:
    """One outbound mobile frame @ 16 kHz (default 20 ms = 640 bytes; ultra allows 10 ms)."""
    return int(VOBIZ_SR * 2 * max(0.01, ms / 1000.0))


def gemini_resample_block_bytes_24k(chunk_ms: float = 20.0) -> int:
    """24 kHz block size that resamples to an exact 16 kHz/mobile frame boundary."""
    ms = max(10.0, float(chunk_ms or 20.0))
    ms = round(ms / 10.0) * 10.0
    return int(GEMINI_OUT_SR * 2 * ms / 1000.0)


def drain_gemini_24k_to_vobiz_16k(
    pending_24k: bytearray,
    out_16k: bytearray,
    state: object | None,
    *,
    chunk_ms: float = 20.0,
    final_flush: bool = False,
) -> object | None:
    """Stateful 24 kHz Gemini PCM → 16 kHz queue for mobile playout (no click at chunk edges)."""
    block = gemini_resample_block_bytes_24k(chunk_ms)
    while len(pending_24k) >= block:
        chunk = bytes(pending_24k[:block])
        del pending_24k[:block]
        pcm_16k, state = pcm_resample(chunk, GEMINI_OUT_SR, VOBIZ_SR, state)
        if pcm_16k:
            out_16k.extend(pcm_16k)
    if final_flush and pending_24k:
        pcm_16k, state = pcm_resample(bytes(pending_24k), GEMINI_OUT_SR, VOBIZ_SR, state)
        pending_24k.clear()
        if pcm_16k:
            out_16k.extend(pcm_16k)
    return state


def pop_l16_chunk(queue: bytearray, chunk_bytes: int) -> bytes:
    if len(queue) >= chunk_bytes:
        out = bytes(queue[:chunk_bytes])
        del queue[:chunk_bytes]
        return out
    if len(queue) > 0:
        n = len(queue)
        out = bytes(queue) + b"\x00" * (chunk_bytes - n)
        queue.clear()
        return out
    return b"\x00" * chunk_bytes


async def send_play_audio(
    ws: WebSocket,
    pcm16_bytes: bytes,
    sr: int = VOBIZ_SR,
    *,
    call_recorder: Optional[CallRecorder] = None,
    sequence_number: Optional[int] = None,
    stream_id: Optional[str] = None,
) -> None:
    if not pcm16_bytes:
        return
    try:
        from config import settings

        gain = float(getattr(settings, "vobiz_outbound_audio_gain", 1.0) or 1.0)
    except Exception:
        gain = 1.0
    if gain != 1.0:
        gain = min(3.0, max(1.0, gain))
        pcm16_bytes = audioop.mul(pcm16_bytes, 2, gain)
    # #region agent log
    if pcm16_bytes and not getattr(send_play_audio, "_dbg_logged", False):
        try:
            from debug_agent_log import agent_debug

            send_play_audio._dbg_logged = True  # type: ignore[attr-defined]
            agent_debug(
                "F",
                "audio.py:send_play_audio",
                "play_audio_sent",
                {
                    "bytes": len(pcm16_bytes),
                    "gain": gain,
                    "out_rms": round(pcm_s16le_rms(pcm16_bytes[:640]), 1),
                },
            )
        except Exception:
            pass
    # #endregion
    if call_recorder is not None:
        call_recorder.add_outbound(pcm16_bytes)
    view = memoryview(pcm16_bytes)
    for offset in range(0, len(view), OUT_CHUNK_BYTES):
        chunk = bytes(view[offset : offset + OUT_CHUNK_BYTES])
        if len(chunk) < 2:
            continue
        msg = {
            "event": "playAudio",
            "media": {
                "contentType": VOBIZ_CONTENT_TYPE,
                "sampleRate": sr,
                "payload": base64.b64encode(chunk).decode("ascii"),
            },
        }
        if stream_id:
            msg["streamId"] = stream_id
        if sequence_number is not None:
            msg["sequenceNumber"] = str(sequence_number)
        await ws.send_text(json.dumps(msg))
