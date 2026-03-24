---
date: 2026-03-23
status: archived
---

# Shared Data Layer: API Proposal

> **ARCHIVED**: This proposal has been implemented. See [API.md](API.md) for the live API contract and [ARCHITECTURE.md](ARCHITECTURE.md) for system design.

## Goal

Replace CloudKit sync with API endpoints on the health-engine gateway (port 18800) so both Kasane (iOS) and Milo (OpenClaw agent) read/write the same person profiles, habits, and check-ins.

## Principles

1. **CoreData stays as the local store on iOS.** The widget depends on it. We're only replacing the sync layer, not the persistence layer.
2. **Offline-first.** Kasane works without internet. Syncs when available.
3. **Health measurements stay local.** HealthKit data, workouts, and documents never leave the device (Paul's intentional PHI boundary).
4. **Person-centric.** Everything hangs off a person ID. One person = one profile + habits + check-ins + focus plans.
5. **Milo gets read/write access.** When Milo learns something, it writes to the same profile Kasane reads.

## What Syncs (mirrors CloudKit scope)

| Entity | Kasane Writes | Milo Writes | Both Read |
|--------|:---:|:---:|:---:|
| Person (profile, health context) | Yes | Yes | Yes |
| Habit (title, state, category, anchor) | Yes | Yes | Yes |
| CheckIn (daily completion) | Yes | Yes | Yes |
| CheckInMessage (coaching messages) | Yes | Yes | Yes |
| FocusPlan (AI recommendations) | Yes | Yes | Yes |
| Connection (family sharing) | Yes | No | Yes |

## What Stays Local (does NOT sync)

- HealthMeasurement (Apple Health data)
- WorkoutRecord (exercise data)
- HealthDocument (imported PDFs, OCR results)

## API Design

Base: `https://<gateway>:18800/api/v1`

Auth: API token in `x-api-token` header (same pattern as existing gateway). Per-user auth via `person_id` tied to Apple Sign-In identifier.

### Person

```
GET    /persons/:id                    → person profile
PUT    /persons/:id                    → update profile
POST   /persons                        → create person
DELETE /persons/:id                    → delete person + all data
```

Person payload:
```json
{
  "id": "uuid",
  "name": "Andrew",
  "date_of_birth": "1990-04-15",
  "biological_sex": "male",
  "health_notes": "markdown text with labs, conditions, etc.",
  "conditions": ["sleep apnea", "elevated ApoB"],
  "medications": ["finasteride 1mg"],
  "family_history": [{"condition": "heart disease", "relation": "father"}],
  "selected_outcomes": ["sleep", "body_composition"],
  "health_obstacles": "shift work, travel",
  "apple_user_identifier": "001234.abc...",
  "created_at": "2026-03-23T00:00:00Z",
  "updated_at": "2026-03-23T12:00:00Z"
}
```

### Habits

```
GET    /persons/:id/habits             → all habits for person
POST   /persons/:id/habits             → create habit
PUT    /habits/:id                     → update habit
DELETE /habits/:id                     → delete habit
```

Habit payload:
```json
{
  "id": "uuid",
  "person_id": "uuid",
  "title": "Morning fiber (chia + flax)",
  "purpose": "Lower ApoB through soluble fiber",
  "emoji": "🌱",
  "category": "nutrition",
  "state": "forming",
  "anchor": "right after morning coffee",
  "identity_threshold": 21,
  "graduated_at": null,
  "show_in_today": true,
  "sort_order": 0,
  "created_at": "2026-03-23T00:00:00Z",
  "updated_at": "2026-03-23T12:00:00Z"
}
```

### Check-Ins

```
GET    /habits/:id/checkins            → check-ins for habit
GET    /persons/:id/checkins?date=YYYY-MM-DD  → all check-ins for person on date
POST   /habits/:id/checkins            → create check-in
PUT    /checkins/:id                   → update check-in
```

CheckIn payload:
```json
{
  "id": "uuid",
  "habit_id": "uuid",
  "date": "2026-03-23",
  "completed": true,
  "note": "walked 20 min after dinner, felt good",
  "created_at": "2026-03-23T00:00:00Z"
}
```

### Focus Plans

```
GET    /persons/:id/focus_plans        → all focus plans (newest first)
POST   /persons/:id/focus_plans        → create focus plan
GET    /focus_plans/:id                → single focus plan
```

FocusPlan payload:
```json
{
  "id": "uuid",
  "person_id": "uuid",
  "health_snapshot": "text",
  "reflection": "text",
  "insight": "text",
  "primary_action": "Add 5g chia seeds to morning smoothie",
  "primary_anchor": "right after waking",
  "primary_reasoning": "Soluble fiber shown to reduce LDL...",
  "primary_category": "nutrition",
  "primary_purpose": "Lower ApoB",
  "alternatives": [{"title": "...", "reasoning": "..."}],
  "encouragement": "text",
  "risk_assessment": "text",
  "care_team_topic": "ApoB trending down",
  "care_team_specialist": "Primary care",
  "suggested_habits": [{"title": "...", "category": "...", "purpose": "..."}],
  "generated_at": "2026-03-23T00:00:00Z"
}
```

### Coaching Messages

```
GET    /persons/:id/messages           → coaching messages
POST   /persons/:id/messages           → create message
```

### Connections (Family)

```
POST   /connections/invite             → generate invite code
POST   /connections/accept             → accept invite code
GET    /persons/:id/connections        → list connections
DELETE /connections/:id                → remove connection
```

### Sync Endpoint (Batch)

For efficient iOS sync, a single batch endpoint:

```
POST   /sync
Body: {
  "person_id": "uuid",
  "last_sync": "2026-03-23T00:00:00Z",
  "changes": {
    "persons": [...updated/created],
    "habits": [...],
    "checkins": [...],
    "messages": [...]
  }
}

Response: {
  "server_changes": {
    "persons": [...changes since last_sync],
    "habits": [...],
    "checkins": [...],
    "focus_plans": [...],
    "messages": [...]
  },
  "sync_token": "2026-03-23T12:00:00Z"
}
```

This lets the iOS app do a single request on foreground/background to stay in sync, rather than polling individual endpoints.

## Storage

SQLite on the Mac Mini (alongside existing health-engine data). One database file at `data/kasane.db`.

Tables mirror the entities above. Foreign keys enforce relationships. Timestamps for sync (created_at, updated_at, deleted_at for soft deletes).

## Migration Path

1. **Phase 1 (now):** Build API + SQLite store. Milo starts reading/writing.
2. **Phase 2 (with Paul):** Add sync service to Kasane iOS. CoreData stays as local store. New `SyncService.swift` replaces CloudKit sync.
3. **Phase 3:** Cut CloudKit. Kasane runs fully on the shared API.

## What Milo Gets

Once the API exists, Milo's health-engine tools can:
- Read a person's habits and check-in history before coaching
- Write coaching messages that show up in the Kasane app
- Create suggested habits that appear in the app's "Suggestions" section
- Read focus plans to stay aligned with the app's recommendations
- Know when someone last checked in (engagement signal)

## Open Questions

- **Where does this run in production?** Mac Mini for now. AWS/Railway/Fly when needed.
- **Auth model:** API token for Milo, Apple Sign-In JWT validation for Kasane? Or same token for both?
- **Conflict resolution:** Last-write-wins (like CloudKit's current merge policy)? Or something smarter?
- **Encryption at rest:** SQLite encryption for PHI? Or rely on disk-level encryption?
