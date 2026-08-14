from __future__ import annotations

import asyncio
import json
import os
import time
import base64
import wave
from pathlib import Path

import httpx
from loguru import logger

from config import settings

MIN_TRANSCRIBE_SEC = float(os.getenv("CALL_RECORDING_MIN_TRANSCRIBE_SEC", "5"))

def _recording_dirs() -> list[Path]:
    """Resolve all recording directories to search."""
    dirs = []
    primary = Path(settings.call_recording_dir)
    if not primary.is_absolute():
        primary = (Path(__file__).resolve().parent.parent / primary).resolve()
    else:
        primary = primary.resolve()
    if primary.is_dir():
        dirs.append(primary)
    legacy = Path(__file__).resolve().parent.parent / "data" / "recordings"
    if legacy.is_dir() and legacy.resolve() != primary.resolve():
        dirs.append(legacy)
    return dirs


def _find_file_recursive(base: Path, log_id: str) -> Path | None:
    """Search a recording base (and its date/lead subfolders) for a file whose
    name contains ``log_id`` and matches the _mixed/_full/vobiz patterns."""
    if not base.is_dir():
        return None
    for f in base.rglob("*"):
        if f.is_file() and log_id in f.name:
            low = f.name.lower()
            if "_mixed" in low or "_full" in low or "_vobiz" in low:
                return f
    return None


def _find_date_dir(log_id: str) -> Path | None:
    """Find the directory containing recordings for this log_id (may be a
    date folder or a per-lead subfolder nested inside it)."""
    for base in _recording_dirs():
        if not base.is_dir():
            continue
        # 1) Direct file under base (flat layout)
        for f in base.iterdir():
            if f.is_file() and log_id in f.name:
                return base
        # 2) Direct date folder containing the file
        for date_part in sorted(p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10):
            date_dir = base / date_part
            hit = _find_file_recursive(date_dir, log_id)
            if hit is not None:
                return hit.parent
        # 3) Any nested folder elsewhere
        hit = _find_file_recursive(base, log_id)
        if hit is not None:
            return hit.parent
    return None


def _resolve_transcribable(log_id: str, date_dir: Path) -> tuple[Path, str] | None:
    """Locate the transcription source file for this log_id inside ``date_dir``
    (which may contain per-lead subfolders). Prefers _mixed.wav, then _mixed.mp3,
    then _full.wav as a fallback so a recording is always transcribed."""
    candidates = [
        (date_dir / f"{log_id}_mixed.wav", "audio/wav"),
        (date_dir / f"{log_id}_mixed.mp3", "audio/mp3"),
        (date_dir / f"{log_id}_full.wav", "audio/wav"),
        (date_dir / f"{log_id}_full.mp3", "audio/mp3"),
        (date_dir / f"{log_id}_vobiz.mp3", "audio/mp3"),
        (date_dir / f"{log_id}_vobiz.wav", "audio/wav"),
    ]
    for cand, mime in candidates:
        if cand.is_file():
            return cand, mime
    if not date_dir.is_dir():
        return None
    # Nested lead folder
    for f in date_dir.rglob(f"*{log_id}*"):
        if not f.is_file():
            continue
        low = f.name.lower()
        if low.endswith((".wav", ".mp3", ".ogg")):
            mime = "audio/mp3" if low.endswith((".mp3", ".ogg")) else "audio/wav"
            return f, mime
    return None


