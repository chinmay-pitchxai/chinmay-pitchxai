"""Brochure and project detail templates for Solitaire Unity (real estate)."""

from __future__ import annotations


# ── Project constants ──
PROJECT_NAME = "Solitaire Unity"
PROJECT_DEVELOPER = "Technopolis Constructions"
PROJECT_LOCATION = "Kondapur, Hyderabad"
PROJECT_TOTAL_UNITS = 352
PROJECT_ACRES = 2.24
PROJECT_TYPES = "2BHK & 3BHK"
PROJECT_PRICE_SQFT = "Rs. 9,799"
PROJECT_PRICE_START = "Rs. 1.34 Cr"
PROJECT_CLUBHOUSE = "32,000 sqft"
PROJECT_STATUS = "Ready to Move (OC Received)"
PROJECT_HIGHLIGHTS = (
    "17+ years experience · 6 completed projects\n"
    "Premium 32,000 sqft clubhouse\n"
    "Prime Kondapur location — close to IT corridor\n"
    "Ready-to-move with OC received\n"
    "Landscaped gardens & children's play area\n"
    "24/7 security with CCTV surveillance"
)
PROJECT_ADDRESS = "Solitaire Unity, Kondapur, Hyderabad, Telangana"

# ── PDF & location URLs ──
PUBLIC_BASE = "https://srv1782911.hstgr.cloud"
BROCHURE_PDF_URL = f"{PUBLIC_BASE}/static/brochures/brochure.pdf"
PRICING_PDF_URL = f"{PUBLIC_BASE}/static/brochures/pricing.pdf"
LOCATION_MAPS_URL = "https://maps.app.goo.gl/eJ8dJdQA5mNwbKvt5?g_st=aw"

def get_public_base() -> str:
    """Return the current public base URL (from .env tunnel URL or fallback)."""
    try:
        from config import settings
        base = (settings.server_url or settings.vobiz_public_base_url or "").strip().rstrip("/")
        if base:
            return base
    except Exception:
        pass
    return PUBLIC_BASE

def get_brochure_url() -> str:
    return f"{get_public_base()}/static/brochures/brochure.pdf"

def get_pricing_url() -> str:
    return f"{get_public_base()}/static/brochures/pricing.pdf"


def greeting_with_location(lead_name: str) -> str:
    return (
        f"Hi {lead_name}! 👋\n\n"
        f"Thank you for your interest in *{PROJECT_NAME}* by {PROJECT_DEVELOPER}.\n\n"
        f"📍 *Location:* {PROJECT_ADDRESS}\n"
        f"🗺️ *Maps Link:* {LOCATION_MAPS_URL}\n\n"
        f"Feel free to ask any questions — I'm here to help! 😊"
    )


async def send_greeting_with_location(phone: str, lead_name: str) -> dict:
    from services.whatsapp.client import send_text_meta
    name = (lead_name or "there").strip()
    msg = (
        f"Hi {name}, 🤝\n\n"
        f"Thank you for your interest in *Technopolis Constructions Private Limited* — *Solitaire Unity, Kondapur*! 🏗️\n\n"
        f"We're excited to share more about our premium ready-to-move project.\n\n"
        f"I'm sending you the brochure and price quotation below 👇\n\n"
        f"Feel free to ask any questions! 😊"
    )
    return await send_text_meta(phone, msg)


async def send_brochure_pdf(phone: str, lead_name: str) -> dict:
    from services.whatsapp.client import send_document_meta
    return await send_document_meta(
        phone,
        get_brochure_url(),
        caption=f"📄 *Solitaire Unity — Brochure*\n{PROJECT_NAME} | {PROJECT_LOCATION} | {PROJECT_STATUS}",
        filename="Solitaire Unity - Brochure.pdf",
    )


async def send_pricing_pdf(phone: str, lead_name: str) -> dict:
    from services.whatsapp.client import send_document_meta
    return await send_document_meta(
        phone,
        get_pricing_url(),
        caption=f"💰 *Solitaire Unity — Price Quotation*\nStarting from {PROJECT_PRICE_START} | OC Received",
        filename="SOLITAIRE UNITY PRICE QUOTATION.pdf",
    )


