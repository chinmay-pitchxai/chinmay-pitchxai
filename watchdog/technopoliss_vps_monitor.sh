#!/usr/bin/env bash
# =============================================================================
# technopoliss_vps_monitor.sh — Layer 2 remote monitor (runs on the DEV machine
# via a Hermes cronjob, every ~15 min). It SSHes into the VPS and reports the
# health of the watchdog + stack.
#
# CONTRACT (for Hermes monitor_script hashing):
#   * Emit a SINGLE, STABLE line when healthy so Hermes suppresses the agent run.
#   * Any change in that line (a failure) triggers the agent to SSH in, drive the
#     watchdog manually, and report to the user.
#   * Do NOT include timestamps / counters in the healthy output (they change
#     every tick and would defeat change-detection).
# =============================================================================
set -uo pipefail

VPS_HOST="${TECHNO_VPS_HOST:-srv1732329.hstgr.cloud}"
VPS_USER="${TECHNO_VPS_USER:-root}"
SSH_KEY="${TECHNO_VPS_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
[ -f "$SSH_KEY" ] || SSH_KEY="${HOME}/.ssh/id_rsa"
[ -f "$SSH_KEY" ] || SSH_KEY="${HOME}/.ssh/vps_key"

SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)

if ! command -v ssh >/dev/null 2>&1; then
  echo "VPS_ERROR ssh_not_installed"
  exit 1
fi
if [ ! -f "$SSH_KEY" ]; then
  echo "VPS_ERROR no_ssh_key"
  exit 1
fi

ssh "${SSH_OPTS[@]}" "$VPS_USER@$VPS_HOST" 'bash -s' <<'REMOTE' 2>/dev/null
set -uo pipefail
# watchdog timer active?
timer="$(systemctl is-active technopoliss-watchdog.timer 2>/dev/null || echo inactive)"
# watchdog ticked recently (log file mtime within 5 min)?
fresh=no
le="$(stat -c %Y /var/log/technopoliss-watchdog.log 2>/dev/null || echo 0)"
ne="$(date +%s)"
[ -n "$le" ] && [ "$le" -gt 0 ] && [ $((ne - le)) -lt 300 ] && fresh=yes
# containers running?
up="$(docker ps --format '{{.Names}}' 2>/dev/null | wc -l | tr -d ' ')"
# backend liveness (backend publishes no host port — reach it inside the container)?
hb=down; docker exec technopoliss-vernika curl -sf --max-time 5 http://localhost:9090/health >/dev/null 2>&1 && hb=ok
# public endpoint?
pub="$(curl -sf --max-time 8 -o /dev/null -w '%{http_code}' https://srv1732329.hstgr.cloud/api/dashboard/leads?limit=1 2>/dev/null || echo 000)"
echo "VPS_OK timer=${timer} watchdog_fresh=${fresh} containers=${up} backend=${hb} public_http=${pub}"
REMOTE
status=$?

if [ "$status" -ne 0 ]; then
  echo "VPS_UNREACHABLE host=${VPS_HOST} ssh_exit=${status}"
fi
