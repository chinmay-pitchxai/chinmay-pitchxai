"""Application lifespan (DB init, shutdown)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path as _Path

from fastapi import FastAPI
from loguru import logger

from config import FRONTEND_DIR, settings, validate_critical_config
from core.state import _CAMPAIGN_TASKS, init_state
from core.storage import (
    close_db,
    init_db,
    roles_with_campaign_run_wanted,
    set_campaign_want_running,
)
from core.worker import _scheduler_loop
from core.orchestration_runtime import (
    ensure_orchestration_running,
    orchestration_supervisor,
    live_orchestration_enabled,
    register_supervisor_task,
)
from services.vobiz_bridge import close_vobiz_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    logger.info("Starting bridge server…")
    data_root = (os.environ.get("VERN_DATA_DIR") or "").strip()
    if data_root:
        data_dir = os.path.abspath(data_root)
    else:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    init_db(data_dir)
    from core.storage import cleanup_orphaned_operational_rows
    orphan_cleanup = cleanup_orphaned_operational_rows()
    if any(orphan_cleanup.values()):
        logger.warning("Removed orphaned operational rows on startup: {}", orphan_cleanup)
    init_state()

    async def _async_bg_startup():
        await asyncio.sleep(0.05)  # allow uvicorn event loop to start processing HTTP immediately
        try:
            from core.storage import sync_lead_log_ids_from_attempts_sync
            n = await asyncio.to_thread(sync_lead_log_ids_from_attempts_sync)
            if n:
                logger.info("Backfilled _log_id on {} lead(s) from call_attempts", n)
        except Exception as exc:
            logger.warning("call_attempts _log_id backfill skipped: {}", exc)

        # Pre-load dashboard states asynchronously in worker thread
        try:
            from core.dashboard_state import get_dashboard_state
            for r in ["sales_1"]:
                await asyncio.to_thread(get_dashboard_state, r)
        except Exception as exc:
            logger.warning("Pre-loading dashboard states failed: {}", exc)

        # Per-role sandbox: refresh packaged prompt/RAG and coerce cross-role greetings.
        try:
            from core.role_sandbox import sync_all_role_sandboxes_on_startup
            await asyncio.to_thread(sync_all_role_sandboxes_on_startup)
        except Exception as exc:
            logger.warning("Role sandbox startup sync skipped: {}", exc)

        if not settings.gemini_api_key:
            logger.warning("GEMINI_API_KEY not set — AI will fail")
        else:
            try:
                from core.gemini_auth import init_gemini_api_key
                await asyncio.to_thread(init_gemini_api_key)
            except Exception as exc:
                logger.warning("Gemini API key validation skipped: {}", exc)


        # Build RAG FTS index if missing
        try:
            from pathlib import Path as _Path
            from scripts.build_rag_db import gather_files, DEFAULT_SOURCE_DIRS, DEFAULT_FILE_GLOBS
            from rag import RagStore
            _rag_path = _Path(settings.rag_db_path)
            if settings.rag_enabled and not _rag_path.exists():
                files = gather_files(DEFAULT_SOURCE_DIRS, DEFAULT_FILE_GLOBS)
                if files:
                    store = RagStore(str(_rag_path))
                    n = store.build_from_files(files)
                    logger.info("Built RAG DB at {} ({} chunks from {} files)", _rag_path, n, len(files))
        except Exception as _rag_exc:
            logger.warning("RAG DB auto-build skipped: {}", _rag_exc)

    asyncio.create_task(_async_bg_startup())


    fe_index = FRONTEND_DIR / "templates" / "console.html"
    if not fe_index.is_file():
        fe_index = FRONTEND_DIR / "templates" / "index.html"
    if fe_index.is_file():
        logger.info(
            "Operator UI: http://127.0.0.1:{}/  (file {})",
            settings.port,
            fe_index,
        )
    else:
        logger.error(
            "Frontend console missing at {} — GET / will show a stub. "
            "Keep ``frontend/templates/console.html`` next to ``backend/`` (or rebuild Docker).",
            fe_index,
        )
    logger.info("OpenAPI / Swagger: http://127.0.0.1:{}/docs", settings.port)

    if settings.webhook_only_mode:
        logger.info(
            "WEBHOOK_ONLY_MODE=true — skipping outbound dialer auto-resume on this host "
            "(Vobiz callbacks + live calls only)."
        )
    else:
        vobiz_host = (settings.vobiz_public_base_url or "").rstrip("/")
        logger.info(
            "Local dialer mode — dashboard at http://127.0.0.1:{}/ ; Vobiz webhooks → {}",
            settings.port,
            vobiz_host or "(VOBIZ_PUBLIC_BASE_URL not set)",
        )

    try:
        from core.state import get_lead_counts as _gcd
        from core.worker import (
            _campaign_worker_role,
            _schedule_preflight,
            release_orphaned_dialing_leads,
        )

        from core.storage import is_campaign_globally_paused

        if settings.webhook_only_mode:
            resume_roles = []
        elif await is_campaign_globally_paused():
            logger.info("Global campaign pause is active — outbound dialers will not auto-resume.")
            resume_roles = []
        else:
            resume_roles = await roles_with_campaign_run_wanted()
        if live_orchestration_enabled():
            logger.info("Legacy campaign auto-resume skipped; autonomous orchestration owns outbound dialing.")
            resume_roles = []
        for r_role in resume_roles:
            ct = _gcd(r_role)
            if int(ct.get("pending", 0) or 0) <= 0 and int(ct.get("dialing", 0) or 0) <= 0:
                await set_campaign_want_running(r_role, False)
                continue
            why = await _schedule_preflight(r_role)
            if why:
                from core.campaign_hours import is_campaign_quiet_hours, quiet_hours_block_message
                # Start it anyway when blocked only by quiet hours / calling window so the
                # worker sleeps and auto-resumes when the window opens.
                if is_campaign_quiet_hours() and why == quiet_hours_block_message():
                    pass
                elif "calling window" in why:
                    pass
                else:
                    logger.warning("Campaign runner resume deferred role={}: {}", r_role, why)
                    try:
                        if is_campaign_quiet_hours():
                            await set_campaign_want_running(r_role, False)
                            await release_orphaned_dialing_leads(
                                r_role,
                                error="Campaign stopped: outside the campaign's calling window.",
                            )
                    except Exception:
                        pass
                    continue
            existing = _CAMPAIGN_TASKS.get(r_role)
            if existing and not existing.done():
                continue
            _CAMPAIGN_TASKS[r_role] = asyncio.create_task(_campaign_worker_role(r_role))
            logger.info(
                "Resumed outbound dialer role={} (operator had Start before last restart)",
                r_role,
            )
    except Exception as exc:
        logger.warning("Campaign runner auto-resume skipped: {}", exc)

    scheduler_task = None
    orchestration_task = None
    digital_excel_task = None
    google_sheets_task = None
    if settings.webhook_only_mode:
        logger.info("WEBHOOK_ONLY_MODE — campaign scheduler not started on this host.")
    else:
        scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("Campaign scheduler background task started.")
        # Start the autonomous orchestration supervisor (workflow queue consumer)
        # Routes jobs through the 4-sandbox pipeline (P1-P9)
        if live_orchestration_enabled():
            # The SQLite queue performs synchronous transactional claims. Run
            # its event loop on a worker thread so busy-timeout waits can never
            # freeze HTTP/WS handling on the FastAPI loop.
            orchestration_task = asyncio.create_task(
                asyncio.to_thread(lambda: asyncio.run(orchestration_supervisor())),
                name="autonomous-orchestration-supervisor",
            )
            register_supervisor_task(orchestration_task)
            logger.info("Autonomous orchestration supervisor started (workflow queue consumer).")
        else:
            logger.info("Orchestration supervisor disabled (ORCHESTRATION_LIVE_ENABLED=false).")
        if settings.digital_excel_path:
            # Ensure the Sandbox 1.2 feed directory exists so the watcher has
            # something to poll on first boot (channel partners drop files here).
            try:
                _digital_excel_dir = _Path(settings.digital_excel_path).expanduser()
                _digital_excel_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Sandbox 1.2 digital Excel feed path ready: {}", _digital_excel_dir)
            except Exception as _dex_exc:
                logger.warning("Could not ensure digital Excel feed path: {}", _dex_exc)
            from services.digital_excel_ingest import digital_excel_watcher

            digital_excel_task = asyncio.create_task(
                digital_excel_watcher(), name="sandbox-1-2-digital-excel-watcher"
            )
            logger.info("Sandbox 1.2 digital Excel watcher started.")
        if settings.digital_broker_1_sheet_url or settings.digital_broker_2_sheet_url or settings.digital_broker_3_sheet_url:
            from services.google_sheets_ingest import google_sheets_watcher
            google_sheets_task = asyncio.create_task(
                google_sheets_watcher(), name="sandbox-1-2-google-sheets-watcher"
            )
            logger.info("Sandbox 1.2 Google Sheets watcher started.")

    agents_task = None
    boss_task = None
    panther_task = None
    if settings.webhook_only_mode:
        logger.info("WEBHOOK_ONLY_MODE — health agents and Super Boss disabled on this host.")
    else:
        try:
            from services.health_agents import start_health_agents

            agents_task = await start_health_agents()
            if agents_task:
                logger.info("Self-healing health agents started.")
        except Exception as exc:
            logger.warning("Health agents startup skipped: {}", exc)

        try:
            from services.supervisor import start_super_boss

            boss_task = await start_super_boss()
            if boss_task:
                logger.info("Super Boss parent supervisor started.")
        except Exception as exc:
            logger.warning("Super Boss startup skipped: {}", exc)

        panther_task = None
        try:
            from services.supervisor.panther_mode import panther_background_loop

            watch_sec = float(os.getenv("PANTHER_WATCH_INTERVAL_SEC", "0") or "0")
            if watch_sec > 0 and os.getenv("PANTHER_AUTO_FIX_ENABLED", "true").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                panther_task = asyncio.create_task(panther_background_loop())
                logger.info("Panther auto-watch started (interval={}s)", watch_sec)
        except Exception as exc:
            logger.warning("Panther auto-watch startup skipped: {}", exc)

    logger.info("Bridge ready on {}:{}", settings.host, settings.port)
    yield

    try:
        from services.health_agents import stop_health_agents

        await stop_health_agents()
    except Exception:
        pass
    try:
        from services.supervisor import stop_super_boss

        await stop_super_boss()
    except Exception:
        pass
    if boss_task and not boss_task.done():
        boss_task.cancel()
    if panther_task and not panther_task.done():
        panther_task.cancel()
    if agents_task and not agents_task.done():
        agents_task.cancel()

    if scheduler_task and not scheduler_task.done():
        scheduler_task.cancel()
        logger.info("Campaign scheduler stopped.")
    if orchestration_task and not orchestration_task.done():
        orchestration_task.cancel()
        logger.info("Autonomous orchestration supervisor stopped.")
    if digital_excel_task and not digital_excel_task.done():
        digital_excel_task.cancel()
        logger.info("Sandbox 1.2 digital Excel watcher stopped.")
    if google_sheets_task and not google_sheets_task.done():
        google_sheets_task.cancel()
        logger.info("Sandbox 1.2 Google Sheets watcher stopped.")
    for role, task in list(_CAMPAIGN_TASKS.items()):
        if task and not task.done():
            task.cancel()
            logger.info("Cancelled task for {}", role)
    await close_vobiz_client()
    close_db()
    logger.info("Shutdown complete")
