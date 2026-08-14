"""Persist and sync outbound camp sessions so Vobiz webhooks can run on a different host than the dialer."""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

import httpx
from loguru import logger

from core.state import _CAMPAIGN_DATA
from core.storage import (
    get_camp_session,
    upsert_camp_session,
    update_camp_session_connected,
    update_camp_session_ended,
)


def _json_safe_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON fields (e.g. opening_pcm bytes) before DB / HTTP sync."""
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        if key in ("opening_pcm", "opening_pcm_b64", "opening_pcm_sr", "name_verify_pcm", "name_verify_pcm_b64", "name_verify_pcm_sr"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, dict):
            out[key] = value
        else:
            try:
                json.dumps(value)
                out[key] = value
            except (TypeError, ValueError):
                out[key] = str(value)
    return out


def _serialize_payload_for_sync(data: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe payload plus base64 greeting/name-verify PCM for webhook host playout."""
    safe = _json_safe_payload(data)
    for key, b64_key, sr_key in (
        ("opening_pcm", "opening_pcm_b64", "opening_pcm_sr"),
        ("name_verify_pcm", "name_verify_pcm_b64", "name_verify_pcm_sr"),
    ):
        pcm = (data or {}).get(key)
        if isinstance(pcm, (list, tuple)) and len(pcm) >= 2:
            raw = pcm[0]
            if isinstance(raw, (bytes, bytearray)) and raw:
                safe[b64_key] = base64.b64encode(bytes(raw)).decode("ascii")
                try:
                    safe[sr_key] = int(pcm[1])
                except (TypeError, ValueError):
                    safe[sr_key] = 16000
    return safe


def _restore_synced_pcm(
    payload: dict[str, Any],
    *,
    field: str,
    b64_key: str,
    sr_key: str,
) -> None:
    if not isinstance(payload, dict) or payload.get(field):
        return
    b64 = payload.get(b64_key)
    if not b64:
        return
    try:
        sr = int(payload.get(sr_key) or 16000)
        payload[field] = (base64.b64decode(str(b64)), sr)
    except Exception as exc:
        logger.warning("Failed to restore {} from sync payload: {}", field, exc)


def _restore_opening_pcm(payload: dict[str, Any]) -> None:
    """Decode synced greeting audio back into ``opening_pcm`` tuple form."""
    _restore_synced_pcm(
        payload,
        field="opening_pcm",
        b64_key="opening_pcm_b64",
        sr_key="opening_pcm_sr",
    )


def _restore_name_verify_pcm(payload: dict[str, Any]) -> None:
    """Decode synced name-verify audio back into ``name_verify_pcm`` tuple form."""
    _restore_synced_pcm(
        payload,
        field="name_verify_pcm",
        b64_key="name_verify_pcm_b64",
        sr_key="name_verify_pcm_sr",
    )


def _answer_host_from_url(public_url: str) -> str:
    return (public_url or "").strip().rstrip("/")


def _normalize_host_key(url: str) -> str:
    """Host identity for same-machine checks (strip scheme, path, trailing slash)."""
    u = (url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix) :]
    return u.split("/")[0].rstrip("/")


def is_same_answer_host(public_url: str) -> bool:
    """True when Vobiz callbacks land on this process (no HTTP camp-session mirror needed)."""
    pub = _normalize_host_key(_answer_host_from_url(public_url))
    if not pub:
        return False
    try:
        from config import settings

        server = _normalize_host_key(settings.server_url or "")
        vobiz = _normalize_host_key(settings.vobiz_public_base_url or "")
        stream = _normalize_host_key(settings.vobiz_stream_public_base_url or "")
        local_hosts = {
            "127.0.0.1",
            "localhost",
            "0.0.0.0",
            server,
            vobiz,
            stream,
        }
        local_hosts = {h for h in local_hosts if h}
        if pub in local_hosts:
            return True
        # nip.io / same hostname with different scheme
        if server and (pub == server or pub.endswith(server) or server.endswith(pub)):
            return True
        if vobiz and (pub == vobiz or pub.endswith(vobiz) or vobiz.endswith(pub)):
            return True
    except Exception:
        return False
    return False


