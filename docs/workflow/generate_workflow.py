#!/usr/bin/env python3
"""Generate PROJECT_WORKFLOW.html — node-type workflow flowchart for Technopolis.

Every node, zone, and edge below was derived from reading the actual codebase:
backend/main.py, backend/api/*.py, backend/core/*.py, backend/services/*,
docker-compose.yml, Dockerfile, Caddyfile, deploy_vps.sh, watchdog/*.

Output: self-contained dark-themed HTML with inline SVG (pan/zoom), legend,
and a node index table. No external dependencies (fonts fall back to monospace).
"""

from __future__ import annotations

import html
import json
import os
import re

# ----------------------------------------------------------------------------
# Colors (semantic categories)
# ----------------------------------------------------------------------------
CATS = {
    "frontend": dict(fill="rgba(8,51,68,0.40)", stroke="#22d3ee", label="Frontend"),
    "backend":  dict(fill="rgba(6,78,59,0.40)",  stroke="#34d399", label="Backend"),
    "db":       dict(fill="rgba(76,29,149,0.40)", stroke="#a78bfa", label="Storage / Data"),
    "external": dict(fill="rgba(30,41,59,0.55)",  stroke="#94a3b8", label="External"),
    "ai":       dict(fill="rgba(88,28,135,0.35)", stroke="#c084fc", label="AI / Gemini Live"),
    "media":    dict(fill="rgba(251,146,60,0.30)", stroke="#fb923c", label="Media / Events"),
    "security": dict(fill="rgba(136,19,55,0.40)", stroke="#fb7185", label="Security / Compliance"),
    "ops":      dict(fill="rgba(120,53,15,0.30)", stroke="#fbbf24", label="Ops / Monitoring"),
}

CANVAS_W, CANVAS_H = 2400, 1960

# ----------------------------------------------------------------------------
# Zones (swimlanes)
# ----------------------------------------------------------------------------
ZONES = [
    ("ENTRY — operator, customers & lead feeds",           30,  90, 2340, 120),
    ("API & WEBHOOK LAYER — FastAPI routers (backend/api/routes)", 30, 240, 2340, 220),
    ("ORCHESTRATION CORE — workflow queue & autonomous dialing",   30, 490, 2340, 230),
    ("CALL PIPELINE — Vobiz ↔ Gemini Live bridge",         30,  750, 2340, 300),
    ("POST-CALL ANALYSIS — transcript → disposition",      30, 1080, 2340, 180),
    ("LEAD LIFECYCLE — 4-sandbox routing (SB1–SB4)",       30, 1290, 2340, 200),
    ("STORAGE — persistence",                               30, 1520, 2340, 160),
    ("OPERATIONS & MONITORING",                             30, 1710, 2340, 190),
]

