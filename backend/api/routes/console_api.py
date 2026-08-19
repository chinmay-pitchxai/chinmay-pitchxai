from __future__ import annotations

import io
import json
import os
import re
import time
import uuid
import wave
from pathlib import Path

import httpx
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Depends
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from loguru import logger

from core.auth import get_current_user, _decode_jwt
from core.state import get_state, save_role_state, normalize_console_role, resolved_greeting_text, _CAMPAIGN_DATA
from core.utils import range_file_response
from config import settings
from core.outbound_numbers import resolve_outbound_from_number
from core.vobiz_credentials import resolve_vobiz_credentials


def _role_from_jwt(request: Request) -> str | None:
    """Extract role from JWT Authorization header, or None."""
    from core.auth import dashboard_role_for_token

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload and payload.get("role"):
            return dashboard_role_for_token(
                payload.get("email"),
                payload.get("role"),
            )
    return None


def _role_from_request(request: Request, default: str = "sales_1") -> str:
    """JWT role (all sandboxes resolve to the single Technopolis console role)."""
    from core.auth import console_role_from_request
    from core.state import normalize_console_role

    role_param = (request.query_params.get("role") or "").strip()
    if role_param:
        return normalize_console_role(role_param)
    return console_role_from_request(request, default=default)
from core.phone_norm import norm_phone_str
from core.greeting_pcm import load_recorded_greeting_pcm
from core.storage import insert_manual_call, mark_manual_call_failed
from core.utils import _build_opening_line


def extract_role(request: Request) -> str:
    """Resolve the console role from query params, headers, or the default."""
    return str(
        (request.query_params.get("role") or "").strip()
        or (request.headers.get("X-User-Role") or "").strip()
        or (request.headers.get("X-Role") or "").strip()
        or "sales_1"
    )
from core.greeting_pcm import ensure_opening_pcm, ensure_name_verify_pcm_for_call
from services.call_recording import (
    fetch_vobiz_recording_if_missing,
    resolve_dashboard_recording_path,
)
from services.vobiz_bridge import make_vobiz_call

router = APIRouter(tags=["console"])


def _readable_transcript_lines(raw: str) -> tuple[str, list[str]]:
    """Return (joined readable text, list of lines) from JSONL or plain text."""
    lines_out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            role = obj.get("role") or obj.get("type", "")
            content = obj.get("content") or obj.get("text") or obj.get("message", "")
            if role in ("user", "assistant") and content:
                lines_out.append(f"{role.capitalize()}: {content.strip()}")
        except Exception:
            lines_out.append(line)
    if lines_out:
        return "\n".join(lines_out), lines_out
    return (raw or "").strip(), []


def _recommended_actions_from_analysis(analysis: dict) -> list[str]:
    bullets: list[str] = []
    disp = (analysis.get("disposition") or "").strip()
    if disp:
        bullets.append(f"Disposition: {disp}")
    ns = analysis.get("next_steps")
    if isinstance(ns, list):
        for x in ns:
            s = str(x).strip().lstrip("•-*").strip()
            if s:
                bullets.append(s)
        return bullets[:24]
    text = str(ns or "").strip()
    if text:
        parts = [p.strip().lstrip("•-*").strip() for p in re.split(r"[\n;]+", text)]
        parts = [p for p in parts if p]
        if parts:
            bullets.extend(parts)
        else:
            bullets.append(text)
    return bullets[:24]


def _recording_flags_for_row(row: dict) -> tuple[bool, bool]:
    """Return (recording_available, recording_pending) for list/summary rows."""
    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        return False, False
    if resolve_dashboard_recording_path(log_id):
        return True, False
    ended = bool((row.get("ended_at") or "").strip())
    status = str(row.get("status") or "").strip().lower()
    pending = ended or status in (
        "completed",
        "answered",
        "failed",
        "busy",
        "no_answer",
        "voicemail",
        "voice mail",
    )
    return False, pending


async def _recording_available_for_row(
    row: dict,
    *,
    try_fetch: bool = False,
) -> bool:
    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        return False
    if resolve_dashboard_recording_path(log_id):
        return True
    if not try_fetch:
        return False
    camp_id = str(row.get("camp_id") or "").strip()
    rec = await fetch_vobiz_recording_if_missing(log_id, camp_id=camp_id, initial_delay_sec=0.0)
    return bool(rec and rec.is_file())


def _manual_call_row_to_summary(row: dict) -> dict:
    recording_available, recording_pending = _recording_flags_for_row(row)
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "to_phone": row["to_phone"],
        "callee_name": row["callee_name"],
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "disposition": row.get("disposition") or "",
        "summary": (row.get("summary") or "")[:400],
        "recording_available": recording_available,
        "recording_pending": recording_pending,
        "recording_url": f"/api/manual/calls/{row['id']}/recording?role={row['role']}" if recording_available else "",
    }


async def _manual_call_detail_response(row: dict) -> dict:
    from core.worker import _read_transcript_jsonl

    role = row["role"]
    log_id = row.get("log_id") or ""
    raw = _read_transcript_jsonl(role, log_id) if log_id else ""
    readable, line_list = _readable_transcript_lines(raw)
    recording_available = await _recording_available_for_row(row, try_fetch=True)
    aj: dict = {}
    try:
        if (row.get("analysis_json") or "").strip():
            parsed = json.loads(row["analysis_json"])
            if isinstance(parsed, dict):
                aj = parsed
    except Exception:
        aj = {}
    # Prefer flattened columns when present
    if not aj.get("summary") and row.get("summary"):
        aj = {**aj, "summary": row.get("summary")}
    if not aj.get("disposition") and row.get("disposition"):
        aj = {**aj, "disposition": row.get("disposition")}
    if not aj.get("next_steps") and row.get("next_steps"):
        aj = {**aj, "next_steps": row.get("next_steps")}
    if "next_action" not in aj:
        aj = {**aj, "next_action": None}
    if not aj.get("emotion_label") and row.get("emotion_label"):
        aj = {
            **aj,
            "emotion_label": row.get("emotion_label"),
            "emotion_rationale": row.get("emotion_rationale"),
            "emotion_confidence": row.get("emotion_confidence"),
        }
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "to_phone": row["to_phone"],
        "callee_name": row["callee_name"],
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "log_id": log_id,
        "transcript_raw": raw,
        "transcript_readable": readable,
        "transcript_lines": line_list,
        "summary": aj.get("summary")
        or row.get("summary")
        or ((row.get("error") or "") if (row.get("status") or "") == "failed" else "")
        or "",
        "disposition": aj.get("disposition") or row.get("disposition") or "",
        "next_steps": aj.get("next_steps") or row.get("next_steps") or "",
        "emotion_label": aj.get("emotion_label") or row.get("emotion_label") or "",
        "emotion_rationale": aj.get("emotion_rationale") or row.get("emotion_rationale") or "",
        "emotion_confidence": aj.get("emotion_confidence", row.get("emotion_confidence")),
        "recommended_actions": _recommended_actions_from_analysis(aj),
        "rating": aj.get("rating"),
        "analysis": aj,
        "error": row.get("error") or "",
        "recording_available": recording_available,
        "recording_pending": (not recording_available) and _recording_flags_for_row(row)[1],
        "recording_url": f"/api/manual/calls/{row['id']}/recording?role={row['role']}" if recording_available else "",
    }