async def transcribe_audio(log_id: str, role: str = "sales_1") -> str | None:
    date_dir = _find_date_dir(log_id)
    if not date_dir:
        logger.warning("No recording directory found for log_id={}", log_id)
        return None

    resolved = _resolve_transcribable(log_id, date_dir)
    if not resolved:
        logger.warning("No transcribable audio found for log_id={}", log_id)
        return None
    fp, mime_type = resolved

    sz = fp.stat().st_size
    is_wav = fp.name.endswith(".wav")
    is_mp3 = fp.name.endswith(".mp3")
    dur_sec: float | None = None
    if is_wav:
        try:
            with wave.open(str(fp), "rb") as w:
                dur_sec = w.getnframes() / float(w.getframerate())
        except (OSError, wave.Error):
            dur_sec = None
    if dur_sec is not None and dur_sec < MIN_TRANSCRIBE_SEC:
        logger.info(
            "Audio {} too short ({:.1f}s < {:.0f}s) — skip transcription to avoid hallucination.",
            fp.name,
            dur_sec,
            MIN_TRANSCRIBE_SEC,
        )
        return None
    if (is_wav and sz < 48000) or (is_mp3 and sz < 10000):
        logger.info("Audio file {} is too short ({:.1f} KB) to contain a conversation. Skipping transcription.", fp.name, sz / 1024)
        return None

    mb = sz / (1024 * 1024)
    logger.info("Transcribing {} ({:.1f} MB) with Gemini...", fp.name, mb)
    t0 = time.time()

    from core.gemini_auth import gemini_auth_headers, gemini_generate_content_url, get_gemini_api_key

    key = get_gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY is not set for transcription")

    from prompts.role_prompts import extract_agent_name
    agent_name = extract_agent_name(role) or "the sales agent from Technopolis Constructions"
    model = (settings.gemini_transcription_model or "gemini-3.1-flash-lite").strip()
    url = gemini_generate_content_url(model)

    with open(fp, "rb") as f:
        audio_data = f.read()

    b64_audio = base64.b64encode(audio_data).decode("utf-8")

    prompt = (
        "You are an expert phone-call transcriber for Indian outbound sales calls "
        "(Technopolis Constructions — Solitaire Unity premium apartments, Kondapur, Hyderabad).\n"
        "Transcribe the FULL audio chronologically. Every spoken turn must appear — "
        "do not summarize, skip, or merge turns.\n\n"
        "SPEAKERS:\n"
        f"- ASSISTANT: the AI agent ({agent_name} from Technopolis Constructions) — usually speaks FIRST.\n"
        "- USER: the customer/callee on the phone.\n\n"
        "RULES:\n"
        "1. Indian English, Hindi, Hinglish, Kannada, and code-switching are common — "
        "transcribe exactly what was spoken (native script or clear transliteration).\n"
        "2. Include short acknowledgements: hello, yes, haan, ji, okay, hmm, achha, etc.\n"
        "3. Include IVR/voicemail/screening text verbatim if present (helps downstream QA).\n"
        "4. If only ringtone, silence, or static with no speech → return [].\n"
        "5. NEVER invent prices. Authoritative Solitaire Unity pricing ONLY:\n"
        "   - 2 BHK apartment from approx ₹1.20 Crore | 2.5 BHK approx ₹1.52 Crore\n"
        "   - 3 BHK from approx ₹1.37 Crore\n"
        "   - If audio is unclear on a number, write [unclear] — do NOT guess ₹1.65 Cr or ₹1.6 Cr\n"
        "6. Output JSON array only. Each item: {\"role\": \"assistant\"|\"user\", \"content\": \"...\"}."
    )

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": b64_audio
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": float(settings.gemini_transcription_temperature),
            "maxOutputTokens": 32768,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "role": {
                            "type": "STRING",
                            "enum": ["assistant", "user"]
                        },
                        "content": {
                            "type": "STRING"
                        }
                    },
                    "required": ["role", "content"]
                }
            }
        }
    }

    raw_json = ""
    max_retries = 3
    # Retry wait times: 5s after attempt 1 (503), 10s after attempt 2
    retry_waits = [5.0, 10.0]
    timeouts = [120.0, 180.0, 240.0]

    for attempt in range(1, max_retries + 1):
        timeout_s = timeouts[attempt - 1]
        try:
            logger.info("Transcription attempt {}/{} model={} (timeout={:.0f}s)", attempt, max_retries, model, timeout_s)
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                r = await client.post(url, json=body, headers=gemini_auth_headers(key))

            if r.status_code in (503, 429):
                wait = retry_waits[attempt - 1] if attempt <= len(retry_waits) else 15.0
                logger.warning(
                    "Gemini transcription HTTP {} on attempt {}/{} — retrying in {:.0f}s: {}",
                    r.status_code, attempt, max_retries, wait, r.text[:300]
                )
                if attempt < max_retries:
                    await asyncio.sleep(wait)
                    continue
                return None

            if r.status_code != 200:
                logger.error("Gemini transcription HTTP {} on attempt {}: {}", r.status_code, attempt, r.text[:500])
                if attempt < max_retries:
                    await asyncio.sleep(retry_waits[0])
                    continue
                return None

            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                logger.error("Gemini transcription no candidates on attempt {}: {}", attempt, str(data)[:500])
                if attempt < max_retries:
                    await asyncio.sleep(retry_waits[0])
                    continue
                return None

            for part in (cands[0].get("content") or {}).get("parts") or []:
                raw_json += part.get("text", "")

            if not raw_json.strip():
                logger.error("Gemini transcription empty response on attempt {}", attempt)
                if attempt < max_retries:
                    await asyncio.sleep(retry_waits[0])
                    continue
                return None

            break

        except httpx.TimeoutException:
            logger.warning("Transcription attempt {}/{} timed out after {:.0f}s", attempt, max_retries, timeout_s)
            if attempt < max_retries:
                await asyncio.sleep(retry_waits[0])
                continue
            logger.error("All {} transcription attempts timed out for log_id={}", max_retries, log_id)
            return None
        except httpx.NetworkError as e:
            logger.warning("Transcription attempt {}/{} network error: {}", attempt, max_retries, e)
            if attempt < max_retries:
                await asyncio.sleep(retry_waits[0])
                continue
            return None
        except Exception as e:
            logger.error("Transcription attempt {}/{} unexpected error: {}", attempt, max_retries, e)
            return None

    # Try JSON first, then plain text "ROLE: text" format
    tagged = []
    try:
        s = raw_json.strip()
        if s.startswith("```"):
            if s.startswith("```json"):
                s = s[7:]
            else:
                s = s[3:]
            if s.endswith("```"):
                s = s[:-3]
            s = s.strip()
        parsed = json.loads(s)
        if isinstance(parsed, list):
            tagged = parsed
        elif isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, str) and v.strip().startswith("["):
                    try:
                        inner = json.loads(v)
                        if isinstance(inner, list):
                            tagged = inner
                            break
                    except Exception:
                        pass
            if not tagged:
                tagged = [{"role": "user", "content": json.dumps(parsed, ensure_ascii=False)[:2000]}]
    except Exception:
        pass

    if not tagged:
        # Parse plain text format: "ASSISTANT: text" or "USER: text"
        import re
        for line in raw_json.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(ASSISTANT|USER|assistant|user)\s*[:]\s*(.+)', line, re.IGNORECASE)
            if m:
                speaker = "assistant" if m.group(1).lower() == "assistant" else "user"
                tagged.append({"role": speaker, "content": m.group(2).strip()})

    if not tagged:
        tagged = [{"role": "user", "content": raw_json[:2000]}]

    # Check if standard roles are already correctly populated
    has_standard_roles = all(
        isinstance(t, dict) and str(t.get("role") or "").strip().lower() in ("assistant", "user")
        for t in tagged
    )

    normalized = []
    agent_keywords = [
        "vernika", "technopolis constructions",
        "solitaire unity", "solitaire unity", "calling from", "apartment", "apartment", "phase 3",
        "phase 1", "phase 2", "2.24 acres", "spacious layout", "kondapur",
    ]
    if has_standard_roles:
        user_agentish = 0
        asst_agentish = 0
        for turn in tagged:
            if not isinstance(turn, dict):
                continue
            turn_role = str(turn.get("role") or "").strip().lower()
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            content_lower = content.lower()
            hits = sum(1 for kw in agent_keywords if kw in content_lower)
            if turn_role == "user":
                user_agentish += hits
            elif turn_role == "assistant":
                asst_agentish += hits

        roles_likely_swapped = user_agentish >= 2 and user_agentish > asst_agentish
        if roles_likely_swapped:
            logger.info(
                "Transcription role swap detected (user_agentish={}, asst_agentish={}) — flipping roles",
                user_agentish,
                asst_agentish,
            )
            for turn in tagged:
                turn_role = str(turn.get("role") or "").strip().lower()
                content = str(turn.get("content") or "").strip()
                if content:
                    normalized.append({
                        "role": "assistant" if turn_role == "user" else "user",
                        "content": content,
                    })
        else:
            for turn in tagged:
                turn_role = str(turn.get("role") or "").strip().lower()
                content = str(turn.get("content") or "").strip()
                if content:
                    normalized.append({"role": turn_role, "content": content})
    else:
        # Fallback to heuristic mapping
        raw_turns = []
        for turn in tagged:
            if not isinstance(turn, dict):
                continue
            turn_role = str(turn.get("role") or "").strip()
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            # Detect hallucinated repetition (e.g. "no, no, no, no..." x500)
            words = content.split()
            if len(words) > 20:
                unique_prefix = words[:10]
                rest = words[10:]
                if rest and all(w == unique_prefix[-1] for w in rest[:50]):
                    content = " ".join(unique_prefix)
                    logger.warning("Truncated hallucinated repetition in transcription turn")
            raw_turns.append({"raw_role": turn_role, "content": content})

        unique_roles = list(dict.fromkeys([t["raw_role"] for t in raw_turns]))
        agent_keywords = ["vernika", "technopolis constructions", "solitaire unity", "solitaire unity", "calling from"]
        role_agent_scores = {r: 0 for r in unique_roles}
        for turn in raw_turns:
            role = turn["raw_role"]
            content = turn["content"].lower()
            if any(kw in content for kw in agent_keywords):
                role_agent_scores[role] += 1

        role_mappings = {}
        if len(unique_roles) == 2:
            r0, r1 = unique_roles[0], unique_roles[1]
            s0, s1 = role_agent_scores[r0], role_agent_scores[r1]
            if s0 > s1:
                role_mappings[r0] = "assistant"
                role_mappings[r1] = "user"
            elif s1 > s0:
                role_mappings[r1] = "assistant"
                role_mappings[r0] = "user"
            else:
                r0_l, r1_l = r0.lower(), r1.lower()
                if r0_l in ("assistant", "agent", "ai", "speaker 0", "speaker0") and r1_l not in ("assistant", "agent", "ai"):
                    role_mappings[r0] = "assistant"
                    role_mappings[r1] = "user"
                elif r1_l in ("assistant", "agent", "ai", "speaker 0", "speaker0") and r0_l not in ("assistant", "agent", "ai"):
                    role_mappings[r1] = "assistant"
                    role_mappings[r0] = "user"
                elif r0_l in ("user", "caller", "customer", "human", "speaker 1", "speaker1"):
                    role_mappings[r0] = "user"
                    role_mappings[r1] = "assistant"
                elif r1_l in ("user", "caller", "customer", "human", "speaker 1", "speaker1"):
                    role_mappings[r1] = "user"
                    role_mappings[r0] = "assistant"
                else:
                    role_mappings[r0] = "assistant"
                    role_mappings[r1] = "user"
        else:
            for r in unique_roles:
                r_l = r.lower()
                if r_l in ("assistant", "agent", "ai", "speaker 0", "speaker0"):
                    role_mappings[r] = "assistant"
                elif r_l in ("user", "caller", "customer", "human", "speaker 1", "speaker1"):
                    role_mappings[r] = "user"
                else:
                    role_mappings[r] = "assistant" if role_agent_scores[r] > 0 else "user"

        # 1. Apply raw mappings
        for turn in raw_turns:
            role = role_mappings.get(turn["raw_role"], "user")
            normalized.append({"role": role, "content": turn["content"]})

    # 2. Final safety swap check: if "user" turns contain agent-specific language, swap all.
    # Runs BEFORE post_process_attribution so segment corrections don't mask a global swap.
    swap_needed = False
    intro_keywords = [
        "this is vernika", "this is vernika from",
        "i am vernika", "i am vernika from",
        "my name is vernika",
        "technopolis constructions", "technopolis", "solitaire unity", "solitaire unity",
        "calling from technopolis", "from technopolis constructions", "from technopolis",
        "vernika from",
        "premium property", "premium properties", "premium rovilla", "premium rovillas", "premium apartment", "premium apartments"
    ]
    # Agent-only phrasing: a lead never refers to the developer's project as "our ...".
    # Catches diarization swaps where the agent pitches without a self-introduction.
    agent_content_markers = [
        "our luxury villa",
        "our premium apartments",
        "our premium apartments",
        "our apartment",
        "our project",
        "our 4 bhk",
        "our 5 bhk",
        "our 4bhk",
        "our 5bhk",
        "share some details about our",
        "share details about our",
        "let me share some details about our",
        "calling to share details about our",
        "i wanted to share some details about our",
    ]
    for turn in normalized:
        if turn["role"] == "user":
            content_lower = turn["content"].lower()
            if any(kw in content_lower for kw in intro_keywords):
                swap_needed = True
                break
            if any(kw in content_lower for kw in agent_content_markers):
                swap_needed = True
                break

    if swap_needed:
        logger.info("Roles swap detected in normalized transcript! Swapping roles back.")
        for turn in normalized:
            turn["role"] = "user" if turn["role"] == "assistant" else "assistant"

    # 3. Correct individual segment attributions using agent keywords
    normalized = post_process_attribution(normalized)

    from prompts.role_prompts import extract_agent_name
    from services.transcript_roles import fix_transcript_speaker_roles
    agent_nm = extract_agent_name(role) if role in ("sales_1",) else ""
    normalized = fix_transcript_speaker_roles(normalized, agent_name=agent_nm or "")

    from services.pricing_facts import sanitize_transcript_turns
    normalized = sanitize_transcript_turns(normalized)

    dt = time.time() - t0
    logger.info("Transcription completed in {:.1f}s ({} total segments)", dt, len(normalized))

    jsonl_lines = [json.dumps(t, ensure_ascii=False) for t in normalized]
    jsonl_text = "\n".join(jsonl_lines)

    from datetime import datetime, timezone
    project_root = Path(__file__).resolve().parent.parent.parent
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_dir = project_root / "data" / role / "logs" / date_str
    log_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = log_dir / f"{log_id}.jsonl"
    audio_path = log_dir / f"{log_id}_audio.jsonl"

    # Never overwrite live session JSONL (has session_id / type=turn markers).
    if jsonl_path.is_file() and _jsonl_is_live_session(jsonl_path):
        jsonl_path = audio_path
        logger.warning(
            "Live session JSONL exists — saving audio transcription to {} (not overwriting live log)",
            jsonl_path,
        )

    with open(str(jsonl_path), "w", encoding="utf-8") as f:
        f.write(jsonl_text)
        if jsonl_path == audio_path:
            f.write("\n")
            f.write(json.dumps({"source": "audio_transcription"}, ensure_ascii=False))

    logger.info("Transcript saved to {}", jsonl_path)
    return jsonl_text


