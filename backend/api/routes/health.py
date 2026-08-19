"""`GET /health` — process summary + self-healing agent status."""

from __future__ import annotations

from fastapi import APIRouter

from core.state import _ACTIVE_VOBIZ_CALLS, _CAMPAIGN_TASKS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    from config import settings

    active_campaigns = sum(1 for t in _CAMPAIGN_TASKS.values() if t and not t.done())
    agents = {}
    try:
        from services.health_agents import get_agent_status as _gas
        agents = _gas()
    except Exception:
        agents = {"overall_health": "ok", "agents": []}
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
    return data
