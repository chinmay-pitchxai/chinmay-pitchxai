# Startup script: starts tunnel, updates .env, starts server
cd "/c/Users/Surya/Desktop/technopoliss(upgrade)/backend"

# 1. Kill any existing processes on port 9090
echo "Cleaning port 9090..."
for pid in $(netstat -ano 2>/dev/null | grep ":9090 " | grep LISTENING | awk '{print $NF}'); do
    taskkill /F /PID $pid 2>/dev/null
done
sleep 2

# 2. Start localtunnel and capture URL
echo "Starting localtunnel..."
lt --port 9090 > /tmp/lt_output.txt 2>&1 &
LT_PID=$!
sleep 5
TUNNEL_URL=$(grep -oP 'https://[a-z-]+\.loca\.lt' /tmp/lt_output.txt | tail -1)
echo "Tunnel URL: $TUNNEL_URL"

if [ -z "$TUNNEL_URL" ]; then
    echo "ERROR: Tunnel failed to start"
    exit 1
fi

# 3. Extract hostname
TUNNEL_HOST=$(echo "$TUNNEL_URL" | sed 's|https://||')
echo "Tunnel host: $TUNNEL_HOST"

# 4. Update .env
sed -i "s|=[a-z-]*\.loca\.lt|=$TUNNEL_HOST|g" .env
echo "Updated .env URLs:"
grep "loca.lt" .env

# 5. Start server
echo "Starting server..."
PYTHONPATH= .venv/Scripts/python.exe main.py &
SERVER_PID=$!
sleep 8

# 6. Verify
echo "Verifying localhost..."
curl -s -o /dev/null -w "  localhost: %{http_code}\n" "http://localhost:9090/api/dashboard/leads?limit=1"

echo "Verifying tunnel probe..."
curl -s -o /dev/null -w "  tunnel: %{http_code}\n" "$TUNNEL_URL/vobiz/answer?camp_id=startup_check&role=sales_1" --connect-timeout 15

echo ""
echo "============================================"
echo "SERVER: http://localhost:9090/"
echo "TUNNEL: $TUNNEL_URL"
echo "SERVER PID: $SERVER_PID  |  TUNNEL PID: $LT_PID"
echo "============================================"
