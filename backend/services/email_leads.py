"""Auto-send project details via email when customer requests it during a call.

Uses Gmail SMTP (smtp.gmail.com:587) with app password.
Configure via .env: SMTP_EMAIL, SMTP_APP_PASSWORD.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import Any
from urllib.parse import quote as _url_quote

from loguru import logger

from config import settings


_PROJECT_DETAILS_HTML = """
<html>
<body style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto;">
  <div style="background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); padding: 30px; text-align: center; border-radius: 12px 12px 0 0;">
    <h1 style="color: #fff; margin: 0; font-size: 24px;">Solitaire Unity</h1>
    <p style="color: #ccc; margin: 5px 0 0;">A ready-to-move gated community by Technopolis Constructions</p>
  </div>

  <div style="padding: 25px; background: #f9f9f9; border: 1px solid #e0e0e0;">
    <h2 style="color: #1a1a2e; border-bottom: 2px solid #e74c3c; padding-bottom: 8px;">Project Overview</h2>

    <table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
      <tr>
        <td style="padding: 8px 12px; font-weight: bold; color: #555; width: 40%;">Location</td>
        <td style="padding: 8px 12px;">Kondapur, Hyderabad &mdash; prime IT corridor</td>
      </tr>
      <tr style="background: #f0f0f0;">
        <td style="padding: 8px 12px; font-weight: bold; color: #555;">Project</td>
        <td style="padding: 8px 12px;">396 premium apartments on 2.24 acres</td>
      </tr>
      <tr>
        <td style="padding: 8px 12px; font-weight: bold; color: #555;">Status</td>
        <td style="padding: 8px 12px;">Ready to move &mdash; OC received, RERA registered</td>
      </tr>
    </table>

    <h2 style="color: #1a1a2e; border-bottom: 2px solid #e74c3c; padding-bottom: 8px;">Configurations</h2>
    <table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
      <tr>
        <td style="padding: 8px 12px; font-weight: bold; color: #555; width: 40%;">2 BHK</td>
        <td style="padding: 8px 12px;">1,225&ndash;1,615 sq.ft | from &#8377;1.34 Cr</td>
      </tr>
      <tr style="background: #f0f0f0;">
        <td style="padding: 8px 12px; font-weight: bold; color: #555;">2.5 BHK</td>
        <td style="padding: 8px 12px;">1,555 sq.ft | approx &#8377;1.34 Cr</td>
      </tr>
      <tr>
        <td style="padding: 8px 12px; font-weight: bold; color: #555;">3 BHK</td>
        <td style="padding: 8px 12px;">1,655&ndash;2,300 sq.ft | from &#8377;1.34 Cr</td>
      </tr>
    </table>

    <h2 style="color: #1a1a2e; border-bottom: 2px solid #e74c3c; padding-bottom: 8px;">Amenities</h2>
    <ul style="line-height: 1.8;">
      <li>32,000 sq.ft clubhouse &mdash; fully operational</li>
      <li>Swimming pool, gymnasium, children&rsquo;s play area</li>
      <li>Jogging track, indoor games, multipurpose hall</li>
      <li>24/7 security with CCTV, landscaped gardens</li>
      <li>All major banks approved for home loans</li>
    </ul>

    <div style="text-align: center; margin: 25px 0;">
      <a href="__WA_LINK__"
         style="display: inline-block; padding: 12px 30px; background: #25D366; color: #fff; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 14px;">
        Chat on WhatsApp
      </a>
      &nbsp;&nbsp;
      <a href="tel:__CALL_LINK__"
         style="display: inline-block; padding: 12px 30px; background: #e74c3c; color: #fff; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 14px;">
        Call Us
      </a>
    </div>
  </div>

  <div style="padding: 15px; text-align: center; color: #888; font-size: 12px; background: #f0f0f0; border-radius: 0 0 12px 12px; border: 1px solid #e0e0e0; border-top: 0;">
    Technopolis Constructions Private Limited | Solitaire Unity, Kondapur, Hyderabad<br>
    This is an automated response. For queries, call +91 80654 80885.
  </div>
