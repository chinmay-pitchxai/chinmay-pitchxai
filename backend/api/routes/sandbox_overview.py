"""Aggregated sandbox overview — across all sandboxes for the Overall dashboard view."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from loguru import logger

from config import settings
from core.state import (
    _CAMPAIGN_TASKS,
    normalize_console_role,
    total_active_vobiz_calls,
)
from core.campaign_payload import (
    build_dashboard_timelines,
    build_campaign_state_dashboard_fields,
    campaign_called_count,
    disposition_counts_for_dashboard,
    hourly_counts_for_dashboard,
    last_seven_dashboard_axis,
    progress_counts_for_dashboard,
    weekday_counts_for_dashboard,
)
from core.storage import (
    _get_conn,
    _get_leads_sync,
    _get_leads_with_outbound_activity_sync,
    _inbound_counts_on_calendar_dates_sync,
    get_lead_counts,
    count_leads_with_outbound_attempt,
)
from sandbox_config import (
    list_sandbox_roles,
    sandbox_display_name,
    get_sandbox_config,
    OPERATIONAL_SANDBOXES,
)

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


def _operational_breakdown() -> list[dict[str, Any]]:
    """Return the four plan-defined sandboxes, even before their queues have data."""
    conn = _get_conn()
    rows: list[dict[str, Any]] = []
    for sandbox_id, definition in OPERATIONAL_SANDBOXES.items():
        job_types = definition["job_types"]
        placeholders = ",".join("?" for _ in job_types)
        try:
            metrics = conn.execute(
                f"""SELECT COUNT(DISTINCT lead_id), COUNT(*),
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('scheduled','ready','claimed','running') THEN 1 ELSE 0 END)
                    FROM workflow_jobs WHERE job_type IN ({placeholders})""",
                job_types,
            ).fetchone()
            leads, jobs, completed, active_jobs = (int(value or 0) for value in metrics)
        except Exception:
            leads = jobs = completed = active_jobs = 0
        rows.append({
            "id": sandbox_id, "role": sandbox_id,
            "display_name": definition["display_name"],
            "phones": definition["phones"], "purpose": definition["purpose"],
            "job_types": list(job_types),
            "total_leads": leads, "called_count": completed,
            "interested": jobs, "active_jobs": active_jobs,
            "active": active_jobs > 0,
        })
    return rows


def _dashboard_tz() -> ZoneInfo:
    try:
        return ZoneInfo(
            (settings.transcript_callback_tz or "Asia/Kolkata").strip()
            or "Asia/Kolkata"
        )
    except Exception:
        return ZoneInfo("Asia/Kolkata")


@router.get("/overview")
async def sandbox_overview(request: Request):
    """Aggregated dashboard stats across all sandboxes."""
    try:
        tz = _dashboard_tz()
        labels, dates = last_seven_dashboard_axis()

        # Aggregate over the roles that actually own leads in this deployment.
        # The sandbox-config keys (sandbox_1_initial_outreach, …) are blueprint
        # definitions, not lead roles — leads live under console roles.
        from core.role_sandbox import ALL_CONSOLE_ROLES

        all_sandbox_roles = sorted(ALL_CONSOLE_ROLES)

        aggregated: dict[str, Any] = {
            "total_leads": 0,
            "called_count": 0,
            "interested_count": 0,
            "site_visit_count": 0,
            "not_interested_count": 0,
            "inbound_callbacks": 0,
            "total_called_count": 0,
            "failed_count": 0,
            "disposition_counts": {},
            "progress_counts": {"connected": 0, "failed": 0, "no_answer": 0, "pending": 0, "other": 0},
            "weekday_counts": [0] * 7,
            "hourly_counts": [0] * 24,
            "timeline_week_labels": labels,
            "timeline_dates_iso": [d.isoformat() for d in dates],
            "timeline_total_calls": [0] * len(dates),
            "timeline_interested": [0] * len(dates),
            "timeline_inbound_per_day": [0] * len(dates),
            "active_campaigns": 0,
            "active_calls": total_active_vobiz_calls(),
            "leads": [],
            "sandbox_breakdown": [],
            "campaign_paused": False,
        }

        for role in all_sandbox_roles:
            try:
                counts = await get_lead_counts(role)
                aggregated["total_leads"] += int(counts.get("total") or 0)

                called = await count_leads_with_outbound_attempt(role)
                aggregated["called_count"] += called

                chart_rows = _get_leads_sync(role, limit=800)
                dash = build_campaign_state_dashboard_fields(role, chart_rows)

                agg_disp = dash.get("disposition_counts", {})
                for k, v in agg_disp.items():
                    aggregated["disposition_counts"][k] = (
                        aggregated["disposition_counts"].get(k, 0) + int(v)
                    )

                agg_prog = dash.get("progress_counts", {})
                for k, v in agg_prog.items():
                    if k in aggregated["progress_counts"]:
                        aggregated["progress_counts"][k] += int(v)

                wd = dash.get("weekday_counts", [0] * 7)
                for i in range(7):
                    aggregated["weekday_counts"][i] += int(wd[i]) if i < len(wd) else 0

                hr = dash.get("hourly_counts", [0] * 24)
                for i in range(24):
                    aggregated["hourly_counts"][i] += int(hr[i]) if i < len(hr) else 0

                ttl = dash.get("timeline_total_calls", [0] * len(dates))
                tins = dash.get("timeline_interested", [0] * len(dates))
                for i in range(len(dates)):
                    if i < len(ttl):
                        aggregated["timeline_total_calls"][i] += int(ttl[i])
                    if i < len(tins):
                        aggregated["timeline_interested"][i] += int(tins[i])

                inbound_on_dates = _inbound_counts_on_calendar_dates_sync(
                    role, [d.isoformat() for d in dates]
                )
                for i, d in enumerate(dates):
                    aggregated["timeline_inbound_per_day"][i] += int(
                        inbound_on_dates.get(d.isoformat(), 0) or 0
                    )

                aggregated["inbound_callbacks"] += int(
                    dash.get("inbound_callbacks") or 0
                )

                is_active = bool(
                    _CAMPAIGN_TASKS.get(role)
                    and not _CAMPAIGN_TASKS[role].done()
                )
                if is_active:
                    aggregated["active_campaigns"] += 1

                aggregated["sandbox_breakdown"].append(
                    {
                        "role": role,
                        "display_name": sandbox_display_name(role),
                        "total_leads": int(counts.get("total") or 0),
                        "called_count": called,
                        "interested": int(
                            dash.get("chart_interested_total") or 0
                        ),
                        "active": is_active,
                    }
                )
            except Exception as exc:
                logger.warning(
                    "sandbox_overview: failed to aggregate role={}: {}", role, exc
                )

        aggregated["interested_count"] = int(
            aggregated["disposition_counts"].get("Interested") or 0
        )
        aggregated["site_visit_count"] = int(
            aggregated["disposition_counts"].get("Site Visit") or 0
        )
        aggregated["not_interested_count"] = int(
            aggregated["disposition_counts"].get("Not Interested") or 0
        )

        # Role aggregation above powers charts. The visible breakdown follows the
        # four operational sandboxes defined in the master workflow specification.
        aggregated["sandbox_breakdown"] = _operational_breakdown()

        from core.storage import is_campaign_globally_paused

        aggregated["campaign_paused"] = await is_campaign_globally_paused()

        return aggregated

    except Exception as e:
        logger.error("sandbox_overview failed: {}", e)
        return {"error": str(e)}


@router.get("/list")
async def sandbox_list():
    """Return all available sandboxes with display names and status."""
    from core.state import _CAMPAIGN_TASKS
    from core.storage import get_lead_counts

    sandboxes = []
    all_roles = list_sandbox_roles()
    for role in all_roles:
        try:
            counts = await get_lead_counts(role)
            is_active = bool(
                _CAMPAIGN_TASKS.get(role)
                and not _CAMPAIGN_TASKS[role].done()
            )
            sandboxes.append(
                {
                    "role": role,
                    "display_name": sandbox_display_name(role),
                    "total_leads": int(counts.get("total") or 0),
                    "active_campaign": is_active,
                }
            )
        except Exception:
            sandboxes.append(
                {
                    "role": role,
                    "display_name": sandbox_display_name(role),
                    "total_leads": 0,
                    "active_campaign": False,
                }
            )
    return {"sandboxes": sandboxes}
