"""Build FastAPI app: routers, static UI, OpenAPI (/docs safe)."""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from loguru import logger
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from api.lifespan import lifespan
from api.routes import (
    callbacks_router,
    campaign_router,
    cases_router,
    console_router,
    events_router,
    health_router,
    schedules_router,
    ui_router,
    vobiz_router,
    web_voice_router,
    auth_router,
    whatsapp_router,
    whatsapp_proxy_router,
    orchestration_router,
    sandbox_overview_router,
    dashboard_router,
    mcp_router,
)
from pathlib import Path
from config import FRONTEND_DIR
from core.auth import limiter


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vernika Bridge",
        version="2.0.0-bridge",
        lifespan=lifespan,
        description=(
            "**Operator console (HTML):** [GET /](/) · [GET /ui](/ui) · [GET /console](/console) · "
            "[GET /bridge](/bridge) — not the same as this API explorer."
        ),
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.include_router(ui_router)
    app.include_router(health_router)
    app.include_router(vobiz_router)
    app.include_router(web_voice_router)
    app.include_router(campaign_router)
    app.include_router(console_router)
    app.include_router(cases_router)
    app.include_router(schedules_router)
    app.include_router(callbacks_router)
    app.include_router(auth_router)
    app.include_router(whatsapp_router)
    app.include_router(whatsapp_proxy_router)
    app.include_router(events_router)
    app.include_router(orchestration_router)
    app.include_router(sandbox_overview_router)
    app.include_router(dashboard_router)
    app.include_router(mcp_router)

    static_dir = FRONTEND_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    media_dir = FRONTEND_DIR.parent / "backend" / "media"
    if media_dir.is_dir():
        app.mount("/media", StaticFiles(directory=str(media_dir)), name="media")

    def openapi_with_fallback() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        hooks = getattr(app, "webhooks", None)
        webhooks_routes = getattr(hooks, "routes", []) if hooks is not None else []

        cand: dict[str, Any] = {
            "title": app.title,
            "version": app.version,
            "openapi_version": app.openapi_version,
            "description": app.description,
            "routes": app.routes,
            "webhooks": webhooks_routes,
        }
        for key, attr in (
            ("summary", "summary"),
            ("terms_of_service", "terms_of_service"),
            ("contact", "contact"),
            ("license_info", "license_info"),
            ("tags", "openapi_tags"),
            ("servers", "servers"),
            ("separate_input_output_schemas", "separate_input_output_schemas"),
            ("external_docs", "openapi_external_docs"),
        ):
            if hasattr(app, attr):
                cand[key] = getattr(app, attr)

        filtered = {k: v for k, v in cand.items() if k in inspect.signature(get_openapi).parameters}
        try:
            schema = get_openapi(**filtered)
        except Exception as exc:  # noqa: BLE001 — documented fallback for Swagger
            logger.exception("OpenAPI generation failed — Swagger will use minimal schema.")
            schema = {
                "openapi": "3.1.0",
                "info": {
                    "title": app.title,
                    "version": app.version,
                    "description": (
                        (app.description or "")
                        + "\n\n_Tip: Full OpenAPI could not be built; see server logs.\nReason: {!r}_".format(
                            exc,
                        ),
                    ),
                },
                "paths": {
                    "/health": {"get": {"summary": "Health", "responses": {"200": {"description": "OK"}}}},
                    "/vobiz/answer": {
                        "post": {"summary": "Vobiz answer POST", "responses": {"200": {"description": "XML"}}},
                        "get": {"summary": "Vobiz answer GET", "responses": {"200": {"description": "XML"}}},
                    },
                },
            }
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_fallback  # type: ignore[method-assign]

    return app


app = create_app()
