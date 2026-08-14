"""Monitors WhatsApp + SMTP integration readiness."""

from __future__ import annotations

from config import settings
from services.health_agents.base import AgentFinding, AgentHealth, AgentReport, BaseHealthAgent


class IntegrationHealthAgent(BaseHealthAgent):
    agent_id = "integration_watch"
    agent_name = "Integration Watch"
    domain = "whatsapp_email"
    interval_sec = 120.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        findings: list[AgentFinding] = []
        has_meta = bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)
        has_botspice = bool(settings.botspice_token and settings.botspice_phone_number_id)
        if not has_meta and not has_botspice:
            findings.append(AgentFinding(
                "wa_config", "WhatsApp credentials incomplete (Meta or BotSpice required)", auto_healable=False,
            ))
        if settings.smtp_email and not settings.smtp_app_password:
            findings.append(AgentFinding(
                "smtp_config", "SMTP email configured without an app password", auto_healable=False,
            ))
        if has_botspice and (not settings.server_url or "localhost" in settings.server_url):
            findings.append(AgentFinding(
                "botspice_url",
                "BotSpice media URLs need a public SERVER_URL (not localhost)",
                auto_healable=False,
            ))
        elif not settings.server_url or "localhost" in settings.server_url:
            findings.append(AgentFinding(
                "server_url",
                "SERVER_URL may not be publicly reachable for WhatsApp image URLs",
                auto_healable=False,
            ))
        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings)
