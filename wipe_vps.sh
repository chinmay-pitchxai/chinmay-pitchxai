#!/bin/bash
set -e

echo '=== STOPPING DATA-EDGE SERVICES ==='
systemctl stop data-edge 2>/dev/null || true
systemctl disable data-edge 2>/dev/null || true
systemctl stop data-edge-watchdog 2>/dev/null || true
systemctl disable data-edge-watchdog 2>/dev/null || true

echo '=== STOPPING ALL DOCKER CONTAINERS ==='
docker stop $(docker ps -q) 2>/dev/null || true
docker rm -f $(docker ps -aq) 2>/dev/null || true

echo '=== STOPPING NGINX ==='
systemctl stop nginx 2>/dev/null || true

echo '=== KILLING PYTHON ON PORT 8000 ==='
kill $(lsof -ti:8000) 2>/dev/null || true

echo '=== DELETING OLD PROJECTS ==='
rm -rf /root/Data-Edge /root/data-edge /root/data-edge.tar.gz
rm -rf /root/vernika
rm -f /root/generate_interested_report.py
rm -rf /opt/OpenWA /opt/data-edge
rm -f /opt/debug-*.log
rm -rf /app
rm -f /root/nul
rm -rf /var/www/html/*

echo '=== CLEANING DOCKER ==='
docker system prune -af --volumes 2>/dev/null || true

echo '=== CLEANING TEMP ==='
rm -rf /tmp/* 2>/dev/null || true

echo '=== VERIFY ==='
echo '--- LISTENING PORTS ---'
ss -tlnp
echo '--- /root/ contents ---'
ls -la /root/
echo '--- Docker containers ---'
docker ps -a 2>/dev/null || echo 'no docker ps'
echo '--- Disk ---'
df -h /