async def register_camp_session(camp_id: str, role: str, payload: dict[str, Any]) -> None:
    """Store camp context locally so any instance can hydrate from SQLite."""
    if not camp_id:
        return
    safe = _serialize_payload_for_sync(payload)
    await upsert_camp_session(camp_id, role, safe)
    # Keep webhook-host memory hot (opening_pcm b64 → bytes) for imminent answer/WS.
    restored = dict(safe)
    _restore_opening_pcm(restored)
    _restore_name_verify_pcm(restored)
    if role and not restored.get("_role"):
        restored["_role"] = role
    _CAMPAIGN_DATA[camp_id] = {**(_CAMPAIGN_DATA.get(camp_id) or {}), **restored}
    lead_name = str((payload or {}).get("name") or "").strip()
    logger.info("Registered camp session: camp_id={} role={} lead_name={!r}", camp_id, role, lead_name)


async def sync_camp_session_to_answer_host(
    public_url: str,
    camp_id: str,
    role: str,
    payload: dict[str, Any],
) -> None:
    """Push camp context to the host that receives Vobiz answer / WebSocket callbacks."""
    base = _answer_host_from_url(public_url)
    if not base or not camp_id:
        return
    url = f"{base}/vobiz/camp-session"
    body = {"camp_id": camp_id, "role": role, "payload": _serialize_payload_for_sync(payload)}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
        ) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            logger.warning(
                "Camp session sync HTTP {} for camp_id={} → {} (host={})",
                resp.status_code,
                camp_id,
                (resp.text or "")[:200],
                base,
            )
        else:
            lead_name = str((payload or {}).get("name") or "").strip()
            logger.info(
                "Camp session synced to {} for camp_id={} lead_name={!r}",
                base,
                camp_id,
                lead_name,
            )
    except Exception as exc:
        logger.warning("Camp session sync to {} failed for camp_id={}: {}", base, camp_id, exc)


async def prepare_outbound_call_session(
    camp_id: str,
    role: str,
    payload: dict[str, Any],
    public_url: str,
    *,
    lead_id: int | None = None,
) -> None:
    """Persist locally and mirror to the Vobiz callback host before placing the call."""
    await register_camp_session(camp_id, role, payload)
    if is_same_answer_host(public_url):
        lead_name = str((payload or {}).get("name") or "").strip()
        logger.info(
            "Camp session local-only (same host as Vobiz callbacks): camp_id={} lead_name={!r}",
            camp_id,
            lead_name,
        )
    else:
        await sync_camp_session_to_answer_host(public_url, camp_id, role, payload)
    if lead_id is not None:
        from core.storage import update_lead_call_info

        await update_lead_call_info(lead_id, call_id=camp_id)


