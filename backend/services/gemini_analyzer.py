"""Post-call transcript QA via Google AI Studio (Gemini API key)."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
from loguru import logger

from config import settings
from services.analysis_prompt import (
    build_analysis_prompt,
    empty_transcript_result,
    parse_json_from_text,
    result_from_json,
)


async def analyze_gemini(transcript_text: str, *, role: str = "") -> dict:
    if not transcript_text.strip():
        return empty_transcript_result(
            summary="No transcript available",
            rationale="",
        )

    prompt = build_analysis_prompt(transcript_text, role=role)
    if not prompt:
        return empty_transcript_result(
            summary="Call ended early / No conversation",
            rationale="No conversational turns in transcript.",
        )

    from core.gemini_auth import gemini_auth_headers, gemini_generate_content_url, get_gemini_api_key

    key = get_gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY is not set")

    primary = (settings.gemini_call_analysis_model or "gemini-3-flash-preview").strip()
    fallback = (
        os.getenv("GEMINI_CALL_ANALYSIS_FALLBACK_MODEL") or "gemini-3.1-flash-lite"
    ).strip()
    models_to_try = [primary]
    if fallback and fallback != primary:
        models_to_try.append(fallback)

    last_error = None
    max_retries = 2
    for model_idx, model in enumerate(models_to_try):
        url = gemini_generate_content_url(model)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": float(settings.gemini_call_analysis_temperature),
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingBudget": int(settings.gemini_call_analysis_thinking_budget)},
            },
        }

        for attempt in range(1, max_retries + 1):
            t0 = time.time()
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                    r = await client.post(url, json=body, headers=gemini_auth_headers(key))

                if r.status_code == 503 or r.status_code == 429:
                    wait = 5 * attempt
                    logger.warning(
                        "Gemini analysis HTTP {} on attempt {}/{} model={} — retrying in {}s",
                        r.status_code,
                        attempt,
                        max_retries,
                        model,
                        wait,
                    )
                    last_error = f"Gemini analysis HTTP {r.status_code}"
                    if attempt < max_retries:
                        await asyncio.sleep(wait)
                        continue
                    break

                if r.status_code != 200:
                    last_error = f"Gemini analysis HTTP {r.status_code}: {r.text[:800]}"
                    logger.error("{}", last_error)
                    # Model unavailable (404) — try fallback model immediately.
                    if r.status_code == 404 and model_idx < len(models_to_try) - 1:
                        break
                    if attempt < max_retries:
                        await asyncio.sleep(3 * attempt)
                        continue
                    break

                data = r.json()
                cands = data.get("candidates") or []
                if not cands:
                    last_error = f"Gemini analysis: no candidates: {str(data)[:600]}"
                    logger.error("{}", last_error)
                    if attempt < max_retries:
                        await asyncio.sleep(3 * attempt)
                        continue
                    break

                parts = (cands[0].get("content") or {}).get("parts") or []
                raw = ""
                for part in parts:
                    raw += str(part.get("text") or "")
                raw = raw.strip()
                if not raw:
                    last_error = "Gemini analysis: empty text in response"
                    logger.error("{}", last_error)
                    if attempt < max_retries:
                        await asyncio.sleep(3 * attempt)
                        continue
                    break

                logger.info(
                    "Gemini call analysis done model={} in {:.1f}s ({} chars)",
                    model,
                    time.time() - t0,
                    len(raw),
                )

                parsed = parse_json_from_text(raw)
                if parsed:
                    _sv = parsed.get("site_visit_agreed")
                    _at = (parsed.get("next_action") or {}).get("action_type", "")
                    _disp = parsed.get("disposition", "")
                    logger.info(
                        "Gemini analysis result: disposition={} site_visit_agreed={} action_type={}",
                        _disp,
                        _sv,
                        _at,
                    )
                    return result_from_json(parsed)

                logger.warning("Gemini analysis JSON parse failed (len={}). raw={}", len(raw), raw[:800])
                last_error = "Gemini returned unparseable JSON"
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
                    continue
                break

            except httpx.TimeoutException:
                logger.warning(
                    "Gemini analysis timed out on attempt {}/{} model={}",
                    attempt,
                    max_retries,
                    model,
                )
                last_error = "Gemini analysis timed out"
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
                    continue
                break
            except Exception as e:
                logger.error(
                    "Gemini analysis unexpected error on attempt {}/{} model={}: {}",
                    attempt,
                    max_retries,
                    model,
                    e,
                )
                last_error = str(e)
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
                    continue
                break

        if model_idx < len(models_to_try) - 1:
            logger.warning("Gemini analysis switching fallback model {} → {}", model, models_to_try[model_idx + 1])

    raise RuntimeError(f"Gemini analysis failed after retries: {last_error}")
