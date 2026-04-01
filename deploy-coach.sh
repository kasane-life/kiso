#!/usr/bin/env bash
set -euo pipefail

# Deploy workspace files to Mac Mini, reset sessions, and verify routing.
# Usage:
#   ./deploy-coach.sh [workspace] --reset <phone>   # Copy files + reset one user
#   ./deploy-coach.sh [workspace] --reset all        # Copy files + reset all users
#   ./deploy-coach.sh [workspace]                    # Copy files + restart gateway

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

# Reset specific user sessions or restart gateway
if [ "${2:-}" = "--reset" ]; then
    PHONE="${3:-}"
    if [ -z "$PHONE" ]; then
        echo "Usage: ./deploy-coach.sh [workspace_dir] --reset <phone>"
        echo "       ./deploy-coach.sh [workspace_dir] --reset all"
        exit 1
    fi

    if [ "$PHONE" = "all" ]; then
        echo "Resetting ALL user sessions..."
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
    fi
    echo ""
    echo "Deploy complete. Session(s) reset. Next message picks up new files."
else
    echo "Restarting gateway..."
    ssh "$REMOTE" "$REMOTE_PATH; openclaw gateway stop && sleep 3 && openclaw gateway install"
    echo ""
    echo "Deploy complete. All sessions reset (gateway restarted)."
    echo ""
    echo "Tip: Use --reset <phone> to reset one user without restarting:"
    echo "  ./deploy-coach.sh --reset +14152009584"
    echo "  ./deploy-coach.sh --reset all"
fi

# Sign shortcuts on Mac Mini (runs natively, not in Docker)
echo ""
echo "Signing Apple Health shortcuts on Mac Mini..."
ssh "$REMOTE" "$REMOTE_PATH; cd ~/src/health-engine && bash scripts/sign_shortcuts.sh" 2>&1 || echo "WARNING: Shortcut signing failed (non-fatal)"

# ── Post-deploy verification ──
echo ""
echo "Verifying agent routing..."
ROUTING_OK=true

# Check bindings are correct
BINDINGS=$(ssh "$REMOTE" "$REMOTE_PATH; openclaw agents bindings 2>&1")
echo "$BINDINGS"
echo ""

# Check all active sessions route to the expected agent
echo "Checking active sessions..."
ssh "$REMOTE" "$REMOTE_PATH; openclaw sessions --json 2>/dev/null" | python3 -c "
import json, sys

expected = {
    'telegram:direct:6460316634': 'main',   # Grigoriy -> Milo
    'telegram:direct:80135247': 'k',         # Andrew -> K
}

d = json.load(sys.stdin)
issues = []
for s in d.get('sessions', []):
    key = s.get('key', '')
    agent = key.split(':')[1] if ':' in key else '?'

    # Check telegram sessions specifically (where misrouting happened)
    for pattern, expected_agent in expected.items():
        if pattern in key and agent != expected_agent:
            issues.append(f'  MISROUTE: {key} -> agent:{agent} (expected {expected_agent})')

if issues:
    print('ROUTING ISSUES DETECTED:')
    for i in issues:
        print(i)
    print('')
    print('Fix: openclaw gateway call sessions.reset --params \\'{ \"key\": \"<session_key>\" }\\'')
    sys.exit(1)
else:
    print('  All sessions routed correctly.')
" 2>&1 && ROUTING_OK=true || ROUTING_OK=false

if [ "$ROUTING_OK" = "false" ]; then
    echo ""
    echo "WARNING: Routing issues detected. Deploy succeeded but fix misroutes before users interact."
else
    echo ""
    echo "Verification complete."
fi
