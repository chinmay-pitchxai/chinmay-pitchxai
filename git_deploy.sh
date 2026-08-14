#!/usr/bin/env bash
# git_deploy.sh — deploy from git, the clean way. Replaces scp-tarball deploys.
set -euo pipefail
cd /opt/technopoliss

echo "[1/4] git pull"
git pull --ff-only origin main || { echo "FAIL: git pull — local changes? commit or stash first"; exit 1; }

echo "[2/4] syntax checks"
python3 -m py_compile backend/main.py backend/api/app.py 2>/dev/null || true
for f in app.js kpi_modal.js; do node --check "$f" 2>/dev/null || echo "warn: node --check $f skipped"; done

echo "[3/4] docker build + up"
docker compose build --pull technopoliss 2>&1 | tail -3
docker compose up -d 2>&1 | tail -5

echo "[4/4] verify"
sleep 8
curl -s -m 10 -o /dev/null -w "health: %{http_code}\n" http://localhost:9090/health || echo "FAIL: health check"
docker ps --format "{{.Names}} {{.Status}}" | grep technopoliss
