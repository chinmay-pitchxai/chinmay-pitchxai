"""Watches global call capacity vs Vobiz limit."""

from __future__ import annotations

import os

from config import settings
from core.state import (
    active_vobiz_calls_for_auth,
    max_concurrency_for_vobiz_account,
    total_active_vobiz_calls,
    vobiz_auth_id_for_role,
    vobiz_provider_concurrent_limit,
)
from services.health_agents.base import AgentFinding, AgentHealth, AgentReport, BaseHealthAgent


class ConcurrencyHealthAgent(BaseHealthAgent):
    agent_id = "concurrency_sentinel"
    agent_name = "Concurrency Sentinel"
    domain = "calling_capacity"
    interval_sec = 20.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        active = total_active_vobiz_calls()
        cap = max(1, int(settings.max_concurrent_calls or 2))
        vobiz_cap = max(1, int(settings.vobiz_max_concurrent_per_account or 2))
        provider_limit = vobiz_provider_concurrent_limit()
        findings: list[AgentFinding] = []
        if active > cap:
            findings.append(AgentFinding(
                "over_cap",
                f"Active calls ({active}) exceed configured cap ({cap})",
                auto_healable=False,
            ))
        for role in ("sales_1",):
            auth_id = vobiz_auth_id_for_role(role)
            if not auth_id:
                continue
            auth_active = active_vobiz_calls_for_auth(auth_id)
            auth_cap = max_concurrency_for_vobiz_account(auth_id)
            if auth_active >= auth_cap:
                findings.append(AgentFinding(
                    "vobiz_auth_cap",
                    f"{auth_id} at {auth_active}/{auth_cap} app cap — new dials may be rejected by Vobiz",
                    auto_healable=False,
                ))
            if auth_active >= provider_limit:
                findings.append(AgentFinding(
                    "vobiz_risk",
                    f"At or above Vobiz {provider_limit}/{provider_limit} limit for {auth_id}",
                    auto_healable=False,
                ))
        if vobiz_cap >= provider_limit:
            findings.append(AgentFinding(
                "vobiz_env_high",
                f"VOBIZ_MAX_CONCURRENT_PER_ACCOUNT={vobiz_cap} matches provider limit — set to {provider_limit - 1}",
                auto_healable=False,
            ))
        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings, active_calls=active, cap=cap)
