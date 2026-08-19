"""Orchestrates all health agents — check + safe auto-heal loop."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any, Optional

from loguru import logger

from config import settings
from core.state import total_active_vobiz_calls
from services.health_agents.agents import ALL_AGENTS
from services.health_agents.base import AgentHealth, AgentHealResult, AgentReport, BaseHealthAgent

# In-memory status for dashboard / API
_AGENT_STATE: dict[str, dict[str, Any]] = {}
_COORDINATOR_TASK: Optional[asyncio.Task] = None
_LAST_CYCLE_AT: float = 0.0


def get_agent_status() -> dict[str, Any]:
    overall = AgentHealth.OK.value
    for st in _AGENT_STATE.values():
        h = st.get("health", "ok")
        if h == AgentHealth.CRITICAL.value:
            overall = AgentHealth.CRITICAL.value
            break
        if h == AgentHealth.WARN.value and overall == AgentHealth.OK.value:
            overall = AgentHealth.WARN.value
    return {
        "enabled": _agents_enabled(),
        "overall_health": overall,
        "active_calls": total_active_vobiz_calls(),
        "last_cycle_at": _LAST_CYCLE_AT,
        "agents": list(_AGENT_STATE.values()),
    }


def _agents_enabled() -> bool:
    import os
    return os.getenv("HEALTH_AGENTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _cycle_interval() -> float:
    import os
    try:
        return max(15.0, float(os.getenv("HEALTH_AGENTS_INTERVAL_SEC", "30")))
    except ValueError:
        return 30.0


async def _run_agent(agent: BaseHealthAgent) -> None:
    try:
        report = await agent.check()
        heal_result: Optional[AgentHealResult] = None
        should_heal = report.health != AgentHealth.OK and any(f.auto_healable for f in report.findings)
        if should_heal:
            if agent.heal_during_active_calls or total_active_vobiz_calls() == 0:
                heal_result = await agent.heal(report)
                if heal_result.healed:
                    report = await agent.check()
            else:
                logger.debug("Agent {}: heal deferred (active calls)", agent.agent_id)
        _AGENT_STATE[agent.agent_id] = {
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_name,
            "domain": agent.domain,
            "health": report.health.value,
            "issue_count": report.issue_count,
            "findings": [asdict(f) for f in report.findings],
            "checked_at": report.checked_at,
            "metadata": report.metadata,
            "last_heal": asdict(heal_result) if heal_result else None,
        }
        if report.health != AgentHealth.OK:
            logger.info(
                "Health agent {} [{}]: {} issue(s) — {}",
                agent.agent_name,
                report.health.value,
                report.issue_count,
                "; ".join(f.message for f in report.findings[:3]),
            )
        if heal_result and heal_result.healed:
            logger.info(
                "Health agent {} auto-healed: {}",
                agent.agent_name,
                "; ".join(a.message for a in heal_result.actions if a.success),
            )
    except Exception as e:
        logger.exception("Health agent {} failed: {}", agent.agent_id, e)
        _AGENT_STATE[agent.agent_id] = {
            "agent_id": agent.agent_id,
            "agent_name": agent.agent_name,
            "domain": agent.domain,
            "health": AgentHealth.CRITICAL.value,
            "issue_count": 1,
            "findings": [{"code": "agent_error", "message": str(e), "detail": "", "auto_healable": False}],
            "checked_at": time.time(),
            "metadata": {},
            "last_heal": None,
        }


async def _coordinator_loop() -> None:
    global _LAST_CYCLE_AT
    logger.info(
        "Health agent coordinator started ({} agents, cycle={}s)",
        len(ALL_AGENTS),
        _cycle_interval(),
    )
    # Stagger first run
    next_run: dict[str, float] = {a.agent_id: 0.0 for a in ALL_AGENTS}
    while True:
        try:
            now = time.time()
            for agent in ALL_AGENTS:
                if now >= next_run.get(agent.agent_id, 0):
                    await _run_agent(agent)
                    next_run[agent.agent_id] = now + agent.interval_sec
            _LAST_CYCLE_AT = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Health coordinator cycle error: {}", e)
        await asyncio.sleep(_cycle_interval())


async def start_health_agents() -> Optional[asyncio.Task]:
    global _COORDINATOR_TASK
    if not _agents_enabled():
        logger.info("Health agents disabled (HEALTH_AGENTS_ENABLED=false)")
        return None
    if _COORDINATOR_TASK and not _COORDINATOR_TASK.done():
        return _COORDINATOR_TASK
    _COORDINATOR_TASK = asyncio.create_task(_coordinator_loop())
    return _COORDINATOR_TASK


async def stop_health_agents() -> None:
    global _COORDINATOR_TASK
    if _COORDINATOR_TASK and not _COORDINATOR_TASK.done():
        _COORDINATOR_TASK.cancel()
        try:
            await _COORDINATOR_TASK
        except asyncio.CancelledError:
            pass
    _COORDINATOR_TASK = None
    logger.info("Health agent coordinator stopped")
