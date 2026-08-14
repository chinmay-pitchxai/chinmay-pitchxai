"""Per-call WebSocket session flags (Vobiz + Gemini coordination)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.call_recording import CallRecorder


@dataclass
class VobizSessionState:
    call_id: str = ""
    stream_id: str = ""
    log_session_id: str = ""
    call_recorder: Optional[CallRecorder] = None
    frame_ms: int = 20
    gemini_silence_kick_sent: bool = False
    recorded_greeting_nudge_sent: bool = False
    whatsapp_sent: bool = False
    email_sent: bool = False
    prefer_whatsapp_only: bool = False