# ----------------------------------------------------------------------------
# Nodes: id, x, y, w, h, cat, title, lines[], files
# ----------------------------------------------------------------------------
NODES = [
    # ---- ENTRY -------------------------------------------------------------
    ("fe_dashboard", 60, 110, 320, 80, "frontend", "Voice Calling Dashboard",
     ["index.html + app.js + kpi_modal.js", "ApexCharts · 5 nav views · live WS push"],
     "index.html, app.js, kpi_modal.js, index.css"),
    ("ext_wa_customer", 560, 110, 240, 80, "external", "WhatsApp Customer",
     ["sends / receives messages"], "—"),
    ("ext_pstn", 900, 110, 240, 80, "external", "PSTN Caller",
     ["dials sales lines (P1–P9)"], "—"),
    ("feed_digital", 1240, 110, 280, 80, "backend", "Digital Lead Feeds",
     ["Excel watcher + Google Sheets", "digital-excel-ingest · sheets-ingest"],
     "services/digital_excel_ingest.py, services/google_sheets_ingest.py"),
    ("ext_openwa", 1620, 110, 280, 80, "external", "OpenWA Gateway",
     ["linked-device WhatsApp API", "container :2786 · webhooks"],
     "docker-compose.yml (openwa service)"),

    # ---- API / WEBHOOK LAYER ----------------------------------------------
    ("api_campaign", 60, 270, 310, 72, "backend", "Campaign & Leads API",
     ["/api/campaign/* — upload · start · stop", "config · contacts · sources · kpis"],
     "routes/campaign.py"),
    ("api_manual", 410, 270, 310, 72, "backend", "Manual Call · Callbacks · Schedules",
     ["/api/manual/call · /api/callbacks", "/api/schedules · /api/cases"],
     "routes/console_api.py, routes/callbacks.py, routes/schedules.py"),
    ("api_whatsapp", 760, 270, 310, 72, "backend", "WhatsApp Webhooks",
     ["/api/whatsapp/webhook · /api/openwa/webhook", "proxy · send-details"],
     "routes/whatsapp.py, routes/whatsapp_proxy.py"),
    ("api_dashboard", 1110, 270, 310, 72, "backend", "Dashboard & Console API",
     ["/api/dashboard/* · /api/sandbox/*", "/api/orchestration/* · /api/tuning"],
     "routes/dashboard.py, sandbox_overview.py, orchestration.py, console_api.py"),
    ("api_auth", 1460, 270, 310, 72, "security", "Auth (JWT)",
     ["/api/login · /api/me", "console-role guard on /api/*"],
     "routes/auth_api.py, core/auth.py"),
    ("api_vobiz_answer", 60, 380, 310, 72, "backend", "Vobiz Answer URL",
     ["GET/POST /vobiz/answer", "returns <Stream> wss:// XML"],
     "routes/vobiz.py (answer)"),
    ("api_vobiz_incoming", 410, 380, 310, 72, "backend", "Vobiz Incoming",
     ["GET/POST /vobiz/incoming", "routes dialed DID → role"],
     "routes/vobiz.py (incoming)"),
    ("api_hangup", 760, 380, 310, 72, "backend", "Hangup · Recording Webhooks",
     ["/vobiz/hangup · recording-webhook", "CallUUID → camp_id mapping"],
     "routes/vobiz.py (hangup/recording)"),
    ("api_ws_vobiz", 1110, 380, 310, 72, "backend", "WS /ws/vobiz",
     ["media WebSocket endpoint", "camp_id · agent_id · manual_role"],
     "routes/vobiz.py (websocket)"),

    # ---- ORCHESTRATION CORE -----------------------------------------------
    ("orch_queue", 60, 520, 310, 72, "db", "workflow_jobs queue",
     ["scheduled → ready → claimed → running → done", "priority · due_at · eligible_pool"],
     "core/workflow_queue.py, core/workflow_models.py"),
    ("orch_dispatcher", 410, 520, 310, 72, "backend", "Priority Dispatcher",
     ["dispatch_once · claim_next · lease", "busy lock · per-line cooldown"],
     "core/orchestration_dispatcher.py"),
    ("orch_alloc", 760, 520, 310, 72, "backend", "Number Allocator",
     ["P1–P2 cold · P3 digital · P4–P6 retry", "P7–P8 nurture · P9 feedback"],
     "core/number_allocator.py"),
    ("orch_exec_phone", 1110, 520, 310, 72, "backend", "Phone Job Executor",
     ["execute_phone_job → _process_single_lead", "reuses full dial/session infra"],
     "core/live_job_executor.py"),
    ("orch_exec_wa", 1460, 520, 310, 72, "backend", "WhatsApp Job Executor",
     ["whatsapp_package · followup_24h", "no-reply-call scheduling"],
     "core/live_job_executor.py"),
    ("orch_service", 60, 630, 470, 72, "backend", "Orchestration Service (lifecycle)",
     ["schedule_job · failed_call · interested · opt_out", "site visits · feedback · retries · lead memory"],
     "core/orchestration_service.py"),
    ("legacy_worker", 760, 630, 470, 72, "backend", "Campaign Worker (legacy dialer)",
     ["_scheduler_loop · _campaign_worker_role", "per-role dial · inter-call gap 120–180s"],
     "core/worker.py"),

    # ---- CALL PIPELINE -----------------------------------------------------
    ("dial_make", 60, 780, 310, 84, "backend", "make_vobiz_call",
     ["REST POST /Account/{auth}/Call/", "answer_url · record · hangup_url"],
     "services/vobiz_bridge/vobiz_client.py"),
    ("dial_slots", 410, 780, 310, 84, "backend", "Slot & Capacity Guards",
     ["acquire/release Vobiz slot · semaphores", "phone round-robin · hourly caps"],
     "core/state.py, core/worker.py"),
    ("live_session", 760, 780, 560, 100, "ai", "Live Session — handle_vobiz_ws_live",
     ["Vobiz media ↔ Gemini Live bridge", "greeting · name-verify · pitch phases · voicemail · dev-mode"],
     "services/vobiz_bridge/live_session.py"),
    ("live_gemini", 1360, 780, 310, 84, "ai", "Gemini Live API",
     ["models/gemini-3.1-flash-live-preview", "live setup · RAG · turn nudges"],
     "services/vobiz_bridge/gemini_protocol.py, core/gemini_auth.py"),
    ("ext_vobiz", 60, 900, 310, 76, "external", "Vobiz Telephony API",
     ["api.vobiz.ai — dials / answers", "PSTN carrier · recordings"],
     "backend/.env (credentials)"),
    ("live_audio", 410, 900, 310, 76, "media", "Audio Engine",
     ["16k↔24k resample · VAD · noise suppression", "greeting PCM · background mix"],
     "services/vobiz_bridge/audio.py"),
    ("live_turns", 760, 900, 310, 76, "media", "Turn-Taking · Voicemail",
     ["anti-loop · site-visit confirmation", "voicemail screening · classify"],
     "services/vobiz_bridge/turn_taking_addon.py, voicemail.py"),
    ("live_transcript", 1110, 900, 310, 76, "backend", "Transcript JSONL",
     ["append_turn per utterance", "session meta · artifacts"],
     "services/conversation_log.py"),
    ("live_record", 1460, 900, 310, 76, "backend", "Call Recording",
     ["CallRecorder · PCM/wav", "Vobiz recording ingest"],
     "services/call_recording.py, services/vobiz_bridge/vobiz_recording.py"),

    # ---- ANALYSIS ----------------------------------------------------------
    ("an_transcriber", 60, 1110, 340, 80, "backend", "Transcriber (STT)",
     ["audio → text (Gemini STT)", "live JSONL passthrough"],
     "services/transcriber.py"),
    ("an_analyzer", 560, 1110, 340, 80, "ai", "Call Analyzer",
     ["Gemini analysis → heuristic fallback", "local analyzer · canonical disposition"],
     "services/call_analyzer.py, services/gemini_analyzer.py"),
    ("an_lead_update", 1060, 1110, 620, 80, "backend", "Lead Update & Memory",
     ["status/disposition · call_attempts · lead_memory facts", "dashboard-state invalidation · events published"],
     "core/worker.py (_analyze_and_update_lead), core/lead_memory.py"),

    # ---- LIFECYCLE ---------------------------------------------------------
    ("lc_interested", 60, 1320, 310, 80, "backend", "Interested → Sandbox 3",
     ["WhatsApp package + 24h followup", "no-reply call after 2–3 wh"],
     "core/orchestration_service.py (interested)"),
    ("lc_failed", 410, 1320, 310, 80, "backend", "Failed / No-Answer → Sandbox 2",
     ["retry after 12/24 working hours", "attempt 3 → lost (P4–P6)"],
     "core/orchestration_service.py (failed_call)"),
    ("lc_callback", 760, 1320, 310, 80, "backend", "Callback Requested → Sandbox 1",
     ["scheduled callback via P1–P3", "bounded relationship retry"],
     "core/orchestration_service.py (schedule_callback)"),
    ("lc_sitevisit", 1110, 1320, 310, 80, "backend", "Site Visit → Sandbox 3/4",
     ["day-before + morning reminders", "completed → feedback P9"],
     "core/orchestration_service.py (schedule_site_visit)"),
    ("lc_whatsapp", 1460, 1320, 310, 80, "backend", "WhatsApp Senders",
     ["brochure package · templates", "disposition messages · followups"],
     "services/whatsapp/*, services/whatsapp_leads.py"),
    ("lc_optout", 60, 1440, 470, 72, "security", "DNC / Opt-out Register",
     ["do_not_contact · cancels lead jobs", "TRAI compliance guard"],
     "core/dnc.py, core/orchestration_service.py (opt_out)"),

    # ---- STORAGE -----------------------------------------------------------
    ("db_pg", 60, 1550, 1430, 84, "db", "PostgreSQL 16 (prod) · SQLite (dev)",
     ["leads · workflow_jobs · call_attempts · lead_memory · site_visits · feedback_records",
      "do_not_contact · whatsapp_messages · camp_sessions · vobiz_call_map · schedules · cases"],
     "core/storage.py, core/db.py"),
    ("db_media", 1540, 1550, 340, 84, "db", "Media Store",
     ["recordings/ · transcripts/", "greeting PCM · JSONL"],
     "backend/media/, data/"),

    # ---- OPS ---------------------------------------------------------------
    ("ops_events", 60, 1740, 310, 84, "media", "Event Bus · WS Push",
     ["/ws/dashboard · /api/events/stream", "lead/call events → UI"],
     "core/events.py, routes/events.py"),
    ("ops_health", 410, 1740, 310, 84, "ops", "Health Agents",
     ["10 self-healing agents", "concurrency · media · RAG · scheduling…"],
     "services/health_agents/"),
    ("ops_boss", 760, 1740, 310, 84, "ops", "Super Boss · Panther",
     ["parent supervisor · auto-fix loop", "prompts · cleanup"],
     "services/supervisor/"),
    ("ops_watchdog", 1110, 1740, 310, 84, "ops", "Hermes Watchdog (5-min cron)",
     ["VPS sensor script · SSH runner", "safe fixes · TTS alert"],
     "watchdog/"),
    ("ops_deploy", 1460, 1740, 310, 84, "ops", "Deploy · Docker Stack",
     ["tar+scp → compose build+up", "Caddy TLS/WSS · VPS host"],
     "deploy_vps.sh, docker-compose.yml, Caddyfile, Dockerfile"),
]

