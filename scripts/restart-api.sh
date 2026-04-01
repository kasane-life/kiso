#!/usr/bin/env bash
set -euo pipefail

# Restart the Kiso API cleanly. Handles launchd KeepAlive respawn race.
#
# Single process: com.baseline.gateway on port 18800.
# Runs python3 -m engine.gateway (server.py) which serves everything:
#   - /api/* (Milo MCP tool dispatch)
#   - /api/v1/* (Kasane iOS sync)
#   - /auth/* (Garmin, Google OAuth)
#   - /dashboard/* (static member dashboard)
#   - /health, /health/deep (monitoring)
#
# Usage (local on Mac Mini):
#   ./scripts/restart-api.sh
#
# Usage (remote from laptop):
#   ssh mac-mini 'cd ~/src/health-engine && bash scripts/restart-api.sh'

SERVICE="com.baseline.gateway"
PLIST="$HOME/Library/LaunchAgents/${SERVICE}.plist"
PORT=18800
LOG="/tmp/baseline-gateway.log"

echo "Stopping $SERVICE..."

# Step 1: Disable KeepAlive by unloading the service entirely
launchctl bootout "gui/$(id -u)/$SERVICE" 2>/dev/null || true
sleep 1

# Step 2: Kill any remaining process on the port (may need multiple attempts
# since bootout can race with KeepAlive respawn)
for attempt in 1 2 3; do
    PID=$(lsof -ti :$PORT 2>/dev/null || true)
    if [ -z "$PID" ]; then
        break
    fi
    echo "Killing process $PID on port $PORT (attempt $attempt)"
    kill -9 $PID 2>/dev/null || true
    sleep 1
done

# Step 3: Verify port is free
if lsof -ti :$PORT >/dev/null 2>&1; then
    echo "ERROR: Port $PORT still in use after kill. Aborting."
    lsof -i :$PORT
    exit 1
fi

# Step 4: Clear Python bytecode cache for gateway module
find ~/src/health-engine/engine/gateway/__pycache__ -name "*.pyc" -delete 2>/dev/null || true
find ~/src/health-engine/mcp_server/__pycache__ -name "*.pyc" -delete 2>/dev/null || true

# Step 5: Re-bootstrap the service (loads fresh code)
echo "Starting $SERVICE..."
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || {
    echo "Bootstrap failed. Starting manually..."
    cd ~/src/health-engine
    nohup .venv/bin/python3 -m engine.gateway > "$LOG" 2>&1 &
}

# Step 6: Wait for API to come up
for i in {1..10}; do
    if curl -sf http://localhost:$PORT/health >/dev/null 2>&1; then
        echo "API is up on port $PORT ($(date +%H:%M:%S))"
        exit 0
    fi
    sleep 1
done

echo "ERROR: API did not start within 10 seconds"
tail -5 "$LOG" 2>/dev/null
exit 1
