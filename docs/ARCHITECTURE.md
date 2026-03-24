# Architecture

Kiso is a monolithic Python backend serving three clients from one process.

## Clients

| Client | Surface | Protocol |
|--------|---------|----------|
| **Kasane** (iOS app) | `/api/v1/*` | REST + sync |
| **Milo** (coaching agent) | MCP tools + `/api/{tool_name}` | MCP / HTTP GET |
| **iOS Shortcuts** | `/api/{tool_name}` | HTTP GET |

## Storage: Two Systems, One Bridge

**SQLite** (`data/kasane.db`) holds relational Kasane entities:
- Persons, habits, check-ins, focus plans, messages, health measurements, workout records
- Synced bidirectionally with the iOS app via `/api/v1/sync`
- WAL mode for concurrent reads

**CSVs** (`data/` flat files) hold health tracking data:
- Weight, meals, labs, blood pressure, supplements, medications, habits
- Written by Milo's 40+ MCP tools
- Per-user directories at `data/users/<user_id>/`

**The bridge**: `person.health_engine_user_id` links a SQLite person to a CSV user directory. The `get_person_context` tool reads both in one call.

## Request Flow

```
iOS App
  |
  v
/api/v1/sync ──> SQLite (kasane.db)
  |
  |  (health_engine_user_id)
  v
/api/v1/persons/:id/context ──> SQLite + CSVs merged
  ^
  |
Milo (get_person_context tool)
  |
  v
/api/{tool_name} ──> CSVs (weight, meals, labs...)
  ^
  |
iOS Shortcuts
```

## Auth

Single API token (gateway.yaml) accepted via:
- Query param: `?token=X`
- Header: `Authorization: Bearer X`

Wearable OAuth uses HMAC-signed links with PKCE (Google Calendar) or credential forms (Garmin).

Tokens encrypted at rest via Fernet (AES-128-CBC + HMAC).

## Deploy Model

One Docker container on Mac Mini (M4 Pro):
- FastAPI + Uvicorn on port 18800
- Cloudflare Tunnel for HTTPS (`auth.mybaseline.health`)
- Data volume: `data/` (CSVs + SQLite + audit logs)
- Config: `~/.config/health-engine/gateway.yaml`

Agent workspace (Milo) deployed separately via `deploy-coach.sh` to `~/.openclaw/workspace/`.

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
tests/             400+ tests
docs/              This directory
```

## Migration to Supabase

When ready, swap SQLite for Postgres in `db.py`:
1. Replace `sqlite3` with `asyncpg` or Supabase client
2. Port schema (TEXT PKs become UUIDs, same column names)
3. Zero changes to v1_api.py, v1_models.py, iOS app, or Milo tools

The `/api/v1/` contract is the constant.