# ----------------------------------------------------------------------------
# Edges: (src, dst, label, style)  style: solid | dashed | double | security
# ----------------------------------------------------------------------------
EDGES = [
    # entry → api
    ("fe_dashboard", "api_dashboard", "REST /api/*", "solid"),
    ("ext_wa_customer", "ext_openwa", "message", "solid"),
    ("ext_openwa", "api_whatsapp", "webhook", "solid"),
    ("ext_pstn", "api_vobiz_incoming", "answer_url hit", "solid"),
    ("feed_digital", "api_campaign", "feeds", "solid"),
    # api → core
    ("api_campaign", "orch_service", "start / stop / config", "solid"),
    ("api_campaign", "db_pg", "leads insert", "dashed"),
    ("api_manual", "dial_make", "manual dial", "solid"),
    ("api_whatsapp", "lc_whatsapp", "inbound → reply", "solid"),
    # orchestration
    ("orch_service", "orch_queue", "schedule_job", "solid"),
    ("orch_queue", "orch_dispatcher", "due ready jobs", "solid"),
    ("orch_alloc", "orch_dispatcher", "P1–P9 line", "solid"),
    ("orch_dispatcher", "orch_exec_phone", "phone job", "solid"),
    ("orch_dispatcher", "orch_exec_wa", "whatsapp job", "solid"),
    ("orch_exec_phone", "dial_make", "dial", "solid"),
    ("orch_exec_wa", "lc_whatsapp", "send", "solid"),
    ("legacy_worker", "dial_make", "campaign dial", "solid"),
    # dial → vobiz → live session
    ("dial_make", "ext_vobiz", "POST Call/", "solid"),
    ("ext_vobiz", "api_vobiz_answer", "answer_url", "solid"),
    ("ext_vobiz", "api_vobiz_incoming", "incoming", "solid"),
    ("ext_vobiz", "api_hangup", "hangup · recording", "solid"),
    ("ext_vobiz", "api_ws_vobiz", "wss:// stream", "solid"),
    ("api_ws_vobiz", "live_session", "handle_vobiz_ws_live", "solid"),
    ("live_session", "live_gemini", "audio 16k↔24k", "double"),
    ("live_session", "live_audio", "PCM in/out", "solid"),
    ("live_session", "live_turns", "turn logic", "solid"),
    ("live_session", "live_transcript", "turns", "solid"),
    ("live_session", "live_record", "stream", "solid"),
    # hangup → analysis
    ("api_hangup", "an_lead_update", "finalize → analyze", "solid"),
    ("live_transcript", "an_transcriber", "audio + jsonl", "solid"),
    ("an_transcriber", "an_analyzer", "text", "solid"),
    ("an_analyzer", "an_lead_update", "disposition", "solid"),
    # analysis → lifecycle
    ("an_lead_update", "db_pg", "leads · attempts · memory", "solid"),
    ("an_lead_update", "lc_interested", "interested", "solid"),
    ("an_lead_update", "lc_failed", "no answer / failed", "solid"),
    ("an_lead_update", "lc_callback", "callback", "solid"),
    ("an_lead_update", "lc_sitevisit", "visit booked", "solid"),
    ("an_lead_update", "lc_optout", "opted out", "solid"),
    # lifecycle → orchestration (new jobs)
    ("lc_interested", "orch_service", "schedule WA jobs", "dashed"),
    ("lc_failed", "orch_service", "retry 12/24wh", "dashed"),
    ("lc_callback", "orch_service", "SB1 callback", "dashed"),
    ("lc_sitevisit", "orch_service", "reminders + feedback", "dashed"),
    ("lc_optout", "orch_service", "cancel jobs", "dashed"),
    ("lc_whatsapp", "ext_openwa", "send", "solid"),
    # storage
    ("live_transcript", "db_media", "JSONL", "dashed"),
    ("live_record", "db_media", "audio", "dashed"),
    ("db_pg", "api_dashboard", "SQL reads", "solid"),
    # events / observability
    ("an_lead_update", "ops_events", "publish", "dashed"),
    ("ops_events", "fe_dashboard", "WS push", "dashed"),
    ("api_auth", "api_campaign", "JWT guard", "security"),
    ("api_auth", "api_dashboard", "JWT guard", "security"),
    ("ops_health", "ops_boss", "reports", "solid"),
    ("ops_boss", "ops_health", "fixes", "dashed"),
    ("ops_watchdog", "ops_deploy", "SSH safe fixes", "dashed"),
    ("ops_deploy", "api_dashboard", "compose build+up", "dashed"),
]