def _jsonl_is_live_session(path: Path) -> bool:
    """True when JSONL was written by live append_turn (not audio transcription)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:40]:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") in ("turn", "session") or obj.get("session_id"):
                return True
    except Exception:
        pass
    return False


def post_process_attribution(turns: list[dict]) -> list[dict]:
    """Correct individual segment speaker diarization errors using agent-identifying keywords."""
    intro_keywords = [
        "this is vernika", "this is vernika from",
        "i am vernika", "i am vernika from",
        "my name is vernika",
        "technopolis constructions", "technopolis", "solitaire unity", "solitaire unity",
        "calling from technopolis", "from technopolis constructions", "from technopolis",
        "vernika from",
        "premium property", "premium properties", "premium rovilla", "premium rovillas", "premium apartment", "premium apartments"
    ]
    user_screening_phrases = [
        "person you are speaking with", "put your call on hold", "stay on the line",
        "ಮಾತನಾಡುತ್ತಿರುವ ವ್ಯಕ್ತಿಯು", "ಕರೆಯನ್ನು ಹೋಲ್ಡ್ ನಲ್ಲಿ", "ಲೈನ್ ನಲ್ಲಿಯೇ ಇರಿ",
        "बात कर रहे हैं", "कॉल को होल्ड पर", "होल्ड पर रखा",
        "leave your message", "after the tone", "not available to take",
        "record your name", "reason for calling", "see if this person is available",
        "would like to leave an additional message"
    ]
    for turn in turns:
        role = turn.get("role")
        if role == "user":
            content_lower = (turn.get("content") or "").lower()
            if any(kw in content_lower for kw in intro_keywords):
                turn["role"] = "assistant"
                logger.info(f"Diarization Correction: Changed turn from user to assistant: {turn['content']}")
        elif role == "assistant":
            content_lower = (turn.get("content") or "").lower()
            if any(p in content_lower for p in user_screening_phrases):
                turn["role"] = "user"
                logger.info(f"Diarization Correction: Changed turn from assistant to user (screening/hold detected): {turn['content']}")
    return turns
