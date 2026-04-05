#!/usr/bin/env bash
set -euo pipefail

# Deploy API code to Mac Mini via git pull and restart.
#
# Flow: commit locally -> push to GitHub -> pull on Mac Mini -> restart
# Mac Mini runs code from git, not rsync'd files.
#
# Usage:
#   ./scripts/deploy-api.sh              # Run tests + deploy + blue/green restart (default)
#   ./scripts/deploy-api.sh --skip-tests # Deploy without running tests
#   ./scripts/deploy-api.sh --reload     # Deploy + HUP reload (no fresh imports)
#   ./scripts/deploy-api.sh --cold       # Deploy + cold restart (dep changes)

REMOTE="mac-mini"
REMOTE_DIR="~/src/health-engine"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RESTART_FLAG="--hard"
RUN_TESTS=true

# Parse flags
for arg in "$@"; do
    case $arg in
        --test-first)
            RUN_TESTS=true  # already default, kept for backwards compat
            ;;
        --skip-tests)
            RUN_TESTS=false
            ;;
        --reload)
            RESTART_FLAG="--reload"
            ;;
        --cold)
            RESTART_FLAG="--cold"
            ;;
    esac
done

# Run tests by default
if [ "$RUN_TESTS" = true ]; then
    echo "Running tests..."
    cd "$LOCAL_DIR" && .venv/bin/python3 -m pytest tests/ -x -q --tb=short || {
        echo "Tests failed. Aborting deploy."
        exit 1
    }
    echo ""
fi

# 1. Push to GitHub
echo "Pushing to GitHub..."
cd "$LOCAL_DIR" && git push origin master

# 2. Pull on Mac Mini + sync deps
echo "Pulling on Mac Mini..."
ssh "$REMOTE" "cd $REMOTE_DIR && git pull && export PATH=\$HOME/.local/bin:\$PATH && uv sync --all-extras"

# 3. Restart API
echo "Restarting API ($RESTART_FLAG)..."
ssh "$REMOTE" "cd $REMOTE_DIR && bash scripts/restart-api.sh $RESTART_FLAG"

echo ""
echo "Deploy complete."
