"""Panther Mode — one-signal full auto-fix across all health agents + Super Boss.

Say the codeword (default: Panther) in chat or POST /health/panther to trigger:
  1. Force-check every health agent
  2. Force-heal all auto-healable issues (even during active calls)
  3. Super Boss cleanup, stalled worker restart, callback promotion
  4. Re-verify until clear or report remaining manual items
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict
from typing import Any, Optional

from loguru import logger

from services.health_agents.agents import ALL_AGENTS
from services.health_agents.base import AgentHealth, AgentHealResult, AgentReport, BaseHealthAgent

# Skill registry — what each agent domain can auto-fix (for dashboard + logs)
PANTHER_SKILL_REGISTRY: dict[str, dict[str, Any]] = {
    "config_guardian": {
        "skills": ["env_validation", "concurrency_cap_check", "vobiz_credential_probe"],
        "fixes": ["misconfigured env flags", "MAX_CONCURRENT drift"],
    },
    "concurrency_sentinel": {
        "skills": ["slot_leak_detection", "orphan_slot_release"],
        "fixes": ["stuck dialer slots", "phantom active call counters"],
    },
    "callback_router": {
        "skills": ["outbound_phone_backfill", "callback_dedup", "due_callback_promote"],
        "fixes": ["missing outbound on callbacks", "duplicate scheduled callbacks"],
    },
    "rag_keeper": {
        "skills": ["rag_db_health", "knowledge_index_check"],
        "fixes": ["RAG database accessibility"],
    },
    "media_curator": {
        "skills": ["whatsapp_media_manifest", "email_attachment_check"],
        "fixes": ["missing brochure/videos/PDFs on server"],
    },
    "campaign_medic": {
        "skills": ["stalled_worker_detect", "orphan_dialing_release", "queue_health"],
        "fixes": ["stalled campaign workers", "leads stuck in dialing"],
    },
    "schedule_harmonizer": {
        "skills": ["quiet_hours_align", "callback_schedule_sanity"],
        "fixes": ["overdue callback promotion", "schedule conflicts"],
    },
    "integration_watch": {
        "skills": ["gemini_ping", "vobiz_balance", "whatsapp_api_probe", "smtp_probe"],
        "fixes": ["API connectivity warnings"],
    },
    "smooth_calls_guardian": {
        "skills": [
            "intro_only_greeting_pcm",
            "env_baseline_drift",
            "failed_call_spike_detect",
            "capture_code_regression",
            "greeting_text_strip",
            "pcm_regenerate",
            "turn_taking_env_baseline",
            "repeat_silence_block",
            "stuck_inbound_finalize",
            "interrupt_storm_tuning",
        ],
        "fixes": [
            "wrong name in prerecorded greeting (Suresh/Rohan)",
            "double name-verify",
            "stale greeting PCM",
            "elevated failed call rate",
            "smooth-calls env drift",
            "inbound rows stuck at connected without summary/recording",
            "repeat are-you-still-there loops",
            "false interrupt / truncated explanations",
        ],
    },
    "voice_latency": {
        "skills": ["greeting_barge_in", "vad_tuning", "forward_audio_during_opening", "ultra_low_latency_playout"],
        "fixes": ["call delay during scripted greeting", "150ms turn-taking target", "playout prebuffer removal"],
    },
    "super_boss": {
        "skills": ["child_heal_orchestration", "vps_cleanup", "sqlite_backup", "infra_watchdog"],
        "fixes": ["aggregated child issues", "disk clutter", "stalled pipelines"],
    },
}

_PANTHER_STATE: dict[str, Any] = {
    "active": False,
    "last_triggered_at": 0.0,
    "last_triggered_by": "",
    "last_result": {},
    "codeword": os.getenv("PANTHER_CODEWORD", "Panther").strip() or "Panther",
}

_PANTHER_LOCK = asyncio.Lock()


def panther_codeword() -> str:
    return _PANTHER_STATE.get("codeword") or "Panther"


def is_panther_mode_active() -> bool:
    if not _panther_enabled():
        return False
    if not _PANTHER_STATE.get("active"):
        return False
    # Auto-expire after 15 minutes unless re-triggered
    ttl = float(os.getenv("PANTHER_MODE_TTL_SEC", "900"))
    last = float(_PANTHER_STATE.get("last_triggered_at") or 0)
    return (time.time() - last) < ttl


def get_panther_status() -> dict[str, Any]:
    return {
        **dict(_PANTHER_STATE),
        "enabled": _panther_enabled(),
        "currently_active": is_panther_mode_active(),
        "skill_registry": PANTHER_SKILL_REGISTRY,
    }


def _panther_enabled() -> bool:
    return os.getenv("PANTHER_AUTO_FIX_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def message_is_panther_signal(text: str) -> bool:
    """True if user message is the activation codeword."""
    if not text:
        return False
    word = panther_codeword().lower()
    return word in (text or "").strip().lower().split()


async def _force_run_agent(agent: BaseHealthAgent, *, panther: bool) -> dict[str, Any]:
    report = await agent.check()
    heal_result: Optional[AgentHealResult] = None
    should_heal = report.health != AgentHealth.OK and any(f.auto_healable for f in report.findings)
    if should_heal:
        if panther or agent.heal_during_active_calls:
            heal_result = await agent.heal(report)
            if heal_result.healed:
                report = await agent.check()
    return {
        "agent_id": agent.agent_id,
        "agent_name": agent.agent_name,
        "domain": agent.domain,
        "health_before": report.health.value,
        "health_after": report.health.value,
        "findings": [asdict(f) for f in report.findings],
        "heal": asdict(heal_result) if heal_result else None,
        "skills": PANTHER_SKILL_REGISTRY.get(agent.agent_id, {}).get("skills", []),
    }


async def _panther_boss_actions() -> dict[str, Any]:
    from services.supervisor.super_boss import (
        _force_heal_children,
        _restart_stalled_workers,
        get_super_boss_status,
    )
    from services.health_agents.coordinator import get_agent_status

    actions: dict[str, Any] = {}
    status = get_agent_status()
    children = status.get("agents") or []

    healed = await _force_heal_children(children)
    actions["child_heal_count"] = healed

    restarted = await _restart_stalled_workers()
    actions["workers_restarted"] = restarted

    try:
        from services.supervisor.cleanup import run_boss_cleanup_sweep
        actions["cleanup"] = await run_boss_cleanup_sweep(app_root=os.getcwd())
    except Exception as e:
        actions["cleanup_error"] = str(e)

    try:
        from core.storage import promote_due_scheduled_callbacks
        actions["callbacks_promoted"] = await promote_due_scheduled_callbacks(time.time())
    except Exception as e:
        actions["promote_error"] = str(e)

    try:
        from core.worker import run_sqlite_backup, run_wal_checkpoint
        await asyncio.to_thread(run_sqlite_backup)
        await asyncio.to_thread(run_wal_checkpoint)
        actions["backup"] = "ok"
    except Exception as e:
        actions["backup_error"] = str(e)

    actions["boss_status"] = get_super_boss_status()
    return actions


async def trigger_panther_mode(triggered_by: str = "api") -> dict[str, Any]:
    """Full auto-fix sweep — the one signal."""
    if not _panther_enabled():
        return {"ok": False, "error": "Panther auto-fix disabled (PANTHER_AUTO_FIX_ENABLED=false)"}

    async with _PANTHER_LOCK:
        _PANTHER_STATE["active"] = True
        _PANTHER_STATE["last_triggered_at"] = time.time()
        _PANTHER_STATE["last_triggered_by"] = triggered_by
        logger.warning("🐆 PANTHER MODE activated by {}", triggered_by)

        agent_results: list[dict[str, Any]] = []
        for agent in ALL_AGENTS:
            try:
                agent_results.append(await _force_run_agent(agent, panther=True))
            except Exception as e:
                logger.exception("Panther: agent {} failed", agent.agent_id)
                agent_results.append({
                    "agent_id": agent.agent_id,
                    "error": str(e),
                    "health_after": "critical",
                })

        boss_actions = await _panther_boss_actions()

        # Second pass — re-check after heals
        remaining_issues = 0
        for agent in ALL_AGENTS:
            try:
                report = await agent.check()
                if report.health != AgentHealth.OK:
                    remaining_issues += report.issue_count
            except Exception:
                remaining_issues += 1

        result = {
            "ok": remaining_issues == 0,
            "codeword": panther_codeword(),
            "triggered_by": triggered_by,
            "triggered_at": _PANTHER_STATE["last_triggered_at"],
            "agents_run": len(agent_results),
            "agent_results": agent_results,
            "boss_actions": boss_actions,
            "remaining_issues": remaining_issues,
            "message": (
                "All systems clear — Panther auto-fix complete."
                if remaining_issues == 0
                else f"Panther fixed what it could; {remaining_issues} issue(s) need manual review."
            ),
        }
        _PANTHER_STATE["last_result"] = result

        try:
            from core.events import get_event_bus
            await get_event_bus().publish("panther_complete", result=result)
        except Exception:
            pass

        logger.info("🐆 PANTHER MODE complete: remaining_issues={}", remaining_issues)
        return result


async def panther_background_loop() -> None:
    """Optional: periodic panther-lite when overall health is critical."""
    interval = float(os.getenv("PANTHER_WATCH_INTERVAL_SEC", "0") or "0")
    if interval <= 0:
        return
    while True:
        try:
            from services.health_agents.coordinator import get_agent_status
            status = get_agent_status()
            if status.get("overall_health") == "critical":
                await trigger_panther_mode("auto_watch")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Panther watch loop error: {}", e)
        await asyncio.sleep(interval)
