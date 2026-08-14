"""Detect soft-positive sales interest from caller text (email/WhatsApp/send details/will check)."""

from __future__ import annotations

import json
import re
from typing import Iterable

# Firm rejection — never mark Interested
_NEGATIVE = re.compile(
    r"(?:"
    r"not\s*interested|no\s*interest|don'?t\s+call|do\s+not\s+call|stop\s+calling|"
    r"remove\s+(?:me|my)|take\s+me\s+off|wrong\s+number|galat\s+number|"
    r"never\s+call|already\s+have\s+(?:a\s+)?vendor|"
    r"not\s*intrested|no\s+thanks|nahi\s+chahiye|not\s+intrested\b"
    r")",
    re.I,
)

# Auto-attendant / carrier IVR — not a prospect
_IVR = re.compile(
    r"(?:"
    r"press\s+(?:one|two|three|\d)|hindi\s+ke\s+liye|for\s+english|english\s+press|"
    r"airtel|jio|vodafone|miss\s+call\s+seva|apni\s+pasand\s+ki\s+seva|"
    r"your\s+call\s+will\s+be\s+recorded|stay\s+on\s+the\s+line|"
    r"chhattisgarhi\s+mein|speed\s+up\s+airtel"
    r")",
    re.I,
)

# Automated voice screening / voicemail systems
_VOICEMAIL = re.compile(
    r"(?:"
    r"voicemail|voice\s+mail|answering\s+machine|leave\s+a\s+message|record\s+your\s+message|"
    r"bixby\s+text\s+call|live\s+voicemail|call\s+screen|screening\s+(?:your\s+)?call|automated\s+assistant|"
    r"state\s+your\s+name|not\s+available|leave\s+a\s+voicemail|stay\s+on\s+the\s+line|"
    r"reason\s+to\s+contact|reason\s+for\s+to\s+contact|contact\s+the\s+person|screen\s+my\s+calls|"
    r"please\s+tell\s+me\s+the\s+reason|tell\s+me\s+the\s+reason|reason\s+for\s+contacting|who\s+is\s+calling|"
    r"switched\s+off|out\s+of\s+(?:coverage|reach|service)|not\s+reachable|number\s+not\s+reachable|"
    r"is\s+currently\s+(?:busy|unavailable|switched)|please\s+try\s+again|mailbox\s+(?:is\s+)?full|"
    r"aapka\s+call|yeh\s+number|is\s+number\s+par|band\s+hai|network\s+mein\s+nahi|"
    r"finished\s+recording|you\s+may\s+hang\s+up|end\s+of\s+the\s+message|when\s+you\s+have\s+finished|"
    r"person\s+you\s+(?:have\s+)?(?:called|dialed|are\s+calling)|"
    r"subscriber(?:\s+you\s+are\s+calling)?\s+is\s+(?:not\s+)?(?:available|busy|unreachable)|"
    r"cannot\s+be\s+connected|could\s+not\s+be\s+connected|"
    r"forwarded\s+to\s+voicemail|sent\s+to\s+voicemail|"
    r"mobile\s+(?:phone\s+)?(?:is\s+)?(?:switched\s+off|powered\s+off)|"
    r"the\s+called\s+party|call\s+has\s+been\s+forwarded"
    r")",
    re.I,
)

# Explicit user-requested callback — NOT no-answer / system redial
_USER_CALLBACK_REQUEST = re.compile(
    r"(?:"
    r"call\s+(?:me\s+)?(?:back|later|again|tomorrow|evening|morning)|"
    r"(?:i'?m|i\s+am|im)\s+busy|busy\s+(?:now|right\s+now|at\s+the\s+moment)|"
    r"not\s+(?:a\s+)?good\s+time|can(?:not|'t)\s+talk|cannot\s+talk|"
    r"later\s+(?:today|tomorrow|evening|morning|tonight)|"
    r"after\s+(?:\d+\s*(?:pm|am|hours?|mins?|minutes?)|lunch|office|some\s+time)|"
    r"baad\s+mein|kal\s+call|busy\s+hoon|abhi\s+busy|"
    r"call\s+kar(?:o|na|iye|iyega)\s+(?:baad|kal|later)|"
    r"ring\s+(?:me\s+)?(?:back|later|again)|"
    r"talk\s+(?:later|tomorrow)|"
    r"in\s+a\s+(?:meeting|call)|"
    r"driving|driving\s+now"
    r")",
    re.I,
)

