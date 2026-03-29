# Garmin Pull Refactor: 210 Calls → 3 Calls

**Date:** 2026-03-29
**Status:** Approved
**Problem:** Garmin pull makes ~210 individual API calls (30 days x 7 metrics), takes 90+ seconds, and returns only averages. Throws away daily detail.
**Solution:** Pull today's data in 3 calls. Aggregate history locally from cached daily series.

---

## Current Architecture (broken)

```
pull_garmin()
  ├── pull_resting_hr()     → 30 API calls (0.3s sleep each) → returns 1 average
  ├── pull_steps()           → 30 API calls → returns 1 average
  ├── pull_sleep_duration()  → 30 API calls → returns 1 average
  ├── pull_sleep_regularity()→ 30 API calls → returns 1 stddev
  ├── pull_hrv()             → 30 API calls → returns 1 average
  ├── pull_vo2_max()         → 7 API calls  → returns 1 value
  └── pull_zone2_minutes()   → 7+ API calls → returns 1 sum

Total: ~210 API calls, ~90 seconds minimum, returns 7 numbers
```

### Problems
1. **Slow.** 90+ seconds per pull. Milo times out. Cron jobs stack.
2. **Wasteful.** 210 calls to Garmin's API. Rate limits hit (429s observed).
3. **Lossy.** Daily detail is fetched then averaged away. We can't answer "what were my steps yesterday?"
4. **Fragile.** Any single failed call in the loop can silently skew the average.
5. **Stale.** get_stats returns today's partial data mixed with yesterday's complete data.

## New Architecture

```
pull_garmin(date=today)
  ├── get_stats(date)         → 1 API call → steps, RHR, calories, distance, floors, stress
  ├── get_sleep_data(date)    → 1 API call → sleep duration, stages (deep/light/REM/awake)
  └── get_hrv_data(date)      → 1 API call → lastNightAvg, weeklyAvg, status

Total: 3 API calls, <3 seconds, returns full daily snapshot
```

### Storage

**garmin_daily.json** (existing file, append today's entry):
```json
[
  {
    "date": "2026-03-29",
    "steps": 287,
    "rhr": 50,
    "hrv": 57,
    "hrv_weekly_avg": 69,
    "hrv_status": "BALANCED",
    "sleep_hrs": 5.0,
    "deep_sleep_hrs": 0.9,
    "light_sleep_hrs": null,
    "rem_sleep_hrs": 0.0,
    "awake_hrs": null,
    "calories_total": 621,
    "calories_active": 30,
    "stress_avg": 32,
    "floors": 0,
    "distance_m": null,
    "max_hr": null
  }
]
```

**garmin_latest.json** (overwrite with today's snapshot + computed averages):
```json
{
  "last_updated": "2026-03-29T08:00:00",
  "date": "2026-03-29",
  "resting_hr": 50,
  "daily_steps_avg": 11030,
  "sleep_duration_avg": 6.3,
  "sleep_regularity_stddev": null,
  "vo2_max": 47,
  "hrv_rmssd_avg": 69,
  "zone2_min_per_week": null,
  "today": {
    "steps": 287,
    "rhr": 50,
    "hrv_last_night": 57,
    "sleep_hrs": 5.0,
    "calories_total": 621,
    "calories_active": 30,
    "stress_avg": 32
  }
}
```

### Aggregation (local, no API calls)

Averages computed from `garmin_daily.json`:
- **7-day avg:** last 7 entries with non-null values
- **30-day avg:** last 30 entries with non-null values
- **Sleep regularity:** stddev of sleep start times from sleep data (if stored)
- **Zone 2:** sum from `garmin_workouts.json` or daily burn data (weekly)
- **VO2 Max:** latest non-null value (doesn't change daily)

These feed directly into the horizons in `build_briefing()`.

### History Backfill

For the initial migration or when `garmin_daily.json` is missing/sparse:
```
pull_garmin(backfill=True, days=90)
```
Uses the existing daily series pull (which is already a bulk endpoint) to populate history. Run once, then daily pulls only need 3 calls.

## API Endpoints Used

| Garmin Endpoint | What It Returns | Calls |
|---|---|---|
| `get_stats(date)` | Steps, RHR, calories, distance, floors, stress, min/max HR | 1 |
| `get_sleep_data(date)` | Sleep duration, stage breakdown (deep/light/REM/awake) | 1 |
| `get_hrv_data(date)` | Last night HRV avg, weekly avg, status | 1 |

VO2 Max and Zone 2 are not daily metrics:
- **VO2 Max:** Changes weekly at most. Read from `get_stats()` if available, otherwise from most recent `garmin_daily.json` entry.
- **Zone 2:** Computed from workout data. Pull workouts separately on a less frequent cadence (weekly or on-demand).

## Migration Plan

1. **Add `_pull_today(date)` method** to GarminClient. Returns a dict with all daily metrics from 3 API calls.
2. **Add `_append_daily(data)` method** that appends to `garmin_daily.json`, deduplicating by date.
3. **Add `_compute_averages(days)` method** that reads `garmin_daily.json` and computes 7d/30d stats locally.
4. **Refactor `pull_all()`** to call `_pull_today()` + `_append_daily()` + `_compute_averages()`. Keep writing `garmin_latest.json` in the same format so nothing downstream breaks.
5. **Keep `history=True` flag** for backfill, but use bulk endpoint instead of per-day loops.
6. **Delete** the individual `pull_resting_hr()`, `pull_steps()`, etc. methods. They become dead code.
7. **Update cron** from every 4 hours to every 2 hours (now that each pull takes 3 seconds, not 90).

## What Doesn't Change

- `garmin_latest.json` schema stays the same (scoring engine reads it)
- `garmin_daily.json` schema stays the same (horizons read it)
- `build_briefing()` reads the same files
- MCP tool interface (`pull_garmin`) stays the same
- Sleep regularity stddev computation stays the same (just reads from local data instead of making 30 API calls)

## Effort

~2 hours. Most of the code already exists in the individual pull methods. This is consolidation and deletion, not new functionality.
