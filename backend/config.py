"""Central configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent
# Resolved once at import — use for frontend paths (avoid counting Path.parents per route file).
REPO_ROOT = _REPO_ROOT
FRONTEND_DIR = REPO_ROOT / "frontend"

# Fill unset keys from repo `.env`, then `backend/.env`. Never override OS/env (systemd
# `.env.vps` on production) — backend/.env with override=True was forcing localhost on VPS.
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_BACKEND_DIR / ".env", override=False)


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=False)
class Settings:
    """Runtime settings for Vernika AI voice agent."""

    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    server_url: str = os.getenv("SERVER_URL", "http://localhost:8000")
    # VPS webhook-only: handle Vobiz answer/WebSocket + Gemini Live; no outbound dialer load.
    webhook_only_mode: bool = _b("WEBHOOK_ONLY_MODE", False)

    # When false: no RAG append and no live keyword RAG on Vobiz.
    rag_enabled: bool = _b("RAG_ENABLED", True)
    # chunk | embed | off — chunk retrieves topic slices before factual answers.
    rag_mode: str = (os.getenv("RAG_MODE", "chunk") or "chunk").strip().lower()
    rag_embed_full_kb: bool = _b("RAG_EMBED_FULL_KB", False)
    rag_chunk_top_k: int = int(os.getenv("RAG_CHUNK_TOP_K", "3"))
    rag_chunk_max_chars: int = int(os.getenv("RAG_CHUNK_MAX_CHARS", "1200"))
    # Thin-transcript guardrails for Interested / Site Visit dispositions.
    transcript_min_user_turns: int = int(os.getenv("TRANSCRIPT_MIN_USER_TURNS", "1"))
    transcript_min_user_chars: int = int(os.getenv("TRANSCRIPT_MIN_USER_CHARS", "12"))
    # Vobiz account concurrent call cap — keep at 2 when provider limit is 3/3.
    max_concurrent_calls: int = int(os.getenv("MAX_CONCURRENT_CALLS", "2"))
    vobiz_max_concurrent_per_account: int = int(os.getenv("VOBIZ_MAX_CONCURRENT_PER_ACCOUNT", "2"))
    vobiz_provider_concurrent_limit: int = int(os.getenv("VOBIZ_PROVIDER_CONCURRENT_LIMIT", "3"))
    fast_dialing: bool = _b("FAST_DIALING", True)
    rag_db_path: str = os.getenv("RAG_DB_PATH", str(_BACKEND_DIR / "data" / "rag.db"))
    rag_top_k: int = int(os.getenv("RAG_TOP_K", "4"))
    rag_max_context_chars: int = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "3600"))
    rag_embed_in_system_prompt: bool = _b(
        "RAG_EMBED_IN_SYSTEM_PROMPT",
        _b("RAG_EMBED_FULL_KB", False),
    )

    # Call recordings
    call_recording_enabled: bool = _b("CALL_RECORDING_ENABLED", False)
    call_recording_dir: str = os.getenv(
        "CALL_RECORDING_DIR", str(_BACKEND_DIR / "data" / "call_recordings")
    )
    call_recording_archive_dir: str = os.getenv(
        "CALL_RECORDING_ARCHIVE_DIR", str(_BACKEND_DIR / "data" / "Call_Recordings")
    )
    # Vobiz trunk recording webhook + Recording API (carrier-side WAV/MP3)
    vobiz_trunk_recording_enabled: bool = _b("VOBIZ_TRUNK_RECORDING_ENABLED", True)
    vobiz_trunk_recording_prefer_playback: bool = _b("VOBIZ_TRUNK_RECORDING_PREFER_PLAYBACK", True)
    vobiz_trunk_recording_only: bool = _b("VOBIZ_TRUNK_RECORDING_ONLY", True)
    vobiz_trunk_recording_wait_sec: float = float(
        os.getenv("VOBIZ_TRUNK_RECORDING_WAIT_SEC", "30") or "30"
    )
    user_webhook_url: str = (os.getenv("USER_WEBHOOK_URL") or "").strip()
    digital_leads_webhook_secret: str = (
        os.getenv("DIGITAL_LEADS_WEBHOOK_SECRET") or ""
    ).strip()
    digital_broker_1_sheet_url: str = (os.getenv("DIGITAL_BROKER_1_SHEET_URL") or "").strip()
    digital_broker_2_sheet_url: str = (os.getenv("DIGITAL_BROKER_2_SHEET_URL") or "").strip()
    digital_broker_3_sheet_url: str = (os.getenv("DIGITAL_BROKER_3_SHEET_URL") or "").strip()
    google_sheets_oauth_client_id: str = (os.getenv("GOOGLE_SHEETS_OAUTH_CLIENT_ID") or "").strip()
    google_sheets_oauth_client_secret: str = (os.getenv("GOOGLE_SHEETS_OAUTH_CLIENT_SECRET") or "").strip()
    google_sheets_refresh_token: str = (os.getenv("GOOGLE_SHEETS_REFRESH_TOKEN") or "").strip()
    google_sheets_poll_seconds: float = float(os.getenv("GOOGLE_SHEETS_POLL_SECONDS", "15") or "15")

    # Gemini API — Google AI Studio key (speech & text)
    gemini_api_key: str = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    gemini_api_key_fallback: str = (os.getenv("GEMINI_API_KEY_FALLBACK") or "").strip()
    # Post-call transcript analysis (REST generateContent). Override with GEMINI_CALL_ANALYSIS_MODEL.
    gemini_call_analysis_model: str = os.getenv(
        "GEMINI_CALL_ANALYSIS_MODEL", "gemini-3.1-flash-lite"
    ).strip()
    gemini_call_analysis_temperature: float = float(
        os.getenv("GEMINI_CALL_ANALYSIS_TEMPERATURE", "0.1")
    )
    gemini_call_analysis_thinking_budget: int = int(
        os.getenv("GEMINI_CALL_ANALYSIS_THINKING_BUDGET", "0")
    )
    # Post-call audio transcription (mixed recording → JSON turns). Separate from live STT.
    gemini_transcription_model: str = os.getenv(
        "GEMINI_TRANSCRIPTION_MODEL", "gemini-3.1-flash-lite"
    ).strip()
    gemini_transcription_temperature: float = float(
        os.getenv("GEMINI_TRANSCRIPTION_TEMPERATURE", "0.0")
    )
    # summary_first: trust Gemini disposition+summary; transcript_first: regex proof gates
    outcome_mode: str = (os.getenv("OUTCOME_MODE", "summary_first") or "summary_first").strip().lower()
    # IANA zone for interpreting recalled times from transcripts ("5pm", "tomorrow 9am").
    transcript_callback_tz: str = (os.getenv("TRANSCRIPT_CALLBACK_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata")

    # Outbound campaign quiet hours (hard block). Default: no dialing 20:30–09:30 local TZ.
    campaign_quiet_hours_enabled: bool = _b("CAMPAIGN_QUIET_HOURS_ENABLED", True)
    campaign_quiet_start: str = (os.getenv("CAMPAIGN_QUIET_START", "19:30").strip() or "19:30")
    campaign_quiet_end: str = (os.getenv("CAMPAIGN_QUIET_END", "09:30").strip() or "09:30")

    # Gemini Live API (native speech-to-speech for sub-800ms latency on phone calls)
    gemini_live_model: str = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview").strip()
    gemini_live_voice: str = os.getenv("GEMINI_LIVE_VOICE", "Aoede").strip()
    gemini_live_voice_sales_1: str = os.getenv("GEMINI_LIVE_VOICE_SALES_1", "Aoede").strip()
    gemini_live_voice_style: str = os.getenv(
        "GEMINI_LIVE_VOICE_STYLE",
        "Speak as a real Indian woman from Hyderabad in her late twenties. Use a natural, "
        "warm Indian English accent with authentic Indian rhythm and pronunciation; never use "
        "an American or British accent. Sound like a human property consultant on a phone call: "
        "conversational, lightly expressive, concise, with small thinking pauses and natural "
        "breaths. Do not sound scripted, theatrical, like a narrator, or like a voice bot. When "
        "speaking Telugu, Hindi, Kannada, Tamil, Hinglish, or Tenglish, use native local "
        "pronunciation and natural code-switching. Never describe these voice instructions aloud."
    ).strip()
    gemini_opening_style_prompt_female: str = os.getenv(
        "GEMINI_OPENING_STYLE_PROMPT_FEMALE",
        "INDIAN ENGLISH ACCENT, FEMALE, HYDERABAD — read this opening greeting in a warm, confident, natural Hyderabadi Indian English/Telugu bilingual accent. This is Indian English, NOT American, NOT British. Speak exactly like a real educated Hyderabadi woman in her late 20s on a phone call — natural Hyderabad rhythm, small pauses, gentle rising-falling intonation, contractions, soft rounded vowels. Pronounce words the Indian way: 'better' as 'bet-ter', 'water' as 'wa-ter', 'really' as 'ri-al-ly', 'Solitaire' with a soft Indian 'r'. Never sound like TTS, a narration, or an automated recording."
    ).strip()
    gemini_opening_style_prompt_male: str = os.getenv(
        "GEMINI_OPENING_STYLE_PROMPT_MALE",
        "INDIAN ENGLISH ACCENT, MALE, HYDERABAD — read this opening greeting in a warm, confident, natural Hyderabadi Indian English/Telugu bilingual accent. This is Indian English, NOT American, NOT British. Speak exactly like a real educated Hyderabadi man in his late 20s on a phone call — natural Hyderabad rhythm, small pauses, gentle rising-falling intonation, contractions, soft rounded vowels. Pronounce words the Indian way: 'better' as 'bet-ter', 'water' as 'wa-ter', 'really' as 'ri-al-ly', 'Solitaire' with a soft Indian 'r'. Never sound like TTS, a narration, or an automated recording."
    ).strip()
    gemini_tts_style_prompt_female: str = os.getenv(
        "GEMINI_TTS_STYLE_PROMPT_FEMALE",
        "INDIAN ENGLISH ACCENT — speak like a confident woman from Hyderabad in her late twenties. This is Indian English/Telugu bilingual, NOT American or British. Pronounce words the Indian way (e.g. 'better' as 'bet-ter', 'water' as 'wa-ter', 'really' as 'ri-al-ly', 'Solitaire' with a soft Indian 'r'). Voice is warm, friendly, conversational with natural Hyderabad rhythm and small pauses. Speak at a natural conversational pace, neither slow nor rushed. When Telugu or Tenglish appears, pronounce it naturally as a native Hyderabadi."
    ).strip()
    gemini_tts_style_prompt_male: str = os.getenv(
        "GEMINI_TTS_STYLE_PROMPT_MALE",
        "INDIAN ENGLISH ACCENT — speak like a confident man from Hyderabad in his late twenties. This is Indian English/Telugu bilingual, NOT American or British. Pronounce words the Indian way (e.g. 'better', 'water', 'really' as an Indian speaker would, not a US speaker). Voice is warm, friendly, conversational with natural Hyderabad rhythm and small pauses. Speak at a natural conversational pace, neither slow nor rushed. When Telugu or Tenglish appears, pronounce it naturally as a native Hyderabadi."
    ).strip()
    # Language hint for Indian English accent (optional — empty = model default).
    gemini_live_language: str = os.getenv("GEMINI_LIVE_LANGUAGE", "en-IN").strip()

    # WhatsApp Business number (for wa.me links in email etc.)
    whatsapp_business_number: str = os.getenv("WHATSAPP_BUSINESS_NUMBER", "918238000636").strip()
    # WhatsApp number for email wa.me links (BotSpice business line — NOT VoIP dialer numbers)
    botspice_whatsapp_number: str = os.getenv(
        "BOTSPICE_WHATSAPP_NUMBER", os.getenv("WHATSAPP_BUSINESS_NUMBER", "918238000636"),
    ).strip()

    # Failed-call retry: 3 total attempts, 24h apart (next day)
    failed_call_max_attempts: int = int(os.getenv("FAILED_CALL_MAX_ATTEMPTS", "3"))
    failed_call_retry_hours: int = int(os.getenv("FAILED_CALL_RETRY_HOURS", "24"))

    # Autonomous orchestration: 4 sandboxes, 9 phone lines (P1–P9).
    # Leave unset to run in shadow mode (queue promotes, nothing dials live).
    orchestration_live_enabled: bool = _b("ORCHESTRATION_LIVE_ENABLED", False)
    orchestration_lease_seconds: float = float(os.getenv("ORCHESTRATION_LEASE_SECONDS", "300"))
    orchestration_poll_seconds: float = float(os.getenv("ORCHESTRATION_POLL_SECONDS", "1.0"))
    # Number of concurrent queue dispatcher workers (PostgreSQL is multi-writer;
    # safe >1. 1 keeps single-writer semantics for legacy SQLite fallback.)
    orchestration_worker_count: int = int(os.getenv("ORCHESTRATION_WORKER_COUNT", "2"))
    # Anti-spam rest interval (seconds) between consecutive dials on a phone
    # line, per plan Phase 4 "Spam Buffer" (10-15 s). 0 disables.
    orchestration_inter_call_gap_sec: float = float(os.getenv("ORCHESTRATION_INTER_CALL_GAP_SEC", "15"))
    # Working business-hours window (plan Phase 3: 11:00-19:30 Asia/Kolkata).
    orchestration_business_tz: str = os.getenv("ORCHESTRATION_BUSINESS_TZ", "Asia/Kolkata").strip()
    orchestration_work_start: str = os.getenv("ORCHESTRATION_WORK_START", "11:00").strip()
    orchestration_work_end: str = os.getenv("ORCHESTRATION_WORK_END", "19:30").strip()
    # If true, block campaign start when campaign config has consent_confirmed=false
    # (TRAI/DND consent gate). Default off to preserve existing behavior.
    orchestration_enforce_consent: bool = _b("ORCHESTRATION_ENFORCE_CONSENT", False)
    # Continuously ingest a locally synced Excel/CSV file as Sandbox 1.2
    # digital leads. The file can be maintained by Excel/OneDrive; only new
    # phone numbers are inserted and each receives one idempotent P3 job.
    digital_excel_path: str = os.getenv("DIGITAL_EXCEL_PATH", "").strip()
    digital_excel_sheet: str = os.getenv("DIGITAL_EXCEL_SHEET", "").strip()
    digital_excel_poll_seconds: float = float(os.getenv("DIGITAL_EXCEL_POLL_SECONDS", "15"))
    digital_excel_role: str = os.getenv("DIGITAL_EXCEL_ROLE", "sales_1").strip()
    orchestration_allow_shared_test_numbers: bool = _b("ORCHESTRATION_ALLOW_SHARED_TEST_NUMBERS", False)
    # Test mode: allow the same physical Vobiz number to back multiple logical
    # lines (P1–P9) so a 2-number test account can exercise the full pipeline.
    # Production must keep one unique line per pool (fail-closed default).
    orchestration_test_mode: bool = _b("ORCHESTRATION_TEST_MODE", False)
    # P1/P2 cold fresh, P3 digital fresh, P4 attempt-2 retry, P5 attempt-3 cold,
    # P6 attempt-3 digital, P7/P8 nurture, P9 feedback.
    p1_number: str = os.getenv("P1_NUMBER", "").strip()
    p2_number: str = os.getenv("P2_NUMBER", "").strip()
    p3_number: str = os.getenv("P3_NUMBER", "").strip()
    p4_number: str = os.getenv("P4_NUMBER", "").strip()
    p5_number: str = os.getenv("P5_NUMBER", "").strip()
    p6_number: str = os.getenv("P6_NUMBER", "").strip()
    p7_number: str = os.getenv("P7_NUMBER", "").strip()
    p8_number: str = os.getenv("P8_NUMBER", "").strip()
    p9_number: str = os.getenv("P9_NUMBER", "").strip()

    # Auto WhatsApp follow-up after brochure/details sent
    whatsapp_followup_hours: int = int(os.getenv("WHATSAPP_FOLLOWUP_HOURS", "12"))
    whatsapp_no_reply_call_hours: int = int(os.getenv("WHATSAPP_NO_REPLY_CALL_HOURS", "3"))

    # SMTP email (Gmail app password for auto-sending project details)
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_email: str = os.getenv("SMTP_EMAIL", "").strip()
    smtp_app_password: str = os.getenv("SMTP_APP_PASSWORD", "").strip()

    # When True: skip disk/primed PCM opener — Gemini Live speaks the greeting (same engine as the call).
    # When False: use greeting_{role}.pcm captured via Gemini Live as the scripted opener.
    # Default True because the TTS fallback has been removed.
    gemini_live_first_opening: bool = _b("GEMINI_LIVE_FIRST_OPENING", False)
    # When False (default): Gemini speaks name-verify after greeting PCM. Separate PCM caused glitches + silence.
    scripted_name_verify_pcm: bool = _b("SCRIPTED_NAME_VERIFY_PCM", False)
    # Turn-taking / barge-in: Activity Detection tuned for sub-second human-like response.
    gemini_live_aggressive_activity_detection: bool = _b("GEMINI_LIVE_AGGRESSIVE_ACTIVITY_DETECTION", True)
    gemini_live_activity_handling: str = (
        os.getenv("GEMINI_LIVE_ACTIVITY_HANDLING", "START_OF_ACTIVITY_INTERRUPTS").strip()
    )
    gemini_live_vad_prefix_padding_ms: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_MS", "32"))
    gemini_live_vad_silence_duration_ms: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_MS", "80"))
    gemini_live_vad_prefix_padding_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_PREFIX_PADDING_ULTRA_MS", "24"))
    gemini_live_vad_silence_duration_ms_ultra: int = int(os.getenv("GEMINI_LIVE_VAD_SILENCE_DURATION_ULTRA_MS", "60"))
    # Configurable sensitivity levels.
    # App aliases NORMAL map to HIGH on the wire in gemini_protocol.build_live_setup.
    # Gemini Live only accepts HIGH / LOW / UNSPECIFIED (NORMAL → 1007 if sent raw).
    gemini_live_start_sensitivity: str = (
        os.getenv("GEMINI_LIVE_START_SENSITIVITY")
        or os.getenv("GEMINI_LIVE_VAD_START_SENSITIVITY")
        or "START_SENSITIVITY_NORMAL"
    ).strip()
    gemini_live_end_sensitivity: str = (
        os.getenv("GEMINI_LIVE_END_SENSITIVITY")
        or os.getenv("GEMINI_LIVE_VAD_END_SENSITIVITY")
        or "END_SENSITIVITY_NORMAL"
    ).strip()
    # Appended system text nudging concise turns + yield-on-overlap (phone calls).
    gemini_live_append_turn_instructions: bool = _b("GEMINI_LIVE_APPEND_TURN_INSTRUCTIONS", True)
    gemini_live_temperature: float = float(os.getenv("GEMINI_LIVE_TEMPERATURE", "0.65"))
    # When no scripted PCM opening: brief gate before forwarding callee mic → Gemini (avoids chopping first model syllable).
    vobiz_gemini_live_forward_mute_seconds: float = float(
        os.getenv("VOBIZ_GEMINI_FORWARD_MUTE_SECONDS", "0.05")
    )

    # Playout jitter buffer — lower = faster first syllable after user stops talking.
    vobiz_playout_prebuffer_seconds: float = float(
        os.getenv("VOBIZ_PLAYOUT_PREBUFFER_SECONDS", "0.01")
    )
    # Ultra mode caused audio glitches — off by default; use stable jitter buffer instead.
    vobiz_ultra_low_latency: bool = _b("VOBIZ_ULTRA_LOW_LATENCY", False)
    vobiz_outbound_audio_gain: float = float(os.getenv("VOBIZ_OUTBOUND_AUDIO_GAIN", "1.45"))
    # Half-duplex echo guard: drop low-energy mic frames while AI audio is on playAudio.
    vobiz_echo_suppress_during_playout: bool = _b("VOBIZ_ECHO_SUPPRESS_DURING_PLAYOUT", False)
    vobiz_echo_suppress_rms_threshold: int = int(os.getenv("VOBIZ_ECHO_SUPPRESS_RMS_THRESHOLD", "900"))
    # Off by default — continuous silent playAudio frames can loop caller audio on some carriers.
    vobiz_playout_idle_silence: bool = _b("VOBIZ_PLAYOUT_IDLE_SILENCE", False)
    gemini_live_rag_block_on_activity_end: bool = _b("GEMINI_LIVE_RAG_BLOCK_ON_ACTIVITY_END", False)
    gemini_live_resample_chunk_ms: float = float(os.getenv("GEMINI_LIVE_RESAMPLE_CHUNK_MS", "20"))
    gemini_tts_min_emit_ms: int = int(os.getenv("GEMINI_TTS_MIN_EMIT_MS", "15"))
    # When KB digest is in system prompt, skip blocking per-question RAG inject (native VAD is faster).
    gemini_live_skip_blocking_rag_when_kb_embedded: bool = _b(
        "GEMINI_LIVE_SKIP_BLOCKING_RAG_WHEN_KB_EMBEDDED", True
    )
    tts_provider: str = (os.getenv("TTS_PROVIDER", "gemini") or "gemini").strip()

    # Pipeline audio output sample rate (Hz). Gemini Live emits 24 kHz;
    # Vobiz telephony carrier wants 16 kHz. This controls resampling target.
    pipeline_audio_out_hz: int = int(os.getenv("PIPELINE_AUDIO_OUT_HZ", "16000"))
    # Resampling quality: "fast" (audioop.ratecv), "high" (numpy linear interp)
    pipeline_audio_resample_quality: str = os.getenv("PIPELINE_AUDIO_RESAMPLE_QUALITY", "high").strip()

    # Conversation logging
    conversation_log_enabled: bool = _b("CONVERSATION_LOG_ENABLED", True)
    conversation_log_dir: str = os.getenv(
        "CONVERSATION_LOG_DIR", str(_BACKEND_DIR / "data" / "conversation_logs")
    )

    # Optional outbound bed noise under voice — **off by default**. Set BACKGROUND_MUSIC_ENABLED=1
    # plus BACKGROUND_MUSIC_PATH / BACKGROUND_MUSIC_VOLUME to re-enable (see live_session mixer).
    background_music_enabled: bool = _b("BACKGROUND_MUSIC_ENABLED", False)
    background_music_path: str = os.getenv("BACKGROUND_MUSIC_PATH", "").strip()
    background_music_volume: float = float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0"))

    # Vobiz Telephony — Global Fallback
    vobiz_auth_id: str = os.getenv("VOBIZ_AUTH_ID", "").strip()
    vobiz_auth_token: str = os.getenv("VOBIZ_AUTH_TOKEN", "").strip()
    vobiz_from_number: str = os.getenv("VOBIZ_FROM_NUMBER", "").strip()
    vobiz_public_base_url: str = os.getenv("VOBIZ_PUBLIC_BASE_URL", "").strip()
    # Origin for Vobiz <Stream> WebSocket only (may differ from callback URL).
    # Quick tunnels often accept POST /vobiz/answer but fail WebSocket upgrades from carrier POPs.
    vobiz_stream_public_base_url: str = os.getenv("VOBIZ_STREAM_PUBLIC_BASE_URL", "").strip().rstrip("/")

    # Vobiz Telephony — Sales 1 role (2 phone numbers)
    vobiz_sales_1_auth_id: str = os.getenv("VOBIZ_SALES_1_AUTH_ID", "").strip()
    vobiz_sales_1_auth_token: str = os.getenv("VOBIZ_SALES_1_AUTH_TOKEN", "").strip()
    vobiz_sales_1_phone_1: str = os.getenv("VOBIZ_SALES_1_PHONE_1", "").strip()
    vobiz_sales_1_phone_2: str = os.getenv("VOBIZ_SALES_1_PHONE_2", "").strip()

    # Opening/greeting line for outbound calls
    vobiz_opening_line_default: str = os.getenv("VOBIZ_OPENING_LINE_DEFAULT", "").strip()

    # Dariaan — auto book discovery call + WhatsApp Meet link (Interested only)
    whatsapp_proxy_enabled: bool = _b("WHATSAPP_PROXY_ENABLED", False)
    whatsapp_proxy_url: str = os.getenv("WHATSAPP_PROXY_URL", "http://127.0.0.1:3001").strip()
    whatsapp_proxy_secret: str = os.getenv("WHATSAPP_PROXY_SECRET", "").strip()

    # Meta WhatsApp Cloud API — direct outbound messaging
    whatsapp_access_token: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    whatsapp_phone_number_id: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    whatsapp_verify_token: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    whatsapp_inbound_leads_enabled: bool = _b("WHATSAPP_INBOUND_LEADS_ENABLED", True)
    whatsapp_auto_dial_dariaan: bool = _b("WHATSAPP_AUTO_DIAL_DARIAAN", False)

    # BotSpice / CloudWapp WhatsApp template API (image / video / document via public URLs)
    botspice_api_url: str = os.getenv(
        "BOTSPICE_API_URL",
        "https://cloudwapp.botspice.com/api/wappBroad/triggerwam",
    ).strip()
    botspice_use_test: bool = _b("BOTSPICE_USE_TEST", False)
    botspice_token: str = os.getenv("BOTSPICE_TOKEN", "").strip()
    botspice_phone_number_id: str = (
        os.getenv("BOTSPICE_TEST_PHONE_NUMBER_ID", "").strip()
        if _b("BOTSPICE_USE_TEST", False)
        else os.getenv("BOTSPICE_PHONE_NUMBER_ID", "").strip()
    )
    botspice_template_name: str = (
        os.getenv("BOTSPICE_TEST_TEMPLATE_NAME", "").strip()
        if _b("BOTSPICE_USE_TEST", False)
        else os.getenv("BOTSPICE_TEMPLATE_NAME", "solitaire_unity").strip()
    )
    botspice_template_interested: str = (
        os.getenv("BOTSPICE_TEST_TEMPLATE_INTERESTED", "").strip()
        if _b("BOTSPICE_USE_TEST", False)
        else (os.getenv("BOTSPICE_TEMPLATE_INTERESTED", "").strip()
              or os.getenv("BOTSPICE_TEMPLATE_NAME", "solitaire_unity").strip())
    )
    botspice_template_site_visit: str = (
        os.getenv("BOTSPICE_TEST_TEMPLATE_SITE_VISIT", "").strip()
        if _b("BOTSPICE_USE_TEST", False)
        else os.getenv("BOTSPICE_TEMPLATE_SITE_VISIT", "").strip()
    )
    botspice_template_callback: str = (
        os.getenv("BOTSPICE_TEST_TEMPLATE_CALLBACK", "").strip()
        if _b("BOTSPICE_USE_TEST", False)
        else os.getenv("BOTSPICE_TEMPLATE_CALLBACK", "").strip()
    )
    botspice_language_code: str = os.getenv("BOTSPICE_LANGUAGE_CODE", "en").strip() or "en"
    botspice_connection_name: str = os.getenv("BOTSPICE_CONNECTION_NAME", "").strip()
    botspice_default_media_url: str = os.getenv("BOTSPICE_DEFAULT_MEDIA_URL", "").strip()
    whatsapp_media_public_base_url: str = os.getenv("WHATSAPP_MEDIA_PUBLIC_BASE_URL", "").strip()
    whatsapp_auto_send_for: str = os.getenv(
        "WHATSAPP_AUTO_SEND_FOR",
        "interested,site_visit,site_visited,callback_scheduled,callback",
    ).strip()

    # Dariaan WhatsApp QR / wa.me link
    dariaan_whatsapp_number: str = os.getenv("DARIAAN_WHATSAPP_NUMBER", "").strip()
    dariaan_whatsapp_qr_message: str = os.getenv("DARIAAN_WHATSAPP_QR_MESSAGE", "").strip()

    # OpenWA — WhatsApp API Gateway (replaces old whatsapp-proxy sidecar)
    openwa_enabled: bool = _b("OPENWA_ENABLED", False)
    openwa_api_url: str = os.getenv("OPENWA_API_URL", "http://127.0.0.1:2785").strip()
    openwa_api_key: str = os.getenv("OPENWA_API_KEY", "").strip()
    openwa_session_id: str = os.getenv("OPENWA_SESSION_ID", "").strip()

    # Daily calling limit per phone number
    daily_call_limit_per_phone: int = int(os.getenv("DAILY_CALL_LIMIT_PER_PHONE", "220"))

    # Live dashboard — SSE tick + background poll (ms). 500ms feels continuous; literal 5ms would overload server/browser.
    live_dashboard_poll_ms: int = max(200, min(5000, int(os.getenv("LIVE_DASHBOARD_POLL_MS", "500"))))
    live_sse_tick_ms: int = max(200, min(5000, int(os.getenv("LIVE_SSE_TICK_MS", "500"))))
    live_kv_cache_ttl_sec: float = max(0.1, min(5.0, float(os.getenv("LIVE_KV_CACHE_TTL_SEC", "0.4"))))

    # Meta Cloud API supplementary brochure sends (images/PDFs/text after BotSpice template).
    # Disable when WHATSAPP_PHONE_NUMBER_ID account is not registered — BotSpice template is enough.
    whatsapp_meta_supplementary_enabled: bool = _b("WHATSAPP_META_SUPPLEMENTARY_ENABLED", True)

    def __post_init__(self) -> None:
        """Resolve BotSpice prod vs test credentials after full env load (systemd .env + .env.vps)."""
        use_test = self.botspice_use_test
        if use_test:
            self.botspice_phone_number_id = (
                os.getenv("BOTSPICE_TEST_PHONE_NUMBER_ID", "") or self.botspice_phone_number_id
            ).strip()
            self.botspice_template_name = (
                os.getenv("BOTSPICE_TEST_TEMPLATE_NAME", "") or self.botspice_template_name
            ).strip()
            self.botspice_template_interested = (
                os.getenv("BOTSPICE_TEST_TEMPLATE_INTERESTED", "") or self.botspice_template_interested
            ).strip()
            self.botspice_template_site_visit = (
                os.getenv("BOTSPICE_TEST_TEMPLATE_SITE_VISIT", "") or self.botspice_template_site_visit
            ).strip()
            self.botspice_template_callback = (
                os.getenv("BOTSPICE_TEST_TEMPLATE_CALLBACK", "") or self.botspice_template_callback
            ).strip()
        else:
            self.botspice_phone_number_id = (
                os.getenv("BOTSPICE_PHONE_NUMBER_ID", "") or self.botspice_phone_number_id
            ).strip()
            self.botspice_template_name = (
                os.getenv("BOTSPICE_TEMPLATE_NAME", "") or self.botspice_template_name
            ).strip() or "solitaire_unity"
            self.botspice_template_interested = (
                os.getenv("BOTSPICE_TEMPLATE_INTERESTED", "")
                or os.getenv("BOTSPICE_TEMPLATE_NAME", "")
                or self.botspice_template_interested
            ).strip() or self.botspice_template_name
            self.botspice_template_site_visit = (
                os.getenv("BOTSPICE_TEMPLATE_SITE_VISIT", "") or self.botspice_template_site_visit
            ).strip()
            self.botspice_template_callback = (
                os.getenv("BOTSPICE_TEMPLATE_CALLBACK", "") or self.botspice_template_callback
            ).strip()


settings = Settings()


def live_dashboard_meta() -> dict[str, int | float]:
    """Timing hints exposed to the dashboard client."""
    return {
        "live_poll_ms": settings.live_dashboard_poll_ms,
        "live_sse_tick_ms": settings.live_sse_tick_ms,
    }


def server_url_to_ws(url: str, path: str = "/ws") -> str:
    """Turn https://host into wss://host/path for Vobiz stream."""
    u = url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://") :] + path
    if u.startswith("http://"):
        return "ws://" + u[len("http://") :] + path
    return u + path


