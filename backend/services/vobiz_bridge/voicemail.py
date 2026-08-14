"""Voicemail / call-screening detection and nudge text for live PSTN calls."""

from __future__ import annotations

import re
from typing import Literal

VoicemailKind = Literal["screening", "beep"]
CalleeClass = Literal["unknown", "human", "automated"]

# Phrases heard on Indian + iPhone/Samsung/Google screening & voicemail systems.
_VM_SCREENING_PHRASES = (
    "using an assistant",
    "please state why",
    "state your name and reason",
    "call may be screened",
    "screen my calls",
    "reason to contact",
    "reason for to contact",
    "reason for contacting",
    "contact the person",
    "please tell me the reason",
    "tell me the reason",
    "who is calling",
    "live voicemail",
    "call screen",
    "screening your call",
    "automated assistant",
    "bixby text call",
    "google assistant",
    "state your name",
)

_VM_BEEP_PHRASES = (
    "leave a message",
    "after the tone",
    "at the beep",
    "voice messaging system",
    "please leave your message",
    "not available to take your call",
    "record your message",
    "mailbox is full",
    "please record",
    "record message",
    "leave a voice mail",
    "leave a voicemail",
    "voice mail",
    "voicemail",
    "person you have called",
    "person you are calling",
    "the called party",
    "subscriber you have dialed",
    "cannot be connected",
    "could not be connected",
    "forwarded to voicemail",
    "sent to voicemail",
    "mobile is switched off",
    "mobile phone is switched off",
    "switched off",
    "not reachable",
    "number not reachable",
    "out of coverage",
    "please try again later",
    "aapka call",
    "is number par",
    # Carrier / Jio / Airtel standard VM prompts (Indian PSTN)
    "when you have finished recording",
    "finished recording you may hang up",
    "you may hang up",
    "end of the message",
    "at the end of the message",
    "when you are done recording",
    "after the message",
    "reply after the tone",
    "at the tone please record",
)

_VM_ALL_PHRASES = _VM_SCREENING_PHRASES + _VM_BEEP_PHRASES


def classify_voicemail_stt(text: str) -> VoicemailKind | None:
    """Return screening vs beep if STT looks like voicemail/IVR, else None."""
    tl = (text or "").lower().strip()
    if not tl:
        return None
    if any(p in tl for p in _VM_BEEP_PHRASES):
        return "beep"
    if any(p in tl for p in _VM_SCREENING_PHRASES):
        return "screening"
    return None


def is_voicemail_stt(text: str) -> bool:
    return classify_voicemail_stt(text) is not None


def looks_like_early_human_during_greeting(text: str) -> bool:
    """Human pickup while prerecorded greeting plays (hello, yes, who is this)."""
    if not text or is_voicemail_stt(text):
        return False
    tl = re.sub(r"[^\w\s]", " ", text.lower()).strip()
    if len(tl) < 2:
        return False
    human_pickup = (
        "hello", "hi", "hey", "haan", "han", "ji", "yes", "yeah", "speaking",
        "who is this", "who's this", "namaste", "namaskar", "bolo", "boliye",
        "tell me", "go ahead", "one minute", "ha bolo", "yes tell me",
    )
    return any(p in tl for p in human_pickup)


def classify_callee_from_stt(text: str) -> tuple[CalleeClass, VoicemailKind | None]:
    """Classify live STT as human, automated (VM/screening), or still unknown."""
    vm_kind = classify_voicemail_stt(text)
    if vm_kind:
        return "automated", vm_kind
    if looks_like_early_human_during_greeting(text) or looks_like_live_human_after_screening(text):
        return "human", None
    return "unknown", None


def looks_like_live_human_after_screening(text: str) -> bool:
    """True when a real person seems to have taken over (not IVR/voicemail)."""
    if not text or is_voicemail_stt(text):
        return False
    tl = re.sub(r"[^\w\s]", " ", text.lower()).strip()
    if len(tl) < 2:
        return False
    words = tl.split()
    # Short human backchannels after screening
    human_tokens = {
        "yes", "yeah", "yep", "speaking", "hello", "hi", "hey", "haan", "han", "ji",
        "bolo", "tell", "who", "what", "namaste", "namaskar", "this", "me", "here",
        "listening", "go", "ahead", "sure", "okay", "ok", "boliye",
    }
    if any(w not in human_tokens for w in words):
        return len(words) >= 2
    return len(words) >= 1 and not all(w in _VM_ALL_PHRASES for w in words)


def screening_nudge_text(agent_name: str) -> str:
    name = (agent_name or "Vernika").strip()
    return (
        "[CALL SCREENING / LIVE VOICEMAIL DETECTED — SPEAK NOW]\n"
        "You reached an automated screener (iPhone, Samsung, or Google Live Voicemail).\n"
        f"Say clearly in 2 short sentences as {name} from Technopolis Constructions:\n"
        "1. 'Hi, this is {name} from Technopolis Constructions, calling about Solitaire Unity — "
        "our premium ready-to-move apartments in Kondapur in Kondapur, Hyderabad.'\n"
        "2. 'Please connect me to the customer, or they can call us back.'\n"
        "Then STOP and WAIT silently up to 5 seconds for a REAL human voice.\n"
        "If a human speaks (yes, hello, speaking, who is this) — continue the normal sales conversation.\n"
        "Do NOT use end_call until you have waited for a human or only hear a beep/silence."
    ).replace("{name}", name)


def beep_message_nudge_text(agent_name: str) -> str:
    name = (agent_name or "Vernika").strip()
    return (
        "[VOICEMAIL / ANSWERING MACHINE — LEAVE MESSAGE NOW — ONE TURN]\n"
        f"Speak as {name} from Technopolis Constructions. Leave a clear 15–20 second voicemail:\n"
        f"'Hi, this is {name} from Technopolis Constructions. You enquired about Solitaire Unity — "
        "our premium 4 and 5 BHK apartments near Kondapur with spacious layouts. "
        "the project possession is December 2026. Please call us back or check WhatsApp for details. Thank you.'\n"
        "Speak naturally, then use end_call immediately. Do NOT wait for a reply."
    )


def silence_voicemail_nudge_text(agent_name: str) -> str:
    """No human answered — treat as voicemail/no-response."""
    return beep_message_nudge_text(agent_name)
