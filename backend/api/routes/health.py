"""`GET /health` — process summary + self-healing agent status."""

from __future__ import annotations

from fastapi import APIRouter, Request

from core.state import _ACTIVE_VOBIZ_CALLS, _CAMPAIGN_TASKS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    from config import settings

    active_campaigns = sum(1 for t in _CAMPAIGN_TASKS.values() if t and not t.done())
    agents = {}
    panther = {}
    try:
        from services.health_agents import get_agent_status as _gas
        agents = _gas()
    except Exception:
        agents = {"overall_health": "ok", "agents": []}
    try:
        from services.supervisor import get_panther_status as _gps
        panther = _gps()
    except Exception:
        panther = {"enabled": False}
    local_url = (settings.server_url or f"http://127.0.0.1:{settings.port}").rstrip("/")
    return {
        "status": "healthy" if agents.get("overall_health") == "ok" else agents.get("overall_health", "healthy"),
        "mode": "bridge",
        "server_url": local_url,
        "port": settings.port,
        "host": settings.host,
        "vobiz_public_base_url": (settings.vobiz_public_base_url or local_url).rstrip("/"),
        "active_calls": _ACTIVE_VOBIZ_CALLS,
        "active_campaigns": active_campaigns,
        "agents": agents,
        "panther": panther,
    }


@router.get("/health/agents")
async def health_agents():
    """Detailed status of all self-healing monitor agents."""
    data = {}
    try:
        from services.health_agents import get_agent_status as _gas
        data = _gas()
    except Exception:
        data = {"agents": []}
    try:
        from services.supervisor import get_panther_status as _gps
        data["panther"] = _gps()
    except Exception:
        data["panther"] = {"enabled": False}
    return data


@router.post("/health/panther")
async def panther_auto_fix(request: Request):
    """One-signal full auto-fix — codeword: Panther. Runs all agent skills + Super Boss."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    triggered_by = str(body.get("triggered_by") or "dashboard")
    try:
        from services.supervisor import trigger_panther_mode as _tpm
        result = await _tpm(triggered_by=triggered_by)
    except Exception:
        result = {"ok": False, "error": "Panther mode not available"}
    return result