def _incoming_call_row_to_summary(row: dict) -> dict:
    recording_available, recording_pending = _recording_flags_for_row(row)
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "from_phone": row["from_phone"],
        "caller_name": row.get("caller_name") or "",
        "callee_name": row.get("caller_name") or "",
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "disposition": row.get("disposition") or "",
        "summary": (row.get("summary") or "")[:400],
        "recording_available": recording_available,
        "recording_pending": recording_pending,
        "recording_url": f"/api/incoming/calls/{row['id']}/recording?role={row['role']}" if recording_available else "",
    }


async def _incoming_call_detail_response(row: dict) -> dict:
    from core.worker import _read_transcript_jsonl

    role = row["role"]
    log_id = row.get("log_id") or ""
    raw = _read_transcript_jsonl(role, log_id) if log_id else ""
    readable, line_list = _readable_transcript_lines(raw)
    recording_available = await _recording_available_for_row(row, try_fetch=True)
    aj: dict = {}
    try:
        if (row.get("analysis_json") or "").strip():
            parsed = json.loads(row["analysis_json"])
            if isinstance(parsed, dict):
                aj = parsed
    except Exception:
        aj = {}
    if not aj.get("summary") and row.get("summary"):
        aj = {**aj, "summary": row.get("summary")}
    if not aj.get("disposition") and row.get("disposition"):
        aj = {**aj, "disposition": row.get("disposition")}
    if not aj.get("next_steps") and row.get("next_steps"):
        aj = {**aj, "next_steps": row.get("next_steps")}
    if "next_action" not in aj:
        aj = {**aj, "next_action": None}
    if not aj.get("emotion_label") and row.get("emotion_label"):
        aj = {
            **aj,
            "emotion_label": row.get("emotion_label"),
            "emotion_rationale": row.get("emotion_rationale"),
            "emotion_confidence": row.get("emotion_confidence"),
        }
    return {
        "id": row["id"],
        "role": row["role"],
        "camp_id": row["camp_id"],
        "from_phone": row["from_phone"],
        "caller_name": row["caller_name"],
        "callee_name": row.get("caller_name") or "",
        "lead_name": row.get("caller_name") or "",
        "status": row["status"],
        "started_at": row.get("started_at"),
        "ended_at": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "log_id": log_id,
        "transcript_raw": raw,
        "transcript_readable": readable,
        "transcript_lines": line_list,
        "summary": aj.get("summary")
        or row.get("summary")
        or ((row.get("error") or "") if (row.get("status") or "") == "failed" else "")
        or "",
        "disposition": aj.get("disposition") or row.get("disposition") or "",
        "next_steps": aj.get("next_steps") or row.get("next_steps") or "",
        "emotion_label": aj.get("emotion_label") or row.get("emotion_label") or "",
        "emotion_rationale": aj.get("emotion_rationale") or row.get("emotion_rationale") or "",
        "emotion_confidence": aj.get("emotion_confidence", row.get("emotion_confidence")),
        "recommended_actions": _recommended_actions_from_analysis(aj),
        "rating": aj.get("rating"),
        "analysis": aj,
        "error": row.get("error") or "",
        "recording_available": recording_available,
        "recording_pending": (not recording_available) and _recording_flags_for_row(row)[1],
        "recording_url": f"/api/incoming/calls/{row['id']}/recording?role={row['role']}" if recording_available else "",
    }


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@router.get("/api/tuning")
async def get_tuning(request: Request):
    role = _role_from_request(request)
    logger.info("get_tuning role={!r} qparams={}", role, dict(request.query_params))
    state = get_state(role)

    from core.opening_line import packaged_fallback_greeting
    from core.role_sandbox import coerce_role_prompt, coerce_role_rag, coerce_stored_greeting
    from prompts.role_prompts import get_role_prompt_text, get_role_rag_source_text

    file_prompt = get_role_prompt_text(role)
    file_rag = get_role_rag_source_text(role)
    prompt = coerce_role_prompt(role, state.get("prompt", ""), file_prompt)
    rag = coerce_role_rag(role, state.get("rag", ""), file_rag)
    gt = coerce_stored_greeting(role, state.get("greeting_text"))
    greeting = gt if gt else packaged_fallback_greeting(role)

    # Include P1-P9 phone numbers from state (fallback to server .env so the
    # UI always shows the numbers actually used by the call allocator)
    result = {
        "role": role,
        "prompt": prompt,
        "rag": rag,
        "greeting_text": greeting
    }
    from core.state import resolved_live_language, resolved_live_voice_profile

    _lang, _mirror = resolved_live_language(role)
    result["language"] = _lang
    result["multilingual_mirror"] = _mirror
    _voice, _voice_style = resolved_live_voice_profile(role)
    result["voice"] = _voice
    result["voice_style"] = _voice_style
    for i in range(1, 10):
        result[f"p{i}_number"] = state.get(f"p{i}_number", "") or getattr(settings, f"p{i}_number", "") or ""
    return result

class TuningUpdate(BaseModel):
    prompt: str = ""
    rag: str = ""
    greeting_text: str = ""
    p1_number: str = ""
    p2_number: str = ""
    p3_number: str = ""
    p4_number: str = ""
    p5_number: str = ""
    p6_number: str = ""
    p7_number: str = ""
    p8_number: str = ""
    p9_number: str = ""
    # Voice & language plug-and-play (mirrors Gemini Live languageCode + prompt)
    language: str = ""            # primary language code, e.g. "te-IN" (Telugu)
    multilingual_mirror: bool = True  # mirror the caller's language when different
    voice: str = ""
    voice_style: str = ""