# ----------------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------------
NODE_MAP = {n[0]: n for n in NODES}


def node_center(nid: str) -> tuple[float, float]:
    _, x, y, w, h, *_ = NODE_MAP[nid]
    return x + w / 2, y + h / 2


def boundary_point(nid: str, dx: float, dy: float) -> tuple[float, float]:
    """Intersection of the center->(dx,dy) ray with the node rect."""
    _, x, y, w, h, *_ = NODE_MAP[nid]
    cx, cy = x + w / 2, y + h / 2
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else float("inf")
    sy = (h / 2) / abs(dy) if dy else float("inf")
    t = min(sx, sy)
    return cx + dx * t, cy + dy * t


def edge_geometry(src: str, dst: str):
    s = node_center(src)
    t = node_center(dst)
    dx, dy = t[0] - s[0], t[1] - s[1]
    p1 = boundary_point(src, dx, dy)
    p2 = boundary_point(dst, -dx, -dy)
    return p1, p2


def esc(s: str) -> str:
    return html.escape(s, quote=True)


# ----------------------------------------------------------------------------
# SVG builders
# ----------------------------------------------------------------------------
def svg_markers() -> str:
    out = []
    colors = {"#94a3b8", "#fb7185", "#fb923c", "#a78bfa", "#fbbf24", "#22d3ee", "#34d399", "#c084fc"}
    for c in colors:
        mid = c.lstrip("#")
        out.append(
            f'<marker id="m-{mid}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M 0 1 L 9 5 L 0 9 z" fill="{c}"/></marker>'
        )
    return "\n".join(out)


