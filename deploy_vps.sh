#!/usr/bin/env bash
# deploy_vps.sh — build + run the Technopoliss stack on the VPS, pointed at a public
# HTTPS/WSS host so Vobiz calls can connect and converse with Gemini.
#
# Run ON THE VPS (after the repo is copied there), e.g.:
#   scp -r . root@srv1732329.hstgr.cloud:/opt/technopoliss
#   ssh root@srv1732329.hstgr.cloud 'cd /opt/technopoliss && bash deploy_vps.sh'
#
# PUB_URL is the public HTTPS host Vobiz + your browser use. Defaults to the VPS
# hostname set in CADDY_HOST inside docker-compose.yml.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --- 1. Decide the public URL -------------------------------------------------
CADDY_HOST="${CADDY_HOST:-srv1732329.hstgr.cloud}"
PUB_URL="https://${CADDY_HOST}"

if command -v docker >/dev/null 2>&1; then
  echo "[OK] docker present: $(docker --version)"
else
  echo "[FAIL] docker not installed. Install: https://docs.docker.com/engine/install/"
  exit 1
fi

# --- 2. Write the public HTTPS URL into the backend env -----------------------
ENVF="$ROOT/backend/.env"
if [ -f "$ENVF" ]; then
  for K in VOBIZ_PUBLIC_BASE_URL VOBIZ_STREAM_PUBLIC_BASE_URL SERVER_URL; do
    if grep -q "^${K}=" "$ENVF"; then
      sed -i "s|^${K}=.*|${K}=${PUB_URL}|" "$ENVF"
    else
      echo "${K}=${PUB_URL}" >> "$ENVF"
    fi
  done
  echo "[OK] $ENVF now points Vobiz webhooks + stream at ${PUB_URL}"
  grep -E "^(VOBIZ_PUBLIC_BASE_URL|VOBIZ_STREAM_PUBLIC_BASE_URL|SERVER_URL)=" "$ENVF"
else
  echo "[WARN] $ENVF not found — Vobiz/Gemini credentials missing; copy backend/.env."
fi

# --- 3. Stop old stack, rebuild, start ---------------------------------------
docker compose down --remove-orphans || true
docker compose build
docker compose up -d

# --- 4. Verify the guard's probe target answer XML ----------------------------
sleep 6
echo "---- direct (inside network) ----"
curl -sf "http://localhost:9090/vobiz/answer?camp_id=deploy_probe&role=sales_1" | head -c 400; echo
echo "---- public HTTPS ----"
if curl -sf "${PUB_URL}/vobiz/answer?camp_id=deploy_probe&role=sales_1"; then
  echo
  echo "[OK] ${PUB_URL}/vobiz/answer reachable — calls should pass the guard."
else
  echo "[WARN] public probe failed. Ensure port 80+443 open and DNS points to this host."
fi

echo
echo "Dashboard : ${PUB_URL}/"
echo "Health    : ${PUB_URL}/health"
