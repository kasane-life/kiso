# Kiso — Instructions for Contributors

Kiso (基礎, "foundation") is the backend platform for Kasane (iOS app), Milo (coaching agent), and the health intelligence system. One monolithic Python process serving all three from a single Docker container on Mac Mini.

## For iOS Developers (Paul)

You care about the `/api/v1/` contract. Start here:

- **API reference**: `docs/API.md` — all endpoints, request/response shapes, auth, error format
- **Pydantic models**: `engine/gateway/v1_models.py` — camelCase output models
- **Route implementations**: `engine/gateway/v1_api.py` — FastAPI handlers
- **Sync protocol**: Last-write-wins by `updatedAt`. Client pushes changes, pulls server changes. See API.md for full details.
- **Base URL**: `http://localhost:18800/api/v1` (dev), `https://auth.mybaseline.health/api/v1` (prod)
- **Auth**: `?token=X` or `Authorization: Bearer X`

Key architecture concept: `person.healthEngineUserId` links a Kasane person to a health data directory (`data/users/<user_id>/`). The context endpoint merges both.

## For Agent Developers (Milo/OpenClaw)

Milo is a coaching agent running on Mac Mini via OpenClaw. It talks to Kiso through MCP tools.

- **MCP tool implementations**: `mcp_server/tools.py` — 40+ tools registered
- **Workspace files**: `workspace/` — deployed to `~/.openclaw/workspace/` on Mac Mini
- **Key workspace files**: SOUL.md (identity), AGENTS.md (coaching logic, 34KB), TOOLS.md (API ref), USER.md (Andrew's profile), HEARTBEAT.md (proactive schedule), users.yaml (phone-to-user mapping)
- **OpenClaw loads only 8 filenames**: AGENTS.md, SOUL.md, TOOLS.md, USER.md, IDENTITY.md, HEARTBEAT.md, BOOTSTRAP.md, MEMORY.md. Any other filename is invisible to the agent.
- **Deploy**: `./deploy-coach.sh workspace --reset +PHONE` copies files to Mac Mini + resets session
- **Multi-user**: All tools accept `user_id`. Per-user data at `data/users/<user_id>/`. `users.yaml` maps phone to user_id.

## Architecture

See `docs/ARCHITECTURE.md` for the full picture. Quick summary:

```
Kasane (iOS) ----> /api/v1/* ----> SQLite (kasane.db)
                                      |
                                      | person.healthEngineUserId
                                      v
Milo (agent) ----> MCP tools ----> CSVs (data/users/<user_id>/)
                                      ^
iOS Shortcuts ---> /api/{tool} ------/
```

**Two storage systems, one bridge:**
- SQLite (`data/kasane.db`): Persons, habits, check-ins, focus plans, messages
- CSVs (`data/users/<user_id>/`): Weight, meals, labs, BP, wearable snapshots
- `get_person_context` merges both into one read

**Deployment:**
- One Docker container on Mac Mini (M4 Pro), port 18800
- Cloudflare Tunnel for HTTPS
- Agent workspace deployed separately via `deploy-coach.sh`
- Config: `~/.config/health-engine/gateway.yaml` (legacy path, backward compat)

## How to Coach

When someone checks in ("how am I doing?", "morning check-in"):

**Use MCP tools (production):**
- `checkin()` — full coaching snapshot
- `score()` — 20 metrics with NHANES percentiles
- `get_protocols()` — active protocol progress

**CLI (local dev):**
```bash
python3 cli.py briefing          # JSON coaching snapshot
python3 cli.py score --json      # Machine-readable scores
python3 cli.py pull garmin       # Fresh wearable data
```

### Coaching voice
- Direct, warm, data-grounded. Like a trainer who knows your numbers.
- Reference actual data: "HRV is at 58, down from 64 last week"
- Connect metrics: "Sleep at 6.2hrs is dragging HRV down, recovery isn't complete"
- One critical thing, one positive thing, one nudge. That's a good check-in.
- Don't show raw JSON unless asked. Don't open with "based on the data." Just coach.

## Data Freshness

Garmin data: check `last_updated` in the briefing. If stale:
```bash
python3 cli.py pull garmin                      # Latest metrics
python3 cli.py pull garmin --history --workouts  # + 90-day trends + workouts
```

Garmin auth:
```bash
python3 cli.py auth garmin    # Interactive, caches tokens at ~/.config/health-engine/garmin-tokens/
```

Apple Health import:
```bash
python3 cli.py import apple-health /path/to/export.zip
python3 cli.py import apple-health /path/to/export.zip --lookback-days 180
```

## Getting Someone Set Up

1. **Quickest**: Run `./setup.sh`
2. **Manual**: `cp config.example.yaml config.yaml`, edit age/sex/targets
3. **You do it**: Ask their age and sex, create config

After setup, `python3 cli.py status` shows available data.

## Project Layout

```
engine/
  gateway/         FastAPI server, auth, API handlers
    server.py      App factory, route registration
    api.py         Tool dispatch (/api/{tool_name})
    v1_api.py      Kasane sync + CRUD (/api/v1/*)
    v1_models.py   Pydantic models (camelCase for iOS)
    db.py          SQLite schema + connection management
    config.py      Gateway config loader
    token_store.py Encrypted token storage
  scoring/         NHANES percentiles, clinical zones
  insights/        Coaching rules, pattern detection
  coaching/        Briefing assembly, protocols
  integrations/    Garmin, Apple Health, Google Calendar, Oura, Whoop
  tracking/        Weight, nutrition, strength, habits
  models.py        Core dataclasses

mcp_server/
  server.py        FastMCP entry point
  tools.py         40+ tool implementations + registry

workspace/         Milo agent files (deployed to Mac Mini)
data/              Local-first storage (gitignored)
scripts/           Admin + seed scripts
tests/             416+ tests
docs/              API, Architecture, Roadmap, Methodology
```

## Rules

- Never hardcode secrets in source files
- Thresholds go in `engine/insights/rules.yaml`, not in code
- Use `python3` not `python`
- Run tests: `python3 -m pytest tests/ -v` — all 416+ must pass
- Smoke test end-to-end before pushing

## Engineering Standards

Every feature ships with:
- Tests (unit + integration)
- Audit logging (user_id, params, latency)
- Error messages that tell the user what to do
- Security review: no plaintext secrets, HMAC-signed links, encrypted tokens at rest

Integration standards:
- OAuth 2.0 Authorization Code + PKCE for third-party services
- Tokens encrypted at rest (Fernet/AES)
- Rate limit auth endpoints
- Narrowest OAuth scope that works

## Methodology

When a user asks "why do you measure this?" or "how does scoring work?", reference `docs/METHODOLOGY.md`. Key points:

- **Clinical zones** (AHA, ADA, ESC) are the primary signal. They answer "am I healthy?"
- **Population percentiles** are context. The 50th percentile = median American. Better than average doesn't mean healthy.
- **Freshness**: Old data counts less. 18-month-old labs get ~33% credit.
- **Cross-metric patterns**: Metabolic syndrome, insulin resistance, recovery stress. Compound signals matter more than individual metrics.

## Docs Index

| Doc | Audience | What it covers |
|-----|----------|----------------|
| `docs/API.md` | iOS developers | Full v1 REST contract |
| `docs/ARCHITECTURE.md` | All contributors | System design, storage, deploy model |
| `docs/ROADMAP.md` | All contributors | Cloud progression phases |
| `docs/MILO.md` | Andrew, Paul | Milo integration, Mac Mini, cloud plan |
| `docs/METHODOLOGY.md` | Curious users, coach | Why we score each metric |
| `docs/SCORING.md` | Contributors | How the scoring engine works |
| `docs/METRICS.md` | Contributors | 20-metric catalog |
| `docs/ONBOARDING.md` | New users | Setup walkthrough |
| `docs/DATA_FORMATS.md` | Contributors | CSV/JSON schemas |
