"""Deterministic workflow vocabulary; deliberately independent of voice/prompt code."""

from __future__ import annotations

from enum import StrEnum


class LeadStage(StrEnum):
    NEW = "new"
    CAMPAIGN_CALLING = "campaign_calling"
    FAILED_RETRY_WAITING = "failed_retry_waiting"
    CONNECTED = "connected"
    INTERESTED = "interested"
    FOLLOW_UP = "follow_up"
    CALLBACK_REQUESTED = "callback_requested"
    SITE_VISIT_SCHEDULED = "site_visit_scheduled"
    SITE_VISIT_COMPLETED = "site_visit_completed"
    FEEDBACK_PENDING = "feedback_pending"
    BOOKED = "booked"
    NOT_INTERESTED = "not_interested"
    LOST = "lost"
    OPTED_OUT = "opted_out"


class JobType(StrEnum):
    FRESH_CALL = "fresh_call"
    FAILED_RETRY = "failed_retry"
    CALLBACK = "callback"
    INTERESTED_FOLLOWUP = "interested_followup"
    SITE_VISIT_REMINDER_DAY_BEFORE = "site_visit_reminder_day_before"
    SITE_VISIT_REMINDER_MORNING = "site_visit_reminder_morning"
    SITE_VISIT_RESCHEDULE = "site_visit_reschedule"
    POST_VISIT_FEEDBACK = "post_visit_feedback"
    WHATSAPP_PACKAGE = "whatsapp_package"
    WHATSAPP_FOLLOWUP_24H = "whatsapp_followup_24h"


class NumberPool(StrEnum):
    # Reference Technopolis model (Technopolisss (2) / AUTONOMOUS_CALLING_IMPLEMENTATION_PLAN):
    #   Sandbox 1 (cold campaign):  P1/P2/P3 fresh · P4 retry-2 · P5 retry-3
    #   Relationship (shared):      P6/P7 — callbacks, follow-ups, visit reminders, feedback
    #   Sandbox 2 (digital):        P8 fresh · P9 retries (attempts 2 & 3)
    SANDBOX1_FRESH = "sandbox1_fresh"        # P1, P2, P3 — cold campaign fresh calls
    SANDBOX1_RETRY_2 = "sandbox1_retry_2"    # P4 — attempt 2 after 12 working hours
    SANDBOX1_RETRY_3 = "sandbox1_retry_3"    # P5 — attempt 3 after 24 working hours
    RELATIONSHIP = "relationship"            # P6, P7 — callbacks/follow-ups/visits/feedback
    SANDBOX2_FRESH = "sandbox2_fresh"        # P8 — digital-marketing fresh calls
    SANDBOX2_RETRY = "sandbox2_retry"        # P9 — digital attempts 2 & 3
    # Canonical four-sandbox pools. Legacy members above remain readable for
    # old queued rows, while all new jobs use these explicit contracts.
    SANDBOX1_DIGITAL = "sandbox1_digital"
    SANDBOX1_CALLBACK = "sandbox1_callback"
    SANDBOX2_RETRY_2 = "sandbox2_retry_2"
    SANDBOX2_RETRY_3_COLD = "sandbox2_retry_3_cold"
    SANDBOX2_RETRY_3_DIGITAL = "sandbox2_retry_3_digital"
    SANDBOX2_CALLBACK = "sandbox2_callback"
    SANDBOX3_NURTURE = "sandbox3_nurture"
    SANDBOX4_FEEDBACK = "sandbox4_feedback"
    WHATSAPP = "whatsapp"


TERMINAL_STAGES = frozenset({
    LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.LOST, LeadStage.OPTED_OUT,
})

