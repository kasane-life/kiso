# Wearable Data Availability

What each wearable can provide, organized by time scale.

## Garmin (Built)

### Real-Time (current day, every sync ~15 min)
- Steps so far
- Calories burned (total + active + BMR)
- Body Battery (current charge level)
- Stress level (average + current)
- Heart rate (last reading)
- Intensity minutes

### Daily Summary (yesterday, full 24hr cycle)
- Total calorie burn (BMR + active + total) via `garmin_daily_burn.json`
- Resting heart rate
- HRV RMSSD (last night)
- Sleep duration + bedtime/waketime
- Step count

### Weekly/Monthly Trends (7-90 day averages)
- RHR trend (30-day)
- HRV trend (7-day)
- Sleep duration avg (30-day)
- Sleep regularity / bedtime stdev (30-day)
- Steps avg (30-day)
- VO2 max (latest estimate)
- Zone 2 minutes/week (7-day)

### Workout Detail
- Activity type, duration, calories, avg HR
- Strength sets: exercise, weight, reps, RPE
- Zone breakdown per activity

### Not Yet Wrapped (available in garminconnect package)
- Training readiness / morning readiness score
- Body composition (if Garmin scale)
- SpO2 trends
- Respiration rate
- Blood pressure (manual entry)
- Endurance score, hill score, lactate threshold

## Oura (Future)
- Sleep stages (deep/REM/light), sleep score
- Readiness score
- HRV (overnight, higher quality than wrist)
- Body temperature deviation
- Activity + steps
- SpO2

## Whoop (Future)
- Strain score (daily/weekly)
- Recovery score (0-100)
- HRV (overnight)
- Sleep performance + sleep need
- Respiratory rate
- Skin temperature

## Apple Health (Built, import only)
- RHR, HRV (SDNN), steps, VO2 max, sleep
- Requires manual export ZIP from iPhone
- No real-time pull

## Data Flow

```
Garmin Connect  --(garminconnect pkg)-->  GarminClient.pull_today()   --> garmin_today.json    (live intraday)
                                          GarminClient.pull_all()     --> garmin_latest.json    (30-day avgs)
                                                                      --> garmin_daily_burn.json (7-day burns)
                                                                      --> garmin_daily.json     (90-day series)
                                          GarminClient.pull_workouts()--> garmin_workouts.json  (workout sets)

Apple Health ZIP --(SAX parser)--------->  apple_health_latest.json   (same schema as garmin_latest)
```

## Auth

- **Garmin**: OAuth via gateway at `auth.mybaseline.health`. Tokens stored at `~/.config/health-engine/tokens/garmin/<user_id>/` (legacy path, kept for backward compat). Legacy CLI path: `~/.config/health-engine/garmin-tokens/`.
- **Apple Health**: No auth needed (manual ZIP export).
- **Oura**: Personal Access Token (PAT). No gateway needed.
- **Whoop**: OAuth 2.0 (would use same gateway pattern as Garmin).
