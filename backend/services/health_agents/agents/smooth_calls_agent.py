"""Master self-healing guardian for outbound call quality (smooth-calls skill).

Monitors greeting PCM, env drift, failed-call spikes, and capture-code regression.
Auto-heals: strip name from stored greetings, wipe stale PCM, regenerate intro-only audio.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from config import settings
from services.health_agents.base import (
    AgentFinding,
    AgentHealth,
    AgentHealResult,
    AgentReport,
    BaseHealthAgent,
    HealAction,
)

_BASELINE_PATH = Path(__file__).resolve().parents[3] / "data" / "smooth_calls_baseline.json"
_CAPTURE_PATH = (
    Path(__file__).resolve().parents[2] / "live_greeting_capture.py"
)
_GREETINGS_DIR = Path(__file__).resolve().parents[3] / "data" / "greetings"
_ROLES = ("sales_1",)


class SmoothCallsGuardianAgent(BaseHealthAgent):
    agent_id = "smooth_calls_guardian"
    agent_name = "Smooth Calls Guardian"
    domain = "voice_call_quality"
    interval_sec = 45.0
    heal_during_active_calls = True  # PCM on disk; next call uses fixed intro

    def _load_baseline(self) -> dict[str, Any]:
        try:
            if _BASELINE_PATH.is_file():
                return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Smooth Calls Guardian: baseline read failed: {}", e)
        return {}

    def _env_drift(self, baseline: dict[str, Any]) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        expected = (baseline.get("env") or {}) if isinstance(baseline, dict) else {}
        for key, want in expected.items():
            got = os.getenv(key, "").strip().lower()
            want_norm = str(want).strip().lower()
            if got != want_norm:
                findings.append(
                    AgentFinding(
                        "env_drift",
                        f"{key}={got or '(unset)'} expected {want_norm}",
                        detail="Sync .env / .env.vps with smooth_calls_baseline.json",
                        auto_healable=False,
                    )
                )
        return findings

    def _greeting_text_issues(self, forbidden: list[str]) -> list[AgentFinding]:
        from core.greeting_text_utils import intro_only_greeting
        from core.state import get_state, save_role_state

        findings: list[AgentFinding] = []
        for role in _ROLES:
            state = get_state(role)
            raw = (state.get("greeting_text") or "").strip()
            if not raw:
                continue
            low = raw.lower()
            if any(f in low for f in forbidden):
                findings.append(
                    AgentFinding(
                        "greeting_has_name_verify",
                        f"Role {role} greeting_text contains name-verify tail",
                        detail=raw[:120],
                        auto_healable=True,
                    )
                )
            fixed = intro_only_greeting(raw)
            if fixed != raw:
                findings.append(
                    AgentFinding(
                        "greeting_needs_strip",
                        f"Role {role} greeting will be stripped to intro-only",
                        auto_healable=True,
                    )
                )
        return findings

    def _capture_code_ok(self, markers: list[str]) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        try:
            src = _CAPTURE_PATH.read_text(encoding="utf-8")
        except Exception as e:
            findings.append(
                AgentFinding(
                    "capture_unreadable",
                    "Cannot read live_greeting_capture.py",
                    detail=str(e),
                    auto_healable=False,
                )
            )
            return findings
        if "build_role_system_prompt" in src and "intro-only" not in src.lower():
            findings.append(
                AgentFinding(
                    "capture_regression",
                    "Greeting capture may use full role prompt (wrong names in PCM)",
                    auto_healable=False,
                )
            )
        elif markers and not any(m.lower() in src.lower() for m in markers):
            findings.append(
                AgentFinding(
                    "capture_markers_missing",
                    "Intro-only capture markers missing from live_greeting_capture.py",
                    auto_healable=False,
                )
            )
        return findings

    def _failed_call_spike(self, baseline: dict[str, Any]) -> tuple[list[AgentFinding], dict[str, Any]]:
        findings: list[AgentFinding] = []
        meta: dict[str, Any] = {}
        window_h = float(baseline.get("failed_rate_window_hours") or 2)
        min_samples = int(baseline.get("failed_rate_min_samples") or 8)
        warn_pct = float(baseline.get("failed_rate_warn_pct") or 40)
        crit_pct = float(baseline.get("failed_rate_critical_pct") or 65)

        try:
            from core.storage import _get_conn

            conn = _get_conn()
            row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status IN ('failed','error') THEN 1 ELSE 0 END) AS failed,
                  COUNT(*) AS total
                FROM leads
                WHERE datetime(updated_at) >= datetime('now', ?)
                  AND status NOT IN ('pending','dialing','callback_scheduled')
                """,
                (f"-{int(window_h)} hours",),
            ).fetchone()
            failed = int(row["failed"] or 0) if row else 0
            total = int(row["total"] or 0) if row else 0
            meta["failed_recent"] = failed
            meta["total_recent"] = total
            if total >= min_samples:
                pct = 100.0 * failed / total
                meta["failed_pct"] = round(pct, 1)
                if pct >= crit_pct:
                    findings.append(
                        AgentFinding(
                            "failed_rate_critical",
                            f"Failed call rate {pct:.0f}% in last {window_h:.0f}h ({failed}/{total})",
                            auto_healable=True,
                        )
                    )
                elif pct >= warn_pct:
                    findings.append(
                        AgentFinding(
                            "failed_rate_warn",
                            f"Elevated failures {pct:.0f}% in last {window_h:.0f}h",
                            auto_healable=True,
                        )
                    )
        except Exception as e:
            findings.append(
                AgentFinding(
                    "failed_rate_query",
                    "Could not compute recent failure rate",
                    detail=str(e),
                    auto_healable=False,
                )
            )
        return findings, meta

    def _stale_greeting_pcm(self) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        if not _GREETINGS_DIR.is_dir():
            findings.append(
                AgentFinding(
                    "no_greetings_dir",
                    "greetings/ directory missing",
                    auto_healable=True,
                )
            )
            return findings
        for role in ("sales_1",):
            pcm = _GREETINGS_DIR / f"greeting_{role}.pcm"
            if not pcm.is_file() or pcm.stat().st_size < 800:
                findings.append(
                    AgentFinding(
                        "greeting_pcm_missing",
                        f"Missing or tiny greeting PCM for {role}",
                        auto_healable=True,
                    )
                )
        return findings

    async def check(self) -> AgentReport:
        baseline = self._load_baseline()
        forbidden = list(baseline.get("greeting_forbidden_substrings") or [])
        markers = list(baseline.get("capture_source_markers") or [])

        findings: list[AgentFinding] = []
        findings.extend(self._env_drift(baseline))
        findings.extend(self._greeting_text_issues(forbidden))
        findings.extend(self._capture_code_ok(markers))
        findings.extend(self._stale_greeting_pcm())

        fail_findings, fail_meta = await asyncio.to_thread(self._failed_call_spike, baseline)
        findings.extend(fail_findings)

        from core.storage import _list_stuck_incoming_calls_sync

        stuck_inbound = await asyncio.to_thread(_list_stuck_incoming_calls_sync, 15, 25)
        if stuck_inbound:
            findings.append(
                AgentFinding(
                    "inbound_stuck_connected",
                    f"{len(stuck_inbound)} inbound call(s) stuck at connected/ringing",
                    detail="; ".join(
                        f"id={r.get('id')} camp={r.get('camp_id')}" for r in stuck_inbound[:5]
                    ),
                    auto_healable=True,
                )
            )

        critical_codes = {
            "capture_regression",
            "failed_rate_critical",
            "greeting_has_name_verify",
        }
        if any(f.code in critical_codes for f in findings):
            health = AgentHealth.CRITICAL
        elif findings:
            health = AgentHealth.WARN
        else:
            health = AgentHealth.OK

        return self._report(
            health,
            findings,
            baseline_version=baseline.get("version"),
            **fail_meta,
        )

    async def heal(self, report: AgentReport) -> AgentHealResult:
        actions: list[HealAction] = []
        healed = False

        codes = {f.code for f in report.findings}

        if codes & {"greeting_has_name_verify", "greeting_needs_strip", "greeting_pcm_missing", "no_greetings_dir"}:
            ok = await self._heal_greetings(actions)
            healed = healed or ok

        if codes & {"failed_rate_warn", "failed_rate_critical", "stale_dialing"}:
            ok = await self._heal_pipeline(actions)
            healed = healed or ok

        if "inbound_stuck_connected" in codes:
            try:
                from core.worker import _heal_stuck_incoming_calls

                n = await _heal_stuck_incoming_calls(max_age_minutes=15)
                actions.append(
                    HealAction(
                        "finalize_stuck_inbound",
                        n > 0,
                        f"Finalized {n} stuck inbound row(s)",
                    )
                )
                healed = healed or n > 0
            except Exception as e:
                actions.append(HealAction("finalize_stuck_inbound", False, str(e)))

        return AgentHealResult(agent_id=self.agent_id, healed=healed, actions=actions)

    async def _heal_greetings(self, actions: list[HealAction]) -> bool:
        from core.greeting_text_utils import intro_only_greeting
        from core.opening_line import packaged_fallback_greeting
        from core.state import get_state, save_role_state

        any_ok = False
        for role in _ROLES:
            state = get_state(role)
            raw = (state.get("greeting_text") or "").strip()
            intro = intro_only_greeting(raw) if raw else packaged_fallback_greeting(role)
            if raw and intro != raw:
                try:
                    save_role_state(role, greeting_text=intro)
                    actions.append(
                        HealAction("strip_greeting_text", True, f"{role} greeting → intro-only")
                    )
                    any_ok = True
                except Exception as e:
                    actions.append(HealAction("strip_greeting_text", False, str(e)))

        try:
            _GREETINGS_DIR.mkdir(parents=True, exist_ok=True)
            removed = 0
            for path in _GREETINGS_DIR.glob("greeting_*.pcm"):
                path.unlink(missing_ok=True)
                removed += 1
            for path in _GREETINGS_DIR.glob("greeting_*.pcm.meta"):
                path.unlink(missing_ok=True)
            for path in _GREETINGS_DIR.glob("name_verify_*"):
                path.unlink(missing_ok=True)
            actions.append(
                HealAction("wipe_greeting_pcm", True, f"Cleared {removed} greeting PCM cache(s)")
            )
            any_ok = True
        except Exception as e:
            actions.append(HealAction("wipe_greeting_pcm", False, str(e)))

        try:
            from core.greeting_pcm import _generate_and_cache_greeting

            for role in ("sales_1",):
                text = packaged_fallback_greeting(role)
                voice = settings.gemini_live_voice_sales_1
                result = await _generate_and_cache_greeting(role, text, voice or settings.gemini_live_voice)
                if result:
                    actions.append(
                        HealAction(
                            "regenerate_greeting_pcm",
                            True,
                            f"{role} intro PCM ({len(result[0])} bytes)",
                        )
                    )
                    any_ok = True
                else:
                    actions.append(
                        HealAction("regenerate_greeting_pcm", False, f"{role} capture failed")
                    )
        except Exception as e:
            actions.append(HealAction("regenerate_greeting_pcm", False, str(e)))

        return any_ok

    async def _heal_pipeline(self, actions: list[HealAction]) -> bool:
        from core.state import total_active_vobiz_calls

        if total_active_vobiz_calls() > 0:
            actions.append(
                HealAction("recover_stale", True, "Deferred stale-dial recovery — active calls")
            )
            return False
        try:
            from core.worker import _recover_stale_dialing

            for role in _ROLES:
                await _recover_stale_dialing(role)
            actions.append(HealAction("recover_stale_dialing", True, "Recycled stale dialing leads"))
            return True
        except Exception as e:
            actions.append(HealAction("recover_stale_dialing", False, str(e)))
            return False
