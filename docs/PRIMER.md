# Kiso Primer for Paul

Everything you need to build SyncService.swift and understand how the pieces fit together.

## The Three Pieces

**Kasane** (iOS app) is the user-facing product. Habits, check-ins, focus plans, the Today view. You own this.

**Kiso** (backend, this repo) is the shared data layer. One FastAPI process on Mac Mini serving both Kasane and Milo. SQLite for entities, CSVs for health metrics. You talk to it via REST at `/api/v1/`.

**Milo** (coaching agent) is the AI coach. Runs on the same Mac Mini via OpenClaw. Reads and writes the same data Kasane does, through Kiso's MCP tools. When Milo writes a coaching message, Kasane picks it up on next sync. When Kasane writes a check-in, Milo sees it in the person context.

```
Kasane (iOS) ----REST----> Kiso (/api/v1/) <----MCP---- Milo (OpenClaw)
                              |
                         SQLite + CSVs
```

Neither client knows about the other. The API is the meeting point.

## Proposed Kasane Changes

To connect Kasane to Kiso, the iOS app needs a sync service that talks to the REST API. Here's how we see it phasing in:

**SyncService.swift**: A new service that syncs CoreData entities to `/api/v1/sync`. Replaces CloudKit as the sync layer.

**Phase 1**: Dual-write. CoreData + CloudKit stays. SyncService also pushes to Kiso. Compare results, build confidence.

**Phase 2**: Disable CloudKit sync. Kasane runs fully on Kiso.

## API Quick Reference

Full docs: [API.md](API.md)

**Base URL**: `https://auth.mybaseline.health/api/v1` (prod), `http://localhost:18800/api/v1` (dev). The domain is on Andrew's Cloudflare tunnel and may move to a Kasane-branded domain later. The API contract stays the same either way.

**Auth**: `Authorization: Bearer YOUR_TOKEN` or `?token=YOUR_TOKEN`

Each device gets its own token mapped to specific person IDs in `gateway.yaml`. Your token can only read/write the persons assigned to it. Andrew's admin token can access everything. See [DATA_POLICY.md](DATA_POLICY.md) for the full access control model.

**Primary endpoint**:
```
POST /api/v1/sync
{
  "deviceId": "pauls-iphone-15",
  "personId": "uuid",
  "lastSyncAt": "2026-03-23T12:00:00Z",   // null for first sync
  "changes": [
    {
      "entity": "habit",
      "id": "habit-uuid",
      "action": "upsert",
      "data": { "personId": "...", "title": "Morning walk", "category": "movement" },
      "updatedAt": "2026-03-23T12:01:00Z"
    }
  ]
}
```

Response:
```json
{
  "serverChanges": [ { "entity": "...", "id": "...", "action": "upsert", "data": {...}, "updatedAt": "..." } ],
  "syncAt": "2026-03-23T12:01:05Z",
  "stats": { "pushed": 1, "pulled": 3 }
}
```

**Conflict resolution**: Last-write-wins by `updatedAt`. Server keeps the newer version.

**All JSON is camelCase.** Request and response. Matches iOS conventions.

**Entities**: person, habit, check_in, check_in_message, focus_plan, health_measurement, workout_record.

**CRUD also available** (see API.md) but sync is the primary interface for the app.

## Data Model Mapping

Your CoreData entities map 1:1 to Kiso's SQLite:

| iOS CoreData | Kiso entity | Notes |
|---|---|---|
| CDPerson | person | `healthEngineUserId` links to CSV health data |
| CDHabit | habit | state: active/graduated/seasonal |
| CDCheckIn | check_in | completed is boolean |
| CDCheckInMessage | check_in_message | Milo writes these, Kasane reads |
| CDFocusPlan | focus_plan | 20+ fields, see API.md |
| CDHealthMeasurement | health_measurement | Sync-only, no CRUD endpoints |
| CDWorkoutRecord | workout_record | Sync-only, no CRUD endpoints |

All entities have: `id`, `createdAt`, `updatedAt`, `deletedAt` (null unless soft-deleted).

## Where to Put SyncService.swift

Based on the existing codebase structure:

```
Habica/Services/SyncService.swift    <-- new
Habica/Services/APIConfig.swift      <-- add Kiso base URL + token
Habica/Models/SyncModels.swift       <-- Codable structs for request/response
```

The existing `AIService.swift` has URLSession patterns you can follow (lines 692-763).

`CoreDataStack.swift` stays as-is. SyncService reads changes from CoreData, pushes to Kiso, and merges server changes back.

## Context Endpoint (Milo's Read)

```
GET /api/v1/persons/:id/context
```

Returns: person profile + active habits with recent check-ins (30 days) + latest focus plan + coaching messages + CSV health data (weight, wearables, labs, meals).

This is what Milo reads before coaching. If you want to see what Milo sees, hit this endpoint.

## Error Format

All errors return:
```json
{ "detail": "person abc-123 not found" }
```

Status codes: 400 (bad request), 403 (auth failed), 404 (not found), 500 (server error).

## What's Deployed

Mac Mini (M4 Pro), Docker container, port 18800, Cloudflare Tunnel for HTTPS.

Andrew's person already seeded: `andrew-deal-001`, `healthEngineUserId=default`.

425 tests passing. Sync protocol, per-user token isolation, and audit logging all tested.

## What's Already Done

- Per-user API tokens with person-level access control
- v1 audit logging (every API call logged with user, endpoint, latency)
- camelCase everywhere (sync, CRUD, context all consistent)
- Soft deletes propagated through sync

## What's Next

1. JWT auth (HMAC-SHA256, 1hr access + 7d refresh) to replace static tokens
2. Litestream backups (SQLite WAL to Cloudflare R2)
3. Supabase migration at 5-10 users (swap db.py internals, API contract stays the same)

Full roadmap: [ROADMAP.md](ROADMAP.md)

## Key Files in This Repo

| File | What it does |
|------|-------------|
| `docs/API.md` | Full v1 API contract (your main reference) |
| `docs/ARCHITECTURE.md` | System design, storage, deploy model |
| `docs/MILO.md` | How Milo integrates, Mac Mini setup, cloud plan |
| `docs/DATA_POLICY.md` | Access control, encryption, audit trail, security roadmap |
| `docs/TEST_PLAN.md` | Automated + manual smoke test procedures |
| `docs/ROADMAP.md` | Cloud progression phases with triggers |
| `engine/gateway/v1_api.py` | Route implementations |
| `engine/gateway/v1_models.py` | Pydantic models (camelCase aliases) |
| `engine/gateway/db.py` | SQLite schema + connection management |
| `tests/test_v1_api.py` | 26 tests: sync, CRUD, token isolation, audit |

## Testing Locally

```bash
cd ~/src/health-engine
python3 -m pip install -e .
python3 -m pytest tests/test_v1_api.py -v    # 26 tests

# Start the gateway locally
python3 -m uvicorn engine.gateway.server:app --port 18800

# Hit it
curl http://localhost:18800/api/v1/persons?token=YOUR_TOKEN
```

## Questions?

Text Andrew or Milo. Milo has full read access to all of this.
