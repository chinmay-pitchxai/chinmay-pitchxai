"""Dashboard campaign JSON helpers: enrich leads & chart fields consumed by ``/api/campaign/state``."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from core.state import normalize_console_role


def _parse_analysis_blob(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def lead_is_called_console(lead: dict) -> bool:
    """Match frontend ``isCalled``: any sign the lead has been touched on the phone."""
    s = str(lead.get("status") or "").strip().lower()
    if s in ("failed", "error", "no answer", "no-answer", "busy"):
        return True
    if lead.get("start_time"):
        try:
            if float(lead.get("start_time")) > 0:
                return True
        except (TypeError, ValueError):
            pass
    log_id = str(lead.get("_log_id") or lead.get("log_id") or "").strip()
    if log_id:
        return True
    called_iso = str(lead.get("called_at_iso") or "").strip()
    return bool(called_iso)


def _has_site_visit_with_particular_date(lead: dict) -> bool:
    s = str(lead.get("status") or "").strip().lower()
    return s in ("site_visit", "site_visited")


def total_called_breakdown(leads_enriched: list[dict]) -> dict[str, int]:
    """Mirror frontend ``showStatPopup`` buckets for Total Called popup + KPI cards."""

    out: dict[str, int] = {
        "total_called": 0,
        "answered": 0,
        "plain_answered": 0,
        "interested": 0,
        "not_interested": 0,
        "callbacks": 0,
        "site_visit": 0,
        "no_response": 0,
        "voicemail": 0,
        "no_answer": 0,
        "busy": 0,
        "failed": 0,
    }
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        out["total_called"] += 1
        status = str(lead.get("status") or "").strip().lower()
        ed = effective_disposition_console(lead)
        el = ed.lower()

        if status in ("failed", "error") or el == "failed":
            out["no_response"] += 1
            continue
        if status in ("no answer", "no-answer") or el in ("no answer", "no-answer"):
            out["no_answer"] += 1
            continue
        if status == "busy" or el == "busy":
            out["busy"] += 1
            continue
        if el in ("voicemail", "voice mail") or "voicemail" in el or "voice mail" in el:
            out["voicemail"] += 1
            continue
        if status in ("no response", "no_response") or el in ("no response", "no_response") or "no response" in el or "no_response" in el:
            out["no_response"] += 1
            continue

        if _has_site_visit_with_particular_date(lead):
            out["site_visit"] += 1
        elif is_user_callback_lead(lead):
            out["callbacks"] += 1
        elif ed == "Not Interested" or "not interested" in el:
            out["not_interested"] += 1
        elif ed == "Interested" or ("interested" in el and "not interested" not in el):
            out["interested"] += 1
        else:
            out["plain_answered"] += 1

    out["answered"] = (
        out["plain_answered"]
        + out["interested"]
        + out["not_interested"]
        + out["callbacks"]
        + out["site_visit"]
    )
    return out


def effective_disposition_console(lead: dict) -> str:
    """Mirror ``effectiveDispo`` in ``frontend/static/js/api_utils.js``."""
    s = str(lead.get("status") or "").strip().lower()
    aj = _parse_analysis_blob(lead.get("analysis"))

    if s == "callback_scheduled":
        if aj.get("system_redial") or aj.get("failed_attempt_number"):
            return str(aj.get("disposition") or "No Answer")
        return "Callback Scheduled"
    if s == "callback_completed":
        return "Callback Completed"
    if s in ("site_visit", "site_visited"):
        return "Site Visited" if s == "site_visited" else "Site Visit"
    if s == "not_interested":
        return "Not Interested"
    if s in ("failed", "error"):
        return "No Response"
    if s == "busy":
        return "Busy"

    d = str(lead.get("disposition") or aj.get("disposition") or "").strip()
    dl = d.lower()
    if dl in ("voice mail", "voicemail"):
        return "Voice Mail"
    if s in ("no answer", "no-answer"):
        return "No Answer"
    if s in ("no response", "no_response"):
        return "No Response"

    if d and dl in ("site visit", "site_visit", "site visit scheduled"):
        if s in ("site_visit", "site_visited"):
            return "Site Visit"
    if d and d not in ("Answered", ""):
        if dl in ("voice mail", "voicemail"):
            return "Voice Mail"
        return d
    from config import settings as _settings

    _summary_first = (getattr(_settings, "outcome_mode", "summary_first") or "").strip().lower() == "summary_first"
    if _summary_first:
        _sd = str(aj.get("disposition") or "").strip()
        if _sd and _sd.lower() not in ("answered", ""):
            return _sd
    if aj.get("proof_verified") is True and _soft_interest_in_lead(lead, aj):
        return "Interested"
    if not d or d == "Answered":
        try:
            from services.transcript_interest import infer_outcome_from_qa_signals

            qa = infer_outcome_from_qa_signals(
                {
                    **aj,
                    "disposition": d,
                    "summary": lead.get("summary") or aj.get("summary"),
                    "next_steps": lead.get("next_steps") or aj.get("next_steps"),
                },
                None,
            )
            qd = str(qa.get("disposition") or "").strip()
            qdl = qd.lower()
            if qd and qdl not in ("answered", ""):
                if qdl in ("site visit", "site_visit") and s not in ("site_visit", "site_visited"):
                    if qa.get("site_visit_agreed"):
                        return "Site Visit"
                elif qd not in ("Answered",):
                    if qd == "Interested" and aj.get("proof_verified") is not True and not _summary_first:
                        pass
                    elif qd == "Site Visit" and aj.get("proof_verified") is not True and not _summary_first:
                        pass
                    else:
                        return qd
        except Exception:
            pass
    if d:
        return d
    status_map = {
        "not_interested": "Not Interested",
        "completed": "Answered",
        "failed": "No Response",
        "pending": "Pending",
        "dialing": "Dialing…",
        "no answer": "No Answer",
        "no response": "No Response",
    }
    return status_map.get(s, s[:1].upper() + s[1:] if s else "")


def _soft_interest_in_lead(lead: dict, aj: dict) -> bool:
    if aj.get("proof_verified") is not True:
        return False
    if aj.get("proof_block_reason"):
        return False
    if aj.get("transcript_thin"):
        return False
    # Only trust transcript-backed interest — never agent LLM next_steps/summary alone.
    return bool(aj.get("outcome_from_transcript"))


def _qa_text_blob_for_lead(lead: dict, aj: dict) -> str:
    parts = [
        str(lead.get("summary") or aj.get("summary") or ""),
        str(lead.get("next_steps") or aj.get("next_steps") or ""),
    ]
    na = aj.get("next_action") if isinstance(aj.get("next_action"), dict) else {}
    if na.get("details"):
        parts.append(str(na["details"]))
    return " ".join(p for p in parts if p).lower()


def is_user_callback_lead(lead: dict, aj: dict | None = None) -> bool:
    aj = aj or _parse_analysis_blob(lead.get("analysis"))
    if aj.get("system_redial") or aj.get("failed_attempt_number"):
        return False
    s = str(lead.get("status") or "").strip().lower()
    ed = effective_disposition_console(lead).lower()
    if ed in ("call later", "busy", "callback", "callback scheduled"):
        return True
    if s == "callback_scheduled" and (
        aj.get("requested_callback_datetime_iso") or aj.get("callback_reminder_epoch")
    ):
        disp = str(aj.get("disposition") or "").lower()
        if disp in ("call later", "busy", "callback", ""):
            return True
    return False


def _sqlite_row_ts_to_utc_date(txt: object) -> date | None:
    """Parse ``YYYY-MM-DD HH:MM:SS`` SQLite timestamps as UTC."""
    if txt is None or not str(txt).strip():
        return None
    s = str(txt).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError:
            continue
    return None


def _dashboard_tz() -> ZoneInfo:
    try:
        return ZoneInfo((settings.transcript_callback_tz or "Asia/Kolkata").strip() or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _sqlite_row_ts_to_ist_date(txt: object, tz: ZoneInfo) -> date | None:
    """SQLite ``updated_at`` style string → calendar date in ``tz`` (server rows are UTC wall time)."""

    if txt is None or not str(txt).strip():
        return None
    s = str(txt).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt_utc = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt_utc.astimezone(tz).date()
        except ValueError:
            continue
    return None


def _lead_anchor_dashboard_date(lead: dict, tz: ZoneInfo) -> date | None:
    """Calendar day in the dashboard TZ (IST by default) for timeline buckets."""

    try:
        st = lead.get("start_time")
        if st is not None:
            f = float(st)
            if f > 0:
                return datetime.fromtimestamp(f, tz=timezone.utc).astimezone(tz).date()
    except (TypeError, ValueError, OSError):
        pass

    iso = lead.get("called_at_iso")
    if isinstance(iso, str) and iso.strip():
        txt = iso.strip()
        try:
            if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt and len(txt) >= 16:
                txt = txt + "Z"
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            return dt.astimezone(tz).date()
        except (ValueError, TypeError):
            pass

    return _sqlite_row_ts_to_ist_date(lead.get("updated_at"), tz)


def _lead_anchor_utc_date(lead: dict) -> date | None:
    """UTC calendar day (legacy / tests). Prefer ``_lead_anchor_dashboard_date`` for charts."""

    try:
        st = lead.get("start_time")
        if st is not None:
            f = float(st)
            if f > 0:
                return datetime.fromtimestamp(f, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        pass

    iso = lead.get("called_at_iso")
    if isinstance(iso, str) and iso.strip():
        txt = iso.strip()
        try:
            if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt and len(txt) >= 16:
                txt = txt + "Z"
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).date()
        except (ValueError, TypeError):
            pass

    u = _sqlite_row_ts_to_utc_date(lead.get("updated_at"))
    return u


def _stored_name_looks_like_row_counter(name: str) -> bool:
    """Match Excel row indices stored as ``name`` (``11.0``, ``7``…) — wrong column mapping."""

    t = str(name or "").strip().replace(",", "").replace(" ", "")
    if not t:
        return False
    return bool(re.fullmatch(r"-?\d+(?:\.(?:0+|00+))?$", t))


# ── Salutation + first-name extraction ──────────────────────────────────────
# Common English / Indian salutations (case-insensitive, periods optional)
_SALUTATION_PATTERN = re.compile(
    r"^(Mr\.?|Mrs\.?|Ms\.?|Miss\.?|Dr\.?|Prof\.?|Er\.?|Eng\.?|"
    r"Sri\.?|Shri\.?|Smt\.?|Smt|Rev\.?|Col\.?|Maj\.?|Capt\.?|Adv\.?|CA\.?|CS\.?)[\s,]+",
    re.IGNORECASE,
)


def extract_salutation_and_first_name(full_name: str) -> tuple[str, str]:
    """Return ``(salutation, first_name)`` from a full name string.

    - ``salutation`` is the honorific prefix, e.g. ``"Dr."`` or ``""`` if absent.
    - ``first_name``  is the first meaningful given-name token (after the salutation).

    Examples::

        "Dr. Ramesh Kumar Mehta"  → ("Dr.", "Ramesh")
        "Mr John Smith"           → ("Mr.", "John")
        "Anjali Sharma"          → ("",    "Anjali")
        "ANITHA"                  → ("",    "Anitha")   # single-word names are title-cased
    """
    name = (full_name or "").strip()
    if not name:
        return "", ""

    # Try to match a leading salutation
    m = _SALUTATION_PATTERN.match(name)
    salutation = ""
    remainder = name
    if m:
        raw_sal = m.group(1).strip().rstrip(".")
        # Normalise to title-case + period
        salutation = raw_sal.capitalize() + "."
        remainder = name[m.end():].strip()

    # First token of whatever is left is the first name
    tokens = remainder.split()
    first_name = tokens[0].capitalize() if tokens else ""
    return salutation, first_name


def addressable_name(full_name: str) -> str:
    """Return the greeting-ready short name: ``"Dr. Ramesh"`` or ``"Anjali"``."""
    salutation, first = extract_salutation_and_first_name(full_name)
    if salutation and first:
        return f"{salutation} {first}"
    return first or full_name.strip()


def enrich_lead_for_console(lead: dict, *, skip_recording_probe: bool = False) -> dict:
    """Expose ``disposition``, ``summary``, ``rating``, ``called_at_iso`` for dashboard rows & charts."""
    out = dict(lead)
    aj = _parse_analysis_blob(out.get("analysis"))
    log_id_raw = str(out.get("_log_id") or out.get("log_id") or "").strip()
    if not log_id_raw:
        try:
            from core.storage import resolve_lead_session_log_id_sync

            log_id_raw = resolve_lead_session_log_id_sync(
                str(out.get("role") or ""),
                int(out["id"]) if out.get("id") is not None else None,
                str(out.get("phone") or ""),
            )
            if log_id_raw:
                out["log_id"] = log_id_raw
                out["_log_id"] = log_id_raw
        except Exception:
            pass
    if log_id_raw:
        out["log_id"] = log_id_raw
        if skip_recording_probe:
            out["recording_available"] = True
            role_key = normalize_console_role(str(out.get("role") or "sales_1"))
            out["recording_url"] = (
                f"/api/campaign/lead/{out['id']}/recording?role={role_key}"
                f"&log_id={log_id_raw}"
            )
        else:
            try:
                from services.call_recording import resolve_dashboard_recording_path

                rp = resolve_dashboard_recording_path(log_id_raw)
                out["recording_available"] = bool(rp and rp.is_file())
                out["recording_pending"] = bool(log_id_raw) and not out["recording_available"]
                if out["recording_available"]:
                    role_key = normalize_console_role(str(out.get("role") or "sales_1"))
                    out["recording_url"] = (
                        f"/api/campaign/lead/{out['id']}/recording?role={role_key}"
                        f"&log_id={log_id_raw}"
                    )
                try:
                    from services.call_recording import recording_duration_sec

                    rec_dur = recording_duration_sec(log_id_raw)
                    if rec_dur is not None and rec_dur > 0:
                        out["recording_duration_sec"] = round(rec_dur, 1)
                        if aj.get("duration") is None:
                            out["duration"] = round(rec_dur, 1)
                except Exception:
                    pass
            except Exception:
                out["recording_available"] = False
                out["recording_pending"] = bool(log_id_raw)
    else:
        out["recording_available"] = False
        out["recording_pending"] = False
    disp = (
        str(out.get("disposition") or aj.get("disposition") or "").strip()
    )
    out["disposition"] = disp
    if "summary" not in out or not out["summary"]:
        out["summary"] = str(aj.get("summary") or "")
    if aj.get("rating") is not None:
        try:
            out["rating"] = int(aj.get("rating"))
        except (ValueError, TypeError):
            out["rating"] = 0

    # Transcript quality badge (good | thin | unreliable | transcript_only)
    if aj.get("transcript_source"):
        out["transcript_source"] = str(aj.get("transcript_source") or "")
    if aj.get("transcript_unreliable"):
        out["transcript_unreliable"] = str(aj.get("transcript_unreliable") or "")
        out["transcript_quality"] = "unreliable"
    elif out.get("recording_available") is False and (
        aj.get("outcome_from_transcript") or aj.get("proof_verified")
    ):
        out["transcript_quality"] = "transcript_only"
    elif aj.get("proof_verified") is True or aj.get("outcome_from_transcript"):
        src = str(aj.get("transcript_source") or "").lower()
        out["transcript_quality"] = "thin" if ("short" in src or src in ("empty",)) else "good"
    elif aj.get("transcript_source"):
        src = str(aj.get("transcript_source") or "").lower()
        out["transcript_quality"] = "thin" if "short" in src else "good"

    ns = aj.get("next_steps")
    if ns is not None and not str(out.get("next_steps") or "").strip():
        out["next_steps"] = ns if isinstance(ns, str) else "; ".join(str(x) for x in ns)

    na = aj.get("next_action")
    if na is not None:
        out["next_action"] = na

    if aj.get("emotion_label") and not out.get("emotion_label"):
        out["emotion_label"] = str(aj.get("emotion_label") or "").strip()
    if aj.get("emotion_rationale") and not out.get("emotion_rationale"):
        out["emotion_rationale"] = str(aj.get("emotion_rationale") or "").strip()
    if aj.get("emotion_confidence") is not None and out.get("emotion_confidence") is None:
        try:
            out["emotion_confidence"] = float(aj.get("emotion_confidence"))
        except (TypeError, ValueError):
            pass

    if aj.get("preferred_location") and not out.get("preferred_location"):
        out["preferred_location"] = str(aj.get("preferred_location") or "").strip()
    if aj.get("preferred_budget") and not out.get("preferred_budget"):
        out["preferred_budget"] = str(aj.get("preferred_budget") or "").strip()
    # If customer provided email during call and lead has no email from CSV, use the transcript-extracted one
    extracted_email = (aj.get("email_address") or "").strip()
    if extracted_email and "@" in extracted_email and not (out.get("email") or "").strip():
        out["email"] = extracted_email

    if out.get("start_time") and not out.get("called_at_iso"):
        try:
            st = float(out["start_time"])
            out["called_at_iso"] = datetime.utcfromtimestamp(st).replace(tzinfo=None).isoformat() + "Z"
        except (ValueError, TypeError, OSError):
            pass
    # Post-call QA from transcript flags (manifest / tooling)
    if aj.get("outcome_from_transcript"):
        out["outcome_from_transcript"] = bool(aj["outcome_from_transcript"])

    # Site visit agreed flag — extracted from analysis for frontend filters
    if aj.get("site_visit_agreed") is not None:
        out["site_visit_agreed"] = bool(aj["site_visit_agreed"])

    # Requested callback datetime — extracted for frontend filters
    if aj.get("requested_callback_datetime_iso") is not None:
        out["requested_callback_datetime_iso"] = str(aj["requested_callback_datetime_iso"])

    cre = aj.get("callback_reminder_epoch")
    if cre is not None:
        try:
            out["callback_reminder_at_iso"] = (
                datetime.fromtimestamp(float(cre), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            out["next_retake_at_iso"] = out["callback_reminder_at_iso"]
        except (TypeError, ValueError, OSError):
            pass

    # Failed-call retry display (3 attempts, 24h apart)
    extra_raw = out.get("extra") or {}
    if isinstance(extra_raw, str):
        try:
            extra_raw = json.loads(extra_raw)
        except json.JSONDecodeError:
            extra_raw = {}
    if not isinstance(extra_raw, dict):
        extra_raw = {}
    retries = int(extra_raw.get("failed_call_retries") or 0)
    max_att = max(1, int(settings.failed_call_max_attempts))
    attempt_num = min(retries + 1, max_att)
    out["failed_attempt_number"] = attempt_num
    out["failed_max_attempts"] = max_att
    sb = int(out.get("sandbox") or 1)
    if retries > 0:
        # Only Sandbox 2+ retries carry a retake number. Attempt 2 -> "Retake 2",
        # attempt 3 -> "Retake 3". Sandbox 1 is always the original call.
        out["retake_label"] = f"Retake {attempt_num}"
    else:
        out["retake_label"] = "Original call"
    orig_ts = extra_raw.get("original_called_at") or out.get("first_called_at") or out.get("start_time")
    if orig_ts:
        try:
            out["original_called_at_iso"] = (
                datetime.fromtimestamp(float(orig_ts), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError, OSError):
            pass
    if extra_raw.get("inbound_interest") or extra_raw.get("inbound_callback"):
        out["inbound_interest"] = True
        out["inbound_reply_type"] = (
            extra_raw.get("inbound_reply_type")
            or ("callback" if extra_raw.get("inbound_callback") else "interested")
        )
        out["inbound_interest_source"] = extra_raw.get("inbound_interest_source") or ""
        out["inbound_interest_at"] = (
            extra_raw.get("inbound_reply_at")
            or extra_raw.get("inbound_interest_at")
            or extra_raw.get("inbound_callback_at")
            or ""
        )
        out["inbound_interest_message"] = (
            extra_raw.get("inbound_reply_message")
            or extra_raw.get("inbound_interest_message")
            or ""
        )

    # Site visit lifecycle + follow-up plan (mirrors retake numbering)
    if extra_raw.get("lifecycle_stage"):
        out["lifecycle_stage"] = str(extra_raw.get("lifecycle_stage") or "")
    if extra_raw.get("site_visit_datetime_iso"):
        out["site_visit_datetime_iso"] = str(extra_raw.get("site_visit_datetime_iso") or "")
    if extra_raw.get("site_visit_headcount") is not None:
        out["site_visit_headcount"] = extra_raw.get("site_visit_headcount")
    if extra_raw.get("site_visit_arrival_time_iso"):
        out["site_visit_arrival_time_iso"] = str(extra_raw.get("site_visit_arrival_time_iso") or "")
    fu_plan = extra_raw.get("follow_up_plan")
    if isinstance(fu_plan, list) and fu_plan:
        out["follow_up_plan"] = fu_plan
        pending = [e for e in fu_plan if isinstance(e, dict) and e.get("status") == "scheduled"]
        if pending:
            out["follow_up_pending_label"] = pending[0].get("label") or f"Follow-up {pending[0].get('follow_up_number')}"
        out["follow_up_count"] = len(fu_plan)
    if extra_raw.get("best_attempt_number"):
        out["best_attempt_number"] = int(extra_raw.get("best_attempt_number") or 1)
    if aj.get("proof_verified") is not None:
        out["proof_verified"] = bool(aj.get("proof_verified"))

    status_lc = str(out.get("status") or "").lower()
    is_failed_like = status_lc in (
        "failed", "error", "no answer", "no response", "no_response", "busy",
    ) or str(out.get("disposition") or "").lower() in (
        "failed", "no answer", "busy", "no response",
    )
    if is_failed_like and retries < max_att - 1 and out.get("next_retake_at_iso"):
        out["failure_title"] = f"Attempt {attempt_num}/{max_att}"
        out["failure_detail"] = f"Next retake scheduled"
        out["failure_severity"] = "warning"
    elif is_failed_like and retries >= max_att - 1:
        out["failure_title"] = f"All {max_att} attempts used"
        out["failure_detail"] = out.get("error") or "No more automatic retries"
        out["failure_severity"] = "error"

    nm_raw = str(out.get("name") or "").strip()
    co_raw = str(out.get("company") or "").strip()
    co_lines = [x.strip() for x in co_raw.splitlines() if str(x).strip()]
    if (_stored_name_looks_like_row_counter(nm_raw) or not nm_raw or nm_raw.lower() == "unknown") and co_lines:
        out["contact_display_primary"] = co_lines[0]
        tail = co_lines[1:]
        out["contact_display_secondary"] = " · ".join(tail) if tail else ""
    elif nm_raw and nm_raw.lower() != "unknown":
        out["contact_display_primary"] = nm_raw
        out["contact_display_secondary"] = co_raw
    else:
        out["contact_display_primary"] = "Unknown"
        out["contact_display_secondary"] = co_raw
    role_key = normalize_console_role(str(out.get("role") or "sales_1"))
    lid = out.get("id")
    log_ref = log_id_raw or str(out.get("log_id") or "").strip()
    if lid is not None and log_ref:
        out["transcript_url"] = f"/api/campaign/lead/{lid}/transcript?role={role_key}&log_id={log_ref}"
        out["recording_url"] = f"/api/campaign/lead/{lid}/recording?role={role_key}&log_id={log_ref}"

    # ── Salutation + first name extraction ──────────────────────────────────
    raw_name_for_addr = nm_raw or co_lines[0] if co_lines else ""
    sal, fn = extract_salutation_and_first_name(raw_name_for_addr)
    out["salutation"] = sal          # e.g. "Dr." or ""
    out["first_name"] = fn           # e.g. "Ramesh" or ""
    out["addressable_name"] = addressable_name(raw_name_for_addr)  # e.g. "Dr. Ramesh"

    # Align manifest/filter disposition with soft-interest rules (email / send details / will check).
    effective = effective_disposition_console(out)
    if effective:
        out["disposition"] = effective
    return out


# Fields safe to send to the browser (omit multi-KB ``analysis`` blobs).
_SLIM_LEAD_KEYS = (
    "id",
    "role",
    "name",
    "phone",
    "email",
    "company",
    "status",
    "disposition",
    "sandbox",
    "source_file",
    "segment",
    "summary",
    "rating",
    "start_time",
    "called_at_iso",
    "_log_id",
    "log_id",
    "recording_available",
    "recording_url",
    "recording_duration_sec",
    "duration",
    "transcript_url",
    "transcript_source",
    "transcript_unreliable",
    "transcript_quality",
    "outcome_from_transcript",
    "next_steps",
    "next_action",
    "emotion_label",
    "emotion_rationale",
    "emotion_confidence",
    "failure_title",
    "failure_detail",
    "failure_reason",
    "failure_severity",
    "contact_display_primary",
    "contact_display_secondary",
    "callback_reminder_at_iso",
    "next_retake_at_iso",
    "original_called_at_iso",
    "failed_attempt_number",
    "failed_max_attempts",
    "retake_label",
    "inbound_interest",
    "inbound_interest_source",
    "inbound_interest_at",
    "inbound_interest_message",
    "inbound_reply_type",
    "first_called_at",
    "error",
    "extra",
    "preferred_location",
    "preferred_budget",
    "site_visit_agreed",
    "requested_callback_datetime_iso",
    "lifecycle_stage",
    "site_visit_datetime_iso",
    "site_visit_headcount",
    "site_visit_arrival_time_iso",
    "follow_up_plan",
    "follow_up_pending_label",
    "follow_up_count",
    "best_attempt_number",
    "proof_verified",
    "salutation",
    "first_name",
    "addressable_name",
    "created_at",
    "whatsapp_sent",
    "email_sent",
    "outbound_phone",
)



def slim_lead_for_api(lead: dict, *, role: str | None = None, skip_recording_probe: bool = False) -> dict:
    """Enrich then drop heavy columns so ``/state`` and ``/manifest`` stay small and reliable."""

    enriched = enrich_lead_for_console(dict(lead), skip_recording_probe=skip_recording_probe)
    role_key = normalize_console_role(role or enriched.get("role") or "sales_1")
    out: dict[str, Any] = {}
    for key in _SLIM_LEAD_KEYS:
        if key in enriched and enriched[key] is not None:
            out[key] = enriched[key]
    out["id"] = enriched.get("id")
    out["role"] = role_key
    if enriched.get("_log_id"):
        out["_log_id"] = enriched["_log_id"]
    if enriched.get("log_id"):
        out["log_id"] = enriched["log_id"]
    lid = enriched.get("id")
    log_ref = str(enriched.get("log_id") or enriched.get("_log_id") or "").strip()
    if lid is not None and log_ref:
        out["transcript_url"] = f"/api/campaign/lead/{lid}/transcript?role={role_key}&log_id={log_ref}"
        if enriched.get("recording_available"):
            out["recording_url"] = f"/api/campaign/lead/{lid}/recording?role={role_key}&log_id={log_ref}"
        elif log_ref:
            out["recording_url"] = f"/api/campaign/lead/{lid}/recording?role={role_key}&log_id={log_ref}"
    return out


def disposition_counts_for_dashboard(leads_enriched: list[dict]) -> dict[str, int]:
    """Bucket QA dispositions for Outcome Distribution (all outbound-touched leads)."""

    keys = (
        "Interested",
        "Not Interested",
        "Call Later",
        "Busy",
        "Callback",
        "Answered",
        "Failed",
        "Voice Mail",   # for charts.js  (dc['Voice Mail'])
        "Voicemail",    # for app.js     (dc['Voicemail'])
        "No Response",
        "Site Visit",
    )
    buckets: dict[str, int] = {k: 0 for k in keys}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        status = str(lead.get("status") or "").strip().lower()
        ed = effective_disposition_console(lead)
        el = ed.lower()

        # ── Voicemail: must be checked BEFORE the generic is_failed block ──
        is_voicemail = (
            ed in ("Voice Mail", "Voicemail") or
            el in ("voice mail", "voicemail")
        )
        if is_voicemail:
            buckets["Voice Mail"] += 1
            buckets["Voicemail"] += 1
            continue

        # Exact alignment with frontend isFailed(lead) — voicemail already handled above
        is_failed = (
            status in ("failed", "error", "no answer", "busy", "no response", "no_response") or
            ed in ("Failed", "No Answer", "Busy", "Wrong Number", "Not Available", "No Response", "Voicemail", "Voice Mail") or
            el in ("failed", "no answer", "busy", "wrong number", "not available", "no response", "no_response", "voicemail", "voice mail")
        )

        if is_failed:
            buckets["Failed"] += 1
            continue

        if status == "not_interested":
            buckets["Not Interested"] += 1
            continue

        if status in ("site_visit", "site_visited") or ed == "Site Visit" or (
            "site visit" in el and "not interested" not in el
        ):
            buckets["Site Visit"] += 1
            continue

        if ed == "Interested" or ("interested" in el and "not interested" not in el):
            buckets["Interested"] += 1
        elif ed == "Not Interested" or "not interested" in el:
            buckets["Not Interested"] += 1
        elif ed == "Call Later":
            buckets["Call Later"] += 1
        elif ed == "Busy":
            buckets["Busy"] += 1
        elif ed == "Callback":
            buckets["Callback"] += 1
        elif status == "completed":
            buckets["Answered"] += 1
        else:
            buckets["Answered"] += 1
    return buckets



def progress_counts_for_dashboard(leads_enriched: list[dict]) -> dict[str, int]:
    """Status breakdown for Campaign Progress bar (full outbound cohort)."""
    out = {"connected": 0, "failed": 0, "no_answer": 0, "pending": 0, "other": 0}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        s = str(lead.get("status") or "").strip().lower()
        if s == "completed":
            out["connected"] += 1
        elif s in ("failed", "error"):
            out["failed"] += 1
        elif s in ("no answer", "busy"):
            out["no_answer"] += 1
        elif s in ("pending", "dialing", ""):
            out["pending"] += 1
        else:
            out["other"] += 1
    return out


def weekday_counts_for_dashboard(
    leads_enriched: list[dict], tz: ZoneInfo
) -> list[int]:
    """Calls by weekday (Mon=0 … Sun=6) in dashboard TZ — matches frontend chartWeekday."""
    counts = [0] * 7
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        day_anchor = _lead_anchor_dashboard_date(lead, tz)
        if day_anchor is None:
            continue
        counts[day_anchor.weekday()] += 1
    return counts


def campaign_called_count(leads_enriched: list[dict]) -> int:
    return sum(1 for l in leads_enriched if lead_is_called_console(l))


def hourly_counts_for_dashboard(leads_enriched: list[dict], tz: ZoneInfo) -> list[int]:
    """Calls by hour of day (0-23) in dashboard TZ — for the Hourly Distribution chart."""
    counts = [0] * 24
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        try:
            st = lead.get("start_time")
            if st is not None:
                f = float(st)
                if f > 0:
                    hour = datetime.fromtimestamp(f, tz=timezone.utc).astimezone(tz).hour
                    counts[hour] += 1
                    continue
        except (TypeError, ValueError, OSError):
            pass
        iso = lead.get("called_at_iso")
        if isinstance(iso, str) and iso.strip():
            try:
                txt = iso.strip()
                if not txt.endswith("Z") and "+" not in txt[-6:] and "T" in txt:
                    txt = txt + "Z"
                dt = datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(tz)
                counts[dt.hour] += 1
            except (ValueError, TypeError):
                pass
    return counts


_DAYS_JS_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _js_weekday_label(d: date) -> str:
    """Local calendar date ``d`` → same short label as JS ``days[d.getDay()]``."""
    weekday_py = d.weekday()  # Monday=0 .. Sunday=6
    js_get_day = (weekday_py + 1) % 7  # JS: Sunday=0
    return _DAYS_JS_LABELS[js_get_day]


def last_seven_dashboard_axis() -> tuple[list[str], list[date]]:
    """Rolling 7 calendar days (oldest → newest) in ``TRANSCRIPT_CALLBACK_TZ`` — default IST."""

    tz = _dashboard_tz()
    today = datetime.now(tz).date()
    dates = [today - timedelta(days=(6 - i)) for i in range(7)]
    labels = [_js_weekday_label(day) for day in dates]
    return labels, dates


def build_dashboard_timelines(
    leads_enriched: list[dict], dates: list[date], tz: ZoneInfo
) -> tuple[list[int], list[int]]:
    """Per‑day outbound counts aligned with ``dates`` (dashboard TZ calendar days)."""

    totals = [0] * len(dates)
    interested = [0] * len(dates)
    idx = {day: i for i, day in enumerate(dates)}
    for lead in leads_enriched:
        if not lead_is_called_console(lead):
            continue
        day_anchor = _lead_anchor_dashboard_date(lead, tz)
        if day_anchor is None or day_anchor not in idx:
            continue
        i = idx[day_anchor]
        totals[i] += 1
        if effective_disposition_console(lead) == "Interested":
            interested[i] += 1
    return totals, interested


def build_campaign_state_dashboard_fields(role: str, leads: list[dict]) -> dict[str, Any]:
    """Chart payload + enriched leads (parsed analysis surfaced for dashboard rows)."""

    role = normalize_console_role(role)
    enriched = [enrich_lead_for_console(dict(l)) for l in leads]
    tz = _dashboard_tz()
    labels, dates = last_seven_dashboard_axis()

    from core.storage import (
        _get_leads_with_outbound_activity_sync,
        call_attempts_timeline_sync,
        count_call_attempts_sync,
    )

    outbound_rows = _get_leads_with_outbound_activity_sync(role)
    enriched_for_timeline = [enrich_lead_for_console(dict(l)) for l in outbound_rows]

    date_keys = [d.isoformat() for d in dates]

    cb_by_label: dict[str, int] = {lab: 0 for lab in labels}

    ttl, tins = build_dashboard_timelines(enriched_for_timeline, dates, tz)
    # Prefer attempt-based timeline (includes retries) when call_attempts has data
    try:
        attempt_timeline = call_attempts_timeline_sync(
            role, dates, tz_name=(settings.transcript_callback_tz or "Asia/Kolkata")
        )
        if sum(attempt_timeline) > sum(ttl):
            ttl = attempt_timeline
    except Exception:
        pass
    disposition = disposition_counts_for_dashboard(enriched_for_timeline)
    called_total = campaign_called_count(enriched_for_timeline)
    called_breakdown = total_called_breakdown(enriched_for_timeline)
    try:
        total_attempts = count_call_attempts_sync(role)
    except Exception:
        total_attempts = called_total

    # Primary "Total Calls" KPI = all dials (retries included) when available
    display_total = total_attempts if total_attempts > called_total else called_total
    if called_breakdown.get("total_called") is not None:
        called_breakdown = dict(called_breakdown)
        called_breakdown["total_called"] = display_total
        called_breakdown["unique_leads_called"] = called_total

    return {
        "called_count": display_total,
        "unique_leads_called": called_total,
        "total_call_attempts": total_attempts,
        "called_breakdown": called_breakdown,
        "disposition_counts": disposition,
        "callback_counts_by_date": cb_by_label,
        "timeline_dates_iso": date_keys,
        "timeline_total_calls": ttl,
        "timeline_total_attempts": list(ttl),
        "timeline_interested": tins,
        "timeline_week_labels": labels,
        "progress_counts": progress_counts_for_dashboard(enriched_for_timeline),
        "weekday_counts": weekday_counts_for_dashboard(enriched_for_timeline, tz),
        "hourly_counts": hourly_counts_for_dashboard(enriched_for_timeline, tz),
        "chart_interested_total": int(disposition.get("Interested") or 0),
        "leads_enriched": enriched,
    }

