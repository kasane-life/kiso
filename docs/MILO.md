# Milo: Coaching Agent Integration

Milo is the AI coaching agent. It runs on the Mac Mini via OpenClaw and uses Kiso as its backend. This doc covers how Milo works today, how it connects to Kasane, and the plan for scaling to cloud.

## How It Works Today

```
User (WhatsApp)
    |
    v
OpenClaw (agent framework, Mac Mini)
    |
    v
Milo (LLM + workspace files + tools)
    |
    +--> MCP tools (40+) --> Kiso gateway (port 18800)
    |                           |
    |                           +--> SQLite (kasane.db) — person context
    |                           +--> CSVs (data/users/) — health metrics
    |
    +--> WhatsApp (sends replies)
```

**Three key pieces:**
1. **OpenClaw** manages the agent lifecycle: sessions, message routing, tool dispatch, WhatsApp integration
2. **Workspace files** (SOUL.md, AGENTS.md, etc.) define Milo's personality, coaching methodology, and behavior rules
3. **Kiso MCP tools** give Milo read/write access to health data and Kasane entities

### What Milo Can Do

Through Kiso's MCP tools, Milo can:
- Read a person's profile, habits, and check-in history before coaching
- Write coaching messages that show up in the Kasane app (via sync)
- Log meals, weight, BP, habits on behalf of the user
- Pull fresh Garmin/wearable data
- Generate scoring reports and health insights
- Read the unified person context (SQLite + CSVs merged)

### Multi-user Routing

Each WhatsApp number maps to a `user_id` in `workspace/users.yaml`. All Kiso tools accept `user_id` as a parameter. Per-user data lives at `data/users/<user_id>/`.

Current users: Andrew (default), Paul, Mike, Dad.

## How Milo Connects to Kasane

The bridge is `person.healthEngineUserId`. When the iOS app syncs a person to the server, that person can be linked to a Kiso user directory. The `/api/v1/persons/:id/context` endpoint merges both data sources into one read.

This means:
- Kasane writes habits and check-ins via `/api/v1/sync`
- Milo reads them via `get_person_context` tool (which hits the same SQLite)
- Milo writes coaching messages and focus plans back to SQLite
- Kasane picks them up on next sync

Neither client needs to know about the other. The API is the meeting point.

## Mac Mini Setup

**Hardware**: Mac Mini M4 Pro, local network at 10.0.0.128 (`ssh mac-mini`)

**Services running:**
- Kiso gateway: Docker container `health-engine-gateway`, port 18800
- Cloudflare Tunnel: HTTPS at `auth.mybaseline.health`
- OpenClaw agent: Manages Milo sessions, WhatsApp, cron jobs

**Key paths on Mac Mini:**
| Path | What |
|------|------|
| `~/src/health-engine/` | Kiso repo (git) |
| `~/src/health-engine/data/` | SQLite + CSVs |
| `~/.openclaw/workspace/` | Milo's workspace files |
| `~/.openclaw/openclaw.json` | OpenClaw config |
| `~/.config/health-engine/gateway.yaml` | Gateway config (port, auth) |
| `~/.config/health-engine/tokens/` | Encrypted wearable tokens |

**Deployment workflow:**
```bash
# From local machine:

# 1. Deploy workspace changes (no gateway restart)
./deploy-coach.sh workspace --reset +14152009584

# 2. Deploy code changes
git push origin main
ssh mac-mini "cd ~/src/health-engine && git pull origin main"
# Then rebuild Docker image + restart container

# 3. Nuclear restart (full Docker rebuild)
ssh mac-mini
docker stop health-engine-gateway && docker rm health-engine-gateway
docker build -t health-engine-gateway .
docker run -d --name health-engine-gateway --restart unless-stopped \
  -p 18800:18800 -e HE_CONFIG_DIR=/app \
  -v ~/src/health-engine/data:/app/data \
  -v ~/src/health-engine/config.yaml:/app/config.yaml:ro \
  -v ~/.config/health-engine/gateway.yaml:/app/gateway.yaml:ro \
  -v ~/.config/health-engine/tokens:/home/appuser/.config/health-engine/tokens \
  -v ~/.config/health-engine/token.key:/home/appuser/.config/health-engine/token.key \
  -v ~/src/health-engine/protocols:/app/protocols:ro \
  -v ~/src/health-engine/engine/coaching/skill_ladders.yaml:/app/engine/coaching/skill_ladders.yaml:ro \
  health-engine-gateway
```

## Why Mac Mini First

Starting with a local server instead of cloud:

