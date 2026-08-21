#!/usr/bin/env python3
"""CLI: capture Gemini Live native opening → greeting_{role}.pcm.

Usage (from repo root, with .env loaded):
  cd backend && PYTHONPATH=. python3 scripts/capture_live_greeting.py --role sales_1 \\
    --text "Hi this is Vernika from Technopolis Constructions. How are you doing today?"

Then deploy data/greetings/greeting_sales_1.pcm to the server or run on VPS.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# backend/ on path when run as scripts/capture_live_greeting.py
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Gemini Live greeting PCM for a role")
    parser.add_argument(
        "--role",
        default="sales_1",
        help="Console role (default: sales_1)",
    )
    parser.add_argument(
        "--text",
        required=True,
        help="Exact opening line the model should speak",
    )
    parser.add_argument(
        "--variant",
        default="",
        help="Optional suffix, e.g. inbound → greeting_{role}_inbound.pcm",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=50.0,
        help="Max seconds to wait for Live opening turn",
    )
    args = parser.parse_args()

    from services.live_greeting_capture import capture_live_greeting_pcm, save_greeting_pcm_file

    pcm, sr = await capture_live_greeting_pcm(
        args.role,
        args.text,
        timeout_sec=args.timeout,
    )
    path = save_greeting_pcm_file(args.role, pcm, sr, variant=args.variant)
    print(f"OK: {path} ({len(pcm)} bytes @ {sr} Hz)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
