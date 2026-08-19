"""HTTP client for Gemini greeting pre-cache — prefer Live capture so opener matches the call."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional, Tuple

from loguru import logger

from config import settings
from core.state import _CAMPAIGN_DATA


def _greetings_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "greetings"


# Live bridge assumes 16 kHz for ``greeting_{role}.pcm`` unless a sidecar meta gives ``sr``.
STORED_GREETING_DEFAULT_SR = 16000


def _text_hash(text: str) -> str:
    return hashlib.md5((text or "").strip().encode()).hexdigest()[:16]


def greeting_pcm_paths(role: str, variant: str = "") -> tuple[Path, Path]:
    """PCM + meta paths; ``variant='inbound'`` → ``greeting_{role}_inbound.pcm``."""
    r = (role or "sales_1").strip().lower()
    v = (variant or "").strip().lower()
    suffix = f"_{v}" if v else ""
    base = _greetings_base_dir()
    stem = f"greeting_{r}{suffix}"
    return base / f"{stem}.pcm", base / f"{stem}.pcm.meta"


def _greeting_profile_matches(meta: dict, role: str = "") -> bool:
    """Require cached audio to use the active voice and delivery profile."""
    if not role:
        return True
    from core.state import resolved_live_voice_profile

    expected_voice, style = resolved_live_voice_profile(role)
    want_style = _text_hash(style)
    stored_style = str(meta.get("style_hash") or "").strip()
    if want_style and stored_style != want_style:
        logger.info("Greeting style prompt changed for {}, will regenerate", role)
        return False
    stored_voice = str(meta.get("voice") or "").strip()
    if expected_voice and stored_voice != expected_voice:
        logger.info("Greeting voice changed for {}, will regenerate", role)
        return False
    return True


def _greeting_meta_matches(meta: dict, text: str, role: str = "") -> bool:
    """Require both current greeting text and current live voice profile."""
    if not _greeting_profile_matches(meta, role):
        return False
    want = _text_hash(text)
    if not want:
        return True
    stored = str(meta.get("text_hash") or "").strip()
    if not stored:
        return True
    return stored == want


def load_recorded_greeting_pcm(
    role: str,
    variant: str = "",
    *,
    greeting_text: str = "",
) -> Optional[Tuple[bytes, int]]:
    """Read ``greeting_{role}[_variant].pcm`` if present and (optionally) text still matches."""
    path, meta_path = greeting_pcm_paths(role, variant)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Invalid greeting meta {}: {}", meta_path, exc)
    matches = _greeting_meta_matches(meta, greeting_text, role=role)
    if not matches and role and _greeting_profile_matches(meta, role):
        try:
            from core.state import resolved_greeting_text
            template_text = resolved_greeting_text(role)
            if template_text:
                matches = _greeting_meta_matches(meta, template_text, role=role)
        except Exception as err:
            logger.warning("Failed to check template text hash in load_recorded_greeting_pcm: {}", err)
    if not matches and role:
        # Narrow tolerance: accept a fresh intro-only capture only when its
        # stored text is an intro-only variant of the SAME greeting (the
        # campaign-opening vs template-text mismatch that historically caused
        # a regeneration loop). If the user PASTED NEW greeting text, the hash
        # differs -> we must re-record (never play stale audio for new text).
        try:
            from core.greeting_text_utils import intro_only_greeting
            import json as _json

            stored_text = str(meta.get("text") or "")
            if stored_text:
                want_intro = intro_only_greeting((greeting_text or "").strip())
                if intro_only_greeting(stored_text) == want_intro:
                    logger.info(
                        "Greeting text hash differs but intro text matches for role={} — accepting",
                        role,
                    )
                    matches = True
        except Exception as _tol_err:
            logger.debug("Greeting tolerance check skipped: {}", _tol_err)
    if greeting_text.strip() and not matches:
        logger.warning(
            "Greeting text changed for role={} (stored source={}) — discarding stale cache.",
            role,
            meta.get("source"),
        )
        return None
    if meta.get("intro_only") is not True:
        logger.warning(
            "Greeting PCM for role={} missing intro_only flag — discarding stale cache (may contain name-verify).",
            role,
        )
        return None
    sr = int(meta.get("sr", STORED_GREETING_DEFAULT_SR))
    try:
        pcm = path.read_bytes()
        if not pcm:
            return None
        label = f"{role}" + (f" ({variant})" if variant else "")
        logger.info(
            "Loaded recorded greeting for role={} ({} bytes, sr={}, source={})",
            label,
            len(pcm),
            sr,
            meta.get("source", "unknown"),
        )
        return pcm, sr
    except Exception as exc:
        logger.warning("Failed to read greeting PCM for role={}: {}", role, exc)
        return None


def _get_greeting_cache_path(role: str) -> Path:
    base_dir = _greetings_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"greeting_{role}_latest.pcm"


def _get_greeting_cache_metadata_path(role: str) -> Path:
    return _greetings_base_dir() / f"greeting_{role}_latest.meta"


def _write_greeting_cache_files(
    role: str,
    text: str,
    pcm: bytes,
    sr: int,
    *,
    source: str,
    voice: str,
) -> None:
    """Persist to both ``greeting_{role}.pcm`` (calls use this) and ``_latest`` cache."""
    h = _text_hash(text)
    
    from core.state import resolved_live_voice_profile

    _, style = resolved_live_voice_profile(role)
        
    meta = {
        "text_hash": h,
        "text": text,  # the exact text this audio speaks (used by tolerance check)
        "style_hash": _text_hash(style),
        "voice": voice,
        "sr": int(sr),
        "source": source,
        "model": settings.gemini_live_model,
        "intro_only": True,
    }
    pcm_path, meta_path = greeting_pcm_paths(role)
    latest_pcm = _get_greeting_cache_path(role)
    latest_meta = _get_greeting_cache_metadata_path(role)
    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    pcm_path.write_bytes(pcm)
    meta_path.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    latest_pcm.write_bytes(pcm)
    latest_meta.write_text(json.dumps(meta, indent=0), encoding="utf-8")
    logger.info(
        "Wrote greeting cache for role={} source={} voice={} ({} bytes)",
        role,
        source,
        voice,
        len(pcm),
    )


async def _generate_and_cache_greeting(role: str, text: str, voice: str) -> Optional[Tuple[bytes, int]]:
    """Cache opening audio using Gemini Live capture (same voice as the call)."""
    text = (text or "").strip()
    if not text:
        return None

    from core.state import resolved_live_voice_profile

    configured_voice, _ = resolved_live_voice_profile(role)
    live_voice = (voice or configured_voice or "Aoede").strip()


    try:
        from services.live_greeting_capture import capture_live_greeting_pcm

        logger.info("Capturing greeting via Gemini Live for role={} (matches call voice)", role)
        pcm, sr = await capture_live_greeting_pcm(role, text)
        if sr != 16000:
            from services.vobiz_bridge.audio import pcm_resample
            logger.info("Resampling captured greeting from {} Hz to 16000 Hz", sr)
            pcm, _ = pcm_resample(pcm, sr, 16000)
            sr = 16000
        _write_greeting_cache_files(role, text, pcm, sr, source="gemini_live_capture", voice=live_voice)
        return pcm, sr
    except Exception as e:
        logger.error("Failed to generate/cache greeting for {}: {}", role, e)
        return None


def _load_cached_greeting(role: str, text: str) -> Optional[Tuple[bytes, int]]:
    """Prefer on-disk ``greeting_{role}.pcm``, then ``_latest`` cache."""
    on_disk = load_recorded_greeting_pcm(role, greeting_text=text)
    if on_disk:
        return on_disk

    try:
        cache_path = _get_greeting_cache_path(role)
        meta_path = _get_greeting_cache_metadata_path(role)
        if not cache_path.exists() or not meta_path.exists():
            return None
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if not _greeting_meta_matches(meta, text, role=role):
            logger.info("Greeting text changed for {}, will regenerate", role)
            return None
        with open(cache_path, "rb") as f:
            pcm = f.read()
        sr = int(meta.get("sr", 24000))
        return pcm, sr
    except Exception as e:
        logger.warning("Failed to load cached greeting for {}: {}", role, e)
        return None


def name_verify_pcm_path(role: str, first_name: str) -> Path:
    r = (role or "sales_1").strip().lower()
    key = _text_hash(f"{r}:{first_name.strip().lower()}")
    return _greetings_base_dir() / f"name_verify_{r}_{key}.pcm"


def load_name_verify_pcm(role: str, first_name: str, *, phrase: str = "") -> Optional[Tuple[bytes, int]]:
    path = name_verify_pcm_path(role, first_name)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    meta_path = path.with_suffix(path.suffix + ".meta")
    sr = STORED_GREETING_DEFAULT_SR
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            sr = int(meta.get("sr", sr))
            if phrase.strip() and meta.get("text_hash") and meta.get("text_hash") != _text_hash(phrase):
                return None
        except Exception:
            pass
    return path.read_bytes(), sr


async def ensure_name_verify_pcm_for_call(call_id: str, role: str) -> bool:
    """Pre-generate name-verify PCM from lead name in ``_CAMPAIGN_DATA`` before dial."""
    if call_id not in _CAMPAIGN_DATA:
        return False
    from core.campaign_payload import addressable_name
    from core.opening_line import looks_like_real_name

    raw = str(_CAMPAIGN_DATA[call_id].get("name") or "").strip()
    if not looks_like_real_name(raw):
        return False
    first = addressable_name(raw)
    if not first:
        return False
    return await ensure_name_verify_pcm(call_id, role, first)


async def ensure_name_verify_pcm(
    call_id: str,
    role: str,
    first_name: str,
) -> bool:
    """Pre-generate scripted 'Am I speaking with {name}?' audio before dial."""
    if settings.gemini_live_first_opening:
        return False
    first_name = (first_name or "").strip()
    if not first_name or call_id not in _CAMPAIGN_DATA:
        return False
    if _CAMPAIGN_DATA[call_id].get("name_verify_pcm"):
        return True

    phrase = f"Am I speaking with {first_name}?"
    cached = load_name_verify_pcm(role, first_name, phrase=phrase)
    if cached:
        _CAMPAIGN_DATA[call_id]["name_verify_pcm"] = cached
        logger.info(
            "Primed cached name-verify PCM for call_id={} ({} bytes)",
            call_id,
            len(cached[0]),
        )
        return True

    try:
        from services.live_greeting_capture import capture_phrase_pcm

        logger.info("Capturing name-verify PCM for call_id={} name={!r}", call_id, first_name)
        pcm, sr = await capture_phrase_pcm(role, phrase)
        if sr != 16000:
            from services.vobiz_bridge.audio import pcm_resample

            pcm, _ = pcm_resample(pcm, sr, 16000)
            sr = 16000
        out = name_verify_pcm_path(role, first_name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pcm)
        meta = {
            "sr": sr,
            "source": "phrase_capture",
            "text_hash": _text_hash(phrase),
            "first_name": first_name,
        }
        out.with_suffix(out.suffix + ".meta").write_text(json.dumps(meta, indent=0), encoding="utf-8")
        _CAMPAIGN_DATA[call_id]["name_verify_pcm"] = (pcm, sr)
        logger.info("Captured name-verify PCM for call_id={} ({} bytes @ {} Hz)", call_id, len(pcm), sr)
        return True
    except Exception as exc:
        logger.warning("Name-verify PCM capture failed for call_id={}: {}", call_id, exc)
        return False


async def ensure_opening_pcm(
    call_id: str,
    role: str,
    opening: str,
    *,
    voice: str = "",
) -> bool:
    """Load or generate ``greeting_{role}.pcm`` before dial so the call plays prerecorded audio first.

    Returns True when ``opening_pcm`` is ready in ``_CAMPAIGN_DATA[call_id]``.
    """
    if settings.gemini_live_first_opening:
        logger.debug(
            "Skip opening PCM ensure for call_id={} — Gemini Live speaks first",
            call_id,
        )
        return False
    if call_id not in _CAMPAIGN_DATA:
        return False

    from core.greeting_text_utils import intro_only_greeting

    # Canonical greeting for PRERECORDED audio: use the stable template text
    # (resolved_greeting_text) so the capture hash matches what the live WS
    # session loads at playback time. The campaign `opening` line (which may
    # include the project name) is used only as a fallback — mixing the two
    # caused a text-hash mismatch that rejected the freshly captured audio and
    # forced a regeneration loop (audio never played).
    try:
        from core.state import resolved_greeting_text as _rgt

        greet_text = intro_only_greeting(_rgt(role) or (opening or "").strip())
    except Exception:
        greet_text = intro_only_greeting((opening or "").strip())
    if not greet_text:
        try:
            from core.state import resolved_greeting_text

            greet_text = intro_only_greeting(resolved_greeting_text(role))
        except Exception:
            greet_text = ""
    if not greet_text:
        from core.opening_line import build_opening_line

        greet_text = intro_only_greeting(
            build_opening_line(_CAMPAIGN_DATA.get(call_id, {}), role) or ""
        )

    want_hash = _text_hash(greet_text)
    existing = _CAMPAIGN_DATA[call_id].get("opening_pcm")
    existing_hash = _CAMPAIGN_DATA[call_id].get("_opening_pcm_text_hash")
    if existing and existing_hash == want_hash:
        return True
    if existing and existing_hash != want_hash:
        logger.info(
            "ensure_opening_pcm: discarding stale opening_pcm for call_id={} (hash mismatch)",
            call_id,
        )
        _CAMPAIGN_DATA[call_id].pop("opening_pcm", None)

    recorded = load_recorded_greeting_pcm(role, greeting_text=greet_text)
    if recorded:
        _CAMPAIGN_DATA[call_id]["opening_pcm"] = recorded
        _CAMPAIGN_DATA[call_id]["_opening_pcm_text_hash"] = _text_hash(greet_text)
        logger.info(
            "Primed recorded greeting for call_id={} role={} ({} bytes @ {} Hz)",
            call_id,
            role,
            len(recorded[0]),
            recorded[1],
        )
        return True

    from core.state import resolved_live_voice_profile

    configured_voice, _ = resolved_live_voice_profile(role)
    live_voice = (voice or configured_voice or "Aoede").strip()

    if not greet_text:
        logger.warning("ensure_opening_pcm: no greeting text for call_id={} role={}", call_id, role)
        return False

    logger.info(
        "No greeting PCM on disk for role={} — capturing via Gemini Live before dial",
        role,
    )
    result = await _generate_and_cache_greeting(role, greet_text, live_voice)
    if result and call_id in _CAMPAIGN_DATA:
        _CAMPAIGN_DATA[call_id]["opening_pcm"] = result
        _CAMPAIGN_DATA[call_id]["_opening_pcm_text_hash"] = _text_hash(greet_text)
        logger.info(
            "Generated and primed greeting for call_id={} role={} ({} bytes)",
            call_id,
            role,
            len(result[0]),
        )
        return True
    return False


async def prewarm_opening(call_id: str, text: str, voice: str) -> None:
    try:
        role = "sales_1"
        if call_id in _CAMPAIGN_DATA:
            role = _CAMPAIGN_DATA[call_id].get("_role", "sales_1")

        cached = _load_cached_greeting(role, text)
        if cached:
            pcm, sr = cached
            if call_id in _CAMPAIGN_DATA:
                _CAMPAIGN_DATA[call_id]["opening_pcm"] = (pcm, sr)
                logger.info("Pre-warmed opening for {} from cache: {} bytes", call_id, len(pcm))
            return

        result = await _generate_and_cache_greeting(role, text, voice)
        if result and call_id in _CAMPAIGN_DATA:
            pcm, sr = result
            _CAMPAIGN_DATA[call_id]["opening_pcm"] = (pcm, sr)
            logger.info("Pre-warmed opening for {} (newly cached): {} bytes", call_id, len(pcm))
    except Exception as e:
        logger.warning("Pre-warm failed for {}: {}", call_id, e)
