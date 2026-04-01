#!/usr/bin/env bash
set -euo pipefail

# Deploy API code to Mac Mini and restart cleanly.
# Run from laptop.
#
# Usage:
#   ./scripts/deploy-api.sh              # Deploy all API files + restart
#   ./scripts/deploy-api.sh --test-first # Run tests before deploying

REMOTE="mac-mini"
REMOTE_DIR="~/src/health-engine"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Optionally run tests first
if [ "${1:-}" = "--test-first" ]; then
    echo "Running tests..."
    cd "$LOCAL_DIR" && .venv/bin/python3 -m pytest tests/ -x -q --tb=short || {
        echo "Tests failed. Aborting deploy."
        exit 1
    }
    echo ""
fi

# Sync API code (engine/ and mcp_server/ directories)
echo "Syncing code to $REMOTE..."
rsync -az --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='data/' \
    --exclude='workspace/' \
    --exclude='.git' \
    "$LOCAL_DIR/engine/" "$REMOTE:$REMOTE_DIR/engine/"

rsync -az --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$LOCAL_DIR/mcp_server/" "$REMOTE:$REMOTE_DIR/mcp_server/"

rsync -az --delete \
    --exclude='__pycache__' \
    "$LOCAL_DIR/dashboard/" "$REMOTE:$REMOTE_DIR/dashboard/"

rsync -az \
    "$LOCAL_DIR/scripts/restart-api.sh" "$REMOTE:$REMOTE_DIR/scripts/restart-api.sh"
rsync -az \
    "$LOCAL_DIR/scripts/run_v1_api.py" "$REMOTE:$REMOTE_DIR/scripts/run_v1_api.py" 2>/dev/null || true

echo "Code synced."
echo ""

# Restart API on Mac Mini
echo "Restarting API..."
ssh "$REMOTE" "cd $REMOTE_DIR && bash scripts/restart-api.sh"

echo ""
echo "Deploy complete."
