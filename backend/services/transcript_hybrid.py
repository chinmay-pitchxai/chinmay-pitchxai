"""Build post-call transcript: prefer coalesced live JSONL over mixed-audio diarization."""

from __future__ import annotations

import json
import os
import re
from typing import Callable

from loguru import logger

_MIN_MEANINGFUL_TURNS = 2
_MIN_TURN_CHARS = 3


def _is_meaningful(content: str) -> bool:
    t = (content or "").strip()
    if len(t) < _MIN_TURN_CHARS:
        return False
    low = t.lower().strip(".,?! ")
    if low in ("silence", "noise", "[silence]", "[noise]", "…"):
        return False
    if re.fullmatch(r"(hello\.?|hi\.?|yeah\.?|yes\.?|ok\.?|okay\.?)+", low):
        return len(t) > 12
    return True


def coalesce_jsonl_turns(raw_jsonl: str) -> list[dict]:
    """Merge consecutive same-role fragments from live session logging."""
    turns: list[dict] = []
    for line in (raw_jsonl or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = str(obj.get("role") or obj.get("type") or "").strip().lower()
        content = str(obj.get("content") or obj.get("text") or obj.get("message") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if turns and turns[-1]["role"] == role:
            prev = turns[-1]["content"]
            if content.startswith(prev) or prev.startswith(content):
                turns[-1]["content"] = content if len(content) > len(prev) else prev
            else:
                turns[-1]["content"] = f"{prev} {content}".strip()
        else:
            turns.append({"role": role, "content": content})
    return turns


def jsonl_from_turns(turns: list[dict]) -> str:
    return "\n".join(json.dumps(t, ensure_ascii=False) for t in turns if t.get("content"))


def meaningful_turn_count(turns: list[dict]) -> int:
    return sum(1 for t in turns if _is_meaningful(str(t.get("content") or "")))


def live_jsonl_is_voicemail(turns: list[dict]) -> bool:
    """True when live session JSONL is a carrier/phone voicemail prompt (even 1–2 turns)."""
    if not turns:
        return False
    from services.transcript_interest import is_voicemail_or_screening_transcript

    return is_voicemail_or_screening_transcript(jsonl_from_turns(turns))


def _user_turn_stats(turns: list[dict]) -> tuple[int, int, int]:
    user_n = 0
    user_chars = 0
    for t in turns:
        if str(t.get("role") or "").lower() != "user":
            continue
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        user_n += 1
        user_chars += len(content)
    return user_n, user_chars, len(turns)


def merge_live_and_audio_turns(live: list[dict], audio: list[dict]) -> list[dict]:
    """Merge live + audio; the recording is authoritative when live STT missed user speech."""
    live_user, _, _ = _user_turn_stats(live)
    audio_user, _, _ = _user_turn_stats(audio)
    if not audio:
        return live
    if not live:
        return audio
    if live_user >= 1 and meaningful_turn_count(live) >= _MIN_MEANINGFUL_TURNS:
        return live
    if audio_user > live_user:
        return audio
    return live


async def build_call_transcript(
    *,
    log_id: str,
    role: str,
    read_jsonl: Callable[[str, str], str],
    transcribe_audio: Callable,
    agent_name: str = "",
) -> tuple[str, str]:
    """Return (transcript_jsonl, source) where source is live_jsonl | audio | empty."""
    raw_jsonl = (read_jsonl(role, log_id) or "").strip()
    coalesced = coalesce_jsonl_turns(raw_jsonl)
    if coalesced:
        from services.transcript_roles import fix_transcript_speaker_roles

        coalesced = fix_transcript_speaker_roles(coalesced, agent_name=agent_name)
        # Voicemail prompts are short (1–2 turns) but must not fall through to audio
        # transcription, which often hallucinates a full sales conversation.
        if live_jsonl_is_voicemail(coalesced):
            out = jsonl_from_turns(coalesced)
            logger.info(
                "Hybrid transcript: voicemail on live JSONL ({} turns) — skip audio log_id={}",
                len(coalesced),
                log_id,
            )
            return out, "live_jsonl_voicemail"

        n = meaningful_turn_count(coalesced)
        user_n, user_chars, _ = _user_turn_stats(coalesced)
        asst_n = sum(
            1 for t in coalesced
            if t.get("role") == "assistant" and _is_meaningful(str(t.get("content") or ""))
        )
        live_sufficient = n >= _MIN_MEANINGFUL_TURNS or (
            user_n >= 1 and asst_n >= 1 and len(coalesced) >= 2
        )
        if live_sufficient:
            out = jsonl_from_turns(coalesced)
            try:
                from debug_agent_log import agent_debug

                agent_debug(
                    "H3",
                    "transcript_hybrid.py:build_call_transcript",
                    "using_live_jsonl",
                    {"log_id": log_id, "turns": len(coalesced), "meaningful": n},
                )
            except Exception:
                pass
            logger.info(
                "Hybrid transcript: using live JSONL ({} coalesced turns, {} meaningful) log_id={}",
                len(coalesced),
                n,
                log_id,
            )
            return out, "live_jsonl"

    audio_text = ""
    try:
        from services.call_recording import recording_duration_sec

        dur = recording_duration_sec(log_id)
        min_sec = float(os.getenv("CALL_RECORDING_MIN_TRANSCRIBE_SEC", "5"))
        force_audio = bool(coalesced) and _user_turn_stats(coalesced)[0] == 0 and dur is not None and dur >= min_sec
        if dur is not None and dur < min_sec and not force_audio:
            logger.info(
                "Hybrid transcript: skip audio (recording {:.1f}s < {:.0f}s) log_id={}",
                dur,
                min_sec,
                log_id,
            )
        else:
            if force_audio:
                logger.info(
                    "Hybrid transcript: live JSONL has no user turns — re-transcribing audio log_id={}",
                    log_id,
                )
            transcribed = await transcribe_audio(log_id, role)
            if (transcribed or "").strip():
                audio_text = transcribed.strip()
    except Exception as exc:
        logger.warning("Hybrid transcript: audio transcription failed log_id={}: {}", log_id, exc)

    if audio_text:
        audio_turns = coalesce_jsonl_turns(audio_text)
        live_user_n = _user_turn_stats(coalesced)[0] if coalesced else 0
        audio_user_n = _user_turn_stats(audio_turns)[0] if audio_turns else 0
        if coalesced and audio_turns:
            merged = merge_live_and_audio_turns(coalesced, audio_turns)
            out = jsonl_from_turns(merged)
            source = "live_jsonl_merged" if merged is not audio_turns else "audio"
            logger.info(
                "Hybrid transcript: merged live+audio ({} turns) source={} log_id={}",
                len(merged),
                source,
                log_id,
            )
            return out, source
        if coalesced and live_user_n == 0 and audio_user_n >= 1:
            logger.info(
                "Hybrid transcript: live STT captured no user turns — using recording transcription log_id={}",
                log_id,
            )
            return audio_text, "audio"
        logger.info("Hybrid transcript: using audio transcription log_id={}", log_id)
        return audio_text, "audio"

    if coalesced:
        user_n, user_chars, _ = _user_turn_stats(coalesced)
        out = jsonl_from_turns(coalesced)
        logger.info(
            "Hybrid transcript: live JSONL fallback ({} turns, user_chars={}) log_id={}",
            len(coalesced),
            user_chars,
            log_id,
        )
        return out, "live_jsonl_short"

    if raw_jsonl:
        return raw_jsonl, "live_jsonl_raw"

    return "", "empty"
