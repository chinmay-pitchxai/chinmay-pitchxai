"""WhatsApp message templates for the real estate lead workflow."""

from __future__ import annotations
from datetime import datetime


def greeting_after_call(lead_name: str, agent_name: str = "Vernika") -> str:
    """Send after a successful connected call where lead is interested."""
    return (
        f"Hi {lead_name}! 👋\n\n"
        f"This is {agent_name} from *Technopolis Constructions*.\n\n"
        f"Thank you for your time on the call today. "
        f"I'm sharing some property details that match your requirements.\n\n"
        f"Feel free to ask any questions — I'm here to help! 😊"
    )


def property_details(lead_name: str, property_name: str, location: str, price: str, highlights: str = "") -> str:
    """Share property details with the lead."""
    msg = (
        f"🏠 *{property_name}*\n"
        f"📍 {location}\n"
        f"💰 {price}\n\n"
    )
    if highlights:
        msg += f"✨ *Highlights:*\n{highlights}\n\n"
    msg += (
        f"Would you like to schedule a site visit? "
        f"I can arrange a convenient time for you.\n\n"
        f"Reply *YES* to schedule or call us for more info."
    )
    return msg


def visit_scheduling_prompt(lead_name: str) -> str:
    """Ask the lead to confirm a visit date."""
    return (
        f"Hi {lead_name}! 📅\n\n"
        f"We'd love to show you the property in person.\n\n"
        f"Please reply with:\n"
        f"• Your preferred *date* (e.g. Saturday, Sunday)\n"
        f"• Preferred *time* (e.g. morning, afternoon, 11am)\n\n"
        f"Our executive will confirm the slot shortly."
    )


def visit_confirmed(lead_name: str, visit_date: str, visit_time: str, address: str) -> str:
    """Confirm a scheduled visit."""
    return (
        f"✅ *Visit Confirmed!*\n\n"
        f"Hi {lead_name}, your site visit is scheduled:\n\n"
        f"📅 *Date:* {visit_date}\n"
        f"🕐 *Time:* {visit_time}\n"
        f"📍 *Address:* {address}\n\n"
        f"Our executive will meet you at the venue.\n"
        f"For any changes, please call us."
    )


def visit_reminder(lead_name: str, visit_date: str, visit_time: str) -> str:
    """Send 1 day before the visit."""
    return (
        f"Hi {lead_name}! 🔔\n\n"
        f"Just a friendly reminder about your site visit tomorrow:\n\n"
        f"📅 *Date:* {visit_date}\n"
        f"🕐 *Time:* {visit_time}\n\n"
        f"We're excited to show you the property!\n"
        f"See you there 🏠"
    )


def morning_of_visit(lead_name: str, visit_time: str, address: str) -> str:
    """Send on the morning of the visit."""
    return (
        f"Good morning {lead_name}! ☀️\n\n"
        f"Looking forward to seeing you today at *{visit_time}*.\n\n"
        f"📍 *Venue:* {address}\n\n"
        f"See you soon!"
    )


def followup_after_no_show(lead_name: str) -> str:
    """Send after a missed visit."""
    return (
        f"Hi {lead_name},\n\n"
        f"We missed you at the site visit today. "
        f"No worries — things happen! 😊\n\n"
        f"Would you like to reschedule? "
        f"Just reply with a new preferred date and time."
    )


def alternative_options(lead_name: str) -> str:
    """When lead is not interested in the shown property."""
    return (
        f"Hi {lead_name},\n\n"
        f"We understand this property might not be the perfect fit. "
        f"We have other options that might interest you:\n\n"
        f"• Different locations\n"
        f"• Various budget ranges\n"
        f"• 1BHK to 4BHK options\n\n"
        f"Would you like me to share some alternatives?"
    )


def final_booking(lead_name: str, property_name: str) -> str:
    """When lead is ready to book."""
    return (
        f"🎉 *Great news, {lead_name}!*\n\n"
        f"Your booking for *{property_name}* has been initiated.\n\n"
        f"Our team will contact you shortly with the next steps.\n"
        f"Thank you for choosing *Technopolis Constructions*!"
    )


def archieve_lead(lead_name: str) -> str:
    """Archive a lost lead."""
    return (
        f"Hi {lead_name},\n\n"
        f"Thank you for your interest. "
        f"We'll keep you updated on new properties that match your needs.\n\n"
        f"Feel free to reach out anytime!"
    )


# --- Inbound (reply parsing) ---

INTEREST_KEYWORDS = frozenset({
    "yes", "yeah", "yep", "sure", "interested", "ok", "okay", "confirm",
    "schedule", "visit", "book", "weekend", "tomorrow", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
})

NOT_INTERESTED_KEYWORDS = frozenset({
    "no", "nah", "not interested", "busy", "later", "maybe", "cancel",
    "stop", "unsubscribe", "don't call",
})

RESCHEDULE_KEYWORDS = frozenset({
    "reschedule", "change", "another", "different", "new date", "new time",
})

BROCHURE_KEYWORDS = frozenset({
    "brochure", "details", "information", "info", "project", "property",
    "floor plan", "floor plans", "layout", "pricing", "price", "cost",
    "payment", "emi", "loan", "amenities", "clubhouse", "parking",
    "2bhk", "3bhk", "bhk", "bedroom", "carpet", "sqft", "area",
})

VISIT_KEYWORDS = frozenset({
    "visit", "site visit", "schedule", "book", "appointment",
    "come and see", "show me", "tour", "show flat", "sample flat",
})


def classify_reply(text: str) -> str:
    """Classify an inbound WhatsApp reply. Returns one of:
    'interested', 'not_interested', 'reschedule', 'brochure_request', 'unknown'
    """
    low = (text or "").strip().lower()
    if not low:
        return "unknown"
    if any(k in low for k in NOT_INTERESTED_KEYWORDS):
        return "not_interested"
    if any(k in low for k in RESCHEDULE_KEYWORDS):
        return "reschedule"
    # Visit request = interested
    if any(k in low for k in VISIT_KEYWORDS):
        return "interested"
    # Brochure / project detail request
    if any(k in low for k in BROCHURE_KEYWORDS):
        return "brochure_request"
    if any(k in low for k in INTEREST_KEYWORDS):
        return "interested"
    return "unknown"
