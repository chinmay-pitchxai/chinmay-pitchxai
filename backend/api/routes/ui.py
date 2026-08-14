"""Serve operator / VPS console HTML under ``frontend/``."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger
from starlette.responses import RedirectResponse

from config import FRONTEND_DIR, _BACKEND_DIR

router = APIRouter(tags=["ui"])

# Homepage order: Technopolis Voice Calling Dashboard (project root), then
# VPS Pro dashboard, then slim bridge fallback.
_MAIN_PAGE_CANDIDATES = ("console.html", "index.html")

# VPS pages link to `/static/styles.css`; files also served by path below.
_HTML_NAMES = frozenset(
    {
        "console.html",
        "index.html",
        "login.html",
        "voice_test.html",
    }
)

# Technopolis dashboard bundle lives at the project root (served alongside the
# FastAPI app): index.html + app.js + index.css + kpi_modal.js.
_DASHBOARD_ROOT = _BACKEND_DIR.parent


from typing import Optional

def _resolve_main_path() -> Optional[Path]:
    # 1. Technopolis Voice Calling Dashboard (project root) — preferred home.
    root_index = _DASHBOARD_ROOT / "index.html"
    if root_index.is_file():
        return root_index
    # 2. VPS console bundle fallback.
    for name in _MAIN_PAGE_CANDIDATES:
        p = FRONTEND_DIR / "templates" / name
        if p.is_file():
            return p
    return None


def _stub_response() -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html><body><h1>Vernika Bridge</h1>"
        "<p>No console HTML — add <code>frontend/templates/console.html</code> "
        "(or <code>frontend/templates/index.html</code>). Try <code>GET /health</code>.</p></body></html>",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _html_response_from_file(path: Path) -> HTMLResponse:
    html = path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def html_main_dashboard():
    """`GET /` / `/ui`: prefer ``console.html`` from VPS bundle."""
    main = _resolve_main_path()
    if not main:
        logger.warning("No main frontend at {} ([console.html | index.html]).", FRONTEND_DIR)
        return _stub_response()
    return _html_response_from_file(main)


async def html_whitelisted_file(name: str):
    """Serve a template *.html without path traversal."""
    if name not in _HTML_NAMES:
        return HTMLResponse("Not found", status_code=404)
    path = FRONTEND_DIR / "templates" / name
    if not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/")
async def serve_home():
    return html_main_dashboard()


_DASHBOARD_JS_NAMES = frozenset({"app.js", "kpi_modal.js", "index.css"})


def _serve_dashboard_asset(name: str):
    path = _DASHBOARD_ROOT / name
    if not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    media_type = "text/javascript" if name.endswith(".js") else "text/css"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/app.js", include_in_schema=False)
async def serve_dashboard_app_js():
    return _serve_dashboard_asset("app.js")


@router.get("/kpi_modal.js", include_in_schema=False)
async def serve_dashboard_kpi_modal_js():
    return _serve_dashboard_asset("kpi_modal.js")


@router.get("/index.css", include_in_schema=False)
async def serve_dashboard_index_css():
    return _serve_dashboard_asset("index.css")


@router.get("/digital-sheets-apps-script.js", include_in_schema=False)
async def serve_digital_sheets_apps_script():
    path = FRONTEND_DIR / "digital-sheets-apps-script.js"
    if not path.is_file():
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(
        path,
        media_type="text/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/bridge")
async def serve_bridge_alias():
    return html_main_dashboard()


@router.get("/login")
async def serve_login_alias():
    """Short path — SPA and operators often hit ``/login`` without ``.html``."""
    return await html_whitelisted_file("login.html")


@router.get("/index.html")
async def serve_index_html_explicit():
    path = FRONTEND_DIR / "index.html"
    if path.is_file():
        return _html_response_from_file(path)
    return html_main_dashboard()


@router.get("/ui")
async def serve_ui_alias():
    return html_main_dashboard()


@router.get("/console")
async def serve_console_alias():
    # Redirect to the Technopolis Voice Calling Dashboard (root index.html)
    return RedirectResponse(url="/", status_code=307)


@router.get("/console.html", include_in_schema=False)
async def serve_console_explicit():
    return await html_whitelisted_file("console.html")


@router.get("/dashboard", include_in_schema=False)
async def redirect_dashboard_to_console():
    """Legacy URL used by older clients — canonical console is `/console`."""
    return RedirectResponse(url="/console", status_code=307)


@router.get("/login.html", include_in_schema=False)
async def serve_login_explicit():
    return await html_whitelisted_file("login.html")


@router.get("/voice_test.html", include_in_schema=False)
async def serve_voice_test_explicit():
    return await html_whitelisted_file("voice_test.html")