@router.post("/api/tuning")
async def update_tuning(data: TuningUpdate, request: Request):
    role = _role_from_request(request)
    logger.info("update_tuning role={!r} qparams={}", role, dict(request.query_params))

    if role not in ("sales_1",):
        logger.warning("update_tuning received invalid role={!r}", role)
        raise HTTPException(400, f"Invalid role: {role}")

    from core.greeting_text_utils import coerce_stored_greeting
    from core.role_sandbox import validate_role_tuning

    # Only fields the client actually sent are touched — a partial save (e.g.
    # just language + mirror) must NEVER blank the prompt/RAG/greeting. The
    # frontend always sends the full form, but API probes and other tools may
    # send a subset; treating absent fields as "keep current" is the safe
    # production behavior.
    raw_body = request._body if hasattr(request, "_body") else b""
    import json as _json

    sent_keys: set[str] = set()
    try:
        sent_keys = set(_json.loads(raw_body or b"{}").keys()) if raw_body else set()
    except Exception:
        pass
    if not sent_keys:
        try:
            import inspect

            body_data = await request.body()
            sent_keys = set(_json.loads(body_data or b"{}").keys())
        except Exception:
            sent_keys = set()

    # Resolve current state for keep-current fallback of untouched fields
    from core.state import get_state

    _cur = get_state(role)

    def _field(field_name: str) -> str:
        if sent_keys and field_name in sent_keys:
            return str(getattr(data, field_name, "") or "")
        return str(_cur.get(field_name) or "")

    prompt_val = _field("prompt")
    rag_val = _field("rag")
    greeting_val = _field("greeting_text")

    tuning_err = validate_role_tuning(
        role,
        prompt=prompt_val,
        rag=rag_val,
        greeting=greeting_val,
    )
    if tuning_err:
        raise HTTPException(400, tuning_err)

    greeting_out = coerce_stored_greeting(role, greeting_val)
    # Collect P1-P9 phone numbers from request
    phone_nums = {}
    for i in range(1, 10):
        val = getattr(data, f"p{i}_number", "") or ""
        phone_nums[f"p{i}_number"] = val.strip()
    save_role_state(role, prompt=prompt_val, rag=rag_val, greeting_text=greeting_out, **phone_nums)

    # Persist voice/language plug-and-play inside vobiz_config (JSON column —
    # no schema migration needed). The live Gemini session resolves the
    # language from role_state first, falling back to GEMINI_LIVE_LANGUAGE.
    try:
        _st = get_state(role)
        vc = dict(_st.get("vobiz") or {})
        if sent_keys and "language" in sent_keys:
            vc["language"] = (data.language or "").strip()
        if sent_keys and "multilingual_mirror" in sent_keys:
            vc["multilingual_mirror"] = bool(data.multilingual_mirror)
        if sent_keys and "voice" in sent_keys:
            from core.state import _GEMINI_LIVE_VOICES

            requested_voice = (data.voice or "").strip()
            if requested_voice not in _GEMINI_LIVE_VOICES:
                raise HTTPException(400, "Unsupported Gemini Live voice")
            vc["voice"] = requested_voice
        if sent_keys and "voice_style" in sent_keys:
            requested_style = (data.voice_style or "").strip()
            if len(requested_style) > 2000:
                raise HTTPException(400, "Voice style must be 2000 characters or fewer")
            vc["voice_style"] = requested_style
        save_role_state(role, vobiz_config=vc)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("voice/language config save failed (non-fatal): {}", exc)

    # Keep prompt + KB files in sync — build_role_system_prompt() prefers non-empty DB,
    # then falls back to these files when the DB field is empty.
    from prompts.role_prompts import set_role_prompt_text, set_role_rag_source_text

    set_role_prompt_text(role, prompt_val)
    set_role_rag_source_text(role, rag_val)

    # Live RAG reads kb_chunks.json — regenerate + drop the cached chunks so the
    # dashboard edit is picked up by the next call immediately.
    try:
        from services.chunk_rag import rebuild_role_kb_chunks

        n = rebuild_role_kb_chunks(role)
        logger.info("Rebuilt kb_chunks for role={} ({} chunks) from dashboard save", role, n)
    except Exception as exc:
        logger.warning("kb_chunks rebuild after tuning save failed: {}", exc)

    try:
        from core import kv_cache

        kv_cache.invalidate_role(role)
    except Exception as exc:
        logger.warning("kv_cache invalidate after tuning save failed: {}", exc)
    try:
        from core.dashboard_state import invalidate_role as _dash_invalidate_role

        _dash_invalidate_role(role)
    except Exception as exc:
        logger.warning("dashboard invalidate after tuning save failed: {}", exc)

    # ─── Auto-save prompt version on every publish ───
    try:
        from core.storage import save_prompt_version
        ver = await save_prompt_version(
            role=role,
            prompt=prompt_val,
            rag=rag_val,
            greeting_text=greeting_out or "",
            status="active",
        )
        logger.info("Prompt version v{} saved for role={}", ver, role)
    except Exception as exc:
        logger.warning("Prompt version save failed (non-fatal): {}", exc)

    return {"status": "ok", "saved_role": role}


# ─── Prompt Versioning API ───

@router.get("/api/tuning/versions")
async def get_prompt_versions_api(request: Request):
    """Get prompt version history for the current role."""
    role = _role_from_request(request)
    try:
        from core.storage import get_prompt_versions, get_active_prompt_version
        versions = await get_prompt_versions(role)
        active = await get_active_prompt_version(role)
        return {
            "versions": versions,
            "active_version_id": active.get("id") if active else None,
            "active_version_number": active.get("version_number") if active else None,
        }
    except Exception as e:
        logger.error("Failed to get prompt versions for role={}: {}", role, e)
        raise HTTPException(500, "Failed to get prompt versions")


@router.post("/api/tuning/versions/{version_id}/restore")
async def restore_prompt_version_api(version_id: int, request: Request):
    """Restore a previous prompt version for the current role."""
    role = _role_from_request(request)
    try:
        from core.storage import restore_prompt_version
        result = await restore_prompt_version(role, version_id)
        if not result:
            raise HTTPException(404, f"Version {version_id} not found for role {role}")
        logger.info("Prompt version v{} restored for role={}", result.get("version_number"), role)
        return {"status": "restored", "version": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to restore prompt version {}: {}", version_id, e)
        raise HTTPException(500, "Failed to restore prompt version")


# ─── Gemini 2.5 Flash Transcription ───

class TranscriptionRequest(BaseModel):
    log_id: str
    role: str = "sales_1"
    audio_path: str = ""

@router.post("/api/transcribe")
async def transcribe_audio(data: TranscriptionRequest, request: Request):
    """Transcribe a call recording using Gemini 2.5 Flash."""
    role = _role_from_request(request) or data.role
    try:
        result = await run_gemini_transcription(
            log_id=data.log_id,
            role=role,
            audio_path=data.audio_path,
        )
        return {"status": "ok", "transcript": result}
    except Exception as e:
        logger.error("Transcription failed for log_id={}: {}", data.log_id, e)
        raise HTTPException(500, f"Transcription failed: {e}")


# ─── Gemini 2.5 Flash Transcription Module ───

async def run_gemini_transcription(
    log_id: str, role: str = "sales_1", audio_path: str = "",
) -> dict:
    """
    Transcribe a call recording using Gemini 2.5 Flash.
    Reads audio from call_recordings directory, sends to Gemini 2.5 Flash,
    and returns structured transcript with speaker labels.
    """
    import base64
    import json as _json
    from pathlib import Path as _Path

    # Resolve audio path
    if not audio_path:
        from services.vobiz_bridge.paths import backend_dir
        rec_dir = _Path(backend_dir) / "data" / "call_recordings"
        candidates = list(rec_dir.glob(f"*{log_id}*"))
        if not candidates:
            raise FileNotFoundError(f"No recording found for log_id={log_id}")
        audio_path = str(candidates[0])

    audio_file = _Path(audio_path)
    if not audio_file.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Read audio
    with open(audio_file, "rb") as f:
        audio_bytes = f.read()

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    mime_type = "audio/wav" if audio_file.suffix.lower() == ".wav" else "audio/mpeg"

    # Load transcription prompt from DB
    from prompts.role_prompts import extract_agent_name, get_role_prompt_text
    agent_name = extract_agent_name(role) or "Vernika"
    trans_prompt = get_role_prompt_text(f"{role}_transcription")
    if not trans_prompt:
        trans_prompt = (
            "You are processing a recorded Indian real-estate sales telephone call.\n\n"
            "Generate a highly accurate verbatim transcript.\n"
            f"Label the sales agent speaker as {agent_name} (never AGENT) and the other party as user (never CUSTOMER).\n"
            "Preserve the language actually spoken — Telugu, English, Tenglish, Hindi, Hinglish.\n"
            "Do NOT automatically translate the conversation.\n"
            "If a section is unintelligible, mark [unclear].\n"
            "Do not turn the transcript into a summary. Do not correct the user's meaning.\n"
            "Preserve numbers, names, prices, dates, and appointment times carefully.\n"
            "Output a chronological transcript with speaker attribution."
        )

    # Build Gemini transcription request (configured model, header auth)
    api_key = getattr(settings, "gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    from core.gemini_auth import gemini_auth_headers, gemini_generate_content_url

    model = (settings.gemini_transcription_model or "gemini-3.1-flash-lite").strip()
    url = gemini_generate_content_url(model)

    payload = {
        "system_instruction": {"parts": [{"text": trans_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "Transcribe this recorded sales call audio with speaker labels (AGENT/CUSTOMER). Output a chronological transcript."},
                    {"inlineData": {"mimeType": mime_type, "data": audio_b64}},
                ],
            }
        ],
    }

    headers = {"Content-Type": "application/json", **gemini_auth_headers(api_key)}

    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"{model} returned {resp.status_code}: {resp.text[:500]}")

    data_json = resp.json()
    transcript_text = (
        data_json.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )

    # Parse into structured utterances
    utterances = _parse_transcript_utterances(transcript_text)

    result = {
        "log_id": log_id,
        "transcript": transcript_text,
        "utterances": utterances,
        "model": model,
        "audio_path": audio_path,
    }

    # Save transcript to conversation log
    try:
        from services.conversation_log import append_artifact
        await append_artifact(log_id, "transcript", _json.dumps(result, indent=2))
    except Exception:
        pass

    return result


