"""Strict P1-P9 eligibility for the four-sandbox calling pipeline."""

from __future__ import annotations

from collections.abc import Iterable

from core.workflow_models import JobType, NumberPool


DEFAULT_POOLS: dict[NumberPool, tuple[str, ...]] = {
    NumberPool.SANDBOX1_FRESH: ("P1", "P2"),
    NumberPool.SANDBOX1_DIGITAL: ("P3",),
    NumberPool.SANDBOX1_CALLBACK: ("P1", "P2", "P3"),
    NumberPool.SANDBOX2_RETRY_2: ("P4",),
    NumberPool.SANDBOX2_RETRY_3_COLD: ("P5",),
    NumberPool.SANDBOX2_RETRY_3_DIGITAL: ("P6",),
    NumberPool.SANDBOX2_CALLBACK: ("P4", "P5", "P6"),
    NumberPool.SANDBOX3_NURTURE: ("P7", "P8"),
    NumberPool.SANDBOX4_FEEDBACK: ("P9",),
    NumberPool.WHATSAPP: (),
}


def pool_for(job_type: JobType | str, source: str, attempt_number: int = 0, sandbox: int = 1) -> NumberPool:
    job_type = JobType(job_type)
    source = (source or "").strip().lower()
    digital = source in ("digital", "digital_marketing")
    if job_type in {JobType.WHATSAPP_PACKAGE, JobType.WHATSAPP_FOLLOWUP_24H}:
        return NumberPool.WHATSAPP
    if job_type == JobType.CALLBACK:
        return NumberPool.SANDBOX1_CALLBACK
    if job_type in {
        JobType.INTERESTED_FOLLOWUP,
        JobType.SITE_VISIT_REMINDER_DAY_BEFORE,
        JobType.SITE_VISIT_REMINDER_MORNING,
        JobType.SITE_VISIT_RESCHEDULE,
    }:
        return NumberPool.SANDBOX3_NURTURE
    if job_type == JobType.POST_VISIT_FEEDBACK:
        return NumberPool.SANDBOX4_FEEDBACK
    if job_type == JobType.FRESH_CALL:
        return NumberPool.SANDBOX1_DIGITAL if digital else NumberPool.SANDBOX1_FRESH
    if job_type == JobType.FAILED_RETRY:
        if attempt_number == 2:
            return NumberPool.SANDBOX2_RETRY_2
        if attempt_number == 3:
            return NumberPool.SANDBOX2_RETRY_3_DIGITAL if digital else NumberPool.SANDBOX2_RETRY_3_COLD
        raise ValueError(f"Retry attempt must be 2 or 3, got {attempt_number}")
    raise ValueError(f"No number pool for {job_type}")


def allocate_number(pool: NumberPool | str, busy: Iterable[str] = (), pools=None) -> str | None:
    """Pick a free number for a pool.

    ``busy`` may be a set of busy numbers (legacy, one slot per number) or a
    dict mapping number -> active call count. When a dict is given, a number
    is free while its active count is below how many times it appears in the
    pool tuple. Digital Leads intentionally registers P3 once so uploaded
    leads dial sequentially, one after another.
    """
    pool = NumberPool(pool)
    pool_lines = (pools or DEFAULT_POOLS).get(pool, ())

    if isinstance(busy, dict):
        for n in pool_lines:
            if not n:
                continue
            # Capacity = how many times this number appears in the pool tuple.
            capacity = sum(1 for x in pool_lines if x == n)
            if int(busy.get(str(n), 0)) < capacity:
                return n
        return None

    busy_set = {str(n) for n in busy}
    return next((n for n in pool_lines if n not in busy_set), None)


def relationship_number_for_source(source: str, pools=None) -> str | None:
    """Choose P3 for a digital callback, otherwise P1/P2."""
    pools = configured_pools() if pools is None else pools
    pool = NumberPool.SANDBOX1_DIGITAL if (source or "").lower() in ("digital", "digital_marketing") else NumberPool.SANDBOX1_FRESH
    return next(iter(pools.get(pool, ())), None)


def configured_pools(settings_obj=None) -> dict[NumberPool, tuple[str, ...]]:
    if settings_obj is None:
        from config import settings as settings_obj
    values = {i: str(getattr(settings_obj, f"p{i}_number", "") or "").strip() for i in range(1, 10)}
    try:
        from core.state import _ROLES, get_state
        for role in _ROLES:
            state = get_state(role)
            for i in range(1, 10):
                value = str(state.get(f"p{i}_number", "") or "").strip()
                if value:
                    values[i] = value
    except Exception:
        pass

    def lines(*indexes: int) -> tuple[str, ...]:
        return tuple(values[i] for i in indexes if values[i])

    return {
        NumberPool.SANDBOX1_FRESH: lines(1, 2),
        # Digital Leads are strictly sequential: one active call on P3, then
        # the next queued lead is claimed after that call finishes.
        NumberPool.SANDBOX1_DIGITAL: lines(3),
        NumberPool.SANDBOX1_CALLBACK: lines(1, 2, 3),
        NumberPool.SANDBOX2_RETRY_2: lines(4),
        NumberPool.SANDBOX2_RETRY_3_COLD: lines(5),
        NumberPool.SANDBOX2_RETRY_3_DIGITAL: lines(6),
        NumberPool.SANDBOX2_CALLBACK: lines(4, 5, 6),
        NumberPool.SANDBOX3_NURTURE: lines(7, 8),
        NumberPool.SANDBOX4_FEEDBACK: lines(9),
        NumberPool.WHATSAPP: (),
    }


def validate_live_pools(pools=None, *, allow_shared_test_numbers: bool | None = None) -> list[str]:
    pools = configured_pools() if pools is None else pools
    if allow_shared_test_numbers is None:
        allow_shared_test_numbers = False
    errors: list[str] = []
    required = {
        NumberPool.SANDBOX1_FRESH: "cold first touch",
        NumberPool.SANDBOX1_DIGITAL: "digital first touch",
        NumberPool.SANDBOX1_CALLBACK: "callbacks",
        NumberPool.SANDBOX2_RETRY_2: "retry attempt 2",
        NumberPool.SANDBOX2_RETRY_3_COLD: "cold retry attempt 3",
        NumberPool.SANDBOX2_RETRY_3_DIGITAL: "digital retry attempt 3",
        NumberPool.SANDBOX3_NURTURE: "nurture",
        NumberPool.SANDBOX4_FEEDBACK: "feedback",
    }
    for pool, label in required.items():
        if not pools.get(pool):
            errors.append(f"{pool.value} ({label}) requires at least 1 number")

    # Callback is an intentional union of P1-P3. All physical ownership pools
    # must otherwise be distinct to prevent cross-sandbox calls.
    if allow_shared_test_numbers:
        # Logical P1-P9 may intentionally alias a smaller set of physical test
        # lines. dispatch_once still locks the physical number, preventing two
        # logical jobs from using the same carrier line concurrently.
        return errors
    owners: dict[str, NumberPool] = {}
    for pool, numbers in pools.items():
        if pool in (NumberPool.WHATSAPP, NumberPool.SANDBOX1_CALLBACK, NumberPool.SANDBOX2_CALLBACK):
            continue
        for number in numbers:
            if number in owners:
                # A duplicate within the same pool is a configured concurrency
                # slot. Only a number shared by different pools is an error.
                if owners[number] is not pool:
                    errors.append(f"Number {number} must be different; used by {owners[number].value} and {pool.value}")
            else:
                owners[number] = pool
    return errors
