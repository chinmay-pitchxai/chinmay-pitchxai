"""Super Boss — parent supervisor that monitors all health agents and cleans the VPS."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

from loguru import logger

from services.health_agents.agents import ALL_AGENTS
from services.health_agents.base import AgentHealth
from services.health_agents.coordinator import get_agent_status
from services.supervisor.cleanup import run_boss_cleanup_sweep
from services.supervisor.prompts import (
    BOSS_DECISION_LABELS,
    SUPER_BOSS_DOMAIN,
    SUPER_BOSS_ID,
    SUPER_BOSS_NAME,
    SUPER_BOSS_SYSTEM_PROMPT,
)

_BOSS_STATE: dict[str, Any] = {
    "agent_id": SUPER_BOSS_ID,
    "agent_name": SUPER_BOSS_NAME,
    "domain": SUPER_BOSS_DOMAIN,
    "health": AgentHealth.OK.value,
    "issue_count": 0,
    "children_monitored": 0,
    "children_critical": 0,
    "children_warn": 0,
    "last_decision": "initializing",
    "last_decision_detail": BOSS_DECISION_LABELS["all_clear"],
    "last_cycle_at": 0.0,
    "last_cleanup_at": 0.0,
    "last_infra_check_at": 0.0,
    "last_backup_at": 0.0,
    "decisions_log": [],
    "cleanup_summary": {},
    "system_prompt_excerpt": SUPER_BOSS_SYSTEM_PROMPT[:280] + "…",
}
_BOSS_TASK: Optional[asyncio.Task] = None


def _boss_enabled() -> bool:
    return os.getenv("SUPER_BOSS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _boss_interval() -> float:
    try:
        return max(20.0, float(os.getenv("SUPER_BOSS_INTERVAL_SEC", "45")))
    except ValueError:
        return 45.0


def get_super_boss_status() -> dict[str, Any]:
    return dict(_BOSS_STATE)


def _log_decision(code: str, detail: str) -> None:
    entry = {"code": code, "detail": detail, "at": time.time()}
    log = list(_BOSS_STATE.get("decisions_log") or [])
    log.append(entry)
    _BOSS_STATE["decisions_log"] = log[-20:]
    _BOSS_STATE["last_decision"] = code
    _BOSS_STATE["last_decision_detail"] = detail
    logger.info("Super Boss [{}]: {}", code, detail)


async def _force_heal_children(child_states: list[dict[str, Any]]) -> int:
    """Boss orders child agents to heal when they reported auto-healable issues."""
    healed = 0
    agent_map = {a.agent_id: a for a in ALL_AGENTS}
    for st in child_states:
        if st.get("health") == AgentHealth.OK.value:
            continue
        findings = st.get("findings") or []
        if not any(f.get("auto_healable") for f in findings):
            continue
        agent = agent_map.get(st.get("agent_id"))
        if not agent:
            continue
        try:
            from services.health_agents.base import AgentFinding, AgentReport

            rebuilt_findings = [
                AgentFinding(
                    code=f.get("code", "unknown"),
                    message=f.get("message", ""),
                    detail=f.get("detail", ""),
                    auto_healable=bool(f.get("auto_healable")),
                )
                for f in findings
            ]
            report = AgentReport(
                agent_id=agent.agent_id,
                agent_name=agent.agent_name,
                domain=agent.domain,
                health=AgentHealth(st.get("health", "warn")),
                findings=rebuilt_findings,
                checked_at=st.get("checked_at") or time.time(),
                metadata=st.get("metadata") or {},
            )
            result = await agent.heal(report)
            if result.healed:
                healed += 1
        except Exception as e:
            logger.warning("Super Boss: heal order failed for {}: {}", st.get("agent_id"), e)
    if healed:
        _log_decision("child_heal", f"Boss ordered heal on {healed} child agent(s).")
    return healed


async def _restart_stalled_workers() -> int:
    """Restart campaign workers that stalled with pending leads."""
    from core.state import _CAMPAIGN_TASKS, _LAST_WORKER_ACTIVITY, _MANUALLY_STOPPED_ROLES
    from core.storage import get_lead_counts, set_campaign_want_running
    from core.worker import release_orphaned_dialing_leads, _campaign_worker_role

    restarted = 0
    now = time.time()
    stall_sec = float(os.getenv("CAMPAIGN_STALL_WATCHDOG_SEC", "600"))
    for role in ("sales_1",):
        if role in _MANUALLY_STOPPED_ROLES:
            continue
        task = _CAMPAIGN_TASKS.get(role)
        if not task or task.done():
            continue
        last = _LAST_WORKER_ACTIVITY.get(role, 0.0)
        if (now - last) <= stall_sec:
            continue
        counts = await get_lead_counts(role)
        pending = int(counts.get("pending", 0) or 0)
        if pending <= 0:
            continue
        logger.warning(
            "Super Boss: restarting stalled worker role={} (silent {}s, pending={})",
            role, int(now - last), pending,
        )
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except BaseException:
            pass
        await release_orphaned_dialing_leads(
            role,
            to_status="pending",
            error="Boss restart: stalled worker recovered.",
        )
        await set_campaign_want_running(role, True)
        _CAMPAIGN_TASKS[role] = asyncio.create_task(_campaign_worker_role(role))
        restarted += 1
    if restarted:
        _log_decision("worker_restart", f"Restarted {restarted} stalled campaign worker(s).")
    return restarted


async def _infra_watchdog() -> None:
    """Gemini / Vobiz health — boss-level escalation (same checks as Super CEO)."""
    from core.storage import set_campaign_globally_paused
    from core.worker import (
        check_gemini_health,
        check_recent_failure_rate_sync,
        check_vobiz_health,
        send_super_ceo_alert,
    )

    gemini = await check_gemini_health()
    if gemini.get("status") != "ok":
        await set_campaign_globally_paused(True)
        send_super_ceo_alert(
            "Super Boss: Gemini API Failure",
            f"Gemini health check failed.\nStatus: {gemini}\nAll campaigns paused by Super Boss.",
        )
        _log_decision("global_pause", "Gemini API unhealthy — campaigns paused.")
        _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
        return

    for role in ("sales_1",):
        vobiz = await check_vobiz_health(role)
        if vobiz.get("status") == "unauthorized":
            await set_campaign_globally_paused(True)
            send_super_ceo_alert(
                "Super Boss: Vobiz Unauthorized",
                f"Vobiz 401 for role={role}. Campaigns paused by Super Boss.",
            )
            _log_decision("global_pause", f"Vobiz unauthorized for {role}.")
            _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
            return
        if (
            vobiz.get("status") == "ok"
            and vobiz.get("balance_known")
            and float(vobiz.get("balance") or 0) < 5.0
        ):
            await set_campaign_globally_paused(True)
            send_super_ceo_alert(
                "Super Boss: Low Vobiz Balance",
                f"Role={role} balance {vobiz.get('balance'):.2f} credits. Campaigns paused.",
            )
            _log_decision("global_pause", f"Low Vobiz balance on {role}.")
            _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
            return

        fail = await asyncio.to_thread(check_recent_failure_rate_sync, role)
        if fail["consecutive_failures"] >= 5:
            await set_campaign_globally_paused(True)
            send_super_ceo_alert(
                "Super Boss: Consecutive Telephony Failures",
                f"Role={role}: {fail['consecutive_failures']} consecutive Vobiz/API failures. Campaigns paused.",
            )
            _log_decision("global_pause", f"Consecutive infra failures on {role}.")
            _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
            return
        infra_fails = fail.get("infra_failures", 0)
        if fail["total_calls"] >= 15 and infra_fails >= 10:
            await set_campaign_globally_paused(True)
            send_super_ceo_alert(
                "Super Boss: High Telephony Failure Rate",
                f"Role={role}: {infra_fails} infra failures in 30m. Campaigns paused.",
            )
            _log_decision("global_pause", f"High infra failure rate on {role}.")
            _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
            return

    # All infra checks passed — clear stale global pause from transient Gemini/Vobiz blips
    from core.storage import is_campaign_globally_paused

    if await is_campaign_globally_paused():
        await set_campaign_globally_paused(False)
        _log_decision("all_clear", "Infra healthy — campaigns unpaused.")
        # #region agent log
        try:
            from debug_agent_log import agent_debug

            agent_debug(
                "H2",
                "super_boss.py:_infra_watchdog",
                "campaigns_unpaused_infra_ok",
                {"gemini_model": gemini.get("model")},
            )
        except Exception:
            pass
        # #endregion


async def _boss_cycle() -> None:
    global _BOSS_STATE
    now = time.time()
    _BOSS_STATE["health"] = AgentHealth.OK.value
    status = get_agent_status()
    children = status.get("agents") or []
    critical = sum(1 for c in children if c.get("health") == AgentHealth.CRITICAL.value)
    warn = sum(1 for c in children if c.get("health") == AgentHealth.WARN.value)

    _BOSS_STATE["children_monitored"] = len(children)
    _BOSS_STATE["children_critical"] = critical
    _BOSS_STATE["children_warn"] = warn
    _BOSS_STATE["last_cycle_at"] = now

    if critical > 0:
        _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
        _BOSS_STATE["issue_count"] = critical
    elif warn > 0:
        _BOSS_STATE["health"] = AgentHealth.WARN.value
        _BOSS_STATE["issue_count"] = warn
    else:
        _BOSS_STATE["health"] = AgentHealth.OK.value
        _BOSS_STATE["issue_count"] = 0

    from core.state import total_active_vobiz_calls
    active_calls = total_active_vobiz_calls()

    if critical > 0 or warn > 0:
        from services.supervisor.panther_mode import is_panther_mode_active
        if is_panther_mode_active():
            await _force_heal_children(children)
        elif active_calls > 0:
            _log_decision("deferred", f"Child issues present but {active_calls} active call(s) — heal deferred.")
        else:
            await _force_heal_children(children)

    await _restart_stalled_workers()

    # Cleanup every 5 minutes
    if now - float(_BOSS_STATE.get("last_cleanup_at") or 0) >= 300:
        app_root = os.getcwd()
        summary = await run_boss_cleanup_sweep(app_root=app_root)
        _BOSS_STATE["last_cleanup_at"] = now
        _BOSS_STATE["cleanup_summary"] = summary
        if any(
            summary.get(k, {}).get("count", 0) or summary.get(k, {}).get("released", 0) or summary.get(k, {}).get("duplicate_callbacks_removed", 0)
            for k in ("temp", "orphaned_dialing", "callback_dedup")
        ):
            _log_decision("boss_cleanup", "Boss cleanup sweep removed stale VPS clutter.")

    # Infra watchdog every 10 minutes
    if now - float(_BOSS_STATE.get("last_infra_check_at") or 0) >= 600:
        _BOSS_STATE["last_infra_check_at"] = now
        try:
            await _infra_watchdog()
        except Exception as e:
            logger.warning("Super Boss infra watchdog failed: {}", e)

    # Hourly backup
    if now - float(_BOSS_STATE.get("last_backup_at") or 0) >= 3600:
        _BOSS_STATE["last_backup_at"] = now
        try:
            from core.worker import run_sqlite_backup, run_wal_checkpoint
            await asyncio.to_thread(run_sqlite_backup)
            await asyncio.to_thread(run_wal_checkpoint)
        except Exception as e:
            logger.warning("Super Boss backup failed: {}", e)

    if _BOSS_STATE["health"] == AgentHealth.OK.value and not _BOSS_STATE.get("last_decision"):
        _log_decision("all_clear", BOSS_DECISION_LABELS["all_clear"])
    elif _BOSS_STATE["health"] == AgentHealth.OK.value and critical == 0 and warn == 0:
        if (_BOSS_STATE.get("last_decision") or "") not in ("all_clear", "boss_cleanup", "child_heal", "worker_restart"):
            _log_decision("all_clear", BOSS_DECISION_LABELS["all_clear"])


async def _boss_loop() -> None:
    logger.info(
        "Super Boss parent supervisor started (interval={}s). Monitoring {} child agents.",
        _boss_interval(),
        len(ALL_AGENTS),
    )
    _log_decision("all_clear", "Super Boss online — monitoring all child agents.")
    while True:
        try:
            await _boss_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Super Boss cycle error: {}", e)
            _BOSS_STATE["health"] = AgentHealth.CRITICAL.value
            _BOSS_STATE["issue_count"] = 1
        await asyncio.sleep(_boss_interval())


async def start_super_boss() -> Optional[asyncio.Task]:
    global _BOSS_TASK
    if not _boss_enabled():
        logger.info("Super Boss disabled (SUPER_BOSS_ENABLED=false)")
        return None
    if _BOSS_TASK and not _BOSS_TASK.done():
        return _BOSS_TASK
    _BOSS_TASK = asyncio.create_task(_boss_loop())
    return _BOSS_TASK


async def stop_super_boss() -> None:
    global _BOSS_TASK
    if _BOSS_TASK and not _BOSS_TASK.done():
        _BOSS_TASK.cancel()
        try:
            await _BOSS_TASK
        except asyncio.CancelledError:
            pass
    _BOSS_TASK = None
    logger.info("Super Boss stopped")