def _parse_transcript_utterances(text: str, role: str = "sales_1") -> list[dict]:
    """Parse transcript text into structured utterances with speaker labels.

    Accepts AGENT/CUSTOMER (legacy) plus agent-name/user labels and maps them
    to the canonical {agent_name, user} speaker set the dashboard renders.
    """
    import re

    from prompts.role_prompts import extract_agent_name

    agent_name = extract_agent_name(role) or "Vernika"
    utterances = []
    lines = (text or "").strip().split("\n")
    label_re = re.compile(r"^(.*?)[:\s-]+(.+)$")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = label_re.match(line)
        if m:
            raw = m.group(1).strip().lower()
            body = m.group(2).strip()
            if raw in ("agent", "assistant", "sales", "vernika", "siri", "ai") or agent_name.lower() in raw:
                speaker = "agent"
            elif raw in ("customer", "caller", "client", "user", "lead"):
                speaker = "user"
            else:
                speaker = "unknown"
            utterances.append({"speaker": speaker, "text": body})
        else:
            utterances.append({"speaker": "unknown", "text": line})
    return utterances


import os
class GreetingTextBody(BaseModel):
    greeting_text: str = ""


@router.get("/api/greeting/status")
async def greeting_status(request: Request):
    """Return the pre-recorded greeting audio status (ready, duration, source)."""
    role = _role_from_request(request)
    from core.greeting_pcm import greeting_pcm_paths, load_recorded_greeting_pcm
    from core.state import resolved_greeting_text

    greet = resolved_greeting_text(role)
    rec = load_recorded_greeting_pcm(role, greeting_text=greet)
    if rec:
        pcm, sr = rec
        pcm_path, meta_path = greeting_pcm_paths(role)
        meta: dict = {}
        try:
            import json as _json
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {
            "ready": True,
            "duration_sec": round(len(pcm) / 2 / max(1, sr), 2),
            "bytes": len(pcm),
            "source": meta.get("source") or "recorded",
            "voice": meta.get("voice") or "",
            "model": meta.get("model") or "",
        }
    return {"ready": False, "duration_sec": 0, "bytes": 0, "source": "", "voice": ""}


@router.post("/api/tuning/record-greeting")
async def record_greeting_tts(data: GreetingTextBody, request: Request):
    """Capture PCM for greeting_{role}.pcm using Gemini Live (native call voice)."""
    role = _role_from_request(request)
    from core.greeting_text_utils import intro_only_greeting

    text = intro_only_greeting((data.greeting_text or "").strip())
    if not text:
        raise HTTPException(400, "greeting_text is required")

    from core.greeting_pcm import _generate_and_cache_greeting, greeting_pcm_paths
    from core.state import resolved_live_voice_profile

    live_voice, _ = resolved_live_voice_profile(role)

    try:
        result = await _generate_and_cache_greeting(
            role,
            text,
            live_voice,
        )
    except Exception as exc:
        logger.exception("record-greeting failed")
        raise HTTPException(503, f"Greeting generation failed: {exc}") from exc

    if not result:
        raise HTTPException(503, "Greeting generation failed")

    pcm, sr = result
    out_path, _ = greeting_pcm_paths(role)

    return {
        "status": "ok",
        "path": str(out_path),
        "bytes": len(pcm),
        "sample_rate": sr,
        "engine": "live",
    }


@router.post("/api/tuning/capture-greeting-live")
async def capture_greeting_live(data: GreetingTextBody, request: Request):
    """Capture opening audio from Gemini Live (native voice) and save greeting_{role}.pcm.

    Returns WAV for immediate playback; PCM on disk is what calls use before Live connects.
    Query ``variant=inbound`` saves ``greeting_{role}_inbound.pcm`` (inbound DID legs).
    """
    role = _role_from_request(request)
    variant = (request.query_params.get("variant") or "").strip().lower()
    from core.greeting_text_utils import intro_only_greeting

    text = intro_only_greeting((data.greeting_text or "").strip())
    if not text:
        raise HTTPException(400, "greeting_text is required")

    from services.live_greeting_capture import capture_live_greeting_pcm, save_greeting_pcm_file

    logger.info(
        "capture-greeting-live: role={} variant={} text_len={}",
        role,
        variant or "(default)",
        len(text),
    )

    try:
        pcm, sr = await capture_live_greeting_pcm(role, text)
        if variant:
            out_path = save_greeting_pcm_file(
                role, pcm, sr, variant=variant, greeting_text=text
            )
        else:
            from core.greeting_pcm import _write_greeting_cache_files, greeting_pcm_paths
            from core.state import resolved_live_voice_profile

            live_voice, _ = resolved_live_voice_profile(role)
            _write_greeting_cache_files(
                role, text, pcm, sr, source="gemini_live_capture", voice=live_voice
            )
            out_path, _ = greeting_pcm_paths(role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("capture-greeting-live failed role={}: {}", role, exc)
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("capture-greeting-live failed role={}", role)
        raise HTTPException(503, f"Live capture failed: {exc}") from exc

    wav = _pcm_s16le_to_wav(pcm, sr)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Sample-Rate": str(sr),
            "X-Role": role,
            "X-Greeting-Bytes": str(len(pcm)),
            "X-Greeting-Path": str(out_path),
            "X-Greeting-Source": "gemini_live",
        },
    )


