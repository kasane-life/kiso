# TOOLS — Health Engine API

You access health data through the health-engine HTTP API running on auth.mybaseline.health.
All tools are GET endpoints. Use web_fetch to call them.

## Authentication

Every request requires: ?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY

## How to Call Tools

Use web_fetch with a GET URL:

web_fetch("https://auth.mybaseline.health/api/checkin?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY")

For user-specific calls, add user_id:

web_fetch("https://auth.mybaseline.health/api/checkin?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul")

For dict/list params, URL-encode a JSON string:

web_fetch("https://auth.mybaseline.health/api/log_habits?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul&habits=%7B%22am_sunlight%22%3A%22y%22%2C%22creatine%22%3A%22y%22%7D")

## Available Tools

### Core — Use These Most

| Tool | Key Params | What it does |
|------|-----------|------|
| checkin | greeting, user_id | Full coaching briefing: scores, insights, weight, nutrition, habits, Garmin data. Call this first. |
| score | user_id | Deep dive: coverage %, NHANES percentiles for 20 metrics, tier breakdown, gap analysis. |
| get_status | user_id | Data inventory: what files exist, last modified, row counts. |
| get_user_profile | user_id | Full profile: intake data, targets, active protocols. |
| get_daily_snapshot | user_id | Live today snapshot: Garmin intraday + meals + calorie balance. Pulls fresh Garmin data (~15s). |

### Logging — Persist User Data

| Tool | Key Params | What it does |
|------|-----------|------|
| log_weight | weight_lbs, date, user_id | Log a weight measurement. |
| log_bp | systolic, diastolic, date, user_id | Log blood pressure. |
| log_meal | description, protein_g, carbs_g, fat_g, calories, date, user_id | Log a meal. Protein required. |
| log_habits | habits (JSON dict), date, user_id | Log daily habits. Pass {"am_sunlight":"y","creatine":"y"}. |
| log_supplements | stack OR supplements, date, user_id | Log supplements. stack="morning" or stack="evening" for predefined stacks. |
| log_sleep | bed_time, wake_time, date, user_id | Log bed/wake times in HH:MM format. |
| log_medication | name, dose, route, notes, date, user_id | Log medication or injection. |
| log_labs | results (JSON dict), date, source, user_id | Log lab results. Names auto-normalize ("Apo B" = "apob"). |
| log_nudge | user_id, nudge_type | Record that a nudge was sent (day1, day3, day7). |

### Workout Programs — Log & Track Against a Plan

| Tool | Key Params | What it does |
|------|-----------|------|
| log_workout | exercises, program_day, rpe, sentiment, energy_level, sleep_quality, notes, user_id | Log a workout against the user's active program. exercises is semicolon-separated: "Back Squat 4x5 @155 RPE 7; RDL 3x8 @135". program_day (1-4) cross-checks adherence. Returns logged exercises + adherence %. |
| get_workout_program | user_id | Get the user's active program with all days and prescribed exercises. Call this to know what they should be doing. |
| get_workout_history | days, user_id | Recent workout sessions with exercises, program adherence, and notes. **Call at session start** to know what they did last time. |

**Workout check-in flow:**
1. User says "just finished Day 2" or describes their workout
2. Call `get_workout_program` to see what Day 2 prescribes
3. Parse their message into the exercises format
4. Call `log_workout` with program_day=2 and the parsed exercises
5. Report back: what they did, adherence, and any coaching notes
6. If they mention sleep, energy, or how they felt, include sentiment/energy_level/sleep_quality

**Exercise format examples:**
- `Back Squat 4x5 @155 RPE 7` — full detail
- `Push-ups 3xmax` — bodyweight
- `Airdyne Intervals 6x30s/90s` — conditioning
- `Leg Curl skipped` — marks as not completed

### Querying

| Tool | Key Params | What it does |
|------|-----------|------|
| get_conversations | user_id, days | Prior conversation history. **Call at session start** to know what you already discussed. Sessions reset often, so this is your only memory of past conversations. |
| get_meals | date, days, user_id | Meals + Garmin burn for a date or range. Shows surplus/deficit. |
| get_labs | user_id | Full lab history: all draws, dates, sources, latest values. |
| get_protocols | user_id | Active protocol progress: day, week, phase, habit completion. |

### Setup & Connections

