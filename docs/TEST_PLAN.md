# Kiso Test Plan

What to test before deploying changes to Mac Mini. Run this after any v1 API changes.

## Automated Tests (425 passing)

```bash
python3 -m pytest tests/ -v
```

### v1 API Coverage (26 tests in `tests/test_v1_api.py`)

| Area | Tests | What they verify |
|------|-------|-----------------|
| Person CRUD | 5 | Create, get, list, update, 404, auth required |
| Habit CRUD | 2 | Create+list, update |
| Check-in CRUD | 2 | Create+list, since filter |
| Focus Plan | 1 | Create+list |
| Sync | 4 | Push+pull, conflict resolution (server wins), soft delete, multi-entity |
| Context | 2 | Merged data (person+habits+focus plan), 404 |
| Bearer auth | 1 | Authorization header works |
| Per-user tokens | 8 | Token auth, cross-person blocking, list filtering, admin sees all, habit isolation, sync isolation, context isolation, invalid token rejected |
| Audit logging | 1 | Sync writes audit entry with correct fields |

### Other Test Suites

- `test_briefing.py` (30 tests): Coaching briefing assembly
- `test_scoring.py` (60+ tests): NHANES percentiles, clinical zones
- `test_insights.py` (30+ tests): Pattern detection, rules engine
- `test_garmin.py`, `test_oura.py`, `test_whoop.py`: Wearable integrations
- `test_tools.py` (70+ tests): MCP tool implementations
- `test_token_store.py`: Encrypted token storage

## Manual Smoke Test (Before Deploying)

Run locally, verify each step works:

```bash
# 1. Start the server
python3 -m uvicorn engine.gateway.server:app --port 18800

# 2. Create a person
curl -s http://localhost:18800/api/v1/persons?token=YOUR_TOKEN \
  -X POST -H 'Content-Type: application/json' \
  -d '{"name": "Smoke Test", "biologicalSex": "M"}' | python3 -m json.tool

# 3. Verify camelCase in response (biologicalSex, not biological_sex)

# 4. Sync a habit
curl -s http://localhost:18800/api/v1/sync?token=YOUR_TOKEN \
  -X POST -H 'Content-Type: application/json' \
  -d '{
    "deviceId": "smoke-test",
    "personId": "PERSON_ID_FROM_STEP_2",
    "lastSyncAt": null,
    "changes": [{
      "entity": "habit",
      "id": "smoke-habit-1",
      "action": "upsert",
      "data": {"person_id": "PERSON_ID", "title": "Smoke test habit"},
      "updatedAt": "2026-03-23T20:00:00+00:00"
    }]
  }' | python3 -m json.tool

# 5. Verify context endpoint
curl -s http://localhost:18800/api/v1/persons/PERSON_ID/context?token=YOUR_TOKEN | python3 -m json.tool

# 6. Check audit log was written
cat data/admin/api_audit.jsonl
```

## Per-User Token Smoke Test

After configuring `gateway.yaml` with `token_persons`:

```yaml
token_persons:
  tok_paul:
    - paul-person-uuid
  tok_andrew:
    - andrew-deal-001
```

```bash
# Paul's token can access Paul's data
curl -s http://localhost:18800/api/v1/persons/paul-person-uuid?token=tok_paul
# Should return 200

# Paul's token CANNOT access Andrew's data
curl -s http://localhost:18800/api/v1/persons/andrew-deal-001?token=tok_paul
# Should return 403

# Admin token can access everything
curl -s http://localhost:18800/api/v1/persons?token=YOUR_ADMIN_TOKEN
# Should return all persons
```

## What to Verify on Mac Mini After Deploy

1. `docker logs health-engine-gateway` shows clean startup
2. `curl https://auth.mybaseline.health/api/v1/persons?token=...` returns Andrew's person
3. Context endpoint returns merged SQLite + CSV data
4. Audit log at `data/admin/api_audit.jsonl` is being written
5. MCP tools still work (Milo can call `checkin()`)
