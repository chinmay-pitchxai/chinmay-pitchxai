"""Safe local orchestration supervisor that promotes due jobs."""

from __future__ import annotations

import asyncio
import threading
import time

from loguru import logger

from core.storage import _get_conn, new_db_connection
from core.workflow_queue import promote_due
from core.number_allocator import configured_pools, validate_live_pools
from config import settings


def live_orchestration_enabled() -> bool:
    return bool(settings.orchestration_live_enabled)


# Handle for the live supervisor task created by lifespan (and respawned by
# ensure_orchestration_running). Guarded by supervisor_running() so a dead
# supervisor can be restarted without ever spawning a second concurrent one.
_SUPERVISOR_TASK: "asyncio.Task | None" = None


def register_supervisor_task(task: asyncio.Task) -> None:
    global _SUPERVISOR_TASK
    _SUPERVISOR_TASK = task


def supervisor_running() -> bool:
    task = _SUPERVISOR_TASK
    return bool(task and not task.done())


async def ensure_orchestration_running() -> tuple[bool, str]:
    """Best-effort respawn of the live workflow-queue supervisor.

    Returns ``(respawned, detail)``. No-op in shadow mode (queue promotion is
    handled by the scheduler loop there) and when a supervisor is already alive.
    """
    status = runtime_status()
    if status["mode"] != "live":
        return False, f"orchestration mode={status['mode']}"
    if not settings.orchestration_live_enabled:
        return False, "live orchestration not requested"
    if supervisor_running():
        return False, "orchestration supervisor already running"
    task = asyncio.create_task(
        asyncio.to_thread(lambda: asyncio.run(orchestration_supervisor())),
        name="autonomous-orchestration-supervisor",
    )
    register_supervisor_task(task)
    logger.info("Orchestration supervisor restarted by ensure_orchestration_running()")
    return True, "orchestration supervisor restarted"


def runtime_status() -> dict:
    pools = configured_pools(settings)
    errors = validate_live_pools(
        pools,
        allow_shared_test_numbers=settings.orchestration_allow_shared_test_numbers,
    )
    requested = live_orchestration_enabled()
    return {
        "mode": "live" if requested and not errors else "shadow",
        "live_requested": requested,
        "live_ready": not errors,
        "configuration_errors": errors,
        "pools": {k.value: len(v) for k, v in pools.items()},
    }


class _LineCooldown:
    """Anti-spam per-line rest interval (plan Phase 4 "Spam Buffer"): a from-number
    cannot be redialed until `orchestration_inter_call_gap_sec` seconds after its
    last call ended. Shared by all dispatcher workers; no-op when the gap is <= 0."""

    def __init__(self) -> None:
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, number: str) -> bool:
        gap = settings.orchestration_inter_call_gap_sec
        if gap <= 0:
            return False
        with self._lock:
            return self._next_allowed.get(number, 0.0) > time.time()

    def record(self, number: str) -> None:
        gap = settings.orchestration_inter_call_gap_sec
        if gap <= 0 or not number:
            return
        with self._lock:
            self._next_allowed[number] = time.time() + gap


_NUMBER_COOLDOWN = _LineCooldown()


def _auto_relaunch_repeating_campaigns(conn) -> int:
    """Re-queue repeating campaigns when their scheduled run time arrives.

    Reads each console role's campaign_config (repeat_type / schedule_at).
    For a repeating campaign whose queue has fully drained (all jobs terminal)
    and whose next scheduled run is due, re-enqueues FRESH_CALL jobs for its
    leads (via the same idempotent path used by campaign start) and stamps
    next_run_at so the cycle continues for daily/weekly repeats.

    Returns the number of campaigns relaunched this tick.
    """
    import time as _time

    launched = 0
    try:
        from core.state import _ROLES, get_campaign_config, save_campaign_config

        for role in _ROLES:
            cfg = dict(get_campaign_config(role) or {})
            repeat = str(cfg.get("repeat_type") or "one_time").lower()
            if repeat == "one_time":
                continue
            # A run is "done" when no workflow job for this campaign's source
            # is still active.
            source_filter = "digital" if str(cfg.get("lead_source") or "").lower() == "digital" else "campaign"
            active = conn.execute(
                """SELECT COUNT(*) FROM workflow_jobs j
                JOIN leads l ON l.id=j.lead_id
                WHERE j.status IN ('scheduled','ready','claimed','running')
                  AND l.source=?""",
                (source_filter,),
            ).fetchone()
            if active and int(active[0] or 0) > 0:
                continue  # still running
            now_epoch = _time.time()
            next_run = float(cfg.get("next_run_at") or 0)
            if next_run and next_run > now_epoch:
                continue  # not due yet

            # Due: re-enqueue the campaign's leads for another pass.
            rows = conn.execute(
                "SELECT id FROM leads WHERE role=? AND source=?",
                (role, source_filter),
            ).fetchall()
            if not rows:
                continue
            from core.orchestration_service import schedule_job
            from core.workflow_models import JobType
            from datetime import datetime, timezone

            feed_id = f"repeat:{repeat}:{int(now_epoch)}"
            queued = 0
            for (lead_id,) in rows:
                try:
                    schedule_job(
                        conn, lead_id=int(lead_id), job_type=JobType.FRESH_CALL,
                        source=source_filter, due_at=datetime.now(timezone.utc),
                        key=f"{feed_id}:{int(lead_id)}", attempt=1,
                        source_type="campaign_repeat", source_id=feed_id,
                        payload={"repeat_type": repeat},
                    )
                    queued += 1
                except Exception:
                    pass  # duplicate key = already queued
            if queued:
                # Stamp the next run: daily → +24h, weekly → +7d.
                import datetime as _dt
                interval_h = 24 if repeat in ("daily", "day") else 7 * 24
                cfg["next_run_at"] = now_epoch + interval_h * 3600
                cfg["last_run_at"] = now_epoch
                try:
                    save_campaign_config(role, cfg)
                except Exception:
                    pass
                launched += 1
                logger.info("Auto-relaunched repeating campaign {} ({} jobs, next in {}h)", role, queued, interval_h)
    except Exception:
        logger.exception("Auto-relaunch repeating campaigns check failed")
    return launched


