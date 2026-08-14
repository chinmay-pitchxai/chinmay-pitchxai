"""Prevents double-dial: scheduled_callbacks vs promote_due conflict."""

from __future__ import annotations

import asyncio

from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)


class SchedulingHealthAgent(BaseHealthAgent):
    agent_id = "schedule_harmonizer"
    agent_name = "Schedule Harmonizer"
    domain = "scheduling"
    interval_sec = 50.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> AgentReport:
        from core.storage import _get_conn

        conn = _get_conn()
        findings: list[AgentFinding] = []

        conflict = conn.execute(
            """
            SELECT COUNT(*) AS c FROM leads l
            WHERE l.status = 'pending'
              AND EXISTS (
                SELECT 1 FROM scheduled_callbacks sc
                WHERE sc.lead_id = l.id
                  AND sc.status IN ('scheduled','queued','calling')
              )
            """
        ).fetchone()
        if conflict and int(conflict["c"]) > 0:
            findings.append(AgentFinding(
                "double_queue",
                f"{conflict['c']} lead(s) pending while also in scheduled_callbacks",
                auto_healable=True,
            ))

        cb_pending = conn.execute(
            """
            SELECT COUNT(*) AS c FROM leads
            WHERE status = 'callback_scheduled'
              AND EXISTS (
                SELECT 1 FROM scheduled_callbacks sc
                WHERE sc.lead_id = leads.id
                  AND sc.status IN ('scheduled','queued','calling')
              )
            """
        ).fetchone()
        if cb_pending and int(cb_pending["c"]) > 0:
            findings.append(AgentFinding(
                "dual_callback_state",
                f"{cb_pending['c']} lead(s) in callback_scheduled with active scheduled_callbacks row",
                auto_healable=True,
            ))

        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings)

    async def heal(self, report: AgentReport) -> AgentHealResult:
        return await asyncio.to_thread(self._heal_sync, report)

    def _heal_sync(self, report: AgentReport) -> AgentHealResult:
        from core.storage import _get_conn, _invalidate_state_cache

        actions: list[HealAction] = []
        conn = _get_conn()
        healed = False

        # Leads with active scheduled_callbacks should NOT be pending (campaign would double-dial)
        cur = conn.execute(
            """
            UPDATE leads SET status = 'callback_scheduled', updated_at = datetime('now')
            WHERE status = 'pending'
              AND id IN (
                SELECT lead_id FROM scheduled_callbacks
                WHERE lead_id IS NOT NULL
                  AND status IN ('scheduled','queued','calling')
              )
            """
        )
        n = int(cur.rowcount or 0)
        if n:
            conn.commit()
            _invalidate_state_cache()
            actions.append(HealAction("fix_pending_conflict", True, f"Moved {n} lead(s) to callback_scheduled"))
            healed = True

        return AgentHealResult(agent_id=self.agent_id, healed=healed, actions=actions)
