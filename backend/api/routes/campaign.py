"""Campaign management routes — SQLite-backed, production-ready."""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import re
import secrets
from datetime import timezone
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from core.utils import range_file_response
from loguru import logger
from pydantic import BaseModel, Field

from config import settings
from core import storage as lead_storage
from core.state import (
    save_role_state, add_leads_bulk,
    update_lead_status, reset_leads,
    export_leads_csv, _CAMPAIGN_TASKS, total_active_vobiz_calls,
    normalize_console_role,
    _ROLES,
    get_state,
)
from core.campaign_payload import (
    build_campaign_state_dashboard_fields,
    enrich_lead_for_console,
    slim_lead_for_api,
)
from core.campaign_hours import get_campaign_hours_status
from core.worker import (
    _campaign_worker_role,
    _analyze_and_update_lead,
    inter_call_gap_seconds_for_role,
    inter_call_gap_display_seconds_for_role,
    _read_transcript_jsonl,
    release_orphaned_dialing_leads,
)

from core.phone_norm import norm_phone_str as _norm_phone_str
from services.call_recording import (
    fetch_vobiz_recording_if_missing,
    resolve_dashboard_recording_path,
)
from services.excel_report import get_report_kpi_summary
from core.outbound_numbers import get_all_outbound_numbers
from core import kv_cache

router = APIRouter(prefix="/api/campaign", tags=["campaign"])


class DigitalLeadWebhookRow(BaseModel):
    name: str = "Unknown"
    phone: str = ""
    email: str = ""
    source: str = ""
    notes: str = ""
    row_id: str = ""


class DigitalLeadWebhookPayload(BaseModel):
    broker_id: str = Field(pattern=r"^broker_[123]$")
    rows: list[DigitalLeadWebhookRow] = Field(min_length=1, max_length=500)


async def _ensure_digital_p3_dispatcher() -> dict:
    """Start only the P3-aware orchestrator; never fall back to cold dialing."""
    if settings.webhook_only_mode:
        return {"auto_started": False, "blocked": "webhook_only_mode"}
    from core.orchestration_runtime import ensure_orchestration_running, runtime_status

    status = runtime_status()
    if status["mode"] != "live":
        return {
            "auto_started": False,
            "blocked": "p3_orchestration_not_live",
            "configuration_errors": status["configuration_errors"],
        }
    respawned, detail = await ensure_orchestration_running()
    return {
        "auto_started": True,
        "engine": "orchestration",
        "phone_line": "P3",
        "respawned": respawned,
        "detail": detail,
    }


@router.post("/digital-leads/webhook")
async def digital_leads_webhook(payload: DigitalLeadWebhookPayload, request: Request):
    """Receive broker Sheet rows, deduplicate them, and queue P3 calls immediately."""
    configured_secret = settings.digital_leads_webhook_secret
    supplied_secret = (request.headers.get("X-Digital-Leads-Secret") or "").strip()
    if not configured_secret or not secrets.compare_digest(supplied_secret, configured_secret):
        raise HTTPException(status_code=401, detail="Invalid digital-leads webhook secret")

    from services.digital_excel_ingest import ingest_digital_rows

    result = await asyncio.to_thread(
        ingest_digital_rows,
        [row.model_dump() for row in payload.rows],
        broker_id=payload.broker_id,
    )
    result["worker"] = await _ensure_digital_p3_dispatcher() if result["queued"] else {"auto_started": False}
    if result["queued"] and not result["worker"].get("auto_started"):
        for row_result in result["results"]:
            if row_result["status"] == "queued":
                row_result["status"] = "queued_waiting_for_dialer"
    return result


@router.get("/digital-feed-status")
async def digital_feed_status():
    """Operator-visible readiness for the Sandbox 1.2 Excel watcher."""
    configured_path = Path(settings.digital_excel_path).expanduser() if settings.digital_excel_path else None
    broker_sheets = [
        {"broker_id": "broker_1", "url": settings.digital_broker_1_sheet_url},
        {"broker_id": "broker_2", "url": settings.digital_broker_2_sheet_url},
        {"broker_id": "broker_3", "url": settings.digital_broker_3_sheet_url},
    ]
    from core.orchestration_runtime import runtime_status

    orchestration = runtime_status()
    blockers = []
    if not settings.digital_leads_webhook_secret:
        blockers.append("DIGITAL_LEADS_WEBHOOK_SECRET is not configured")
    if settings.webhook_only_mode:
        blockers.append("WEBHOOK_ONLY_MODE disables outbound dialing on this host")
    if orchestration["mode"] != "live":
        blockers.extend(orchestration["configuration_errors"] or ["ORCHESTRATION_LIVE_ENABLED is false"])
    return {
        "enabled": bool(configured_path),
        "path": str(configured_path) if configured_path else "",
        "exists": bool(configured_path and configured_path.exists()),
        "kind": "directory" if configured_path and configured_path.is_dir() else "file",
        "sheet": settings.digital_excel_sheet,
        "poll_seconds": settings.digital_excel_poll_seconds,
        "role": settings.digital_excel_role,
        "sub_sandbox": "1.2",
        "eligible_pool": "sandbox1_digital",
        "phone_lines": ["P3"],
        "webhook_configured": bool(settings.digital_leads_webhook_secret),
        "realtime_ready": not blockers,
        "realtime_blockers": blockers,
        "orchestration_mode": orchestration["mode"],
        "broker_sheets": broker_sheets,
        "connected_brokers": sum(bool(item["url"]) for item in broker_sheets),
    }


async def _try_start_campaign_worker(role: str) -> dict:
    """Start dialer for role after upload. Returns status dict (never raises)."""
    if settings.orchestration_enforce_consent:
        from core.state import get_campaign_config
        if not get_campaign_config(role).get("consent_confirmed"):
            return {"auto_started": False, "blocked": "consent_not_confirmed"}
    from core.orchestration_runtime import ensure_orchestration_running, runtime_status

    if runtime_status()["mode"] == "live":
        # Autonomous orchestration owns all outbound dialing (P1-P9); the
        # source-blind legacy worker must NOT start here (double-dial + cold-line
        # routing of digital leads). Ensure the orchestrator supervisor is alive.
        await ensure_orchestration_running()
        return {
            "auto_started": True,
            "engine": "orchestration",
            "campaign_status": "orchestration_live",
            "pending": 0,
        }
    try:
        run = _CAMPAIGN_TASKS.get(role)
        if run and not run.done():
            c = await lead_storage.get_lead_counts(role)
            return {
                "auto_started": True,
                "campaign_status": "already_running",
                "pending": c.get("pending", 0),
            }
        from core.worker import _schedule_preflight

        await lead_storage.set_campaign_globally_paused(False)
        err = await _schedule_preflight(role)
        if err:
            return {"auto_started": False, "campaign_status": "blocked", "detail": err}
        from core.state import _MANUALLY_STOPPED_ROLES
        _MANUALLY_STOPPED_ROLES.discard(role)
        await lead_storage.set_campaign_want_running(role, True)
        _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
        c = await lead_storage.get_lead_counts(role)
        try:
            from core.events import get_event_bus
            await get_event_bus().publish("lead_updated", role=role, lead_id=None)
        except Exception:
            pass
        return {
            "auto_started": True,
            "campaign_status": "started",
            "pending": c.get("pending", 0),
        }
    except Exception as exc:
        logger.error(f"Auto-start after upload failed for {role}: {exc}")
        return {"auto_started": False, "campaign_status": "error", "detail": str(exc)}


def _jwt_payload_from_request(request: Request) -> dict | None:
    """Bearer header or ``access_token`` / ``token`` query (for ``<audio src>`` playback)."""
    from core.auth import _decode_jwt

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = _decode_jwt(auth[7:])
        if payload:
            return payload
    for key in ("access_token", "token"):
        raw = (request.query_params.get(key) or "").strip()
        if raw:
            payload = _decode_jwt(raw)
            if payload:
                return payload
    return None


def _campaign_role(request: Request) -> str:
    """Resolve role from query param first, then JWT, then default."""
    from core.state import normalize_console_role
    
    # Check query parameter first (frontend sends ?role=sales_1)
    role_param = request.query_params.get("role", "").strip()
    if role_param:
        normalized = normalize_console_role(role_param)
        return normalized
    
    # Fall back to JWT
    from core.auth import console_role_from_request
    return console_role_from_request(request, default="sales_1")


def _sanitize_tabular_rows(rows: list[dict]) -> list[dict]:
    """Normalize CSV/XLS headers: strip BOM, trim keys and string cell values."""
    fixed: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        nr: dict = {}
        for k, v in r.items():
            nk = str(k).replace("\ufeff", "").strip() if k is not None else ""
            if not nk:
                nk = str(k)
            nv = "" if v is None else str(v).strip()
            nr[nk] = nv
        fixed.append(nr)
    return fixed


def _extract_phone_cell(row: dict, phone_hint: str | None, norm_phone) -> str:
    """Use auto-detected phone column first, then scan every cell for a dialable number."""
    keys = list(row.keys())
    order: list[str] = []
    if phone_hint:
        h = str(phone_hint).strip()
        if h in keys:
            order.append(h)
    order.extend(k for k in keys if k not in order)
    for k in order:
        cand = norm_phone(str(row.get(k, "") or "").strip())
        if cand:
            return cand
    return ""


def _looks_like_row_index_header(col: str) -> bool:
    """Headers such as ``S.No``, ``#``, ``ID`` — not person's name / company."""

    raw = str(col or "").strip()
    if not raw:
        return False
    hn = re.sub(r"[^\w+#]+", " ", raw.strip().lower()).strip().replace(".", "").replace("_", "")
    if not hn.replace("#", ""):
        return True
    if any(tok in hn for tok in ("name", "fullname", "first name", "person", "contact name", "customer name", "lead name")):
        return False
    compact = hn.replace(" ", "")
    needles = ("sno", "slno", "serialno", "linenumber", "lineno", "rownum", "rownumber")
    if any(n in compact for n in needles):
        return True
    if hn in {"id", "#", "sn", "sl", "index", "rank", "serial", "row"} or hn.endswith(" id"):
        return True
    if hn.startswith("unnamed"):
        return True
    if hn.startswith("col") and len(hn) > 3 and hn[3:].isdigit():
        return True
    return False


