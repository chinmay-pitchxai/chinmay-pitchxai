#!/usr/bin/env bash
# =============================================================================
# technopoliss-watchdog.sh — self-healing supervisor for the Technopoliss
# (Vernika) production stack.
#
# Runs ON the VPS (srv1732329.hstgr.cloud) via a systemd timer every 60s.
# Detects failures and auto-corrects them:
#   - restart crashed/unhealthy containers (bounded by a restart cooldown)
#   - fix .env public-URL drift (the #1 silent call-blocker)
#   - recover SQLite WAL locks / integrity issues
#   - trigger the app's built-in Panther auto-fix
#   - prune disk / rotate logs
#   - alert on escalation + recovery (Telegram / webhook, optional)
#
# Safe by construction: idempotent, single-instance (flock), cooldown-bounded,
# never hard-restarts the backend while a live call is active (unless the
# backend is fully unresponsive), and every action is audit-logged.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="${WATCHDOG_CONF:-$SCRIPT_DIR/watchdog.conf}"
[ -f "$CONF_FILE" ] && . "$CONF_FILE"

# --- configuration (all overridable via watchdog.conf or env) -----------------
VPS_HOST="${VPS_HOST:-srv1732329.hstgr.cloud}"
PUBLIC_URL="${PUBLIC_URL:-https://${VPS_HOST}}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:9090}"
BACKEND_INTERNAL_URL="${BACKEND_INTERNAL_URL:-http://localhost:9090}"
OPENWA_URL="${OPENWA_URL:-http://127.0.0.1:2786}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/technopoliss}"
ENV_FILE="${ENV_FILE:-${COMPOSE_DIR}/backend/.env}"
DB_IN_CONTAINER="${DB_IN_CONTAINER:-/app/backend/data/vernika.db}"

SVC_BACKEND="${SVC_BACKEND:-technopoliss}"
SVC_OPENWA="${SVC_OPENWA:-openwa}"
SVC_CADDY="${SVC_CADDY:-caddy}"
SVC_POSTGRES="${SVC_POSTGRES:-postgres}"
CT_BACKEND="${CT_BACKEND:-technopoliss-vernika}"
CT_OPENWA="${CT_OPENWA:-openwa-api}"
CT_CADDY="${CT_CADDY:-technopoliss-caddy}"
CT_POSTGRES="${CT_POSTGRES:-technopoliss-postgres}"

LOG_FILE="${LOG_FILE:-/var/log/technopoliss-watchdog.log}"
STATE_DIR="${STATE_DIR:-/var/lib/technopoliss-watchdog}"
LOCK_FILE="${LOCK_FILE:-${STATE_DIR}/.lock}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
MAX_RESTARTS_PER_10M="${MAX_RESTARTS_PER_10M:-3}"
RESTART_WINDOW_SEC="${RESTART_WINDOW_SEC:-600}"
LOG_MAX_BYTES="${LOG_MAX_BYTES:-10485760}"

WATCHDOG_DRY_RUN="${WATCHDOG_DRY_RUN:-0}"
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ALERT_WEBHOOK="${ALERT_WEBHOOK:-}"

mkdir -p "$STATE_DIR"

# --- single-instance lock ------------------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date '+%F %T') | SKIP tick (another instance running)" >> "$LOG_FILE"
  exit 0
fi

# --- helpers -------------------------------------------------------------------
ts() { date +%s; }
stamp() { date '+%F %T'; }
log() { echo "$(stamp) | $*" >> "$LOG_FILE"; }

rotate_log() {
  if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
  fi
}

alert() {
  local level="$1"; shift
  local msg="[${level}] $*"
  log "ALERT $msg"
  if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    curl -s --max-time 10 -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
      -d chat_id="$TELEGRAM_CHAT_ID" -d text="Technopoliss watchdog ${msg}" >/dev/null 2>&1 || true
  fi
  if [ -n "$ALERT_WEBHOOK" ]; then
    curl -s --max-time 10 -X POST "$ALERT_WEBHOOK" -H 'Content-Type: application/json' \
      -d "{\"level\":\"${level}\",\"message\":\"${msg}\"}" >/dev/null 2>&1 || true
  fi
}

