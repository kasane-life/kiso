# Kasane v1 API

REST API for the Kasane iOS app and Milo coaching agent. All endpoints require authentication.

## Base URL

| Environment | URL |
|-------------|-----|
| Local dev | `http://localhost:18800/api/v1` |
| Production (Mac Mini) | `https://auth.mybaseline.health/api/v1` |

## Auth

Pass your API token one of two ways:
```
GET /api/v1/persons?token=YOUR_TOKEN
GET /api/v1/persons
  Authorization: Bearer YOUR_TOKEN
```

## Error Responses

All errors return JSON with a `detail` field:

```json
{
  "detail": "person abc-123 not found"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Invalid request (malformed JSON, missing required fields) |
| 403 | Invalid or missing token |
| 404 | Resource not found (or soft-deleted) |
| 500 | Server error |

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

### First Sync

Pass `lastSyncAt: null` for the initial sync. The server returns all data for the given `personId`. Subsequent syncs should pass the `syncAt` value from the previous response.

## CRUD Endpoints

All request/response bodies use **camelCase** keys. Requests accept camelCase, responses return camelCase.

GET endpoints exclude soft-deleted records (where `deletedAt` is set). To see deleted records, use the sync endpoint.

### Persons

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/persons` | 200 | List all persons |
| GET | `/api/v1/persons/:id` | 200 | Get person by ID |
| POST | `/api/v1/persons` | 201 | Create person |
| PUT | `/api/v1/persons/:id` | 200 | Update person |

### Habits

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/persons/:id/habits` | 200 | List habits for person (ordered by `sortOrder`) |
| POST | `/api/v1/persons/:id/habits` | 201 | Create habit |
| PUT | `/api/v1/habits/:id` | 200 | Update habit |

### Check-ins

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/habits/:id/checkins` | 200 | List check-ins (optional `?since=YYYY-MM-DD`) |
| POST | `/api/v1/habits/:id/checkins` | 201 | Create check-in |

### Focus Plans

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| GET | `/api/v1/persons/:id/focus-plans` | 200 | List focus plans (optional `?limit=N`, default 10) |
| POST | `/api/v1/persons/:id/focus-plans` | 201 | Create focus plan |

### Context (Milo)

```
GET /api/v1/persons/:id/context
```

Returns merged view: person profile + active habits with recent check-ins (last 30 days) + latest focus plan + recent coaching messages + health metrics from CSVs (weight, wearables, labs, meals).

Response shape:
```json
{
  "person": { "...Person..." },
  "activeHabits": [
    {
      "...Habit...",
      "recentCheckins": [ "...CheckIn..." ]
    }
  ],
  "latestFocusPlan": { "...FocusPlan..." },
  "recentMessages": [ "...CheckInMessage..." ],
  "health": {
    "weightRecent": [ { "date": "...", "weightLbs": 192 } ],
    "wearableSnapshot": { "...Garmin/Oura/Whoop data..." },
    "wearableSource": "garmin",
    "latestLabs": { "..." },
    "mealsToday": [ "..." ]
  }
}
```

The `health` field is only present when the person has a `healthEngineUserId` linking them to CSV data.

### Sync-only Entities

The following entities sync via `POST /api/v1/sync` but do not have dedicated CRUD endpoints:

- **check_in_message**: Coaching messages from Milo
- **health_measurement**: Apple Health metrics (stays on-device, synced for backup)
- **workout_record**: Exercise data (stays on-device, synced for backup)

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

### FocusPlan
```json
{
  "id": "uuid",
  "personId": "uuid",
  "generatedAt": "2026-03-23T12:00:00Z",
  "healthSnapshot": "text summary of current health state",
  "reflection": "What's been going well and what's challenging",
  "insight": "Key observation from the data",
  "encouragement": "Motivational note",
  "primaryAction": "Walk 10 min after lunch",
  "primaryAnchor": "right after eating",
  "primaryReasoning": "Movement after meals helps glucose response",
  "primaryCategory": "movement",
  "primaryPurpose": "Improve metabolic health",
  "alternativesJson": "[{\"title\": \"...\", \"reasoning\": \"...\"}]",
  "riskAssessment": null,
  "careTeamNote": null,
  "careTeamSummary": null,
  "careTeamSuggestions": null,
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```

### CheckInMessage
```json
{
  "id": "uuid",
  "personId": "uuid",
  "habitId": "uuid or null",
  "messageText": "Great consistency this week!",
  "messageType": "encouragement",
  "actionType": "suggestion",
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```

### HealthMeasurement (sync-only)
```json
{
  "id": "uuid",
  "personId": "uuid",
  "typeIdentifier": "HKQuantityTypeIdentifierHeartRate",
  "value": 62.0,
  "unit": "count/min",
  "date": "2026-03-23",
  "source": "apple_health",
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```

### WorkoutRecord (sync-only)
```json
{
  "id": "uuid",
  "personId": "uuid",
  "workoutType": "running",
  "duration": 1800.0,
  "calories": 350.0,
  "date": "2026-03-23",
  "source": "apple_health",
  "createdAt": "...",
  "updatedAt": "...",
  "deletedAt": null
}
```
