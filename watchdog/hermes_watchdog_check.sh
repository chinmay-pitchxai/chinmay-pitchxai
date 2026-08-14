#!/usr/bin/env bash
# =============================================================================
# hermes_watchdog_check.sh — read-only health snapshot for the Hermes watchdog.
#
# Runs ON the VPS (srv1732329.hstgr.cloud) and prints a DETERMINISTIC status
# report to stdout. It never writes anything, never modifies state, and never
# takes a fix action — it is a pure sensor. The Hermes cron job (see
# hermes_watchdog_job.md) runs it every 5 minutes, compares its output to the
# previous run, and acts on CHANGE (monitor mode).
#
# Output contract (byte-stable for change detection):
#   Healthy  -> exactly:  STATUS=OK
#   Anomaly  ->            STATUS=ANOMALY
#                          <class lines: key=value, one per check>
#                          <detail lines:  detail:...  informational only>
#
# Class lines are stable (bucketed) so the output only changes when the
# situation genuinely changes. `detail:` lines carry counts/names and MUST be
# ignored by the change-detection comparison (the Hermes prompt says so).
#
# No timestamps anywhere: timestamps would make the output change every run
# and defeat monitor-mode change detection. The Hermes side records when it
# first saw an anomaly.
#
# Invocation from Hermes (Windows) — read-only, nothing is copied to the VPS:
#   ssh -o BatchMode=yes root@srv1732329.hstgr.cloud 'bash -s' \
#       < watchdog/hermes_watchdog_check.sh
# =============================================================================
set -uo pipefail

PUBLIC_URL="${PUBLIC_URL:-https://srv1732329.hstgr.cloud}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/technopoliss}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
GLITCH_WINDOW="${GLITCH_WINDOW:-10m}"

CT_BACKEND="technopoliss-vernika"
CT_POSTGRES="technopoliss-postgres"
CT_OPENWA="openwa-api"
CT_CADDY="technopoliss-caddy"
PG_USER="technopoliss"
PG_DB="technopoliss"

ok_parts=()        # collected for a compact STATUS=OK line (currently unused)
bad_lines=()       # class lines for the anomaly report
detail_lines=()    # informational detail lines (ignored by change detection)

say_bad() { bad_lines+=("$1"); }
say_detail() { detail_lines+=("$1"); }

# --- 0. docker daemon --------------------------------------------------------
if ! docker info >/dev/null 2>&1; then
  echo "STATUS=ANOMALY"
  echo "docker=down"
  exit 0
fi

# --- 1. compose file present? ------------------------------------------------
if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
  say_bad "compose=missing:$COMPOSE_DIR/docker-compose.yml"
fi

# --- 2. containers: running + health -----------------------------------------
running=0
ct_bad=""
for ct in "$CT_BACKEND" "$CT_POSTGRES" "$CT_OPENWA" "$CT_CADDY"; do
  if docker inspect -f '{{.State.Running}}' "$ct" 2>/dev/null | grep -qx true; then
    running=$((running + 1))
    h=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$ct" 2>/dev/null)
    if [ "$h" = "unhealthy" ]; then
      ct_bad="$ct_bad $ct(unhealthy)"
    fi
  else
    ct_bad="$ct_bad $ct(down)"
  fi
done
if [ "$running" -eq 4 ] && [ -z "$ct_bad" ]; then
  : # containers OK -> no class line needed
else
  say_bad "containers=$running/4"
  [ -n "$ct_bad" ] && say_detail "detail:containers_bad=$ct_bad"
fi

# --- 3. public /health HTTP code ---------------------------------------------
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$PUBLIC_URL/health" 2>/dev/null)
if [ "$code" = "200" ]; then
  :
else
  say_bad "http=$code"
fi

# --- 4. postgres readiness ---------------------------------------------------
if docker exec "$CT_POSTGRES" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
  :
else
  say_bad "pg=notready"
fi

# --- 5. queue depth (runs INSIDE the backend container; env supplies creds) --
# stale_ready = workflow_jobs still 'ready' but due > 5 min ago (scheduler
# should have promoted/claimed them) -> potentially stuck queue.
# stuck_claimed = 'claimed'/'running' jobs whose lease expired > 5 min ago
# (claim_next should have reclaimed them) -> dead worker holding the queue.
# NEW: dispatcher_activity = completed jobs in the last 10 min. A large but
# FLOWING queue (560 jobs at 2 concurrent x 15s gap takes hours) is NOT stuck
# — only flag when stale jobs exist AND the dispatcher has gone quiet.
q=$(docker exec "$CT_BACKEND" python -c '
import os, psycopg2
try:
    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "postgres"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "technopoliss"),
        user=os.getenv("PGUSER", "technopoliss"),
        password=os.getenv("PGPASSWORD", ""),
        connect_timeout=5)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM workflow_jobs WHERE status=%s AND due_at_utc < extract(epoch from now()) - 300", ("ready",))
    stale = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM workflow_jobs WHERE status IN (%s,%s) AND lease_expires_at < extract(epoch from now()) - 300", ("claimed", "running"))
    stuck = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM workflow_jobs WHERE status=%s AND lease_expires_at >= extract(epoch from now()) - 600", ("completed",))
    recent = cur.fetchone()[0]
    conn.close()
    print("%d %d %d" % (stale, stuck, recent))
except Exception:
    print("ERR")
' 2>/dev/null) || q="ERR"

if [ "$q" = "ERR" ]; then
  say_bad "queue=err"
else
  stale=${q%% *}
  rest=${q#* }
  stuck=${rest%% *}
  recent=${rest##* }
  if [ "$stale" -eq 0 ] && [ "$stuck" -eq 0 ]; then
    : # queue empty -> OK
  elif [ "$stuck" -gt 0 ]; then
    say_bad "queue=stuck"
    say_detail "detail:queue_stuck_claimed=$stuck stale_ready=$stale recent_completed=$recent"
  elif [ "$recent" -gt 0 ]; then
    # Dispatcher still completing jobs -> queue is flowing, just large.
    say_detail "detail:queue_stale_ready=$stale recent_completed=$recent"
  else
    say_bad "queue=stuck"
    say_detail "detail:queue_stale_ready=$stale recent_completed=$recent"
  fi
fi

# --- 6. recent call glitches in backend logs ---------------------------------
# ERROR-level lines in the last window matching Vobiz stream close codes,
# Gemini quota exhaustion, tracebacks, or API-key validation failures.
g=$(docker logs --since "$GLITCH_WINDOW" "$CT_BACKEND" 2>&1 \
    | grep -E "ERROR" \
    | grep -cE "1011|RESOURCE_EXHAUSTED|Traceback|GEMINI_API_KEY failed validation" || true)
g="${g:-0}"
if [ "$g" -eq 0 ]; then
  :
elif [ "$g" -le 5 ]; then
  say_bad "glitch=low"
else
  say_bad "glitch=burst"
fi
[ "$g" -gt 0 ] && say_detail "detail:glitch_count_${GLITCH_WINDOW}=$g"

# --- 7. disk usage -----------------------------------------------------------
used=$(df -P / 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ -n "$used" ] && [ "$used" -gt "$DISK_WARN_PCT" ]; then
  say_bad "disk=high"
  say_detail "detail:disk_used_pct=$used"
fi

# --- emit --------------------------------------------------------------------
if [ "${#bad_lines[@]}" -eq 0 ]; then
  echo "STATUS=OK"
else
  echo "STATUS=ANOMALY"
  printf '%s\n' "${bad_lines[@]}"
  printf '%s\n' "${detail_lines[@]}"
fi
exit 0