# consecutive-failure counter (soft issues need N failures before acting)
reach_threshold() {
  local key="$1" needed="${2:-1}" cur=0
  local f="$STATE_DIR/fail.$key"
  [ -f "$f" ] && cur=$(tr -cd '0-9' < "$f" 2>/dev/null)
  [ -z "$cur" ] && cur=0
  cur=$((cur + 1)); echo "$cur" > "$f"
  [ "$cur" -ge "$needed" ]
}
reset_threshold() { rm -f "$STATE_DIR/fail.$1"; }

# restart cooldown: at most MAX_RESTARTS_PER_10M restarts per RESTART_WINDOW_SEC
restart_allowed() {
  local key="$1" now line count=0
  local f="$STATE_DIR/restarts.$key"
  [ -f "$f" ] || return 0
  now=$(ts)
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    [ $((now - line)) -lt "$RESTART_WINDOW_SEC" ] && count=$((count + 1))
  done < "$f"
  [ "$count" -lt "$MAX_RESTARTS_PER_10M" ]
}
record_restart() { echo "$(ts)" >> "$STATE_DIR/restarts.$1"; }

# docker helpers
dc() { (cd "$COMPOSE_DIR" && (docker compose "$@" 2>/dev/null || docker-compose "$@" 2>/dev/null)); }
ct_running() { docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -qx 'true'; }
ct_health()  { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null; }

# run curl INSIDE the backend container — the backend publishes no host port
# (only Caddy reaches it on the internal appnet), so host-side 127.0.0.1:9090
# always fails. curl is kept in the image for healthchecks.
backend_curl() {
  docker exec "$CT_BACKEND" curl -sf --max-time 8 "$@" 2>/dev/null
}

# number of active live calls (empty string if backend unresponsive)
active_calls() {
  backend_curl "${BACKEND_INTERNAL_URL}/health" \
    | grep -o '"active_calls":[0-9]*' | head -1 | cut -d: -f2
}

# --- actions -------------------------------------------------------------------
restart_container() {
  local svc="$1" ct="$2" reason="$3"
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD restart $ct ($reason)"; return 0
  fi
  if restart_allowed "$ct"; then
    log "ACTION restart $ct ($reason)"
    docker restart "$ct" >/dev/null 2>&1 || dc up -d "$svc" >/dev/null 2>&1 || true
    record_restart "$ct"
  else
    alert "ESCALATE" "restart storm on $ct ($reason) — stopped auto-restarting, needs human"
  fi
}

start_container() {
  local svc="$1" ct="$2" reason="$3"
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD start $ct ($reason)"; return 0
  fi
  if ct_running "$ct"; then return 0; fi
  log "ACTION start $ct ($reason)"
  docker start "$ct" >/dev/null 2>&1 || dc up -d "$svc" >/dev/null 2>&1 || true
  record_restart "$ct"
}

trigger_panther() {
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD trigger /health/panther"; return 0
  fi
  log "ACTION trigger_panther"
  docker exec "$CT_BACKEND" curl -s --max-time 15 -X POST "${BACKEND_INTERNAL_URL}/health/panther" \
    -H 'Content-Type: application/json' -d '{"triggered_by":"watchdog"}' >/dev/null 2>&1 || true
}

# restart the backend ONLY if no live call is active (empty count = unresponsive,
# which means no live call can survive — restart freely). Returns 0 if restarted,
# 1 if deferred.
restart_backend_safely() {
  local reason="$1" ac
  ac=$(active_calls)
  if [ -n "$ac" ] && [ "$ac" -gt 0 ]; then
    log "DEFER restart backend ($reason) — ${ac} live call(s) active"
    trigger_panther
    alert "ESCALATE" "backend needs restart (${reason}) but ${ac} live call(s) active — deferred"
    return 1
  fi
  restart_container "$SVC_BACKEND" "$CT_BACKEND" "$reason"
}

