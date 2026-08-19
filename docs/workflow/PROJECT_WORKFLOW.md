# Technopolis · Vernika Bridge — Complete Project Workflow

> Node-type systematic map of the AI voice-calling pipeline. Every item below was
> verified by reading the actual codebase (branch `deploy/technopolis-v1`).
> Interactive flowchart: **`PROJECT_WORKFLOW.html`** (open in a browser — wheel zoom, drag pan).
> Static preview: **`PROJECT_WORKFLOW_preview.png`**. Regenerate: `python generate_workflow.py`.

---

## 0. System identity

| | |
|---|---|
| **App** | Vernika Bridge — FastAPI `v2.0.0-bridge`, port 9090 (`backend/main.py` → `backend/api/app.py`) |
| **DB** | PostgreSQL 16 (prod) / SQLite (dev) — schema in `backend/core/storage.py` |
| **Telephony** | Vobiz (api.vobiz.ai) — outbound dials, inbound answer URL, media WebSocket |
| **AI** | Gemini Live (`models/gemini-3.1-flash-live-preview`) — real-time voice conversation |
| **WhatsApp** | OpenWA gateway container (port 2786) — linked-device WhatsApp API |
| **UI** | `index.html` + `app.js` + `kpi_modal.js` + `index.css` + ApexCharts (served from project root, baked into Docker image) |
| **Host** | VPS `srv1732329.hstgr.cloud` — Docker Compose stack: technopoliss · openwa · postgres · caddy |
| **Frontend deploy note** | Dashboard files are **baked into the image** → any `index.html`/`app.js` change needs `docker compose build technopoliss` + `up -d` |

---

## 1. Zones & node map (46 nodes, 55 connections)

### ZONE 1 — ENTRY (operator, customers & feeds)

| Node | What it is | Source (as read) |
|---|---|---|
| `fe_dashboard` | Operator console: dashboard / campaigns / make-a-call / vobiz / config views | `index.html`, `app.js`, `kpi_modal.js` |
| `ext_wa_customer` | WhatsApp end-user messaging the linked device | — |
| `ext_pstn` | Customer dialing a sales line (P1–P9 DIDs) | — |
| `feed_digital` | Autonomous lead feeds: Excel file watcher + Google Sheets watcher | `services/digital_excel_ingest.py`, `services/google_sheets_ingest.py` |
| `ext_openwa` | OpenWA gateway container (WhatsApp API) | `docker-compose.yml` (openwa) |

### ZONE 2 — API & WEBHOOK LAYER (FastAPI routers)

| Node | Endpoints | Source |
|---|---|---|
| `api_campaign` | `/api/campaign/*` — upload, start, stop, reset, wipe, config, contacts, sources, kpi-summary | `backend/api/routes/campaign.py` |
| `api_manual` | `/api/manual/call`, `/api/callbacks`, `/api/schedules`, `/api/cases`, `/api/tuning` | `routes/console_api.py`, `routes/callbacks.py`, `routes/schedules.py`, `routes/cases.py` |
| `api_whatsapp` | `/api/whatsapp/webhook`, `/api/openwa/webhook`, proxy, send-details | `routes/whatsapp.py`, `routes/whatsapp_proxy.py` |
| `api_dashboard` | `/api/dashboard/leads|overview`, `/api/sandbox/*`, `/api/orchestration/*`, `/api/tuning` | `routes/dashboard.py`, `sandbox_overview.py`, `orchestration.py`, `console_api.py` |
| `api_auth` | `/api/login`, `/api/me` — JWT + console-role guard on `/api/*` | `routes/auth_api.py`, `core/auth.py` |
| `api_vobiz_answer` | GET/POST `/vobiz/answer` → returns `<Stream> wss://…/ws/vobiz` XML | `routes/vobiz.py` |
| `api_vobiz_incoming` | GET/POST `/vobiz/incoming` → dialed DID → role mapping → busy gate | `routes/vobiz.py` |
| `api_hangup` | `/vobiz/hangup`, recording-webhook → CallUUID→camp_id mapping | `routes/vobiz.py` |
| `api_ws_vobiz` | WS `/ws/vobiz` — media socket (camp_id, agent_id, manual_role) | `routes/vobiz.py` |

### ZONE 3 — ORCHESTRATION CORE (workflow queue & autonomous dialing)

| Node | What it is | Source |
|---|---|---|
| `orch_queue` | `workflow_jobs` table: scheduled → ready → claimed → running → done | `core/workflow_queue.py`, `core/workflow_models.py` |
| `orch_dispatcher` | `dispatch_once()`: promote_due, claim_next (lease), busy lock, per-line cooldown | `core/orchestration_dispatcher.py` |
| `orch_alloc` | Number pools: P1/P2 cold · P3 digital · P4–P6 retry · P7/P8 nurture · P9 feedback | `core/number_allocator.py` |
| `orch_exec_phone` | `execute_phone_job()` → `_process_single_lead()` (dial + session + finalize) | `core/live_job_executor.py` |
| `orch_exec_wa` | `execute_whatsapp_job()` — package / 24h followup / no-reply call | `core/live_job_executor.py` |
| `orch_service` | Lifecycle: schedule_job, failed_call, interested, opt_out, site visits, memory | `core/orchestration_service.py` |
| `legacy_worker` | Campaign worker: `_scheduler_loop`, `_campaign_worker_role`, inter-call gap 120–180s | `core/worker.py` |

