"""Ensures the RAG knowledge base exists and is indexed.

Live RAG runs in ``RAG_MODE=chunk``: the canonical source is
``data/{role}/rag_source.txt`` chunked into ``data/{role}/kb_chunks.json``,
and the Postgres-backed ``RagStore`` serves keyword retrieval from the
``chunks`` table. The legacy SQLite ``rag.db`` file is no longer used.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import settings
from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)


class RAGHealthAgent(BaseHealthAgent):
    agent_id = "rag_keeper"
    agent_name = "RAG Keeper"
    domain = "knowledge_base"
    interval_sec = 300.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        findings: list[AgentFinding] = []
        if not settings.rag_enabled:
            return self._report(AgentHealth.OK, [], rag_enabled=False)
        from services.chunk_rag import _chunks_path

        for role in ("sales_1",):
            kb = _chunks_path(role)
            if not kb.exists():
                findings.append(AgentFinding(
                    "rag_missing",
                    f"KB chunks missing at {kb}",
                    auto_healable=True,
                ))
                continue
            if kb.stat().st_size < 256:
                findings.append(AgentFinding(
                    "rag_empty",
                    f"KB chunks exist but appear empty at {kb}",
                    auto_healable=True,
                ))
        health = AgentHealth.OK if not findings else AgentHealth.WARN
        return self._report(health, findings, path="data/{role}/kb_chunks.json")

    async def heal(self, report: AgentReport) -> AgentHealResult:
        actions: list[HealAction] = []
        if not any(f.code in ("rag_missing", "rag_empty") for f in report.findings):
            return AgentHealResult(agent_id=self.agent_id, healed=False, actions=actions)
        try:
            from prompts.role_prompts import get_role_rag_source_text
            from scripts.build_kb_chunks import build_chunks_from_rag
            from services.chunk_rag import _chunks_path

            text = get_role_rag_source_text("sales_1")
            chunks = build_chunks_from_rag(text)
            out = _chunks_path("sales_1")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps({"role": "sales_1", "version": 1, "chunks": chunks}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            n = len(chunks)
            actions.append(HealAction("build_kb_chunks", True, f"Rebuilt KB chunks: {n} from sales_1 rag_source.txt"))
            return AgentHealResult(agent_id=self.agent_id, healed=True, actions=actions)
        except Exception as e:
            actions.append(HealAction("build_kb_chunks", False, str(e)))
            return AgentHealResult(agent_id=self.agent_id, healed=False, actions=actions)
