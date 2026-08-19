"""Capture opening audio from Gemini Live (native voice) for greeting_{role}.pcm."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Optional

import websockets as ws_client
from loguru import logger

from config import settings
from core.greeting_pcm import STORED_GREETING_DEFAULT_SR, _greetings_base_dir, greeting_pcm_paths
from core.state import get_state, normalize_console_role
from prompts.role_prompts import build_role_system_prompt
from services.vobiz_bridge.constants import GEMINI_OUT_SR
from core.gemini_auth import gemini_auth_headers, get_gemini_api_key
from services.vobiz_bridge.gemini_protocol import (
    build_live_setup,
    gemini_live_ws_url,
    gemini_send_live_opening_turn_nudge,
    gemini_send_pcm_silence_kick,
)
from services.vobiz_bridge.turn_taking_addon import apply_live_voice_turn_addon


def _append_opening_instruction(system_prompt: str, opening_line: str, role: str = "") -> str:
    line = (opening_line or "").strip()
    if not line:
        return system_prompt
    
    style_prompt = getattr(settings, "gemini_opening_style_prompt_female", "").strip()

    opening_style = f"\n\n[OPENING AUDIO STYLE & ACCENT]\n{style_prompt}" if style_prompt else ""
    
    return (
        system_prompt
        + opening_style
        + "\n\n[OPENING — YOUR FIRST SPOKEN UTTERANCE ON THIS CALL]\n"
        "You begin the conversation now. Your first audible reply must follow this scripted "
        "opening faithfully (adapt only pacing and natural delivery in the caller's language; "
        "keep names and factual content).\n"
        "IMPORTANT PACING: Speak SLOWLY and clearly. Pause briefly after the company name "
        "and after the project name. Do NOT rush — deliver like a real calm phone call. "
        "Take about 5-6 seconds for the full sentence.\n\""
        + line
        + "\""
    )




def _extract_model_audio_pcm(obj: dict) -> bytes:
    sc = obj.get("serverContent") or {}
    mt = sc.get("modelTurn") or {}
    chunks: list[bytes] = []
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
            chunks.append(base64.b64decode(b64_in))
        except Exception:
            continue
    return b"".join(chunks)


async def capture_phrase_pcm(
    role: str,
    phrase: str,
    *,
    timeout_sec: float = 25.0,
) -> tuple[bytes, int]:
    """Capture a short fixed phrase (e.g. name verify) with minimal prompt — no role script examples."""
    role = normalize_console_role(role)
    phrase = (phrase or "").strip()
    if not phrase:
        raise ValueError("phrase is required")

    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    from core.state import resolved_live_voice_profile

    voice, voice_style = resolved_live_voice_profile(role)
    model = settings.gemini_live_model

    system_prompt = (
        "You are a voice agent on a phone call. Speak ONLY the exact sentence you are given. "
        "One sentence, natural Indian English delivery, then stop."
    )
    if voice_style:
        system_prompt += f"\nVoice delivery: {voice_style}"
    setup = build_live_setup(
        model=model,
        system_instruction=system_prompt,
        voice=voice,
        vad_ultra=False,
    )

    gemini_url = gemini_live_ws_url()
    collected = bytearray()

    async def _collect_turn(gem: Any) -> None:
        async for raw in gem:
            try:
                obj = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if obj.get("error"):
                raise RuntimeError(f"Gemini Live error: {obj.get('error')}")
            pcm = _extract_model_audio_pcm(obj)
            if pcm:
                collected.extend(pcm)
            sc = obj.get("serverContent") or {}
            if sc.get("turnComplete") or sc.get("generationComplete"):
                return

    async with ws_client.connect(
        gemini_url,
        extra_headers=gemini_auth_headers(api_key),
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        close_timeout=2,
    ) as gem:
        await gem.send(json.dumps(setup))
        await gemini_send_pcm_silence_kick(gem, duration_ms=120)
        await gem.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [
                            {
                                "role": "user",
                                "parts": [{"text": f"Speak aloud exactly: \"{phrase}\""}],
                            }
                        ],
                        "turnComplete": True,
                    }
                }
            )
        )
        await asyncio.wait_for(_collect_turn(gem), timeout=timeout_sec)

    if len(collected) < 400:
        raise RuntimeError(f"Captured phrase too short ({len(collected)} bytes)")
    sr = int(GEMINI_OUT_SR)
    logger.info("Phrase capture: role={} phrase_len={} pcm_bytes={}", role, len(phrase), len(collected))
    return bytes(collected), sr


async def capture_live_greeting_pcm(
    role: str,
    greeting_text: str,
    *,
    timeout_sec: float = 50.0,
) -> tuple[bytes, int]:
    """
    Capture prerecorded greeting intro ONLY — minimal prompt, no role script.

    Must NOT use the full sales system prompt (it injects name-verify and random names).
    Name verification is always a separate live Gemini turn with authoritative lead name.
    """
    from core.greeting_text_utils import intro_only_greeting

    role = normalize_console_role(role)
    opening = intro_only_greeting((greeting_text or "").strip())
    if not opening:
        raise ValueError("greeting_text is required")

    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not configured")

    from core.state import resolved_live_voice_profile

    voice, voice_style = resolved_live_voice_profile(role)
    model = settings.gemini_live_model

    style = voice_style

    system_prompt = (
        "You are recording one prerecorded phone greeting for a real-estate outbound call.\n"
        "Speak ONLY the exact intro sentence given — nothing before it, nothing after it.\n"
        "Do NOT ask the callee's name. Do NOT say 'Am I speaking with…'. Do NOT add questions.\n"
        "Deliver like a calm Bangalore sales professional on a desk phone: warm, clear, unhurried.\n"
        "Pause briefly after the company name and after the project name. About 6 seconds total.\n"
    )
    if style:
        system_prompt += f"\nVoice style: {style}\n"

    setup = build_live_setup(
        model=model,
        system_instruction=system_prompt,
        voice=voice,
        vad_ultra=False,
    )

    gemini_url = gemini_live_ws_url()
    collected = bytearray()

    async def _collect_turn(gem: Any) -> None:
        async for raw in gem:
            try:
                obj = (
                    json.loads(raw)
                    if isinstance(raw, str)
                    else json.loads(raw.decode("utf-8"))
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if obj.get("error"):
                raise RuntimeError(f"Gemini Live error: {obj.get('error')}")
            pcm = _extract_model_audio_pcm(obj)
            if pcm:
                collected.extend(pcm)
            sc = obj.get("serverContent") or {}
            if sc.get("turnComplete") or sc.get("generationComplete"):
                return

    logger.info(
        "Live greeting capture (intro-only): role={} voice={} text_len={}",
        role,
        voice,
        len(opening),
    )

    async with ws_client.connect(
        gemini_url,
        extra_headers=gemini_auth_headers(api_key),
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        close_timeout=2,
    ) as gem:
        await gem.send(json.dumps(setup))
        await gemini_send_pcm_silence_kick(gem, duration_ms=120)
        await gem.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [
                            {
                                "role": "user",
                                "parts": [
                                    {
                                        "text": (
                                            f'Speak aloud exactly this intro — no name question, no extra words: '
                                            f'"{opening}"'
                                        )
                                    }
                                ],
                            }
                        ],
                        "turnComplete": True,
                    }
                }
            )
        )
        try:
            await asyncio.wait_for(_collect_turn(gem), timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Gemini Live did not finish the opening within {timeout_sec:.0f}s"
            ) from exc

    if len(collected) < 800:
        raise RuntimeError(
            "Captured audio too short — check GEMINI_API_KEY, model access, and greeting text"
        )

    sr = int(GEMINI_OUT_SR)
    logger.info("Live greeting capture: role={} collected {} bytes @ {} Hz", role, len(collected), sr)
    return bytes(collected), sr


def save_greeting_pcm_file(
    role: str,
    pcm: bytes,
    sample_rate: int,
    *,
    variant: str = "",
    greeting_text: str = "",
) -> Path:
    """Write ``greeting_{role}[_variant].pcm`` + ``.pcm.meta`` under data/greetings/."""
    import hashlib

    role = normalize_console_role(role)
    out_path, meta_path = greeting_pcm_paths(role, variant)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pcm)
    meta = {
        "sr": int(sample_rate or STORED_GREETING_DEFAULT_SR),
        "source": "gemini_live_capture",
        "voice": settings.gemini_live_voice,
        "model": settings.gemini_live_model,
        "intro_only": True,
    }
    txt = (greeting_text or "").strip()
    if txt:
        meta["text_hash"] = hashlib.md5(txt.encode()).hexdigest()[:16]
    if variant:
        meta["variant"] = variant
    meta_path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    logger.info("Saved Live greeting PCM: {} ({} bytes)", out_path, len(pcm))
    return out_path