async def orchestration_supervisor() -> None:
    status = runtime_status()
    logger.info("Autonomous orchestration supervisor started in {} mode", status["mode"])
    if status["live_requested"] and not status["live_ready"]:
        logger.error("Live orchestration refused: {}", "; ".join(status["configuration_errors"]))
    if status["mode"] == "live":
        from core.live_job_executor import execute_phone_job, execute_whatsapp_job
        from core.orchestration_dispatcher import dispatch_once
        from core.workflow_queue import (
            recover_completed_pending_phone_jobs,
            recover_jobless_pending_digital_leads,
        )
        pools = configured_pools(settings)
        # busy_numbers: number -> active concurrent call count. Digital Leads
        # registers P3 once, enforcing one-after-another dispatch.
        busy_numbers: dict[str, int] = {}

        repair_conn = new_db_connection()
        try:
            recovered = recover_completed_pending_phone_jobs(repair_conn)
            if recovered:
                logger.warning("Recovered {} incomplete phone workflow job(s)", recovered)
            jobless = recover_jobless_pending_digital_leads(repair_conn)
            if jobless:
                logger.warning("Queued {} jobless pending Digital Lead(s)", jobless)
        finally:
            repair_conn.close()

        async def dispatcher_worker(worker_id: int) -> None:
            conn = new_db_connection()
            try:
                while True:
                    job = None
                    try:
                        job = await dispatch_once(
                            conn, pools=pools, busy_numbers=busy_numbers,
                            phone_executor=execute_phone_job, whatsapp_executor=execute_whatsapp_job,
                            lease_seconds=settings.orchestration_lease_seconds,
                            number_cooling=_NUMBER_COOLDOWN,
                        )
                        if job:
                            logger.info("Dispatcher {} completed workflow job {}", worker_id, job["id"])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Dispatcher worker {} failed", worker_id)
                    await asyncio.sleep(0.25 if job else max(0.25, settings.orchestration_poll_seconds))
            finally:
                conn.close()

        # SQLite is a single-writer store; keep one queue dispatcher there so a
        # second synchronous BEGIN/UPDATE cannot block the FastAPI event loop
        # while a call outcome is being persisted. The app now runs on
        # PostgreSQL (multi-writer safe), so the worker count is configurable.
        worker_count = settings.orchestration_worker_count
        workers = [asyncio.create_task(dispatcher_worker(i + 1)) for i in range(worker_count)]

        async def _repeat_relauncher() -> None:
            """Every 60s: re-queue repeating campaigns whose run is due."""
            conn = new_db_connection()
            try:
                while True:
                    try:
                        _auto_relaunch_repeating_campaigns(conn)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Repeat-relauncher tick failed")
                    await asyncio.sleep(60)
            finally:
                conn.close()

        workers.append(asyncio.create_task(_repeat_relauncher()))
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
        return

    while True:
        try:
            promoted = await asyncio.to_thread(promote_due, _get_conn(), time.time())
            if promoted:
                logger.info("Orchestration promoted {} due workflow job(s)", promoted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Orchestration supervisor iteration failed")
        await asyncio.sleep(max(0.25, settings.orchestration_poll_seconds))
