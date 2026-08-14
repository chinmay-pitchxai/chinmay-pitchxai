"""Wire-format sizes, sampling rates, and VAD constants for Vobiz + Gemini Live."""

# Vobiz: audio/x-l16;rate=16000
VOBIZ_SR = 16000
VOBIZ_CONTENT_TYPE = "audio/x-l16"
VOBIZ_MEDIA_CONTENT_TYPE = f"{VOBIZ_CONTENT_TYPE};rate={VOBIZ_SR}"

# 20 ms outbound chunks to the handset → 320 samples × 2 bytes
OUT_CHUNK_MS = 20
OUT_CHUNK_BYTES = int(VOBIZ_SR * (OUT_CHUNK_MS / 1000.0)) * 2

# Energy-based VAD (reserved for future use)
VAD_RMS_OPEN = 0.015
VAD_RMS_CLOSE = 0.008
VAD_SILENCE_MS_TO_CLOSE = 650
VAD_MIN_UTTERANCE_MS = 250
VAD_MAX_UTTERANCE_MS = 12000

# Gemini Live emits 24 kHz mono PCM; Vobiz wants 16 kHz.
GEMINI_OUT_SR = 24000
