#!/usr/bin/env bash
set -euo pipefail

# Restart the Kiso API. Two modes:
#
#   --reload  (default) Graceful reload via HUP signal. Zero downtime.
#             New workers start, old workers drain in-flight requests.
#
#   --cold    Full stop/start. Use only when graceful reload fails
#             or after dependency changes that require process restart.
#
# Gunicorn master process stays alive across reloads. Workers are replaced.
# Tested: 30 concurrent health checks during reload, zero failures.
#
# Usage (local on Mac Mini):
#   ./scripts/restart-api.sh           # graceful reload (default)
#   ./scripts/restart-api.sh --cold    # full restart
#
# Usage (remote from laptop):
#   ssh mac-mini 'cd ~/src/health-engine && bash scripts/restart-api.sh'

SERVICE="com.baseline.gateway"
PLIST="$HOME/Library/LaunchAgents/${SERVICE}.plist"
PORT=18800
LOG="/tmp/baseline-gateway.log"
MODE="${1:---reload}"
PIDFILE="/tmp/kiso-gunicorn.pid"

if [ "$MODE" = "--reload" ]; then
    # Graceful reload: send HUP to gunicorn master
    if [ -f "$PIDFILE" ]; then
        MASTER_PID=$(cat "$PIDFILE")
        if kill -0 "$MASTER_PID" 2>/dev/null; then
            echo "Graceful reload: sending HUP to gunicorn master (PID $MASTER_PID)..."

            # Clear bytecode cache so new workers pick up fresh code
            find ~/src/health-engine/engine/gateway/__pycache__ -name "*.pyc" -delete 2>/dev/null || true
            find ~/src/health-engine/mcp_server/__pycache__ -name "*.pyc" -delete 2>/dev/null || true

            kill -HUP "$MASTER_PID"

            # Wait for new workers to come up
            sleep 2
            if curl -sf http://localhost:$PORT/health >/dev/null 2>&1; then
                echo "Reload complete. API healthy on port $PORT ($(date +%H:%M:%S))"
                exit 0
            else
                echo "WARN: Health check failed after reload. Falling back to cold restart."
                MODE="--cold"
            fi
        else
            echo "PID file exists but process $MASTER_PID is dead. Doing cold start."
            MODE="--cold"
        fi
    else
        echo "No PID file at $PIDFILE. Doing cold start."
        MODE="--cold"
    fi
fi

if [ "$MODE" = "--cold" ]; then
    echo "Cold restart: stopping $SERVICE..."

    # Step 1: Unload launchd service
    launchctl bootout "gui/$(id -u)/$SERVICE" 2>/dev/null || true
    sleep 1

    # Step 2: Kill any remaining process on the port
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

    # Step 4: Clear bytecode cache
    find ~/src/health-engine/engine/gateway/__pycache__ -name "*.pyc" -delete 2>/dev/null || true
    find ~/src/health-engine/mcp_server/__pycache__ -name "*.pyc" -delete 2>/dev/null || true

    # Step 5: Re-bootstrap the service
    echo "Starting $SERVICE..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || {
        echo "Bootstrap failed. Starting manually..."
        cd ~/src/health-engine
        nohup .venv/bin/gunicorn -c gunicorn.conf.py \
            "engine.gateway.server:create_app()" \
            --pid "$PIDFILE" \
            > "$LOG" 2>&1 &
    }

    # Step 6: Wait for API to come up
    for i in {1..15}; do
        if curl -sf http://localhost:$PORT/health >/dev/null 2>&1; then
            echo "API is up on port $PORT ($(date +%H:%M:%S))"
            exit 0
        fi
        sleep 1
    done

    echo "ERROR: API did not start within 15 seconds"
    tail -10 "$LOG" 2>/dev/null
    exit 1
fi
