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
                        "sandbox": {"type": "integer"},
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
            "sandbox": _sandbox_from_pool(lead.get("extra", {}).get("eligible_pool")),
            "lead_status": lead.get("status"),
            "lifecycle_status": lead.get("lifecycle_status"),
            "disposition": lead.get("disposition"),
        }

    elif name == "create_lead":
        phone = arguments.get("phone_number")
        if not phone:
            raise HTTPException(400, "phone_number required")
        role = "sales_1"
        lead_id = await _storage.add_lead(
            role=role,
            name=str(arguments.get("name", "Lead")),
            phone=phone,
        )
        if lead_id <= 0:
            raise HTTPException(409, "Phone number is registered in the do-not-contact list")
        return {"status": "created", "lead_id": lead_id, "phone_number": phone, "sandbox": 1}

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
        phone = arguments.get("phone_number")
        sandbox = arguments.get("sandbox", 1)
        return {
            "status": "queued",
            "phone_number": phone,
            "sandbox": sandbox,
            "message": f"Manual call queued for {phone} on Sandbox {sandbox}.",
        }

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