async def send_full_package(phone: str, lead_name: str) -> None:
    """Send greeting with location → brochure PDF → pricing PDF (sequential)."""
    await send_greeting_with_location(phone, lead_name)
    import asyncio
    await asyncio.sleep(2)
    await send_brochure_pdf(phone, lead_name)
    await asyncio.sleep(2)
    await send_pricing_pdf(phone, lead_name)


def brochure_message(lead_name: str) -> str:
    """Full brochure / project overview message."""
    return (
        f"Hi {lead_name}! 👋\n\n"
        f"Thank you for your interest in *{PROJECT_NAME}* by {PROJECT_DEVELOPER}.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏗 *{PROJECT_NAME}*\n"
        f"📍 {PROJECT_LOCATION}\n"
        f"🏢 {PROJECT_TOTAL_UNITS} Premium Apartments · {PROJECT_ACRES} Acres\n"
        f"🏠 {PROJECT_TYPES}\n"
        f"💰 Starting at *{PROJECT_PRICE_START}* ({PROJECT_PRICE_SQFT}/sqft)\n"
        f"✅ {PROJECT_STATUS}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ *Highlights:*\n{PROJECT_HIGHLIGHTS}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Floor Plans:*\n"
        f"• 2BHK — 1,100 to 1,250 sqft\n"
        f"• 3BHK — 1,650 to 1,950 sqft\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Would you like to:\n"
        f"• 📅 Schedule a *site visit*?\n"
        f"• 💰 Get *pricing & payment plan* details?\n"
        f"• 📄 Receive *floor plans* on WhatsApp?\n\n"
        f"Reply with your choice or call us anytime! 😊"
    )


def floor_plans_message(lead_name: str) -> str:
    """Floor plan details message."""
    return (
        f"📐 *{PROJECT_NAME} — Floor Plans*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏠 *2BHK*\n"
        f"• Super Built-up: 1,100 – 1,250 sqft\n"
        f"• Carpet Area: ~850 – 975 sqft\n"
        f"• 2 Bedrooms + 2 Bathrooms + Balcony\n"
        f"• Price: {PROJECT_PRICE_SQFT}/sqft\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏠 *3BHK*\n"
        f"• Super Built-up: 1,650 – 1,950 sqft\n"
        f"• Carpet Area: ~1,280 – 1,520 sqft\n"
        f"• 3 Bedrooms + 3 Bathrooms + 2 Balconies\n"
        f"• Price: {PROJECT_PRICE_SQFT}/sqft\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"All units feature:\n"
        f"• Vastu-compliant design\n"
        f"• Premium vitrified tile flooring\n"
        f"• Modular kitchen provisions\n"
        f"• Split AC wiring in all bedrooms\n\n"
        f"Want to schedule a *site visit*? Just reply *VISIT* 📅"
    )


def pricing_message(lead_name: str) -> str:
    """Pricing and payment plan details."""
    return (
        f"💰 *{PROJECT_NAME} — Pricing & Payment Plan*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏠 *2BHK*\n"
        f"• Flat Cost: {PROJECT_PRICE_START} onwards\n"
        f"• EMI starts ~₹{chr(8377)}42,000/month*\n\n"
        f"🏠 *3BHK*\n"
        f"• Flat Cost: Rs. 1.82 Cr onwards\n"
        f"• EMI starts ~₹{chr(8377)}58,000/month*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💳 *Payment Plan:*\n"
        f"• 10% on booking\n"
        f"• 80% during construction (linked to milestones)\n"
        f"• 10% on possession\n\n"
        f"🏦 *Home Loan Available:*\n"
        f"• Up to 80% financing from top banks\n"
        f"• Competitive interest rates\n"
        f"• Quick approval process\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏰ *Early bird benefits:*\n"
        f"• Preferred floor selection\n"
        f"• Flexible payment options\n\n"
        f"Interested? Reply *VISIT* to schedule a site visit! 🏗"
    )


