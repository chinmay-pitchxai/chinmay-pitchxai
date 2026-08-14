"""Base types for self-healing health agents."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AgentHealth(str, Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class AgentFinding:
    code: str
    message: str
    detail: str = ""
    auto_healable: bool = False


@dataclass
class AgentReport:
    agent_id: str
    agent_name: str
    domain: str
    health: AgentHealth
    findings: list[AgentFinding] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def issue_count(self) -> int:
        return len(self.findings)


@dataclass
class HealAction:
    action: str
    success: bool
    message: str


@dataclass
class AgentHealResult:
    agent_id: str
    healed: bool
    actions: list[HealAction] = field(default_factory=list)
    healed_at: float = field(default_factory=time.time)


class BaseHealthAgent:
    """Specialized monitor + safe auto-healer. Never hangs up live calls."""

    agent_id: str = "base"
    agent_name: str = "Base Agent"
    domain: str = "general"
    interval_sec: float = 45.0
    heal_during_active_calls: bool = False  # if False, skip heal when calls active

    async def check(self) -> AgentReport:
        raise NotImplementedError

    async def heal(self, report: AgentReport) -> AgentHealResult:
        return AgentHealResult(agent_id=self.agent_id, healed=False, actions=[])

    def _report(
        self,
        health: AgentHealth,
        findings: Optional[list[AgentFinding]] = None,
        **metadata: Any,
    ) -> AgentReport:
        return AgentReport(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            domain=self.domain,
            health=health,
            findings=findings or [],
            metadata=metadata,
        )
