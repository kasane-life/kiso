# Mac Mini Admin Guide (for Claude Code)

Use this when debugging OpenClaw agents or the Kiso API on the Mac Mini.

## Architecture

- **Kiso API**: Python FastAPI running directly (NOT Docker). Port 18800.
  - Script: `~/src/health-engine/scripts/run_v1_api.py`
  - Logs: `/tmp/kiso-api.log`
  - Data: `~/src/health-engine/data/` (kasane.db + per-user CSVs)
  - Config: `~/.config/health-engine/gateway.yaml`
- **OpenClaw**: Node.js gateway on port 18789. LaunchAgent service.
  - Config: `~/.openclaw/openclaw.json`
  - Workspace: `~/.openclaw/workspace/` (SOUL.md, AGENTS.md, etc.)
  - Logs: `/tmp/openclaw/openclaw-$(date +%Y-%m-%d).log`
- **Cloudflare Tunnel**: Routes `auth.mybaseline.health` to localhost:18800.
  - Config: `~/.cloudflared/config.yml`

## Common Commands

### OpenClaw
```bash
export PATH="/opt/homebrew/bin:$HOME/Library/pnpm:$PATH"

# Status
openclaw gateway status
openclaw cron list

# Sessions
openclaw gateway call sessions.list
openclaw gateway call sessions.reset --params '{"key": "agent:main:whatsapp:direct:+PHONE"}'
openclaw gateway call sessions.delete --params '{"key": "agent:main:whatsapp:direct:+PHONE"}'

# Restart gateway (CAUTION: only once per 30 min, see WhatsApp safety rules)
openclaw gateway stop
openclaw gateway install

# Send agent message (never use --deliver, agent sends via its own WhatsApp tool)
openclaw agent --to +PHONE --channel whatsapp --message '...'
```

### Kiso API
```bash
# Check status
curl -s http://localhost:18800/health

# Restart API
kill $(lsof -t -i :18800) $(lsof -t -i :18801) 2>/dev/null
sleep 2
export KISO_V1_PORT=18800
nohup python3 ~/src/health-engine/scripts/run_v1_api.py > /tmp/kiso-api.log 2>&1 &

# Test with admin token
TOKEN=$(grep api_token ~/.config/health-engine/gateway.yaml | head -1 | awk '{print $2}')
curl -s "http://localhost:18800/api/v1/persons?token=$TOKEN"

# Pull Garmin
curl -s "http://localhost:18800/api/pull_garmin?user_id=andrew&token=$TOKEN"
```

### Deploy workspace changes
```bash
# From laptop:
./deploy-coach.sh workspace --reset +PHONE
# Or manually:
scp workspace/* mac-mini:~/.openclaw/workspace/
```

## Troubleshooting

### Milo not responding
1. Check OpenClaw: `openclaw gateway status` (should say "running")
2. Check API: `curl http://localhost:18800/health` (should return ok)
3. Check logs: `tail -50 /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | grep -i error`
4. If gateway down: `openclaw gateway install`
5. If API down: restart using commands above

### Garmin pull failing
1. Check tokens: `ls ~/.config/health-engine/tokens/garmin/andrew/`
2. Token expiry: tokens expire every ~24hrs, refresh automatically if refresh token valid
3. If "No tokens found": symlink may be broken. Check `ls -la ~/.config/health-engine/tokens/garmin/`
4. If rate limited (429): wait 30 min. The throttle resets.
5. Re-auth: `cd ~/src/health-engine && .venv/bin/python3 cli.py auth garmin`

### WhatsApp disconnected
1. DO NOT restart gateway more than once in 30 minutes
2. Check: `openclaw gateway status` and look for WhatsApp connection
3. If disconnected: relink from WhatsApp app (Linked Devices > Link a Device)
4. Wait 60 seconds after relink before sending any messages

### Tool errors
1. Check API logs: `tail -50 /tmp/kiso-api.log`
2. Common: "could not convert string to float: ''" → empty CSV field. Check the specific CSV.
3. Common: "Person not found" → user has CSV data but no person record in kasane.db

## User Mapping

| User | user_id | person_id | Phone | Channel |
|---|---|---|---|---|
| Andrew | andrew | andrew-deal-001 | +14152009584 | WhatsApp |
| Grigoriy | grigoriy | grigoriy-001 | +79872907160 | Telegram |
| Manny | manny | manny-001 | - | Text |
| Dad | dad | dad-001 | +12022552119 | - |
| Paul | paul | 230b25d3-... | +17038878948 | WhatsApp |
| Mike | mike | mike-001 | +17033625977 | WhatsApp |

## Data Locations

- SQLite: `~/src/health-engine/data/kasane.db` (primary store)
- CSVs: `~/src/health-engine/data/users/<user_id>/` (backup, dual-write during migration)
- Garmin tokens: `~/.config/health-engine/tokens/garmin/andrew/` (symlinked from default/)
- Briefing: `~/src/health-engine/data/users/<user_id>/briefing.json` (rebuilt on pull)

## Safety Rules

- NEVER restart OpenClaw gateway more than once in 30 minutes
- NEVER send outbound messages to multiple new numbers quickly
- NEVER push code without running tests first
- NEVER use Opus for cron/background tasks (Sonnet or Haiku only)
- Always backup kasane.db before schema changes: `cp data/kasane.db data/kasane.db.backup-$(date +%Y%m%d)`