def visit_scheduling_message(lead_name: str) -> str:
    """Prompt for scheduling a site visit."""
    return (
        f"📅 *Schedule Your Site Visit*\n\n"
        f"Hi {lead_name}!\n\n"
        f"We'd love to show you *{PROJECT_NAME}* in person.\n\n"
        f"📍 *Address:* {PROJECT_ADDRESS}\n\n"
        f"Please reply with:\n"
        f"• Your preferred *date* (e.g. Saturday, Sunday, tomorrow)\n"
        f"• Preferred *time* (e.g. morning, afternoon, 11am)\n\n"
        f"Our executive will confirm your slot shortly.\n\n"
        f"🕐 Visits available: 10 AM – 7 PM, all days"
    )


def brochure_request_response(lead_name: str) -> str:
    """When someone asks for brochure/project details via WhatsApp."""
    return (
        f"Hi {lead_name}! 📄\n\n"
        f"Here are the details for *{PROJECT_NAME}*:\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏗 *{PROJECT_NAME}*\n"
        f"📍 {PROJECT_LOCATION}\n"
        f"🏢 {PROJECT_TOTAL_UNITS} Apartments · {PROJECT_ACRES} Acres\n"
        f"🏠 {PROJECT_TYPES}\n"
        f"💰 From *{PROJECT_PRICE_START}*\n"
        f"✅ {PROJECT_STATUS}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ *Key Highlights:*\n{PROJECT_HIGHLIGHTS}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"What would you like to know more about?\n"
        f"• Reply *FLOOR PLAN* for layout details\n"
        f"• Reply *PRICING* for cost & EMI info\n"
        f"• Reply *VISIT* to schedule a site visit\n\n"
        f"Feel free to ask any questions! 😊"
    )


def followup_brochure_message(lead_name: str) -> str:
    """Follow-up message after brochure is shared (next day)."""
    return (
        f"Hi {lead_name}! 👋\n\n"
        f"Just following up on the *{PROJECT_NAME}* brochure I shared earlier.\n\n"
        f"Have you had a chance to review the floor plans and pricing?\n\n"
        f"We have limited units available and early bird benefits are ending soon.\n\n"
        f"Would you like to:\n"
        f"• 📅 Schedule a *site visit* this weekend?\n"
        f"• 💬 Talk to our sales team?\n\n"
        f"Reply *YES* or call us at +918046733382 📞"
    )


# ── Keyword sets for classifying brochure-related inbound messages ──

BROCHURE_KEYWORDS = frozenset({
    "brochure", "details", "information", "info", "project", "property",
    "floor plan", "floor plans", "layout", "pricing", "price", "cost",
    "payment", "emi", "loan", "carpet", "super built", "sqft", "area",
    "amenities", "facilities", "clubhouse", "parking", "location",
    "address", "map", "direction", "how to reach", "visit", "site visit",
    "schedule", "booking", "availability", "units", "flat", "apartment",
    "2bhk", "3bhk", "bhk", "bedroom",
})

VISIT_KEYWORDS = frozenset({
    "visit", "site visit", "schedule", "book", "appointment",
    "come and see", "show me", "tour", "show flat", "sample flat",
})

PRICING_KEYWORDS = frozenset({
    "price", "pricing", "cost", "rate", "emi", "payment", "loan",
    "afford", "budget", "down payment", "installment", "finance",
})

FLOORPLAN_KEYWORDS = frozenset({
    "floor plan", "floor plans", "layout", "blueprint", "map",
    "carpet area", "super built", "sqft", "size", "dimension",
})


def classify_brochure_request(text: str) -> str:
    """Classify an inbound WhatsApp message for brochure-related intent.

    Returns one of: 'brochure', 'visit', 'pricing', 'floorplan', 'unknown'
    """
    low = (text or "").strip().lower()
    if not low:
        return "unknown"

    # Check visit first (highest priority action)
    if any(k in low for k in VISIT_KEYWORDS):
        return "visit"

    # Check pricing
    if any(k in low for k in PRICING_KEYWORDS):
        return "pricing"

    # Check floor plans
    if any(k in low for k in FLOORPLAN_KEYWORDS):
        return "floorplan"

    # Check general brochure request
    if any(k in low for k in BROCHURE_KEYWORDS):
        return "brochure"

    return "unknown"