def svg_zones() -> str:
    out = []
    for name, x, y, w, h in ZONES:
        out.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="rgba(2,6,23,0.55)" '
            f'stroke="#334155" stroke-width="1" stroke-dasharray="7 5"/>'
        )
        out.append(
            f'<text x="{x + 14}" y="{y + 20}" font-size="12" font-weight="700" '
            f'fill="#cbd5e1" letter-spacing="1.5">{esc(name)}</text>'
        )
    return "\n".join(out)


def svg_node(nid: str) -> str:
    nid, x, y, w, h, cat, title, lines, files = NODE_MAP[nid]
    st = CATS[cat]
    title_lines = [title] + lines
    out = []
    # opaque backing + translucent fill (double-rect masking)
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="#0f172a"/>')
    out.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{st["fill"]}" '
        f'stroke="{st["stroke"]}" stroke-width="1.5"/>'
    )
    # accent bar
    out.append(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{st["stroke"]}"/>')
    ty = y + 18
    out.append(
        f'<text x="{x + 14}" y="{ty}" font-size="12.5" font-weight="700" fill="#e2e8f0">'
        f'{esc(title)}</text>'
    )
    for line in lines:
        ty += 15
        out.append(
            f'<text x="{x + 14}" y="{ty}" font-size="9.5" fill="#94a3b8">{esc(line)}</text>'
        )
    return "\n".join(out)


def svg_edge(src: str, dst: str, label: str, style: str) -> str:
    p1, p2 = edge_geometry(src, dst)
    x1, y1 = p1
    x2, y2 = p2
    color = {
        "solid": "#94a3b8",
        "dashed": "#64748b",
        "double": "#c084fc",
        "security": "#fb7185",
    }[style]
    dash = ' stroke-dasharray="6 4"' if style in ("dashed", "security") else ""
    mid = color.lstrip("#")
    marker_start = f' marker-start="url(#m-{mid})"' if style == "double" else ""
    marker_end = f' marker-end="url(#m-{mid})"'
    stroke_w = 1.4 if style != "double" else 1.8
    out = [
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{color}" stroke-width="{stroke_w}"{dash}{marker_start}{marker_end}/>'
    ]
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        lw = max(46, len(label) * 6.0 + 10)
        out.append(
            f'<rect x="{mx - lw / 2:.1f}" y="{my - 9}" width="{lw:.1f}" height="14" rx="4" '
            f'fill="#0f172a" stroke="{color}" stroke-width="0.7"/>'
        )
        out.append(
            f'<text x="{mx:.1f}" y="{my + 2.5}" font-size="8.5" fill="{color}" '
            f'text-anchor="middle">{esc(label)}</text>'
        )
    return "\n".join(out)


def svg_legend() -> str:
    out = ['<g id="legend">']
    x = 60
    out.append(f'<text x="{x}" y="1925" font-size="11" font-weight="700" fill="#cbd5e1">LEGEND</text>')
    x += 70
    for cat, meta in CATS.items():
        out.append(
            f'<rect x="{x}" y="1912" width="16" height="10" rx="3" fill="{meta["fill"]}" '
            f'stroke="{meta["stroke"]}" stroke-width="1.2"/>'
        )
        out.append(f'<text x="{x + 22}" y="{1921}" font-size="9.5" fill="#94a3b8">{esc(meta["label"])}</text>')
        x += 150
    out.append("</g>")
    return "\n".join(out)


def build_svg() -> str:
    parts = [
        f'<svg id="map" viewBox="0 0 {CANVAS_W} {CANVAS_H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;cursor:grab;touch-action:none;display:block">',
        "<defs>",
        '<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">'
        '<path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/></pattern>',
        svg_markers(),
        "</defs>",
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="#020617"/>',
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="url(#grid)"/>',
        svg_zones(),
    ]
    # edges first (behind nodes), nodes after
    for e in EDGES:
        parts.append(svg_edge(*e))
    for n in NODES:
        parts.append(svg_node(n[0]))
    parts.append(svg_legend())
    parts.append("</svg>")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Node index table (HTML)
# ----------------------------------------------------------------------------
def node_table() -> str:
    rows = []
    for i, (nid, _x, _y, _w, _h, cat, title, _lines, files) in enumerate(NODES, 1):
        rows.append(
            f"<tr><td>{i}</td><td><span class='sw' style='background:{CATS[cat]['stroke']}'></span>"
            f"<b>{esc(nid)}</b></td><td>{esc(title)}</td>"
            f"<td>{esc(CATS[cat]['label'])}</td><td class='mono'>{esc(files)}</td></tr>"
        )
    return "\n".join(rows)


# ----------------------------------------------------------------------------
# Assemble HTML
# ----------------------------------------------------------------------------
def build_html() -> str:
    svg = build_svg()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Technopolis — Project Workflow Flowchart</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#020617; color:#e2e8f0;
         font-family:'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace; }}
  header {{ padding:22px 28px 10px; border-bottom:1px solid #1e293b; }}
  h1 {{ margin:0; font-size:20px; letter-spacing:.5px; display:flex; align-items:center; gap:12px; }}
  .dot {{ width:10px; height:10px; border-radius:50%; background:#34d399;
          box-shadow:0 0 0 0 rgba(52,211,153,.6); animation:pulse 2s infinite; }}
  @keyframes pulse {{ 0%{{box-shadow:0 0 0 0 rgba(52,211,153,.55);}}
                     70%{{box-shadow:0 0 0 10px rgba(52,211,153,0);}}
                     100%{{box-shadow:0 0 0 0 rgba(52,211,153,0);}} }}
  .sub {{ color:#94a3b8; font-size:12px; margin-top:6px; }}
  .controls {{ position:sticky; top:0; z-index:5; background:#020617cc; backdrop-filter:blur(4px);
              padding:8px 28px; border-bottom:1px solid #1e293b; display:flex; gap:8px; }}
  .controls button {{ background:#0f172a; color:#cbd5e1; border:1px solid #334155; border-radius:6px;
                      padding:5px 12px; font-family:inherit; font-size:11px; cursor:pointer; }}
  .controls button:hover {{ border-color:#22d3ee; color:#22d3ee; }}
  .card {{ margin:18px 28px; background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:18px; }}
  .card h2 {{ margin:0 0 10px; font-size:14px; color:#cbd5e1; letter-spacing:1px; }}
  table {{ border-collapse:collapse; width:100%; font-size:11px; }}
  th,td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #1e293b; vertical-align:top; }}
  th {{ color:#64748b; font-size:10px; letter-spacing:1px; text-transform:uppercase; }}
  .sw {{ display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:6px; }}
  .mono {{ color:#94a3b8; }}
  footer {{ padding:14px 28px 30px; color:#64748b; font-size:10.5px; }}
  .cols {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }}
  .cols .card {{ margin:0; }}
  ul {{ margin:6px 0 0; padding-left:18px; color:#94a3b8; font-size:11px; line-height:1.7; }}
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span>Technopolis · Vernika Bridge — Complete Project Workflow</h1>
  <div class="sub">Node-type flowchart of the AI voice-calling pipeline · every node derived from the actual codebase ·
  <b style="color:#e2e8f0">{len(NODES)} nodes</b> · <b style="color:#e2e8f0">{len(EDGES)} connections</b> · {len(ZONES)} zones</div>
</header>
<div class="controls">
  <button onclick="zoomAt(1.5)">+ Zoom</button>
  <button onclick="zoomAt(1/1.5)">− Zoom</button>
  <button onclick="fit()">Fit</button>
  <span style="color:#475569;font-size:11px;align-self:center">wheel = zoom · drag = pan</span>
</div>
<div class="card">
{svg}
</div>
<div class="cols">
  <div class="card"><h2>▶ How a call flows</h2><ul>
    <li>Operator uploads leads → <span class="mono">leads</span> → FRESH_CALL jobs → dispatcher claims → number allocator picks P1–P9 line → dial.</li>
    <li>Vobiz answers → <span class="mono">/vobiz/answer</span> returns wss XML → <span class="mono">/ws/vobiz</span> → live session bridges audio to Gemini Live.</li>
    <li>Hangup webhook → transcript → analyzer → disposition → 4-sandbox lifecycle schedules retries / WhatsApp / callbacks / feedback.</li>
  </ul></div>
  <div class="card"><h2>▶ Pipeline guardrails</h2><ul>
    <li>DNC / opt-out register blocks callbacks &amp; jobs (TRAI).</li>
    <li>Slot guards, semaphores, inter-call gap (120–180s), hourly caps, line cooldown.</li>
    <li>Strict lead-stage transition machine (<span class="mono">workflow_models.py</span>).</li>
  </ul></div>
  <div class="card"><h2>▶ Ops &amp; delivery</h2><ul>
    <li>Docker stack: technopoliss · openwa · postgres · caddy on VPS.</li>
    <li>Self-healing: 10 health agents → Super Boss → Panther; Hermes watchdog cron.</li>
    <li>Deploy: tar+scp → <span class="mono">docker compose build &amp;&amp; up -d</span> (frontend baked into image).</li>
  </ul></div>
</div>
<div class="card">
  <h2>Node index (id → role → source files)</h2>
  <table>
    <thead><tr><th>#</th><th>Node</th><th>Component</th><th>Layer</th><th>Key files (as read)</th></tr></thead>
    <tbody>
{node_table()}
    </tbody>
  </table>
</div>
<footer>Generated from a full codebase read · branch <span class="mono">deploy/technopolis-v1</span> ·
backend/main.py → api/app.py registers all routers · regen with <span class="mono">python docs/workflow/generate_workflow.py</span></footer>
<script>
(function(){{
  var svg=document.getElementById('map');
  var W={CANVAS_W},H={CANVAS_H};
  var k=1,tx=0,ty=0;
  function apply(){{ svg.setAttribute('viewBox', tx+' '+ty+' '+(W/k)+' '+(H/k)); }}
  function zoomAt(f,cx,cy){{
    if(!cx||!cy){{
      var r=svg.getBoundingClientRect();
      cx=r.width/2; cy=r.height/2;
    }}
    var vx=tx+cx*(W/k)/ (svg.clientWidth||W);
    var vy=ty+cy*(H/k)/ (svg.clientHeight||H);
    var nk=Math.min(6,Math.max(0.25,k*f));
    tx=vx-(vx-tx)*(nk/k); ty=vy-(vy-ty)*(nk/k); k=nk; apply();
  }}
  function fit(){{ k=1;tx=0;ty=0;apply(); }}
  svg.addEventListener('wheel',function(e){{
    e.preventDefault();
    var r=svg.getBoundingClientRect();
    zoomAt(e.deltaY<0?1.2:1/1.2, e.clientX-r.left, e.clientY-r.top);
  }},{{passive:false}});
  var drag=false,sx=0,sy=0,stx=0,sty=0;
  svg.addEventListener('pointerdown',function(e){{drag=true;sx=e.clientX;sy=e.clientY;stx=tx;sty=ty;svg.style.cursor='grabbing';}});
  window.addEventListener('pointermove',function(e){{if(!drag)return;tx=stx-(e.clientX-sx)*(W/k)/ (svg.clientWidth||W);ty=sty-(e.clientY-sy)*(H/k)/ (svg.clientHeight||H);apply();}});
  window.addEventListener('pointerup',function(){{drag=false;svg.style.cursor='grab';}});
  window.zoomAt=zoomAt; window.fit=fit;
}})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------
def validate() -> list[str]:
    errs = []
    for e in EDGES:
        for nid in (e[0], e[1]):
            if nid not in NODE_MAP:
                errs.append(f"edge references unknown node: {nid}")
    for n in NODES:
        nid, x, y, w, h, *_ = n
        if x < 0 or y < 0 or x + w > CANVAS_W + 10 or y + h > CANVAS_H + 10:
            errs.append(f"node {nid} out of canvas: {x},{y},{w},{h}")
    # pairwise overlap (same zone rows should not collide)
    for i in range(len(NODES)):
        for j in range(i + 1, len(NODES)):
            a, b = NODES[i], NODES[j]
            ax, ay, aw, ah = a[1], a[2], a[3], a[4]
            bx, by, bw, bh = b[1], b[2], b[3], b[4]
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                errs.append(f"overlap: {a[0]} <-> {b[0]}")
    return errs


def main() -> None:
    errs = validate()
    if errs:
        raise SystemExit("LAYOUT ERRORS:\n" + "\n".join(errs))
    out_dir = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(out_dir, "PROJECT_WORKFLOW.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(build_html())
    print(f"OK: {out_path} ({os.path.getsize(out_path)} bytes, "
          f"{len(NODES)} nodes, {len(EDGES)} edges, {len(ZONES)} zones)")


if __name__ == "__main__":
    main()
