"""Heals callback routing: outbound_phone backfill, duplicate cleanup."""

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


class CallbackHealthAgent(BaseHealthAgent):
    agent_id = "callback_router"
    agent_name = "Callback Router"
    domain = "callbacks"
    interval_sec = 45.0
    heal_during_active_calls = True

    async def check(self) -> AgentReport:
        return await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> AgentReport:
        from core.storage import _get_conn

        conn = _get_conn()
        findings: list[AgentFinding] = []

        no_out = conn.execute(
            """
            SELECT COUNT(*) AS c FROM scheduled_callbacks
            WHERE status IN ('scheduled','queued')
              AND (outbound_phone IS NULL OR outbound_phone = '')
            """
        ).fetchone()
        if no_out and int(no_out["c"]) > 0:
            findings.append(AgentFinding(
                "missing_outbound",
                f"{no_out['c']} scheduled callback(s) lack outbound_phone",
                auto_healable=True,
            ))

        dupes = conn.execute(
            """
            SELECT phone, role, COUNT(*) AS c FROM scheduled_callbacks
            WHERE status IN ('scheduled','queued')
            GROUP BY phone, role HAVING COUNT(*) > 1
            """
        ).fetchall()
        if dupes:
            findings.append(AgentFinding(
                "duplicate_callbacks",
                f"{len(dupes)} phone(s) have duplicate pending callbacks",
                auto_healable=True,
            ))

        overdue = conn.execute(
            """
            SELECT COUNT(*) AS c FROM scheduled_callbacks
            WHERE status = 'scheduled' AND scheduled_at < ?
            """,
            (__import__("time").time() - 300,),
        ).fetchone()
        if overdue and int(overdue["c"]) > 5:
            findings.append(AgentFinding(
                "overdue_callbacks",
                f"{overdue['c']} callbacks overdue >5min (sub-workers should claim)",
                auto_healable=False,
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

        # Backfill outbound_phone from lead record
        rows = conn.execute(
            """
            SELECT sc.id, sc.lead_id FROM scheduled_callbacks sc
            WHERE sc.status IN ('scheduled','queued')
              AND (sc.outbound_phone IS NULL OR sc.outbound_phone = '')
              AND sc.lead_id IS NOT NULL
            """
        ).fetchall()
        backfilled = 0
        for row in rows:
            lead = conn.execute(
                "SELECT outbound_phone FROM leads WHERE id = ?", (row["lead_id"],)
            ).fetchone()
            if lead and (lead["outbound_phone"] or "").strip():
                conn.execute(
                    "UPDATE scheduled_callbacks SET outbound_phone = ? WHERE id = ?",
                    (lead["outbound_phone"].strip(), row["id"]),
                )
                backfilled += 1
        if backfilled:
            conn.commit()
            _invalidate_state_cache()
            actions.append(HealAction("backfill_outbound", True, f"Backfilled outbound_phone on {backfilled} callback(s)"))
            healed = True

        # Fallback: assign role's first outbound line when lead has no outbound_phone
        still_empty = conn.execute(
            """
            SELECT sc.id, sc.role FROM scheduled_callbacks sc
            WHERE sc.status IN ('scheduled','queued')
              AND (sc.outbound_phone IS NULL OR sc.outbound_phone = '')
            """
        ).fetchall()
        role_phone_cache: dict[str, str] = {}
        fallback_n = 0
        for row in still_empty:
            role_key = (row["role"] or "sales_1").strip().lower()
            if role_key not in role_phone_cache:
                try:
                    from core.state import get_state
                    from core.outbound_numbers import get_all_outbound_numbers
                    nums = get_all_outbound_numbers(role_key, get_state(role_key).get("vobiz", {}) or {})
                    role_phone_cache[role_key] = nums[0] if nums else ""
                except Exception:
                    role_phone_cache[role_key] = ""
            fallback = role_phone_cache.get(role_key, "")
            if fallback:
                conn.execute(
                    "UPDATE scheduled_callbacks SET outbound_phone = ? WHERE id = ?",
                    (fallback.strip(), row["id"]),
                )
                fallback_n += 1
        if fallback_n:
            conn.commit()
            _invalidate_state_cache()
            actions.append(HealAction("fallback_outbound", True, f"Assigned default outbound line on {fallback_n} callback(s)"))
            healed = True

        # Cancel duplicate pending callbacks (keep earliest per phone+role)
        dup_groups = conn.execute(
            """
            SELECT phone, role FROM scheduled_callbacks
            WHERE status IN ('scheduled','queued')
            GROUP BY phone, role HAVING COUNT(*) > 1
            """
        ).fetchall()
        cancelled = 0
        for g in dup_groups:
            ids = conn.execute(
                """
                SELECT id FROM scheduled_callbacks
                WHERE phone = ? AND role = ? AND status IN ('scheduled','queued')
                ORDER BY scheduled_at ASC
                """,
                (g["phone"], g["role"]),
            ).fetchall()
            for extra in ids[1:]:
                conn.execute(
                    "UPDATE scheduled_callbacks SET status = 'cancelled', error = 'dedup by callback_router agent' WHERE id = ?",
                    (extra["id"],),
                )
                cancelled += 1
        if cancelled:
            conn.commit()
            _invalidate_state_cache()
            actions.append(HealAction("dedup_callbacks", True, f"Cancelled {cancelled} duplicate callback(s)"))
            healed = True

        return AgentHealResult(agent_id=self.agent_id, healed=healed, actions=actions)
