"""MCP Server Wrapper for Technopolis AI Real Estate Calling Pipeline.

Exposes REST and internal workflow endpoints as standard Model Context Protocol (MCP) tools.
Mounted into the main FastAPI app via ``api.app``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import storage as _storage

router = APIRouter(prefix="/mcp", tags=["mcp"])


class CreateLeadRequest(BaseModel):
    phone_number: str
    name: str = "Lead"
    source: str = "cold"
    budget: str | None = None
    preferred_location: str | None = None
    property_type: str | None = None
    sandbox: int = 1


class ManualCallRequest(BaseModel):
    phone_number: str
    sandbox: int = 1


@router.get("/tools")
def list_mcp_tools() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "get_lead_status",
                "description": "Fetch current sandbox, status, and latest outcome for a lead by phone number.",
                "parameters": {
                    "type": "object",
                    "properties": {"phone_number": {"type": "string"}},
                    "required": ["phone_number"],
                },
            },
            {
                "name": "create_lead",
                "description": "Inject a new lead into Sandbox 1 or specified sandbox queue.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string"},
                        "name": {"type": "string"},
                        "source": {"type": "string"},
                        "budget": {"type": "string"},
                        "preferred_location": {"type": "string"},
                        "property_type": {"type": "string"},
                        "sandbox": {"type": "integer"},
                    },
                    "required": ["phone_number"],
                },
            },
            {
                "name": "get_call_history",
                "description": "Retrieve rolling memory, call history, and transcript summaries for a lead.",
                "parameters": {
                    "type": "object",
                    "properties": {"phone_number": {"type": "string"}},
                    "required": ["phone_number"],
                },
            },
            {
                "name": "trigger_manual_call",
                "description": "Trigger an immediate manual call override for a lead number on a given sandbox line.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string"},
                        "role": {"type": "string", "default": "sales_1"},
                        "source": {"type": "string", "default": "campaign"},
                    },
                    "required": ["phone_number"],
                },
            },
            {
                "name": "get_weekly_report",
                "description": "Fetch latest weekly audio archive metadata and performance stats.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    }


def _sandbox_from_pool(pool_name: str) -> int:
    lowered = (pool_name or "").lower()
    if "sandbox1" in lowered or "callback" in lowered:
        return 1
    if "sandbox2" in lowered:
        return 2
    if "sandbox3" in lowered or "nurture" in lowered:
        return 3
    if "sandbox4" in lowered or "feedback" in lowered:
        return 4
    return 1


def _lead_extra_dict(lead: dict) -> dict:
    """``leads.extra`` is a JSON string in the DB — tolerate both shapes."""
    raw = lead.get("extra")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _lead_sandbox(lead: dict) -> int:
    """Authoritative sandbox for a lead: explicit column > lifecycle stage."""
    extra = _lead_extra_dict(lead)
    try:
        sb = int(extra.get("sandbox") or 0)
        if 1 <= sb <= 4:
            return sb
    except (TypeError, ValueError):
        pass
    try:
        from core.workflow_models import sandbox_for_stage

        return int(sandbox_for_stage(str(lead.get("lifecycle_status") or "new")))
    except Exception:
        return 1


def _queue_manual_call_sync(phone: str, role: str, source: str) -> dict[str, Any]:
    import time
    from datetime import datetime, timezone

    from core.phone_norm import norm_phone_str
    from core.dnc import is_phone_blocked
    from core.orchestration_service import schedule_job
    from core.workflow_models import JobType

    normalized = norm_phone_str(phone)
    if not normalized:
        return {"status": "error", "detail": "invalid phone_number"}
    if is_phone_blocked(normalized):
        return {"status": "blocked", "phone_number": normalized, "detail": "number is on the do-not-contact list"}
    # TRAI register (do_not_contact table) — keyed by last 10 digits like
    # orchestration_service.opt_out. The operator dnc_list above is separate.
    try:
        _digits = "".join(ch for ch in normalized if ch.isdigit())
        _dnc_key = _digits[-10:] if len(_digits) >= 10 else _digits
        if _storage._get_conn().execute(
            "SELECT 1 FROM do_not_contact WHERE normalized_phone=?", (_dnc_key,)
        ).fetchone():
            return {"status": "blocked", "phone_number": normalized, "detail": "number is on the do-not-contact list"}
    except Exception:
        pass
    try:
        conn = _storage._get_conn()
        lead = _storage._find_lead_by_phone_any_role_sync(normalized)
        if lead:
            lead_id = int(lead["id"])
        else:
            lead_id = _storage._add_lead_sync(role=role, name="Lead", phone=normalized)
            if lead_id <= 0:
                return {"status": "blocked", "phone_number": normalized, "detail": "number is on the do-not-contact list"}
        job_key = f"mcp-manual:{lead_id}:{int(time.time())}"
        schedule_job(
            conn,
            lead_id=lead_id,
            job_type=JobType.FRESH_CALL,
            source=source,
            due_at=datetime.now(timezone.utc),
            key=job_key,
            attempt=1,
            source_type="mcp",
            source_id=f"mcp:{normalized}",
            payload={"manual": True, "role": role},
        )
        return {
            "status": "enqueued",
            "lead_id": lead_id,
            "phone_number": normalized,
            "job_key": job_key,
        }
    except PermissionError:
        return {"status": "blocked", "phone_number": normalized, "detail": "lead is opted out"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.post("/call_tool")
async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_lead_status":
        phone = arguments.get("phone_number")
        if not phone:
            raise HTTPException(400, "phone_number required")
        lead = await _storage.find_lead_by_phone_any_role(phone)
        if not lead:
            return {"status": "not_found", "phone_number": phone}
        return {
            "status": "found",
            "lead_id": lead.get("id"),
            "name": lead.get("name"),
            "role": lead.get("role"),
            "sandbox": _lead_sandbox(lead),
            "current_sandbox": _lead_sandbox(lead),
            "lead_status": lead.get("status"),
            "lifecycle_status": lead.get("lifecycle_status"),
            "disposition": lead.get("disposition"),
        }

    elif name == "create_lead":
        phone = arguments.get("phone_number")
        if not phone:
            raise HTTPException(400, "phone_number required")
        from core.phone_norm import norm_phone_str as _norm_mcp

        normalized = _norm_mcp(phone)
        if not normalized:
            raise HTTPException(400, "Invalid phone_number — could not normalize to E.164.")
        role = "sales_1"
        source = str(arguments.get("source", "cold") or "cold").strip().lower()
        if source == "digital_marketing":
            source = "digital"
        if source not in ("cold", "digital"):
            source = "cold"
        name = str(arguments.get("name", "Lead") or "Lead").strip()[:200] or "Lead"
        budget = str(arguments.get("budget") or "").strip()
        location = str(arguments.get("preferred_location") or "").strip()
        ptype = str(arguments.get("property_type") or "").strip()
        try:
            sandbox_arg = int(arguments.get("sandbox") or 1)
        except (TypeError, ValueError):
            sandbox_arg = 1
        # Leads may only enter through Sandbox 1 (outcome-driven downstream).
        sandbox_arg = 1 if sandbox_arg not in (1, 2, 3, 4) else sandbox_arg
        from core.storage import _bulk_add_leads_sync, _get_conn

        extra: dict[str, str] = {}
        if budget:
            extra["budget"] = budget
        if location:
            extra["preferred_location"] = location
        if ptype:
            extra["property_type"] = ptype
        extra["mcp_source"] = "create_lead"
        saved, _dupes, dnc_blocked = await asyncio.to_thread(
            _bulk_add_leads_sync, role, [{
                "name": name, "phone": normalized, "source": source,
                "sandbox": sandbox_arg, "extra": extra,
            }]
        )
        if dnc_blocked:
            raise HTTPException(409, "Phone number is registered in the do-not-contact list")
        conn = _get_conn()
        row = conn.execute(
            "SELECT id FROM leads WHERE role=? AND phone=? ORDER BY id DESC LIMIT 1",
            (role, normalized),
        ).fetchone()
        if not row:
            raise HTTPException(500, "Lead was not persisted")
        lead_id = int(row[0])
        # Plan Section 1.2: create_lead injects into the Sandbox 1 queue — the
        # lead must receive a fresh_call job or the autonomous dispatcher will
        # never dial it.
        from datetime import datetime, timezone
        from core.orchestration_service import schedule_job
        from core.workflow_models import JobType

        try:
            schedule_job(
                conn,
                lead_id=lead_id,
                job_type=JobType.FRESH_CALL,
                source=source,
                due_at=datetime.now(timezone.utc),
                key=f"mcp-create:{normalized}",
                attempt=1,
                source_type="mcp",
                source_id="create_lead",
                payload={"sub_sandbox": "1.2" if source == "digital" else "1.1"},
            )
        except PermissionError:
            raise HTTPException(409, "Phone number is registered in the do-not-contact list")
        return {
            "status": "created",
            "lead_id": lead_id,
            "phone_number": normalized,
            "source": source,
            "sandbox": 1,
            "job_enqueued": "fresh_call",
        }

    elif name == "get_call_history":
        phone = arguments.get("phone_number")
        if not phone:
            raise HTTPException(400, "phone_number required")
        lead = await _storage.find_lead_by_phone_any_role(phone)
        if not lead:
            return {"status": "not_found", "phone_number": phone}
        lead_id = int(lead["id"])
        attempts = await _storage.get_call_attempts(lead_id)
        from core import lead_memory as _lm
        memory = ""
        try:
            memory = _lm.memory_context(_storage._get_conn(), lead_id)
        except Exception:
            memory = ""
        return {
            "status": "found",
            "phone_number": phone,
            "lead_id": lead_id,
            "call_count": len(attempts),
            "memory": memory,
            "attempts": attempts,
        }

    elif name == "trigger_manual_call":
        phone = str(arguments.get("phone_number", "") or "").strip()
        if not phone:
            return {"status": "error", "detail": "phone_number required"}
        role = str(arguments.get("role", "sales_1") or "sales_1")
        source = str(arguments.get("source", "campaign") or "campaign").lower()
        return await asyncio.to_thread(_queue_manual_call_sync, phone, role, source)

    elif name == "get_weekly_report":
        backend_dir = Path(__file__).resolve().parent
        exports_dir = backend_dir / "data" / "exports"
        archives = sorted(exports_dir.glob("*.zip")) if exports_dir.is_dir() else []
        return {
            "status": "success",
            "archive_count": len(archives),
            "latest_archive": archives[-1].name if archives else None,
        }

    raise HTTPException(404, f"Tool {name} not found")