</body>
</html>
"""

_PROJECT_DETAILS_TEXT = """Solitaire Unity — Ready-to-Move Gated Community by Technopolis Constructions

Location: Kondapur, Hyderabad — prime IT corridor
Status: Ready to move — OC received, RERA registered
Project: 396 premium apartments on 2.24 acres

Configurations:
• 2 BHK — 1,225–1,615 sq.ft | from ₹1.34 Cr
• 2.5 BHK — 1,555 sq.ft | approx ₹1.34 Cr
• 3 BHK — 1,655–2,300 sq.ft | from ₹1.34 Cr

Amenities:
• 32,000 sq.ft clubhouse — fully operational
• Swimming pool, gymnasium, children's play area
• Jogging track, indoor games, multipurpose hall
• 24/7 security with CCTV, landscaped gardens
• All major banks approved for home loans

For more details or to schedule a site visit, reply to this email or call +91 80654 80885.
"""


def _smtp_configured() -> bool:
    return bool(settings.smtp_email and settings.smtp_app_password)


def _send_email_project_details_sync(
    to_email: str,
    subject: str,
    summary: str,
    outbound_phone: str = "",
) -> dict[str, Any]:
    try:
        import os
        from email.mime.application import MIMEApplication

        # Create mixed container to hold both message body and PDF attachment
        msg = MIMEMultipart("mixed")
        msg["From"] = f"Technopolis Constructions <{settings.smtp_email}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        # Create alternative container for text & HTML bodies
        msg_body = MIMEMultipart("alternative")

        # Plain text fallback
        body_text = _PROJECT_DETAILS_TEXT
        if summary:
            body_text = f"{summary}\n\n---\n\n{body_text}"
        msg_body.attach(MIMEText(body_text, "plain"))

        # HTML version
        body_html = _resolve_email_links(_PROJECT_DETAILS_HTML, outbound_phone=outbound_phone)
        if summary:
            summary_html = f'<div style="padding:15px;background:#fff3cd;border-radius:8px;margin-bottom:20px;border:1px solid #ffc107;"><strong>Note:</strong> {summary}</div>'
            body_html = body_html.replace("<div", f"{summary_html}<div", 1)
        msg_body.attach(MIMEText(body_html, "html"))

        # Attach alternative body to mixed message
        msg.attach(msg_body)

        # Locate and attach brochure PDF
        pdf_path = "/opt/technopolis/backend/media/solitaire_unity_brochure.pdf"
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(os.path.dirname(__file__), "..", "media", "solitaire_unity_brochure.pdf")
            pdf_path = os.path.abspath(pdf_path)

        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_data = f.read()
            pdf_attachment = MIMEApplication(pdf_data, _subtype="pdf")
            pdf_attachment.add_header("Content-Disposition", "attachment", filename="Solitaire_Unity_Brochure.pdf")
            msg.attach(pdf_attachment)
            logger.info("Attached brochure PDF from {}", pdf_path)
        else:
            logger.warning("Brochure PDF file NOT found at {} to attach", pdf_path)

        # Send via configured SMTP server
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings.smtp_email, settings.smtp_app_password)
            server.sendmail(settings.smtp_email, to_email, msg.as_string())

        logger.info("Project details email sent to {}", to_email)
        return {"sent": True, "to": to_email}

    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check SMTP_APP_PASSWORD (use Gmail App Password, not account password)")
        return {"sent": False, "error": "SMTP authentication failed"}
    except Exception as e:
        logger.exception("Email send failed to {}: {}", to_email, e)
        return {"sent": False, "error": str(e)}


async def send_email_project_details(
    to_email: str,
    subject: str = "Solitaire Unity — Premium Row Villas | Project Details",
    summary: str = "",
    outbound_phone: str = "",
) -> dict[str, Any]:
    """Send Solitaire Unity project details via email asynchronously using asyncio.to_thread.

    Uses Gmail SMTP (smtp.gmail.com:587) with app password.
    """
    if not to_email or "@" not in to_email:
        return {"sent": False, "error": "invalid email address"}

    if not _smtp_configured():
        return {"sent": False, "error": "SMTP not configured (set SMTP_EMAIL and SMTP_APP_PASSWORD in .env)"}

    import asyncio
    return await asyncio.to_thread(_send_email_project_details_sync, to_email, subject, summary, outbound_phone)


async def send_report_email(
    to_email: str = "chinmay@pitchxai.com",
) -> dict[str, Any]:
    """Generate and send the EOD Excel report via email.

    Runs synchronously in a thread. Creates a multi-sheet workbook
    (SUMMARY + per-category sheets) and attaches it to an email.
    """
    if not _smtp_configured():
        return {"sent": False, "error": "SMTP not configured"}

    import asyncio
    return await asyncio.to_thread(_send_report_email_sync, to_email)


def _send_report_email_sync(to_email: str) -> dict[str, Any]:
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    from datetime import datetime
    import zoneinfo

    try:
        from services.excel_report import generate_excel_report_workbook
    except ImportError:
        # Fallback: try direct import when called from worker context
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from services.excel_report import generate_excel_report_workbook

    tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(tz)
    date_str = now_ist.strftime("%Y-%m-%d")

    workbook_bytes = generate_excel_report_workbook()

    msg = MIMEMultipart()
    msg["From"] = f"Technopolis Constructions <{settings.smtp_email}>"
    msg["To"] = to_email
    msg["Subject"] = f"Technopolis Constructions — Daily Call Report ({date_str})"

    body = f"""\
