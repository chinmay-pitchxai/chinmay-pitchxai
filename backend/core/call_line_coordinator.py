"""Outbound line coordinator — 4-number pipeline with max-4 concurrency.

After each campaign call a line enters a REST window (120–180s anti-spam gap).
During REST the same line may dial due scheduled callbacks / 24h failed retries.
Lines in REST do not take new campaign leads; idle lines pick up the queue so
two lines per role (sales_1) can run in parallel.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    pass

_REST_LOCK = threading.Lock()
# norm_phone -> rest-until epoch (monotonic wall time)
_PHONE_REST_UNTIL: dict[str, float] = {}


def _norm(phone: str) -> str:
    from core.phone_norm import norm_phone_str
    return norm_phone_str(phone or "")


def mark_phone_rest_start(phone_number: str, gap_sec: float) -> float:
    """Start (or extend) rest window for an outbound line."""
    norm = _norm(phone_number)
    if not norm:
        return 0.0
    until = time.time() + max(0.0, float(gap_sec))
    with _REST_LOCK:
        prev = _PHONE_REST_UNTIL.get(norm, 0.0)
        _PHONE_REST_UNTIL[norm] = max(prev, until)
    logger.info(
        "Line {} resting {:.0f}s (until +{:.0f}s from now)",
        norm,
        gap_sec,
        _PHONE_REST_UNTIL[norm] - time.time(),
    )
    return _PHONE_REST_UNTIL[norm]


def phone_is_resting(phone_number: str) -> bool:
    norm = _norm(phone_number)
    if not norm:
        return False
    with _REST_LOCK:
        until = _PHONE_REST_UNTIL.get(norm, 0.0)
    return time.time() < until


def phone_rest_remaining(phone_number: str) -> float:
    norm = _norm(phone_number)
    if not norm:
        return 0.0
    with _REST_LOCK:
        until = _PHONE_REST_UNTIL.get(norm, 0.0)
    return max(0.0, until - time.time())


def clear_phone_rest(phone_number: str) -> None:
    norm = _norm(phone_number)
    if not norm:
        return
    with _REST_LOCK:
        _PHONE_REST_UNTIL.pop(norm, None)


def snapshot_line_states(phones: list[str]) -> dict[str, str]:
    """Return resting | idle for each configured outbound line (busy tracked elsewhere)."""
    from core.state import phone_is_busy

    out: dict[str, str] = {}
    for p in phones:
        norm = _norm(p)
        if not norm:
            continue
        if phone_is_busy(p):
            out[norm] = "busy"
        elif phone_is_resting(p):
            out[norm] = "resting"
        else:
            out[norm] = "idle"
    return out


def inter_call_gap_sample_sec() -> float:
    """Random inter-call pause (2–3 min default)."""
    lo = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_MIN_SEC", "120"))
    hi = float(os.getenv("CAMPAIGN_INTER_CALL_GAP_MAX_SEC", "180"))
    if hi < lo:
        hi = lo
    return random.uniform(lo, hi)


def failed_retry_delay_sec() -> float:
    """Delay before a failed-call retry while campaign is actively running (legacy opt-in)."""
    lo = float(os.getenv("FAILED_CALL_REST_RETRY_MIN_SEC", "120"))
    hi = float(os.getenv("FAILED_CALL_REST_RETRY_MAX_SEC", "180"))
    if hi < lo:
        hi = lo
    return random.uniform(lo, hi)


def rest_retry_enabled() -> bool:
    return os.getenv("FAILED_CALL_REST_RETRY_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def run_phone_rest_cycle(
    role: str,
    phone_number: str,
    phone_index: int,
    gap_sec: float,
    *,
    campaign_running,
    execute_callback,
    cancellable_sleep,
    continue_existing: bool = False,
) -> None:
    """Rest window: poll for scheduled callbacks / 24h failed retries on this line."""
    from core.state import phone_is_busy

    if gap_sec <= 0 and not (continue_existing and phone_is_resting(phone_number)):
        return

    if continue_existing and phone_is_resting(phone_number):
        end = time.time() + phone_rest_remaining(phone_number)
    else:
        mark_phone_rest_start(phone_number, gap_sec)
        end = time.time() + gap_sec
    polls = 0

    while time.time() < end:
        if not campaign_running():
            return

        remaining = end - time.time()
        if remaining <= 0:
            break

        polls += 1
        if polls == 1 or polls % 10 == 0:
            logger.debug(
                "Rest cycle role={} line={} idx={} remaining={:.0f}s",
                role,
                _norm(phone_number),
                phone_index,
                remaining,
            )

        if phone_is_busy(phone_number):
            await asyncio.sleep(min(1.0, remaining))
            continue

        try:
            from core.storage import claim_next_immediate_callback

            cb = await claim_next_immediate_callback(
                role, time.time(), outbound_phone=phone_number
            )
        except Exception:
            logger.exception("Rest-cycle callback claim failed role={} line={}", role, phone_number)
            cb = None

        if cb:
            from core.state import vobiz_auth_can_accept_call

            if not vobiz_auth_can_accept_call(role):
                logger.info(
                    "Rest-cycle callback deferred — Vobiz at concurrent cap (role={} line={})",
                    role,
                    _norm(phone_number),
                )
                slept = min(5.0, remaining)
                if not await cancellable_sleep(role, slept):
                    return
                continue
            logger.info(
                "Rest-cycle dialing callback/failed-retry id={} on line {} (role={})",
                cb.get("id"),
                _norm(phone_number),
                role,
            )
            try:
                await execute_callback(role, cb, phone_number)
            except Exception:
                logger.exception(
                    "Rest-cycle callback execution failed id={}", cb.get("id")
                )
            # After callback — same 2–3 min anti-spam gap before next campaign dial
            extra_gap = inter_call_gap_sample_sec()
            end = max(end, time.time() + extra_gap)
            mark_phone_rest_start(phone_number, extra_gap)
            continue

        slept = min(2.0, remaining)
        if not await cancellable_sleep(role, slept):
            return

    clear_phone_rest(phone_number)
    logger.info(
        "Line {} rest complete — ready for campaign queue (role={})",
        _norm(phone_number),
        role,
    )
