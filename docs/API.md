# Kasane v1 API

REST API for the Kasane iOS app. All endpoints require authentication.

## Auth

Pass your API token one of two ways:
```
GET /api/v1/persons?token=YOUR_TOKEN
GET /api/v1/persons
  Authorization: Bearer YOUR_TOKEN
```

## Sync

The primary endpoint. Client pushes local changes, pulls server changes.

```
POST /api/v1/sync
```

### Request
```json
{
  "deviceId": "pauls-iphone-15",
  "personId": "uuid-of-person",
  "lastSyncAt": "2026-03-23T12:00:00Z",
  "changes": [
    {
      "entity": "habit",
      "id": "habit-uuid",
      "action": "upsert",
      "data": {
        "personId": "uuid-of-person",
        "title": "Morning walk",
        "category": "movement"
      },
      "updatedAt": "2026-03-23T12:01:00Z"
    }
  ]
}
```

### Response
```json
{
  "serverChanges": [
    {
      "entity": "check_in_message",
      "id": "msg-uuid",
      "action": "upsert",
      "data": { "...": "..." },
      "updatedAt": "2026-03-23T12:01:05Z"
    }
  ],
  "syncAt": "2026-03-23T12:01:05Z",
  "stats": { "pushed": 1, "pulled": 3 }
}
```

### Conflict Resolution

Last-write-wins by `updatedAt`. If the server has a newer `updatedAt` for a record, the client's change is silently dropped. The client will receive the server's version in `serverChanges`.

### Entities

Valid `entity` values: `person`, `habit`, `check_in`, `check_in_message`, `focus_plan`, `health_measurement`, `workout_record`.

### Actions

- `upsert`: Create or update. Data fields are merged (only provided fields are updated).
- `delete`: Soft delete. Sets `deletedAt` timestamp. Record remains queryable via sync but excluded from CRUD GETs.

## CRUD Endpoints

All request/response bodies use **camelCase** keys.

### Persons

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/persons` | List all persons |
| GET | `/api/v1/persons/:id` | Get person by ID |
| POST | `/api/v1/persons` | Create person |
| PUT | `/api/v1/persons/:id` | Update person |

### Habits

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/persons/:id/habits` | List habits for person |
| POST | `/api/v1/persons/:id/habits` | Create habit |
| PUT | `/api/v1/habits/:id` | Update habit |

### Check-ins

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/habits/:id/checkins` | List check-ins (optional `?since=YYYY-MM-DD`) |
| POST | `/api/v1/habits/:id/checkins` | Create check-in |

### Focus Plans

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/persons/:id/focus-plans` | List focus plans (optional `?limit=N`, default 10) |
| POST | `/api/v1/persons/:id/focus-plans` | Create focus plan |

### Context (Milo)

```
GET /api/v1/persons/:id/context
```

Returns merged view: person profile + active habits with recent check-ins + latest focus plan + health metrics from CSVs (weight, wearables, labs, meals). This is what Milo reads for full coaching context.

## Data Types

### Person
```json
{
  "id": "uuid",
  "name": "Andrew Deal",
  "relationship": "self",
  "dateOfBirth": "1991-01-01",
  "biologicalSex": "M",
  "conditionsJson": "[]",
  "medications": null,
  "familyHistoryJson": "[]",
  "healthNotes": null,
  "healthEngineUserId": "default",
  "createdAt": "2026-03-23T12:00:00Z",
  "updatedAt": "2026-03-23T12:00:00Z",
  "deletedAt": null
}
```

### Habit
```json
{
  "id": "uuid",
  "personId": "uuid",
  "title": "Morning walk",
  "purpose": "Start day with movement",
  "category": "movement",
  "emoji": "🚶",
  "anchor": "After coffee",
  "state": "active",
  "sortOrder": 0,
  "identityThreshold": null,
  "graduatedAt": null,
  "showInToday": true,
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```

### CheckIn
```json
{
  "id": "uuid",
  "habitId": "uuid",
  "date": "2026-03-23",
  "completed": true,
  "note": "30 min around the block",
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```