Dear Team,

Please find attached the end-of-day call report for {date_str}.

This report covers all data sources (Luxury Car Data, New Luxury Car Data, MyGate Data, Recco Data, Doctors Data, and any other active sources).

Regards,
Technopolis Constructions — Automated Reporting System
"""
    msg.attach(MIMEText(body, "plain"))

    attachment = MIMEApplication(workbook_bytes, _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    attachment.add_header("Content-Disposition", "attachment", filename=f"daily_report_{date_str}.xlsx")
    msg.attach(attachment)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings.smtp_email, settings.smtp_app_password)
        server.sendmail(settings.smtp_email, to_email, msg.as_string())

    logger.info("EOD report sent to {} for date {}", to_email, date_str)
    return {"sent": True, "to": to_email, "date": date_str}


# ── Solitaire Unity media paths (mirrors whatsapp_leads.py) ─────────

_SERVICES_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR2 = os.path.dirname(_SERVICES_DIR)
_MEDIA_DIR_VPS = "/opt/technopolis/backend/media/whatsapp/"
_MEDIA_DIR_DEV = os.path.join(_BACKEND_DIR2, "media", "whatsapp")


def _media_path_email(filename: str) -> str:
    vps = os.path.join(_MEDIA_DIR_VPS, filename)
    if os.path.exists(vps):
        return vps
    dev = os.path.join(_MEDIA_DIR_DEV, filename)
    if os.path.exists(dev):
        return dev
    return vps


_PROJECT_DETAILS_EMAIL_BODY_TEXT = _PROJECT_DETAILS_TEXT

_PROJECT_DETAILS_EMAIL_HTML = _PROJECT_DETAILS_HTML


def _resolve_email_links(html: str, outbound_phone: str = "") -> str:
    """Replace __WA_LINK__ (business WhatsApp) and __CALL_LINK__ (outbound dialer line)."""
    from core.phone_norm import norm_phone_str
    from services.whatsapp_leads import resolve_whatsapp_business_number, _EMAIL_WA_PREFILL

    wa_num = resolve_whatsapp_business_number().replace("+", "").strip()
    wa_msg = _url_quote(_EMAIL_WA_PREFILL)
    wa_link = f"https://wa.me/{wa_num}?text={wa_msg}" if wa_num else "#"
    call_digits = norm_phone_str(outbound_phone or "").lstrip("+")
    call_link = f"+{call_digits}" if call_digits else ""
    html = html.replace("__WA_LINK__", wa_link)
    html = html.replace("__CALL_LINK__", call_link)
    return html


def _attach_media(msg: MIMEMultipart, rel_subdir: str = "") -> list[str]:
    """Attach Solitaire Unity media files to an email message (no videos — too large for email).
    Returns list of attached filenames.
    """
    media_files = [
        ("solitaire_unity_image.jpeg", "image/jpeg"),
        ("solitaire_unity_price_sheet.pdf", "application/pdf"),
        ("solitaire_unity_brochure.pdf", "application/pdf"),
    ]
    attached = []
    for fname, mime in media_files:
        fpath = _media_path_email(fname)
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                data = f.read()
            part = MIMEApplication(data, _subtype=mime.split("/")[-1])
            part.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(part)
            attached.append(fname)
        else:
            logger.warning("Media file not found for email: {}", fpath)
    return attached


async def send_bulk_project_email(
    to_email: str,
    summary: str = "",
    lead_name: str = "",
    outbound_phone: str = "",
) -> dict[str, Any]:
    """Send the full Solitaire Unity package with all media attachments via email."""
    if not to_email or "@" not in to_email:
        return {"sent": False, "error": "invalid email address"}
    if not _smtp_configured():
        return {"sent": False, "error": "SMTP not configured"}
    import asyncio
    return await asyncio.to_thread(_send_bulk_project_email_sync, to_email, summary, lead_name, outbound_phone)


def _send_bulk_project_email_sync(
    to_email: str,
    summary: str = "",
    lead_name: str = "",
    outbound_phone: str = "",
) -> dict[str, Any]:
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    greeting = f"Hi {lead_name}, " if lead_name else ""
    greeting += "Thank you for your interest in Solitaire Unity."
    body_text = f"{greeting}\n\n{_PROJECT_DETAILS_EMAIL_BODY_TEXT}"
    if summary:
        body_text = f"{summary}\n\n---\n\n{body_text}"

    msg = MIMEMultipart("mixed")
    msg["From"] = f"Technopolis Constructions <{settings.smtp_email}>"
    msg["To"] = to_email
    msg["Subject"] = "🌴 Solitaire Unity — Premium Spanish‑Themed Villas | Project Details"

    # Alternative body (text + html)
    msg_body = MIMEMultipart("alternative")
    msg_body.attach(MIMEText(body_text, "plain"))
    body_html = _resolve_email_links(_PROJECT_DETAILS_EMAIL_HTML, outbound_phone=outbound_phone)
    if summary:
        summary_html = (
            f'<div style="padding:15px;background:#fff3cd;border-radius:8px;'
            f'margin-bottom:20px;border:1px solid #ffc107;">'
            f"<strong>Note:</strong> {summary}</div>"
        )
        body_html = body_html.replace("<div", f"{summary_html}<div", 1)
    if lead_name:
        greeting_html = f'<p style="font-size:15px;color:#1a1a2e;">Hi <strong>{lead_name}</strong>,</p>'
        body_html = body_html.replace("<p", f"{greeting_html}<p", 1)
    msg_body.attach(MIMEText(body_html, "html"))
    msg.attach(msg_body)

    # Attach media files
    attached = _attach_media(msg)

    # Send
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings.smtp_email, settings.smtp_app_password)
            server.sendmail(settings.smtp_email, to_email, msg.as_string())
        logger.info("Bulk project email sent to {} with attachments: {}", to_email, attached)
        return {"sent": True, "to": to_email, "attachments": attached}
    except Exception as e:
        logger.exception("Bulk email send failed to {}: {}", to_email, e)
        return {"sent": False, "error": str(e)}
