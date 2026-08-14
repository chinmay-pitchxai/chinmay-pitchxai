"""Monitors critical config: API keys, Vobiz URLs, phone numbers."""

from __future__ import annotations

from config import settings, validate_critical_config
from services.health_agents.base import AgentFinding, AgentHealth, AgentReport, BaseHealthAgent


class ConfigHealthAgent(BaseHealthAgent):
    agent_id = "config_guardian"
    agent_name = "Config Guardian"
    domain = "configuration"
    interval_sec = 120.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        findings: list[AgentFinding] = []
        if not settings.gemini_api_key:
            findings.append(AgentFinding("no_gemini_key", "GEMINI_API_KEY is missing", auto_healable=False))
        if not settings.vobiz_public_base_url:
            findings.append(AgentFinding("no_vobiz_url", "VOBIZ_PUBLIC_BASE_URL is missing", auto_healable=False))
        for p in settings.vobiz_sales_1_phone_1, settings.vobiz_sales_1_phone_2:
            if not p:
                findings.append(AgentFinding("sales1_phone", "Sales 1 outbound phone not configured", auto_healable=False))
                break
        for msg in validate_critical_config():
            findings.append(AgentFinding("config_warn", msg, auto_healable=False))
        safe_per_auth = max(1, settings.vobiz_provider_concurrent_limit - 1)
        if settings.vobiz_max_concurrent_per_account >= settings.vobiz_provider_concurrent_limit:
            findings.append(AgentFinding(
                "vobiz_cap_at_provider",
                f"VOBIZ_MAX_CONCURRENT_PER_ACCOUNT={settings.vobiz_max_concurrent_per_account} "
                f"matches Vobiz limit ({settings.vobiz_provider_concurrent_limit}) — use {safe_per_auth}",
                auto_healable=False,
            ))
        dual_account_cap = safe_per_auth * 2
        if settings.max_concurrent_calls > dual_account_cap:
            findings.append(AgentFinding(
                "concurrency_high",
                f"MAX_CONCURRENT_CALLS={settings.max_concurrent_calls} exceeds safe headroom ({dual_account_cap})",
                auto_healable=False,
            ))
        health = AgentHealth.OK if not findings else (
            AgentHealth.CRITICAL if any(f.code in ("no_gemini_key", "no_vobiz_url") for f in findings) else AgentHealth.WARN
        )
        return self._report(health, findings, max_concurrent=settings.max_concurrent_calls)