### ZONE 4 — CALL PIPELINE (Vobiz ↔ Gemini Live bridge)

| Node | What it is | Source |
|---|---|---|
| `dial_make` | `make_vobiz_call()` → REST POST `/Account/{auth}/Call/` | `services/vobiz_bridge/vobiz_client.py` |
| `dial_slots` | Slot/capacity guards: acquire/release Vobiz slot, global semaphore, phone round-robin, hourly caps | `core/state.py`, `core/worker.py` |
| `live_session` | `handle_vobiz_ws_live()` — the live state machine: greeting → name-verify → pitch → voicemail → dev-mode | `services/vobiz_bridge/live_session.py` (6097 lines) |
| `live_gemini` | Gemini Live WS: setup, RAG injection, turn nudges, PCM silence kick | `services/vobiz_bridge/gemini_protocol.py`, `core/gemini_auth.py` |
| `ext_vobiz` | Vobiz Telephony API (PSTN carrier, recordings) | creds in `backend/.env` |
| `live_audio` | Audio engine: 16k↔24k resample, VAD, noise suppression, greeting PCM, background mix | `services/vobiz_bridge/audio.py` |
| `live_turns` | Turn-taking addons: anti-loop, site-visit confirmation, voicemail screening/classify | `services/vobiz_bridge/turn_taking_addon.py`, `voicemail.py` |
| `live_transcript` | `conversation_log` JSONL — append_turn per utterance, session meta, artifacts | `services/conversation_log.py` |
| `live_record` | CallRecorder (PCM/wav) + Vobiz recording ingest | `services/call_recording.py`, `services/vobiz_bridge/vobiz_recording.py` |

### ZONE 5 — POST-CALL ANALYSIS

| Node | What it is | Source |
|---|---|---|
| `an_transcriber` | STT: audio → text via Gemini, live JSONL passthrough | `services/transcriber.py` |
| `an_analyzer` | Gemini analysis → heuristic fallback → local analyzer; canonical disposition | `services/call_analyzer.py`, `services/gemini_analyzer.py` |
| `an_lead_update` | Lead status/disposition, `call_attempts` row, `lead_memory` facts, dashboard-state invalidation, event publish | `core/worker.py` (`_analyze_and_update_lead`), `core/lead_memory.py` |

### ZONE 6 — LEAD LIFECYCLE (4-sandbox routing)

| Node | What it is | Source |
|---|---|---|
| `lc_interested` | Interested → **Sandbox 3**: WhatsApp package + 24-working-hour followup + no-reply call | `core/orchestration_service.py` (`interested`) |
| `lc_failed` | Failed/no-answer → **Sandbox 2**: retry after 12/24 working hours, attempt 3 → lost (P4–P6) | `core/orchestration_service.py` (`failed_call`) |
| `lc_callback` | Callback requested → **Sandbox 1** lines (P1–P3), bounded relationship retry | `core/orchestration_service.py` (`schedule_callback`) |
| `lc_sitevisit` | Site visit → day-before + morning reminders → completed → **Sandbox 4** feedback (P9) | `core/orchestration_service.py` (`schedule_site_visit`) |
| `lc_whatsapp` | WhatsApp senders: brochure package, templates, disposition messages | `services/whatsapp/*`, `services/whatsapp_leads.py` |
| `lc_optout` | DNC / opt-out register — blocks callbacks, cancels lead jobs (TRAI) | `core/dnc.py`, `core/orchestration_service.py` (`opt_out`) |

### ZONE 7 — STORAGE

| Node | What it is | Source |
|---|---|---|
| `db_pg` | PostgreSQL 16 / SQLite: leads, workflow_jobs, call_attempts, lead_memory, site_visits, feedback_records, do_not_contact, whatsapp_messages, camp_sessions, vobiz_call_map, schedules, cases, virtual_meets | `core/storage.py`, `core/db.py` |
| `db_media` | Recordings, transcripts, greeting PCM, JSONL logs | `backend/media/`, `data/` |

### ZONE 8 — OPERATIONS & MONITORING

| Node | What it is | Source |
|---|---|---|
| `ops_events` | Event bus + `/ws/dashboard` + `/api/events/stream` live push | `core/events.py`, `routes/events.py` |
| `ops_health` | 10 self-healing agents (call quality, callback, campaign, concurrency, config, integration, media, RAG, scheduling, smooth calls) | `services/health_agents/` |
| `ops_watchdog` | Hermes watchdog — 5-min cron, VPS sensor script + Windows SSH runner, safe fixes, TTS alert | `watchdog/` |
| `ops_deploy` | tar+scp → `docker compose build && up -d`; Caddy TLS/WSS | `deploy_vps.sh`, `docker-compose.yml`, `Caddyfile`, `Dockerfile` |