1. **Zero cost**. No AWS/Railway bill while we have 2 users.
2. **Full control**. SSH in, inspect logs, tweak configs, no deploy pipeline to debug.
3. **Fast iteration**. Change workspace files and reset a session in seconds.
4. **Data stays local**. No HIPAA/PHI concerns while we're pre-product.
5. **Performance is fine**. M4 Pro handles our load with headroom.

The trade-offs we accept:
- No redundancy (if Mac Mini goes down, so does Milo)
- No automatic scaling
- Manual deploys via SSH
- Single point of failure for data (Litestream backup planned)

These are acceptable trade-offs for 2 users. They become unacceptable at different thresholds.

## Cloud Progression Plan

Each phase triggers when the previous one creates friction. Not before.

### Phase 0: Now (0-2 users)

**Where we are.** SQLite + CSVs, static API token, Mac Mini Docker.

**What's next (before Phase 1):**
- **JWT auth**: Replace static token with per-user JWTs. HMAC-SHA256, 1hr access + 7d refresh. Existing static token stays as admin/service key. Required for multi-user sync.
- **Litestream backups**: Stream SQLite WAL to Cloudflare R2 in real-time. One binary, zero config. Point-in-time restore. ~$0.01/mo. Eliminates "what if the Mac Mini dies" anxiety.
- **Request ID tracing**: Correlate sync calls end-to-end for debugging.

**Trigger for Phase 1**: Paul's SyncService validated, CloudKit disabled, first external users beyond Andrew/Paul.

### Phase 1: Concierge Beta (5-10 users)

**What changes:**
- **Supabase migration**: Swap `db.py` internals from SQLite to Supabase Postgres. Same API contract, zero changes to `v1_api.py`, iOS, or Milo. Free tier covers <500MB, 50K requests/mo.
- **Row-level security**: Users only see their own data. Enforced at the database level.
- **Realtime subscriptions**: Paul drops polling, gets push updates via Supabase Realtime.
- **Background job queue**: Replace threadpool with Redis or pg-backed queue. Retries, dead letter, visibility.
- **Structured observability**: Request logs, latency percentiles, error rates.

**Migration plan:**
1. Provision Supabase project
2. Port schema (TEXT PKs to UUIDs, same column names)
3. Replace `get_db()` in `db.py` with Supabase client
4. Run both in parallel for one week, compare data
5. Cut over

**Trigger for Phase 2**: Paying users, latency SLAs matter, team grows.

### Phase 2: Growth (50-100 users)

- CDN for API (Cloudflare Workers, edge caching for GETs)
- Rate limiting per user
- Webhooks (notify external systems on data changes)
- Audit log in Postgres
- CI/CD pipeline (automated tests + deploy on merge)

### Deferred Until It Hurts

| Pattern | Why Not Now |
|---------|-------------|
| Microservices | One process, two developers, two users |
| Container orchestration | One Docker container works |
| Event sourcing / CQRS | Sync protocol is simple enough |
| Multi-region | All users in SF |
| GraphQL | REST + sync covers all current needs |
| Feature flags service | UserDefaults on iOS is fine |

## The Seam

The `/api/v1/` contract is the split point. If Kasane ever needs its own scaling or auth system, pull `db.py`, `v1_api.py`, `v1_models.py` into a separate service. Paul's iOS code doesn't change. Milo's tool switches from local import to HTTP call.

The decision to split happens when:
- Deploy cycle friction (Milo changes break Kasane or vice versa)
- Scaling needs (>100 concurrent sync clients)
- Team growth (separate backend engineers for each surface)

None of these are close.

## Options We've Evaluated

| Option | Pros | Cons | When |
|--------|------|------|------|
| **Mac Mini (current)** | Free, fast iteration, full control | No redundancy, manual deploys | Now |
| **Supabase** | Managed Postgres, realtime, RLS, free tier | Vendor lock-in for realtime features | 5-10 users |
| **Neon** | Serverless Postgres, branching for dev/staging | Less ecosystem than Supabase | Alternative to Supabase |
| **Railway** | Simple deploy from GitHub, $5/mo | Another vendor to manage | If we want managed Docker before Supabase |
| **Fly.io** | Edge deploys, good for low-latency API | More ops complexity than Railway | If latency matters pre-Supabase |
| **AWS (ECS/RDS)** | Enterprise-grade, full control | Expensive, complex, overkill for <100 users | Phase 2+ |

The recommended path: Mac Mini now, Supabase at 5-10 users, re-evaluate at 50+.