async def hydrate_camp_session(camp_id: str) -> bool:
    """Load camp context into ``_CAMPAIGN_DATA`` from memory, SQLite, or lead row."""
    if not camp_id:
        return False
    if camp_id in _CAMPAIGN_DATA:
        return True

    row = await get_camp_session(camp_id)
    if row:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload:
            restored = dict(payload)
            _restore_opening_pcm(restored)
            _restore_name_verify_pcm(restored)
            _CAMPAIGN_DATA[camp_id] = restored
            role = str(restored.get("_role") or row.get("role") or "").strip()
            if role and not _CAMPAIGN_DATA[camp_id].get("opening_pcm"):
                try:
                    from core.greeting_pcm import load_recorded_greeting_pcm
                    from core.opening_line import build_opening_line

                    opening = build_opening_line(_CAMPAIGN_DATA[camp_id], role)
                    recorded = load_recorded_greeting_pcm(role, greeting_text=opening)
                    if recorded:
                        _CAMPAIGN_DATA[camp_id]["opening_pcm"] = recorded
                except Exception as exc:
                    logger.debug("Greeting hydrate skipped for camp_id={}: {}", camp_id, exc)
            connected_at = row.get("connected_at")
            ended_at = row.get("ended_at")
            log_id = row.get("log_id")
            if connected_at:
                _CAMPAIGN_DATA[camp_id]["_call_connected_at"] = float(connected_at)
            if ended_at:
                _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = float(ended_at)
            if log_id:
                _CAMPAIGN_DATA[camp_id]["_log_id"] = log_id
            logger.info("Hydrated camp session from DB for camp_id={}", camp_id)
            return True

    try:
        from core.storage import lead_row_by_call_id

        lead = await lead_row_by_call_id(camp_id)
        if lead:
            _CAMPAIGN_DATA[camp_id] = {
                **lead,
                "_lead_id": lead.get("id"),
                "_role": lead.get("role"),
                "_call_id": camp_id,
            }
            logger.info("Hydrated camp session from lead row for camp_id={}", camp_id)
            return True
    except Exception as exc:
        logger.warning("Lead hydrate failed for camp_id={}: {}", camp_id, exc)

    return False


async def fetch_camp_session_status(public_url: str, camp_id: str) -> Optional[dict[str, Any]]:
    base = _answer_host_from_url(public_url)
    if not base or not camp_id:
        return None
    url = f"{base}/vobiz/camp-session/{camp_id}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=4.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def poll_camp_session_into_memory(
    camp_id: str,
    public_url: str,
) -> dict[str, Any]:
    """Merge remote / DB session state into ``_CAMPAIGN_DATA[camp_id]`` for the dialer wait loop."""
    info = dict(_CAMPAIGN_DATA.get(camp_id) or {})
    remote = await fetch_camp_session_status(public_url, camp_id)
    if remote:
        if remote.get("connected_at") and not info.get("_call_connected_at"):
            info["_call_connected_at"] = float(remote["connected_at"])
        if remote.get("ended_at") and not info.get("_call_ended_at"):
            info["_call_ended_at"] = float(remote["ended_at"])
        if remote.get("log_id") and not info.get("_log_id"):
            info["_log_id"] = str(remote["log_id"])
        payload = remote.get("payload")
        if isinstance(payload, dict) and payload:
            merged = dict(payload)
            _restore_opening_pcm(merged)
            info = {**info, **merged}

    if not info.get("_call_connected_at") or not info.get("_call_ended_at"):
        row = await get_camp_session(camp_id)
        if row:
            if row.get("connected_at") and not info.get("_call_connected_at"):
                info["_call_connected_at"] = float(row["connected_at"])
            if row.get("ended_at") and not info.get("_call_ended_at"):
                info["_call_ended_at"] = float(row["ended_at"])
            if row.get("log_id") and not info.get("_log_id"):
                info["_log_id"] = str(row["log_id"])

    if info:
        _CAMPAIGN_DATA[camp_id] = {**(_CAMPAIGN_DATA.get(camp_id) or {}), **info}
    return _CAMPAIGN_DATA.get(camp_id) or info


async def mark_camp_connected(camp_id: str, connected_at: float, log_id: str = "") -> None:
    role = ""
    if camp_id in _CAMPAIGN_DATA:
        _CAMPAIGN_DATA[camp_id]["_call_connected_at"] = connected_at
        if log_id:
            _CAMPAIGN_DATA[camp_id]["_log_id"] = log_id
        role = str(_CAMPAIGN_DATA[camp_id].get("_role") or "")
    await update_camp_session_connected(camp_id, connected_at, log_id or None, role)


async def mark_camp_ended(camp_id: str, ended_at: float) -> None:
    role = ""
    if camp_id in _CAMPAIGN_DATA:
        _CAMPAIGN_DATA[camp_id]["_call_ended_at"] = ended_at
        role = str(_CAMPAIGN_DATA[camp_id].get("_role") or "")
    await update_camp_session_ended(camp_id, ended_at, role)
