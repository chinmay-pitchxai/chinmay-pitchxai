"""Provision Vobiz Applications + attach DIDs for inbound call routing.

Usage (from repo root):
  python backend/scripts/setup_vobiz_inbound.py

Requires backend/.env or VPS env with VOBIZ_SALES_* credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
load_dotenv(BACKEND / ".env")

from config import settings
from core.outbound_numbers import get_all_outbound_numbers
from core.state import normalize_console_role
from services.vobiz_bridge.vobiz_applications import (
    attach_number_to_application,
    create_application,
    list_applications,
    list_phone_numbers,
    update_application,
)


def _public_base() -> str:
    for key in ("VPS_PUBLIC_URL", "VOBIZ_PUBLIC_BASE_URL", "SERVER_URL"):
        val = (os.environ.get(key) or "").strip().rstrip("/")
        if val and val.startswith("http") and "127.0.0.1" not in val and "localhost" not in val:
            return val
    return "https://technopolis.200.97.171.250.nip.io"


async def _ensure_app(
    auth_id: str,
    auth_token: str,
    *,
    friendly_name: str,
    voice_url: str,
    hangup_url: str,
) -> str:
    apps = await list_applications(auth_id, auth_token)
    for app in apps:
        name = str(app.get("app_name") or app.get("friendly_name") or "")
        app_id = str(app.get("app_id") or app.get("id") or "")
        if friendly_name.lower() in name.lower() and app_id:
            await update_application(
                auth_id,
                auth_token,
                app_id,
                answer_url=voice_url,
                answer_method="POST",
                hangup_url=hangup_url,
                hangup_method="POST",
            )
            print(f"  Updated app {app_id} ({name}) -> {voice_url}")
            return app_id

    created = await create_application(
        auth_id,
        auth_token,
        friendly_name=friendly_name,
        voice_url=voice_url,
        hangup_url=hangup_url,
    )
    app_id = str(created.get("app_id") or created.get("id") or "")
    if not app_id:
        raise RuntimeError(f"create_application returned no app_id: {created}")
    print(f"  Created app {app_id} ({friendly_name}) -> {voice_url}")
    return app_id


async def _setup_role(role: str, auth_id: str, auth_token: str, base_url: str) -> None:
    role = normalize_console_role(role)
    incoming_url = f"{base_url}/vobiz/incoming"
    hangup_url = f"{base_url}/vobiz/hangup"
    numbers = get_all_outbound_numbers(role)
    if not auth_id or not auth_token:
        print(f"[SKIP] {role}: missing Vobiz credentials")
        return
    if not numbers:
        print(f"[SKIP] {role}: no phone numbers configured")
        return

    print(f"\n=== {role} (auth={auth_id}) ===")
    app_id = await _ensure_app(
        auth_id,
        auth_token,
        friendly_name=f"Technopolis Inbound {role}",
        voice_url=incoming_url,
        hangup_url=hangup_url,
    )

    for num in numbers:
        try:
            await attach_number_to_application(auth_id, auth_token, app_id, num)
            print(f"  Attached {num} -> app {app_id}")
        except Exception as exc:
            print(f"  FAILED attach {num}: {exc}")


async def main() -> None:
    base = _public_base()
    print(f"Inbound answer URL base: {base}/vobiz/incoming")

    roles = [
        (
            "sales_1",
            settings.vobiz_sales_1_auth_id or os.environ.get("VOBIZ_SALES_1_AUTH_ID", ""),
            settings.vobiz_sales_1_auth_token or os.environ.get("VOBIZ_SALES_1_AUTH_TOKEN", ""),
        ),
    ]

    for role, auth_id, auth_token in roles:
        await _setup_role(role, auth_id, auth_token, base)

    print("\n--- Vobiz account numbers (sales_1) ---")
    aid = settings.vobiz_sales_1_auth_id or os.environ.get("VOBIZ_SALES_1_AUTH_ID", "")
    tok = settings.vobiz_sales_1_auth_token or os.environ.get("VOBIZ_SALES_1_AUTH_TOKEN", "")
    if aid and tok:
        try:
            nums = await list_phone_numbers(aid, tok)
            for n in nums[:20]:
                if isinstance(n, dict):
                    print(
                        " ",
                        n.get("number") or n.get("phone_number") or n,
                        "app=",
                        n.get("application") or n.get("application_id") or n.get("app_id") or "?",
                    )
                else:
                    print(" ", n)
        except Exception as exc:
            print("  list_phone_numbers failed:", exc)


if __name__ == "__main__":
    asyncio.run(main())
