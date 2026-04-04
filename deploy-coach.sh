#!/usr/bin/env bash
set -euo pipefail

# Deploy workspace files to Mac Mini, reset sessions, and verify routing.
# Usage:
#   ./deploy-coach.sh [workspace]                    # Copy files + reset all sessions (default)
#   ./deploy-coach.sh [workspace] --reset <phone>    # Copy files + reset one user
#   ./deploy-coach.sh [workspace] --reset all        # Copy files + reset all sessions (explicit)
#   ./deploy-coach.sh [workspace] --cold             # Copy files + full gateway restart (OpenClaw code changes only)

WORKSPACE_DIR="${1:-$(dirname "$0")/workspace}"
REMOTE="mac-mini"
REMOTE_WORKSPACE="~/.openclaw/workspace/"
REMOTE_PATH='export PATH="/opt/homebrew/bin:$HOME/Library/pnpm:$PATH"'

# Resolve to absolute path
WORKSPACE_DIR="$(cd "$WORKSPACE_DIR" && pwd)"

if [ ! -d "$WORKSPACE_DIR" ]; then
    echo "ERROR: workspace directory not found: $WORKSPACE_DIR"
    exit 1
fi

# Collect files to deploy: all .md files + users.yaml
FILES=()
for f in "$WORKSPACE_DIR"/*.md "$WORKSPACE_DIR"/users.yaml; do
    [ -f "$f" ] && FILES+=("$f")
done

if [ ${#FILES[@]} -eq 0 ]; then
    echo "ERROR: no .md or users.yaml files found in $WORKSPACE_DIR"
    exit 1
fi

echo "Deploying ${#FILES[@]} files to $REMOTE:$REMOTE_WORKSPACE"
echo "---"

TOTAL_SIZE=0
for f in "${FILES[@]}"; do
    SIZE=$(wc -c < "$f" | tr -d ' ')
    TOTAL_SIZE=$((TOTAL_SIZE + SIZE))
    printf "  %-30s %s bytes\n" "$(basename "$f")" "$SIZE"
done
echo "---"
echo "Total: $TOTAL_SIZE bytes"
echo ""

# Copy files
scp "${FILES[@]}" "$REMOTE:$REMOTE_WORKSPACE"
echo "Files copied."
echo ""

# Determine reset mode:
#   default (no flag)       -> reset all sessions (no gateway restart)
#   --reset <phone>         -> reset one user session
#   --reset all             -> reset all sessions (explicit)
#   --cold                  -> full gateway restart (only for OpenClaw code changes)

_reset_all_sessions() {
    echo "Resetting all user sessions..."
    ssh "$REMOTE" "$REMOTE_PATH; openclaw sessions --json 2>/dev/null | python3 -c \"
import json, sys
d = json.load(sys.stdin)
for s in d['sessions']:
    if ':direct:' in s['key']:
        print(s['key'])
\"" | while read -r key; do
        echo "  Resetting: $key"
        ssh "$REMOTE" "$REMOTE_PATH; openclaw gateway call sessions.reset --params '{\"key\": \"$key\"}' 2>/dev/null"
    done
    echo ""
    echo "Deploy complete. All sessions reset. Next message picks up new files."
}

if [ "${2:-}" = "--cold" ]; then
    echo "Cold restarting gateway (use only for OpenClaw code changes)..."
    ssh "$REMOTE" "$REMOTE_PATH; openclaw gateway stop && sleep 3 && openclaw gateway install"
    echo ""
    echo "Deploy complete. Gateway restarted."
elif [ "${2:-}" = "--reset" ]; then
    PHONE="${3:-all}"
    if [ "$PHONE" = "all" ]; then
        _reset_all_sessions
    else
        # Accept full session keys (e.g. agent:main:telegram:direct:80135247)
        # or phone numbers (defaults to whatsapp:direct)
        if [[ "$PHONE" == agent:* ]]; then
            SESSION_KEY="$PHONE"
        else
            SESSION_KEY="agent:main:whatsapp:direct:$PHONE"
        fi
        echo "Resetting session: $SESSION_KEY"
        ssh "$REMOTE" "$REMOTE_PATH; openclaw gateway call sessions.reset --params '{\"key\": \"$SESSION_KEY\"}'"
        echo ""
        echo "Deploy complete. Session reset. Next message picks up new files."
    fi
else
    # Default: reset all sessions without gateway restart
    _reset_all_sessions
fi

# Sign shortcuts on Mac Mini (runs natively, not in Docker)
echo ""
echo "Signing Apple Health shortcuts on Mac Mini..."
ssh "$REMOTE" "$REMOTE_PATH; cd ~/src/health-engine && bash scripts/sign_shortcuts.sh" 2>&1 || echo "WARNING: Shortcut signing failed (non-fatal)"

# ── Post-deploy verification ──
echo ""
echo "Verifying agent bindings..."

# Bindings are what actually route messages. Session key prefixes are unreliable
# after gateway restarts (sessions get recreated under default agent).
BINDINGS=$(ssh "$REMOTE" "$REMOTE_PATH; openclaw agents bindings 2>&1")
echo "$BINDINGS"
echo ""

# Verify expected bindings and enforce routing rules
echo "$BINDINGS" | python3 -c "
import sys
lines = sys.stdin.read()

# Required bindings
expected = {
    'main <- telegram': 'Default Telegram -> Milo',
}

# Forbidden bindings: Grigoriy (80135247) must NEVER route to K or any non-main agent
forbidden = {
    'k <- telegram peer=dm:80135247': 'Grigoriy must route to main (Milo), not K',
}

issues = []
for pattern, label in expected.items():
    if pattern not in lines:
        issues.append(f'  MISSING: {label} ({pattern})')

for pattern, label in forbidden.items():
    if pattern in lines:
        issues.append(f'  FORBIDDEN: {label} ({pattern})')
        issues.append(f'  FIX: openclaw agents unbind --agent k --all')

if issues:
    print('ROUTING ISSUES:')
    for i in issues:
        print(i)
    print('')
    print('Check: openclaw agents bindings')
    sys.exit(1)
else:
    print('  All bindings verified.')
" 2>&1 && ROUTING_OK=true || ROUTING_OK=false

if [ "$ROUTING_OK" = "false" ]; then
    echo ""
    echo "WARNING: Binding issues detected. Deploy succeeded but fix before users interact."
else
    echo ""
    echo "Verification complete."
fi
