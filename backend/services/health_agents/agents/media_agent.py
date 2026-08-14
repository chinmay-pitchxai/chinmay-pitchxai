"""Monitors WhatsApp/email media assets."""

from __future__ import annotations

import os

from config import settings
from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)

REQUIRED_MEDIA = (
    "solitaire_unity_image.jpeg",
    "solitaire_unity_brochure.pdf",
    "solitaire_unity_price_sheet.pdf",
)


class MediaHealthAgent(BaseHealthAgent):
    agent_id = "media_curator"
    agent_name = "Media Curator"
    domain = "whatsapp_assets"
    interval_sec = 180.0
    heal_during_active_calls = True

    def _media_dir(self) -> str:
        # backend/services/health_agents/agents → four levels up to backend root
        backend_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        return os.path.join(backend_root, "media", "whatsapp")

    async def check(self) -> AgentReport:
        media_dir = self._media_dir()
        missing = [f for f in REQUIRED_MEDIA if not os.path.isfile(os.path.join(media_dir, f))]
        findings: list[AgentFinding] = []
        if missing:
            findings.append(AgentFinding(
                "media_missing",
                f"Missing {len(missing)} WhatsApp media file(s)",
                detail=", ".join(missing),
                auto_healable=True,
            ))
        if not settings.whatsapp_access_token or not settings.whatsapp_phone_number_id:
            findings.append(AgentFinding("wa_api", "WhatsApp Cloud API not fully configured", auto_healable=False))
        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings, media_dir=media_dir, missing_count=len(missing))

    async def heal(self, report: AgentReport) -> AgentHealResult:
        actions: list[HealAction] = []
        media_dir = self._media_dir()
        os.makedirs(media_dir, exist_ok=True)
        manifest = os.path.join(media_dir, "REQUIRED_FILES.txt")
        try:
            with open(manifest, "w", encoding="utf-8") as fh:
                fh.write("# Place these files for full WhatsApp automation:\n")
                for f in REQUIRED_MEDIA:
                    fh.write(f + "\n")
            actions.append(HealAction("ensure_dir", True, f"Ensured {media_dir} + manifest"))
        except Exception as e:
            actions.append(HealAction("ensure_dir", False, str(e)))
        return AgentHealResult(agent_id=self.agent_id, healed=bool(actions and actions[-1].success), actions=actions)
