#!/usr/bin/env bash
# install-watchdog.sh — install the Technopoliss self-healing watchdog on the VPS.
# Idempotent: safe to re-run. Copies files, enables the systemd timer, and runs
# one DRY-RUN tick so you can see exactly what it WOULD do before it goes live.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/opt/technopoliss/watchdog"

if [ "$(id -u)" -ne 0 ]; then
  echo "[FAIL] must run as root (systemd units + /var/log + docker access)." >&2
  exit 1
fi

echo "== installing watchdog from $SRC_DIR -> $DEST =="

if [ "$SRC_DIR" != "$DEST" ]; then
  mkdir -p "$DEST"
  cp -f "$SRC_DIR/technopoliss-watchdog.sh" "$DEST/"
  cp -f "$SRC_DIR/watchdog.conf" "$DEST/"
fi
chmod +x "$DEST/technopoliss-watchdog.sh"

cp -f "$SRC_DIR/technopoliss-watchdog.service" /etc/systemd/system/
cp -f "$SRC_DIR/technopoliss-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now technopoliss-watchdog.timer

echo
echo "== DRY-RUN tick (shows what the watchdog WOULD do) =="
WATCHDOG_DRY_RUN=1 WATCHDOG_CONF="$DEST/watchdog.conf" bash "$DEST/technopoliss-watchdog.sh"
echo
echo "== last log lines =="
tail -n 20 /var/log/technopoliss-watchdog.log 2>/dev/null || true
echo
echo "== timer status =="
systemctl list-timers technopoliss-watchdog.timer --no-pager 2>/dev/null || true
echo
echo "[OK] watchdog installed and timer active. Full log: /var/log/technopoliss-watchdog.log"