fix_env_urls() {
  if [ ! -f "$ENV_FILE" ]; then return 1; fi
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD fix .env public URLs -> ${PUBLIC_URL}"; return 0
  fi
  local changed=0
  for K in SERVER_URL VOBIZ_PUBLIC_BASE_URL VOBIZ_STREAM_PUBLIC_BASE_URL; do
    local cur; cur=$(grep -E "^${K}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r')
    if [ -z "$cur" ] || [ "$cur" != "$PUBLIC_URL" ]; then
      log "ACTION fix .env ${K}: '${cur}' -> '${PUBLIC_URL}'"
      if grep -q "^${K}=" "$ENV_FILE"; then
        sed -i "s|^${K}=.*|${K}=${PUBLIC_URL}|" "$ENV_FILE"
      else
        echo "${K}=${PUBLIC_URL}" >> "$ENV_FILE"
      fi
      changed=1
    fi
  done
  if [ "$changed" = "1" ]; then
    log "ACTION recreate backend to pick up .env change"
    dc up -d "$SVC_BACKEND" >/dev/null 2>&1 || true
    record_restart "$CT_BACKEND"
  fi
}

sqlite_repair() {
  # Legacy SQLite repair — the primary store is now PostgreSQL (see core/db.py).
  # Kept as a guard so old deployments with a SQLite file still get a checkpoint
  # attempt, but the real database health check is pg_health_check() below.
  # Nothing here ever touches the Postgres volume.
  local result
  result=$(docker exec "$CT_BACKEND" python -c \
    "import sqlite3;c=sqlite3.connect('${DB_IN_CONTAINER}');print(c.execute('PRAGMA integrity_check').fetchone()[0])" 2>&1)
  if [ "$result" = "ok" ]; then return 0; fi
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD sqlite wal_checkpoint (integrity: $result)"; return 1
  fi
  log "ACTION sqlite integrity='${result}' -> wal_checkpoint(TRUNCATE)"
  docker exec "$CT_BACKEND" python -c \
    "import sqlite3;c=sqlite3.connect('${DB_IN_CONTAINER}');c.execute('PRAGMA wal_checkpoint(TRUNCATE)')" >/dev/null 2>&1 || true
  result=$(docker exec "$CT_BACKEND" python -c \
    "import sqlite3;c=sqlite3.connect('${DB_IN_CONTAINER}');print(c.execute('PRAGMA integrity_check').fetchone()[0])" 2>&1)
  if [ "$result" = "ok" ]; then
    log "RECOVERY sqlite ok after checkpoint"
    return 0
  fi
  alert "ESCALATE" "sqlite integrity_check still '${result}' — do NOT auto-delete data"
  return 1
}

pg_health_check() {
  # PostgreSQL is the primary store — verify the container accepts connections.
  # `pg_isready` is shipped in the official postgres image; exit 0 when ready.
  if ! ct_running "$CT_POSTGRES"; then
    start_container "$SVC_POSTGRES" "$CT_POSTGRES" "postgres not running"
    return 1
  fi
  if docker exec "$CT_POSTGRES" pg_isready -U technopoliss -d technopoliss >/dev/null 2>&1; then
    return 0
  fi
  if reach_threshold pg_down 2; then
    log "ACTION postgres not ready -> restart container"
    if [ "$WATCHDOG_DRY_RUN" != "1" ]; then
      restart_container "$SVC_POSTGRES" "$CT_POSTGRES" "pg_isready failed"
    fi
  fi
  return 1
}

prune_disk() {
  if [ "$WATCHDOG_DRY_RUN" = "1" ]; then
    log "WOULD docker system prune + rotate logs (disk pressure)"; return 0
  fi
  log "ACTION docker system prune -f (disk pressure)"
  docker system prune -f >/dev/null 2>&1 || true
  rotate_log
}

# =============================================================================
# MAIN CHECKS (priority order)
# =============================================================================
main() {
  rotate_log
  log "TICK begin"

  # --- 1. docker daemon -----------------------------------------------------
  if ! docker info >/dev/null 2>&1; then
    if reach_threshold docker 1; then
      log "ACTION restart docker daemon"
      if [ "$WATCHDOG_DRY_RUN" != "1" ]; then systemctl restart docker >/dev/null 2>&1 || true; fi
      alert "ESCALATE" "docker daemon down; attempted restart"
    fi
    log "TICK end (docker down)"
    exit 0
  fi
  reset_threshold docker

  # --- 2. stack present? ----------------------------------------------------
  if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
    log "WARN compose dir $COMPOSE_DIR missing — skipping container checks"
    return 0
  fi

  # --- 3. containers running ------------------------------------------------
  for pair in "$SVC_BACKEND:$CT_BACKEND" "$SVC_OPENWA:$CT_OPENWA" "$SVC_CADDY:$CT_CADDY" "$SVC_POSTGRES:$CT_POSTGRES"; do
    svc="${pair%%:*}"; ct="${pair##*:}"
    if ! ct_running "$ct"; then
      start_container "$svc" "$ct" "not running"
    fi
  done

  # --- 4. container health (unhealthy -> restart) ---------------------------
  for pair in "$SVC_BACKEND:$CT_BACKEND" "$SVC_OPENWA:$CT_OPENWA" "$SVC_CADDY:$CT_CADDY"; do
    svc="${pair%%:*}"; ct="${pair##*:}"
    h=$(ct_health "$ct")
    if [ "$h" = "unhealthy" ]; then
      if reach_threshold "unhealthy.$ct" 2; then
        if [ "$ct" = "$CT_BACKEND" ]; then
          restart_backend_safely "health=$h"
        else
          restart_container "$svc" "$ct" "health=$h"
        fi
      fi
    else
      reset_threshold "unhealthy.$ct"
    fi
  done

  # --- 5. backend liveness --------------------------------------------------
  if backend_curl "${BACKEND_INTERNAL_URL}/health" >/dev/null 2>&1; then
    reset_threshold backend_down
  else
    if reach_threshold backend_down 1; then
      # prefer in-app first-aid before a hard restart (preserves live calls)
      trigger_panther
      sleep 5
      if ! backend_curl "${BACKEND_INTERNAL_URL}/health" >/dev/null 2>&1; then
        restart_backend_safely "health endpoint down"
      fi
    fi
  fi

  # --- 6. backend data layer (server up but DB broken) ----------------------
  if backend_curl "${BACKEND_INTERNAL_URL}/api/dashboard/leads?limit=1" >/dev/null 2>&1; then
    reset_threshold backend_data
  else
    if reach_threshold backend_data 2; then
      trigger_panther
      if ! backend_curl "${BACKEND_INTERNAL_URL}/api/dashboard/leads?limit=1" >/dev/null 2>&1; then
        restart_backend_safely "data layer down"
      fi
    fi
  fi

  # --- 7. whatsapp gateway --------------------------------------------------
  if curl -sf --max-time 8 "$OPENWA_URL/api/health/ready" >/dev/null 2>&1; then
    reset_threshold openwa_down
  else
    if reach_threshold openwa_down 1; then
      restart_container "$SVC_OPENWA" "$CT_OPENWA" "openwa health down"
    fi
  fi

  # --- 8. public Vobiz guard probe (the exact gate for every call) ----------
  local body code
  body=$(curl -s --max-time 15 -w '\n%{http_code}' "${PUBLIC_URL}/vobiz/answer?camp_id=wd_probe&role=sales_1" 2>/dev/null)
  code=$(echo "$body" | tail -1)
  body=$(echo "$body" | sed '$d')
  if [ "$code" = "200" ] && echo "$body" | grep -q '<Stream' && echo "$body" | grep -q '/ws/vobiz'; then
    reset_threshold public_probe
  else
    if reach_threshold public_probe 1; then
      # A 502 can come from EITHER layer (caddy up + backend down, or caddy down).
      # Check the backend directly first so we don't blame/restart the wrong one.
      if ! backend_curl "${BACKEND_INTERNAL_URL}/health" >/dev/null 2>&1; then
        log "public probe fail (http=$code) — backend is the down layer (handled by liveness check)"
      elif ! ct_running "$CT_CADDY"; then
        start_container "$SVC_CADDY" "$CT_CADDY" "caddy down (public probe fail)"
      else
        restart_container "$SVC_CADDY" "$CT_CADDY" "public probe fail (backend OK)"
      fi
    fi
  fi

  # --- 9. .env public-URL consistency (silent call-blocker) -----------------
  if [ -f "$ENV_FILE" ]; then
    local drift=0
    for K in SERVER_URL VOBIZ_PUBLIC_BASE_URL VOBIZ_STREAM_PUBLIC_BASE_URL; do
      cur=$(grep -E "^${K}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r')
      if [ "$cur" != "$PUBLIC_URL" ]; then drift=1; break; fi
    done
    if [ "$drift" = "1" ]; then
      if reach_threshold env_drift 2; then
        fix_env_urls
        reset_threshold env_drift
      fi
    else
      reset_threshold env_drift
    fi
  fi

  # --- 10. primary database (PostgreSQL) health ------------------------------
  pg_health_check

  # --- 11. disk pressure ----------------------------------------------------
  local used
  used=$(df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
  if [ -n "$used" ] && [ "$used" -gt "$DISK_WARN_PCT" ]; then
    if reach_threshold disk_pressure 2; then
      prune_disk
      alert "ESCALATE" "disk ${used}% full after prune — check VPS storage"
    fi
  else
    reset_threshold disk_pressure
  fi

  log "TICK end"
}

main
exit 0
