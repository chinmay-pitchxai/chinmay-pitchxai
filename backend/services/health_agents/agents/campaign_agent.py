"""Recycles stuck dialing leads and orphaned campaign state."""

from __future__ import annotations

import asyncio
import time

from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)

_STALE_DIALING_SEC = 600


class CampaignHealthAgent(BaseHealthAgent):
    agent_id = "campaign_medic"
    agent_name = "Campaign Medic"
    domain = "campaign_pipeline"
    interval_sec = 60.0
    heal_during_active_calls = False  # only heal stale when no active calls on role

    async def check(self) -> AgentReport:
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> AgentReport:
        from core.storage import _get_conn

        conn = _get_conn()
        findings: list[AgentFinding] = []
        cutoff = time.time() - _STALE_DIALING_SEC

        for role in ("sales_1",):
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM leads
                WHERE role = ? AND status = 'dialing'
                  AND start_time IS NOT NULL AND start_time < ?
                """,
                (role, cutoff),
            ).fetchone()
            if row and int(row["c"]) > 0:
                findings.append(AgentFinding(
                    "stale_dialing",
                    f"{row['c']} stale dialing lead(s) in {role}",
                    auto_healable=True,
                ))

        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings)

    async def heal(self, report: AgentReport) -> AgentHealResult:
        from core.state import total_active_vobiz_calls

        if total_active_vobiz_calls() > 0 and not self.heal_during_active_calls:
            return AgentHealResult(
                agent_id=self.agent_id,
                healed=False,
                actions=[HealAction("skip", True, "Skipped heal — active calls in progress")],
            )
        actions: list[HealAction] = []
        healed_any = False
        try:
            from core.worker import _recover_stale_dialing

            for role in ("sales_1",):
                await _recover_stale_dialing(role)
            actions.append(HealAction("recover_stale_dialing", True, "Recycled stale dialing leads"))
            healed_any = True
        except Exception as e:
            actions.append(HealAction("recover_stale_dialing", False, str(e)))
        return AgentHealResult(agent_id=self.agent_id, healed=healed_any, actions=actions)
