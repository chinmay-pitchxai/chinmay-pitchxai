"""Per-call 16 kHz mono recordings for Vobiz WebSocket calls.

Capture writes PCM WAV stems; on close the primary playback artifact is
``{session}_mixed.wav`` (16 kHz mono duplex mix). Optional stereo/MP3 kept for review.
"""

from __future__ import annotations

import audioop
import json
import os
import re
import subprocess
import threading
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config import settings


def _safe_stem(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:180]


def _safe_folder_name(name: str, phone: str) -> str:
    nm = _safe_stem((name or "Unknown").replace(" ", "_"))[:72] or "Unknown"
    digits = "".join(c for c in str(phone or "") if c.isdigit())[-10:]
    return f"{nm}_{digits or 'unknown'}"


def _ist_day_dir(base_dir: Path) -> Path:
    """Calendar day folder in dashboard TZ (IST) for operator-friendly browsing."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
        day = datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return base_dir / day


def _recording_roots(base_dir: Optional[str | Path] = None) -> tuple[Path, Path]:
    session_base = Path(base_dir or settings.call_recording_dir).resolve()
    archive_base = Path(settings.call_recording_archive_dir).resolve()
    return session_base, archive_base


def _recording_env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _spectral_nr_enabled() -> bool:
    return (os.getenv("CALL_RECORDING_SPECTRAL_NR") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# Dashboard / API playback: mono mixed WAV first; MP3/stereo are optional fallbacks.
_PLAYBACK_EXT_ORDER = (".wav", ".mp3")


def _mp3_encoding_enabled() -> bool:
    return (os.getenv("CALL_RECORDING_MP3_ENABLED") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _mp3_bitrate() -> str:
    return (os.getenv("CALL_RECORDING_MP3_BITRATE") or "128k").strip() or "128k"


def _mp3_stereo_bitrate() -> str:
    return (os.getenv("CALL_RECORDING_MP3_STEREO_BITRATE") or "192k").strip() or "192k"


def _mp3_sample_rate() -> int:
    try:
        return int(os.getenv("CALL_RECORDING_MP3_SAMPLE_RATE", "44100") or "44100")
    except (TypeError, ValueError):
        return 44100


def _encode_wav_to_mp3(
    wav_path: str | Path,
    mp3_path: str | Path,
    *,
    channels: int | None = None,
) -> bool:
    """Encode WAV to MP3 (libmp3lame, 44.1 kHz). Stereo = L user / R agent for review."""
    src = Path(wav_path)
    dest = Path(mp3_path)
    if not src.is_file():
        return False
    try:
        with wave.open(str(src), "rb") as w:
            nch = w.getnchannels()
    except (OSError, wave.Error):
        nch = 1
    ac = channels if channels is not None else max(1, min(2, nch))
    bitrate = _mp3_stereo_bitrate() if ac >= 2 else _mp3_bitrate()
    sr = _mp3_sample_rate()
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-acodec",
                "libmp3lame",
                "-b:a",
                bitrate,
                "-ac",
                str(ac),
                "-ar",
                str(sr),
                str(dest),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        ok = dest.is_file() and dest.stat().st_size > 128
        if ok:
            logger.info(
                "Call recording: MP3 written {} ({} B, {}ch @ {}Hz)",
                dest,
                dest.stat().st_size,
                ac,
                sr,
            )
        return ok
    except Exception as exc:
        logger.warning("Call recording MP3 encode failed {} -> {}: {}", src, dest, exc)
        return False


def _audio_duration_sec(path: Path) -> float | None:
    if not path.is_file():
        return None
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                return w.getnframes() / float(w.getframerate())
        except (OSError, wave.Error):
            return None
    if path.suffix.lower() == ".mp3":
        try:
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except Exception:
            pass
    return None


def _vobiz_playback_preferred() -> bool:
    """Vobiz trunk recording should be used for playback instead of local mixed WAV."""
    try:
        from config import settings
        return bool(
            getattr(settings, "vobiz_trunk_recording_prefer_playback", False)
            and getattr(settings, "vobiz_trunk_recording_enabled", False)
        )
    except Exception:
        return False


def _vobiz_only_recording() -> bool:
    try:
        from config import settings

        return bool(
            getattr(settings, "vobiz_trunk_recording_only", False)
            or (
                not getattr(settings, "call_recording_enabled", True)
                and getattr(settings, "vobiz_trunk_recording_enabled", True)
            )
        )
    except Exception:
        return False


def _playback_name_candidates(stem: str) -> list[str]:
    """Playback order: Vobiz trunk WAV first; local stems only when not vobiz-only mode."""
    names: list[str] = []
    if getattr(settings, "vobiz_trunk_recording_enabled", True) or getattr(
        settings, "vobiz_trunk_recording_prefer_playback", True
    ):
        for ext in _PLAYBACK_EXT_ORDER:
            names.append(f"{stem}_vobiz{ext}")
    if _vobiz_only_recording():
        # Strict carrier-only: never fall back to noisy local bridge mixes.
        return names
    for ext in _PLAYBACK_EXT_ORDER:
        names.append(f"{stem}_mixed{ext}")
    for base in ("_full", "_outbound", "_inbound", "_stereo"):
        for ext in _PLAYBACK_EXT_ORDER:
            names.append(f"{stem}{base}{ext}")
    return names


def _pcm_frame_bytes() -> int:
    """20 ms @ 16 kHz mono s16le — matches Vobiz playout tick (640 bytes)."""
    ms = max(10.0, min(40.0, _recording_env_float("CALL_RECORDING_FRAME_MS", 20.0)))
    return max(320, int(16_000 * 2 * ms / 1000.0))


def _trim_to_frames(pcm: bytes, frame_bytes: int) -> bytes:
    if not pcm or frame_bytes <= 0:
        return b""
    n = (len(pcm) // frame_bytes) * frame_bytes
    return pcm[:n]


def _align_pcm_tracks(in_pcm: bytes, out_pcm: bytes, frame_bytes: int) -> tuple[bytes, bytes, dict[str, int]]:
    """Frame-align and pad to equal length without shifting either track in time.

    Both stems share the same wall-clock start from CallRecorder; trimming leading
    silence independently desyncs agent greeting vs user mic and makes user speech
    sound noisy or missing in the mixed WAV.
    """
    in_pcm = _trim_to_frames(in_pcm, frame_bytes)
    out_pcm = _trim_to_frames(out_pcm, frame_bytes)
    in_len = len(in_pcm)
    out_len = len(out_pcm)
    ln = max(in_len, out_len)
    ln = (ln // frame_bytes) * frame_bytes
    if in_len < ln:
        in_pcm = in_pcm + b"\x00" * (ln - in_len)
    if out_len < ln:
        out_pcm = out_pcm + b"\x00" * (ln - out_len)
    meta = {
        "in_pad_frames": (ln - in_len) // frame_bytes if frame_bytes else 0,
        "out_pad_frames": (ln - out_len) // frame_bytes if frame_bytes else 0,
        "aligned_frames": ln // frame_bytes if frame_bytes else 0,
    }
    return in_pcm[:ln], out_pcm[:ln], meta


class _SpectralNoiseReducer:
    """Real-time streaming spectral subtraction for PSTN inbound line hiss.

    Uses overlap-add FFT processing with an adaptive noise floor that is
    bootstrapped from the first ~400 ms of each call (which is always
    pure PSTN carrier noise before the callee picks up) and then updated
    continuously during silent frames.

    Algorithm: Boll (1979) spectral subtraction with oversubtraction factor
    and spectral flooring to prevent musical noise artefacts.

    Thread-safety: each CallRecorder owns one instance; access is serialised
    by CallRecorder._lock so no additional locking is needed here.
    """

    # FFT frame size (32 ms @ 16 kHz) — long enough for good Hz resolution.
    _N: int = 512
    # Hop = 50% overlap for smooth reconstruction.
    _HOP: int = 256
    # Oversubtraction factor: higher = more aggressive noise removal.
    _ALPHA: float = 3.0
    # Spectral floor: prevents musical noise; 0.015 = floor at 1.5% of noise.
    _BETA: float = 0.015
    # How many quiet frames to collect for noise bootstrap (~1.3 s @ 16ms/hop).
    _NOISE_BOOTSTRAP_FRAMES: int = 80
    # RMS threshold below which a frame is treated as pure PSTN noise.
    # PSTN carrier hiss typically sits at 3500-6000 RMS; speech starts at ~8000+.
    _NOISE_RMS_THRESHOLD: float = 7000.0

    def __init__(self) -> None:
        import numpy as np

        self._np = np
        self._window: np.ndarray = np.hanning(self._N).astype(np.float32)
        self._noise_power: np.ndarray | None = None
        self._bootstrap_specs: list[np.ndarray] = []
        self._bootstrapped: bool = False
        # Overlap-add state: carry-over from previous frame.
        self._ola_buf: np.ndarray = np.zeros(self._N, dtype=np.float32)
        self._norm_buf: np.ndarray = np.zeros(self._N, dtype=np.float32)
        # Input sample buffer for sub-frame chunks.
        self._in_buf: np.ndarray = np.zeros(0, dtype=np.float32)
        self._frames_processed: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, pcm_s16le: bytes) -> bytes:
        """Denoise one chunk of s16le mono PCM; returns same-length s16le."""
        if not pcm_s16le:
            return pcm_s16le
        np = self._np
        try:
            chunk = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32)
            # Append to internal buffer
            self._in_buf = np.concatenate([self._in_buf, chunk])
            output_samples: list[np.ndarray] = []

            while len(self._in_buf) >= self._N:
                frame = self._in_buf[: self._N] * self._window
                self._in_buf = self._in_buf[self._HOP :]

                spec = np.fft.rfft(frame, n=self._N)
                mag = np.abs(spec)
                phase = np.angle(spec)
                power = mag ** 2

                # ---- Bootstrap phase: accumulate quiet frames ----
                if not self._bootstrapped:
                    frame_rms = float(np.sqrt(np.mean(frame ** 2)))
                    # Accept frame as pure noise if below speech onset threshold.
                    # PSTN carrier hiss: 3500-6000 RMS; speech starts at ~8000+ RMS.
                    if frame_rms < self._NOISE_RMS_THRESHOLD:
                        self._bootstrap_specs.append(power)
                    if len(self._bootstrap_specs) >= self._NOISE_BOOTSTRAP_FRAMES:
                        self._noise_power = np.mean(self._bootstrap_specs, axis=0)
                        self._bootstrapped = True
                        logger.debug(
                            "SpectralNR: noise floor bootstrapped from {} frames",
                            len(self._bootstrap_specs),
                        )

                # ---- Spectral subtraction ----
                if self._bootstrapped and self._noise_power is not None:
                    mag_sq_clean = power - self._ALPHA * self._noise_power
                    mag_sq_clean = np.maximum(mag_sq_clean, self._BETA * self._noise_power)
                    mag_clean = np.sqrt(mag_sq_clean)

                    # Adaptive noise update: refresh during likely-quiet frames.
                    snr_est = float(np.mean(mag) / (np.mean(np.sqrt(self._noise_power)) + 1e-6))
                    if snr_est < 1.4:  # frame is mostly noise
                        # Slow exponential update so noise floor tracks drift.
                        self._noise_power = 0.92 * self._noise_power + 0.08 * power
                else:
                    mag_clean = mag  # passthrough until bootstrapped

                spec_clean = mag_clean * np.exp(1j * phase)
                frame_out = np.real(np.fft.irfft(spec_clean, n=self._N)).astype(np.float32)

                # Overlap-add into output buffer
                self._ola_buf[: self._N] += frame_out * self._window
                self._norm_buf[: self._N] += self._window ** 2

                # Emit the first _HOP samples (fully accumulated)
                ready = self._ola_buf[: self._HOP].copy()
                norm = np.maximum(self._norm_buf[: self._HOP], 1e-10)
                output_samples.append(ready / norm)

                # Shift buffers
                self._ola_buf[: self._N - self._HOP] = self._ola_buf[self._HOP : self._N]
                self._ola_buf[self._N - self._HOP :] = 0.0
                self._norm_buf[: self._N - self._HOP] = self._norm_buf[self._HOP : self._N]
                self._norm_buf[self._N - self._HOP :] = 0.0

                self._frames_processed += 1

            if not output_samples:
                # Not enough data yet — return silence of the same length.
                return bytes(len(pcm_s16le))

            out_arr = np.concatenate(output_samples)
            # Trim/pad to match input length
            target_samples = len(pcm_s16le) // 2
            if len(out_arr) < target_samples:
                out_arr = np.concatenate([out_arr, np.zeros(target_samples - len(out_arr), dtype=np.float32)])
            else:
                out_arr = out_arr[:target_samples]

            out_int16 = np.clip(out_arr, -32768, 32767).astype(np.int16)
            return out_int16.tobytes()
        except Exception as exc:
            logger.warning("SpectralNoiseReducer.process failed: {}", exc)
            return pcm_s16le


def _create_stereo_pcm(in_pcm: bytes, out_pcm: bytes) -> bytes:
    """Interleave mono Inbound (Left) and mono Outbound (Right) into 2-channel Stereo PCM."""
    if not in_pcm and not out_pcm:
        return b""
    import numpy as np

    try:
        in_arr = np.frombuffer(in_pcm, dtype=np.int16) if in_pcm else np.zeros(len(out_pcm) // 2, dtype=np.int16)
        out_arr = np.frombuffer(out_pcm, dtype=np.int16) if out_pcm else np.zeros(len(in_pcm) // 2, dtype=np.int16)
        ln = min(len(in_arr), len(out_arr))
        stereo = np.empty((ln, 2), dtype=np.int16)
        stereo[:, 0] = in_arr[:ln]  # Left: Inbound User (PSTN)
        stereo[:, 1] = out_arr[:ln]  # Right: Outbound Agent (Gemini)
        return stereo.tobytes()
    except Exception as exc:
        logger.warning("Stereo PCM creation failed: {}", exc)
        return b""


def _compress_inbound_frame(chunk: bytes, *, threshold: float, ratio: float) -> bytes:
    """Soft-knee compression on hot PSTN inbound (recording playback only)."""
    if not chunk or ratio <= 1.0:
        return chunk
    try:
        rms = float(audioop.rms(chunk, 2))
    except Exception:
        return chunk
    if rms <= threshold:
        return chunk
    factor = min(1.0, (threshold + (rms - threshold) / ratio) / rms)
    return audioop.mul(chunk, 2, factor)


def _recording_mix_weights(
    in_rms: float,
    out_rms: float,
    *,
    in_gain: float,
    out_gain: float,
) -> tuple[float, float, str]:
    """Per-frame duplex weights: user-primary when they talk, agent-primary otherwise."""
    agent_thr = max(200.0, _recording_env_float("CALL_RECORDING_AGENT_RMS", 450.0))
    user_thr = max(800.0, _recording_env_float("CALL_RECORDING_USER_RMS", 1500.0))
    silence_thr = max(100.0, _recording_env_float("CALL_RECORDING_SILENCE_RMS", 400.0))
    user_in_w = min(1.0, max(0.45, _recording_env_float("CALL_RECORDING_USER_IN_WEIGHT", 0.82)))
    user_out_w = min(0.45, max(0.05, _recording_env_float("CALL_RECORDING_USER_OUT_BLEED", 0.18)))
    agent_in_w = min(0.35, max(0.02, _recording_env_float("CALL_RECORDING_AGENT_IN_BLEED", 0.10)))

    if in_rms < silence_thr and out_rms < silence_thr:
        return 0.0, 0.0, "silence"
    if out_rms >= agent_thr and out_rms > in_rms * 1.35:
        return agent_in_w * in_gain, out_gain, "agent"
    if in_rms >= user_thr and in_rms > out_rms * 1.12:
        return user_in_w * in_gain, user_out_w, "user"
    return in_gain * 0.52, out_gain * 0.58, "overlap"


def _subtract_pcm(a: bytes, b: bytes) -> bytes:
    """Sample-wise a - b with int16 clipping (playback DSP only)."""
    import struct

    ln = min(len(a), len(b))
    if ln < 2:
        return a[:ln]
    out = bytearray()
    for i in range(0, ln - (ln % 2), 2):
        sa = struct.unpack("<h", a[i : i + 2])[0]
        sb = struct.unpack("<h", b[i : i + 2])[0]
        v = sa - sb
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.extend(struct.pack("<h", v))
    return bytes(out)


class _PcmFrameBuffer:
    """Accumulate variable-size PCM into fixed 20 ms frames before WAV write."""

    __slots__ = ("_w", "_fb", "_buf", "frames_written", "bytes_in", "clip_frames", "peak")

    def __init__(self, wave_writer: wave.Wave_write | None, frame_bytes: int) -> None:
        self._w = wave_writer
        self._fb = frame_bytes
        self._buf = bytearray()
        self.frames_written = 0
        self.bytes_in = 0
        self.clip_frames = 0
        self.peak = 0

    def write(self, pcm: bytes) -> None:
        if not pcm or self._w is None:
            return
        self.bytes_in += len(pcm)
        self._buf.extend(pcm)
        while len(self._buf) >= self._fb:
            frame = bytes(self._buf[: self._fb])
            del self._buf[: self._fb]
            try:
                pk = audioop.max(frame, 2)
            except Exception:
                pk = 0
            if pk > self.peak:
                self.peak = pk
            if pk >= 32700:
                self.clip_frames += 1
            self._w.writeframes(frame)
            self.frames_written += 1

    def flush(self) -> None:
        if not self._buf or self._w is None:
            return
        rem = bytes(self._buf)
        self._buf.clear()
        if len(rem) < self._fb:
            rem = rem + b"\x00" * (self._fb - len(rem))
        try:
            pk = audioop.max(rem[: self._fb], 2)
        except Exception:
            pk = 0
        if pk > self.peak:
            self.peak = pk
        if pk >= 32700:
            self.clip_frames += 1
        self._w.writeframes(rem[: self._fb])
        self.frames_written += 1


class CallRecorder:
    """Appends 16 kHz s16le mono PCM; writes WAV on close. Thread-safe for async pumps."""

    def __init__(
        self,
        session_id: str,
        *,
        channel: str,
        base_dir: Optional[str] = None,
        lead_name: str = "",
        phone: str = "",
        role: str = "",
    ) -> None:
        self._session_id = session_id
        self._channel = channel
        self._lead_name = (lead_name or "").strip()
        self._phone = (phone or "").strip()
        self._role = (role or "").strip()
        self._lock = threading.Lock()
        self._in_w: wave.Wave_write | None = None
        self._out_w: wave.Wave_write | None = None
        self._full_w: wave.Wave_write | None = None
        self._in_path: Optional[str] = None
        self._out_path: Optional[str] = None
        self._full_path: Optional[str] = None
        self._mixed_mp3_path: Optional[str] = None
        self._stereo_mp3_path: Optional[str] = None
        self._archive_full_path: Optional[str] = None
        self._started_at = datetime.now(timezone.utc)
        self._frame_bytes = _pcm_frame_bytes()
        self._in_buf: _PcmFrameBuffer | None = None
        self._out_buf: _PcmFrameBuffer | None = None
        self._full_buf: _PcmFrameBuffer | None = None
        self._last_mix_diag: dict[str, Any] = {}
        self._inbound_nr: _SpectralNoiseReducer | None = None
        if not settings.call_recording_enabled:
            return
        session_base, archive_base = _recording_roots(base_dir)
        d = _ist_day_dir(session_base)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Call recording: cannot create dir {}: {}", d, e)
            return
        stem = _safe_stem(session_id)
        self._in_path = str(d / f"{stem}_inbound.wav")
        self._out_path = str(d / f"{stem}_outbound.wav")
        self._full_path = str(d / f"{stem}_full.wav")
        try:
            for path, attr in (
                (self._in_path, "_in_w"),
                (self._out_path, "_out_w"),
                (self._full_path, "_full_w"),
            ):
                w = wave.open(path, "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16_000)
                setattr(self, attr, w)
            self._in_buf = _PcmFrameBuffer(self._in_w, self._frame_bytes)
            self._out_buf = _PcmFrameBuffer(self._out_w, self._frame_bytes)
            self._full_buf = _PcmFrameBuffer(self._full_w, self._frame_bytes)
            if _spectral_nr_enabled():
                self._inbound_nr = _SpectralNoiseReducer()
            logger.info(
                "Call recording: started channel={} session={} full={} lead={!r} frame_bytes={} noise_reduction={}",
                channel,
                session_id,
                self._full_path,
                self._lead_name or self._phone,
                self._frame_bytes,
                "spectral_subtraction" if self._inbound_nr else "off",
            )
        except OSError as e:
            logger.warning("Call recording: init failed: {}", e)
            self._close_waves_unlocked()
            self._in_path = self._out_path = self._full_path = None

    def _close_waves_unlocked(self) -> None:
        for w in (self._in_w, self._out_w, self._full_w):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass
        self._in_w = None
        self._out_w = None
        self._full_w = None
        self._in_buf = None
        self._out_buf = None
        self._full_buf = None

    def _flush_track_buffers_unlocked(self) -> None:
        for buf in (self._in_buf, self._out_buf, self._full_buf):
            if buf is not None:
                buf.flush()

    def add_inbound(self, pcm_s16le_mono: bytes) -> None:
        if not pcm_s16le_mono:
            return
        with self._lock:
            if self._in_buf is not None:
                try:
                    # Apply spectral noise reduction to strip PSTN carrier hiss
                    # before writing to disk so both _inbound.wav and the final
                    # mix are clean without any post-processing step.
                    if self._inbound_nr is not None:
                        pcm_s16le_mono = self._inbound_nr.process(pcm_s16le_mono)
                    self._in_buf.write(pcm_s16le_mono)
                except OSError as e:
                    logger.warning("Call recording inbound: {}", e)

    def add_outbound(self, pcm_s16le_mono: bytes) -> None:
        if not pcm_s16le_mono:
            return
        with self._lock:
            if self._out_buf is not None:
                try:
                    self._out_buf.write(pcm_s16le_mono)
                except OSError as e:
                    logger.warning("Call recording outbound: {}", e)

    def add_phone_mix(self, pcm_s16le_mono: bytes) -> None:
        """Exact audio sent to the callee (agent + silence ticks) — full call timeline."""
        if not pcm_s16le_mono:
            return
        with self._lock:
            if self._full_buf is not None:
                try:
                    self._full_buf.write(pcm_s16le_mono)
                except OSError as e:
                    logger.warning("Call recording full mix: {}", e)

    def close(self) -> None:
        with self._lock:
            self._flush_track_buffers_unlocked()
            self._close_waves_unlocked()
        if self._in_path or self._out_path or self._full_path:
            self._log_track_diagnostics()
            logger.info(
                "Call recording: closed channel={} session={} full={}",
                self._channel,
                self._session_id,
                self._full_path,
            )
            if _vobiz_playback_preferred():
                logger.info(
                    "Call recording: stems saved — playback awaits Vobiz trunk webhook (log_id={})",
                    self._session_id,
                )
            # Always write the local duplex mix so transcription has a file even if
            # the Vobiz trunk webhook never delivers (stale / unsupported calls).
            self._write_mixed_wav()
            self._archive_full_recording()

    def _log_track_diagnostics(self) -> None:
        fb = self._frame_bytes
        in_buf, out_buf, full_buf = self._in_buf, self._out_buf, self._full_buf
        diag = {
            "session_id": self._session_id,
            "sample_rate_hz": 16_000,
            "frame_bytes": fb,
            "frame_ms": round(1000.0 * fb / (16_000 * 2), 2),
            "inbound_frames": getattr(in_buf, "frames_written", 0),
            "outbound_frames": getattr(out_buf, "frames_written", 0),
            "phone_mix_frames": getattr(full_buf, "frames_written", 0),
            "inbound_peak": getattr(in_buf, "peak", 0),
            "outbound_peak": getattr(out_buf, "peak", 0),
            "inbound_clip_frames": getattr(in_buf, "clip_frames", 0),
            "outbound_clip_frames": getattr(out_buf, "clip_frames", 0),
        }
        if self._last_mix_diag:
            diag["mix"] = self._last_mix_diag
        logger.info("Call recording diagnostics: {}", diag)

    def _mix_duplex_pcm(self, in_frames: bytes, out_frames: bytes) -> bytes:
        """Frame-aligned duplex mix with PSTN echo compensation (playback only)."""
        if not in_frames and not out_frames:
            return b""
        fb = self._frame_bytes
        in_gain = min(2.0, max(0.35, _recording_env_float("CALL_RECORDING_INBOUND_GAIN", 0.78)))
        out_gain = min(1.5, max(0.5, _recording_env_float("CALL_RECORDING_OUTBOUND_GAIN", 0.95)))
        master_gain = min(2.0, max(0.5, _recording_env_float("CALL_RECORDING_MASTER_GAIN", 1.0)))
        echo_coef = min(0.45, max(0.0, _recording_env_float("CALL_RECORDING_ECHO_CANCEL_COEF", 0.18)))
        in_comp_thr = max(800.0, _recording_env_float("CALL_RECORDING_IN_COMPRESS_RMS", 3200.0))
        in_comp_ratio = max(1.5, _recording_env_float("CALL_RECORDING_IN_COMPRESS_RATIO", 3.5))
        limiter_ceiling = min(32767.0, max(24000.0, _recording_env_float("CALL_RECORDING_LIMITER_PEAK", 28000.0)))
        if not in_frames:
            self._last_mix_diag = {"mode": "outbound_only", "out_gain": out_gain}
            return audioop.mul(out_frames, 2, out_gain)
        if not out_frames:
            comp_in = bytearray()
            for offset in range(0, len(in_frames), fb):
                chunk = in_frames[offset : offset + fb]
                if len(chunk) < fb:
                    chunk = chunk + b"\x00" * (fb - len(chunk))
                comp_in.extend(
                    _compress_inbound_frame(
                        chunk, threshold=in_comp_thr, ratio=in_comp_ratio
                    )
                )
            self._last_mix_diag = {"mode": "inbound_only", "in_gain": in_gain}
            return audioop.mul(bytes(comp_in), 2, in_gain)

        in_frames, out_frames, align = _align_pcm_tracks(in_frames, out_frames, fb)
        if not in_frames or not out_frames:
            return b""

        mixed_parts: list[bytes] = []
        limiter_hits = 0
        echo_frames = 0
        user_frames = 0
        agent_frames = 0
        for offset in range(0, len(in_frames), fb):
            in_chunk = in_frames[offset : offset + fb]
            out_chunk = out_frames[offset : offset + fb]
            if len(in_chunk) < fb:
                in_chunk = in_chunk + b"\x00" * (fb - len(in_chunk))
            if len(out_chunk) < fb:
                out_chunk = out_chunk + b"\x00" * (fb - len(out_chunk))

            try:
                in_rms = float(audioop.rms(in_chunk, 2))
                out_rms = float(audioop.rms(out_chunk, 2))
            except Exception:
                in_rms = 0.0
                out_rms = 0.0

            in_clean = in_chunk
            # Echo-cancel only while agent dominates; subtracting during user speech adds artifacts.
            if echo_coef > 0.0 and out_rms >= 400.0 and in_rms < out_rms * 2.2:
                echo_frames += 1
                in_clean = _subtract_pcm(in_chunk, audioop.mul(out_chunk, 2, echo_coef))

            in_clean = _compress_inbound_frame(
                in_clean, threshold=in_comp_thr, ratio=in_comp_ratio
            )
            w_in, w_out, role = _recording_mix_weights(
                in_rms, out_rms, in_gain=in_gain, out_gain=out_gain
            )
            if role == "user":
                user_frames += 1
            elif role == "agent":
                agent_frames += 1

            if w_in <= 0.0 and w_out <= 0.0:
                mixed_parts.append(b"\x00" * fb)
                continue

            in_scaled = audioop.mul(in_clean, 2, w_in)
            out_scaled = audioop.mul(out_chunk, 2, w_out)
            mixed = audioop.add(in_scaled, out_scaled, 2)
            if master_gain != 1.0:
                mixed = audioop.mul(mixed, 2, master_gain)
            peak = audioop.max(mixed, 2)
            if peak > limiter_ceiling:
                mixed = audioop.mul(mixed, 2, limiter_ceiling / peak)
                limiter_hits += 1
            mixed_parts.append(mixed)

        self._last_mix_diag = {
            "mode": "duplex",
            "in_gain": in_gain,
            "out_gain": out_gain,
            "echo_coef": echo_coef,
            "in_compress_rms": in_comp_thr,
            "in_compress_ratio": in_comp_ratio,
            "limiter_ceiling": limiter_ceiling,
            "limiter_frames": limiter_hits,
            "echo_cancel_frames": echo_frames,
            "user_primary_frames": user_frames,
            "agent_primary_frames": agent_frames,
            **align,
        }
        return b"".join(mixed_parts)

    def _maybe_write_diag_stems(
        self,
        stem: str,
        parent: Path,
        in_frames: bytes,
        out_frames: bytes,
        mixed: bytes,
    ) -> None:
        diag_dir = (os.getenv("CALL_RECORDING_DIAG_DIR") or "").strip()
        if not diag_dir:
            return
        try:
            d = Path(diag_dir) / _ist_day_dir(Path(diag_dir))
            d.mkdir(parents=True, exist_ok=True)
            for name, payload in (
                (f"{stem}_diag_inbound.wav", in_frames),
                (f"{stem}_diag_outbound.wav", out_frames),
                (f"{stem}_diag_mixed.wav", mixed),
            ):
                if not payload:
                    continue
                with wave.open(str(d / name), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16_000)
                    w.writeframes(payload)
            meta_path = d / f"{stem}_diag.json"
            meta_path.write_text(
                json.dumps(self._last_mix_diag, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Call recording diag export failed: {}", exc)

    def _write_mixed_wav(self) -> None:
        """Write noise-reduced inbound + outbound: mono mixed WAV and 2-channel stereo WAV.

        Inbound audio is already spectrally denoised at the point of capture
        (add_inbound → _SpectralNoiseReducer), so no further filtering is needed here.
        """
        if not self._in_path and not self._out_path:
            return
        try:
            in_frames = b""
            out_frames = b""
            for path, assign in ((self._in_path, "in"), (self._out_path, "out")):
                if not path:
                    continue
                try:
                    with wave.open(path, "rb") as w:
                        frames = w.readframes(w.getnframes())
                except (OSError, wave.Error) as e:
                    logger.warning("Call recording mix: cannot read {}: {}", path, e)
                    continue
                if assign == "in":
                    in_frames = frames
                else:
                    out_frames = frames
            if not in_frames and not out_frames:
                return

            # Inbound is already clean (denoised at ingest). Mix directly.
            mixed = self._mix_duplex_pcm(in_frames, out_frames)
            stem = Path(self._in_path or self._out_path).stem
            for suffix in ("_inbound", "_outbound"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            parent = Path((self._in_path or self._out_path)).parent
            self._maybe_write_diag_stems(stem, parent, in_frames, out_frames, mixed)
            mixed_path = str(parent / f"{stem}_mixed.wav")
            with wave.open(mixed_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16_000)
                w.writeframes(mixed)
            logger.info("Call recording: mixed WAV written {} ({} B)", mixed_path, len(mixed))

            in_aligned, out_aligned, _ = _align_pcm_tracks(in_frames, out_frames, self._frame_bytes)
            stereo_pcm = _create_stereo_pcm(in_aligned, out_aligned)
            stereo_path: str | None = None
            if stereo_pcm:
                stereo_path = str(parent / f"{stem}_stereo.wav")
                try:
                    with wave.open(stereo_path, "wb") as w:
                        w.setnchannels(2)
                        w.setsampwidth(2)
                        w.setframerate(16_000)
                        w.writeframes(stereo_pcm)
                    logger.info("Call recording: stereo WAV written {} ({} B)", stereo_path, len(stereo_pcm))
                    if _mp3_encoding_enabled():
                        stereo_mp3_path = str(parent / f"{stem}_stereo.mp3")
                        if _encode_wav_to_mp3(stereo_path, stereo_mp3_path, channels=2):
                            self._stereo_mp3_path = stereo_mp3_path
                except OSError as e:
                    logger.warning("Call recording: stereo WAV write failed: {}", e)
                    stereo_path = None

            if self._full_path and mixed:
                try:
                    with wave.open(self._full_path, "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(16_000)
                        w.writeframes(mixed)
                    logger.info(
                        "Call recording: full WAV updated with mono duplex mix {}",
                        self._full_path,
                    )
                except OSError as e:
                    logger.warning("Call recording: could not update full WAV with mix: {}", e)

            if _mp3_encoding_enabled():
                mp3_path = str(parent / f"{stem}_mixed.mp3")
                if _encode_wav_to_mp3(mixed_path, mp3_path, channels=1):
                    self._mixed_mp3_path = mp3_path
        except Exception as e:
            logger.warning("Call recording mix failed: {}", e)

    def _archive_full_recording(self) -> None:
        """Copy primary mono mixed WAV to Call_Recordings/{date}/{name_phone}/."""
        src = self._pick_primary_playback_path()
        if not src or not src.is_file():
            return
        try:
            _, archive_base = _recording_roots(None)
            day_dir = _ist_day_dir(archive_base)
            folder = day_dir / _safe_folder_name(self._lead_name, self._phone)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
                ts = self._started_at.astimezone(tz).strftime("%H%M%S")
            except Exception:
                ts = self._started_at.strftime("%H%M%S")
            stem = _safe_stem(self._session_id)
            ext = src.suffix.lower() if src.suffix else ".wav"
            dest = folder / f"{ts}_{stem}_full{ext}"
            dest.write_bytes(src.read_bytes())
            self._archive_full_path = str(dest)
            meta = {
                "session_id": self._session_id,
                "lead_name": self._lead_name,
                "phone": self._phone,
                "role": self._role,
                "channel": self._channel,
                "recording_path": str(dest),
                "source_path": str(src),
                "started_at_utc": self._started_at.isoformat(),
            }
            meta_path = dest.with_suffix(".json")
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("Call recording archived: {}", dest)
        except Exception as exc:
            logger.warning("Call recording archive failed: {}", exc)

    def _pick_primary_playback_path(self) -> Path | None:
        stem = _safe_stem(self._session_id)
        parent = Path(self._full_path or self._out_path or self._in_path or "").parent
        if parent.is_dir() and stem:
            for name in _playback_name_candidates(stem):
                cand = parent / name
                if cand.is_file() and cand.stat().st_size > 44:
                    return cand
        for p in (self._full_path, self._out_path, self._in_path):
            if not p:
                continue
            pp = Path(p)
            if pp.is_file() and pp.stat().st_size > 44:
                return pp
        return None

    def meta(self) -> dict[str, Any]:
        primary = self._pick_primary_playback_path()
        stem = _safe_stem(self._session_id)
        parent = Path(self._full_path or self._out_path or self._in_path or "").parent
        mixed_cand = parent / f"{stem}_mixed.wav" if parent.is_dir() and stem else None
        out: dict[str, Any] = {
            "inbound_wav": self._in_path,
            "outbound_wav": self._out_path,
            "full_wav": self._full_path,
            "mixed_mp3": self._mixed_mp3_path,
            "stereo_mp3": self._stereo_mp3_path,
            "archive_wav": self._archive_full_path,
            "call_recording": bool(primary),
        }
        if mixed_cand and mixed_cand.is_file():
            out["mixed_wav"] = str(mixed_cand)
        if primary:
            out["playback_path"] = str(primary)
            if primary.suffix.lower() == ".wav":
                out["playback_wav"] = str(primary)
            elif primary.suffix.lower() == ".mp3":
                out["playback_mp3"] = str(primary)
            if self._mixed_mp3_path:
                out["mixed_mp3"] = self._mixed_mp3_path
            if self._stereo_mp3_path:
                out["playback_stereo_mp3"] = self._stereo_mp3_path
            if "playback_wav" not in out and mixed_cand and mixed_cand.is_file():
                out["playback_wav"] = str(mixed_cand)
        return out


def _parse_log_id_date(session_id: str) -> str | None:
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", session_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _search_recording_dirs(
    stem: str,
    roots: list[Path],
    date_hint: str | None = None,
    scan_recent_days: int = 31,
) -> Path | None:
    """Search for full/mixed/outbound/inbound recordings (prefer longest / full)."""
    from datetime import datetime, timedelta

    suffixes = ("_vobiz", "_mixed", "_full", "_outbound", "_inbound", "_stereo")
    ext_order = _PLAYBACK_EXT_ORDER

    def _scan_day_dir(day_dir: Path) -> Path | None:
        if not day_dir.is_dir():
            return None
        for name in _playback_name_candidates(stem):
            cand = day_dir / name
            if cand.is_file() and cand.stat().st_size > 44:
                return cand
        return None

    def _scan_named_subdirs(day_dir: Path) -> Path | None:
        if not day_dir.is_dir():
            return None
        for sub in sorted(day_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not sub.is_dir():
                continue
            found = _scan_day_dir(sub)
            if found:
                return found
            for ext in ext_order:
                for p in sub.glob(f"*{stem}*full{ext}"):
                    if p.is_file() and p.stat().st_size > 44:
                        return p
        return None

    for root in roots:
        if not root.is_dir():
            continue
        for sfx in suffixes:
            for ext in ext_order:
                cand = root / f"{stem}{sfx}{ext}"
                if cand.is_file() and cand.stat().st_size > 44:
                    return cand
        if date_hint:
            try:
                base_date = datetime.strptime(date_hint, "%Y-%m-%d").date()
                day_candidates = [
                    (base_date + timedelta(days=delta)).isoformat()
                    for delta in (0, -1, 1, -2, 2)
                ]
            except ValueError:
                day_candidates = [date_hint]
            for day in day_candidates:
                found = _scan_day_dir(root / day) or _scan_named_subdirs(root / day)
                if found:
                    return found
        dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and len(p.name) == 10),
            key=lambda p: p.name,
            reverse=True,
        )
        for day in dirs[: max(7, scan_recent_days)]:
            found = _scan_day_dir(day) or _scan_named_subdirs(day)
            if found:
                return found
    return None


def recording_search_roots(base_dir: Optional[str | Path] = None) -> list[Path]:
    import os

    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            return
        if key in seen:
            return
        if p.is_dir():
            seen.add(key)
            roots.append(p)

    session_base, archive_base = _recording_roots(base_dir)
    _add(session_base)
    _add(archive_base)
    if base_dir:
        _add(Path(base_dir))
    else:
        for extra in (os.getenv("CALL_RECORDING_EXTRA_DIRS") or "").split(","):
            extra = extra.strip()
            if extra:
                _add(Path(extra))
        for candidate in (
            "/root/technopolis/backend/data/call_recordings",
            "/root/technopolis/agent/data/call_recordings",
        ):
            _add(Path(candidate))
        _recordings_dir = Path(__file__).resolve().parent.parent / "data" / "recordings"
        _add(_recordings_dir)
    return roots


def resolve_session_recording_path(
    session_id: str,
    base_dir: Optional[str | Path] = None,
    *,
    scan_recent_days: int = 60,
) -> Path | None:
    stem = _safe_stem(session_id.strip())
    if not stem:
        return None
    date_hint = _parse_log_id_date(session_id)
    found = _search_recording_dirs(
        stem, recording_search_roots(base_dir), date_hint, scan_recent_days
    )
    if found:
        return found
    # Early sessions often logged call_recording=false while per-leg WAVs still exist
    # at paths recorded in live JSONL session meta.
    return resolve_recording_from_session_meta(session_id)


def resolve_vobiz_recording_path(
    session_id: str,
    base_dir: Optional[str | Path] = None,
    *,
    scan_recent_days: int = 60,
) -> Path | None:
    """Return carrier-side ``{log_id}_vobiz.wav`` when ingested from Vobiz webhook."""
    stem = _safe_stem((session_id or "").strip())
    if not stem:
        return None
    date_hint = _parse_log_id_date(session_id)
    for root in recording_search_roots(base_dir):
        for ext in (".wav", ".mp3"):
            direct = root / f"{stem}_vobiz{ext}"
            if direct.is_file() and direct.stat().st_size > 128:
                return direct
        for day_dir in _iter_day_dirs(root, date_hint, scan_recent_days=scan_recent_days):
            for ext in (".wav", ".mp3"):
                cand = day_dir / f"{stem}_vobiz{ext}"
                if cand.is_file() and cand.stat().st_size > 128:
                    return cand
            if day_dir.is_dir():
                for sub in day_dir.iterdir():
                    if not sub.is_dir():
                        continue
                    for ext in (".wav", ".mp3"):
                        cand = sub / f"{stem}_vobiz{ext}"
                        if cand.is_file() and cand.stat().st_size > 128:
                            return cand
    return None


def resolve_dashboard_recording_path(
    session_id: str,
    base_dir: Optional[str | Path] = None,
    *,
    scan_recent_days: int = 60,
) -> Path | None:
    """Playback path for dashboard Play buttons.

    In Vobiz-only mode, return only carrier ``{log_id}_vobiz.*`` — never noisy
    local ``_mixed`` / stem WAVs from the old bridge recorder.
    """
    if _vobiz_only_recording():
        return resolve_vobiz_recording_path(
            session_id, base_dir, scan_recent_days=scan_recent_days
        )
    vobiz = resolve_vobiz_recording_path(
        session_id, base_dir, scan_recent_days=scan_recent_days
    )
    if vobiz:
        return vobiz
    return resolve_session_recording_path(
        session_id, base_dir, scan_recent_days=scan_recent_days
    )


def _find_stem_recording_paths(session_id: str) -> tuple[Path | None, Path | None, Path | None]:
    """Locate inbound/outbound stem WAVs and their parent folder for a session."""
    stem = _safe_stem((session_id or "").strip())
    if not stem:
        return None, None, None
    date_hint = _parse_log_id_date(session_id)
    in_path = out_path = None
    parent: Path | None = None
    for root in recording_search_roots():
        for day_dir in _iter_day_dirs(root, date_hint, scan_recent_days=60):
            candidates = [day_dir]
            if day_dir.is_dir():
                candidates.extend(p for p in day_dir.iterdir() if p.is_dir())
            for folder in candidates:
                ip = folder / f"{stem}_inbound.wav"
                op = folder / f"{stem}_outbound.wav"
                if ip.is_file():
                    in_path = ip
                    parent = folder
                if op.is_file():
                    out_path = op
                    parent = folder
                if in_path or out_path:
                    return in_path, out_path, parent
    return in_path, out_path, parent


def build_local_mixed_wav_from_stems(session_id: str) -> Path | None:
    """Build ``_mixed.wav`` from inbound/outbound stems when Vobiz webhook is missing."""
    in_path, out_path, parent = _find_stem_recording_paths(session_id)
    if not parent or (not in_path and not out_path):
        return None
    stem = _safe_stem((session_id or "").strip())
    mixed_path = parent / f"{stem}_mixed.wav"
    if mixed_path.is_file() and mixed_path.stat().st_size > 128:
        return mixed_path
    in_frames = out_frames = b""
    for path, assign in ((in_path, "in"), (out_path, "out")):
        if not path:
            continue
        try:
            with wave.open(str(path), "rb") as w:
                frames = w.readframes(w.getnframes())
        except (OSError, wave.Error) as exc:
            logger.warning("Local mixed fallback: cannot read {}: {}", path, exc)
            continue
        if assign == "in":
            in_frames = frames
        else:
            out_frames = frames
    if not in_frames and not out_frames:
        return None
    mixer = CallRecorder.__new__(CallRecorder)
    mixer._frame_bytes = _pcm_frame_bytes()
    mixer._last_mix_diag = {}
    mixed = mixer._mix_duplex_pcm(in_frames, out_frames)
    if not mixed:
        return None
    try:
        with wave.open(str(mixed_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16_000)
            w.writeframes(mixed)
        logger.info("Call recording: local mixed fallback written {} ({} B)", mixed_path, len(mixed))
        return mixed_path
    except OSError as exc:
        logger.warning("Local mixed fallback write failed: {}", exc)
        return None


async def wait_for_vobiz_trunk_recording(
    session_id: str,
    *,
    timeout_sec: float | None = None,
    poll_sec: float = 2.0,
) -> Path | None:
    """Poll until Vobiz trunk WAV is ingested or timeout."""
    import asyncio
    import time

    from config import settings

    if not getattr(settings, "vobiz_trunk_recording_enabled", True):
        return None
    wait = timeout_sec
    if wait is None:
        wait = float(getattr(settings, "vobiz_trunk_recording_wait_sec", 25) or 25)
    if wait <= 0:
        return resolve_vobiz_recording_path(session_id)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        found = resolve_vobiz_recording_path(session_id)
        if found:
            logger.info("Vobiz trunk recording ready log_id={} path={}", session_id, found)
            return found
        await asyncio.sleep(max(0.5, poll_sec))
    logger.info(
        "Vobiz trunk recording not received within {:.0f}s for log_id={}",
        wait,
        session_id,
    )
    return None


async def fetch_vobiz_recording_if_missing(
    session_id: str,
    *,
    camp_id: str = "",
    initial_delay_sec: float = 0.0,
) -> Path | None:
    """Return dashboard playback path, pulling from Vobiz Recording API when absent."""
    log_id = (session_id or "").strip()
    if not log_id:
        return None
    found = resolve_dashboard_recording_path(log_id)
    if found:
        return found
    try:
        from config import settings

        if not getattr(settings, "vobiz_trunk_recording_enabled", True):
            return None
        from services.vobiz_bridge.vobiz_recording import ensure_vobiz_application_recording

        await ensure_vobiz_application_recording(
            log_id,
            camp_id=camp_id,
            initial_delay_sec=initial_delay_sec,
        )
    except Exception as exc:
        logger.warning("Vobiz recording fetch failed log_id={}: {}", log_id, exc)
    return resolve_dashboard_recording_path(log_id)


async def prepare_playback_recording(session_id: str, *, camp_id: str = "") -> Path | None:
    """Wait for Vobiz Application recording; actively fetch from Vobiz API when missing."""
    from config import settings

    vobiz_only = _vobiz_only_recording()
    if getattr(settings, "vobiz_trunk_recording_enabled", True) and (
        vobiz_only or getattr(settings, "vobiz_trunk_recording_prefer_playback", True)
    ):
        existing = resolve_vobiz_recording_path(session_id)
        if not existing:
            try:
                from services.vobiz_bridge.vobiz_recording import ensure_vobiz_application_recording

                await ensure_vobiz_application_recording(session_id, camp_id=camp_id)
            except Exception as exc:
                logger.warning("Vobiz application recording fetch failed log_id={}: {}", session_id, exc)
        vobiz = await wait_for_vobiz_trunk_recording(session_id)
        if vobiz:
            return vobiz
        if vobiz_only:
            logger.warning(
                "No Vobiz trunk recording for log_id={} (webhook/API ingest pending or failed)",
                session_id,
            )
            return None
        built = build_local_mixed_wav_from_stems(session_id)
        if built:
            logger.warning(
                "Playback using local mixed fallback (no Vobiz trunk recording) log_id={}",
                session_id,
            )
            return built
    if vobiz_only:
        return resolve_vobiz_recording_path(session_id)
    return resolve_dashboard_recording_path(session_id)


def resolve_recording_from_session_meta(log_id: str) -> Path | None:
    """Recover playback WAV from live JSONL session meta paths when standard lookup fails.

    Early calls often have ``call_recording: false`` in meta while ``inbound_wav`` /
    ``outbound_wav`` / ``full_wav`` / ``playback_wav`` files still exist on disk.
    """
    log_id = (log_id or "").strip()
    if not log_id:
        return None
    date_hint = _parse_log_id_date(log_id)
    backend_dir = Path(__file__).resolve().parent.parent
    conv_base = Path(settings.conversation_log_dir)
    if not conv_base.is_absolute():
        conv_base = backend_dir / conv_base

    candidates: list[Path] = []
    if date_hint:
        candidates.append(conv_base / date_hint / f"{log_id}.jsonl")
        for role in ("sales_1",):
            candidates.append(backend_dir / "data" / role / "logs" / date_hint / f"{log_id}.jsonl")
    # Recent-day fallback
    if conv_base.is_dir():
        for day_dir in sorted(
            (p for p in conv_base.iterdir() if p.is_dir() and len(p.name) == 10),
            reverse=True,
        )[:60]:
            p = day_dir / f"{log_id}.jsonl"
            if p.is_file():
                candidates.append(p)
                break

    meta_keys = (
        "mixed_wav",
        "playback_wav",
        "full_wav",
        "playback_path",
        "outbound_wav",
        "inbound_wav",
        "archive_wav",
        "mixed_mp3",
        "playback_mp3",
        "playback_stereo_mp3",
        "stereo_mp3",
    )
    for jsonl_path in candidates:
        if not jsonl_path.is_file():
            continue
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    low = line.lower()
                    if not line or not any(k in low for k in ("playback", "mixed", ".wav", ".mp3")):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    meta = obj.get("meta") if isinstance(obj, dict) else None
                    if not isinstance(meta, dict):
                        continue
                    for key in meta_keys:
                        raw = str(meta.get(key) or "").strip()
                        if not raw:
                            continue
                        p = Path(raw)
                        if p.is_file() and p.stat().st_size > 44:
                            return p
        except OSError:
            continue
    return None


def list_recording_days(base_dir: Optional[str] = None) -> list[str]:
    base = Path(base_dir or settings.call_recording_dir).resolve()
    if not base.is_dir():
        return []
    return sorted(
        [p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10],
        reverse=True,
    )


def list_recordings_wavs(day: str, base_dir: Optional[str] = None) -> list[str]:
    d = Path(base_dir or settings.call_recording_dir).resolve() / day
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.wav"))


def recording_duration_sec(log_id: str, base_dir: Optional[str | Path] = None) -> float | None:
    """Longest mixed/full recording duration for a session (seconds), or None."""
    stem = _safe_stem((log_id or "").strip())
    if not stem:
        return None
    date_hint = _parse_log_id_date(log_id)
    roots = recording_search_roots(base_dir)
    best: float | None = None
    for root in roots:
        for day_dir in _iter_day_dirs(root, date_hint, scan_recent_days=60):
            for name in _playback_name_candidates(stem):
                p = day_dir / name
                if not p.is_file():
                    continue
                sec = _audio_duration_sec(p)
                if sec is not None and (best is None or sec > best):
                    best = sec
    if best is not None:
        return best
    recovered = resolve_recording_from_session_meta(log_id)
    if recovered and recovered.is_file():
        return _audio_duration_sec(recovered)
    return None


def _iter_day_dirs(root: Path, date_hint: str | None, *, scan_recent_days: int):
    if date_hint:
        d = root / date_hint
        if d.is_dir():
            yield d
    if not root.is_dir():
        return
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and len(p.name) == 10),
        reverse=True,
    )
    for p in dirs[:scan_recent_days]:
        if date_hint and p.name == date_hint:
            continue
        yield p


def resolve_recording_file(day: str, filename: str, base_dir: Optional[str] = None) -> Optional[Path]:
    if not day or len(day) != 10 or ".." in day or "/" in day or "\\" in day:
        return None
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    safe = Path(filename).name
    if safe != filename or not (safe.lower().endswith(".wav") or safe.lower().endswith(".mp3")):
        return None

    base_root = Path(base_dir or settings.call_recording_dir).resolve()
    p = (base_root / day / safe).resolve()
    if not p.is_file():
        stem = safe.rsplit(".", 1)[0]
        for name in (
            f"{stem}_stereo.mp3",
            f"{stem}_mixed.mp3",
            f"{stem}_full.mp3",
            f"{stem}_mixed.wav",
            f"{stem}_full.wav",
        ):
            cand = (base_root / day / name).resolve()
            if cand.is_file():
                p = cand
                break

    root = (base_root / day).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


def attach_vobiz_trunk_recording(
    log_id: str,
    *,
    suffix_ext: str = ".wav",
    role: str = "",
    camp_id: str = "",
    call_uuid: str = "",
    recording_id: str = "",
) -> Path | None:
    """Reserve destination path for a Vobiz trunk recording download."""
    session_id = (log_id or "").strip()
    if not session_id:
        return None
    stem = _safe_stem(session_id)
    ext = suffix_ext if suffix_ext.startswith(".") else f".{suffix_ext}"
    if ext.lower() not in (".wav", ".mp3"):
        ext = ".wav"

    session_base, _archive_base = _recording_roots()
    date_hint = _parse_log_id_date(session_id)
    day_dir = session_base / date_hint if date_hint else _ist_day_dir(session_base)

    phone = ""
    name = ""
    if camp_id:
        try:
            from core.state import _CAMPAIGN_DATA

            info = _CAMPAIGN_DATA.get(camp_id) or {}
            if isinstance(info, dict):
                phone = str(info.get("_answered_phone") or info.get("phone") or "")
                name = str(info.get("name") or "")
        except Exception:
            pass

    if phone or name:
        folder = day_dir / _safe_folder_name(name, phone)
    else:
        folder = day_dir
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{stem}_vobiz{ext}"
    meta = {
        "session_id": session_id,
        "camp_id": camp_id,
        "role": role,
        "call_uuid": call_uuid,
        "recording_id": recording_id,
        "dest": str(dest),
    }
    try:
        dest.with_suffix(".vobiz_pending.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return dest