ALLOWED_TRANSITIONS: dict[LeadStage, frozenset[LeadStage]] = {
    LeadStage.NEW: frozenset({LeadStage.CAMPAIGN_CALLING, LeadStage.OPTED_OUT}),
    LeadStage.CAMPAIGN_CALLING: frozenset({LeadStage.FAILED_RETRY_WAITING, LeadStage.CONNECTED, LeadStage.OPTED_OUT}),
    LeadStage.FAILED_RETRY_WAITING: frozenset({LeadStage.CAMPAIGN_CALLING, LeadStage.CONNECTED, LeadStage.LOST, LeadStage.OPTED_OUT}),
    LeadStage.CONNECTED: frozenset({LeadStage.INTERESTED, LeadStage.FOLLOW_UP, LeadStage.CALLBACK_REQUESTED, LeadStage.SITE_VISIT_SCHEDULED, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.OPTED_OUT}),
    LeadStage.INTERESTED: frozenset({LeadStage.FOLLOW_UP, LeadStage.CALLBACK_REQUESTED, LeadStage.SITE_VISIT_SCHEDULED, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.OPTED_OUT}),
    LeadStage.FOLLOW_UP: frozenset({LeadStage.FOLLOW_UP, LeadStage.CALLBACK_REQUESTED, LeadStage.SITE_VISIT_SCHEDULED, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.LOST, LeadStage.OPTED_OUT}),
    LeadStage.CALLBACK_REQUESTED: frozenset({LeadStage.FOLLOW_UP, LeadStage.SITE_VISIT_SCHEDULED, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.OPTED_OUT}),
    LeadStage.SITE_VISIT_SCHEDULED: frozenset({LeadStage.SITE_VISIT_SCHEDULED, LeadStage.SITE_VISIT_COMPLETED, LeadStage.FOLLOW_UP, LeadStage.NOT_INTERESTED, LeadStage.OPTED_OUT}),
    LeadStage.SITE_VISIT_COMPLETED: frozenset({LeadStage.FEEDBACK_PENDING, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.OPTED_OUT}),
    LeadStage.FEEDBACK_PENDING: frozenset({LeadStage.FOLLOW_UP, LeadStage.SITE_VISIT_SCHEDULED, LeadStage.BOOKED, LeadStage.NOT_INTERESTED, LeadStage.LOST, LeadStage.OPTED_OUT}),
}


def can_transition(current: LeadStage | str, target: LeadStage | str) -> bool:
    current, target = LeadStage(current), LeadStage(target)
    if current in TERMINAL_STAGES:
        return False
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def require_transition(current: LeadStage | str, target: LeadStage | str) -> None:
    if not can_transition(current, target):
        raise ValueError(f"Invalid lead transition: {current!s} -> {target!s}")


def sandbox_for_stage(stage: LeadStage | str) -> int:
    """Map a lead stage to its sandbox number.

    Sandbox mapping per the implementation plan:
      SB1 (1): Fresh cold + digital first touch
      SB2 (2): Retry engine for failed calls
      SB3 (3): Nurture & site visits (interested leads)
      SB4 (4): Post-visit feedback

    Returns 0 for terminal stages (booked, not_interested, lost, opted_out)
    which no longer belong to any active sandbox.
    """
    STAGE_SANDBOX_MAP = {
        LeadStage.NEW: 1,
        LeadStage.CAMPAIGN_CALLING: 1,
        LeadStage.FAILED_RETRY_WAITING: 2,
        LeadStage.CONNECTED: 1,  # Connected from first touch stays in SB1 context
        LeadStage.INTERESTED: 3,
        LeadStage.FOLLOW_UP: 3,
        # Plan flowchart: scheduled callbacks dial back through Sandbox 1 lines
        # (P1/P2 cold, P3 digital), so a callback-requested lead stays in SB1.
        LeadStage.CALLBACK_REQUESTED: 1,
        LeadStage.SITE_VISIT_SCHEDULED: 3,
        LeadStage.SITE_VISIT_COMPLETED: 4,
        LeadStage.FEEDBACK_PENDING: 4,
        LeadStage.BOOKED: 0,  # Terminal — no active sandbox
        LeadStage.NOT_INTERESTED: 0,
        LeadStage.LOST: 0,
        LeadStage.OPTED_OUT: 0,
    }
    s = LeadStage(stage) if isinstance(stage, str) else stage
    return STAGE_SANDBOX_MAP.get(s, 1)
