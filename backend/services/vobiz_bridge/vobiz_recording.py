"""Vobiz trunk recording webhook + Recording API download into call_recording storage."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from config import settings
from core.vobiz_credentials import resolve_vobiz_credentials
from core.state import normalize_console_role
from services.vobiz_bridge.vobiz_client import _get_vobiz_client

VOBIZ_API_BASE = "https://api.vobiz.ai/api/v1"

# call_uuid (lower) -> session metadata for recording ingest
_CALL_UUID_INDEX: dict[str, dict[str, str]] = {}


def _norm_uuid(value: str) -> str:
    return (value or "").strip().lower()


def _auth_pairs_for_account(auth_id: str) -> list[tuple[str, str, str]]:
    """Return (role, auth_id, auth_token) candidates — prefer matching auth_id."""
    aid = (auth_id or "").strip()
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def _add(role: str) -> None:
        r = normalize_console_role(role)
        auth_id_v, auth_token_v, _, _ = resolve_vobiz_credentials(r)
        key = f"{auth_id_v}:{auth_token_v}"
        if not auth_id_v or not auth_token_v or key in seen:
            return
        if aid and auth_id_v != aid:
            return
        seen.add(key)
        out.append((r, auth_id_v, auth_token_v))

    if aid:
        for role in ("sales_1",):
            _add(role)
    if not out:
        for role in ("sales_1",):
            _add(role)
    return out


def register_vobiz_call_mapping(
    *,
    call_uuid: str,
    camp_id: str = "",
    log_id: str = "",
    role: str = "",
    phone: str = "",
    auth_id: str = "",
) -> None:
    """Index Vobiz CallUUID → camp/log for recording webhook correlation."""
    cu = _norm_uuid(call_uuid)
    if not cu:
        return
    prev = _CALL_UUID_INDEX.get(cu) or {}
    entry = {
        "call_uuid": call_uuid.strip(),
        "camp_id": (camp_id or prev.get("camp_id") or "").strip(),
        "log_id": (log_id or prev.get("log_id") or "").strip(),
        "role": normalize_console_role(role) if role else (prev.get("role") or ""),
        "phone": (phone or prev.get("phone") or "").strip(),
        "auth_id": (auth_id or prev.get("auth_id") or "").strip(),
    }
    _CALL_UUID_INDEX[cu] = entry
    if entry["camp_id"]:
        try:
            from core.state import _CAMPAIGN_DATA

            info = _CAMPAIGN_DATA.get(entry["camp_id"])
            if isinstance(info, dict):
                info["_vobiz_call_uuid"] = call_uuid.strip()
                if entry["phone"]:
                    info["_answered_phone"] = entry["phone"]
                    info.setdefault("phone", entry["phone"])
                if entry["log_id"]:
                    info["_log_id"] = entry["log_id"]
        except Exception:
            pass
    try:
        from core.storage import upsert_vobiz_call_map

        upsert_vobiz_call_map(
            call_uuid=entry["call_uuid"],
            camp_id=entry["camp_id"],
            log_id=entry["log_id"],
            role=entry["role"],
            phone=entry["phone"],
            auth_id=entry["auth_id"],
        )
    except Exception as exc:
        logger.debug("Persist vobiz_call_map failed: {}", exc)


def lookup_vobiz_call_mapping(call_uuid: str) -> dict[str, str]:
    cu = _norm_uuid(call_uuid)
    hit = _CALL_UUID_INDEX.get(cu)
    if hit:
        return dict(hit)
    try:
        from core.storage import lookup_vobiz_call_map

        db_hit = lookup_vobiz_call_map(call_uuid)
        if db_hit:
            # Prefer original CallUUID casing from request when available.
            if call_uuid and call_uuid.strip():
                db_hit["call_uuid"] = call_uuid.strip()
            _CALL_UUID_INDEX[cu] = dict(db_hit)
            return dict(db_hit)
    except Exception:
        pass
    return {}


def lookup_mapping_by_log_id(log_id: str) -> dict[str, str]:
    """Reverse lookup CallUUID mapping by session log_id."""
    target = (log_id or "").strip()
    if not target:
        return {}
    for entry in _CALL_UUID_INDEX.values():
        if (entry.get("log_id") or "").strip() == target:
            return dict(entry)
    try:
        from core.storage import lookup_vobiz_call_map_by_log_id

        db_hit = lookup_vobiz_call_map_by_log_id(target)
        if db_hit:
            cu = _norm_uuid(db_hit.get("call_uuid") or "")
            if cu:
                _CALL_UUID_INDEX[cu] = dict(db_hit)
            return dict(db_hit)
    except Exception:
        pass
    return {}


def _call_uuid_from_session_meta(log_id: str) -> tuple[str, str]:
    """Return (call_uuid, role_hint) from live JSONL session meta if present."""
    try:
        from services.call_recording import resolve_recording_from_session_meta  # noqa: F401
        from services.call_recording import _parse_log_id_date
        from pathlib import Path
        import json

        date_hint = _parse_log_id_date(log_id)
        backend_dir = Path(__file__).resolve().parent.parent
        conv_base = Path(settings.conversation_log_dir)
        if not conv_base.is_absolute():
            conv_base = backend_dir / conv_base
        candidates = []
        if date_hint:
            candidates.append(conv_base / date_hint / f"{log_id}.jsonl")
        if conv_base.is_dir():
            p = conv_base / f"{log_id}.jsonl"
            if p.is_file():
                candidates.append(p)
        for path in candidates:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                cu = ""
                role_hint = str(row.get("role") or "")
                if row.get("type") == "meta":
                    cu = str(row.get("call_id") or row.get("call_uuid") or "").strip()
                elif row.get("type") == "session":
                    meta = row.get("meta")
                    if isinstance(meta, dict):
                        cu = str(
                            meta.get("call_id") or meta.get("call_uuid") or ""
                        ).strip()
                if cu:
                    return cu, role_hint
        # Legacy per-role log paths (e.g. data/sales_1/logs/YYYY-MM-DD/…)
        if date_hint:
            backend_data = backend_dir / "data"
            for role_dir in backend_data.glob("sales_*"):
                alt = role_dir / "logs" / date_hint / f"{log_id}.jsonl"
                if not alt.is_file():
                    continue
                for line in alt.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "session":
                        continue
                    meta = row.get("meta")
                    if isinstance(meta, dict):
                        cu = str(
                            meta.get("call_id") or meta.get("call_uuid") or ""
                        ).strip()
                        if cu:
                            return cu, str(row.get("role") or "")
    except Exception:
        pass
    return "", ""


async def resolve_recording_context_for_log_id(
    log_id: str,
    *,
    camp_id: str = "",
) -> dict[str, str]:
    """Best-effort CallUUID + auth + camp for Application recording fetch."""
    log_id = (log_id or "").strip()
    cid = (camp_id or "").strip()
    call_uuid = ""
    role = ""
    auth_id = ""

    mapping = lookup_mapping_by_log_id(log_id)
    if mapping:
        call_uuid = mapping.get("call_uuid") or ""
        cid = cid or mapping.get("camp_id") or ""
        role = mapping.get("role") or ""
        auth_id = mapping.get("auth_id") or ""

    if not call_uuid and cid:
        try:
            from core.state import _CAMPAIGN_DATA

            info = _CAMPAIGN_DATA.get(cid) or {}
            if isinstance(info, dict):
                call_uuid = str(info.get("_vobiz_call_uuid") or "").strip()
                role = role or str(info.get("_role") or info.get("role") or "").strip()
        except Exception:
            pass

    if not call_uuid:
        try:
            from core.state import _CAMPAIGN_DATA

            for meta_cid, info in list(_CAMPAIGN_DATA.items()):
                if not isinstance(info, dict):
                    continue
                if str(info.get("_log_id") or "").strip() == log_id:
                    cid = cid or str(meta_cid)
                    call_uuid = str(info.get("_vobiz_call_uuid") or "").strip()
                    role = role or str(info.get("_role") or info.get("role") or "").strip()
                    break
        except Exception:
            pass

    if not call_uuid:
        call_uuid, role_hint = _call_uuid_from_session_meta(log_id)
        role = role or role_hint

    if cid and not role:
        try:
            from core.storage import incoming_call_row_by_camp_id, manual_call_row_by_camp_id

            row = await incoming_call_row_by_camp_id(cid)
            if not row:
                row = await manual_call_row_by_camp_id(cid)
            if row:
                role = str(row.get("role") or "").strip()
        except Exception:
            pass

    if not auth_id and role:
        auth_id, _, _, _ = resolve_vobiz_credentials(role)
    elif not auth_id:
        auth_id, _, _, _ = resolve_vobiz_credentials("sales_1")

    return {
        "log_id": log_id,
        "camp_id": cid,
        "call_uuid": call_uuid,
        "role": role,
        "auth_id": auth_id,
    }


async def ensure_vobiz_application_recording(
    log_id: str,
    *,
    camp_id: str = "",
    initial_delay_sec: float = 12.0,
) -> dict[str, Any]:
    """Actively pull Application recording from Vobiz API for dashboard playback."""
    if not settings.vobiz_trunk_recording_enabled:
        return {"ok": False, "skipped": True, "reason": "disabled"}

    from services.call_recording import resolve_vobiz_recording_path

    existing = resolve_vobiz_recording_path(log_id)
    if existing:
        return {"ok": True, "path": str(existing), "cached": True}

    ctx = await resolve_recording_context_for_log_id(log_id, camp_id=camp_id)
    call_uuid = ctx.get("call_uuid") or ""
    if not call_uuid:
        return {"ok": False, "error": "no_call_uuid", "log_id": log_id}

    if initial_delay_sec > 0:
        await asyncio.sleep(initial_delay_sec)

    payload = {
        "CallUUID": call_uuid,
        "auth_id": ctx.get("auth_id") or "",
        "ParentAuthID": ctx.get("auth_id") or "",
        "Event": "Hangup",
    }
    return await ingest_vobiz_trunk_recording(payload, max_retries=5, retry_delay_sec=10.0)


async def _api_get_recording(
    auth_id: str,
    auth_token: str,
    recording_id: str,
) -> dict[str, Any]:
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Recording/{recording_id}/"
    client = _get_vobiz_client()
    resp = await client.get(
        url,
        headers={
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Content-Type": "application/json",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def _api_list_by_call_uuid(
    auth_id: str,
    auth_token: str,
    call_uuid: str,
) -> Optional[dict[str, Any]]:
    url = f"{VOBIZ_API_BASE}/Account/{auth_id}/Recording/"
    client = _get_vobiz_client()
    resp = await client.get(
        url,
        params={"call_uuid": call_uuid, "limit": 5},
        headers={
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Content-Type": "application/json",
        },
    )
    if resp.status_code >= 400:
        return None
    data = resp.json()
    if not isinstance(data, dict):
        return None
    objects = data.get("objects")
    if isinstance(objects, list) and objects:
        first = objects[0]
        if isinstance(first, dict):
            return first
    return None


async def _download_recording_file(
    auth_id: str,
    auth_token: str,
    recording_url: str,
    dest: Path,
) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"X-Auth-ID": auth_id, "X-Auth-Token": auth_token}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True) as client:
            resp = await client.get(recording_url, headers=headers)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        ok = dest.is_file() and dest.stat().st_size > 128
        if ok:
            logger.info("Vobiz recording downloaded {} ({} B)", dest, dest.stat().st_size)
        return ok
    except Exception as exc:
        logger.warning("Vobiz recording download failed {}: {}", recording_url, exc)
        return False


def _parse_webhook_payload(raw: dict[str, Any]) -> dict[str, str]:
    """Normalize JSON or form-style Vobiz recording callbacks."""
    def _g(*keys: str) -> str:
        for k in keys:
            v = raw.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    event = _g("Event", "event")
    recording_id = _g("recording_id", "RecordingID", "recordingId")
    call_uuid = _g("CallUUID", "call_uuid", "callUuid")
    record_url = _g("RecordFile", "RecordUrl", "recording_url")
    auth_id = _g("auth_id", "AccountId", "account_id", "ParentAuthID", "parent_auth_id")
    return {
        "event": event,
        "recording_id": recording_id,
        "call_uuid": call_uuid,
        "record_url": record_url,
        "auth_id": auth_id,
    }


async def _resolve_log_id_for_recording(
    *,
    call_uuid: str = "",
    camp_id: str = "",
    from_num: str = "",
    to_num: str = "",
    direction: str = "",
) -> tuple[str, str, str]:
    """Resolve log_id, camp_id, role for Application / REST call recording ingest."""
    log_id = ""
    role = ""
    cid = (camp_id or "").strip()

    mapping = lookup_vobiz_call_mapping(call_uuid) if call_uuid else {}
    if mapping:
        log_id = str(mapping.get("log_id") or "").strip()
        cid = cid or str(mapping.get("camp_id") or "").strip()
        role = str(mapping.get("role") or "").strip()

    if cid and not log_id:
        try:
            from core.camp_session import hydrate_camp_session
            from core.state import _CAMPAIGN_DATA

            await hydrate_camp_session(cid)
            info = _CAMPAIGN_DATA.get(cid) or {}
            if isinstance(info, dict):
                log_id = str(info.get("_log_id") or "").strip()
                role = role or str(info.get("_role") or info.get("role") or "").strip()
        except Exception:
            pass

    if cid and not log_id:
        try:
            from core.storage import incoming_call_row_by_camp_id, manual_call_row_by_camp_id

            row = await incoming_call_row_by_camp_id(cid)
            if not row:
                row = await manual_call_row_by_camp_id(cid)
            if row:
                log_id = str(row.get("log_id") or "").strip()
                role = role or str(row.get("role") or "").strip()
        except Exception:
            pass

    if cid and not log_id:
        try:
            from core.storage import get_camp_session

            row = await get_camp_session(cid)
            if row:
                log_id = str(row.get("log_id") or "").strip()
                role = role or str(row.get("role") or "").strip()
        except Exception:
            pass

    if not log_id and (from_num or to_num):
        try:
            from core.state import _CAMPAIGN_DATA
            from core.utils import _norm_phone_str

            inbound = (direction or "").strip().lower() == "inbound"
            probe = _norm_phone_str(from_num if inbound else to_num)
            alt = _norm_phone_str(to_num if inbound else from_num)
            for meta_cid, meta in list(_CAMPAIGN_DATA.items()):
                if not isinstance(meta, dict):
                    continue
                phones = {
                    _norm_phone_str(str(meta.get("phone") or "")),
                    _norm_phone_str(str(meta.get("_answered_phone") or "")),
                    _norm_phone_str(str(meta.get("_outbound_phone") or "")),
                }
                if probe and probe in phones:
                    cid = cid or str(meta_cid)
                    log_id = str(meta.get("_log_id") or "").strip()
                    role = role or str(meta.get("_role") or meta.get("role") or "").strip()
                    break
                if alt and alt in phones and not log_id:
                    cid = cid or str(meta_cid)
                    log_id = str(meta.get("_log_id") or "").strip()
                    role = role or str(meta.get("_role") or meta.get("role") or "").strip()
        except Exception:
            pass

    return log_id, cid, role


async def ingest_vobiz_trunk_recording(
    payload: dict[str, Any],
    *,
    max_retries: int = 3,
    retry_delay_sec: float = 8.0,
) -> dict[str, Any]:
    """Download a Vobiz Application/trunk recording and attach it to the local call session."""
    if not settings.vobiz_trunk_recording_enabled:
        return {"ok": False, "skipped": True, "reason": "disabled"}

    parsed = _parse_webhook_payload(payload)
    event = (parsed.get("event") or "").lower()
    recording_id = parsed.get("recording_id") or ""
    call_uuid = parsed.get("call_uuid") or ""
    inline_url = parsed.get("record_url") or ""
    auth_hint = parsed.get("auth_id") or ""

    if event and "recording" not in event and event not in ("recordstop",):
        allow_hangup_poll = call_uuid and event in ("hangup", "callcompleted", "completed")
        if not recording_id and not inline_url and not allow_hangup_poll:
            return {"ok": True, "ignored": True, "reason": f"event:{event}"}

    log_id, camp_id, role = await _resolve_log_id_for_recording(
        call_uuid=call_uuid,
        from_num=str(payload.get("From") or payload.get("from") or ""),
        to_num=str(payload.get("To") or payload.get("to") or ""),
        direction=str(payload.get("Direction") or payload.get("direction") or ""),
    )

    meta: dict[str, Any] = {}
    dest_path: Optional[Path] = None

    mapping = lookup_vobiz_call_mapping(call_uuid) if call_uuid else {}
    auth_candidates = _auth_pairs_for_account(
        auth_hint or mapping.get("auth_id") or ""
    )
    last_err = ""
    for attempt in range(max(1, max_retries)):
        for role_try, auth_id, auth_token in auth_candidates:
            rec_meta: dict[str, Any] = {}
            recording_url = inline_url
            if recording_id:
                try:
                    rec_meta = await _api_get_recording(auth_id, auth_token, recording_id)
                    recording_url = str(rec_meta.get("recording_url") or recording_url or "")
                except Exception as exc:
                    last_err = str(exc)
                    continue
            elif call_uuid and not recording_url:
                try:
                    listed = await _api_list_by_call_uuid(auth_id, auth_token, call_uuid)
                    if listed:
                        rec_meta = listed
                        recording_id = str(listed.get("recording_id") or "")
                        recording_url = str(listed.get("recording_url") or "")
                except Exception as exc:
                    last_err = str(exc)
                    continue

            if not recording_url:
                continue

            if not log_id:
                last_err = "no_log_id_for_session"
                continue

            from services.call_recording import attach_vobiz_trunk_recording

            ext = ".wav" if "wav" in recording_url.lower() or str(rec_meta.get("recording_format") or "").lower() == "wav" else ".mp3"
            dest_path = attach_vobiz_trunk_recording(
                log_id,
                suffix_ext=ext,
                role=role_try or role,
                camp_id=camp_id,
                call_uuid=call_uuid,
                recording_id=recording_id,
            )
            if not dest_path:
                last_err = "attach_path_failed"
                continue

            ok = await _download_recording_file(auth_id, auth_token, recording_url, dest_path)
            if not ok:
                last_err = "download_failed"
                try:
                    dest_path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            meta = {
                "vobiz_recording_id": recording_id,
                "vobiz_call_uuid": call_uuid,
                "vobiz_recording_url": recording_url,
                "vobiz_recording_path": str(dest_path),
                "recording_format": rec_meta.get("recording_format"),
                "from_number": rec_meta.get("from_number"),
                "to_number": rec_meta.get("to_number"),
            }
            sidecar = dest_path.with_suffix(".json")
            try:
                sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            if settings.vobiz_trunk_recording_prefer_playback and dest_path.suffix.lower() in (".wav", ".mp3"):
                mixed_stem = dest_path.stem.replace("_vobiz", "_mixed")
                mixed = dest_path.parent / f"{mixed_stem}{dest_path.suffix}"
                if mixed != dest_path:
                    try:
                        shutil.copy2(dest_path, mixed)
                        logger.info("Vobiz application recording set as playback mixed: {}", mixed)
                    except Exception as exc:
                        logger.warning("Copy vobiz recording to mixed failed: {}", exc)

            logger.info(
                "Vobiz application recording ingested log_id={} camp_id={} path={}",
                log_id,
                camp_id,
                dest_path,
            )
            return {"ok": True, "log_id": log_id, "camp_id": camp_id, "path": str(dest_path), **meta}

        if attempt + 1 < max_retries:
            await asyncio.sleep(retry_delay_sec)

    return {"ok": False, "error": last_err or "not_found", "call_uuid": call_uuid, "recording_id": recording_id}


async def schedule_vobiz_recording_ingest(payload: dict[str, Any]) -> None:
    """Fire-and-forget background ingest (webhook must respond fast)."""
    try:
        result = await ingest_vobiz_trunk_recording(payload)
        if not result.get("ok") and not result.get("ignored"):
            logger.warning("Vobiz recording ingest failed: {}", result)
    except Exception as exc:
        logger.exception("Vobiz recording ingest error: {}", exc)


async def schedule_vobiz_application_recording_ingest(
    payload: dict[str, Any],
    *,
    delay_sec: float = 18.0,
) -> None:
    """Poll Vobiz Recording API after Application hangup (recording lags hangup by ~10–20s)."""
    try:
        await asyncio.sleep(max(0.0, float(delay_sec)))
        body = dict(payload or {})
        if not body.get("auth_id"):
            body["auth_id"] = body.get("ParentAuthID") or body.get("parent_auth_id") or ""
        result = await ingest_vobiz_trunk_recording(body, max_retries=4, retry_delay_sec=10.0)
        if not result.get("ok") and not result.get("ignored"):
            logger.warning("Vobiz application recording ingest failed: {}", result)
    except Exception as exc:
        logger.exception("Vobiz application recording ingest error: {}", exc)


async def get_call_results(call_uuid: str, mapping: dict) -> tuple[Optional[str], Optional[str]]:
    """Query SQLite DB to retrieve the transcript and summary for the call."""
    log_id = mapping.get("log_id")
    camp_id = mapping.get("camp_id")
    role = mapping.get("role") or "sales_1"
    
    summary = None
    transcript = None
    
    def _query_db():
        from core.storage import _get_conn
        conn = _get_conn()
        
        # 1. Check leads table
        if log_id:
            row = conn.execute("SELECT analysis FROM leads WHERE _log_id = ?", (log_id,)).fetchone()
            if row and row["analysis"]:
                try:
                    analysis = json.loads(row["analysis"])
                    s = analysis.get("summary")
                    if s:
                        return s, None
                except Exception:
                    pass
                    
        # 2. Check manual_calls table
        if camp_id:
            row = conn.execute("SELECT summary, analysis_json FROM manual_calls WHERE camp_id = ?", (camp_id,)).fetchone()
            if row:
                s = row["summary"]
                if not s and row["analysis_json"]:
                    try:
                        aj = json.loads(row["analysis_json"])
                        s = aj.get("summary")
                    except Exception:
                        pass
                if s:
                    return s, None
                    
        # 3. Check incoming_calls table
        if camp_id:
            row = conn.execute("SELECT summary, analysis_json FROM incoming_calls WHERE camp_id = ?", (camp_id,)).fetchone()
            if row:
                s = row["summary"]
                if not s and row["analysis_json"]:
                    try:
                        aj = json.loads(row["analysis_json"])
                        s = aj.get("summary")
                    except Exception:
                        pass
                if s:
                    return s, None
                    
        return None, None

    try:
        summary, _ = await asyncio.to_thread(_query_db)
    except Exception as e:
        logger.warning("Error querying DB for call summary: {}", e)

    # Retrieve transcript (this uses file resolution/hybrid)
    if log_id:
        from core.worker import _resolve_call_transcript
        try:
            transcript, _ = await _resolve_call_transcript(role, log_id)
        except Exception as e:
            logger.warning("Failed to resolve transcript for log_id={}: {}", log_id, e)

    return summary, transcript


async def deliver_final_webhook(call_uuid: str) -> None:
    """Deliver final webhook payload to the user's configured webhook URL.
    Waits 45s for the Vobiz callback to store recording_url, falls back to polling Vobiz API.
    Then waits for transcript & summary and fires user's webhook_url.
    """
    logger.info("Starting deliver_final_webhook for call_uuid={}", call_uuid)
    
    # 1. Lookup call mapping
    mapping = lookup_vobiz_call_mapping(call_uuid)
    if not mapping:
        # If mapping is not registered yet, sleep briefly and retry
        await asyncio.sleep(2.0)
        mapping = lookup_vobiz_call_mapping(call_uuid)
        if not mapping:
            logger.warning("Could not find call mapping for call_uuid={}. Aborting webhook delivery.", call_uuid)
            return

    # 2. Wait up to 45s for the recording URL callback to store in calls_db
    recording_url = None
    for _ in range(45):
        from core.state import calls_db
        if call_uuid in calls_db and calls_db[call_uuid].get("recording_url"):
            recording_url = calls_db[call_uuid]["recording_url"]
            break
        await asyncio.sleep(1.0)

    # 3. Fall back to polling Vobiz Recording API if still missing
    if not recording_url:
        logger.info("Recording callback not received for call_uuid={} within 45s, polling Vobiz API...", call_uuid)
        try:
            from core.vobiz_credentials import resolve_vobiz_credentials
            role = mapping.get("role") or "sales_1"
            auth_id, auth_token, _, _ = resolve_vobiz_credentials(role)
            
            for poll_attempt in range(3):
                listed = await _api_list_by_call_uuid(auth_id, auth_token, call_uuid)
                if listed and listed.get("recording_url"):
                    recording_url = listed["recording_url"]
                    from core.state import calls_db
                    if call_uuid not in calls_db:
                        calls_db[call_uuid] = {}
                    calls_db[call_uuid]["recording_url"] = recording_url
                    logger.info("Found recording_url via API polling for call_uuid={}: {}", call_uuid, recording_url)
                    break
                await asyncio.sleep(5.0)
        except Exception as exc:
            logger.error("Polling Vobiz Recording API failed for call_uuid={}: {}", call_uuid, exc)

    # 4. Wait for the call analysis/transcript to be completed (up to 45s)
    summary = None
    transcript = None
    for _ in range(45):
        try:
            summary, transcript = await get_call_results(call_uuid, mapping)
            if summary and transcript:
                break
        except Exception as e:
            logger.warning("Error fetching call results for call_uuid={}: {}", call_uuid, e)
        await asyncio.sleep(1.0)

    # 5. Resolve user's webhook_url
    webhook_url = getattr(settings, "user_webhook_url", "").strip()
    if not webhook_url:
        # Check campaign payload or campaign context
        camp_id = mapping.get("camp_id")
        if camp_id:
            from core.state import _CAMPAIGN_DATA
            if camp_id in _CAMPAIGN_DATA:
                webhook_url = str(_CAMPAIGN_DATA[camp_id].get("webhook_url") or "").strip()

    if not webhook_url:
        logger.warning("No user webhook_url configured/found for call_uuid={}. Skipping final webhook delivery.", call_uuid)
        return

    # 6. Fire the webhook payload
    payload = {
        "call_id": call_uuid,
        "camp_id": mapping.get("camp_id"),
        "role": mapping.get("role"),
        "phone": mapping.get("phone"),
        "recording_url": recording_url,
        "summary": summary or "Analysis not completed.",
        "transcript": transcript or "Transcript not available.",
    }

    logger.info("Firing user webhook to {} with payload call_id={}", webhook_url, call_uuid)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(webhook_url, json=payload)
            logger.info("User webhook delivered to {} response status={}", webhook_url, resp.status_code)
    except Exception as exc:
        logger.error("Failed to deliver final webhook to {}: {}", webhook_url, exc)

