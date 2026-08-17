"""Central sandbox configuration — single source of truth for all sandboxes.

The implementation plan defines 4 operational sandboxes:
  Sandbox 1: Initial Outreach (P1/P2 cold, P3 digital) + callbacks
  Sandbox 2: Failed-Call Retry (P4 attempt 2, P5/P6 attempt 3)
  Sandbox 3: Nurture & Site Visits (P7, P8)
  Sandbox 4: Post-Visit Feedback (P9)

Source types (campaign, digital) determine which fresh-call pool a lead routes to.
"""

from __future__ import annotations

from typing import Any

# ── Operational sandboxes (implementation plan model) ──
OPERATIONAL_SANDBOXES: dict[str, dict[str, Any]] = {
    "sandbox_1_initial_outreach": {
        "display_name": "Sandbox 1 · Initial Outreach",
        "phones": ["P1", "P2", "P3"],
        "purpose": "Fresh cold and digital lead qualification",
        "job_types": ("fresh_call", "callback"),
    },
    "sandbox_2_retry": {
        "display_name": "Sandbox 2 · Failed-Call Retry",
        "phones": ["P4", "P5", "P6"],
        "purpose": "12-hour and 24-hour memory-aware retries",
        "job_types": ("failed_retry",),
    },
    "sandbox_3_nurture": {
        "display_name": "Sandbox 3 · Nurture & Site Visits",
        "phones": ["P7", "P8"],
        "purpose": "Interested-lead nurture, callbacks and visit reminders",
        "job_types": (
            "interested_followup", "site_visit_reminder_day_before",
            "site_visit_reminder_morning", "site_visit_reschedule",
            "whatsapp_package", "whatsapp_followup_24h",
        ),
    },
    "sandbox_4_feedback": {
        "display_name": "Sandbox 4 · Post-Visit Feedback",
        "phones": ["P9"],
        "purpose": "Feedback after completed site visits and sales handover",
        "job_types": ("post_visit_feedback",),
    },
}

# ── Source types for orchestration routing ──
# These determine which fresh-call pool a lead routes to in number_allocator.py
SOURCE_TYPES = {
    "campaign": {
        "display_name": "Cold Campaign",
        "description": "Cold-called leads — route to P1/P2 (SANDBOX1_FRESH)",
        "fresh_pool": "sandbox1_fresh",
    },
    "digital": {
        "display_name": "Digital",
        "description": "Digital leads — route to P3 (SANDBOX1_DIGITAL)",
        "fresh_pool": "sandbox1_digital",
    },
}

# ── Role-to-sandbox mapping ──
ROLE_SANDBOX_MAP: dict[str, str] = {
    "sales_1": "sandbox_1_initial_outreach",
}


def list_sandbox_roles() -> list[str]:
    """Return all operational sandbox keys (sorted)."""
    return sorted(OPERATIONAL_SANDBOXES.keys())


def list_source_types() -> list[str]:
    """Return all source type keys (sorted)."""
    return sorted(SOURCE_TYPES.keys())


def all_console_roles() -> frozenset[str]:
    """Return every valid role: sandboxes + source types."""
    return frozenset(set(OPERATIONAL_SANDBOXES.keys()) | set(SOURCE_TYPES.keys()))


def get_sandbox_config(role: str) -> dict[str, Any] | None:
    """Return the operational sandbox config for a given key, or None."""
    return OPERATIONAL_SANDBOXES.get(role)


def get_source_type_config(source: str) -> dict[str, Any] | None:
    """Return the source type config for a given source, or None."""
    return SOURCE_TYPES.get(source)


def sandbox_display_name(role: str) -> str:
    """Return the human-readable display name for a sandbox role or source type."""
    cfg = OPERATIONAL_SANDBOXES.get(role)
    if cfg:
        return cfg["display_name"]
    src = SOURCE_TYPES.get(role)
    if src:
        return src["display_name"]
    return role.replace("_", " ").title()


def role_to_sandbox(role: str) -> str:
    """Map an operational role to its primary sandbox."""
    return ROLE_SANDBOX_MAP.get(role, "sandbox_1_initial_outreach")


def sandbox_phones(sandbox_id: str) -> list[str]:
    """Return the phone numbers for a given sandbox."""
    cfg = OPERATIONAL_SANDBOXES.get(sandbox_id)
    if cfg:
        return cfg.get("phones", [])
    return []
