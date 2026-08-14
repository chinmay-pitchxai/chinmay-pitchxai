"""Append-only JSONL conversation logs (user + assistant turns) per session.

Files: ``<CONVERSATION_LOG_DIR>/<YYYY-MM-DD>/<session_id>.jsonl``

Enable with ``CONVERSATION_LOG_ENABLED=1`` (default on). Disable with ``0``.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import settings

_lock = threading.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id(prefix: str) -> str:
    """Unique id for one voice session (browser, Vobiz, Twilio)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:8]}"


def _log_dir_for_session(session_id: str, base_dir: Optional[str] = None) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = Path(base_dir or settings.conversation_log_dir).resolve()
    return base / day


def append_session_meta(session_id: str, channel: str, base_dir: Optional[str] = None, **meta: Any) -> None:
    """Record session start / context (call id, stream, etc.)."""
    if not settings.conversation_log_enabled:
        return
    record: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "session_id": session_id,
        "channel": channel,
        "type": "session",
        "meta": {k: v for k, v in meta.items() if v is not None and v != ""},
    }
    _write_record(session_id, record, base_dir)


def append_artifact(
    session_id: str,
    channel: str,
    kind: str,
    summary: str,
    base_dir: Optional[str] = None,
    **extra: Any,
) -> None:
    """Log a non-turn event (e.g. RAG injection) for the Transcripts / JSONL view."""
    if not settings.conversation_log_enabled:
        return
    text = (summary or "").strip()
    if not text:
        return
    record: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "session_id": session_id,
        "channel": channel,
        "type": "artifact",
        "kind": kind,
        "content": text[:2000],
    }
    if extra:
        record["extra"] = {k: v for k, v in extra.items() if v is not None and v != ""}
    _write_record(session_id, record, base_dir)


def append_turn(
    session_id: str,
    role: str,
    content: str,
    channel: str,
    base_dir: Optional[str] = None,
    **extra: Any,
) -> None:
    """Log one user or assistant message."""
    if not settings.conversation_log_enabled:
        return
    text = (content or "").strip()
    if not text:
        return
    record: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "session_id": session_id,
        "channel": channel,
        "type": "turn",
        "role": role,
        "content": text,
    }
    if extra:
        record["extra"] = {k: v for k, v in extra.items() if v is not None}
    _write_record(session_id, record, base_dir)


def _write_record(session_id: str, record: dict[str, Any], base_dir: Optional[str] = None) -> None:
    primary = Path(base_dir or settings.conversation_log_dir).resolve()
    d = _log_dir_for_session(session_id, str(primary))
    path = d / f"{_safe_filename(session_id)}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    # Mirror to canonical conversation_log_dir when writing to role-specific logs (Vobiz live).
    canonical = Path(settings.conversation_log_dir).resolve()
    if primary != canonical:
        cd = _log_dir_for_session(session_id, str(canonical))
        cpath = cd / f"{_safe_filename(session_id)}.jsonl"
        with _lock:
            cd.mkdir(parents=True, exist_ok=True)
            with open(cpath, "a", encoding="utf-8") as f:
                f.write(line)


def _safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:180]


def list_days(base_dir: Optional[str] = None) -> list[str]:
    """ISO date strings (newest first) that have log subdirs."""
    base = Path(base_dir or settings.conversation_log_dir).resolve()
    if not base.is_dir():
        return []
    days = sorted(
        [p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10],
        reverse=True,
    )
    return days


def list_sessions(day: str, base_dir: Optional[str] = None) -> list[str]:
    """Session ids (file stems) for a given YYYY-MM-DD."""
    base = Path(base_dir or settings.conversation_log_dir).resolve() / day
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.jsonl"))


def read_session_log(day: str, session_id: str, base_dir: Optional[str] = None) -> str:
    """Raw JSONL text for a session file."""
    safe = _safe_filename(session_id)
    path = Path(base_dir or settings.conversation_log_dir).resolve() / day / f"{safe}.jsonl"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


# --- Pipecat hooks (browser / Twilio) ------------------------------------------

try:
    from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class ConversationUserLogger(FrameProcessor):
        """After STT (+ optional RAG): log user transcription."""

        def __init__(self, session_id: str, channel: str) -> None:
            super().__init__()
            self._session_id = session_id
            self._channel = channel

        async def process_frame(self, frame, direction) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                t = (frame.text or "").strip()
                if t:
                    append_turn(self._session_id, "user", t, self._channel)
            await self.push_frame(frame, direction)


    class ConversationAssistantLogger(FrameProcessor):
        """After LLM: log assistant text (same text sent to TTS)."""

        def __init__(self, session_id: str, channel: str) -> None:
            super().__init__()
            self._session_id = session_id
            self._channel = channel

        async def process_frame(self, frame, direction) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TextFrame):
                t = (frame.text or "").strip()
                if t:
                    append_turn(self._session_id, "assistant", t, self._channel)
            await self.push_frame(frame, direction)

except Exception:
    # Pipecat not installed or incompatible (e.g. Python 3.9 syntax errors)
    ConversationUserLogger = None
    ConversationAssistantLogger = None