---

## 2. The 5 primary flows

### Flow A — Outbound campaign call (autonomous orchestration)
1. Leads land in `leads` (upload, paste, digital Excel watcher, Google Sheets watcher, agent factory).
2. `orch_service.schedule_job()` → `workflow_jobs` **FRESH_CALL** (priority 6, pool P1/P2 cold or P3 digital).
3. Orchestration supervisor loop → `promote_due()` → `dispatch_once()` claims job, `allocate_number()` picks the line, capacity lock + cooldown checked.
4. `execute_phone_job()` → `_process_single_lead()`: lead → `dialing`, opening PCM primed, slots acquired → `make_vobiz_call()` with `answer_url=/vobiz/answer?camp_id=…`.
5. Vobiz calls the number; on answer it hits `/vobiz/answer` → gets `<Stream> wss://…/ws/vobiz` XML → opens the media socket.
6. `handle_vobiz_ws_live()` runs the conversation: greeting PCM → name verify → pitch → RAG context → turn-taking, voicemail detection, dev-mode whitelist; every utterance appended to JSONL.
7. Hangup webhook → finalize: transcript resolved → analyzer (Gemini → heuristic) → disposition → lead/memory/attempts updated → lifecycle transition scheduled.
8. Live push to dashboard via event bus → `/ws/dashboard`.

### Flow B — Inbound call (customer dials a sales line)
1. Vobiz Application answer URL hits `/vobiz/incoming` (From/To/CallUUID).
2. `build_phone_to_role_map()` resolves the dialed DID → role; busy gate (line busy / active call / running campaign) → polite busy XML + `incoming_calls` row (`missed_busy`).
3. Known lead lookup → unique `incoming_*` camp_id → wss URL with lead_name → media session (same engine as outbound).
4. Hangup → incoming-call finalizer → same analysis pipeline.

### Flow C — Manual call / callback / schedule
- `/api/manual/call` → `make_vobiz_call` with `manual_*` camp_id → full session → `manual_calls`.
- `/api/callbacks` → due callbacks picked up by the campaign worker at due time (queued while busy).
- `/api/schedules` → `_scheduler_loop` polls every 30s, fires the campaign worker at `run_at`.

### Flow D — WhatsApp (24/7 chatbot)
1. OpenWA webhook → `/api/openwa/webhook` / `/api/whatsapp/webhook` → conversation workflow → reply via `send_text` / brochure package.
2. Outbound: `whatsapp_package` + `whatsapp_followup_24h` jobs → `execute_whatsapp_job()` → OpenWA send; no reply after 2–3 working hours → `INTERESTED_FOLLOWUP` call.

### Flow E — Site visits & feedback
- `schedule_site_visit()` → reminders (day-before, morning 9:00) → `complete_site_visit()` → SB4 → `post_visit_feedback` (P9) next day; `record_feedback()` → booked/follow_up/not_interested/lost.

---

## 3. Guardrails built into the pipeline

- **DNC / opt-out** — `do_not_contact` register blocks callback scheduling (409) and cancels all lead jobs.
- **Concurrency** — global semaphore, per-role dialer slots, Vobiz per-account cap (VOBIZ_MAX_CONCURRENT_PER_ACCOUNT=2, provider=3), per-line busy map + lock.
- **Pacing** — inter-call gap 120–180s, hourly caps (30/phone, 60/role), phone round-robin, line cooldown after each leg.
- **Stage machine** — `LeadStage` transitions enforced via `require_transition()`; terminal: booked / not_interested / lost / opted_out.
- **Idempotency** — `idempotency_key` on workflow_jobs; duplicate lead detection on upload.
- **State recovery** — stale `dialing` leads recovered on worker start; orphaned rows cleaned on lifespan startup.

## 4. Deploy & ops (as configured)

1. Code is **not a git repo on the VPS** — sync via `tar + scp` (excludes `.env`, `backend/data`, `backend/openwa`, `.venv`, `node_modules`).
2. `deploy_vps.sh` on the VPS: pins VOBIZ_PUBLIC_BASE_URL / VOBIZ_STREAM_PUBLIC_BASE_URL / SERVER_URL → `docker compose down` → `build` → `up -d` → probes `/vobiz/answer` internally + over public HTTPS.
3. Caddy terminates TLS/WSS, reverse-proxies `technopoliss:9090`.
4. Self-healing: health agents perform safe checks and guarded fixes; Hermes watchdog cron (every 5 min) SSH-checks VPS, restarts containers only when `active_calls=0`, prunes Docker on disk >85%, and announces via TTS.