@router.post("/api/tuning/upload-doc")
async def upload_doc(request: Request, file: UploadFile = File(...)):
    role = _role_from_request(request)
    # extract text
    content = await file.read()
    filename = file.filename.lower()
    text = ""
    try:
        if filename.endswith(".txt"):
            text = content.decode("utf-8", errors="replace")
        elif filename.endswith(".pdf"):
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(content))
            for page in reader.pages:
                text += page.extract_text() + "\n"
        elif filename.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(content))
            for para in doc.paragraphs:
                text += para.text + "\n"
        else:
            raise HTTPException(400, "Unsupported file type")
    except Exception as e:
        logger.error(f"Failed to extract document: {e}")
        raise HTTPException(500, f"Extraction failed: {e}")
        
    from prompts.role_prompts import get_role_rag_source_text, set_role_rag_source_text

    state = get_state(role)
    current_rag = get_role_rag_source_text(role) or state.get("rag", "")
    new_rag = current_rag + "\n\n" + text if current_rag else text
    save_role_state(role, rag=new_rag)
    set_role_rag_source_text(role, new_rag)

    try:
        from services.chunk_rag import rebuild_role_kb_chunks

        n = rebuild_role_kb_chunks(role)
        logger.info("Rebuilt kb_chunks for role={} ({} chunks) after doc upload", role, n)
    except Exception as exc:
        logger.warning("kb_chunks rebuild after doc upload failed: {}", exc)

    try:
        from core import kv_cache

        kv_cache.invalidate_role(role)
    except Exception as exc:
        logger.warning("kv_cache invalidate after doc upload failed: {}", exc)

    return {"status": "ok", "filename": file.filename, "extracted_length": len(text)}

class VobizAccount(BaseModel):
    """One Vobiz trunk: auth id + token (+ optional label)."""
    name: str = "Default"
    auth_id: str = ""
    auth_token: str = ""


class VobizUpdate(BaseModel):
    auth_id: str = ""
    auth_token: str = ""
    from_number: str = ""
    public_url: str = ""
    accounts: list[VobizAccount] = []
    phone_numbers: list[str] = []


@router.get("/api/settings/vobiz")
async def get_vobiz_config(request: Request):
    """Return the current Vobiz config for the role (auth accounts, webhook, numbers)."""
    role = _role_from_request(request)
    state = get_state(role)
    vobiz_config = state.get("vobiz", {}) if isinstance(state.get("vobiz"), dict) else {}

    # Single legacy account fields
    accounts = list(vobiz_config.get("accounts") or [])
    if not accounts:
        legacy = {
            "name": "Default",
            "auth_id": str(vobiz_config.get("auth_id") or ""),
            "auth_token": str(vobiz_config.get("auth_token") or ""),
        }
        if legacy["auth_id"] or legacy["auth_token"]:
            accounts.append(legacy)

    # Webhook = public answer URL for this deployment
    from core.vobiz_credentials import _normalize_vobiz_public_url
    webhook = _normalize_vobiz_public_url(
        str(vobiz_config.get("public_url") or ""),
        settings.vobiz_public_base_url,
        settings.server_url,
    )

    result = {
        "role": role,
        "accounts": accounts,
        "webhook_url": f"{webhook.rstrip('/')}/vobiz/answer" if webhook else "",
        "inbound_webhook_url": f"{webhook.rstrip('/')}/vobiz/incoming" if webhook else "",
        "public_url": webhook,
        "from_number": str(vobiz_config.get("from_number") or ""),
        "phone_numbers": [n for n in (vobiz_config.get("phone_numbers") or []) if n],
        "has_env_override": bool(
            (settings.vobiz_sales_1_auth_id or "").strip()
            and (settings.vobiz_sales_1_auth_token or "").strip()
        ),
    }
    for i in range(1, 10):
        result[f"p{i}_number"] = state.get(f"p{i}_number", "") or getattr(settings, f"p{i}_number", "") or ""
    return result


@router.post("/api/settings/vobiz")
async def update_vobiz(data: VobizUpdate, request: Request):
    role = _role_from_request(request)
    state = get_state(role)
    vobiz_config = state.get("vobiz", {}) if isinstance(state.get("vobiz"), dict) else {}

    if data.accounts:
        # Multi-account mode: store the whole list; first account is the active one.
        cleaned = [
            {
                "name": (a.name or "Default").strip() or "Default",
                "auth_id": (a.auth_id or "").strip(),
                "auth_token": (a.auth_token or "").strip(),
            }
            for a in data.accounts
        ]
        vobiz_config["accounts"] = cleaned
        active = cleaned[0]
        vobiz_config["auth_id"] = active["auth_id"]
        vobiz_config["auth_token"] = active["auth_token"]
    else:
        # Legacy single-account mode
        vobiz_config["auth_id"] = (data.auth_id or "").strip()
        vobiz_config["auth_token"] = (data.auth_token or "").strip()

    if data.from_number:
        vobiz_config["from_number"] = (data.from_number or "").strip()
    if data.public_url:
        vobiz_config["public_url"] = (data.public_url or "").strip()
    if data.phone_numbers is not None:
        # Extra dial-able numbers (beyond P1-P9): used by the allocator for
        # round-robin dialing via get_all_outbound_numbers().
        cleaned_numbers = [(n or "").strip() for n in data.phone_numbers]
        vobiz_config["phone_numbers"] = [n for n in cleaned_numbers if n]

    save_role_state(role, vobiz_config=vobiz_config)
    return {"status": "ok", "role": role, "accounts": vobiz_config.get("accounts", [])}

class ManualCallReq(BaseModel):
    to: str
    callee_name: str


