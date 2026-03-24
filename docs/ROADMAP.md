# Roadmap

Phased cloud adoption tied to user count thresholds. Each phase unlocks when the previous one creates friction.

## Phase 0: Now (0-2 users)

Current: SQLite + CSVs, static API token, Mac Mini Docker.

### Priorities
- [x] Shared data layer (SQLite for Kasane entities)
- [x] Sync protocol (last-write-wins, bidirectional)
- [x] Milo integration (`get_person_context` merges SQLite + CSVs)
- [ ] JWT auth (replace static token with per-user JWTs, token refresh)
- [ ] Litestream backups (stream SQLite WAL to Cloudflare R2)
- [ ] Request ID tracing (correlate sync calls end-to-end)
- [ ] Paul dark-launch: SyncService.swift dual-writing to CloudKit + API

### JWT Auth Plan
- Issue JWTs on login (HMAC-SHA256, 1hr access + 7d refresh)
- Per-user identity in token claims (person_id, user_id)
- Existing static token stays as admin/service key
- iOS sends `Authorization: Bearer <jwt>` on every request

### Litestream Plan
- Single binary, streams WAL changes to R2 in real-time
- Point-in-time restore to any moment
- Cost: ~$0.01/mo for our data volume
- Replaces manual backup anxiety

## Phase 1: Concierge Beta (5-10 users)

Trigger: Paul's SyncService validated, CloudKit disabled, first external users.

### Priorities
- [ ] Supabase migration (swap `db.py` internals, same API contract)
- [ ] Row-level security (users only see their own data)
- [ ] Realtime subscriptions (Paul drops polling, gets push updates)
- [ ] Background job queue (Redis or pg-backed, replaces threadpool)
- [ ] Structured observability (request logs, latency percentiles, error rates)

### Supabase Migration Plan
1. Provision Supabase project (free tier covers <500MB, 50K requests/mo)
2. Port schema from SQLite (TEXT PKs to UUIDs, same column names)
3. Replace `get_db()` in `db.py` with Supabase client
4. Zero changes to `v1_api.py`, `v1_models.py`, iOS, or Milo
5. Run both in parallel for one week, compare data, then cut over

## Phase 2: Growth (50-100 users)

Trigger: paying users, latency SLAs matter, team grows.

### Priorities
- [ ] CDN for API (Cloudflare Workers or edge caching for GETs)
- [ ] Rate limiting per user (not just per IP)
- [ ] Webhooks (notify external systems on data changes)
- [ ] Audit log in Postgres (replace JSONL file)
- [ ] CI/CD pipeline (automated tests + deploy on merge)

## Deferred (until it hurts)

| Pattern | Why Not Now |
|---------|-------------|
| Microservices | One process, two developers, two users |
| Container orchestration | One Docker container works |
| Event sourcing / CQRS | Sync protocol is simple enough |
| Multi-region | All users in SF |
| GraphQL | REST + sync covers all current needs |
| Feature flags service | UserDefaults on iOS is fine |