| Tool | Key Params | What it does |
|------|-----------|------|
| setup_profile | age, sex, weight_target, protein_target, name, goals, medications, conditions, phq9_score, waist_inches, family_history, obstacles, existing_habits, exercise_freq, sleep_hours, sleep_quality, stress_level, alcohol_use, tobacco_use, user_id | Partial profile update. All fields optional (age/sex not required). Call with ONLY the fields the user shared. Rebuilds briefing immediately. |
| connect_garmin | user_id | Check Garmin connection status. |
| pull_garmin | history, workouts, user_id | Pull fresh Garmin data. |
| connect_oura | user_id | Check Oura Ring connection status. |
| pull_oura | history, user_id | Pull fresh Oura Ring data. |
| connect_whoop | user_id | Check WHOOP connection status. |
| pull_whoop | history, user_id | Pull fresh WHOOP data. |
| connect_wearable | service, user_id | Get connection instructions for any wearable. Supports: garmin, oura, whoop (OAuth link), apple_health/apple_watch (iOS Shortcuts guide). |
| import_apple_health | file_path, user_id | Import an Apple Health export ZIP or XML file. |
| ingest_health_snapshot | user_id, metrics (JSON) | Receive a daily health snapshot from an iOS Shortcut. See Apple Health Shortcuts section below. |
| check_health_priorities | user_id | Scan labs and vitals for red flags. Returns flags with severity, coaching messages, and connections to the user's current goal. Call after new lab results arrive. |
| check_engagement | user_id | Check if user engaged after onboarding. Returns nudge recommendations. |
| onboard | user_id | Coverage map + guided setup. All 20 metrics, what is tracked vs missing. |

## Examples

### Morning check-in for Andrew
web_fetch("https://auth.mybaseline.health/api/checkin?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY")

### Log a meal for Paul
web_fetch("https://auth.mybaseline.health/api/log_meal?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul&description=Chicken%20bowl&protein_g=45&calories=650")

### Log weight for Dad
web_fetch("https://auth.mybaseline.health/api/log_weight?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=dad&weight_lbs=185")

### Get Paul's meals for today
web_fetch("https://auth.mybaseline.health/api/get_meals?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul")

### Log labs for a user
web_fetch("https://auth.mybaseline.health/api/log_labs?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul&results=%7B%22apob%22%3A85%2C%22ldl_c%22%3A110%2C%22hdl_c%22%3A55%7D&source=Quest")

### List all available tools
web_fetch("https://auth.mybaseline.health/api/tools?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY")

## Error Handling

- 403: Invalid token. Check the token value.
- 404: Unknown tool name. Check /api/tools for the list.
- 400: Parameter error. Check param types (numbers must be numbers, not strings).
- 500: Tool execution error. The response body has the error message.

## Nutrition Lookups

When someone asks about a food's macros, use web_search to find nutrition facts. Extract the answer from search snippets directly. Do NOT follow up with web_fetch on product pages (they block scrapers). The snippets almost always have what you need.

## Meal Logging Protocol (MANDATORY — NO EXCEPTIONS)

THIS IS THE MOST IMPORTANT SECTION IN THIS FILE.

After EVERY log_meal call, you MUST immediately call get_meals for that date.
Do NOT report totals from your conversation memory. ONLY report what get_meals returns.

### The sequence, every single time:

1. User mentions food → call log_meal to write it to disk
2. IMMEDIATELY call get_meals for today → read back what is ON DISK
3. Report to user:
   - "Logged: [description], [protein]g, [calories] cal"
   - "Day total from log: [X]g protein, [Y] cal" (THIS NUMBER COMES FROM get_meals)
   - "Remaining: [Z]g protein, [W] cal to hit targets"
4. If the numbers from get_meals do not match your memory, get_meals wins. Always.

### Why this exists:

Session restarts wipe your memory. If you track meals in your head and the session
resets, you will give advice based on incomplete data. This caused a user to overeat
by 800 calories because the agent thought meals were missing when they were already
on disk. The agent then re-logged them and gave bad "you need to eat more" advice.

NEVER skip step 2. NEVER report a running total from memory. Disk is truth.

### Nutrition advice rule:

Before giving ANY nutrition advice (what to eat, how much room is left, whether
to eat more), call get_meals FIRST. No exceptions. Even if you just logged a meal
30 seconds ago. Even if you "know" the total. Read from disk.

### Google Calendar

| Tool | Key Params | What it does |
|------|-----------|------|
| calendar_list_events | time_min, time_max, max_results, query, calendar_id | List upcoming events. calendar_id defaults to primary. |
| calendar_create_event | summary, start, end, description, location, calendar_id | Create a calendar event. Times in ISO 8601 (2026-03-22T14:00:00). |
| calendar_search_events | query, time_min, time_max, max_results, calendar_id | Search events by text. |
| connect_google_calendar | user_id | Generate OAuth link for connecting Google Calendar (new users). |

