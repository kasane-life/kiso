#!/bin/bash
# Gateway health check — validates that port 18800 serves the FULL gateway.
# Runs hourly via launchd. Alerts Andrew via Telegram on failure.

FAILURES=""

# Check 1: health endpoint
HEALTH=$(curl -sf http://localhost:18800/health 2>/dev/null)
if [ $? -ne 0 ]; then
    FAILURES="$FAILURES\n- Port 18800 not responding"
fi

# Check 2: /auth/garmin returns non-404
AUTH_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:18800/auth/garmin?user=probe&state=probe:garmin:0:0" 2>/dev/null)
if [ "$AUTH_STATUS" = "404" ] || [ "$AUTH_STATUS" = "000" ]; then
    FAILURES="$FAILURES\n- /auth/garmin returning $AUTH_STATUS (full gateway not running)"
fi

# Check 3: service name should NOT be kiso-v1
SERVICE=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get(service,unknown))" 2>/dev/null)
if [ "$SERVICE" = "kiso-v1" ]; then
    FAILURES="$FAILURES\n- Port 18800 running v1-only API instead of full gateway"
fi

# Check 4: Stuck users (created >48h ago, no wearable data)
TOKEN="NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY"
DEEP=$(curl -sf "http://localhost:18800/health/deep?token=$TOKEN" 2>/dev/null)
STUCK=$(echo "$DEEP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    stuck = d.get('checks', {}).get('stuck_users', {})
    if stuck:
        names = [f'{k} ({v[\"days\"]}d)' for k, v in stuck.items() if isinstance(v, dict) and v.get('status') == 'stuck']
        if names:
            print(', '.join(names))
except: pass
" 2>/dev/null)
if [ -n "$STUCK" ]; then
    FAILURES="$FAILURES\n- Stuck users (no wearable data): $STUCK"
fi

# Check 5: Cloudflare tunnel alive
CF_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "https://auth.mybaseline.health/health" 2>/dev/null)
if [ "$CF_STATUS" = "000" ] || [ "$CF_STATUS" = "502" ] || [ "$CF_STATUS" = "503" ]; then
    FAILURES="$FAILURES\n- Cloudflare tunnel down (HTTP $CF_STATUS)"
fi

if [ -n "$FAILURES" ]; then
    MSG="GATEWAY ALERT $(date +%H:%M):$(echo -e "$FAILURES")"
    echo "$MSG"

    # Dedup: only send one alert per unique failure set per day.
    # Data stays in the state file for debugging; repeated alerts are suppressed.
    STATE_FILE="/tmp/healthcheck_last_alert_$(date +%Y%m%d)"
    if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE")" = "$FAILURES" ]; then
        echo "duplicate alert suppressed $(date)"
        exit 1
    fi
    echo "$FAILURES" > "$STATE_FILE"

    # Alert via WhatsApp
    export PATH="/opt/homebrew/bin:$HOME/Library/pnpm:$PATH"
    openclaw agent --to +14152009584 --channel whatsapp --message "$MSG" 2>/dev/null
    exit 1
else
    echo "ok $(date)"
    exit 0
fi
