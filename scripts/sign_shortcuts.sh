#!/usr/bin/env bash
set -euo pipefail

# Generate and sign Apple Health shortcuts for all users.
# Must run NATIVELY on macOS (not in Docker) because `shortcuts sign` is macOS-only.
#
# Usage:
#   ./scripts/sign_shortcuts.sh                  # Sign for all users in users.yaml
#   ./scripts/sign_shortcuts.sh paul dad          # Sign for specific users only
#
# Output: data/shortcuts/<user_id>.shortcut (signed, ready to serve)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SHORTCUTS_DIR="$PROJECT_DIR/data/shortcuts"
UNSIGNED_DIR="$SHORTCUTS_DIR/unsigned"

# Check that shortcuts CLI exists (macOS only)
if ! command -v shortcuts &>/dev/null; then
    echo "ERROR: 'shortcuts' CLI not found. This must run on macOS, not in Docker."
    exit 1
fi

mkdir -p "$SHORTCUTS_DIR" "$UNSIGNED_DIR"

# Read the API token from gateway.yaml (plain grep, no PyYAML dependency)
GATEWAY_YAML=""
if [ -f "$HOME/.config/health-engine/gateway.yaml" ]; then
    GATEWAY_YAML="$HOME/.config/health-engine/gateway.yaml"
elif [ -f "$PROJECT_DIR/gateway.yaml" ]; then
    GATEWAY_YAML="$PROJECT_DIR/gateway.yaml"
else
    echo "ERROR: No gateway.yaml found. Cannot read API token."
    exit 1
fi

API_TOKEN=$(grep '^api_token:' "$GATEWAY_YAML" | sed 's/^api_token: *//' | tr -d '"' | tr -d "'")

if [ -z "$API_TOKEN" ]; then
    echo "ERROR: api_token not found in $GATEWAY_YAML"
    exit 1
fi

# Get user IDs: from args, or from users.yaml
if [ $# -gt 0 ]; then
    USER_IDS=("$@")
else
    USERS_YAML="$PROJECT_DIR/workspace/users.yaml"
    if [ ! -f "$USERS_YAML" ]; then
        USERS_YAML="$HOME/.openclaw/workspace/users.yaml"
    fi
    if [ ! -f "$USERS_YAML" ]; then
        echo "ERROR: users.yaml not found"
        exit 1
    fi
    # Extract unique user_ids with grep (no PyYAML needed)
    mapfile -t USER_IDS < <(grep 'user_id:' "$USERS_YAML" | sed 's/.*user_id: *//' | tr -d '"' | tr -d "'" | sort -u)
fi

echo "Signing shortcuts for ${#USER_IDS[@]} users..."
echo ""

SIGNED=0
FAILED=0

for uid in "${USER_IDS[@]}"; do
    UNSIGNED="$UNSIGNED_DIR/$uid.shortcut"
    SIGNED_FILE="$SHORTCUTS_DIR/$uid.shortcut"

    # Step 1: Generate unsigned shortcut (uses health-engine's venv if available)
    PYTHON="python3"
    if [ -f "$PROJECT_DIR/.venv/bin/python3" ]; then
        PYTHON="$PROJECT_DIR/.venv/bin/python3"
    fi

    $PYTHON -c "
import sys
sys.path.insert(0, '$PROJECT_DIR')
from engine.shortcuts.generator import generate_shortcut
data = generate_shortcut(user_id='$uid', api_token='$API_TOKEN')
with open('$UNSIGNED', 'wb') as f:
    f.write(data)
" 2>&1

    if [ ! -f "$UNSIGNED" ]; then
        echo "  FAIL  $uid — could not generate unsigned shortcut"
        FAILED=$((FAILED + 1))
        continue
    fi

    # Step 2: Sign it
    if shortcuts sign --mode anyone --input "$UNSIGNED" --output "$SIGNED_FILE" 2>&1; then
        SIZE=$(wc -c < "$SIGNED_FILE" | tr -d ' ')
        echo "  OK    $uid — signed ($SIZE bytes)"
        SIGNED=$((SIGNED + 1))
        rm -f "$UNSIGNED"
    else
        echo "  FAIL  $uid — signing failed, keeping unsigned as fallback"
        mv "$UNSIGNED" "$SIGNED_FILE"
        FAILED=$((FAILED + 1))
    fi
done

# Clean up unsigned dir if empty
rmdir "$UNSIGNED_DIR" 2>/dev/null || true

echo ""
echo "Done. $SIGNED signed, $FAILED failed."
echo "Files at: $SHORTCUTS_DIR/"
