"""Safe local orchestration supervisor that promotes due jobs."""

from __future__ import annotations

import asyncio
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


async def orchestration_supervisor() -> None:
    status = runtime_status()
    logger.info("Autonomous orchestration supervisor started in {} mode", status["mode"])
    if status["live_requested"] and not status["live_ready"]:
        logger.error("Live orchestration refused: {}", "; ".join(status["configuration_errors"]))
    if status["mode"] == "live":
        from core.live_job_executor import execute_phone_job, execute_whatsapp_job
        from core.orchestration_dispatcher import dispatch_once
        pools = configured_pools(settings)
        busy_numbers: set[str] = set()

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

        # SQLite is a single-writer store. Keep one queue dispatcher so a second
        # synchronous BEGIN/UPDATE cannot block the FastAPI event loop while a
        # call outcome is being persisted. Multi-line concurrency can be enabled
        # when this queue moves to PostgreSQL.
        worker_count = 1
        workers = [asyncio.create_task(dispatcher_worker(i + 1)) for i in range(worker_count)]
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