def _column_values_mostly_row_numbers(values: list[str], threshold: float = 0.7) -> bool:
    """True when cells look like spreadsheet row counters (``11.0``, ``10``…) not people."""

    nonempty: list[str] = []
    for v in values:
        t = str(v or "").strip().replace(",", "").replace(" ", "")
        if t:
            nonempty.append(t)
    if len(nonempty) < 3:
        return False
    pat = re.compile(r"^-?\d+(?:\.(?:0+|00+))?$")
    hits = sum(1 for t in nonempty if pat.fullmatch(t))
    return hits / len(nonempty) >= threshold


@router.get("/sources")
async def campaign_sources(request: Request):
    """List all upload sources for this role with lead counts and pause status.
    
    Optionally filter by sandbox number via ?sandbox= query parameter.
    """
    role = _campaign_role(request)
    sandbox = request.query_params.get("sandbox")
    paused_sources = await lead_storage.get_paused_sources(role)

    sources = await lead_storage.get_campaign_sources(role, paused_sources)
    
    # Filter by sandbox if specified
    if sandbox:
        try:
            sb = int(sandbox)
            sources = [s for s in sources if s.get("sandbox") == sb]
        except (ValueError, TypeError):
            pass
    
    return {"sources": sources, "paused_sources": paused_sources}


@router.post("/sources/toggle")
async def campaign_source_toggle(request: Request):
    """Toggle pause/play for a specific upload source."""
    role = _campaign_role(request)
    body = await request.json()
    source_name = body.get("source", "")
    if not source_name:
        raise HTTPException(status_code=400, detail="Missing 'source' field")

    paused_sources = await lead_storage.get_paused_sources(role)

    if source_name in paused_sources:
        paused_sources.remove(source_name)
    else:
        paused_sources.append(source_name)

    await lead_storage.set_paused_sources(role, paused_sources)
    kv_cache.invalidate_role(role)
    return {"paused_sources": paused_sources, "toggled": source_name}


@router.post("/sources/run-only")
async def campaign_source_run_only(request: Request):
    """Sandbox mode: pause ALL sources except the given one so only its leads are dialed.

    Pass ``source=""`` (empty string) to exit sandbox mode and resume all sources.
    """
    role = _campaign_role(request)
    body = await request.json()
    source_name = (body.get("source") or "").strip()

    all_sources_data = await lead_storage.get_campaign_sources(role, [])
    all_source_names = [s["name"] for s in all_sources_data]

    if not source_name:
        # Exit sandbox: clear all pauses
        await lead_storage.set_paused_sources(role, [])
        kv_cache.invalidate_role(role)
        return {"mode": "all_running", "paused_sources": [], "active_source": None}

    if source_name not in all_source_names:
        raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

    # Pause everything EXCEPT the selected source
    paused = [s for s in all_source_names if s != source_name]
    await lead_storage.set_paused_sources(role, paused)
    kv_cache.invalidate_role(role)
    logger.info(f"Sandbox mode: role={role} running only '{source_name}', paused {len(paused)} other sources")
    return {
        "mode": "sandbox",
        "active_source": source_name,
        "paused_sources": paused,
        "paused_count": len(paused),
    }


@router.delete("/sources")
async def campaign_source_delete(request: Request):
    """Delete all leads from a specific upload source."""
    role = _campaign_role(request)
    source_name = (request.query_params.get("source") or "").strip()
    if not source_name:
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        source_name = (body.get("source") or "").strip()
    if not source_name:
        raise HTTPException(status_code=400, detail="Missing 'source' parameter")

    deleted = await lead_storage.delete_campaign_source(role, source_name)
    paused = await lead_storage.get_paused_sources(role)
    if source_name in paused:
        paused = [s for s in paused if s != source_name]
        await lead_storage.set_paused_sources(role, paused)
    kv_cache.invalidate_role(role)
    logger.info("Deleted {} leads from source '{}' for role={}", deleted, source_name, role)
    return {"deleted": deleted, "source": source_name}


