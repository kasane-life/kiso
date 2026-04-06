# HEARTBEAT — Critical Alert Monitoring

## Message Sending Rule

Every `message` tool call MUST include `buttons=[]`. The tool validation requires this field even when no buttons are needed. Omitting it causes a silent validation error and the message will not be sent.

```
message(action="send", channel=user.channel, target=user.channel_target, message=..., buttons=[])
```

## Multi-User Loop

All checks loop over every user in `users.yaml`.
Pass `user_id` to every tool call. Never mix data between users.

Each user entry includes:
- `channel`: "whatsapp" or "telegram"
- `channel_target`: phone number for WhatsApp, numeric user ID for Telegram
- `timezone`: IANA timezone string (e.g. "America/Los_Angeles", "Europe/Moscow")

```
for each user in users.yaml:
    user_id = user.user_id
    name = user.name
    channel = user.channel
    target = user.channel_target
    tz = user.timezone
```

## Heartbeat (Every 30 Minutes)

Silent check per user. No message unless action needed.

```
for each user in users.yaml:
    # Check if user is in quiet hours (9:15 PM - 6:00 AM in THEIR timezone)
    if user_local_time(user.timezone) is in quiet hours: skip

    call checkin(user_id=user.user_id)
    read coaching_signals

    if any signal.severity == "critical":
        send message(channel=user.channel, target=user.channel_target, message=..., buttons=[])

    if 2+ signals.severity == "warning":
        send message(channel=user.channel, target=user.channel_target, message=..., buttons=[])

    else:
        log to daily memory, surface at next scheduled check-in
```

### Critical Triggers (Interrupt Immediately)

| Signal | Condition | Message Style |
|---|---|---|
| HRV collapse | HRV <50ms | "HRV dropped to [X]. Skip today's session, prioritize sleep tonight." |
| RHR spike | RHR >55 during deficit | "RHR at [X] — your body's flagging the deficit. Consider a maintenance day." |
| Sleep crisis | Debt >7hr over 7 days | "You're running on fumes — [X]hr debt this week. Tonight is non-negotiable: bed by 10." |
| Deficit unsustainable | Loss >2lb/wk + HRV <55 | "Losing too fast with HRV at [X]. Back off to maintenance calories today." |

### Warning Triggers (Hold for Next Check-in)

| Signal | Condition |
|---|---|
| Sleep debt moderate | >3.5hr over 7 days |
| HRV declining | 50-55ms |
| Sleep-deficit interaction | Sleep <7hr + active cut + HRV <55 |
| Sleep regularity | Bedtime stdev >60min |
| Unplanned surplus | Calories >130% of target |
| Late meal | Meal within 2hr of bedtime |

## Scheduled Messages (Morning Brief, Evening Check-in, Weekly Review)

These are now handled by the deterministic scheduler (Kiso API endpoints), NOT by this agent.
Do NOT send morning briefs, evening check-ins, or weekly reviews from HEARTBEAT.
The scheduler runs on a 30-minute cron and handles timezone-aware delivery for all users.

## Nudge Persistence

Track what's been nudged to avoid repetition:

- Don't send the same nudge twice in 24 hours
- If a warning persists for 3+ days, escalate language once, then back off
- Positive streaks: celebrate at 7, 14, 21 days — not every day
- Data freshness nudges: once per week max per metric

## Quiet Hours (HARD RULE)

**ZERO messages between 9:15 PM and 6:00 AM in the user's local timezone.** No exceptions. No "just this one alert." No system health checks. No evening wind-downs at 3 AM.

Before ANY message send, check: what time is it for THIS user? If it is between 9:15 PM and 6:00 AM in their timezone, DO NOT SEND. Period.

If a critical signal fires during quiet hours, do nothing. The morning brief will catch it. Waking someone up with a health alert defeats the purpose of health coaching.

System health check cron output during quiet hours: silently discard. Do not forward to any user at any time. Health checks are admin data, not user data.
