"""Vobiz answer URL + incoming call webhook + media WebSocket."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, WebSocket
from loguru import logger

from config import settings
from core.outbound_numbers import build_phone_to_role_map
from core.state import (
    role_has_active_vobiz_call,
    phone_is_busy,
    _CAMPAIGN_DATA,
    _CAMPAIGN_TASKS,
    _ACTIVE_VOBIZ_CALLS_BY_ROLE,
    acquire_vobiz_call_slot,
    get_state,
    normalize_console_role,
    parse_manual_camp_role_suffix,
)
from core.storage import find_lead_by_phone, insert_incoming_call
from services.vobiz_bridge import (
    build_answer_xml,
    build_busy_message_xml,
    build_incoming_stream_xml,
    handle_vobiz_ws_live,
)

router = APIRouter(tags=["vobiz"])


def _build_busy_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Reject reason="busy"/></Response>'
    )


async def _vobiz_answer_impl(
    camp_id: Optional[str] = None,
    role: Optional[str] = None,
    request: Optional[Request] = None,
) -> Response:
    # Inbound PSTN legs often hit /vobiz/answer when the Vobiz Application answer_url
    # was set to the outbound URL. Detect caller→DID without camp_id and route inbound.
    if request is not None and not camp_id:
        try:
            form = await request.form()
        except Exception:
            form = {}
        from_num = str(form.get("From") or form.get("from") or "").strip()
        to_num = str(form.get("To") or form.get("to") or "").strip()
        if from_num and to_num:
            phone_map = build_phone_to_role_map()
            to_digits = re.sub(r"\D", "", to_num)
            mapped = phone_map.get(to_digits) or phone_map.get(
                to_digits[-10:] if len(to_digits) >= 10 else "", ""
            )
            if mapped:
                logger.info(
                    "Vobiz answer URL received inbound leg From={} To={} — routing to incoming handler",
                    from_num,
                    to_num,
                )
                return await _handle_inbound_call(
                    request,
                    from_num=from_num,
                    to_num=to_num,
                    caller_id=str(form.get("CallUUID") or form.get("call_uuid") or "").strip(),
                )

    normalized_role = normalize_console_role(role) if role else None

    if camp_id:
        try:
            from core.camp_session import hydrate_camp_session

            await hydrate_camp_session(camp_id)
        except Exception as exc:
            logger.warning("Camp session hydrate failed for camp_id={}: {}", camp_id, exc)

    # Outbound Application answer: register CallUUID → camp_id before WS (recording ingest).
    if camp_id and request is not None:
        try:
            form = await request.form()
        except Exception:
            form = {}
        answer_call_uuid = str(
            form.get("CallUUID") or form.get("call_uuid") or ""
        ).strip()
        if answer_call_uuid:
            try:
                from core.vobiz_credentials import resolve_vobiz_credentials
                from services.vobiz_bridge.vobiz_recording import register_vobiz_call_mapping

                info = _CAMPAIGN_DATA.get(camp_id) or {}
                log_id = str(info.get("_log_id") or "").strip()
                map_role = normalized_role or str(
                    info.get("_role") or info.get("role") or ""
                ).strip()
                auth_id, _, _, _ = resolve_vobiz_credentials(map_role or "sales_1")
                phone = str(
                    info.get("phone") or info.get("_answered_phone") or ""
                ).strip()
                register_vobiz_call_mapping(
                    call_uuid=answer_call_uuid,
                    camp_id=camp_id,
                    log_id=log_id,
                    role=map_role,
                    phone=phone,
                    auth_id=auth_id,
                )
                logger.info(
                    "Vobiz answer mapped CallUUID={} camp_id={} log_id={}",
                    answer_call_uuid,
                    camp_id,
                    log_id or "(pending)",
                )
            except Exception as exc:
                logger.warning("Answer URL call mapping failed camp_id={}: {}", camp_id, exc)

    # If camp_id is in _CAMPAIGN_DATA, WE initiated this call (manual or campaign).
    # The slot was already acquired by the campaign worker before dial — skip the
    # busy check so the campaign's own answer URL doesn't block its own call.
    known_call = bool(camp_id and camp_id in _CAMPAIGN_DATA)
    # Outbound legs always carry camp_id (campaign UUID, manual_*, sched_cb_*).
    # Never reject them as busy on the webhook host — the dialer holds slots locally.
    outbound_leg = bool(
        camp_id
        and not str(camp_id).startswith("incoming_")
    )

    is_busy = False
    if normalized_role and not known_call and not outbound_leg:
        if role_has_active_vobiz_call(normalized_role):
            is_busy = True

    if is_busy:
        return Response(content=_build_busy_xml(), media_type="application/xml")

    role_base = None
    if camp_id and camp_id in _CAMPAIGN_DATA:
        camp_role = _CAMPAIGN_DATA[camp_id].get("_role")
        if camp_role:
            state = get_state(camp_role)
            role_base = state.get("vobiz", {}).get("public_url")
    elif normalized_role:
        try:
            state = get_state(normalized_role)
            role_base = state.get("vobiz", {}).get("public_url")
        except Exception:
            role_base = None

    # Resolve media from the host that Vobiz actually reached. This keeps the
    # answer webhook and bidirectional WebSocket on one deployment and avoids
    # instant hangups caused by a stale VOBIZ_STREAM_PUBLIC_BASE_URL.
    wss_base = ""
    if request is not None:
        try:
            host_header = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if host_header:
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                wss_base = f"{scheme}://{host_header}"
        except Exception:
            pass
    if not wss_base:
        wss_base = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/")
    if not wss_base:
        wss_base = settings.server_url.rstrip("/")
    if not wss_base:
        wss_base = (role_base or settings.vobiz_public_base_url or "").rstrip("/")

    use_ws = False
    if request is not None:
        try:
            use_ws = request.query_params.get("use_ws", "").strip().lower() in ("1", "true", "yes")
        except Exception:
            pass

    if use_ws:
        wss_url = wss_base.replace("https://", "http://").replace("wss://", "ws://").replace("http://", "ws://") + "/ws/vobiz"
    else:
        wss_url = wss_base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"

    params = []
    agent_id = None
    resolved_manual_role = None
    if camp_id:
        params.append(f"camp_id={camp_id}")
        if camp_id in _CAMPAIGN_DATA:
            agent_id = _CAMPAIGN_DATA[camp_id].get("_agent_id")
        elif camp_id.startswith("sandbox-"):
            parts = camp_id.split("-")
            if len(parts) >= 2:
                agent_id = parts[1]

    if agent_id:
        params.append(f"agent_id={agent_id}")

    if camp_id and str(camp_id).startswith("manual_"):
        suffix = str(camp_id)[len("manual_") :]
        mr, _ = parse_manual_camp_role_suffix(suffix)
        if mr:
            resolved_manual_role = mr
    elif normalized_role:
        resolved_manual_role = normalized_role

    if resolved_manual_role:
        params.append(f"manual_role={quote(resolved_manual_role, safe='')}")

    if use_ws:
        params.append("use_ws=true")

    if params:
        wss_url += "?" + "&".join(params)

    if wss_base and (
        "trycloudflare.com" in wss_base
        or "trycloudflare.dev" in wss_base
        or "cfargotunnel.com" in wss_base
    ):
        logger.warning(
            "Vobiz <Stream> URL uses a Cloudflare quick-tunnel host ({}…). "
            "For stable calls set VOBIZ_STREAM_PUBLIC_BASE_URL to your VPS "
            "http://IP:PORT (same FastAPI server).",
            wss_base.split("//")[-1][:48],
        )

    if request is not None:
        try:
            logger.info(
                "Vobiz answer: method={} qs={}",
                request.method, dict(request.query_params),
            )
        except Exception:
            pass

    logger.info(
        "Vobiz answer: camp={} role={} wss_url={}",
        camp_id,
        normalized_role,
        wss_url,
    )
    xml_content = build_answer_xml(wss_url)
    logger.info("Vobiz answer returned XML: {}", xml_content)
    return Response(
        content=xml_content,
        media_type="application/xml",
    )


@router.post("/vobiz/answer")
async def vobiz_answer_post(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.get("/vobiz/answer")
async def vobiz_answer_get(request: Request, camp_id: Optional[str] = None, role: Optional[str] = None):
    return await _vobiz_answer_impl(camp_id=camp_id, role=role, request=request)


@router.post("/vobiz/camp-session")
async def vobiz_camp_session_register(request: Request):
    """Register outbound camp context on the webhook host (local or VPS)."""
    try:
        body = await request.json()
    except Exception:
        return Response(content='{"ok":false,"error":"invalid json"}', media_type="application/json", status_code=400)
    camp_id = str(body.get("camp_id") or "").strip()
    role = normalize_console_role(str(body.get("role") or "").strip()) if body.get("role") else ""
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if not camp_id or not role:
        return Response(
            content='{"ok":false,"error":"camp_id and role required"}',
            media_type="application/json",
            status_code=400,
        )
    from core.camp_session import hydrate_camp_session, register_camp_session

    await register_camp_session(camp_id, role, payload)
    await hydrate_camp_session(camp_id)
    lead_name = str((payload or {}).get("name") or "").strip()
    logger.info(
        "Camp session registered via API: camp_id={} role={} lead_name={!r}",
        camp_id,
        role,
        lead_name,
    )
    return {"ok": True, "camp_id": camp_id}


@router.get("/vobiz/camp-session/{camp_id}")
async def vobiz_camp_session_status(camp_id: str):
    from core.camp_session import hydrate_camp_session
    from core.storage import get_camp_session

    await hydrate_camp_session(camp_id)
    row = await get_camp_session(camp_id)
    if not row:
        return Response(content='{"ok":false,"error":"not found"}', media_type="application/json", status_code=404)
    payload = {}
    try:
        import json as _json

        payload = _json.loads(row.get("payload_json") or "{}")
    except Exception:
        payload = {}
    return {
        "ok": True,
        "camp_id": camp_id,
        "role": row.get("role"),
        "connected_at": row.get("connected_at"),
        "ended_at": row.get("ended_at"),
        "log_id": row.get("log_id"),
        "payload": payload if isinstance(payload, dict) else {},
    }


@router.websocket("/ws/vobiz")
async def vobiz_ws_endpoint(
    websocket: WebSocket,
    camp_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    manual_role: Optional[str] = None,
    lead_name: Optional[str] = None,
):
    logger.info(
        "Vobiz WS connect: camp_id={} agent_id={} manual_role={} lead_name={}",
        camp_id, agent_id, manual_role, lead_name,
    )
    await handle_vobiz_ws_live(
        websocket,
        camp_id=camp_id,
        agent_id=agent_id,
        manual_role=manual_role,
        lead_name=lead_name,
    )


@router.post("/vobiz/incoming")
async def vobiz_incoming_post(request: Request):
    """
    Vobiz Application answer URL for incoming calls.
    Called when someone dials one of our phone numbers.
    """
    try:
        form = await request.form()
    except Exception:
        form = {}
    from_num = str(form.get("From") or form.get("from") or request.query_params.get("From", "")).strip()
    to_num = str(form.get("To") or form.get("to") or request.query_params.get("To", "")).strip()
    caller_id = str(form.get("CallUUID") or form.get("call_uuid") or "").strip()
    auth_id = str(form.get("ParentAuthID") or form.get("auth_id") or "").strip()
    return await _handle_inbound_call(request, from_num, to_num, caller_id, auth_id=auth_id)


@router.get("/vobiz/incoming")
async def vobiz_incoming_get(request: Request):
    """GET fallback for Vobiz Application answer URL."""
    from_num = str(request.query_params.get("From") or request.query_params.get("from") or "").strip()
    to_num = str(request.query_params.get("To") or request.query_params.get("to") or "").strip()
    caller_id = str(request.query_params.get("CallUUID") or request.query_params.get("call_uuid") or "").strip()
    auth_id = str(request.query_params.get("ParentAuthID") or request.query_params.get("auth_id") or "").strip()
    return await _handle_inbound_call(request, from_num, to_num, caller_id, auth_id=auth_id)


async def _handle_inbound_call(
    request: Request,
    from_num: str,
    to_num: str,
    caller_id: str = "",
    auth_id: str = "",
) -> Response:
    logger.info("Vobiz incoming call: CallUUID={} From={} To={}", caller_id, from_num, to_num)

    if not from_num or not to_num:
        logger.warning("Inbound webhook missing From/To (from={!r} to={!r})", from_num, to_num)
        return Response(
            content=build_busy_message_xml(
                "Sorry, we could not connect your call. Please dial one of our sales lines: "
                "+918071579959 or +918071580022."
            ),
            media_type="application/xml",
        )

    # Determine role from the dialed number
    phone_map = build_phone_to_role_map()
    to_digits = re.sub(r"\D", "", to_num)
    role = phone_map.get(to_digits, "") or phone_map.get(to_digits[-10:] if len(to_digits) >= 10 else "", "")

    if not role:
        logger.warning("Incoming call to unknown number To={} (digits={})", to_num, to_digits)
        return Response(
            content=build_busy_message_xml(
                "Thank you for calling Technopolis Constructions. Our sales lines are "
                "+918071579959, +918071581599, +918071580022, or +918071579354. "
                "Please try again on one of those numbers."
            ),
            media_type="application/xml",
        )

    role = normalize_console_role(role)
    logger.info("Incoming call routed to role={} (dialed number {})", role, to_num)

    # Check if role or dialed line is busy with an active call / campaign
    campaign_task = _CAMPAIGN_TASKS.get(role)
    campaign_active = bool(campaign_task and not campaign_task.done())
    line_busy = phone_is_busy(to_num) if to_num else False
    if line_busy or role_has_active_vobiz_call(role) or campaign_active:
        logger.info(
            "Role={} busy (line_busy={}, calls={}, campaign={}), sending polite busy message",
            role, line_busy, _ACTIVE_VOBIZ_CALLS_BY_ROLE.get(role, 0), campaign_active,
        )
        from_digits = re.sub(r"\D", "", from_num) if from_num else "unknown"
        camp_id = f"incoming_{role}_missed_{from_digits}_{int(time.time())}"
        lead = await find_lead_by_phone(role, from_num)
        lead_name = (lead or {}).get("name", "") if lead else ""
        try:
            await insert_incoming_call(
                role,
                camp_id,
                from_num,
                lead_name or "",
                status="missed_busy",
                to_phone=to_num,
            )
            try:
                from core.events import get_event_bus

                await get_event_bus().publish(
                    "incoming_call",
                    role=role,
                    camp_id=camp_id,
                    from_phone=from_num,
                    caller_name=lead_name or "",
                    status="missed_busy",
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to log missed_busy incoming call: {}", e)
        return Response(
            content=build_busy_message_xml(
                "Thank you for calling back. I am currently on another call. "
                "I will call you back shortly. Have a great day."
            ),
            media_type="application/xml",
        )

    # Look up caller as a known lead — try the resolved role first, then cross-role
    lead = await find_lead_by_phone(role, from_num)
    if not lead and role == "sales_1":
        # If role is the fallback default, try other roles too
        for alt_role in ("sales_1",):
            lead = await find_lead_by_phone(alt_role, from_num)
            if lead:
                role = alt_role
                logger.info("Incoming call matched to role={} from cross-role lead lookup", role)
                break
    lead_name = (lead or {}).get("name", "") if lead else ""

    # Build WebSocket URL with incoming camp_id format
    from_digits = re.sub(r"\D", "", from_num) if from_num else "unknown"
    explicit_stream = (settings.vobiz_stream_public_base_url or "").strip().rstrip("/")
    wss_base = explicit_stream
    if not wss_base:
        try:
            host_header = request.headers.get("host", "")
            if host_header:
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                wss_base = f"{scheme}://{host_header}"
        except Exception:
            pass
    if not wss_base:
        wss_base = settings.server_url.rstrip("/")
    if not wss_base:
        wss_base = (settings.vobiz_public_base_url or "").rstrip("/")
    wss_url = wss_base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/vobiz"
    # Unique camp_id per leg — reusing phone-only ids hit UNIQUE(camp_id) and dropped rows.
    camp_id = f"incoming_{role}_from_{from_digits}_{int(time.time())}"
    wss_url += f"?camp_id={quote(camp_id, safe='')}"
    if lead_name:
        wss_url += f"&lead_name={quote(lead_name, safe='')}"

    # Application recording: map Vobiz CallUUID → camp_id before WS connects (hangup ingest).
    if caller_id:
        try:
            from core.vobiz_credentials import resolve_vobiz_credentials
            from services.vobiz_bridge.vobiz_recording import register_vobiz_call_mapping

            parent_auth = (auth_id or "").strip()
            if not parent_auth:
                parent_auth, _, _, _ = resolve_vobiz_credentials(role)
            register_vobiz_call_mapping(
                call_uuid=caller_id,
                camp_id=camp_id,
                log_id="",
                role=role,
                phone=from_num,
                auth_id=parent_auth,
            )
        except Exception as exc:
            logger.warning("Incoming Application call mapping failed: {}", exc)

    if wss_base and (
        "trycloudflare.com" in wss_base
        or "trycloudflare.dev" in wss_base
        or "cfargotunnel.com" in wss_base
    ):
        logger.warning(
            "Incoming call WSS uses a Cloudflare quick-tunnel host ({}…). "
            "For stable calls set VOBIZ_STREAM_PUBLIC_BASE_URL to the VPS http://IP:PORT.",
            wss_base.split("//")[-1][:48],
        )

    # Acquire a Vobiz call slot so the role is marked busy during this incoming call
    if not acquire_vobiz_call_slot(role):
        logger.warning("Incoming call rejected — Vobiz concurrent cap for role={}", role)
        return Response(content=_build_busy_xml(), media_type="application/xml")

    # Create a persistent record in the incoming_calls table
    try:
        await insert_incoming_call(
            role, camp_id, from_num, lead_name or "", status="ringing", to_phone=to_num
        )
        try:
            from core.events import get_event_bus
            await get_event_bus().publish(
                "incoming_call",
                role=role,
                camp_id=camp_id,
                from_phone=from_num,
                caller_name=lead_name or "",
            )
        except Exception:
            pass
    except Exception as e:
        logger.warning("Failed to insert incoming call record: {}", e)

    logger.info("Incoming call: routing to wss_url={} lead={}", wss_url, lead_name or "unknown")
    return Response(
        content=build_incoming_stream_xml(wss_url),
        media_type="application/xml",
    )


@router.post("/vobiz/hangup")
async def vobiz_hangup(request: Request):
    """Vobiz Application Hangup URL — triggers carrier recording download after call ends."""
    import asyncio

    payload = await _parse_vobiz_webhook_body(request)
    logger.info("Vobiz hangup: {}", payload)
    call_uuid = str(payload.get("CallUUID") or payload.get("call_uuid") or "").strip()
    event = str(payload.get("Event") or payload.get("event") or "Hangup").strip()
    if call_uuid and event.lower() in ("hangup", "callcompleted", "completed", ""):
        from services.vobiz_bridge.vobiz_recording import schedule_vobiz_application_recording_ingest, deliver_final_webhook

        asyncio.create_task(schedule_vobiz_application_recording_ingest(payload))
        asyncio.create_task(deliver_final_webhook(call_uuid))
    return Response(content="OK", status_code=200)


async def _parse_vobiz_webhook_body(request: Request) -> dict[str, Any]:
    """Parse JSON or form-urlencoded Vobiz webhook payloads."""
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}
    try:
        form = await request.form()
        return {str(k): str(v) for k, v in dict(form).items()}
    except Exception:
        pass
    try:
        raw = await request.body()
        if raw:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    return {}


@router.post("/vobiz/trunk-webhook")
async def vobiz_trunk_webhook(request: Request):
    """Vobiz outbound trunk webhook — CallInitiated / Hangup (registers CallUUID)."""
    import asyncio

    payload = await _parse_vobiz_webhook_body(request)
    event = str(payload.get("Event") or payload.get("event") or "").strip()
    call_uuid = str(payload.get("CallUUID") or payload.get("call_uuid") or "").strip()
    to_num = str(payload.get("To") or payload.get("to") or "").strip()
    from_num = str(payload.get("From") or payload.get("from") or "").strip()
    auth_id = str(payload.get("auth_id") or payload.get("AccountId") or "").strip()
    logger.info("Vobiz trunk webhook event={} CallUUID={} To={}", event, call_uuid, to_num)

    if call_uuid and to_num:
        try:
            from services.vobiz_bridge.vobiz_recording import register_vobiz_call_mapping

            camp_id = ""
            log_id = ""
            role = ""
            try:
                from core.state import _CAMPAIGN_DATA
                from core.utils import _norm_phone_str

                norm_to = _norm_phone_str(to_num)
                for cid, meta in list(_CAMPAIGN_DATA.items()):
                    if not isinstance(meta, dict):
                        continue
                    phones = {
                        _norm_phone_str(str(meta.get("phone") or "")),
                        _norm_phone_str(str(meta.get("_answered_phone") or "")),
                        _norm_phone_str(str(meta.get("_outbound_phone") or "")),
                    }
                    if norm_to and norm_to in phones:
                        camp_id = str(cid)
                        log_id = str(meta.get("_log_id") or "")
                        role = str(meta.get("_role") or meta.get("role") or "")
                        break
            except Exception:
                pass

            register_vobiz_call_mapping(
                call_uuid=call_uuid,
                camp_id=camp_id,
                log_id=log_id,
                role=role,
                phone=to_num,
                auth_id=auth_id,
            )
        except Exception as exc:
            logger.warning("Trunk webhook call mapping failed: {}", exc)

    if event.lower() == "hangup" and call_uuid:
        # Recording may arrive slightly after hangup — poll once in background.
        async def _delayed_poll() -> None:
            import asyncio
            from services.vobiz_bridge.vobiz_recording import ingest_vobiz_trunk_recording

            await asyncio.sleep(18.0)
            await ingest_vobiz_trunk_recording(
                {"Event": "recording.completed", "CallUUID": call_uuid, "auth_id": auth_id},
            )

        asyncio.create_task(_delayed_poll())

    return Response(content='{"status":"received"}', media_type="application/json", status_code=200)


@router.post("/vobiz/recording-webhook")
async def vobiz_recording_webhook(request: Request):
    """Vobiz trunk Recording Webhook — recording.completed / RecordStop."""
    import asyncio

    payload = await _parse_vobiz_webhook_body(request)
    logger.info(
        "Vobiz recording webhook: event={} CallUUID={} recording_id={}",
        payload.get("Event") or payload.get("event"),
        payload.get("CallUUID") or payload.get("call_uuid"),
        payload.get("recording_id") or payload.get("RecordingID"),
    )
    asyncio.create_task(_schedule_recording_ingest(payload))
    return Response(content='{"status":"received"}', media_type="application/json", status_code=200)


@router.get("/vobiz/recording-webhook")
async def vobiz_recording_webhook_get(request: Request):
    import asyncio

    payload = {k: str(v) for k, v in request.query_params.items()}
    asyncio.create_task(_schedule_recording_ingest(payload))
    return Response(content='{"status":"received"}', media_type="application/json", status_code=200)


async def _schedule_recording_ingest(payload: dict[str, Any]) -> None:
    from services.vobiz_bridge.vobiz_recording import schedule_vobiz_recording_ingest

    await schedule_vobiz_recording_ingest(payload)


@router.get("/vobiz/hangup")
async def vobiz_hangup_get(request: Request):
    """Vobiz Application Hangup URL (GET fallback)."""
    import asyncio

    payload = {k: str(v) for k, v in request.query_params.items()}
    logger.info("Vobiz hangup (GET): {}", payload)
    call_uuid = str(payload.get("CallUUID") or payload.get("call_uuid") or "").strip()
    event = str(payload.get("Event") or payload.get("event") or "Hangup").strip()
    if call_uuid and event.lower() in ("hangup", "callcompleted", "completed", ""):
        from services.vobiz_bridge.vobiz_recording import schedule_vobiz_application_recording_ingest, deliver_final_webhook

        asyncio.create_task(schedule_vobiz_application_recording_ingest(payload))
        asyncio.create_task(deliver_final_webhook(call_uuid))
    return Response(content="OK", status_code=200)


@router.post("/vobiz_recording")
async def vobiz_recording_callback(request: Request):
    """
    Vobiz recording callback endpoint.
    Receives recording completion details and stores RecordUrl in calls_db.
    """
    payload = await _parse_vobiz_webhook_body(request)
    logger.info("Received /vobiz_recording callback: {}", payload)
    
    call_id = str(
        payload.get("CallUUID")
        or payload.get("call_uuid")
        or payload.get("call_id")
        or ""
    ).strip()
    
    record_url = str(
        payload.get("RecordUrl")
        or payload.get("RecordFile")
        or payload.get("record_url")
        or payload.get("recording_url")
        or ""
    ).strip()
    
    if not call_id:
        logger.warning("/vobiz_recording callback missing CallUUID")
        return Response(content='{"ok":false,"error":"missing CallUUID"}', media_type="application/json", status_code=400)
        
    from core.state import calls_db
    if call_id not in calls_db:
        calls_db[call_id] = {}
        
    calls_db[call_id]["recording_url"] = record_url
    logger.info("Stored recording_url in calls_db for call_id={}: {}", call_id, record_url)
    return {"ok": True, "call_id": call_id}


@router.get("/api/call/{call_id}/recording")
async def get_call_recording_stream(call_id: str):
    """
    Streams call recording back to consumer with Vobiz auth credentials attached.
    """
    from core.state import calls_db
    from services.vobiz_bridge.vobiz_recording import lookup_vobiz_call_mapping, _api_list_by_call_uuid
    from core.vobiz_credentials import resolve_vobiz_credentials
    from fastapi.responses import StreamingResponse
    from fastapi import HTTPException
    import httpx
    
    call_id = call_id.strip()
    # 1. Lookup in calls_db cache
    recording_url = calls_db.get(call_id, {}).get("recording_url")
    
    # 2. Get call mapping for role credentials
    mapping = lookup_vobiz_call_mapping(call_id)
    role = mapping.get("role") or "sales_1"
    auth_id, auth_token, _, _ = resolve_vobiz_credentials(role)
    
    # 3. Fallback to API lookup if cache is empty
    if not recording_url:
        logger.info("Recording URL not cached for call_id={}, polling Vobiz API...", call_id)
        try:
            listed = await _api_list_by_call_uuid(auth_id, auth_token, call_id)
            if listed and listed.get("recording_url"):
                recording_url = listed["recording_url"]
                if call_id not in calls_db:
                    calls_db[call_id] = {}
                calls_db[call_id]["recording_url"] = recording_url
        except Exception as exc:
            logger.warning("Vobiz API recording poll failed for call_id={}: {}", call_id, exc)
            
    if not recording_url:
        raise HTTPException(status_code=404, detail="Recording URL not found or not yet available.")
        
    # 4. Stream back to consumer with credentials attached
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
    }
    
    client = httpx.AsyncClient()
    
    async def stream_generator():
        try:
            async with client.stream("GET", recording_url, headers=headers, timeout=120.0) as r:
                r.raise_for_status()
                async for chunk in r.iter_bytes():
                    yield chunk
        except Exception as exc:
            logger.error("Error streaming Vobiz recording for call_id={}: {}", call_id, exc)
        finally:
            await client.aclose()
            
    return StreamingResponse(
        stream_generator(),
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="call_{call_id}.mp3"'
        }
    )