@router.post("/upload")
async def upload_leads(file: UploadFile = File(...), request: Request = None, source: str = "", sandbox: int = 1):
    try:
        role = _campaign_role(request) if request else "sales_1"
        content = await file.read()
        filename = (file.filename or "").lower()
        # Determine lead source: explicit param > role-based default
        lead_source = (source or "").strip().lower()
        if lead_source not in ("cold", "digital", "digital_marketing"):
            lead_source = "campaign"
        if lead_source == "digital_marketing":
            lead_source = "digital"
        # Leads may only enter through Sandbox 1. Downstream sandboxes are
        # outcome-driven and cannot be populated manually by an upload.
        if int(sandbox or 1) != 1:
            raise HTTPException(status_code=400, detail="Lead uploads are allowed only in Sandbox 1")
        lead_sandbox = 1

        EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

        def _is_phone(val: str) -> bool:
            v = val.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "")
            # Handle Excel float format: "9876543210.0" → "9876543210"
            if '.' in v:
                parts = v.split('.')
                if len(parts) == 2 and parts[1] == '0':
                    v = parts[0]
                else:
                    v = v.replace(".", "")
            return v.isdigit() and 7 <= len(v) <= 15

        def _is_email(val: str) -> bool:
            return bool(EMAIL_RE.match(val.strip()))

        def _score_column(values: list, check_fn) -> float:
            if not values:
                return 0.0
            hits = sum(1 for v in values if v and check_fn(str(v)))
            return hits / len(values)

        PHONE_HEADER_KEYWORDS = ('phone', 'mobile', 'cell', 'contact number', 'tel', 'telephone',
                                  'whatsapp', 'no.', 'number', 'call', 'dial', 'ph')

        def _col_is_phone_header(col: str) -> bool:
            cl = col.strip().lower().replace('.', '').replace(':', '').replace('_', ' ')
            return any(kw in cl for kw in PHONE_HEADER_KEYWORDS)

        def _detect_columns(rows: list[dict], upload_role: str = "sales_1") -> dict:
            if not rows:
                return {}
            cols = list(rows[0].keys())
            sample = rows[:30]
            col_values = {c: [str(r.get(c, "") or "") for r in sample] for c in cols}

            phone_scores = {c: _score_column(col_values[c], _is_phone) for c in cols}
            email_scores = {c: _score_column(col_values[c], _is_email) for c in cols}

            # Boost score for columns whose header name looks like a phone field
            for c in cols:
                if _col_is_phone_header(c):
                    phone_scores[c] = max(phone_scores.get(c, 0.0), 0.85)

            phone_col = max(phone_scores, key=phone_scores.get) if phone_scores else None
            email_col = max(email_scores, key=email_scores.get) if email_scores else None
            if phone_col and phone_scores[phone_col] < 0.3:
                phone_col = None
            if email_col and email_scores[email_col] < 0.3:
                email_col = None

            if phone_col is None and phone_scores:
                bk = max(phone_scores, key=phone_scores.get)
                if phone_scores[bk] > 0:
                    phone_col = bk

            text_cols = [c for c in cols if c not in (phone_col, email_col)]
            NAME_KEYWORDS = ['name', 'person', 'client', 'buyer', 'seller', 'agent', 'contact', 'lead', 'customer']
            COMPANY_KEYWORDS = ['company', 'business', 'organization', 'org', 'firm', 'brand', 'employer', 'shop', 'store', 'enterprise']

            def _col_matches(col: str, keywords: list) -> bool:
                cl = col.strip().lower()
                return any(kw in cl for kw in keywords)

            def _bad_for_contact_field(c: str) -> bool:
                return bool(
                    _looks_like_row_index_header(c)
                    or _column_values_mostly_row_numbers(col_values.get(c, []))
                )

            product_cols: set[str] = set()

            name_col = company_col = None
            for c in text_cols:
                if _bad_for_contact_field(c):
                    continue
                if c.strip().lower() in ('name', 'full name', 'first name', 'contact name', 'customer name'):
                    name_col = c
                    break
            for c in text_cols:
                if _bad_for_contact_field(c):
                    continue
                if c.strip().lower() in ('company', 'company name', 'business', 'organization'):
                    company_col = c
                    break
            if not name_col:
                for c in text_cols:
                    if c == company_col or _bad_for_contact_field(c):
                        continue
                    if _col_matches(c, NAME_KEYWORDS):
                        name_col = c
                        break
            if not company_col:
                for c in text_cols:
                    if c == name_col or _bad_for_contact_field(c):
                        continue
                    if _col_matches(c, COMPANY_KEYWORDS):
                        company_col = c
                        break

            remaining = [c for c in text_cols if c not in (name_col, company_col)]

            def _pick_fallback(candidates: list[str]):
                for c in candidates:
                    if _bad_for_contact_field(c):
                        continue
                    return c
                return None

            if not name_col:
                name_col = _pick_fallback([c for c in remaining if c not in product_cols])
                if name_col:
                    remaining = [c for c in remaining if c != name_col]
            if not company_col:
                company_col = _pick_fallback(remaining)

            logger.info(
                f"Auto-detected columns for {upload_role} → phone:{phone_col}, name:{name_col}, "
                f"email:{email_col}, company:{company_col}"
            )
            return {
                "phone": phone_col,
                "name": name_col,
                "email": email_col,
                "company": company_col,
                "product_cols": list(product_cols) if product_cols else [],
            }

        rows = []
        headers = []
        try:
            if filename.endswith('.xlsx'):
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                ws = wb.active
                all_rows = list(ws.iter_rows(values_only=True))
                if all_rows:
                    headers = [str(c or f"col{i}").strip() for i, c in enumerate(all_rows[0])]
                    for row in all_rows[1:]:
                        cleaned = {}
                        for i, v in enumerate(row):
                            if i >= len(headers):
                                break
                            # Convert numeric phone values: 9.876543210e+09 → "9876543210"
                            if isinstance(v, float) and v == int(v):
                                cleaned[headers[i]] = str(int(v))
                            elif isinstance(v, int):
                                cleaned[headers[i]] = str(v)
                            else:
                                cleaned[headers[i]] = str(v or "").strip()
                        rows.append(cleaned)
            elif filename.endswith('.xls'):
                import xlrd
                wb = xlrd.open_workbook(file_contents=content)
                ws = wb.sheet_by_index(0)
                if ws.nrows > 0:
                    headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
                    for r in range(1, ws.nrows):
                        cleaned = {}
                        for c in range(ws.ncols):
                            v = ws.cell_value(r, c)
                            if isinstance(v, float) and v == int(v):
                                cleaned[headers[c]] = str(int(v))
                            else:
                                cleaned[headers[c]] = str(v or "").strip()
                        rows.append(cleaned)
            else:
                decoded = None
                for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
                    try:
                        decoded = content.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
                if not decoded:
                    decoded = content.decode('latin-1', errors='replace')
                decoded = decoded.replace('\r\n', '\n').replace('\r', '\n')
                reader = csv.DictReader(io.StringIO(decoded))
                rows = list(reader)
                headers = reader.fieldnames or (list(rows[0].keys()) if rows else [])
        except Exception as e:
            logger.error(f"File parse error: {e}")
            raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")

        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        rows = _sanitize_tabular_rows(rows)
        if not rows:
            return {"status": "ok", "count": 0, "leads": [], "headers": [], "error": "No data rows found"}

        col_map = _detect_columns(rows, upload_role=role)
        phone_col = col_map.get("phone")
        name_col = col_map.get("name")
        email_col = col_map.get("email")
        company_col = col_map.get("company")
        mapped_cols = {c for c in (phone_col, name_col, email_col, company_col) if c}

        original_filename = file.filename or "uploaded_leads"

        total_rows = len(rows)
        invalid_phones = 0
        file_duplicates = 0
        seen_in_file: set[str] = set()
        clean_leads = []
        for r in rows:
            ph = _extract_phone_cell(r, phone_col, _norm_phone_str)
            if not ph:
                invalid_phones += 1
                continue
            norm_ph = _norm_phone_str(ph)
            if not norm_ph:
                invalid_phones += 1
                continue
            if norm_ph in seen_in_file:
                file_duplicates += 1
                continue
            seen_in_file.add(norm_ph)
            raw_name = str(r.get(name_col, "") if name_col else "").strip()
            entry = {
                "name": raw_name or "Unknown",
                "phone": norm_ph,
                "email": str(r.get(email_col, "") if email_col else "").strip(),
                "company": str(r.get(company_col, "") if company_col else "").strip(),
                "details": "",
                "upload_source": original_filename,
                "source": lead_source,
                "sandbox": lead_sandbox,
            }
            for col, val in r.items():
                if col in mapped_cols:
                    continue
                sv = str(val or "").strip()
                if sv:
                    entry[col] = sv
            clean_leads.append(entry)

        async def _publish_upload(event_type: str, **extra: object) -> None:
            try:
                from core.events import get_event_bus
                await get_event_bus().publish(event_type, role=role, **extra)
            except Exception:
                pass

        await _publish_upload(
            "upload_started",
            filename=original_filename,
            total_rows=total_rows,
            valid_rows=len(clean_leads),
        )

        chunk_size = 200
        count = 0
        db_duplicates = 0
        dnc_blocked = 0
        processed = 0
        total_to_save = len(clean_leads)
        for i in range(0, total_to_save, chunk_size):
            chunk = clean_leads[i : i + chunk_size]
            c, dup, dnc = await asyncio.to_thread(
                lead_storage._bulk_add_leads_sync, role, chunk
            )
            count += c
            db_duplicates += dup
            dnc_blocked += dnc
            processed = min(i + len(chunk), total_to_save)
            await _publish_upload(
                "upload_progress",
                filename=original_filename,
                processed=processed,
                saved=count,
                total=total_to_save,
                skipped_duplicates=file_duplicates + db_duplicates,
            )

        # Mirror uploaded contacts into campaign_contacts for the contact panel
        if clean_leads:
            try:
                from core.state import add_campaign_contacts
                contacts_to_mirror = [
                    {
                        "phone": e["phone"], "name": e.get("name", ""),
                        "source": "digital" if lead_source == "digital" else "campaign",
                        "upload_source": original_filename,
                    }
                    for e in clean_leads
                ]
                mirrored = add_campaign_contacts(role, contacts_to_mirror)
                logger.info(
                    f"Mirrored {mirrored} uploaded contacts into campaign_contacts for role '{role}'."
                )
            except Exception as mirror_exc:
                logger.warning("Mirror to campaign_contacts failed: {}", mirror_exc)

        skipped_total = file_duplicates + db_duplicates
        # Every Sandbox 1 upload also receives an idempotent orchestration job.
        # Cold rows resolve to P1/P2 (1.1), digital rows to P3 (1.2).
        queued_jobs = 0
        try:
            import hashlib
            from datetime import datetime, timezone
            from core.orchestration_service import schedule_job
            from core.storage import _get_conn
            from core.workflow_models import JobType

            queue_conn = _get_conn()
            for entry in clean_leads:
                lead_row = queue_conn.execute(
                    "SELECT id FROM leads WHERE role=? AND phone=? ORDER BY id DESC LIMIT 1",
                    (role, entry["phone"]),
                ).fetchone()
                if not lead_row:
                    continue
                phone_key = hashlib.sha256(f"{role}:{entry['phone']}".encode("utf-8")).hexdigest()[:20]
                schedule_job(
                    queue_conn,
                    lead_id=int(lead_row[0]),
                    job_type=JobType.FRESH_CALL,
                    source=lead_source,
                    due_at=datetime.now(timezone.utc),
                    key=f"sandbox1-upload:{phone_key}",
                    attempt=1,
                    source_type="lead_upload",
                    source_id=original_filename,
                    payload={"filename": original_filename, "sub_sandbox": "1.2" if lead_source == "digital" else "1.1"},
                )
                queued_jobs += 1
        except Exception as queue_exc:
            logger.exception("Sandbox 1 workflow queue creation failed: {}", queue_exc)
        kv_cache.invalidate_role(role)
        try:
            from core.dashboard_state import invalidate_role as _dash_invalidate_role

            _dash_invalidate_role(role)
        except Exception as exc:
            logger.warning("Dashboard invalidate after upload failed for role={}: {}", role, exc)
        logger.info(
            f"Upload complete for role '{role}': {count} saved, "
            f"{skipped_total} dupes ({file_duplicates} in-file, {db_duplicates} in-db), "
            f"{invalid_phones} invalid, {dnc_blocked} DNC blocked."
        )
        auto_start_result: dict = {}
        if count > 0:
            from core.orchestration_runtime import runtime_status
            if runtime_status()["mode"] == "live":
                auto_start_result = {"auto_started": True, "engine": "orchestration", "queued_jobs": queued_jobs}
            else:
                auto_start_result = await _try_start_campaign_worker(role)
        try:
            from core.events import get_event_bus
            await get_event_bus().publish("lead_updated", role=role, lead_id=None)
            await get_event_bus().publish(
                "upload_complete",
                role=role,
                filename=original_filename,
                count=count,
                skipped_duplicates=skipped_total,
                invalid_phones=invalid_phones,
                dnc_blocked=dnc_blocked,
            )
        except Exception:
            pass
        recent: list = []
        if count:
            n = min(150, max(int(count), 1))
            recent_raw = await lead_storage.get_leads(role, limit=n)
            recent = [enrich_lead_for_console(dict(x)) for x in recent_raw]
        return {
            "status": "ok",
            "count": count,
            "skipped_duplicates": skipped_total,
            "cleaning": {
                "total_rows": total_rows,
                "invalid_phones": invalid_phones,
                "file_duplicates": file_duplicates,
                "db_duplicates": db_duplicates,
                "dnc_blocked": dnc_blocked,
                "saved": count,
            },
            "recent": recent,
            "leads": clean_leads[:50],
            "headers": headers,
            "column_map": col_map,
            "queued_jobs": queued_jobs,
            **auto_start_result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lead upload failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload leads")


@router.post("/toggle")
async def toggle_campaign(request: Request):
    """Legacy alternating start/stop. Prefer ``POST /api/campaign/start`` and ``/stop``.

    Mirrors intent flags used for auto-resume after restarts (see ``START``).
    """
    try:
        role = _campaign_role(request)

        if _CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done():
            await lead_storage.set_campaign_want_running(role, False)
            await lead_storage.set_campaign_globally_paused(True)
            from core.state import _MANUALLY_STOPPED_ROLES
            _MANUALLY_STOPPED_ROLES.add(role)
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
            await release_orphaned_dialing_leads(role)
            logger.info(f"Stopped campaign for {role} (toggle).")
            try:
                from core.events import get_event_bus
                await get_event_bus().publish("lead_updated", role=role, lead_id=None)
            except Exception:
                pass
            return {"status": "stopped", "active": False, "campaign_paused": True}
        else:
            from core.worker import _schedule_preflight

            await lead_storage.set_campaign_globally_paused(False)
            err = await _schedule_preflight(role)
            if err:
                raise HTTPException(status_code=400, detail=err)
            from core.state import _MANUALLY_STOPPED_ROLES
            _MANUALLY_STOPPED_ROLES.discard(role)
            await lead_storage.set_campaign_want_running(role, True)
            _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
            logger.info(f"Started campaign for {role} (toggle).")
            try:
                from core.events import get_event_bus
                await get_event_bus().publish("lead_updated", role=role, lead_id=None)
            except Exception:
                pass
            return {"status": "started", "active": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Toggle campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle campaign")


def _enqueue_pending_lead_jobs(role: str) -> int:
    """Enqueue now-due FRESH_CALL workflow jobs for pending leads with no active job."""
    import hashlib
    from datetime import datetime, timezone
    from core.orchestration_service import schedule_job
    from core.storage import _get_conn
    from core.workflow_models import JobType

    conn = _get_conn()
    queued = 0
    try:
        active = conn.execute(
            """SELECT lead_id FROM workflow_jobs
               WHERE status IN ('scheduled','ready','claimed','running')"""
        ).fetchall()
        active_ids = {int(r[0]) for r in active}
        rows = conn.execute(
            "SELECT id, phone FROM leads WHERE role=? AND status='pending'",
            (role,),
        ).fetchall()
        for row in rows:
            lead_id = int(row["id"]) if isinstance(row, dict) or hasattr(row, "keys") else int(row[0])
            phone = str(row["phone"] if (isinstance(row, dict) or hasattr(row, "keys")) else row[1])
            if lead_id in active_ids:
                continue
            phone_key = hashlib.sha256(f"{role}:{phone}".encode("utf-8")).hexdigest()[:20]
            schedule_job(
                conn,
                lead_id=lead_id,
                job_type=JobType.FRESH_CALL,
                source="campaign",
                due_at=datetime.now(timezone.utc),
                key=f"sandbox1-upload:{phone_key}",
                attempt=1,
                source_type="lead_upload",
                source_id="campaign_start",
                payload={"sub_sandbox": "1.1"},
            )
            queued += 1
    finally:
        try:
            conn.commit()
        except Exception:
            pass
    if queued:
        logger.info("Start campaign: enqueued {} FRESH_CALL job(s) for role={}", queued, role)
    return queued


@router.post("/start")
async def start_campaign(request: Request):
    """Start the dialer for this role (**idempotent** — never stops an already-running worker).

    Historically `/start` mistakenly called toggle logic that would **stop** the campaign when a
    task was already alive, which confused the dashboard and halted runs on double-clicks/resync.
    """
    try:
        role = _campaign_role(request)
        if settings.orchestration_enforce_consent:
            from core.state import get_campaign_config
            if not get_campaign_config(role).get("consent_confirmed"):
                raise HTTPException(
                    status_code=400,
                    detail="Campaign blocked: consent_confirmed is not set. Confirm TRAI/DND consent in campaign config first.",
                )
        run = _CAMPAIGN_TASKS.get(role)
        if run and not run.done():
            c = await lead_storage.get_lead_counts(role)
            return {
                "status": "already_running",
                "active": True,
                "pending": c.get("pending", 0),
                "dialing": c.get("dialing", 0),
            }
        from core.orchestration_runtime import ensure_orchestration_running, runtime_status

        if runtime_status()["mode"] == "live":
            # Orchestration owns outbound dialing — never start the source-blind
            # legacy worker; just make sure the workflow-queue supervisor is alive.
            await ensure_orchestration_running()
            # Enqueue now-due FRESH_CALL jobs for any pending lead that has no
            # active workflow job, so Start Campaign dials immediately (no queue wait).
            try:
                await asyncio.to_thread(_enqueue_pending_lead_jobs, role)
            except Exception as enq_exc:
                logger.warning("Start campaign: pending-lead enqueue failed: {}", enq_exc)
            c = await lead_storage.get_lead_counts(role)
            return {
                "status": "started",
                "active": True,
                "engine": "orchestration",
                "campaign_status": "orchestration_live",
                "pending": c.get("pending", 0),
                "dialing": c.get("dialing", 0),
                "campaign_paused": False,
            }
        from core.worker import _schedule_preflight

        await lead_storage.set_campaign_globally_paused(False)
        err = await _schedule_preflight(role)
        if err:
            raise HTTPException(status_code=400, detail=err)
        # Merge campaign_contacts into leads before starting
        try:
            merge_result = await merge_contacts_to_leads(request)
            if merge_result.get("merged", 0) > 0:
                logger.info(
                    "Campaign start: merged {} contacts into leads for role={}",
                    merge_result["merged"], role,
                )
        except Exception as merge_exc:
            logger.warning("Campaign start: merge-contacts skipped: {}", merge_exc)
        from core.state import _MANUALLY_STOPPED_ROLES
        _MANUALLY_STOPPED_ROLES.discard(role)
        await lead_storage.set_campaign_want_running(role, True)
        _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
        c = await lead_storage.get_lead_counts(role)
        try:
            from core.events import get_event_bus
            await get_event_bus().publish("lead_updated", role=role, lead_id=None)
        except Exception:
            pass
        return {
            "status": "started",
            "active": True,
            "pending": c.get("pending", 0),
            "dialing": c.get("dialing", 0),
            "campaign_paused": False,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Start campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to start campaign")


@router.post("/stop")
async def stop_campaign(request: Request):
    try:
        role = _campaign_role(request)
        await lead_storage.set_campaign_want_running(role, False)
        await lead_storage.set_campaign_globally_paused(True)
        from core.state import _MANUALLY_STOPPED_ROLES
        _MANUALLY_STOPPED_ROLES.add(role)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        if role in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[role]["running"] = False
        released = await release_orphaned_dialing_leads(role)
        try:
            from core.events import get_event_bus
            await get_event_bus().publish("lead_updated", role=role, lead_id=None)
        except Exception:
            pass
        return {"status": "stopped", "active": False, "released_dialing": released, "campaign_paused": True}
    except Exception as e:
        logger.error(f"Stop campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to stop campaign")


@router.post("/stop-all")
async def stop_all_campaigns(request: Request):
    """Stop outbound dialers for every console role and clear orphaned dialing rows."""
    caller = _campaign_role(request)
    if caller != "admin":
        raise HTTPException(status_code=403, detail="Admin role required to stop all campaigns")

    await lead_storage.set_campaign_globally_paused(True)
    from core.state import _MANUALLY_STOPPED_ROLES
    stopped: list[str] = []
    for r in _ROLES:
        _MANUALLY_STOPPED_ROLES.add(r)
        await lead_storage.set_campaign_want_running(r, False)
        task = _CAMPAIGN_TASKS.get(r)
        if task and not task.done():
            task.cancel()
        _CAMPAIGN_TASKS[r] = None
        released = await release_orphaned_dialing_leads(r)
        if r in _REANALYZE_ALL_PROGRESS:
            _REANALYZE_ALL_PROGRESS[r]["running"] = False
        stopped.append(r)
        logger.info("stop-all: role={} released_dialing={}", r, released)

    return {
        "status": "stopped_all",
        "roles": stopped,
        "active_campaigns": 0,
        "campaign_paused": True,
    }


@router.post("/reset")
async def reset_campaign(request: Request):
    try:
        role = _campaign_role(request)
        reset_leads(role)
        counts = await lead_storage.get_lead_counts(role)
        return {"status": "reset", "count": counts.get("total", 0)}
    except Exception as e:
        logger.error(f"Reset campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset campaign")


def _clear_role_files(role: str, log_ids: list[str]) -> int:
    """Remove role-scoped transcript/recording/log files, mirroring
    ``scripts/wipe_local_full.py`` but only for one role. Files keyed by the
    role's ``log_id`` values are matched by filename prefix; the per-role
    ``data/<role>/`` directory and the digital-leads dir are cleared in full."""
    backend_dir = Path(__file__).resolve().parent.parent
    data_dir = backend_dir / "data"
    removed = 0
    log_id_set = {str(x) for x in (log_ids or [])}

    # Per-role logs dir (data/<role>/logs/...) — entirely this role's files.
    role_dir = data_dir / role
    if role_dir.is_dir():
        for path in sorted(role_dir.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass

    # Transcripts / recordings keyed by log_id (role-scoped via collected ids).
    scan_dirs: list[Path] = []
    for name in (
        "conversation_logs",
        "call_recordings",
        "recordings",
        "Technopolis_Call_Recordings",
    ):
        d = data_dir / name
        if d.is_dir():
            scan_dirs.append(d)
    for cfg in (
        settings.conversation_log_dir,
        settings.call_recording_dir,
        settings.call_recording_archive_dir,
    ):
        if cfg:
            d = Path(cfg)
            if d.is_dir() and d not in scan_dirs:
                scan_dirs.append(d)
    for d in scan_dirs:
        for path in d.rglob("*"):
            if not path.is_file():
                continue
            if any(pid and path.name.startswith(pid) for pid in log_id_set):
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass

    # Digital-leads uploads dir — digital leads are stored in ``leads`` (already
    # wiped role-scoped above); only populated dirs are cleared.
    digital_dir = data_dir / "digital_leads"
    if digital_dir.is_dir():
        for path in sorted(digital_dir.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass
    return removed


@router.post("/wipe")
async def wipe_campaign(request: Request):
    try:
        role = _campaign_role(request)
        await lead_storage.set_campaign_want_running(role, False)
        if _CAMPAIGN_TASKS.get(role):
            _CAMPAIGN_TASKS[role].cancel()
            _CAMPAIGN_TASKS[role] = None
        log_ids = await lead_storage.wipe_all_role_data(role)
        kv_cache.invalidate_role(role)
        try:
            from core.dashboard_state import invalidate_role as _dash_invalidate_role
            _dash_invalidate_role(role)
        except Exception as _de:
            logger.warning("DashboardState invalidate after wipe failed: {}", _de)
        # Drop this role's in-flight in-memory campaign/call data.
        try:
            from core.state import _CAMPAIGN_DATA as _cd
            role_key = normalize_console_role(role)
            for cid in [
                k for k, v in list(_cd.items())
                if str((v or {}).get("_role") or "").strip().lower() == role_key
            ]:
                _cd.pop(cid, None)
        except Exception as _ce:
            logger.warning("Failed to clear _CAMPAIGN_DATA for role {}: {}", role, _ce)
        # Remove role-scoped transcript/recording/log files.
        removed_files = _clear_role_files(role, log_ids)
        logger.info(f"Wipe complete for role: {role} (files removed: {removed_files})")
        return {"status": "wiped"}
    except Exception as e:
        logger.error(f"Wipe campaign failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to wipe campaign")


@router.post("/lead/{lead_id}/status")
async def update_lead_status_route(lead_id: int, request: Request):
    try:
        role = _campaign_role(request)
        data = await request.json()
        new_status = data.get("status", "")
        VALID = {
            "pending",
            "completed",
            "failed",
            "not_interested",
            "callback_scheduled",
            "site_visit",
            "interested",
        }
        if new_status not in VALID:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID}")
        row = await lead_storage.get_lead(role, lead_id)
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        await update_lead_status(lead_id, new_status)
        logger.info(f"Lead {lead_id} marked as {new_status}")

        try:
            import json
            from services.whatsapp_outcome import send_outcome_whatsapp_if_eligible

            raw_a = row.get("analysis") or "{}"
            analysis = json.loads(raw_a) if isinstance(raw_a, str) else (raw_a if isinstance(raw_a, dict) else {})
            await send_outcome_whatsapp_if_eligible(
                role=role,
                phone=str(row.get("phone") or ""),
                lead_name=str(row.get("name") or ""),
                disposition=str(analysis.get("disposition") or ""),
                status=new_status,
                analysis=analysis,
                lead_id=lead_id,
                camp_id=str(row.get("_call_id") or row.get("_log_id") or ""),
                email_on_file=str(row.get("email") or ""),
                force_resend=True,
            )
        except Exception as wa_err:
            logger.warning("WhatsApp on status update failed for lead {}: {}", lead_id, wa_err)

        return {"status": "ok", "lead_id": lead_id, "new_status": new_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update lead status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to update lead status")


@router.post("/lead/{lead_id}/whatsapp-sent")
async def mark_lead_whatsapp_sent_route(lead_id: int, request: Request):
    try:
        from core.storage import mark_whatsapp_sent
        await mark_whatsapp_sent(lead_id)
        logger.info(f"Lead {lead_id} manually marked as whatsapp_sent")
        return {"status": "ok", "lead_id": lead_id}
    except Exception as e:
        logger.error(f"Mark lead whatsapp sent failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to mark lead whatsapp sent")


class ManualSendBody(BaseModel):
    channel: str = "whatsapp"  # "whatsapp" or "email" or "both"


@router.post("/lead/{lead_id}/send-details")
async def manual_send_lead_details(lead_id: int, body: ManualSendBody, request: Request):
    """Manually trigger WhatsApp / Email send for a lead."""
    role = _campaign_role(request)
    row = await lead_storage.get_lead(role, lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    phone = row.get("phone", "")
    email = row.get("email", "")
    name = row.get("name", "") or "there"
    results = []
    channel = body.channel
    try:
        if channel in ("whatsapp", "both") and phone:
            from services.whatsapp_leads import send_whatsapp_project_details
            wa = await send_whatsapp_project_details(phone, lead_name=name)
            if wa.get("sent"):
                from core.storage import mark_whatsapp_sent
                await mark_whatsapp_sent(lead_id)
            results.append(("whatsapp", wa))
        if channel in ("email", "both") and email and "@" in email:
            from services.email_leads import send_email_project_details
            em = await send_email_project_details(email, lead_name=name)
            if em.get("sent"):
                from core.storage import mark_email_sent
                await mark_email_sent(lead_id)
            results.append(("email", em))
    except Exception as e:
        logger.error(f"Manual send failed for lead {lead_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "lead_id": lead_id, "results": results}


class FreeformSendBody(BaseModel):
    phone: str = ""
    email: str = ""
    name: str = ""
    channel: str = "both"


@router.post("/send-details")
async def freeform_send_details(body: FreeformSendBody, request: Request):
    """Send project details to any phone/email (not tied to a lead)."""
    role = _campaign_role(request)
    results = []
    body_phone = (body.phone or "").strip()
    body_email = (body.email or "").strip()
    body_name = (body.name or "").strip() or "there"
    channel = body.channel or "both"
    try:
        if channel in ("whatsapp", "both") and body_phone:
            from services.whatsapp_leads import send_whatsapp_project_details
            wa = await send_whatsapp_project_details(body_phone, lead_name=body_name)
            results.append(("whatsapp", wa))
        if channel in ("email", "both") and body_email and "@" in body_email:
            from services.email_leads import send_email_project_details
            em = await send_email_project_details(body_email, lead_name=body_name)
            results.append(("email", em))
    except Exception as e:
        logger.error(f"Freeform send failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "channel": channel, "results": results}


def _norm_phone_digits(phone: object) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


async def _resolve_lead_session_log_id(role: str, row: dict) -> str:
    from core.storage import resolve_lead_session_log_id_sync

    return resolve_lead_session_log_id_sync(
        role,
        int(row["id"]) if row.get("id") is not None else None,
        str(row.get("phone") or ""),
        current_log_id=str(row.get("_log_id") or row.get("log_id") or "").strip(),
    )


@router.get("/lead/{lead_id}/media")
async def campaign_lead_media(lead_id: int, request: Request):
    """Resolved log_id + recording/transcript availability (uses call_attempts when lead._log_id is empty)."""
    role = _campaign_role(request)
    row = await lead_storage.get_lead(role, lead_id)
    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    log_id = await _resolve_lead_session_log_id(role, row)
    role_key = normalize_console_role(role)
    if not log_id:
        return {
            "log_id": "",
            "recording_available": False,
            "recording_pending": False,
            "transcript_available": False,
            "recording_url": "",
            "transcript_url": "",
        }
    rec = resolve_dashboard_recording_path(log_id)
    if not rec or not rec.is_file():
        camp_id = str(row.get("_call_id") or row.get("camp_id") or "").strip()
        rec = await fetch_vobiz_recording_if_missing(
            log_id, camp_id=camp_id, initial_delay_sec=0.0
        )
    available = bool(rec and rec.is_file())
    raw = _read_transcript_jsonl(role, log_id)
    return {
        "log_id": log_id,
        "recording_available": available,
        "recording_pending": bool(log_id) and not available,
        "transcript_available": bool((raw or "").strip()),
        "recording_url": f"/api/campaign/lead/{lead_id}/recording?role={role_key}&log_id={log_id}",
        "transcript_url": f"/api/campaign/lead/{lead_id}/transcript?role={role_key}&log_id={log_id}",
    }


@router.get("/lead/{lead_id}/transcript")
async def campaign_lead_transcript(
    lead_id: int,
    request: Request,
    log_id: str | None = None,
):
    """Hybrid JSONL transcript (live STT + recording when needed)."""
    from core.worker import _resolve_call_transcript

    role = _campaign_role(request)
    if log_id:
        _log_id = log_id.strip()
    else:
        row = await lead_storage.get_lead(role, lead_id)
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        _log_id = await _resolve_lead_session_log_id(role, row)
        if not _log_id:
            raise HTTPException(status_code=404, detail="No transcript session for this lead")
    raw, source = await _resolve_call_transcript(role, _log_id)
    if not (raw or "").strip():
        raise HTTPException(status_code=404, detail="Transcript file missing or empty")
    headers = {"X-Transcript-Source": source or "unknown"}
    if source in ("live_jsonl_short", "audio", "live_jsonl_raw"):
        from core.worker import _read_live_jsonl_only
        from services.transcript_thin import user_speech_stats

        live_user, _, _ = user_speech_stats(_read_live_jsonl_only(role, _log_id) or "")
        if live_user < 1:
            headers["X-Transcript-Warning"] = "live_stt_no_user_speech"
    return Response(content=raw, media_type="text/plain; charset=utf-8", headers=headers)


@router.get("/lead/{lead_id}/recording")
async def campaign_lead_recording(
    lead_id: int,
    request: Request,
    log_id: str | None = None,
):
    """Vobiz Application recording for dashboard playback (carrier-side WAV/MP3)."""
    role = _campaign_role(request)
    row = None
    if log_id:
        _log_id = log_id.strip()
    else:
        row = await lead_storage.get_lead(role, lead_id)
        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")
        _log_id = await _resolve_lead_session_log_id(role, row)
        if not _log_id:
            raise HTTPException(status_code=404, detail="No session log for recording lookup")
    if row is None:
        row = await lead_storage.get_lead(role, lead_id)
    camp_id = ""
    if row:
        camp_id = str(row.get("_call_id") or row.get("camp_id") or "").strip()
    rec = resolve_dashboard_recording_path(_log_id)
    if not rec or not rec.is_file():
        rec = await fetch_vobiz_recording_if_missing(
            _log_id, camp_id=camp_id, initial_delay_sec=8.0
        )
    if not rec or not rec.is_file():
        raise HTTPException(
            status_code=404,
            detail="Recording not found — Vobiz carrier recording may still be processing",
        )
    media_type = "audio/mpeg" if rec.name.endswith(".mp3") else "audio/wav"
    return range_file_response(rec, request, media_type)


@router.get("/lead/{lead_id}/attempts")
async def campaign_lead_attempts(lead_id: int, request: Request):
    """Return historical call attempts (including retakes) for a lead."""
    role = _campaign_role(request)
    from core.storage import get_call_attempts, pick_best_call_attempt
    attempts = await get_call_attempts(lead_id)
    best = pick_best_call_attempt(attempts)
    best_id = best.get("id") if best else None
    # Resolve recording/transcript URLs for each attempt that has a log_id
    role_key = normalize_console_role(role)
    for a in attempts:
        log_id = (a.get("log_id") or "").strip()
        a["is_best"] = bool(best_id and a.get("id") == best_id)
        if log_id:
            a["log_id"] = log_id
            try:
                rp = resolve_dashboard_recording_path(log_id)
                a["recording_available"] = bool(rp and rp.is_file())
                a["recording_pending"] = bool(log_id) and not a["recording_available"]
                if a["recording_available"]:
                    a["recording_url"] = f"/api/campaign/lead/{lead_id}/recording?role={role_key}&log_id={log_id}"
                    try:
                        from services.call_recording import recording_duration_sec
                        a["recording_duration_sec"] = recording_duration_sec(log_id)
                    except Exception:
                        pass
            except Exception:
                a["recording_available"] = False
                a["recording_pending"] = bool(log_id)
            a["transcript_url"] = f"/api/campaign/lead/{lead_id}/transcript?role={role_key}&log_id={log_id}"
        else:
            a["recording_available"] = False
            a["recording_pending"] = False
    return {"role": role, "lead_id": lead_id, "attempts": attempts, "best_attempt_id": best_id}


@router.get("/lead/{lead_id}/best")
async def campaign_lead_best_attempt(lead_id: int, request: Request):
    """Return best call attempt bundle for fast modal load."""
    role = _campaign_role(request)
    from core.storage import get_call_attempts, pick_best_call_attempt
    attempts = await get_call_attempts(lead_id)
    best = pick_best_call_attempt(attempts)
    if not best:
        raise HTTPException(status_code=404, detail="No attempts found")
    role_key = normalize_console_role(role)
    log_id = (best.get("log_id") or "").strip()
    if log_id:
        best["transcript_url"] = f"/api/campaign/lead/{lead_id}/transcript?role={role_key}&log_id={log_id}"
        rp = resolve_dashboard_recording_path(log_id)
        best["recording_available"] = bool(rp and rp.is_file())
        best["recording_pending"] = not best["recording_available"]
        if best["recording_available"]:
            best["recording_url"] = f"/api/campaign/lead/{lead_id}/recording?role={role_key}&log_id={log_id}"
    else:
        best["recording_available"] = False
        best["recording_pending"] = False
    best["is_best"] = True
    return {"role": role, "lead_id": lead_id, "best": best}


@router.post("/lead/{lead_id}/analyze")
async def retrigger_analysis(
    lead_id: int,
    request: Request,
):
    try:
        role = _campaign_role(request)
        lead_row = await lead_storage.get_lead(role, lead_id)
        if not lead_row:
            raise HTTPException(status_code=404, detail="Lead not found")
        log_id = lead_row.get("_log_id")
        if not log_id:
            raise HTTPException(status_code=400, detail="No log ID found for this lead")
        await _analyze_and_update_lead(role, lead_id, log_id)
        refreshed = await lead_storage.get_lead(role, lead_id)
        if not refreshed:
            raise HTTPException(status_code=500, detail="Lead missing after analyze")
        return {"status": "ok", "lead": slim_lead_for_api(dict(refreshed), role=role)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Retrigger analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to analyze call")


# ── Re-analyze All ────────────────────────────────────────────────────

_REANALYZE_ALL_PROGRESS: dict[str, dict] = {}
_KPI_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}
_KPI_SUMMARY_CACHE_TTL_SEC = 60.0

@router.post("/reanalyze-all")
async def campaign_reanalyze_all(request: Request):
    """Re-analyze every completed lead that has a log_id and recording."""
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS and _REANALYZE_ALL_PROGRESS[role].get("running"):
        raise HTTPException(status_code=409, detail="Re-analyze already running for this role")

    leads = await lead_storage.get_leads(role, limit=20000)
    eligible = [l for l in leads if l.get("status") in ("completed", "failed", "not_interested") and l.get("_log_id")]
    if not eligible:
        raise HTTPException(status_code=400, detail="No eligible leads found (need completed/failed status + log_id)")

    total = len(eligible)
    _REANALYZE_ALL_PROGRESS[role] = {
        "running": True,
        "total": total,
        "completed": 0,
        "current": "",
        "errors": [],
    }

    async def _run():
        try:
            for idx, lead in enumerate(eligible):
                if not _REANALYZE_ALL_PROGRESS.get(role, {}).get("running"):
                    break
                lid = lead["id"]
                log_id = lead.get("_log_id", "")
                name = lead.get("name", f"#{lid}")
                _REANALYZE_ALL_PROGRESS[role]["current"] = f"{name} ({lead.get('phone','')})"
                try:
                    await _analyze_and_update_lead(role, lid, log_id)
                except Exception as e:
                    _REANALYZE_ALL_PROGRESS[role]["errors"].append(f"#{lid} {name}: {e}")
                _REANALYZE_ALL_PROGRESS[role]["completed"] = idx + 1
        finally:
            if role in _REANALYZE_ALL_PROGRESS:
                _REANALYZE_ALL_PROGRESS[role]["running"] = False

    asyncio.create_task(_run())
    return {"status": "started", "total": total}


@router.get("/reanalyze-all/progress")
async def campaign_reanalyze_all_progress(request: Request):
    role = _campaign_role(request)
    state = _REANALYZE_ALL_PROGRESS.get(role)
    if not state:
        return {"running": False, "total": 0, "completed": 0, "current": "", "errors": []}
    return {
        "running": state.get("running", False),
        "total": state.get("total", 0),
        "completed": state.get("completed", 0),
        "current": state.get("current", ""),
        "errors": state.get("errors", []),
    }


@router.post("/reanalyze-all/cancel")
async def campaign_reanalyze_all_cancel(request: Request):
    role = _campaign_role(request)
    if role in _REANALYZE_ALL_PROGRESS:
        _REANALYZE_ALL_PROGRESS[role]["running"] = False
    return {"status": "cancelled"}


@router.get("/manifest")
async def campaign_manifest_preview(
    request: Request,
    limit: int = Query(500, ge=1, le=70_000, description="Max rows for dashboard Lead Manifest preview"),
    scope: str = Query("all", description="all = activity-ordered CSV rows; called = outbound cohort only"),
    sandbox: int = Query(0, ge=0, le=4, description="Filter by sandbox number (0=all, 1-4=sandbox)"),
):
    """Lightweight full-row fetch for UI tables — avoids oversized ``/state`` payloads.

    When ``sandbox`` is 1-4, only leads belonging to that sandbox are returned.
    This ensures the dashboard shows the correct filtered view per sandbox tab.
    """
    role = _campaign_role(request)
    cap = min(int(limit), 70_000)
    counts = await lead_storage.get_lead_counts(role)
    scope_key = (scope or "all").strip().lower()
    if scope_key == "called":
        from core.storage import get_leads_with_outbound_activity, count_leads_with_outbound_attempt

        called_total = await count_leads_with_outbound_attempt(role)
        rows = await get_leads_with_outbound_activity(role, limit=cap)
        enriched = [slim_lead_for_api(dict(r), role=role, skip_recording_probe=True) for r in rows]
        # Filter by sandbox if specified
        if sandbox:
            enriched = [l for l in enriched if (l.get("sandbox") or 1) == sandbox]
        total_in_db = int(counts.get("total") or 0)
        return {
            "role": role,
            "scope": "called",
            "sandbox": sandbox,
            "returned": len(enriched),
            "total_in_db": total_in_db,
            "called_total": called_total,
            "called_list_truncated": called_total > len(enriched),
            "lead_list_truncated": called_total > len(enriched),
            "leads": enriched,
        }
    rows = await lead_storage.get_leads(
        role, limit=cap, order="activity"
    )
    enriched = [slim_lead_for_api(dict(r), role=role, skip_recording_probe=True) for r in rows]
    # Filter by sandbox if specified
    if sandbox:
        enriched = [l for l in enriched if (l.get("sandbox") or 1) == sandbox]
    total_in_db = int(counts.get("total") or 0)
    return {"role": role, "scope": "all", "sandbox": sandbox, "returned": len(enriched), "total_in_db": total_in_db, "lead_list_truncated": total_in_db > len(enriched), "leads": enriched}


@router.get("/lead-count")
async def campaign_lead_count(request: Request):
    """Lightweight total lead count for KPI (no row payload)."""
    role = _campaign_role(request)
    counts = await lead_storage.get_lead_counts(role)
    return {"role": role, "total": int(counts.get("total") or 0), **counts}


@router.get("/inbound-interest")
async def campaign_inbound_interest(request: Request):
    """Leads who replied Interested via WhatsApp (or email WhatsApp button)."""
    role = _campaign_role(request)
    from core.storage import get_inbound_interest_leads, count_inbound_interest_leads
    from core.campaign_payload import slim_lead_for_api

    rows = await get_inbound_interest_leads(role)
    return {
        "role": role,
        "count": await count_inbound_interest_leads(role),
        "leads": [slim_lead_for_api(dict(r), role=role) for r in rows],
    }


@router.get("/state")
async def get_campaign_status(
    request: Request,
    chart_sample_limit: int = Query(250, ge=50, le=5000, description="Sample size for donut/callback charts embedded in state"),
    _skip_cache: bool = Query(False, alias="_skip_cache"),
    sandbox: int = Query(0, ge=0, le=4, description="Filter by sandbox number (0=all, 1-4=sandbox)"),
):
    try:
        role = _campaign_role(request)

        # Serve from KV cache unless explicitly skipped
        if not _skip_cache:
            cached = kv_cache.state_get(role)
            if cached is not None:
                return cached

        # Serve from pre-computed materialized dashboard state (<5ms, no DB rebuild)
        from core.dashboard_state import build_api_payload_sync
        payload = build_api_payload_sync(role)
        if payload is None:
            # Fallback: build from scratch (first load / error)
            counts = await lead_storage.get_lead_counts(role)
            sample_cap = min(int(chart_sample_limit), 5000)
            chart_rows = await lead_storage.get_leads(role, limit=sample_cap)
            dash = build_campaign_state_dashboard_fields(role, chart_rows)
            chart_leads = [
                slim_lead_for_api(l, role=role) for l in dash.pop("leads_enriched", [])
            ]
            total_in_db = int(counts.get("total") or 0)
            dash["called_count"] = await lead_storage.count_leads_with_outbound_attempt(role)
            try:
                from core.storage import count_call_attempts
                attempts = await count_call_attempts(role)
                dash["total_call_attempts"] = attempts
                dash["unique_leads_called"] = dash["called_count"]
                if attempts > dash["called_count"]:
                    dash["called_count"] = attempts
            except Exception:
                pass
            from core.storage import (
                is_strict_gap_core_role,
                STRICT_CORE_GAP_MIN_SEC,
                STRICT_CORE_GAP_MAX_SEC,
            )
            gap_strict = is_strict_gap_core_role(role)
            scheduled_cb_today = await lead_storage.count_scheduled_callbacks_due_today(role)
            completed_cb_today = await lead_storage.count_callbacks_completed_today(role)
            from core.worker import get_campaign_mode
            payload = {
                "active": bool(_CAMPAIGN_TASKS.get(role) and not _CAMPAIGN_TASKS[role].done()),
                "mode": get_campaign_mode(role),
                "inter_call_gap_sec": inter_call_gap_display_seconds_for_role(role),
                "inter_call_gap_strict": gap_strict,
                "inter_call_gap_min_sec": int(STRICT_CORE_GAP_MIN_SEC) if gap_strict else None,
                "inter_call_gap_max_sec": int(STRICT_CORE_GAP_MAX_SEC) if gap_strict else None,
                **counts,
                **dash,
                "chart_sample": chart_leads,
                "leads": chart_leads,
                "manifest_fetch_hint": {"endpoint": "/api/campaign/manifest", "suggested_limit": min(2500, max(500, sample_cap))},
                "lead_list_truncated": total_in_db > len(chart_leads),
                "leads_returned": len(chart_leads),
                "active_calls": total_active_vobiz_calls(),
                "campaign_hours": get_campaign_hours_status(),
                "campaign_paused": await lead_storage.is_campaign_globally_paused(),
                "scheduled_callbacks_today": scheduled_cb_today,
                "completed_callbacks_today": completed_cb_today,
                "total_callbacks_today": scheduled_cb_today + completed_cb_today,
            }
            from core.storage import count_inbound_interest_leads
            payload["inbound_interest_count"] = await count_inbound_interest_leads(role)

        # Override the campaign_paused
        payload["campaign_paused"] = await lead_storage.is_campaign_globally_paused()

        from core.storage import count_inbound_interest_leads
        payload["inbound_interest_count"] = await count_inbound_interest_leads(role)

        from config import live_dashboard_meta
        payload.update(live_dashboard_meta())

        # Cache the payload for subsequent polls
        from config import settings as _settings
        kv_cache.state_set(role, payload, ttl=_settings.live_kv_cache_ttl_sec)

        return payload
    except Exception as e:
        logger.error(f"Get campaign status failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get campaign status")


class InterCallGapBody(BaseModel):
    """Seconds to wait after each outbound leg before dialing the next lead (same role)."""
    seconds: float = Field(..., ge=0, le=1200, description="0 = back-to-back; max 20 minutes")


@router.post("/inter-call-gap")
async def set_inter_call_gap(body: InterCallGapBody, request: Request):
    """Persist pause between consecutive campaign calls for this role (``role_state.delay_sec``)."""
    try:
        from core.storage import (
            is_strict_gap_core_role,
            STRICT_CORE_GAP_SEC,
            STRICT_CORE_GAP_MIN_SEC,
            STRICT_CORE_GAP_MAX_SEC,
        )

        role = _campaign_role(request)
        if is_strict_gap_core_role(role):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Sales 1 / Vernika (Technopolis – Solitaire Unity) uses a fixed {int(STRICT_CORE_GAP_SEC)}s pause "
                    f"({int(STRICT_CORE_GAP_MIN_SEC)}–{int(STRICT_CORE_GAP_MAX_SEC)}s carrier safety); "
                    "it cannot be changed."
                ),
            )
        sec = float(body.seconds)
        save_role_state(role, delay_sec=sec)
        logger.info(f"inter_call_gap_sec={sec} saved for role={role}")
        return {"status": "ok", "inter_call_gap_sec": sec}
    except Exception as e:
        logger.error(f"Set inter-call gap failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to save inter-call gap")


@router.get("/phone-numbers")
async def get_phone_numbers(request: Request):
    """Get configured phone numbers for the current role."""
    try:
        role = _campaign_role(request)
        state = get_state(role)
        v_cfg = state.get("vobiz", {}) or {}
        numbers = get_all_outbound_numbers(role, v_cfg)
        
        # Get round-robin state
        from core.worker import _PHONE_ROUND_ROBIN_STATE, _MAX_CALLS_PER_HOUR, _CAMPAIGN_HOURLY_CALLS_PER_PHONE
        from core.call_line_coordinator import snapshot_line_states
        rr_state = _PHONE_ROUND_ROBIN_STATE.get(role, {})
        
        from core.state import get_campaign_config, get_max_concurrency_for_role, max_concurrency_for_vobiz_account, vobiz_auth_id_for_role
        return {
            "role": role,
            "phone_numbers": numbers,
            "current_index": rr_state.get("phone_index", 0),
            "total_calls_this_hour": rr_state.get("total_calls_this_hour", 0),
            "max_calls_per_hour": _MAX_CALLS_PER_HOUR,
            "max_calls_per_phone_per_hour": _CAMPAIGN_HOURLY_CALLS_PER_PHONE,
            "configured_concurrent_call_limit": int((get_campaign_config(role) or {}).get("concurrent_call_limit") or 1),
            "effective_concurrent_call_limit": get_max_concurrency_for_role(role),
            "vobiz_account_safe_limit": max_concurrency_for_vobiz_account(vobiz_auth_id_for_role(role)),
            "line_states": snapshot_line_states(numbers),
        }
    except Exception as e:
        logger.error(f"Get phone numbers failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get phone numbers")


@router.get("/download")
async def download_leads(request: Request, filter: str = "all"):
    try:
        role = _campaign_role(request)
        leads = export_leads_csv(role, filter)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "S.NO",
            "date and time",
            "lead ID",
            "name",
            "phone number",
            "email id",
            "conversation timing",
            "answered and not answered",
            "interested and not interested",
            "rating",
            "summary",
            "whatsapp sent (yes or no)",
            "email sent (yes or no)",
            "call direction",
            "conversation transcript"
        ])
        for idx, l in enumerate(leads):
            s_no = idx + 1
            called_at = ""
            if l.get("start_time") and l["start_time"] > 0:
                try:
                    called_at = datetime.datetime.fromtimestamp(l["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    called_at = str(l.get("created_at") or "")
            else:
                called_at = str(l.get("created_at") or "")

            lead_id = l.get("id", "")
            name = l.get("name", "")
            phone_raw = l.get("phone", "")
            # Force Excel/Spreadsheets to treat phone as text to prevent scientific notation
            phone = f"\t{phone_raw}" if phone_raw else ""
            email = l.get("email", "")

            analysis_raw = l.get("analysis")
            analysis = {}
            if analysis_raw:
                try:
                    import json
                    analysis = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
                except Exception:
                    analysis = {}

            duration_val = analysis.get("duration")
            if duration_val is not None:
                try:
                    conversation_timing = f"{round(float(duration_val))} seconds"
                except Exception:
                    conversation_timing = "0 seconds"
            else:
                conversation_timing = "0 seconds"

            status_lc = str(l.get("status") or "").lower().strip()
            if status_lc in ("completed", "site_visit", "callback_scheduled"):
                answered_not_answered = "Answered"
            else:
                answered_not_answered = "Not Answered"

            disp_lc = str(analysis.get("disposition") or "").lower().strip()
            site_visit_agreed = bool(analysis.get("site_visit_agreed"))
            if disp_lc == "interested" or site_visit_agreed or status_lc == "site_visit":
                interested_not_interested = "Interested"
            else:
                interested_not_interested = "Not Interested"

            # Retrieve rating from analysis (hide 0 values)
            rating_val = analysis.get("rating")
            rating = ""
            if rating_val is not None:
                try:
                    val = float(rating_val)
                    if val > 0:
                        rating = str(int(val)) if val.is_integer() else str(val)
                except Exception:
                    pass

            summary = analysis.get("summary") or l.get("error") or "Call did not connect."
            whatsapp_sent = "Yes" if l.get("whatsapp_sent") else "No"
            email_sent = "Yes" if l.get("email_sent") else "No"

            # Call direction resolution
            call_id = l.get("_call_id") or ""
            is_incoming = str(call_id).startswith("incoming_") or str(name).lower().startswith("inbound")
            call_direction = "Incoming" if is_incoming else "Outbound"

            # Transcript extraction
            log_id = l.get("_log_id")
            conversation_transcript = ""
            if log_id:
                try:
                    from core.worker import _read_transcript_jsonl
                    raw_tr = _read_transcript_jsonl(role, log_id)
                    if raw_tr:
                        lines_out = []
                        for line in raw_tr.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                import json
                                obj = json.loads(line)
                                msg_role = obj.get("role") or obj.get("type", "")
                                msg_content = obj.get("content") or obj.get("text") or obj.get("message", "")
                                if msg_role in ("user", "assistant") and msg_content:
                                    lines_out.append(f"{msg_role.capitalize()}: {msg_content.strip()}")
                            except Exception:
                                lines_out.append(line)
                        conversation_transcript = "\n".join(lines_out)
                except Exception as tr_err:
                    logger.warning("Failed to read transcript for CSV download: {}", tr_err)

            writer.writerow([
                s_no,
                called_at,
                lead_id,
                name,
                phone,
                email,
                conversation_timing,
                answered_not_answered,
                interested_not_interested,
                rating,
                summary,
                whatsapp_sent,
                email_sent,
                call_direction,
                conversation_transcript
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        filename = f"leads_{role}_{filter}.csv"
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"Download leads failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to download leads")


@router.get("/kpi-summary")
async def campaign_kpi_summary(
    request: Request,
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
):
    """Return per-category KPI summary (used by the dashboard overview table)."""
    import time

    _jwt_payload_from_request(request)
    role = _campaign_role(request)
    cache_key = f"{role}:{from_date or ''}:{to_date or ''}"
    now = time.monotonic()
    cached = _KPI_SUMMARY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _KPI_SUMMARY_CACHE_TTL_SEC:
        return cached[1]

    result = await asyncio.to_thread(
        get_report_kpi_summary,
        from_date=from_date,
        to_date=to_date,
        role=role,
    )
    _KPI_SUMMARY_CACHE[cache_key] = (now, result)
    return result


@router.get("/followup-leads")
async def campaign_followup_leads(request: Request):
    """Return site visit follow-up candidate leads for the active role."""
    role = _campaign_role(request)
    leads = await lead_storage.get_site_visit_followup_leads(role, limit=500)
    count = await lead_storage.count_site_visit_followup_leads(role)
    return {
        "role": role,
        "followup_count": count,
        "leads": leads,
    }


@router.post("/trigger-followup")
async def campaign_trigger_followup(request: Request):
    """Stop active general campaign call task (if running) and trigger calls to Site Visit Follow-Up leads."""
    role = _campaign_role(request)
    
    # 1. Stop current general campaign execution task if active
    from core.state import _CAMPAIGN_TASKS, _MANUALLY_STOPPED_ROLES
    run = _CAMPAIGN_TASKS.get(role)
    if run and not run.done():
        run.cancel()
        _CAMPAIGN_TASKS[role] = None
        logger.info(f"Stopped running general campaign for role={role} to switch to Site Visit Follow-Up calling mode.")
    
    _MANUALLY_STOPPED_ROLES.discard(role)
    await lead_storage.set_campaign_globally_paused(False)
    await lead_storage.set_campaign_want_running(role, True)

    # 2. Reset site visit follow-up leads that are failed or completed to pending if requested, so they can be re-contacted
    conn = lead_storage._get_conn()
    conn.execute(
        """
        UPDATE leads 
        SET status = 'pending', updated_at = datetime('now')
        WHERE role = ? AND status IN ('failed', 'no answer', 'busy', 'no response') AND (
            status = 'site_visit'
            OR extra LIKE '%Assetz SV%'
            OR extra LIKE '%site_visit%'
            OR extra LIKE '%"sv"%'
            OR analysis LIKE '%site_visit%'
            OR analysis LIKE '%Site Visit%'
            OR analysis LIKE '%site visit%'
        )
        """,
        (role,)
    )
    conn.commit()

    # 3. Launch campaign worker for role
    from core.worker import _schedule_preflight, set_campaign_mode
    set_campaign_mode(role, "site_visit_followup")
    err = await _schedule_preflight(role)
    if err:
        raise HTTPException(status_code=400, detail=err)

    _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
    
    count = await lead_storage.count_site_visit_followup_leads(role)
    try:
        from core.events import get_event_bus
        await get_event_bus().publish("lead_updated", role=role, lead_id=None)
    except Exception:
        pass

    return {
        "status": "started",
        "mode": "site_visit_followup",
        "active": True,
        "followup_count": count,
        "message": f"Site Visit Follow-Up campaign started for {count} leads on role {role}.",
    }


# ── Outpero-style Campaign Config ────────────────────────────────────────

class CampaignConfigBody(BaseModel):
    campaign_name: str = ""
    employee_role: str = "sales_1"
    concurrent_call_limit: int = Field(default=2, ge=1, le=9)
    window_start: str = "11:00"
    window_end: str = "19:30"
    calling_window_start: str = ""
    calling_window_end: str = ""
    calling_days: list[int] = [0, 1, 2, 3, 4, 5, 6]
    holidays: list[str] = []
    skip_opted_out: bool = True
    skip_recently_days: int = 0
    skip_recently_called_days: int = 0
    retry_count: int = 2
    auto_retry_count: int = 2
    retry_when: str = "next_day"
    auto_retry_when: str = "next_day"
    repeat_type: str = "one_time"
    schedule_at: str | None = None
    consent_confirmed: bool = False
    lead_source: str = Field(default="campaign", pattern=r"^(campaign|digital)$")
    sandbox: int = Field(default=1, ge=1, le=4)

    def model_dump(self, **kwargs):
        """Normalize field names for storage."""
        d = super().model_dump(**kwargs)
        # Normalize window fields: prefer window_start/end, fallback to calling_window_*
        if not d.get("window_start") and d.get("calling_window_start"):
            d["window_start"] = d["calling_window_start"]
        if not d.get("window_end") and d.get("calling_window_end"):
            d["window_end"] = d["calling_window_end"]
        # Normalize retry fields
        if not d.get("retry_count") and d.get("auto_retry_count"):
            d["retry_count"] = d["auto_retry_count"]
        if not d.get("retry_when") and d.get("auto_retry_when"):
            d["retry_when"] = d["auto_retry_when"]
        # Normalize skip_recently fields
        if not d.get("skip_recently_days") and d.get("skip_recently_called_days"):
            d["skip_recently_days"] = d["skip_recently_called_days"]
        return d


@router.get("/config")
async def get_campaign_config(request: Request):
    role = _campaign_role(request)
    from core.state import get_campaign_config
    config = get_campaign_config(role)
    if not config.get("window_start") and not config.get("calling_window_start"):
        config["window_start"] = "11:00"
    if not config.get("window_end") and not config.get("calling_window_end"):
        config["window_end"] = "19:30"
    return {"role": role, "config": config}


@router.post("/config")
async def save_campaign_config_api(body: CampaignConfigBody, request: Request):
    role = _campaign_role(request)
    from core.state import save_campaign_config
    config = body.model_dump()
    save_campaign_config(role, config)
    return {"status": "ok", "role": role}


# ── Individual Contact Management ────────────────────────────────────────

class ContactBody(BaseModel):
    phone: str
    name: str = ""
    source: str = Field(default="campaign", pattern=r"^(campaign|digital)$")
    extra: dict = {}


class PasteBody(BaseModel):
    text: str


@router.post("/contact")
async def add_contact(body: ContactBody, request: Request):
    role = _campaign_role(request)
    from core.state import add_campaign_contacts
    count = add_campaign_contacts(role, [body.model_dump()])
    return {"status": "ok", "added": count}


@router.post("/contacts/paste")
async def paste_contacts(body: PasteBody, request: Request):
    role = _campaign_role(request)
    from core.state import paste_campaign_contacts, get_campaign_config
    source = get_campaign_config(role).get("lead_source", "campaign")
    count = paste_campaign_contacts(role, body.text, source=source)
    return {"status": "ok", "added": count}


@router.get("/contacts")
async def list_contacts(request: Request, limit: int = 500):
    role = _campaign_role(request)
    from core.state import get_campaign_contacts
    contacts = get_campaign_contacts(role, limit=limit)
    source = (request.query_params.get("source") or "").strip().lower()
    if source in ("campaign", "digital"):
        contacts = [c for c in contacts if c.get("source", "campaign") == source]
    return {"role": role, "count": len(contacts), "contacts": contacts}


@router.delete("/contacts")
async def clear_contacts(request: Request):
    role = _campaign_role(request)
    from core.state import clear_campaign_contacts
    source = (request.query_params.get("source") or "").strip().lower()
    deleted = clear_campaign_contacts(role, source=source if source in ("campaign", "digital") else "")
    from core.storage import _get_conn, _invalidate_state_cache
    conn = _get_conn()
    if source == "digital":
        cur = conn.execute(
            "DELETE FROM leads WHERE role = ? AND lower(COALESCE(source, '')) IN ('digital','digital_marketing')",
            (role,),
        )
    elif source == "campaign":
        cur = conn.execute(
            "DELETE FROM leads WHERE role = ? AND lower(COALESCE(source, 'campaign')) NOT IN ('digital','digital_marketing')",
            (role,),
        )
    else:
        cur = conn.execute("DELETE FROM leads WHERE role = ?", (role,))
    leads_deleted = max(0, int(cur.rowcount or 0))
    conn.commit()
    _invalidate_state_cache()
    kv_cache.invalidate_role(role)
    return {"status": "ok", "deleted": deleted, "leads_deleted": leads_deleted}


@router.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: int, request: Request):
    role = _campaign_role(request)
    from core.state import delete_campaign_contact
    deleted = delete_campaign_contact(role, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "ok", "deleted": deleted, "contact_id": contact_id}


@router.post("/merge-contacts")
async def merge_contacts_to_leads(request: Request):
    """Merge campaign_contacts into leads table for dialing.

    Called before campaign start to ensure contacts added via the form
    (add single, paste, CSV mirror) are included in the dial queue.
    """
    role = _campaign_role(request)
    from core.state import get_campaign_contacts, clear_campaign_contacts
    from core.storage import _get_conn
    contacts = get_campaign_contacts(role, limit=10000)
    if not contacts:
        return {"status": "ok", "merged": 0, "message": "No contacts to merge"}
    conn = _get_conn()
    merged = 0
    skipped = 0
    for c in contacts:
        phone = c.get("phone", "").strip()
        name = c.get("name", "").strip()
        if not phone:
            continue
        try:
            existing = conn.execute(
                "SELECT id FROM leads WHERE role=? AND phone=? AND status IN ('pending','dialing')",
                (role, phone),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            source = "digital" if c.get("source") == "digital" else "campaign"
            conn.execute(
                """INSERT INTO leads
                   (role, name, phone, status, source, sandbox, source_file, created_at)
                   VALUES (?, ?, ?, 'pending', ?, 1, 'campaign_contacts', datetime('now'))""",
                (role, name or "Unknown", phone, source),
            )
            merged += 1
        except Exception as e:
            logger.warning("Failed to merge contact {}: {}", phone, e)
            skipped += 1
    conn.commit()
    return {"status": "ok", "merged": merged, "skipped": skipped, "total": len(contacts)}