def validate_critical_config() -> list[str]:
    """Return list of human-readable configuration problems (empty if OK)."""
    problems: list[str] = []
    if not settings.gemini_api_key:
        problems.append("GEMINI_API_KEY / GOOGLE_API_KEY is not set")
    vb = (
        settings.vobiz_auth_id
        and settings.vobiz_auth_token
        and settings.vobiz_from_number
    )
    if vb and not settings.vobiz_public_base_url:
        problems.append(
            "Vobiz is partially configured (auth/from set) but VOBIZ_PUBLIC_BASE_URL is empty — "
            "outbound calls cannot deliver answer XML or media WebSocket."
        )
    if vb and settings.vobiz_public_base_url and "proxy.runpod.net" in settings.vobiz_public_base_url:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL uses RunPod HTTP proxy, which may not work externally. "
            "Consider switching to a Cloudflare tunnel or direct domain."
        )
    ts = settings.vobiz_stream_public_base_url or ""
    pub = settings.vobiz_public_base_url or ""
    if vb and pub and ("trycloudflare.com" in pub or "trycloudflare.dev" in pub) and not ts:
        problems.append(
            "VOBIZ_PUBLIC_BASE_URL looks like a Cloudflare quick tunnel — media WebSockets often never "
            "reach your server (calls ring then drop). Set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http(s) origin with port (e.g. http://YOUR_IP:8000) while keeping callbacks on the tunnel "
            "if needed, or switch fully to a stable domain."
        )
    return problems