**Calendar IDs for Andrew:**
- `primary` — default calendar (meetings, personal)
- `7f88e5f263e40be2efa23f5bd21482a4dac97e45611be337983b717b8f227b68@group.calendar.google.com` — Health calendar (training, health events)

**Examples:**

List next 5 events:
web_fetch("https://auth.mybaseline.health/api/calendar_list_events?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&max_results=5")

Create a training event on Health calendar:
web_fetch("https://auth.mybaseline.health/api/calendar_create_event?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&summary=Training%3A%20Lower%20%2B%20Pull&start=2026-03-23T15:00:00&end=2026-03-23T16:30:00&calendar_id=7f88e5f263e40be2efa23f5bd21482a4dac97e45611be337983b717b8f227b68%40group.calendar.google.com")

Search for events:
web_fetch("https://auth.mybaseline.health/api/calendar_search_events?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&query=training&max_results=5")


### Coaching / Programs

| Tool | Key Params | What it does |
|------|-----------|------|
| get_skill_ladder | goal_id | Ranked skill ladder for a goal. Returns levels ordered by impact, each with habit, evidence, and diagnostic question. Use during onboarding to find the right starting point. |

Valid goal_ids: sleep-better, less-stress, lose-weight, build-strength, more-energy, sharper-focus, better-mood, eat-healthier.

**Example:**
web_fetch("https://auth.mybaseline.health/api/get_skill_ladder?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&goal_id=sleep-better")

Returns ranked levels with diagnostic questions. Walk the ladder from Level 1: ask the diagnostic question, if they have it handled, move to the next level. First unmastered level = their 14-day program focus. See COACH.md for full onboarding flow.

### Apple Health Shortcuts Bridge (for Apple Watch users)

Apple Watch is fully supported. When a user says they have an Apple Watch:

1. Send them the install link: "Tap this link to add a shortcut that syncs your Apple Watch data to me every morning. When it opens, tap Add Shortcut." Link: https://www.icloud.com/shortcuts/b0c11b2912c1434fad4a2d87f4d2a762
2. After they confirm it installed: "Now open Shortcuts, tap Automation at the bottom, tap +, pick Time of Day, set 7 AM, choose Baseline Health Sync, and turn off Ask Before Running. That's it."
3. "The first time it runs, your phone will ask permission to read your health data. Just tap Allow for everything."

The shortcut reads 9 health metrics (heart rate, HRV, steps, sleep, weight, VO2 max, blood oxygen, calories, respiratory rate) and sends them automatically. All metrics are optional. Whatever the watch tracks, it sends.

**NEVER use technical language with users.** No "API", "JSON", "endpoint", "token", "POST", "HealthKit", or "payload". The user should feel like they're installing a simple phone feature, not configuring software.

### Health Priority Checkpoint

After new lab results arrive (via log_labs), call check_health_priorities to scan for red flags.

**When to call it:** After any log_labs call. Also useful during periodic reviews.

**What it returns:**
- List of flags with severity ("urgent" or "notable")
- Coaching-appropriate messages for each flag
- Goal connections: how each flag relates to the user's current goal
- Suggested coaching response

**10 conditions checked:** pre-diabetic/diabetic glucose, high HbA1c, thyroid (TSH), high blood pressure, low testosterone, high LDL, low vitamin D, high CRP, kidney function (eGFR), low iron (ferritin).

**How to use the results:** Connect findings to the user's current goal when possible. "Your sleep work is even more important given this glucose reading." For urgent flags, always suggest talking to their doctor. Tone: "I noticed" not "WARNING."

**Example:**
web_fetch("https://auth.mybaseline.health/api/check_health_priorities?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&user_id=paul")

### Admin / Diagnostics

| Tool | Key Params | What it does |
|------|-----------|------|
| get_api_stats | days, user_id | API latency stats: p50/p95/max per tool, error rates, timeout counts. |


### Async Tools (Background Jobs)

For slow tools like pull_garmin (60-120s), use the _async suffix to run in background:

**Start a background pull:**
web_fetch("https://auth.mybaseline.health/api/pull_garmin_async?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY")

Returns immediately: {"job_id": "pull_garmin_1234567890", "status": "running"}

**Check job status:**
web_fetch("https://auth.mybaseline.health/api/job_status?token=NZCT4pzvxC36OSaCztUYjq2_LAkqdC5_LmTFysa9VAY&job_id=pull_garmin_1234567890")

Returns: {"status": "running"} or {"status": "completed", "result": {...}} or {"status": "error", "error": "..."}

Any tool can be called async by adding _async suffix. Use for pull_garmin and other slow operations.