_HUMAN_CONVERSATION = re.compile(
    r"(?:yes|yeah|tell\s+me|interested|send|email|price|location|who\s+is\s+this|speaking|go\s+ahead)",
    re.I,
)

# Virtual-meet / online-meet only phrases — agent scheduled a video call NOT a physical site visit
_VIRTUAL_MEET_ONLY = re.compile(
    r"(?:virtual\s+meet|virtual\s+meeting|video\s+(?:call|meet|walk.?through)|online\s+meet|"
    r"video\s+tour|virtual\s+tour|zoom|google\s+meet|teams\s+meet|online\s+walkthrough)",
    re.I,
)

# User confirming a physical visit (not the agent asking "would you like to visit?")
_USER_SITE_VISIT_CONFIRM = re.compile(
    r"(?:"
    r"(?:yes|yeah|sure|okay|ok|haan|ji).{0,40}(?:visit|come|see\s+the\s+site)|"
    r"(?:i(?:'ll|\s+will)|we(?:'ll|\s+will))\s+(?:visit|come|be\s+there)|"
    r"(?:visit|coming)\s+(?:this\s+)?(?:saturday|sunday|weekend|tomorrow)|"
    r"this\s+(?:saturday|sunday|weekend)\s+(?:is\s+)?(?:fine|works|ok|good)|"
    r"(?:two|three|four|2|3|4)\s+(?:of\s+us|people|persons)\s+(?:will\s+)?(?:come|visit)"
    r")",
    re.I,
)

_AGENT_SITE_VISIT_ASK = re.compile(
    r"(?:would you like|do you want|shall we|can you|can we|when would you|would you be able).{0,60}(?:visit|site)",
    re.I,
)

_LOCATION_ASK_ONLY = re.compile(
    r"(?:where\s+(?:exactly\s+)?(?:is\s+it|it\s+is)\s+(?:located|location)|where\s+(?:is|are)\s+(?:the|it|this|that))",
    re.I,
)

# Soft interest — email, send details, will review, etc.
_POSITIVE = re.compile(
    r"(?:"
    r"send\s+(?:me|us|the|kar|dijiye|dijiyega|details|information|info|a\s+note|write.?up|brochure)|"
    r"(?:please\s+)?(?:share|bhej|bhejna|bhej\s+dijiye|mail\s+kar)\s+.*(?:detail|info|email|mail|whatsapp)|"
    r"(?:email|e-?mail|whatsapp|whats\s*app).{0,40}(?:send|share|bhej|kar\s+dijiye|pe\s+bhej)|"
    r"(?:send|share).{0,30}(?:email|e-?mail|whatsapp|whats\s*app|mail)|"
    r"(?:provide|give|share).{0,20}(?:my\s+)?(?:email|e-?mail|mail\s+id)|"
    r"(?:requested|asked|wants?).{0,30}(?:email|whatsapp|details|information)|"
    r"information\s+via\s+email|preference\s+for.{0,20}email|"
    r"will\s+check|i'?ll\s+check|let\s+me\s+check|check\s+and\s+(?:get\s+back|revert)|"
    r"(?:our\s+)?people\s+will\s+decide|decide\s+on\s+that|"
    r"expressed\s+interest|"
    r"(?:i\s+am|i'?m)\s+interested\s+(?:in\s+(?:this|it|the|your|surya|property|villa|project|flat|plot|site|apartment)|to\s+know|to\s+see|to\s+visit)|"
    r"self[\-\s]?use|for\s+self[\-\s]?use|investment\s+or\s+self|"
    r"(?:\d+(?:\.\d+)?\s*(?:to|-)\s*\d+(?:\.\d+)?\s*crore)|(?:budget|range).{0,20}crore|"
    r"what\s+is\s+it\s+priced|priced\s+at|price|where\s+exactly|location|"
    r"(?:okay|ok|theek|thik)\s*.{0,12}(?:send|bhej|mail)|"
    r"(?:demo|quotation|quote|pricing|brochure).{0,30}(?:send|email|share)|"
    r"note\s+write.?up|write.?up\s+on\s+that|"
    r"sales@|@(?:gmail|yahoo|outlook|hotmail|co\.in|com)\b|"
    r"tell\s+me\s+more|what\s+is\s+the\s+(?:price|cost|location|size|area)|where\s+is\s+it|location\s+kaha\s+hai"
    r")",
    re.I,
)

