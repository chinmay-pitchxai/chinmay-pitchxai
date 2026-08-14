"""Call Quality Guardian — monitors response latency, noise levels, and turn-taking quality.

Analyzes per-call metrics stored by ``call_quality_metrics.py`` and flags
degraded audio quality, high latency, or excessive noise.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)

# ── Thresholds (tunable) ─────────────────────────────────────────────────
_LATENCY_WARN_MS = 5000.0      # 5s greeting→response is acceptable
_LATENCY_CRIT_MS = 12000.0     # 12s+ is broken
_NOISE_FLOOR_WARN = 800.0      # High noise floor in PCM terms
_TURN_LAT_WARN_MS = 3000.0     # Average turn latency warning
_MIN_SAMPLES = 5               # Minimum calls before we report anything


class CallQualityGuardianAgent(BaseHealthAgent):
    agent_id = "call_quality_guardian"
    agent_name = "Call Quality Guardian"
    domain = "call_quality"
    interval_sec = 60.0  # Check every 60s
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        findings: list[AgentFinding] = []
        meta: dict[str, Any] = {}

        try:
            from services.call_quality_metrics import get_latency_stats, get_recent_calls

            stats = await asyncio.to_thread(get_latency_stats, min_samples=_MIN_SAMPLES)
            sample_count = int(stats.get("sample_count") or 0)
            meta["samples"] = sample_count

            if sample_count >= _MIN_SAMPLES:
                # ── Greeting→Response latency ────────────────────────────
                avg_greet = float(stats.get("avg_greeting_lat") or -1)
                max_greet = float(stats.get("max_greeting_lat") or -1)
                meta["avg_greeting_latency_ms"] = avg_greet
                meta["max_greeting_latency_ms"] = max_greet

                if avg_greet >= _LATENCY_CRIT_MS:
                    findings.append(
                        AgentFinding(
                            "greeting_latency_critical",
                            f"Avg greeting→response {avg_greet:.0f}ms (critical threshold {_LATENCY_CRIT_MS:.0f}ms)",
                            detail=f"Max: {max_greet:.0f}ms over {sample_count} calls",
                            auto_healable=False,
                        )
                    )
                elif avg_greet >= _LATENCY_WARN_MS:
                    findings.append(
                        AgentFinding(
                            "greeting_latency_warn",
                            f"Avg greeting→response {avg_greet:.0f}ms (warn threshold {_LATENCY_WARN_MS:.0f}ms)",
                            auto_healable=False,
                        )
                    )
                else:
                    logger.info(
                        "📊 CQ Guardian: greeting latency = {:.0f}ms (OK)", avg_greet
                    )

                # ── Turn-by-turn latency ─────────────────────────────────
                avg_turn = float(stats.get("avg_turn_lat") or -1)
                meta["avg_turn_latency_ms"] = avg_turn
                if avg_turn >= _TURN_LAT_WARN_MS:
                    findings.append(
                        AgentFinding(
                            "turn_latency_warn",
                            f"Avg turn latency {avg_turn:.0f}ms (threshold {_TURN_LAT_WARN_MS:.0f}ms)",
                            auto_healable=False,
                        )
                    )

                # ── Noise floor ──────────────────────────────────────────
                avg_noise = float(stats.get("avg_noise_floor") or -1)
                meta["avg_noise_floor_rms"] = avg_noise
                if avg_noise >= _NOISE_FLOOR_WARN:
                    findings.append(
                        AgentFinding(
                            "high_noise_floor",
                            f"Avg noise floor {avg_noise:.0f} RMS (threshold {_NOISE_FLOOR_WARN:.0f})",
                            detail="Inbound PSTN noise may degrade Gemini understanding",
                            auto_healable=False,
                        )
                    )

                # ── Outcome summary ──────────────────────────────────────
                total = sample_count
                vm = int(stats.get("voicemail_count") or 0)
                named = int(stats.get("name_confirmed_count") or 0)
                pitched = int(stats.get("pitch_delivered_count") or 0)
                barge = int(stats.get("total_barge_ins") or 0)
                meta["voicemail_pct"] = round(100.0 * vm / total, 1) if total else 0
                meta["name_confirm_pct"] = round(100.0 * named / total, 1) if total else 0
                meta["pitch_delivery_pct"] = round(100.0 * pitched / total, 1) if total else 0
                meta["barge_ins_total"] = barge

            else:
                logger.info(
                    "📊 CQ Guardian: only {} sample(s) — waiting for {}",
                    sample_count, _MIN_SAMPLES,
                )

        except Exception as e:
            logger.exception("CQ Guardian check failed: {}", e)
            findings.append(
                AgentFinding(
                    "query_error", str(e)[:200], auto_healable=False
                )
            )

        # Determine overall health
        critical_codes = {"greeting_latency_critical"}
        if any(f.code in critical_codes for f in findings):
            health = AgentHealth.CRITICAL
        elif findings:
            health = AgentHealth.WARN
        else:
            health = AgentHealth.OK

        return self._report(health, findings, **meta)

    async def heal(self, report: AgentReport) -> AgentHealResult:
        """Call Quality Guardian has no auto-heal — it monitors only.
        Reports findings for manual inspection via the dashboard/API.
        """
        return AgentHealResult(agent_id=self.agent_id, healed=False, actions=[])


import asyncio  # noqa: E402 (needed for async check)