async def _assert_public_vobiz_callback_ready(base_url: str) -> None:
    """Fail before dialing when Vobiz would hang up on an unreachable answer URL."""
    from urllib.parse import urlparse

    parsed = urlparse((base_url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(503, "Vobiz callback must be a reachable public HTTPS URL.")
    probe = f"{base_url.rstrip('/')}/vobiz/answer?camp_id=connectivity_probe&role=sales_1"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True) as client:
            response = await client.get(probe)
        body = response.text or ""
        if response.status_code >= 400 or "<Stream" not in body or "/ws/vobiz" not in body:
            raise RuntimeError(f"HTTP {response.status_code}: invalid Vobiz answer XML")
    except Exception as exc:
        raise HTTPException(
            503,
            "Vobiz answer webhook is offline. The call would disconnect immediately after pickup. "
            "Deploy the callback service on a reachable HTTPS/WSS host, then retry.",
        ) from exc

@router.post("/api/manual/call")
async def manual_call(
    data: ManualCallReq,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    import asyncio

    role = _role_from_request(request)
    to_raw = (data.to or "").strip()
    logger.info("Manual call API: role={} to={!r}", role, to_raw[:24])

    from core.worker import sweep_stale_call_capacity

    swept = await sweep_stale_call_capacity(max_age_sec=180.0)
    if swept:
        logger.info("Manual call: swept {} stale slot(s) before dial", swept)

    state = get_state(role)
    vobiz_config = state.get("vobiz", {})

    auth_id, auth_token, _default_from, v_base = resolve_vobiz_credentials(role, vobiz_config)

    from core.outbound_numbers import dialable_outbound_numbers, is_vobiz_from_line_blocked_error, mark_outbound_line_blocked
    from services.vobiz_bridge import VobizCallError

    from_candidates = dialable_outbound_numbers(role, vobiz_config)
    if not from_candidates:
        from_candidates = [_default_from] if _default_from else []
    if role in ("sales_1",) and from_candidates:
        from core.worker import get_next_phone_number

        preferred = get_next_phone_number(role, vobiz_config)
        if preferred in from_candidates:
            from_candidates = [preferred] + [n for n in from_candidates if n != preferred]

    if not auth_id or not auth_token:
        raise HTTPException(400, "Vobiz credentials not configured")
    if not from_candidates:
        raise HTTPException(400, "Outbound caller ID (from_number) is not configured for this role")
    if not v_base:
        raise HTTPException(400, "VOBIZ_PUBLIC_BASE_URL is not configured")
    await _assert_public_vobiz_callback_ready(v_base)

    to_norm = norm_phone_str((data.to or "").strip())
    if not to_norm:
        raise HTTPException(400, "Invalid phone number — enter 10 digits (after +91), or a full number starting with + (e.g. +971…).")

    camp_id = f"manual_{role}_{uuid.uuid4()}"
    manual_row: dict = {
        "_role": role,
        "_manual_leg": True,
        "phone": to_norm,
        "name": (data.callee_name or "").strip() or "Unknown",
    }
    from core.greeting_text_utils import coerce_stored_greeting

    gt = coerce_stored_greeting(role, (state.get("greeting_text") or "").strip())
    if gt:
        manual_row["greeting_text"] = gt

    manual_call_id = await insert_manual_call(
        role,
        camp_id,
        to_norm,
        (data.callee_name or "").strip() or "Unknown",
    )
    manual_row["_manual_call_id"] = manual_call_id
    manual_row["_lead_id"] = manual_call_id  # legacy key used by schedule_callback tool
    manual_row["_registered_at"] = time.time()
    manual_row["phone"] = to_norm
    _CAMPAIGN_DATA[camp_id] = manual_row

    opening_text = gt or _build_opening_line(
        {"name": manual_row["name"], "phone": to_norm},
        role,
    )

    async def _prime_manual_audio() -> None:
        """Non-blocking: greeting plays from disk at WS connect if ready in time."""
        try:
            pcm_ok = await asyncio.wait_for(
                ensure_opening_pcm(camp_id, role, opening_text),
                timeout=8.0,
            )
            if pcm_ok:
                pcm, sr = _CAMPAIGN_DATA[camp_id]["opening_pcm"]
                logger.info(
                    "Manual call background PCM ready ({} bytes @ {} Hz) camp_id={}",
                    len(pcm),
                    sr,
                    camp_id,
                )
            if settings.scripted_name_verify_pcm:
                await asyncio.wait_for(
                    ensure_name_verify_pcm_for_call(camp_id, role),
                    timeout=8.0,
                )
        except asyncio.TimeoutError:
            logger.warning("Manual call: background PCM prep timed out camp_id={}", camp_id)
        except Exception as exc:
            logger.warning("Manual call: background PCM prep failed camp_id={}: {}", camp_id, exc)

    asyncio.create_task(_prime_manual_audio())

    from core.state import phone_is_busy, acquire_phone_slot, release_phone_slot, acquire_vobiz_call_slot, release_vobiz_call_slot
    from core.worker import _GLOBAL_CALL_SEMAPHORE, release_manual_call_resources, _manual_call_slot_watchdog

    global_slot_acquired = False
    slot_acquired = False
    phone_slot_acquired = False
    from_number = ""
    last_dial_err: Exception | None = None

    try:
        try:
            await asyncio.wait_for(_GLOBAL_CALL_SEMAPHORE.acquire(), timeout=8.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                503,
                "All call lines are busy (max 2 concurrent). Wait for active calls to finish, then retry.",
            )
        global_slot_acquired = True
        manual_row["_global_sem_acquired"] = True

        from core.camp_session import prepare_outbound_call_session

        for from_number in from_candidates:
            if phone_is_busy(from_number):
                logger.warning("Manual call: phone line {} busy — trying next outbound line", from_number)
                continue
            if phone_slot_acquired:
                release_phone_slot(from_number)
                phone_slot_acquired = False
            if slot_acquired:
                release_vobiz_call_slot(role)
                slot_acquired = False

            acquire_phone_slot(from_number)
            phone_slot_acquired = True
            acquire_vobiz_call_slot(role)
            slot_acquired = True
            manual_row["_outbound_phone"] = from_number

            auth_tail = auth_id[-6:] if auth_id else ""
            logger.info(
                "Manual Vobiz dial context: role={} auth_id_tail={!r} from_number={!r} base_url={!r} camp_id={}",
                role,
                auth_tail,
                from_number.strip(),
                v_base or "",
                camp_id,
            )
            hangup_url = f"{v_base}/vobiz/hangup" if v_base else ""
            await prepare_outbound_call_session(camp_id, role, _CAMPAIGN_DATA[camp_id], v_base)
            logger.info("Manual call: placing Vobiz dial camp_id={} from={} to={}", camp_id, from_number, to_norm)
            try:
                await make_vobiz_call(
                    to=to_norm,
                    from_=from_number,
                    answer_url=f"{v_base}/vobiz/answer?camp_id={camp_id}&role={role}",
                    auth_id=auth_id,
                    auth_token=auth_token,
                    hangup_url=hangup_url,
                    record=True,
                )
                asyncio.create_task(_manual_call_slot_watchdog(camp_id, role))
                return {"status": "ok", "camp_id": camp_id, "manual_call_id": manual_call_id}
            except VobizCallError as ve:
                last_dial_err = ve
                if is_vobiz_from_line_blocked_error(ve):
                    mark_outbound_line_blocked(from_number)
                    logger.warning(
                        "Manual call: outbound line {} blocked by Vobiz — trying alternate line",
                        from_number,
                    )
                    release_phone_slot(from_number)
                    phone_slot_acquired = False
                    release_vobiz_call_slot(role)
                    slot_acquired = False
                    continue
                await mark_manual_call_failed(camp_id, ve.message)
                raise HTTPException(
                    502,
                    f"Vobiz refused the call ({ve.status}): {ve.message}",
                )

        if last_dial_err and isinstance(last_dial_err, VobizCallError):
            await mark_manual_call_failed(camp_id, last_dial_err.message)
            raise HTTPException(
                502,
                "All outbound phone lines are blocked or busy. Try again in a few minutes.",
            )
        raise HTTPException(
            503,
            "All outbound phone lines are busy. Wait for active calls to finish, then retry.",
        )
    except HTTPException:
        if global_slot_acquired:
            await release_manual_call_resources(camp_id, role)
        if phone_slot_acquired and from_number:
            release_phone_slot(from_number)
        if slot_acquired:
            release_vobiz_call_slot(role)
        _CAMPAIGN_DATA.pop(camp_id, None)
        raise
    except Exception as e:
        logger.exception("Manual call failed")
        if global_slot_acquired:
            await release_manual_call_resources(camp_id, role)
        if phone_slot_acquired and from_number:
            release_phone_slot(from_number)
        if slot_acquired:
            release_vobiz_call_slot(role)
        await mark_manual_call_failed(camp_id, str(e))
        _CAMPAIGN_DATA.pop(camp_id, None)
        raise HTTPException(500, str(e))


@router.get("/api/manual/calls/recent")
async def manual_calls_recent(
    request: Request,
    _user: dict = Depends(get_current_user),
    limit: int = 15,
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import list_recent_manual_calls

    rows = await list_recent_manual_calls(role, limit=max(1, min(int(limit), 50)))
    return {"items": [_manual_call_row_to_summary(r) for r in rows]}


@router.get("/api/manual/calls/{call_id}")
async def manual_call_detail(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_manual_call_by_id

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")
    return await _manual_call_detail_response(row)


@router.post("/api/manual/calls/{call_id}/reanalyze")
async def manual_call_reanalyze(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    """Re-run post-call Gemini/Gemma QA on the saved JSONL transcript and update SQLite."""
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_manual_call_by_id, update_manual_call_analysis_by_id
    from core.worker import _read_transcript_jsonl, _resolve_call_transcript, _transcript_indicates_voicemail, _voicemail_analysis_dict
    from services.call_analyzer import analyze_call_transcript

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")

    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(400, "Call has no log_id transcript yet")

    transcript, _tx_source = await _resolve_call_transcript(role, log_id)

    if not (transcript or "").strip():
        raise HTTPException(400, "No transcript and no transcribable recording for this call")

    if _transcript_indicates_voicemail(transcript):
        analysis = _voicemail_analysis_dict(for_manual=True)
    else:
        from config import settings
        from services.callback_time import annotate_analysis_callback_epoch
        from services.transcript_interest import apply_interest_disposition_override

        analysis = await analyze_call_transcript(transcript)
        annotate_analysis_callback_epoch(
            analysis,
            tz_name=settings.transcript_callback_tz,
            transcript_text=transcript,
        )
        analysis = apply_interest_disposition_override(analysis, transcript)
    if not await update_manual_call_analysis_by_id(call_id, analysis):
        raise HTTPException(500, "Could not persist analysis")

    try:
        from core.storage import find_lead_by_phone
        from services.whatsapp_outcome import send_outcome_whatsapp_if_eligible

        _manual_status = ""
        if analysis.get("site_visit_agreed"):
            _manual_status = "site_visit"
        elif analysis.get("callback_reminder_epoch") or analysis.get("requested_callback_datetime_iso"):
            _manual_status = "callback_scheduled"
        _linked_lead_id = None
        _phone = (row.get("to_phone") or "").strip()
        if _phone:
            _dl = await find_lead_by_phone(role, _phone)
            if _dl:
                _linked_lead_id = int(_dl["id"])
        await send_outcome_whatsapp_if_eligible(
            role=role,
            phone=_phone,
            lead_name=str(row.get("callee_name") or ""),
            disposition=str(analysis.get("disposition") or ""),
            status=_manual_status,
            analysis=analysis,
            lead_id=_linked_lead_id,
            camp_id=str(row.get("camp_id") or ""),
            force_resend=True,
        )
    except Exception as _wa_err:
        from loguru import logger
        logger.exception("Manual reanalyze WhatsApp send failed call_id={}: {}", call_id, _wa_err)

    refreshed = await get_manual_call_by_id(call_id)
    if not refreshed:
        raise HTTPException(500, "Row missing after update")
    return await _manual_call_detail_response(refreshed)


@router.get("/api/manual/calls/{call_id}/recording")
async def manual_call_recording_download(
    call_id: int,
    request: Request,
):
    """Mono mixed WAV (16 kHz) with streaming support. Bearer auth or ``?access_token=`` for <audio src>."""
    from loguru import logger
    from core.auth import _decode_jwt
    auth = (request.headers.get("Authorization") or "").strip()
    payload = None
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        logger.info(f"recording auth: Bearer header, payload={payload}")
    if not payload:
        for key in ("access_token", "token"):
            raw = (request.query_params.get(key) or "").strip()
            logger.info(f"recording auth: trying query key={key}, raw_len={len(raw)}")
            if raw:
                payload = _decode_jwt(raw)
                if payload:
                    logger.info(f"recording auth: query param {key} decoded, payload={payload}")
                    break
                else:
                    logger.warning(f"recording auth: query param {key} failed to decode")
    if not payload:
        logger.warning(f"recording auth: NO payload. auth_header={auth[:30]}, qparams={dict(request.query_params)}")
        raise HTTPException(401, "Not authenticated")

    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_manual_call_by_id

    row = await get_manual_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Manual call not found")
    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(404, "No session log for recording lookup")
    camp_id = str(row.get("camp_id") or "").strip()
    rec = resolve_dashboard_recording_path(log_id)
    if not rec or not rec.is_file():
        rec = await fetch_vobiz_recording_if_missing(
            log_id,
            camp_id=camp_id,
            initial_delay_sec=8.0,
        )
    if not rec or not rec.is_file():
        raise HTTPException(404, "Recording not found — Vobiz carrier recording may still be processing")
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return range_file_response(rec, request, media_type)


@router.get("/api/incoming/calls/recent")
async def incoming_calls_recent(
    request: Request,
    _user: dict = Depends(get_current_user),
    limit: int = 15,
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import list_recent_incoming_calls

    rows = await list_recent_incoming_calls(role, limit=max(1, min(int(limit), 5000)))
    return {"items": [_incoming_call_row_to_summary(r) for r in rows]}


@router.get("/api/incoming/calls/{call_id}")
async def incoming_call_detail(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_incoming_call_by_id

    row = await get_incoming_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Incoming call not found")
    return await _incoming_call_detail_response(row)


@router.post("/api/incoming/calls/{call_id}/reanalyze")
async def incoming_call_reanalyze(
    call_id: int,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_incoming_call_by_id, update_incoming_call_analysis_by_id
    from core.worker import _read_transcript_jsonl
    from services.call_analyzer import analyze_call_transcript

    row = await get_incoming_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Incoming call not found")

    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(400, "Call has no log_id transcript yet")

    transcript = ""
    try:
        from services.transcriber import transcribe_audio
        transcribed = await transcribe_audio(log_id, role)
        if transcribed:
            transcript = transcribed
            logger.info("Incoming reanalyze: audio transcription successful for call_id={}", call_id)
    except Exception as e:
        logger.warning("Incoming reanalyze: audio transcription failed for call_id={}: {}", call_id, e)

    if not (transcript or "").strip():
        transcript = _read_transcript_jsonl(role, log_id)
        if (transcript or "").strip():
            logger.info("Incoming reanalyze: falling back to JSONL transcript for call_id={}", call_id)

    if not (transcript or "").strip():
        raise HTTPException(400, "No transcript and no transcribable recording for this call")

    analysis = await analyze_call_transcript(transcript)
    if not await update_incoming_call_analysis_by_id(call_id, analysis):
        raise HTTPException(500, "Could not persist analysis")

    refreshed = await get_incoming_call_by_id(call_id)
    if not refreshed:
        raise HTTPException(500, "Row missing after update")
    return await _incoming_call_detail_response(refreshed)


@router.get("/api/incoming/calls/{call_id}/recording")
async def incoming_call_recording_download(
    call_id: int,
    request: Request,
):
    from loguru import logger
    from core.auth import _decode_jwt
    auth = (request.headers.get("Authorization") or "").strip()
    payload = None
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
    if not payload:
        for key in ("access_token", "token"):
            raw = (request.query_params.get(key) or "").strip()
            if raw:
                payload = _decode_jwt(raw)
                if payload:
                    break
    if not payload:
        raise HTTPException(401, "Not authenticated")

    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )
    from core.storage import get_incoming_call_by_id

    row = await get_incoming_call_by_id(call_id)
    if not row or row.get("role") != role:
        raise HTTPException(404, "Incoming call not found")
    log_id = (row.get("log_id") or "").strip()
    if not log_id:
        raise HTTPException(404, "No session log for recording lookup")
    camp_id = str(row.get("camp_id") or "").strip()
    rec = resolve_dashboard_recording_path(log_id)
    if not rec or not rec.is_file():
        rec = await fetch_vobiz_recording_if_missing(
            log_id,
            camp_id=camp_id,
            initial_delay_sec=8.0,
        )
    if not rec or not rec.is_file():
        raise HTTPException(404, "Recording not found — Vobiz carrier recording may still be processing")
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return range_file_response(rec, request, media_type)


class RescheduleCampaignReq(BaseModel):
    from_date: str
    to_date: str
    outcomes: list[str]
    target_datetime: str


@router.post("/api/campaign/reschedule")
async def reschedule_campaign_calls(
    data: RescheduleCampaignReq,
    request: Request,
    _user: dict = Depends(get_current_user),
):
    """Reschedule historical campaign leads to a future callback datetime."""
    from datetime import datetime
    from core.storage import reschedule_leads

    role = normalize_console_role(
        request.query_params.get("role") or request.headers.get("X-User-Role") or "sales_1"
    )

    # Parse target datetime (ISO 8601) to epoch seconds.
    target_dt_str = (data.target_datetime or "").strip()
    if not target_dt_str:
        raise HTTPException(400, "Target date/time is required")
    try:
        # Handle both "2026-06-13T18:00" and "2026-06-13T18:00:00" plus offsets.
        if target_dt_str.endswith("Z"):
            target_dt_str = target_dt_str[:-1] + "+00:00"
        target_dt = datetime.fromisoformat(target_dt_str)
        if target_dt.tzinfo is None:
            # Assume Asia/Kolkata when no timezone is provided (matches dashboard UI).
            from zoneinfo import ZoneInfo
            target_dt = target_dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        target_epoch = target_dt.timestamp()
    except Exception as exc:
        logger.warning("Invalid target_datetime {}: {}", data.target_datetime, exc)
        raise HTTPException(400, f"Invalid target date/time: {data.target_datetime}")

    if target_epoch <= time.time():
        raise HTTPException(400, "Target date/time must be in the future")

    valid_outcomes = {"failed_no_answer", "interested", "cut_in_middle", "not_interested"}
    outcomes = [o for o in data.outcomes if o in valid_outcomes]
    if not outcomes:
        raise HTTPException(400, "Select at least one outcome to reschedule")

    try:
        count = await reschedule_leads(
            role,
            data.from_date,
            data.to_date,
            outcomes,
            target_epoch,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Reschedule campaign failed")
        raise HTTPException(500, f"Reschedule failed: {exc}")

    return {"status": "ok", "rescheduled_count": count}


@router.get("/api/conversation-logs/{date}/{log_id}")
async def get_conversation_log(date: str, log_id: str):
    log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs", date, f"{log_id}.txt")
    if os.path.exists(log_path):
        return FileResponse(log_path)
    raise HTTPException(404, "Log not found")

@router.get("/api/recordings/{date}/{filename}")
async def get_recording(date: str, filename: str, request: Request):
    rec_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "logs", date, filename)
    if os.path.exists(rec_path):
        media_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"
        return range_file_response(Path(rec_path), request, media_type)
    raise HTTPException(404, "Recording not found")



# ── Virtual Meet API ───────────────────────────────────────────────


@router.get("/api/campaign/lead/{lead_id}/virtual-meet")
async def get_virtual_meet(lead_id: int, request: Request):
    """Return the latest virtual meet record for a lead."""
    from core.storage import get_virtual_meet_for_lead

    role = extract_role(request)
    vm = await get_virtual_meet_for_lead(lead_id)
    if vm and vm["role"] == role:
        return vm
    return {"id": None}


@router.post("/api/campaign/lead/{lead_id}/virtual-meet/reschedule")
async def reschedule_virtual_meet(lead_id: int, request: Request):
    """Reschedule a virtual meet for a lead."""
    from core.storage import (
        get_virtual_meet_for_lead,
        reschedule_virtual_meet as _reschedule_vm,
    )

    role = extract_role(request)
    body = await request.json()
    new_date = (body.get("meet_date") or "").strip()
    new_time = (body.get("meet_time") or "").strip()
    new_notes = (body.get("notes") or "").strip()
    if not new_date or not new_time:
        raise HTTPException(400, "meet_date and meet_time are required")

    vm = await get_virtual_meet_for_lead(lead_id)
    if not vm:
        # No existing meet — create a new one
        from core.storage import add_virtual_meet as _add_vm

        new_id = await _add_vm(lead_id, role, new_date, new_time, new_notes)
        return {"status": "ok", "id": new_id, "action": "created"}

    ok = await _reschedule_vm(vm["id"], new_date, new_time, new_notes)
    if not ok:
        raise HTTPException(500, "Failed to reschedule virtual meet")
    return {"status": "ok", "id": vm["id"], "action": "rescheduled"}


@router.post("/api/campaign/lead/{lead_id}/virtual-meet/cancel")
async def cancel_virtual_meet(lead_id: int, request: Request):
    """Cancel a virtual meet for a lead."""
    from core.storage import get_virtual_meet_for_lead, cancel_virtual_meet as _cancel_vm

    role = extract_role(request)
    vm = await get_virtual_meet_for_lead(lead_id)
    if not vm or vm["role"] != role:
        raise HTTPException(404, "No virtual meet found for this lead")
    ok = await _cancel_vm(vm["id"])
    if not ok:
        raise HTTPException(500, "Failed to cancel virtual meet")
    return {"status": "ok"}