# Generic financial/career interest that is NOT property interest — used to exclude false positives
# e.g. "I'm interested in investments" / "interested in stocks" / "interested in a job"
_GENERIC_NON_PROPERTY_INTEREST = re.compile(
    r"interested\s+in\s+(?:invest|stock|share\s+market|mutual\s+fund|sip|trading|equity|job|jobs|work|career|joining|loan|insurance|fd|fixed\s+deposit)",
    re.I,
)


def _iter_turns(transcript_text: str) -> Iterable[tuple[str, str]]:
    for line in (transcript_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = str(obj.get("role") or obj.get("type") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = str(obj.get("content") or obj.get("text") or obj.get("message") or "").strip()
        if content:
            yield role, content


def _iter_user_lines(transcript_text: str) -> Iterable[str]:
    for role, content in _iter_turns(transcript_text):
        if role == "user":
            yield content


_AFFIRMATIVE = re.compile(
    r"^(?:yes|yeah|yep|yup|ok(?:ay)?|sure|please|haan|haanji|ha|ji|theek|thik|"
    r"bilkul|send\s+it|go\s+ahead|bhej|kar\s+dijiye|mail\s+kar|interested)\b",
    re.I,
)

_ASK_SEND = re.compile(
    r"(?:send|share|bhej|mail|email|whatsapp).{0,40}(?:detail|info|information|email|mail|brochure)|"
    r"(?:can|shall|may)\s+i\s+send",
    re.I,
)


def caller_text_from_transcript(transcript_text: str) -> str:
    return " ".join(_iter_user_lines(transcript_text))


def full_transcript_blob(transcript_text: str) -> str:
    """All spoken turns (user + assistant) — carrier VM prompts often land on assistant."""
    parts = [content for _, content in _iter_turns(transcript_text)]
    if parts:
        return " ".join(parts)
    return (transcript_text or "").strip()


def user_requested_callback_in_transcript(transcript_text: str) -> bool:
    """True only when the callee explicitly asked to be called back later / said they are busy."""
    user = caller_text_from_transcript(transcript_text)
    if not user or len(user.strip()) < 4:
        return False
    if _NEGATIVE.search(user):
        return False
    return bool(_USER_CALLBACK_REQUEST.search(user))


def analysis_indicates_user_callback(analysis: dict, transcript_text: str) -> bool:
    """Callback bucket requires transcript proof — never infer from LLM alone."""
    tx = (transcript_text or "").strip()
    if not user_requested_callback_in_transcript(tx):
        return False
    from services.call_analyzer import canonical_disposition

    canon = canonical_disposition(str(analysis.get("disposition") or ""))
    if canon in ("Not Interested", "Wrong Number", "Voicemail", "Call Screened", "No Answer", "No Response"):
        return False
    if canon in ("Call Later", "Busy", "Callback", "Interested", "Answered"):
        return True
    na = analysis.get("next_action") if isinstance(analysis.get("next_action"), dict) else {}
    action = str(na.get("action_type") or "").lower()
    if action in ("callback", "call back", "call later", "follow up", "follow-up", "followup"):
        return True
    if analysis.get("requested_callback_datetime_iso"):
        return True
    return False


def _assistant_asked_send_user_agreed(transcript_text: str) -> bool:
    """e.g. assistant offers email → user says yes / okay / haan."""
    turns = list(_iter_turns(transcript_text))
    for i, (role, content) in enumerate(turns):
        if role != "assistant" or not _ASK_SEND.search(content):
            continue
        for j in range(i + 1, min(i + 4, len(turns))):
            r2, c2 = turns[j]
            if r2 == "user" and (_AFFIRMATIVE.search(c2) or _POSITIVE.search(c2)):
                return True
    return False


def soft_interest_in_text(*chunks: str | None) -> bool:
    """True when combined text shows send-details / email / will-review style interest."""
    blob = " ".join(str(c or "").strip() for c in chunks if c)
    if len(blob) < 8:
        return False
    if _NEGATIVE.search(blob):
        return False
    # Exclude generic financial/career interest — these are NOT property interest
    if _GENERIC_NON_PROPERTY_INTEREST.search(blob):
        return False
    if _POSITIVE.search(blob):
        return True
    return False


def is_likely_ivr_or_no_prospect(transcript_text: str) -> bool:
    user = caller_text_from_transcript(transcript_text)
    if not user or len(user) < 12:
        return False
    if _IVR.search(user) and not _POSITIVE.search(user):
        return True
    return False


def is_voicemail_or_screening_transcript(transcript_text: str) -> bool:
    blob = full_transcript_blob(transcript_text)
    if not blob.strip():
        return False
    if _VOICEMAIL.search(blob):
        return True

    user_turns = [content for role, content in _iter_turns(transcript_text) if role == "user"]
    if len(user_turns) >= 3:
        user_blob = " ".join(user_turns)
        if _HUMAN_CONVERSATION.search(user_blob) and not _VOICEMAIL.search(user_blob):
            return False
    return False


def infer_site_visit_from_transcript(transcript_text: str) -> bool:
    """True when the caller confirmed a physical site visit — not when agent asked."""
    if not (transcript_text or "").strip():
        return False
    if is_voicemail_or_screening_transcript(transcript_text):
        return False
    user = caller_text_from_transcript(transcript_text)
    if not user or len(user.strip()) < 6:
        return False
    user_lc = user.lower()
    # Agent question mis-attributed to user (common STT bug) — not a booking
    if _AGENT_SITE_VISIT_ASK.search(user_lc):
        return False
    if _LOCATION_ASK_ONLY.search(user_lc) and not _USER_SITE_VISIT_CONFIRM.search(user_lc):
        return False
    if user_lc.strip().endswith("?") and "visit" in user_lc:
        return False
    if not _USER_SITE_VISIT_CONFIRM.search(user_lc):
        return False
    if _VIRTUAL_MEET_ONLY.search(user_lc):
        return False
    return True


def infer_interest_for_role(
    transcript_text: str,
    *,
    role: str = "",
    lead_name: str | None = None,
) -> bool:
    return infer_interest_from_transcript(transcript_text)


def infer_interest_from_transcript(transcript_text: str) -> bool:
    if is_voicemail_or_screening_transcript(transcript_text):
        return False
    if infer_site_visit_from_transcript(transcript_text):
        return False
    user = caller_text_from_transcript(transcript_text)
    if is_likely_ivr_or_no_prospect(transcript_text):
        return False
    if _assistant_asked_send_user_agreed(transcript_text):
        return True
    if not user or len(user) < 4:
        return False
    # Exclude generic financial/career interest (e.g. STT mishear of "not interested" → "interested in investments")
    if _GENERIC_NON_PROPERTY_INTEREST.search(user):
        return False
    # Very short single-turn calls (user spoke only once, < 60 chars) — too ambiguous to call Interested
    # unless they explicitly asked to send details or mentioned property
    turns = list(_iter_turns(transcript_text))
    user_turns = [c for role, c in turns if role == "user"]
    if len(user_turns) == 1 and len(user.strip()) < 60:
        # Only flag Interested if there's a concrete details/email/whatsapp ask
        concrete = re.compile(
            r"send|email|whatsapp|details|info|brochure|call\s+back|price|location|site\s+visit",
            re.I,
        )
        if not concrete.search(user):
            return False
    return soft_interest_in_text(user)


def thin_transcript_blocks_interest(
    transcript_text: str | None,
    *,
    role: str = "",
    lead_name: str | None = None,
) -> bool:
    """True when customer speech is too thin to trust Interested / Site Visit outcomes."""
    tx = (transcript_text or "").strip()
    if not tx:
        return False
    from services.transcript_thin import transcript_is_thin

    thin, _reason = transcript_is_thin(tx)
    if not thin:
        return False
    return not infer_interest_for_role(tx, role=role, lead_name=lead_name)


def _cap_answered_for_thin_transcript(
    out: dict,
    transcript_text: str | None,
    *,
    role: str = "",
    lead_name: str | None = None,
) -> dict:
    """Downgrade LLM-inferred interest when the live transcript lacks customer proof."""
    if not thin_transcript_blocks_interest(transcript_text, role=role, lead_name=lead_name):
        return out
    out["disposition"] = "Answered"
    out["site_visit_agreed"] = False
    out.pop("outcome_from_transcript", None)
    return out


def apply_interest_disposition_override(
    analysis: dict,
    transcript_text: str | None = None,
    *,
    role: str = "",
    lead_name: str | None = None,
) -> dict:
    """
    Upgrade generic ``Answered`` (or empty) to ``Interested`` when caller asked for
    email/WhatsApp/details or will review — matches Technopolis sales QA expectations.
    """
    out = dict(analysis or {})
    from services.call_analyzer import canonical_disposition

    _infer = lambda tx: infer_interest_for_role(tx, role=role, lead_name=lead_name)

    # 1. Check for voicemail screening override first
    if transcript_text and is_voicemail_or_screening_transcript(transcript_text):
        out["disposition"] = "Voice Mail"
        out["summary"] = "Call reached voicemail, Apple Live Voicemail, or Samsung Bixby Text Call screening."
        out["next_steps"] = "Reschedule for next day."
        return out

    # 1b. Site Visit: promote when transcript confirms physical visit; downgrade virtual-only.
    if transcript_text:
        if infer_site_visit_from_transcript(transcript_text):
            out["disposition"] = "Site Visit"
            out["site_visit_agreed"] = True
            out.pop("outcome_from_transcript", None)
            return out
        if out.get("disposition") == "Site Visit":
            full_text = transcript_text.lower()
            has_virtual_only = bool(_VIRTUAL_MEET_ONLY.search(full_text))
            has_physical = infer_site_visit_from_transcript(transcript_text)
            if has_virtual_only and not has_physical:
                out["disposition"] = "Interested"
                out["site_visit_agreed"] = False
                out["summary"] = (out.get("summary") or "") + " (Virtual meet scheduled; no physical site visit confirmed.)"

    # 2. Check for direct negative rejection override
    if transcript_text:
        user_text = caller_text_from_transcript(transcript_text)
        if user_text:
            normalized_user = " ".join(user_text.lower().split())
            has_negative = _NEGATIVE.search(normalized_user)
            
            # Direct "no" safety check (avoiding "no problem", "no issues", etc.)
            is_direct_no = False
            # Strip punctuation from each word individually
            words = [w.strip(".,?!:;()\"'-") for w in normalized_user.split()]
            if len(words) > 0 and words[0].lower() == "no":
                if len(words) == 1 or words[1].lower() not in ("problem", "issues", "issue", "worries", "doubt", "doubts"):
                    is_direct_no = True
            
            if has_negative or is_direct_no:
                out["disposition"] = "Not Interested"
                out["summary"] = "Callee expressed no interest or declined the call."
                out["next_steps"] = "Do not call again."
                return out

    canon = canonical_disposition(out.get("disposition"))
    if canon == "Interested":
        if not (transcript_text and _infer(transcript_text)):
            out["disposition"] = "Answered"
            return out

    if canon in ("Interested", "Not Interested", "Call Later", "Busy", "Wrong Number", "No Response", "Voice Mail"):
        out["disposition"] = canon
        return out

    # Only upgrade to Interested from caller transcript — never from LLM summary alone.
    if (
        transcript_text
        and _infer(transcript_text)
        and not thin_transcript_blocks_interest(transcript_text, role=role, lead_name=lead_name)
    ):
        out["disposition"] = "Interested"
        out["outcome_from_transcript"] = True
        if not str(out.get("next_steps") or "").strip() or out.get("next_steps") == "N/A":
            out["next_steps"] = "Send requested details via email or WhatsApp and schedule follow-up."
    out = _cap_answered_for_thin_transcript(
        infer_outcome_from_qa_signals(out, transcript_text, role=role, lead_name=lead_name),
        transcript_text,
        role=role,
        lead_name=lead_name,
    )
    return out


def _qa_text_blob(analysis: dict) -> str:
    """Combined summary + next_steps + next_action.details for QA signal matching."""
    parts: list[str] = []
    s = str(analysis.get("summary") or "").strip()
    if s:
        parts.append(s)
    ns = analysis.get("next_steps")
    if isinstance(ns, list):
        parts.extend(str(x) for x in ns if x)
    elif ns:
        parts.append(str(ns))
    na = analysis.get("next_action") if isinstance(analysis.get("next_action"), dict) else {}
    det = str(na.get("details") or "").strip()
    if det:
        parts.append(det)
    return " ".join(parts)


def _emotion_blocks_interest(emotion: str) -> bool:
    return str(emotion or "").strip().lower() in (
        "frustrated", "negative", "angry", "skeptical",
    )


def _emotion_boosts_interest(emotion: str) -> bool:
    return str(emotion or "").strip().lower() in (
        "interested", "excited", "positive",
    )


def _qa_site_visit_confirmed(analysis: dict, transcript_text: str | None) -> bool:
    """Physical site visit from transcript (primary) or QA text with user-confirm patterns."""
    tx = (transcript_text or "").strip()
    if tx and infer_site_visit_from_transcript(tx):
        return True
    blob = _qa_text_blob(analysis).lower()
    na = analysis.get("next_action") if isinstance(analysis.get("next_action"), dict) else {}
    details = str(na.get("details") or "").lower()
    if _VIRTUAL_MEET_ONLY.search(blob) or _VIRTUAL_MEET_ONLY.search(details):
        return False
    if _LOCATION_ASK_ONLY.search(blob) and not _USER_SITE_VISIT_CONFIRM.search(blob):
        return False
    return bool(_USER_SITE_VISIT_CONFIRM.search(blob) or _USER_SITE_VISIT_CONFIRM.search(details))


def infer_outcome_from_qa_signals(
    analysis: dict,
    transcript_text: str | None = None,
    *,
    role: str = "",
    lead_name: str | None = None,
) -> dict:
    """
    Refine disposition using summary, emotion, next_steps, and next_action.
    Transcript rules always win when they provide a clear outcome.
    """
    out = dict(analysis or {})
    from services.call_analyzer import canonical_disposition

    _infer = lambda tx: infer_interest_for_role(tx, role=role, lead_name=lead_name)

    tx = (transcript_text or "").strip()
    emotion = str(out.get("emotion_label") or "").strip()
    blob = _qa_text_blob(out)
    blob_lc = blob.lower()
    na = out.get("next_action") if isinstance(out.get("next_action"), dict) else {}
    action_type = str(na.get("action_type") or "").strip().lower()

    if tx:
        if is_voicemail_or_screening_transcript(tx):
            out["disposition"] = "Voice Mail"
            return out
        if infer_site_visit_from_transcript(tx):
            out["disposition"] = "Site Visit"
            out["site_visit_agreed"] = True
            out.pop("outcome_from_transcript", None)
            return out
        if _infer(tx):
            out["disposition"] = "Interested"
            out["outcome_from_transcript"] = True
            return out
        user_text = caller_text_from_transcript(tx)
        if user_text and _NEGATIVE.search(" ".join(user_text.lower().split())):
            out["disposition"] = "Not Interested"
            return out
        if thin_transcript_blocks_interest(tx, role=role, lead_name=lead_name):
            out["disposition"] = "Answered"
            out["site_visit_agreed"] = False
            out.pop("outcome_from_transcript", None)
            return out

    canon = canonical_disposition(out.get("disposition"))

    if canon == "Site Visit" or out.get("site_visit_agreed"):
        if not _qa_site_visit_confirmed(out, tx or None):
            out["site_visit_agreed"] = False
            tx_blocks = bool(tx and thin_transcript_blocks_interest(tx, role=role, lead_name=lead_name))
            if (
                not tx_blocks
                and (
                    (tx and _infer(tx))
                    or (soft_interest_in_text(caller_text_from_transcript(tx) if tx else ""))
                )
            ) or (
                not tx
                and soft_interest_in_text(blob)
                and action_type in ("whatsapp", "email", "virtual meet", "virtual_meet")
            ):
                out["disposition"] = "Interested"
            else:
                out["disposition"] = "Answered"
            canon = canonical_disposition(out.get("disposition"))

    if _emotion_blocks_interest(emotion):
        if _NEGATIVE.search(blob_lc) or "declined" in blob_lc or "not interested" in blob_lc:
            out["disposition"] = "Not Interested"
            return out
        if canon in ("Interested", "Site Visit") and not out.get("outcome_from_transcript"):
            out["disposition"] = "Answered"
            out["site_visit_agreed"] = False
            return out

    if canon in ("Answered", "", "Completed") or not out.get("disposition"):
        user_text = caller_text_from_transcript(tx) if tx else ""
        if action_type in ("site visit", "site_visit"):
            if _qa_site_visit_confirmed(out, tx or None):
                out["disposition"] = "Site Visit"
                out["site_visit_agreed"] = True
                return out
        elif action_type in ("virtual meet", "virtual_meet"):
            if user_text and len(user_text.strip()) >= 4:
                out["disposition"] = "Interested"
                out["site_visit_agreed"] = False
                return out
        elif action_type in ("whatsapp", "email"):
            if user_text and len(user_text.strip()) >= 4 and (
                not _emotion_blocks_interest(emotion) or _emotion_boosts_interest(emotion)
            ):
                if _infer(tx) or (soft_interest_in_text(user_text)):
                    out["disposition"] = "Interested"
                    return out
        elif action_type in ("call again", "call_again", "callback"):
            if (tx and user_requested_callback_in_transcript(tx)) or out.get("requested_callback_datetime_iso"):
                out["disposition"] = "Call Later"
                return out

    canon = canonical_disposition(out.get("disposition"))
    if canon in ("Interested", "Not Interested", "Call Later", "Busy", "Site Visit", "Voice Mail", "No Response"):
        if canon == "Site Visit":
            out["site_visit_agreed"] = True
        return out

    if blob_lc and len(blob_lc) >= 8:
        if _NEGATIVE.search(blob_lc):
            out["disposition"] = "Not Interested"
            return out
        if _qa_site_visit_confirmed(out, tx or None):
            out["disposition"] = "Site Visit"
            out["site_visit_agreed"] = True
            return out
        if soft_interest_in_text(blob) and not _emotion_blocks_interest(emotion):
            if tx and _infer(tx):
                out["disposition"] = "Interested"
                return out
        if (
            _emotion_boosts_interest(emotion)
            and tx
            and _infer(tx)
            and (
                "detail" in blob_lc or "brochure" in blob_lc or "pricing" in blob_lc or "budget" in blob_lc
            )
        ):
            out["disposition"] = "Interested"
            return out

    return out
